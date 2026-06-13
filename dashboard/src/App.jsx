import React, { useCallback, useEffect, useRef, useState } from "react";
import { api, streamRun } from "./api.js";
import { STATUS_KIND, STATUS_LABEL, fmtTokens, fmtUsd, fmtTime } from "./format.js";
import TraceTimeline from "./components/TraceTimeline.jsx";
import ApprovalGate from "./components/ApprovalGate.jsx";
import Benchmark from "./components/Benchmark.jsx";

function StatusChip({ status }) {
  return (
    <span className={`chip status-chip kind-${STATUS_KIND[status]}`}>
      {status === "running" && <span className="spinner" />}
      {STATUS_LABEL[status] || status}
    </span>
  );
}

function Sidebar({ scenarios, runs, activeId, onStart, onSelect, starting }) {
  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-mark">⛑️</div>
        <div>
          <div className="brand-name">AIOps Autopilot</div>
          <div className="brand-sub">incident → resolution agent</div>
        </div>
      </div>

      <div className="side-h">Inject a scenario</div>
      <div className="scenario-list">
        {scenarios.map((s) => (
          <button
            key={s.fault_id}
            className="scenario-btn"
            disabled={starting}
            onClick={() => onStart(s.fault_id)}
          >
            <span className="scenario-fault">{s.fault_id}</span>
            <span className="scenario-title">{s.title}</span>
          </button>
        ))}
      </div>

      <div className="side-h">Recent runs</div>
      <div className="run-feed">
        {runs.length === 0 && <div className="muted small">No runs yet.</div>}
        {runs.map((r) => (
          <button
            key={r.id}
            className={`run-item ${r.id === activeId ? "active" : ""}`}
            onClick={() => onSelect(r.id)}
          >
            <div className="run-item-top">
              <span className="run-fault">{r.fault_id}</span>
              <StatusChip status={r.status} />
            </div>
            <div className="run-item-sub">
              <span>{fmtTime(r.started_at)}</span>
              <span>
                {fmtTokens(r.total_tokens)} tok · {fmtUsd(r.est_cost_usd)}
              </span>
            </div>
          </button>
        ))}
      </div>
    </aside>
  );
}

function RunHeader({ run }) {
  return (
    <div className="run-header">
      <div>
        <div className="run-title">{run.scenario_title}</div>
        <div className="muted small">
          {run.fault_id} · {run.id}
          {run.top_cause && <> · diagnosed: {run.top_cause}</>}
        </div>
      </div>
      <div className="run-header-right">
        <StatusChip status={run.status} />
        <span className="chip chip-cost">
          {fmtTokens(run.total_tokens)} tok · {fmtUsd(run.est_cost_usd)}
        </span>
      </div>
    </div>
  );
}

export default function App() {
  const [tab, setTab] = useState("live");
  const [scenarios, setScenarios] = useState([]);
  const [runs, setRuns] = useState([]);
  const [activeId, setActiveId] = useState(null);
  const [run, setRun] = useState(null);
  const [starting, setStarting] = useState(false);
  const [busy, setBusy] = useState(false);
  const closerRef = useRef(null);

  useEffect(() => {
    api.scenarios().then(setScenarios).catch(() => {});
    refreshRuns();
  }, []);

  const refreshRuns = useCallback(() => {
    api.runs().then(setRuns).catch(() => {});
  }, []);

  // Subscribe to the active run's live trace.
  useEffect(() => {
    if (!activeId) return;
    closerRef.current?.();
    api.run(activeId).then(setRun).catch(() => {});
    closerRef.current = streamRun(
      activeId,
      (snap) => {
        setRun(snap);
        setRuns((prev) =>
          prev.map((r) =>
            r.id === snap.id
              ? {
                  ...r,
                  status: snap.status,
                  total_tokens: snap.total_tokens,
                  est_cost_usd: snap.est_cost_usd,
                }
              : r
          )
        );
      },
      refreshRuns
    );
    return () => closerRef.current?.();
  }, [activeId, refreshRuns]);

  const onStart = async (faultId) => {
    setStarting(true);
    try {
      const summary = await api.startRun(faultId);
      setTab("live");
      setRuns((prev) => [summary, ...prev]);
      setActiveId(summary.id);
    } finally {
      setStarting(false);
    }
  };

  const onDecide = async (action, note, steps) => {
    setBusy(true);
    try {
      await api.decide(activeId, action, note, steps);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="layout">
      <Sidebar
        scenarios={scenarios}
        runs={runs}
        activeId={activeId}
        starting={starting}
        onStart={onStart}
        onSelect={setActiveId}
      />

      <main className="main">
        <div className="tabs">
          <button
            className={`tab ${tab === "live" ? "active" : ""}`}
            onClick={() => setTab("live")}
          >
            Live run
          </button>
          <button
            className={`tab ${tab === "benchmark" ? "active" : ""}`}
            onClick={() => setTab("benchmark")}
          >
            Benchmark
          </button>
        </div>

        {tab === "benchmark" ? (
          <Benchmark />
        ) : !run ? (
          <div className="welcome">
            <h1>Watch the agent work an incident</h1>
            <p className="muted">
              Inject a scenario from the left. The agent triages the ambiguous
              signals, cites evidence, proposes a risk-scored remediation, pauses
              at the human gate when it must, executes only in the sandbox, and
              verifies the fix — every step shown live with its token cost.
            </p>
          </div>
        ) : (
          <div className="run-view">
            <RunHeader run={run} />
            {run.approval && (
              <ApprovalGate approval={run.approval} onDecide={onDecide} busy={busy} />
            )}
            <TraceTimeline events={run.events} />
          </div>
        )}
      </main>
    </div>
  );
}
