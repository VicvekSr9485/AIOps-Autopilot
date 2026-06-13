#!/usr/bin/env bash
# Alibaba Cloud ECS bootstrap (paste into the instance "User Data" field, or run
# by hand on a fresh Ubuntu/Alibaba Cloud Linux instance). Idempotent and
# bounded: every step is safe to re-run and nothing blocks indefinitely.
#
# Provisions Docker, fetches the repo, and brings up the backend container,
# which then serves requests and forwards LLM calls to the Qwen Cloud
# (Alibaba Cloud) endpoint. Secrets are NEVER baked in — set DASHSCOPE_API_KEY
# in $APP_DIR/.env before serving real traffic.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/your-org/AIOps-Autopilot.git}"
BRANCH="${BRANCH:-main}"
APP_DIR="${APP_DIR:-/opt/aiops-autopilot}"

log() { echo "[ecs-bootstrap] $*"; }

# 1. Docker engine + compose plugin -------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  log "installing Docker"
  curl -fsSL --max-time 120 https://get.docker.com | sh
fi
systemctl enable --now docker

# 2. Source --------------------------------------------------------------------
if [ ! -d "$APP_DIR/.git" ]; then
  log "cloning $REPO_URL@$BRANCH -> $APP_DIR"
  git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
else
  log "updating existing checkout"
  git -C "$APP_DIR" pull --ff-only
fi
cd "$APP_DIR"

# 3. Secrets -------------------------------------------------------------------
# Populate from an Alibaba Cloud secret store / instance metadata, NOT from git.
if [ ! -f .env ]; then
  cp .env.example .env
  log "WARNING: wrote .env from .env.example — set DASHSCOPE_API_KEY (and keep"
  log "         AUTOPILOT_MOCK_LLM unset/0) before serving real traffic"
fi

# Anti-footgun: a real proof must NOT run in mock mode. Refuse, loudly.
if grep -qE '^[[:space:]]*AUTOPILOT_MOCK_LLM[[:space:]]*=[[:space:]]*1' .env; then
  log "FATAL: AUTOPILOT_MOCK_LLM=1 in .env — /api/cloud/selfcheck would be FAKE."
  log "       Remove it (or set =0) for a real Qwen Cloud proof, then re-run."
  exit 1
fi
if ! grep -qE '^[[:space:]]*DASHSCOPE_API_KEY=sk-[^[:space:]]' .env; then
  log "FATAL: DASHSCOPE_API_KEY not set to a real key in .env — cannot reach Qwen Cloud."
  exit 1
fi

# 4. Build + run the backend ---------------------------------------------------
log "building and starting the backend"
docker compose -f deployment/docker-compose.yml up -d --build --wait

# 5. Verify (bounded) ----------------------------------------------------------
log "health check"
curl -fsS --max-time 10 http://localhost:8080/healthz && echo
log "Qwen Cloud proof — the LOUD banner below must say REAL / mocked=false:"
docker compose -f deployment/docker-compose.yml exec -T backend \
  python -m autopilot.cloud.qwen_live
log "done — if the banner above is green/REAL with a *.aliyuncs.com host, the"
log "       deployed backend reached Alibaba Cloud for real."
