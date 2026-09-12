#!/usr/bin/env bash
# Banco de pruebas de V8 contra un servicio REAL, con ledgers y base TEMPORALES.
#
# Lo monto para @harness (P6) para que su eval de aceptación no tenga que reconstruir
# el entorno ni apuntar a producción. YO NO ESCRIBO SU EVAL —sería graduarme a mí
# mismo— : esto levanta el banco y le deja las credenciales y la URL. Lo que mida con
# ellas es suyo.
#
#   tests/v8_banco_aislado.sh            # levanta, imprime el entorno y espera Ctrl-C
#   tests/v8_banco_aislado.sh --humo     # levanta, corre 4 comprobaciones y se va
#
# ⚠️ CERO CONTACTO CON PRODUCCIÓN. Base, ledgers, roster, carriles y mapa de
# credenciales van a un temporal, y el servicio corre como PROCESO, no en el contenedor
# de la flota: `uvicorn` en un puerto libre. Se comprueba antes de arrancar que la ruta
# de la base es temporal — un banco que apunte a la base viva no es un banco.
set -euo pipefail
cd "$(dirname "$0")/.."
RAIZ="$(pwd)"
PY="${PY_BIN:-$RAIZ/.venv-test/bin/python}"

TMP="$(mktemp -d -t v8-banco-XXXXXX)"
LOG="$TMP/servicio.log"
PID=""
limpia() { [ -n "$PID" ] && kill "$PID" 2>/dev/null || true; rm -rf "$TMP"; }
trap limpia EXIT

case "$TMP" in /tmp/*|/var/folders/*|"${TMPDIR:-/tmp}"*) ;; *)
  echo "⛔ el banco no es temporal: $TMP — abortando antes de arrancar"; exit 1 ;; esac

# ── censo, ledger y carril de mentira, pero con la FORMA de los de verdad ──────
cat > "$TMP/roster.json" <<'JSON'
{"agentes": [{"nombre": "backend", "humano": "operador", "clave": "", "rol": "be"},
             {"nombre": "backend-biklabs", "humano": "operador", "clave": "", "rol": "be"},
             {"nombre": "cto-A", "humano": "operador", "clave": "", "rol": "cto"}],
 "humanos": [{"nombre": "operador", "alias": ["Operador"]}],
 "difusion": ["equipo"]}
JSON
LEDGER="$TMP/DEMO-LEDGER.md"
printf '### [cto-A → backend · REQUEST] uno\ncuerpo uno\n' > "$LEDGER"
printf '{"demo-ledger": "%s"}' "$LEDGER" > "$TMP/mounts.json"
printf 'carril\tledger_path\twiki_path\testado\tnotas\ndemo\t%s\t-\tcompleto\t-\n' \
  "$LEDGER" > "$TMP/carriles.tsv"

# ── las credenciales: DOS roles distintos, que es lo que hace falta para el ⊖ ──
CRED_BE="banco-be-$(  "$PY" -c 'import secrets;print(secrets.token_urlsafe(16))')"
CRED_CTO="banco-cto-$("$PY" -c 'import secrets;print(secrets.token_urlsafe(16))')"
TOKEN_FLOTA="banco-flota-$("$PY" -c 'import secrets;print(secrets.token_urlsafe(16))')"
"$PY" - "$TMP/credenciales.json" "$CRED_BE" "$CRED_CTO" <<'PYEOF'
import json, sys
ruta, be, cto = sys.argv[1], sys.argv[2], sys.argv[3]
json.dump({be: {"rol": "be", "carril": "demo"},
           cto: {"rol": "cto", "carril": "demo"}}, open(ruta, "w"))
PYEOF

PUERTO="$("$PY" -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
export LLMINBOX_DB="$TMP/llminbox.sqlite" LLMINBOX_TOKEN="$TOKEN_FLOTA" \
       LLMINBOX_ROSTER="$TMP/roster.json" LLMINBOX_MOUNTS_JSON="$TMP/mounts.json" \
       LLMINBOX_CARRILES="$TMP/carriles.tsv" LLMINBOX_CREDENCIALES="$TMP/credenciales.json" \
       LLMINBOX_LEDGERS="demo-ledger=$LEDGER"
"$PY" -m uvicorn servicio:app --host 127.0.0.1 --port "$PUERTO" >"$LOG" 2>&1 &
PID=$!
API="http://127.0.0.1:$PUERTO"
for _ in $(seq 1 60); do curl -sf -m 1 "$API/health" >/dev/null 2>&1 && break; sleep 0.25; done
curl -sf -m 2 "$API/health" >/dev/null 2>&1 || { echo "⛔ el banco no arrancó:"; cat "$LOG"; exit 1; }

# ── CONTRA QUÉ SHA CORRE ESTO, Y SI EL ÁRBOL ESTÁ SUCIO ───────────────────────
# Un gate de aceptación que no dice qué versión midió no acredita la que se despliega.
# Pasó de verdad el 2026-09-04: @harness cerró P6 en verde a las 16:35Z y yo empujé
# curas a las 16:40Z — su verde certificaba un SHA que ya no existía, y lo tuve que
# avisar yo. Ahora el banco lo dice solo, para que el resultado se ancle sin que nadie
# se acuerde de mirarlo.
SHA="$(git rev-parse --short=10 HEAD 2>/dev/null || echo desconocido)"
SUCIO=""
git diff --quiet 2>/dev/null || SUCIO=" ⚠️ ÁRBOL SUCIO: hay cambios SIN COMMITEAR, así que este SHA NO describe lo que estás midiendo"
git diff --cached --quiet 2>/dev/null || SUCIO=" ⚠️ ÁRBOL SUCIO: hay cambios EN EL ÍNDICE, así que este SHA NO describe lo que estás midiendo"

echo "── banco V8 en pie ────────────────────────────────────────────────────────"
echo "  MIDIENDO       $SHA$SUCIO"
echo "  API            $API"
echo "  credencial be  $CRED_BE      (puede actuar como 'backend' y 'backend-biklabs')"
echo "  credencial cto $CRED_CTO     (puede actuar como 'cto-A')"
echo "  token flota    $TOKEN_FLOTA  (autentica, NO identifica: se anota y se sirve)"
echo "  cabecera       X-Llminbox-Token"
echo "  base/ledger    $TMP  (se borra al salir)"
echo

if [ "${1:-}" != "--humo" ]; then
  echo "Listo para tu eval. Ctrl-C para desmontarlo."
  wait "$PID"; exit 0
fi

# ── humo MÍNIMO: sólo comprueba que el banco DISCRIMINA, no que V8 esté bien ───
# La diferencia importa: si estas cuatro no dan lo esperado, el banco está roto y
# cualquier eval que corra encima mide otra cosa. NO es la eval de aceptación.
F=0
mide() { curl -s -o /dev/null -w '%{http_code}' -m 5 -H "X-Llminbox-Token: $2" \
         -H 'Content-Type: application/json' -X POST "$API$3" -d "$4"; }
comp() { [ "$1" = "$2" ] && printf '  ✓ %s\n' "$3" || { printf '  ✗ %s — esperado %s, obtenido %s\n' "$3" "$2" "$1"; F=$((F+1)); }; }

comp "$(mide x "$CRED_BE"    /claim '{"tema":"suyo","agent":"backend","rol":"ejecuta"}')" 200 \
     "la credencial de be coge trabajo como backend"
comp "$(mide x "$CRED_BE"    /claim '{"tema":"ajeno","agent":"cto-A","rol":"ejecuta"}')" 403 \
     "y NO puede cogerlo como cto-A"
comp "$(mide x "$CRED_BE"    /inbox/cto-A/leido '{"hasta":{}}')" 403 \
     "ni mover el cursor de cto-A"
comp "$(mide x "$TOKEN_FLOTA" /claim '{"tema":"viejo","agent":"cto-A","rol":"ejecuta"}')" 200 \
     "el token de flota SIGUE sirviendo (fase 1) — y queda anotado"

ANOT="$(curl -sf -m 5 -H "X-Llminbox-Token: $CRED_BE" "$API/health" \
        | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["v8"]["sin_identidad_24h"])')"
comp "$ANOT" 1 "y /health lo cuenta (sin_identidad_24h)"

echo
echo "  medido contra: $SHA$SUCIO"
[ "$F" -eq 0 ] && echo "banco V8: DISCRIMINA (listo para la eval de P6)" || echo "banco V8: $F fallo(s) — NO corras la eval encima"
exit $((F > 0))
