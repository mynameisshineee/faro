# Collected dependency notices

This directory contains the exact 63 license texts collected for 41 npm packages
and 19 pinned Python packages. `manifest.json` records the bytes, package versions,
collection sources and five dependency input hashes. Texts are copied unchanged;
the source collection and verification are recorded in
`docs/evidence/2026-09-08-license-collection.json`.

Git attributes preserve their bytes. The http-parser MIT text includes an upstream
trailing space on its last line; the narrow whitespace exception retains that
verified text instead of editing a third-party notice to satisfy a style check.

The collection came from the resolved npm production dependency tree and existing
wheel caches. The uvicorn 0.39.0 text came from its exact pinned PyPI wheel. These
are collection sources, not assertions that a built image has been inspected.

Run the source check with:

```sh
python3 tools/check-third-party-texts.py --input-root . --notices-root third_party
```

The Docker build checks the same collection in an isolated stage and copies it to
`/usr/share/doc/llminbox/third-party/`. Removing, altering or emptying a recorded
text, changing a dependency input, or omitting a pinned Python package rejects the
collection. Dependency updates require a fresh reviewed collection and manifest.

The JSON result states coverage separately: Python package membership is compared
to the exact lock pins; npm membership is the declared collection only. An npm
omission from both the manifest and files is not detected by this check. Python
extras retain their distribution name; unsupported lock syntax, including
conditional markers, is rejected until its target environment is handled.

This is **collection and packaging evidence only**, not release qualification.
The npm set is the reviewed production closure, a conservative inventory; it is
not derived from emitted bundle modules. Gates still pending include the actual
web bundle closure, installed wheel metadata/text comparison in the final image,
base/OS and optional component scope, image extraction checks, reproducibility,
and the required human approvals. `THIRD_PARTY_NOTICES.md` remains an incomplete
inventory and does not claim those gates passed.
