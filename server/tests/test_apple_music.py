"""Apple Music developer-token, account-link, and catalog-resolution tests."""

import json

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import text

from app.core.config import Settings
from app.models.provider_connection import UserProviderConnection
from app.services import apple_music
from app.services.setbuilder.export_common import ExportTrack


@pytest.fixture
def apple_settings():
    private_key = (
        ec.generate_private_key(ec.SECP256R1())
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )
    return Settings(
        apple_music_team_id="TEAM123456",
        apple_music_key_id="KEY1234567",
        apple_music_private_key=private_key,
    )


def test_developer_token_is_es256_and_short_lived(apple_settings):
    token, expires_at = apple_music.create_developer_token(apple_settings, now=1_800_000_000)

    assert jwt.get_unverified_header(token) == {"alg": "ES256", "kid": "KEY1234567", "typ": "JWT"}
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["iss"] == "TEAM123456"
    assert claims["iat"] == 1_800_000_000
    assert expires_at == claims["exp"]
    assert 0 < expires_at - claims["iat"] <= apple_music.DEVELOPER_TOKEN_TTL_SECONDS


def test_developer_token_rejects_malformed_private_key(apple_settings):
    apple_settings.apple_music_private_key = "not a private key"

    with pytest.raises(apple_music.AppleMusicNotConfigured):
        apple_music.create_developer_token(apple_settings, now=1_800_000_000)


def test_developer_token_requires_apple_credentials():
    with pytest.raises(apple_music.AppleMusicNotConfigured):
        apple_music.create_developer_token(Settings(), now=1_800_000_000)


def test_link_validates_user_token_and_persists_encrypted_credential(db, test_user, monkeypatch):
    monkeypatch.setattr(apple_music, "create_developer_token", lambda *_args: ("dev-token", 100))
    monkeypatch.setattr(apple_music, "fetch_user_storefront", lambda *_args: "us")

    connection = apple_music.link_account(db, test_user, "music-user-token-1234567890")

    assert connection.provider_id == "apple_music"
    assert connection.connection_state == "connected"
    assert connection.metadata_json == '{"storefront":"us"}'
    assert json.loads(connection.credential_payload) == {
        "version": 1,
        "music_user_token": "music-user-token-1234567890",
    }
    raw = db.execute(
        text("SELECT credential_payload FROM user_provider_connections WHERE user_id = :id"),
        {"id": test_user.id},
    ).scalar_one()
    assert raw.startswith("gAAAAA")
    assert "music-user-token-1234567890" not in raw


def test_invalid_user_token_does_not_create_connection(db, test_user, monkeypatch):
    monkeypatch.setattr(apple_music, "create_developer_token", lambda *_args: ("dev-token", 100))

    def reject(*_args):
        raise apple_music.AppleMusicUserAuthorizationError

    monkeypatch.setattr(apple_music, "fetch_user_storefront", reject)

    with pytest.raises(apple_music.AppleMusicUserAuthorizationError):
        apple_music.link_account(db, test_user, "music-user-token-1234567890")

    assert db.query(UserProviderConnection).count() == 0


def test_resolution_tries_exact_isrc_then_fuzzy_title_artist(db, test_user, monkeypatch):
    connection = UserProviderConnection(
        user_id=test_user.id,
        provider_id="apple_music",
        connection_state="connected",
        credential_type="music_user_token",
        credential_payload='{"version":1,"music_user_token":"music-token"}',
        metadata_json='{"storefront":"us"}',
    )
    db.add(connection)
    db.commit()
    queries = []

    def fake_search(_db, storefront, term, *, isrc=None):
        queries.append((storefront, term, isrc))
        if isrc:
            return []
        return [
            apple_music.AppleCatalogSong(
                id="12345", title="Night Drive", artist="The Example", isrc=None
            )
        ]

    monkeypatch.setattr(apple_music, "search_catalog", fake_search)
    tracks = [
        ExportTrack(
            position=0,
            title="Night Drive",
            artist="The Example",
            album=None,
            isrc="USRC17607839",
            duration_sec=None,
        )
    ]

    resolved, unresolved = apple_music.resolve_tracks(db, test_user, tracks)

    assert [(track.position, song_id) for track, song_id in resolved] == [(0, "12345")]
    assert unresolved == []
    assert queries == [
        ("us", "", "USRC17607839"),
        ("us", "The Example Night Drive", None),
    ]


def test_resolution_filters_unwanted_versions(db, test_user, monkeypatch):
    db.add(
        UserProviderConnection(
            user_id=test_user.id,
            provider_id="apple_music",
            connection_state="connected",
            credential_type="music_user_token",
            credential_payload='{"version":1,"music_user_token":"music-token"}',
            metadata_json='{"storefront":"us"}',
        )
    )
    db.commit()
    monkeypatch.setattr(
        apple_music,
        "search_catalog",
        lambda *_args, **_kwargs: [
            apple_music.AppleCatalogSong(
                id="bad", title="Night Drive (Live)", artist="The Example", isrc=None
            )
        ],
    )
    tracks = [
        ExportTrack(
            position=0,
            title="Night Drive",
            artist="The Example",
            album=None,
            isrc=None,
            duration_sec=None,
        )
    ]

    resolved, unresolved = apple_music.resolve_tracks(db, test_user, tracks)

    assert resolved == []
    assert [track.position for track in unresolved] == [0]


def test_account_status_is_unconfigured_and_unlinked_by_default(db, test_user, monkeypatch):
    monkeypatch.setattr(apple_music, "get_settings", lambda: Settings())

    assert apple_music.account_status(db, test_user) == {
        "configured": False,
        "linked": False,
        "requires_reauthorization": False,
        "storefront": None,
    }


def test_account_status_marks_rejected_stored_user_token_for_reauthorization(
    db, test_user, monkeypatch
):
    connection = UserProviderConnection(
        user_id=test_user.id,
        provider_id="apple_music",
        connection_state="connected",
        credential_type="music_user_token",
        credential_payload='{"version":1,"music_user_token":"stored-user-token"}',
        metadata_json='{"storefront":"us"}',
    )
    db.add(connection)
    db.commit()
    monkeypatch.setattr(apple_music, "is_configured", lambda: True)
    monkeypatch.setattr(apple_music, "create_developer_token", lambda: ("developer-token", 100))

    def reject(*_args):
        raise apple_music.AppleMusicUserAuthorizationError

    monkeypatch.setattr(apple_music, "fetch_user_storefront", reject)

    status = apple_music.account_status(db, test_user)

    assert status == {
        "configured": True,
        "linked": False,
        "requires_reauthorization": True,
        "storefront": None,
    }
    db.refresh(connection)
    assert connection.connection_state == "reauthorize"
    assert connection.credential_payload is not None


def test_account_status_does_not_request_reauthorization_for_bad_developer_credentials(
    db, test_user, monkeypatch
):
    db.add(
        UserProviderConnection(
            user_id=test_user.id,
            provider_id="apple_music",
            connection_state="connected",
            credential_type="music_user_token",
            credential_payload='{"version":1,"music_user_token":"stored-user-token"}',
            metadata_json='{"storefront":"us"}',
        )
    )
    db.commit()
    monkeypatch.setattr(apple_music, "is_configured", lambda: True)
    monkeypatch.setattr(
        apple_music,
        "create_developer_token",
        lambda: (_ for _ in ()).throw(apple_music.AppleMusicNotConfigured),
    )

    status = apple_music.account_status(db, test_user)

    assert status["configured"] is False
    assert status["requires_reauthorization"] is False


def test_disconnect_clears_credentials_and_keeps_connection_tombstone(db, test_user):
    connection = UserProviderConnection(
        user_id=test_user.id,
        provider_id="apple_music",
        connection_state="connected",
        credential_type="music_user_token",
        credential_payload='{"version":1,"music_user_token":"secret"}',
        metadata_json='{"storefront":"us"}',
    )
    db.add(connection)
    db.commit()

    apple_music.disconnect_account(db, test_user)

    db.refresh(connection)
    assert connection.connection_state == "disconnected"
    assert connection.credential_payload is None
    assert connection.metadata_json is None
    assert db.query(UserProviderConnection).count() == 1


def test_user_storefront_request_uses_developer_and_music_user_headers(monkeypatch):
    request = {}

    class Response:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"id": "gb"}]}

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, url, *, headers, params=None):
            request.update(url=url, headers=headers, params=params)
            return Response()

    monkeypatch.setattr(apple_music.httpx, "Client", Client)

    assert apple_music.fetch_user_storefront("music-token", "developer-token") == "gb"
    assert request == {
        "url": f"{apple_music.APPLE_MUSIC_API_BASE}/me/storefront",
        "headers": {
            "Authorization": "Bearer developer-token",
            "Music-User-Token": "music-token",
        },
        "params": None,
    }


def test_developer_token_rejection_does_not_mark_user_for_reauthorization(monkeypatch):
    class Response:
        status_code = 401

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(apple_music.httpx, "Client", Client)

    with pytest.raises(apple_music.AppleMusicNotConfigured):
        apple_music.fetch_user_storefront("music-token", "bad-developer-token")


def test_user_token_rejection_surfaces_as_reauthorization(monkeypatch):
    class Response:
        status_code = 403

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(apple_music.httpx, "Client", Client)

    with pytest.raises(apple_music.AppleMusicUserAuthorizationError):
        apple_music.fetch_user_storefront("invalid-user-token", "developer-token")


def test_api_status_does_not_expose_tokens(client, auth_headers, monkeypatch):
    monkeypatch.setattr("app.services.apple_music.get_settings", lambda: Settings())

    response = client.get("/api/apple-music/status", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {
        "configured": False,
        "linked": False,
        "requires_reauthorization": False,
        "storefront": None,
    }
    assert "token" not in response.text.lower()


def test_api_developer_token_reports_clear_not_configured(client, auth_headers, monkeypatch):
    monkeypatch.setattr("app.services.apple_music.get_settings", lambda: Settings())

    response = client.get("/api/apple-music/developer-token", headers=auth_headers)

    assert response.status_code == 503
    assert response.json()["detail"] == "Apple Music is not configured"


def test_api_developer_token_is_not_cached(client, auth_headers, monkeypatch, apple_settings):
    monkeypatch.setattr("app.services.apple_music.get_settings", lambda: apple_settings)

    response = client.get("/api/apple-music/developer-token", headers=auth_headers)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["developer_token"]


def test_api_link_validates_token_before_service_call(client, auth_headers, monkeypatch):
    monkeypatch.setattr(
        "app.api.apple_music.apple_music.link_account",
        lambda *_args: pytest.fail("invalid token must be rejected at the API boundary"),
    )

    response = client.post(
        "/api/apple-music/link",
        headers=auth_headers,
        json={"music_user_token": "short"},
    )

    assert response.status_code == 422


def test_api_link_returns_storefront_without_echoing_user_token(client, auth_headers, monkeypatch):
    monkeypatch.setattr(
        "app.api.apple_music.apple_music.link_account",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "app.api.apple_music.apple_music.account_status",
        lambda *_args: {
            "configured": True,
            "linked": True,
            "requires_reauthorization": False,
            "storefront": "us",
        },
    )
    user_token = "music-user-token-1234567890"

    response = client.post(
        "/api/apple-music/link",
        headers=auth_headers,
        json={"music_user_token": user_token},
    )

    assert response.status_code == 200
    assert response.json() == {
        "configured": True,
        "linked": True,
        "requires_reauthorization": False,
        "storefront": "us",
    }
    assert user_token not in response.text


@pytest.mark.parametrize(
    ("exception", "expected_status"),
    [
        (apple_music.AppleMusicUserAuthorizationError, 400),
        (apple_music.AppleMusicNotConfigured, 503),
        (apple_music.AppleMusicError, 502),
    ],
)
def test_api_link_distinguishes_user_token_from_developer_configuration_errors(
    client, auth_headers, monkeypatch, exception, expected_status
):
    def fail(*_args):
        raise exception

    monkeypatch.setattr("app.api.apple_music.apple_music.link_account", fail)

    response = client.post(
        "/api/apple-music/link",
        headers=auth_headers,
        json={"music_user_token": "music-user-token-1234567890"},
    )

    assert response.status_code == expected_status
    assert "music-user-token-1234567890" not in response.text


def test_catalog_search_uses_isrc_filter_and_requested_storefront(monkeypatch):
    request = {}
    monkeypatch.setattr(
        apple_music,
        "create_developer_token",
        lambda: ("developer-token", 100),
    )

    class Response:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "data": [
                    {
                        "id": "apple-song-id",
                        "attributes": {
                            "name": "Night Drive",
                            "artistName": "The Example",
                            "isrc": "USRC17607839",
                        },
                    }
                ]
            }

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, url, *, headers, params=None):
            request.update(url=url, headers=headers, params=params)
            return Response()

    monkeypatch.setattr(apple_music.httpx, "Client", Client)

    songs = apple_music.search_catalog(None, "gb", "", isrc="USRC17607839")

    assert songs == [
        apple_music.AppleCatalogSong(
            id="apple-song-id", title="Night Drive", artist="The Example", isrc="USRC17607839"
        )
    ]
    assert request["url"] == f"{apple_music.APPLE_MUSIC_API_BASE}/catalog/gb/songs"
    assert request["params"] == {"filter[isrc]": "USRC17607839", "limit": "25"}
    assert request["headers"]["Authorization"] == "Bearer developer-token"
