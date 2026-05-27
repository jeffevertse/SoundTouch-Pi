"""
Gunicorn configuration for SoundTouch Pi Controller.

Production command (used by systemd service):
    gunicorn -c gunicorn.conf.py server:app

Worker model
------------
- workers=1    : single worker process — fits the Pi Zero 2 W's 512 MB RAM
- worker_class : gthread — thread-based so SSE and the audio proxy (both
                 long-lived connections) don't block the API endpoints
- threads=8    : 6 preset audio proxies + 1 SSE stream + API headroom
- timeout=120  : well above the 25 s SSE keepalive heartbeat; gunicorn
                 only kills workers that are *silent* this long, so active
                 streaming connections are never affected
"""

bind             = "0.0.0.0:5000"
workers          = 1
worker_class     = "gthread"
threads          = 8
timeout          = 120
graceful_timeout = 30

# Log to stdout/stderr so journalctl picks everything up
accesslog  = "-"
errorlog   = "-"
loglevel   = "info"

# Forward print() output from server.py into gunicorn's error log
capture_output = True


def post_worker_init(worker):
    """
    Called inside the worker process once it is fully initialised.
    Daemon threads must be started here — they are NOT inherited across
    the fork() that gunicorn uses to create worker processes.
    """
    import server
    # Parse the port from `bind` so it stays in sync if bind is ever changed
    port = int(bind.split(":")[-1])
    server._startup(port)
