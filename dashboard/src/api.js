// Thin client over the FastAPI backend. Paths are same-origin (Vite proxies
// /api -> :8080 in dev; served together in prod).

async function jget(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return r.json();
}

async function jpost(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return r.json();
}

export const api = {
  scenarios: () => jget("/api/scenarios"),
  runs: () => jget("/api/runs"),
  run: (id) => jget(`/api/runs/${id}`),
  startRun: (faultId) => jpost("/api/runs", { fault_id: faultId }),
  decide: (id, action, note = "", steps = null) =>
    jpost(`/api/runs/${id}/decision`, { action, note, steps }),
  benchmark: () => jget("/api/benchmark"),
};

// Subscribe to a run's live trace via SSE. Calls onSnapshot(detail) on each
// update and onDone() when the run reaches a terminal state. Returns a closer.
export function streamRun(id, onSnapshot, onDone) {
  const es = new EventSource(`/api/runs/${id}/stream`);
  es.onmessage = (e) => {
    try {
      onSnapshot(JSON.parse(e.data));
    } catch {
      /* ignore keep-alive / malformed frames */
    }
  };
  es.addEventListener("done", () => {
    es.close();
    onDone?.();
  });
  es.onerror = () => es.close();
  return () => es.close();
}
