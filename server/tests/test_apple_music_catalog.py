"""Apple Music catalog lookup and DJ storefront resolution tests."""

import json

from app.models.provider_connection import UserProviderConnection
from app.services import apple_music_catalog, apple_music_tokens
from app.services.setbuilder.export_common import ExportTrack

VALID_TOKEN = "music-user-token-1234567890"


def _track(*, isrc: str | None = None) -> ExportTrack:
    return ExportTrack(
        position=2,
        title="Night Drive",
        artist="The Example",
        album=None,
        isrc=isrc,
        duration_sec=None,
    )


def _connect_dj(db, user):
    db.add(
        UserProviderConnection(
            user_id=user.id,
            provider_id="apple_music",
            connection_state="connected",
            credential_type="music_user_token",
            credential_payload=json.dumps({"version": 1, "music_user_token": VALID_TOKEN}),
            metadata_json='{"storefront":"gb"}',
        )
    )
    db.commit()


def test_resolution_tries_exact_isrc_then_fuzzy_title_artist(db, test_user, monkeypatch):
    _connect_dj(db, test_user)
    queries = []

    def search(_db, storefront, term, *, isrc=None):
        queries.append((storefront, term, isrc))
        if isrc:
            return []
        return [
            apple_music_catalog.AppleCatalogSong(
                id="song-17", title="Night Drive", artist="The Example", isrc=None
            )
        ]

    monkeypatch.setattr(apple_music_catalog, "search_catalog", search)

    resolved, unresolved = apple_music_catalog.resolve_tracks(
        db, test_user, [_track(isrc="USRC17607839")]
    )

    assert [(track.position, song_id) for track, song_id in resolved] == [(2, "song-17")]
    assert unresolved == []
    assert queries == [
        ("gb", "", "USRC17607839"),
        ("gb", "The Example Night Drive", None),
    ]


def test_resolution_accepts_only_exact_isrc_before_fuzzy_fallback(db, test_user, monkeypatch):
    _connect_dj(db, test_user)
    queries = []

    def search(_db, storefront, term, *, isrc=None):
        queries.append((storefront, term, isrc))
        if isrc:
            return [
                apple_music_catalog.AppleCatalogSong(
                    id="wrong-song", title="Other Track", artist="Different Artist", isrc="other"
                ),
                apple_music_catalog.AppleCatalogSong(
                    id="exact-song",
                    title="Other Track",
                    artist="Different Artist",
                    isrc="usrc17607839",
                ),
            ]
        return []

    monkeypatch.setattr(apple_music_catalog, "search_catalog", search)

    resolved, unresolved = apple_music_catalog.resolve_tracks(
        db, test_user, [_track(isrc="USRC17607839")]
    )

    assert [(track.position, song_id) for track, song_id in resolved] == [(2, "exact-song")]
    assert unresolved == []
    assert queries == [("gb", "", "USRC17607839")]


def test_resolution_rejects_unwanted_versions_and_preserves_unresolved_tracks(
    db, test_user, monkeypatch
):
    _connect_dj(db, test_user)
    monkeypatch.setattr(
        apple_music_catalog,
        "search_catalog",
        lambda *_args, **_kwargs: [
            apple_music_catalog.AppleCatalogSong(
                id="live-song", title="Night Drive (Live)", artist="The Example", isrc=None
            )
        ],
    )

    resolved, unresolved = apple_music_catalog.resolve_tracks(db, test_user, [_track()])

    assert resolved == []
    assert [track.position for track in unresolved] == [2]


def test_search_uses_developer_token_and_exact_storefront_filters(monkeypatch):
    monkeypatch.setattr(apple_music_tokens, "create_developer_token", lambda: ("dev-token", 100))
    requests = []

    def apple_get(path, developer_token, *, params=None, **_kwargs):
        requests.append((path, developer_token, params))
        return {
            "data": [
                {
                    "id": "song-17",
                    "attributes": {
                        "name": "Night Drive",
                        "artistName": "The Example",
                        "isrc": "USRC17607839",
                    },
                }
            ]
        }

    monkeypatch.setattr(apple_music_tokens, "apple_get", apple_get)

    songs = apple_music_catalog.search_catalog(None, "gb", "", isrc="USRC17607839")

    assert songs == [
        apple_music_catalog.AppleCatalogSong(
            id="song-17", title="Night Drive", artist="The Example", isrc="USRC17607839"
        )
    ]
    assert requests == [
        ("/catalog/gb/songs", "dev-token", {"filter[isrc]": "USRC17607839", "limit": "25"})
    ]


def test_title_search_sends_title_artist_term_to_the_dj_storefront(monkeypatch):
    monkeypatch.setattr(apple_music_tokens, "create_developer_token", lambda: ("dev-token", 100))
    requests = []

    def apple_get(path, developer_token, *, params=None, **_kwargs):
        requests.append((path, developer_token, params))
        return {"results": {"songs": {"data": []}}}

    monkeypatch.setattr(apple_music_tokens, "apple_get", apple_get)

    assert apple_music_catalog.search_catalog(None, "gb", "The Example Night Drive") == []

    assert requests == [
        (
            "/catalog/gb/search",
            "dev-token",
            {"term": "The Example Night Drive", "types": "songs", "limit": "25"},
        )
    ]
