#!/usr/bin/env bash
# ── UN MUTANTE CONTRA LA SUITE DE PYTEST, CON LA APLICACIÓN COMPROBADA ────────
#
# Los arneses de mutación de este repo (`mutantes*.sh`) miden contra `humo.sh` y ya
# comprueban que el ancla casa: sin eso, un mutante que no se aplica se lee como
# «el gate lo aguanta» y certifica en verde lo contrario de lo que mide.
#
# Los mutantes contra `pytest` se venían escribiendo a mano, en bucles de shell, y
# ahí el control se perdía: el 2026-08-19, en un solo ciclo, DOS mutantes que daban
# «sobrevive» no estaban modificando el programa —el ancla llevaba escapes de más—,
# y un tercero sí se aplicaba pero su test miraba desde otra conexión y medía el
# aislamiento de SQLite en vez de la propiedad. Tres lecturas falsas seguidas.
#
# LA REGLA, ahora impuesta y no recordada:
#
#     aplicar → comprobar QUE CAMBIÓ → comprobar que compila → correr → clasificar
#
# Un mutante no cuenta como vivo ni como muerto hasta que consta que se aplicó.
#
# Uso — SÓLO por ancla textual:
#   tests/mutante-pytest.sh "<etiqueta>" <fichero> <ancla> [<reemplazo>]
#
# Sin reemplazo, borra el ancla. NO hay mutación por número de línea, y no es un
# olvido: el 2026-08-19 un `--borra-linea 528` salió «VIVO» porque alguien —yo—
# había movido código por arriba, así que borró OTRA línea, compiló igual y se
# leyó como «el gate lo aguanta». Un falso vivo es la misma mentira que un falso
# muerto. El ancla textual se rompe EN VOZ ALTA cuando deja de casar; un número no
# se entera. Borrar una línea es `ancla → ""`, que sí avisa.
#
# SALIDA Y CÓDIGO DE SALIDA (los tres distintos, para que CI no los confunda):
#   0  ✅ muerto        la suite se puso roja: el falsador existe
#   1  🔴 VIVO          la suite aguanta: falta falsador, o el mutante no muerde
#   2  ⛔ NO APLICADO   el experimento FALLÓ. No es un resultado neutral: no se ha
#                       medido nada, y contarlo como «vivo» inventa cobertura.
#
set -uo pipefail
cd "$(dirname "$0")/.."

ETQ="${1:?falta la etiqueta}"; FIC="${2:?falta el fichero}"
PY_BIN="${PY_BIN:-python3}"
COPIA="$(mktemp)"; cp "$FIC" "$COPIA"
trap 'cp "$COPIA" "$FIC"; rm -f "$COPIA"; find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null' EXIT

ANCLA="${3:?falta el ancla}"; NUEVO="${4-}"
# El ancla tiene que casar UNA vez. Cero = el mutante no existe; más de una =
# no se sabe cuál se tocó, y un mutante ambiguo no prueba nada.
python3 - "$FIC" "$ANCLA" "$NUEVO" <<'PY' || { printf '  ⛔ NO APLICADO  %s (el ancla casa 0 o >1 veces)\n' "$ETQ"; exit 2; }
import sys
f, o, r = sys.argv[1], sys.argv[2], sys.argv[3]
s = open(f).read()
if s.count(o) != 1:
    sys.exit(1)
open(f, "w").write(s.replace(o, r, 1))
PY

# ¿CAMBIÓ DE VERDAD? Es la comprobación que faltaba. Un reemplazo que deja el
# fichero idéntico (ancla == reemplazo, escapes mal puestos) pasaba por mutante.
if cmp -s "$COPIA" "$FIC"; then
  printf '  ⛔ NO APLICADO  %s (el fichero no cambió)\n' "$ETQ"; exit 2
fi
if [[ "$FIC" == *.py ]] && ! python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$FIC" 2>/dev/null; then
  printf '  ⛔ NO APLICADO  %s (no compila: el mutante no llega a correr)\n' "$ETQ"; exit 2
fi

find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null
SALIDA="$(PYTHONDONTWRITEBYTECODE=1 "$PY_BIN" -m pytest tests/pytest -p no:cacheprovider -q 2>&1 | tail -1)"
if printf '%s' "$SALIDA" | grep -q 'failed'; then
  printf '  ✅ muerto      %-44s %s\n' "$ETQ" "$SALIDA"
  exit 0
fi
printf '  🔴 VIVO        %-44s %s\n' "$ETQ" "$SALIDA"
printf '     ↳ el mutante SÍ se aplicó y compila: falta falsador, o no muerde. MIRAR EL EFECTO.\n'
exit 1
