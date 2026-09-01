#!/usr/bin/env bash
#
# Build a portable, obfuscated onefile of a Python app inside the manylinux_2_28
# container, so it runs on any Linux with glibc >= 2.28.
#
#   ./build-in-docker.sh path/to/entry.py [extra pybin args...]
#
# The resulting binary lands in ./dist/ on the host. The first run builds the
# image (slow: pulls manylinux, installs LLVM + Nuitka + deps); later runs reuse
# the cached image and only recompile the app.
set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: $(basename "$0") path/to/entry.py [extra pybin args...]" >&2
    echo "  e.g. $(basename "$0") ../cryptor/cli.py" >&2
    exit 2
fi

# Resolve the app + output against the CALLER's cwd, before moving to the build
# context (so relative paths work when invoked from anywhere, e.g. via an alias).
ENTRY_ABS="$(realpath "$1")"; shift
[[ -f "$ENTRY_ABS" ]] || { echo "error: entry not found: $ENTRY_ABS" >&2; exit 1; }
APP_DIR="$(dirname "$ENTRY_ABS")"
ENTRY_NAME="$(basename "$ENTRY_ABS")"
OUT_DIR="$PWD/dist"; mkdir -p "$OUT_DIR"

# Build context = this script's directory (Dockerfile + pybin.py + obfuscator/).
cd "$(cd "$(dirname "$0")" && pwd)"
IMAGE="pybin-manylinux"

command -v docker >/dev/null 2>&1 || { echo "error: docker not found on PATH" >&2; exit 1; }

echo "[docker] building image '$IMAGE' (cached after first run)…"
docker build -t "$IMAGE" .

echo "[docker] compiling $ENTRY_NAME  →  $OUT_DIR/"
# --user keeps the output owned by you; HOME/NUITKA_CACHE_DIR (set in the image)
# give Nuitka somewhere writable. App mounted read-only; artifacts to /out.
docker run --rm \
    --user "$(id -u):$(id -g)" \
    -v "$APP_DIR:/app:ro" \
    -v "$OUT_DIR:/out" \
    "$IMAGE" "/app/$ENTRY_NAME" "$@"

echo
echo "[docker] done — portable artifact(s) in $OUT_DIR:"
ls -la "$OUT_DIR"
echo
echo "Runs on any glibc >= 2.28 Linux. Verify the target's glibc with: ldd --version"
