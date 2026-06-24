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

import json
import logging    # structured logging
import re         # YouTube URL allow-list pattern
import secrets    # constant-time string comparison for auth
import threading  # spawn worker threads
import uuid       # unique job IDs
from functools import wraps

from flask import Blueprint, jsonify, request, render_template_string

from config import MAX_JOBS_PER_CHAT_DAY, SECRET_KEY, TELEGRAM_BOT_TOKEN, ADMIN_SECRET
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


# ── URL validation pattern ─────────────────────────────────────────────────────
# Relaxed validation to support multiple video platforms via yt-dlp.
# Accepts any HTTP/HTTPS URLs.
_URL_PATTERN = re.compile(
    r"^https?://[^\s/$.?#].[^\s]*$"
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

    # ── URL validation ─────────────────────────────────────────────────────────
    url = data["url"].strip()
    if not _URL_PATTERN.match(url):
        return jsonify({
            "error": (
                "Invalid URL format. Must start with http:// or https://"
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


@bp.route("/tg-webhook/<token>", methods=["POST"])
def tg_webhook(token: str):
    """
    POST /tg-webhook/<token> — Receive direct updates from Telegram.
    This replaces Make.com by handling incoming messages and callback queries.
    """
    if not TELEGRAM_BOT_TOKEN or token != TELEGRAM_BOT_TOKEN:
        return jsonify({"error": "Unauthorized"}), 401

    update = request.get_json(silent=True) or {}

    # 1. Handle Callback Query (e.g. inline Cancel button clicked)
    if "callback_query" in update:
        cb = update["callback_query"]
        cb_id = cb.get("id")
        cb_data = cb.get("data", "")
        bot_token = TELEGRAM_BOT_TOKEN

        if cb_data.startswith("cancel:"):
            job_id = cb_data.split("cancel:")[1]
            from core.telegram import tg_answer_callback_query
            if cancel_job(job_id):
                tg_answer_callback_query(bot_token, cb_id, "Cancellation requested.")
            else:
                tg_answer_callback_query(bot_token, cb_id, "Job already finished or not running.")
        return jsonify({"ok": True})

    # 2. Handle standard message
    if "message" not in update:
        return jsonify({"ok": True})

    msg = update["message"]
    chat_id = str(msg.get("chat", {}).get("id", ""))
    text = msg.get("text", "").strip()
    bot_token = TELEGRAM_BOT_TOKEN

    if not text or not chat_id:
        return jsonify({"ok": True})

    # Support /start and /help commands
    if text in ("/start", "/help"):
        help_text = (
            "🎬 *YouTube Clipper Bot*\n\n"
            "Cut video or audio clips from YouTube & more platforms!\n\n"
            "*Usage:*\n"
            "`/clip <url> <start> <end> [filename] [mp3|mp4]`\n\n"
            "*Examples:*\n"
            "• `/clip https://youtu.be/dQw4w9WgXcQ 1:00 1:30` (MP4 clip)\n"
            "• `/clip https://youtu.be/dQw4w9WgXcQ 0:10 0:40 my_audio mp3` (MP3 export)\n"
            "• `/clip https://youtu.be/dQw4w9WgXcQ 90 120` (using seconds)\n\n"
            "Use the inline button that appears to cancel processing at any time!"
        )
        from core.telegram import tg_send
        tg_send(bot_token, chat_id, help_text)
        return jsonify({"ok": True})

    if text.startswith("/clip"):
        # Match pattern: URL, start, end, optional filename, optional format
        # Pattern structure: /clip <url> <start> <end> [filename] [mp4|mp3]
        pattern = r"^\/clip\s+(https?:\/\/\S+)\s+(\S+)\s+(\S+)(?:\s+(\S+?))?(?:\s+(mp3|mp4))?$"
        match = re.match(pattern, text)
        if not match:
            usage_err = (
                "❌ *Invalid Command Format.*\n\n"
                "Please use: `/clip <url> <start> <end> [filename] [mp3|mp4]`\n\n"
                "Example: `/clip https://youtu.be/dQw4w9WgXcQ 1:30 2:00 my_clip`"
            )
            from core.telegram import tg_send
            tg_send(bot_token, chat_id, usage_err)
            return jsonify({"ok": True})

        url = match.group(1)
        start = match.group(2)
        end = match.group(3)
        filename = match.group(4) or ""
        fmt = match.group(5) or "mp4"

        # Check rate limits
        daily = chat_daily_count(chat_id)
        if daily >= MAX_JOBS_PER_CHAT_DAY:
            limit_err = (
                f"❌ *Rate Limit Exceeded.*\n\n"
                f"You have used your daily limit of {MAX_JOBS_PER_CHAT_DAY} clips. "
                "Please try again in 24 hours."
            )
            from core.telegram import tg_send
            tg_send(bot_token, chat_id, limit_err)
            return jsonify({"ok": True})

        # Global concurrency semaphore
        if not CLIP_SEMAPHORE.acquire(blocking=False):
            busy_err = "⏳ *Server is busy.* Other downloads are running. Please wait and try again shortly."
            from core.telegram import tg_send
            tg_send(bot_token, chat_id, busy_err)
            return jsonify({"ok": True})

        job_id = str(uuid.uuid4())
        create_job(job_id, chat_id)
        record_chat_job(chat_id)

        # Send initial message with the Cancel inline button
        cancel_markup = {
            "inline_keyboard": [[
                {"text": "🚫 Cancel", "callback_data": f"cancel:{job_id}"}
            ]]
        }
        from core.telegram import tg_send
        resp = tg_send(
            bot_token,
            chat_id,
            f"🎬 *Processing your clip…*\n{progress_bar(0)}",
            reply_markup=cancel_markup
        )
        msg_id = resp.get("result", {}).get("message_id")

        if not msg_id:
            CLIP_SEMAPHORE.release()
            finish_job(job_id, error="Failed to contact Telegram")
            return jsonify({"ok": True})

        # Spawn worker
        thread = threading.Thread(
            target=process_clip,
            args=(job_id, url, start, end, filename, fmt, bot_token, chat_id, msg_id),
            daemon=True,
            name=f"clip-{job_id[:8]}"
        )
        thread.start()

    return jsonify({"ok": True})


@bp.route("/admin", methods=["GET"])
def admin_dashboard():
    """
    GET /admin — Observability dashboard.
    Renders a premium HTML page displaying system state, active queue, and job history.
    """
    key = request.args.get("key", "")
    if not ADMIN_SECRET or not secrets.compare_digest(key, ADMIN_SECRET):
        return "Unauthorized", 401

    from core.jobs import _backend
    jobs_list = []
    
    active_count = 0
    done_count = 0
    error_count = 0

    if hasattr(_backend, "_jobs"):
        with _backend._lock:
            for jid, job in _backend._jobs.items():
                j_copy = {k: v for k, v in job.items() if not k.startswith("_")}
                j_copy["id"] = jid
                jobs_list.append(j_copy)
    elif hasattr(_backend, "_r"):
        r = _backend._r
        keys = r.keys(_backend._JOB_PFX + "*")
        for key_str in keys:
            raw = r.get(key_str)
            if raw:
                try:
                    job = json.loads(raw)
                    job["id"] = key_str.replace(_backend._JOB_PFX, "")
                    jobs_list.append(job)
                except Exception:
                    pass

    jobs_list.sort(key=lambda j: j.get("created_at", 0), reverse=True)

    for j in jobs_list:
        status = j.get("status")
        if status == "processing":
            active_count += 1
        elif status == "done":
            done_count += 1
        elif status == "error":
            error_count += 1

    html_template = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🎬 YouTube Clipper Admin Dashboard</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;800&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: #0b0f19;
            --card-bg: rgba(20, 30, 55, 0.6);
            --border-color: rgba(255, 255, 255, 0.08);
            --primary: #4f46e5;
            --primary-glow: rgba(79, 70, 229, 0.4);
            --text: #f3f4f6;
            --text-muted: #9ca3af;
            --success: #10b981;
            --warning: #f59e0b;
            --error: #ef4444;
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }

        body {
            font-family: 'Outfit', sans-serif;
            background-color: var(--bg-color);
            color: var(--text);
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            align-items: center;
            padding: 2rem 1rem;
            background-image: radial-gradient(circle at 50% 0%, rgba(79, 70, 229, 0.15) 0%, transparent 50%);
        }

        header {
            width: 100%;
            max-width: 1100px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 2.5rem;
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 1rem;
        }

        h1 {
            font-weight: 800;
            font-size: 2rem;
            background: linear-gradient(to right, #a5b4fc, #818cf8, #6366f1);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }

        .refresh-btn {
            background-color: var(--primary);
            color: white;
            border: none;
            padding: 0.6rem 1.2rem;
            border-radius: 8px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
            box-shadow: 0 0 15px var(--primary-glow);
        }

        .refresh-btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 0 25px var(--primary);
        }

        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 1.5rem;
            width: 100%;
            max-width: 1100px;
            margin-bottom: 2.5rem;
        }

        .stat-card {
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 1.5rem;
            backdrop-filter: blur(12px);
            display: flex;
            flex-direction: column;
            gap: 0.5rem;
            box-shadow: 0 4px 30px rgba(0, 0, 0, 0.25);
        }

        .stat-title {
            font-size: 0.9rem;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }

        .stat-value {
            font-size: 2.2rem;
            font-weight: 800;
        }

        .active-value { color: var(--warning); }
        .success-value { color: var(--success); }
        .error-value { color: var(--error); }

        .dashboard-container {
            width: 100%;
            max-width: 1100px;
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 16px;
            overflow: hidden;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
            backdrop-filter: blur(12px);
        }

        .table-header {
            padding: 1.25rem 1.5rem;
            border-bottom: 1px solid var(--border-color);
            font-weight: 600;
            font-size: 1.1rem;
            background: rgba(255, 255, 255, 0.02);
        }

        table {
            width: 100%;
            border-collapse: collapse;
            text-align: left;
        }

        th {
            padding: 1rem 1.5rem;
            color: var(--text-muted);
            font-weight: 600;
            border-bottom: 1px solid var(--border-color);
            font-size: 0.9rem;
        }

        td {
            padding: 1rem 1.5rem;
            border-bottom: 1px solid var(--border-color);
            font-size: 0.95rem;
        }

        tr:hover {
            background: rgba(255, 255, 255, 0.01);
        }

        .status-badge {
            display: inline-block;
            padding: 0.25rem 0.6rem;
            border-radius: 50px;
            font-size: 0.8rem;
            font-weight: 600;
            text-transform: uppercase;
        }

        .badge-processing {
            background-color: rgba(245, 158, 11, 0.15);
            color: var(--warning);
            border: 1px solid rgba(245, 158, 11, 0.3);
        }

        .badge-done {
            background-color: rgba(16, 185, 129, 0.15);
            color: var(--success);
            border: 1px solid rgba(16, 185, 129, 0.3);
        }

        .badge-error {
            background-color: rgba(239, 68, 68, 0.15);
            color: var(--error);
            border: 1px solid rgba(239, 68, 68, 0.3);
        }

        .error-msg {
            font-family: monospace;
            font-size: 0.85rem;
            color: var(--error);
            max-width: 250px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        .time-str {
            font-size: 0.85rem;
            color: var(--text-muted);
        }

        .empty-state {
            padding: 3rem;
            text-align: center;
            color: var(--text-muted);
        }
    </style>
</head>
<body>
    <header>
        <div>
            <h1>🎬 YouTube Clipper API Dashboard</h1>
        </div>
        <button class="refresh-btn" onclick="location.reload()">Refresh Data</button>
    </header>

    <div class="stats-grid">
        <div class="stat-card">
            <span class="stat-title">Active Workers</span>
            <span class="stat-value active-value">{{ active_count }}</span>
        </div>
        <div class="stat-card">
            <span class="stat-title">Completed Jobs</span>
            <span class="stat-value success-value">{{ done_count }}</span>
        </div>
        <div class="stat-card">
            <span class="stat-title">Errored / Cancelled</span>
            <span class="stat-value error-value">{{ error_count }}</span>
        </div>
        <div class="stat-card">
            <span class="stat-title">Total Tracked Jobs</span>
            <span class="stat-value">{{ total_count }}</span>
        </div>
    </div>

    <div class="dashboard-container">
        <div class="table-header">Job Queue & History</div>
        <div style="overflow-x: auto;">
            {% if jobs %}
            <table>
                <thead>
                    <tr>
                        <th>Job ID</th>
                        <th>Chat ID</th>
                        <th>Status</th>
                        <th>Created At</th>
                        <th>Runtime Details</th>
                    </tr>
                </thead>
                <tbody>
                    {% for job in jobs %}
                    <tr>
                        <td style="font-family: monospace; font-size: 0.85rem; color: var(--text-muted);">{{ job.id[:8] }}...</td>
                        <td>{{ job.chat_id }}</td>
                        <td>
                            <span class="status-badge badge-{{ job.status }}">{{ job.status }}</span>
                        </td>
                        <td class="time-str">{{ datetime(job.created_at) }}</td>
                        <td>
                            {% if job.status == 'error' %}
                            <div class="error-msg" title="{{ job.error }}">{{ job.error }}</div>
                            {% elif job.status == 'done' %}
                            <span class="time-str">Finished {{ datetime(job.finished_at) }}</span>
                            {% else %}
                            <span class="time-str">In progress...</span>
                            {% endif %}
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% else %}
            <div class="empty-state">No jobs recorded in store yet. Submit a clip to populate stats!</div>
            {% endif %}
        </div>
    </div>
</body>
</html>
    """

    from datetime import datetime
    def datetime_filter(timestamp):
        if not timestamp:
            return ""
        return datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')

    return render_template_string(
        html_template,
        jobs=jobs_list,
        active_count=active_count,
        done_count=done_count,
        error_count=error_count,
        total_count=len(jobs_list),
        datetime=datetime_filter
    )
