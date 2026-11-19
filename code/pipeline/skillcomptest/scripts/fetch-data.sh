#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

DATA_VERSION=$(cat "$REPO_ROOT/DATA_VERSION" 2>/dev/null || echo "")
if [ -z "$DATA_VERSION" ]; then
  echo "ERROR: DATA_VERSION file is empty or missing." >&2
  echo "Create it with the desired release tag (e.g. 'v0.1.0')." >&2
  exit 1
fi

REPO="${SKILLCOMPTEST_REPO:-TankTechnology/skillcomptest}"
DATA_STORE="${DATA_STORE:-$REPO_ROOT/.cache/skillcomptest}"
mkdir -p "$DATA_STORE"

echo "Fetching data release: $DATA_VERSION"
echo "Repo: $REPO"
echo "Destination: $DATA_STORE"

# Clean up any previously downloaded tarballs and checksums
rm -f "$DATA_STORE"/*.tar.gz "$DATA_STORE"/checksums.txt

# Try gh CLI first (needs GH_TOKEN or prior gh auth login)
if command -v gh &>/dev/null && (gh auth status &>/dev/null || [ -n "${GH_TOKEN:-}" ]); then
  echo "Using gh CLI for download..."
  gh release download "$DATA_VERSION" \
    --repo "$REPO" \
    --dir "$DATA_STORE" \
    --pattern '*.tar.gz' \
    --pattern 'checksums.txt'
else
  # Fall back to curl for public repos (no auth needed)
  echo "gh not authenticated, using curl fallback..."
  BASE_URL="https://github.com/$REPO/releases/download/$DATA_VERSION"
  for asset in samples.tar.gz exec.tar.gz skills-repo.tar.gz checksums.txt; do
    echo "Downloading $asset..."
    if command -v curl &>/dev/null; then
      curl -fsSL -o "$DATA_STORE/$asset" "$BASE_URL/$asset"
    elif command -v wget &>/dev/null; then
      wget -q -O "$DATA_STORE/$asset" "$BASE_URL/$asset"
    else
      echo "ERROR: neither curl nor wget found." >&2
      exit 1
    fi
  done
fi

# Verify checksums
if [ -f "$DATA_STORE/checksums.txt" ]; then
  echo "Verifying checksums..."
  cd "$DATA_STORE"
  if command -v shasum &>/dev/null; then
    shasum -a 256 -c checksums.txt --ignore-missing
  elif command -v sha256sum &>/dev/null; then
    sha256sum -c checksums.txt --ignore-missing
  else
    echo "WARNING: no sha256 tool found, skipping checksum verification"
  fi
fi

# Extract tarballs
for tar in "$DATA_STORE"/*.tar.gz; do
  if [ -f "$tar" ]; then
    echo "Extracting: $(basename "$tar")"
    tar -xzf "$tar" -C "$DATA_STORE"
    rm "$tar"
  fi
done

echo "Data fetch complete. DATA_STORE=$DATA_STORE"
