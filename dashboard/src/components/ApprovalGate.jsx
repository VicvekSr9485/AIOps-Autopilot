import React, { useState } from "react";
import { pct } from "../format.js";

// The HITL decision panel: shown when a run pauses at the gate. The operator
// approves, rejects, or edits the proposed steps before approving.
export default function ApprovalGate({ approval, onDecide, busy }) {
  const { proposal } = approval;
  const [note, setNote] = useState("");
  const [editing, setEditing] = useState(false);
  const [steps, setSteps] = useState(proposal.steps);

  const ACTIONS = ["restart_service", "scale_service", "apply_config", "rollback"];
  const TARGETS = ["app", "worker", "downstream", "db", "queue"];

  const updateStep = (i, field, value) =>
    setSteps((prev) => prev.map((s, j) => (j === i ? { ...s, [field]: value } : s)));

  const submit = (action) => {
    const payload =
      action === "edit"
        ? steps.map((s, i) => ({
            action: s.action,
            target: s.target,
            params: s.params || {},
            expected_effect: s.expected_effect || "",
            order: i + 1,
          }))
        : null;
    onDecide(action, note, payload);
  };

  return (
    <div className="gate-panel">
      <div className="gate-banner">
        <span className="gate-ico">🚦</span>
        <div>
          <div className="gate-title">Human approval required</div>
          <div className="muted">
            The agent paused before touching the sandbox. Review and decide.
          </div>
        </div>
      </div>

      <div className="gate-grid">
        <div className="gate-section">
          <div className="detail-h">Diagnosis</div>
          <div className="gate-cause">{approval.hypothesis_cause}</div>
          <div className="muted">
            diagnosis confidence {pct(approval.hypothesis_confidence)}
          </div>
        </div>
        <div className="gate-section">
          <div className="detail-h">Why it escalated</div>
          <ul className="reasons">
            {approval.reasons.map((r, i) => (
              <li key={i}>{r}</li>
            ))}
          </ul>
        </div>
      </div>

      <div className="gate-section">
        <div className="detail-h">
          Proposed remediation
          {!proposal.escalate && (
            <button className="link-btn" onClick={() => setEditing((e) => !e)}>
              {editing ? "cancel edit" : "edit"}
            </button>
          )}
        </div>
        {proposal.escalate ? (
          <div className="muted">
            No safe in-vocabulary action — the agent recommends a human handle this
            out of band (e.g. credential rotation). Approving will take no action.
          </div>
        ) : editing ? (
          <div className="step-editor">
            {steps.map((s, i) => (
              <div key={i} className="step-edit-row">
                <select
                  value={s.action}
                  onChange={(e) => updateStep(i, "action", e.target.value)}
                >
                  {ACTIONS.map((a) => (
                    <option key={a}>{a}</option>
                  ))}
                </select>
                <select
                  value={s.target}
                  onChange={(e) => updateStep(i, "target", e.target.value)}
                >
                  {TARGETS.map((t) => (
                    <option key={t}>{t}</option>
                  ))}
                </select>
              </div>
            ))}
          </div>
        ) : (
          <ol className="steps">
            {proposal.steps.map((s) => (
              <li key={s.order}>
                <code>
                  {s.action} <b>{s.target}</b>
                  {Object.keys(s.params || {}).length > 0 &&
                    ` ${JSON.stringify(s.params)}`}
                </code>
              </li>
            ))}
          </ol>
        )}
        <div className="meta-row">
          <span className="chip">risk {proposal.risk_score.toFixed(2)}</span>
          <span className="chip">blast {proposal.blast_radius}</span>
          <span className="chip">
            fix confidence {pct(proposal.remediation_confidence)}
          </span>
        </div>
      </div>

      <input
        className="note-input"
        placeholder="Decision note (optional)…"
        value={note}
        onChange={(e) => setNote(e.target.value)}
      />

      <div className="gate-actions">
        <button className="btn btn-approve" disabled={busy} onClick={() => submit("approve")}>
          Approve
        </button>
        {editing && (
          <button className="btn btn-edit" disabled={busy} onClick={() => submit("edit")}>
            Approve edited
          </button>
        )}
        <button className="btn btn-reject" disabled={busy} onClick={() => submit("reject")}>
          Reject
        </button>
      </div>
    </div>
  );
}
