# Deploying the AIOps Autopilot backend on Alibaba Cloud

This directory contains everything needed to run the backend on **Alibaba Cloud
compute (ECS)** with its LLM requests going to the **Qwen Cloud** (Model Studio
/ DashScope) inference endpoint on `*.aliyuncs.com`.

```
                Alibaba Cloud ECS instance
   ┌───────────────────────────────────────────────┐
   │  docker compose (deployment/docker-compose.yml) │
   │   ┌─────────────────────────────────────────┐   │      HTTPS
   │   │ autopilot-backend  (Dockerfile)          │   │   ┌────────────────────────────┐
   │   │  uvicorn autopilot.api.app:app  :8080    │───┼──▶│ Qwen Cloud / DashScope      │
   │   │  • /healthz                              │   │   │ dashscope-intl.aliyuncs.com │
   │   │  • /api/cloud/selfcheck  (live proof)    │   │   │  qwen3.7-max / qwen3.7-plus │
   │   │  • /api/runs ... (incident pipeline)     │   │   └────────────────────────────┘
   │   └─────────────────────────────────────────┘   │
   │   (no Docker socket mounted → no host access)    │
   └───────────────────────────────────────────────┘
```

## What proves the cloud integration

- **Proof file:** [`src/autopilot/cloud/qwen_live.py`](../src/autopilot/cloud/qwen_live.py)
  — makes one real, metered chat completion against the Qwen Cloud endpoint and
  reports the resolved Alibaba Cloud host, region, model tiering, tokens, cost,
  and latency.
- **Deployed endpoint:** `GET /api/cloud/selfcheck` runs that proof on the live
  backend. `ok: true` with a non-`aliyuncs`-free `cloud_host` is the deployment
  proof.
- **CLI / health gate:** `python -m autopilot.cloud.qwen_live` prints the report
  and exits non-zero if the live call fails.

## Prerequisites

- An Alibaba Cloud account with **Model Studio / DashScope** enabled and an API
  key (`DASHSCOPE_API_KEY`).
- An ECS instance (Ubuntu 22.04 or Alibaba Cloud Linux 3; `ecs.t6-c1m2.large` /
  2 vCPU 4 GiB is ample). Security group: inbound **8080** (or front it with an
  SLB/Nginx on 443).

## 1. Provision the instance

Create the ECS instance and paste [`ecs-cloud-init.sh`](./ecs-cloud-init.sh)
into the **User Data** field (or run it by hand after SSH). It installs Docker +
the compose plugin, clones this repo to `/opt/aiops-autopilot`, and brings up
the backend. Set `REPO_URL`/`BRANCH` at the top if you forked.

## 2. Configure secrets (no secrets in the repo)

Secrets live only in `.env` on the instance, which is **git-ignored** and
**`.dockerignore`d** — it is never committed and never copied into an image
layer. Copy the template and fill it in:

```bash
cd /opt/aiops-autopilot
cp .env.example .env
# edit .env:
#   DASHSCOPE_API_KEY=sk-...        # from the Alibaba Cloud console
#   AUTOPILOT_MOCK_LLM=0            # 0 = call the real Qwen Cloud endpoint
```

In production, source `DASHSCOPE_API_KEY` from **Alibaba Cloud Secrets Manager**
(KMS) or instance RAM-role metadata and write it into `.env` at boot — do not
hardcode it. See [`../.env.example`](../.env.example) for every supported
variable.

## 3. Deploy

```bash
cd /opt/aiops-autopilot
docker compose -f deployment/docker-compose.yml up -d --build --wait
```

`--wait` blocks until the container's healthcheck passes, so this is safe in a
provisioning script.

## 4. Verify

```bash
# Liveness
curl -fsS http://<ecs-public-ip>:8080/healthz
# -> {"status":"ok","version":"0.1.0"}

# Cloud integration — the deployment proof (ok:true == reached Alibaba Cloud)
curl -fsS http://<ecs-public-ip>:8080/api/cloud/selfcheck

# Full smoke test from your laptop: health + live Qwen check + one incident e2e
AUTOPILOT_SMOKE_BASE_URL=http://<ecs-public-ip>:8080 \
AUTOPILOT_SMOKE_REAL_CLOUD=1 \
  make smoke-deploy
```

The smoke test (`tests/test_deploy_smoke.py`) is **env-gated**: it is skipped
unless `AUTOPILOT_SMOKE_BASE_URL` is set, so `make test` never runs it
accidentally or spends tokens.

## Real-sandbox executor cycles (and the sandbox-only guarantee)

The HTTP API drives an **in-process MockWorld** — no Docker, no tokens — so the
public service has no path to the host or any container. The deploy compose
mounts **no Docker socket**, so even a compromised API process cannot reach the
host Docker daemon.

The full real-model loop (real Qwen + real Docker fault injection + the executor
acting on the sandbox) runs **on the ECS host**, where the repo and Docker
daemon live:

```bash
cd /opt/aiops-autopilot
make install            # one-time: venv + deps for host-side runs
make sandbox-up         # bring up sandbox/docker-compose.yml (the ONLY target)
make bench-real         # real Qwen models + real sandbox fault cycles
```

The **sandbox-only guard holds in this environment by construction**, not by
configuration:

1. `SandboxController` only ever runs `docker compose -f sandbox/docker-compose.yml …`
   — it has no code path to any other project, the host, or external systems.
2. Every mutating MCP tool's target is a closed `Literal`
   (`guards.SandboxService`), and `guards.ensure_sandbox_service()` re-validates
   at runtime. A target naming anything outside the five sandbox services is
   rejected before the tool body runs.
3. These are pinned to `sandbox/docker-compose.yml` by a test
   (`test_service_allowlist_matches_compose_file`) and re-asserted by the deploy
   smoke test's `test_executor_sandbox_guard_holds`.

## Hardening summary

- **Timeouts:** every Qwen Cloud call has a wall-clock timeout
  (`AUTOPILOT_LLM_TIMEOUT_S`, default 30s); sandbox HTTP probes use bounded
  `httpx` timeouts.
- **Bounded retries:** transient LLM failures (429/5xx/timeout) retry with
  exponential backoff up to `AUTOPILOT_LLM_MAX_RETRIES` (default 2), then raise
  a typed `QwenCallError` — never an unbounded loop.
- **Graceful failure:** `/api/cloud/selfcheck` returns `200` with `ok:false` and
  an `error` string on connectivity failure (not a 500); pipeline stage errors
  are caught and surfaced as failed runs.
- **Cost ceiling:** `AUTOPILOT_RUN_TOKEN_CAP` refuses the next LLM call once a
  run crosses the cap — a deployed runaway cannot drain the budget.

## Reproducible deploy (copy-paste)

```bash
# On a fresh Alibaba Cloud ECS instance (Ubuntu/Alibaba Cloud Linux):
export REPO_URL=https://github.com/your-org/AIOps-Autopilot.git
curl -fsSL "$REPO_URL/raw/main/deployment/ecs-cloud-init.sh" | sudo -E bash
#   (or: scp this repo up, then `sudo deployment/ecs-cloud-init.sh`)

# Set the real key and switch off mock mode:
sudo sed -i 's/^DASHSCOPE_API_KEY=.*/DASHSCOPE_API_KEY=sk-REAL/' /opt/aiops-autopilot/.env
sudo sed -i 's/^AUTOPILOT_MOCK_LLM=.*/AUTOPILOT_MOCK_LLM=0/'     /opt/aiops-autopilot/.env
cd /opt/aiops-autopilot && sudo docker compose -f deployment/docker-compose.yml up -d --build --wait

# Confirm the deployment reached Alibaba Cloud:
curl -fsS http://localhost:8080/api/cloud/selfcheck
```
