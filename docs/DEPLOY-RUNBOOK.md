# Deploy & Prove Runbook: real Qwen Cloud on Alibaba Cloud ECS

Turnkey, copy-pasteable path from a **fresh ECS instance** to a **green
`mocked:false`** proof that the deployed backend reached Qwen Cloud for real.

- Architecture, security-group, and rationale: [DEPLOYMENT.md](../deployment/DEPLOYMENT.md)
- Bootstrap script this runbook drives: [`deployment/ecs-cloud-init.sh`](../deployment/ecs-cloud-init.sh)
- The proof itself: [`src/autopilot/cloud/qwen_live.py`](../src/autopilot/cloud/qwen_live.py)
  → route `GET /api/cloud/selfcheck`

> **The one thing that matters:** the self-check must print **`mocked: false`** and
> a green **`REAL Qwen Cloud round-trip`** banner with a `*.aliyuncs.com` host. A
> `mocked: true` / yellow `MOCK MODE` banner proves **nothing**; see the
> anti-footgun section below. Everything here is engineered so the two cannot be
> confused on camera.

---

## 0 · Before touching ECS (2 min)

1. **Get a Qwen Cloud key** from the Alibaba Cloud console (Model Studio /
   DashScope) → `sk-...`.
2. **Note the key's region** and pick the matching base URL: they must agree or
   calls 401/404:
   - International (Singapore): `https://dashscope-intl.aliyuncs.com/compatible-mode/v1`
   - China (Beijing): `https://dashscope.aliyuncs.com/compatible-mode/v1`
3. **Fork/clone URL** of this repo (public); set `REPO_URL` below.

## 1 · Provision the ECS instance (5 min)

Create an ECS instance (Ubuntu 22.04 or Alibaba Cloud Linux 3; 2 vCPU / 4 GiB,
e.g. `ecs.t6-c1m2.large`). **Security group inbound:** TCP **22** (SSH) and TCP
**8080** (the API). Then either paste `ecs-cloud-init.sh` into the **User Data**
field at creation, or SSH in and run it by hand:

```bash
ssh root@<ECS_PUBLIC_IP>
export REPO_URL=https://github.com/ORG/AIOps-Autopilot.git   # the fork
curl -fsSL "$REPO_URL/raw/main/deployment/ecs-cloud-init.sh" -o /tmp/bootstrap.sh
sudo -E bash /tmp/bootstrap.sh        # installs Docker, clones, builds; will STOP at step 3 until the key is set
```

The script **refuses to proceed** to the proof if `AUTOPILOT_MOCK_LLM=1` or the
key is missing, so the first run writes `.env` from the template and stops. Good.

## 2 · Set the real env, server-side only, never committed (1 min)

Secrets live **only** in `/opt/aiops-autopilot/.env` on the instance. That file is
git-ignored and `.dockerignore`d; it is never committed and never baked into an
image layer. Set the key and the matching base URL, and make sure mock is OFF:

```bash
cd /opt/aiops-autopilot
sudo tee -a .env >/dev/null <<'EOF'
DASHSCOPE_API_KEY=sk-REPLACE_WITH_REAL_KEY
DASHSCOPE_BASE_URL=https://dashscope-intl.aliyuncs.com/compatible-mode/v1
EOF
# Force mock OFF (idempotent; also covers a stale value from the template):
sudo sed -i 's/^[[:space:]]*AUTOPILOT_MOCK_LLM[[:space:]]*=.*/AUTOPILOT_MOCK_LLM=0/' .env
grep -E 'DASHSCOPE_API_KEY|DASHSCOPE_BASE_URL|AUTOPILOT_MOCK_LLM' .env   # eyeball it
```

> In production, source the key from **Alibaba Cloud Secrets Manager (KMS)** or a
> RAM-role and write it into `.env` at boot; do not type it in by hand. Either
> way it stays out of git.

## 3 · Bring it up (2 min)

```bash
cd /opt/aiops-autopilot
sudo bash deployment/ecs-cloud-init.sh     # re-run: it now passes the mock/key guards
# (or directly:)
sudo docker compose -f deployment/docker-compose.yml up -d --build --wait
```

`--wait` blocks until the container healthcheck passes. The deploy compose arms
the resilience knobs by default: **timeout** (`AUTOPILOT_LLM_TIMEOUT_S=30`),
**bounded retries** (`AUTOPILOT_LLM_MAX_RETRIES=2`), and the **token cap**
(`AUTOPILOT_RUN_TOKEN_CAP=200000`). It deliberately does **not** set
`AUTOPILOT_MOCK_LLM`, so it can never ship a mock value.

Check the container's startup banner; it shouts the mode:

```bash
sudo docker logs autopilot-backend 2>&1 | grep "LLM MODE"
# want: ✅ REAL, cloud calls hit Qwen Cloud at https://...aliyuncs.com/...
```

## 4 · Prove it (the green moment)

```bash
# a) liveness
curl -fsS http://localhost:8080/healthz
# -> {"status":"ok","version":"0.1.0"}

# b) THE PROOF: loud banner from inside the container (green = REAL):
sudo docker compose -f deployment/docker-compose.yml exec -T backend \
  python -m autopilot.cloud.qwen_live

# c) the same as JSON over HTTP (note "mocked": false and the headline):
curl -fsS http://<ECS_PUBLIC_IP>:8080/api/cloud/selfcheck | jq '{headline, mocked, cloud_host, region, model, input_tokens, output_tokens, est_cost_usd, latency_ms}'
```

Expected (b)/(c): **`mocked: false`**, a `*.aliyuncs.com` `cloud_host`, a real
`region`, the `model` (`qwen3.7-plus`), non-zero tokens, an `est_cost_usd`, and a
`latency_ms` > 0, and the headline starts with **`REAL Qwen Cloud round-trip`**.

## 5 · Full real smoke (from a workstation, ~1 min, spends a few tokens)

Drives the real round-trip **and** one incident end-to-end through the deployed
pipeline. The `_REAL_CLOUD=1` flag makes it **fail loudly** if the backend is mock:

```bash
AUTOPILOT_SMOKE_BASE_URL=http://<ECS_PUBLIC_IP>:8080 \
AUTOPILOT_SMOKE_REAL_CLOUD=1 \
  make smoke-deploy
# prints:  CLOUD SELF-CHECK → REAL Qwen Cloud round-trip (mocked=false) ...
# and:     N passed
```

---

## Anti-footgun (why a mock run can't pass as real)

- The container **startup banner** prints `MOCK` (yellow) vs `REAL` (green).
- `/api/cloud/selfcheck` returns a **`headline`** field that says `MOCK MODE …` or
  `REAL Qwen Cloud round-trip (mocked=false) …`; the CLI frames it green/red.
- `ecs-cloud-init.sh` **refuses to run the proof** if `AUTOPILOT_MOCK_LLM=1` or the
  key is absent.
- `make smoke-deploy` with `AUTOPILOT_SMOKE_REAL_CLOUD=1` **fails the build** if the
  backend reports `mocked:true`.

## Pre-record checklist (tick every box before hitting record)

- [ ] **Key region matches base URL** (intl key ↔ `dashscope-intl…`; cn key ↔ `dashscope…`).
- [ ] `grep AUTOPILOT_MOCK_LLM /opt/aiops-autopilot/.env` shows **`=0`** (or absent).
- [ ] `grep DASHSCOPE_API_KEY .env` shows a real `sk-…` (not the placeholder).
- [ ] Security group **inbound 8080** open; `curl http://<IP>:8080/healthz` from a workstation returns `ok`.
- [ ] `docker logs autopilot-backend | grep "LLM MODE"` shows **✅ REAL**.
- [ ] A dry `curl …/api/cloud/selfcheck | jq .mocked` returns **`false`**.
- [ ] Terminal font large; `jq` installed; window wide enough that the banner isn't wrapped.

## Recording shot-list (~60-90 s of the 3-min video)

1. **Console (5s):** the running ECS instance + its public IP and region.
2. **Health (5s):** `curl http://<IP>:8080/healthz` → `{"status":"ok",...}`.
3. **The proof (15s):** run the in-container CLI (step 4b) → the **green REAL banner**;
   pause on `mocked: false` + the `*.aliyuncs.com` host + tokens/cost/latency.
4. **HTTP proof (10s):** `curl …/api/cloud/selfcheck | jq` → same JSON in the API response.
5. **Real smoke (15s):** run step 5 from a workstation → `CLOUD SELF-CHECK → REAL …`
   line and `passed`.
6. **(optional, 10s):** `docker logs … | grep "LLM MODE"` → ✅ REAL startup banner.

The link submitted as deployment proof should point at
[`src/autopilot/cloud/qwen_live.py`](../src/autopilot/cloud/qwen_live.py).

## Teardown

```bash
sudo docker compose -f deployment/docker-compose.yml down
# then stop/release the ECS instance from the console to stop billing.
```
