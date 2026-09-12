#!/usr/bin/env bash
# SBOM del artefacto + su falsador. NO construye la imagen de despliegue: genera el
# inventario y comprueba que cubre lo que de verdad cambia.
#
# El falsador que importa: un SBOM que sólo lista paquetes del SISTEMA OPERATIVO deja
# fuera `fastapi`, `uvicorn`, `pydantic` y `starlette` — que son justo las que
# `requirements.lock` fija y las que un aviso de seguridad va a nombrar. Un inventario
# que no cubre la superficie que cambia es un inventario que tranquiliza sin informar.
set -uo pipefail
cd "$(dirname "$0")/.."
: "${DOCKER:=docker}"
DEST="${1:-./sbom}"
mkdir -p "$DEST" || exit 1

"$DOCKER" buildx build --sbom=true --output "type=local,dest=$DEST" . || {
  echo "⛔ el build con SBOM falló" >&2; exit 1; }

F="$(find "$DEST" -name '*.spdx.json' | head -1)"
[ -n "$F" ] || { echo "⛔ no se generó ningún SPDX" >&2; exit 1; }

python3 - "$F" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
pk = d.get("packages", [])
por_nombre = {p.get("name", "").lower(): p.get("versionInfo") for p in pk}
faltan = [n for n in ("fastapi", "uvicorn", "pydantic", "starlette") if n not in por_nombre]
print(f"paquetes en el SBOM: {len(pk)}")
for n in ("fastapi", "uvicorn", "pydantic", "starlette"):
    print(f"  {n:10s} {por_nombre.get(n, '⛔ AUSENTE')}")
if faltan:
    sys.exit(f"⛔ el SBOM no cubre la superficie python: faltan {faltan}. "
             "Un inventario de sólo-SO no sirve para este artefacto.")
print("✓ el SBOM cubre los paquetes python del lock")
PY
