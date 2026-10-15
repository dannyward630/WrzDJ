"""Apple Music connection lifecycle and stored-credential boundary tests."""

import json

import pytest
from sqlalchemy import text

from app.models.provider_connection import UserProviderConnection
from app.services import apple_music_accounts, apple_music_tokens

VALID_TOKEN = "music-user-token-1234567890"


def test_link_validates_and_stores_the_encrypted_user_token(db, test_user, monkeypatch):
    monkeypatch.setattr(apple_music_tokens, "create_developer_token", lambda: ("dev", 100))
    monkeypatch.setattr(apple_music_accounts, "fetch_user_storefront", lambda *_: "us")

    connection = apple_music_accounts.link_account(db, test_user, VALID_TOKEN)

    assert connection.connection_state == "connected"
    assert json.loads(connection.credential_payload) == {
        "version": 1,
        "music_user_token": VALID_TOKEN,
    }
    assert json.loads(connection.metadata_json) == {"storefront": "us"}
    stored = db.execute(
        text("SELECT credential_payload FROM user_provider_connections WHERE id = :id"),
        {"id": connection.id},
    ).scalar_one()
    assert VALID_TOKEN not in stored


def test_disconnect_during_validation_invalidates_the_in_flight_link(db, test_user, monkeypatch):
    monkeypatch.setattr(apple_music_tokens, "create_developer_token", lambda: ("dev", 100))

    def disconnect_while_apple_is_validating(*_args):
        apple_music_accounts.disconnect_account(db, test_user)
        return "us"

    monkeypatch.setattr(
        apple_music_accounts, "fetch_user_storefront", disconnect_while_apple_is_validating
    )

    with pytest.raises(apple_music_tokens.AppleMusicLinkSuperseded):
        apple_music_accounts.link_account(db, test_user, VALID_TOKEN)

    connection = db.query(UserProviderConnection).filter_by(user_id=test_user.id).one()
    assert connection.connection_state == "disconnected"
    assert connection.credential_payload is None
    assert connection.metadata_json is None
    assert connection.authorization_generation == 2


def test_invalid_user_token_leaves_a_disconnected_tombstone(db, test_user, monkeypatch):
    monkeypatch.setattr(apple_music_tokens, "create_developer_token", lambda: ("dev", 100))

    def reject(*_args):
        raise apple_music_tokens.AppleMusicUserAuthorizationError

    monkeypatch.setattr(apple_music_accounts, "fetch_user_storefront", reject)

    with pytest.raises(apple_music_tokens.AppleMusicUserAuthorizationError):
        apple_music_accounts.link_account(db, test_user, VALID_TOKEN)

    connection = db.query(UserProviderConnection).filter_by(user_id=test_user.id).one()
    assert connection.connection_state == "disconnected"
    assert connection.credential_payload is None


def test_account_status_rechecks_the_saved_user_token(db, test_user, monkeypatch):
    connection = UserProviderConnection(
        user_id=test_user.id,
        provider_id="apple_music",
        connection_state="connected",
        credential_type="music_user_token",
        credential_payload=json.dumps({"version": 1, "music_user_token": VALID_TOKEN}),
        metadata_json='{"storefront":"us"}',
    )
    db.add(connection)
    db.commit()
    monkeypatch.setattr(apple_music_tokens, "is_configured", lambda: True)
    monkeypatch.setattr(apple_music_tokens, "create_developer_token", lambda: ("dev", 100))
    monkeypatch.setattr(apple_music_accounts, "fetch_user_storefront", lambda *_: "gb")

    assert apple_music_accounts.account_status(db, test_user) == {
        "configured": True,
        "linked": True,
        "requires_reauthorization": False,
        "storefront": "gb",
    }


@pytest.mark.parametrize(
    ("credential_payload", "metadata_json"),
    [
        ("{not-json", '{"storefront":"us"}'),
        ('{"version":1}', '{"storefront":"us"}'),
        ('{"version":true,"music_user_token":"' + VALID_TOKEN + '"}', '{"storefront":"us"}'),
        ('{"version":2,"music_user_token":"' + VALID_TOKEN + '"}', '{"storefront":"us"}'),
        ('{"version":1,"music_user_token":12}', '{"storefront":"us"}'),
        ('{"version":1,"music_user_token":"' + VALID_TOKEN + '"}', "{not-json"),
        ('{"version":1,"music_user_token":"' + VALID_TOKEN + '"}', '{"storefront":12}'),
    ],
)
def test_status_marks_malformed_stored_credentials_for_reauthorization(
    db, test_user, monkeypatch, credential_payload, metadata_json
):
    connection = UserProviderConnection(
        user_id=test_user.id,
        provider_id="apple_music",
        connection_state="connected",
        credential_type="music_user_token",
        credential_payload=credential_payload,
        metadata_json=metadata_json,
    )
    db.add(connection)
    db.commit()
    monkeypatch.setattr(apple_music_tokens, "is_configured", lambda: True)
    monkeypatch.setattr(
        apple_music_accounts,
        "fetch_user_storefront",
        lambda *_: pytest.fail("malformed stored credentials must not be sent to Apple"),
    )

    assert apple_music_accounts.account_status(db, test_user) == {
        "configured": True,
        "linked": False,
        "requires_reauthorization": True,
        "storefront": None,
    }


def test_resolution_storage_boundary_fails_closed_for_malformed_payload(db, test_user):
    db.add(
        UserProviderConnection(
            user_id=test_user.id,
            provider_id="apple_music",
            connection_state="connected",
            credential_type="music_user_token",
            credential_payload="{not-json",
            metadata_json='{"storefront":"us"}',
        )
    )
    db.commit()

    with pytest.raises(apple_music_tokens.AppleMusicNotConnected):
        apple_music_accounts.stored_connection(db, test_user)

    connection = db.query(UserProviderConnection).filter_by(user_id=test_user.id).one()
    assert connection.connection_state == "reauthorize"


def test_disconnect_increments_authorization_generation(db, test_user):
    apple_music_accounts.disconnect_account(db, test_user)

    connection = db.query(UserProviderConnection).filter_by(user_id=test_user.id).one()
    assert connection.connection_state == "disconnected"
    assert connection.authorization_generation == 1


def test_apple_status_codes_separate_deployment_and_user_authorization(monkeypatch):
    class Response:
        def __init__(self, status_code):
            self.status_code = status_code

    class Client:
        def __init__(self, status_code, **_kwargs):
            self.status_code = status_code

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, *_args, **_kwargs):
            return Response(self.status_code)

    monkeypatch.setattr(apple_music_tokens.httpx, "Client", lambda **kw: Client(401, **kw))
    with pytest.raises(apple_music_tokens.AppleMusicNotConfigured):
        apple_music_tokens.apple_get("/me/storefront", "bad-dev", music_user_token=VALID_TOKEN)

    monkeypatch.setattr(apple_music_tokens.httpx, "Client", lambda **kw: Client(403, **kw))
    with pytest.raises(apple_music_tokens.AppleMusicUserAuthorizationError):
        apple_music_tokens.apple_get("/me/storefront", "dev", music_user_token=VALID_TOKEN)


def test_api_status_returns_reconnect_state_for_malformed_stored_data(
    client, auth_headers, db, test_user
):
    db.add(
        UserProviderConnection(
            user_id=test_user.id,
            provider_id="apple_music",
            connection_state="connected",
            credential_type="music_user_token",
            credential_payload="{not-json",
            metadata_json='{"storefront":"us"}',
        )
    )
    db.commit()

    response = client.get("/api/apple-music/status", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {
        "configured": False,
        "linked": False,
        "requires_reauthorization": True,
        "storefront": None,
    }
    assert "not-json" not in response.text


def test_api_link_reports_when_disconnect_supersedes_the_attempt(client, auth_headers, monkeypatch):
    def superseded(*_args):
        raise apple_music_tokens.AppleMusicLinkSuperseded

    monkeypatch.setattr(apple_music_accounts, "link_account", superseded)

    response = client.post(
        "/api/apple-music/link",
        headers=auth_headers,
        json={"music_user_token": VALID_TOKEN},
    )

    assert response.status_code == 409
    assert "canceled" in response.json()["detail"]
    assert VALID_TOKEN not in response.text
