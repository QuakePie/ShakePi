"""Gunicorn settings loaded from the validated archive configuration."""

from shakepie_archive.config import load_settings

settings = load_settings()
bind = f"{settings.web.bind_address}:{settings.web.port}"
workers = settings.web.workers
threads = settings.web.threads
worker_class = "gthread"
timeout = 120
graceful_timeout = 30
keepalive = 2
accesslog = "-"
errorlog = "-"
capture_output = True
