#!/usr/bin/env python
"""Pure alert-evaluation logic for inverter load monitoring.

No I/O and no framework dependencies: the state machines here are evaluated
per inverter sample by the application threads and are fully unit-tested in
``tests/test_load_alerts.py``.
"""

from dataclasses import dataclass


class LoadShedLatch:
    """Latched high-load trip with a self-extending cooldown.

    Trips while ``load_w`` is above ``threshold_w`` and releases only after
    ``cooldown_secs`` have elapsed with no above-threshold sample.  Every
    above-threshold sample refreshes the cooldown (extends the release).
    """

    def __init__(self, threshold_w: float, cooldown_secs: float) -> None:
        self.threshold_w = threshold_w
        self.cooldown_secs = cooldown_secs
        self._active = False
        self._last_above: float | None = None

    @property
    def active(self) -> bool:
        """Current latch state without consuming a sample."""
        return self._active

    def update(self, load_w: float, now: float) -> bool:
        """Evaluate one load sample; return the (possibly updated) state."""
        if load_w > self.threshold_w:
            self._active = True
            self._last_above = now
        elif (
            self._active
            and self._last_above is not None
            and now - self._last_above > self.cooldown_secs
        ):
            self._active = False
            self._last_above = None
        return self._active


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
        self._warning = LoadShedLatch(
            threshold_w=warning_w, cooldown_secs=cooldown_secs
        )
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
