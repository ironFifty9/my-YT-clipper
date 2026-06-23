"""
routes/clip.py — Flask Blueprint containing all HTTP endpoints.

Registered in app.py via app.register_blueprint(bp). Using a Blueprint keeps
route definitions separate from application setup and makes this module
independently testable with Flask's test client.

Endpoints exposed:
  GET  /                — service info (public, no auth required)
  POST /clip            — submit a clip job (requires X-Secret-Key header)
  POST /cancel/<id>     — cancel a running job (public; job UUID is the token)
  GET  /status/<id>     — poll the status of a submitted job (public)
  GET  /health          — liveness probe for Railway/Render (public)

Security on POST /clip:
  1. @require_secret   — validates the X-Secret-Key header (timing-safe compare)
  2. _YT_PATTERN       — allows only HTTPS YouTube URLs
  3. Daily chat limit  — rejects if chat has hit MAX_JOBS_PER_CHAT_DAY today
  4. CLIP_SEMAPHORE    — non-blocking capacity gate; returns 429 when full

POST /cancel is intentionally public: the job UUID (128-bit) is the
capability token — it is computationally infeasible to guess.
"""

import logging    # structured logging
import re         # YouTube URL allow-list pattern
import secrets    # constant-time string comparison for auth
import threading  # spawn worker threads
import uuid       # unique job IDs
from functools import wraps

from flask import Blueprint, jsonify, request

from config import MAX_JOBS_PER_CHAT_DAY, SECRET_KEY
from core.jobs import (
    CLIP_SEMAPHORE,
    cancel_job,
    chat_daily_count,
    create_job,
    finish_job,
    get_job,
    job_count,
    prune_old_jobs,
    record_chat_job,
)
from core.telegram import tg_send
from core.utils import progress_bar
from core.worker import process_clip

log = logging.getLogger(__name__)
bp  = Blueprint("clip", __name__)


# ── YouTube URL allow-list ─────────────────────────────────────────────────────
# Accepts only HTTPS URLs for YouTube properties.
# Domains matched: youtube.com/watch|shorts|live, youtu.be/, m.youtube.com/watch,
#                  music.youtube.com/
_YT_PATTERN = re.compile(
    r"^https://(www\.)?"
    r"(youtube\.com/(watch|shorts|live)|youtu\.be/|m\.youtube\.com/watch|music\.youtube\.com/)"
)


# ── Authentication decorator ───────────────────────────────────────────────────

def require_secret(f):
    """
    Route decorator that enforces API key authentication via the X-Secret-Key
    HTTP header using a timing-safe constant-time comparison.

    - Header present and correct → route function is called.
    - Header missing or wrong   → 401 Unauthorized (caller IP is logged).
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        provided = request.headers.get("X-Secret-Key", "")
        if not secrets.compare_digest(provided, SECRET_KEY):
            log.warning("Unauthorized /clip attempt from %s", request.remote_addr)
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper


# ── Routes ─────────────────────────────────────────────────────────────────────

@bp.route("/", methods=["GET"])
def index():
    """
    GET / — Service discovery.
    Returns a JSON summary of all available endpoints. No auth required.
    """
    return jsonify({
        "service": "YouTube Clipper Bot",
        "status":  "running",
        "endpoints": {
            "POST /clip":          "Submit a clip job (requires X-Secret-Key header)",
            "POST /cancel/<id>":   "Cancel a running job (public; UUID is the token)",
            "GET  /status/<id>":   "Check job status",
            "GET  /health":        "Health check + active job count",
        },
        "usage": "Send /clip <url> <start> <end> [filename] [mp3] to your Telegram bot",
    })


@bp.route("/clip", methods=["POST"])
@require_secret
def clip():
    """
    POST /clip — Submit a new clip job.

    Required headers:
      X-Secret-Key: <SECRET_KEY>

    Request body (JSON):
      {
        "url":       "https://youtu.be/...",
        "start":     "1:30",
        "end":       "2:45",
        "filename":  "my_clip",          // optional
        "format":    "mp4",              // "mp4" | "mp3"  (default: "mp4")
        "bot_token": "123456:ABC...",
        "chat_id":   "987654321"
      }

    Success response (202-style):
      { "job_id": "<uuid>", "status": "processing" }

    Error responses:
      400 — missing / invalid fields or unrecognised YouTube URL
      401 — missing or wrong X-Secret-Key header
      429 — daily chat limit reached, or server at full capacity
      502 — could not reach Telegram to send the initial status message
    """
    data = request.get_json(silent=True) or {}

    # ── Required-field validation ──────────────────────────────────────────────
    missing = [
        f for f in ("url", "start", "end", "bot_token", "chat_id")
        if not data.get(f)
    ]
    if missing:
        return jsonify({"error": f"Missing required fields: {', '.join(missing)}"}), 400

    # ── Format validation ──────────────────────────────────────────────────────
    fmt = data.get("format", "mp4").lower()
    if fmt not in ("mp4", "mp3"):
        return jsonify({"error": "format must be 'mp4' or 'mp3'"}), 400

    # ── URL allow-list ─────────────────────────────────────────────────────────
    url = data["url"].strip()
    if not _YT_PATTERN.match(url):
        return jsonify({
            "error": (
                "Invalid or unsupported YouTube URL. "
                "Must start with https://youtube.com/ or https://youtu.be/"
            )
        }), 400

    # ── Per-chat daily rate limit ──────────────────────────────────────────────
    # Check *before* the semaphore so a chat that is over-limit does not consume
    # a concurrency slot.  The count is the number of jobs submitted by this
    # chat_id within the last 24 rolling hours.
    daily = chat_daily_count(data["chat_id"])
    if daily >= MAX_JOBS_PER_CHAT_DAY:
        return jsonify({
            "error": (
                f"Daily limit reached. This chat can submit at most "
                f"{MAX_JOBS_PER_CHAT_DAY} clips per 24-hour window. "
                "Try again later."
            ),
            "daily_used":    daily,
            "daily_limit":   MAX_JOBS_PER_CHAT_DAY,
            "retry_after":   86400,   # hint: try again in 24 hours
        }), 429

    # ── Global concurrency gate ────────────────────────────────────────────────
    # Non-blocking acquire: returns False immediately if all slots are taken.
    if not CLIP_SEMAPHORE.acquire(blocking=False):
        return jsonify({
            "error":       "Server is busy processing other clips. Please try again shortly.",
            "retry_after": 30,
        }), 429

    # ── Create job ────────────────────────────────────────────────────────────
    job_id = str(uuid.uuid4())
    create_job(job_id, data["chat_id"])

    # ── Record daily usage ────────────────────────────────────────────────────
    # Called after job creation (not before) so the count only includes
    # confirmed submissions that consumed a concurrency slot.
    record_chat_job(data["chat_id"])

    # ── Send initial Telegram status message ───────────────────────────────────
    resp   = tg_send(
        data["bot_token"],
        data["chat_id"],
        f"🎬 *Processing your clip…*\n{progress_bar(0)}",
    )
    msg_id = resp.get("result", {}).get("message_id")

    if not msg_id:
        # Telegram rejected/unreachable — release slot and abort.
        CLIP_SEMAPHORE.release()
        finish_job(job_id, error="Failed to contact Telegram")
        return jsonify({
            "error": "Failed to contact Telegram. Check bot_token and chat_id."
        }), 502

    log.info(
        "Job %s created | url=%.80s | fmt=%s | start=%s | end=%s | chat=%s",
        job_id, url, fmt, data["start"], data["end"], data["chat_id"],
    )

    # ── Spawn worker thread ────────────────────────────────────────────────────
    # CLIP_SEMAPHORE is already acquired; the worker releases it in its finally.
    thread = threading.Thread(
        target=process_clip,
        args=(
            job_id,
            url,
            data["start"],
            data["end"],
            data.get("filename", ""),
            fmt,
            data["bot_token"],
            data["chat_id"],
            msg_id,
        ),
        daemon=True,
        name=f"clip-{job_id[:8]}",
    )
    thread.start()

    return jsonify({"job_id": job_id, "status": "processing"})


@bp.route("/cancel/<job_id>", methods=["POST"])
def cancel(job_id: str):
    """
    POST /cancel/<job_id> — Request cancellation of a running job.

    No authentication required: the job UUID (128-bit) is the capability token.
    A UUID cannot be guessed without prior knowledge of the job_id.

    Responses:
      200 — cancel signal sent; worker will stop at the next checkpoint
      404 — job not found (unknown ID or already pruned)
      409 — job is not in 'processing' state (already done / errored)
    """
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    if job.get("status") != "processing":
        return jsonify({
            "error":  f"Job cannot be cancelled (status: '{job['status']}'). "
                      "Only jobs with status 'processing' can be cancelled.",
            "status": job["status"],
        }), 409

    if not cancel_job(job_id):
        # Race condition: job finished between the get_job check and cancel_job.
        return jsonify({"error": "Job finished before cancel could be applied"}), 409

    log.info("Cancel requested for job %s", job_id)
    return jsonify({"job_id": job_id, "status": "cancel_requested"})


@bp.route("/status/<job_id>", methods=["GET"])
def status(job_id: str):
    """
    GET /status/<job_id> — Poll the current state of a clip job.

    No authentication required (job UUID is a capability token).

    Returns the full job state dict, e.g.:
      {"status": "processing", "created_at": 1700000000.0}
      {"status": "done",       "created_at": ..., "finished_at": ...}
      {"status": "error",      "created_at": ..., "finished_at": ..., "error": "..."}

    Returns 404 if the job_id is unknown or has been pruned from memory.
    """
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@bp.route("/health", methods=["GET"])
def health():
    """
    GET /health — Liveness probe for Railway/Render health checks.

    Returns {"status": "ok", "jobs": <total jobs in store>}.
    Also opportunistically prunes stale jobs on each call.
    """
    prune_old_jobs()
    return jsonify({"status": "ok", "jobs": job_count()})
