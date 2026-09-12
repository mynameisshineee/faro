#!/usr/bin/env bash
# ¿Qué recibe DE VERDAD el daemon cuando alguien construye la imagen?
#
# Dos preguntas distintas, y las dos hacen falta:
#   ① ¿las REGLAS de `.dockerignore` excluyen lo que dicen excluir?
#   ② ¿el contexto REAL de este repo contiene algo que no debería?
#
# ⚠️ NADA DE ESTO ESCRIBE NI BORRA EN EL ÁRBOL DE TRABAJO.
# La primera versión de este fichero plantaba los señuelos EN EL REPO y registraba
# el `trap` de limpieza ANTES de comprobar si ya existían. En un worktree con un
# `.env` o un `roster.json` de verdad, el pre-check salía y el trap los borraba con
# `rm -f`: un test que destruye justo los ficheros que existe para proteger.
# Ahora ① se corre contra un contexto SINTÉTICO dentro de `mktemp` —al que sólo se
# copia `.dockerignore`— y ② es de sólo lectura. La limpieza toca únicamente el
# temporal y la imagen/contenedor creados con un id único de esta corrida.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
REPO="$PWD"

command -v docker >/dev/null || { echo "✗ no hay docker: NO PUEDO MEDIR el contexto." >&2
                                  echo "  Un gate que no puede medir no está en verde, está ciego." >&2
                                  exit 2; }

ID="ctxprobe-$$-$(date +%s)"
TMP="$(mktemp -d)" || exit 2
limpia() { rm -rf "$TMP"; docker rmi -f "$ID" >/dev/null 2>&1 || true; }
trap limpia EXIT

# Huella ANTES: este test se juzga también por no haber tocado nada.
HUELLA_ANTES="$(cd "$REPO" && git status --porcelain 2>/dev/null; \
                shasum -a 256 "$REPO/.dockerignore" "$REPO/servicio.py" 2>/dev/null)"

printf 'FROM scratch\nCOPY . /ctx\nCMD ["/ctx"]\n' > "$TMP/probe.Dockerfile"

lista_contexto() {  # $1 = directorio de contexto -> stdout: rutas relativas
  local ctx="$1" cid
  docker build -q --no-cache -f "$TMP/probe.Dockerfile" -t "$ID" "$ctx" >/dev/null 2>&1 || return 2
  cid="$(docker create "$ID")" || return 2
  docker export "$cid" | tar -t 2>/dev/null | grep '^ctx/' | sed 's|^ctx/||' | grep -v '/$'
  local rc=${PIPESTATUS[0]}
  docker rm -f "$cid" >/dev/null 2>&1
  return $rc
}

malos=0
# ═══ ① LAS REGLAS, sobre un contexto sintético ═══════════════════════════════
SIN="$TMP/sintetico"; mkdir -p "$SIN/web"
cp "$REPO/.dockerignore" "$SIN/.dockerignore"
DEBEN_QUEDARSE=(.env roster.json plantada.pem estado.sqlite .llminbox-excluir
                .llmi-applied .llmi-mounts.json .llminbox-state .llminbox.token secreto.key
                docs/AGENT-OS-v0.3.md tests/algo.sh)
DEBE_CRUZAR=(servicio.py kind_registry.py Dockerfile web/package.json senuelo-control.txt)
for f in "${DEBEN_QUEDARSE[@]}" "${DEBE_CRUZAR[@]}"; do
  mkdir -p "$SIN/$(dirname "$f")"; printf 'PLANTADO sv_no_debe_viajar_0123456789\n' > "$SIN/$f"
done
mkdir -p "$SIN/.git" "$SIN/web/node_modules" "$SIN/scratch"
printf 'x\n' > "$SIN/.git/config"; printf 'x\n' > "$SIN/web/node_modules/x.js"; printf 'x\n' > "$SIN/scratch/nota.txt"

lista_contexto "$SIN" > "$TMP/ctx-sin.txt"
[ -s "$TMP/ctx-sin.txt" ] || { echo "✗ el contexto sintético salió VACÍO: el instrumento no midió" >&2; exit 2; }
echo "① reglas de .dockerignore, contra un contexto sintético"
for f in "${DEBEN_QUEDARSE[@]}" .git/config web/node_modules/x.js scratch/nota.txt; do
  if grep -qxF "$f" "$TMP/ctx-sin.txt"; then echo "  ✗ $f CRUZÓ"; malos=$((malos+1))
  else echo "  ✓ $f excluido"; fi
done
# ⊕ control: sin esto, un probe roto daría "nada cruzó", que se lee como el mejor
# resultado posible y es el peor.
for f in "${DEBE_CRUZAR[@]}"; do
  if grep -qxF "$f" "$TMP/ctx-sin.txt"; then echo "  ✓ control: $f cruzó ⇒ el probe distingue"
  else echo "  ✗ control positivo: $f NO cruzó — el probe no mide"; malos=$((malos+1)); fi
done

# ═══ ② EL CONTEXTO REAL, sólo lectura ════════════════════════════════════════
echo
echo "② contexto real de este repo (sólo lectura, sin plantar nada)"
lista_contexto "$REPO" > "$TMP/ctx-real.txt"
[ -s "$TMP/ctx-real.txt" ] || { echo "✗ el contexto real salió VACÍO" >&2; exit 2; }
PROHIBIDO='(^|/)\.env$|(^|/)roster\.json$|\.pem$|\.key$|\.p12$|\.pfx$|\.sqlite|\.db$|(^|/)\.git/|node_modules/|(^|/)\.llmi|(^|/)\.llminbox'
if grep -nE "$PROHIBIDO" "$TMP/ctx-real.txt"; then
  echo "  ✗ el contexto real contiene ficheros de las clases prohibidas"; malos=$((malos+1))
else
  echo "  ✓ ninguna clase prohibida en el contexto real"
fi
# ⊖ control del patrón: si no casa ni su propio caso, el verde de arriba no vale.
echo "web/node_modules/x.js" | grep -qE "$PROHIBIDO" \
  || { echo "  ✗ el patrón de prohibidos no casa su propio caso"; malos=$((malos+1)); }
for f in servicio.py ledger_parse.py kind_registry.py ui.html Dockerfile web/package.json; do
  grep -qxF "$f" "$TMP/ctx-real.txt" || { echo "  ✗ falta $f: el build no podría hacerse"; malos=$((malos+1)); }
done
echo "  ficheros en el contexto real: $(wc -l < "$TMP/ctx-real.txt" | tr -d ' ')"

# ═══ ③ ESTE TEST NO TOCA EL ÁRBOL ════════════════════════════════════════════
echo
HUELLA_DESPUES="$(cd "$REPO" && git status --porcelain 2>/dev/null; \
                  shasum -a 256 "$REPO/.dockerignore" "$REPO/servicio.py" 2>/dev/null)"
if [ "$HUELLA_ANTES" = "$HUELLA_DESPUES" ]; then
  echo "③ ✓ el árbol de trabajo quedó byte-idéntico (git status y sha256 de dos ficheros)"
else
  echo "③ ✗ EL TEST MODIFICÓ EL ÁRBOL DE TRABAJO"; malos=$((malos+1))
  diff <(printf '%s\n' "$HUELLA_ANTES") <(printf '%s\n' "$HUELLA_DESPUES") | head
fi

echo
[ "$malos" -eq 0 ] && { echo "contexto de build: limpio."; exit 0; }
echo "$malos problema(s) en el contexto de build." >&2; exit 1
