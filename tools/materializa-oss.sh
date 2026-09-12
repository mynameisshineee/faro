#!/usr/bin/env bash
# materializa-oss.sh — construye el artefacto OSS a partir de `OSS-MANIFEST.txt` y lo NOMBRA.
#
#   tools/materializa-oss.sh <commit> [--manifiesto <ref>] [--destino <dir>] [--quitar <ruta>] [--sin-git]
#
# rc: 0 el artefacto es EXACTAMENTE lo que el manifiesto declara · 1 no lo es · 2 mal uso
#
# ⚠️ POR QUÉ EXISTE, y el motivo es una hora perdida, no una teoría:
# `OSS-MANIFEST.txt` no lo abría NINGÚN programa del repo (medido: 1 mención, en prosa, dentro
# del propio checklist). Consecuencia: cada mano que necesitó el artefacto escribió su propio
# materializador en su propio scratchpad, y el mismo manifiesto dio 450, 455 y 457 ficheros
# porque cada uno lo cruzó con un ÁRBOL distinto. No era un desacuerdo de conteo: era que el
# SUJETO no se podía fijar, porque no había forma compartida de fabricarlo.
#
# 🔑 LO QUE ESTA HERRAMIENTA IMPRIME NO ES UN DIRECTORIO: ES UN NOMBRE.
# Un artefacto sin su censo firmado no es citable — el número que salga de él no se puede
# comparar con el de otra mano. Por eso la salida lleva SIEMPRE los DOS shas (el commit de la
# POBLACIÓN y el blob del MANIFIESTO) y el md5 de la lista ordenada de ficheros.
#
# ⚠️ Y EL PASO QUE NO ES CEREMONIA — hallazgo de la revisión de calidad, adoptado entero:
# `git archive` HONRA `export-ignore` de `.gitattributes`, así que puede dejar fuera un fichero
# que el manifiesto INCLUYE y salir con rc=0 sin decir nada. Por eso lo obligatorio no es
# materializar: es COMPARAR EN LAS DOS DIRECCIONES contra lo que el manifiesto declara.
#
# ⛔ LO QUE **NO** HACE: correr la suite. Ejecutar dentro del artefacto necesita un intérprete,
# un entorno y —en la casa que lo escribió— un turno de máquina; nada de eso es del producto.
# Esa mitad vive en el envoltorio del operador, fuera de este repo, y por eso este fichero
# puede viajar: no abre nada que no viaje con él.
set -uo pipefail

_AQUI="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RAIZ="$(git -C "$_AQUI" rev-parse --show-toplevel 2>/dev/null \
        || git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "materializa-oss.sh: ni el script ni el directorio actual están en un repo git." >&2; exit 2; }
uso(){ sed -n '2,4p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 2; }

COMMIT=""; MANIF=""; DEST=""; QUITA=""
while [ $# -gt 0 ]; do
  case "$1" in
    --manifiesto) MANIF="${2:-}"; shift 2 || uso ;;
    --destino)    DEST="${2:-}";  shift 2 || uso ;;
    --quitar)     QUITA="${2:-}"; shift 2 || uso ;;
    --autotest)   AUTOTEST=1; shift ;;
    --sin-git)    SIN_GIT=1; shift ;;
    -h|--help)    uso ;;
    -*)           echo "materializa-oss.sh: opción desconocida '$1'" >&2; uso ;;
    *)            [ -n "$COMMIT" ] && uso; COMMIT="$1"; shift ;;
  esac
done

# ── autotest: un repo de LABORATORIO, para que los controles se disparen de verdad ──────────
if [ "${AUTOTEST:-0}" = 1 ]; then
  lab="$(mktemp -d)"; trap 'rm -rf "$lab"' EXIT
  git -C "$lab" init -q .
  printf 'a\n' > "$lab/a.py"; printf 'b\n' > "$lab/b.py"; mkdir -p "$lab/sub"; printf 'c\n' > "$lab/sub/c.py"
  printf 'a.py\nb.py\nsub/\n' > "$lab/OSS-MANIFEST.txt"
  git -C "$lab" add -A >/dev/null; git -C "$lab" -c user.email=t@t -c user.name=t commit -qm x
  mkdir -p "$lab/tools"; cp "${BASH_SOURCE[0]}" "$lab/tools/materializa-oss.sh"
  fallos=0; checks=0
  ok(){ checks=$((checks+1)); echo "  ✅ $1"; }
  ko(){ checks=$((checks+1)); fallos=$((fallos+1)); echo "  ❌ $1"; }
  probar(){ # $1=titular  $2=rc esperado  resto=comando
    local t="$1" e="$2"; shift 2
    "$@" >/tmp/.mo.out 2>&1; local r=$?
    if [ "$r" = "$e" ]; then ok "$t (rc=$r)"; else ko "$t: esperaba rc=$e, dio rc=$r"; sed 's/^/      /' /tmp/.mo.out; fi
  }
  echo "⊕ CONTROLES POSITIVOS"
  probar "el arbol limpio materializa coherente" 0 bash "$lab/tools/materializa-oss.sh" HEAD
  echo "⊖ CONTROLES NEGATIVOS — cada uno tiene que PONERSE ROJO"
  # el ⊖ que justifica toda la comparacion: export-ignore tira lo que el manifiesto incluye
  printf 'b.py export-ignore\n' > "$lab/.gitattributes"
  git -C "$lab" add -A >/dev/null; git -C "$lab" -c user.email=t@t -c user.name=t commit -qm ei
  probar "export-ignore tira un fichero que el manifiesto INCLUYE" 1 bash "$lab/tools/materializa-oss.sh" HEAD
  git -C "$lab" rm -q --cached .gitattributes >/dev/null; rm -f "$lab/.gitattributes"
  git -C "$lab" -c user.email=t@t -c user.name=t commit -qm sin-ei >/dev/null
  probar "quitado el export-ignore, vuelve a cuadrar" 0 bash "$lab/tools/materializa-oss.sh" HEAD
  probar "un commit que no existe" 2 bash "$lab/tools/materializa-oss.sh" 0000000
  probar "--quitar de algo que NO estaba no es un control" 2 bash "$lab/tools/materializa-oss.sh" HEAD --quitar zzz.py
  # 🩸 `--quitar` NO produce un rojo, y esperar uno era un defecto de MI expectativa, no del
  # tool: si sale del manifiesto, sale de las DOS listas y el artefacto es coherente y MENOR.
  # Lo que hay que aseverar es el EFECTO: declara uno menos y el fichero NO está.
  d="$lab/.art"; rm -rf "$d"
  probar "--quitar de algo que SI estaba: sigue coherente" 0 bash "$lab/tools/materializa-oss.sh" HEAD --quitar b.py --destino "$d"
  if [ -e "$d/b.py" ]; then ko "--quitar b.py: el fichero SIGUE en el artefacto"
  else ok "--quitar b.py: el fichero NO está en el artefacto"; fi
  if grep -q 'declara        2 ficheros' /tmp/.mo.out; then ok "--quitar b.py: declara 2 (era 3)"
  else ko "--quitar b.py: el censo declarado no bajó"; sed 's/^/      /' /tmp/.mo.out; fi
  # ⊕ el nombre del artefacto REPRODUCE entre corridas: dos veces, mismo commit y mismo arbol
  r1="$lab/.r1"; r2="$lab/.r2"; rm -rf "$r1" "$r2"
  bash "$lab/tools/materializa-oss.sh" HEAD --destino "$r1" >/dev/null 2>&1
  sleep 1
  bash "$lab/tools/materializa-oss.sh" HEAD --destino "$r2" >/dev/null 2>&1
  c1=$(git -C "$r1" rev-parse HEAD 2>/dev/null); c2=$(git -C "$r2" rev-parse HEAD 2>/dev/null)
  t1=$(git -C "$r1" rev-parse HEAD^{tree} 2>/dev/null); t2=$(git -C "$r2" rev-parse HEAD^{tree} 2>/dev/null)
  [ -n "$t1" ] && [ "$t1" = "$t2" ] && ok "dos corridas: MISMO arbol" || ko "dos corridas: arboles distintos ($t1 / $t2)"
  [ -n "$c1" ] && [ "$c1" = "$c2" ] && ok "dos corridas: MISMO commit (la fecha sale del origen, no del reloj)" \
                                    || ko "dos corridas: commits distintos ($c1 / $c2)"

  echo; [ "$fallos" = 0 ] && { echo "autotest: $checks/$checks ✅"; exit 0; } \
                          || { echo "autotest: $fallos de $checks fallo(s) ❌"; exit 1; }
fi

[ -n "$COMMIT" ] || uso
MANIF="${MANIF:-$COMMIT}"
git -C "$RAIZ" rev-parse --verify -q "${COMMIT}^{commit}" >/dev/null || {
  echo "materializa-oss.sh: '$COMMIT' no es un commit de este repo." >&2; exit 2; }
git -C "$RAIZ" cat-file -e "${MANIF}:OSS-MANIFEST.txt" 2>/dev/null || {
  echo "materializa-oss.sh: '${MANIF}' no tiene OSS-MANIFEST.txt." >&2; exit 2; }

SHA_COMMIT="$(git -C "$RAIZ" rev-parse "${COMMIT}^{commit}")"
SHA_MANIF="$(git -C "$RAIZ" rev-parse "${MANIF}:OSS-MANIFEST.txt")"
DEST="${DEST:-$(mktemp -d)}"
mkdir -p "$DEST" || { echo "materializa-oss.sh: no puedo crear '$DEST'." >&2; exit 2; }

# 🩸 EL REF SE IMPRIME COMO LO PEDISTE **Y** RESUELTO, Y CON QUIEN LO CONTIENE.
# Hallazgo de la revisión de canon, medido y peor de lo que él lo planteó: un ref equivocado
# NO produce un error. `declara` se deriva del MISMO árbol que se archiva, así que un fichero
# que ese commit no tiene tampoco se declara y NO puede «faltar». Lo que produce es un
# artefacto COHERENTE, `rc=0`, con OTRA cifra — medido: `cd4ffbc` con el manifiesto de la
# cadena da `459 · coherente ✓` donde la cadena da `462`. Silencioso, y exactamente la clase
# que costó una hora esta mañana. Un sha desnudo no delata que te has ido a otra línea; el
# nombre del ref, sí.
_REFS="$(git -C "$RAIZ" for-each-ref --format='%(refname:short)' --contains "$SHA_COMMIT" \
         refs/heads refs/remotes refs/tags 2>/dev/null | paste -sd' ' - | cut -c1-100)"
[ -n "$_REFS" ] || _REFS="⚠️ NINGUNA rama/etiqueta local lo contiene"
echo "artefacto      $DEST"
echo "poblacion      pediste «${COMMIT}» = ${SHA_COMMIT}"
echo "               contenido en: ${_REFS}"
echo "manifiesto     blob   ${SHA_MANIF}  (de «${MANIF}»)"
# ⊕ Y EL DISCRIMINADOR QUE NO NECESITA UNA RAMA DECLARADA: si el manifiesto viene de otro ref,
# se comprueba que los dos commits estén en la MISMA línea. Dos commits sin parentesco con un
# manifiesto compartido es la forma exacta del error de sujeto: sale coherente y con otra cifra.
if [ "$MANIF" != "$COMMIT" ]; then
  _CM="$(git -C "$RAIZ" rev-parse -q --verify "${MANIF}^{commit}" 2>/dev/null || true)"
  if [ -n "$_CM" ] && [ "$_CM" != "$SHA_COMMIT" ] \
     && ! git -C "$RAIZ" merge-base --is-ancestor "$SHA_COMMIT" "$_CM" 2>/dev/null \
     && ! git -C "$RAIZ" merge-base --is-ancestor "$_CM" "$SHA_COMMIT" 2>/dev/null; then
    echo "⚠️  el commit de la POBLACIÓN y el del MANIFIESTO no están en la misma línea:" >&2
    echo "    ninguno desciende del otro. El artefacto saldrá COHERENTE y con otra cifra." >&2
  fi
fi

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
# 🩸 los prefijos se leen SIN comentarios y SIN cola en blanco: una linea vacia es un pathspec
# vacio, y git contesta «empty string is not a valid pathspec» abortando el archive entero.
git -C "$RAIZ" show "${MANIF}:OSS-MANIFEST.txt" \
  | sed 's/[[:space:]]*$//' | grep -v '^[[:space:]]*#' | grep -v '^[[:space:]]*$' > "$tmp/pref.txt"

if [ -n "$QUITA" ]; then
  if ! grep -qxF -- "$QUITA" "$tmp/pref.txt"; then
    echo "materializa-oss.sh: '--quitar $QUITA' no está en el manifiesto ⇒ el control NO cambia" >&2
    echo "        nada y se leería como si hubiera medido algo. INÚTIL, no es un ⊖." >&2
    exit 2
  fi
  grep -vxF -- "$QUITA" "$tmp/pref.txt" > "$tmp/p2" && mv "$tmp/p2" "$tmp/pref.txt"
fi
[ -s "$tmp/pref.txt" ] || { echo "materializa-oss.sh: el manifiesto no declara ningún prefijo." >&2; exit 2; }

# ── ① LO QUE EL MANIFIESTO DECLARA (derivado del ÁRBOL, no de la lista de prefijos) ──────────
git -C "$RAIZ" ls-tree -r --name-only "$SHA_COMMIT" > "$tmp/pob.txt"
awk 'NR==FNR{p[++n]=$0; next}
     { for(i=1;i<=n;i++){ m=p[i]; sub(/\/+$/,"",m);
         if ($0==m || index($0, m "/")==1) { print; break } } }' \
    "$tmp/pref.txt" "$tmp/pob.txt" | LC_ALL=C sort > "$tmp/declara.txt"

# ── ② LO QUE SALE DE VERDAD ─────────────────────────────────────────────────────────────────
# shellcheck disable=SC2046
git -C "$RAIZ" archive "$SHA_COMMIT" -- $(tr '\n' ' ' < "$tmp/pref.txt") 2>"$tmp/e" \
  | tar -x -C "$DEST" -f - || { echo "materializa-oss.sh: git archive falló:"; sed 's/^/   /' "$tmp/e"; exit 2; } >&2
( cd "$DEST" && find . -type f | sed 's|^\./||' ) | LC_ALL=C sort > "$tmp/sale.txt"

# ── ③ LA COMPARACIÓN EN LAS DOS DIRECCIONES — esto es lo que lo convierte en un SUJETO ───────
FALTAN="$(comm -23 "$tmp/declara.txt" "$tmp/sale.txt")"
SOBRAN="$(comm -13 "$tmp/declara.txt" "$tmp/sale.txt")"
N_DECL=$(wc -l < "$tmp/declara.txt" | tr -d ' ')
N_SALE=$(wc -l < "$tmp/sale.txt" | tr -d ' ')
MD5="$(LC_ALL=C sort "$tmp/sale.txt" | md5 -q 2>/dev/null || LC_ALL=C sort "$tmp/sale.txt" | md5sum | cut -d' ' -f1)"

echo "declara        ${N_DECL} ficheros"
echo "contiene       ${N_SALE} ficheros   ·   md5 de la lista ordenada: ${MD5}"

if [ -n "$FALTAN" ] || [ -n "$SOBRAN" ]; then
  echo "::error::el artefacto NO es lo que el manifiesto declara" >&2
  [ -n "$FALTAN" ] && { echo "   FALTAN (el manifiesto los incluye y no están):" >&2; echo "$FALTAN" | sed 's/^/     - /' >&2; }
  [ -n "$SOBRAN" ] && { echo "   SOBRAN (están y el manifiesto no los declara):" >&2; echo "$SOBRAN" | sed 's/^/     + /' >&2; }
  echo "   ⇒ la causa habitual es \`export-ignore\` en .gitattributes: git archive obedece y calla." >&2
  exit 1
fi
echo "coherente ✓    falta 0 · sobra 0"

# ── ④ EL ARTEFACTO ES UN REPOSITORIO, no un directorio ───────────────────────────────────────
# 🩸 MEDIDO, y por eso está aquí y no es adorno: `tools/higiene.py` —el ÚNICO gate bloqueante
# del paquete— escanea `git ls-files`, «because that is what a reader of the published
# repository actually receives». Sobre un directorio suelto no da un veredicto: CASCA con
# `CalledProcessError: git ls-files … 128` y sale con `rc=1`. Un rojo de instrumento y un rojo
# de hallazgo se imprimen igual, y ése es el error caro.
# Y hay una segunda razón, que es de producto: el tercer vehículo ES un repositorio público
# creado como ARTEFACTO NUEVO, sin el histórico del repo de trabajo. Un commit único cuyo árbol
# es exactamente esto es la forma de ese vehículo, no una comodidad del gate.
if [ "${SIN_GIT:-0}" != 1 ]; then
  git -C "$DEST" init -q 2>/dev/null || { echo "materializa-oss.sh: no pude iniciar el repo del artefacto." >&2; exit 2; }
  git -C "$DEST" add -A || { echo "materializa-oss.sh: no pude indexar el artefacto." >&2; exit 2; }
  # 🩸 LA FECHA SALE DEL COMMIT DE ORIGEN, NO DEL RELOJ DE PARED. Medido por la revisión de
  # seguridad: dos corridas honestas del MISMO comando daban el MISMO árbol y commits
  # DISTINTOS (27 s de diferencia), así que el artefacto era determinista en contenido y no
  # en su nombre — y dos manos citando dos shas para el mismo contenido es exactamente el
  # desacuerdo de SUJETO que costó una hora con el 455/457. Con la fecha del commit de
  # población, el sha del artefacto es función de (población, manifiesto) y nada más.
  _FECHA="$(git -C "$RAIZ" show -s --format=%cI "$SHA_COMMIT")"
  GIT_AUTHOR_DATE="$_FECHA" GIT_COMMITTER_DATE="$_FECHA" \
  git -C "$DEST" -c user.name=release -c user.email=release@llminbox.invalid \
      commit -q -m "artefacto OSS · poblacion ${SHA_COMMIT} · manifiesto ${SHA_MANIF}" \
      || { echo "materializa-oss.sh: no pude commitear el artefacto." >&2; exit 2; }
  N_GIT=$(git -C "$DEST" ls-files | wc -l | tr -d " ")
  _SHA_ART="$(git -C "$DEST" rev-parse HEAD)"
  _TREE_ART="$(git -C "$DEST" rev-parse HEAD^{tree})"
  echo "repositorio    1 commit · git ls-files ve ${N_GIT} ficheros"
  echo "artefacto sha  commit ${_SHA_ART}"
  echo "               arbol  ${_TREE_ART}"
  # 🔑 LA HISTORIA DEL ARTEFACTO ES SINTETICA, Y ESO NO ES UN DEFECTO: ES EL VEHICULO.
  # El repo OSS se decidio como artefacto NUEVO sin el historico del repo de trabajo, asi que
  # aqui hay UN commit y su sha NO EXISTE en el repositorio real. Consecuencia medida: todo
  # test cuyo SUJETO sea la historia de git se comporta distinto dentro del paquete — no falla
  # por un defecto del producto, falla porque el sujeto cambio. Comparar el artefacto contra un
  # `git worktree` del mismo sha produce esa divergencia SIEMPRE; contra `git archive` no, porque
  # el tarball tampoco tiene historia. Las dos comparaciones son validas y contestan preguntas
  # distintas: el archive mide el RECORTE, el worktree mide la MATERIALIZACION.
  echo "⚠️  esa historia es SINTETICA (1 commit, sha inexistente en el repo de origen):"
  echo "    todo test cuyo sujeto sea la historia de git medira algo distinto aqui."
  _N_HIST=$(cd "$DEST" && grep -rl -e "rev-list" -e "git log" -e "cat-file" -e "for-each-ref" \
              -e "merge-base" tests 2>/dev/null | wc -l | tr -d " ")
  echo "    ficheros de test que tocan la historia en este artefacto: ${_N_HIST}"
  # ⊕ el gate del paquete mira `git ls-files`: si esa cifra no casa con el censo, lo que el
  #   gate escanea NO es lo que acabamos de nombrar, y todo lo de arriba sería decorativo.
  if [ "$N_GIT" != "$N_SALE" ]; then
    echo "::error::git ls-files ve ${N_GIT} y el artefacto tiene ${N_SALE}: el gate escanearía otra cosa." >&2
    exit 1
  fi
fi
exit 0
