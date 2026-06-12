"""Seed runbook corpus for the knowledge store.

Written as general SRE knowledge an operator would have on file — aligned with
the failure classes the sandbox can exhibit, plus distractors so retrieval is a
real ranking problem. These are NOT copies of harness ground truth; FaultSpec
text stays in harness/faults.py and never enters agent-visible data paths.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Runbook(BaseModel):
    slug: str
    title: str
    body: str
    tags: list[str] = Field(default_factory=list)


SEED_RUNBOOKS: list[Runbook] = [
    Runbook(
        slug="postgres-connection-exhaustion",
        title="Postgres connection slots exhausted / too many clients",
        body=(
            "Symptoms: health checks report the database component down; application "
            "requests fail with 'remaining connection slots are reserved' or 'sorry, "
            "too many clients already'; errors appear without a deploy or config change.\n"
            "Diagnosis: inspect pg_stat_activity for idle or long-running sessions "
            "(state, query_start, usename); compare session count against max_connections; "
            "look for batch jobs or leaked pools holding connections open.\n"
            "Remediation: terminate the offending idle/long-running backends with "
            "pg_terminate_backend(pid) using an admin connection (superuser-reserved "
            "slots stay available), then confirm the app reconnects. Longer term: add "
            "connection pooling or lower client pool sizes.\n"
            "Risk: low — terminating idle sessions does not lose committed data."
        ),
        tags=["db", "postgres", "connections"],
    ),
    Runbook(
        slug="bad-config-rollout-rollback",
        title="Error spike immediately after a config rollout",
        body=(
            "Symptoms: request errors begin right after a configuration change or "
            "deploy; responses mention an invalid or unsupported setting value; "
            "dependency health checks stay green because infrastructure is fine.\n"
            "Diagnosis: diff the active config against the last known-good version; "
            "correlate the error start time with the rollout time; check app logs for "
            "validation failures naming the bad key.\n"
            "Remediation: roll the configuration back to the last known-good version "
            "and restart the affected service so it re-reads config. Verify error rate "
            "returns to baseline before re-attempting the change.\n"
            "Risk: low — rollback restores a previously working state."
        ),
        tags=["app", "config", "rollback"],
    ),
    Runbook(
        slug="downstream-dependency-timeout",
        title="Upstream requests timing out on a downstream dependency",
        body=(
            "Symptoms: requests fail with 504 gateway/downstream timeout; latency "
            "pins at the configured timeout ceiling; the calling service is otherwise "
            "healthy.\n"
            "Diagnosis: check whether the downstream service is running and responsive "
            "(container state, its own health endpoint); rule out network policy "
            "changes; confirm timeouts started when the dependency stopped responding.\n"
            "Remediation: restore the downstream service — unpause or restart its "
            "container — then confirm end-to-end requests succeed. Consider circuit "
            "breaking if the dependency is flaky.\n"
            "Risk: low — restarting the dependency affects only traffic already failing."
        ),
        tags=["downstream", "timeout", "latency"],
    ),
    Runbook(
        slug="queue-consumer-stall",
        title="Queue backlog growing while consumers sit idle",
        body=(
            "Symptoms: queue_depth grows monotonically; jobs_processed counter stops "
            "advancing; user-facing requests and health checks stay green — a silent "
            "backlog with no error logs.\n"
            "Diagnosis: compare producer and consumer rates over a window; check the "
            "worker/consumer process state (paused, crashed, deadlocked); verify the "
            "queue itself accepts reads.\n"
            "Remediation: restart (or unpause) the worker service so consumption "
            "resumes, then watch queue_depth drain and jobs_processed advance.\n"
            "Risk: low — jobs remain queued; restarting the consumer loses no work."
        ),
        tags=["worker", "queue", "backlog"],
    ),
    Runbook(
        slug="credential-rotation-failure",
        title="Authentication failures after a credential rotation",
        body=(
            "Symptoms: a service abruptly fails to reach its database or API with "
            "'password authentication failed' / 401-style errors; often follows a "
            "secret rotation that updated one side only.\n"
            "Diagnosis: check when the credential was last rotated; confirm whether "
            "the consuming service holds the old secret; look for auth failures in "
            "both client and server logs.\n"
            "Remediation: restore a valid credential pair — either reset the account "
            "password to the secret the service uses, or roll the new secret out to "
            "the service and restart it. Verify the dependency health check recovers.\n"
            "Risk: medium — touching credentials can lock out other consumers; "
            "coordinate the reset."
        ),
        tags=["db", "credentials", "auth"],
    ),
    Runbook(
        slug="config-rollout-wedged-consumer",
        title="Config rollout breaks requests AND wedges queue consumers",
        body=(
            "Symptoms: work_failed errors naming an invalid setting (e.g. "
            "invalid_feature_mode) right after a rollout, while queue_depth sits "
            "above zero without draining and jobs_processed stops advancing.\n"
            "Remediation: roll the configuration back AND restart the consumer "
            "as well — a consumer that loaded the bad config stays wedged after "
            "the rollback, so the backlog will not drain on its own. Verify both "
            "the error rate AND queue_depth recover.\n"
            "Diagnosis: correlate the error start with the rollout; check whether "
            "jobs_processed advances after the rollback — if not, the consumer is "
            "still wedged.\n"
            "Risk: low — rollback restores known-good state; restarting the "
            "consumer loses no queued work."
        ),
        tags=["app", "config", "worker", "queue"],
    ),
    Runbook(
        slug="consumer-scaled-to-zero",
        title="Queue consumer scaled to zero replicas",
        body=(
            "Symptoms: worker_shutdown (SIGTERM) in the logs and then silence; "
            "queue_depth climbing while jobs_processed stays flat; health checks "
            "green because nothing user-facing is failing yet.\n"
            "Remediation: scale the consumer back to its baseline replica count. "
            "IMPORTANT: restarting the service is a NO-OP when zero replicas "
            "exist — there is no container to restart; an explicit scale-up is "
            "required. Treat the scale-up as a capacity change (review before "
            "applying).\n"
            "Diagnosis: check the service's desired replica count vs running "
            "containers; a SIGTERM shutdown with no restart strongly suggests a "
            "deliberate scale-down or autoscaler action.\n"
            "Risk: medium — capacity changes are destructive-class operations; "
            "confirm the original scale-down was not intentional."
        ),
        tags=["worker", "queue", "capacity"],
    ),
    # ----- distractors: realistic runbooks for failure modes the sandbox can't have
    Runbook(
        slug="host-disk-pressure",
        title="Disk pressure / no space left on device",
        body=(
            "Symptoms: writes fail with 'no space left on device'; databases go "
            "read-only; log shipping stalls.\n"
            "Diagnosis: df -h per mount; find runaway log files or core dumps; check "
            "image/layer cache growth.\n"
            "Remediation: prune old artifacts and rotate logs, then re-enable writes; "
            "expand the volume if pressure recurs.\n"
            "Risk: medium — deleting the wrong files is unrecoverable; prune known-safe "
            "paths only."
        ),
        tags=["host", "disk"],
    ),
    Runbook(
        slug="cache-hit-rate-degradation",
        title="Cache hit rate collapse driving latency up",
        body=(
            "Symptoms: p99 latency climbs while error rate stays flat; cache hit rate "
            "drops after a key-schema change or cache node restart.\n"
            "Diagnosis: compare hit/miss ratios before and after the change; check for "
            "cold caches after restarts; look for key cardinality explosions.\n"
            "Remediation: warm the cache with the hot key set or roll back the "
            "key-schema change; scale read replicas while the cache refills.\n"
            "Risk: low — warming is additive; rollback follows the standard config path."
        ),
        tags=["cache", "latency"],
    ),
]
