#!/usr/bin/env bash
# Build every contestant. Modes: c | rust | all
#   ZSTD_SYSTEM=1  link impls/c/driver.c against the libzstd already installed on
#                  this host instead of building the pinned source tree
set -euo pipefail
MODE="${1:-all}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
JOBS="${JOBS:-$(nproc)}"
ZSTD_VERSION="${ZSTD_VERSION:-1.5.7}"
CC="${CC:-cc}"

mkdir -p "$ROOT/third_party/bin"

if [ "$MODE" = all ] || [ "$MODE" = c ]; then
  if [ "${ZSTD_SYSTEM:-0}" = "1" ]; then
    INC="${ZSTD_INCLUDE:-${PREFIX:-/usr}/include}"
    LIB="${ZSTD_LIBDIR:-${PREFIX:-/usr}/lib}"
    "$CC" -O3 -I"$INC" -o "$ROOT/third_party/bin/zstd-c-driver" "$ROOT/impls/c/driver.c" \
      -L"$LIB" -lzstd
  else
    cd "$ROOT/third_party"
    if [ ! -d "zstd-$ZSTD_VERSION" ]; then
      curl -sSL -o "zstd-$ZSTD_VERSION.tar.gz" \
        "https://github.com/facebook/zstd/releases/download/v$ZSTD_VERSION/zstd-$ZSTD_VERSION.tar.gz"
      tar xzf "zstd-$ZSTD_VERSION.tar.gz"
    fi
    make -C "zstd-$ZSTD_VERSION" -j"$JOBS" lib-release >/dev/null
    "$CC" -O3 -I"zstd-$ZSTD_VERSION/lib" -o "$ROOT/third_party/bin/zstd-c-driver" \
      "$ROOT/impls/c/driver.c" "$ROOT/third_party/zstd-$ZSTD_VERSION/lib/libzstd.a" -pthread
  fi
  "$ROOT/third_party/bin/zstd-c-driver" encode /dev/null "$ROOT/third_party/probe.zst" --level 1 >/dev/null
  "$ROOT/third_party/bin/zstd-c-driver" decode "$ROOT/third_party/probe.zst" "$ROOT/third_party/probe.out" >/dev/null
  rm -f "$ROOT/third_party/probe.zst" "$ROOT/third_party/probe.out"
  echo "C driver built and self-checked"
fi

if [ "$MODE" = all ] || [ "$MODE" = rust ]; then
  for d in "$ROOT"/impls/*/; do
    [ -f "$d/Cargo.toml" ] || continue
    echo "=== building $(basename "$d")"
    cargo build --release --locked --manifest-path "$d/Cargo.toml" 2>/dev/null \
      || cargo build --release --manifest-path "$d/Cargo.toml"
  done
  rustc -Vv | sed -n '1,3p'
fi
