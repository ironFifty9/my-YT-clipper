"""
core/jobs.py — Job store with automatic in-memory / Redis backend selection.

The backend is chosen **once at import time** based on whether REDIS_URL is set:

  REDIS_URL set   → _RedisBackend   (safe for gunicorn workers > 1)
  REDIS_URL unset → _InMemoryBackend (single-process only; original behaviour)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Public API (all callers use these functions regardless of backend)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  create_job(job_id, chat_id)  — register a new job
  get_job(job_id)              — return a copy of job state, or None
  finish_job(job_id, *, error) — mark as done / error
  cancel_job(job_id)           — request cancellation; True if signal was sent
  is_job_cancelled(job_id)     — True if a cancel has been requested
  prune_old_jobs()             — evict stale finished jobs (no-op for Redis)
  job_count()                  — total jobs currently in the store
  record_chat_job(chat_id)     — record a submission for daily rate-limiting
  chat_daily_count(chat_id)    — count submissions for this chat in last 24 h
  start_pruner(interval)       — start the background pruner daemon
  CLIP_SEMAPHORE               — shared semaphore (same .acquire/.release API)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Redis semaphore (token-pool pattern)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  A Redis list `yt_clipper:semaphore` holds MAX_JOBS tokens.
  LPOP to acquire (returns None when empty → busy → 429).
  RPUSH to release.
  Both operations are atomically handled by Redis — no Lua needed.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Per-chat daily rate limiting
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  In-memory: CHAT_DAILY dict maps chat_id → list[float] (Unix timestamps).
  Redis:     sorted set `yt_clipper:daily:{chat_id}` with score=timestamp.
  Both use a rolling 24-hour window.
"""

import json
import logging
import threading
import time
from typing import Any

from config import MAX_JOBS, REDIS_URL

log = logging.getLogger(__name__)

# Finished jobs are kept for this many seconds before pruning.
JOBS_TTL = 3600       # 1 hour

_DAY_SECS = 86_400    # seconds in 24 hours


# ══════════════════════════════════════════════════════════════════════════════
# Redis semaphore (token-pool via Redis list — same interface as threading.Semaphore)
# ══════════════════════════════════════════════════════════════════════════════

class _RedisSemaphore:
    """
    Distributed semaphore backed by a Redis list used as a token pool.

    Acquire:  LPOP yt_clipper:semaphore  → token (acquired) or None (busy)
    Release:  RPUSH yt_clipper:semaphore 1

    The list is initialised with MAX_JOBS tokens at startup. It persists in
    Redis so token count survives a single-process restart.  If the key is
    missing (Redis was flushed / first run), it is re-initialised.
    """

    _KEY = "yt_clipper:semaphore"

    def __init__(self, r: Any, max_jobs: int) -> None:
        self._r = r
        self._max = max_jobs
        self._ensure_pool()

    def _ensure_pool(self) -> None:
        """Initialise the token pool if the key does not already exist."""
        if not self._r.exists(self._KEY):
            # RPUSH with unpacked list: pushes max_jobs copies of 1.
            self._r.rpush(self._KEY, *([1] * self._max))

    def acquire(self, blocking: bool = True) -> bool:
        if blocking:
            # BLPOP blocks indefinitely until a token is available.
            result = self._r.blpop(self._KEY)
            return result is not None
        # Non-blocking: LPOP returns None when the list is empty.
        return self._r.lpop(self._KEY) is not None

    def release(self) -> None:
        """Return a token to the pool."""
        self._r.rpush(self._KEY, 1)


# ══════════════════════════════════════════════════════════════════════════════
# In-Memory Backend
# ══════════════════════════════════════════════════════════════════════════════

class _InMemoryBackend:
    """
    Thread-safe in-memory job store.

    Must run inside a **single gunicorn worker process** (workers=1 in
    gunicorn.conf.py).  All state lives in Python dicts — nothing is shared
    across OS processes.
    """

    def __init__(self) -> None:
        # Main job store: job_id → job-state dict
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

        # Counting semaphore: caps the number of concurrently running workers.
        self.semaphore = threading.Semaphore(MAX_JOBS)

        # Daily rate-limit tracker: chat_id → list of Unix timestamps (floats).
        # Each float is the creation time of one submitted job.
        self._chat_daily: dict[str, list[float]] = {}

    # ── CRUD helpers ──────────────────────────────────────────────────────────

    def create_job(self, job_id: str, chat_id: str) -> dict:
        """Register a new job in PROCESSING state and return a public copy."""
        with self._lock:
            job: dict[str, Any] = {
                "status":     "processing",
                "chat_id":    chat_id,
                "created_at": time.time(),
                # Private key — stripped by get_job(); holds the cancel signal.
                "_cancel_event": threading.Event(),
            }
            self._jobs[job_id] = job
        # Return a copy without private keys so callers cannot access internals.
        return {k: v for k, v in job.items() if not k.startswith("_")}

    def get_job(self, job_id: str) -> dict | None:
        """
        Return a shallow copy of the job state dict (private keys excluded),
        or None if the job is not found / has been pruned.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            return {k: v for k, v in job.items() if not k.startswith("_")}

    def finish_job(self, job_id: str, *, error: str | None = None) -> None:
        """Mark a job as 'done' (no error) or 'error' (error message given)."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job["status"]      = "error" if error else "done"
                job["finished_at"] = time.time()
                if error:
                    job["error"] = error

    def cancel_job(self, job_id: str) -> bool:
        """
        Signal the worker for this job to stop.

        Sets the private threading.Event without changing the visible status
        (the worker's except block calls finish_job when it detects the event).

        Returns True  if the job was in 'processing' state (signal sent).
        Returns False if the job is finished, already cancelled, or not found.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.get("status") != "processing":
                return False
            event: threading.Event = job["_cancel_event"]
            event.set()
        return True

    def is_job_cancelled(self, job_id: str) -> bool:
        """Return True if a cancel signal has been set for this job."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            event: threading.Event | None = job.get("_cancel_event")
            return bool(event and event.is_set())

    def prune_old_jobs(self) -> None:
        """
        Remove finished jobs whose finished_at timestamp is older than JOBS_TTL.
        'processing' jobs are never pruned — only 'done' and 'error' states.
        """
        now = time.time()
        with self._lock:
            stale = [
                jid for jid, j in self._jobs.items()
                if j.get("status") in ("done", "error")
                and now - j.get("finished_at", 0) > JOBS_TTL
            ]
            for jid in stale:
                del self._jobs[jid]
        if stale:
            log.info("Pruned %d stale job(s)", len(stale))

    def job_count(self) -> int:
        """Total number of jobs currently in the store."""
        with self._lock:
            return len(self._jobs)

    # ── Daily rate limiting ────────────────────────────────────────────────────

    def record_chat_job(self, chat_id: str) -> None:
        """
        Record a new job submission for this chat_id.
        Old timestamps (> 24 h) are pruned opportunistically on each call.
        """
        now = time.time()
        with self._lock:
            times = self._chat_daily.setdefault(chat_id, [])
            # Prune stale entries before appending the new one.
            self._chat_daily[chat_id] = [t for t in times if now - t < _DAY_SECS]
            self._chat_daily[chat_id].append(now)

    def chat_daily_count(self, chat_id: str) -> int:
        """
        Return the number of jobs this chat has submitted in the last 24 hours.
        Also prunes stale timestamps as a side effect.
        """
        now = time.time()
        with self._lock:
            times = self._chat_daily.get(chat_id, [])
            fresh = [t for t in times if now - t < _DAY_SECS]
            self._chat_daily[chat_id] = fresh   # prune in-place
            return len(fresh)


# ══════════════════════════════════════════════════════════════════════════════
# Redis Backend
# ══════════════════════════════════════════════════════════════════════════════

class _RedisBackend:
    """
    Redis-backed job store. Safe for use with gunicorn workers > 1.

    Job data:        JSON string at  yt_clipper:job:{job_id}
    Cancel flag:     Redis key       yt_clipper:cancel:{job_id}
    Daily counts:    Redis sorted set  yt_clipper:daily:{chat_id}
                     (members are string timestamps; scores are Unix timestamps)

    Redis TTLs handle expiry automatically; prune_old_jobs() is a no-op.
    """

    _JOB_PFX    = "yt_clipper:job:"
    _CANCEL_PFX = "yt_clipper:cancel:"
    _DAILY_PFX  = "yt_clipper:daily:"

    def __init__(self, r: Any) -> None:
        self._r = r

    # ── Key helpers ───────────────────────────────────────────────────────────

    def _job_key(self, job_id: str) -> str:
        return self._JOB_PFX + job_id

    def _cancel_key(self, job_id: str) -> str:
        return self._CANCEL_PFX + job_id

    def _daily_key(self, chat_id: str) -> str:
        return self._DAILY_PFX + chat_id

    # ── CRUD helpers ──────────────────────────────────────────────────────────

    def create_job(self, job_id: str, chat_id: str) -> dict:
        job: dict[str, Any] = {
            "status":     "processing",
            "chat_id":    chat_id,
            "created_at": time.time(),
        }
        # Store for up to 24 h; TTL is reset to JOBS_TTL when the job finishes.
        self._r.set(self._job_key(job_id), json.dumps(job), ex=_DAY_SECS)
        return dict(job)

    def get_job(self, job_id: str) -> dict | None:
        raw = self._r.get(self._job_key(job_id))
        if raw is None:
            return None
        return json.loads(raw)

    def finish_job(self, job_id: str, *, error: str | None = None) -> None:
        raw = self._r.get(self._job_key(job_id))
        if raw is None:
            return
        job = json.loads(raw)
        job["status"]      = "error" if error else "done"
        job["finished_at"] = time.time()
        if error:
            job["error"] = error
        # Shorten TTL to JOBS_TTL now that the job is finished.
        self._r.set(self._job_key(job_id), json.dumps(job), ex=JOBS_TTL)

    def cancel_job(self, job_id: str) -> bool:
        """Set the cancel flag key. Worker polls is_job_cancelled() to detect it."""
        raw = self._r.get(self._job_key(job_id))
        if raw is None:
            return False
        job = json.loads(raw)
        if job.get("status") != "processing":
            return False
        # Write the cancel signal key; the worker reads it via is_job_cancelled().
        self._r.set(self._cancel_key(job_id), "1", ex=JOBS_TTL)
        return True

    def is_job_cancelled(self, job_id: str) -> bool:
        return bool(self._r.exists(self._cancel_key(job_id)))

    def prune_old_jobs(self) -> None:
        """No-op: Redis TTLs automatically expire stale job keys."""

    def job_count(self) -> int:
        # KEYS is O(n) but acceptable for health checks; use SCAN in high-scale.
        return len(self._r.keys(self._JOB_PFX + "*"))

    # ── Daily rate limiting ────────────────────────────────────────────────────

    def record_chat_job(self, chat_id: str) -> None:
        """
        Add a timestamped entry to the chat's sorted set.
        The set expires after 24 h if no new jobs arrive (refreshed on each call).
        """
        now = time.time()
        key = self._daily_key(chat_id)
        # Use the string representation of the timestamp as a unique member.
        self._r.zadd(key, {str(now): now})
        self._r.expire(key, _DAY_SECS)   # refresh TTL on each new job

    def chat_daily_count(self, chat_id: str) -> int:
        """Count members in the sorted set whose score is within the last 24 h."""
        now = time.time()
        key = self._daily_key(chat_id)
        # Prune stale entries before counting (keeps the set lean).
        self._r.zremrangebyscore(key, 0, now - _DAY_SECS)
        count = self._r.zcount(key, now - _DAY_SECS, now)
        return int(count)


# ══════════════════════════════════════════════════════════════════════════════
# Backend initialisation
# ══════════════════════════════════════════════════════════════════════════════

def _init_backend() -> tuple[Any, Any]:
    """
    Select and initialise the appropriate backend at import time.

    Returns (backend_instance, semaphore_instance).
    Falls back to in-memory if REDIS_URL is set but the connection fails.
    """
    if REDIS_URL:
        try:
            import redis as _redis
            r = _redis.from_url(REDIS_URL, decode_responses=True)
            r.ping()   # Verify connectivity — raises redis.ConnectionError on failure.
            log.info("Redis backend initialised: %s", REDIS_URL)
            backend   = _RedisBackend(r)
            semaphore = _RedisSemaphore(r, MAX_JOBS)
            return backend, semaphore
        except Exception as exc:
            log.warning(
                "Redis connection failed (%s) — falling back to in-memory backend.",
                exc,
            )

    backend = _InMemoryBackend()
    log.info("In-memory job store initialised (single-process mode).")
    return backend, backend.semaphore


_backend, CLIP_SEMAPHORE = _init_backend()


# ══════════════════════════════════════════════════════════════════════════════
# Public API — all callers use these regardless of backend
# ══════════════════════════════════════════════════════════════════════════════

def create_job(job_id: str, chat_id: str) -> dict:
    """
    Register a new job in PROCESSING state.

    Args:
        job_id:  UUID string identifying this job.
        chat_id: Telegram chat ID — stored for rate-limit accounting.

    Returns the newly created public job dict (no private keys).
    """
    return _backend.create_job(job_id, chat_id)


def get_job(job_id: str) -> dict | None:
    """
    Look up a job by ID and return a shallow copy of its state,
    or None if the job is not found or has been pruned.
    """
    return _backend.get_job(job_id)


def finish_job(job_id: str, *, error: str | None = None) -> None:
    """
    Mark a job as completed ('done') or failed ('error').

    Args:
        job_id: The UUID of the job to update.
        error:  Error message string → status becomes 'error'.
                None → status becomes 'done'.
    """
    _backend.finish_job(job_id, error=error)


def cancel_job(job_id: str) -> bool:
    """
    Request cancellation for a running job.

    Sets an internal cancel flag that the worker checks at each pipeline
    checkpoint.  The job status changes to 'error' (message: 'Cancelled by
    user') when the worker detects the flag and exits cleanly.

    Returns:
        True  — cancel signal was sent (job was in 'processing' state).
        False — job is already finished, unknown, or not cancellable.
    """
    return _backend.cancel_job(job_id)


def is_job_cancelled(job_id: str) -> bool:
    """
    Return True if a cancel has been requested for this job.
    Called frequently by core/worker.py at each pipeline checkpoint.
    """
    return _backend.is_job_cancelled(job_id)


def prune_old_jobs() -> None:
    """
    Evict finished jobs older than JOBS_TTL from the in-memory store.
    No-op for the Redis backend (TTLs handle expiry automatically).
    """
    _backend.prune_old_jobs()


def job_count() -> int:
    """Return the total number of jobs currently tracked in the store."""
    return _backend.job_count()


def record_chat_job(chat_id: str) -> None:
    """
    Record a new clip submission for `chat_id`.
    Must be called **after** a job is successfully created (not before the
    rate-limit check) so that the count reflects confirmed submissions.
    """
    _backend.record_chat_job(chat_id)


def chat_daily_count(chat_id: str) -> int:
    """
    Return the number of clip jobs this chat has submitted in the last 24 hours.
    Used by routes/clip.py to enforce MAX_JOBS_PER_CHAT_DAY.
    """
    return _backend.chat_daily_count(chat_id)


def start_pruner(interval: int = 300) -> None:
    """
    Start a background daemon thread that calls prune_old_jobs() every
    `interval` seconds (default 300 s = 5 minutes).

    For the Redis backend this thread still runs but prune_old_jobs() is a
    no-op, so it is harmless.  It is kept for consistency and to provide a
    single startup code path in app.py.

    Call this exactly once at application startup (see app.py).
    """
    def _loop() -> None:
        # Sleep first — no jobs to prune right after startup.
        while True:
            time.sleep(interval)
            prune_old_jobs()

    t = threading.Thread(target=_loop, daemon=True, name="job-pruner")
    t.start()
    log.info("Job pruner started (interval=%ds, TTL=%ds)", interval, JOBS_TTL)
