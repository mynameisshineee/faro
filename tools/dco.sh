#!/bin/bash
# dco.sh — el gate de Signed-off-by, corrible DONDE SE TRABAJA.
#
#     tools/dco.sh                    # HEAD contra su upstream, o contra main
#     tools/dco.sh <base>..<head>     # un rango explícito
#     tools/dco.sh <sha>              # ESE commit y ninguno más (no sus ancestros)
#
# Salidas:  0 todos firmados · 1 hay commits sin firmar · 2 mal uso / rango inválido
#           3 NO MEDIDO (el rango no contiene commits) — tiene casilla PROPIA a propósito:
#             mi primera versión imprimía «NO MEDIDO, no verde» y salía con rc=0, o sea
#             VERDE. Un «no medido» con rc=0 se suma como verde en cualquier `&&` y en
#             cualquier tabla. El mensaje decía una cosa y el código de salida otra.
#
# ⚠️ POR QUÉ ESTE FICHERO EXISTE, y no es una idea nueva:
# es EXACTAMENTE el caso que `tools/higiene.py:14-19` ya documentó para el detector
# de higiene — «el detector vivía dentro de .github/workflows/ci.yml, así que sólo
# corría en CI y sólo en main/PR […] Un gate que sólo corre donde no miras tiene el
# mismo valor que no tenerlo». El job de DCO (`ci.yml:354-370`) sigue viviendo en un
# heredoc del workflow y depende de `github.event.pull_request.*`, así que NO se puede
# correr a mano. La casa ya resolvió esta clase una vez; esto la aplica al que faltaba.
#
# 🩸 EL CASO REAL QUE LO MOTIVA (cto, 2026-09-09): integré la cura del manifiesto con un
# commit de merge SIN Signed-off-by, ENCIMA de un commit que sí lo llevaba, cuando lo que
# tocaba era un fast-forward. Lo corregí yo en ~3 min — pero no tenía que haber dependido
# de que yo lo mirara: este gate ya existía y no había forma de correrlo. Lo cazó
# @security leyendo el workflow, no el workflow.
#
# ⚠️ ES BASH Y NO PYTHON A PROPÓSITO, aunque el precedente (higiene.py) sea Python: el job
# original es bash y su regex es la que decide qué pasa. Traducirlo introduce diferencias
# de comportamiento que habría que volver a verificar; copiarlo, no. La fidelidad al gate
# que de verdad bloquea vale más aquí que la uniformidad de lenguaje.
#
# ⛔ LO QUE ESTE GATE **NO** COMPRUEBA, dicho para que nadie lo lea de más:
# que la firma sea CIERTA. `Signed-off-by` es una certificación (DCO) de quien la pone;
# este script comprueba que ESTÁ, con la forma que el CI exige. Que quien firma tenga
# derecho a hacerlo no lo puede medir ningún script.
set -uo pipefail

# ── LA REGEX SE EXTRAE DEL WORKFLOW, no se copia ──────────────────────────────
# v1 llevaba su propia copia con un comentario que decía «misma regex que ci.yml:361 —
# si una cambia, la otra miente». Lo cazó @security: **una constante que coincide en dos
# celdas es un ACUERDO, no un invariante** — hoy son idénticas, mañana es una convención
# que nadie mide. Aquí hay UNA sola fuente: el job que de verdad bloquea.
# ⛔ Y si no se puede extraer, ABORTA (rc=2). NO hay copia de respaldo a propósito: un
#    fallback silencioso a una regex vieja es exactamente el «verde que no midió nada»
#    que este fichero existe para no producir.
# 🩸 Y la saco del ÁRBOL QUE SE AUDITA, no del checkout. Mi v2 leía el ci.yml del working
# tree y eso mide la política de OTRA rama: medido el 2026-09-09, el ci.yml de
# `codex/llminbox-v0.9` tiene 120 L y CERO «Signed-off-by» — el job sólo existe en la rama
# de integración (allí, :354-370). Un gate que lee su propia regla del sitio equivocado es
# la misma clase que llevamos el día cazando: sujeto correcto, instrumento apuntando a otro.
RANGO="${1:-}"
# El arbol AUDITADO decide la regex, y en modo <sha> ese arbol ES el sha. La version
# anterior hacia `${RANGO##*..}` y, sobre una cadena SIN `..`, eso devuelve la cadena
# entera; la guarda que seguia la reescribia a HEAD justo en ese modo, asi que auditar
# un commit suelto leia la politica de la rama donde estas parado. Hallazgo de @security
# (MARK:security-el-modo-sha-del-gate-dco-lee-la-regex-de-head), corrido no razonado.
_UN_SOLO=0
case "$RANGO" in
  "")   _HEAD_REF=HEAD ;;
  *..*) _HEAD_REF="${RANGO##*..}" ;;
  *)    _HEAD_REF="$RANGO"; _UN_SOLO=1 ;;
esac
CI_YML_PATH=".github/workflows/ci.yml"
if ! _CI="$(git show "${_HEAD_REF}:${CI_YML_PATH}" 2>/dev/null)" || [ -z "$_CI" ]; then
  echo "dco.sh: no hay ${CI_YML_PATH} en '${_HEAD_REF}' — la regex vive ahí y NO la copio. NO MEDIDO." >&2
  exit 2
fi
RE_DCO="$(printf '%s\n' "$_CI" | grep -oE "grep -qiE '[^']+'" | grep -i 'signed-off-by' | head -1 |
          sed -E "s/^grep -qiE '//; s/'\$//")"
if [ -z "$RE_DCO" ]; then
  echo "dco.sh: '${_HEAD_REF}' no declara el job de Signed-off-by en ${CI_YML_PATH}." >&2
  echo "        ⇒ esa rama NO TIENE la regla; medir contra una regex prestada sería inventarla. NO MEDIDO." >&2
  exit 2
fi

if [ -z "$RANGO" ]; then
  # sin argumento: lo que esta rama añade sobre su upstream; si no hay, sobre main.
  BASE="$(git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null || true)"
  [ -z "$BASE" ] && BASE="$(git rev-parse --verify -q main >/dev/null 2>&1 && echo main || echo '')"
  if [ -z "$BASE" ]; then
    echo "dco.sh: no hay upstream ni 'main' — pasa un rango explícito (base..head)" >&2
    exit 2
  fi
  RANGO="${BASE}..HEAD"
fi

# Un rango vacío NO es un verde: es que no medí nada. Ésa es la casilla que al gate del
# workflow le falta, y es la misma clase que el `llmi doctor` mudo de esta mañana.
# 🩸 `git rev-list <sha>` lista TODOS LOS ANCESTROS, no un commit — y la cabecera de este
# fichero documenta «<sha>  # un solo commit». El modo nunca hizo lo que decía: auditaba
# la historia entera y la teñía de rojo por commits viejos ajenos al que preguntas. Salió
# al correr el falsador que @security pidió para SU hallazgo (el de la regex): su brazo ⊕
# daba rc=1 sobre un commit FIRMADO, y la causa no era su cura sino este segundo defecto.
if [ "$_UN_SOLO" = 1 ]; then
  if ! LISTA="$(git rev-list -n 1 "$RANGO" 2>/dev/null)" || [ -z "$LISTA" ]; then
    echo "dco.sh: rango inválido: $RANGO" >&2
    exit 2
  fi
elif ! LISTA="$(git rev-list "$RANGO" 2>/dev/null)"; then
  echo "dco.sh: rango inválido: $RANGO" >&2
  exit 2
fi

n=0; malos=0
while read -r sha; do
  [ -z "$sha" ] && continue
  n=$((n+1))
  # $RE_DCO sale del propio ci.yml (arriba): una sola fuente, sin acuerdo que mantener
  if ! git log -1 --format='%B' "$sha" | grep -qiE "$RE_DCO"; then
    echo "::error::$(git log -1 --format='%h %s' "$sha") - falta Signed-off-by"
    malos=$((malos+1))
  fi
done <<< "$LISTA"

if [ "$n" -eq 0 ]; then
  echo "dco.sh: 0 commits en '$RANGO' — NO MEDIDO (rc=3), no verde." >&2
  exit 3
fi

if [ "$malos" -gt 0 ]; then
  echo "$malos de $n commit(s) sin firmar. Ver CONTRIBUTING.md y DCO."
  exit 1
fi
echo "los $n commit(s) de '$RANGO' llevan Signed-off-by ✓"
