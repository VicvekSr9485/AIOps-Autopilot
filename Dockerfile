# Backend container for the AIOps Autopilot API (FastAPI + the agent pipeline).
#
# Runs the demo/control surface (autopilot.api.app:app) and the Qwen Cloud
# live-proof route. Requests to the LLM go to the Qwen Cloud (Alibaba Cloud)
# endpoint configured via DASHSCOPE_BASE_URL / DASHSCOPE_API_KEY at runtime —
# never baked into the image.
#
# NOTE: this image intentionally does NOT bundle the Docker CLI. The HTTP API
# drives an in-process MockWorld (no Docker, no tokens). The real-sandbox
# executor cycles (`make bench-real`, sandbox tests) run on the ECS host where
# the Docker daemon and repo live — see deployment/DEPLOYMENT.md.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first (cached) using just the metadata, then the source.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
# Editable install keeps the package at /app/src/... so app.py's repo-root
# discovery (parents[3]) resolves to /app and the /api/benchmark route can read
# the artifacts copied below.
RUN pip install -e .

# Runtime data the API serves / the pipeline reads. Secrets are NOT copied
# (.env is excluded by .dockerignore and injected at runtime).
COPY benchmark_results_real_v2 ./benchmark_results_real_v2
COPY sandbox ./sandbox

# Drop privileges.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser /app
USER appuser

EXPOSE 8080

# Liveness: the API's own /healthz. Bounded so a hung process is detected.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status==200 else 1)"

CMD ["uvicorn", "autopilot.api.app:app", "--host", "0.0.0.0", "--port", "8080"]
