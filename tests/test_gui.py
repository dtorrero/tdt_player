"""Tests for the tkinter front-end (:mod:`tdt_player.gui`).

The window is real but withdrawn: no user sees it, the player is a fake that only
records calls, and the playlist loader is injected, so nothing here touches the
network, mpv or a file outside ``tmp_path``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

gui = pytest.importorskip("tdt_player.gui")

from tdt_player import store  # noqa: E402
from tdt_player.m3u import ParseResult, PlaylistError  # noqa: E402
from tdt_player.models import Channel, Playlist, Source  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"

pytestmark = pytest.mark.skipif(
    not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")),
    reason="needs a display",
)


class FakePlayer:
    """Records what the window asks for and replays scripted events."""

    def __init__(self) -> None:
        self.calls = []
        self.events = []

    def play(self, channel, *, start_index=0):
        self.calls.append((channel.name, start_index))
        return True

    def stop(self):
        self.calls.append(("stop",))

    def detach(self):
        self.calls.append(("detach",))

    def drain(self):
        events, self.events = self.events, []
        return events

    def wait(self, timeout=None):
        return True


def make_channel(name: str, group: str = "", url: str = "", playlist: str = "") -> Channel:
    return Channel(
        name=name,
        group=group,
        sources=[Source(url=url or f"https://example.test/{name}", playlist=playlist)],
    )


def wait_for(app, predicate, timeout: float = 5.0) -> bool:
    """Pump the Tk loop (and the worker queue) until ``predicate`` is true."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.update()
        app._drain_worker_events()
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture()
def library(tmp_path):
    one = Playlist(id="p1", name="TDTChannels", url="https://example.test/tv.m3u8")
    two = Playlist(id="p2", name="IPTV-org Spain", url="https://example.test/es.m3u")
    channels = [
        Channel(
            name="La 1",
            group="Generalistas",
            tvg_id="La1.TV",
            sources=[Source(url="https://example.test/la1", playlist="p1")],
        ),
        Channel(
            name="Canal Sur Andalucía",
            group="Locales",
            sources=[Source(url="https://example.test/sur", playlist="p1")],
        ),
        Channel(
            name="24h",
            group="Informativos",
            sources=[
                Source(url="https://example.test/24", playlist="p1"),
                Source(url="https://example.test/24b", playlist="p2"),
            ],
        ),
        Channel(
            name="3CatInfo",
            group="News;Public",
            sources=[Source(url="https://example.test/cat", playlist="p2")],
        ),
    ]
    library = store.Library(path=tmp_path / "config.json", playlists=[one, two], channels=channels)
    library.save()
    return library


@pytest.fixture()
def fake_player():
    return FakePlayer()


@pytest.fixture()
def app(library, fake_player):
    window = gui.App(library=library, player=fake_player)
    window.withdraw()
    window.update()
    yield window
    try:
        window.destroy()
    except Exception:  # pragma: no cover - already destroyed by a test
        pass


def test_rows_and_columns(app):
    rows = app.tree.get_children()
    assert len(rows) == 4
    first = rows[0]
    assert app.tree.set(first, "star") == ""
    assert app.tree.set(first, "name") == "La 1"
    assert app.tree.set(first, "group") == "Generalistas"
    assert app.tree.set(first, "playlist") == "TDTChannels"


def test_channel_rows_are_numbered_and_zebra_striped(app):
    rows = app.tree.get_children()
    # Channel numbers follow the library order, like a printed TV guide.
    assert [app.tree.set(row, "num") for row in rows] == ["1", "2", "3", "4"]
    # Adjacent rows carry different background tags (alternating bands).
    first_tags = app.tree.item(rows[0], "tags")
    second_tags = app.tree.item(rows[1], "tags")
    assert first_tags and second_tags
    assert first_tags != second_tags


def test_playing_channel_is_highlighted_until_it_stops(app):
    channel = app.library.channels[2]  # 24h, channel number 3
    app._select(channel.id)
    app.play_channel(channel)
    assert app.tree.item(channel.id, "tags") == (gui.PLAYING_TAG,)
    assert app.tree.set(channel.id, "num").startswith("▶")

    app._stop_player()
    assert gui.PLAYING_TAG not in app.tree.item(channel.id, "tags")
    assert app.tree.set(channel.id, "num") == "3"


def test_playlist_column_lists_every_source(app):
    rows = {app.tree.set(row, "name"): row for row in app.tree.get_children()}
    assert app.tree.set(rows["24h"], "playlist") == "TDTChannels, IPTV-org Spain"
    assert app.tree.set(rows["3CatInfo"], "group") == "News, Public"


def test_filters_are_filled(app):
    groups = [str(value) for value in app.group_box.cget("values")]
    playlists = [str(value) for value in app.playlist_box.cget("values")]
    assert groups[0] == gui.ALL_GROUPS
    assert {"Generalistas", "Locales", "News", "Public"} <= set(groups)
    assert playlists[0] == gui.ALL_PLAYLISTS
    assert {"TDTChannels", "IPTV-org Spain"} <= set(playlists)


def test_search_filters_rows(app):
    app.search_var.set("canal")
    app._apply_filter()
    rows = app.tree.get_children()
    assert len(rows) == 1
    assert app.tree.set(rows[0], "name") == "Canal Sur Andalucía"


def test_search_ignores_accents(app):
    app.search_var.set("andalucia")
    app._apply_filter()
    assert len(app.tree.get_children()) == 1


def test_group_filter_matches_one_of_several_groups(app):
    app.group_var.set("News")
    app._apply_filter()
    rows = app.tree.get_children()
    assert [app.tree.set(row, "name") for row in rows] == ["3CatInfo"]


def test_playlist_filter_limits_the_table(app):
    app.playlist_var.set("IPTV-org Spain")
    app._apply_filter()
    rows = app.tree.get_children()
    assert {app.tree.set(row, "name") for row in rows} == {"24h", "3CatInfo"}


def test_counts_and_selection_title(app):
    assert app.counts_var.get() == "4 channels · 4 shown"

    app.search_var.set("canal")
    app._apply_filter()
    assert app.counts_var.get() == "4 channels · 1 shown"

    app._select(app.library.channels[1].id)
    app.update()
    assert app.title().startswith("Canal Sur Andalucía [Locales]")
    assert gui.TITLE in app.title()


def test_playing_the_selection_uses_the_fake_player(app, fake_player):
    channel = app.library.channels[1]
    app._select(channel.id)
    app._play_selected()
    assert fake_player.calls[-1] == (channel.name, 0)


def test_next_source_starts_at_the_second_mirror(app, fake_player):
    channel = app.library.channels[2]  # 24h has two streams
    app._select(channel.id)
    app._play_selected(next_source=True)
    assert fake_player.calls[-1] == (channel.name, 1)


def test_player_events_drive_the_status_bar(app, fake_player):
    channel = app.library.channels[0]
    fake_player.events.append(
        {"kind": "started", "channel": channel, "attempt": 1, "total": 1}
    )
    app._drain_player_events()
    assert app.status_var.get().startswith("Trying La 1")

    fake_player.events.append({"kind": "loaded", "channel": channel})
    app._drain_player_events()
    assert app.status_var.get() == "Playing La 1"
    assert app._playing_channel is channel

    fake_player.events.append(
        {"kind": "failed", "channel": channel, "message": "mpv exited with status 1"}
    )
    app._drain_player_events()
    assert "status 1" in app.status_var.get()


def test_toggle_favourite_persists_and_marks_the_row(app):
    channel = app.library.channels[0]
    app._select(channel.id)
    app._toggle_favourite()

    assert channel.favourite is True
    assert app.tree.set(channel.id, "star") == "★"
    assert store.load(app.library.path).channels[0].favourite is True

    app._toggle_favourite()
    assert channel.favourite is False


def test_favourites_filter_shows_only_starred_channels(app):
    app.library.channels[0].favourite = True
    app.favourites_var.set(True)
    app._apply_filter()

    rows = app.tree.get_children()
    assert [app.tree.set(row, "name") for row in rows] == ["La 1"]


def test_switcher_lists_favourites_and_plays_the_chosen_one(app, fake_player):
    app.library.channels[0].favourite = True
    app.library.channels[1].favourite = True
    favourites = [c for c in app.library.channels if c.favourite]

    switcher = gui.Switcher(app, favourites)
    switcher.withdraw()
    assert len(switcher.tree.get_children()) == 2

    switcher.query.set("sur")
    switcher._reload()
    rows = switcher.tree.get_children()
    assert len(rows) == 1

    switcher.tree.selection_set(rows[0])
    switcher._choose()

    assert fake_player.calls[-1][0] == "Canal Sur Andalucía"


def test_zap_cycles_through_the_favourites(app, fake_player):
    for channel in app.library.channels[:3]:
        channel.favourite = True
    app._apply_filter()

    app._playing_channel = app.library.channels[0]
    app._zap(1)
    assert fake_player.calls[-1][0] == app.library.channels[1].name

    app._playing_channel = app.library.channels[2]
    app._zap(1)
    assert fake_player.calls[-1][0] == app.library.channels[0].name  # wrapped

    app._playing_channel = app.library.channels[0]
    app._zap(-1)
    assert fake_player.calls[-1][0] == app.library.channels[2].name  # wrapped back


def test_remove_asks_and_persists(app, monkeypatch):
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *args, **kwargs: True)
    channel = app.library.channels[0]

    app._select(channel.id)
    app._remove_selected()

    assert len(app.tree.get_children()) == 3
    stored = store.load(app.library.path)
    assert [item.name for item in stored.channels] == [
        "Canal Sur Andalucía",
        "24h",
        "3CatInfo",
    ]


def test_remove_can_be_cancelled(app, monkeypatch):
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *args, **kwargs: False)
    app._select(app.library.channels[0].id)
    app._remove_selected()
    assert len(app.tree.get_children()) == 4


def test_add_channel_through_the_dialog(app, monkeypatch):
    new = make_channel("Nueva", "Locales", "https://example.test/nueva")
    monkeypatch.setattr(gui, "ask_channel", lambda *args, **kwargs: new)

    app._add_channel()

    names = [app.tree.set(row, "name") for row in app.tree.get_children()]
    assert "Nueva" in names
    stored = store.load(app.library.path)
    assert stored.channels[-1].name == "Nueva"
    assert stored.channels[-1].playlists == []  # added by hand
    assert app.tree.set(new.id, "playlist") == gui.MANUAL_LABEL


def test_edit_channel_keeps_the_other_mirrors(app, monkeypatch):
    channel = Channel(
        name="La 1",
        group="Generalistas",
        sources=[
            Source(url="https://example.test/one", playlist="p1"),
            Source(url="https://example.test/two", playlist="p2"),
        ],
    )
    app.library.add(channel)
    app._apply_filter()

    def fake_dialog(parent, *, title, channel=None):
        channel.name = "La Uno"
        channel.sources[0].url = "https://example.test/primary"
        return channel

    monkeypatch.setattr(gui, "ask_channel", fake_dialog)
    app._select(channel.id)
    app._edit_selected()

    stored = [item for item in store.load(app.library.path).channels if item.name == "La Uno"]
    assert len(stored) == 1
    assert [source.url for source in stored[0].sources] == [
        "https://example.test/primary",
        "https://example.test/two",
    ]


def test_refresh_all_merges_every_playlist(app):
    feeds = {
        "https://example.test/tv.m3u8": ParseResult(
            channels=[make_channel("Nueva TDT", "Locales", "https://example.test/nt")],
            entries=1,
        ),
        "https://example.test/es.m3u": ParseResult(
            channels=[make_channel("Nueva IPTV", "News", "https://example.test/ni")],
            entries=1,
        ),
    }
    app.loader = lambda location: feeds[location]

    app.refresh_playlists()

    assert wait_for(app, lambda: not app._busy) is True
    names = {app.tree.set(row, "name") for row in app.tree.get_children()}
    # Each feed now lists a single station: the ones that left the feeds are
    # pruned (including the one both playlists used to share).
    assert names == {"Nueva TDT", "Nueva IPTV"}
    assert "2 playlists" in app.status_var.get()
    assert "removed" in app.status_var.get()

    stored = store.load(app.library.path)
    assert {playlist.last_error for playlist in stored.playlists} == {""}
    assert all(playlist.last_refresh for playlist in stored.playlists)
    assert all(playlist.channel_count > 0 for playlist in stored.playlists)


def test_refresh_reports_a_failing_playlist_without_losing_channels(app):
    def failing(location):
        raise PlaylistError("HTTP 503")

    app.loader = failing
    app.refresh_playlists()

    assert wait_for(app, lambda: not app._busy) is True
    assert "failed" in app.status_var.get()
    assert "HTTP 503" in app.status_var.get()
    assert len(app.tree.get_children()) == 4

    stored = store.load(app.library.path)
    assert all(playlist.last_error.startswith("HTTP 503") for playlist in stored.playlists)


def test_playlists_dialog_adds_a_preset(app):
    dialog = gui.PlaylistsDialog(app)
    dialog.withdraw()

    dialog._add_preset("iptv-org-es")

    assert app.library.playlist_by_name("IPTV-org Spain") is not None
    stored = store.load(app.library.path)
    assert "IPTV-org Spain" in [playlist.name for playlist in stored.playlists]
    assert len(dialog.tree.get_children()) == len(app.library.playlists)
    dialog.destroy()


def test_playlists_dialog_removes_a_playlist_and_can_keep_channels(app, monkeypatch):
    monkeypatch.setattr(gui.messagebox, "askyesnocancel", lambda *args, **kwargs: False)
    dialog = gui.PlaylistsDialog(app)
    dialog.withdraw()

    dialog.tree.selection_set("p2")
    dialog._remove()

    assert app.library.playlist("p2") is None
    assert "3CatInfo" in [channel.name for channel in app.library.channels]
    dialog.destroy()


def test_playlists_dialog_toggles_enabled(app):
    dialog = gui.PlaylistsDialog(app)
    dialog.withdraw()

    dialog.tree.selection_set("p1")
    dialog._toggle()

    assert app.library.playlist("p1").enabled is False
    assert store.load(app.library.path).playlist("p1").enabled is False
    dialog.destroy()


def test_playlist_dialog_validates_the_location(app, monkeypatch):
    errors = []
    monkeypatch.setattr(
        gui.messagebox, "showerror", lambda *args, **kwargs: errors.append(args[1])
    )

    dialog = gui.PlaylistDialog(app, title="Add playlist")
    dialog.withdraw()
    dialog.url_var.set("not-a-location")
    dialog._save()
    assert dialog.result is None
    assert errors

    dialog.url_var.set("https://example.test/list.m3u8")
    dialog._save()
    assert dialog.result is not None
    assert dialog.result.url == "https://example.test/list.m3u8"


def test_closing_saves_the_session(app):
    app.search_var.set("canal")
    app.playlist_var.set("IPTV-org Spain")
    app.favourites_var.set(True)
    app._apply_filter()

    app._on_close()

    stored = store.load(app.library.path)
    assert stored.settings["last_filter"] == "canal"
    assert stored.settings["last_playlist_filter"] == "IPTV-org Spain"
    assert stored.settings["favourites_only"] is True
    assert stored.settings["geometry"]


def test_closing_leaves_the_player_open_by_default(app, fake_player):
    app._on_close()

    assert ("detach",) in fake_player.calls
    assert ("stop",) not in fake_player.calls
    assert store.load(app.library.path).settings["keep_player_open"] is True


def test_closing_stops_the_player_when_the_box_is_off(app, fake_player):
    app.keep_player_var.set(False)

    app._on_close()

    assert ("stop",) in fake_player.calls
    assert ("detach",) not in fake_player.calls
    assert store.load(app.library.path).settings["keep_player_open"] is False


def test_keep_player_open_is_read_from_the_config(library, fake_player):
    library.settings["keep_player_open"] = False

    window = gui.App(library=library, player=fake_player)
    window.withdraw()
    try:
        assert window.keep_player_var.get() is False
    finally:
        window.destroy()


# -- the catalogue (countries / languages) ----------------------------------


def fake_catalogue_loader():
    """Serve the fixture indexes instead of the network."""
    countries = json.loads((FIXTURES / "countries.json").read_text(encoding="utf-8"))
    languages = json.loads((FIXTURES / "languages.json").read_text(encoding="utf-8"))
    spoken = {code for country in countries for code in country.get("languages", [])}

    def loader(kind, *, all_languages=False, refresh=False):
        if kind == "country":
            entries = [
                gui.catalogue.CatalogueEntry(
                    "country", item["code"], item["name"], item["flag"]
                )
                for item in countries
            ]
        else:
            entries = [
                gui.catalogue.CatalogueEntry("language", item["code"], item["name"])
                for item in languages
            ]
            if not all_languages:
                entries = [entry for entry in entries if entry.code in spoken]
        entries.sort(key=lambda entry: entry.name.casefold())
        return gui.catalogue.Catalogue(kind=kind, entries=entries, total=len(entries))

    return loader


@pytest.fixture()
def catalogue_app(library, fake_player, monkeypatch):
    monkeypatch.setattr(
        gui.catalogue,
        "probe_with_cache",
        lambda url, cached=None, probe=None: True,
    )
    window = gui.App(
        library=library,
        player=fake_player,
        catalogue_loader=fake_catalogue_loader(),
        catalogue_probes_loader=lambda: {},
        first_run=False,
    )
    window.withdraw()
    window.update()
    yield window
    try:
        window.destroy()
    except Exception:  # pragma: no cover
        pass


def test_catalogue_browser_loads_searches_and_marks(catalogue_app):
    statuses = []
    browser = gui.CatalogueBrowser(
        catalogue_app, loader=fake_catalogue_loader(), on_status=statuses.append
    )
    browser.set_library(catalogue_app.library)
    browser.start()

    assert wait_for(
        catalogue_app,
        lambda: browser.loaded("country") and browser.loaded("language"),
    )
    assert any("countries" in text for text in statuses)

    tree = browser.tabs["country"]["tree"]
    assert len(tree.get_children()) == 4

    browser.tabs["country"]["query"].set("spain")
    browser._apply("country")
    rows = tree.get_children()
    assert [tree.set(row, "code") for row in rows] == ["ES"]

    es_url = "https://iptv-org.github.io/iptv/countries/es.m3u"
    browser.set_probes({es_url: False})
    assert tree.set(rows[0], "state") == "! no playlist"

    catalogue_app.library.playlists.append(
        Playlist(url=es_url, name="IPTV-org (Spain)", id="catalogue-es")
    )
    browser.set_library(catalogue_app.library)
    assert tree.set(rows[0], "state") == "✓ added"

    browser.destroy()


def test_catalogue_browser_shows_every_language_on_request(catalogue_app):
    browser = gui.CatalogueBrowser(catalogue_app, loader=fake_catalogue_loader())
    browser.start()

    assert wait_for(catalogue_app, lambda: browser.loaded("language"))
    tree = browser.tabs["language"]["tree"]
    assert len(tree.get_children()) == 4  # the languages spoken in some country

    browser.tabs["language"]["show_all"].set(True)
    browser.reload("language", all_languages=True)

    assert wait_for(catalogue_app, lambda: len(tree.get_children()) == 6)
    browser.destroy()


def test_catalogue_dialog_adds_the_selection(catalogue_app):
    dialog = gui.CatalogueDialog(catalogue_app)
    dialog.withdraw()
    dialog.refresh_after.set(False)

    assert wait_for(catalogue_app, lambda: dialog.browser.loaded("country"))
    dialog.browser.tabs["country"]["tree"].selection_set("country:ES")
    dialog._add_selected()

    assert wait_for(
        catalogue_app,
        lambda: not dialog._busy
        and catalogue_app.library.playlist_by_name("IPTV-org (Spain)") is not None,
    )

    assert "1 added" in dialog.status_var.get()
    tree = dialog.browser.tabs["country"]["tree"]
    assert tree.set("country:ES", "state") == "✓ added"
    stored = store.load(catalogue_app.library.path)
    assert stored.playlist_by_name("IPTV-org (Spain)") is not None
    dialog.destroy()


def test_catalogue_dialog_reports_a_playlist_that_does_not_exist(
    catalogue_app, monkeypatch
):
    monkeypatch.setattr(
        gui.catalogue, "probe_with_cache", lambda url, cached=None, probe=None: False
    )
    dialog = gui.CatalogueDialog(catalogue_app)
    dialog.withdraw()
    dialog.refresh_after.set(False)

    assert wait_for(catalogue_app, lambda: dialog.browser.loaded("country"))
    dialog.browser.tabs["country"]["tree"].selection_set("country:AQ")
    dialog._add_selected()

    assert wait_for(
        catalogue_app,
        lambda: not dialog._busy and "playlist" in dialog.status_var.get(),
    )
    assert "without a playlist" in dialog.status_var.get()
    assert catalogue_app.library.playlist_by_name("Antarctica") is None
    dialog.destroy()


def test_playlists_manager_opens_the_catalogue(catalogue_app):
    manager = gui.PlaylistsDialog(catalogue_app)
    manager.withdraw()

    manager._browse_catalogue()
    catalogue_app.update()

    opened = [
        child
        for child in catalogue_app.winfo_children()
        if isinstance(child, gui.CatalogueDialog)
    ]
    assert opened
    opened[0].destroy()
    manager.destroy()


# -- the first-run wizard ---------------------------------------------------


def empty_catalogue_app(tmp_path):
    library = store.Library(path=tmp_path / "empty.json")
    library.save()
    window = gui.App(
        library=library,
        player=FakePlayer(),
        catalogue_loader=fake_catalogue_loader(),
        catalogue_probes_loader=lambda: {},
        first_run=False,
    )
    window.withdraw()
    window.update()
    return window


def test_welcome_is_only_offered_for_an_empty_library(tmp_path, app):
    assert app._should_offer_welcome() is False  # the fixture has channels

    window = empty_catalogue_app(tmp_path)
    try:
        assert window._should_offer_welcome() is True
        window.library.settings["welcome_done"] = True
        assert window._should_offer_welcome() is False
    finally:
        window.destroy()


def test_welcome_skip_is_remembered(tmp_path):
    window = empty_catalogue_app(tmp_path)
    dialog = window.show_welcome()
    dialog.withdraw()

    dialog._skip()

    assert window._welcome is None
    assert store.load(window.library.path).settings["welcome_done"] is True
    assert window._should_offer_welcome() is False
    window.destroy()


def test_welcome_adds_the_selection_and_refreshes(tmp_path, monkeypatch):
    monkeypatch.setattr(
        gui.catalogue, "probe_with_cache", lambda url, cached=None, probe=None: True
    )
    window = empty_catalogue_app(tmp_path)
    window.loader = lambda location: ParseResult(
        channels=[make_channel("Nueva", "Locales", "https://example.test/n")],
        entries=1,
    )
    dialog = window.show_welcome()
    dialog.withdraw()

    assert wait_for(window, lambda: dialog.browser.loaded("country"))
    dialog.browser.tabs["country"]["tree"].selection_set("country:ES")
    dialog._accept()

    assert wait_for(
        window, lambda: window.library.playlist_by_name("IPTV-org (Spain)") is not None
    )
    assert wait_for(window, lambda: not window._busy)

    assert window._welcome is None
    assert store.load(window.library.path).settings["welcome_done"] is True
    assert [channel.name for channel in window.library.channels] == ["Nueva"]
    window.destroy()
