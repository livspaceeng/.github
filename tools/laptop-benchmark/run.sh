#!/usr/bin/env bash
# Convenience wrapper: checks prerequisites, then runs the benchmark.
#
#   ./run.sh                 ~15 min realistic CAD + web 3D + tabs workload
#   ./run.sh --profile quick ~3 min, to verify the setup works
#   ./run.sh --profile soak  ~50 min, exposes thermal decay
#
# Any additional arguments are passed straight through to bench.py.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

die() { printf '\nerror: %s\n' "$1" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || die "python3 not found."
PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' \
  || die "Python 3.8+ required, found $PYV."

if ! command -v cc >/dev/null 2>&1 && ! command -v clang >/dev/null 2>&1 \
   && ! command -v gcc >/dev/null 2>&1; then
  if [[ "$(uname -s)" == "Darwin" ]]; then
    die "No C compiler. Install the Command Line Tools:  xcode-select --install"
  fi
  die "No C compiler found (need cc, clang or gcc)."
fi

if [[ "$(uname -s)" == "Darwin" ]]; then
  if ! pmset -g batt 2>/dev/null | grep -q "AC Power"; then
    printf '\nWARNING: running on battery. macOS caps sustained performance when unplugged.\n'
    printf 'Plug in and re-run for numbers that reflect desk use.\n'
    printf 'Continue anyway? [y/N] '
    read -r reply
    [[ "$reply" =~ ^[Yy]$ ]] || exit 1
  fi
  printf '\nTip: for package power and per-cluster frequency, pre-authorise sudo first:\n'
  printf '  sudo -v && ./run.sh %s\n' "$*"
fi

exec python3 bench.py "$@"
