"""Full-loop tests (mock mode, offline): triage -> plan -> HITL gate ->
execute (dry-run then apply) -> verify -> auto-rollback -> record_outcome,
over every fault scenario. Planner is structurally toolless; destructive
proposals always escalate; failed verification triggers rollback; outcomes
land in the knowledge store; spend is metered end-to-end."""

from __future__ import annotations

import inspect
import json

import pytest

from autopilot.harness.synthetic import FAULT_IDS, scenario_capture
from autopilot.mcp_servers.context import RunContext
from autopilot.mcp_servers.infra import build_infra_server
from autopilot.mcp_servers.knowledge import build_knowledge_server
from autopilot.mcp_servers.store import KnowledgeStore
from autopilot.mcp_servers.telemetry import build_telemetry_server
from autopilot.models import RemediationProposal, RemediationStep
from autopilot.pipeline.executor import ExecutionRefused, execute
from autopilot.pipeline.hitl import StaticApprover, hitl_gate
from autopilot.pipeline.ingest import ingest
from autopilot.pipeline.remediation import plan_remediation
from autopilot.pipeline.run import run_incident
from test_mcp_servers import FakeController, healthy_snap
from test_pipeline_triage import ScriptedClient, valid_triage_json

pytestmark = pytest.mark.anyio


def plan_json(action="restart_service", target="worker", params=None, risk=0.2):
    step = {"action": action, "target": target, "params": params or {},
            "expected_effect": "service converges to healthy"}
    return json.dumps({
        "steps": [step],
        "rollback_plan": [{"action": "restart_service", "target": "app",
                           "params": {}, "expected_effect": "restore baseline"}],
        "risk_score": risk,
        "blast_radius": "single_service",
    })


class World:
    """One incident's worth of wiring: servers over a shared fake controller,
    a seeded in-memory store, and a run-bound context."""

    def __init__(self, fault_id: str, recovers: bool = True):
        log_text, snapshots = scenario_capture(fault_id)
        self.incident = ingest(log_text, snapshots)
        # Probe order: triage's live re-check (2 samples) consumes faulty
        # snapshots; by verification time the stack reads healthy — unless
        # recovers=False, in which case it stays faulty throughout.
        probe_seq = list(snapshots[:2]) + [healthy_snap()] if recovers else list(snapshots)
        self.ctrl = FakeController(snapshots=probe_seq)
        self.store = KnowledgeStore(":memory:")
        self.context = RunContext()
        self.servers = {
            "telemetry": build_telemetry_server(self.ctrl),
            "infra": build_infra_server(self.ctrl),
            "knowledge": build_knowledge_server(store=self.store, context=self.context),
        }


@pytest.mark.parametrize("fault_id", FAULT_IDS)
async def test_full_loop_resolves_each_fault(fault_id):
    world = World(fault_id)
    client = ScriptedClient([valid_triage_json(), plan_json()])
    approver = StaticApprover("approve")

    report = await run_incident(world.incident, world.servers, client,
                                approver, world.context, verify_interval_s=0.0)

    assert report.resolved
    assert report.gate.route == "auto"  # confidence .85 >= .75, risk .2 <= .4
    assert approver.requests == []  # human never consulted on the green path
    assert report.execution and report.execution.success
    assert report.verification and report.verification.resolved
    assert not report.rolled_back
    # the remediation actually hit the (fake) stack exactly once, post-dry-run
    assert world.ctrl.calls.count(("restart", "worker")) == 1
    # outcome recorded under the incident's id (server-side binding)
    assert report.outcome_recorded
    assert world.store.count("incident") == 1
    hit = world.store.search(report.triage.top.cause, kind="incident", k=1)[0]
    assert hit.key == world.incident.id
    assert "[resolved]" in hit.title


async def test_planner_is_structurally_toolless():
    # No servers/tools parameter exists on the planner at all.
    params = inspect.signature(plan_remediation).parameters
    assert not any(name in params for name in ("servers", "tools", "mcp"))

    world = World("queue_consumer_stall")
    client = ScriptedClient([valid_triage_json(), plan_json()])
    triage = await __import__("autopilot.pipeline.triage", fromlist=["run_triage"]) \
        .run_triage(world.incident, world.servers, client)
    calls_after_triage = list(world.ctrl.calls)

    proposal = plan_remediation(triage, client)

    assert world.ctrl.calls == calls_after_triage  # planning touched nothing
    assert RemediationProposal.model_validate(proposal.model_dump())
    assert proposal.requires_approval  # only the gate may clear this
    assert proposal.incident_id == world.incident.id
    # planner ran on the default tier, triage on reasoning — and nothing else
    assert [r.role for r in client.meter.records] == ["reasoning", "default"]
    assert client.meter.records[1].model == "qwen3.7-plus"
    assert client.meter.records[1].step == "remediation.plan"
    # runbook guidance flowed forward from triage instead of being re-fetched
    assert "RUNBOOK GUIDANCE" in client.prompts[1][1]["content"]


async def test_verifier_catches_silent_backlog():
    """Green probes alone must not verify resolution: a queue that grows — or
    sits stuck above zero — while jobs_processed stays flat fails the
    backlog_draining check (this is what catches the baseline's partial fix on
    the multi-step fault)."""
    from autopilot.pipeline.verify import verify

    def servers_for(snaps):
        return {"telemetry": build_telemetry_server(FakeController(snapshots=snaps))}

    growing = [healthy_snap(queue_depth=4 + 3 * i, jobs_processed=9)
               for i in range(8)]
    result = await verify("inc-grow", servers_for(growing), interval_s=0.0)
    assert not result.resolved
    assert any(c.name == "backlog_draining" and not c.passed for c in result.checks)

    stuck = [healthy_snap(queue_depth=7, jobs_processed=9) for _ in range(8)]
    result = await verify("inc-stuck", servers_for(stuck), interval_s=0.0)
    assert not result.resolved

    draining = [healthy_snap(queue_depth=max(0, 21 - 3 * i), jobs_processed=9 + i)
                for i in range(8)]
    result = await verify("inc-drain", servers_for(draining), interval_s=0.0)
    assert result.resolved


async def test_planner_receives_triage_evidence_handoff():
    """Planner-handoff contract (fix for the first benchmark's planner losses):
    the planner prompt leads with the symptom summary triage gathered, the
    hypothesis comes second, and retrieved runbook text comes last, labeled as
    fallible reference material — primary evidence outranks retrieval noise."""
    from autopilot.pipeline.triage import run_triage

    world = World("bad_config_rollout")
    client = ScriptedClient([valid_triage_json(), plan_json()])
    triage = await run_triage(world.incident, world.servers, client)
    assert "invalid_feature_mode" in triage.telemetry_summary  # populated

    plan_remediation(triage, client)
    prompt = client.prompts[1][1]["content"]
    sym = prompt.index("INCIDENT SYMPTOMS")
    hyp = prompt.index("ROOT-CAUSE HYPOTHESIS")
    runbook = prompt.index("RUNBOOK GUIDANCE")
    assert sym < hyp < runbook
    assert triage.telemetry_summary in prompt
    assert "relevance is approximate" in prompt  # runbooks marked as reference


@pytest.mark.parametrize("action,params", [
    ("apply_config", {"feature_mode": "standard"}),
    ("scale_service", {"replicas": 0}),
])
async def test_destructive_proposals_always_escalate(action, params):
    """Even at confidence 0.95 and model-claimed risk 0.05, destructive actions
    reach a human — and the risk floor overrides the model's optimism."""
    world = World("bad_config_rollout")
    client = ScriptedClient([
        valid_triage_json().replace("0.85", "0.95"),
        plan_json(action=action, target="app", params=params, risk=0.05),
    ])
    approver = StaticApprover("reject", note="not during business hours")

    report = await run_incident(world.incident, world.servers, client,
                                approver, world.context, verify_interval_s=0.0)

    assert report.gate.route == "human"
    assert not report.gate.approved
    assert any("destructive" in r for r in report.gate.escalation_reasons)
    assert report.proposal.risk_score >= 0.6  # server-side floor applied
    assert len(approver.requests) == 1
    assert report.execution is None  # nothing executed
    assert world.ctrl.calls == []  # the stack was never touched
    # rejection still recorded for future retrieval
    assert world.store.count("incident") == 1
    hit = world.store.search("config", kind="incident", k=1)[0]
    assert "[unresolved]" in hit.title and "human=reject" in hit.body


async def test_low_confidence_escalates_and_human_approval_executes():
    world = World("downstream_timeout")
    client = ScriptedClient([
        valid_triage_json().replace("0.85", "0.55"),  # below the 0.75 threshold
        plan_json(target="downstream"),
    ])
    approver = StaticApprover("approve", note="looks right, go")

    report = await run_incident(world.incident, world.servers, client,
                                approver, world.context, verify_interval_s=0.0)

    assert report.gate.route == "human" and report.gate.approved
    assert any("confidence" in r for r in report.gate.escalation_reasons)
    assert report.execution and report.execution.success  # approval clears the flag
    assert ("restart", "downstream") in world.ctrl.calls


async def test_failed_verification_triggers_auto_rollback():
    world = World("db_pool_exhaustion", recovers=False)  # stays unhealthy
    client = ScriptedClient([valid_triage_json(), plan_json(target="db")])

    report = await run_incident(world.incident, world.servers, client,
                                StaticApprover("approve"), world.context, verify_interval_s=0.0)

    assert report.execution and report.execution.success  # steps applied fine
    assert report.verification and not report.verification.resolved
    assert report.rolled_back
    assert not report.resolved
    # remediation step ran, then the rollback plan ran
    assert world.ctrl.calls.index(("restart", "db")) < \
        world.ctrl.calls.index(("restart", "app"))
    hit = world.store.search("connection", kind="incident", k=1)[0]
    assert "auto_rolled_back" in hit.body and "[unresolved]" in hit.title


async def test_failed_step_halts_execution_and_rolls_back():
    class BrokenRestartController(FakeController):
        def restart(self, service):
            if service == "worker":
                raise RuntimeError("docker daemon went away")
            super().restart(service)

    world = World("queue_consumer_stall")
    world.ctrl.__class__ = BrokenRestartController
    client = ScriptedClient([valid_triage_json(), plan_json(target="worker")])

    report = await run_incident(world.incident, world.servers, client,
                                StaticApprover("approve"), world.context, verify_interval_s=0.0)

    assert report.execution and not report.execution.success
    assert not report.resolved and report.rolled_back
    failed = [o for o in report.execution.step_outcomes if not o.success]
    assert failed and "docker daemon" in failed[0].output


async def test_executor_refuses_unapproved_proposals():
    world = World("bad_config_rollout")
    proposal = RemediationProposal(
        incident_id=world.incident.id, hypothesis_cause="x",
        steps=[RemediationStep(order=1, action="restart_service", target="app")],
        risk_score=0.1, blast_radius="single_service", requires_approval=True,
    )
    with pytest.raises(ExecutionRefused, match="requires approval"):
        await execute(proposal, world.servers)
    assert world.ctrl.calls == []


async def test_gate_threshold_boundaries_auto_approve():
    world = World("downstream_timeout")
    client = ScriptedClient([
        valid_triage_json().replace("0.85", "0.75"),  # exactly at T
        plan_json(risk=0.4),  # exactly at R
    ])
    report = await run_incident(world.incident, world.servers, client,
                                StaticApprover("reject"), world.context, verify_interval_s=0.0)
    assert report.gate.route == "auto" and report.gate.approved


async def test_edit_decision_executes_edited_proposal():
    world = World("downstream_timeout")
    client = ScriptedClient([
        valid_triage_json().replace("0.85", "0.5"),
        plan_json(target="db"),  # the "wrong" plan the human corrects
    ])
    edited = RemediationProposal(
        incident_id=world.incident.id, hypothesis_cause="downstream stopped responding",
        steps=[RemediationStep(order=1, action="restart_service", target="downstream")],
        rollback_plan=[RemediationStep(order=1, action="restart_service", target="app")],
        risk_score=0.1, blast_radius="single_service",
    )
    report = await run_incident(world.incident, world.servers, client,
                                StaticApprover("edit", edited_proposal=edited),
                                world.context, verify_interval_s=0.0)
    assert report.gate.human_action == "edit"
    assert ("restart", "downstream") in world.ctrl.calls
    assert ("restart", "db") not in world.ctrl.calls  # original plan discarded


async def test_end_to_end_metering_per_incident():
    world = World("expired_credential")
    client = ScriptedClient(["not json", valid_triage_json(), plan_json(target="db")])

    report = await run_incident(world.incident, world.servers, client,
                                StaticApprover("approve"), world.context, verify_interval_s=0.0)

    assert report.llm.calls == 3  # 1 triage retry + 1 triage ok + 1 plan
    assert report.llm.steps == ["triage.root_cause", "triage.root_cause",
                                "remediation.plan"]
    assert report.llm.input_tokens > 0 and report.llm.output_tokens > 0
    assert report.llm.est_cost_usd > 0
    models = [r.model for r in client.meter.records]
    assert models == ["qwen3.7-max", "qwen3.7-max", "qwen3.7-plus"]


async def test_record_outcome_is_idempotent_across_reruns():
    world = World("queue_consumer_stall")
    for _ in range(2):
        client = ScriptedClient([valid_triage_json(), plan_json()])
        await run_incident(world.incident, world.servers, client,
                           StaticApprover("approve"), world.context, verify_interval_s=0.0)
        world.ctrl._snapshots = [healthy_snap()]
    assert world.store.count("incident") == 1  # upsert by bound incident id


def test_hitl_gate_unit_routes():
    from autopilot.models import RootCauseHypothesis
    hyp = RootCauseHypothesis(incident_id="inc-1", cause="c", confidence=0.9)
    safe = RemediationProposal(
        incident_id="inc-1", hypothesis_cause="c",
        steps=[RemediationStep(order=1, action="restart_service", target="app")],
        risk_score=0.2, blast_radius="single_service",
    )
    auto = hitl_gate(hyp, safe, StaticApprover("reject"))
    assert auto.route == "auto" and auto.approved
    assert not auto.proposal.requires_approval

    risky = safe.model_copy(update={"risk_score": 0.9})
    escalated = hitl_gate(hyp, risky, StaticApprover("approve"))
    assert escalated.route == "human" and escalated.approved
    assert not escalated.proposal.requires_approval  # human approval clears it
