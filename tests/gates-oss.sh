#!/usr/bin/env bash
# Los gates OSS saben decir que NO.
#
# Cada caso planta un defecto CONCRETO y exige ROJO; el positivo exige VERDE. Sin
# el positivo, un gate que dijera «no» a todo pasaría esta suite entera — y un gate
# que siempre dice no se desactiva el primer día, que es la otra forma de no tener
# gate.
#
# ⚠️ No escribe nada dentro del árbol de trabajo: fixtures, llavero GPG y raíz de
# confianza viven en un `mktemp` propio. La limpieza sólo toca ese temporal.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
REPO="$PWD"

command -v gpg >/dev/null || { echo "✗ sin gpg no puedo firmar de verdad: NO PUEDO MEDIR" >&2; exit 2; }
TMP="$(mktemp -d)" || exit 2
# ⚠️ El agente que arranca la GENERACIÓN de claves sobrevive al script y queda
# huérfano bajo PID 1 apuntando a un directorio ya borrado. Se le pide morir ANTES
# de borrar su casa: al revés, queda un agente vivo sobre una ruta que no existe.
limpia() {
  command -v gpgconf >/dev/null && gpgconf --homedir "$GNUPGHOME" --kill gpg-agent >/dev/null 2>&1
  rm -rf "$TMP"
}
trap limpia EXIT
export GNUPGHOME="$TMP/gnupg"; mkdir -p "$GNUPGHOME"; chmod 700 "$GNUPGHOME"

# ⚠️ El gate resuelve su RAÍZ desde su propia ubicación y confina ahí el material
# de confianza. Para ejercer ese contrato de verdad —y sin escribir un solo byte en
# el árbol real— se monta un repositorio SINTÉTICO en el temporal y se corre la
# copia del gate que vive dentro de él.
SIN_REPO="$TMP/repo"; mkdir -p "$SIN_REPO/tools"
cp "$REPO/tools/artefacto-gate.py" "$SIN_REPO/tools/" || exit 2
GATE="python3 $SIN_REPO/tools/artefacto-gate.py"

# El gate contrasta los materiales declarados con el checkout que lo contiene.
# Estas fuentes sintéticas permiten probar ese vínculo sin depender del checkout real.
mkdir -p "$SIN_REPO/web"
BASE_NODE="$(printf 'node-base' | shasum -a 256 | awk '{print $1}')"
BASE_PYTHON="$(printf 'python-base' | shasum -a 256 | awk '{print $1}')"
printf 'FROM node:24-slim@sha256:%s AS web\nFROM python:3.13-slim@sha256:%s\n' \
  "$BASE_NODE" "$BASE_PYTHON" > "$SIN_REPO/Dockerfile"
printf 'fastapi==0.1 --hash=sha256:%064d\n' 0 > "$SIN_REPO/requirements.lock"
printf 'lockfileVersion: 9.0\n' > "$SIN_REPO/web/pnpm-lock.yaml"
MAT_DOCKERFILE="$(shasum -a 256 "$SIN_REPO/Dockerfile" | awk '{print $1}')"
MAT_REQUIREMENTS="$(shasum -a 256 "$SIN_REPO/requirements.lock" | awk '{print $1}')"
MAT_PNPM="$(shasum -a 256 "$SIN_REPO/web/pnpm-lock.yaml" | awk '{print $1}')"
REVISION="0123456789abcdef0123456789abcdef01234567"
FUENTE="git+https://github.com/llminbox/llminbox@$REVISION"
CONSTRUCTOR="git+https://github.com/llminbox/llminbox/tools/build-release.sh"

ok=0; mal=0
esperar() {  # esperar <ROJO|VERDE> <etiqueta> -- <comando...>
  local quiero="$1" etiqueta="$2"; shift 3
  local salida rc
  salida="$("$@" 2>&1)"; rc=$?
  if { [ "$quiero" = VERDE ] && [ $rc -eq 0 ]; } || { [ "$quiero" = ROJO ] && [ $rc -ne 0 ]; }; then
    ok=$((ok+1)); printf '  ✓ %-52s (%s, rc=%d)\n' "$etiqueta" "$quiero" "$rc"
  else
    mal=$((mal+1)); printf '  ✗ %-52s esperaba %s, rc=%d\n' "$etiqueta" "$quiero" "$rc"
    printf '%s\n' "$salida" | sed 's/^/      /' | tail -4
  fi
}

# ── dos identidades REALES ───────────────────────────────────────────────────
gpg --batch --quick-gen-key --passphrase '' 'Release Key <release@llminbox.invalid>' default default never >/dev/null 2>&1
gpg --batch --quick-gen-key --passphrase '' 'Someone Else <otro@llminbox.invalid>'   default default never >/dev/null 2>&1
FP_OK="$(gpg --list-keys --with-colons release@llminbox.invalid | awk -F: '/^fpr:/{print $10; exit}')"
FP_OTRO="$(gpg --list-keys --with-colons otro@llminbox.invalid | awk -F: '/^fpr:/{print $10; exit}')"
[ -n "$FP_OK" ] && [ -n "$FP_OTRO" ] && [ "$FP_OK" != "$FP_OTRO" ] \
  || { echo "✗ no pude crear dos identidades distintas" >&2; exit 2; }

# Lo que se versiona es la CLAVE PÚBLICA en armadura ASCII, no un llavero: el gate
# monta un GNUPGHOME efímero y la importa. Aquí se exportan las dos, y una de ellas
# hace de "clave sustituida" en su propio caso.
gpg --batch --yes --armor --export "$FP_OK"   > "$SIN_REPO/release-pubkey.asc"
gpg --batch --yes --armor --export "$FP_OTRO" > "$SIN_REPO/release-pubkey-otra.asc"
printf 'clave fuera del repo\n' > "$TMP/pub-fuera.asc"
[ -s "$SIN_REPO/release-pubkey.asc" ] && [ -s "$SIN_REPO/release-pubkey-otra.asc" ] \
  || { echo "✗ no pude exportar las claves" >&2; exit 2; }

confianza() {  # confianza <fichero> <huella> <ruta-de-clave RELATIVA a la raíz>
  printf '{"backend":"gpg","fingerprint":"%s","public_key":"%s"}\n' "$2" "$3" > "$1"
}
confianza "$TMP/trust-ok.json"        "$FP_OK"   "release-pubkey.asc"
confianza "$TMP/trust-otro.json"      "$FP_OTRO" "release-pubkey-otra.asc"
confianza "$TMP/trust-sin-clave.json" "$FP_OK"   "release-pubkey-que-no-existe.asc"
# Clave SUSTITUIDA: la raíz declara la huella buena y el fichero trae otra clave.
confianza "$TMP/trust-alterada.json"  "$FP_OK"   "release-pubkey-otra.asc"
printf '{"backend":"gpg","public_key":"release-pubkey.asc"}\n' > "$TMP/trust-sin-huella.json"
printf '{"backend":"inventado"}\n' > "$TMP/trust-backend.json"

# ── fixture VÁLIDO, que se rehace entero en cada caso ────────────────────────
construir() {  # construir <dir>
  local d="$1"; rm -rf "$d"; mkdir -p "$d"
  printf 'binario de mentira\n' > "$d/llminbox-0.9.0.tar.gz"
  local dig; dig="$(shasum -a 256 "$d/llminbox-0.9.0.tar.gz" | awk '{print $1}')"
  cat > "$d/sbom.cdx.json" <<JSON
{"bomFormat":"CycloneDX","specVersion":"1.5",
 "serialNumber":"urn:uuid:123e4567-e89b-42d3-a456-426614174000","version":1,
 "metadata":{"component":{"type":"container","name":"llminbox-0.9.0"}},
 "components":[
  {"type":"library","bom-ref":"pkg:npm/react@19.2.8","name":"react","version":"19.2.8","purl":"pkg:npm/react@19.2.8"},
  {"type":"file","bom-ref":"file:/app/runtime_root.py","name":"/app/runtime_root.py","hashes":[{"alg":"SHA-256","content":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"}]},
  {"type":"operating-system","bom-ref":"os:debian@13","name":"debian","version":"13"}
 ]}
JSON
  cat > "$d/provenance.json" <<JSON
{"_type":"https://in-toto.io/Statement/v1",
 "predicateType":"https://slsa.dev/provenance/v1",
 "subject":[{"name":"llminbox-0.9.0.tar.gz","digest":{"sha256":"$dig"}}],
 "predicate":{
   "buildDefinition":{
     "buildType":"urn:llminbox:build/oci/v1",
     "externalParameters":{"platform":"linux/amd64","source":"$FUENTE"},
     "internalParameters":{"sourceDateEpoch":1700000000},
     "resolvedDependencies":[
       {"uri":"pkg:docker/node@24-slim","digest":{"sha256":"$BASE_NODE"}},
       {"uri":"pkg:docker/python@3.13-slim","digest":{"sha256":"$BASE_PYTHON"}},
       {"uri":"file:Dockerfile","digest":{"sha256":"$MAT_DOCKERFILE"}},
       {"uri":"file:requirements.lock","digest":{"sha256":"$MAT_REQUIREMENTS"}},
       {"uri":"file:web/pnpm-lock.yaml","digest":{"sha256":"$MAT_PNPM"}}
     ]},
   "runDetails":{"builder":{"id":"$CONSTRUCTOR"},"metadata":{"invocationId":"$REVISION"}}
 }}
JSON
  ( cd "$d" && shasum -a 256 llminbox-0.9.0.tar.gz sbom.cdx.json provenance.json > SHA256SUMS )
  gpg --batch --yes --pinentry-mode loopback --passphrase '' -u "$FP_OK" \
      --detach-sign --armor -o "$d/SHA256SUMS.asc" "$d/SHA256SUMS" >/dev/null 2>&1
}
resellar() {  # resellar <dir>, tras mutar evidencia en un falsador
  local d="$1"
  ( cd "$d" && shasum -a 256 llminbox-0.9.0.tar.gz sbom.cdx.json provenance.json > SHA256SUMS )
  gpg --batch --yes --pinentry-mode loopback --passphrase '' -u "$FP_OK" \
      --detach-sign --armor -o "$d/SHA256SUMS.asc" "$d/SHA256SUMS" >/dev/null 2>&1
}
D="$SIN_REPO/dist"

echo "① el positivo — sin él, un gate que diga NO a todo aprobaría esta suite"
construir "$D"
esperar VERDE "fixture válido, firma verificada" -- $GATE --dir "$D" --trust "$TMP/trust-ok.json"
esperar VERDE "el mismo fixture en --precheck" -- $GATE --dir "$D" --precheck

echo
echo "② firma"
construir "$D"; : > "$D/SHA256SUMS.asc"
esperar ROJO "firma VACÍA" -- $GATE --dir "$D" --trust "$TMP/trust-ok.json"
construir "$D"
gpg --batch --yes --pinentry-mode loopback --passphrase '' -u "$FP_OTRO" \
    --detach-sign --armor -o "$D/SHA256SUMS.asc" "$D/SHA256SUMS" >/dev/null 2>&1
esperar ROJO "firma AJENA (válida, otra clave)" -- $GATE --dir "$D" --trust "$TMP/trust-ok.json"
construir "$D"
esperar ROJO "IDENTIDAD equivocada en la raíz de confianza" -- $GATE --dir "$D" --trust "$TMP/trust-otro.json"
esperar ROJO "raíz de confianza AUSENTE" -- $GATE --dir "$D" --trust "$TMP/no-existe.json"
esperar ROJO "raíz sin huella (aceptaría cualquier clave)" -- $GATE --dir "$D" --trust "$TMP/trust-sin-huella.json"
esperar ROJO "backend desconocido" -- $GATE --dir "$D" --trust "$TMP/trust-backend.json"
construir "$D"; sed -i.bak 's/^-----BEGIN/-----XEGIN/' "$D/SHA256SUMS.asc"; rm -f "$D/SHA256SUMS.asc.bak"
esperar ROJO "firma corrupta" -- $GATE --dir "$D" --trust "$TMP/trust-ok.json"

echo
echo "③ SHA256SUMS: el nombre está confinado"
construir "$D"; printf '%s  /etc/passwd\n' "$(printf 0123456789abcdef | shasum -a 256 | awk '{print $1}')" >> "$D/SHA256SUMS"
esperar ROJO "ruta ABSOLUTA en SHA256SUMS" -- $GATE --dir "$D" --trust "$TMP/trust-ok.json"
construir "$D"; printf '%s  ../fuera.txt\n' "$(printf x | shasum -a 256 | awk '{print $1}')" >> "$D/SHA256SUMS"
esperar ROJO "ruta con .. en SHA256SUMS" -- $GATE --dir "$D" --trust "$TMP/trust-ok.json"
construir "$D"; grep 'sbom' "$D/SHA256SUMS" >> "$D/SHA256SUMS"
esperar ROJO "entrada DUPLICADA" -- $GATE --dir "$D" --trust "$TMP/trust-ok.json"
construir "$D"; printf 'nohex  sbom.cdx.json\n' >> "$D/SHA256SUMS"
esperar ROJO "digest que no es 64 hex" -- $GATE --dir "$D" --trust "$TMP/trust-ok.json"
construir "$D"; ln -s /etc/hosts "$D/enlace.txt"
esperar ROJO "enlace simbólico en el directorio" -- $GATE --dir "$D" --trust "$TMP/trust-ok.json"
construir "$D"; printf 'colado\n' > "$D/extra.bin"
esperar ROJO "fichero sin sumar en el directorio" -- $GATE --dir "$D" --trust "$TMP/trust-ok.json"
construir "$D"; printf 'cambiado\n' > "$D/llminbox-0.9.0.tar.gz"
esperar ROJO "checksum que no cuadra" -- $GATE --dir "$D" --trust "$TMP/trust-ok.json"

echo
echo "④ procedencia atada al artefacto"
construir "$D"; printf '{"sha256":"%s"}\n' "$(shasum -a 256 "$D/llminbox-0.9.0.tar.gz" | awk '{print $1}')" > "$D/provenance.json"
( cd "$D" && shasum -a 256 llminbox-0.9.0.tar.gz sbom.cdx.json provenance.json > SHA256SUMS )
gpg --batch --yes --pinentry-mode loopback --passphrase '' -u "$FP_OK" --detach-sign --armor -o "$D/SHA256SUMS.asc" "$D/SHA256SUMS" >/dev/null 2>&1
esperar ROJO "sha256 suelto, sin subject in-toto" -- $GATE --dir "$D" --trust "$TMP/trust-ok.json"
construir "$D"; python3 - "$D" <<'PY'
import json,sys,os
d=sys.argv[1]; p=os.path.join(d,"provenance.json"); j=json.load(open(p))
j["subject"][0]["name"]="otro-artefacto.tar.gz"; json.dump(j,open(p,"w"))
PY
( cd "$D" && shasum -a 256 llminbox-0.9.0.tar.gz sbom.cdx.json provenance.json > SHA256SUMS )
gpg --batch --yes --pinentry-mode loopback --passphrase '' -u "$FP_OK" --detach-sign --armor -o "$D/SHA256SUMS.asc" "$D/SHA256SUMS" >/dev/null 2>&1
esperar ROJO "subject que nombra un fichero no sumado" -- $GATE --dir "$D" --trust "$TMP/trust-ok.json"
construir "$D"; python3 - "$D" <<'PY'
import json,sys,os
d=sys.argv[1]; p=os.path.join(d,"provenance.json"); j=json.load(open(p))
j["subject"][0]["digest"]["sha256"]="0"*64; json.dump(j,open(p,"w"))
PY
( cd "$D" && shasum -a 256 llminbox-0.9.0.tar.gz sbom.cdx.json provenance.json > SHA256SUMS )
gpg --batch --yes --pinentry-mode loopback --passphrase '' -u "$FP_OK" --detach-sign --armor -o "$D/SHA256SUMS.asc" "$D/SHA256SUMS" >/dev/null 2>&1
esperar ROJO "subject con digest que no coincide" -- $GATE --dir "$D" --trust "$TMP/trust-ok.json"

echo
echo "⑤ SBOM y directorio"
construir "$D"; printf '{"bomFormat":"CycloneDX","components":[]}\n' > "$D/sbom.cdx.json"
( cd "$D" && shasum -a 256 llminbox-0.9.0.tar.gz sbom.cdx.json provenance.json > SHA256SUMS )
gpg --batch --yes --pinentry-mode loopback --passphrase '' -u "$FP_OK" --detach-sign --armor -o "$D/SHA256SUMS.asc" "$D/SHA256SUMS" >/dev/null 2>&1
esperar ROJO "SBOM con CERO componentes" -- $GATE --dir "$D" --trust "$TMP/trust-ok.json"
construir "$D"; python3 - "$D" <<'PY'
import json, os, sys
p=os.path.join(sys.argv[1], "sbom.cdx.json"); j=json.load(open(p))
j.pop("serialNumber"); json.dump(j, open(p, "w"))
PY
resellar "$D"
esperar ROJO "SBOM sin identidad documental" -- $GATE --dir "$D" --trust "$TMP/trust-ok.json"
construir "$D"; python3 - "$D" <<'PY'
import json, os, sys
p=os.path.join(sys.argv[1], "sbom.cdx.json"); j=json.load(open(p))
j["components"][0].pop("purl"); json.dump(j, open(p, "w"))
PY
resellar "$D"
esperar ROJO "componente SBOM sin purl ni hashes" -- $GATE --dir "$D" --trust "$TMP/trust-ok.json"
construir "$D"; python3 - "$D" <<'PY'
import json, os, sys
p=os.path.join(sys.argv[1], "sbom.cdx.json"); j=json.load(open(p))
j["components"][1].pop("hashes"); json.dump(j, open(p, "w"))
PY
resellar "$D"
esperar ROJO "fichero SBOM sin hash de contenido" -- $GATE --dir "$D" --trust "$TMP/trust-ok.json"
construir "$D"; python3 - "$D" <<'PY'
import json, os, sys
p=os.path.join(sys.argv[1], "sbom.cdx.json"); j=json.load(open(p))
j["components"][2]["type"]="inventado"; json.dump(j, open(p, "w"))
PY
resellar "$D"
esperar ROJO "componente SBOM con tipo inventado" -- $GATE --dir "$D" --trust "$TMP/trust-ok.json"
esperar ROJO "directorio VACÍO" -- $GATE --dir "$TMP/vacio-inexistente" --trust "$TMP/trust-ok.json"
mkdir -p "$TMP/vacio"; esperar ROJO "directorio existente y vacío" -- $GATE --dir "$TMP/vacio" --trust "$TMP/trust-ok.json"

echo
echo "⑥ procedencia SLSA semántica y materiales"
construir "$D"; python3 - "$D" <<'PY'
import json, os, sys
p=os.path.join(sys.argv[1], "provenance.json"); j=json.load(open(p))
j["predicate"]["buildDefinition"]["buildType"]="prueba"; json.dump(j, open(p, "w"))
PY
resellar "$D"
esperar ROJO "buildType inventado" -- $GATE --dir "$D" --trust "$TMP/trust-ok.json"
construir "$D"; python3 - "$D" <<'PY'
import json, os, sys
p=os.path.join(sys.argv[1], "provenance.json"); j=json.load(open(p))
j["predicate"]["buildDefinition"]["resolvedDependencies"][0]["digest"]["sha256"]="0"*64
json.dump(j, open(p, "w"))
PY
resellar "$D"
esperar ROJO "digest base distinto del Dockerfile" -- $GATE --dir "$D" --trust "$TMP/trust-ok.json"
construir "$D"; python3 - "$D" <<'PY'
import json, os, sys
p=os.path.join(sys.argv[1], "provenance.json"); j=json.load(open(p))
j["predicate"]["runDetails"]["metadata"]["invocationId"]="f"*40
json.dump(j, open(p, "w"))
PY
resellar "$D"
esperar ROJO "invocationId ajeno al commit fuente" -- $GATE --dir "$D" --trust "$TMP/trust-ok.json"

echo
echo "⑦ los cuatro que encontró la revisión estática"
construir "$D"; mkdir -p "$D/anidado" && printf 'payload\n' > "$D/anidado/x.bin"
esperar ROJO "payload dentro de un SUBDIRECTORIO" -- $GATE --dir "$D" --trust "$TMP/trust-ok.json"
construir "$D"; mkfifo "$D/tuberia" 2>/dev/null && \
  esperar ROJO "nodo que no es fichero regular (fifo)" -- $GATE --dir "$D" --trust "$TMP/trust-ok.json"
construir "$D"; confianza "$TMP/trust-parcial.json" "${FP_OK: -8}" "release-pubkey.asc"
esperar ROJO "huella PARCIAL (sufijo de 8) en la raíz" -- $GATE --dir "$D" --trust "$TMP/trust-parcial.json"
printf '{"backend":"gpg","fingerprint":"%s"}\n' "$FP_OK" > "$TMP/trust-sin-clave-decl.json"
esperar ROJO "gpg SIN clave pública declarada (usaría el ambiente)" -- $GATE --dir "$D" --trust "$TMP/trust-sin-clave-decl.json"
esperar ROJO "fichero de clave AUSENTE" -- $GATE --dir "$D" --trust "$TMP/trust-sin-clave.json"
esperar ROJO "fichero de clave SUSTITUIDO por otra clave" -- $GATE --dir "$D" --trust "$TMP/trust-alterada.json"
construir "$D"; python3 - "$D" <<'PYX'
import json,sys,os
d=sys.argv[1]; p=os.path.join(d,"provenance.json"); j=json.load(open(p))
j["subject"][0]["name"]="/otro/sitio/llminbox-0.9.0.tar.gz"; json.dump(j,open(p,"w"))
PYX
( cd "$D" && shasum -a 256 llminbox-0.9.0.tar.gz sbom.cdx.json provenance.json > SHA256SUMS )
gpg --batch --yes --pinentry-mode loopback --passphrase '' -u "$FP_OK" --detach-sign --armor -o "$D/SHA256SUMS.asc" "$D/SHA256SUMS" >/dev/null 2>&1
esperar ROJO "subject.name con RUTA (no basename)" -- $GATE --dir "$D" --trust "$TMP/trust-ok.json"
construir "$D"; cp "$D/SHA256SUMS.asc" "$D/SHA256SUMS.sig"
esperar ROJO "DOS firmas reconocidas" -- $GATE --dir "$D" --trust "$TMP/trust-ok.json"
construir "$D"; cp "$D/sbom.cdx.json" "$D/sbom.json"
esperar ROJO "DOS SBOM reconocidos" -- $GATE --dir "$D" --trust "$TMP/trust-ok.json"
construir "$D"; cp "$D/provenance.json" "$D/attestation.json"
esperar ROJO "DOS procedencias reconocidas" -- $GATE --dir "$D" --trust "$TMP/trust-ok.json"

echo
echo "⑧ la raíz de confianza no puede salir del repositorio"
construir "$D"
confianza "$TMP/trust-abs.json" "$FP_OK" "$TMP/pub-fuera.asc"
esperar ROJO "public_key con ruta ABSOLUTA" -- $GATE --dir "$D" --trust "$TMP/trust-abs.json"
confianza "$TMP/trust-trav.json" "$FP_OK" "../pub-fuera.asc"
esperar ROJO "public_key con .. (traversal)" -- $GATE --dir "$D" --trust "$TMP/trust-trav.json"
ln -sf "$TMP/pub-fuera.asc" "$SIN_REPO/enlace-fuera.asc"
confianza "$TMP/trust-link-fuera.json" "$FP_OK" "enlace-fuera.asc"
esperar ROJO "public_key vía SYMLINK que sale del repo" -- $GATE --dir "$D" --trust "$TMP/trust-link-fuera.json"
ln -sf "release-pubkey.asc" "$SIN_REPO/enlace-dentro.asc"
confianza "$TMP/trust-link-dentro.json" "$FP_OK" "enlace-dentro.asc"
esperar ROJO "public_key vía SYMLINK aunque apunte DENTRO" -- $GATE --dir "$D" --trust "$TMP/trust-link-dentro.json"
# ⊕ el positivo se repite AL FINAL: si el confinamiento hubiera roto el camino
# bueno, todos los rojos de arriba serían rojos por el motivo equivocado.
esperar VERDE "y la ruta buena sigue verificando" -- $GATE --dir "$D" --trust "$TMP/trust-ok.json"

echo
echo "⑨ verificar no deja procesos colgando"
# La aguja lleva corchetes: sin ellos el propio `grep` sale en `ps` y el contador
# se cuenta a sí mismo. Medido: dio 2 antes y 2 después con CERO agentes reales.
agentes_efimeros() { ps -eo command | grep '[g]pg-agent' | grep -c '[l]lminbox-verify-'; }
ANTES="$(agentes_efimeros)"
construir "$D"; $GATE --dir "$D" --trust "$TMP/trust-ok.json" >/dev/null 2>&1
sleep 1
DESPUES="$(agentes_efimeros)"
CONTROL="$(ps -eo command | grep -c "[g]pg-agent --homedir $GNUPGHOME")"
if [ "$CONTROL" -ge 1 ]; then
  ok=$((ok+1)); printf '  ✓ %-52s (control, %s visible)\n' "la aguja ve un agente real" "$CONTROL"
else
  mal=$((mal+1)); printf '  ✗ %-52s la aguja no ve NI el agente del propio test\n' "control de la aguja"
fi
if [ "$DESPUES" -eq 0 ] && [ "$ANTES" -eq 0 ]; then
  ok=$((ok+1)); printf '  ✓ %-52s (antes=%s despues=%s)\n' "0 gpg-agent con el homedir efímero del gate" "$ANTES" "$DESPUES"
else
  mal=$((mal+1)); printf '  ✗ %-52s antes=%s despues=%s\n' "FUGA de gpg-agent" "$ANTES" "$DESPUES"
fi

echo
echo "casos: $((ok+mal)) · verdes esperados y obtenidos: $ok · fallos: $mal"
[ "$mal" -eq 0 ] && exit 0 || exit 1
