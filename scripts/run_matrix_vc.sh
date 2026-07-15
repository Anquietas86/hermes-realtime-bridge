#!/bin/bash
# Hermes Realtime Bridge — Matrix VC (LiveKit) launcher
# Sources .env properly and starts the bridge

set -a
cd "$(dirname "$0")/.."
source .env
set +a

source .venv/bin/activate

exec hermes-realtime \
  --adapter matrix-vc \
  --matrix-room "!ooYStQUSKarbOQeTOj:hagger.au" \
  -v \
  "$@"
