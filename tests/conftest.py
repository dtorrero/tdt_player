"""Shared fixtures for the tdt_player test-suite.

Nothing here touches the network: the parser is exercised against a handcrafted
playlist and the rest of the suite works on ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def sample_playlist() -> Path:
    """Path to the handcrafted M3U used by the parser tests."""
    return FIXTURES / "tv_sample.m3u8"


@pytest.fixture()
def sample_text(sample_playlist: Path) -> str:
    """The fixture playlist as text."""
    return sample_playlist.read_text(encoding="utf-8")
