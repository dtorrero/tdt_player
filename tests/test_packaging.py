"""Tests for the packaging bits: desktop entry, icon, installer and uninstaller.

The installer is exercised for real, but into a ``--prefix`` under ``tmp_path``
and with ``--no-refresh`` so the developer's own menu, icon cache and config are
never touched.
"""

from __future__ import annotations

import os
import struct
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
DESKTOP = PACKAGING / "tdtplayer.desktop"
INSTALLER = PACKAGING / "install.sh"
ICON = ROOT / "tdt_player" / "tdtplayer.png"


def run(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True, **kwargs)


def test_desktop_entry_is_a_complete_launcher_template():
    text = DESKTOP.read_text(encoding="utf-8")

    assert text.startswith("# ") or text.lstrip().startswith("#")
    assert "[Desktop Entry]" in text
    for key in (
        "Type=Application",
        "Name=tdt_player",
        "Exec=@EXEC@",
        "TryExec=@EXEC@",
        "Icon=tdtplayer",
        "Terminal=false",
        "Categories=AudioVideo;",
        "StartupWMClass=tdt_player",
        "StartupNotify=true",
    ):
        assert key in text, key


def test_installer_parses_and_documents_itself():
    result = run("bash", "-n", str(INSTALLER))
    assert result.returncode == 0, result.stderr

    text = INSTALLER.read_text(encoding="utf-8")
    assert "--uninstall" in text
    assert "--prefix" in text
    assert "--checkout" in text
    assert "kbuildsycoca6" in text and "kbuildsycoca5" in text
    assert "$APP_ID.desktop" in text  # the menu entry is named after the app id
    assert "PYTHONPATH" in text  # no pip, no console script needed

    usage = run("bash", str(INSTALLER), "--help")
    assert usage.returncode == 0
    assert "install or update" in usage.stdout


def test_icon_is_a_square_png_big_enough_for_a_menu():
    data = ICON.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    assert width == height >= 256


def test_installer_installs_everything_and_the_launcher_works(tmp_path):
    prefix = tmp_path / "prefix"

    result = run("bash", str(INSTALLER), "--prefix", str(prefix), "--no-refresh")
    assert result.returncode == 0, result.stdout + result.stderr

    launcher = prefix / "bin" / "tdtplayer"
    entry = prefix / "share" / "applications" / "tdtplayer.desktop"
    package = prefix / "share" / "tdtplayer" / "tdt_player" / "__init__.py"
    icons = sorted((prefix / "share" / "icons" / "hicolor").rglob("tdtplayer.png"))

    assert launcher.is_file() and os.access(launcher, os.X_OK)
    assert entry.is_file()
    assert package.is_file()
    assert icons, "no icon was installed"
    assert (prefix / "share" / "tdtplayer" / "tdt_player" / "tdtplayer.png").is_file()

    entry_text = entry.read_text(encoding="utf-8")
    assert f"Exec={launcher}" in entry_text
    assert f"TryExec={launcher}" in entry_text
    assert "Icon=tdtplayer" in entry_text

    # The launcher must run the installed copy, not the checkout: different cwd,
    # no PYTHONPATH, and the checkout's own package must not be importable.
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env["PYTHONPATH"] = ""
    version = run(str(launcher), "--version", cwd=str(tmp_path), env=env)
    assert version.returncode == 0, version.stdout + version.stderr
    assert version.stdout.startswith("tdtplayer ")


def test_uninstaller_removes_the_installation_but_not_the_checkout(tmp_path):
    prefix = tmp_path / "prefix"
    assert run("bash", str(INSTALLER), "--prefix", str(prefix), "--no-refresh").returncode == 0

    result = run(
        "bash", str(INSTALLER), "--uninstall", "--prefix", str(prefix), "--no-refresh"
    )
    assert result.returncode == 0, result.stdout + result.stderr

    assert not (prefix / "bin" / "tdtplayer").exists()
    assert not (prefix / "share" / "applications" / "tdtplayer.desktop").exists()
    assert not (prefix / "share" / "tdtplayer").exists()
    assert not list((prefix / "share" / "icons").rglob("tdtplayer.png"))

    # The source tree is untouched.
    assert (ROOT / "tdt_player" / "__init__.py").is_file()
    assert (ROOT / "packaging" / "install.sh").is_file()


def test_checkout_mode_points_the_launcher_at_the_repository(tmp_path):
    prefix = tmp_path / "prefix"

    result = run(
        "bash", str(INSTALLER), "--checkout", "--prefix", str(prefix), "--no-refresh"
    )
    assert result.returncode == 0, result.stdout + result.stderr

    launcher = prefix / "bin" / "tdtplayer"
    assert launcher.is_file()
    assert str(ROOT) in launcher.read_text(encoding="utf-8")

    # ...and uninstalling it must never delete the checkout itself.
    assert (
        run("bash", str(INSTALLER), "--uninstall", "--prefix", str(prefix), "--no-refresh").returncode
        == 0
    )
    assert (ROOT / "tdt_player" / "gui.py").is_file()
