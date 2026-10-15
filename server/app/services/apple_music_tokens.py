"""Apple Music developer credentials and short-lived token signing."""

import logging
import re
import time

import jwt

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

DEVELOPER_TOKEN_TTL_SECONDS = 15 * 60
_APPLE_IDENTIFIER_RE = re.compile(r"^[A-Z0-9]{10}$")


class AppleMusicNotConfigured(Exception):
    """Deployment credentials are incomplete or Apple's key cannot sign tokens."""


def is_configured(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    return bool(
        _APPLE_IDENTIFIER_RE.fullmatch(settings.apple_music_team_id)
        and _APPLE_IDENTIFIER_RE.fullmatch(settings.apple_music_key_id)
        and settings.apple_music_private_key
    )


def create_developer_token(
    settings: Settings | None = None, *, now: int | None = None
) -> tuple[str, int]:
    """Sign a short-lived Apple developer token without returning signing material."""
    settings = settings or get_settings()
    if not is_configured(settings):
        raise AppleMusicNotConfigured
    issued_at = int(time.time()) if now is None else now
    expires_at = issued_at + DEVELOPER_TOKEN_TTL_SECONDS
    private_key = settings.apple_music_private_key.replace("\\n", "\n")
    claims: dict[str, object] = {
        "iss": settings.apple_music_team_id,
        "iat": issued_at,
        "exp": expires_at,
    }
    origins = [origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()]
    if origins and "*" not in origins:
        claims["origin"] = origins
    try:
        token = jwt.encode(
            claims,
            private_key,
            algorithm="ES256",
            headers={"kid": settings.apple_music_key_id},
        )
    except (jwt.PyJWTError, TypeError, ValueError) as exc:
        logger.warning("Apple Music developer token signing failed: %s", type(exc).__name__)
        raise AppleMusicNotConfigured from exc
    return token, expires_at
