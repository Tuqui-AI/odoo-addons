import base64
import hashlib
import hmac
import json
import logging
import secrets
import time

from odoo import http
from odoo.http import Response, request


_LOG = logging.getLogger(__name__)

_ACCESS_TOKEN_TTL_SECONDS = 3600  # 1 hour
_SIGNING_KEY_PARAM = "tuqui.oauth.signing_key"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = b"=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value.encode("ascii") + padding)


def _get_or_create_signing_key(env):
    """HMAC-SHA256 key for signing access tokens. Persists in ir.config_parameter (sudo)."""
    icp = env["ir.config_parameter"].sudo()
    key_b64 = icp.get_param(_SIGNING_KEY_PARAM)
    if not key_b64:
        key_b64 = _b64url(secrets.token_bytes(32))
        icp.set_param(_SIGNING_KEY_PARAM, key_b64)
    return _b64url_decode(key_b64)


def _issue_access_token(env, client_id):
    """Compact JWT-like token: header.payload.signature (HS256, base64url)."""
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "iss": "tuqui-module",
        "sub": client_id,
        "iat": now,
        "exp": now + _ACCESS_TOKEN_TTL_SECONDS,
        "aud": "tuqui",
    }
    head_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{head_b64}.{payload_b64}".encode("ascii")
    signature = hmac.new(_get_or_create_signing_key(env), signing_input, hashlib.sha256).digest()
    return f"{head_b64}.{payload_b64}.{_b64url(signature)}"


def verify_access_token(env, token):
    """Return payload dict if valid, otherwise None. Validates signature and exp."""
    try:
        head_b64, payload_b64, sig_b64 = token.split(".")
    except (ValueError, AttributeError):
        return None
    signing_input = f"{head_b64}.{payload_b64}".encode("ascii")
    expected_sig = hmac.new(
        _get_or_create_signing_key(env), signing_input, hashlib.sha256
    ).digest()
    try:
        provided_sig = _b64url_decode(sig_b64)
    except Exception:
        return None
    if not hmac.compare_digest(expected_sig, provided_sig):
        return None
    try:
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload


def _json_response(body, status=200):
    return Response(
        json.dumps(body),
        content_type="application/json",
        status=status,
    )


def _oauth_error(code, status=400, description=None):
    body = {"error": code}
    if description:
        body["error_description"] = description
    return _json_response(body, status=status)


class TuquiOAuth(http.Controller):
    """OAuth 2.0 client_credentials grant (RFC 6749 §4.4) — minimal."""

    @http.route(
        "/tuqui/oauth/token",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
    )
    def token(self, **post):
        grant_type = (post.get("grant_type") or "").strip()
        client_id = (post.get("client_id") or "").strip()
        client_secret = (post.get("client_secret") or "").strip()
        if grant_type != "client_credentials":
            return _oauth_error("unsupported_grant_type", status=400)
        if not client_id or not client_secret:
            return _oauth_error("invalid_request", status=400)
        env = request.env
        client = env["tuqui.oauth.client"].sudo()._get_singleton()
        if not client or client.client_id != client_id or not client.verify_secret(client_secret):
            _LOG.info("Tuqui OAuth: invalid credentials for client_id=%s", client_id)
            return _oauth_error("invalid_client", status=401)
        access_token = _issue_access_token(env, client_id)
        client.touch_last_seen()
        return _json_response(
            {
                "access_token": access_token,
                "token_type": "Bearer",
                "expires_in": _ACCESS_TOKEN_TTL_SECONDS,
            }
        )

    @http.route(
        "/tuqui/oauth/revoke",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
    )
    def revoke(self, **post):
        """Rotate the signing key, which invalidates every outstanding access token.

        Authenticated by the current ``client_secret`` so a leaked access token
        alone cannot wipe the workspace's connection.
        """
        client_id = (post.get("client_id") or "").strip()
        client_secret = (post.get("client_secret") or "").strip()
        env = request.env
        client = env["tuqui.oauth.client"].sudo()._get_singleton()
        if not client or client.client_id != client_id or not client.verify_secret(client_secret):
            return _oauth_error("invalid_client", status=401)
        # Rotate signing key → invalidate every token issued so far.
        new_key = _b64url(secrets.token_bytes(32))
        env["ir.config_parameter"].sudo().set_param(_SIGNING_KEY_PARAM, new_key)
        return _json_response({"ok": True})
