#!/usr/bin/env bash
# Build the pybin LLVM obfuscation pass-plugin (libpybinobf.so).
# Override the toolchain with LLVM_CONFIG=... CXX=... if needed.
set -euo pipefail
cd "$(dirname "$0")"

LLVM_CONFIG="${LLVM_CONFIG:-}"
if [[ -z "$LLVM_CONFIG" ]]; then
  for c in llvm-config llvm-config-21 llvm-config-20 llvm-config-19; do
    command -v "$c" >/dev/null 2>&1 && { LLVM_CONFIG="$c"; break; }
  done
fi
[[ -n "$LLVM_CONFIG" ]] || { echo "error: llvm-config not found; install llvm-21-dev or set LLVM_CONFIG" >&2; exit 1; }

CXX="${CXX:-clang++}"
command -v "$CXX" >/dev/null 2>&1 || { echo "error: $CXX not found" >&2; exit 1; }

OUT=libpybinobf.so
echo "[obf] LLVM $("$LLVM_CONFIG" --version)  ($LLVM_CONFIG)"
echo "[obf] compiling Obfuscator.cpp -> $OUT"

# A pass plugin is loaded into clang at run time, so we do NOT link LLVM libs;
# its symbols are resolved by the hosting clang. Just produce a shared object.
"$CXX" -shared -fPIC -fno-rtti -O2 -Wall \
  $("$LLVM_CONFIG" --cxxflags) \
  Obfuscator.cpp -o "$OUT"

echo "[obf] built $(pwd)/$OUT"
echo "[obf] use:  clang -O2 -fpass-plugin=$(pwd)/$OUT file.c -o file"
