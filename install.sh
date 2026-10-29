#!/usr/bin/env bash
# One-shot setup for azurlane-daily-tools on a fresh checkout:
# install dependencies, create the local config if missing, run the offline
# test suite. Prerequisites (Docker + ALAS container, Waydroid, adb) are
# covered by README.md; this script only sets up the Python side.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

command -v uv >/dev/null 2>&1 || {
    echo "error: uv is required (https://docs.astral.sh/uv/getting-started/installation/)" >&2
    exit 1
}

echo "=== [1/3] uv sync (sidecar dependencies) ==="
(cd sidecar && uv sync)

echo "=== [2/3] local config ==="
if [ ! -f sidecar/daily.yaml ]; then
    ./azdaily init
else
    echo "kept existing sidecar/daily.yaml"
fi

echo "=== [3/3] offline test suite ==="
./azdaily test

echo
echo "Cài đặt xong. Cấu hình thiết bị trong sidecar/daily.yaml rồi chạy:"
echo "  ./azdaily check   # soát an toàn"
echo "  ./azdaily run     # chạy daily"
echo "Hướng dẫn đầy đủ: docs/HUONG-DAN.md"
