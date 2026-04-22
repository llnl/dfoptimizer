import pytest

from dfoptimizer.planner.planner import Planner
from dfoptimizer.planner.cooldown import CooldownTracker, CooldownResult
from dfoptimizer.types import DiagnosisFindingMsg, KnobDef, KnobResponse


def _make_finding(**overrides):
    payload = {
        "finding_type": "fetch_pressure",
        "motif": "persistent_pressure",
        "severity": "high",
        "severity_score": 0.72,
        "confidence": 0.9,
        "prevalence": 1.0,
        "persistence": 4,
        "trend_direction": "stable",
        "contributing_facts": [("fetch_pressure", "reader_posix:epoch")],
        "recommendation_bundle": "input_pipeline_tuning",
        "opportunity_tags": ["reader_parallelism"],
        "suppresses_tags": [],
        "summary": "fetch_pressure(reader_posix:epoch)",
        "scope": "reader_posix:epoch",
        "layer": "reader_posix",
        "support_windows": 4,
        "last_seen_window": 3,
        "window_index": 3,
        "publish_mode": "control",
    }
    payload.update(overrides)
    return DiagnosisFindingMsg(**payload)


def _make_planner(knob_id="dlio.read_threads", default=0, range=(0, 16),
                  tag="reader_parallelism", **response_kw):
    """Create a planner with one registered knob."""
    response_defaults = dict(
        direction="increase", step=1,
        min_severity=0.5, min_persistence=2, cooldown_windows=3,
    )
    response_defaults.update(response_kw)
    planner = Planner()
    planner.register_knobs({
        knob_id: KnobDef(
            id=knob_id, default=default, type=int, range=range,
            target_function="make_loader",
            responds_to={tag: KnobResponse(**response_defaults)},
        ),
    })
    return planner


# ── Gate 1: Trend filter ──

class TestTrendGate:
    def test_blocks_improving(self):
        planner = _make_planner()
        finding = _make_finding(trend_direction="improving")
        assert planner.process_finding(finding) == []

    def test_passes_stable(self):
        planner = _make_planner()
        finding = _make_finding(trend_direction="stable")
        plans = planner.process_finding(finding)
        assert len(plans) == 1

    def test_passes_worsening(self):
        planner = _make_planner()
        finding = _make_finding(trend_direction="worsening")
        plans = planner.process_finding(finding)
        assert len(plans) == 1

    def test_passes_insufficient_data(self):
        planner = _make_planner()
        finding = _make_finding(trend_direction="insufficient_data")
        plans = planner.process_finding(finding)
        assert len(plans) == 1


# ── Gate 2: Severity gate ──

class TestSeverityGate:
    def test_uses_continuous_score_not_label(self):
        """Severity gate uses the continuous score, not the label."""
        planner = _make_planner(min_severity=0.6)
        # Label says "high" but continuous score is 0.55 — should be blocked
        finding = _make_finding(severity="high", severity_score=0.55)
        assert planner.process_finding(finding) == []

    def test_blocks_below_threshold(self):
        planner = _make_planner(min_severity=0.5)
        finding = _make_finding(severity_score=0.3)
        assert planner.process_finding(finding) == []

    def test_passes_at_threshold(self):
        planner = _make_planner(min_severity=0.5)
        finding = _make_finding(severity_score=0.5)
        plans = planner.process_finding(finding)
        assert len(plans) == 1

    def test_passes_above_threshold(self):
        planner = _make_planner(min_severity=0.5)
        finding = _make_finding(severity_score=0.8)
        plans = planner.process_finding(finding)
        assert len(plans) == 1

    def test_continuous_score_affects_delta(self):
        """Higher severity should produce larger delta via _scaled_delta."""
        planner_low = _make_planner(min_severity=0.3, step=2)
        planner_high = _make_planner(min_severity=0.3, step=2)

        plans_low = planner_low.process_finding(
            _make_finding(severity_score=0.4, trend_direction="worsening")
        )
        plans_high = planner_high.process_finding(
            _make_finding(severity_score=0.9, trend_direction="worsening")
        )

        assert len(plans_low) == 1 and len(plans_high) == 1
        # Higher severity → larger new_value
        assert plans_high[0].new_value >= plans_low[0].new_value


# ── Gate 3: Persistence gate ──

class TestPersistenceGate:
    def test_blocks_insufficient(self):
        planner = _make_planner(min_persistence=3)
        finding = _make_finding(persistence=2)
        assert planner.process_finding(finding) == []

    def test_passes_at_threshold(self):
        planner = _make_planner(min_persistence=2)
        finding = _make_finding(persistence=2)
        plans = planner.process_finding(finding)
        assert len(plans) == 1

    def test_warmup_filtered_by_persistence(self):
        """Warmup transients naturally fail persistence without a motif gate."""
        planner = _make_planner(min_persistence=2)
        # Warmup spike: persistence=1, high severity
        finding = _make_finding(
            motif="warmup_transient", persistence=1, severity_score=0.9,
        )
        assert planner.process_finding(finding) == []


# ── Gate 4: Cooldown ──

class TestCooldownGate:
    def test_time_cooldown_blocks(self):
        tracker = CooldownTracker()
        tracker.record("knob_a", window_index=5, severity_score=0.7)
        result = tracker.check("knob_a", current_window=6, cooldown_windows=3)
        assert result.blocked
        assert result.reason == "time"

    def test_time_cooldown_passes_after_period(self):
        tracker = CooldownTracker()
        tracker.record("knob_a", window_index=5, severity_score=0.7)
        result = tracker.check("knob_a", current_window=8, cooldown_windows=3)
        # Effectiveness check: needs current_severity
        assert not result.blocked

    def test_effectiveness_blocks_when_no_improvement(self):
        tracker = CooldownTracker()
        tracker.record("knob_a", window_index=5, severity_score=0.7)
        # After cooldown, severity still 0.7 — no improvement
        result = tracker.check(
            "knob_a", current_window=8, cooldown_windows=3,
            current_severity=0.7, effectiveness_threshold=0.10,
        )
        assert result.blocked
        assert result.reason == "ineffective"

    def test_effectiveness_passes_when_improved(self):
        tracker = CooldownTracker()
        tracker.record("knob_a", window_index=5, severity_score=0.8)
        # After cooldown, severity dropped to 0.45 (43.7% improvement)
        result = tracker.check(
            "knob_a", current_window=8, cooldown_windows=3,
            current_severity=0.45, effectiveness_threshold=0.10,
        )
        assert not result.blocked

    def test_effectiveness_threshold_boundary(self):
        tracker = CooldownTracker()
        tracker.record("knob_a", window_index=5, severity_score=0.80)
        # Exactly 10% improvement: (0.80 - 0.72) / 0.80 = 0.10
        result = tracker.check(
            "knob_a", current_window=8, cooldown_windows=3,
            current_severity=0.72, effectiveness_threshold=0.10,
        )
        assert not result.blocked  # 10% meets threshold

    def test_per_node_isolation(self):
        tracker = CooldownTracker()
        tracker.record("knob_a", window_index=5, severity_score=0.7, node="node1")
        # node2 has no record — should not be blocked
        result = tracker.check(
            "knob_a", current_window=6, cooldown_windows=3, node="node2",
        )
        assert not result.blocked

    def test_no_record_passes(self):
        tracker = CooldownTracker()
        result = tracker.check("knob_a", current_window=10, cooldown_windows=3)
        assert not result.blocked

    def test_legacy_in_cooldown_api(self):
        tracker = CooldownTracker()
        tracker.record("knob_a", window_index=5, severity_score=0.7)
        assert tracker.in_cooldown("knob_a", 6, 3) is True
        assert tracker.in_cooldown("knob_a", 8, 3) is False


# ── Amdahl evidence fix ──

class TestEvidenceSaturation:
    def test_amdahl_stops_when_overprovisioned(self):
        """When Amdahl target <= current, don't increase."""
        planner = _make_planner(
            default=24, range=(0, 32), step=1, step_mode="evidence",
            tag="reader_parallelism",
        )
        planner.current_values["dlio.read_threads"] = 24
        finding = _make_finding(
            severity_score=0.7,
            key_metrics={
                "fetch_iter_time_frac_parent": 0.3,
                "compute_time_frac_parent": 0.7,
            },
        )
        plans = planner.process_finding(finding)
        # Amdahl target = 24 * (0.3/0.7) = 10 < 24 → no increase
        assert plans == []

    def test_amdahl_increases_when_justified(self):
        """When Amdahl target > current, allow increase."""
        planner = _make_planner(
            default=1, step=1, step_mode="evidence",
            tag="reader_parallelism",
        )
        planner.current_values["dlio.read_threads"] = 1
        finding = _make_finding(
            severity_score=0.7,
            key_metrics={
                "fetch_iter_time_frac_parent": 0.8,
                "compute_time_frac_parent": 0.2,
            },
        )
        plans = planner.process_finding(finding)
        assert len(plans) == 1
        # Amdahl target = 1 * (0.8/0.2) = 4
        assert plans[0].new_value == 4


# ── Decrease / contention ──

class TestDecrease:
    def _make_contention_planner(self):
        """Planner with read_threads that responds to both increase and decrease."""
        planner = Planner()
        planner.register_knobs({
            "dlio.read_threads": KnobDef(
                id="dlio.read_threads", default=0, type=int, range=(0, 16),
                target_function="make_loader",
                responds_to={
                    "reader_parallelism": KnobResponse(
                        direction="increase", step=1, step_mode="evidence",
                        min_severity=0.5, min_persistence=1, cooldown_windows=2,
                    ),
                    "reader_contention": KnobResponse(
                        direction="decrease", step=1,
                        min_severity=0.5, min_persistence=2, cooldown_windows=3,
                    ),
                },
            ),
        })
        return planner

    def test_contention_decreases_workers(self):
        """io_contention finding with reader_contention tag triggers decrease."""
        planner = self._make_contention_planner()
        planner.current_values["dlio.read_threads"] = 4

        finding = _make_finding(
            finding_type="io_contention",
            opportunity_tags=["reader_contention"],
            severity_score=0.7,
            persistence=2,
        )
        plans = planner.process_finding(finding)
        assert len(plans) == 1
        assert plans[0].new_value == 3  # decreased from 4

    def test_contention_requires_persistence(self):
        """Decrease requires min_persistence=2, single window is not enough."""
        planner = self._make_contention_planner()
        planner.current_values["dlio.read_threads"] = 4

        finding = _make_finding(
            finding_type="io_contention",
            opportunity_tags=["reader_contention"],
            severity_score=0.7,
            persistence=1,  # below threshold of 2
        )
        assert planner.process_finding(finding) == []

    def test_contention_stops_at_minimum(self):
        """Decrease can't go below range minimum (0)."""
        planner = self._make_contention_planner()
        planner.current_values["dlio.read_threads"] = 1

        finding = _make_finding(
            finding_type="io_contention",
            opportunity_tags=["reader_contention"],
            severity_score=0.7,
            persistence=2,
        )
        plans = planner.process_finding(finding)
        assert len(plans) == 1
        assert plans[0].new_value == 0  # clamped to range minimum

    def test_evidence_decrease_uses_amdahl(self):
        """With step_mode=evidence and direction=decrease, Amdahl computes target."""
        planner = Planner()
        planner.register_knobs({
            "dlio.read_threads": KnobDef(
                id="dlio.read_threads", default=0, type=int, range=(0, 16),
                target_function="make_loader",
                responds_to={
                    "reader_contention": KnobResponse(
                        direction="decrease", step_mode="evidence",
                        min_severity=0.5, min_persistence=1, cooldown_windows=2,
                    ),
                },
            ),
        })
        planner.current_values["dlio.read_threads"] = 8

        # compute_frac=0.8, fetch_frac=0.1 → over-provisioned
        finding = _make_finding(
            finding_type="io_contention",
            opportunity_tags=["reader_contention"],
            severity_score=0.7,
            key_metrics={
                "fetch_iter_time_frac_parent": 0.1,
                "compute_time_frac_parent": 0.8,
            },
        )
        plans = planner.process_finding(finding)
        assert len(plans) == 1
        # Amdahl: 8 * (0.1/0.8) = 1
        assert plans[0].new_value == 1

    def test_increase_then_decrease_full_cycle(self):
        """Full cycle: increase workers, detect contention, decrease back."""
        planner = self._make_contention_planner()

        # Phase 1: fetch_pressure → increase T=0→1
        plans1 = planner.process_finding(_make_finding(
            finding_type="fetch_pressure",
            opportunity_tags=["reader_parallelism"],
            severity_score=0.8, persistence=1, window_index=0,
            trend_direction="worsening",
            key_metrics={"fetch_iter_time_frac_parent": 0.7, "compute_time_frac_parent": 0.3},
        ))
        assert len(plans1) == 1
        assert plans1[0].new_value > 0

        # ACK
        planner.apply_ack(
            plans1[0].plan_id, "dlio.read_threads", "applied",
            old_value=0, new_value=plans1[0].new_value, window_index=1,
        )

        # Phase 2: io_contention → decrease
        plans2 = planner.process_finding(_make_finding(
            finding_type="io_contention",
            opportunity_tags=["reader_contention"],
            severity_score=0.7, persistence=2, window_index=5,
        ))
        assert len(plans2) == 1
        assert plans2[0].new_value < plans1[0].new_value


# ── Full pipeline integration ──

class TestFullPipeline:
    def test_workers_stop_increasing(self):
        """The full regression scenario: effectiveness check stops escalation."""
        planner = _make_planner(
            default=1, cooldown_windows=2, min_persistence=1,
            min_severity=0.3, effectiveness_threshold=0.10,
        )

        # Window 0: initial finding, plan T=1→2
        finding0 = _make_finding(
            window_index=0, persistence=1, severity_score=0.8,
            trend_direction="worsening",
        )
        plans0 = planner.process_finding(finding0)
        assert len(plans0) == 1
        assert plans0[0].new_value == 2

        # ACK at window 1
        planner.apply_ack(
            plans0[0].plan_id, "dlio.read_threads", "applied",
            old_value=1, new_value=2, window_index=1,
        )

        # Window 3 (after cooldown=2): severity dropped 0.8→0.45 — effective
        finding3 = _make_finding(
            window_index=3, persistence=3, severity_score=0.45,
            trend_direction="stable",
        )
        plans3 = planner.process_finding(finding3)
        assert len(plans3) == 1  # allowed — action was effective
        assert plans3[0].new_value == 3

        # ACK at window 4
        planner.apply_ack(
            plans3[0].plan_id, "dlio.read_threads", "applied",
            old_value=2, new_value=3, window_index=4,
        )

        # Window 6 (after cooldown=2): severity barely changed 0.45→0.41 (8.9%)
        finding6 = _make_finding(
            window_index=6, persistence=6, severity_score=0.41,
            trend_direction="stable",
        )
        plans6 = planner.process_finding(finding6)
        # Blocked: improvement (0.45-0.41)/0.45 = 8.9% < 10% threshold
        assert plans6 == []

    def test_no_motif_gate_persistence_handles_warmup(self):
        """Without a dedicated motif gate, persistence naturally filters warmup."""
        planner = _make_planner(min_persistence=2)

        # Window 0: warmup spike (high severity, persist=1)
        finding0 = _make_finding(
            window_index=0, persistence=1, severity_score=0.9,
            motif="warmup_transient",
        )
        assert planner.process_finding(finding0) == []  # persist=1 < 2

        # Window 1: warmup fades (lower severity, persist=1 — not consecutive)
        finding1 = _make_finding(
            window_index=1, persistence=1, severity_score=0.3,
            motif="warmup_transient",
        )
        assert planner.process_finding(finding1) == []  # severity < 0.5

    def test_cross_node_suppression(self):
        planner = _make_planner()

        # Global finding with suppresses_tags activates suppression
        global_finding = _make_finding(
            finding_type="compute_dominance",
            scope="global:global",
            opportunity_tags=["none"],
            suppresses_tags=["reader_parallelism", "dataloader_prefetch"],
            severity_score=0.9,
        )
        planner.process_finding(global_finding)

        # Per-node fetch_pressure should be suppressed
        node_finding = _make_finding(
            scope="node:tuolumne1022:reader_posix:epoch",
            opportunity_tags=["reader_parallelism"],
            severity_score=0.8,
        )
        assert planner.process_finding(node_finding) == []


# ── Data-driven suppression ──

class TestSuppression:
    def test_suppression_from_yaml_tags(self):
        """suppresses_tags from rules drives suppression, not hardcoded types."""
        planner = _make_planner()

        # A novel global rule we've never seen — suppresses reader_parallelism
        global_finding = _make_finding(
            finding_type="straggler_pressure",
            scope="global:global",
            opportunity_tags=["straggler_mitigation"],
            suppresses_tags=["reader_parallelism"],
            severity_score=0.7,
        )
        planner.process_finding(global_finding)

        # Per-node finding with reader_parallelism should be suppressed
        node_finding = _make_finding(
            scope="node:node1:reader_posix:epoch",
            opportunity_tags=["reader_parallelism"],
            severity_score=0.6,
        )
        assert planner.process_finding(node_finding) == []

    def test_suppression_cleared_when_global_stops(self):
        """Suppressions clear when a global finding of the same type has no suppresses_tags."""
        planner = _make_planner()

        # Activate suppression
        planner.process_finding(_make_finding(
            finding_type="compute_dominance",
            scope="global:global",
            opportunity_tags=["none"],
            suppresses_tags=["reader_parallelism"],
            severity_score=0.8,
        ))
        assert "reader_parallelism" in planner._active_suppressions

        # Global compute_dominance now fires WITHOUT suppresses_tags
        # (e.g., the condition changed or severity dropped)
        planner.process_finding(_make_finding(
            finding_type="compute_dominance",
            scope="global:global",
            opportunity_tags=["none"],
            suppresses_tags=[],
            severity_score=0.3,
        ))
        assert "reader_parallelism" not in planner._active_suppressions

    def test_suppression_does_not_affect_global_findings(self):
        """Global findings are never suppressed by other global findings."""
        planner = _make_planner(tag="straggler_mitigation")

        # Activate suppression for straggler_mitigation
        planner.process_finding(_make_finding(
            finding_type="compute_dominance",
            scope="global:global",
            suppresses_tags=["straggler_mitigation"],
            severity_score=0.8,
        ))

        # Another global finding with the suppressed tag should still work
        global_finding = _make_finding(
            finding_type="straggler_pressure",
            scope="global:global",
            opportunity_tags=["straggler_mitigation"],
            severity_score=0.9,
        )
        plans = planner.process_finding(global_finding)
        assert len(plans) == 1  # global is not suppressed

    def test_severity_weighted_agreement_exempts_straggler(self):
        """Node with higher local severity than global is exempted."""
        planner = _make_planner(effectiveness_threshold=0.0)

        # First: local finding establishes per-node severity and produces plan
        plans1 = planner.process_finding(_make_finding(
            scope="node:straggler_node:reader_posix:epoch",
            opportunity_tags=["reader_parallelism"],
            severity_score=0.85,
            window_index=1,
        ))
        assert len(plans1) == 1

        # ACK the first plan so pending_plan gate doesn't block later
        planner.apply_ack(
            plans1[0].plan_id, "dlio.read_threads", "applied",
            old_value=0, new_value=1, window_index=2,
            target_nodes=["straggler_node"],
        )

        # Global suppression at lower severity
        planner.process_finding(_make_finding(
            finding_type="compute_dominance",
            scope="global:global",
            suppresses_tags=["reader_parallelism"],
            severity_score=0.6,
            window_index=5,
        ))

        # Straggler node's severity (0.85) > global (0.6) → exempted
        straggler_finding = _make_finding(
            scope="node:straggler_node:reader_posix:epoch",
            opportunity_tags=["reader_parallelism"],
            severity_score=0.85,
            window_index=6,
        )
        plans = planner.process_finding(straggler_finding)
        assert len(plans) == 1  # exempted, plan generated

    def test_severity_weighted_agreement_suppresses_normal_node(self):
        """Node with lower severity than global is suppressed."""
        planner = _make_planner()

        # Local finding with low severity
        planner.process_finding(_make_finding(
            scope="node:normal_node:reader_posix:epoch",
            opportunity_tags=["reader_parallelism"],
            severity_score=0.3,
            window_index=1,
        ))

        # Global suppression at higher severity
        planner.process_finding(_make_finding(
            finding_type="compute_dominance",
            scope="global:global",
            suppresses_tags=["reader_parallelism"],
            severity_score=0.8,
            window_index=2,
        ))

        # Normal node's severity (0.3) < global (0.8) → suppressed
        node_finding = _make_finding(
            scope="node:normal_node:reader_posix:epoch",
            opportunity_tags=["reader_parallelism"],
            severity_score=0.3,
            window_index=3,
        )
        assert planner.process_finding(node_finding) == []

    def test_node_without_prior_data_is_suppressed(self):
        """Node with no prior local data agrees with global by default."""
        planner = _make_planner()

        # Global suppression — no prior per-node data for new_node
        planner.process_finding(_make_finding(
            finding_type="compute_dominance",
            scope="global:global",
            suppresses_tags=["reader_parallelism"],
            severity_score=0.7,
        ))

        node_finding = _make_finding(
            scope="node:new_node:reader_posix:epoch",
            opportunity_tags=["reader_parallelism"],
            severity_score=0.6,
        )
        assert planner.process_finding(node_finding) == []

    def test_multiple_tags_suppressed_independently(self):
        """Different global rules can suppress different tags."""
        planner = _make_planner()

        # Rule 1 suppresses reader_parallelism
        planner.process_finding(_make_finding(
            finding_type="compute_dominance",
            scope="global:global",
            suppresses_tags=["reader_parallelism"],
            severity_score=0.8,
        ))

        # Rule 2 suppresses dataloader_prefetch
        planner.process_finding(_make_finding(
            finding_type="io_imbalance",
            scope="global:global",
            suppresses_tags=["dataloader_prefetch"],
            severity_score=0.7,
        ))

        assert "reader_parallelism" in planner._active_suppressions
        assert "dataloader_prefetch" in planner._active_suppressions

        # Clear only compute_dominance's suppressions
        planner.process_finding(_make_finding(
            finding_type="compute_dominance",
            scope="global:global",
            suppresses_tags=[],
            severity_score=0.3,
        ))

        assert "reader_parallelism" not in planner._active_suppressions
        assert "dataloader_prefetch" in planner._active_suppressions  # still active


# ── Global plan acceptance ──

def _make_global_plan(**overrides):
    from dfoptimizer.types import ActionPlan
    defaults = dict(
        plan_id="global-001",
        knob_id="dlio.read_threads",
        target_function="make_loader",
        old_value=0,
        new_value=4,
        apply_when="next_window",
        rationale="global straggler mitigation",
        finding_type="global",
        severity=0.8,
        opportunity_tag="reader_parallelism",
        window_index=5,
        target_nodes=["node1"],
    )
    defaults.update(overrides)
    return ActionPlan(**defaults)


class TestApplyGlobalPlan:
    def test_accepts_valid_plan(self):
        planner = _make_planner()
        plan = _make_global_plan(new_value=4)
        result = planner.apply_global_plan(plan)
        assert result is not None
        assert result.new_value == 4
        assert result.old_value == 0
        assert planner.current_values["dlio.read_threads"] == 4

    def test_rejects_unregistered_knob(self):
        planner = _make_planner()
        plan = _make_global_plan(knob_id="unknown.knob")
        result = planner.apply_global_plan(plan)
        assert result is None

    def test_clamps_to_max(self):
        planner = _make_planner(range=(0, 8))
        plan = _make_global_plan(new_value=16)
        result = planner.apply_global_plan(plan)
        assert result is not None
        assert result.new_value == 8

    def test_clamps_to_min(self):
        planner = _make_planner(range=(2, 16))
        plan = _make_global_plan(new_value=0)
        result = planner.apply_global_plan(plan)
        assert result is not None
        assert result.new_value == 2

    def test_rejects_no_change(self):
        planner = _make_planner()
        planner.current_values["dlio.read_threads"] = 4
        plan = _make_global_plan(new_value=4)
        result = planner.apply_global_plan(plan)
        assert result is None

    def test_starts_cooldown(self):
        planner = _make_planner(cooldown_windows=3)
        plan = _make_global_plan(new_value=4, window_index=5)
        planner.apply_global_plan(plan)
        # Local finding right after should be blocked by cooldown
        finding = _make_finding(window_index=6, persistence=4, severity_score=0.8)
        plans = planner.process_finding(finding)
        assert plans == []

    def test_cooldown_expires(self):
        planner = _make_planner(cooldown_windows=3, effectiveness_threshold=0.0)
        plan = _make_global_plan(new_value=4, window_index=5)
        planner.apply_global_plan(plan)
        # After cooldown period, local should work again
        finding = _make_finding(window_index=9, persistence=4, severity_score=0.8)
        plans = planner.process_finding(finding)
        assert len(plans) == 1

    def test_global_overrides_local_value(self):
        """Global plan overrides whatever value the local planner had."""
        planner = _make_planner()
        # Local planner increased to 8
        planner.current_values["dlio.read_threads"] = 8
        # Global says set to 3 (e.g., contention detected globally)
        plan = _make_global_plan(new_value=3)
        result = planner.apply_global_plan(plan)
        assert result is not None
        assert result.old_value == 8
        assert result.new_value == 3
        assert planner.current_values["dlio.read_threads"] == 3
