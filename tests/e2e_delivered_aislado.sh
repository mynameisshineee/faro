#!/usr/bin/env bash
# E2E del recibo DELIVERED contra un ledger TEMPORAL — cero contacto con ledgers vivos.
#
# Lo pidió @harness para G6. Es más simple de lo que esperaba: no hace falta ni servicio
# ni base. `publicar.py` escribe en la ruta que le da `LLMI_LEDGER`, y `ledger_parse.parse`
# lee cualquier fichero. Así que generar → publicar → recuperar → revalidar corre entero
# sobre un temporal.
#
#   tests/e2e_delivered_aislado.sh
#
# ⚠️ El arranque COMPRUEBA que el ledger es temporal antes de escribir. Un E2E que
# publique en un ledger vivo no es una prueba, es una entrada falsa en el canon de alguien.
set -euo pipefail
cd "$(dirname "$0")/.."
RAIZ="$(pwd)"

TMP="$(mktemp -d -t e2e-delivered-XXXXXX)"
trap 'rm -rf "$TMP"' EXIT
LEDGER="$TMP/LEDGER.md"
# EL AISLAMIENTO VA POR EL MANIFIESTO, no por una ruta. `LLMI_LEDGER` es el NOMBRE del
# ledger y se valida contra los montados —hay una guarda deliberada: «el carril manda; no
# hay argumento para saltárselo»—. Así que se monta un manifiesto temporal con un ledger
# temporal y se pide ese nombre. Lo deduje leyendo el código y me equivoqué; esto está
# EJECUTADO.
mkdir -p "$TMP/.llminbox-state"
MOUNTS="$TMP/.llminbox-state/mounts.json"
printf '{"e2e-aislado": "%s"}' "$LEDGER" > "$MOUNTS"
case "$LEDGER" in /tmp/*|/var/folders/*|"${TMPDIR:-/tmp}"*) ;; *)
  echo "⛔ el ledger no es temporal: $LEDGER — abortando antes de escribir"; exit 1 ;; esac
: > "$LEDGER"

CAMPOS=(owner repository branch commit_sha artifact falsifier reviewer gate_result integration_state)
cuerpo() {                       # $1 = campo a OMITIR (vacío = ninguno)
  for c in "${CAMPOS[@]}"; do
    [ "$c" = "${1:-}" ] && continue
    printf -- '- %s: valor-de-%s\n' "$c" "$c"
  done
}

publica() {                      # $1 = cuerpo por stdin, $2 = tipo
  LLMI_DIR="$RAIZ" LLMI_MOUNTS="$MOUNTS" LLMI_LEDGER=e2e-aislado LLMI_YO=em LLMI_A=qa \
  LLMI_TIPO="$2" LLMI_TITULAR="recibo e2e" \
  .venv-test/bin/python publicar.py <<< "$1"
}

valida() {                       # cuenta campos presentes COMO CAMPO en la última entrada
  .venv-test/bin/python - "$LEDGER" <<'PY'
import re, sys
sys.path.insert(0, ".")
import ledger_parse as lp
ents, _ = lp.parse(sys.argv[1])
if not ents:
    print("0 SIN-ENTRADA"); raise SystemExit
e = ents[-1]
CAMPOS = ["owner","repository","branch","commit_sha","artifact","falsifier","reviewer",
          "gate_result","integration_state"]
txt = (e.head or "") + "\n" + (e.text or "")
n = sum(1 for c in CAMPOS
        if re.search(rf"(?:^|\n)\s*(?:[-*]\s*)?\**{re.escape(c)}\**\s*:", txt, re.I))
print(f"{n} {lp.canonical_tipo(e.raw_tipo)}")
PY
}

fallos=0
di() { printf '  %s\n' "$1"; }

# ⊕ EL CAMINO BUENO
if ! publica "$(cuerpo)" DELIVERED >/dev/null 2>&1; then
  di "⛔ publicar un DELIVERED falla — ¿está en CANON_TIPOS y desplegado?"; exit 1
fi
read -r N T <<< "$(valida)"
di "⊕ generar→publicar→recuperar→revalidar: $N/9 campos · tipo=$T"
[ "$N" = 9 ] || { di "⛔ esperaba 9/9"; fallos=$((fallos+1)); }
[ "$T" = DELIVERED ] || { di "⛔ el tipo recuperado es $T, no DELIVERED"; fallos=$((fallos+1)); }

# ⊖ OMITIR CADA CAMPO tiene que bajar la cuenta — si no, el validador no mira ese campo
for c in "${CAMPOS[@]}"; do
  : > "$LEDGER"
  publica "$(cuerpo "$c")" DELIVERED >/dev/null 2>&1
  read -r N _ <<< "$(valida)"
  if [ "$N" != 8 ]; then di "⛔ omitiendo '$c' la cuenta es $N, no 8: no se está mirando"; fallos=$((fallos+1)); fi
done
di "⊖ omitir cada uno de los 9 campos: comprobado"

# ⊖ CONTROL DEL ARNÉS: un tipo inválido NO puede publicarse. Si pasa, el E2E entero
# estaría midiendo una puerta abierta y los ⊕ de arriba no dirían nada.
: > "$LEDGER"
if publica "$(cuerpo)" NO_EXISTE_ESTE_TIPO >/dev/null 2>&1; then
  di "⛔ un tipo inventado se publicó: la puerta no gatea y este E2E no mide nada"
  fallos=$((fallos+1))
else
  di "⊖ un tipo inventado se rechaza: la puerta gatea"
fi

di ""
[ "$fallos" -eq 0 ] && { di "✅ E2E DELIVERED: todo verde, en $TMP"; exit 0; }
di "⛔ $fallos fallo(s)"; exit 1
