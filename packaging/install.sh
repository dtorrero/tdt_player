#!/usr/bin/env bash
# Install tdt_player for the current user: a launcher, a KDE (XDG) menu entry
# and its icon.
#
#   ./packaging/install.sh                     install or update it
#   ./packaging/install.sh --checkout          …but run this checkout itself
#   ./packaging/install.sh --prefix DIR        install under DIR (default ~/.local)
#   ./packaging/install.sh --uninstall         remove launcher, menu entry, icon, copy
#   ./packaging/install.sh --uninstall --purge …and the stored settings and cache
#   ./packaging/install.sh --no-refresh        do not run the desktop/icon caches
#
# By default the package is COPIED to <prefix>/share/tdtplayer, so the menu entry
# keeps working when this checkout is moved or deleted; --checkout leaves it in
# place and points the launcher here instead (handy while developing).
#
# No root and no pip: the launcher runs the installed copy with `python3 -m
# tdt_player` and PYTHONPATH, which leaves Manjaro's externally-managed Python
# alone. The only requirements are python3 with tkinter and mpv.
set -euo pipefail

APP_ID="tdtplayer"
APP_NAME="tdt_player"
COMMAND="tdtplayer"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PACKAGE_SOURCE="$PROJECT_DIR/tdt_player"
DESKTOP_TEMPLATE="$PROJECT_DIR/packaging/$APP_ID.desktop"
ICON_SOURCE="$PACKAGE_SOURCE/$APP_ID.png"

PREFIX=""
MODE="copy"
REFRESH=1
UNINSTALL=0
PURGE=0
ICON_SIZES=(256 128 64 48 32 24 16)

say() { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 2; }

usage() {
    # Print the comment block at the top of this file (lines 2-18).
    sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' | sed -e :a -e '/^\n*$/{$d;N;ba' -e '}'
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --prefix)
                [[ $# -ge 2 ]] || die "--prefix needs a directory"
                PREFIX="$2"
                shift 2
                ;;
            --prefix=*) PREFIX="${1#*=}"; shift ;;
            --checkout|--link) MODE="checkout"; shift ;;
            --uninstall|-u) UNINSTALL=1; shift ;;
            --purge) PURGE=1; shift ;;
            --no-refresh) REFRESH=0; shift ;;
            -h|--help) usage; exit 0 ;;
            *) die "unknown option: $1 (try --help)" ;;
        esac
    done
}

resolve_paths() {
    if [[ -n "$PREFIX" ]]; then
        mkdir -p "$PREFIX"
        PREFIX="$(cd "$PREFIX" && pwd)"
        BIN_DIR="$PREFIX/bin"
        DATA_DIR="$PREFIX/share"
    else
        BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
        DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}"
    fi
    if [[ "$MODE" == "checkout" ]]; then
        # The launcher runs this checkout; nothing is copied.
        APP_DIR="$PROJECT_DIR"
    else
        APP_DIR="$DATA_DIR/$APP_ID"
    fi
    LAUNCHER="$BIN_DIR/$COMMAND"
    ENTRY="$DATA_DIR/applications/$APP_ID.desktop"
    ICON_THEME_DIR="$DATA_DIR/icons/hicolor"
    ICON_NAME="$APP_ID.png"
}

check_runtime() {
    command -v python3 >/dev/null 2>&1 || die "python3 is required (sudo pacman -S python)"
    python3 -c 'import tkinter' >/dev/null 2>&1 \
        || die "tkinter is missing: sudo pacman -S tk"
    command -v mpv >/dev/null 2>&1 \
        || warn "mpv was not found - playing a channel needs it (sudo pacman -S mpv)"
    command -v yt-dlp >/dev/null 2>&1 \
        || say "  tip: sudo pacman -S yt-dlp   # for the YouTube-hosted channels"
}

install_copy() {
    say "Copying the package to $APP_DIR"
    mkdir -p "$APP_DIR"
    rm -rf "$APP_DIR/tdt_player"
    cp -a "$PACKAGE_SOURCE" "$APP_DIR/tdt_player"
    find "$APP_DIR/tdt_player" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
    find "$APP_DIR/tdt_player" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete 2>/dev/null || true
}

write_launcher() {
    mkdir -p "$BIN_DIR"
    rm -f "$LAUNCHER"
    cat >"$LAUNCHER" <<EOF
#!/bin/sh
# tdt_player launcher, written by packaging/install.sh.
# It runs the app from APP_DIR with python3 and PYTHONPATH, so no pip install
# and no console script are needed (Manjaro's Python is externally managed).
APP_DIR="$APP_DIR"
if [ ! -d "\$APP_DIR/tdt_player" ]; then
    echo "tdt_player is missing from \$APP_DIR; re-run packaging/install.sh" >&2
    exit 1
fi
PYTHONPATH="\$APP_DIR\${PYTHONPATH:+:\$PYTHONPATH}"
export PYTHONPATH
exec python3 -m tdt_player "\$@"
EOF
    chmod 755 "$LAUNCHER"
    say "  launcher: $LAUNCHER"
}

master_pixel_size() {
    # Read the width straight out of the PNG header - no image library needed.
    python3 - "$ICON_SOURCE" <<'PY'
import sys

PNG_MAGIC = bytes.fromhex("89504e470d0a1a0a")
with open(sys.argv[1], "rb") as handle:
    header = handle.read(24)
if header[:8] != PNG_MAGIC:
    raise SystemExit(f"{sys.argv[1]} is not a PNG file")
print(int.from_bytes(header[16:20], "big"))
PY
}

install_icon() {
    if [[ ! -f "$ICON_SOURCE" ]]; then
        say "No icon yet; drawing one with packaging/make_icon.py"
        python3 "$PROJECT_DIR/packaging/make_icon.py" --out "$ICON_SOURCE" \
            || die "could not draw the icon (is Pillow installed?)"
    fi
    local master
    master="$(master_pixel_size 2>/dev/null || true)"
    [[ "$master" =~ ^[0-9]+$ ]] || die "$ICON_SOURCE is not a readable PNG"

    # An icon theme entry is looked up by size, so every copy must live in a
    # directory named after its real pixel size. Replace rather than overwrite:
    # a recreated file makes the icon caches notice the change.
    local target="$ICON_THEME_DIR/${master}x${master}/apps/$ICON_NAME"
    mkdir -p "$(dirname "$target")"
    rm -f "$target"
    cp -f "$ICON_SOURCE" "$target"
    chmod 644 "$target"
    say "  icon:     $target (${master}x${master} master)"

    if ! python3 -c 'import PIL' >/dev/null 2>&1; then
        warn "Pillow is not installed - only the ${master}x${master} icon was installed"
        return 0
    fi
    local size scaled
    for size in "${ICON_SIZES[@]}"; do
        (( size < master )) || continue
        scaled="$ICON_THEME_DIR/${size}x${size}/apps/$ICON_NAME"
        mkdir -p "$(dirname "$scaled")"
        rm -f "$scaled"
        python3 - "$ICON_SOURCE" "$scaled" "$size" <<'PY'
import sys

from PIL import Image

with Image.open(sys.argv[1]) as image:
    image.resize((int(sys.argv[3]), int(sys.argv[3])), Image.LANCZOS).save(
        sys.argv[2], optimize=True
    )
PY
        chmod 644 "$scaled"
    done
    say "  icons:    ${ICON_SIZES[*]} px copies of the same icon"
}

install_entry() {
    mkdir -p "$(dirname "$ENTRY")"
    local rendered
    rendered="$(sed -e "s|@EXEC@|$LAUNCHER|g" "$DESKTOP_TEMPLATE")"
    rm -f "$ENTRY"
    printf '%s\n' "$rendered" >"$ENTRY"
    chmod 644 "$ENTRY"
    say "  menu:     $ENTRY"
}

refresh_caches() {
    [[ "$REFRESH" -eq 1 ]] || return 0
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database "$DATA_DIR/applications" >/dev/null 2>&1 || true
    fi
    if command -v gtk-update-icon-cache >/dev/null 2>&1 && [[ -d "$ICON_THEME_DIR" ]]; then
        gtk-update-icon-cache -f -t "$ICON_THEME_DIR" >/dev/null 2>&1 || true
    fi
    # Plasma answers menu lookups from kbuildsycoca's cache and icon lookups from
    # ~/.cache/icon-cache.kcache; drop the latter so a changed icon shows up.
    local sycoca
    for sycoca in kbuildsycoca6 kbuildsycoca5; do
        if command -v "$sycoca" >/dev/null 2>&1; then
            "$sycoca" >/dev/null 2>&1 || true
            break
        fi
    done
    rm -f "${XDG_CACHE_HOME:-$HOME/.cache}/icon-cache.kcache" 2>/dev/null || true
}

install_app() {
    [[ -f "$PACKAGE_SOURCE/__init__.py" ]] || die "$PACKAGE_SOURCE/__init__.py not found"
    [[ -f "$DESKTOP_TEMPLATE" ]] || die "$DESKTOP_TEMPLATE is missing"
    check_runtime
    resolve_paths

    if [[ "$MODE" == "copy" ]]; then
        install_copy
    else
        say "Using the checkout at $PROJECT_DIR (--checkout)"
    fi
    write_launcher
    install_icon
    install_entry
    refresh_caches

    # Prove the launcher can start: catching an import problem here is friendlier
    # than a menu entry that does nothing.
    if ! "$LAUNCHER" --version >/dev/null 2>&1; then
        warn "the launcher could not start ($LAUNCHER --version failed)"
    fi

    if command -v desktop-file-validate >/dev/null 2>&1; then
        desktop-file-validate "$ENTRY" || warn "desktop-file-validate reported the above"
    fi

    say ""
    say "Installed $APP_NAME."
    say "Look for “tdt_player” in the application launcher (Audio & Video → Players),"
    say "or run: $COMMAND"
    say ""
    say "If the menu does not show it yet, refresh KDE's cache once:"
    say "  kbuildsycoca6     # or kbuildsycoca5 on Plasma 5"
    say "and, if a stale icon lingers in a panel, restart the shell:"
    say "  kquitapp6 plasmashell && plasmashell --no-respawn"
}

uninstall() {
    resolve_paths

    if [[ -e "$LAUNCHER" ]]; then
        rm -f "$LAUNCHER"
        say "Removed $LAUNCHER"
    fi
    if [[ -e "$ENTRY" ]]; then
        rm -f "$ENTRY"
        say "Removed $ENTRY"
    fi

    if [[ -d "$ICON_THEME_DIR" ]]; then
        while IFS= read -r installed; do
            rm -f "$installed"
            say "Removed $installed"
        done < <(find "$ICON_THEME_DIR" -maxdepth 3 -type f -name "$ICON_NAME" -path '*/apps/*' 2>/dev/null || true)
    fi

    # Never delete the checkout: only the copy this installer made.
    if [[ "$APP_DIR" != "$PROJECT_DIR" && -d "$APP_DIR/tdt_player" ]]; then
        rm -rf "$APP_DIR"
        say "Removed $APP_DIR"
    fi

    if [[ "$PURGE" -eq 1 ]]; then
        local config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/$APP_NAME"
        local cache_dir="${XDG_CACHE_HOME:-$HOME/.cache}/$APP_NAME"
        rm -rf "$config_dir" "$cache_dir"
        say "Removed $config_dir and $cache_dir"
    fi

    refresh_caches
    say "The checkout in $PROJECT_DIR was left untouched"
}

parse_args "$@"
if [[ "$UNINSTALL" -eq 1 ]]; then
    uninstall
else
    install_app
fi
