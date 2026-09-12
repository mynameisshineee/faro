#!/bin/bash
# tarball-vs-manifiesto.sh — ¿viaja algo que el manifiesto NO declara?
#
#     tools/tarball-vs-manifiesto.sh [<ref>]        # def. HEAD
#
# Salidas:  0 nada viaja sin declarar
#           1 VIAJA lo no declarado (los lista)
#           2 NO MEDIDO (sin repo, sin ref, sin manifiesto, o el control ⊕ falla)
#
# POR QUÉ EXISTE, y es un modo de fallo medido, no una teoría:
# el artefacto OSS se construye del manifiesto, pero el TARBALL se construye con
# `git archive`, que no lo mira. El 2026-09-11 se midió que 35 ficheros viajaban en
# el tarball sin estar declarados: 10 FREEZE-*.md, documentos de despliegue interno,
# y un fichero de la flota. Ninguno lo había decidido nadie: viajaban por omisión.
# El gate de higiene no lo veía porque su universo era el ÁRBOL (`git ls-files`), no
# las superficies PUBLICADAS.
#
# LA REGLA DE COBERTURA, declarada aquí para que se pueda contradecir: un fichero
# está cubierto si el manifiesto lo nombra EXACTO o si cuelga de una entrada que
# nombra un directorio. Es la misma que usa el constructor del artefacto.
set -u
REF="${1:-HEAD}"
RAIZ="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "tarball-vs-manifiesto.sh: esto no es un repo git. NO MEDIDO." >&2; exit 2; }
cd "$RAIZ" || exit 2
git cat-file -e "${REF}^{commit}" 2>/dev/null || {
  echo "tarball-vs-manifiesto.sh: la ref '${REF}' no está en este repo. NO MEDIDO." >&2; exit 2; }
git cat-file -e "${REF}:OSS-MANIFEST.txt" 2>/dev/null || {
  echo "tarball-vs-manifiesto.sh: '${REF}' no trae OSS-MANIFEST.txt. NO MEDIDO." >&2; exit 2; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
git archive "$REF" | tar -t > "$TMP/tar.txt" 2>/dev/null
grep -v '/$' "$TMP/tar.txt" | sort > "$TMP/ficheros.txt"
git cat-file -p "${REF}:OSS-MANIFEST.txt" | sed 's/[[:space:]]*$//' \
  | grep -vE '^[[:space:]]*#' | grep -v '^$' | sort > "$TMP/entradas.txt"

N_TAR=$(wc -l < "$TMP/ficheros.txt" | tr -d ' ')
N_ENT=$(wc -l < "$TMP/entradas.txt" | tr -d ' ')
# ⊕ CONTROL DE QUE LA AGUJA LEE: sin esto, un tarball vacío o un manifiesto ilegible
# darían «0 sin declarar» — un verde por ceguera, que es el defecto que este fichero
# existe para no cometer.
if [ "$N_TAR" -lt 50 ] || [ "$N_ENT" -lt 10 ]; then
  echo "tarball-vs-manifiesto.sh: tarball=$N_TAR ficheros y manifiesto=$N_ENT entradas." >&2
  echo "        Uno de los dos no se ha leído: un 0 aquí sería ceguera. NO MEDIDO." >&2
  exit 2
fi

awk -v ENT="$TMP/entradas.txt" '
  BEGIN { n=0; while ((getline l < ENT) > 0) { if (l != "") e[n++]=l } }
  { cubierto=0
    for (i=0; i<n; i++) {
      if ($0 == e[i]) { cubierto=1; break }
      pref = (substr(e[i], length(e[i])) == "/") ? e[i] : e[i] "/"
      if (index($0, pref) == 1) { cubierto=1; break }
    }
    if (!cubierto) print }
' "$TMP/ficheros.txt" > "$TMP/fuera.txt"

N_FUERA=$(wc -l < "$TMP/fuera.txt" | tr -d ' ')
# ⊕ SEGUNDO CONTROL: la primera entrada del manifiesto tiene que estar CUBIERTA por
# la regla. Si no lo está, la regla no casa nada y el «0» de arriba no vale.
PRIMERA=$(head -1 "$TMP/entradas.txt")
if ! grep -qxF "$PRIMERA" "$TMP/ficheros.txt" && ! grep -q "^${PRIMERA}/" "$TMP/ficheros.txt"; then
  echo "tarball-vs-manifiesto.sh: la primera entrada declarada ('$PRIMERA') no aparece en el" >&2
  echo "        tarball. O el manifiesto describe otro árbol, o la regla no lee. NO MEDIDO." >&2
  exit 2
fi

echo "ref ${REF} · tarball ${N_TAR} ficheros · manifiesto ${N_ENT} entradas · sin declarar ${N_FUERA}"
if [ "$N_FUERA" -gt 0 ]; then
  echo "🔴 VIAJAN SIN DECLARAR — el manifiesto no los nombra y el tarball los lleva:"
  sed 's/^/   · /' "$TMP/fuera.txt"
  echo "   Cura: declararlos en OSS-MANIFEST.txt (si deben viajar) o marcarlos"
  echo "   'export-ignore' en .gitattributes (si no). Publicar por omisión no lo decide nadie."
  exit 1
fi
echo "✅ nada viaja sin declarar (regla: nombre exacto o bajo una entrada de directorio)"
exit 0
