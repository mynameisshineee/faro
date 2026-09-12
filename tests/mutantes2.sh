#!/usr/bin/env bash
# AUDITORÍA DE MUTANTES — ¿puede ponerse ROJA cada comprobación del humo?
#
# Un gate que nunca ha fallado no ha demostrado nada. El 2026-08-08, cuatro
# comprobaciones escritas ese mismo día salieron VERDES contra una imagen rota a
# propósito: colgaban de estado ajeno, el proceso que tomaba el cerrojo fallaba en
# silencio, y una columna de coste registraba ceros. Se arreglaron. Las otras 38
# nunca habían pasado por esa prueba.
#
# Este arnés rompe el código a propósito, zona por zona, y anota qué comprobaciones
# se enteran. Lo que NO se pone rojo con ningún mutante es sospechoso: o no mide lo
# que dice, o le falta el mutante que lo tocaría — y las dos cosas hay que saberlas.
#
# Uso:  bash tests/mutantes.sh [nombre-de-mutante]
# Salida: tests/mutantes-resultado2.txt (una línea por mutante, con sus rojos)
set -uo pipefail
cd "$(dirname "$0")/.."
SALIDA="tests/mutantes-resultado2.txt"
: > "$SALIDA"

# Cada mutante: nombre | fichero | texto original | texto roto
# El original se restaura SIEMPRE, incluso si el humo revienta a mitad.
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
  if ! python3 -c "import ast,sys; ast.parse(open('$fic').read())" 2>/dev/null && [[ "$fic" == *.py ]]; then
    cp "$copia" "$fic"; echo "SALTADO $nom (no compila)" | tee -a "$SALIDA"; return
  fi
  docker build -q -t "llminbox:mut" . >/dev/null 2>&1
  cp "$copia" "$fic"; rm -f "$copia"          # restaurar ANTES de correr: el humo usa la imagen
  local rojos
  rojos="$(IMAGEN=llminbox:mut PUERTO=$((8300 + RANDOM % 300)) bash tests/humo.sh 2>&1 \
           | grep -A1 '✗' | grep -oE '✗ .*' | sed 's/^✗ //' | sort -u | tr '\n' '|')"
  if [ -z "$rojos" ]; then
    echo "🔴 NINGÚN ROJO  $nom  ← el mutante pasó desapercibido" | tee -a "$SALIDA"
  else
    echo "✅ $nom → ${rojos}" | tee -a "$SALIDA"
  fi
}

echo "── segunda pasada: M4 arreglado + los que no llegaron ──"

mutante "M4-borrado-no-alarma" servicio.py \
  'if k not in vivos and r["ausente"] is None and not r["provisional"]]' \
  'if False]'

mutante "M5-censo-tira-cursores" servicio.py \
  'def huella_esquema() -> str:
    return hashlib.sha256(str(SCHEMA_V).encode()).hexdigest()[:16]' \
  'def huella_esquema() -> str:
    return hashlib.sha256((str(SCHEMA_V) + ",".join(sorted(lp.AGENTES))).encode()).hexdigest()[:16]'

mutante "M6-archivo-vuelve-a-la-bandeja" servicio.py \
  'INBOX_EXCLUIR = {x.strip() for x in os.environ.get("LLMINBOX_INBOX_EXCLUIR", "").split(",") if x.strip()}' \
  'INBOX_EXCLUIR = set()'

mutante "M7-provisional-desaparece" servicio.py \
  'if k not in vivos and r["ausente"] is None and r["provisional"]]' \
  'if False]'

echo "── fin. Resultado en $SALIDA ──"
cat "$SALIDA"
