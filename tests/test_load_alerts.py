#!/usr/bin/env python
"""Unit tests for the pure load-alert state machines."""

from app.load_alerts import LoadAlertDecision, LoadAlertEvaluator, LoadShedLatch

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


class TestLoadShedLatch:
    """Load shed latch: trip, self-extending cooldown, release."""

    def test_inactive_until_threshold_exceeded(self) -> None:
        latch = LoadShedLatch(threshold_w=WARNING_W, cooldown_secs=COOLDOWN_SECS)
        assert latch.active is False
        # exactly at the threshold is not "above"
        assert latch.update(load_w=WARNING_W, now=0.0) is False
        assert latch.update(load_w=WARNING_W - 1, now=1.0) is False
        assert latch.active is False

    def test_trips_on_single_high_sample(self) -> None:
        latch = LoadShedLatch(threshold_w=WARNING_W, cooldown_secs=COOLDOWN_SECS)
        assert latch.update(load_w=WARNING_W + 1, now=0.0) is True
        assert latch.active is True

    def test_latches_through_cooldown(self) -> None:
        latch = LoadShedLatch(threshold_w=WARNING_W, cooldown_secs=COOLDOWN_SECS)
        latch.update(load_w=WARNING_W + 1, now=0.0)
        assert latch.update(load_w=WARNING_W - 100, now=COOLDOWN_SECS) is True
        assert latch.update(load_w=WARNING_W - 100, now=COOLDOWN_SECS + 1) is False
        assert latch.active is False

    def test_continued_high_samples_extend_cooldown(self) -> None:
        latch = LoadShedLatch(threshold_w=WARNING_W, cooldown_secs=COOLDOWN_SECS)
        assert latch.update(load_w=WARNING_W + 1, now=0.0) is True
        # continued evaluation of the condition extends the cooldown
        assert latch.update(load_w=WARNING_W + 1, now=400.0) is True
        # 500 s since the last high sample: still latched
        assert latch.update(load_w=WARNING_W - 1, now=900.0) is True
        # more than the cooldown since the last high sample: released
        assert latch.update(load_w=WARNING_W - 1, now=1001.0) is False


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
