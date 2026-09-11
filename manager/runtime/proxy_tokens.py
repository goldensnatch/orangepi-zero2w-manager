from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from manager.runtime.media_stack import TRANSFER_PROXY_APP_IDS


PROXY_TOKEN_SECRET_PATH = Path("/opt/zero2w-manager/runtime/config/proxy-token-secret")
FALLBACK_SECRET = "rocky-fallback-secret"
DEFAULT_TTL_SECONDS = int(os.environ.get("ROCKY_WEB_PROXY_TOKEN_TTL_SECONDS", "43200"))


def load_proxy_token_secret() -> str:
    env_secret = os.environ.get("ROCKY_WEB_PROXY_TOKEN_SECRET")
    if env_secret:
        return env_secret.strip()
    try:
        if PROXY_TOKEN_SECRET_PATH.is_file():
            value = PROXY_TOKEN_SECRET_PATH.read_text(encoding="utf-8").strip()
            if value:
                return value
        PROXY_TOKEN_SECRET_PATH.parent.mkdir(mode=0o775, parents=True, exist_ok=True)
        generated = secrets.token_urlsafe(32)
        PROXY_TOKEN_SECRET_PATH.write_text(generated + "\n", encoding="utf-8")
        return generated
    except OSError:
        return FALLBACK_SECRET


def build_proxy_token(app_id: str, *, ttl_seconds: int | None = None) -> str:
    expires_at = int(time.time()) + max(60, int(ttl_seconds or DEFAULT_TTL_SECONDS))
    payload = json.dumps({"app": app_id, "exp": expires_at}, separators=(",", ":")).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    signature = hmac.new(
        load_proxy_token_secret().encode("utf-8"),
        payload_b64.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    signature_b64 = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return f"{payload_b64}.{signature_b64}"


def _token_app_matches(token_app: str, app_id: str) -> bool:
    token_app = str(token_app or "")
    app_id = str(app_id or "")
    if token_app == app_id:
        return True
    return token_app in TRANSFER_PROXY_APP_IDS and app_id in TRANSFER_PROXY_APP_IDS


def validate_proxy_token(token: str, app_id: str) -> bool:
    try:
        payload_b64, signature_b64 = token.split(".", 1)
    except ValueError:
        return False

    expected_signature = hmac.new(
        load_proxy_token_secret().encode("utf-8"),
        payload_b64.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    expected_signature_b64 = base64.urlsafe_b64encode(expected_signature).decode("ascii").rstrip("=")
    if not hmac.compare_digest(signature_b64, expected_signature_b64):
        return False

    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception:
        return False

    if not _token_app_matches(str(payload.get("app")), app_id):
        return False
    try:
        expires_at = int(payload.get("exp", 0))
    except (TypeError, ValueError):
        return False
    return expires_at >= int(time.time())
