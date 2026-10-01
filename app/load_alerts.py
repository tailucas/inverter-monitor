#!/usr/bin/env python
"""Pure alert-evaluation logic for inverter load monitoring.

No I/O and no framework dependencies: the state machines here are evaluated
per inverter sample by the application threads and are fully unit-tested in
``tests/test_load_alerts.py``.
"""

from collections import deque
from dataclasses import dataclass, field

# manual switch-bank commands issued from the Telegram bot
SWITCH_COMMAND_START_LOAD_SHED = "start_load_shed"
SWITCH_COMMAND_END_LOAD_SHED = "end_load_shed"
# switch-bank reasons reported for the manual commands
REASON_MANUAL_LOAD_SHED = "manual_load_shed"
REASON_MANUAL_RESTORE = "manual_restore"


class CooldownLatch:
    """Latched trip condition with a self-extending cooldown.

    Trips while the evaluated value meets the condition (``> threshold``, or
    ``>= threshold`` when ``inclusive`` is set) and releases only after
    ``cooldown_secs`` have elapsed with no tripping sample.  Every tripping
    sample refreshes the cooldown (extends the release).
    """

    def __init__(
        self, threshold: float, cooldown_secs: float, inclusive: bool = False
    ) -> None:
        self.threshold = threshold
        self.cooldown_secs = cooldown_secs
        self.inclusive = inclusive
        self._active = False
        self._last_trip: float | None = None

    @property
    def active(self) -> bool:
        """Current latch state without consuming a sample."""
        return self._active

    def _trips(self, value: float) -> bool:
        if self.inclusive:
            return value >= self.threshold
        return value > self.threshold

    def update(self, value: float, now: float) -> bool:
        """Evaluate one sample; return the (possibly updated) state."""
        if self._trips(value):
            self._active = True
            self._last_trip = now
        elif (
            self._active
            and self._last_trip is not None
            and now - self._last_trip > self.cooldown_secs
        ):
            self._active = False
            self._last_trip = None
        return self._active

    def force(self, now: float) -> None:
        """Force the latched condition active and (re)start its cooldown."""
        self._active = True
        self._last_trip = now

    def reset(self) -> None:
        """Release the latch and clear its cooldown anchor."""
        self._active = False
        self._last_trip = None


def manual_switch_decision(
    command: str,
    load_shed_latch: CooldownLatch,
    overcast_latch: CooldownLatch,
    now: float,
) -> tuple[int, str]:
    """Apply a manual switch command to the cooldown latches.

    Returns the switch state and reason for the decision.  Force-starting a
    load shed restarts the cooldown clock, so the shed holds through the
    cooldown and is self-extended by any above-threshold sample.  Ending the
    load shed clears the cooldown latches so the restore takes effect now;
    a trip condition that is still live sheds again on the next sample.
    """
    if command == SWITCH_COMMAND_START_LOAD_SHED:
        load_shed_latch.force(now)
        return 0, REASON_MANUAL_LOAD_SHED
    if command == SWITCH_COMMAND_END_LOAD_SHED:
        load_shed_latch.reset()
        overcast_latch.reset()
        return 1, REASON_MANUAL_RESTORE
    raise ValueError(f"Unknown switch command: {command}")


class TimeWindowAverage:
    """Mean of samples retained within a rolling time window.

    Samples older than ``window_secs`` relative to the newest sample are
    discarded, so every sample contributes equally for exactly the window
    duration regardless of sampling rate.
    """

    def __init__(self, window_secs: float) -> None:
        self.window_secs = window_secs
        self._samples: deque[tuple[float, float]] = deque()

    def add(self, value: float, now: float) -> float:
        """Add a sample and return the mean over the retained window."""
        self._samples.append((now, value))
        cutoff = now - self.window_secs
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()
        total = 0.0
        for _, sample in self._samples:
            total += sample
        return total / len(self._samples)


# switch-rationing conditions; the values double as the switch-stats
# reason keys reported to the consumers
RATION_LOAD_SHED = "load_shed"
RATION_OVERCAST = "overcast"
RATION_SURPLUS = "surplus_ration"
RATION_BATTERY = "battery_ration"
RATIONING_CONDITIONS = (
    RATION_LOAD_SHED,
    RATION_OVERCAST,
    RATION_SURPLUS,
    RATION_BATTERY,
)


class WarnOnceTracker:
    """Warn once per trip episode, re-armed when the condition clears.

    Keyed per condition, so independent trips cannot mask each other and a
    condition that recovers (and trips again later) warns again.
    """

    def __init__(self) -> None:
        self._active: set[str] = set()

    def should_warn(self, condition: str, tripped: bool) -> bool:
        """Report whether this trip of ``condition`` still needs a warning."""
        if not tripped:
            self._active.discard(condition)
            return False
        if condition in self._active:
            return False
        self._active.add(condition)
        return True


@dataclass(frozen=True)
class SwitchConditionConfig:
    """Enablement and thresholds for the automatic switch conditions.

    A condition with its ``*_enabled`` flag false is still evaluated and
    reported as tripped (the switch stats stay truthful) but never changes
    the switch state; the decision lists it under ``suppressed`` so the
    caller can warn once per episode.
    """

    load_shed_enabled: bool = True
    overcast_enabled: bool = True
    surplus_ration_enabled: bool = True
    battery_ration_enabled: bool = True
    battery_low_pct: float = 45.0
    battery_critical_pct: float = 40.0
    battery_major_draw_w: float = 500.0
    # assuming CFE drop-out at 30 %
    grid_dropout_v: float = 90.0
    overcast_cloudiness_pct: float = 100.0
    # cloudiness older than this is treated as unknown
    cloudiness_stale_seconds: float = 180.0

    def enabled(self, condition: str) -> bool:
        """Report whether a rationing condition may control the switches."""
        return {
            RATION_LOAD_SHED: self.load_shed_enabled,
            RATION_OVERCAST: self.overcast_enabled,
            RATION_SURPLUS: self.surplus_ration_enabled,
            RATION_BATTERY: self.battery_ration_enabled,
        }[condition]


@dataclass(frozen=True)
class SwitchConditionDecision:
    """Outcome of one switch-rationing evaluation over an inverter sample."""

    switch_state: int
    reasons: dict[str, int]
    suppressed: list[str]
    alert_restore: bool
    load_missing: bool = False
    weather_stale: bool = False
    supporting: dict[str, float] = field(default_factory=dict)


def evaluate_switch_conditions(
    inverter_data: dict,
    now: float,
    load_shed_latch: CooldownLatch,
    overcast_latch: CooldownLatch,
    generation_average: TimeWindowAverage,
    cloudiness_pct: float | None,
    cloudiness_age_secs: float | None,
    config: SwitchConditionConfig,
    manual_shed: bool = False,
) -> SwitchConditionDecision:
    """Evaluate the automatic switch conditions for one inverter sample.

    Mirrors the thread's decision order: the overall-load latch has top
    priority, overcast rationing is next, an inverter alert restores the
    banks unless one of those latches is active, and the surplus/battery
    rationing checks follow.  The latches and the generation average are
    updated in place; ``reasons`` is the switch-stats mapping reported to
    the consumers.  A disabled condition is still reported as tripped but
    never forces ``switch_state`` to 0; it is listed in ``suppressed``
    instead.  A manual load shed acts even when its condition is disabled.
    """
    reasons = {condition: 0 for condition in RATIONING_CONDITIONS}
    # check 0 (top priority): overall load shedding with cooldown
    load_field = inverter_data.get("total_load_power_w")
    if isinstance(load_field, bool) or not isinstance(load_field, (int, float)):
        load_missing = True
        load_shed_active = load_shed_latch.active
    else:
        load_missing = False
        load_shed_active = load_shed_latch.update(float(load_field), now)
    if load_shed_active:
        reasons[RATION_LOAD_SHED] = 1
    # check 0b: overcast rationing; stale weather retains the latch
    weather_stale = False
    if (
        cloudiness_pct is not None
        and cloudiness_age_secs is not None
        and cloudiness_age_secs <= config.cloudiness_stale_seconds
    ):
        overcast_active = overcast_latch.update(cloudiness_pct, now)
    else:
        overcast_active = overcast_latch.active
        weather_stale = cloudiness_pct is not None
    if overcast_active:
        reasons[RATION_OVERCAST] = 1
    if (
        int(inverter_data["alert"]) == 1
        and not load_shed_active
        and not overcast_active
    ):
        # do not load shed during an alert condition; a latched high-load
        # or overcast condition takes priority over this guard
        return SwitchConditionDecision(
            switch_state=1,
            reasons=reasons,
            suppressed=[],
            alert_restore=True,
            load_missing=load_missing,
            weather_stale=weather_stale,
        )
    # check 1: disable switches if the battery is critically low without
    # adequate surplus (i.e. not charging from solar)
    pv1_power_w = float(inverter_data["pv1_power_w"])
    pv2_power_w = float(inverter_data["pv2_power_w"])
    battery_power_w = float(inverter_data["battery_power_w"])
    power_generation_w_avg = generation_average.add(
        pv1_power_w + pv2_power_w - battery_power_w, now
    )
    battery_soc_pct = float(inverter_data["battery_soc_pct"])
    if battery_soc_pct < config.battery_critical_pct and power_generation_w_avg < 0:
        reasons[RATION_SURPLUS] = 1
    # check 2: more conservative rationing if no grid backup (draw assumes
    # no surplus)
    grid_voltage = max(
        float(inverter_data["grid_voltage_l1_v"]),
        float(inverter_data["grid_voltage_l2_v"]),
    )
    if (
        battery_soc_pct < config.battery_low_pct
        and grid_voltage < config.grid_dropout_v
        and battery_power_w >= config.battery_major_draw_w
    ):
        reasons[RATION_BATTERY] = 1
    # check 3: the inverter is no longer pulling from solar or battery
    # (i.e. drawing from the grid)
    inverter_power_w = float(inverter_data["inverter_l1_power_w"]) + float(
        inverter_data["inverter_l2_power_w"]
    )
    if inverter_power_w < 0:
        reasons[RATION_BATTERY] = 1
    switch_state = 1
    suppressed: list[str] = []
    for condition in RATIONING_CONDITIONS:
        if not reasons[condition]:
            continue
        if condition == RATION_LOAD_SHED and manual_shed:
            switch_state = 0
        elif config.enabled(condition):
            switch_state = 0
        else:
            suppressed.append(condition)
    return SwitchConditionDecision(
        switch_state=switch_state,
        reasons=reasons,
        suppressed=suppressed,
        alert_restore=False,
        load_missing=load_missing,
        weather_stale=weather_stale,
        supporting={
            "inverter_power_w": inverter_power_w,
            "power_generation_w_avg": power_generation_w_avg,
            "pv1_power_w": pv1_power_w,
            "pv2_power_w": pv2_power_w,
            "battery_power_w": battery_power_w,
            "battery_soc_pct": battery_soc_pct,
            "grid_voltage_v": grid_voltage,
        },
    )


@dataclass
class LoadAlertDecision:
    """Actions requested by one load sample evaluation."""

    warning: bool = False
    recovery: bool = False
    pd_trigger: bool = False
    pd_resolve: bool = False


class LoadAlertEvaluator:
    """Threshold state machine over overall-load samples.

    - The Telegram warning trips once above ``warning_w``; recovery is emitted
      only after the load has stayed at or below the threshold for
      ``cooldown_secs`` (the same clock as the switch load-shed release), so a
      load hovering at the threshold cannot flap messages.
    - PagerDuty trips on a single sample above ``critical_w`` and resolves once
      the load has been at or below the threshold for ``resolve_secs``.
    - PagerDuty incident state is seeded so an incident left open by a previous
      run auto-resolves; a high sample at startup still triggers.
    - Failed actions are retried on the next sample unless the caller reports
      success via the ``*_succeeded`` hooks.
    """

    def __init__(
        self,
        warning_w: float,
        critical_w: float,
        resolve_secs: float,
        cooldown_secs: float,
    ) -> None:
        self.warning_w = warning_w
        self.critical_w = critical_w
        self.resolve_secs = resolve_secs
        self.cooldown_secs = cooldown_secs
        self._warning = CooldownLatch(threshold=warning_w, cooldown_secs=cooldown_secs)
        self._warning_sent = False
        self._pd_triggered = False
        self._pd_stale_pending = True
        self._below_since: float | None = None

    def evaluate(self, load_w: float, now: float) -> LoadAlertDecision:
        """Evaluate one load sample and return the requested actions."""
        decision = LoadAlertDecision()
        # Telegram warning / recovery (cooldown-aligned latch)
        warning_latched = self._warning.update(load_w, now)
        if warning_latched and not self._warning_sent:
            decision.warning = True
            self._warning_sent = True
        elif not warning_latched and self._warning_sent:
            decision.recovery = True
            self._warning_sent = False
        # PagerDuty high-load incident
        if load_w > self.critical_w:
            self._below_since = None
            if not self._pd_triggered:
                decision.pd_trigger = True
        else:
            if self._below_since is None:
                self._below_since = now
            elif (
                self._pd_triggered or self._pd_stale_pending
            ) and now - self._below_since > self.resolve_secs:
                decision.pd_resolve = True
        return decision

    # -- action acknowledgements (failures retry on the next sample) ----------

    def pd_trigger_succeeded(self) -> None:
        """Record that the high-load incident was raised (or deliberately skipped)."""
        self._pd_triggered = True
        self._pd_stale_pending = False

    def pd_trigger_failed(self) -> None:
        """Leave state unchanged so the next sample re-requests the trigger."""
        return

    def pd_resolve_succeeded(self) -> None:
        """Record that the incident was resolved (or deliberately skipped)."""
        self._pd_triggered = False
        self._pd_stale_pending = False
        self._below_since = None

    def pd_resolve_failed(self) -> None:
        """Leave state unchanged so the next sample re-requests the resolve."""
        return
