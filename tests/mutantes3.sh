#!/usr/bin/env bash
# TERCERA PASADA — las zonas que ningún mutante alcanzaba.
#
# Las dos primeras pasadas pusieron rojas 30 de 64 comprobaciones y no encontraron
# un solo gate de teatro: todo lo que un mutante alcanza, muerde. Lo que quedó
# abierto no eran gates ciegos, eran ZONAS SIN MUTANTE — y «ningún rojo» tiene esas
# dos causas, que sólo se distinguen mirando el efecto.
#
# Esta pasada ataca las que faltaban: la documentación cerrada, la resolución de
# nombre de ledger en las citas, los informes de sólo-lectura, el techo de salud del
# barrido y la reconstrucción del índice.
set -uo pipefail
cd "$(dirname "$0")/.."
SALIDA="tests/mutantes-resultado3.txt"
: > "$SALIDA"

mutante() {
  local nom="$1" fic="$2" ori="$3" rot="$4"
  local copia; copia="$(mktemp)"
  cp "$fic" "$copia"
  python3 - "$fic" "$ori" "$rot" <<'PY' || { cp "$copia" "$fic"; echo "SALTADO $nom (el ancla no casa)" | tee -a "$SALIDA"; return; }
import sys
f,o,r = sys.argv[1], sys.argv[2], sys.argv[3]
s = open(f).read()
if o not in s:
    sys.exit(1)
open(f,'w').write(s.replace(o, r, 1))
PY
  if [[ "$fic" == *.py ]] && ! python3 -c "import ast; ast.parse(open('$fic').read())" 2>/dev/null; then
    cp "$copia" "$fic"; echo "SALTADO $nom (no compila)" | tee -a "$SALIDA"; return
  fi
  docker build -q -t "llminbox:mut3" . >/dev/null 2>&1
  cp "$copia" "$fic"; rm -f "$copia"
  local rojos
  rojos="$(IMAGEN=llminbox:mut3 PUERTO=$((8600 + RANDOM % 200)) bash tests/humo.sh 2>&1 \
           | grep -oE '✗ .*' | sed 's/^✗ //' | sort -u | tr '\n' '|')"
  if [ -z "$rojos" ]; then
    echo "🔴 NINGÚN ROJO  $nom  ← o el gate es ciego, o el mutante no llega: MIRAR EL EFECTO" | tee -a "$SALIDA"
  else
    echo "✅ $nom → ${rojos}" | tee -a "$SALIDA"
  fi
}

echo "── tercera pasada ──"

# La documentación interactiva abierta fue un agujero real: servía el canon entero
# sin token. Las comprobaciones 4 y 5 existen por eso y nunca se habían falsado.
mutante "M8-docs-abiertos" servicio.py \
  '              docs_url=None, redoc_url=None, openapi_url=None)' \
  '              )'

# La resolución del nombre de ledger en una cita: `64bis-wiki/WIKI-QUEUE.md` y
# `64bis-wiki` comparten prefijo, y confundirlos manda una cita al ledger de al lado.
mutante "M9-cita-resuelve-por-prefijo" servicio.py \
  '                cands = [n for n in conocidos
                         if n.lower() in pista or pista.startswith(n.lower())]' \
  '                cands = [n for n in conocidos if n.lower() in pista]'

# El techo de salud que decae: sin él, `/health` daba rojo con TODO sano en 3 de
# cada 8 muestras, y un hook de arranque anunciaba «llminbox no responde».
mutante "M11-techo-de-salud-no-decae" servicio.py \
  '                         duracion_max=max(dur, SALUD["duracion_max"] * 0.95))' \
  '                         duracion_max=0.0)'

# La cura de la corrupción: el 2026-08-01 el servicio DETECTÓ el índice roto, lo
# anotó como «ledger roto» y se rindió. `entries` es derivada y se re-deriva sola.
mutante "M12-la-corrupcion-no-se-cura" servicio.py \
  'def reconstruir_indice(motivo: str) -> bool:' \
  'def reconstruir_indice(motivo: str) -> bool:
    return False'

echo "── fin ──"
cat "$SALIDA"
