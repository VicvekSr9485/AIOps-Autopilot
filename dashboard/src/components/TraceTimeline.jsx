import React, { useState } from "react";
import { STAGE_ICON, STAGE_LABEL, fmtTokens, fmtUsd, pct } from "../format.js";

function ConfidenceBar({ value }) {
  const tone = value >= 0.75 ? "ok" : value >= 0.5 ? "warn" : "low";
  return (
    <div className="confbar" title={`confidence ${pct(value)}`}>
      <div className={`confbar-fill conf-${tone}`} style={{ width: pct(value) }} />
      <span className="confbar-label">{pct(value)}</span>
    </div>
  );
}

function Hypotheses({ list, runbooks }) {
  return (
    <div className="detail-block">
      <div className="detail-h">Ranked hypotheses</div>
      {list.map((h, i) => (
        <div key={i} className="hyp">
          <div className="hyp-head">
            <span className={`rank ${i === 0 ? "rank-top" : ""}`}>#{i + 1}</span>
            <span className="hyp-cause">{h.cause}</span>
            <span className="hyp-conf">{pct(h.confidence)}</span>
          </div>
          {h.reasoning_summary && <div className="hyp-reason">{h.reasoning_summary}</div>}
          {h.evidence?.length > 0 && (
            <div className="evidence">
              {h.evidence.map((e, j) => (
                <span key={j} className="chip chip-ev" title={e.excerpt}>
                  {e.kind}:{e.pointer}
                </span>
              ))}
            </div>
          )}
        </div>
      ))}
      {runbooks?.length > 0 && (
        <>
          <div className="detail-h" style={{ marginTop: 10 }}>
            Runbooks consulted
          </div>
          <ul className="runbooks">
            {runbooks.map((r, i) => (
              <li key={i}>{r.split(" (score=")[0]}</li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

function Steps({ proposal }) {
  if (proposal.escalate) {
    return (
      <div className="detail-block">
        <div className="muted">
          Planner declined — no safe in-vocabulary remediation. Routed to a human.
        </div>
      </div>
    );
  }
  return (
    <div className="detail-block">
      <div className="detail-h">Planned steps</div>
      <ol className="steps">
        {proposal.steps.map((s) => (
          <li key={s.order}>
            <code>
              {s.action} <b>{s.target}</b>
              {Object.keys(s.params || {}).length > 0 &&
                ` ${JSON.stringify(s.params)}`}
            </code>
            {s.expected_effect && <span className="muted"> — {s.expected_effect}</span>}
          </li>
        ))}
      </ol>
      <div className="meta-row">
        <span className="chip">risk {proposal.risk_score.toFixed(2)}</span>
        <span className="chip">blast {proposal.blast_radius}</span>
        <span className="chip">
          fix confidence {pct(proposal.remediation_confidence)}
        </span>
      </div>
    </div>
  );
}

function Checks({ checks }) {
  return (
    <div className="detail-block">
      {checks.map((c, i) => (
        <div key={i} className={`check ${c.passed ? "ok" : "fail"}`}>
          <span>{c.passed ? "✓" : "✗"}</span>
          <span className="check-name">{c.name}</span>
          <span className="muted">{c.detail}</span>
        </div>
      ))}
    </div>
  );
}

function EventRow({ ev }) {
  const expandable =
    ev.payload?.hypotheses || ev.payload?.proposal || ev.payload?.checks;
  const [open, setOpen] = useState(ev.stage === "triage" || ev.stage === "remediation");
  return (
    <div className={`trace-row status-${ev.status}`}>
      <div className="trace-dot" />
      <div className="trace-body">
        <div
          className={`trace-head ${expandable ? "clickable" : ""}`}
          onClick={() => expandable && setOpen((o) => !o)}
        >
          <span className="stage-ico">{STAGE_ICON[ev.stage]}</span>
          <div className="trace-titles">
            <div className="trace-title">
              {ev.title}
              {expandable && <span className="caret">{open ? "▾" : "▸"}</span>}
            </div>
            <div className="trace-stage">{STAGE_LABEL[ev.stage]}</div>
          </div>
          <div className="trace-right">
            {ev.confidence != null && <ConfidenceBar value={ev.confidence} />}
            {ev.tokens > 0 && (
              <span className="chip chip-cost" title="tokens · est. cost">
                {fmtTokens(ev.tokens)} tok · {fmtUsd(ev.cost_usd)}
              </span>
            )}
          </div>
        </div>
        {ev.detail && <div className="trace-detail">{ev.detail}</div>}
        {open && ev.payload?.hypotheses && (
          <Hypotheses list={ev.payload.hypotheses} runbooks={ev.payload.runbooks} />
        )}
        {open && ev.payload?.proposal && <Steps proposal={ev.payload.proposal} />}
        {open && ev.payload?.checks && <Checks checks={ev.payload.checks} />}
      </div>
    </div>
  );
}

export default function TraceTimeline({ events }) {
  if (!events?.length) {
    return <div className="empty">Waiting for the agent to start…</div>;
  }
  return (
    <div className="trace">
      {events.map((ev, i) => (
        <EventRow key={i} ev={ev} />
      ))}
    </div>
  );
}
