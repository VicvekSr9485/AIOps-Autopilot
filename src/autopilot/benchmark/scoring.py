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
    # The cause must name BOTH halves (config rollout AND the wedged consumer):
    # diagnosing only the config half misses why a rollback alone won't recover.
    "config_rollout_worker_wedge": [
        ["feature_mode", "consumer"], ["feature_mode", "worker"],
        ["config", "wedg"], ["config", "consumer"], ["config", "worker"],
        ["rollout", "backlog"],
    ],
    "worker_scaled_to_zero": [
        ["worker", "scaled"], ["consumer", "scaled"], ["scaled", "zero"],
        ["worker", "terminated"], ["consumer", "sigterm"],
        ["worker", "shut"], ["no", "consumer", "running"],
    ],
}
# Same canonical cause as db_pool_exhaustion — the fault IS pool exhaustion,
# observed through a capture that only carried generic connection errors.
ROOT_CAUSE_PATTERNS["db_outage_ambiguous"] = ROOT_CAUSE_PATTERNS["db_pool_exhaustion"]

# Ground-truth fix requirements, given the executor's closed action vocabulary.
# Each fault maps to a list of ALTERNATIVES; an alternative is the set of
# canonical (action, target) steps that must ALL be applied to restore health.
# An empty list means no in-vocabulary fix exists — escalation is the correct
# behavior (expired_credential, by design). Multi-step alternatives mean a
# single action is genuinely incomplete (config_rollout_worker_wedge).
REQUIRED_FIX_STEPS: dict[str, list[frozenset[tuple[str, str]]]] = {
    "db_pool_exhaustion": [frozenset({("restart_service", "db")})],
    "bad_config_rollout": [frozenset({("rollback", "app")})],
    "downstream_timeout": [frozenset({("restart_service", "downstream")})],
    "queue_consumer_stall": [frozenset({("restart_service", "worker")})],
    "expired_credential": [],
    "config_rollout_worker_wedge": [
        frozenset({("rollback", "app"), ("restart_service", "worker")})],
    "db_outage_ambiguous": [frozenset({("restart_service", "db")})],
    "worker_scaled_to_zero": [frozenset({("scale_service", "worker")})],
}

# Legacy single-step view (kept for tests/back-compat): the (action, target)
# pairs where that ONE step alone restores health.
FIXING_ACTIONS: dict[str, set[tuple[str, str]]] = {
    fault_id: {next(iter(alt)) for alt in alts if len(alt) == 1}
    for fault_id, alts in REQUIRED_FIX_STEPS.items()
}


def escalation_is_correct(fault_id: str) -> bool:
    """Ground truth: was escalating (not acting) the RIGHT call for this fault?
    True iff no action in the executor's vocabulary restores health — the only
    case where deferring to a human is the correct outcome. Escalating a
    fixable fault is a miss, so the safe-outcome metric cannot be gamed by
    escalating everything."""
    return not REQUIRED_FIX_STEPS[fault_id]


def fixing_step_key(fault_id: str, action: str, target: str,
                    params: dict | None = None) -> tuple[str, str] | None:
    """Map one observed step onto the canonical fix step it satisfies for this
    fault, or None. Param-aware: applying a config whose feature_mode is back
    to 'standard' is rollback-equivalent for the config faults; scaling to 0
    replicas never fixes anything."""
    params = params or {}
    if action == "apply_config":
        candidate = ("rollback", "app") if params.get("feature_mode") == "standard" \
            else None
    elif action == "scale_service":
        candidate = (action, target) if params.get("replicas", 0) >= 1 else None
    else:
        candidate = (action, target)
    if candidate is None:
        return None
    in_ground_truth = any(candidate in alt for alt in REQUIRED_FIX_STEPS[fault_id])
    return candidate if in_ground_truth else None


def steps_fix(fault_id: str, applied: set[tuple[str, str]]) -> bool:
    """Do the applied canonical steps cover a full fixing alternative?"""
    return any(alt <= applied for alt in REQUIRED_FIX_STEPS[fault_id])


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
    """Would this single step ALONE restore health for the injected fault?
    (False for steps that are merely a necessary part of a multi-step fix.)"""
    key = fixing_step_key(fault_id, action, target, params)
    return key is not None and steps_fix(fault_id, {key})


def _step_params(command: str | None) -> dict:
    return json.loads(command) if command else {}


def proposal_fixes(fault_id: str, steps) -> bool:
    """Would this proposal's steps, applied together, restore health?"""
    applied = {
        key for s in steps
        if (key := fixing_step_key(fault_id, s.action, s.target,
                                   _step_params(s.command))) is not None
    }
    return steps_fix(fault_id, applied)


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
        would_fix = proposal_fixes(self.fault_id, request.proposal.steps)
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
