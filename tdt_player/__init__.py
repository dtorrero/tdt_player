"""tdt_player - browse M3U playlists and play the channels in mpv.

The package is deliberately standard-library only: :mod:`tkinter` draws the
window, :mod:`urllib` fetches the playlists and :mod:`subprocess` drives ``mpv``
(over its JSON IPC socket when possible).
"""

__version__ = "0.3.0"

__all__ = ["__version__"]
