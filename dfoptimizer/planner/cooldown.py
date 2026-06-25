import dataclasses as dc
from typing import Dict, Optional, Tuple


@dc.dataclass
class ActionRecord:
    """State recorded when a plan is applied."""

    window_index: int
    severity_at_action: float


@dc.dataclass
class CooldownResult:
    """Result of a cooldown check."""

    blocked: bool
    reason: str = ""  # "time", "ineffective", or ""


class CooldownTracker:
    """Tracks when each knob was last changed to enforce cooldown periods
    and verify action effectiveness.

    Phase 1 (time-based): Must wait ``cooldown_windows`` after the last
    action before allowing another change.

    Phase 2 (effectiveness): After the time-based cooldown expires, checks
    whether severity improved by at least ``effectiveness_threshold`` since
    the last action.  If the previous action didn't help, further changes
    to the same knob are blocked.

    Keyed by ``(knob_id, node)`` to support per-node cooldown tracking.
    Use ``node=""`` for global (all-node) plans.
    """

    def __init__(self):
        # (knob_id, node) -> ActionRecord
        self._last_action: Dict[Tuple[str, str], ActionRecord] = {}

    def record(
        self,
        knob_id: str,
        window_index: int,
        severity_score: float = 0.0,
        node: str = "",
    ):
        """Record that a plan was applied for this knob at this window."""
        self._last_action[(knob_id, node)] = ActionRecord(
            window_index=window_index,
            severity_at_action=severity_score,
        )

    def check(
        self,
        knob_id: str,
        current_window: int,
        cooldown_windows: int,
        current_severity: Optional[float] = None,
        effectiveness_threshold: float = 0.10,
        node: str = "",
    ) -> CooldownResult:
        """Check if a knob is in cooldown or if the last action was ineffective.

        Returns a CooldownResult indicating whether the knob is blocked and why.
        """
        rec = self._last_action.get((knob_id, node))
        if rec is None:
            return CooldownResult(blocked=False)

        # Phase 1: Time-based cooldown
        if (current_window - rec.window_index) < cooldown_windows:
            return CooldownResult(blocked=True, reason="time")

        # Phase 2: Effectiveness check
        if (
            current_severity is not None
            and rec.severity_at_action > 0
        ):
            improvement = (
                (rec.severity_at_action - current_severity)
                / rec.severity_at_action
            )
            if improvement < effectiveness_threshold:
                return CooldownResult(blocked=True, reason="ineffective")

        return CooldownResult(blocked=False)

    # Legacy API for backward compatibility
    def in_cooldown(
        self, knob_id: str, current_window: int, cooldown_windows: int,
        node: str = "",
    ) -> bool:
        """Time-based cooldown check only (no effectiveness)."""
        result = self.check(
            knob_id, current_window, cooldown_windows, node=node,
            current_severity=None,
        )
        return result.blocked
