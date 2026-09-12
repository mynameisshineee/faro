#!/usr/bin/env bash
# PILOTAJE M4 — despliega una instancia y deja la evidencia que la reversión necesitará.
#
# Lo que este guion existe para arreglar (medido en M0): el despliegue normal NO
# preservaba la imagen saliente, así que retroceder exigía reconstruir — y reconstruir no
# era reproducible. Sin ancla previa no hay reversión; hay una reconstrucción con suerte.
#
# ⛔ NO se corre contra la instancia de la flota. Exige `LLMINBOX_NAME` distinto de
# `llminbox` salvo `--acepto-la-flota`, que existe para que apuntar a producción sea un
# acto y no un descuido.
set -uo pipefail
cd "$(dirname "$0")/.."
. scripts/m4-evidencia.sh

INST="${LLMINBOX_NAME:-}"
API="${LLMINBOX_API:-http://127.0.0.1:${LLMINBOX_PORT:-8077}}"
IMG="${LLMINBOX_IMAGEN:-${INST}-llminbox:latest}"
EVID="${M4_EVIDENCIA:-./evidencia-m4}"
SECO=0; FLOTA=0
for a in "$@"; do
  case "$a" in
    --dry-run|--seco) SECO=1 ;;
    --acepto-la-flota) FLOTA=1 ;;
    *) rojo "argumento desconocido: $a"; exit 2 ;;
  esac
done

[ -n "$INST" ] || { rojo "⛔ falta LLMINBOX_NAME: sin instancia no hay aislamiento"; exit 2; }
instancia_valida "$INST" || exit 2
if [ "$INST" = "llminbox" ] && [ "$FLOTA" -ne 1 ]; then
  rojo "⛔ \`llminbox\` es la instancia de la FLOTA. Este guion no la toca por defecto."
  rojo "   Si de verdad es lo que quieres: --acepto-la-flota (y anúncialo antes y después)."
  exit 2
fi
command -v jq >/dev/null || { rojo "⛔ falta jq: la evidencia no es proyectable"; exit 2; }

evidencia_prepara "$EVID" || exit 1
EVID="$(cd "$EVID" && pwd -P)"
SELLO="$(date -u +%Y%m%dT%H%M%SZ)"
# UN FLUJO A LA VEZ POR INSTANCIA, antes del primer efecto lateral. Dos pilotajes
# simultáneos anclan, respaldan y recrean la MISMA instancia sin verse.
cerrojo_flujo pilotaje "$INST" || exit 1
VER="$EVID/$SELLO-veredicto.json"
PUERTAS="evidencia_previa ancla snapshot recreate gate salud"

nota "① evidencia ANTES (id · creado · arrancado · digest · reinicios · montajes)"
ANTES="$(evidencia_contenedor "$INST")"; HAY=$?
texto_publica "$EVID/$SELLO-antes.json" "$ANTES" || exit 1
ID_ANTES="$(printf %s "$ANTES" | jq -r '.id // ""')"
IMG_ANTES="$(printf %s "$ANTES" | jq -r '.imagen // ""')"
if [ "$HAY" -eq 0 ]; then nota "   id_antes=$ID_ANTES"; veredicto_anota evidencia_previa ok "$ID_ANTES"
elif [ "$(printf %s "$ANTES" | jq -r '.motivo // ""' 2>/dev/null)" = "ausente" ]; then
  nota "   ausencia explícita: primer pilotaje"; veredicto_anota evidencia_previa ok "sin contenedor previo"
else
  rojo "⛔ inspect no medible NO equivale a primer pilotaje"; veredicto_anota evidencia_previa fallo "inspect no medible"
  veredicto_escribe "$VER" pilotaje "$INST" $PUERTAS; exit 1
fi

# ② ANCLA DE LA IMAGEN SALIENTE. Es el paso que no existía, y sin él la reversión no
# tiene destino: en M0 se midieron CERO etiquetas del estado anterior a `80e9199`.
# Se ancla ANTES de construir nada; anclarla después es anclar la entrante.
if [ -n "$IMG_ANTES" ]; then
  SHA_ANTES="$(salud_lee "$API" | jq -r '.build.sha // "desconocido"')"
  ANCLA="${IMG%%:*}:rollback-${SHA_ANTES:0:12}"
  nota "② anclo la imagen saliente como \`$ANCLA\`"
  if [ "$SECO" -eq 1 ]; then nota "   [seco] docker tag $IMG_ANTES $ANCLA"
  else "$DOCKER" tag "$IMG_ANTES" "$ANCLA" || { rojo "⛔ no pude anclar: no despliego"
         veredicto_anota ancla fallo "docker tag falló"
         veredicto_escribe "$VER" pilotaje "$INST" $PUERTAS; exit 1; }; fi
  # EL DIGEST ES LA EVIDENCIA, NO LA ETIQUETA. `docker tag` no crea nada: apunta un
  # nombre MUTABLE a un id. Si nadie registra el digest, la reversión confía en que esa
  # etiqueta no se haya movido — y si se mueve, va a otro sitio sin decirlo.
  DIG_ANTES=""
  if [ "$SECO" -ne 1 ]; then
    DIG_ANTES="$(digest_de_imagen "$IMG_ANTES")" || {
      rojo "⛔ no pude resolver el digest de la imagen saliente: sin él, el ancla es"
      rojo "   sólo un nombre y la reversión no tendría con qué comprobarse."
      veredicto_anota ancla fallo "digest no resoluble"
      veredicto_escribe "$VER" pilotaje "$INST" $PUERTAS; exit 1; }
    nota "   digest anclado: ${DIG_ANTES:0:23}…"
  fi
  veredicto_anota ancla ok "$ANCLA @ ${DIG_ANTES:-seco}"
  salud_olvida
  ANCLA_JSON="$(python3 - "$ANCLA" "$IMG_ANTES" "$SHA_ANTES" "$DIG_ANTES" <<'PYEOF'
import json,sys; print(json.dumps(dict(zip(("ancla","imagen","sha","digest"),sys.argv[1:])),sort_keys=True))
PYEOF
)"; texto_publica "$EVID/$SELLO-ancla.json" "$ANCLA_JSON" || exit 1
  artefacto_anota "$EVID/$SELLO-ancla.json"
  nota "   para revertir: scripts/m4-rollback.sh --a $ANCLA --digest $DIG_ANTES"
else
  nota "② sin imagen previa que anclar"; veredicto_anota ancla ok "sin imagen previa"
fi

# ③ SNAPSHOT DE DATOS — por la API de respaldo de SQLite, no `cp`.
# `cp` de un fichero con WAL abierto copia un estado que puede no ser consistente;
# `Connection.backup()` es la API que SQLite publica justo para esto. Se guarda FUERA
# del volumen para que borrar el volumen no se lleve también el respaldo.
# ⚠️ El rollback de DATOS es una decisión SEPARADA: esto deja el punto de restauración,
# no lo aplica. Ver docs/M4-PILOT.md §«Rollback de datos».
nota "③ snapshot del almacén (API de respaldo de SQLite), FUERA del volumen"
# EL RESPALDO SALE AL HOST. Mi primera versión lo dejaba en `/data/…`, que ES el volumen
# (`llminbox-data:/data`): un respaldo que muere con lo que respalda no es un respaldo.
# Se hace en `/tmp` del contenedor —capa efímera, no el volumen— y se saca con
# `docker cp`; si sacarlo falla, no se despliega, porque entonces no habría con qué
# volver.
SNAP_TMP="/tmp/llminbox.sqlite.snapshot-$SELLO"
SNAP="$EVID/llminbox.sqlite.snapshot-$SELLO"
if [ "$HAY" -eq 0 ]; then
  if [ "$SECO" -eq 1 ]; then nota "   [seco] backup → $SNAP_TMP → docker cp → $SNAP"
  else
    "$DOCKER" exec "$INST" python3 -c "
import sqlite3,sys
o=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); o.backup(d); d.close(); o.close()
print('ok')" "${LLMINBOX_DB_INTERNO:-/data/llminbox.sqlite}" "$SNAP_TMP" >/dev/null \
      || { rojo "⛔ el snapshot falló: no despliego sin punto de restauración"
           veredicto_anota snapshot fallo "backup no completado"
           veredicto_escribe "$EVID/$SELLO-veredicto.json" pilotaje "$INST" \
             $PUERTAS; exit 1; }
    SNAP_STAGE_DIR="$(umask 077; mktemp -d "$EVID/.snapshot-stage.XXXXXXXX")" || exit 1
    SNAP_STAGE="$SNAP_STAGE_DIR/snapshot.sqlite"
    "$DOCKER" cp "$INST:$SNAP_TMP" "$SNAP_STAGE" >/dev/null 2>&1 \
      || { rojo "⛔ no pude sacar el snapshot al host: quedaría dentro del volumen"
           veredicto_anota snapshot fallo "docker cp falló"
           veredicto_escribe "$EVID/$SELLO-veredicto.json" pilotaje "$INST" \
             $PUERTAS; exit 1; }
    archivo_copia "$SNAP_STAGE" "$SNAP" >/dev/null || { rojo "⛔ snapshot preplantado/no publicable"; exit 1; }
    rm -f -- "$SNAP_STAGE"
    rmdir "$SNAP_STAGE_DIR" 2>/dev/null || true
    "$DOCKER" exec "$INST" rm -f "$SNAP_TMP" >/dev/null 2>&1 || true
    snapshot_fuera_del_volumen "$SNAP" "${LLMINBOX_VOLUMEN:-/data}" \
      || { veredicto_anota snapshot fallo "dentro del volumen o vacío"
           veredicto_escribe "$EVID/$SELLO-veredicto.json" pilotaje "$INST" \
             $PUERTAS; exit 1; }
  fi
  veredicto_anota snapshot ok "$SNAP"
  if [ "$SECO" -ne 1 ]; then
    SNAP_INV="$(valida_snapshot "$SNAP" "${LLMINBOX_VOLUMEN:-/data}")" || {
      veredicto_anota snapshot fallo "copia no íntegra/no inventariable"; exit 1; }
    python3 - "$EVID/$SELLO-snapshot.json" "$SNAP" "$SELLO" "$SNAP_INV" <<'PYEOF'
import json,os,sys
p,snap,sello,inv=sys.argv[1:]; b=json.dumps({"snapshot":snap,"api":"sqlite3.Connection.backup","fuera_del_volumen":True,"sello":sello,"inventario":json.loads(inv)},sort_keys=True).encode()
fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0),0o600)
try:
 pos=0
 while pos<len(b): pos+=os.write(fd,b[pos:])
 os.fsync(fd)
finally: os.close(fd)
d=os.open(os.path.dirname(p),os.O_RDONLY); os.fsync(d); os.close(d)
PYEOF
    [ "$?" -eq 0 ] || { veredicto_anota snapshot fallo "inventario no publicable"; exit 1; }
    artefacto_anota "$EVID/$SELLO-snapshot.json"
  fi
else
  nota "   sin contenedor previo: nada que respaldar"
  veredicto_anota snapshot ok "sin contenedor previo"
fi

# ④ DESPLIEGUE. `llmi up` recrea; este guion NO reinicia nunca — un restart conserva el
# id del contenedor y no es un despliegue.
nota "④ recreo la instancia"
salud_olvida    # el mundo va a cambiar: la caché de la fase previa deja de describirlo
if [ "$SECO" -eq 1 ]; then nota "   [seco] ./llmi up --cobertura-verificada"
else ./llmi up --cobertura-verificada; nota "   rc de llmi up = $? (informativo: el rc NO es la evidencia)"; fi
veredicto_anota recreate ok "llmi up ejecutado; lo acredita el gate, no el rc"

# ⑤ GATE POST-RECREATE. Aquí es donde el despliegue se acredita o se cae.
nota "⑤ gate post-recreate"
if [ "$SECO" -eq 1 ]; then
  # DRY-RUN DEJA VEREDICTO, Y NO ACREDITADO. Salir 0 sin escribir nada hacía que un
  # `--seco` fuera indistinguible de un pilotaje certificado para quien encadene por rc.
  nota "   [seco] gates omitidos: el veredicto queda NO ACREDITADO a propósito"
  veredicto_anota gate no_medido "dry-run"
  veredicto_anota salud no_medido "dry-run"
  veredicto_escribe "$VER" pilotaje "$INST" $PUERTAS
  RC_VER=$?; artefacto_anota "$VER"
  rojo "⛔ dry-run: veredicto NO acreditado en $VER (correcto: no se desplegó nada)"
  exit "$RC_VER"
fi
DESPUES="$(evidencia_contenedor "$INST")"
texto_publica "$EVID/$SELLO-despues.json" "$DESPUES" || exit 1
if gate_post_recreate "$INST" "$ID_ANTES" "$API"; then
  veredicto_anota gate ok
else
  veredicto_anota gate fallo "ver stderr"
fi
# SALUD: el CUERPO. `gate_post_recreate` mira `.v8` y `.artefacto`; ninguno mira el flag.
if gate_salud "$API"; then veredicto_anota salud ok
else veredicto_anota salud fallo "el servicio no se declara sano"; fi

for f in "$EVID/$SELLO-antes.json" "$EVID/$SELLO-despues.json"; do artefacto_anota "$f"; done
veredicto_escribe "$VER" pilotaje "$INST" $PUERTAS
RC_VER=$?
artefacto_anota "$VER"
if [ "$RC_VER" -eq 0 ]; then
  verde "✓ PILOTAJE ACREDITADO · veredicto en $VER"
  exit 0
fi
rojo "⛔ PILOTAJE NO ACREDITADO. Puertas fallidas: $(jq -r '.fallidas | join(", ")' "$VER" 2>/dev/null)"
rojo "   El ancla \`${ANCLA:-<ninguna>}\` y el snapshot \`${SNAP:-<ninguno>}\` (en el host,"
rojo "   fuera del volumen) existen: la reversión es posible."
exit 1
