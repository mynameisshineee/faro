#!/usr/bin/env bash
# REVERSIÓN M4 — se niega más de lo que hace, y ésa es la funcionalidad.
#
# DOS FASES, Y UN DISPATCH INMEDIATO TRAS EL PARSEO. Ésta es la corrección estructural:
# las versiones anteriores exigían `--a` en la cabecera y corrían los gates de outbox,
# journal y salud ANTES de mirar si venía un plan — gates que necesitan la aplicación
# VIVA, justo lo contrario de lo que la fase 2 requiere. Resultado: la fase 2 era
# inalcanzable por construcción, con la ruta escrita y probada en su propio vacío.
#
#   FASE 1 · servicio VIVO      `--a <imagen>` [--datos <snapshot>] [--digest <sha>]
#            preflight, gates que necesitan la app, y CONGELA en un plan sellado todo lo
#            que la fase 2 va a necesitar: imagen destino, digest, snapshot y su SHA,
#            id del contenedor, volumen, mountpoint y ruta de la DB — derivados por
#            `inspect`, no por variables de entorno que pueden cambiar entre fases.
#            Termina SIN acreditar: emite plan + token y para.
#
#   FASE 2 · servicio PARADO    `--reanudar <token> --plan <ruta>`
#            NO acepta `--a`, `--datos` ni `--digest`: todo sale del plan verificado.
#            NO repite los gates que exigen la app viva. Exige `stopped`, comprueba que
#            los mounts siguen siendo los congelados, consume el plan, corre el helper,
#            recrea y sólo entonces hace los gates post.
#
# El token de reanudación es el SHA256 EXACTO del fichero de plan y NO vive dentro de
# él: editar una coma lo invalida.
set -uo pipefail
cd "$(dirname "$0")/.."
. scripts/m4-evidencia.sh

INST="${LLMINBOX_NAME:-}"
API="${LLMINBOX_API:-http://127.0.0.1:${LLMINBOX_PORT:-8077}}"
EVID="${M4_EVIDENCIA:-./evidencia-m4}"
DESTINO=""; SNAPSHOT=""; SECO=0; FLOTA=0; SIN_OUTBOX=0; SIN_JOURNAL=0
DIGEST_ESP=""; REANUDA=""; PLAN_RUTA=""; SOLO_BIN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --a) DESTINO="${2:-}"; shift 2 ;;
    --datos) SNAPSHOT="${2:-}"; shift 2 ;;
    --solo-binario) SOLO_BIN=1; shift ;;
    --digest) DIGEST_ESP="${2:-}"; shift 2 ;;
    --reanudar) REANUDA="${2:-}"; shift 2 ;;
    --plan) PLAN_RUTA="${2:-}"; shift 2 ;;
    --dry-run|--seco) SECO=1; shift ;;
    --acepto-la-flota) FLOTA=1; shift ;;
    --emergencia-sin-outbox) SIN_OUTBOX=1; shift ;;
    --emergencia-sin-journal) SIN_JOURNAL=1; shift ;;
    *) rojo "argumento desconocido: $1"; exit 2 ;;
  esac
done

# ── lo común a las dos fases, y NADA MÁS ───────────────────────────────────────────
[ -n "$INST" ] || { rojo "⛔ falta LLMINBOX_NAME"; exit 2; }
instancia_valida "$INST" || exit 2
if [ "$INST" = "llminbox" ] && [ "$FLOTA" -ne 1 ]; then
  rojo "⛔ \`llminbox\` es la instancia de la FLOTA; no la toco sin --acepto-la-flota"; exit 2
fi
command -v jq >/dev/null || { rojo "⛔ falta jq"; exit 2; }
evidencia_prepara "$EVID" || exit 1
EVID="$(cd "$EVID" && pwd -P)"
SELLO="$(date -u +%Y%m%dT%H%M%SZ)"
cerrojo_flujo reversion "$INST" || exit 1
VER="$EVID/$SELLO-veredicto.json"

# ══ DISPATCH ══════════════════════════════════════════════════════════════════════
if [ -n "$REANUDA" ] || [ -n "$PLAN_RUTA" ]; then FASE=2; else FASE=1; fi

if [ "$FASE" -eq 2 ]; then
  PUERTAS="plan pre_restore restore post_restore gate salud completed"
  [ -z "$DESTINO$SNAPSHOT$DIGEST_ESP" ] && [ "$SOLO_BIN" -eq 0 ] || {
    rojo "⛔ la fase 2 NO acepta --a, --datos, --digest ni --solo-binario: todo sale del"
    rojo "   plan. Aceptarlos permitiría revertir a otro sitio —o saltarse la"
    rojo "   restauración— con la bendición de un plan que decía otra cosa."; exit 2; }
  [ -n "$PLAN_RUTA" ] && [ -n "$REANUDA" ] || {
    rojo "⛔ la fase 2 exige --reanudar <token> Y --plan <ruta>"; exit 2; }
  [ -f "$PLAN_RUTA" ] && [ ! -L "$PLAN_RUTA" ] || { rojo "⛔ plan ausente, no regular o symlink: $PLAN_RUTA"; exit 2; }

  VERIF="$(plan_lee_verifica "$PLAN_RUTA" "$REANUDA" "$INST")"
  case "$VERIF" in
    "OK "*) veredicto_anota plan ok "$PLAN_RUTA" ;;
    *) rojo "⛔ PLAN NO VÁLIDO: ${VERIF#NO }"
       veredicto_anota plan fallo "plan no válido"
       veredicto_escribe "$VER" reversion "$INST" $PUERTAS; exit 1 ;;
  esac
  P="${VERIF#OK }"
  DESTINO="$(printf %s "$P" | jq -r .destino_imagen)"
  DIGEST_ESP="$(printf %s "$P" | jq -r .destino_digest)"
  DESTINO_ID="$(printf %s "$P" | jq -r .destino_image_id)"
  RESTAURA="$(printf %s "$P" | jq -r .restaura_indice)"
  case "$RESTAURA" in
    true) VEREDICTO_MODO_ACREDITADO="binario_mas_indice" ;;
    false) VEREDICTO_MODO_ACREDITADO="solo_binario" ;;
    *) rojo "⛔ restaura_indice dejó de ser booleano tras verificar el plan"; exit 1 ;;
  esac
  SNAPSHOT="$(printf %s "$P" | jq -r '.snapshot // ""')"
  SNAP_SHA_ESP="$(printf %s "$P" | jq -r '.snapshot_sha // ""')"
  VOLUMEN="$(printf %s "$P" | jq -r .volumen)"
  MOUNTPOINT="$(printf %s "$P" | jq -r .mountpoint)"
  DB_INT="$(printf %s "$P" | jq -r .db_interna)"
  CID_PLAN="$(printf %s "$P" | jq -r .contenedor_id)"
  IMG_HELPER="$(printf %s "$P" | jq -r .imagen_helper)"
  IMG_DIGEST="$(printf %s "$P" | jq -r .imagen_digest)"
  IMG_HELPER_DIGEST="$(printf %s "$P" | jq -r .imagen_helper_digest)"
  VOLUMEN_IDENTIDAD="$(printf %s "$P" | jq -c .volumen_identidad)"
  EVID_PLAN="$(printf %s "$P" | jq -r .evid_root)"
  HELPER="$(printf %s "$P" | jq -r .evidencias.helper.ruta)"
  HELPER_SHA="$(printf %s "$P" | jq -r .evidencias.helper.sha256)"
  [ "$EVID_PLAN" = "$EVID" ] || { rojo "⛔ el plan está ligado a EVID=$EVID_PLAN, no $EVID"
    veredicto_anota plan fallo "EVID alterno"; veredicto_escribe "$VER" reversion "$INST" $PUERTAS; exit 1; }
  ESTADO="$(estado_actual "$EVID" "$REANUDA" "$INST")" || { rojo "⛔ state machine corrupta"; exit 1; }
  [ "$ESTADO" != "completed" ] || { verde "✓ plan ya completado; replay sin mutación"; exit 0; }
  nota "① plan verificado: $PLAN_RUTA"
  nota "   destino=$DESTINO · volumen=$VOLUMEN · db=$DB_INT"
  if [ "$RESTAURA" = "true" ]; then nota "   eje de DATOS: restaura el ÍNDICE desde $SNAPSHOT"
  else nota "   eje de DATOS: NO se toca (plan solo-binario)"; fi

  # Validaciones PURAS. Ningún marker/claim existe todavía y dry-run termina aquí.
  evidencias_verifica "$P" || { rojo "⛔ artefactos de fase 1 cambiaron"
    veredicto_anota plan fallo "evidencias alteradas"; veredicto_escribe "$VER" reversion "$INST" $PUERTAS; exit 1; }
  verifica_digest "$DESTINO" "$DIGEST_ESP" || { veredicto_anota mounts fallo "destino cambió"; exit 1; }
  [ "$(id_de_imagen "$DESTINO")" = "$DESTINO_ID" ] || { rojo "⛔ image ID destino cambió"; exit 1; }
  # El propio primer intento cambia esta etiqueta al destino justo antes de `llmi up`.
  # Por tanto, al reanudar desde `post_restore` exigir el digest viejo del helper haría
  # irrecuperable exactamente el fallo que este estado promete reintentar. Los estados
  # anteriores todavía no han hecho el tag y conservan el anclaje original; post_restore
  # acepta una sola identidad distinta: el image ID DESTINO ya congelado en el plan.
  if [ "$ESTADO" = "post_restore" ]; then
    [ "$(id_de_imagen "$IMG_HELPER")" = "$DESTINO_ID" ] || {
      rojo "⛔ imagen helper no apunta al destino congelado en post_restore"
      veredicto_anota mounts fallo "imagen helper no es destino en post_restore"; exit 1; }
  elif [ "$ESTADO" = "restored" ]; then
    # Corte recuperable: el proceso pudo morir después de mover el tag y antes de
    # publicar post_restore. Sólo hay dos identidades admisibles, ambas del plan.
    IMG_HELPER_ACTUAL="$(id_de_imagen "$IMG_HELPER")" || exit 1
    [ "$IMG_HELPER_ACTUAL" = "$IMG_DIGEST" ] || [ "$IMG_HELPER_ACTUAL" = "$DESTINO_ID" ] || {
      rojo "⛔ imagen helper no es ni la original ni el destino congelado en restored"
      veredicto_anota mounts fallo "tag fuera de los dos estados reanudables"; exit 1; }
  else
    verifica_digest "$IMG_HELPER" "$IMG_HELPER_DIGEST" || {
      veredicto_anota mounts fallo "imagen helper cambió"; exit 1; }
  fi
  [ "$(id_de_imagen "$IMG_DIGEST")" = "$IMG_DIGEST" ] || { rojo "⛔ image ID del helper ausente/distinto"; exit 1; }
  volumen_valida "$VOLUMEN" "$VOLUMEN_IDENTIDAD" || { veredicto_anota plan fallo "volumen ausente/distinto"; exit 1; }
  if [ "$RESTAURA" = "true" ]; then
    INV="$(valida_snapshot "$SNAPSHOT" "$MOUNTPOINT" "$SNAP_SHA_ESP")" || {
      veredicto_anota restore fallo "snapshot: sha distinto del anclado, integridad o clase"
      veredicto_escribe "$VER" reversion "$INST" $PUERTAS; exit 1; }
    INV_F="$EVID/$SELLO-$$-snapshot-validado.json"; texto_publica "$INV_F" "$INV" || exit 1; artefacto_anota "$INV_F"
  fi
  if [ "$SECO" -eq 1 ]; then
    nota "[seco] validaciones completas; ningún state/claim creado"
    veredicto_anota plan ok; veredicto_anota pre_restore no_medido "dry-run sin claim"
    veredicto_anota restore no_medido "dry-run"
    veredicto_anota post_restore no_medido "dry-run"
    veredicto_anota gate no_medido "dry-run"
    veredicto_anota salud no_medido "dry-run"
    veredicto_anota completed no_medido "dry-run"
    veredicto_escribe "$VER" reversion "$INST" $PUERTAS
    exit 1
  fi

  # ⛔ EL EJE BINARIO NO TOCA DATOS, Y AQUÍ ES DONDE SE CUMPLE. Con `restaura_indice`
  # en `false` no se llama al helper, no se valida ningún snapshot y no se calcula ni un
  # hash del almacén: el fichero de datos no se abre siquiera. Las tres puertas del eje
  # de datos NO desaparecen del veredicto — se anotan `no_aplica` CON MOTIVO, que es lo
  # que separa «este modo no la tiene» de «no llegué a correrla».
  if [ "$RESTAURA" != "true" ]; then
    veredicto_anota pre_restore no_aplica "plan solo-binario: el eje de datos no se toca"
    veredicto_anota restore    no_aplica "plan solo-binario: el eje de datos no se toca"
    if [ "$ESTADO" = "none" ]; then
      MARCA="$(estado_publica "$EVID" "$REANUDA" restored "$INST")" || exit 1; artefacto_anota "$MARCA"
    fi
    ESTADO=restored
  else
    if [ "$ESTADO" = "none" ] || [ "$ESTADO" = "pre_restore" ]; then
      MODO=resume
      if [ "$ESTADO" = "none" ]; then
        MARCA="$(estado_publica "$EVID" "$REANUDA" pre_restore "$INST")" || exit 1
        artefacto_anota "$MARCA"; MODO=fresh
      fi
      SALIDA_TMP="$(umask 077; mktemp "$_M4_SALUD_DIR/helper.XXXXXXXX")" || exit 1
      # ÚLTIMA operación antes del helper mutante: un único inspect proyectado y todas
      # sus invariantes. Ausencia explícita vale; running/created/error Docker/jq, no.
      ultimo_stopped_plan "$INST" "$CID_PLAN" "$IMG_DIGEST" "$VOLUMEN" "$MOUNTPOINT" "$DB_INT" || exit 1
      helper_volumen "$IMG_DIGEST" "$VOLUMEN" "$MOUNTPOINT" "$HELPER" "$HELPER_SHA" "$SNAPSHOT" \
                     "$DB_INT" "$REANUDA" "$SNAP_SHA_ESP" "$MODO" >"$SALIDA_TMP" 2>&1
      RC_H=$?; SALIDA_H="$EVID/$SELLO-$$-restore-helper.out"
      archivo_copia "$SALIDA_TMP" "$SALIDA_H" >/dev/null || exit 1; rc_anota helper "$RC_H"; artefacto_anota "$SALIDA_H"
      if [ "$RC_H" -ne 0 ] || ! head -c 3 "$SALIDA_H" | command grep -qF "OK "; then
        rojo "⛔ helper no acreditó restore; reanudable desde pre_restore"
        veredicto_anota restore fallo "helper rc=$RC_H"; veredicto_escribe "$VER" reversion "$INST" $PUERTAS; exit 1
      fi
      MARCA="$(estado_publica "$EVID" "$REANUDA" restored "$INST")" || exit 1; artefacto_anota "$MARCA"
      ESTADO=restored
    fi
    # Fuera del `if` A PROPÓSITO: una reanudación desde `restored`/`post_restore` no
    # repite el restore, pero sus dos puertas SÍ están acreditadas — anotarlas dentro
    # las dejaría en `no_medido` en cada reintento, que es fallo, y un reintento se
    # volvería imposible de acreditar por haber funcionado a la primera.
    veredicto_anota pre_restore ok; veredicto_anota restore ok
  fi

  if [ "$ESTADO" = "restored" ]; then
    # El efecto ocurre ANTES que el marker que lo afirma. Si el proceso cae entre ambos,
    # el estado sigue en `restored`; la validación anterior acepta de forma cerrada el
    # tag original o éste ya movido y repetir `docker tag` es idempotente.
    "$DOCKER" tag "$DESTINO_ID" "$IMG_HELPER" || { rojo "⛔ tag desde image ID falló"; exit 1; }
    MARCA="$(estado_publica "$EVID" "$REANUDA" post_restore "$INST")" || exit 1; artefacto_anota "$MARCA"
    ESTADO=post_restore
  fi
  # `post_restore` significa que el tag ya está durablemente en el destino; un fallo de
  # recreate se reintenta sin volver a tocar datos ni mover otra referencia.
  ./llmi up --cobertura-verificada; RC_UP=$?; rc_anota llmi_up "$RC_UP"
  [ "$RC_UP" -eq 0 ] || { rojo "⛔ recreate falló; reanudable desde post_restore"; exit 1; }
  veredicto_anota post_restore ok; salud_olvida
  DESPUES="$(evidencia_contenedor "$INST")" || { rojo "⛔ inspect post_restore no medible"; exit 1; }
  DESPUES_F="$EVID/$SELLO-$$-rb-despues.json"; texto_publica "$DESPUES_F" "$DESPUES" || exit 1; artefacto_anota "$DESPUES_F"
  GATES_OK=1
  if gate_post_recreate "$INST" "$CID_PLAN" "$API"; then veredicto_anota gate ok
  else veredicto_anota gate fallo "ver stderr"; GATES_OK=0; fi
  if gate_salud "$API"; then veredicto_anota salud ok
  else veredicto_anota salud fallo "el servicio no se declara sano"; GATES_OK=0; fi
  if [ "$GATES_OK" -eq 1 ]; then
    MARCA="$(estado_publica "$EVID" "$REANUDA" completed "$INST")" || exit 1; artefacto_anota "$MARCA"
    veredicto_anota completed ok
  else
    veredicto_anota completed fallo "post gates; reanudable desde post_restore"
  fi
  veredicto_escribe "$VER" reversion "$INST" $PUERTAS
  RC_VER=$?; artefacto_anota "$VER"
  [ "$RC_VER" -eq 0 ] && { verde "✓ REVERSIÓN ACREDITADA · $VER"; exit 0; }
  rojo "⛔ REVERSIÓN NO ACREDITADA. Fallidas: $(jq -r '.fallidas | join(", ")' "$VER" 2>/dev/null)"
  exit 1
fi

# ══ FASE 1 · servicio VIVO ════════════════════════════════════════════════════════
PUERTAS="destino digest outbox journal plan"
[ -n "$DESTINO" ] || { rojo "⛔ falta --a <imagen@sha256:… | imagen:rollback-…>"; exit 2; }
# ── LOS DOS EJES SE ELIGEN, Y NO HAY DEFECTO IMPLÍCITO ─────────────────────────────
# `docs/M4-PILOT.md` dice desde el principio que «revertir el binario NO revierte los
# datos» y que «sin --datos el almacén se queda donde está». Era FALSO en el guion: esta
# línea EXIGÍA `--datos`, así que el modo que el documento presenta como el defecto salía
# `exit 2`. Ahora se elige, y no elegir es un rechazo: un defecto implícito en un eje que
# PIERDE lo escrito desde el snapshot convertiría esa pérdida en un efecto secundario de
# «volver atrás», que es justo lo que ese apartado dice que no quiere.
if [ "$SOLO_BIN" -eq 1 ] && [ -n "$SNAPSHOT" ]; then
  rojo "⛔ --solo-binario y --datos son EXCLUYENTES: o el eje de datos se toca o no."
  exit 2
fi
if [ "$SOLO_BIN" -eq 0 ] && [ -z "$SNAPSHOT" ]; then
  rojo "⛔ falta decidir el EJE DE DATOS. Las dos opciones, y ninguna es el defecto:"
  rojo "   --solo-binario          revierte la imagen y DEJA el índice donde está"
  rojo "   --datos <snapshot>      revierte la imagen Y restaura el ÍNDICE (pierde lo"
  rojo "                           escrito desde ese snapshot)"
  rojo "   El journal de coordinación NO es restaurable por ninguna de las dos (ADR-001)."
  exit 2
fi

nota "① destino: $DESTINO"
destino_inmutable "$DESTINO" || { veredicto_anota destino fallo "móvil o digest inválido"
  veredicto_escribe "$VER" reversion "$INST" $PUERTAS; exit 1; }
verde "✓ destino inmutable"; veredicto_anota destino ok "$DESTINO"

DEST_DIG="$(digest_de_imagen "$DESTINO")" || {
  rojo "⛔ no puedo resolver el destino a un digest exacto"
  veredicto_anota digest fallo "destino no resoluble"; veredicto_escribe "$VER" reversion "$INST" $PUERTAS; exit 1; }
DESTINO_ID="$(id_de_imagen "$DESTINO")" || { rojo "⛔ destino sin image ID inmutable"; exit 1; }
if [ -n "$DIGEST_ESP" ]; then
  verifica_digest "$DESTINO" "$DIGEST_ESP" \
    && veredicto_anota digest ok "$DIGEST_ESP" \
    || { veredicto_anota digest fallo "la referencia ya no resuelve al digest anclado"
         veredicto_escribe "$VER" reversion "$INST" $PUERTAS; exit 1; }
elif printf %s "$DESTINO" | command grep -qF '@sha256:'; then
  veredicto_anota digest ok "$DEST_DIG"
else
  rojo "⛔ destino por ETIQUETA y sin --digest: una etiqueta no es evidencia."
  veredicto_anota digest fallo "etiqueta sin digest de anclaje"
  veredicto_escribe "$VER" reversion "$INST" $PUERTAS; exit 1
fi

nota "② outbox"
if gate_outbox "$API"; then veredicto_anota outbox ok "drenado"
elif [ "$SIN_OUTBOX" -eq 1 ]; then
  rojo "⚠️ --emergencia-sin-outbox: sigo SIN certificar el outbox. Queda registrado."
  veredicto_anota outbox fallo "SALTADO por --emergencia-sin-outbox"
else
  rojo "   (si es emergencia consciente: --emergencia-sin-outbox, que no sale 0)"
  veredicto_anota outbox fallo "pendiente/fallido o no certificable"
  veredicto_escribe "$VER" reversion "$INST" $PUERTAS; exit 1
fi

nota "③ journal de coordinación"
if gate_journal "$API"; then veredicto_anota journal ok "disponible"
elif [ "$SIN_JOURNAL" -eq 1 ]; then
  rojo "⚠️ --emergencia-sin-journal: sigo SIN acreditar el journal. Queda registrado."
  veredicto_anota journal fallo "SALTADO por --emergencia-sin-journal"
else
  rojo "   (dependencia pendiente: M1 no integrado)"
  veredicto_anota journal fallo "no disponible o bloque ausente"
  veredicto_escribe "$VER" reversion "$INST" $PUERTAS; exit 1
fi

# Los dos gates anteriores y el resto de gates de esta fase consumieron UNA respuesta
# cacheada. Se publica esa foto exacta y dos proyecciones derivadas de SUS MISMOS bytes.
HEALTH_F="$EVID/$SELLO-health.json"; OUTBOX_F="$EVID/$SELLO-outbox.json"; JOURNAL_F="$EVID/$SELLO-journal.json"
SALUD_EVID="$(salud_sella "$API" "$HEALTH_F" "$OUTBOX_F" "$JOURNAL_F")" || {
  rojo "⛔ no pude sellar la foto exacta de /health"; veredicto_anota plan fallo "health no sellable"; exit 1; }
artefacto_anota "$HEALTH_F"; artefacto_anota "$OUTBOX_F"; artefacto_anota "$JOURNAL_F"

# Una emergencia deja evidencia roja, pero deliberadamente NO genera un plan que la
# fase 2 pueda consumir. La excepción humana no se transforma en autorización verde.
if [ "$SIN_OUTBOX" -eq 1 ] || [ "$SIN_JOURNAL" -eq 1 ]; then
  EMERG="$EVID/$SELLO-emergencia-sin-autorizacion.json"
  python3 - "$EMERG" "$INST" "$SELLO" "$SIN_OUTBOX" "$SIN_JOURNAL" <<'PYEOF'
import json,os,sys
p,inst,sello,o,j=sys.argv[1:]; b=json.dumps({"operacion":"emergencia-no-plan","instancia":inst,"sello":sello,"sin_outbox":o=="1","sin_journal":j=="1"},sort_keys=True).encode()
fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0),0o600)
try:
 pos=0
 while pos<len(b): pos+=os.write(fd,b[pos:])
 os.fsync(fd)
finally: os.close(fd)
d=os.open(os.path.dirname(p),os.O_RDONLY); os.fsync(d); os.close(d)
PYEOF
  [ "$?" -eq 0 ] || { rojo "⛔ no pude publicar evidencia de emergencia exclusiva"; exit 1; }
  artefacto_anota "$EMERG"; veredicto_anota plan fallo "emergencia: plan deliberadamente no emitido"
  veredicto_escribe "$VER" reversion "$INST" $PUERTAS >/dev/null
  rojo "⛔ emergencia registrada SIN plan consumible: $EMERG"; exit 1
fi

# ④ CONGELAR EL MUNDO EN EL PLAN. Todo por `inspect`, no por variables de entorno:
# entre las dos fases el entorno cambia y el plan tiene que describir ESTE contenedor.
EV="$(evidencia_contenedor "$INST")" || { rojo "⛔ no medible: no sello un plan a ciegas"
  veredicto_anota plan fallo "contenedor no medible"
  veredicto_escribe "$VER" reversion "$INST" $PUERTAS; exit 1; }
INSPECT_F="$EVID/$SELLO-rb-antes.json"
INSPECT_INV="$(python3 - "$INSPECT_F" "$EV" <<'PYEOF'
import hashlib,json,os,sys
p=os.path.abspath(sys.argv[1]); b=(sys.argv[2]+"\n").encode()
fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0),0o600)
try:
 pos=0
 while pos<len(b): pos+=os.write(fd,b[pos:])
 os.fsync(fd)
finally: os.close(fd)
d=os.open(os.path.dirname(p),os.O_RDONLY); os.fsync(d); os.close(d)
print(json.dumps({"ruta":p,"sha256":hashlib.sha256(b).hexdigest(),"bytes":len(b)},separators=(",",":")))
PYEOF
)" || { rojo "⛔ no pude publicar inspect exclusivo"; exit 1; }
artefacto_anota "$INSPECT_F"
MOUNTPOINT="${LLMINBOX_VOLUMEN:-/data}"
CID="$(printf %s "$EV" | jq -r .id)"
IMG_HELPER="$(printf %s "$EV" | jq -r .imagen_nombre)"
IMG_DIGEST="$(printf %s "$EV" | jq -r .imagen)"
IMG_HELPER_DIGEST="$(digest_de_imagen "$IMG_HELPER")" || {
  veredicto_anota plan fallo "imagen helper no resoluble"; veredicto_escribe "$VER" reversion "$INST" $PUERTAS; exit 1; }
VOLUMEN="$(volumen_del_mount "$EV" "$MOUNTPOINT")" || {
  veredicto_anota plan fallo "volumen no derivable sin ambigüedad"
  veredicto_escribe "$VER" reversion "$INST" $PUERTAS; exit 1; }
VOLUMEN_IDENTIDAD="$(volumen_identidad "$VOLUMEN")" || {
  rojo "⛔ el volumen no ofrece identidad fuerte revalidable"; veredicto_anota plan fallo "volumen sin identidad"; exit 1; }
DB_INT="$(db_del_contenedor "$EV" "$MOUNTPOINT")" || {
  veredicto_anota plan fallo "ruta de DB inválida o fuera del montaje"
  veredicto_escribe "$VER" reversion "$INST" $PUERTAS; exit 1; }
# ⛔ CON `--solo-binario` NO SE TOCA UN HASH DE DATOS. Ni `archivo_copia` del snapshot,
# ni `valida_snapshot`, ni el `jq` que busca su SHA anclado: el almacén no se abre, no se
# copia y no se hashea. La `evidencias` del plan queda con sus cinco artefactos y sin el
# sexto, y `evidencias_verifica` rechaza un plan solo-binario que traiga uno.
SNAP_INV=""
if [ "$SOLO_BIN" -eq 1 ]; then
  [ -n "$VOLUMEN" ] || { rojo "⛔ sin volumen derivable: el plan describiría otro almacén"
    veredicto_anota plan fallo "volumen no derivable"
    veredicto_escribe "$VER" reversion "$INST" $PUERTAS; exit 1; }
  SNAPSHOT=""; SNAP_SHA_ESP=""
  nota "④ plan SOLO-BINARIO: el eje de datos queda fuera y no se calcula ningún hash"
else
SNAP_SHA_ESP="${M4_SNAPSHOT_SHA:-$(jq -r --arg s "$SNAPSHOT" \
    'select(.snapshot==$s) | .inventario.sha256 // empty' \
    "$EVID"/*-snapshot.json 2>/dev/null | head -1)}"
[ -n "$SNAP_SHA_ESP" ] && [ -n "$VOLUMEN" ] || {
    rojo "⛔ sin SHA anclado del snapshot (${SNAP_SHA_ESP:-vacío}) o sin volumen (${VOLUMEN:-vacío})."
    rojo "   Estar sano no es ser LA base, y sin el volumen exacto se tocaría otro almacén."
    veredicto_anota plan fallo "snapshot sin SHA anclado o volumen no derivable"
    veredicto_escribe "$VER" reversion "$INST" $PUERTAS; exit 1; }
SNAP_F="$EVID/$SELLO-rb-snapshot.sqlite"
SNAP_INV="$(archivo_copia "$SNAPSHOT" "$SNAP_F")" || { rojo "⛔ no pude congelar snapshot"; exit 1; }
SNAP_COPY_SHA="$(printf %s "$SNAP_INV" | jq -r .sha256)"
[ "$SNAP_COPY_SHA" = "$SNAP_SHA_ESP" ] || { rojo "⛔ snapshot cambió respecto al SHA anclado"; exit 1; }
# `valida_snapshot` es también el gate de CLASE: un snapshot del journal se rechaza aquí,
# por identidad y contenido. La restauración opcional se limita al ÍNDICE.
valida_snapshot "$SNAP_F" "$MOUNTPOINT" "$SNAP_SHA_ESP" >/dev/null || exit 1
SNAPSHOT="$SNAP_F"
fi
HELPER_SRC="$(cd "$(dirname "$0")" && pwd)/m4-restore-helper.py"
HELPER_F="$EVID/$SELLO-restore-helper.py"
HELPER_INV="$(archivo_copia "$HELPER_SRC" "$HELPER_F")" || { rojo "⛔ no pude ligar helper exacto"; exit 1; }
EVIDENCIAS="$(python3 - "$SALUD_EVID" "$SNAP_INV" "$INSPECT_INV" "$HELPER_INV" <<'PYEOF'
import json,sys
e=json.loads(sys.argv[1])
# Sin eje de datos NO se liga artefacto de snapshot. No es que se ligue vacío: es que la
# clave NO EXISTE, y `evidencias_verifica` rechaza un plan solo-binario que la traiga.
if sys.argv[2]: e["snapshot"]=json.loads(sys.argv[2])
e["inspect"]=json.loads(sys.argv[3]); e["helper"]=json.loads(sys.argv[4]); print(json.dumps(e,sort_keys=True,separators=(",",":")))
PYEOF
)"
PLAN="$EVID/$SELLO-plan.json"
PLAN_JSON="$(python3 - "$INST" "$DESTINO" "$DEST_DIG" "$DESTINO_ID" "$SNAPSHOT" \
                      "${SNAP_SHA_ESP:-}" "$SELLO" "$VOLUMEN" "$MOUNTPOINT" \
                      "$DB_INT" "$CID" "$IMG_DIGEST" "$IMG_HELPER" "$IMG_HELPER_DIGEST" \
                      "$EVID" "$VOLUMEN_IDENTIDAD" "$EVIDENCIAS" "$SOLO_BIN" <<'PYEOF'
import json, sys
(inst, destino, digest, destino_id, snap, snap_sha, sello, vol, mnt, db, cid, imgdig,
 imghelper, imghelperdig, evidroot, volident, evidencias, solo_bin) = sys.argv[1:19]
restaura = solo_bin != "1"
print(json.dumps({"esquema": 4, "operacion": "reversion", "instancia": inst,
                  "sello": sello, "destino_imagen": destino,
                  "destino_digest": digest, "destino_image_id": destino_id,
                  # El eje de datos va en el PLAN, no en la línea de órdenes de la fase 2:
                  # la fase 2 no acepta banderas de cliente, así que lo que se decidió con
                  # la app viva es lo único que puede ejecutarse con la app parada.
                  "restaura_indice": restaura,
                  "snapshot": (snap or None) if restaura else None,
                  "snapshot_sha": (snap_sha or None) if restaura else None,
                  "volumen": vol, "mountpoint": mnt,
                  "db_interna": db, "contenedor_id": cid,
                  "imagen_digest": imgdig, "imagen_helper": imghelper,
                  "imagen_helper_digest": imghelperdig,
                  "evid_root": evidroot, "volumen_identidad": json.loads(volident),
                  "evidencias": json.loads(evidencias)},
                 ensure_ascii=False, sort_keys=True, indent=1))
PYEOF
)"
TOKEN="$(plan_escribe "$PLAN" "$PLAN_JSON")" || { rojo "⛔ no pude sellar el plan"; exit 1; }
artefacto_anota "$PLAN"
veredicto_anota plan ok "$PLAN"; veredicto_anota ejecucion no_medido "fase 2 pendiente"
PUERTAS="$PUERTAS ejecucion"
veredicto_escribe "$VER" reversion "$INST" $PUERTAS
RC_VER=$?; artefacto_anota "$VER"
if [ "$SOLO_BIN" -eq 1 ]; then verde "✓ FASE 1 completa (SOLO BINARIO). Plan sellado en $PLAN"
else verde "✓ FASE 1 completa (binario + índice). Plan sellado en $PLAN"; fi
rojo "⛔ NO ACREDITADA todavía: la fase 2 exige el servicio PARADO, y pararlo no es mío."
rojo "   ① párala tú"
rojo "   ② scripts/m4-rollback.sh --reanudar $TOKEN --plan $PLAN"
rojo "   (el token ES el sha256 del plan: si alguien lo edita, deja de valer)"
exit 1
