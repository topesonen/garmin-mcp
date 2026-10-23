"""OAuth for the HTTP server, with the login handed over to GitHub.

The MCP SDK serves the OAuth endpoints (metadata, /register, /authorize,
/token, /revoke) and checks PKCE and bearer tokens. This module is the
storage and policy behind them:

    1. claude.ai registers itself as a client (/register) and sends the
       browser to /authorize.
    2. authorize() remembers the request and sends the browser to GitHub.
    3. GitHub sends the browser back to /github/callback. The callback asks
       GitHub who logged in and continues only for GITHUB_ALLOWED_USER.
    4. The callback stores a one-time code and sends the browser back to
       claude.ai, which trades the code for tokens at /token.

Tokens and codes are stored in SQLite as SHA-256 hashes, so a copy of the
database file does not give working tokens.

Settings, from the environment (or .env next to this file):
    GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET  the GitHub OAuth app
    GITHUB_ALLOWED_USER                     the only GitHub login accepted
    PUBLIC_URL      base URL of this server (default http://127.0.0.1:8765)
    TOKEN_DB        SQLite file (default tokens.sqlite next to this file)
"""

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import anyio
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse

HERE = Path(__file__).resolve().parent

ACCESS_TOKEN_TTL_S = 3600
REFRESH_TOKEN_TTL_S = 30 * 24 * 3600
CODE_TTL_S = 300
LOGIN_TTL_S = 600

# Where an authorization code may be sent. Registering a client with any
# other redirect URI is refused, so nobody can set up a client that would
# receive codes for a login of the allowed user.
CLAUDE_REDIRECTS = {
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
}
LOCAL_REDIRECT_PREFIXES = ("http://localhost:", "http://127.0.0.1:")

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"


def load_dotenv(path=HERE / ".env"):
    """Read KEY=VALUE lines into os.environ, without overriding what is set."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def new_secret():
    # 256 bits; the spec asks for at least 128 for authorization codes.
    return secrets.token_urlsafe(32)


class GitHubOAuthProvider:
    """OAuthAuthorizationServerProvider for the SDK, with GitHub as the login."""

    def __init__(self, public_url, github_client_id, github_client_secret, allowed_user, db_path):
        self.public_url = public_url.rstrip("/")
        self.resource_url = f"{self.public_url}/mcp"
        self.github_client_id = github_client_id
        self.github_client_secret = github_client_secret
        self.allowed_user = allowed_user.lower()
        self.allow_local_redirects = self.public_url.startswith(LOCAL_REDIRECT_PREFIXES)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        with self.db:
            for table in ("clients", "logins", "codes", "access_tokens", "refresh_tokens"):
                self.db.execute(f"CREATE TABLE IF NOT EXISTS {table} (key TEXT PRIMARY KEY, data TEXT)")

    # Small key-value store, one table per kind of record.

    def put(self, table, key, data):
        with self.lock, self.db:
            self.db.execute(f"INSERT OR REPLACE INTO {table} VALUES (?, ?)", (key, json.dumps(data)))

    def get(self, table, key):
        with self.lock:
            row = self.db.execute(f"SELECT data FROM {table} WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def pop(self, table, key):
        with self.lock, self.db:
            row = self.db.execute(f"SELECT data FROM {table} WHERE key = ?", (key,)).fetchone()
            self.db.execute(f"DELETE FROM {table} WHERE key = ?", (key,))
        return json.loads(row[0]) if row else None

    def delete_client_tokens(self, client_id):
        with self.lock, self.db:
            for table in ("access_tokens", "refresh_tokens"):
                self.db.execute(f"DELETE FROM {table} WHERE json_extract(data, '$.client_id') = ?", (client_id,))

    # Clients

    async def get_client(self, client_id):
        data = self.get("clients", client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info):
        for uri in client_info.redirect_uris or []:
            uri = str(uri)
            if uri in CLAUDE_REDIRECTS:
                continue
            if self.allow_local_redirects and uri.startswith(LOCAL_REDIRECT_PREFIXES):
                continue
            raise RegistrationError("invalid_redirect_uri", f"redirect URI not allowed: {uri}")
        self.put("clients", client_info.client_id, client_info.model_dump(mode="json"))

    # Authorization: hand over to GitHub, come back through the callback.

    async def authorize(self, client, params):
        if params.resource and params.resource.rstrip("/") != self.resource_url:
            raise AuthorizeError("invalid_target", f"this server only issues tokens for {self.resource_url}")
        state = new_secret()
        self.put(
            "logins",
            state,
            {
                "client_id": client.client_id,
                "params": params.model_dump(mode="json"),
                "expires_at": time.time() + LOGIN_TTL_S,
            },
        )
        query = urllib.parse.urlencode(
            {
                "client_id": self.github_client_id,
                "redirect_uri": f"{self.public_url}/github/callback",
                "state": state,
                "allow_signup": "false",
            }
        )
        return f"{GITHUB_AUTHORIZE_URL}?{query}"

    def github_login(self, github_code):
        """Trade GitHub's code for a GitHub token and return the user's login name."""
        body = urllib.parse.urlencode(
            {
                "client_id": self.github_client_id,
                "client_secret": self.github_client_secret,
                "code": github_code,
                "redirect_uri": f"{self.public_url}/github/callback",
            }
        ).encode()
        request = urllib.request.Request(GITHUB_TOKEN_URL, data=body, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            github_token = json.load(response).get("access_token")
        if not github_token:
            return None
        # The GitHub token is used for this one request and then dropped.
        request = urllib.request.Request(
            GITHUB_USER_URL,
            headers={"Authorization": f"Bearer {github_token}", "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response).get("login")

    async def github_callback(self, request: Request):
        login_state = request.query_params.get("state", "")
        login = self.pop("logins", login_state)
        if login is None or login["expires_at"] < time.time():
            return PlainTextResponse("Login expired or unknown. Start again from Claude.", status_code=400)
        params = AuthorizationParams.model_validate(login["params"])
        github_code = request.query_params.get("code")
        user = await anyio.to_thread.run_sync(self.github_login, github_code) if github_code else None
        if user is None or user.lower() != self.allowed_user:
            redirect = construct_redirect_uri(
                str(params.redirect_uri),
                error="access_denied",
                error_description="this GitHub account is not allowed",
                state=params.state,
            )
            return RedirectResponse(redirect, status_code=302)
        code = new_secret()
        self.put(
            "codes",
            token_hash(code),
            AuthorizationCode(
                code="",
                scopes=params.scopes or [],
                expires_at=time.time() + CODE_TTL_S,
                client_id=login["client_id"],
                code_challenge=params.code_challenge,
                redirect_uri=params.redirect_uri,
                redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                resource=self.resource_url,
                subject=user,
            ).model_dump(mode="json"),
        )
        redirect = construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)
        return RedirectResponse(redirect, status_code=302)

    # Codes and tokens. The SDK checks client, expiry, redirect URI and PKCE
    # before calling the exchange methods.

    async def load_authorization_code(self, client, authorization_code):
        data = self.get("codes", token_hash(authorization_code))
        return AuthorizationCode.model_validate({**data, "code": authorization_code}) if data else None

    def issue_tokens(self, client_id, scopes, subject):
        access, refresh = new_secret(), new_secret()
        now = int(time.time())
        common = {"client_id": client_id, "scopes": scopes, "resource": self.resource_url, "subject": subject}
        self.put("access_tokens", token_hash(access), {**common, "expires_at": now + ACCESS_TOKEN_TTL_S})
        self.put("refresh_tokens", token_hash(refresh), {**common, "expires_at": now + REFRESH_TOKEN_TTL_S})
        return OAuthToken(
            access_token=access,
            expires_in=ACCESS_TOKEN_TTL_S,
            scope=" ".join(scopes) or None,
            refresh_token=refresh,
        )

    async def exchange_authorization_code(self, client, authorization_code):
        # A code works once.
        self.pop("codes", token_hash(authorization_code.code))
        return self.issue_tokens(client.client_id, authorization_code.scopes, authorization_code.subject)

    async def load_refresh_token(self, client, refresh_token):
        data = self.get("refresh_tokens", token_hash(refresh_token))
        return RefreshToken.model_validate({**data, "token": refresh_token}) if data else None

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        # Rotate: the old refresh token stops working.
        self.pop("refresh_tokens", token_hash(refresh_token.token))
        return self.issue_tokens(client.client_id, scopes, refresh_token.subject)

    async def load_access_token(self, token):
        data = self.get("access_tokens", token_hash(token))
        if data is None:
            return None
        if data["expires_at"] < time.time():
            self.pop("access_tokens", token_hash(token))
            return None
        return AccessToken.model_validate({**data, "token": token})

    async def revoke_token(self, token):
        # Revoking either token ends the whole grant for that client.
        self.delete_client_tokens(token.client_id)


def from_env():
    """Build the provider and the SDK's AuthSettings from the environment."""
    load_dotenv()
    missing = [k for k in ("GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET", "GITHUB_ALLOWED_USER") if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"missing settings for the HTTP server: {', '.join(missing)} (see auth.py)")
    provider = GitHubOAuthProvider(
        public_url=os.environ.get("PUBLIC_URL", "http://127.0.0.1:8765"),
        github_client_id=os.environ["GITHUB_CLIENT_ID"],
        github_client_secret=os.environ["GITHUB_CLIENT_SECRET"],
        allowed_user=os.environ["GITHUB_ALLOWED_USER"],
        db_path=os.environ.get("TOKEN_DB", HERE / "tokens.sqlite"),
    )
    settings = AuthSettings(
        issuer_url=provider.public_url,
        resource_server_url=provider.resource_url,
        validate_token_resource=True,
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True),
    )
    return provider, settings
