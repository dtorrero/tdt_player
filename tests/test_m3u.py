"""Tests for :mod:`tdt_player.m3u`: parsing, grouping, decoding and fetching."""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from tdt_player.m3u import (
    PlaylistError,
    decode_text,
    fetch_text,
    is_playable_url,
    load_m3u,
    parse_m3u,
)


def by_name(result, name: str):
    for channel in result.channels:
        if channel.name == name:
            return channel
    raise AssertionError(f"{name!r} not in {[c.name for c in result.channels]}")


@pytest.fixture()
def result(sample_text: str):
    return parse_m3u(sample_text)


def test_counts(result):
    assert result.entries == 10
    assert len(result.channels) == 7
    assert result.sources == 8


def test_mirrors_are_folded_into_one_channel(result):
    channel = by_name(result, "La 1")
    assert [source.url for source in channel.sources] == [
        "https://example.test/la1/main.m3u8",
        "https://example.test/la1/backup.m3u8",
    ]


def test_referrer_and_user_agent_follow_their_entry(result):
    source = by_name(result, "24h").sources[0]
    # The referrer value itself contains "=" pairs: only the first one splits.
    assert source.referrer == "https://player.example.test/?id=7&auto=1"
    assert source.user_agent == "Mozilla/5.0 (X11; Linux x86_64) TestAgent/1.0"


def test_only_the_preceding_entry_gets_the_options(result):
    assert by_name(result, "La 1").sources[0].referrer == ""
    assert by_name(result, "Canal Local, S.A.").sources[0].referrer == ""


def test_display_name_may_contain_commas(result):
    assert by_name(result, "Canal Local, S.A.").group == "Locales"


def test_tvg_name_wins_over_the_display_name():
    text = (
        "#EXTM3U\n"
        '#EXTINF:-1 tvg-name="3CatInfo",3CatInfo GEO CAT\n'
        "https://example.test/cat.m3u8\n"
    )
    channel = parse_m3u(text).channels[0]
    assert channel.name == "3CatInfo"
    assert channel.sources[0].variant_name == "3CatInfo GEO CAT"


def test_attributes_are_kept(result):
    channel = by_name(result, "La 1")
    assert channel.tvg_id == "La1.TV"
    assert channel.logo == "https://example.test/la1.png"
    assert channel.group == "Generalistas"


def test_missing_attributes_leave_empty_fields(result):
    channel = by_name(result, "Sin atributos")
    assert channel.group == "Sin logo"
    assert channel.tvg_id == ""
    assert channel.logo == ""


def test_entry_without_url_is_skipped_and_reported(result):
    assert "Sin URL" not in [channel.name for channel in result.channels]
    assert any("Sin URL" in warning for warning in result.warnings)


def test_unplayable_url_is_skipped_and_reported(result):
    assert "Basura" not in [channel.name for channel in result.channels]
    assert any("magnet:" in warning for warning in result.warnings)


def test_youtube_and_rtmp_urls_are_kept(result):
    assert by_name(result, "YouTube").url == "https://www.youtube.com/watch?v=abc123"
    assert by_name(result, "RTMP").url == "rtmp://example.test/live/stream"


def test_extgrp_supplies_the_group_when_group_title_is_missing():
    text = (
        "#EXTM3U\n"
        '#EXTINF:-1 tvg-name="X",X\n'
        "#EXTGRP:Autonómicas\n"
        "https://example.test/x.m3u8\n"
    )
    channel = parse_m3u(text).channels[0]
    assert channel.group == "Autonómicas"


def test_crlf_and_bom_are_tolerated():
    text = (
        "\ufeff#EXTM3U\r\n"
        '#EXTINF:-1 tvg-name="Uno",Uno\r\n'
        "https://example.test/uno.m3u8\r\n"
    )
    result = parse_m3u(text)
    assert [channel.name for channel in result.channels] == ["Uno"]


def test_the_same_url_is_only_kept_once():
    text = (
        "#EXTM3U\n"
        '#EXTINF:-1 tvg-id="Same" tvg-name="Uno",Uno\n'
        "https://example.test/uno.m3u8\n"
        '#EXTINF:-1 tvg-id="Same" tvg-name="Uno",Uno\n'
        "https://example.test/uno.m3u8\n"
    )
    result = parse_m3u(text)
    assert len(result.channels) == 1
    assert len(result.channels[0].sources) == 1


def test_unquoted_attribute_values_are_accepted():
    text = (
        "#EXTM3U\n"
        "#EXTINF:-1 tvg-id=abc tvg-name=Uno,Uno\n"
        "https://example.test/uno.m3u8\n"
    )
    channel = parse_m3u(text).channels[0]
    assert channel.name == "Uno"
    assert channel.tvg_id == "abc"


def test_lines_before_the_first_extinf_are_ignored():
    text = (
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        "https://example.test/orphan.m3u8\n"
        '#EXTINF:-1 tvg-name="Uno",Uno\n'
        "https://example.test/uno.m3u8\n"
    )
    result = parse_m3u(text)
    assert [channel.name for channel in result.channels] == ["Uno"]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.test/a.m3u8", True),
        ("HTTP://example.test/a.m3u8", True),
        ("rtmp://example.test/live", True),
        ("udp://@239.0.0.1:1234", True),
        ("file:///tmp/a.m3u8", True),
        ("magnet:?xt=urn:btih:0", False),
        ("", False),
        ("just a string", False),
    ],
)
def test_is_playable_url(url, expected):
    assert is_playable_url(url) is expected


def test_decode_text_unwraps_gzip():
    assert decode_text(gzip.compress(b'#EXTM3U\n')) == "#EXTM3U\n"


def test_decode_text_survives_latin1():
    assert decode_text("Andalucía".encode("latin-1")) == "Andalucía"


def test_load_m3u_reads_a_local_file(sample_playlist: Path):
    result = load_m3u(str(sample_playlist))
    assert len(result.channels) == 7


def test_load_m3u_accepts_a_file_uri(sample_playlist: Path):
    result = load_m3u(sample_playlist.as_uri())
    assert len(result.channels) == 7


def test_missing_file_raises_playlist_error(tmp_path: Path):
    with pytest.raises(PlaylistError) as excinfo:
        load_m3u(str(tmp_path / "nope.m3u8"))
    assert "nope.m3u8" in str(excinfo.value)


def test_unsupported_scheme_raises_playlist_error():
    with pytest.raises(PlaylistError):
        fetch_text("ftp://example.test/tv.m3u8")


def test_http_errors_are_wrapped(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("no route to host")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(PlaylistError) as excinfo:
        fetch_text("https://example.test/tv.m3u8")
    assert "no route to host" in str(excinfo.value)


def test_headers_in_the_extinf_line_are_read():
    text = (
        "#EXTM3U\n"
        '#EXTINF:-1 tvg-name="12tv" http-referrer="https://player.example.test/?a=1&b=2" '
        'http-user-agent="Agent/2.0" group-title="General",12tv (720p)\n'
        "https://example.test/12tv.m3u8\n"
    )
    source = parse_m3u(text).channels[0].sources[0]
    assert source.referrer == "https://player.example.test/?a=1&b=2"
    assert source.user_agent == "Agent/2.0"


def test_extvlcopt_wins_over_the_extinf_attribute():
    text = (
        "#EXTM3U\n"
        '#EXTINF:-1 tvg-name="X" http-referrer="https://wrong.test/",X\n'
        "#EXTVLCOPT:http-referrer=https://right.test/\n"
        "https://example.test/x.m3u8\n"
    )
    assert parse_m3u(text).channels[0].sources[0].referrer == "https://right.test/"


def test_multiple_groups_are_normalised():
    text = (
        "#EXTM3U\n"
        '#EXTINF:-1 tvg-name="24h" group-title="News; Public ;News",24h\n'
        "https://example.test/24.m3u8\n"
    )
    channel = parse_m3u(text).channels[0]
    assert channel.group == "News; Public"
    assert channel.groups == ["News", "Public"]
    assert channel.group_label == "News, Public"


def test_ids_with_at_signs_are_kept():
    text = (
        "#EXTM3U\n"
        '#EXTINF:-1 tvg-id="3CatInfo.es@SD" tvg-name="3CatInfo",3CatInfo\n'
        "https://example.test/cat.m3u8\n"
    )
    assert parse_m3u(text).channels[0].tvg_id == "3CatInfo.es@SD"


def test_different_ids_with_the_same_name_stay_separate():
    text = (
        "#EXTM3U\n"
        '#EXTINF:-1 tvg-id="La1.TV" tvg-name="La 1",La 1\n'
        "https://example.test/tdt.m3u8\n"
        '#EXTINF:-1 tvg-id="La1.es@SD" tvg-name="La 1",La 1\n'
        "https://example.test/iptv.m3u8\n"
    )
    assert len(parse_m3u(text).channels) == 2
