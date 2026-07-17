#!/usr/bin/env bash
# Launch the Wallpaper Engine Manager GUI from anywhere.
cd "$(dirname "$0")" || exit 1
exec python -m wpe_manager "$@"
