#!/usr/bin/env bash
# Build the same OCI artefact twice, compare its bytes, then generate evidence.
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION="${LLMINBOX_VERSION:-0.9.0}"
PLATFORM="${LLMINBOX_PLATFORM:-linux/amd64}"
ARTEFACT="llminbox-${VERSION}.oci.tar"
SYFT_IMAGE="anchore/syft:v1.51.1@sha256:95fe0835e5bebc6f8b1f8acef68d47d63d594ef4c0f25c097ff853b23cbac74c"

[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9]+)*$ ]] || {
  echo "invalid LLMINBOX_VERSION: $VERSION" >&2; exit 2;
}
case "$PLATFORM" in
  linux/amd64|linux/arm64) ;;
  *) echo "unsupported LLMINBOX_PLATFORM: $PLATFORM" >&2; exit 2 ;;
esac

command -v docker >/dev/null || { echo "docker is required" >&2; exit 2; }
command -v shasum >/dev/null || { echo "shasum is required" >&2; exit 2; }
[ -z "$(git status --porcelain)" ] || {
  echo "refusing to build release evidence from a dirty tree" >&2; exit 2;
}
[ ! -e dist ] && [ ! -L dist ] || {
  echo "dist already exists; refusing to mix new evidence with stale bytes" >&2; exit 2;
}

# Verifica los textos antes de arrancar builders. La misma comprobación corre
# dentro del Dockerfile para cubrir builds que no pasan por este wrapper.
python3 tools/check-third-party-texts.py --input-root . --notices-root third_party

TMP="$(mktemp -d)"
BUILD_TAG="$(basename "$TMP" | tr -cd '[:alnum:]_-')"
BUILDER_ONE="llminbox-release-a-${BUILD_TAG}"
BUILDER_TWO="llminbox-release-b-${BUILD_TAG}"
BUILDERS=()
cleanup() {
  local builder
  for builder in "${BUILDERS[@]}"; do
    docker buildx rm "$builder" >/dev/null 2>&1 || true
  done
  rm -rf "$TMP"
}
trap cleanup EXIT
EPOCH="$(git show -s --format=%ct HEAD)"
VCS_REF="$(git rev-parse HEAD)"
export SOURCE_DATE_EPOCH="$EPOCH"

build_one() {
  local destination="$1"
  local builder="$2"
  docker buildx build \
    --builder "$builder" \
    --no-cache \
    --platform "$PLATFORM" \
    --build-arg "SOURCE_DATE_EPOCH=$EPOCH" \
    --build-arg "VCS_REF=$VCS_REF" \
    --provenance=false --sbom=false \
    --output "type=oci,dest=$destination,rewrite-timestamp=true" .
}

# Dos daemons BuildKit distintos, ambos sin caché de instrucciones. Comparar dos
# exports del mismo daemon/caché sólo prueba que la caché devuelve los mismos
# bytes; no que una reconstrucción independiente sea reproducible.
docker buildx create --name "$BUILDER_ONE" --driver docker-container >/dev/null
BUILDERS+=("$BUILDER_ONE")
docker buildx create --name "$BUILDER_TWO" --driver docker-container >/dev/null
BUILDERS+=("$BUILDER_TWO")
build_one "$TMP/one.oci.tar" "$BUILDER_ONE"
build_one "$TMP/two.oci.tar" "$BUILDER_TWO"
ONE="$(shasum -a 256 "$TMP/one.oci.tar" | awk '{print $1}')"
TWO="$(shasum -a 256 "$TMP/two.oci.tar" | awk '{print $1}')"
[ "$ONE" = "$TWO" ] || {
  echo "NON-REPRODUCIBLE: first=$ONE second=$TWO" >&2; exit 1;
}

STAGE="$TMP/stage"
mkdir "$STAGE"
install -m 0644 "$TMP/one.oci.tar" "$STAGE/$ARTEFACT"
docker run --rm -v "$STAGE:/work" "$SYFT_IMAGE" \
  scan "oci-archive:/work/$ARTEFACT" -o "cyclonedx-json=/work/sbom.cdx.json"
python3 tools/release-provenance.py "$STAGE/$ARTEFACT" "$STAGE/provenance.json" \
  --platform "$PLATFORM"
(
  cd "$STAGE"
  shasum -a 256 "$ARTEFACT" sbom.cdx.json provenance.json > SHA256SUMS
  printf 'UNSIGNED STRUCTURAL PRECHECK — NOT A RELEASE\n' > SHA256SUMS.asc
)
python3 tools/artefacto-gate.py --dir "$STAGE" --precheck
rm -f "$STAGE/SHA256SUMS.asc"
python3 tools/publish-release-stage.py "$STAGE" dist
printf 'reproducible OCI: %s  %s\n' "$ONE" "$ARTEFACT"
printf 'unsigned evidence ready in dist/; sign with tools/sign-release.sh\n'
