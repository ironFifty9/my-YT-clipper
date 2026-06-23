# ══════════════════════════════════════════════════════════════════════════════
# Procfile — Process declaration for Heroku-compatible platforms
# ══════════════════════════════════════════════════════════════════════════════
#
# The Procfile is used by:
#   - Heroku (reads this natively)
#   - Render (can be configured to use Procfile instead of a start command)
#   - Any Heroku-buildpack-compatible platform
#
# For Railway deployments, railway.toml (startCommand) takes precedence.
# This file is kept as a fallback for portability.
#
# Format:  <process_type>: <command>
#   "web" is the special process type for HTTP-handling processes.
#   Platforms route external HTTP traffic only to processes declared as "web".
# ══════════════════════════════════════════════════════════════════════════════

# Start gunicorn as the web process.
# --config gunicorn.conf.py applies all settings from that file:
#   workers, worker_class, threads, timeout, accesslog, errorlog, etc.
web: gunicorn app:app --config gunicorn.conf.py
