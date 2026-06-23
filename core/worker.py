"""
core/worker.py — The clip processing worker.

This module contains the single long-running function (process_clip) that is
executed in a daemon thread for each clip job. It implements the full pipeline:

  Step 1 — Download  (yt-dlp with CDN range extraction)
  Step 2 — Cut       (ffmpeg two-pass seeking)
  Step 3 — Upload    (Telegram Bot API sendDocument)
  Step 4 — Done      (progress message updated, deferred cleanup scheduled)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Threading model
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
process_clip() is called by threading.Thread(daemon=True) in routes/clip.py.
It must not be called directly from a request handler because yt-dlp and
ffmpeg can take 30–300 seconds — far beyond any sensible HTTP timeout.

The CLIP_SEMAPHORE is acquired *before* the thread is started (in
routes/clip.py) and released *inside* the finally block here, ensuring
the concurrency slot is freed regardless of how the function exits.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Cancellation model
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
When POST /cancel/<job_id> is called:
  1. core/jobs.py sets an internal cancel flag (threading.Event / Redis key).
  2. The worker checks is_job_cancelled(job_id) at three checkpoints:
       - Before Step 1 (download)
       - During download  → via a yt-dlp progress hook
       - Before Step 2 (cut)
       - During ffmpeg    → via a Popen polling loop with proc.terminate()
       - Before Step 3 (upload)
  3. Any checkpoint that detects cancellation raises JobCancelledError,
     which is caught by the outer except block.
  4. The except block calls finish_job(job_id, error="Cancelled by user")
     and edits the Telegram message to 🚫.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Key design decisions
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  #1  Deferred cleanup via _deferred_delete() — prevents the worker from
      blocking the CLIP_SEMAPHORE slot during a 60-second sleep.
  #2  Output path prefixed with job_id — no concurrent filename collisions.
  #8  yt-dlp download_ranges — fetches only the CDN bytes for the clip range.
  #10 Two-pass ffmpeg seeking — fast keyframe seek + precise inner trim.
  #12 Post-download duration validation against the actual video length.
  #13 tg_send_document() return value checked — raises RuntimeError on failure.
  #14 subprocess.Popen (not run()) for ffmpeg — allows cancel + timeout polling.
"""

import glob       # find the downloaded file by extension wildcard
import logging    # structured log output
import os         # file system operations
import subprocess # Popen for ffmpeg; run() for startup ffmpeg check in app.py
import threading  # deferred cleanup thread
import time       # sleep in _deferred_delete and ffmpeg poll loop

import yt_dlp                           # YouTube/audio downloader
from yt_dlp.utils import download_range_func   # CDN range extraction helper

from config import COOKIES_PATH, DOWNLOAD_DIR, MAX_DURATION, MAX_FILE_MB
from core.jobs import CLIP_SEMAPHORE, finish_job, is_job_cancelled
from core.telegram import tg_edit, tg_send_document
from core.utils import progress_bar, safe_filename, time_to_seconds

log = logging.getLogger(__name__)   # scoped logger: "core.worker"


# ── Custom exception for user-requested cancellation ──────────────────────────

class JobCancelledError(Exception):
    """
    Raised at any pipeline checkpoint when is_job_cancelled() returns True.
    Inherits from Exception (not BaseException) so it is caught by the
    `except Exception` block in process_clip() and handled cleanly.
    """


# ── Deferred cleanup helper ────────────────────────────────────────────────────

def _deferred_delete(path: str, delay: int = 60) -> None:
    """
    Schedule deletion of `path` after `delay` seconds.

    A short-lived daemon thread sleeps then removes the file.  This gives
    Telegram time to finish downloading the clip before we delete it.
    (See design decision #1 for why we don't sleep inside the worker thread.)
    """
    def _do() -> None:
        time.sleep(delay)
        try:
            if os.path.exists(path):
                os.remove(path)
                log.debug("Deferred cleanup: removed %s", path)
        except OSError as exc:
            log.warning("Deferred cleanup failed for %s: %s", path, exc)

    threading.Thread(target=_do, daemon=True, name="cleanup").start()


# ── Cancel checkpoint helper ───────────────────────────────────────────────────

def _check_cancel(job_id: str) -> None:
    """
    Raise JobCancelledError if a cancel has been requested for this job.
    Called at the start of each pipeline step.
    """
    if is_job_cancelled(job_id):
        raise JobCancelledError("Cancelled by user")


# ── yt-dlp progress hook factory ──────────────────────────────────────────────

def _make_cancel_hook(job_id: str):
    """
    Return a yt-dlp progress hook that raises JobCancelledError if the job
    has been cancelled during the download step.

    yt-dlp calls progress hooks synchronously between downloaded chunks, so
    this provides sub-second cancellation response during active downloads.
    """
    def _hook(d: dict) -> None:
        if d.get("status") == "downloading" and is_job_cancelled(job_id):
            raise JobCancelledError("Cancelled during download")
    return _hook


# ── Main clip worker ───────────────────────────────────────────────────────────

def process_clip(
    job_id: str,        # UUID string identifying this job in the store
    url: str,           # validated YouTube URL from the HTTP request
    start: str,         # start time string (HH:MM:SS, MM:SS, or seconds)
    end: str,           # end time string (same formats as start)
    filename: str,      # desired output filename without extension (may be "")
    fmt: str,           # output format: "mp4" or "mp3"
    bot_token: str,     # Telegram Bot API token
    chat_id: str,       # Telegram chat ID to deliver the clip to
    status_msg_id: int, # message_id of the "🎬 Processing…" message to edit
) -> None:
    """
    Execute the full clip pipeline: download → cut → upload → cleanup.

    Progress is communicated by editing `status_msg_id` in place at the start
    of each step.  On cancellation or any other exception:
      1. The error (or "Cancelled by user") is logged.
      2. The job is marked finished in the store.
      3. The Telegram status message is updated.
      4. Temp files are deleted in the finally block.
      5. The CLIP_SEMAPHORE slot is released.

    The CLIP_SEMAPHORE *must* already be acquired by the caller.
    """
    out_file: str | None = None   # trimmed clip output path
    src_file: str | None = None   # raw downloaded source path

    try:
        # ── Validate timestamps ────────────────────────────────────────────────
        start_sec = time_to_seconds(start)
        end_sec   = time_to_seconds(end)
        duration  = end_sec - start_sec

        if duration <= 0:
            raise ValueError("End time must be after start time.")

        if duration > MAX_DURATION:
            raise ValueError(
                f"Clip duration ({int(duration)}s) exceeds the "
                f"{MAX_DURATION // 60}-minute limit."
            )

        # ── Pre-step cancel check ─────────────────────────────────────────────
        # Check immediately after validation; the user may have cancelled while
        # the job was sitting in the semaphore queue.
        _check_cancel(job_id)

        # ── Step 1 / 4 — Download ─────────────────────────────────────────────
        tg_edit(
            bot_token, chat_id, status_msg_id,
            f"📥 *Step 1/4 — Downloading video…*\n{progress_bar(1)}",
        )

        temp_base = os.path.join(DOWNLOAD_DIR, f"{job_id}_src")

        ydl_opts: dict = {
            "format": (
                "bv*+ba/b"
                if fmt == "mp4"
                else "bestaudio/best"
            ),
            "outtmpl":       f"{temp_base}.%(ext)s",
            "quiet":         True,
            "no_warnings":   True,
            # CDN range extraction: only fetch the bytes we need (Fix #8).
            "download_ranges":         download_range_func(None, [(start_sec, end_sec)]),
            "force_keyframes_at_cuts": True,
            # Cancel hook: raises JobCancelledError between downloaded chunks.
            "progress_hooks": [_make_cancel_hook(job_id)],
            # Cookies: bypass YouTube bot detection when available.
            "cookiefile":               COOKIES_PATH if os.path.exists(COOKIES_PATH) else None,
            # JS runtime: needed by yt-dlp 2026+ to solve YouTube challenges.
            "js_runtimes":              {"node": None},
        }

        if fmt == "mp4":
            ydl_opts["format_sort"]         = ["res:1080", "ext:mp4:m4a"]
            ydl_opts["merge_output_format"] = "mp4"

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info           = ydl.extract_info(url, download=True)
            video_title    = info.get("title", "video")
            video_duration = info.get("duration")

        # Post-download duration validation (Fix #12).
        if video_duration and end_sec > video_duration:
            raise ValueError(
                f"End time ({end_sec:.0f}s) exceeds the video length "
                f"({video_duration:.0f}s)."
            )

        matches = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}_src.*"))
        if not matches:
            raise FileNotFoundError("Downloaded source file not found.")
        src_file = matches[0]

        # ── Pre-step cancel check ─────────────────────────────────────────────
        _check_cancel(job_id)

        # ── Step 2 / 4 — Cut ──────────────────────────────────────────────────
        tg_edit(
            bot_token, chat_id, status_msg_id,
            f"✂️ *Step 2/4 — Cutting clip…*\n{progress_bar(2)}",
        )

        out_label = safe_filename(filename) if filename else "clip"
        out_ext   = "mp3" if fmt == "mp3" else "mp4"
        out_file  = os.path.join(DOWNLOAD_DIR, f"{job_id}_{out_label}.{out_ext}")

        if fmt == "mp3":
            cmd = [
                "ffmpeg", "-y",
                "-ss", str(start_sec),
                "-i",  src_file,
                "-t",  str(duration),
                "-vn",
                "-acodec", "libmp3lame",
                "-q:a", "2",
                out_file,
            ]
        else:
            # Two-pass seeking (Fix #10): fast keyframe pre-seek + precise trim.
            cmd = [
                "ffmpeg", "-y",
                "-ss", str(start_sec),          # ① fast keyframe seek
                "-i",  src_file,
                "-ss", "0",                     # ② precise inner trim
                "-t",  str(duration),
                "-c",  "copy",
                "-avoid_negative_ts", "make_zero",
                "-movflags", "+faststart",
                out_file,
            ]

        # ── Run ffmpeg with cancel + timeout polling (Fix #14) ─────────────────
        # subprocess.Popen (not run()) lets us terminate the process on cancel
        # without blocking the thread for the full ffmpeg duration.
        _ffmpeg_deadline = time.monotonic() + 300   # 300 s hard cap

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        while proc.poll() is None:
            # Check cancellation first — terminate and raise immediately.
            if is_job_cancelled(job_id):
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                raise JobCancelledError("Cancelled during ffmpeg cut")

            # Hard timeout guard — kill if ffmpeg is stuck.
            if time.monotonic() > _ffmpeg_deadline:
                proc.kill()
                proc.wait()
                raise RuntimeError("ffmpeg timed out after 300 seconds")

            time.sleep(0.25)   # poll every 250 ms

        # Process has exited — read stderr and check return code.
        result_returncode = proc.returncode
        result_stderr     = proc.stderr.read().decode(errors="replace")
        proc.stdout.close()
        proc.stderr.close()

        if result_returncode != 0:
            log.error("ffmpeg full stderr:\n%s", result_stderr)
            error_lines = [
                ln for ln in result_stderr.splitlines()
                if ln.strip() and not any(
                    ln.lstrip().startswith(p)
                    for p in ("frame=", "fps=", "size=", "time=", "speed=", "bitrate=")
                )
            ]
            raise RuntimeError("ffmpeg error:\n" + "\n".join(error_lines[-20:]))

        # Source file no longer needed — free disk space before the upload.
        os.remove(src_file)
        src_file = None   # disown: tells finally block not to attempt again

        # ── Pre-step cancel check ─────────────────────────────────────────────
        _check_cancel(job_id)

        # ── Step 3 / 4 — Upload ───────────────────────────────────────────────
        tg_edit(
            bot_token, chat_id, status_msg_id,
            f"📤 *Step 3/4 — Uploading to Telegram…*\n{progress_bar(3)}",
        )

        max_bytes = MAX_FILE_MB * 1024 * 1024
        file_size = os.path.getsize(out_file)
        if file_size > max_bytes:
            raise ValueError(
                f"File too large ({file_size / 1024 / 1024:.1f} MB). "
                f"Telegram limit is {MAX_FILE_MB} MB. "
                "Try a shorter clip or use MP3 format."
            )

        caption = (
            f"✅ *{out_label}.{out_ext}*\n"
            f"📹 _{video_title}_\n"
            f"⏱ `{start}` → `{end}` ({int(duration)}s)"
        )

        if not tg_send_document(bot_token, chat_id, out_file, caption):
            raise RuntimeError(
                "Failed to upload clip to Telegram — the Bot API rejected the file."
            )

        # ── Step 4 / 4 — Done ─────────────────────────────────────────────────
        tg_edit(
            bot_token, chat_id, status_msg_id,
            f"✅ *Done!* Your clip has been sent.\n{progress_bar(4)}",
        )

        finish_job(job_id)
        log.info("Job %s completed: %s.%s", job_id, out_label, out_ext)

        # Schedule cleanup: wait 60 s before deleting so Telegram can download.
        _deferred_delete(out_file, delay=60)
        out_file = None   # disown: tells finally block not to delete early

    except Exception as exc:
        # ── Unified error / cancel handler ────────────────────────────────────
        # Determine whether this was a deliberate cancellation.
        if is_job_cancelled(job_id):
            log.info("Job %s cancelled by user", job_id)
            finish_job(job_id, error="Cancelled by user")
            try:
                tg_edit(
                    bot_token, chat_id, status_msg_id,
                    "🚫 *Your clip was cancelled.*",
                )
            except Exception:
                pass
        else:
            log.error("Job %s failed: %s", job_id, exc)
            finish_job(job_id, error=str(exc))
            try:
                tg_edit(
                    bot_token, chat_id, status_msg_id,
                    f"❌ *Error processing your clip:*\n`{exc!s}`\n\n"
                    "Please check your URL and timestamps.",
                )
            except Exception:
                pass   # already in error handling — swallow secondary failures

    finally:
        # ── Guaranteed cleanup ────────────────────────────────────────────────
        # Always runs regardless of success, error, or cancellation.

        # Release the semaphore slot so the next queued job can start.
        CLIP_SEMAPHORE.release()

        # Delete any temp files not already removed or disowned.
        for f in (src_file, out_file):
            if f and os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass
