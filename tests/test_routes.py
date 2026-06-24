"""
tests/test_routes.py — Integration tests for HTTP endpoints (Flask test client).

All five endpoints are tested:
  GET  /           — service info
  POST /clip        — submit a clip job
  POST /cancel/<id> — cancel a running job
  GET  /status/<id> — poll job status
  GET  /health      — liveness probe

Network-bound functions (tg_send, process_clip, threading.Thread) are mocked
so tests run instantly without needing Telegram credentials, ffmpeg, or yt-dlp.

Auth:
  The SECRET_KEY is set to "test-secret-key-for-pytest" in conftest.py.
  Tests that require auth pass the header explicitly.
"""

import threading
import uuid
from unittest.mock import MagicMock, patch

import pytest

from core.jobs import cancel_job, create_job, finish_job, record_chat_job
from config import MAX_JOBS, MAX_JOBS_PER_CHAT_DAY

SECRET = "test-secret-key-for-pytest"

# A valid YouTube URL that passes the _YT_PATTERN allow-list.
VALID_URL = "https://youtu.be/dQw4w9WgXcQ"

# Minimal valid POST /clip body.
VALID_CLIP_BODY = {
    "url":       VALID_URL,
    "start":     "0:30",
    "end":       "1:00",
    "bot_token": "123456:ABCDEFGtest",
    "chat_id":   "987654321",
}

AUTH_HEADER = {"X-Secret-Key": SECRET}

# Mock Telegram sendMessage response (used by tg_send in routes/clip.py).
TG_SEND_OK = {"ok": True, "result": {"message_id": 42}}


# ══════════════════════════════════════════════════════════════════════════════
# GET /
# ══════════════════════════════════════════════════════════════════════════════

class TestIndex:

    def test_returns_200(self, client):
        r = client.get("/")
        assert r.status_code == 200

    def test_contains_service_name(self, client):
        data = client.get("/").get_json()
        assert "YouTube Clipper Bot" in data.get("service", "")

    def test_lists_endpoints(self, client):
        data = client.get("/").get_json()
        assert "endpoints" in data

    def test_no_auth_required(self, client):
        # Must be publicly accessible
        r = client.get("/")
        assert r.status_code != 401


# ══════════════════════════════════════════════════════════════════════════════
# GET /health
# ══════════════════════════════════════════════════════════════════════════════

class TestHealth:

    def test_returns_200(self, client):
        r = client.get("/health")
        assert r.status_code == 200

    def test_status_is_ok(self, client):
        data = client.get("/health").get_json()
        assert data["status"] == "ok"

    def test_includes_job_count(self, client):
        data = client.get("/health").get_json()
        assert "jobs" in data
        assert isinstance(data["jobs"], int)

    def test_job_count_reflects_store(self, client):
        create_job(str(uuid.uuid4()), "chat_health")
        data = client.get("/health").get_json()
        assert data["jobs"] >= 1


# ══════════════════════════════════════════════════════════════════════════════
# POST /clip — authentication
# ══════════════════════════════════════════════════════════════════════════════

class TestClipAuth:

    def test_no_header_returns_401(self, client):
        r = client.post("/clip", json=VALID_CLIP_BODY)
        assert r.status_code == 401

    def test_wrong_key_returns_401(self, client):
        r = client.post("/clip",
            headers={"X-Secret-Key": "wrong-key"},
            json=VALID_CLIP_BODY,
        )
        assert r.status_code == 401

    def test_correct_key_passes_auth(self, client):
        # Auth passes — may fail for another reason (Telegram mock), but not 401.
        with patch("routes.clip.tg_send", return_value=TG_SEND_OK), \
             patch("routes.clip.threading.Thread"):
            r = client.post("/clip", headers=AUTH_HEADER, json=VALID_CLIP_BODY)
        assert r.status_code != 401


# ══════════════════════════════════════════════════════════════════════════════
# POST /clip — input validation
# ══════════════════════════════════════════════════════════════════════════════

class TestClipValidation:

    def test_missing_url_returns_400(self, client):
        body = {k: v for k, v in VALID_CLIP_BODY.items() if k != "url"}
        r = client.post("/clip", headers=AUTH_HEADER, json=body)
        assert r.status_code == 400
        assert "url" in r.get_json()["error"]

    def test_missing_start_returns_400(self, client):
        body = {k: v for k, v in VALID_CLIP_BODY.items() if k != "start"}
        r = client.post("/clip", headers=AUTH_HEADER, json=body)
        assert r.status_code == 400

    def test_missing_bot_token_returns_400(self, client):
        body = {k: v for k, v in VALID_CLIP_BODY.items() if k != "bot_token"}
        r = client.post("/clip", headers=AUTH_HEADER, json=body)
        assert r.status_code == 400

    def test_invalid_format_returns_400(self, client):
        body = {**VALID_CLIP_BODY, "format": "avi"}
        r = client.post("/clip", headers=AUTH_HEADER, json=body)
        assert r.status_code == 400
        assert "format" in r.get_json()["error"]

    def test_http_url_rejected(self, client):
        body = {**VALID_CLIP_BODY, "url": "http://youtu.be/dQw4w9WgXcQ"}
        r = client.post("/clip", headers=AUTH_HEADER, json=body)
        assert r.status_code == 400

    def test_non_youtube_url_rejected(self, client):
        body = {**VALID_CLIP_BODY, "url": "https://vimeo.com/12345"}
        r = client.post("/clip", headers=AUTH_HEADER, json=body)
        assert r.status_code == 400

    def test_empty_json_body_returns_400(self, client):
        r = client.post("/clip", headers=AUTH_HEADER, json={})
        assert r.status_code == 400

    def test_malformed_json_returns_400(self, client):
        r = client.post(
            "/clip",
            headers={**AUTH_HEADER, "Content-Type": "application/json"},
            data="not json",
        )
        assert r.status_code == 400

    # ── Valid URL variants ─────────────────────────────────────────────────────

    @pytest.mark.parametrize("url", [
        "https://youtube.com/watch?v=abc",
        "https://www.youtube.com/watch?v=abc",
        "https://youtu.be/abc",
        "https://youtube.com/shorts/abc",
        "https://youtube.com/live/abc",
        "https://m.youtube.com/watch?v=abc",
        "https://music.youtube.com/watch?v=abc",
    ])
    def test_valid_youtube_url_accepted(self, client, url):
        body = {**VALID_CLIP_BODY, "url": url}
        with patch("routes.clip.tg_send", return_value=TG_SEND_OK), \
             patch("routes.clip.threading.Thread"):
            r = client.post("/clip", headers=AUTH_HEADER, json=body)
        # Must not be rejected by the URL allow-list
        assert r.status_code != 400 or "URL" not in r.get_json().get("error", "")


# ══════════════════════════════════════════════════════════════════════════════
# POST /clip — successful submission
# ══════════════════════════════════════════════════════════════════════════════

class TestClipSuccess:

    @patch("routes.clip.threading.Thread")
    @patch("routes.clip.tg_send", return_value=TG_SEND_OK)
    def test_returns_200_with_job_id(self, mock_tg, mock_thread, client):
        r = client.post("/clip", headers=AUTH_HEADER, json=VALID_CLIP_BODY)
        assert r.status_code == 200
        data = r.get_json()
        assert "job_id" in data
        assert data["status"] == "processing"

    @patch("routes.clip.threading.Thread")
    @patch("routes.clip.tg_send", return_value=TG_SEND_OK)
    def test_job_id_is_valid_uuid(self, mock_tg, mock_thread, client):
        r = client.post("/clip", headers=AUTH_HEADER, json=VALID_CLIP_BODY)
        job_id = r.get_json()["job_id"]
        # Should not raise
        uuid.UUID(job_id)

    @patch("routes.clip.threading.Thread")
    @patch("routes.clip.tg_send", return_value=TG_SEND_OK)
    def test_thread_is_started(self, mock_tg, mock_thread, client):
        client.post("/clip", headers=AUTH_HEADER, json=VALID_CLIP_BODY)
        mock_thread.return_value.start.assert_called_once()

    @patch("routes.clip.threading.Thread")
    @patch("routes.clip.tg_send", return_value=TG_SEND_OK)
    def test_job_appears_in_status(self, mock_tg, mock_thread, client):
        r = client.post("/clip", headers=AUTH_HEADER, json=VALID_CLIP_BODY)
        job_id = r.get_json()["job_id"]
        status_r = client.get(f"/status/{job_id}")
        assert status_r.status_code == 200
        assert status_r.get_json()["status"] == "processing"

    @patch("routes.clip.threading.Thread")
    @patch("routes.clip.tg_send", return_value=TG_SEND_OK)
    def test_mp3_format_accepted(self, mock_tg, mock_thread, client):
        body = {**VALID_CLIP_BODY, "format": "mp3"}
        r = client.post("/clip", headers=AUTH_HEADER, json=body)
        assert r.status_code == 200

    def test_telegram_failure_returns_502(self, client):
        # tg_send returns a response without message_id → 502
        bad_tg = {"ok": False}
        with patch("routes.clip.tg_send", return_value=bad_tg):
            r = client.post("/clip", headers=AUTH_HEADER, json=VALID_CLIP_BODY)
        assert r.status_code == 502


# ══════════════════════════════════════════════════════════════════════════════
# POST /clip — rate limiting
# ══════════════════════════════════════════════════════════════════════════════

class TestClipRateLimiting:

    def test_semaphore_full_returns_429(self, client):
        """Server busy: all MAX_JOBS slots acquired."""
        import core.jobs as jobs_module
        sem = jobs_module.CLIP_SEMAPHORE
        # Drain all semaphore slots to simulate a full server
        acquired = []
        while sem.acquire(blocking=False):
            acquired.append(True)

        try:
            r = client.post("/clip", headers=AUTH_HEADER, json=VALID_CLIP_BODY)
            assert r.status_code == 429
            assert "retry_after" in r.get_json()
        finally:
            # Restore slots so reset_state fixture works correctly
            for _ in acquired:
                sem.release()

    def test_daily_limit_returns_429(self, client):
        """Chat has hit MAX_JOBS_PER_CHAT_DAY today."""
        chat_id = VALID_CLIP_BODY["chat_id"]
        for _ in range(MAX_JOBS_PER_CHAT_DAY):
            record_chat_job(chat_id)

        r = client.post("/clip", headers=AUTH_HEADER, json=VALID_CLIP_BODY)
        assert r.status_code == 429
        data = r.get_json()
        assert data["daily_limit"] == MAX_JOBS_PER_CHAT_DAY
        assert data["daily_used"] == MAX_JOBS_PER_CHAT_DAY
        assert data["retry_after"] == 86400

    def test_daily_limit_error_message(self, client):
        chat_id = VALID_CLIP_BODY["chat_id"]
        for _ in range(MAX_JOBS_PER_CHAT_DAY):
            record_chat_job(chat_id)
        r = client.post("/clip", headers=AUTH_HEADER, json=VALID_CLIP_BODY)
        assert "Daily limit" in r.get_json()["error"]

    @patch("routes.clip.threading.Thread")
    @patch("routes.clip.tg_send", return_value=TG_SEND_OK)
    def test_daily_count_increments_on_success(self, mock_tg, mock_thread, client):
        from core.jobs import chat_daily_count
        chat_id = VALID_CLIP_BODY["chat_id"]
        before = chat_daily_count(chat_id)
        client.post("/clip", headers=AUTH_HEADER, json=VALID_CLIP_BODY)
        assert chat_daily_count(chat_id) == before + 1


# ══════════════════════════════════════════════════════════════════════════════
# POST /cancel/<job_id>
# ══════════════════════════════════════════════════════════════════════════════

class TestCancel:

    def test_cancel_unknown_job_returns_404(self, client):
        r = client.post("/cancel/nonexistent-id-xyz")
        assert r.status_code == 404
        assert "not found" in r.get_json()["error"].lower()

    def test_cancel_processing_job_returns_200(self, client):
        job_id = str(uuid.uuid4())
        create_job(job_id, "some_chat")
        r = client.post(f"/cancel/{job_id}")
        assert r.status_code == 200
        data = r.get_json()
        assert data["job_id"] == job_id
        assert data["status"] == "cancel_requested"

    def test_cancel_done_job_returns_409(self, client):
        job_id = str(uuid.uuid4())
        create_job(job_id, "some_chat")
        finish_job(job_id)
        r = client.post(f"/cancel/{job_id}")
        assert r.status_code == 409

    def test_cancel_error_job_returns_409(self, client):
        job_id = str(uuid.uuid4())
        create_job(job_id, "some_chat")
        finish_job(job_id, error="Something failed")
        r = client.post(f"/cancel/{job_id}")
        assert r.status_code == 409

    def test_cancel_sets_cancel_flag(self, client):
        from core.jobs import is_job_cancelled
        job_id = str(uuid.uuid4())
        create_job(job_id, "some_chat")
        client.post(f"/cancel/{job_id}")
        assert is_job_cancelled(job_id) is True

    def test_cancel_requires_no_auth(self, client):
        """Cancel endpoint is intentionally public — no X-Secret-Key needed."""
        job_id = str(uuid.uuid4())
        create_job(job_id, "some_chat")
        # No auth header
        r = client.post(f"/cancel/{job_id}")
        assert r.status_code != 401

    def test_cancel_409_includes_current_status(self, client):
        job_id = str(uuid.uuid4())
        create_job(job_id, "some_chat")
        finish_job(job_id)
        r = client.post(f"/cancel/{job_id}")
        data = r.get_json()
        assert data.get("status") == "done"


# ══════════════════════════════════════════════════════════════════════════════
# GET /status/<job_id>
# ══════════════════════════════════════════════════════════════════════════════

class TestStatus:

    def test_unknown_id_returns_404(self, client):
        r = client.get("/status/nonexistent-abc-123")
        assert r.status_code == 404

    def test_processing_job_returns_200(self, client):
        job_id = str(uuid.uuid4())
        create_job(job_id, "chat_x")
        r = client.get(f"/status/{job_id}")
        assert r.status_code == 200
        assert r.get_json()["status"] == "processing"

    def test_done_job_returns_status(self, client):
        job_id = str(uuid.uuid4())
        create_job(job_id, "chat_x")
        finish_job(job_id)
        r = client.get(f"/status/{job_id}")
        assert r.status_code == 200
        assert r.get_json()["status"] == "done"

    def test_error_job_includes_error_message(self, client):
        job_id = str(uuid.uuid4())
        create_job(job_id, "chat_x")
        finish_job(job_id, error="Clip too large")
        data = client.get(f"/status/{job_id}").get_json()
        assert data["status"] == "error"
        assert data["error"] == "Clip too large"

    def test_status_includes_created_at(self, client):
        job_id = str(uuid.uuid4())
        create_job(job_id, "chat_x")
        data = client.get(f"/status/{job_id}").get_json()
        assert "created_at" in data


# ══════════════════════════════════════════════════════════════════════════════
# POST /tg-webhook/<token>
# ══════════════════════════════════════════════════════════════════════════════

class TestWebhook:

    def test_unauthorized_token_returns_401(self, client):
        r = client.post("/tg-webhook/wrong-token", json={})
        assert r.status_code == 401

    @patch("routes.clip.TELEGRAM_BOT_TOKEN", "valid-bot-token")
    def test_authorized_token_returns_200(self, client):
        r = client.post("/tg-webhook/valid-bot-token", json={})
        assert r.status_code == 200

    @patch("routes.clip.TELEGRAM_BOT_TOKEN", "valid-bot-token")
    @patch("core.telegram.tg_send", return_value={"ok": True})
    def test_webhook_start_command(self, mock_send, client):
        r = client.post(
            "/tg-webhook/valid-bot-token",
            json={
                "message": {
                    "chat": {"id": 12345},
                    "text": "/start"
                }
            }
        )
        assert r.status_code == 200
        mock_send.assert_called_once()

    @patch("routes.clip.TELEGRAM_BOT_TOKEN", "valid-bot-token")
    @patch("core.telegram.tg_send", return_value=TG_SEND_OK)
    @patch("routes.clip.process_clip")
    def test_webhook_valid_clip_command(self, mock_process, mock_send, client):
        r = client.post(
            "/tg-webhook/valid-bot-token",
            json={
                "message": {
                    "chat": {"id": 12345},
                    "text": "/clip https://youtube.com/watch?v=123 0:10 0:30"
                }
            }
        )
        assert r.status_code == 200
        mock_send.assert_called_once()

    @patch("routes.clip.TELEGRAM_BOT_TOKEN", "valid-bot-token")
    @patch("core.telegram.tg_answer_callback_query")
    def test_webhook_callback_query_cancel(self, mock_answer, client):
        job_id = str(uuid.uuid4())
        create_job(job_id, "12345")
        r = client.post(
            "/tg-webhook/valid-bot-token",
            json={
                "callback_query": {
                    "id": "cb_query_123",
                    "data": f"cancel:{job_id}"
                }
            }
        )
        assert r.status_code == 200
        mock_answer.assert_called_once()


# ══════════════════════════════════════════════════════════════════════════════
# GET /admin
# ══════════════════════════════════════════════════════════════════════════════

class TestAdminDashboard:

    def test_admin_unauthorized_key_returns_401(self, client):
        r = client.get("/admin?key=wrong-secret")
        assert r.status_code == 401

    @patch("routes.clip.ADMIN_SECRET", "test-admin-secret")
    def test_admin_authorized_key_renders_dashboard(self, client):
        r = client.get("/admin?key=test-admin-secret")
        assert r.status_code == 200
        assert b"Clipper API Dashboard" in r.data

