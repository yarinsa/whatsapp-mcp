"""OAuth 2.1 authorization server for the remote (HTTP) MCP endpoint.

Login works without a password: when a client (claude.ai, the Claude iPhone
app, Claude Code) starts the OAuth flow, a one-time code is sent through the
bridge to the owner's own WhatsApp chat ("Message yourself"). Typing that code
on the login page approves the client. Only someone who can read the owner's
WhatsApp can approve a client.

State (clients, codes, tokens) lives in SQLite next to the bridge store so it
survives redeploys when the store is on a persistent volume. Tokens are stored
as SHA-256 hashes, never in plain text.
"""
import hashlib
import hmac
import html
import json
import os
import secrets
import sqlite3
import threading
import time
from typing import Optional

import requests
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from bridge import API_BASE_URL

STORE_DIR = os.environ.get(
    "WHATSAPP_STORE_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "whatsapp-bridge", "store"),
)
AUTH_DB_PATH = os.path.join(STORE_DIR, "oauth.db")

SCOPE = "whatsapp"
LOGIN_TTL = 10 * 60  # seconds a WhatsApp login code stays valid
MAX_ATTEMPTS = 5  # wrong codes before the login request is burned
AUTH_CODE_TTL = 5 * 60
ACCESS_TTL = 60 * 60
REFRESH_TTL = 30 * 24 * 3600

# Redirect URIs a client may register. Defaults cover Claude web/mobile
# connectors and local CLI callbacks; override with a comma-separated list.
DEFAULT_REDIRECT_PREFIXES = (
    "https://claude.ai/api/mcp/auth_callback,"
    "https://claude.com/api/mcp/auth_callback,"
    "http://localhost:,http://127.0.0.1:"
)
REDIRECT_PREFIXES = [
    p.strip()
    for p in os.environ.get("OAUTH_ALLOWED_REDIRECT_PREFIXES", DEFAULT_REDIRECT_PREFIXES).split(",")
    if p.strip()
]


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _redirect_allowed(uri: str) -> bool:
    return any(uri.startswith(prefix) for prefix in REDIRECT_PREFIXES)


class WhatsAppOAuthProvider:
    """Implements the SDK's OAuthAuthorizationServerProvider protocol."""

    def __init__(self, issuer_url: str, owner_phone: str):
        self.issuer_url = issuer_url.rstrip("/")
        self.owner_phone = owner_phone.lstrip("+").replace(" ", "")
        self._lock = threading.Lock()
        os.makedirs(STORE_DIR, exist_ok=True)
        self._db = sqlite3.connect(AUTH_DB_PATH, check_same_thread=False)
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS clients (client_id TEXT PRIMARY KEY, info TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS logins (
                id TEXT PRIMARY KEY, client_id TEXT NOT NULL, params TEXT NOT NULL,
                code_hash TEXT NOT NULL, expires_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS codes (code_hash TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tokens (
                token_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, data TEXT NOT NULL,
                family TEXT NOT NULL);
            """
        )
        self._db.commit()

    # -- storage helpers -------------------------------------------------

    def _exec(self, sql: str, args: tuple = ()) -> list:
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
            self._db.commit()
            return rows

    # -- clients ---------------------------------------------------------

    async def get_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        rows = self._exec("SELECT info FROM clients WHERE client_id = ?", (client_id,))
        return OAuthClientInformationFull.model_validate_json(rows[0][0]) if rows else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            if not _redirect_allowed(str(uri)):
                raise RegistrationError(
                    error="invalid_redirect_uri",
                    error_description=f"Redirect URI not allowed: {uri}",
                )
        self._exec(
            "INSERT OR REPLACE INTO clients VALUES (?, ?)",
            (client_info.client_id, client_info.model_dump_json()),
        )

    # -- authorization: WhatsApp one-time code ---------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        login_id = secrets.token_urlsafe(24)
        code = f"{secrets.randbelow(1_000_000):06d}"
        name = client.client_name or "an MCP client"
        message = (
            f"WhatsApp MCP login code: {code}\n"
            f"Requested by {name}. It expires in 10 minutes.\n"
            "If you didn't start this login, ignore this message."
        )
        try:
            resp = requests.post(
                f"{API_BASE_URL}/send",
                json={"recipient": self.owner_phone, "message": message},
                timeout=15,
            )
            ok = resp.status_code == 200 and resp.json().get("success")
        except requests.RequestException:
            ok = False
        if not ok:
            raise AuthorizeError(
                error="temporarily_unavailable",
                error_description="Could not send the login code over WhatsApp; is the bridge connected?",
            )
        self._exec("DELETE FROM logins WHERE expires_at < ?", (time.time(),))
        self._exec(
            "INSERT INTO logins (id, client_id, params, code_hash, expires_at) VALUES (?, ?, ?, ?, ?)",
            (login_id, client.client_id, params.model_dump_json(), _hash(code), time.time() + LOGIN_TTL),
        )
        return f"{self.issuer_url}/login?id={login_id}"

    async def login_page(self, request: Request) -> Response:
        """GET shows the code form; POST checks the code and redirects back to the client."""
        if request.method == "GET":
            return _login_html(request.query_params.get("id", ""))

        form = await request.form()
        login_id = str(form.get("id", ""))
        code = str(form.get("code", "")).strip().replace(" ", "")
        rows = self._exec(
            "SELECT client_id, params, code_hash, expires_at, attempts FROM logins WHERE id = ?",
            (login_id,),
        )
        if not rows or rows[0][3] < time.time():
            return _login_html(login_id, "This login expired. Start the connection again from your app.", done=True)
        client_id, params_json, code_hash, _, attempts = rows[0]
        if not hmac.compare_digest(_hash(code), code_hash):
            attempts += 1
            if attempts >= MAX_ATTEMPTS:
                self._exec("DELETE FROM logins WHERE id = ?", (login_id,))
                return _login_html(login_id, "Too many wrong codes. Start the connection again.", done=True)
            self._exec("UPDATE logins SET attempts = ? WHERE id = ?", (attempts, login_id))
            return _login_html(login_id, f"Wrong code. {MAX_ATTEMPTS - attempts} tries left.")

        self._exec("DELETE FROM logins WHERE id = ?", (login_id,))
        params = AuthorizationParams.model_validate_json(params_json)
        auth_code = secrets.token_urlsafe(32)
        record = AuthorizationCode(
            code=auth_code,
            scopes=params.scopes or [SCOPE],
            expires_at=time.time() + AUTH_CODE_TTL,
            client_id=client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=self.owner_phone,
        )
        self._exec("INSERT INTO codes VALUES (?, ?)", (_hash(auth_code), record.model_dump_json()))
        return RedirectResponse(
            construct_redirect_uri(str(params.redirect_uri), code=auth_code, state=params.state),
            status_code=302,
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> Optional[AuthorizationCode]:
        rows = self._exec("SELECT data FROM codes WHERE code_hash = ?", (_hash(authorization_code),))
        if not rows:
            return None
        code = AuthorizationCode.model_validate_json(rows[0][0])
        if code.client_id != client.client_id or code.expires_at < time.time():
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        # Single use: delete first so a replayed code fails.
        self._exec("DELETE FROM codes WHERE code_hash = ?", (_hash(authorization_code.code),))
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource,
                           family=secrets.token_hex(8))

    # -- tokens ----------------------------------------------------------

    def _issue(self, client_id: str, scopes: list[str], resource: Optional[str], family: str) -> OAuthToken:
        # Tokens are only for this server's /mcp endpoint; clients that omit the
        # RFC 8707 `resource` parameter get it filled in.
        resource = resource or f"{self.issuer_url}/mcp"
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = int(time.time())
        at = AccessToken(token=access, client_id=client_id, scopes=scopes, expires_at=now + ACCESS_TTL,
                         resource=resource, subject=self.owner_phone)
        rt = RefreshToken(token=refresh, client_id=client_id, scopes=scopes, expires_at=now + REFRESH_TTL,
                          resource=resource, subject=self.owner_phone)
        self._exec("INSERT INTO tokens VALUES (?, 'access', ?, ?)", (_hash(access), at.model_dump_json(), family))
        self._exec("INSERT INTO tokens VALUES (?, 'refresh', ?, ?)", (_hash(refresh), rt.model_dump_json(), family))
        self._exec("DELETE FROM tokens WHERE json_extract(data, '$.expires_at') < ?", (now,))
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_TTL,
                          refresh_token=refresh, scope=" ".join(scopes))

    def _load(self, token: str, kind: str):
        rows = self._exec("SELECT data, family FROM tokens WHERE token_hash = ? AND kind = ?", (_hash(token), kind))
        if not rows:
            return None, None
        model = AccessToken if kind == "access" else RefreshToken
        record = model.model_validate_json(rows[0][0])
        if record.expires_at and record.expires_at < time.time():
            return None, None
        return record, rows[0][1]

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> Optional[RefreshToken]:
        record, _ = self._load(refresh_token, "refresh")
        return record if record and record.client_id == client.client_id else None

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        _, family = self._load(refresh_token.token, "refresh")
        if family is None:
            raise TokenError(error="invalid_grant", error_description="Refresh token expired or revoked")
        # Rotate: the old family (access + refresh) is dropped and a new pair issued.
        self._exec("DELETE FROM tokens WHERE family = ?", (family,))
        return self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.resource,
                           family=secrets.token_hex(8))

    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        record, _ = self._load(token, "access")
        return record

    async def revoke_token(self, token) -> None:
        rows = self._exec("SELECT family FROM tokens WHERE token_hash = ?", (_hash(token.token),))
        if rows:
            self._exec("DELETE FROM tokens WHERE family = ?", (rows[0][0],))


def _login_html(login_id: str, error: str = "", done: bool = False) -> HTMLResponse:
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    form = "" if done else f"""
      <form method="post">
        <input type="hidden" name="id" value="{html.escape(login_id)}">
        <input name="code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9 ]*"
               maxlength="7" placeholder="123456" autofocus required>
        <button type="submit">Approve</button>
      </form>"""
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>WhatsApp MCP login</title>
<style>
 body{{font-family:-apple-system,system-ui,sans-serif;background:#f4f4f2;color:#1a1a1a;margin:0;
      display:flex;min-height:100vh;align-items:center;justify-content:center;padding:16px}}
 .card{{background:#fff;border-radius:16px;padding:28px;max-width:360px;width:100%;
        box-shadow:0 2px 12px rgba(0,0,0,.06)}}
 h1{{font-size:20px;margin:0 0 8px}} p{{color:#555;line-height:1.45;margin:0 0 18px}}
 input{{font-size:28px;letter-spacing:6px;text-align:center;width:100%;box-sizing:border-box;
        padding:12px;border:1px solid #ccc;border-radius:10px;margin-bottom:12px}}
 button{{width:100%;padding:14px;font-size:17px;border:0;border-radius:10px;background:#1f8a5b;color:#fff}}
 .err{{color:#b3261e}}
 @media (prefers-color-scheme:dark){{body{{background:#111;color:#eee}}.card{{background:#1c1c1c}}
   p{{color:#aaa}}input{{background:#111;color:#eee;border-color:#444}}}}
</style></head><body><div class="card">
<h1>Approve this connection</h1>
<p>We sent a 6-digit code to your own WhatsApp chat ("Message yourself"). Enter it to give this app access to your WhatsApp.</p>
{err}{form}
</div></body></html>""", status_code=400 if error else 200)
