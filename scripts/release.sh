#!/bin/bash

set -euo pipefail

REPO="Azure-Samples/azure-container-apps-sandboxes"

usage() {
  cat <<'EOF'
Usage: ./scripts/release.sh <version-tag> [artifact-directory]

Create a GitHub Release in Azure-Samples/azure-container-apps-sandboxes and upload build artifacts.

Arguments:
  version-tag         Release tag to create (for example: v0.1.0b1)
  artifact-directory  Directory containing artifacts. Defaults to the current directory.

Artifact selection:
  - All wheel files (*.whl), including CLI extension wheels that may not start with "azure"
  - Azure source distributions matching azure*.tar.gz

Examples:
  ./scripts/release.sh v0.1.0b1
  ./scripts/release.sh v0.1.0b1 ./dist
EOF
}

die() {
  echo "Error: $*" >&2
  exit 1
}

if [[ ${1:-} == "-h" || ${1:-} == "--help" ]]; then
  usage
  exit 0
fi

if [[ $# -lt 1 || $# -gt 2 ]]; then
  usage
  exit 1
fi

# Parse the requested release tag and optional artifact directory.
TAG="$1"
ARTIFACT_DIR="${2:-.}"

command -v gh >/dev/null 2>&1 || die "GitHub CLI (gh) is required but was not found in PATH."
[[ -n "$TAG" ]] || die "A version tag is required."
[[ -d "$ARTIFACT_DIR" ]] || die "Artifact directory does not exist: $ARTIFACT_DIR"

ARTIFACT_DIR="$(cd "$ARTIFACT_DIR" && pwd -P)"

# Upload every wheel so CLI extension wheels are included, and azure*.tar.gz source archives.
artifacts=()
while IFS= read -r -d '' file; do
  artifacts+=("$file")
done < <(find "$ARTIFACT_DIR" -maxdepth 1 -type f \( -name '*.whl' -o -name 'azure*.tar.gz' \) -print0)

if [[ ${#artifacts[@]} -eq 0 ]]; then
  die "No release artifacts found in $ARTIFACT_DIR. Expected wheel files (*.whl) and/or azure*.tar.gz files."
fi

# Fail early with a clear message instead of letting gh return a less specific error.
if gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
  die "A release with tag $TAG already exists in $REPO."
fi

echo "Creating release $TAG in $REPO"
echo "Using artifact directory: $ARTIFACT_DIR"
echo "Uploading ${#artifacts[@]} artifact(s):"
for artifact in "${artifacts[@]}"; do
  echo "  - $(basename "$artifact")"
done

# gh release create both creates the release and uploads each artifact passed on the command line.
if ! gh release create "$TAG" --repo "$REPO" --title "$TAG" --notes "Release $TAG" --prerelease "${artifacts[@]}"; then
  die "gh release create failed for tag $TAG."
fi

echo "Release $TAG created successfully."
