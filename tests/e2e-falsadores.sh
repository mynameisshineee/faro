#!/usr/bin/env bash
# ── ¿SABE EL ARNÉS E2E DECIR QUE NO? ─────────────────────────────────────────
#
# `e2e-nativo-rojo.sh` decía «16 ausentes, 0 incompatibles» contra un servicio sin
# pasarela. Eso acredita que sabe ver un 404 y NADA MÁS: un arnés que nunca ha visto
# un gateway no puede afirmar qué haría con uno. Un auditor lo probó con un stub roto
# y sacó 17 VERDES. Esto es lo que faltaba.
#
# Tres perfiles, y el del medio es el que importa:
#   ausente   404 en todo menos /health   -> el arnés debe dar ROJO POR AUSENCIA
#   roto      responde y MIENTE           -> debe dar INCOMPAT, y ni un solo verde de más
#   conforme  cumple el ADR               -> debe dar VERDE
# Sin `conforme`, «marca incompat» podría ser «marca incompat a todo».
set -uo pipefail
cd "$(dirname "$0")/.." || { echo "no puedo entrar en el repo" >&2; exit 3; }
REPO="$PWD"
BASE="$(mktemp -d)"
MALOS=0; TOTAL=0
bien() { TOTAL=$((TOTAL+1)); printf "  ✓ %s\n" "$1"; }
mal()  { TOTAL=$((TOTAL+1)); MALOS=$((MALOS+1))
         printf "  ✗ %s\n     esperado: %s\n     obtenido: %s\n" "$1" "$2" "$3"; }

PID=""
parar() { if [ -n "$PID" ]; then kill "$PID" 2>/dev/null; wait "$PID" 2>/dev/null; PID=""; fi; }
# shellcheck disable=SC2329  # se invoca por `trap limpia EXIT`, que shellcheck no ve.
# Se silencia NOMBRANDO el motivo en vez de quitar el trap: sin el, un fallo a media
# pasada dejaria el stub escuchando en un puerto y el temporal en disco.
limpia() { parar; rm -rf "$BASE"; }
trap limpia EXIT

PUERTO=0
arranca() {   # $1=perfil -> deja el stub vivo y LOGSTUB apuntando a su registro
  parar
  PUERTO=$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')
  LOGSTUB="$BASE/$1.log"
  python3 "$REPO/tests/falso/gateway-stub.py" "$1" "$PUERTO" "$LOGSTUB" &
  PID=$!
  # Esperar a que escuche, sin dormir a ciegas: un sleep fijo o sobra o se queda corto.
  local i=0
  while [ "$i" -lt 100 ]; do
    if curl -s -o /dev/null -m 1 "http://127.0.0.1:$PUERTO/health" 2>/dev/null; then return 0; fi
    i=$((i+1))
  done
  echo "el stub $1 no llegó a escuchar" >&2; return 1
}

corre_e2e() {   # $1=MUTA [$2=run-id] -> deja la salida en $SAL y el rc en $RC
  # El run-id se pasa SIEMPRE: vacio significa «generalo tu». Un prefijo condicional
  # (`${2:+VAR=...}`) NO es una asignacion valida — se parsea como comando y da rc=127 en
  # las 25 comprobaciones a la vez, que es como se vio.
  SAL="$( LLMI_E2E=1 LLMI_E2E_MUTA="$1" LLMI_E2E_RUNID="${2:-}" \
          LLMINBOX_API="http://127.0.0.1:$PUERTO" \
          LLMINBOX_WORKLOAD_FILE="$BASE/workload" \
          bash "$REPO/tests/e2e-nativo-rojo.sh" 2>&1 )"; RC=$?
}
# Se usa en los mensajes de fallo: un ✗ que no ensena el resumen obliga a re-correr a mano.
resumen() { printf '%s' "$SAL" | grep -oE 'ausentes=[0-9]+  incompatibles=[0-9]+  verdes=[0-9]+'; }
campo_res() { printf '%s' "$SAL" | sed -n "s/.*$1=\([0-9]*\).*/\1/p" | tail -1; }
# La credencial de WORKLOAD abre sesion; el stub estricto rechaza cualquier otra cosa.
printf 'wl-credencial\n' >> "$BASE/workload"

echo "── ⓪ desarmado: sin LLMI_E2E no corre ──"
RC0=$( LLMI_E2E=0 bash "$REPO/tests/e2e-nativo-rojo.sh" >/dev/null 2>&1; echo $? )
if [ "$RC0" = "2" ]; then bien "sin LLMI_E2E ⇒ rc=2"
else mal "sin LLMI_E2E ⇒ rc=2" "2" "$RC0"; fi

echo "── ① perfil AUSENTE: rojo por ausencia, cero incompatibles ──"
arranca ausente || exit 3
corre_e2e 0
if [ "$RC" = "1" ]; then bien "rc=1"
else mal "rc=1" "1" "$RC"; fi
if [ "$(campo_res incompatibles)" = "0" ]; then bien "incompatibles=0 (no inventa conflictos)"
else mal "incompatibles=0" "0" "$(resumen)"; fi
if [ "$(campo_res ausentes)" -gt 0 ]; then bien "ausentes>0"
else mal "ausentes>0" ">0" "$(resumen)"; fi

echo "── ② 🔑 SIN MUTA NO SE EMITE NI UN MÉTODO QUE ESCRIBA ──"
# La prueba no es que el arnés lo diga: es el REGISTRO del stub, que ve lo que llegó.
MUT=$(grep -cE '^(POST|PUT|DELETE|PATCH) ' "$LOGSTUB" || true)
if [ "$MUT" = "0" ]; then bien "el stub no registró NINGÚN POST/PUT/DELETE"
else mal "0 métodos mutantes con MUTA=0" "0" "$MUT (registrados: $(grep -E '^(POST|PUT|DELETE)' "$LOGSTUB" | tr '\n' ' '))"; fi
LEC=$(grep -cE '^(GET|HEAD) ' "$LOGSTUB" || true)
if [ "$LEC" -gt 0 ]; then bien "⊕ y sí registró lecturas ($LEC) — el registro NO está muerto"
else mal "⊕ el registro ve lecturas" ">0" "$LEC"; fi

echo "── ③ 🔑 perfil ROTO: responde y MIENTE ⇒ INCOMPAT, no verdes ──"
arranca roto || exit 3
corre_e2e 1
if [ "$(campo_res incompatibles)" -gt 0 ]; then bien "incompatibles>0 (lo caza)"
else mal "incompatibles>0 con un stub que miente" ">0" "$(resumen)"; fi
if [ "$RC" = "1" ]; then bien "y sale rc=1"
else mal "rc=1" "1" "$RC"; fi
case "$SAL" in *"error de servidor"*) bien "nombra el 500 como NO conformidad" ;;
               *) mal "nombra el 500" "«error de servidor»" "$(printf '%s' "$SAL" | grep -c INCOMPAT) incompat sin esa frase" ;; esac
case "$SAL" in *"SIN ids"*) bien "caza el replay con flag y SIN ids (ADR §136)" ;;
               *) mal "caza el replay sin ids" "«SIN ids»" "no lo dice" ;; esac

echo "── ④ perfil CONFORME: si marcara incompat a todo, no discriminaría ──"
arranca conforme || exit 3
corre_e2e 1
if [ "$(campo_res incompatibles)" = "0" ]; then bien "incompatibles=0 con un stub que cumple"
else mal "incompatibles=0 con stub conforme" "0" "$(resumen)"; fi
if [ "$(campo_res verdes)" -gt 5 ]; then bien "y verdes>5"
else mal "verdes>5" ">5" "$(resumen)"; fi
case "$SAL" in *"flag y los dos ids coinciden"*) bien "acredita el replay comparando IDS" ;;
               *) mal "acredita el replay por ids" "«flag y los dos ids coinciden»" "no aparece" ;; esac

echo "── ⑤ 🔑 DOS CORRIDAS CONSECUTIVAS, las dos rc=0, sin colisión ──"
# P1 del auditor: las claves eran fijas (`e2e-probe-1`), asi que contra un gateway CONFORME
# la 1ª corrida daba 201 y la 2ª un replay 200. El arnes pasaba UNA vez y fallaba para
# siempre — y el rojo habria acusado a la pasarela de una colision que ponia el fixture.
# El sujeto se comportaba BIEN: el replay 200 es lo que el ADR manda.
arranca conforme || exit 3
corre_e2e 1; RC_A="$RC"
corre_e2e 1; RC_B="$RC"
if [ "$RC_A" = "0" ] && [ "$RC_B" = "0" ]; then bien "dos corridas seguidas, las dos rc=0"
else mal "dos corridas seguidas rc=0" "0 y 0" "$RC_A y $RC_B"; fi
# ⊕ una tercera, por si el defecto fuera de la 2ª y no del patrón.
corre_e2e 1
if [ "$(campo_res incompatibles)" = "0" ]; then bien "⊕ y la 3ª tampoco marca incompatibles"
else mal "⊕ la 3ª sin incompatibles" "0" "$(resumen)"; fi
# 🩸 ESTE CONTROL ERA FALSO y lo cazó una auditoría: no asertaba el `rc` y pasaba con un
# `grep` de «replay» que aparece SIEMPRE (está en el nombre del aserto). Con run-id FIJO la
# 2ª corrida da rc=1 —el 1er POST ya no es una aceptación, es un replay 200— y eso NO es un
# defecto del gateway: es el fixture repitiendo la clave. Se asertan LOS DOS rc, que es lo
# único que separa «el run-id sirve» de «no sirve».
arranca conforme || exit 3
corre_e2e 1 fijo-para-el-control; RC_F1="$RC"
corre_e2e 1 fijo-para-el-control; RC_F2="$RC"
if [ "$RC_F1" = "0" ] && [ "$RC_F2" != "0" ]; then
  bien "⊖ con run-id FIJO la 2ª corrida SÍ rompe (rc $RC_F1 -> $RC_F2): la colisión es real"
else
  mal "⊖ run-id fijo rompe la 2ª" "rc 0 y luego ≠0" "$RC_F1 y $RC_F2 · $(resumen)"
fi
# ⊕ y el mismo par con run-ids DISTINTOS tiene que dar 0 y 0 — es lo que prueba que el
# run-id es la cura y no una casualidad del orden.
arranca conforme || exit 3
corre_e2e 1 unico-A; RC_A2="$RC"
corre_e2e 1 unico-B; RC_B2="$RC"
if [ "$RC_A2" = "0" ] && [ "$RC_B2" = "0" ]; then bien "⊕ con run-ids DISTINTOS: 0 y 0"
else mal "⊕ run-ids distintos ⇒ 0 y 0" "0 y 0" "$RC_A2 y $RC_B2 · $(resumen)"; fi
# ⊕ y las claves de idempotencia enviadas tienen que ser TODAS distintas entre las dos
CLAVES=$(grep -oE 'Idempotency-Key: [^ ]+' "$LOGSTUB" | sort -u | wc -l | tr -d " ")
POSTS=$(grep -cE '^POST ' "$LOGSTUB" || true)
if [ "$CLAVES" -ge 4 ]; then bien "⊕ $CLAVES claves ÚNICAS sobre $POSTS POST (ninguna se repite entre corridas)"
else mal "⊕ claves únicas por corrida" "≥4" "$CLAVES sobre $POSTS POST"; fi

echo "── ⑦ 🔑 P0: EL TOKEN COMPARTIDO NO ABRE SESIÓN (D8 en el bootstrap) ──"
# El arnés viejo mandaba `X-Llminbox-Token` a /sessions y a /events, y el stub no validaba
# auth: el fallo quedaba TAPADO por los dos lados a la vez.
arranca conforme || exit 3
CODIGO=$(curl -s -o /dev/null -w '%{http_code}' -m 5 -X POST \
         -H "X-Llminbox-Token: compartido" "http://127.0.0.1:$PUERTO/native/v1/sessions")
if [ "$CODIGO" = "401" ]; then bien "el stub rechaza el legado en /sessions ($CODIGO)"
else mal "legado rechazado en /sessions" "401" "$CODIGO"; fi
CUERPO_401=$(curl -s -m 5 -X POST -H "X-Llminbox-Token: compartido" \
             "http://127.0.0.1:$PUERTO/native/v1/sessions")
case "$CUERPO_401" in *RUNTIME_CREDENTIAL_REQUIRED*) bien "y nombra RUNTIME_CREDENTIAL_REQUIRED" ;;
                      *) mal "código del 401" "RUNTIME_CREDENTIAL_REQUIRED" "$CUERPO_401" ;; esac
# ⊕ el control: con la credencial de WORKLOAD por Bearer, SÍ abre
CODIGO_OK=$(curl -s -o /dev/null -w '%{http_code}' -m 5 -X POST \
            -H "Authorization: Bearer wl-credencial" "http://127.0.0.1:$PUERTO/native/v1/sessions")
if [ "$CODIGO_OK" = "201" ]; then bien "⊕ y con Bearer de workload SÍ abre ($CODIGO_OK)"
else mal "⊕ Bearer de workload abre" "201" "$CODIGO_OK"; fi
# ⊖ y el token de SESIÓN tampoco vale para abrir otra: son secretos de ámbitos distintos
CODIGO_SES=$(curl -s -o /dev/null -w '%{http_code}' -m 5 -X POST \
             -H "Authorization: Bearer ses-t1" "http://127.0.0.1:$PUERTO/native/v1/sessions")
if [ "$CODIGO_SES" = "401" ]; then bien "⊖ y un token de SESIÓN no abre sesión ($CODIGO_SES)"
else mal "⊖ token de sesión no abre" "401" "$CODIGO_SES"; fi

echo "── ⑧ D8 canónico: /sessions y /sessions/refresh son puertas DISTINTAS ──"
arranca conforme || exit 3
C_WL_REF=$(curl -s -o /dev/null -w '%{http_code}' -m 5 -X POST \
           -H "Authorization: Bearer wl-credencial" "http://127.0.0.1:$PUERTO/native/v1/sessions/refresh")
if [ "$C_WL_REF" = "401" ]; then bien "⊖ workload en /sessions/refresh ⇒ 401 (rota UNA sesión concreta)"
else mal "workload no vale en refresh" "401" "$C_WL_REF"; fi
SES_BODY=$(curl -s -m 5 -X POST -H "Authorization: Bearer wl-credencial" \
           "http://127.0.0.1:$PUERTO/native/v1/sessions")
SES_TOKEN=$(printf '%s' "$SES_BODY" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("token", ""))' 2>/dev/null)
C_SES_REF=$(curl -s -o /dev/null -w '%{http_code}' -m 5 -X POST \
            -H "Authorization: Bearer $SES_TOKEN" "http://127.0.0.1:$PUERTO/native/v1/sessions/refresh")
if [ "$C_SES_REF" = "200" ]; then bien "⊕ y con token de SESIÓN sí rota ($C_SES_REF)"
else mal "sesión rota en refresh" "200" "$C_SES_REF"; fi
# ⊖⊖ y las puertas no se han vuelto la misma: workload SÍ abre en /sessions
C_WL_NEW=$(curl -s -o /dev/null -w '%{http_code}' -m 5 -X POST \
           -H "Authorization: Bearer wl-credencial" "http://127.0.0.1:$PUERTO/native/v1/sessions")
if [ "$C_WL_NEW" = "201" ]; then bien "⊖⊖ y workload sigue abriendo en /sessions ($C_WL_NEW)"
else mal "workload abre en /sessions" "201" "$C_WL_NEW"; fi
LEASE_BODY=$(curl -s -m 5 -X POST -H "Authorization: Bearer wl-credencial" \
             "http://127.0.0.1:$PUERTO/native/v1/sessions")
LEASE_TOKEN=$(printf '%s' "$LEASE_BODY" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("token", ""))' 2>/dev/null)
C_LEASE_DEL=$(curl -s -o /dev/null -w '%{http_code}' -m 5 -X DELETE \
              -H "Authorization: Bearer $LEASE_TOKEN" "http://127.0.0.1:$PUERTO/native/v1/leases/recurso-e2e")
if [ "$C_LEASE_DEL" = "204" ]; then bien "⊕ liberar lease está modelado por el stub ($C_LEASE_DEL)"
else mal "liberar lease en stub conforme" "204" "$C_LEASE_DEL"; fi

echo "── ⑥ 🔑 403 EN LOS DOS ESQUEMAS **NO** PUEDE SALIR VERDE ──"
# El caso exacto del auditor: /whoami devolvia 403 a los dos y el arnes imprimia verde con
# incompatibles=0, porque la rama por defecto era OK.
arranca auth403 || exit 3
corre_e2e 0
case "$SAL" in *"NO ACREDITADO"*) bien "403/403 sin MUTA ⇒ NO ACREDITADO, no verde" ;;
               *) mal "403/403 ⇒ NO ACREDITADO" "«NO ACREDITADO»" "$(printf '%s' "$SAL" | grep -i 'Authorization' | head -1)" ;; esac
case "$SAL" in *"✅ VERDE     esquema de Authorization"*) mal "403/403 no puede dar VERDE" "sin verde en auth" "salio VERDE" ;;
               *) bien "⊖ y NO hay VERDE en la fila de auth" ;; esac
# ⊕ el control que impide «marca no-acreditado a todo»: con el stub conforme y MUTA=1,
# la evidencia positiva SI existe y la fila tiene que ponerse verde.
arranca conforme || exit 3
corre_e2e 1
case "$SAL" in *"D8 acreditado"*) bien "⊕ con token VÁLIDO y Bearer ⇒ acreditado (discrimina)" ;;
               *) mal "⊕ acredita con token valido" "«D8 acreditado»" "$(printf '%s' "$SAL" | grep -i 'Authorization' | head -1)" ;; esac

echo "── ⑨ CONTROL NEGATIVO DE LA CREDENCIAL DE WORKLOAD (P2 del auditor) ──"
# 🩸 El stub aceptaba CUALQUIER Bearer que no empezara por `ses-`, asi que el bootstrap
# daba 201 con una credencial de workload EQUIVOCADA: el falsador medía que el CLI manda
# *un* Bearer, no que mande *el correcto*. Sin el ⊖ de aqui abajo, «el bootstrap funciona»
# se cumplía con cualquier secreto, incluido ninguno reconocible.
arranca conforme || exit 3
CASA9="$BASE/casa9"; mkdir -p "$CASA9"
login9() {   # $1 = credencial de workload a usar -> deja SAL9 y RC9
  printf '%s\n' "$1" > "$CASA9/workload"
  rm -f "$CASA9/sesion.json"
  SAL9="$( HOME="$CASA9" LLMINBOX_API="http://127.0.0.1:$PUERTO" \
           LLMINBOX_WORKLOAD_FILE="$CASA9/workload" \
           LLMINBOX_SESSION_FILE="$CASA9/sesion.json" \
           "$REPO/llmi" login 2>&1 )"; RC9=$?
}
login9 "wl-credencial"
if [ "$RC9" = "0" ]; then bien "⊕ con la credencial de workload CORRECTA ⇒ login abre sesión"
else mal "⊕ login con credencial buena" "rc=0" "rc=$RC9 · $(printf '%s' "$SAL9" | head -1)"; fi

login9 "wl-credencial-EQUIVOCADA"
if [ "$RC9" = "0" ]; then mal "⊖ credencial de workload INCORRECTA ⇒ NO puede abrir sesión" "rc≠0" "rc=0 (el stub la aceptó: el falsador no discrimina)"
else bien "⊖ credencial de workload INCORRECTA ⇒ rc=$RC9, no abre sesión"; fi
case "$SAL9" in *RUNTIME_CREDENTIAL_REQUIRED*|*401*) bien "⊖ y el motivo es de CREDENCIAL, no otro error" ;;
                *) mal "⊖ motivo de credencial" "RUNTIME_CREDENTIAL_REQUIRED o 401" "$(printf '%s' "$SAL9" | head -2 | tr '\n' '|')" ;; esac
if [ -s "$CASA9/sesion.json" ]; then mal "⊖ y NO deja fichero de sesión" "sin sesion.json" "quedó escrito"
else bien "⊖ y NO deja fichero de sesión escrito"; fi

echo
if [ "$MALOS" = "0" ]; then echo "✅ $TOTAL comprobaciones, 0 fallos"; exit 0; fi
echo "❌ $MALOS de $TOTAL FALLAN"; exit 1
