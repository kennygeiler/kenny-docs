"""Access control and abuse limits for a shared deployment (PRD §10).

Locally this file does nothing: with no passwords set, `install()` leaves the app open
so `uvicorn core.app:app` still just works. The moment the app is reachable from the
internet it needs four things it does not otherwise have:

  1. A password, because /admin can ratify rules and delete the document library.
  2. A SECOND password on /admin — ENFORCED per-path (TICKETS.md C1): "can ask
     questions" and "can decide what the contract means" are different jobs held by
     different people (PRD §2). The viewer credential opens chat and documents; only
     the admin credential opens /admin/*.
  3. A rate limit on /chat and on the routes that reach the model or docling
     (/admin/draft_scenario, /admin/ingest, /admin/upload), because every prompt spends
     the operator's Anthropic budget, plus a throttle on FAILED logins (TICKETS.md C3) so the passwords can't
     be guessed online at full speed.
  4. An Origin check on state-changing requests (TICKETS.md C2): browsers attach
     cached Basic credentials to cross-site form POSTs, so without this a hostile page
     could replace a contract PDF via /admin/upload using the admin's own browser.

Modes: with only KENNY_ADMIN_PASSWORD set, chat/docs and /admin reads stay public and every /admin write path
needs the admin credential (admin-only, for a public demo); with both passwords set,
everything needs a credential and /admin needs the admin one (two-password).

Set KENNY_REQUIRE_AUTH=1 (the Dockerfile does) and a deploy missing its admin password
or its ledger HMAC key fails at startup rather than silently serving the admin panel to
the world.
"""
from __future__ import annotations

import base64
import hmac
import os
import threading
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

# JSON so the admin page can show the message in place when a visitor cancels the
# browser's sign-in prompt (admin.js reads every reply as JSON).
_UNAUTHORIZED = JSONResponse(
    {"error": "Sign-in required. Only the site owner can change rules, documents or "
              "the review queue; everything here can be viewed without signing in."},
    status_code=401,
    headers={"WWW-Authenticate": 'Basic realm="Kenny", charset="UTF-8"'},
)


def _equal(a: str, b: str) -> bool:
    # Constant-time: a plain == leaks the password one character at a time via timing.
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _credentials(request) -> tuple[str, str] | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        user, _, pw = base64.b64decode(token).decode("utf-8").partition(":")
    except Exception:
        return None
    return user, pw


def _client_ip(request) -> str:
    """The real client, not the proxy (TICKETS.md C4).

    Behind a platform proxy every connection arrives from the proxy, so
    request.client.host is the proxy's address and 'per-IP' limits collapse into one
    shared bucket — one runaway visitor 429s everyone.

      * Fly: Fly-Client-IP, trusted ONLY when FLY_APP_NAME is set (by the platform).
      * Railway / any proxy: the FIRST hop of X-Forwarded-For — the address the edge
        proxy saw. Locally nothing sets the header, so this only changes behaviour
        behind a proxy. A client can pre-seed X-Forwarded-For to pick its own bucket;
        that costs it nothing it didn't already have (fresh IPs are cheap) and the
        alternative is everyone sharing the proxy's bucket. scripts/entrypoint.sh
        also runs uvicorn with --proxy-headers so request.client is the forwarded
        address when the header is absent from this layer's view.
    """
    if os.environ.get("FLY_APP_NAME"):
        fly = request.headers.get("fly-client-ip", "").strip()
        if fly:
            return fly
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


def _origin_ok(request) -> bool:
    """Reject state-changing requests whose Origin names ANOTHER site (CSRF).

    Browsers send Origin on every cross-site POST (including the text/plain form
    trick), so a hostile page cannot avoid presenting one — and cannot forge ours.
    A missing Origin means a non-browser client (curl, tests); those don't carry
    ambient credentials, which is the thing CSRF exploits, so they pass.
    """
    origin = request.headers.get("origin", "").strip()
    if not origin:
        return True
    if origin.lower() == "null":            # sandboxed-iframe / data: URL attacker
        return False
    host = request.headers.get("host", "")
    _, _, origin_host = origin.partition("://")
    return origin_host.lower() == host.lower()


# Paths served without a credential: /healthz is the platform's liveness probe and
# reads no case data.
_PUBLIC = {"/healthz"}


class RateLimiter:
    """Fixed-window per-IP counter. Cheap, in-process, and enough for a shared demo.

    Deliberately not a distributed limiter: this exists to cap API spend and stop a
    runaway loop, not to survive a determined attacker. Multi-worker or multi-instance
    needs Redis (PRD §8) — with N workers the effective limit is N x the configured one.
    """

    def __init__(self, limit: int, window_s: int = 60):
        self.limit, self.window_s = limit, window_s
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self.window_s]
            if len(hits) >= self.limit:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            if len(self._hits) > 2048:  # bound memory; drop windows that have expired
                self._hits = {k: v for k, v in self._hits.items()
                              if v and now - v[-1] < self.window_s}
            return True


class FailureThrottle:
    """Exponential backoff on FAILED authentication, per client (TICKETS.md C3).

    The deploy script generates 144-bit passwords, but DEPLOY.md lets an operator set
    a memorable one by hand — and an unthrottled Basic-auth prompt is an online oracle.
    After `free` misses, each further miss doubles a lockout (capped), and successes
    clear it. In-process state is enough for the single-worker demo posture.
    """

    def __init__(self, free: int = 5, base_s: float = 2.0, cap_s: float = 300.0):
        self.free, self.base_s, self.cap_s = free, base_s, cap_s
        self._misses: dict[str, tuple[int, float]] = {}   # key -> (count, locked_until)
        self._lock = threading.Lock()

    def locked_for(self, key: str) -> float:
        with self._lock:
            _, until = self._misses.get(key, (0, 0.0))
            return max(0.0, until - time.time())

    def record_failure(self, key: str) -> int:
        with self._lock:
            count, _ = self._misses.get(key, (0, 0.0))
            count += 1
            delay = 0.0
            if count > self.free:
                delay = min(self.cap_s, self.base_s * (2 ** (count - self.free - 1)))
            self._misses[key] = (count, time.time() + delay)
            if len(self._misses) > 4096:
                now = time.time()
                self._misses = {k: v for k, v in self._misses.items()
                                if v[1] > now or v[0] > self.free}
            return count

    def reset(self, key: str) -> None:
        with self._lock:
            self._misses.pop(key, None)


# POSTs that reach the model (Anthropic spend) or docling (minutes of CPU per PDF).
# /chat has its own (higher) budget; the authoring routes share a tighter one. In every
# mode — open, admin-only, two-password — these are rate limited per client: removing
# a password must never remove the spend cap.
_CHAT_PATHS = {"/chat"}
_COSTLY_ADMIN_PATHS = {"/admin/draft_scenario", "/admin/ingest", "/admin/upload",
                       "/admin/try_break"}
# Admin POSTs a public visitor may run in admin-only mode: they change nothing on disk.
# try_break mutates a rule in memory and reports which known answers catch it.
_PUBLIC_ADMIN_POSTS = {"/admin/try_break"}
_READ_METHODS = ("GET", "HEAD", "OPTIONS")
_TOO_MANY = ("Too many questions in the last minute. Wait a moment and ask again.")
_TOO_MANY_ADMIN = ("Too many drafting / ingest requests in the last minute. Wait a "
                   "moment and try again.")


def _is_admin_path(path: str) -> bool:
    return path == "/admin" or path.startswith("/admin/")


class _Limits:
    def __init__(self, chat_limit: int, admin_limit: int):
        self.chat = RateLimiter(chat_limit)
        self.admin = RateLimiter(admin_limit)

    def check(self, request, ip: str) -> JSONResponse | None:
        if request.method != "POST":
            return None
        path = request.url.path
        if path in _CHAT_PATHS and not self.chat.allow(ip):
            return JSONResponse({"error": _TOO_MANY}, status_code=429)
        if path in _COSTLY_ADMIN_PATHS and not self.admin.allow(ip):
            return JSONResponse({"error": _TOO_MANY_ADMIN}, status_code=429)
        return None


class RateLimitOnlyMiddleware(BaseHTTPMiddleware):
    """Open mode: no passwords anywhere, every page serves without credentials — but
    /chat and the drafting/ingest routes still spend the operator's Anthropic budget and
    CPU, so the per-client rate limits stay on."""

    def __init__(self, app, chat_limit: int, admin_limit: int = 10):
        super().__init__(app)
        self.limits = _Limits(chat_limit, admin_limit)

    async def dispatch(self, request, call_next):
        limited = self.limits.check(request, _client_ip(request))
        if limited is not None:
            return limited
        return await call_next(request)


class AccessMiddleware(BaseHTTPMiddleware):
    """Two modes:

      * two-password (viewer_pw set): every path but /healthz needs a credential; the
        viewer password opens chat and documents, only the admin password opens /admin.
      * admin-only (viewer_pw empty): chat, documents, pages and every READ of /admin
        (the page, verification, review queue, rule library, ledger) are PUBLIC, so a
        visitor can see the whole trust story and take the tour. Every /admin WRITE —
        ratify, upload, ingest, draft, cell confirm, demo-draft load — needs the admin
        password. try_break stays public: it changes nothing on disk.
    """

    def __init__(self, app, viewer_pw: str | None, admin_pw: str, chat_limit: int,
                 ledger_factory=None, admin_limit: int = 10):
        super().__init__(app)
        self.viewer_pw, self.admin_pw = (viewer_pw or None), admin_pw
        self.limits = _Limits(chat_limit, admin_limit)
        self.failures = FailureThrottle()
        self._ledger_factory = ledger_factory

    def _ledger(self, type_: str, payload: dict) -> None:
        """Best-effort audit of auth events — a ledger problem must never take the
        auth path down with it."""
        if self._ledger_factory is None:
            return
        try:
            self._ledger_factory().append(type_, payload, actor="auth")
        except Exception:
            pass

    async def dispatch(self, request, call_next):
        path = request.url.path
        if path in _PUBLIC:
            return await call_next(request)

        ip = _client_ip(request)
        admin_path = _is_admin_path(path)
        if self.viewer_pw is not None:
            needs_auth = True
        else:
            needs_auth = (admin_path and request.method not in _READ_METHODS
                          and path not in _PUBLIC_ADMIN_POSTS)

        if needs_auth:
            remaining = self.failures.locked_for(ip)
            if remaining > 0:
                return JSONResponse(
                    {"error": "Too many failed sign-in attempts. Wait and try again."},
                    status_code=429, headers={"Retry-After": str(int(remaining) + 1)})

            creds = _credentials(request)
            if not creds:
                return _UNAUTHORIZED
            _user, pw = creds

            is_admin = _equal(pw, self.admin_pw)
            is_viewer = self.viewer_pw is not None and _equal(pw, self.viewer_pw)
            if not (is_admin or is_viewer):
                count = self.failures.record_failure(ip)
                # Ledger the first miss past the free allowance and every 10th after —
                # the trail must show a guessing campaign without letting the campaign
                # itself flood the ledger.
                if count == self.failures.free + 1 or count % 10 == 0:
                    self._ledger("auth.failed", {"ip": ip, "path": path, "count": count})
                return _UNAUTHORIZED
            self.failures.reset(ip)

            # ROLE SPLIT (TICKETS.md C1): /admin/* — the ratify gate, uploads, ingest,
            # the full ledger — requires the ADMIN credential. The viewer password opens
            # chat and the documents it cites.
            if admin_path and not is_admin:
                self._ledger("auth.denied",
                             {"ip": ip, "path": path, "role": "viewer"})
                return JSONResponse(
                    {"error": "The admin surface needs the admin password — the "
                              "viewer credential only opens chat."}, status_code=403)

        # CSRF (TICKETS.md C2): a state-changing request from another origin rides the
        # browser's cached credentials, not the user's intent.
        if request.method not in ("GET", "HEAD", "OPTIONS") and not _origin_ok(request):
            self._ledger("auth.csrf_blocked",
                         {"ip": ip, "path": path,
                          "origin": request.headers.get("origin", "")})
            return JSONResponse({"error": "cross-origin request refused"},
                                status_code=403)

        limited = self.limits.check(request, ip)
        if limited is not None:
            return limited
        return await call_next(request)


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def install(app, ledger_factory=None) -> None:
    """Attach access control if configured. Raises if a deploy asks for auth and can't have it.

    KENNY_ADMIN_PASSWORD only        -> admin-only: chat and /admin reads public, /admin writes locked.
    KENNY_ADMIN_PASSWORD + VIEWER    -> two-password: everything locked, role split.
    neither                          -> open (local dev); refused under KENNY_REQUIRE_AUTH.
    KENNY_VIEWER_PASSWORD alone      -> refused: it would lock chat yet leave /admin open.
    """
    viewer_pw = os.environ.get("KENNY_VIEWER_PASSWORD", "")
    admin_pw = os.environ.get("KENNY_ADMIN_PASSWORD", "")
    required = _truthy("KENNY_REQUIRE_AUTH")
    limit = int(os.environ.get("KENNY_CHAT_RATE_LIMIT", "20"))
    admin_limit = int(os.environ.get("KENNY_ADMIN_RATE_LIMIT", "10"))

    if viewer_pw and not admin_pw:
        raise RuntimeError(
            "KENNY_VIEWER_PASSWORD is set without KENNY_ADMIN_PASSWORD. Refusing to "
            "start: that would put chat behind a password while leaving /admin — the "
            "ratify gate and uploads — open. Set KENNY_ADMIN_PASSWORD (admin-only mode "
            "needs just that one)."
        )

    if not admin_pw:
        if required:
            raise RuntimeError(
                "KENNY_REQUIRE_AUTH is set but KENNY_ADMIN_PASSWORD is missing. Refusing "
                "to start: this would serve the admin panel, the rule-ratify gate and "
                "the API key's spend to anyone with the URL. Set KENNY_ADMIN_PASSWORD "
                "(and KENNY_VIEWER_PASSWORD too if chat should be private), or unset "
                "KENNY_REQUIRE_AUTH for a local run."
            )
        # OPEN mode (the default for a local run): no sign-in anywhere. Spend caps stay.
        app.add_middleware(RateLimitOnlyMiddleware, chat_limit=limit,
                           admin_limit=admin_limit)
        return

    if required and not os.environ.get("KENNY_LEDGER_KEY", "").strip():
        raise RuntimeError(
            "KENNY_REQUIRE_AUTH is set but KENNY_LEDGER_KEY is missing. Refusing to "
            "start: without the HMAC key the audit ledger is only internally "
            "consistent — anyone with volume access could rewrite it and verify() "
            "would still pass. Set KENNY_LEDGER_KEY in the platform's secret store "
            "(never on the data volume)."
        )

    if viewer_pw and _equal(viewer_pw, admin_pw):
        raise RuntimeError(
            "KENNY_VIEWER_PASSWORD and KENNY_ADMIN_PASSWORD are identical, which "
            "collapses the reviewer/asker separation the review gate depends on (PRD §2)."
        )

    app.add_middleware(AccessMiddleware, viewer_pw=viewer_pw or None, admin_pw=admin_pw,
                       chat_limit=limit, admin_limit=admin_limit,
                       ledger_factory=ledger_factory)
