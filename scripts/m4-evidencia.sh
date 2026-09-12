#!/usr/bin/env bash
# Biblioteca de EVIDENCIA de M4. No despliega nada: sólo mide y sabe decir que no.
#
# Se separa de los dos guiones que la usan porque el pilotaje y la reversión tienen que
# medir EXACTAMENTE lo mismo: si cada uno captura sus campos, la comparación antes/después
# deja de ser una comparación. Lo aprendido en M0 es que el `rc` de un paso no describe
# el efecto —hubo un `rc=1` con el contenedor ya recreado—, así que aquí la evidencia son
# siempre HECHOS del daemon y del proceso, nunca códigos de salida.
#
# `set -uo pipefail` sin `-e`, como el resto del repo: cada camino declara su corte.
set -uo pipefail

: "${DOCKER:=docker}"
: "${CURL:=curl}"

rojo() { printf '\033[31m%s\033[0m\n' "$*" >&2; }
verde() { printf '\033[32m%s\033[0m\n' "$*"; }
nota() { printf '· %s\n' "$*"; }

# ── evidencia_contenedor <nombre> → JSON en stdout, rc=1 si no se puede medir ────────
# Los campos son los del contrato M4: id, creado, arrancado, digest de imagen,
# RestartCount y montajes. Van JUNTOS y de UNA sola llamada a `inspect`: pedirlos por
# separado abre una ventana entre lo medido y lo decidido, que es la familia de fallo que
# más veces se ha pagado en este carril.
evidencia_contenedor() {
  local nombre="$1" err raw out rc_d rc_j
  err="$(umask 077; mktemp "${TMPDIR:-/tmp}/m4-inspect-err.XXXXXXXX")" || return 1
  raw="$(umask 077; mktemp "${TMPDIR:-/tmp}/m4-inspect-raw.XXXXXXXX")" || { rm -f "$err"; return 1; }
  # `inspect` COMPLETO + `jq`, y no un `--format` con `dict`: Docker usa plantillas Go y
  # `dict` no es una función suya —lo comprobé pidiéndoselo: «function "dict" not
  # defined»—. La proyección la hace `jq`, que sí existe aquí (1.8.1), sobre la MISMA
  # y única lectura.
  "$DOCKER" inspect "$nombre" >"$raw" 2>"$err"; rc_d=$?
  if [ "$rc_d" -ne 0 ]; then
    local e; e="$(<"$err")"; rm -f "$err" "$raw"
    case "$e" in
      *"No such object"*|*"no such object"*|*"No such container"*)
        printf '{"medible":false,"motivo":"ausente"}\n'; return 1 ;;
      *) printf '{"medible":false,"motivo":"inspect no medible: %s"}\n' \
           "$(printf %s "$e" | tr -d '"' | head -c 120)"; return 1 ;;
    esac
  fi
  out="$(jq -c 'if (type!="array" or length!=1) then error("inspect cardinality")
    else .[0] | if ((.Id|type)!="string" or (.State.Status|type)!="string" or
                     (.Image|type)!="string" or (.Config.Image|type)!="string" or
                     (.Mounts|type)!="array") then error("inspect shape") else {
      medible: true,
      id: .Id,
      creado: .Created,
      arrancado: .State.StartedAt,
      estado: .State.Status,
      reinicios: .RestartCount,
      salud: (.State.Health.Status // "sin-healthcheck"),
      imagen: .Image,
      imagen_nombre: .Config.Image,
      montajes: [.Mounts[]? | {tipo: .Type, nombre: (.Name // ""), origen: .Source,
                               destino: .Destination, rw: .RW}],
      # SÓLO `LLMINBOX_DB` del entorno, NUNCA el resto: `Config.Env` lleva
      # `LLMINBOX_TOKEN` y `LLMINBOX_WATCHER_TOKEN`. Serializar el bloque entero
      # metería secretos en la evidencia, que es un fichero que se comparte.
      db_env: ([.Config.Env[]? | select(startswith("LLMINBOX_DB=")) |
                sub("^LLMINBOX_DB=";"")] | first // "")
  } end end' <"$raw" 2>"$err")"; rc_j=$?
  local e; e="$(<"$err")"; rm -f "$err" "$raw"
  [ "$rc_j" -eq 0 ] && [ -n "$out" ] || {
    printf '{"medible":false,"motivo":"jq no medible: %s"}\n' \
      "$(printf %s "$e" | tr -d '"' | head -c 120)"; return 1; }
  printf '%s\n' "$out"
}

# ── digest INMUTABLE de la imagen que corre ────────────────────────────────────────
# `.Image` es el identificador del daemon; `RepoDigests` es lo que un tercero puede
# volver a bajar. Se publican los dos porque no son lo mismo y confundirlos ya produjo
# una lectura falsa en la auditoría de M0 (el label `com.docker.compose.image` no es
# `.Image`, y eso no es una imagen que falte).
evidencia_imagen() {
  local ref="$1"
  "$DOCKER" image inspect "$ref" 2>/dev/null | jq -c '.[0] | {
      medible: true, id: .Id, repo_tags: .RepoTags, repo_digests: .RepoDigests,
      creada: .Created, descriptor: (.Descriptor.digest // null)}' \
    2>/dev/null || printf '{"medible":false,"motivo":"imagen ausente"}\n'
}

# ── gate POST-RECREATE ─────────────────────────────────────────────────────────────
# Cinco condiciones. Las tres primeras son el contrato C-1/C-5 del runbook y ninguna
# sobra:
#   · el id CAMBIA           → un `restart` conserva el id y cambia `StartedAt`; mirar
#                              sólo la fecha da un reinicio por recreación.
#   · running ∧ StartedAt≠0  → `RestartCount=0` es cierto TAMBIÉN en un contenedor que
#                              nunca arrancó (`created`, cero de Go). Sin estas dos, el
#                              contador verde no distingue sano de nonato.
#   · reinicios = 0
#   · integridad verificada  → y sus CLAVES presentes (ver `gate_integridad`).
#   · artefacto disponible   → la imagen tiene que saber decir qué es.
gate_post_recreate() {
  local nombre="$1" id_antes="$2" api="$3" fallos=0 ev
  ev="$(evidencia_contenedor "$nombre")" || { rojo "⛔ no medible tras recrear"; return 1; }
  local id est desde n
  id="$(printf %s "$ev" | jq -r .id)"
  est="$(printf %s "$ev" | jq -r .estado)"
  desde="$(printf %s "$ev" | jq -r .arrancado)"
  n="$(printf %s "$ev" | jq -r .reinicios)"
  [ "$id" != "$id_antes" ] || { rojo "⛔ RECREATE FALSO: mismo id de contenedor ($id_antes) — esto fue un restart"; fallos=1; }
  [ "$est" = "running" ] || { rojo "⛔ estado=$est, no running"; fallos=1; }
  case "$desde" in 0001-01-01*) rojo "⛔ StartedAt en el cero de Go: nunca arrancó"; fallos=1 ;; esac
  [ "$n" = "0" ] || { rojo "⛔ RestartCount=$n"; fallos=1; }
  gate_integridad "$api" || fallos=1
  gate_artefacto "$api" || fallos=1
  [ "$fallos" -eq 0 ] && verde "✓ gate post-recreate: id nuevo · running · StartedAt real · 0 reinicios · integridad · artefacto"
  return "$fallos"
}

# ── UNA SOLA LECTURA DE /health POR FASE ───────────────────────────────────────────
# `gate_salud`, `gate_integridad`, `gate_artefacto`, `gate_journal` y `gate_outbox`
# pedían `/health` cada uno por su lado: cinco lecturas de un estado MUTABLE para
# decidir UNA cosa, o sea cinco instantes distintos presentados como uno. Con el
# servicio cambiando de estado entre ellas, media decisión se toma sobre una foto y la
# otra media sobre otra.
#
# `salud_lee` cachea la respuesta de la fase; `salud_olvida` la invalida cuando el mundo
# cambia de verdad (tras recrear), que es el único momento en el que releer es correcto.
# ⚠️ LA CACHÉ VIVE EN UN FICHERO, NO EN VARIABLES. Mi primera versión guardaba la
# respuesta en `_SALUD_CACHE`, pero cada llamada es `j="$(salud_lee …)"` y una
# sustitución de mandato corre en SUBSHELL: la asignación se perdía al volver, así que
# la caché no cacheaba nada y los cuatro gates seguían leyendo cuatro veces. El síntoma
# era invisible —todo funcionaba— y sólo lo delató contar las llamadas.
# La RUTA se DERIVA, no se asigna: asignarla dentro de `salud_lee` la perdía por el
# mismo motivo que la caché anterior —subshell—, y entonces `salud_olvida` no tenía
# nada que borrar. Es la misma trampa dos niveles más abajo.
# Se crea al CARGAR la biblioteca, no dentro de `salud_lee`: todos los gates llaman a
# ésta desde una sustitución de mandato y, por tanto, desde subshells distintos. Un
# `mktemp` perezoso dentro de ellas crearía una caché por gate y volvería a mezclar
# instantes. `mktemp -d` hace el nombre impredecible; 0700 hace imposible preplantar
# entradas aun compartiendo TMPDIR.
_M4_SALUD_DIR="$(umask 077; mktemp -d "${TMPDIR:-/tmp}/m4-salud.XXXXXXXX")" || {
  printf '%s\n' '⛔ no pude crear caché privada de /health' >&2; return 1 2>/dev/null || exit 1; }
_M4_SALUD_DIR="$(cd "$_M4_SALUD_DIR" && pwd -P)" || { return 1 2>/dev/null || exit 1; }
chmod 700 "$_M4_SALUD_DIR" || { return 1 2>/dev/null || exit 1; }
python3 - "$_M4_SALUD_DIR" <<'PYEOF' || { return 1 2>/dev/null || exit 1; }
import os,stat,sys
st=os.lstat(sys.argv[1])
if (not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode) or st.st_uid!=os.getuid()
    or stat.S_IMODE(st.st_mode)!=0o700): raise SystemExit("caché health no privada/real")
PYEOF
export _M4_SALUD_DIR

_m4_limpia_salud() {
  local p="${1:-${_M4_SALUD_DIR:-}}"
  [ -n "$p" ] || return 0
  python3 - "$p" <<'PYEOF' 2>/dev/null || true
import os,shutil,stat,sys,tempfile
p=os.path.abspath(sys.argv[1]); parent=os.path.dirname(p); st=os.lstat(p)
if (stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode) and st.st_uid==os.getuid()
    and os.path.realpath(parent)==parent and os.path.basename(p).startswith("m4-salud.")):
 shutil.rmtree(p)
PYEOF
}
trap '_m4_limpia_salud' EXIT

_salud_key() {
  printf %s "$1" | python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())'
}

_salud_publica() {   # <api> <key>; el lock ya pertenece a este proceso
  local api="$1" key="$2" raiz="$_M4_SALUD_DIR" tmp rc
  tmp="$(umask 077; mktemp -d "$raiz/.entrada-$key.XXXXXXXX")" || return 1
  "$CURL" -sf --max-time 5 "$api/health" > "$tmp/cuerpo" 2>/dev/null; rc=$?
  python3 - "$tmp" "$raiz/$key" "$api" "$key" "$rc" <<'PYEOF'
import hashlib, json, os, stat, sys
tmp, final, api, key, rc = sys.argv[1:]
body = os.path.join(tmp, "cuerpo")
fd = os.open(body, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
try:
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode): raise SystemExit("cuerpo no regular")
    data = b""
    while True:
        block = os.read(fd, 1 << 20)
        if not block: break
        data += block
    os.fsync(fd)
finally: os.close(fd)
meta = json.dumps({"api": api, "key": key, "rc": int(rc),
                   "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)},
                  sort_keys=True, separators=(",", ":")).encode()
m = os.open(os.path.join(tmp, "meta.json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL |
            getattr(os, "O_NOFOLLOW", 0), 0o600)
try:
    pos=0
    while pos<len(meta): pos+=os.write(m,meta[pos:])
    os.fsync(m)
finally: os.close(m)
dfd = os.open(tmp, os.O_RDONLY); os.fsync(dfd); os.close(dfd)
if os.path.lexists(final): raise SystemExit("entrada ya publicada bajo lock")
os.rename(tmp, final)
dfd = os.open(os.path.dirname(final), os.O_RDONLY); os.fsync(dfd); os.close(dfd)
PYEOF
}

salud_lee() {   # <api> → EXACTAMENTE el cuerpo cacheado, rc original del curl
  local api="$1" key entrada lock i rc
  key="$(_salud_key "$api")" || return 1
  entrada="$_M4_SALUD_DIR/$key"; lock="$_M4_SALUD_DIR/$key.lock"
  if [ ! -d "$entrada" ]; then
    if mkdir "$lock" 2>/dev/null; then
      _salud_publica "$api" "$key"; rc=$?
      rmdir "$lock" 2>/dev/null
      [ "$rc" -eq 0 ] || return "$rc"
    else
      i=0
      while [ ! -d "$entrada" ] && [ "$i" -lt 100 ]; do sleep 0.01; i=$((i+1)); done
      [ -d "$entrada" ] || { rojo "⛔ timeout esperando caché de /health"; return 1; }
    fi
  fi
  python3 - "$entrada" "$api" "$key" <<'PYEOF'
import hashlib, json, os, stat, sys
entry, api, key = sys.argv[1:]
if os.path.islink(entry) or not stat.S_ISDIR(os.lstat(entry).st_mode): raise SystemExit(1)
def read_regular(name):
    p = os.path.join(entry, name)
    fd = os.open(p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode): raise SystemExit(1)
        chunks=[]
        while True:
            b=os.read(fd, 1 << 20)
            if not b: break
            chunks.append(b)
        return b"".join(chunks)
    finally: os.close(fd)
meta = json.loads(read_regular("meta.json"))
body = read_regular("cuerpo")
if (meta.get("api") != api or meta.get("key") != key or
    meta.get("sha256") != hashlib.sha256(body).hexdigest() or
    meta.get("bytes") != len(body)): raise SystemExit(1)
sys.stdout.buffer.write(body)
raise SystemExit(int(meta["rc"]))
PYEOF
}

salud_olvida() {
  # La raíz es privada e impredecible. Se reemplaza COMPLETA, sin glob ni nombres
  # derivados del cliente; una lectura en vuelo conserva su inode y la nueva fase no.
  local vieja="$_M4_SALUD_DIR" nueva
  nueva="$(umask 077; mktemp -d "${TMPDIR:-/tmp}/m4-salud.XXXXXXXX")" || return 1
  _M4_SALUD_DIR="$(cd "$nueva" && pwd -P)" || return 1
  chmod 700 "$_M4_SALUD_DIR" || return 1
  python3 - "$_M4_SALUD_DIR" <<'PYEOF' || return 1
import os,stat,sys
st=os.lstat(sys.argv[1])
if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode) or st.st_uid!=os.getuid() or stat.S_IMODE(st.st_mode)!=0o700: raise SystemExit(1)
PYEOF
  export _M4_SALUD_DIR
  _m4_limpia_salud "$vieja"
}


# ── los campos de integridad se comprueban por PRESENCIA de clave ───────────────────
# Medido en M0: al retroceder a una imagen anterior al gate de integridad, `v8` PIERDE
# `integridad` y `mapa_alterado`, y lo que queda —`credenciales:1, roles_cubiertos:1`—
# se lee como sano. Por eso primero se exige que las claves EXISTAN y sólo después se
# mira el valor: comprobar el valor de una clave ausente es un verde por omisión.
gate_integridad() {
  local api="$1" j
  j="$(salud_lee "$api")" \
    || { rojo "⛔ /health no responde: no se puede acreditar integridad"; return 1; }
  printf %s "$j" | jq -e '.v8 | has("integridad") and has("mapa_alterado")' >/dev/null 2>&1 \
    || { rojo "⛔ el bloque v8 NO declara integridad: imagen anterior al gate. Su AUSENCIA es rojo, no verde"; return 1; }
  printf %s "$j" | jq -e '.v8.integridad=="verificada"' >/dev/null 2>&1 \
    || { rojo "⛔ integridad=$(printf %s "$j" | jq -r .v8.integridad)"; return 1; }
  return 0
}

gate_artefacto() {
  local api="$1" j
  j="$(salud_lee "$api")" || return 1
  # `disponible:true` a solas no acredita nada: una imagen pre-cura o un stub
  # podía publicar exactamente ese booleano. El consumidor vuelve a comprobar
  # el mínimo que convierte el bloque en atestado: versión, lock, cierre exacto
  # del runner y un SHA-256 por cada fuente que ese cierre declara.
  printf %s "$j" | jq -e '
    .artefacto as $a
    | ($a | type) == "object"
      and $a.disponible == true
      and $a.esquema == 1
      and (($a.requirements_lock_sha256 | type) == "string")
      and ($a.requirements_lock_sha256 | length) == 64
      and ($a.requirements_lock_sha256 | test("^[0-9a-f]{64}$"))
      and (($a.fuentes_sha256 | type) == "object")
      and (($a.runner_empaquetado | type) == "object")
      and $a.runner_empaquetado.completo == true
      and $a.runner_empaquetado.faltan == []
      and $a.runner_empaquetado.entrypoints ==
          ["runtime_root", "projector", "projector_runner"]
      and $a.runner_empaquetado.exigidos == [
          "coordination.py", "ledger_parse.py", "native_gateway.py",
          "observability.py", "projector.py", "projector_runner.py",
          "runtime_root.py", "search_contract.py", "search_cursor.py",
          "search_store.py", "servicio.py", "telemetry_bridge.py"
      ]
      and all($a.runner_empaquetado.exigidos[];
          . as $fuente
          | (($a.fuentes_sha256[$fuente] | type) == "string")
            and ($a.fuentes_sha256[$fuente] | length) == 64
            and ($a.fuentes_sha256[$fuente] | test("^[0-9a-f]{64}$")))
  ' >/dev/null 2>&1 \
    || { rojo "⛔ ARTEFACTO NO CONFIABLE: falta esquema, digest o cierre completo del runner"; return 1; }
  return 0
}

# ── OUTBOX: fail-closed, y nombra los (lane, verb) ─────────────────────────────────
# Contrato ADR-001 §Storage, invariante 18. Devuelve rc=0 SÓLO si el outbox se puede
# consultar Y está drenado. Que no se pueda consultar NO es que esté vacío: hoy M1 no
# está integrado y `/health.politica.outbox.disponible` es `false`, así que esto se niega
# — que es lo correcto, porque autorizar por no saber es exactamente el fail-open que
# este gate existe para impedir.
gate_outbox() {
  local api="$1" j ob
  j="$(salud_lee "$api")" \
    || { rojo "⛔ /health no responde: outbox no certificable"; return 1; }
  ob="$(printf %s "$j" | jq -c '.politica.outbox // {"disponible":false,"motivo":"sin bloque politica"}')"
  if [ "$(printf %s "$ob" | jq -r .disponible)" != "true" ]; then
    rojo "⛔ OUTBOX NO CERTIFICABLE: $(printf %s "$ob" | jq -r '.motivo // "sin motivo"')"
    rojo "   «no se puede consultar» NO es «está vacío». La reversión se niega."
    return 1
  fi
  local pend fall
  pend="$(printf %s "$ob" | jq -r '.pendientes // "null"')"
  fall="$(printf %s "$ob" | jq -r '.fallidos // "null"')"
  if [ "$pend" = "null" ] || [ "$fall" = "null" ]; then
    rojo "⛔ el outbox se declara disponible pero no da cifras: no certificable"; return 1
  fi
  if [ "$pend" -gt 0 ] || [ "$fall" -gt 0 ]; then
    rojo "⛔ OUTBOX CON $pend PENDIENTE(S) Y $fall FALLIDO(S). Pares afectados:"
    printf %s "$ob" | jq -r '.pares[]? | "     · lane=\(.lane) verb=\(.verb) pendientes=\(.pendientes // 0) fallidos=\(.fallidos // 0)"' >&2
    rojo "   Drena el outbox o cambia cada (lane, verb) a `bridge` a sabiendas."
    return 1
  fi
  verde "✓ outbox drenado: 0 pendientes, 0 fallidos"
  return 0
}

# ── el destino de una reversión tiene que ser INMUTABLE ────────────────────────────
# `latest` se mueve, así que «volver a latest» no describe un estado: describe lo que
# haya cuando se ejecute. Se acepta `imagen@sha256:<64hex>` siempre; una etiqueta sólo
# si es explícitamente de anclaje (`rollback-…`, `v…`, o con un sha embebido).
destino_inmutable() {
  local ref="$1"
  case "$ref" in
    *@sha256:*)
      # EXACTAMENTE 64 hex en minúscula. Mi primera versión pedía «8 o más» con un glob
      # `[0-9a-f]×8*`, así que aceptaba 8, 63 y 65 — las tres. Un digest corto no es un
      # digest: `sha256:aaaaaaaa` no identifica nada, y Docker lo rechazaría más tarde,
      # ya con el `tag` hecho y el contenedor recreado. La longitud se comprueba AQUÍ,
      # que es donde todavía no se ha tocado nada.
      _d="${ref##*@sha256:}"
      case "${#_d}" in
        64) case "$_d" in
              *[!0-9a-f]*) rojo "⛔ digest con caracteres no hex: $_d"; return 1 ;;
              *) return 0 ;;
            esac ;;
        *) rojo "⛔ digest de ${#_d} caracteres; sha256 son 64 exactos: $_d"; return 1 ;;
      esac ;;
    *:latest|latest)
      rojo "⛔ DESTINO PROHIBIDO: \`latest\` es una etiqueta MÓVIL."
      rojo "   Una reversión a \`latest\` no nombra un estado, nombra lo que haya al ejecutarla."
      rojo "   Usa imagen@sha256:… o una etiqueta de anclaje (rollback-<sha>)."
      return 1 ;;
    *:rollback-*|*:v[0-9]*) return 0 ;;
    *:*)
      rojo "⛔ etiqueta no reconocida como inmutable: ${ref##*:}"
      rojo "   Acepto: @sha256:… · :rollback-<sha> · :v<semver>"
      return 1 ;;
    *) rojo "⛔ destino sin etiqueta ni digest: '$ref' resolvería a \`latest\`"; return 1 ;;
  esac
}


# ── VEREDICTO ESTRUCTURADO, Y FAIL-CLOSED POR CONSTRUCCIÓN ─────────────────────────
# Un guion que sólo imprime texto obliga a que quien lo audita lea prosa y confíe en el
# `rc`. Aquí cada corrida deja un JSON con una entrada por puerta.
#
# FAIL-CLOSED de tres maneras, y las tres hacen falta:
#   ① el veredicto nace `false` y sólo lo mueve una puerta que MIDIÓ algo;
#   ② una puerta que no se llegó a correr queda `no_medido`, que **cuenta como fallo** —
#      no como «pendiente» ni como ausencia benigna;
#   ③ si el proceso muere a mitad, el fichero o no existe o existe con `ok:false`. Nunca
#      hay un instante en el que un veredicto a medias se lea como aprobado, porque el
#      `ok:true` se escribe de una vez, al final, y sólo si NINGUNA puerta falta.
#
# Quien consuma esto trata «fichero ausente» igual que `ok:false`: no saber si pasó no es
# que pasara.
VEREDICTO_PUERTAS=""
# Sólo el consumidor de un plan v4 ya verificado puede cambiar este valor. Se reinicia
# al cargar la biblioteca para que el entorno del operador no pueda autodeclarar el modo.
VEREDICTO_MODO_ACREDITADO="normal"

veredicto_anota() {   # <puerta> <ok|fallo|no_medido|no_aplica> [detalle]
  # `no_aplica` es el ÚNICO estado no-ok que puede no hundir el veredicto, pero sólo
  # CON MOTIVO + modo acreditado solo_binario + puerta en allowlist cerrada.
  # Existe porque un modo puede no tener una puerta —`--solo-binario` no restaura nada—
  # y borrarla de la lista de esperadas la haría invisible: desaparecer del JSON es
  # exactamente como un gate deja de existir sin que nadie lo note. La AUSENCIA sigue
  # dando `no_medido`, que sigue siendo FALLO: `no_aplica` hay que declararlo a mano.
  local p="$1" e="$2" d="${3:-}"
  VEREDICTO_PUERTAS="$VEREDICTO_PUERTAS$(printf '%s\t%s\t%s\n' "$p" "$e" "$d")
"
}

veredicto_escribe() {  # <fichero> <operacion> <instancia> <esperadas...>
  local fichero="$1" op="$2" inst="$3"; shift 3
  printf %s "$VEREDICTO_PUERTAS" | ESPERADAS="$*" OP="$op" INST="$inst" \
    MODO="$VEREDICTO_MODO_ACREDITADO" ARTEFACTOS="$VEREDICTO_ARTEFACTOS" \
    RCS="$VEREDICTO_RCS" python3 -c '
import json, os, sys, datetime
fichero=os.path.abspath(sys.argv[1])
esperadas = os.environ["ESPERADAS"].split()
vistas = {}
for linea in sys.stdin.read().splitlines():
    if not linea.strip():
        continue
    campos = linea.split("\t")
    vistas[campos[0]] = {"estado": campos[1],
                         "detalle": (campos[2] if len(campos) > 2 else "") or None}
puertas = {}
for p in esperadas:
    # AUSENTE ⇒ `no_medido`, y `no_medido` es FALLO. Una puerta que no se corrió no
    # puede desaparecer del veredicto: desaparecer es exactamente como un gate deja de
    # existir sin que nadie lo note.
    puertas[p] = vistas.get(p, {"estado": "no_medido",
                                "detalle": "la puerta no llegó a ejecutarse"})
modo = os.environ.get("MODO", "normal")
def cuenta(p, v):
    if v["estado"] == "ok":
        return True
    # Motivo solo no autoriza: sin operación, modo y puerta cerrados, `no_aplica` sería
    # una puerta trasera capaz de apagar salud/completed con una frase cualquiera.
    return (v["estado"] == "no_aplica" and bool(v.get("detalle")) and
            os.environ["OP"] == "reversion" and modo == "solo_binario" and
            p in {"pre_restore", "restore"})
ok = bool(puertas) and all(cuenta(p, v) for p, v in puertas.items())
# ARTEFACTOS Y RC. Sin el sha256 de cada fichero, el veredicto cita nombres que nadie
# puede volver a comprobar; sin el rc, el paso que falló no se distingue del que no
# corrio. Un artefacto AUSENTE se publica como tal: es un hecho, no un hueco.
artefactos = []
for linea in os.environ.get("ARTEFACTOS", "").splitlines():
    if not linea.strip():
        continue
    c = linea.split("\t")
    artefactos.append({"ruta": c[0], "sha256": c[1] if len(c) > 1 else "AUSENTE",
                       "bytes": int(c[2]) if len(c) > 2 and c[2].isdigit() else 0})
rcs = {}
for linea in os.environ.get("RCS", "").splitlines():
    if not linea.strip():
        continue
    c = linea.split("\t")
    if len(c) > 1:
        rcs[c[0]] = int(c[1]) if c[1].lstrip("-").isdigit() else c[1]
data=json.dumps({"esquema": 2,
           "operacion": os.environ["OP"],
           "instancia": os.environ["INST"],
           "modo": modo,
           "cuando": datetime.datetime.now(datetime.timezone.utc).isoformat(),
           "ok": ok,
           "acreditacion": "acreditado" if ok else "no_acreditado",
           "puertas": puertas,
           "artefactos": artefactos,
           "rc": rcs,
           "fallidas": sorted(p for p, v in puertas.items() if not cuenta(p, v))},
          ensure_ascii=False, indent=1).encode()
fd=os.open(fichero,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0),0o600)
try:
 pos=0
 while pos<len(data): pos+=os.write(fd,data[pos:])
 os.fsync(fd)
finally: os.close(fd)
d=os.open(os.path.dirname(fichero),os.O_RDONLY); os.fsync(d); os.close(d)
raise SystemExit(0 if ok else 1)
' "$fichero"
}

# ── CERROJO DE RESTAURACIÓN ────────────────────────────────────────────────────────
# Dos restauraciones a la vez sobre el mismo almacén no son una cola: son dos escrituras
# pisándose sobre el fichero que existe para poder volver atrás. `mkdir` es atómico en
# POSIX y no necesita `flock`, que no está en macOS — el mismo idioma que ya usa `llmi`
# para su ciclo de vida. Se RECHAZA en vez de esperar, y por el mismo motivo.
cerrojo_restauracion() {   # <instancia>
  _LOCK_RESTORE="${LLMI_LOCK_DIR:-${TMPDIR:-/tmp}}/.m4-restore-$1.lock"
  mkdir "$_LOCK_RESTORE" 2>/dev/null || {
    rojo "⛔ hay otra restauración en marcha sobre \`$1\` (o quedó su cerrojo)."
    rojo "   Dos restauraciones concurrentes se pisan sobre el mismo almacén."
    rojo "   Si estás seguro de que no corre ninguna: rmdir $_LOCK_RESTORE"
    return 1; }
  trap 'rmdir "$_LOCK_RESTORE" 2>/dev/null' EXIT
  return 0
}

# ── el snapshot tiene que estar FUERA del volumen ──────────────────────────────────
# Un respaldo dentro de `/data` desaparece con el volumen que existe para sobrevivir.
# Es el fallo que tenía mi propia primera versión de `m4-pilot.sh`.
snapshot_fuera_del_volumen() {   # <ruta_en_host> <destino_del_volumen>
  local ruta="$1" vol="${2:-/data}"
  case "$ruta" in
    "$vol"/*|"$vol")
      rojo "⛔ el snapshot vive DENTRO del volumen ($vol): perder el volumen se lo lleva."
      rojo "   Un respaldo que muere con lo que respalda no es un respaldo."
      return 1 ;;
  esac
  [ -s "$ruta" ] || { rojo "⛔ el snapshot no existe o está vacío en el host: $ruta"; return 1; }
  return 0
}


# ══════════════════════════════════════════════════════════════════════════════════
# M4-3 · LOS OCHO BLOQUEANTES DE GUION. Todo lo de aquí abajo mide o se niega; nada
# despliega. Cada función tiene su falsador causal en `tests/pytest/test_m4_bloqueos.py`.
# ══════════════════════════════════════════════════════════════════════════════════

# ── ① SALUD: el CUERPO se valida, y `false` es rojo ────────────────────────────────
# `curl -sf` sólo acredita el CÓDIGO HTTP. Un `/health` que responde 200 con
# `{"ok":false}` pasaba los dos gates que había: ninguno miraba el cuerpo entero ni el
# flag, sólo campos de dentro de `.v8`. Un servicio que se declara enfermo y devuelve
# 200 es exactamente el caso que un gate de readiness existe para cazar.
#
# TRES pasos, y el orden importa:
#   ① el cuerpo PARSEA y es un objeto  → un 200 con HTML de un proxy no es salud
#   ② la clave EXISTE                  → mirar el valor de una clave ausente es un
#                                         verde por omisión (misma doctrina que `v8`)
#   ③ el valor es verdadero
gate_salud() {   # <api>  → rc=0 sólo si el cuerpo dice, explícitamente, que está sano
  local api="$1" j
  j="$(salud_lee "$api")" \
    || { rojo "⛔ /health no responde (HTTP no-2xx o sin respuesta)"; return 1; }
  printf %s "$j" | jq -e 'type=="object"' >/dev/null 2>&1 \
    || { rojo "⛔ /health no devuelve un OBJETO JSON: 200 no es salud"; return 1; }
  printf %s "$j" | jq -e 'has("ok")' >/dev/null 2>&1 \
    || { rojo "⛔ /health no declara \`ok\`: su AUSENCIA es rojo, no verde"; return 1; }
  printf %s "$j" | jq -e '.ok==true' >/dev/null 2>&1 \
    || { rojo "⛔ el servicio se declara NO sano: ok=$(printf %s "$j" | jq -c .ok)"
         printf %s "$j" | jq -r '.avisos[]? | "     · " + .' >&2
         return 1; }
  return 0
}

# ── ② JOURNAL: la AUSENCIA nunca es OK ─────────────────────────────────────────────
# La versión anterior anotaba `ok` cuando el servicio decía `disponible:false` «con
# motivo». Un motivo explica una ausencia; no la convierte en presencia. Y el journal de
# coordinación es dato de usuario: revertir sin poder acreditar que sobrevive es
# exactamente lo que ADR-001 §Storage prohíbe.
#
# ⚠️ DEPENDENCIA EXPLÍCITA PENDIENTE: hoy M1 NO está integrado en esta rama, así que
# `.journal.disponible` es `false` y este gate se NIEGA. Es lo correcto y es el estado
# real: no se tapa con un `skip` ni con un default benigno. Quien necesite revertir antes
# de que M1 aterrice usa `--emergencia-sin-journal`, que deja rastro y NO puede salir 0.
gate_journal() {   # <api>
  local api="$1" j blo
  j="$(salud_lee "$api")" \
    || { rojo "⛔ /health no responde: el journal no es certificable"; return 1; }
  printf %s "$j" | jq -e 'has("journal")' >/dev/null 2>&1 \
    || { rojo "⛔ el servicio no publica bloque \`journal\`: imagen anterior a M4."
         rojo "   No puedo acreditar que el journal de coordinación sobreviva."; return 1; }
  blo="$(printf %s "$j" | jq -c .journal)"
  printf %s "$blo" | jq -e 'has("disponible")' >/dev/null 2>&1 \
    || { rojo "⛔ el bloque \`journal\` no declara \`disponible\`"; return 1; }
  if [ "$(printf %s "$blo" | jq -r .disponible)" != "true" ]; then
    rojo "⛔ JOURNAL NO DISPONIBLE: $(printf %s "$blo" | jq -r '.motivo // "sin motivo"')"
    rojo "   Un motivo EXPLICA una ausencia; no la convierte en presencia."
    rojo "   (dependencia pendiente: M1 no integrado en esta rama)"
    return 1
  fi
  return 0
}

# ── ③ IMAGEN: el digest es la evidencia; la etiqueta no ────────────────────────────
# `docker tag` no crea nada: apunta un nombre MUTABLE a un id. Anclar la imagen saliente
# con `rollback-<sha>` y llamarlo evidencia es fiarse de que nadie mueva esa etiqueta —
# y si alguien la mueve, la reversión va a otro sitio sin decirlo. Lo que no se puede
# falsificar sin ser esa imagen es el DIGEST.
digest_de_imagen() {   # <ref> → digest en stdout, rc=1 si no se puede resolver
  local ref="$1" j d
  j="$("$DOCKER" image inspect "$ref" 2>/dev/null)" || { rojo "⛔ imagen ausente: $ref"; return 1; }
  # Se prefiere `RepoDigests` —lo que un tercero puede volver a bajar— y se cae a `.Id`,
  # que identifica el contenido local. Los dos son inmutables; la etiqueta no.
  d="$(printf %s "$j" | jq -r '.[0] | (.RepoDigests[0] // "" | split("@")[1]) // .Id // ""')"
  [ -n "$d" ] && [ "$d" != "null" ] || { rojo "⛔ no pude resolver digest de $ref"; return 1; }
  printf %s "$d"
}

# Compara el digest que se ANCLÓ con el que la referencia resuelve AHORA. Si la etiqueta
# se movió entre el anclaje y la reversión, esto lo dice antes de tocar nada.
verifica_digest() {   # <ref> <digest_esperado>
  local ref="$1" esperado="$2" real
  real="$(digest_de_imagen "$ref")" || return 1
  [ "$real" = "$esperado" ] && return 0
  rojo "⛔ DIGEST DISTINTO. La referencia \`$ref\` ya no es la que se ancló:"
  rojo "     anclado: $esperado"
  rojo "     ahora:   $real"
  rojo "   Una etiqueta es mutable: alguien la movió, o se reconstruyó encima."
  return 1
}

id_de_imagen() {   # <ref> → image ID local inmutable
  local j
  j="$("$DOCKER" image inspect "$1" 2>/dev/null)" || return 1
  printf %s "$j" | jq -er 'if length==1 and (.[0].Id|type)=="string" and (.[0].Id|startswith("sha256:")) then .[0].Id else error("image id") end'
}

# ── ④ SNAPSHOT: sha256 · integrity_check · versión de esquema ──────────────────────
# «No vacío y fuera del volumen» no dice que el fichero sea una base íntegra ni de qué
# versión. Restaurar desde un snapshot truncado o de otro esquema es peor que no
# restaurar: deja el almacén en un estado que nadie declaró.
valida_snapshot() {   # <ruta> [volumen] [sha_esperado] → inventario JSON en stdout
    # ⛔ SÓLO ACREDITA EL ÍNDICE RECONSTRUIBLE. Un snapshot del journal de coordinación
    # se rechaza AQUÍ, por identidad y contenido, no por su nombre ni por su ruta.
    # EL TERCER ARGUMENTO ES LO QUE SEPARA «una base íntegra» de «LA base». Sin él se
    # aceptaba cualquier SQLite que superara `integrity_check`: un fichero perfectamente
    # sano y perfectamente ajeno pasa esa prueba. Mi versión anterior LO DECLARABA en la
    # firma y no lo usaba —el parche no llegó a aplicarse—, que es peor que no tenerlo:
    # la firma prometía una comprobación que no ocurría.
    local ruta="$1" vol="${2:-/data}" esperado="${3:-}"
    snapshot_fuera_del_volumen "$ruta" "$vol" || return 1
    # DATOS POR ARGV, NUNCA INTERPOLADOS EN EL FUENTE. Una ruta con comilla o salto de
    # línea dentro de un heredoc sin comillar es ejecución de código.
    python3 - "$ruta" "$esperado" <<'PYEOF' || return 1
import hashlib, json, os, sqlite3, stat, sys
ruta, esperado = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "")
h = hashlib.sha256()
if not hasattr(os, "O_NOFOLLOW"):
    raise SystemExit("⛔ la plataforma no ofrece O_NOFOLLOW")
# ── CLASIFICADOR · segunda implementación, a propósito ──────────────────────────────
# La primera vive en `scripts/m4-restore-helper.py`, que se ejecuta DENTRO de un
# contenedor efímero montado como UN SOLO fichero (`helper_volumen`: `-v $prog:/m4/
# restore.py:ro`) y con su sha verificado antes del `exec`. Compartir módulo obligaría a
# un segundo bind, un segundo hash y un campo más en el plan — más superficie, no menos.
# El precio de las dos copias es la DERIVA, y se paga con un test que exige que las dos
# clasifiquen igual el mismo corpus (`test_las_dos_implementaciones_clasifican_igual`):
# así la deriva sale ROJA en vez de quedar en confianza.
JOURNAL_TABLAS = {"principals", "credential_bindings", "runtime_sessions", "events",
                  "receipts", "receipt_transitions", "idempotency", "outbox", "leases",
                  "commands"}
JOURNAL_META = {"durable_v", "pepper_check", "generation"}
INDICE_TABLAS = {"entries", "recipients", "files", "cursors"}
INDICE_META = {"schema_v"}
INDICE_SCHEMA_V = "e7f6c011776e8db7"  # sha256(str(SCHEMA_V=6))[:16]
INDICE_DDL = """
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE entries (
  ledger TEXT NOT NULL, eid TEXT NOT NULL,
  arrival INTEGER, seq INTEGER, line_no INTEGER, byte_off INTEGER,
  ts TEXT, actor TEXT, tipo TEXT, head TEXT, body TEXT,
  visto TEXT, ausente TEXT, provisional INTEGER DEFAULT 0,
  raw_tipo TEXT, canonical_kind TEXT, kind_registry_rev INTEGER,
  PRIMARY KEY (ledger, eid));
CREATE INDEX i_arr ON entries(ledger, arrival);
CREATE INDEX i_seq ON entries(ledger, seq);
CREATE INDEX i_ts ON entries(ledger, ts);
CREATE INDEX i_actor ON entries(ledger, actor);
CREATE INDEX i_tipo ON entries(ledger, tipo);
CREATE INDEX i_raw_tipo ON entries(raw_tipo COLLATE NOCASE, ledger);
CREATE TABLE recipients (
  ledger TEXT NOT NULL, eid TEXT NOT NULL, who TEXT NOT NULL,
  PRIMARY KEY (ledger, eid, who));
CREATE INDEX i_who ON recipients(who, ledger, eid);
CREATE TABLE files (
  ledger TEXT PRIMARY KEY, path TEXT, bytes INTEGER, entries INTEGER,
  mtime REAL, scanned REAL);
CREATE TABLE cursors (
  agent TEXT NOT NULL, ledger TEXT NOT NULL, last_arrival INTEGER, updated TEXT,
  PRIMARY KEY (agent, ledger));
"""
SIDECARS = ("-wal", "-shm", "-journal")
INDICE_OBJETOS_DDL = (
    "meta", "entries", "recipients", "files", "cursors",
    "i_arr", "i_seq", "i_ts", "i_actor", "i_tipo", "i_raw_tipo", "i_who")


def sidecars_presentes(path):
    return tuple(s for s in SIDECARS if os.path.lexists(path + s))


def ascii_sqlite_fold(valor):
    """Case-insensitive de identificadores SQLite: sólo ASCII, nunca Unicode."""
    if not isinstance(valor, str):
        return valor
    return "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in valor)


def forma_tabla(db, tabla):
    columnas = tuple(
        (ascii_sqlite_fold(r[1]), ascii_sqlite_fold(r[2]), r[3], r[4], r[5])
        for r in db.execute(f'PRAGMA table_info("{tabla}")'))
    indices = []
    for r in db.execute(f'PRAGMA index_list("{tabla}")'):
        nombre = r[1]
        cols = tuple(
            (ascii_sqlite_fold(x[2]), x[3], ascii_sqlite_fold(x[4]), x[5])
            for x in db.execute(f'PRAGMA index_xinfo("{nombre}")'))
        indices.append((ascii_sqlite_fold(nombre), bool(r[2]),
                        ascii_sqlite_fold(r[3]), bool(r[4]), cols))
    fks = tuple(
        tuple(ascii_sqlite_fold(v) for v in r[2:8])
        for r in db.execute(f'PRAGMA foreign_key_list("{tabla}")'))
    return columnas, tuple(sorted(indices)), fks


def canon_sql(sql, identificadores=()):
    """Ignora comentarios, espacios y case ASCII; conserva cláusulas/literales."""
    if not isinstance(sql, str):
        return ()
    identificadores = {ascii_sqlite_fold(x) for x in identificadores}
    tokens = []
    i = 0
    n = len(sql)
    while i < n:
        c = sql[i]
        if c.isspace():
            i += 1
            continue
        if sql.startswith("--", i):
            fin = sql.find("\n", i + 2)
            i = n if fin < 0 else fin + 1
            continue
        if sql.startswith("/*", i):
            fin = sql.find("*/", i + 2)
            if fin < 0:
                return (("error", "comentario-sin-cerrar"),)
            i = fin + 2
            continue
        if c == "'":
            inicio = i
            i += 1
            while i < n:
                if sql[i] == c:
                    if i + 1 < n and sql[i + 1] == c:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            else:
                return (("error", "literal-sin-cerrar"),)
            tokens.append(("literal", sql[inicio:i]))
            continue
        if c in ('"', "`"):
            i += 1
            partes = []
            while i < n:
                if sql[i] == c:
                    if i + 1 < n and sql[i + 1] == c:
                        partes.append(c); i += 2
                        continue
                    i += 1
                    break
                partes.append(sql[i]); i += 1
            else:
                return (("error", "identificador-sin-cerrar"),)
            tokens.append(("ident", ascii_sqlite_fold("".join(partes))))
            continue
        if c == "[":
            fin = sql.find("]", i + 1)
            if fin < 0:
                return (("error", "identificador-sin-cerrar"),)
            tokens.append(("ident", ascii_sqlite_fold(sql[i + 1:fin])))
            i = fin + 1
            continue
        if c.isalnum() or c in "_$":
            fin = i + 1
            while fin < n and (sql[fin].isalnum() or sql[fin] in "_$"):
                fin += 1
            palabra = ascii_sqlite_fold(sql[i:fin])
            tokens.append(("ident" if palabra in identificadores else "word", palabra))
            i = fin
            continue
        op = next((x for x in ("->>", "||", "<<", ">>", "<=", ">=", "==", "!=", "<>", "->")
                   if sql.startswith(x, i)), None)
        if op:
            tokens.append(("op", op)); i += len(op)
        else:
            tokens.append(("punct", c)); i += 1
    return tuple(tokens)


def identificadores_canonicos(ref):
    ids = set()
    for nombre, tabla in ref.execute(
            "SELECT name, tbl_name FROM sqlite_master WHERE sql IS NOT NULL"):
        ids.update((ascii_sqlite_fold(nombre), ascii_sqlite_fold(tabla)))
    for tabla in ("meta", *sorted(INDICE_TABLAS)):
        ids.update(ascii_sqlite_fold(r[1]) for r in ref.execute(
            f'PRAGMA table_info("{tabla}")'))
        for r in ref.execute(f'PRAGMA index_list("{tabla}")'):
            ids.add(ascii_sqlite_fold(r[1]))
            for x in ref.execute(f'PRAGMA index_xinfo("{r[1]}")'):
                if x[2] is not None:
                    ids.add(ascii_sqlite_fold(x[2]))
                if x[4] is not None:
                    ids.add(ascii_sqlite_fold(x[4]))
    return ids


def ddl_indice_canonico(db, ref):
    ids = identificadores_canonicos(ref)
    reales = {}
    for nombre, tipo, tabla, sql in db.execute(
            "SELECT name, type, tbl_name, sql FROM sqlite_master"):
        clave = ascii_sqlite_fold(nombre)
        if clave in reales:
            return False
        reales[clave] = (tipo, tabla, sql)
    for nombre in INDICE_OBJETOS_DDL:
        real = reales.get(ascii_sqlite_fold(nombre))
        esperado = ref.execute(
            "SELECT type, tbl_name, sql FROM sqlite_master WHERE name=?", (nombre,)
        ).fetchone()
        if not real or not esperado:
            return False
        if (tuple(ascii_sqlite_fold(x) for x in real[:2]) !=
                tuple(ascii_sqlite_fold(x) for x in esperado[:2]) or
                canon_sql(real[2], ids) != canon_sql(esperado[2], ids)):
            return False
    return True


def forma_indice_canonica(db):
    ref = sqlite3.connect(":memory:")
    try:
        ref.executescript(INDICE_DDL)
        tablas = ("meta", *sorted(INDICE_TABLAS))
        tablas_indice = {"meta", *(ascii_sqlite_fold(t) for t in INDICE_TABLAS)}
        sin_triggers = not any(
            ascii_sqlite_fold(r[0]) in tablas_indice
            for r in db.execute("SELECT tbl_name FROM sqlite_master WHERE type='trigger'")
        )
        return (sin_triggers and ddl_indice_canonico(db, ref) and
                all(forma_tabla(db, t) == forma_tabla(ref, t) for t in tablas))
    finally:
        ref.close()


def clasifica(con):
    tablas = {ascii_sqlite_fold(r[0]) for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    claves = set()
    valores = {}
    if "meta" in tablas:
        try:
            valores = dict(con.execute("SELECT k, v FROM meta"))
            claves = set(valores)
        except (TypeError, ValueError, sqlite3.Error) as e:
            return ("indeterminado", f"`meta` ilegible: {e}")
    senales = sorted(tablas & JOURNAL_TABLAS) + sorted(f"meta.{k}" for k in claves & JOURNAL_META)
    if senales:
        return ("journal", "señales de journal de coordinación: " + ", ".join(senales))
    faltan = sorted(INDICE_TABLAS - tablas) + sorted(f"meta.{k}" for k in INDICE_META - claves)
    if faltan:
        return ("indeterminado", "no acredita ser el índice; le faltan: " + ", ".join(faltan))
    if valores.get("schema_v") != INDICE_SCHEMA_V:
        return ("indeterminado", f"meta.schema_v no es el sello canónico {INDICE_SCHEMA_V}")
    if not forma_indice_canonica(con):
        return ("indeterminado", "DDL/columnas/índices no coinciden con el manifiesto "
                "canónico del índice")
    return ("indice", "índice reconstruible: sello y manifiesto canónicos")


try:
    calientes = sidecars_presentes(ruta)
    if calientes:
        raise RuntimeError("bundle caliente con sidecars: " + ", ".join(calientes))
    fd = os.open(ruta, os.O_RDONLY | os.O_NOFOLLOW)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        raise RuntimeError("snapshot no regular")
    identidad = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
    for b in iter(lambda: os.read(fd, 1 << 20), b""):
        h.update(b)
    os.lseek(fd, 0, os.SEEK_SET)
    con = sqlite3.connect(f"file:/dev/fd/{fd}?mode=ro&immutable=1", uri=True)
    integridad = con.execute("PRAGMA integrity_check").fetchone()[0]
    clase, motivo = clasifica(con)
    con.close()
    os.close(fd)
    calientes = sidecars_presentes(ruta)
    if calientes:
        raise RuntimeError("aparecieron sidecars durante la lectura: " + ", ".join(calientes))
    ahora = os.stat(ruta, follow_symlinks=False)
    if (ahora.st_dev, ahora.st_ino, ahora.st_size, ahora.st_mtime_ns) != identidad:
        raise RuntimeError("el path cambió durante la lectura")
except (OSError, RuntimeError, sqlite3.Error) as e:
    try:
        con.close()
    except (NameError, sqlite3.Error):
        pass
    try:
        os.close(fd)
    except (NameError, OSError):
        pass
    print(f"⛔ el snapshot no se puede abrir como SQLite: {e}", file=sys.stderr)
    sys.exit(1)
if integridad != "ok":
    print(f"⛔ integrity_check = {integridad!r}: NO es una base íntegra", file=sys.stderr)
    sys.exit(1)
# 🩸 AQUÍ SE EXIGÍA `durable_v`, Y SELECCIONABA AL REVÉS: `durable_v` es el sello del
# JOURNAL; el índice escribe `meta.schema_v` y nunca aquél. La comprobación rechazaba
# todo snapshot REAL del índice y aceptaba uno del journal.
if clase == "journal":
    print(f"⛔ el snapshot es el JOURNAL de coordinación: {motivo}.\n"
          "   La restauración genérica NO lo toca (ADR-001): necesita su propio\n"
          "   procedimiento, que hoy NO existe.", file=sys.stderr)
    sys.exit(1)
if clase != "indice":
    print(f"⛔ el snapshot no acredita ser el índice reconstruible: {motivo}",
          file=sys.stderr)
    sys.exit(1)
real = h.hexdigest()
if esperado and real != esperado:
    print(f"⛔ el snapshot NO es el que se ancló:\n     esperado: {esperado}\n"
          f"     este:     {real}\n"
          "   `integrity_check` dice que una base está SANA, no que sea LA base.",
          file=sys.stderr)
    sys.exit(1)
json.dump({"sha256": real, "bytes": identidad[2],
           "integrity_check": integridad, "clase": clase,
           "sha_verificado_contra_ancla": bool(esperado)},
          sys.stdout, ensure_ascii=False)
PYEOF
}

# ── EL VOLUMEN SE NOMBRA, NO SE APUNTA POR RUTA ────────────────────────────────────
# `.Mounts[].Source` en Docker Desktop es la ruta INTERNA de la VM
# (`/var/lib/docker/volumes/…/_data`), que `-v` interpreta como bind del host y no
# resuelve al mismo almacén. Lo que `-v` entiende es el NOMBRE del volumen.
#
# Y se exige EXACTAMENTE UN montaje que cumpla las cuatro condiciones: tipo `volume`,
# destino == mountpoint, `rw:true` y nombre no vacío. Dos montajes al mismo destino, o
# uno de tipo `bind`, o uno de sólo lectura, describen un almacén que este flujo no
# sabe restaurar — y elegir «el primero» sería elegir a ciegas.
volumen_del_mount() {   # <evidencia_json> <mountpoint> → nombre del volumen en stdout
  local ev="$1" mnt="$2" cand n
  # SE CUENTAN PRIMERO LOS MONTAJES AL DESTINO, y sólo después se aplican los
  # criterios. Filtrar antes de contar hacía que un bind y un volumen sobre el mismo
  # destino dejaran UN candidato y pasaran por buenos: la ambigüedad desaparecía en el
  # propio filtro que existía para verla.
  local todos
  todos="$(printf %s "$ev" | jq -c --arg m "$mnt" '[.montajes[]? | select(.destino==$m)]')"
  if [ "$(printf %s "$todos" | jq 'length')" -gt 1 ]; then
    rojo "⛔ $(printf %s "$todos" | jq 'length') montajes distintos apuntan a \`$mnt\`:"
    rojo "   elegir uno sería a ciegas."
    printf %s "$todos" | jq -r '.[] | "     visto: tipo=\(.tipo) nombre=\(.nombre) rw=\(.rw)"' >&2
    return 1
  fi
  cand="$(printf %s "$todos" | jq -c \
    '[.[] | select(.tipo=="volume" and .rw==true and (.nombre|length)>0)]')"
  n="$(printf %s "$cand" | jq 'length')"
  case "$n" in
    1) printf %s "$(printf %s "$cand" | jq -r '.[0].nombre')"; return 0 ;;
    0) rojo "⛔ no hay UN volumen con nombre, rw y destino \`$mnt\`."
       rojo "   (un bind, un montaje :ro o un volumen anónimo no sirven aquí)"
       printf %s "$ev" | jq -r --arg m "$mnt" \
         '.montajes[]? | select(.destino==$m) |
          "     visto: tipo=\(.tipo) nombre=\(.nombre) rw=\(.rw)"' >&2
       return 1 ;;
    *) rojo "⛔ estado imposible en la derivación de volumen"; return 1 ;;
  esac
}

# ── LA RUTA DE LA DB SALE DEL MISMO `inspect`, y tiene que caer BAJO el mount ───────
# Tomarla de `LLMINBOX_DB_INTERNO` del shell describe el entorno de quien ejecuta, no el
# del contenedor congelado — y entre las dos fases ese shell puede ser otro.
db_del_contenedor() {   # <evidencia_json> <mountpoint> → ruta en stdout
  local ev="$1" mnt="$2" db
  db="$(printf %s "$ev" | jq -r '.db_env // ""')"
  [ -n "$db" ] || db="$mnt/llminbox.sqlite"        # defecto CONTRACTUAL, no del shell
  MNT="$mnt" DB="$db" python3 - <<'PYEOF' || return 1
import os, posixpath, sys
mnt = posixpath.normpath(os.environ["MNT"])
db = os.environ["DB"]
norm = posixpath.normpath(db)
if not posixpath.isabs(norm):
    print(f"⛔ la ruta de la DB no es absoluta: {db!r}", file=sys.stderr); sys.exit(1)
if norm != db and ".." in db.split("/"):
    print(f"⛔ travesía en la ruta de la DB: {db!r}", file=sys.stderr); sys.exit(1)
if norm != mnt and not norm.startswith(mnt.rstrip("/") + "/"):
    print(f"⛔ la DB {norm!r} NO cae bajo el montaje {mnt!r}: el helper tocaría algo "
          "que este flujo no ha congelado", file=sys.stderr)
    sys.exit(1)
print(norm)
PYEOF
}

# ── HELPER EFÍMERO: `docker exec` NO existe contra un contenedor PARADO ─────────────
# Mi staged restore corría `docker exec` DESPUÉS de exigir el servicio parado: los dos
# requisitos juntos son imposibles y la ruta entera era inejecutable. La operación sobre
# el volumen se hace con un contenedor EFÍMERO que monta los mismos volúmenes y NO
# arranca la aplicación —`entrypoint` vacío y un `python3` directo—, así que el almacén
# se toca sin que nadie lo tenga abierto.
helper_volumen() {   # <image-id> <vol> <mnt> <script> <helper-sha> <snapshot> <db> <token> <sha> <modo>
  # `--network none`: esto toca datos, no habla con nadie. El script y el snapshot van
  # en SÓLO LECTURA: el helper no puede reescribir su propio programa ni el punto de
  # restauración, que es lo único a lo que se podría volver.
  #
  # ⚠️ EL SNAPSHOT ENTRA COMO FUENTE, NUNCA COMO ETAPA. Un bind `:ro` del host es OTRO
  # filesystem que el volumen: `os.replace` desde ahí da `EXDEV` siempre, así que la
  # fase 2 habría fallado en el 100% de las corridas. El helper COPIA sus bytes a una
  # etapa creada DENTRO del volumen y sólo esa entra en el renombrado.
  local img="$1" vol="$2" mnt="$3" prog="$4" progsha="$5" snap="$6" db="$7" sello="$8" sha="$9" modo="${10}"
  "$DOCKER" run --rm --network none --entrypoint python3 \
    -v "$vol:$mnt" -v "$prog:/m4/restore.py:ro" -v "$snap:/m4/fuente.sqlite:ro" \
    "$img" -c 'import hashlib,sys; p=sys.argv[1]; expected=sys.argv[2]; b=open(p,"rb").read();\
assert hashlib.sha256(b).hexdigest()==expected,"helper hash mismatch";\
sys.argv=[p]+sys.argv[3:]; exec(compile(b,p,"exec"),{"__name__":"__main__","__file__":p})' \
    /m4/restore.py "$progsha" "$db" /m4/fuente.sqlite "$sello" "$sha" "$modo"
}

# ── PLAN SELLADO DE VERDAD ─────────────────────────────────────────────────────────
# «Sellado» no es escribir un nonce DENTRO del JSON: si el token vive en el mismo
# fichero editable, quien puede cambiar el plan puede cambiar su token, y el token no
# acredita nada. El token de reanudación es el SHA256 EXACTO del fichero y NO se guarda
# dentro: alterar una coma cambia el hash y la fase 2 se niega.
evidencia_prepara() {   # <directorio>: privado, propio y nunca symlink
  python3 - "$1" <<'PYEOF'
import os, stat, sys
p=os.path.abspath(sys.argv[1])
try: os.mkdir(p,0o700)
except FileExistsError: pass
st=os.lstat(p)
if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode) or st.st_uid!=os.getuid() or os.path.realpath(p)!=p:
 print(f"⛔ EVID no es un directorio real, confinado y propio: {p}",file=sys.stderr); raise SystemExit(1)
os.chmod(p,0o700)
PYEOF
}

texto_publica() {   # <ruta> <contenido>: evidencia O_EXCL y durable
  python3 - "$1" "$2" <<'PYEOF'
import os,sys
p=os.path.abspath(sys.argv[1]); b=sys.argv[2].encode(); d=os.path.dirname(p)
if os.path.realpath(d)!=d: raise SystemExit("directorio no confinado")
fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0),0o600)
try:
 pos=0
 while pos<len(b): pos+=os.write(fd,b[pos:])
 os.fsync(fd)
finally: os.close(fd)
dd=os.open(d,os.O_RDONLY); os.fsync(dd); os.close(dd)
PYEOF
}

archivo_copia() {   # <origen> <destino> → inventario; una apertura de origen
  python3 - "$1" "$2" <<'PYEOF'
import hashlib,json,os,stat,sys
src,dst=map(os.path.abspath,sys.argv[1:3]); d=os.path.dirname(dst)
sfd=os.open(src,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0))
try:
 if not stat.S_ISREG(os.fstat(sfd).st_mode): raise SystemExit("origen no regular")
 dfd=os.open(dst,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0),0o600)
 h=hashlib.sha256(); n=0
 try:
  while True:
   b=os.read(sfd,1<<20)
   if not b: break
   h.update(b); n+=len(b)
   pos=0
   while pos<len(b): pos+=os.write(dfd,b[pos:])
  os.fsync(dfd)
 finally: os.close(dfd)
finally: os.close(sfd)
dd=os.open(d,os.O_RDONLY); os.fsync(dd); os.close(dd)
print(json.dumps({"ruta":dst,"sha256":h.hexdigest(),"bytes":n},separators=(",",":")))
PYEOF
}

salud_sella() {   # <api> <health> <outbox> <journal>; derivados de los mismos bytes
  local api="$1" h="$2" o="$3" j="$4" key entrada
  salud_lee "$api" >/dev/null || return 1
  key="$(_salud_key "$api")" || return 1; entrada="$_M4_SALUD_DIR/$key"
  python3 - "$entrada" "$api" "$key" "$h" "$o" "$j" <<'PYEOF'
import hashlib,json,os,stat,sys
entry,api,key,*paths=sys.argv[1:]
def rd(p):
 fd=os.open(p,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0))
 try:
  if not stat.S_ISREG(os.fstat(fd).st_mode): raise SystemExit("no regular")
  out=[]
  while True:
   b=os.read(fd,1<<20)
   if not b: break
   out.append(b)
  return b"".join(out)
 finally: os.close(fd)
meta=json.loads(rd(os.path.join(entry,"meta.json"))); body=rd(os.path.join(entry,"cuerpo"))
if meta.get("api")!=api or meta.get("key")!=key or meta.get("sha256")!=hashlib.sha256(body).hexdigest():
 raise SystemExit("caché health no corresponde a API/bytes")
doc=json.loads(body)
vals=[body,json.dumps(doc.get("politica",{}).get("outbox"),sort_keys=True,separators=(",",":")).encode(),json.dumps(doc.get("journal"),sort_keys=True,separators=(",",":")).encode()]
res={}
for label,p,b in zip(("health","outbox","journal"),paths,vals):
 p=os.path.abspath(p); d=os.path.dirname(p)
 fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0),0o600)
 try:
  pos=0
  while pos<len(b): pos+=os.write(fd,b[pos:])
  os.fsync(fd)
 finally: os.close(fd)
 dd=os.open(d,os.O_RDONLY); os.fsync(dd); os.close(dd)
 res[label]={"ruta":p,"sha256":hashlib.sha256(b).hexdigest(),"bytes":len(b)}
print(json.dumps(res,sort_keys=True,separators=(",",":")))
PYEOF
}

plan_escribe() {   # <ruta> <json> → token; staging+link O_EXCL+fsync
  python3 - "$1" "$2" <<'PYEOF'
import hashlib,os,sys,tempfile
final=os.path.abspath(sys.argv[1]); data=sys.argv[2].encode(); d=os.path.dirname(final)
if os.path.realpath(d)!=d: raise SystemExit("directorio de plan no confinado")
fd,tmp=tempfile.mkstemp(prefix=".m4-plan-",dir=d)
try:
 os.fchmod(fd,0o600); pos=0
 while pos<len(data): pos+=os.write(fd,data[pos:])
 os.fsync(fd); os.close(fd); fd=-1
 os.link(tmp,final,follow_symlinks=False)
 dd=os.open(d,os.O_RDONLY); os.fsync(dd); os.close(dd)
finally:
 if fd>=0: os.close(fd)
 try: os.unlink(tmp)
 except FileNotFoundError: pass
print(hashlib.sha256(data).hexdigest())
PYEOF
}

plan_lee_verifica() {   # <ruta> <token> <instancia>; una apertura, mismos bytes
  python3 - "$1" "$2" "$3" <<'PYEOF'
import hashlib,json,os,stat,sys
ruta,token,inst=sys.argv[1:]
fd=os.open(ruta,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0))
try:
 st=os.fstat(fd)
 if not stat.S_ISREG(st.st_mode): raise ValueError("plan no regular")
 chunks=[]
 while True:
  block=os.read(fd,1<<20)
  if not block: break
  chunks.append(block)
finally: os.close(fd)
raw=b"".join(chunks); real=hashlib.sha256(raw).hexdigest()
def mal(m): print("NO "+m); raise SystemExit(0)
if real!=token: mal(f"TOKEN QUE NO CORRESPONDE (real {real})")
try: p=json.loads(raw)
except Exception as e: mal(f"plan ilegible: {e}")
if p.get("esquema")!=4 or p.get("operacion")!="reversion": mal("no es plan de reversión v4")
if p.get("instancia")!=inst: mal("instancia distinta")
# `restaura_indice` gobierna el eje de DATOS y por eso se exige BOOLEANO EXPLÍCITO: un
# campo ausente que se leyera como `False` convertiría un plan viejo, o uno truncado, en
# una autorización a saltarse la restauración sin que nadie lo declarara.
if not isinstance(p.get("restaura_indice"), bool): mal("`restaura_indice` ausente o no booleano")
req=["destino_imagen","destino_digest","destino_image_id","volumen","volumen_identidad","mountpoint","db_interna","contenedor_id","imagen_digest","imagen_helper","imagen_helper_digest","evid_root","evidencias"]
if p["restaura_indice"]: req += ["snapshot","snapshot_sha"]
for k in req:
 if not p.get(k): mal(f"falta {k}")
# Y el reverso: con `false` los dos campos tienen que estar en NULL. Dejarlos puestos
# haría que un plan solo-binario cargara una ruta de datos que nadie va a validar, y esa
# ruta es lo único que la fase 2 necesitaría para restaurar por error.
if not p["restaura_indice"]:
 if "snapshot" not in p or "snapshot_sha" not in p:
  mal("plan solo-binario sin los null explícitos de snapshot/snapshot_sha")
 if p["snapshot"] is not None or p["snapshot_sha"] is not None:
  mal("plan solo-binario con snapshot cargado: exige null exacto en ambos campos")
p["_plan_sha"]=real; p["_plan_bytes"]=len(raw)
print("OK "+json.dumps(p,sort_keys=True,separators=(",",":")))
PYEOF
}

evidencias_verifica() {   # <plan-json>: cada artefacto regular, no-symlink y hash exacto
  python3 - "$1" <<'PYEOF'
import hashlib,json,os,stat,sys
p=json.loads(sys.argv[1]); ev=p.get("evidencias",{}); root=os.path.abspath(p.get("evid_root", ""))
if not root or os.path.realpath(root)!=root: raise SystemExit("EVID del plan no canónico")
# El artefacto `snapshot` sólo existe si la fase 1 restauraba el índice. Los otros cinco
# se exigen SIEMPRE: son la foto de salud y la identidad del helper, y de esos no depende
# el modo.
nombres=["health","outbox","journal","inspect","helper"]
if p.get("restaura_indice"): nombres.insert(3,"snapshot")
if not isinstance(ev,dict) or set(ev) != set(nombres):
 raise SystemExit("conjunto de evidencias no es el exacto del modo acreditado")
for nombre in nombres:
 item=ev.get(nombre) or {}
 ruta=item.get("ruta"); esperado=item.get("sha256"); n=item.get("bytes")
 if not ruta or not esperado or not isinstance(n,int): raise SystemExit(f"evidencia {nombre} incompleta")
 ruta=os.path.abspath(ruta)
 if os.path.dirname(ruta)!=root: raise SystemExit(f"{nombre} fuera del EVID ligado")
 fd=os.open(ruta,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0))
 try:
  if not stat.S_ISREG(os.fstat(fd).st_mode): raise SystemExit(f"{nombre} no regular")
  h=hashlib.sha256(); total=0
  while True:
   b=os.read(fd,1<<20)
   if not b: break
   h.update(b); total+=len(b)
 finally: os.close(fd)
 if h.hexdigest()!=esperado or total!=n: raise SystemExit(f"{nombre} cambió desde fase 1")
if p.get("restaura_indice") and (ev["snapshot"]["sha256"]!=p["snapshot_sha"] or ev["snapshot"]["ruta"]!=os.path.abspath(p["snapshot"])):
 raise SystemExit("snapshot no coincide con el artefacto ligado")
PYEOF
}

volumen_valida() {   # <nombre> <identidad-json>: identidad fuerte exacta
  local v="$1" esperada="$2" real
  real="$(volumen_identidad "$v")" || { rojo "⛔ volumen $v no existe/no tiene identidad fuerte"; return 1; }
  printf %s "$real" | jq -e --argjson e "$esperada" '.==$e' >/dev/null 2>&1 || {
    rojo "⛔ identidad del volumen $v cambió"; return 1; }
}

volumen_identidad() {   # <nombre> → descriptor fuerte y canónico
  local v="$1" j
  j="$("$DOCKER" volume inspect "$v" 2>/dev/null)" || return 1
  printf %s "$j" | jq -ce --arg v "$v" '
    if length!=1 or .[0].Name!=$v or ((.[0].Driver // "")|length)==0 or
       ((.[0].Mountpoint // "")|length)==0 or ((.[0].CreatedAt // "")|length)==0
       then error("volumen sin identidad fuerte")
    else .[0] | {name:.Name,driver:.Driver,mountpoint:.Mountpoint,
                  scope:(.Scope // ""),created_at:(.CreatedAt // ""),
                  labels:(.Labels // {}),options:(.Options // {})} end'
}

ultimo_stopped_plan() {  # <inst> <cid> <image-id> <vol> <mnt> <db>
  local inst="$1" cid="$2" img="$3" vol="$4" mnt="$5" db="$6" ev rc motivo
  ev="$(evidencia_contenedor "$inst")"; rc=$?
  if [ "$rc" -ne 0 ]; then
    motivo="$(printf %s "$ev" | jq -r '.motivo // ""' 2>/dev/null)"
    [ "$motivo" = "ausente" ] && return 0
    rojo "⛔ último inspect no medible ($motivo)"; return 1
  fi
  printf %s "$ev" | jq -e --arg cid "$cid" --arg img "$img" \
    '.medible==true and .estado=="exited" and .id==$cid and .imagen==$img' >/dev/null 2>&1 || {
      rojo "⛔ último inspect no acredita exited/container/image"; return 1; }
  [ "$(volumen_del_mount "$ev" "$mnt")" = "$vol" ] && \
    [ "$(db_del_contenedor "$ev" "$mnt")" = "$db" ] || {
      rojo "⛔ último inspect no acredita mount/db"; return 1; }
}

estado_actual() {   # <EVID> <token> <instancia>: none|pre_restore|restored|post_restore|completed
  python3 - "$1" "$2" "$3" <<'PYEOF'
import json,os,stat,sys
root=os.path.abspath(sys.argv[1]); token=sys.argv[2]; inst=sys.argv[3]
orden=("pre_restore","restored","post_restore","completed"); visto=[]
for estado in orden:
 p=os.path.join(root,f"state-{token}-{estado}.json")
 if not os.path.lexists(p): continue
 fd=os.open(p,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0))
 try:
  if not stat.S_ISREG(os.fstat(fd).st_mode): raise SystemExit("state no regular")
  b=b""
  while True:
   x=os.read(fd,65536)
   if not x: break
   b+=x
 finally: os.close(fd)
 d=json.loads(b)
 if d.get("token")!=token or d.get("state")!=estado or d.get("evid_root")!=root or d.get("instancia")!=inst: raise SystemExit("state no ligado")
 visto.append(estado)
if visto != list(orden[:len(visto)]): raise SystemExit("secuencia de state inválida")
print(visto[-1] if visto else "none")
PYEOF
}

estado_publica() {   # <EVID> <token> <estado> <instancia>: transición O_EXCL durable
  python3 - "$1" "$2" "$3" "$4" <<'PYEOF'
import datetime,json,os,stat,sys
root,token,state,inst=sys.argv[1:]; root=os.path.abspath(root)
if state not in ("pre_restore","restored","post_restore","completed"): raise SystemExit("state inválido")
rst=os.lstat(root)
if not stat.S_ISDIR(rst.st_mode) or stat.S_ISLNK(rst.st_mode) or rst.st_uid!=os.getuid() or os.path.realpath(root)!=root:
 raise SystemExit("EVID no confinado")
p=os.path.join(root,f"state-{token}-{state}.json")
b=json.dumps({"state":state,"token":token,"instancia":inst,"evid_root":root,
 "cuando":datetime.datetime.now(datetime.timezone.utc).isoformat()},sort_keys=True,separators=(",",":")).encode()
fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0),0o600)
try:
 pos=0
 while pos<len(b): pos+=os.write(fd,b[pos:])
 os.fsync(fd)
finally: os.close(fd)
dd=os.open(root,os.O_RDONLY); os.fsync(dd); os.close(dd)
print(p)
PYEOF
}

# ── ⑤ RESTAURACIÓN: contra servicio VIVO, jamás ────────────────────────────────────
# `Connection.backup()` sobre el fichero que el proceso tiene abierto compite con sus
# propias escrituras. La restauración se hace con el servicio PARADO, y este guion no
# para nada: se niega y deja que lo pare quien tenga esa autoridad.
exige_servicio_parado() {   # <instancia>
  local ev est motivo
  # «NO MEDIBLE» NO ES «AUSENTE». `evidencia_contenedor` devuelve rc=1 en los DOS casos,
  # así que tratarlos igual abría la restauración cuando el daemon simplemente no
  # contestaba — y entonces se restaura encima de un servicio que puede estar vivo.
  # Sólo el motivo `ausente`, que es el mensaje explícito de Docker, autoriza.
  if ! ev="$(evidencia_contenedor "$1")"; then
    motivo="$(printf %s "$ev" | jq -r '.motivo // "desconocido"' 2>/dev/null)"
    if [ "$motivo" = "ausente" ]; then
      nota "   sin contenedor (Docker lo dice explícitamente): nada vivo que estorbe"
      return 0
    fi
    rojo "⛔ NO MEDIBLE el estado de \`$1\` ($motivo): no restauro a ciegas."
    rojo "   «No sé si está vivo» no es «está parado». Confundirlas es cómo se escribe"
    rojo "   encima de un almacén abierto."
    return 1
  fi
  est="$(printf %s "$ev" | jq -r .estado)"
  [ "$est" != "running" ] && return 0
  rojo "⛔ el servicio \`$1\` está RUNNING: no restauro datos sobre un almacén abierto."
  rojo "   \`backup()\` competiría con las escrituras del propio proceso."
  rojo "   Párala tú (ese verbo no es de este guion) y vuelve a lanzarlo."
  return 1
}

# ── ⑦ CERROJO DE FLUJO ─────────────────────────────────────────────────────────────
# El cerrojo que había cubría sólo la restauración de datos. Dos `m4-pilot.sh` a la vez
# anclan, respaldan y recrean la MISMA instancia sin verse.
# ── GRAMÁTICA DE INSTANCIA — se valida ANTES de cualquier efecto ───────────────────
# `INST` sólo se comprobaba «no vacío» y se interpolaba en la ruta del cerrojo, cuya
# limpieza corría `rm -rf`. Una instancia con `/` o `..` movía ese borrado FUERA del
# directorio de cerrojos: `LLMINBOX_NAME=../../algo` es un `rm -rf` dirigido. La forma
# se valida donde todavía no se ha tocado nada, y con la gramática de Docker/Compose,
# ENUMERADA y no por rango —`[a-z]` depende de la colación del locale—.
instancia_valida() {   # <nombre>
  local n="${1:-}"
  [ -n "$n" ] || { rojo "⛔ instancia vacía"; return 1; }
  [ "${#n}" -le 64 ] || { rojo "⛔ instancia de ${#n} caracteres: máximo 64"; return 1; }
  case "$n" in
    *[!abcdefghijklmnopqrstuvwxyz0123456789_.-]*|[!abcdefghijklmnopqrstuvwxyz0123456789]*)
      rojo "⛔ nombre de instancia inválido: \`$n\`"
      rojo "   Compose exige [a-z0-9_.-] empezando por letra o dígito MINÚSCULA."
      rojo "   Y esto es una guarda de seguridad: este nombre entra en una RUTA."
      return 1 ;;
  esac
  case "$n" in
    *..*) rojo "⛔ \`..\` en el nombre: eso es travesía de rutas, no un nombre"; return 1 ;;
  esac
  return 0
}

# ── ⑦ CERROJO DE FLUJO ─────────────────────────────────────────────────────────────
# El cerrojo que había cubría sólo la restauración de datos. Dos `m4-pilot.sh` a la vez
# anclan, respaldan y recrean la MISMA instancia sin verse.
#
# TRES defectos más que tenía y que no son cosméticos:
#   · el nombre entraba crudo en la ruta          → travesía (arriba)
#   · una variable GLOBAL que el segundo cerrojo pisaba → el primero perdía su ruta
#   · `trap ... EXIT` REEMPLAZABA el trap anterior → el cerrojo previo no se soltaba
# La ruta se deriva de un hash del nombre —no del nombre—, se comprueba que el padre
# resuelto ES el directorio de cerrojos, y se retira con `rmdir` del directorio EXACTO
# y vacío, nunca con `rm -rf`.
_M4_LOCKS=""          # pila, no variable pisable

_m4_suelta_cerrojos() {
  local l
  for l in $_M4_LOCKS; do
    rm -f "$l/quien" 2>/dev/null
    rmdir "$l" 2>/dev/null      # `rmdir`: si no está vacío, NO se borra. Nunca `rm -rf`.
  done
  _m4_limpia_salud
}

_m4_reclama_stale() {   # sólo una lease regular que acredita PID muerto
  python3 - "$1" "$2" <<'PYEOF'
import json,os,secrets,stat,sys
lock,root=map(os.path.abspath,sys.argv[1:]); who=os.path.join(lock,"quien")
if os.path.dirname(lock)!=root or os.path.islink(lock) or not stat.S_ISDIR(os.lstat(lock).st_mode): raise SystemExit(1)
fd=os.open(who,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0))
try:
 if not stat.S_ISREG(os.fstat(fd).st_mode): raise SystemExit(1)
 data=json.loads(os.read(fd,65536))
finally: os.close(fd)
pid=data.get("pid")
if not isinstance(pid,int) or pid<2: raise SystemExit(1)
try: os.kill(pid,0); raise SystemExit(1)
except ProcessLookupError: pass
except PermissionError: raise SystemExit(1)
q=os.path.join(root,".m4-stale-"+secrets.token_hex(16))
os.rename(lock,q); os.unlink(os.path.join(q,"quien")); os.rmdir(q)
PYEOF
}

cerrojo_flujo() {   # <operacion> <instancia>
  local op="$1" inst="$2" raiz hash lock padre QUIEN
  instancia_valida "$inst" || return 1
  raiz="${LLMI_LOCK_DIR:-${TMPDIR:-/tmp}}"
  raiz="${raiz%/}"
  [ -d "$raiz" ] || mkdir -p "$raiz" 2>/dev/null || {
    rojo "⛔ no existe el directorio de cerrojos: $raiz"; return 1; }
  raiz="$(cd "$raiz" 2>/dev/null && pwd -P)" || return 1
  # EL NOMBRE NO ENTRA EN LA RUTA. Entra su sha256: aunque la validación de arriba
  # tuviera un hueco, el componente de ruta sigue siendo 32 hex y nada más.
  hash="$(printf %s "$inst" | python3 -c 'import hashlib,sys;print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest()[:32])' 2>/dev/null)"
  case "$hash" in
    [0-9a-f][0-9a-f]*) : ;;
    *) rojo "⛔ no pude derivar un nombre de cerrojo seguro"; return 1 ;;
  esac
  [ "${#hash}" -eq 32 ] || { rojo "⛔ hash de cerrojo con longitud inesperada"; return 1; }
  lock="$raiz/.m4-flujo-$hash.lock"
  if ! mkdir "$lock" 2>/dev/null; then
    _m4_reclama_stale "$lock" "$raiz" 2>/dev/null && mkdir "$lock" 2>/dev/null || {
      rojo "⛔ hay otro flujo M4 en marcha sobre \`$inst\` (o lease no acreditable)."
      return 1; }
  fi
  # EL PADRE RESUELTO TIENE QUE SER LA RAÍZ. Se comprueba DESPUÉS de crear, sobre la
  # ruta real: un symlink interpuesto se ve aquí y no antes.
  padre="$(cd "$lock/.." 2>/dev/null && pwd -P)"
  if [ "$padre" != "$(cd "$raiz" 2>/dev/null && pwd -P)" ]; then
    rojo "⛔ el cerrojo NO cuelga del directorio de cerrojos: $padre ≠ $raiz"
    rmdir "$lock" 2>/dev/null
    return 1
  fi
  QUIEN="$(python3 - "$op" "$inst" "$$" <<'PYEOF'
import datetime,json,sys
print(json.dumps({"op":sys.argv[1],"instancia":sys.argv[2],"pid":int(sys.argv[3]),"cuando":datetime.datetime.now(datetime.timezone.utc).isoformat()},sort_keys=True,separators=(",",":")))
PYEOF
)" || { rmdir "$lock" 2>/dev/null; return 1; }
  texto_publica "$lock/quien" "$QUIEN" 2>/dev/null || { rmdir "$lock" 2>/dev/null; return 1; }
  _M4_LOCKS="$_M4_LOCKS $lock"
  # TRAP ACUMULATIVO: un `trap '...' EXIT` reemplaza el anterior, así que el segundo
  # cerrojo dejaba huérfano al primero. Se instala UNA vez y suelta la pila entera.
  trap '_m4_suelta_cerrojos' EXIT
  return 0
}

# ── ⑧ ARTEFACTO DE EVIDENCIA: hashes y rc, no sólo prosa ───────────────────────────
# Un veredicto que sólo enumera puertas no permite comprobar DESPUÉS que los ficheros
# citados son los que se produjeron. Cada corrida sella el sha256 de cada artefacto y el
# rc del paso que lo generó.
VEREDICTO_ARTEFACTOS=""
VEREDICTO_RCS=""

artefacto_anota() {   # <ruta>
  # UN FICHERO VACÍO NO SE SELLA COMO CONTENIDO. Si `curl` falla y su salida se redirige
  # igual, queda un fichero de 0 bytes; hashearlo publica `e3b0c442…`, que es un sha
  # perfectamente válido y perfectamente mentiroso: dice «aquí está la evidencia» sobre
  # una lectura que no ocurrió.
  local r="$1" s=""
  if [ -f "$r" ] && [ ! -s "$r" ]; then
    s="VACIO"
  elif [ -f "$r" ]; then
    s="$(python3 -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$r" 2>/dev/null)"
  fi
  VEREDICTO_ARTEFACTOS="$VEREDICTO_ARTEFACTOS$(printf '%s\t%s\t%s\n' "$r" "${s:-AUSENTE}" "$( [ -f "$r" ] && wc -c < "$r" | tr -d ' ' || echo 0)")
"
}

rc_anota() {   # <paso> <rc>
  VEREDICTO_RCS="$VEREDICTO_RCS$(printf '%s\t%s\n' "$1" "$2")
"
}
