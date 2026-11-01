"""Tests for :mod:`tdt_player.cli`: resolution, subcommands and the config flag.

The GUI command is stubbed and the playlist is a local file, so nothing here
touches the network, mpv or the user's real configuration.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tdt_player import catalogue, cli, store
from tdt_player.models import Channel, Source

FIXTURES = Path(__file__).parent / "fixtures"


def install_catalogue(monkeypatch, *, unavailable=()):
    """Serve the catalogue fixtures instead of the network."""
    countries = json.loads((FIXTURES / "countries.json").read_text(encoding="utf-8"))
    languages = json.loads((FIXTURES / "languages.json").read_text(encoding="utf-8"))
    spoken = {code for country in countries for code in country.get("languages", [])}

    def fake_load(kind, *, all_languages=False, refresh=False):
        if kind == "country":
            entries = [
                catalogue.CatalogueEntry("country", item["code"], item["name"], item["flag"])
                for item in countries
            ]
        else:
            entries = [
                catalogue.CatalogueEntry("language", item["code"], item["name"])
                for item in languages
            ]
            if not all_languages:
                entries = [entry for entry in entries if entry.code in spoken]
        entries.sort(key=lambda entry: entry.name.casefold())
        return catalogue.Catalogue(kind=kind, entries=entries, total=len(entries))

    monkeypatch.setattr(cli.catalogue, "load_catalogue", fake_load)
    monkeypatch.setattr(cli.catalogue, "load_probes", lambda **kwargs: {})
    monkeypatch.setattr(
        cli.catalogue,
        "probe_with_cache",
        lambda url, cached=None, probe=None: False if url in unavailable else True,
    )


def make(name: str, group: str = "", url: str = "") -> Channel:
    return Channel(
        name=name,
        group=group,
        sources=[Source(url=url or f"https://example.test/{name}")],
    )


def save_library(tmp_path, channels):
    library = store.Library(path=tmp_path / "config.json", channels=channels)
    library.save()
    return library.path


def test_no_subcommand_opens_the_gui(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_command_gui", lambda args: calls.append(args) or 0)

    assert cli.main([]) == 0

    assert len(calls) == 1


def test_gui_receives_the_config_and_mpv_options(monkeypatch):
    forwarded = []
    monkeypatch.setattr(cli, "_command_gui", lambda args: forwarded.append(args) or 0)

    cli.main(["--config", "/tmp/custom.json", "gui", "--mpv", "/usr/bin/mpv"])

    assert forwarded[0].config == "/tmp/custom.json"
    assert forwarded[0].mpv == "/usr/bin/mpv"


def test_config_may_come_before_or_after_the_subcommand(tmp_path, capsys):
    path = save_library(tmp_path, [make("La 1", "Generalistas")])

    assert cli.main(["--config", str(path), "list"]) == 0
    assert "La 1" in capsys.readouterr().out

    assert cli.main(["list", "--config", str(path)]) == 0
    assert "La 1" in capsys.readouterr().out


def test_list_without_channels_hints_at_refresh(tmp_path, capsys):
    assert cli.main(["--config", str(tmp_path / "empty.json"), "list"]) == 0
    assert "tdtplayer refresh" in capsys.readouterr().err


def test_list_filters_by_search_and_group(tmp_path, capsys):
    path = save_library(
        tmp_path,
        [
            make("La 1", "Generalistas"),
            make("Canal Sur Andalucía", "Locales"),
            make("Canal Sur Más", "Locales"),
        ],
    )

    assert cli.main(["--config", str(path), "list", "--search", "andalucia"]) == 0
    out = capsys.readouterr().out
    assert "Canal Sur Andalucía" in out
    assert "La 1" not in out

    assert cli.main(["--config", str(path), "list", "--group", "Generalistas"]) == 0
    assert "La 1" in capsys.readouterr().out


def test_list_can_print_groups_only(tmp_path, capsys):
    path = save_library(
        tmp_path, [make("La 1", "Generalistas"), make("24h", "Informativos")]
    )

    assert cli.main(["--config", str(path), "list", "--groups"]) == 0

    assert capsys.readouterr().out.split() == ["Generalistas", "Informativos"]


def test_resolve_prefers_name_matches():
    channels = [make("Canal Sur Andalucía", "Locales"), make("Canal Local, S.A.", "Locales")]

    resolved = cli._resolve(channels, "canal local")

    assert resolved is not None
    assert resolved.name == "Canal Local, S.A."


def test_resolve_reports_ambiguity(capsys):
    channels = [make("Canal Sur", "Locales"), make("Canal Norte", "Locales")]

    assert cli._resolve(channels, "canal") is None
    err = capsys.readouterr().err
    assert "2 channels match" in err
    assert "Canal Sur" in err and "Canal Norte" in err


def test_resolve_suggests_close_names(capsys):
    assert cli._resolve([make("La 1")], "la 12") is None
    assert "did you mean" in capsys.readouterr().err


def test_resolve_rejects_an_empty_name(capsys):
    assert cli._resolve([make("La 1")], "   ") is None
    assert "empty channel name" in capsys.readouterr().err


def test_import_then_list(tmp_path, capsys, sample_playlist):
    path = tmp_path / "config.json"

    assert cli.main(["--config", str(path), "import", str(sample_playlist)]) == 0
    out = capsys.readouterr().out
    assert "7 added" in out

    assert cli.main(["--config", str(path), "list", "--search", "andalucia"]) == 0
    assert "Canal Sur Andalucía" in capsys.readouterr().out


def test_import_reports_a_missing_file(tmp_path, capsys):
    assert cli.main(["--config", str(tmp_path / "c.json"), "import", "/nope.m3u8"]) == 1
    assert "nope.m3u8" in capsys.readouterr().err


def test_refresh_reads_the_url_from_the_command_line(tmp_path, sample_playlist):
    path = tmp_path / "config.json"

    assert cli.main(["--config", str(path), "refresh", "--url", sample_playlist.as_uri()]) == 0
    assert len(store.load(path).channels) == 7


def test_play_unknown_channel_fails(tmp_path, capsys, sample_playlist):
    path = tmp_path / "config.json"
    cli.main(["--config", str(path), "import", str(sample_playlist)])
    capsys.readouterr()

    assert cli.main(["--config", str(path), "play", "no such channel"]) == 1
    assert "no channel matches" in capsys.readouterr().err


@pytest.mark.skipif(shutil.which("mpv") is None, reason="needs mpv to be installed")
def test_play_print_command_does_not_run_anything(tmp_path, capsys, sample_playlist):
    path = tmp_path / "config.json"
    cli.main(["--config", str(path), "import", str(sample_playlist)])
    capsys.readouterr()

    assert cli.main(["--config", str(path), "play", "Canal Sur", "--print-command"]) == 0

    out = capsys.readouterr().out
    assert out.startswith("mpv ")
    assert "https://example.test/sur/index.m3u8" in out


def test_sources_list_shows_the_configured_playlists(tmp_path, capsys):
    path = tmp_path / "config.json"

    assert cli.main(["--config", str(path), "sources", "list"]) == 0

    out = capsys.readouterr().out
    assert "tdtchannels" in out
    assert store.DEFAULT_LIST_URL in out


def test_sources_add_enable_disable_and_remove(tmp_path, sample_playlist):
    path = tmp_path / "config.json"

    assert (
        cli.main(
            [
                "--config",
                str(path),
                "sources",
                "add",
                str(sample_playlist),
                "--name",
                "Local",
                "--refresh",
            ]
        )
        == 0
    )
    playlist = store.load(path).playlist_by_name("Local")
    assert playlist is not None
    assert playlist.channel_count == 7

    assert cli.main(["--config", str(path), "sources", "disable", "Local"]) == 0
    assert store.load(path).playlist_by_name("Local").enabled is False

    assert cli.main(["--config", str(path), "sources", "enable", "Local"]) == 0
    assert store.load(path).playlist_by_name("Local").enabled is True

    assert cli.main(["--config", str(path), "sources", "remove", "Local"]) == 0
    library = store.load(path)
    assert library.playlist_by_name("Local") is None
    assert library.channels == []  # its channels went with it


def test_sources_add_accepts_a_preset(tmp_path):
    path = tmp_path / "config.json"

    assert (
        cli.main(
            [
                "--config",
                str(path),
                "sources",
                "add",
                "--preset",
                "iptv-org-es",
                "--disabled",
            ]
        )
        == 0
    )

    preset = store.load(path).playlist_by_name("IPTV-org Spain")
    assert preset is not None
    assert preset.url.endswith("/countries/es.m3u")
    assert preset.enabled is False


def test_refresh_reports_each_playlist_and_keeps_going(tmp_path, capsys, sample_playlist):
    path = tmp_path / "config.json"
    cli.main(["--config", str(path), "sources", "add", str(sample_playlist), "--name", "Local"])

    library = store.load(path)
    library.playlists[0].url = str(tmp_path / "missing.m3u8")  # the TDT one
    library.save()
    capsys.readouterr()

    assert cli.main(["--config", str(path), "refresh"]) == 1

    captured = capsys.readouterr()
    assert "Local: 7 channels" in captured.out
    assert "missing.m3u8" in captured.err
    assert "1 failed" in captured.err
    assert len(store.load(path).channels) == 7


def test_refresh_with_an_unknown_source_fails(tmp_path, capsys):
    assert (
        cli.main(["--config", str(tmp_path / "c.json"), "refresh", "--source", "nope"]) == 1
    )
    assert "no playlist matches" in capsys.readouterr().err


def test_import_once_does_not_register_a_playlist(tmp_path, sample_playlist):
    path = tmp_path / "config.json"

    assert cli.main(["--config", str(path), "import", str(sample_playlist), "--once"]) == 0

    library = store.load(path)
    assert len(library.channels) == 7
    assert [playlist.id for playlist in library.playlists] == ["tdtchannels"]
    assert library.channels[0].playlists == []  # nothing was tagged


def test_list_can_filter_by_playlist(tmp_path, capsys, sample_playlist):
    path = tmp_path / "config.json"
    cli.main(["--config", str(path), "import", str(sample_playlist)])
    capsys.readouterr()

    assert cli.main(["--config", str(path), "list", "--playlist", "tv_sample"]) == 0
    assert "La 1" in capsys.readouterr().out

    assert cli.main(["--config", str(path), "list", "--playlist", "TDTChannels"]) == 0
    assert "No channel matches" in capsys.readouterr().err


def test_catalogue_countries_lists_and_marks_the_configured_ones(
    tmp_path, capsys, monkeypatch
):
    install_catalogue(monkeypatch)
    path = tmp_path / "config.json"

    assert cli.main(["--config", str(path), "catalogue", "countries"]) == 0
    captured = capsys.readouterr()
    assert "Spain" in captured.out and " ES " in captured.out
    assert "countries" in captured.err
    assert captured.out.splitlines()[0].startswith(" ")

    assert cli.main(["--config", str(path), "sources", "add", "--country", "es"]) == 0
    capsys.readouterr()

    assert (
        cli.main(["--config", str(path), "catalogue", "countries", "--search", "spain"])
        == 0
    )
    assert capsys.readouterr().out.startswith("✓")


def test_catalogue_languages_hide_the_rare_ones_unless_all_is_given(
    tmp_path, capsys, monkeypatch
):
    install_catalogue(monkeypatch)
    path = tmp_path / "config.json"

    assert cli.main(["--config", str(path), "catalogue", "languages"]) == 0
    assert "Spanish" in capsys.readouterr().out

    assert cli.main(["--config", str(path), "catalogue", "languages", "--search", "assamese"]) == 0
    assert "Assamese" not in capsys.readouterr().out

    assert (
        cli.main(
            ["--config", str(path), "catalogue", "languages", "--search", "assamese", "--all"]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "Assamese" in out and "asm" in out


def test_catalogue_without_a_kind_shows_the_usage(tmp_path, capsys):
    assert cli.main(["--config", str(tmp_path / "c.json"), "catalogue"]) == 2
    assert "catalogue countries" in capsys.readouterr().err


def test_sources_add_country_and_language(tmp_path, capsys, monkeypatch):
    install_catalogue(monkeypatch)
    path = tmp_path / "config.json"

    assert (
        cli.main(
            [
                "--config",
                str(path),
                "sources",
                "add",
                "--country",
                "ES",
                "--language",
                "spa",
            ]
        )
        == 0
    )

    library = store.load(path)
    urls = [playlist.url for playlist in library.playlists]
    assert "https://iptv-org.github.io/iptv/countries/es.m3u" in urls
    assert "https://iptv-org.github.io/iptv/languages/spa.m3u" in urls
    assert library.playlist_by_name("IPTV-org (Spain)") is not None
    assert library.playlist_by_name("IPTV-org (Spanish)") is not None
    captured = capsys.readouterr()
    assert "2 added" in captured.err


def test_sources_add_country_without_a_playlist_fails(tmp_path, capsys, monkeypatch):
    install_catalogue(
        monkeypatch,
        unavailable={"https://iptv-org.github.io/iptv/countries/aq.m3u"},
    )
    path = tmp_path / "config.json"

    assert cli.main(["--config", str(path), "sources", "add", "--country", "AQ"]) == 1

    assert "no playlist" in capsys.readouterr().err.lower()
    assert store.load(path).playlist_by_name("Antarctica") is None


def test_sources_add_unknown_country_fails(tmp_path, capsys, monkeypatch):
    install_catalogue(monkeypatch)

    assert (
        cli.main(
            ["--config", str(tmp_path / "c.json"), "sources", "add", "--country", "Atlantis"]
        )
        == 1
    )
    assert "no country matches" in capsys.readouterr().err


def test_sources_add_country_is_idempotent(tmp_path, capsys, monkeypatch):
    install_catalogue(monkeypatch)
    path = tmp_path / "config.json"

    assert cli.main(["--config", str(path), "sources", "add", "--country", "es"]) == 0
    capsys.readouterr()
    assert cli.main(["--config", str(path), "sources", "add", "--country", "Spain"]) == 0

    assert "already configured" in capsys.readouterr().out
    assert len(store.load(path).playlists) == 2  # TDTChannels + IPTV-org Spain


def test_favourite_command_stars_and_unstars(tmp_path, capsys, sample_playlist):
    path = tmp_path / "config.json"
    cli.main(["--config", str(path), "import", str(sample_playlist), "--once"])
    capsys.readouterr()

    assert cli.main(["--config", str(path), "favourite", "Canal Sur"]) == 0
    assert "starred" in capsys.readouterr().out
    starred = [c for c in store.load(path).channels if c.name.startswith("Canal Sur")]
    assert starred and starred[0].favourite is True

    assert cli.main(["--config", str(path), "list", "--favourites"]) == 0
    assert "Canal Sur" in capsys.readouterr().out

    assert cli.main(["--config", str(path), "favourite", "Canal Sur", "--off"]) == 0
    assert "unstarred" in capsys.readouterr().out
    again = [c for c in store.load(path).channels if c.name.startswith("Canal Sur")]
    assert again and again[0].favourite is False
