"""Sign the IT Support account in on this machine, once.

    uv run python -m app.itsupport.connect

Stop the backend first: this listens on localhost:8000 for Microsoft's answer,
at the redirect address the backend's app registration already has
(/api/v1/auth/callback). A browser opens; sign in as IT Support (not as
yourself). The sign-in is kept, encrypted, in IT_SUPPORT_TOKEN_FILE, and the
backend uses it from its next start.
"""

from __future__ import annotations

import asyncio
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from app.auth.oidc import generate_pkce_pair
from app.core.config import get_settings
from app.core.mail import GRAPH_BASE
from app.itsupport.graph import CONNECT_SCOPES, SCOPES, save_sign_in

REDIRECT = "http://localhost:8000/api/v1/auth/callback"


def _wait_for_code(expected_state: str) -> dict[str, str]:
    got: dict[str, str] = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the server's name for it
            url = urlparse(self.path)
            if url.path != "/api/v1/auth/callback":
                self.send_response(404)
                self.end_headers()
                return
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            ok = query.get("state") == expected_state and "code" in query
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.end_headers()
            message = (
                "IT Support is connected. You can close this tab."
                if ok else f"Sign-in failed: {query.get('error_description') or query}"
            )
            self.wfile.write(f"<p style='font:16px sans-serif'>{message}</p>".encode())
            if ok:
                got.update(query)
            done.set()

        def log_message(self, *args: Any) -> None:
            pass

    try:
        server = HTTPServer(("localhost", 8000), Handler)
    except OSError:
        sys.exit("Port 8000 is busy. Stop the backend (run.py) first, then run this again.")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    done.wait(timeout=300)
    server.shutdown()
    return got


async def main() -> None:
    s = get_settings()
    verifier, challenge = generate_pkce_pair()
    state = secrets.token_urlsafe(24)
    url = f"{s.authority}/oauth2/v2.0/authorize?" + urlencode({
        "client_id": s.azure_client_id,
        "response_type": "code",
        "redirect_uri": REDIRECT,
        "response_mode": "query",
        # The chat's scopes and the file one: Microsoft asks consent for all
        # at once, and the one refresh token then serves both.
        "scope": CONNECT_SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "login_hint": s.it_support_email,
        "prompt": "select_account",
    })
    print(f"Sign in as {s.it_support_email} in the browser that opens.")
    print(f"If it doesn't open: {url}\n")
    webbrowser.open(url)
    got = await asyncio.to_thread(_wait_for_code, state)
    if not got:
        sys.exit("No sign-in came back.")
    async with httpx.AsyncClient(timeout=30) as http:
        response = await http.post(f"{s.authority}/oauth2/v2.0/token", data={
            "client_id": s.azure_client_id,
            "client_secret": s.azure_client_secret,
            "grant_type": "authorization_code",
            "code": got["code"],
            "redirect_uri": REDIRECT,
            "code_verifier": verifier,
            "scope": SCOPES,
        })
        if response.status_code != 200:
            sys.exit(f"Microsoft refused the sign-in: {response.text[:300]}")
        tokens = response.json()
        if not tokens.get("refresh_token"):
            sys.exit("No refresh token came back (offline_access not granted).")
        me = (await http.get(
            f"{GRAPH_BASE}/me", params={"$select": "id,displayName,mail,userPrincipalName"},
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )).json()
    email = (me.get("mail") or me.get("userPrincipalName") or "").lower()
    if email != s.it_support_email.lower():
        sys.exit(f"That was {email or 'someone else'}, not {s.it_support_email}. Nothing kept.")
    path = save_sign_in(s, tokens, me)
    print(f"Connected as {me.get('displayName')} ({email}). Kept in {path}.")
    print("Now set IT_SUPPORT_CHAT_ENABLED=true in .env and start the backend.")


if __name__ == "__main__":
    asyncio.run(main())
