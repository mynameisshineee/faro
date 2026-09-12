#!/usr/bin/env bash
# ── MUTANTES DEL ANDAMIO (no del producto) ────────────────────────────────────
#
# Los otros cuatro arneses de mutación de este repo tocan `servicio.py` (16 mutantes)
# y `ledger_parse.py` (1). Ninguno toca el ANDAMIO — y ahí vivía el defecto más caro
# del proyecto: el 2026-08-11 se supo que ocho comprobaciones del humo llevaban DOS
# DÍAS certificando en verde una reconstrucción del índice que nunca ocurría, porque
# los pasos de preparación reventaban en Linux y el bloque seguía corriendo igual.
# Ningún mutante podía verlo: un mutante del producto no encuentra una puerta que no
# se llega a ejecutar.
#
# Esto rompe las guardas de `guardas.lib.sh` una a una y exige que `tests/guardas.sh`
# lo diga. Es la prueba de la prueba de la prueba, y es donde se para: `guardas.sh`
# se juzga con contadores propios, no con las guardas que mide.
#
# 🔴 NINGÚN ROJO aquí significa una de dos —la prueba es teatro, o el mutante no
# llega—, y sólo se distinguen mirando el efecto. Igual que en los otros arneses.
set -uo pipefail
cd "$(dirname "$0")/.."
LIB=tests/guardas.lib.sh
. tests/guardas.lib.sh
aviso_imagen_rancia
ORIG="$(mktemp)"; cp "$LIB" "$ORIG"
trap 'cp "$ORIG" "$LIB"; rm -f "$ORIG"' EXIT
VIVOS=0
# Un mutante que NO SE APLICA no mide nada, y hasta ahora eso salía por pantalla y
# NO por el código de salida: con las 5 anclas obsoletas este arnés imprimía
# «mutantes del andamio: todos cazados» y salía 0. Verde en CI habiendo comprobado
# CERO. Es el defecto que este propio fichero existe para cazar, en él mismo.
SALTADOS=0

mutante() {                     # mutante <nombre> <ancla> <reemplazo>
  local nom="$1"
  python3 - "$LIB" "$2" "$3" <<'PY' || { cp "$ORIG" "$LIB"; echo "  SALTADO $nom (el ancla no casa)"; SALTADOS=$((SALTADOS+1)); return; }
import sys
f, o, r = sys.argv[1], sys.argv[2], sys.argv[3]
s = open(f).read()
if o not in s: sys.exit(1)
open(f, 'w').write(s.replace(o, r, 1))
PY
  if ! bash -n "$LIB" 2>/dev/null; then
    cp "$ORIG" "$LIB"; echo "  SALTADO $nom (no compila)"; SALTADOS=$((SALTADOS+1)); return
  fi
  local rojo
  rojo="$(IMAGEN="${IMAGEN:-llminbox:test}" bash tests/guardas.sh 2>&1 | grep -m1 '^  ✗' | sed 's/^  ✗ //')"
  cp "$ORIG" "$LIB"
  if [ -n "$rojo" ]; then
    printf '  ✅ %-42s → %s\n' "$nom" "$rojo"
  else
    printf '  🔴 NINGÚN ROJO  %-30s ← teatro, o el mutante no llega: MIRAR EL EFECTO\n' "$nom"
    VIVOS=$((VIVOS+1))
  fi
}

echo "── mutantes del andamio ──"

# La avería del 2026-08-09 en una línea: `set -uo pipefail` SIN `-e`, un paso de
# preparación que revienta, y el bloque midiendo sobre una base intacta.
mutante "paso() nunca falla" \
  'if "$@"; then return 0; fi' \
  'if "$@"; then return 0; fi; return 0'

# Si «no medido» dejara de contar, romper el transporte apagaría la suite entera.
mutante "no_medido() no cuenta como fallo" \
  'FALLOS=$((FALLOS+1))
}' \
  'FALLOS=$((FALLOS+0))
}'

# Las diez esperas originales se agotaban en silencio y lo que venía después medía
# un servicio ausente, reportándolo como valores incorrectos del producto.
mutante "esperar_salud() no sabe agotarse" \
  'no_medido "$et" "no contestó a /health en $n intentos' \
  'return 0; no_medido "$et" "no contestó a /health en $n intentos'

# La guarda que el 2026-08-11 no existía: sin ella, un artefacto del test (ficheros
# de otro uid junto a la base) se publica como avería del arranque del producto.
mutante "ajenos_en_datos() calla siempre" \
  '  [ -z "$malos" ] && return 0' \
  '  return 0'

# Las dos elecciones «de sentido común» del uid fallaron, cada una en la máquina de
# otro. Este mutante es la que se probó primero: usar el `USER` de la imagen.
mutante "en_contenedor() ignora al dueño de la base" \
  'docker run --rm ${dueno:+-u "$dueno"}' \
  'docker run --rm'

echo
# DECLARADOS vs EJERCITADOS. Sin esta cuenta, «todos cazados» significaba «ninguno
# sobrevivió», que es cierto también cuando NINGUNO SE PROBÓ. Un arnés que no puede
# distinguir «todo bien» de «no medí nada» da la señal contraria justo cuando más
# falta hace: después de un refactor que dejó las anclas obsoletas.
DECLARADOS=$(grep -c '^mutante ' "$0")
EJERCITADOS=$((DECLARADOS - SALTADOS))
printf 'mutantes del andamio: %s declarados · %s ejercitados · %s saltados · %s vivo(s)\n' \
  "$DECLARADOS" "$EJERCITADOS" "$SALTADOS" "$VIVOS"
[ "$SALTADOS" -gt 0 ] && echo "  🔴 un mutante SALTADO no mide nada: su ancla ya no casa con el código" >&2
[ "$EJERCITADOS" -eq 0 ] && echo "  🔴 CERO mutantes ejercitados: este arnés no ha comprobado NADA" >&2
exit $(( VIVOS > 0 || SALTADOS > 0 ))
