import React, { useEffect, useState } from "react";
import { api } from "../api.js";
import { fmtTokens, fmtUsd, pct } from "../format.js";

const ROWS = [
  ["Root-cause top-1", (a) => pct(a.root_cause_top1_acc), true],
  ["Remediation correct", (a) => pct(a.remediation_correct_rate), true],
  ["Safe-outcome rate", (a) => pct(a.safe_outcome_rate), true],
  ["Auto-resolution", (a) => pct(a.auto_resolution_rate), true],
  ["False-remediation", (a) => pct(a.false_remediation_rate), false],
  ["Residual damage (left broken)", (a) => pct(a.residual_damage_rate), false],
  ["Tokens / incident", (a) => fmtTokens(a.tokens_mean), null],
  ["Est. cost / incident", (a) => fmtUsd(a.est_cost_usd / a.scenarios), null],
];

function Metric({ label, pipe, base, higherBetter }) {
  // winner highlight only when one is strictly better in the right direction
  let pipeWin = false;
  let baseWin = false;
  if (higherBetter !== null) {
    const p = parseFloat(pipe);
    const b = parseFloat(base);
    if (!Number.isNaN(p) && !Number.isNaN(b) && p !== b) {
      pipeWin = higherBetter ? p > b : p < b;
      baseWin = !pipeWin;
    }
  }
  return (
    <tr>
      <td className="metric-name">{label}</td>
      <td className={`metric-val ${pipeWin ? "win" : ""}`}>{pipe}</td>
      <td className={`metric-val ${baseWin ? "win" : ""}`}>{base}</td>
    </tr>
  );
}

export default function Benchmark() {
  const [report, setReport] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    api.benchmark().then(setReport).catch((e) => setErr(String(e)));
  }, []);

  if (err) return <div className="empty">Benchmark unavailable: {err}</div>;
  if (!report) return <div className="empty">Loading benchmark…</div>;

  const pipe = report.approaches.find((a) => a.approach === "pipeline");
  const base = report.approaches.find((a) => a.approach === "baseline");
  const ab = report.ablation;

  return (
    <div className="benchmark">
      <div className="bench-head">
        <h2>Staged pipeline vs. single-prompt baseline</h2>
        <div className="muted">
          {report.mode} run ·{" "}
          {Object.entries(report.models)
            .map(([r, m]) => `${r}→${m}`)
            .join(" · ")}{" "}
          · model consistency {report.model_consistency_ok ? "PASSED" : "FAILED"}
        </div>
      </div>

      <table className="bench-table">
        <thead>
          <tr>
            <th></th>
            <th>Pipeline</th>
            <th>Baseline</th>
          </tr>
        </thead>
        <tbody>
          {ROWS.map(([label, fn, hb]) => (
            <Metric
              key={label}
              label={label}
              pipe={fn(pipe)}
              base={fn(base)}
              higherBetter={hb}
            />
          ))}
        </tbody>
      </table>

      <div className="bench-cards">
        <div className="bench-card">
          <div className="bench-card-h">Damage containment</div>
          <div className="big-stat">
            <span className="win">{pct(pipe.residual_damage_rate)}</span>
            <span className="vs">vs</span>
            <span>{pct(base.residual_damage_rate)}</span>
          </div>
          <div className="muted">
            Fraction of incidents left altered/broken (acted, not resolved, not
            rolled back). The pipeline’s auto-rollback contains its failures; the
            gateless baseline leaves wrong mutations applied.
          </div>
        </div>

        {ab && (
          <div className="bench-card">
            <div className="bench-card-h">Summarization ablation</div>
            <div className="big-stat">
              <span className="win">{ab.mean_saving_pct.toFixed(1)}%</span>
              <span className="muted">tokens saved</span>
            </div>
            <table className="mini-table">
              <tbody>
                <tr>
                  <td>summarized</td>
                  <td>{fmtTokens(ab.mean_tokens_summarized)} / incident</td>
                </tr>
                <tr>
                  <td>raw context</td>
                  <td>{fmtTokens(ab.mean_tokens_raw)} / incident</td>
                </tr>
              </tbody>
            </table>
          </div>
        )}

        <div className="bench-card">
          <div className="bench-card-h">Run cost</div>
          <div className="big-stat">
            <span>{fmtUsd(report.cost.est_cost_usd)}</span>
            <span className="muted">{fmtTokens(report.cost.total_tokens)} tokens</span>
          </div>
          <div className="muted">
            {fmtTokens(report.cost.free_tokens_used)} free ·{" "}
            {fmtTokens(report.cost.voucher_tokens_used)} voucher. Local estimate;
            Qwen Cloud Usage is authoritative.
          </div>
        </div>
      </div>

      <h3 className="per-fault-h">Per-fault outcomes</h3>
      <table className="bench-table compact">
        <thead>
          <tr>
            <th>Fault</th>
            <th>Pipeline</th>
            <th>Baseline</th>
          </tr>
        </thead>
        <tbody>
          {Object.values(
            report.scenarios
              .filter((s) => s.context_mode === "summarized")
              .reduce((acc, s) => {
                acc[s.fault_id] = acc[s.fault_id] || { fault: s.fault_id };
                acc[s.fault_id][s.approach] = s.outcome;
                return acc;
              }, {})
          ).map((row) => (
            <tr key={row.fault}>
              <td className="metric-name">{row.fault}</td>
              <td className={`outcome o-${row.pipeline}`}>{row.pipeline}</td>
              <td className={`outcome o-${row.baseline}`}>{row.baseline}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
