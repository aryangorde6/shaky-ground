#!/usr/bin/env bash
# Copy what the Hugging Face Space needs into a clone of the Space's git repo.
#   deploy/make-space.sh ../shaky-space
# Then, in that folder: git add -A && git commit -m "Update" && git push
set -euo pipefail
dest="${1:?usage: deploy/make-space.sh <clone of the Space repo>}"
here="$(cd "$(dirname "$0")/.." && pwd)"
[ -d "$dest/.git" ] || { echo "$dest is not a git clone of the Space" >&2; exit 1; }
rm -rf "$dest/shaky" "$dest/web"
cp -r "$here/shaky" "$here/web" "$dest/"
cp "$here/Dockerfile" "$here/.dockerignore" "$here/pyproject.toml" "$here/uv.lock" "$here/.python-version" "$here/LICENSE" "$dest/"
cp "$here/deploy/space-readme.md" "$dest/README.md"
find "$dest/shaky" -name __pycache__ -type d -prune -exec rm -rf {} +
echo "copied into $dest; review with: git -C $dest status"
