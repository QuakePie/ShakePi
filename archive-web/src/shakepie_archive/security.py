"""Session authentication, CSRF checks, login throttling, and response headers."""

from __future__ import annotations

import secrets
import time
from collections import OrderedDict, deque
from typing import Any
from urllib.parse import urlsplit

from flask import Flask, abort, redirect, request, session, url_for

from shakepie_archive.config import SecurityConfig


class LoginLimiter:
    """Small bounded in-memory limiter suitable for the required single worker."""

    def __init__(self, maximum: int, window_seconds: int, capacity: int = 256) -> None:
        self.maximum = maximum
        self.window_seconds = window_seconds
        self.capacity = capacity
        self.attempts: OrderedDict[str, deque[float]] = OrderedDict()

    def allowed(self, address: str, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        values = self.attempts.setdefault(address, deque())
        while values and values[0] <= current - self.window_seconds:
            values.popleft()
        self.attempts.move_to_end(address)
        return len(values) < self.maximum

    def failed(self, address: str, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        self.attempts.setdefault(address, deque()).append(current)
        self.attempts.move_to_end(address)
        while len(self.attempts) > self.capacity:
            self.attempts.popitem(last=False)

    def clear(self, address: str) -> None:
        self.attempts.pop(address, None)


def csrf_token() -> str:
    token = session.get("csrf_token")
    if not isinstance(token, str):
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def require_csrf() -> None:
    expected = session.get("csrf_token", "")
    supplied = request.form.get("csrf_token", "") or request.headers.get("X-CSRF-Token", "")
    if not isinstance(expected, str) or not secrets.compare_digest(expected, supplied):
        abort(400, description="The form expired. Reload the page and try again.")


def safe_next(value: str | None) -> str:
    if not value:
        return url_for("overview")
    parsed = urlsplit(value)
    if (
        parsed.scheme
        or parsed.netloc
        or not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
    ):
        return url_for("overview")
    return value


def install_security(app: Flask, config: SecurityConfig) -> LoginLimiter:
    limiter = LoginLimiter(config.login_attempts, config.login_window_seconds)
    app.config.update(
        SECRET_KEY=config.secret_key,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=config.session_cookie_secure,
        PERMANENT_SESSION_LIFETIME=config.session_lifetime_minutes * 60,
        MAX_CONTENT_LENGTH=16 * 1024,
    )

    @app.before_request
    def protect_request() -> Any:
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            require_csrf()
        endpoint = request.endpoint or ""
        if endpoint in {"health", "login", "login_submit", "static"}:
            return None
        if session.get("authenticated") is not True:
            return redirect(url_for("login", next=request.full_path.rstrip("?")))
        session.permanent = True
        return None

    @app.after_request
    def add_headers(response: Any) -> Any:
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
        )
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "no-store"
        return response

    app.jinja_env.globals["csrf_token"] = csrf_token
    return limiter
