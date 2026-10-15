"""Stored Apple Music credentials fail closed when encrypted data is incompatible."""

import pytest

from app.models.provider_connection import UserProviderConnection
from app.services.apple_music_storage import read_stored_connection
from app.services.apple_music_tokens import AppleMusicNotConnected

VALID_TOKEN = "music-user-token-1234567890"


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
def test_malformed_stored_credentials_mark_connection_for_reauthorization(
    db, test_user, credential_payload, metadata_json
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

    with pytest.raises(AppleMusicNotConnected):
        read_stored_connection(db, test_user)

    db.refresh(connection)
    assert connection.connection_state == "reauthorize"


def test_valid_stored_connection_returns_token_and_storefront(db, test_user):
    import json

    connection = UserProviderConnection(
        user_id=test_user.id,
        provider_id="apple_music",
        connection_state="connected",
        credential_type="music_user_token",
        credential_payload=json.dumps({"version": 1, "music_user_token": VALID_TOKEN}),
        metadata_json='{"storefront":"gb"}',
    )
    db.add(connection)
    db.commit()

    assert read_stored_connection(db, test_user) == (VALID_TOKEN, "gb")
