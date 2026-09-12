#!/usr/bin/env bash
# Final offline step. The committed trust root decides which key may sign.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f release-trust.json ] || {
  echo "release-trust.json is missing; perform the documented key ceremony first" >&2
  exit 2
}
FINGERPRINT="$(python3 -c 'import json; print(json.load(open("release-trust.json"))["fingerprint"])')"
[ -n "$FINGERPRINT" ] || { echo "empty release fingerprint" >&2; exit 2; }
[ -f dist/SHA256SUMS ] && [ ! -L dist/SHA256SUMS ] || {
  echo "dist/SHA256SUMS must be a regular non-symlink file" >&2; exit 2;
}
[ ! -e dist/SHA256SUMS.asc ] && [ ! -L dist/SHA256SUMS.asc ] || {
  echo "dist/SHA256SUMS.asc already exists; refusing to overwrite it" >&2; exit 2;
}
SIGN_TMP="$(mktemp -d dist/.sign.XXXXXX)"
cleanup() { rm -rf "$SIGN_TMP"; }
trap cleanup EXIT
gpg --batch --local-user "$FINGERPRINT" --detach-sign --armor \
  --output "$SIGN_TMP/SHA256SUMS.asc" dist/SHA256SUMS
# hard-link is an atomic create-without-overwrite on the same filesystem
ln "$SIGN_TMP/SHA256SUMS.asc" dist/SHA256SUMS.asc
cleanup
trap - EXIT
python3 tools/artefacto-gate.py --dir dist
