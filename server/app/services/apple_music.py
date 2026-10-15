"""Apple Music developer tokens, DJ authorization, and catalog matching."""

import json
import logging
import re
import time
from dataclasses import dataclass

import httpx
import jwt
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.provider_connection import UserProviderConnection
from app.models.user import User
from app.services.setbuilder.export_common import ExportTrack
from app.services.track_normalizer import artist_match_score, fuzzy_match_score
from app.services.version_filter import is_unwanted_version

logger = logging.getLogger(__name__)

APPLE_MUSIC_API_BASE = "https://api.music.apple.com/v1"
DEVELOPER_TOKEN_TTL_SECONDS = 15 * 60
HTTP_TIMEOUT_SECONDS = 10
MIN_TITLE_SIMILARITY = 0.85
MIN_ARTIST_SIMILARITY = 0.85
MATCH_THRESHOLD = 0.88
_STOREFRONT_RE = re.compile(r"^[a-z]{2}$")
_APPLE_IDENTIFIER_RE = re.compile(r"^[A-Z0-9]{10}$")


class AppleMusicError(Exception):
    """Apple Music configuration or API failure."""


class AppleMusicNotConfigured(AppleMusicError):
    """Deployment credentials are incomplete or Apple rejects its developer token."""


class AppleMusicUserAuthorizationError(AppleMusicError):
    """Apple rejected the DJ's Music User Token; the DJ must link again."""


class AppleMusicNotConnected(AppleMusicError):
    """The DJ has no connected Apple Music account."""


@dataclass(frozen=True)
class AppleCatalogSong:
    id: str
    title: str
    artist: str
    isrc: str | None


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


def _apple_get(
    path: str,
    developer_token: str,
    *,
    music_user_token: str | None = None,
    params: dict[str, str] | None = None,
) -> dict:
    headers = {"Authorization": f"Bearer {developer_token}"}
    if music_user_token:
        headers["Music-User-Token"] = music_user_token
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS) as client:
            response = client.get(f"{APPLE_MUSIC_API_BASE}{path}", headers=headers, params=params)
    except httpx.HTTPError as exc:
        logger.warning("Apple Music request failed: %s", type(exc).__name__)
        raise AppleMusicError("Apple Music request failed") from exc

    if response.status_code == 401:
        raise AppleMusicNotConfigured
    if response.status_code == 403 and music_user_token:
        raise AppleMusicUserAuthorizationError
    try:
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Apple Music returned an error: status=%s", response.status_code)
        raise AppleMusicError("Apple Music request failed") from exc


def fetch_user_storefront(music_user_token: str, developer_token: str) -> str:
    """Validate the DJ token and return Apple's two-letter storefront identifier."""
    payload = _apple_get("/me/storefront", developer_token, music_user_token=music_user_token)
    data = payload.get("data")
    storefront = data[0].get("id") if isinstance(data, list) and data else None
    if not isinstance(storefront, str) or not _STOREFRONT_RE.fullmatch(storefront):
        raise AppleMusicUserAuthorizationError
    return storefront


def _connection(db: Session, user: User) -> UserProviderConnection | None:
    return (
        db.query(UserProviderConnection)
        .filter_by(user_id=user.id, provider_id="apple_music")
        .one_or_none()
    )


def _lock_user(db: Session, user: User) -> None:
    """Serialize Apple account writes for this DJ, including first-time connects."""
    db.query(User).filter_by(id=user.id).with_for_update().one()


def link_account(db: Session, user: User, music_user_token: str) -> UserProviderConnection:
    """Validate the browser-issued user token before storing it encrypted."""
    developer_token, _ = create_developer_token()
    storefront = fetch_user_storefront(music_user_token, developer_token)
    _lock_user(db, user)
    connection = _connection(db, user)
    if connection is None:
        connection = UserProviderConnection(user_id=user.id, provider_id="apple_music")
        db.add(connection)
    connection.connection_state = "connected"
    connection.credential_type = "music_user_token"
    connection.credential_payload = json.dumps(
        {"version": 1, "music_user_token": music_user_token}, separators=(",", ":")
    )
    connection.metadata_json = json.dumps({"storefront": storefront}, separators=(",", ":"))
    connection.expires_at = None
    db.commit()
    db.refresh(connection)
    return connection


def disconnect_account(db: Session, user: User) -> None:
    """Clear user credentials while keeping a tombstone for safe future backfills."""
    _lock_user(db, user)
    connection = _connection(db, user)
    if connection is None:
        connection = UserProviderConnection(
            user_id=user.id,
            provider_id="apple_music",
            credential_type="music_user_token",
        )
        db.add(connection)
    connection.connection_state = "disconnected"
    connection.credential_payload = None
    connection.metadata_json = None
    connection.expires_at = None
    db.commit()


def account_status(db: Session, user: User) -> dict[str, object]:
    connection = _connection(db, user)
    configured = is_configured()
    if (
        configured
        and connection
        and connection.connection_state == "connected"
        and connection.credential_payload
    ):
        credentials = json.loads(connection.credential_payload)
        music_user_token = credentials.get("music_user_token")
        if not isinstance(music_user_token, str):
            connection.connection_state = "reauthorize"
            db.commit()
        else:
            try:
                developer_token, _ = create_developer_token()
                storefront = fetch_user_storefront(music_user_token, developer_token)
            except AppleMusicUserAuthorizationError:
                connection.connection_state = "reauthorize"
                db.commit()
            except AppleMusicNotConfigured:
                configured = False
            else:
                connection.metadata_json = json.dumps(
                    {"storefront": storefront}, separators=(",", ":")
                )
                db.commit()

    state = connection.connection_state if connection else "disconnected"
    metadata = json.loads(connection.metadata_json or "{}") if connection else {}
    return {
        "configured": configured,
        "linked": state == "connected" and bool(connection and connection.credential_payload),
        "requires_reauthorization": state == "reauthorize",
        "storefront": metadata.get("storefront") if state == "connected" else None,
    }


def _parse_songs(payload: dict) -> list[AppleCatalogSong]:
    data = payload.get("data", [])
    if not isinstance(data, list):
        return []
    songs = []
    for item in data:
        if not isinstance(item, dict):
            continue
        attributes = item.get("attributes") or {}
        song_id = item.get("id")
        title = attributes.get("name")
        artist = attributes.get("artistName")
        if isinstance(song_id, str) and isinstance(title, str) and isinstance(artist, str):
            isrc = attributes.get("isrc")
            songs.append(
                AppleCatalogSong(
                    id=song_id,
                    title=title,
                    artist=artist,
                    isrc=isrc if isinstance(isrc, str) else None,
                )
            )
    return songs


def search_catalog(
    _db: Session,
    storefront: str,
    term: str,
    *,
    isrc: str | None = None,
) -> list[AppleCatalogSong]:
    """Search one Apple storefront by exact ISRC or by song title/artist."""
    developer_token, _ = create_developer_token()
    if isrc:
        payload = _apple_get(
            f"/catalog/{storefront}/songs",
            developer_token,
            params={"filter[isrc]": isrc, "limit": "25"},
        )
    else:
        payload = _apple_get(
            f"/catalog/{storefront}/search",
            developer_token,
            params={"term": term, "types": "songs", "limit": "25"},
        )
        results = payload.get("results", {})
        payload = results.get("songs", {}) if isinstance(results, dict) else {}
    return _parse_songs(payload)


def _stored_connection(db: Session, user: User) -> tuple[str, str]:
    connection = _connection(db, user)
    if (
        not connection
        or connection.connection_state != "connected"
        or not connection.credential_payload
    ):
        raise AppleMusicNotConnected
    credentials = json.loads(connection.credential_payload)
    metadata = json.loads(connection.metadata_json or "{}")
    token = credentials.get("music_user_token")
    storefront = metadata.get("storefront")
    if not isinstance(token, str) or not isinstance(storefront, str):
        raise AppleMusicNotConnected
    return token, storefront


def _search_match(db: Session, track: ExportTrack, storefront: str) -> str | None:
    best_song_id = None
    best_score = 0.0
    for candidate in search_catalog(db, storefront, f"{track.artist} {track.title}"):
        if is_unwanted_version(candidate.title):
            continue
        title_score = fuzzy_match_score(track.title, candidate.title)
        artist_score = artist_match_score(track.artist, candidate.artist)
        score = title_score * 0.7 + artist_score * 0.3
        if (
            title_score >= MIN_TITLE_SIMILARITY
            and artist_score >= MIN_ARTIST_SIMILARITY
            and score >= MATCH_THRESHOLD
            and score > best_score
        ):
            best_song_id = candidate.id
            best_score = score
    return best_song_id


def resolve_tracks(
    db: Session, user: User, tracks: list[ExportTrack]
) -> tuple[list[tuple[ExportTrack, str]], list[ExportTrack]]:
    """Resolve tracks by exact ISRC first, then fuzzy title/artist in the DJ storefront."""
    _, storefront = _stored_connection(db, user)
    resolved: list[tuple[ExportTrack, str]] = []
    unresolved: list[ExportTrack] = []
    for track in tracks:
        if not track.has_metadata:
            unresolved.append(track)
            continue
        exact = None
        if track.isrc:
            exact = next(
                (
                    song
                    for song in search_catalog(db, storefront, "", isrc=track.isrc)
                    if song.isrc and song.isrc.casefold() == track.isrc.casefold()
                ),
                None,
            )
        song_id = exact.id if exact else _search_match(db, track, storefront)
        if song_id:
            resolved.append((track, song_id))
        else:
            unresolved.append(track)
    return resolved, unresolved
