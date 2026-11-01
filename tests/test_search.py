"""Tests for :mod:`tdt_player.search`."""

from __future__ import annotations

from tdt_player.models import Channel, Source
from tdt_player.search import channel_haystack, filter_channels, favourites, fold, groups


def make(name: str, group: str = "", tvg_id: str = "", *urls: str) -> Channel:
    sources = [Source(url=url) for url in urls] or [Source(url="https://example.test/stream")]
    return Channel(name=name, group=group, tvg_id=tvg_id, sources=sources)


def test_fold_removes_accents_and_case():
    assert fold("Canal Sur Andalucía") == "canal sur andalucia"
    assert fold("ANDALUCIA") == fold("Andalucía")


def test_search_ignores_accents_in_both_directions():
    accented = [make("Canal Sur Andalucía", "Locales")]
    assert filter_channels(accented, "andalucia") == accented
    plain = [make("Andalucia TV")]
    assert filter_channels(plain, "Andalucía") == plain


def test_every_token_has_to_match():
    channels = [make("Canal Sur Andalucía"), make("Canal Extremadura")]
    assert [channel.name for channel in filter_channels(channels, "canal sur")] == [
        "Canal Sur Andalucía"
    ]
    assert filter_channels(channels, "canal sur extremadura") == []


def test_group_and_tvg_id_are_searchable():
    channels = [make("La 1", "Generalistas", "La1.TV")]
    assert filter_channels(channels, "generalistas") == channels
    assert filter_channels(channels, "la1.tv") == channels


def test_group_filter_matches_case_and_accents():
    channels = [make("A", "Locales"), make("B", "Locales"), make("C", "Nacional")]
    assert [channel.name for channel in filter_channels(channels, "", "Locales")] == ["A", "B"]
    assert [channel.name for channel in filter_channels(channels, "", "locales")] == ["A", "B"]
    assert filter_channels(channels, "", "Nacionales") == []


def test_group_filter_accepts_any_of_several_groups():
    channel = make("24h", "News;Public")
    assert channel.groups == ["News", "Public"]
    assert channel.group_label == "News, Public"
    assert filter_channels([channel], "", "News") == [channel]
    assert filter_channels([channel], "", "public") == [channel]
    assert filter_channels([channel], "", "Sports") == []


def test_empty_query_returns_everything():
    channels = [make("A"), make("B")]
    assert filter_channels(channels) == channels
    assert filter_channels(channels, "   ") == channels


def test_groups_are_sorted_and_unique():
    channels = [make("A", "Locales"), make("B", "Andalucía"), make("C", "Locales"), make("D")]
    assert groups(channels) == ["Andalucía", "Locales"]


def test_haystack_includes_mirror_variant_names():
    channel = Channel(
        name="24h",
        sources=[Source(url="https://example.test/24", variant_name="24h GEO")],
    )
    assert "geo" in channel_haystack(channel)


def test_playlist_and_favourite_filters():
    one = Channel(name="A", sources=[Source(url="https://example.test/a", playlist="p1")])
    two = Channel(name="B", sources=[Source(url="https://example.test/b", playlist="p2")])
    two.favourite = True

    assert filter_channels([one, two], playlist="p2") == [two]
    assert filter_channels([one, two], favourites_only=True) == [two]
    assert filter_channels([one, two], favourites_only=True, playlist="p1") == []


def test_favourites_helper_keeps_the_stored_order():
    channels = [make("A"), make("B"), make("C")]
    channels[2].favourite = True
    channels[0].favourite = True

    assert [channel.name for channel in favourites(channels)] == ["A", "C"]
