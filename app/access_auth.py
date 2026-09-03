"""Single-invite access gate for the public single-service deployment.

The invite value remains a deployment secret. A browser only receives an
opaque, signed capability cookie. Its signature derives from the current
invite code, so rotating that Railway variable invalidates every prior cookie
without a user table or a server-side session store.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from fastapi import Response

from app.config import Settings


class InviteAuthenticator:
    cookie_name = "research_access"
    cookie_max_age = 60 * 60 * 24 * 3650
    _public_api_paths = {
        "/api/health",
        "/api/readiness",
        "/api/auth/invite",
        "/api/auth/session",
        "/api/auth/logout",
    }

    def __init__(self, settings: Settings) -> None:
        self.enabled = settings.access_auth_enabled
        self._invite_code = settings.access_invite_code
        self._cookie_secret = settings.access_cookie_secret
        self._secure_cookie = settings.access_cookie_secure

    def is_public_api_path(self, path: str) -> bool:
        return path in self._public_api_paths

    def verify_invite(self, candidate: str) -> bool:
        return bool(self._invite_code) and hmac.compare_digest(candidate, self._invite_code)

    def is_authenticated(self, token: str | None) -> bool:
        if not self.enabled:
            return True
        if not token:
            return False
        try:
            prefix, nonce, signature = token.split(".", 2)
        except ValueError:
            return False
        if prefix != "v1" or not nonce or not signature:
            return False
        return hmac.compare_digest(signature, self._signature(nonce))

    def grant(self, response: Response) -> None:
        nonce = secrets.token_urlsafe(32)
        response.set_cookie(
            key=self.cookie_name,
            value=f"v1.{nonce}.{self._signature(nonce)}",
            max_age=self.cookie_max_age,
            httponly=True,
            secure=self._secure_cookie,
            samesite="lax",
            path="/",
        )

    def revoke(self, response: Response) -> None:
        response.delete_cookie(
            key=self.cookie_name,
            httponly=True,
            secure=self._secure_cookie,
            samesite="lax",
            path="/",
        )

    def _signature(self, nonce: str) -> str:
        key_material = f"{self._cookie_secret}\x00{self._invite_code}".encode("utf-8")
        key = hashlib.sha256(key_material).digest()
        return hmac.new(key, f"v1.{nonce}".encode("utf-8"), hashlib.sha256).hexdigest()
