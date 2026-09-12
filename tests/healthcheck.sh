#!/usr/bin/env bash
# El healthcheck de la IMAGEN distingue «responde» de «está sano».
#
# No prueba una copia del comando: lo EXTRAE del Dockerfile y lo corre. Un test
# sobre una copia sigue verde el día que alguien cambia el original — que es
# exactamente lo que había pasado aquí: `docker-compose.yml` miraba el cuerpo y el
# `HEALTHCHECK` de la imagen sólo el código HTTP, así que quien corriera la imagen
# sin compose tenía un «healthy» sobre un servicio mudo.
#
# Lo único que se sustituye del comando extraído es el PUERTO, para no tocar el
# 8077 de una instalación viva. La lógica que se prueba es la que se envía.
set -uo pipefail
set +m          # sin avisos de job control al matar el servidor de prueba
cd "$(dirname "$0")/.." || exit 2
PUERTO="${PUERTO_TEST:-8791}"

CMD_IMG="$(python3 tools/extrae-healthcheck.py)" || { echo "✗ no pude extraer el healthcheck: NO PUEDO MEDIR" >&2; exit 2; }
[ -n "$CMD_IMG" ] || { echo "✗ healthcheck vacío" >&2; exit 2; }

servidor() {  # servidor <cuerpo-json> -> deja el PID en $SRV
  python3 - "$PUERTO" "$1" >/dev/null 2>&1 &
  SRV=$!
  sleep 0.7
} <<'PY'
import http.server, json, sys
cuerpo = sys.argv[2].encode()
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)            # SIEMPRE 200: ese es el punto del test
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers(); self.wfile.write(cuerpo)
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
PY

correr() { python3 -c "${CMD_IMG//8077/$PUERTO}"; }

mal=0
servidor '{"ok": true, "ledgers": 3}'
correr; rc=$?
kill "$SRV" 2>/dev/null; wait "$SRV" 2>/dev/null
if [ $rc -eq 0 ]; then echo "  ✓ 200 con ok:true  -> sano   (rc=0)"
else echo "  ✗ 200 con ok:true debería ser sano, rc=$rc"; mal=$((mal+1)); fi

servidor '{"ok": false, "ledgers": 0}'
correr; rc=$?
kill "$SRV" 2>/dev/null; wait "$SRV" 2>/dev/null
if [ $rc -ne 0 ]; then echo "  ✓ 200 con ok:FALSE -> enfermo (rc=$rc)"
else echo "  ✗ 200 con ok:false salió SANO: el healthcheck sólo mira el código"; mal=$((mal+1)); fi

servidor '{"ledgers": 0}'
correr; rc=$?
kill "$SRV" 2>/dev/null; wait "$SRV" 2>/dev/null
if [ $rc -ne 0 ]; then echo "  ✓ 200 SIN campo ok -> enfermo (rc=$rc)"
else echo "  ✗ 200 sin campo ok salió SANO"; mal=$((mal+1)); fi

# Y que los dos healthcheck del repo digan lo mismo.
if grep -q "d.get('ok')" Dockerfile && grep -q "d.get('ok')" docker-compose.yml; then
  echo "  ✓ imagen y compose comprueban el mismo campo"
else
  echo "  ✗ imagen y compose NO comprueban lo mismo"; mal=$((mal+1))
fi

echo
[ "$mal" -eq 0 ] && { echo "healthcheck: correcto."; exit 0; }
echo "$mal fallo(s) en el healthcheck." >&2; exit 1
