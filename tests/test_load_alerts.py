#!/usr/bin/env python
"""Unit tests for the pure load-alert state machines."""

from typing import Any

import pytest

from app.load_alerts import (
    RATION_BATTERY,
    RATION_LOAD_SHED,
    RATION_OVERCAST,
    RATION_SURPLUS,
    RATIONING_CONDITIONS,
    REASON_MANUAL_LOAD_SHED,
    REASON_MANUAL_RESTORE,
    SWITCH_COMMAND_END_LOAD_SHED,
    SWITCH_COMMAND_START_LOAD_SHED,
    CooldownLatch,
    LoadAlertDecision,
    LoadAlertEvaluator,
    SwitchConditionConfig,
    SwitchConditionDecision,
    TimeWindowAverage,
    WarnOnceTracker,
    evaluate_switch_conditions,
    manual_switch_decision,
)

WARNING_W = 7000.0
CRITICAL_W = 7500.0
COOLDOWN_SECS = 600.0
RESOLVE_SECS = 60.0


def make_evaluator() -> LoadAlertEvaluator:
    """Build an evaluator with the default production thresholds."""
    return LoadAlertEvaluator(
        warning_w=WARNING_W,
        critical_w=CRITICAL_W,
        resolve_secs=RESOLVE_SECS,
        cooldown_secs=COOLDOWN_SECS,
    )


class TestCooldownLatch:
    """Cooldown latch: trip, self-extending cooldown, release."""

    def test_inactive_until_threshold_exceeded(self) -> None:
        latch = CooldownLatch(threshold=WARNING_W, cooldown_secs=COOLDOWN_SECS)
        assert latch.active is False
        # exactly at the threshold is not "above"
        assert latch.update(value=WARNING_W, now=0.0) is False
        assert latch.update(value=WARNING_W - 1, now=1.0) is False
        assert latch.active is False

    def test_inclusive_threshold_trips_at_boundary(self) -> None:
        latch = CooldownLatch(
            threshold=100.0, cooldown_secs=COOLDOWN_SECS, inclusive=True
        )
        assert latch.update(value=99.0, now=0.0) is False
        assert latch.update(value=100.0, now=1.0) is True
        assert latch.active is True

    def test_trips_on_single_high_sample(self) -> None:
        latch = CooldownLatch(threshold=WARNING_W, cooldown_secs=COOLDOWN_SECS)
        assert latch.update(value=WARNING_W + 1, now=0.0) is True
        assert latch.active is True

    def test_latches_through_cooldown(self) -> None:
        latch = CooldownLatch(threshold=WARNING_W, cooldown_secs=COOLDOWN_SECS)
        latch.update(value=WARNING_W + 1, now=0.0)
        assert latch.update(value=WARNING_W - 100, now=COOLDOWN_SECS) is True
        assert latch.update(value=WARNING_W - 100, now=COOLDOWN_SECS + 1) is False
        assert latch.active is False

    def test_continued_high_samples_extend_cooldown(self) -> None:
        latch = CooldownLatch(threshold=WARNING_W, cooldown_secs=COOLDOWN_SECS)
        assert latch.update(value=WARNING_W + 1, now=0.0) is True
        # continued evaluation of the condition extends the cooldown
        assert latch.update(value=WARNING_W + 1, now=400.0) is True
        # 500 s since the last high sample: still latched
        assert latch.update(value=WARNING_W - 1, now=900.0) is True
        # more than the cooldown since the last high sample: released
        assert latch.update(value=WARNING_W - 1, now=1001.0) is False


class TestTimeWindowAverage:
    """Rolling time-window average used for the surplus decision."""

    def test_single_sample(self) -> None:
        average = TimeWindowAverage(window_secs=300.0)
        assert average.add(value=1200.0, now=0.0) == 1200.0

    def test_mean_of_retained_samples(self) -> None:
        average = TimeWindowAverage(window_secs=300.0)
        average.add(value=1000.0, now=0.0)
        average.add(value=2000.0, now=10.0)
        assert average.add(value=3000.0, now=20.0) == 2000.0

    def test_expired_samples_are_discarded(self) -> None:
        average = TimeWindowAverage(window_secs=300.0)
        average.add(value=1000.0, now=0.0)
        average.add(value=5000.0, now=100.0)
        # the t=0 sample falls outside the 300 s window at t=400
        assert average.add(value=3000.0, now=400.0) == 4000.0

    def test_window_slides_with_newest_sample(self) -> None:
        average = TimeWindowAverage(window_secs=300.0)
        average.add(value=100.0, now=0.0)
        average.add(value=200.0, now=250.0)
        # at t=500 only the t=250 and t=500 samples remain
        assert average.add(value=300.0, now=500.0) == 250.0


class TestLoadAlertEvaluator:
    """Warning/recovery latch and PagerDuty lifecycle decisions."""

    def test_warning_trips_once_then_recovers_after_cooldown(self) -> None:
        evaluator = make_evaluator()
        first = evaluator.evaluate(load_w=WARNING_W + 1, now=0.0)
        assert isinstance(first, LoadAlertDecision)
        assert first.warning is True
        assert evaluator.evaluate(load_w=WARNING_W + 1, now=60.0).warning is False
        # still inside the cooldown window after the last high sample (t=60)
        assert evaluator.evaluate(load_w=WARNING_W - 1, now=600.0).recovery is False
        assert evaluator.evaluate(load_w=WARNING_W - 1, now=661.0).recovery is True

    def test_no_warning_at_boundary(self) -> None:
        evaluator = make_evaluator()
        assert evaluator.evaluate(load_w=WARNING_W, now=0.0).warning is False
        assert evaluator.evaluate(load_w=WARNING_W - 1, now=1.0).warning is False

    def test_warning_rearms_after_recovery(self) -> None:
        evaluator = make_evaluator()
        assert evaluator.evaluate(load_w=WARNING_W + 1, now=0.0).warning is True
        assert evaluator.evaluate(load_w=WARNING_W - 1, now=1000.0).recovery is True
        assert evaluator.evaluate(load_w=WARNING_W + 1, now=1100.0).warning is True

    def test_pd_triggers_on_single_sample_above_critical(self) -> None:
        evaluator = make_evaluator()
        decision = evaluator.evaluate(load_w=CRITICAL_W + 1, now=0.0)
        assert decision.pd_trigger is True
        assert decision.warning is True
        evaluator.pd_trigger_succeeded()
        assert evaluator.evaluate(load_w=CRITICAL_W + 1, now=60.0).pd_trigger is False

    def test_pd_boundary_does_not_trigger(self) -> None:
        evaluator = make_evaluator()
        assert evaluator.evaluate(load_w=CRITICAL_W, now=0.0).pd_trigger is False

    def test_pd_resolves_only_after_resolve_window(self) -> None:
        evaluator = make_evaluator()
        evaluator.evaluate(load_w=CRITICAL_W + 10, now=0.0)
        evaluator.pd_trigger_succeeded()
        # first below-threshold sample starts the resolve clock
        assert evaluator.evaluate(load_w=CRITICAL_W - 1, now=60.0).pd_resolve is False
        assert evaluator.evaluate(load_w=CRITICAL_W - 1, now=121.0).pd_resolve is True
        evaluator.pd_resolve_succeeded()
        assert evaluator.evaluate(load_w=CRITICAL_W - 1, now=200.0).pd_resolve is False

    def test_stale_incident_resolves_without_trigger(self) -> None:
        evaluator = make_evaluator()
        assert evaluator.evaluate(load_w=CRITICAL_W - 1, now=0.0).pd_resolve is False
        assert evaluator.evaluate(load_w=CRITICAL_W - 1, now=61.0).pd_resolve is True

    def test_failed_trigger_is_retried(self) -> None:
        evaluator = make_evaluator()
        assert evaluator.evaluate(load_w=CRITICAL_W + 1, now=0.0).pd_trigger is True
        evaluator.pd_trigger_failed()
        assert evaluator.evaluate(load_w=CRITICAL_W + 1, now=60.0).pd_trigger is True
        evaluator.pd_trigger_succeeded()
        assert evaluator.evaluate(load_w=CRITICAL_W + 1, now=120.0).pd_trigger is False

    def test_failed_resolve_is_retried(self) -> None:
        evaluator = make_evaluator()
        evaluator.evaluate(load_w=CRITICAL_W + 1, now=0.0)
        evaluator.pd_trigger_succeeded()
        # first below-threshold sample starts the resolve clock
        assert evaluator.evaluate(load_w=CRITICAL_W - 1, now=61.0).pd_resolve is False
        assert evaluator.evaluate(load_w=CRITICAL_W - 1, now=122.0).pd_resolve is True
        evaluator.pd_resolve_failed()
        assert evaluator.evaluate(load_w=CRITICAL_W - 1, now=183.0).pd_resolve is True


class TestManualSwitchDecision:
    """Force/reset support for the manual load-shed bot commands."""

    @staticmethod
    def _latches() -> tuple[CooldownLatch, CooldownLatch]:
        load_shed = CooldownLatch(threshold=WARNING_W, cooldown_secs=COOLDOWN_SECS)
        overcast = CooldownLatch(threshold=100.0, cooldown_secs=3600.0, inclusive=True)
        return load_shed, overcast

    def test_force_holds_through_the_cooldown(self) -> None:
        latch = CooldownLatch(threshold=WARNING_W, cooldown_secs=COOLDOWN_SECS)
        latch.force(now=0.0)
        assert latch.active is True
        # a low sample keeps a forced latch through its cooldown
        assert latch.update(value=WARNING_W - 1, now=COOLDOWN_SECS) is True
        assert latch.update(value=WARNING_W - 1, now=COOLDOWN_SECS + 1) is False

    def test_force_is_self_extended_by_high_samples(self) -> None:
        latch = CooldownLatch(threshold=WARNING_W, cooldown_secs=COOLDOWN_SECS)
        latch.force(now=0.0)
        assert latch.update(value=WARNING_W + 1, now=COOLDOWN_SECS - 1) is True
        assert latch.update(value=WARNING_W - 1, now=COOLDOWN_SECS + 1) is True
        assert latch.update(value=WARNING_W - 1, now=2 * COOLDOWN_SECS + 1) is False

    def test_reset_releases_now_and_stays_released(self) -> None:
        latch = CooldownLatch(threshold=WARNING_W, cooldown_secs=COOLDOWN_SECS)
        latch.update(value=WARNING_W + 1, now=0.0)
        latch.reset()
        assert latch.active is False
        assert latch.update(value=WARNING_W - 1, now=1.0) is False
        # a later tripping sample re-arms the latch
        assert latch.update(value=WARNING_W + 1, now=2.0) is True

    def test_start_load_shed_forces_the_load_shed_latch(self) -> None:
        load_shed, overcast = self._latches()
        decision = manual_switch_decision(
            command=SWITCH_COMMAND_START_LOAD_SHED,
            load_shed_latch=load_shed,
            overcast_latch=overcast,
            now=10.0,
        )
        assert decision == (0, REASON_MANUAL_LOAD_SHED)
        assert load_shed.active is True
        assert load_shed.update(value=WARNING_W - 1, now=10.0 + COOLDOWN_SECS) is True
        # a forced load shed is not an overcast event
        assert overcast.active is False

    def test_end_load_shed_resets_both_cooldown_latches(self) -> None:
        load_shed, overcast = self._latches()
        load_shed.force(now=0.0)
        overcast.force(now=0.0)
        decision = manual_switch_decision(
            command=SWITCH_COMMAND_END_LOAD_SHED,
            load_shed_latch=load_shed,
            overcast_latch=overcast,
            now=5.0,
        )
        assert decision == (1, REASON_MANUAL_RESTORE)
        assert load_shed.active is False
        assert overcast.active is False

    def test_unknown_command_raises(self) -> None:
        load_shed, overcast = self._latches()
        with pytest.raises(ValueError):
            manual_switch_decision(
                command="unknown",
                load_shed_latch=load_shed,
                overcast_latch=overcast,
                now=0.0,
            )


def make_switch_sample(**overrides: Any) -> dict:
    """A plausible inverter sample for the switch-condition evaluator."""
    sample: dict[str, Any] = {
        "alert": 0,
        "total_load_power_w": 1000.0,
        "battery_soc_pct": 80.0,
        "battery_power_w": 0.0,
        "pv1_power_w": 2000.0,
        "pv2_power_w": 1500.0,
        "grid_voltage_l1_v": 230.0,
        "grid_voltage_l2_v": 230.0,
        "inverter_l1_power_w": 500.0,
        "inverter_l2_power_w": 0.0,
    }
    sample.update(overrides)
    return sample


def evaluate_sample(
    sample: dict | None = None,
    config: SwitchConditionConfig | None = None,
    load_shed: CooldownLatch | None = None,
    overcast: CooldownLatch | None = None,
    average: TimeWindowAverage | None = None,
    cloudiness_pct: float | None = None,
    cloudiness_age_secs: float | None = None,
    manual_shed: bool = False,
    now: float = 0.0,
) -> SwitchConditionDecision:
    """Run the pure evaluator with fake-clock inputs."""
    if sample is None:
        sample = make_switch_sample()
    if config is None:
        config = SwitchConditionConfig()
    if load_shed is None:
        load_shed = CooldownLatch(threshold=WARNING_W, cooldown_secs=COOLDOWN_SECS)
    if overcast is None:
        overcast = CooldownLatch(threshold=100.0, cooldown_secs=3600.0, inclusive=True)
    if average is None:
        average = TimeWindowAverage(window_secs=300.0)
    return evaluate_switch_conditions(
        inverter_data=sample,
        now=now,
        load_shed_latch=load_shed,
        overcast_latch=overcast,
        generation_average=average,
        cloudiness_pct=cloudiness_pct,
        cloudiness_age_secs=cloudiness_age_secs,
        config=config,
        manual_shed=manual_shed,
    )


class TestWarnOnceTracker:
    """Warn once per trip episode, re-armed when the condition clears."""

    def test_warns_once_per_episode_and_rearms_after_recovery(self) -> None:
        tracker = WarnOnceTracker()
        assert tracker.should_warn("overcast", True) is True
        assert tracker.should_warn("overcast", True) is False
        # recovery re-arms the warning for the next episode
        assert tracker.should_warn("overcast", False) is False
        assert tracker.should_warn("overcast", True) is True

    def test_conditions_are_tracked_independently(self) -> None:
        tracker = WarnOnceTracker()
        assert tracker.should_warn("load_shed", True) is True
        assert tracker.should_warn("surplus_ration", True) is True
        assert tracker.should_warn("load_shed", True) is False
        assert tracker.should_warn("surplus_ration", False) is False
        assert tracker.should_warn("surplus_ration", True) is True


class TestEvaluateSwitchConditions:
    """The pure per-sample decision behind the switch-bank changes."""

    def test_default_config_enables_every_condition(self) -> None:
        config = SwitchConditionConfig()
        assert all(config.enabled(condition) for condition in RATIONING_CONDITIONS)

    def test_high_load_trips_the_load_shed_latch(self) -> None:
        decision = evaluate_sample(make_switch_sample(total_load_power_w=WARNING_W + 1))
        assert decision.switch_state == 0
        assert decision.reasons[RATION_LOAD_SHED] == 1
        assert decision.suppressed == []
        assert decision.alert_restore is False

    def test_warn_only_load_shed_reports_without_shedding(self) -> None:
        decision = evaluate_sample(
            make_switch_sample(total_load_power_w=WARNING_W + 1),
            SwitchConditionConfig(load_shed_enabled=False),
        )
        assert decision.switch_state == 1
        assert decision.reasons[RATION_LOAD_SHED] == 1
        assert decision.suppressed == [RATION_LOAD_SHED]

    def test_manual_shed_acts_when_load_shed_is_warn_only(self) -> None:
        decision = evaluate_sample(
            make_switch_sample(total_load_power_w=WARNING_W + 1),
            SwitchConditionConfig(load_shed_enabled=False),
            manual_shed=True,
        )
        assert decision.switch_state == 0
        assert decision.suppressed == []

    def test_missing_load_retains_the_latch(self) -> None:
        latch = CooldownLatch(threshold=WARNING_W, cooldown_secs=COOLDOWN_SECS)
        latch.force(now=0.0)
        decision = evaluate_sample(
            make_switch_sample(total_load_power_w=None),
            load_shed=latch,
        )
        assert decision.load_missing is True
        assert decision.switch_state == 0
        assert decision.reasons[RATION_LOAD_SHED] == 1

    def test_overcast_trips_on_full_cloudiness(self) -> None:
        decision = evaluate_sample(cloudiness_pct=100.0, cloudiness_age_secs=10.0)
        assert decision.switch_state == 0
        assert decision.reasons[RATION_OVERCAST] == 1
        assert decision.suppressed == []

    def test_warn_only_overcast_reports_without_shedding(self) -> None:
        decision = evaluate_sample(
            cloudiness_pct=100.0,
            cloudiness_age_secs=10.0,
            config=SwitchConditionConfig(overcast_enabled=False),
        )
        assert decision.switch_state == 1
        assert decision.reasons[RATION_OVERCAST] == 1
        assert decision.suppressed == [RATION_OVERCAST]

    def test_stale_weather_retains_the_overcast_latch(self) -> None:
        overcast = CooldownLatch(threshold=100.0, cooldown_secs=3600.0, inclusive=True)
        evaluate_sample(
            cloudiness_pct=100.0,
            cloudiness_age_secs=10.0,
            overcast=overcast,
            now=0.0,
        )
        decision = evaluate_sample(
            cloudiness_pct=100.0,
            cloudiness_age_secs=200.0,
            overcast=overcast,
            now=200.0,
        )
        assert decision.weather_stale is True
        assert decision.switch_state == 0
        assert decision.reasons[RATION_OVERCAST] == 1

    def test_surplus_ration_trips_and_warn_only_suppresses(self) -> None:
        sample = make_switch_sample(
            battery_soc_pct=39.0,
            pv1_power_w=0.0,
            pv2_power_w=0.0,
            battery_power_w=100.0,
        )
        decision = evaluate_sample(sample)
        assert decision.switch_state == 0
        assert decision.reasons[RATION_SURPLUS] == 1
        warn_only = evaluate_sample(
            sample, SwitchConditionConfig(surplus_ration_enabled=False)
        )
        assert warn_only.switch_state == 1
        assert warn_only.suppressed == [RATION_SURPLUS]

    def test_battery_ration_trips_on_low_soc_without_grid(self) -> None:
        sample = make_switch_sample(
            battery_soc_pct=44.0,
            grid_voltage_l1_v=80.0,
            grid_voltage_l2_v=80.0,
            battery_power_w=600.0,
        )
        decision = evaluate_sample(sample)
        assert decision.switch_state == 0
        assert decision.reasons[RATION_BATTERY] == 1
        warn_only = evaluate_sample(
            sample, SwitchConditionConfig(battery_ration_enabled=False)
        )
        assert warn_only.switch_state == 1
        assert warn_only.suppressed == [RATION_BATTERY]

    def test_battery_ration_trips_when_drawing_from_grid(self) -> None:
        sample = make_switch_sample(inverter_l1_power_w=-50.0)
        decision = evaluate_sample(sample)
        assert decision.switch_state == 0
        assert decision.reasons[RATION_BATTERY] == 1

    def test_inverter_alert_restores_unless_a_latch_is_active(self) -> None:
        decision = evaluate_sample(make_switch_sample(alert=1))
        assert decision.alert_restore is True
        assert decision.switch_state == 1
        assert not any(decision.reasons.values())
        # a latched high-load condition takes priority over the guard
        latched = evaluate_sample(
            make_switch_sample(alert=1, total_load_power_w=WARNING_W + 1)
        )
        assert latched.alert_restore is False
        assert latched.switch_state == 0
