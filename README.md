# 🎬 YouTube Clipper Bot

A hybrid **Telegram Bot + Make.com** automation that lets users cut YouTube videos into MP4/MP3 clips — right from Telegram.

---

## Architecture

```
User (Telegram)
     │  /clip <url> <start> <end> [filename] [mp3]
     ▼
Telegram Bot
     │  webhook (new message event)
     ▼
Make.com Scenario
     │  parses message → HTTP POST
     ▼
Flask API (your server)
     │  yt-dlp downloads, ffmpeg cuts
     ▼
Telegram Bot API
     │  sends file directly to user
     ▼
User receives clip ✅
```

---

## 1 · Deploy the Backend

### Option A — Railway (Recommended, free tier available)

1. Push this folder to a GitHub repo
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Railway auto-detects `railway.toml` and installs `ffmpeg`
4. Note your public URL: `https://your-app.up.railway.app`

**Optional Redis (multi-worker):** Add a Railway Redis plugin → it auto-injects `REDIS_URL`, and gunicorn automatically scales to 4 workers.

### Option B — Render

1. Push to GitHub → New Web Service → connect repo
2. **Build Command:** `pip install -r requirements.txt`
3. **Start Command:** `gunicorn app:app --config gunicorn.conf.py`
4. Add env var: `NIXPACKS_APT_PKGS=ffmpeg`

### Option C — Local (with ngrok for testing)

```bash
# Install system deps
sudo apt install ffmpeg          # Linux
brew install ffmpeg              # macOS

# Copy and fill in your config
cp .env.example .env
# Edit .env — set SECRET_KEY and optionally REDIS_URL

# Install Python deps
pip install -r requirements.txt

# Run server
python app.py

# Expose via ngrok (new terminal)
ngrok http 5000
```

---

## 2 · Environment Variables

Copy `.env.example` to `.env` for local development. In production, set these in Railway / Render / Docker.

| Variable | Default | Description |
|---|---|---|
| `SECRET_KEY` | *random* | **Required in prod.** Sent in `X-Secret-Key` header. |
| `REDIS_URL` | *(none)* | Optional. Enables Redis backend + multi-worker gunicorn. |
| `MAX_CLIP_DURATION` | `3600` | Max clip length in seconds (1 hour). |
| `MAX_FILE_MB` | `50` | Telegram upload limit in MB. |
| `MAX_CONCURRENT_JOBS` | `4` | Global simultaneous worker cap. |
| `MAX_JOBS_PER_CHAT_DAY` | `5` | Max clips per chat per 24-hour rolling window. |
| `DOWNLOAD_DIR` | `downloads` | Temp folder for in-progress clips. |
| `PORT` | `5000` | HTTP port (auto-set by Railway/Render). |

---

## 3 · Create the Telegram Bot

1. Open Telegram → search **@BotFather** → `/newbot`
2. Follow prompts, choose a name and username
3. Copy the **Bot Token** (looks like `123456789:ABCdef...`)
4. Optionally set bot commands via BotFather:
   ```
   /setcommands
   clip - Cut a YouTube video clip
   start - Show usage instructions
   help - Show help
   ```

---

## 4 · Set Up Make.com Scenario

### Import the scenario manually:

Create a new scenario with these **5 modules** in order:

---

#### Module 1 — Telegram Bot: Watch Updates
- **Connection:** Add your bot token
- **Update Types:** `message`

---

#### Module 2 — Text Parser: Parse with Regex
- **Text:** `{{1.message.text}}`
- **Pattern:**
  ```
  ^\/clip\s+(https?:\/\/\S+)\s+(\S+)\s+(\S+)(?:\s+(\S+?))?(?:\s+(mp3|mp4))?$
  ```
- This captures:
  - Group 1 → YouTube URL
  - Group 2 → Start time
  - Group 3 → End time
  - Group 4 → Filename (optional)
  - Group 5 → Format `mp3` or `mp4` (optional)

---

#### Module 3 — Router (Add a filter)
- Add a **Filter** before the HTTP module:
  - Condition: `{{1.message.text}}` **Contains** `/clip`
  - This ignores non-clip messages

---

#### Module 4 — HTTP: Make a Request
- **URL:** `https://your-app.up.railway.app/clip`
- **Method:** `POST`
- **Body type:** `Raw`
- **Content type:** `application/json`
- **Headers:** add `X-Secret-Key: YOUR_SECRET_KEY`
- **Body:**
  ```json
  {
    "url":       "{{2.group[].value[1]}}",
    "start":     "{{2.group[].value[2]}}",
    "end":       "{{2.group[].value[3]}}",
    "filename":  "{{2.group[].value[4]}}",
    "format":    "{{if(2.group[].value[5]; 2.group[].value[5]; \"mp4\")}}",
    "bot_token": "YOUR_BOT_TOKEN_HERE",
    "chat_id":   "{{1.message.chat.id}}"
  }
  ```
  > ⚠️ Replace `YOUR_BOT_TOKEN_HERE` and `YOUR_SECRET_KEY` with real values.

---

#### Module 5 — (Optional) Telegram: Send a Message
- Send a quick "⏳ Got it! Processing your clip…" reply while the backend works
- Place this **before** Module 4 so users get instant feedback

---

## 5 · Using the Bot

```
/clip <youtube_url> <start> <end> [filename] [mp3]
```

### Examples

```bash
# Basic MP4 clip
/clip https://youtu.be/dQw4w9WgXcQ 0:30 1:45

# With custom filename
/clip https://youtu.be/dQw4w9WgXcQ 0:30 1:45 my_clip

# MP3 audio export
/clip https://youtu.be/dQw4w9WgXcQ 0:30 1:45 my_audio mp3

# Using hour:min:sec format
/clip https://youtu.be/dQw4w9WgXcQ 1:02:30 1:05:00 highlight_reel
```

### Time formats accepted
| Format | Example | Meaning |
|--------|---------|---------|
| `MM:SS` | `1:30` | 1 min 30 sec |
| `HH:MM:SS` | `1:02:30` | 1 hr 2 min 30 sec |
| Seconds | `90` | 90 seconds |

---

## 6 · API Reference

### `POST /clip`
```json
{
  "url":       "https://youtu.be/...",
  "start":     "1:30",
  "end":       "2:45",
  "filename":  "my_clip",
  "format":    "mp4",
  "bot_token": "123456:ABC...",
  "chat_id":   "987654321"
}
```
**Required header:** `X-Secret-Key: <SECRET_KEY>`  
**Response:** `{ "job_id": "uuid", "status": "processing" }`

---

### `POST /cancel/<job_id>`
Cancel a running clip job. **No authentication required** — the UUID is the capability token.

**Response:**
```json
{ "job_id": "uuid", "status": "cancel_requested" }
```

| Status code | Meaning |
|---|---|
| `200` | Cancel signal sent; worker will stop at the next checkpoint |
| `404` | Unknown job ID |
| `409` | Job already finished or errored |

---

### `GET /status/<job_id>`
**Response:**
```json
{ "status": "processing" | "done" | "error", "created_at": 1700000000.0 }
```

### `GET /health`
**Response:** `{ "status": "ok", "jobs": 5 }`

---

## 7 · Rate Limits & Constraints

| Limit | Value | Env var to change |
|-------|-------|---|
| Max clip duration | 60 minutes | `MAX_CLIP_DURATION` |
| Max Telegram file size | 50 MB | `MAX_FILE_MB` |
| Concurrent workers | 4 | `MAX_CONCURRENT_JOBS` |
| Per-chat daily limit | 5 clips / 24 h | `MAX_JOBS_PER_CHAT_DAY` |

> **Per-chat limit:** Each Telegram chat can submit at most 5 clip jobs in any rolling 24-hour window. The 6th request returns HTTP 429 with a `retry_after: 86400` hint.

> **File size limit:** Files over 50 MB cannot be sent via the Bot API. For longer clips, shorten the range or use MP3 format.

---

## 8 · Redis (optional, for multi-worker scaling)

By default, the bot uses an **in-memory job store** (single gunicorn worker). To scale horizontally:

1. Provision a Redis instance (Railway Redis plugin, Render Redis, etc.)
2. Set `REDIS_URL=redis://...` in your environment
3. gunicorn automatically scales to 4 workers (override with `WEB_CONCURRENCY`)

The in-memory and Redis backends expose **identical APIs** — switching is a single env-var change with no code modifications.

---

## 9 · Running the Test Suite

```bash
# Install dev dependencies (pytest + pytest-cov)
pip install -r requirements-dev.txt

# Run all tests with coverage
pytest

# Run a specific test file
pytest tests/test_utils.py -v

# Run tests matching a keyword
pytest -k "cancel" -v
```

The test suite covers:
- **`tests/test_utils.py`** — `time_to_seconds`, `safe_filename`, `progress_bar`
- **`tests/test_jobs.py`** — in-memory backend: CRUD, cancellation, pruning, daily rate-limiting
- **`tests/test_routes.py`** — all 5 HTTP endpoints via Flask test client

> Tests do **not** require ffmpeg, Telegram credentials, or a Redis instance — all external calls are mocked.

---

## 10 · Troubleshooting

| Problem | Fix |
|---------|-----|
| `ffmpeg not found` | Install ffmpeg: `apt install ffmpeg` or `brew install ffmpeg` |
| `yt-dlp error: Video unavailable` | Video may be geo-restricted or private |
| `Failed to contact Telegram` | Double-check your bot token and chat ID |
| Make.com regex not matching | Test your regex at [regex101.com](https://regex101.com) |
| File too large | Shorten the clip or use MP3 format |
| `SECRET_KEY` warning in logs | Set `SECRET_KEY` env var — random fallback changes on restart |
| 429 daily limit | Chat has hit 5 clips today — try again tomorrow (or raise `MAX_JOBS_PER_CHAT_DAY`) |
