"""Apple Music developer credentials and short-lived token signing."""

import logging
import re
import time
from urllib.parse import urlsplit

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


def _configured_origins(settings: Settings) -> list[str]:
    origins = [
        origin.strip() for origin in settings.apple_music_origins.split(",") if origin.strip()
    ]
    normalized_origins = []
    for origin in origins:
        try:
            parsed = urlsplit(origin)
            port = parsed.port
        except ValueError:
            logger.warning("Apple Music developer token origin configuration is invalid")
            raise AppleMusicNotConfigured from None
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            logger.warning("Apple Music developer token origin configuration is invalid")
            raise AppleMusicNotConfigured
        hostname = parsed.hostname
        host = f"[{hostname}]" if ":" in hostname else hostname
        default_port = 443 if parsed.scheme == "https" else 80
        if port is not None and port != default_port:
            host = f"{host}:{port}"
        normalized = f"{parsed.scheme}://{host}"
        if origin != normalized:
            logger.warning("Apple Music developer token origin configuration is invalid")
            raise AppleMusicNotConfigured
        normalized_origins.append(normalized)
    if not normalized_origins:
        logger.warning("Apple Music developer token origin configuration is missing")
        raise AppleMusicNotConfigured
    return normalized_origins


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
    origins = _configured_origins(settings)
    claims: dict[str, object] = {
        "iss": settings.apple_music_team_id,
        "iat": issued_at,
        "exp": expires_at,
        "origin": origins,
    }
    try:
        token = jwt.encode(
            claims,
            private_key,
            algorithm="ES256",
            headers={"kid": settings.apple_music_key_id},
        )
    except (jwt.PyJWTError, TypeError, ValueError) as exc:
        logger.warning(
            "Apple Music developer token signing failed: configured private key is invalid "
            "or unsupported (%s)",
            type(exc).__name__,
        )
        raise AppleMusicNotConfigured from exc
    return token, expires_at
