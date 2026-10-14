#!/usr/bin/env bash
# install.sh — symlink `argo-hold` into <prefix>/bin (default: ~/.local).
# No sudo: argo-hold only calls kubectl and writes ~/.local/state/argo-hold.
#
#   ./install.sh [--prefix DIR]     # symlink into DIR/bin (default ~/.local)
#   ./install.sh --uninstall [--prefix DIR]
set -euo pipefail

PREFIX="${HOME}/.local"
UNINSTALL=0

usage() {
  printf 'usage: install.sh [--prefix DIR] [--uninstall] [--help]\n'
  printf '  Installs the argo-hold command as a symlink into DIR/bin (default: %s/bin).\n' "${HOME}/.local"
  printf '  --uninstall removes that symlink instead.\n'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h | --help) usage; exit 0 ;;
    *) printf 'install.sh: unknown option: %s\n' "$1" >&2; exit 2 ;;
  esac
done

BIN="$PREFIX/bin"
SRC="$(cd "$(dirname "$0")/scripts" && pwd)/argo-hold"

if [[ "$UNINSTALL" -eq 1 ]]; then
  rm -f "$BIN/argo-hold"
  printf 'removed %s\n' "$BIN/argo-hold"
  exit 0
fi

mkdir -p "$BIN"
chmod +x "$SRC"
ln -sf "$SRC" "$BIN/argo-hold"
printf 'installed: %s -> %s\n' "$BIN/argo-hold" "$SRC"
