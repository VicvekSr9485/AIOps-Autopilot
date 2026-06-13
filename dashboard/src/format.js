export const STATUS_LABEL = {
  running: "Running",
  awaiting_approval: "Needs approval",
  resolved: "Resolved",
  rolled_back: "Rolled back",
  rejected: "Escalated · rejected",
  failed: "Failed",
};

export const STATUS_KIND = {
  running: "info",
  awaiting_approval: "warn",
  resolved: "ok",
  rolled_back: "warn",
  rejected: "warn",
  failed: "error",
};

export const STAGE_ICON = {
  ingest: "📥",
  triage: "🔬",
  remediation: "🧭",
  gate: "🚦",
  execution: "⚙️",
  verification: "✅",
  rollback: "↩️",
  outcome: "🏁",
};

export const STAGE_LABEL = {
  ingest: "Ingestion",
  triage: "Triage · root cause",
  remediation: "Remediation plan",
  gate: "HITL gate",
  execution: "Executor",
  verification: "Verification",
  rollback: "Auto-rollback",
  outcome: "Outcome",
};

export const fmtTokens = (n) =>
  n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n ?? 0);

export const fmtUsd = (n) => `$${(n ?? 0).toFixed(4)}`;

export const pct = (n) => `${Math.round((n ?? 0) * 100)}%`;

export const fmtTime = (iso) => {
  try {
    return new Date(iso).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return iso;
  }
};
