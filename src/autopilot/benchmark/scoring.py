"""Ground-truth scoring: the ONLY benchmark module allowed to read FaultSpec.

Everything here lives on the measurement side of the leak boundary — it judges
agent output against known fault ground truth and never feeds anything into the
agent's context. Root-cause matching is deterministic keyword groups (auditable,
zero LLM tokens); remediation correctness in REAL runs is measured by the
sandbox itself (health re-check), while FIXING_ACTIONS below drives only the
mock-mode sandbox simulation and the ground-truth oracle approver.
"""

from __future__ import annotations

import json

import structlog

from autopilot.pipeline.hitl import ApprovalRequest, HumanDecision

log = structlog.get_logger("autopilot.benchmark.scoring")

# A hypothesis matches a fault's canonical root cause iff, for at least one
# group, EVERY substring in that group appears in the hypothesis text
# (case-insensitive). Groups are alternative phrasings of the same cause.
ROOT_CAUSE_PATTERNS: dict[str, list[list[str]]] = {
    "db_pool_exhaustion": [
        ["connection", "exhaust"], ["connection", "slot"], ["connection", "pool"],
        ["too many", "client"], ["idle", "session"],
    ],
    "bad_config_rollout": [
        ["feature_mode"], ["config", "invalid"], ["config", "rollout"],
        ["config", "bad"], ["config", "unsupported"],
    ],
    "downstream_timeout": [
        ["downstream", "timeout"], ["downstream", "time", "out"],
        ["downstream", "not responding"], ["downstream", "unreachable"],
        ["downstream", "stopped"], ["downstream", "unresponsive"],
        ["downstream", "hang"],
    ],
    "queue_consumer_stall": [
        ["consumer", "stall"], ["worker", "stall"], ["worker", "paused"],
        ["worker", "stopped"], ["worker", "not", "consum"],
        ["queue", "backlog"], ["jobs", "not", "processed"],
    ],
    "expired_credential": [
        ["password", "auth"], ["credential", "expired"], ["credential", "invalid"],
        ["credential", "rotat"], ["password", "rotat"], ["auth", "fail"],
    ],
}

# (action, target) pairs that genuinely restore health for each fault, given the
# executor's closed action vocabulary. expired_credential is deliberately empty:
# its real fix (reset the role password) is OUTSIDE the action vocabulary, so the
# correct agent behavior is to escalate, not to act.
FIXING_ACTIONS: dict[str, set[tuple[str, str]]] = {
    "db_pool_exhaustion": {("restart_service", "db")},
    "bad_config_rollout": {("rollback", "app")},
    "downstream_timeout": {("restart_service", "downstream")},
    "queue_consumer_stall": {("restart_service", "worker")},
    "expired_credential": set(),
}


def root_cause_matches(fault_id: str, cause: str) -> bool:
    text = cause.lower()
    return any(
        all(token in text for token in group)
        for group in ROOT_CAUSE_PATTERNS[fault_id]
    )


def score_root_cause(fault_id: str, ranked_causes: list[str]) -> tuple[bool, bool]:
    """(top-1 hit, top-3 hit) for a confidence-ranked cause list."""
    hits = [root_cause_matches(fault_id, c) for c in ranked_causes[:3]]
    return (bool(hits and hits[0]), any(hits))


def remediation_fixes(
    fault_id: str, action: str, target: str, params: dict | None = None
) -> bool:
    """Would this single step restore health for the injected fault?"""
    if (action, target) in FIXING_ACTIONS[fault_id]:
        return True
    if fault_id == "bad_config_rollout" and action == "apply_config":
        return (params or {}).get("feature_mode") == "standard"
    return False


def _step_params(command: str | None) -> dict:
    return json.loads(command) if command else {}


class GroundTruthApprover:
    """Benchmark stand-in for the human at the HITL gate: answers escalations
    from ground truth — approve iff the proposal contains a step that actually
    fixes the injected fault, reject otherwise (a perfect operator). Decisions
    are recorded so escalation behavior lands in the metrics."""

    def __init__(self, fault_id: str):
        self.fault_id = fault_id
        self.requests: list[ApprovalRequest] = []
        self.decisions: list[str] = []

    def decide(self, request: ApprovalRequest) -> HumanDecision:
        self.requests.append(request)
        would_fix = any(
            remediation_fixes(self.fault_id, s.action, s.target,
                              _step_params(s.command))
            for s in request.proposal.steps
        )
        action = "approve" if would_fix else "reject"
        self.decisions.append(action)
        log.info("ground_truth_approver_decided", step="benchmark.hitl",
                 fault_id=self.fault_id, action=action,
                 reasons=request.escalation_reasons)
        return HumanDecision(
            action=action,
            note=f"ground-truth oracle: proposal {'fixes' if would_fix else 'does not fix'} "
                 f"fault {self.fault_id}",
        )
