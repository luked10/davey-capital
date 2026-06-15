#!/bin/bash
# Sync the vibe-trading/ subtree from the HKUDS/Vibe-Trading upstream.
#
# Run from the repo root. Uses git subtree so vibe-trading/ stays a vendored
# copy that can be pulled forward without losing local Davey-specific files
# (fly.toml, etc.). Squashes upstream history into a single sync commit.
set -euo pipefail

git remote add vibe-trading-upstream https://github.com/HKUDS/Vibe-Trading.git 2>/dev/null || true
git fetch vibe-trading-upstream main
git subtree pull --prefix=vibe-trading vibe-trading-upstream main --squash \
  -m "chore: sync vibe-trading upstream"
echo "vibe-trading synced"
