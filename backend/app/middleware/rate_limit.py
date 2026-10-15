"""Rate Limiting Middleware - Token Bucket per client IP"""
import time
from collections import defaultdict
from threading import Lock
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.config import settings


class _TokenBucket:
    """Simple token bucket for a single client."""

    __slots__ = ("tokens", "last_refill", "capacity", "refill_rate")

    def __init__(self, capacity: int, refill_rate: float):
        self.capacity = capacity
        self.refill_rate = refill_rate  # tokens per second
        self.tokens = float(capacity)
        self.last_refill = time.monotonic()

    def consume(self) -> bool:
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Per-IP rate limiting middleware.

    Uses a token-bucket algorithm. Each IP gets *capacity* tokens that refill
    at *capacity / window* tokens per second.  When the bucket is empty the
    request is rejected with 429.
    """

    def __init__(self, app, capacity: int | None = None, window: int | None = None):
        super().__init__(app)
        self.capacity = capacity or settings.RATE_LIMIT_REQUESTS
        self.window = window or settings.RATE_LIMIT_WINDOW_SECONDS
        self.refill_rate = self.capacity / self.window
        self._buckets: dict[str, _TokenBucket] = defaultdict(
            lambda: _TokenBucket(self.capacity, self.refill_rate)
        )
        self._lock = Lock()
        self._last_cleanup = time.monotonic()

    # Paths that should never be rate-limited
    _EXEMPT = frozenset({"/health", "/docs", "/redoc", "/openapi.json"})

    async def dispatch(self, request: Request, call_next):
        # Skip rate limiting for health / docs endpoints
        if request.url.path in self._EXEMPT:
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"

        with self._lock:
            # Periodic cleanup of stale buckets (every 5 minutes)
            now = time.monotonic()
            if now - self._last_cleanup > 300:
                self._cleanup(now)
                self._last_cleanup = now

            bucket = self._buckets[client_ip]
            allowed = bucket.consume()

        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": "请求过于频繁，请稍后重试"},
            )

        return await call_next(request)

    def _cleanup(self, now: float) -> None:
        """Remove buckets that have been idle for longer than 2× the window."""
        stale_threshold = self.window * 2
        stale = [
            ip
            for ip, b in self._buckets.items()
            if now - b.last_refill > stale_threshold
        ]
        for ip in stale:
            del self._buckets[ip]


# ---------------------------------------------------------------------------
# Auth-endpoint limits (P2-AUTH-BOUNDS / DS-13).
#
# The middleware above is one generous bucket per client IP for the whole API.
# Credential stuffing and guest-token farming need their own, tighter budgets:
# a failure counter for /auth/login and an issuance counter for /auth/guest.
# Keys stay the client IP, i.e. the existing `request.client.host` semantics are
# preserved (no proxy / X-Forwarded-For interpretation change).
# ---------------------------------------------------------------------------
LOGIN_MAX_FAILURES = 5
LOGIN_FAILURE_WINDOW_SECONDS = 300
GUEST_MAX_ISSUES = 10
GUEST_ISSUE_WINDOW_SECONDS = 60


class FailureWindowLimiter:
    """Count failures per key inside a sliding window; block once the cap is hit.

    Account and IP keys have independent budgets. Concurrent in-flight attempts
    reserve budget before authentication; success can clear only its account key.
    """

    def __init__(self, max_failures: int = LOGIN_MAX_FAILURES,
                 window_seconds: int = LOGIN_FAILURE_WINDOW_SECONDS):
        self.max_failures = max(1, int(max_failures))
        self.window = max(1, int(window_seconds))
        self._events: dict[str, list[float]] = defaultdict(list)
        self._lock = Lock()
        self._pending: dict[object, int] = defaultdict(int)

    def begin_attempt(self, keys) -> bool:
        """Atomically reserve each budget before the password check yields."""
        with self._lock:
            now = time.monotonic()
            if any(len(self._recent(key, now)) + self._pending.get(key, 0)
                   >= self.max_failures for key in keys):
                return False
            for key in keys:
                self._pending[key] += 1
            return True

    def finish_attempt(self, keys, succeeded: bool, account_key) -> None:
        with self._lock:
            now = time.monotonic()
            for key in keys:
                remaining = self._pending.get(key, 0) - 1
                if remaining > 0:
                    self._pending[key] = remaining
                else:
                    self._pending.pop(key, None)
                if not succeeded:
                    events = self._recent(key, now)
                    events.append(now)
                    self._events[key] = events
            if succeeded:
                self._events.pop(account_key, None)

    def _recent(self, key: str, now: float) -> list[float]:
        cutoff = now - self.window
        kept = [t for t in self._events.get(key, ()) if t > cutoff]
        if kept:
            self._events[key] = kept
        else:
            self._events.pop(key, None)
        return kept

    def is_blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._recent(key, time.monotonic())) >= self.max_failures

    def record_failure(self, key: str) -> None:
        with self._lock:
            now = time.monotonic()
            events = self._recent(key, now)
            events.append(now)
            self._events[key] = events

    def reset(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)


class IssueWindowLimiter:
    """Cap how many times one key may issue something inside a sliding window."""

    def __init__(self, max_issues: int = GUEST_MAX_ISSUES,
                 window_seconds: int = GUEST_ISSUE_WINDOW_SECONDS):
        self.max_issues = max(1, int(max_issues))
        self.window = max(1, int(window_seconds))
        self._events: dict[str, list[float]] = defaultdict(list)
        self._lock = Lock()

    def allow(self, key: str) -> bool:
        with self._lock:
            now = time.monotonic()
            cutoff = now - self.window
            events = [t for t in self._events.get(key, ()) if t > cutoff]
            if len(events) >= self.max_issues:
                self._events[key] = events
                return False
            events.append(now)
            self._events[key] = events
            return True


def client_key(request) -> str:
    """The rate-limit key for an auth request — unchanged client.host semantics."""
    client = getattr(request, "client", None)
    return client.host if client else "unknown"
