"""Toast authentication (machine-client / client-credentials flow).

Toast issues a bearer token from the Authentication API. Tokens are valid for a
while (Toast returns ``expiresIn`` seconds); we cache and refresh a minute early.
"""

from __future__ import annotations

import time

import requests

from .config import ToastCredentials

_AUTH_PATH = "/authentication/v1/authentication/login"


class ToastAuth:
    def __init__(self, credentials: ToastCredentials, session: requests.Session | None = None):
        self._creds = credentials
        self._session = session or requests.Session()
        self._token: str | None = None
        self._expires_at: float = 0.0

    def token(self) -> str:
        """Return a valid bearer token, fetching/refreshing as needed."""
        if self._token and time.monotonic() < self._expires_at:
            return self._token
        return self._login()

    def _login(self) -> str:
        url = f"{self._creds.host}{_AUTH_PATH}"
        payload = {
            "clientId": self._creds.client_id,
            "clientSecret": self._creds.client_secret,
            "userAccessType": self._creds.user_access_type,
        }
        resp = self._session.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        token_obj = data.get("token", data)
        access_token = token_obj.get("accessToken")
        if not access_token:
            raise RuntimeError("Toast auth response did not contain an access token")

        # Refresh 60s before the real expiry to avoid mid-request expiration.
        expires_in = float(token_obj.get("expiresIn", 3600))
        self._token = access_token
        self._expires_at = time.monotonic() + max(expires_in - 60, 30)
        return access_token
