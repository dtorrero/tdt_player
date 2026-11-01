"""Tests for :mod:`tdt_player.refresh`: tagging, pruning, merging, orchestration."""

from __future__ import annotations

from tdt_player import refresh, store
from tdt_player.m3u import ParseResult, PlaylistError
from tdt_player.models import Channel, Playlist, Source


def make(name: str, url: str, group: str = "", tvg_id: str = "", playlist: str = "") -> Channel:
    return Channel(
        name=name,
        group=group,
        tvg_id=tvg_id,
        sources=[Source(url=url, playlist=playlist)],
    )


def parsed(*channels: Channel, warnings=None) -> ParseResult:
    return ParseResult(
        channels=list(channels), warnings=list(warnings or []), entries=len(channels)
    )


def loader(mapping):
    """A fake ``load_m3u`` keyed by location; ``Exception`` values are raised."""

    def load(location: str):
        value = mapping[location]
        if isinstance(value, Exception):
            raise value
        return value

    return load


def library_with(tmp_path, *playlists: Playlist, channels=()) -> store.Library:
    return store.Library(
        path=tmp_path / "config.json",
        playlists=list(playlists),
        channels=list(channels),
    )


def test_refresh_tags_streams_and_records_metadata(tmp_path):
    playlist = Playlist(id="p1", url="mem://list", name="List")
    library = library_with(tmp_path, playlist)
    load = loader({"mem://list": parsed(make("A", "https://example.test/a"))})

    result = refresh.refresh_playlist(library, playlist, loader=load)

    assert result.ok
    assert (result.added, result.updated, result.removed) == (1, 0, 0)
    assert library.channels[0].sources[0].playlist == "p1"
    assert playlist.channel_count == 1
    assert playlist.last_refresh
    assert playlist.last_error == ""


def test_refresh_keeps_the_channel_object_its_star_and_its_name(tmp_path):
    playlist = Playlist(id="p1", url="mem://list")
    existing = make("A", "https://example.test/a", tvg_id="A.TV", playlist="p1")
    existing.favourite = True
    existing.name = "Renamed by the user"
    library = library_with(tmp_path, playlist, channels=[existing])
    load = loader(
        {"mem://list": parsed(make("A", "https://example.test/a2", tvg_id="A.TV"))}
    )

    result = refresh.refresh_playlist(library, playlist, loader=load)

    assert library.channels == [existing]
    assert existing.favourite is True
    assert existing.name == "Renamed by the user"
    assert [source.url for source in existing.sources] == ["https://example.test/a2"]
    assert (result.added, result.updated, result.removed) == (0, 1, 0)


def test_refresh_removes_stations_that_left_the_feed(tmp_path):
    playlist = Playlist(id="p1", url="mem://list")
    library = library_with(tmp_path, playlist)
    first = loader(
        {
            "mem://list": parsed(
                make("A", "https://example.test/a", tvg_id="A.TV"),
                make("B", "https://example.test/b", tvg_id="B.TV"),
            )
        }
    )
    refresh.refresh_playlist(library, playlist, loader=first)
    survivor = library.channels[0]

    second = loader({"mem://list": parsed(make("A", "https://example.test/a", tvg_id="A.TV"))})
    result = refresh.refresh_playlist(library, playlist, loader=second)

    assert result.removed == 1
    assert [channel.name for channel in library.channels] == ["A"]
    assert library.channels[0].id == survivor.id  # the same object survived
    assert playlist.channel_count == 1


def test_a_station_shared_with_another_playlist_survives(tmp_path):
    one = Playlist(id="p1", url="mem://one")
    two = Playlist(id="p2", url="mem://two")
    shared = make("Shared", "https://example.test/x", tvg_id="X.TV", playlist="p2")
    library = library_with(tmp_path, one, two, channels=[shared])
    load = loader({"mem://one": parsed(make("Other", "https://example.test/o"))})

    result = refresh.refresh_playlist(library, one, loader=load)

    assert result.removed == 0
    assert shared in library.channels
    assert [source.playlist for source in shared.sources] == ["p2"]


def test_a_channel_added_by_hand_is_never_pruned(tmp_path):
    playlist = Playlist(id="p1", url="mem://list")
    manual = make("Mine", "https://example.test/mine")
    library = library_with(tmp_path, playlist, channels=[manual])

    result = refresh.refresh_playlist(
        library,
        playlist,
        loader=loader({"mem://list": parsed(make("A", "https://example.test/a"))}),
    )

    assert result.removed == 0
    assert manual in library.channels
    assert [channel.name for channel in library.channels] == ["Mine", "A"]


def test_a_failing_playlist_keeps_the_channels_and_records_the_error(tmp_path):
    playlist = Playlist(id="p1", url="mem://list", last_refresh="2020-01-01T00:00:00")
    existing = make("A", "https://example.test/a", playlist="p1")
    library = library_with(tmp_path, playlist, channels=[existing])
    load = loader({"mem://list": PlaylistError("HTTP 503 Service Unavailable")})

    result = refresh.refresh_playlist(library, playlist, loader=load)

    assert result.ok is False
    assert "503" in result.error
    assert library.channels == [existing]
    assert playlist.last_error.startswith("HTTP 503")
    assert playlist.last_refresh == "2020-01-01T00:00:00"  # untouched


def test_an_empty_playlist_is_an_error_and_does_not_prune(tmp_path):
    playlist = Playlist(id="p1", url="mem://list")
    existing = make("A", "https://example.test/a", playlist="p1")
    library = library_with(tmp_path, playlist, channels=[existing])

    result = refresh.refresh_playlist(
        library, playlist, loader=loader({"mem://list": parsed()})
    )

    assert result.ok is False
    assert "no playable channel" in result.error
    assert library.channels == [existing]


def test_an_unexpected_loader_error_is_captured(tmp_path):
    playlist = Playlist(id="p1", url="mem://list")
    library = library_with(tmp_path, playlist)

    result = refresh.refresh_playlist(
        library, playlist, loader=loader({"mem://list": ValueError("boom")})
    )

    assert result.ok is False
    assert "boom" in result.error


def test_refresh_all_continues_after_a_failure(tmp_path):
    good = Playlist(id="p1", url="mem://good", name="Good")
    bad = Playlist(id="p2", url="mem://bad", name="Bad")
    library = library_with(tmp_path, good, bad)
    load = loader(
        {
            "mem://good": parsed(make("A", "https://example.test/a")),
            "mem://bad": PlaylistError("no route to host"),
        }
    )

    results = refresh.refresh_all(library, loader=load)

    assert [result.ok for result in results] == [True, False]
    assert [channel.name for channel in library.channels] == ["A"]
    summary = refresh.summarise(results)
    assert "1 added" in summary and "1 failed" in summary
    assert bad.last_error == "no route to host"


def test_refresh_all_can_target_one_playlist(tmp_path):
    one = Playlist(id="p1", url="mem://one")
    two = Playlist(id="p2", url="mem://two")
    library = library_with(tmp_path, one, two)
    load = loader(
        {
            "mem://one": parsed(make("A", "https://example.test/a")),
            "mem://two": parsed(make("B", "https://example.test/b")),
        }
    )

    results = refresh.refresh_all(library, loader=load, only=["p2"])

    assert [result.playlist.id for result in results] == ["p2"]
    assert [channel.name for channel in library.channels] == ["B"]


def test_refresh_all_skips_disabled_playlists(tmp_path):
    enabled = Playlist(id="p1", url="mem://one", enabled=True)
    disabled = Playlist(id="p2", url="mem://two", enabled=False)
    library = library_with(tmp_path, enabled, disabled)

    assert refresh.refresh_targets(library) == [enabled]
    assert refresh.refresh_targets(library, include_disabled=True) == [enabled, disabled]
    assert refresh.refresh_targets(library, only=["p2"]) == [disabled]


def test_replace_drops_everything_before_merging(tmp_path):
    playlist = Playlist(id="p1", url="mem://list")
    old = make("Old", "https://example.test/old")
    library = library_with(tmp_path, playlist, channels=[old])

    result = refresh.refresh_playlist(
        library,
        playlist,
        loader=loader({"mem://list": parsed(make("New", "https://example.test/new"))}),
        replace=True,
    )

    assert [channel.name for channel in library.channels] == ["New"]
    assert result.removed == 0  # replaced channels are not counted as pruned


def test_progress_callback_runs_once_per_playlist(tmp_path):
    one = Playlist(id="p1", url="mem://one", name="One")
    two = Playlist(id="p2", url="mem://two", name="Two")
    library = library_with(tmp_path, one, two)
    load = loader(
        {
            "mem://one": parsed(make("A", "https://example.test/a")),
            "mem://two": parsed(make("B", "https://example.test/b")),
        }
    )
    seen = []

    refresh.refresh_all(library, loader=load, progress=lambda i, t, p: seen.append((i, t, p.name)))

    assert seen == [(1, 2, "One"), (2, 2, "Two")]


def test_summarise_without_results():
    assert refresh.summarise([]) == "No playlists to refresh"
