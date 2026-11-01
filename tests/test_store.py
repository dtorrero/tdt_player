"""Tests for :mod:`tdt_player.store`: atomic saves, migration, merging, playlists."""

from __future__ import annotations

import json

from tdt_player import store
from tdt_player.models import Channel, Playlist, Source


def channel(name: str, url: str, group: str = "", tvg_id: str = "", playlist: str = "") -> Channel:
    return Channel(
        name=name,
        group=group,
        tvg_id=tvg_id,
        sources=[Source(url=url, playlist=playlist)],
    )


def test_missing_file_gives_an_empty_library(tmp_path):
    library = store.load(tmp_path / "config.json")
    assert library.channels == []
    assert library.warnings == []
    assert [playlist.id for playlist in library.playlists] == ["tdtchannels"]
    assert library.playlists[0].url == store.DEFAULT_LIST_URL
    assert library.settings["mpv_path"] == "mpv"
    assert "list_url" not in library.settings


def test_round_trip_keeps_settings_playlists_and_favourites(tmp_path):
    library = store.Library(path=tmp_path / "config.json")
    library.settings["last_filter"] = "sur"
    library.playlists = [
        Playlist(id="p1", name="Uno", url="https://example.test/a.m3u8", enabled=False)
    ]
    library.channels = [
        channel("La 1", "https://example.test/la1", "Generalistas", "La1.TV", "p1")
    ]
    library.channels[0].favourite = True

    path = library.save()
    assert path == tmp_path / "config.json"
    assert list(tmp_path.glob("*.tmp")) == []

    again = store.load(path)
    assert again.warnings == []
    assert [item.name for item in again.channels] == ["La 1"]
    assert again.channels[0].favourite is True
    assert again.channels[0].sources[0].playlist == "p1"
    assert [playlist.name for playlist in again.playlists] == ["Uno"]
    assert again.playlists[0].enabled is False
    assert again.settings["last_filter"] == "sur"


def test_version_1_is_migrated_not_discarded(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "settings": {
                    "list_url": "https://example.test/old.m3u8",
                    "mpv_args": ["--mute=yes"],
                },
                "channels": [
                    {
                        "name": "Old",
                        "group": "Locales",
                        "sources": [{"url": "https://example.test/old"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    library = store.load(path)

    assert path.exists()  # migrated in place
    assert not (tmp_path / "config.json.bak").exists()
    assert [playlist.url for playlist in library.playlists] == [
        "https://example.test/old.m3u8"
    ]
    assert [item.name for item in library.channels] == ["Old"]
    assert library.channels[0].sources[0].playlist == ""  # origin unknown
    assert library.settings["mpv_args"] == ["--mute=yes"]
    assert "upgraded from version 1" in library.warnings[0]


def test_version_1_without_a_list_url_gets_the_default_playlist(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": 1, "settings": {}, "channels": []}), encoding="utf-8")

    library = store.load(path)

    assert library.playlists[0].url == store.DEFAULT_LIST_URL
    assert library.playlists[0].name == "TDTChannels"


def test_corrupt_file_is_moved_aside(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")

    library = store.load(path)

    assert library.channels == []
    assert library.warnings
    assert not path.exists()
    assert (tmp_path / "config.json.bak").read_text(encoding="utf-8") == "{not json"


def test_unknown_version_is_moved_aside(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": 99, "channels": []}), encoding="utf-8")

    library = store.load(path)

    assert library.channels == []
    assert "another version" in library.warnings[0]


def test_config_path_honours_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("TDT_PLAYER_CONFIG", str(tmp_path / "custom.json"))
    assert store.config_path() == tmp_path / "custom.json"

    monkeypatch.delenv("TDT_PLAYER_CONFIG")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert store.config_path() == tmp_path / "xdg" / "tdt_player" / "config.json"


def test_merge_adds_new_channels_and_updates_mirrors():
    stored = [
        channel("Mine", "https://example.test/mine"),
        channel("La 1", "https://example.test/la1", playlist="p1"),
    ]
    incoming = [
        channel("La 1", "https://example.test/la1", playlist="p1"),  # already known
        channel("La 1", "https://example.test/la1-backup", playlist="p1"),  # new mirror
        channel("La 1", "https://example.test/la1-backup", playlist="p1"),  # repeated
        channel("New", "https://example.test/new", playlist="p1"),
    ]

    merged, added, updated = store.merge_channels(stored, incoming)

    names = [item.name for item in merged]
    assert names[0] == "Mine"  # user channels survive a refresh
    assert names.count("La 1") == 1
    assert "New" in names
    assert (added, updated) == (1, 1)
    assert [source.url for source in merged[1].sources] == [
        "https://example.test/la1",
        "https://example.test/la1-backup",
    ]


def test_merge_adopts_an_untagged_stream_instead_of_duplicating_it():
    stored = [channel("La 1", "https://example.test/la1")]  # imported before v0.2
    incoming = [channel("La 1", "https://example.test/la1", playlist="p1")]

    merged, added, updated = store.merge_channels(stored, incoming)

    assert (added, updated) == (0, 1)
    assert len(merged[0].sources) == 1
    assert merged[0].sources[0].playlist == "p1"


def test_merge_is_idempotent():
    incoming = [
        channel("La 1", "https://example.test/la1", playlist="p1"),
        channel("La 1", "https://example.test/la1-backup", playlist="p1"),
    ]
    merged, added, updated = store.merge_channels([], incoming)
    assert (added, updated) == (1, 1)

    merged, added_again, updated_again = store.merge_channels(merged, incoming)
    assert (added_again, updated_again) == (0, 0)
    assert len(merged) == 1
    assert len(merged[0].sources) == 2


def test_merge_keeps_a_renamed_channel_and_refreshes_its_logo():
    stored = [channel("Mi La 1", "https://example.test/la1", "Generalistas", "La1.TV")]
    incoming = [channel("La 1", "https://example.test/la1", "Generalistas", "La1.TV")]
    incoming[0].logo = "https://example.test/la1.png"

    merged, added, updated = store.merge_channels(stored, incoming)

    assert (added, updated) == (0, 1)
    assert merged[0].name == "Mi La 1"
    assert merged[0].logo == "https://example.test/la1.png"


def test_the_same_stream_from_two_playlists_is_kept_twice_but_played_once():
    existing = [channel("A", "https://example.test/a", playlist="p1")]
    incoming = [channel("A", "https://example.test/a", playlist="p2")]

    merged, added, updated = store.merge_channels(existing, incoming)

    assert (added, updated) == (0, 1)
    assert len(merged[0].sources) == 2
    assert merged[0].playlists == ["p1", "p2"]
    assert len(merged[0].playable_sources()) == 1


def test_channel_without_sources_is_not_loaded(tmp_path):
    path = tmp_path / "config.json"
    payload = {
        "version": store.CONFIG_VERSION,
        "settings": {},
        "playlists": [],
        "channels": [
            {"name": "Empty"},
            {"name": "Kept", "sources": [{"url": "https://example.test/kept"}]},
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    library = store.load(path)

    assert [item.name for item in library.channels] == ["Kept"]
    assert library.playlists == []  # an explicitly empty list stays empty


def test_library_add_find_and_remove(tmp_path):
    library = store.Library(path=tmp_path / "config.json")
    first = library.add(channel("A", "https://example.test/a"))
    second = library.add(channel("B", "https://example.test/b"))

    assert library.find(second.id) is second
    assert library.remove(first.id) is True
    assert library.remove("missing") is False
    assert [item.name for item in library.channels] == ["B"]
    assert library.find(first.id) is None


def test_save_creates_the_directory(tmp_path):
    target = tmp_path / "nested" / "deeper" / "config.json"
    library = store.Library(path=target, channels=[channel("A", "https://example.test/a")])
    assert library.save() == target
    assert target.is_file()


def test_add_playlist_refuses_duplicates_by_url():
    library = store.Library()
    first = library.add_playlist(Playlist(url="https://example.test/a.m3u8", name="A"))
    again = library.add_playlist(Playlist(url="https://example.test/a.m3u8", name="Other"))

    assert again.id == first.id
    assert len(library.playlists) == 2  # the default TDTChannels plus A
    assert library.playlist(first.id).name == "A"


def test_add_playlist_keeps_ids_unique():
    library = store.Library()
    first = library.add_playlist(
        Playlist(id="tdtchannels", url="https://example.test/a.m3u8")
    )
    assert first.id != "tdtchannels"
    assert len({playlist.id for playlist in library.playlists}) == len(library.playlists)


def test_remove_playlist_can_drop_or_keep_its_channels():
    library = store.Library()
    playlist = library.add_playlist(Playlist(url="https://example.test/a.m3u8"))
    shared = library.add(channel("Shared", "https://example.test/x", playlist=playlist.id))
    shared.add_source(Source(url="https://example.test/x", playlist="other"))
    library.add(channel("Only", "https://example.test/y", playlist=playlist.id))
    library.add(channel("Manual", "https://example.test/z"))

    removed = library.remove_playlist(playlist.id)

    assert removed == 1  # "Only" is gone, "Shared" still has its other stream
    assert [item.name for item in library.channels] == ["Shared", "Manual"]
    assert [source.playlist for source in shared.sources] == ["other"]
    assert shared.playlists == ["other"]


def test_remove_playlist_can_keep_every_channel():
    library = store.Library()
    playlist = library.add_playlist(Playlist(url="https://example.test/a.m3u8"))
    library.add(channel("Only", "https://example.test/y", playlist=playlist.id))

    assert library.remove_playlist(playlist.id, keep_channels=True) == 0
    assert [item.name for item in library.channels] == ["Only"]
    assert library.playlist(playlist.id) is None


def test_playlist_lookup_and_enabling():
    library = store.Library()
    playlist = library.add_playlist(
        Playlist(
            url="https://iptv-org.github.io/iptv/countries/es.m3u",
            name="IPTV-org Spain",
        )
    )

    assert library.playlist_by_name("iptv-org") is playlist
    assert library.playlist_by_name(playlist.id) is playlist
    assert library.playlist_by_name("es.m3u") is playlist
    assert library.playlist_by_name("nothing") is None

    assert library.set_playlist_enabled(playlist.id, False) is True
    assert playlist.enabled is False
    assert library.set_playlist_enabled("missing", True) is False


def test_channel_count_of_a_playlist():
    library = store.Library()
    one = library.add_playlist(Playlist(url="https://example.test/one.m3u8"))
    library.add(channel("A", "https://example.test/a", playlist=one.id))
    library.add(channel("B", "https://example.test/b", playlist=one.id))
    library.add(channel("C", "https://example.test/c"))

    assert len(library.channels_of_playlist(one.id)) == 2


def test_presets_cover_the_recommended_playlists():
    presets = store.preset_playlists()
    assert set(presets) == {"tdt", "tdt-m3u", "iptv-org-es"}
    assert presets["tdt"][1] == store.DEFAULT_LIST_URL
    assert presets["iptv-org-es"][1].endswith("/countries/es.m3u")
