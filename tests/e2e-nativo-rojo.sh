#!/usr/bin/env bash
# ── E2E DEL CAMINO NATIVO — NACEN ROJOS A PROPÓSITO ──────────────────────────
#
# QUÉ SON. Las únicas pruebas de este repo que hablan con un servicio de verdad.
# `tests/nativo.sh` prueba el CLI contra un `curl` falso: acredita el CONTRATO del
# cliente y NO acredita interoperabilidad con nadie.
#
# POR QUÉ NACEN ROJOS. Medido en la instancia viva: `/sessions /whoami /events
# /receipts/x /leases/x` ⇒ 404 los cinco, con ⊕ de control en la bandeja ⇒ 200. Y 0
# ficheros de pasarela en las 17 refs del repo. Un test que pasara hoy mediría otra cosa.
#
# 🩸 LO QUE UN AUDITOR ME CAZÓ EN LA PRIMERA VERSIÓN, y por qué esta es distinta:
#   ① emitía POST/PUT/DELETE con `LLMI_E2E=1` y sin `LLMI_E2E_MUTA`. Hoy dan 404 y no
#      pasa nada; el día que exista la pasarela, sondear su EXISTENCIA habría abierto
#      sesiones y tocado leases. Ahora la sonda por defecto es GET/HEAD y hay una
#      GUARDA EN EL EMISOR que se niega a mandar un método mutante sin el permiso.
#      Un contrato que depende de que cada llamada se acuerde no es un contrato.
#   ② contaba `2*|4*|5*` como «existe ⇒ VERDE». Con un stub que devolvía 500 en todo,
#      daba 17 verdes. Un 500 no es conformidad: es una ruta que responde mal.
#   ③ el brazo de auth caía a OK por defecto, así que 401/500 en las dos formas salía verde.
#   ④ el replay sólo miraba el flag `replayed`. El ADR §136 exige que cite los MISMOS
#      `event_id` y `receipt_id`: un stub que devuelve el flag y NO los ids pasaba.
#   ⑤ tenía un backtick DENTRO de comillas dobles — `replayed` se EJECUTABA.
#
# 🔑 UN ROJO DICE DE QUÉ CLASE ES, porque son acciones opuestas:
#     AUSENTE   la ruta no existe donde el ADR dice que debe haber algo
#               ⇒ no hay nada que arreglar en el cliente: falta construir.
#     INCOMPAT  responde y NO cumple el ADR ⇒ decisión de protocolo, y es del CTO.
#     VERDE     cumple.
#
# ⛔ NO SE EJECUTA SOLO: exige LLMI_E2E=1, y lo que MUTA exige además LLMI_E2E_MUTA=1.
set -uo pipefail
cd "$(dirname "$0")/.." || { echo "no puedo entrar en el repo" >&2; exit 3; }

API="${LLMINBOX_API:-http://127.0.0.1:${LLMINBOX_PORT:-8077}}"
# NO se lee el token COMPARTIDO: ninguna ruta nativa lo admite (D8), asi que tenerlo a mano
# solo serviria para volver a colarlo. Que esta variable ya no exista es la prueba de que el
# arnes dejo de tapar el P0 — antes lo mandaba a /sessions y a /events.
MUTA="${LLMI_E2E_MUTA:-0}"
NAT="${LLMINBOX_NATIVE_PREFIX:-/native/v1}"

# 🩸 RUN-ID ÚNICO POR EJECUCIÓN — P1 del auditor sobre 3d00c8f, y era un defecto de los que
# sólo se ven la SEGUNDA vez: las claves eran fijas (`e2e-probe-1`/`-2`), así que contra un
# gateway CONFORME la primera corrida daba 201 y la segunda 200 replayed. El arnés pasaba
# una vez y fallaba para siempre, y el rojo habría acusado a la pasarela de una colisión
# que ponía yo. Es la clase «mi fixture ES el defecto», con la agravante de que el sujeto
# se comporta CORRECTAMENTE: el replay 200 es exactamente lo que el ADR manda.
#
# Inyectable para que los tests sean deterministas; aleatorio cuando nadie lo fija.
RUNID="${LLMI_E2E_RUNID:-$(date -u +%Y%m%dT%H%M%SZ)-$$-${RANDOM:-0}}"
# TODO lo que colisiona se deriva de él: claves de idempotencia Y nombres de recurso.
K1="e2e-$RUNID-1"; K2="e2e-$RUNID-2"; K3="e2e-$RUNID-3"; REC="probe-$RUNID"

# 🩸 EL ARNES MANDABA `X-Llminbox-Token` A RUTAS NATIVAS. Eso es el legado que D8 cierra,
# y con un stub que no validaba auth el fallo quedaba TAPADO: con stub estricto,
# POST /sessions da 401 RUNTIME_CREDENTIAL_REQUIRED y los events dan 401.
# La credencial de WORKLOAD abre sesion; el token de SESION muta. Ninguna es el compartido.
WLFILE="${LLMINBOX_WORKLOAD_FILE:-$HOME/.llminbox-workload.credential}"
WLCRED=""
if [ -r "$WLFILE" ]; then WLCRED="$(tr -d '\n' < "$WLFILE")"; fi
# 🩸 EL ARNÉS VOLVÍA A PONER EL SECRETO EN argv con `-H`, justo lo que el CLI acaba de
# dejar de hacer: un `ps` durante la corrida lo ve. Aquí también va por `--config`, con
# modo 600 y borrado al salir.
CFG_WL=""; CFG_SES=""
cfg_para() {   # $1=secreto → imprime la ruta de un config de curl con esa cabecera
  local f
  f="$(umask 077; mktemp)"
  chmod 600 "$f" 2>/dev/null || { rm -f "$f"; echo ""; return 1; }
  printf 'header = "Authorization: Bearer %s"\n' "$1" > "$f"
  printf '%s' "$f"
}
AUTH=()
if [ -n "$WLCRED" ]; then
  CFG_WL="$(cfg_para "$WLCRED")"
  [ -n "$CFG_WL" ] && AUTH=(--config "$CFG_WL")
fi
SES=()   # cabecera de SESION; se llena al abrirla, y solo entonces se puede mutar

if [ "${LLMI_E2E:-0}" != "1" ]; then
  echo "· e2e desarmado. Habla con un servicio REAL: exporta LLMI_E2E=1." >&2
  echo "  (y LLMI_E2E_MUTA=1 además para los casos que ESCRIBEN)" >&2
  exit 2
fi

AUSENTE=0; INCOMPAT=0; VERDE=0
ROJO() { AUSENTE=$((AUSENTE+1));   printf '  🔴 AUSENTE   %-44s %s\n' "$1" "$2"; }
INC()  { INCOMPAT=$((INCOMPAT+1)); printf '  🟠 INCOMPAT  %-44s %s\n' "$1" "$2"; }
OK()   { VERDE=$((VERDE+1));       printf '  ✅ VERDE     %-44s %s\n' "$1" "$2"; }

CUERPO="$(mktemp)"
CFG_MALA=""
trap 'rm -f "$CUERPO" "$CFG_WL" "$CFG_SES" "$CFG_MALA" 2>/dev/null' EXIT

# GUARDA EN EL EMISOR, no en cada llamada. Es la única forma de que «sin MUTA no se
# emite nada que escriba» sea una propiedad del programa y no una costumbre.
H=""; RCC=0
pide() {   # $1=método $2=ruta [$3=datos] [$4..=cabeceras]
  local m="$1" ruta="$2" datos="${3:-}"
  shift 2; [ $# -gt 0 ] && shift
  case "$m" in
    POST|PUT|DELETE|PATCH)
      if [ "$MUTA" != "1" ]; then
        echo "  ⛔ BUG DEL ARNÉS: intentó $m $ruta sin LLMI_E2E_MUTA=1. NO se envía." >&2
        H="000"; RCC=99; return 1
      fi ;;
  esac
  local a=(-s -m 10 -X "$m" -o "$CUERPO" -w '%{http_code}')
  if [ -n "$datos" ]; then a=("${a[@]}" -H 'Content-Type: application/json' -d "$datos"); fi
  H="$(curl "${a[@]}" "$@" "$API$ruta" 2>/dev/null)"; RCC=$?
  case "$H" in ''|*[!0-9]*) H="000" ;; esac
  return 0
}
campo() {
  python3 -c '
import json,sys
try: d=json.load(open(sys.argv[1]))
except Exception: raise SystemExit
v=d.get(sys.argv[2])
print("" if v is None else (v if not isinstance(v,(dict,list)) else json.dumps(v)))' \
    "$CUERPO" "$1" 2>/dev/null
}

echo "── E2E nativo contra $API   (MUTA=$MUTA) ──"
pide GET /health
if [ "$H" = "200" ]; then
  OK "control /health" "200 — el servicio responde"
else
  echo "  ⛔ el servicio no responde (/health ⇒ $H, curl rc=$RCC). Sin él TODO saldría"
  echo "     «ausente» por la razón equivocada y no mediría nada. Paro."
  exit 3
fi
echo

echo "── ① RUTAS QUE EL ADR NOMBRA — sonda NO MUTANTE ──"
# Se sondea con GET aunque la ruta sea de POST: un 405 dice «existe» sin escribir nada.
# ⛔ COTA: una pasarela que devolviera 404 ante método incorrecto sería indistinguible
# de una ausente. Se declara porque cambia lo que este bloque puede afirmar.
for par in "/sessions:POST" "/whoami:GET" "/events:POST"; do
  r="$NAT${par%%:*}"; real="${par##*:}"
  pide GET "$r" "" ${AUTH[@]+"${AUTH[@]}"}
  case "$H" in
    404)         ROJO "$real $r" "404 — el ADR la nombra y no existe" ;;
    405|401|403) OK   "$real $r" "$H — existe (sonda GET, sin escribir)" ;;
    2*)          OK   "$real $r" "$H — existe" ;;
    5*)          INC  "$real $r" "$H — responde con error de servidor, no es conformidad" ;;
    *)           INC  "$real $r" "$H inesperado" ;;
  esac
done
echo

echo "── ② PREFIX — el ADR NO fija ninguno; el CLI asume raíz ──"
# El eje ya no es «raíz vs /v1»: D9 fijó `/native/v1`. Se mide que el CLIENTE y la
# pasarela coinciden en ÉL, y que la RAÍZ no sirve la superficie nativa (si la sirviera,
# habría dos y el log no diría cuál).
pide GET "/whoami"      "" ${AUTH[@]+"${AUTH[@]}"}; V1="$H"
pide GET "$NAT/whoami"  "" ${AUTH[@]+"${AUTH[@]}"}; RAIZ="$H"
if [ "$V1" = "404" ] && [ "$RAIZ" = "404" ]; then
  ROJO "prefix ($NAT)" "las dos 404: no hay superficie nativa en ninguna de las dos"
elif [ "$RAIZ" = "404" ] && [ "$V1" != "404" ]; then
  INC "prefix ($NAT)" "la RAÍZ responde ($V1) y $NAT no: el CLI apunta al sitio equivocado"
elif [ "$RAIZ" = "404" ]; then
  ROJO "prefix ($NAT)" "$NAT no responde"
else
  case "$RAIZ" in
    5*) INC "prefix ($NAT)" "$NAT da $RAIZ: responde mal, no es conformidad" ;;
    *)  OK  "prefix ($NAT)" "responde en $NAT ($RAIZ), como asume el CLI (D9)" ;;
  esac
fi
echo

echo "── ③ AUTH — hace falta EVIDENCIA POSITIVA del esquema canónico (D8: Bearer) ──"
# 🩸 SEGUNDO P1 del auditor: con 403 en LOS DOS esquemas esto imprimía VERDE. El defecto de
# fondo no era la rama que faltaba: era creer que el esquema se puede acreditar con una
# credencial INVÁLIDA. No se puede — un gateway `Bearer` y uno de cualquier otro esquema
# contestan 401 igual ante un token malo. Lo único que acredita `Bearer` es que, CON UNA
# CREDENCIAL VÁLIDA, responda 2xx. Y eso exige abrir sesión, que ESCRIBE.
# ⇒ Sin MUTA el veredicto honesto es NO ACREDITADO, y NO ACREDITADO no es verde.
pide GET "$NAT/whoami" "" -H "Authorization: Llminbox-Session probe-invalida"; A1="$H"
pide GET "$NAT/whoami" "" -H "Authorization: Bearer probe-invalida";           A2="$H"
case "$A1$A2" in
  *5*) INC "esquema de Authorization" "error de servidor (propio ⇒ $A1 · Bearer ⇒ $A2)" ;;
  *)
    if [ "$A1" = "404" ] && [ "$A2" = "404" ]; then
      ROJO "esquema de Authorization" "404 con los dos: no hay superficie que interrogar"
    elif [ "$MUTA" != "1" ]; then
      # Aquí caen 401/401 y 403/403: son respuestas COHERENTES con cualquier esquema.
      ROJO "esquema de Authorization" "NO ACREDITADO sin credencial válida (propio ⇒ $A1 · Bearer ⇒ $A2) — exige LLMI_E2E_MUTA=1"
    else
      # Evidencia positiva: sesión real, y `Bearer` tiene que abrir la puerta.
      pide POST "$NAT/sessions" "" ${AUTH[@]+"${AUTH[@]}"}
      TOK_VIVO="$(campo token)"
      if [ -z "$TOK_VIVO" ]; then
        ROJO "esquema de Authorization" "no pude abrir sesión (POST sessions ⇒ $H): sin token válido no hay evidencia"
      else
        pide GET "$NAT/whoami" "" -H "Authorization: Bearer $TOK_VIVO";           B_OK="$H"
        pide GET "$NAT/whoami" "" -H "Authorization: Llminbox-Session $TOK_VIVO"; P_OK="$H"
        case "$B_OK" in
          2*)
            if [ "$P_OK" = "401" ] || [ "$P_OK" = "403" ]; then
              OK "esquema de Authorization" "Bearer con token VÁLIDO ⇒ $B_OK y el esquema propio ⇒ $P_OK (D8 acreditado)"
            else
              INC "esquema de Authorization" "Bearer ⇒ $B_OK pero el esquema propio TAMBIÉN pasa ($P_OK): acepta dos, y entonces el log no dice cuál"
            fi ;;
          401|403) INC "esquema de Authorization" "Bearer con token VÁLIDO ⇒ $B_OK: NO admite el esquema que D8 ratifica" ;;
          5*)      INC "esquema de Authorization" "Bearer con token válido ⇒ $B_OK (error de servidor)" ;;
          *)       INC "esquema de Authorization" "Bearer con token válido ⇒ $B_OK inesperado" ;;
        esac
      fi
    fi ;;
esac
echo

echo "── ④ ENVELOPE e IDEMPOTENCIA de POST /events (§86-136) ──"
if [ "$MUTA" != "1" ]; then
  ROJO "envelope de /events"    "no medido: exige LLMI_E2E_MUTA=1 (ESCRIBE)"
  ROJO "atribucion rechazada"   "no medido: exige LLMI_E2E_MUTA=1 (ESCRIBE)"
  ROJO "replay cita los MISMOS ids" "no medido: exige LLMI_E2E_MUTA=1 (ESCRIBE)"
  ROJO "409 con misma clave y cuerpo distinto" "no medido: exige LLMI_E2E_MUTA=1"
else
  # LA SESION PRIMERO: `/events` exige token de SESION (Bearer), no la credencial de
  # workload y desde luego no el compartido. Sin esto, los cuatro POST darian 401 y el
  # arnes acusaria al envelope de un fallo que es de auth.
  pide POST "$NAT/sessions" "" ${AUTH[@]+"${AUTH[@]}"}
  TOK_SES="$(campo token)"
  if [ -z "$TOK_SES" ]; then
    ROJO "sesion para mutar" "no pude abrir sesion (POST sessions ⇒ $H): sin ella /events no se puede medir"
  else
    CFG_SES="$(cfg_para "$TOK_SES")"
    [ -n "$CFG_SES" ] && SES=(--config "$CFG_SES")
  fi
  P='{"type":"message","verb":"inform","to":["fe"],"kind":"FYI","head":"e2e","body":"e2e","causes":[]}'
  pide POST "$NAT/events" "$P" ${SES[@]+"${SES[@]}"} -H "Idempotency-Key: $K1"
  E1="$(campo event_id)"; R1="$(campo receipt_id)"
  case "$H" in
    404) ROJO "envelope de /events" "404 — no hay dónde probarlo" ;;
    202)
      # El ADR vigente (bdec479, «ratify accepted and replay wire semantics») fija
      # `202 Accepted` en la aceptación y dice por qué `201 Created` seria FALSO: prometeria
      # un recurso proyectado que aun no existe. Ya no hay divergencia que tolerar.
      if [ -z "$E1" ] || [ -z "$R1" ]; then
        INC "envelope de /events" "202 SIN event_id/receipt_id (ADR §101-107)"
      else
        OK "envelope de /events" "202 Accepted con los dos ids (ADR bdec479)"
      fi ;;
    201) INC "envelope de /events" "201 Created — el ADR vigente manda 202 Accepted (bdec479): 201 promete un recurso proyectado que no existe" ;;
    401|403) ROJO "envelope de /events" "$H — hace falta sesión" ;;
    *) INC "envelope de /events" "$H — el ADR manda 201 al insertar" ;;
  esac

  PA='{"type":"message","verb":"inform","to":["fe"],"kind":"FYI","head":"e2e","body":"x","principal":"otro"}'
  pide POST "$NAT/events" "$PA" ${SES[@]+"${SES[@]}"} -H "Idempotency-Key: $K2"
  case "$H" in
    404)     ROJO "atribucion rechazada" "404 — no hay dónde probarlo" ;;
    422|400) OK   "atribucion rechazada" "$H — rechaza el principal del cuerpo" ;;
    2*)      INC  "atribucion rechazada" "$H — ACEPTÓ un principal del cliente (el ADR lo RECHAZA)" ;;
    *)       INC  "atribucion rechazada" "$H inesperado" ;;
  esac

  # ADR §136: «every replay cite the same event_id and receipt_id, with a response-only
  # replayed flag». El flag SOLO no acredita nada: hay que comparar los ids con los del
  # original. Un stub que devuelve el flag sin ids es la forma barata de mentir aquí.
  pide POST "$NAT/events" "$P" ${SES[@]+"${SES[@]}"} -H "Idempotency-Key: $K1"
  E2="$(campo event_id)"; R2="$(campo receipt_id)"; FLAG="$(campo replayed)"
  case "$H" in
    404) ROJO "replay cita los MISMOS ids" "404 — no hay dónde probarlo" ;;
    409) INC  "replay cita los MISMOS ids" "409 al REPETIR el mismo cuerpo (el ADR reserva 409 para cuerpo DISTINTO)" ;;
    2*)
      if [ "$FLAG" != "True" ] && [ "$FLAG" != "true" ]; then
        INC "replay cita los MISMOS ids" "$H sin el flag que el ADR exige"
      elif [ -z "$E2" ] || [ -z "$R2" ]; then
        INC "replay cita los MISMOS ids" "flag presente pero SIN ids (ADR §136 exige citarlos)"
      elif [ "$E1" != "$E2" ] || [ "$R1" != "$R2" ]; then
        INC "replay cita los MISMOS ids" "ids DISTINTOS del original ($E1/$R1 vs $E2/$R2)"
      else
        OK "replay cita los MISMOS ids" "$H · flag y los dos ids coinciden"
      fi ;;
    *) INC "replay cita los MISMOS ids" "$H inesperado" ;;
  esac

  # 🔑 IDs SERVER-ISSUED: con ids ESTATICOS, «el replay conserva los ids» pasaria aunque el
  # servidor los repitiera SIEMPRE. Hace falta la otra mitad: claves DISTINTAS ⇒ ids DISTINTOS.
  P3='{"type":"message","verb":"inform","to":["fe"],"kind":"FYI","head":"otro-e2e","body":"otro","causes":[]}'
  pide POST "$NAT/events" "$P3" ${SES[@]+"${SES[@]}"} -H "Idempotency-Key: $K3"
  E3="$(campo event_id)"; R3="$(campo receipt_id)"
  case "$H" in
    404) ROJO "ids distintos con clave distinta" "404 — no hay dónde probarlo" ;;
    2*)
      if [ -z "$E3" ]; then INC "ids distintos con clave distinta" "$H sin event_id"
      elif [ "$E3" = "$E1" ] || [ "$R3" = "$R1" ]; then
        INC "ids distintos con clave distinta" "REPITE los ids ($E3/$R3): son estáticos, no emitidos por aceptación"
      else OK "ids distintos con clave distinta" "$H con ids nuevos ($E3)"; fi ;;
    *) INC "ids distintos con clave distinta" "$H inesperado" ;;
  esac

  PD='{"type":"message","verb":"inform","to":["fe"],"kind":"FYI","head":"OTRO","body":"distinto"}'
  pide POST "$NAT/events" "$PD" ${SES[@]+"${SES[@]}"} -H "Idempotency-Key: $K1"
  case "$H" in
    404) ROJO "409 con misma clave y cuerpo distinto" "404 — no hay dónde probarlo" ;;
    409) OK   "409 con misma clave y cuerpo distinto" "409 (ADR §117)" ;;
    *)   INC  "409 con misma clave y cuerpo distinto" "$H — el ADR manda 409 sin mutar" ;;
  esac
fi
echo

echo "── ④b D11: el cuerpo de error va PLANO, no envuelto ──"
# El CLI lee `code` en la RAIZ. Si la pasarela envuelve ({"error":{"code":…}}), su tabla de
# consejos queda INERTE ENTERA y ningun mensaje llega — el defecto mas caro y el menos visible.
CFG_MALA="$(cfg_para "credencial-invalida")"
pide GET "$NAT/whoami" "" --config "$CFG_MALA"
case "$H" in
  404)   ROJO "cuerpo de error PLANO" "404 — no hay dónde probarlo" ;;
  401|403)
    if [ -n "$(campo code)" ]; then OK "cuerpo de error PLANO" "$H con \`code\` en la raíz (D11)"
    elif [ -n "$(campo error)" ]; then INC "cuerpo de error PLANO" "$H ENVUELTO en \`error\`: el CLI lee en la raíz y su tabla queda inerte"
    else INC "cuerpo de error PLANO" "$H sin \`code\` ni \`error\`"; fi ;;
  2*)    INC "cuerpo de error PLANO" "$H — una credencial inválida no puede dar 2xx" ;;
  *)     INC "cuerpo de error PLANO" "$H inesperado" ;;
esac
echo

echo "── ⑤ RUTAS QUE EL CLI USA Y EL ADR **NO** NOMBRA (propuesta sin adjudicar) ──"
for par in "/sessions/refresh:POST" "/receipts/$REC:GET" "/events/$REC/receipt:GET" "/leases/$REC:POST"; do
  r="$NAT${par%%:*}"; real="${par##*:}"
  pide GET "$r" "" ${AUTH[@]+"${AUTH[@]}"}
  case "$H" in
    404)         ROJO "$real $r" "404 — propuesta, no texto del ADR" ;;
    405|401|403) OK   "$real $r" "$H — existe (sonda GET)" ;;
    2*)          OK   "$real $r" "$H — existe" ;;
    5*)          INC  "$real $r" "$H — responde con error de servidor" ;;
    *)           INC  "$real $r" "$H inesperado" ;;
  esac
done

echo
echo "══ RESUMEN ══  ausentes=$AUSENTE  incompatibles=$INCOMPAT  verdes=$VERDE"
if [ "$INCOMPAT" -gt 0 ]; then
  echo "🟠 Hay INCOMPATIBILIDADES reales: eso es una decisión de protocolo, y es del CTO."
  exit 1
fi
if [ "$AUSENTE" -gt 0 ]; then
  echo "🔴 ROJO POR AUSENCIA, el estado ESPERADO hoy: no hay pasarela nativa."
  echo "   Esto no pide tocar el cliente. Pide construirla."
  exit 1
fi
echo "✅ Todo verde: la pasarela cumple el ADR en lo que este fichero sabe medir."
