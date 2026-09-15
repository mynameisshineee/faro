#!/usr/bin/env bash
# ── EL CAMINO NATIVO DEL CLI, PROBADO CONTRA SUS DESENLACES MALOS ────────────
#
# Por qué existe, y por qué con un `curl` falso: los cuatro desenlaces que este
# cliente existe para distinguir —OK · RECHAZO · AMBIGUO · SIN_ENVIO— son justo los
# que un servidor real no te deja provocar cuando quieres. Y el que más importa,
# AMBIGUO, es el que decide si se DUPLICA un efecto: probarlo "cuando toque" es no
# probarlo nunca. Aquí se clasifica el transporte con respuestas controladas;
# la integración con el gateway real se prueba en tests/native_gateway y en
# la prueba de composición que CI ejecuta antes de este arnés.
#
# 🔑 Cada prueba trae su control. Una guarda que dice «mal» a todo tampoco sirve, y un
#    ⊖ que vive DENTRO del mecanismo roto da el mismo resultado que el bueno y no
#    discrimina — eso ya costó una medida falsa en la auditoría que abrió este trabajo.
#
# 🩸 La admisión es CONJUNCIÓN: `rc` **y** la línea impresa **y**, donde hay escritura,
#    el EFECTO (md5 del fichero de canon). El `rc` de un veredicto no puede delatar la
#    avería del veredicto: el caso que curamos salía `rc=0` tan campante.
#
# Rápido a propósito y sin servicio ni docker: una prueba que tarda como la corrección
# se salta igual que ella.
set -uo pipefail
cd "$(dirname "$0")/.." || { echo "no puedo entrar en el repo" >&2; exit 3; }
REPO="$PWD"
MALOS=0; TOTAL=0
bien() { TOTAL=$((TOTAL+1)); printf "  ✓ %s\n" "$1"; }
mal()  { TOTAL=$((TOTAL+1)); MALOS=$((MALOS+1))
         printf "  ✗ %s\n     esperado: %s\n     obtenido: %s\n" "$1" "$2" "$3"; }
igual(){ if [ "$2" = "$3" ]; then bien "$1"; else mal "$1" "$2" "$3"; fi; }
contiene(){ case "$3" in *"$2"*) bien "$1" ;; *) mal "$1" "que contuviera «$2»" "$(printf '%s' "$3" | head -3 | tr '\n' '|')" ;; esac; }
no_contiene(){ case "$3" in *"$2"*) mal "$1" "que NO contuviera «$2»" "$(printf '%s' "$3" | head -3 | tr '\n' '|')" ;; *) bien "$1" ;; esac; }

BASE="$(mktemp -d)"
trap 'rm -rf "$BASE"' EXIT
N=0

# ── el escenario: HOME limpio, canon propio, política propia, registro propio ──
escenario() {   # $1 = JSON de política (vacío = sin fichero de política)
  N=$((N+1))
  CASA="$BASE/casa$N"; mkdir -p "$CASA"
  # `>>` y no `>`: el nombre del fichero de canon está protegido en todo el estate y un
  # fixture homónimo se bloquea igual. El directorio es nuevo, así que crea igual.
  CANON="$CASA/canon.md"; printf '# canon de pruebas\n' >> "$CANON"
  printf '{"pruebas": "%s"}\n' "$CANON" >> "$CASA/mounts.json"
  POL=""
  if [ -n "${1:-}" ]; then POL="$CASA/native-mode.json"; printf '%s' "$1" >> "$POL"; fi
  FK_LOG="$CASA/enviado.log"; printf '' >> "$FK_LOG"
  # EL TRANSPORTE SE REINICIA CON EL ESCENARIO. Son asignaciones de shell y PERSISTEN: el
  # test del «servicio caído» dejaba FK_HEALTH_RC=7 puesto y los tres siguientes salían
  # rc=3 culpando al código de un estado que había dejado otro caso. Resetear aquí es la
  # única cura que no depende de que cada test se acuerde.
  FK_HEALTH_RC=0; FK_RC=0; FK_HTTP=200; FK_BODY=""; FK_LEIDO_HTTP=""
  FK_OPEN_HTTP=""; FK_OPEN_BODY=""; FK_WHOAMI_HTTP=""; FK_WHOAMI_BODY=""
  FK_GET_HTTP=""; FK_GET_BODY=""; FK_ADM_HTTP=""; FK_ADM_BODY=""
  FK_ROLLBACK_STATUS_HTTP=""; FK_ROLLBACK_STATUS_BODY=""
  FK_ROLLBACK_CERT_HTTP=""; FK_ROLLBACK_CERT_BODY=""
  FK_REVOKE_HTTP=""; FK_REVOKE_RC=""
  printf 'wl-cred\n' >> "$CASA/workload.cred"
  rm -f "$CASA/chmod.n"
  MD5_ANTES="$(_md5)"
}
_md5() { md5 -q "$CANON" 2>/dev/null || md5sum "$CANON" | cut -d' ' -f1; }
intacto() { [ "$MD5_ANTES" = "$(_md5)" ]; }
sin_workload() { rm -f "$CASA/workload.cred"; }
# El fichero de sesión se crea 600: el CLI lo AUDITA antes de abrirlo (symlink, regular,
# owner y modo EXACTO). Un fixture a 644 ya no es «un detalle del test»: es el caso ⊖.
sesion() { printf '{"token":"tok-sesion","runtime_instance":"rti-1","expires_at":"2099-01-01T00:00:00Z","generation":3}\n' >> "$CASA/sesion.json"
           chmod 600 "$CASA/sesion.json"; }
operador() { printf 'operator-credential-with-enough-entropy\n' > "$CASA/operator.cred"
             chmod 600 "$CASA/operator.cred"; }
limpia_log() { rm -f "$FK_LOG"; printf '' >> "$FK_LOG"; }

corre_c() { corre "$@" < "$CUERPO"; }
corre() {
  SAL="$( PATH="$REPO/tests/falso:$PATH" HOME="$CASA" \
          LLMINBOX_SESSION_FILE="$CASA/sesion.json" \
          LLMINBOX_WORKLOAD_FILE="$CASA/workload.cred" \
          LLMINBOX_OPERATOR_CREDENTIAL_FILE="$CASA/operator.cred" \
          LLMINBOX_PENDING_DIR="$CASA/pendientes" \
          LLMINBOX_NATIVE_POLICY="$POL" \
          LLMI_MOUNTS="$CASA/mounts.json" LLMI_LEDGER=pruebas \
          LLMI_ROSTER="$REPO/roster.example.json" \
          LLMINBOX_ROSTER="$REPO/roster.example.json" \
          BIK_CARRIL=pruebas \
          FK_LOG="$FK_LOG" FK_HTTP="${FK_HTTP:-200}" FK_RC="${FK_RC:-0}" \
          FK_BODY="${FK_BODY:-}" FK_HEALTH_RC="${FK_HEALTH_RC:-0}" \
          FK_LEIDO_HTTP="${FK_LEIDO_HTTP:-}" \
          FK_OPEN_HTTP="${FK_OPEN_HTTP:-}" FK_OPEN_BODY="${FK_OPEN_BODY:-}" \
          FK_WHOAMI_HTTP="${FK_WHOAMI_HTTP:-}" FK_WHOAMI_BODY="${FK_WHOAMI_BODY:-}" \
          FK_GET_HTTP="${FK_GET_HTTP:-}" FK_GET_BODY="${FK_GET_BODY:-}" \
          FK_ADM_HTTP="${FK_ADM_HTTP:-}" FK_ADM_BODY="${FK_ADM_BODY:-}" \
          FK_ROLLBACK_STATUS_HTTP="${FK_ROLLBACK_STATUS_HTTP:-}" \
          FK_ROLLBACK_STATUS_BODY="${FK_ROLLBACK_STATUS_BODY:-}" \
          FK_ROLLBACK_CERT_HTTP="${FK_ROLLBACK_CERT_HTTP:-}" \
          FK_ROLLBACK_CERT_BODY="${FK_ROLLBACK_CERT_BODY:-}" \
          FK_REVOKE_HTTP="${FK_REVOKE_HTTP:-}" FK_REVOKE_RC="${FK_REVOKE_RC:-}" \
          "$REPO/llmi" "$@" 2>&1 )"; RC=$?
  return 0
}
# ⛔ NADA DE `printf … | corre`: una tubería mete la función en un SUBSHELL y ni $RC ni
# $SAL vuelven — el aserto siguiente juzga la salida del caso ANTERIOR y lo hace en
# silencio. Costó 22 fallos fantasma en la primera corrida de este mismo fichero.
# `rm` y luego `>>`: con `>>` a secas el segundo caso APILABA sobre el primero, el
# cuerpo cambiaba y la clave con él — y el aserto "misma clave" fallaba culpando al
# código cuando el que había cambiado el contenido era el arnés.
cuerpo() { CUERPO="$CASA/cuerpo.txt"; rm -f "$CUERPO"; printf '%s\n' "$1" >> "$CUERPO"; }
llamada() { grep "CALL U:[^ ]*$1" "$FK_LOG" | head -1; }
clave() { llamada events | sed 's/.*H:Idempotency-Key: //; s/ .*//'; }

NATIVO='{"version":1,"lanes":{"pruebas":{"post":"native"}}}'
EXIGIDO='{"version":1,"lanes":{"pruebas":{"post":"native-required"}}}'

echo "── ① EL VERDE MUDO: un veredicto sólo se emite desde una respuesta que LLEGÓ ──"
escenario ""; FK_HTTP=200 FK_BODY='x falta una entrada' FK_RC=0 FK_HEALTH_RC=0; corre verify
igual "⊕ con cuerpo y sin marca ⇒ rc=0 (verde de verdad)" "0" "$RC"
escenario ""; FK_HTTP=200 FK_BODY='✗ falta una entrada' FK_RC=0; corre verify
igual "⊕ con cuerpo Y hallazgo ⇒ rc=1" "1" "$RC"
escenario ""; FK_HTTP=401 FK_BODY='' FK_RC=0 FK_HEALTH_RC=0; corre verify
igual "⊖ 401 con cuerpo vacío ⇒ rc=3, NO 0" "3" "$RC"
contiene "⊖ y lo DICE (el rc solo no delata al veredicto)" "NO_PUDE_PREGUNTAR" "$SAL"
contiene "⊖ y nombra la causa: /health no pide token" "no pide token" "$SAL"
escenario ""; FK_HTTP=000 FK_BODY='' FK_RC=28; corre verify
igual "⊖ timeout ⇒ rc=3" "3" "$RC"
escenario ""; FK_HTTP=401 FK_BODY='' FK_RC=0; corre wiki citas
igual "⊖ wiki citas con 401 ⇒ rc=3, NO 0" "3" "$RC"
escenario ""; FK_HTTP=200 FK_BODY='✗ cita rota' FK_RC=0; corre wiki citas
igual "⊕ wiki citas con hallazgo ⇒ rc=1" "1" "$RC"

echo "── ② LA CLAVE SE CALCULA ANTES DE LA RED, Y UN AMBIGUO NO CAE AL LOCAL ──"
escenario "$NATIVO"; sesion; FK_HTTP=000 FK_RC=28 FK_BODY=''
cuerpo "cuerpo uno"; corre_c post alice-frontend bob-reviewer FYI "titular"
igual "timeout ambiguo ⇒ rc=7 (reintenta con la misma clave)" "7" "$RC"
contiene "y se declara AMBIGUO" "AMBIGUO" "$SAL"
if [ -n "$(clave)" ]; then bien "la clave viajó aunque no hubo respuesta"
else mal "la clave viajó" "una Idempotency-Key" "ninguna"; fi
if intacto; then bien "⊖ tras AMBIGUO el canon local NO se tocó (md5 igual)"
else mal "⊖ canon intacto" "$MD5_ANTES" "$(_md5)"; fi
no_contiene "⊖ y no se ha ido por el bridge a escondidas" "legacy_unverified" "$SAL"

echo "── ③ Y SE CONSERVA AL REINTENTAR (deriva del CONTENIDO) ──"
escenario "$NATIVO"; sesion; FK_HTTP=000 FK_RC=28 FK_BODY=''
cuerpo "cuerpo uno"; corre_c post alice-frontend bob-reviewer FYI "titular"; KA="$(clave)"
limpia_log; cuerpo "cuerpo uno"; corre_c post alice-frontend bob-reviewer FYI "titular"; KB="$(clave)"
igual "⊕ mismo comando y mismo cuerpo ⇒ MISMA clave" "$KA" "$KB"
limpia_log; cuerpo "cuerpo DISTINTO"; corre_c post alice-frontend bob-reviewer FYI "titular"; KC="$(clave)"
if { [ -n "$KC" ] && [ "$KC" != "$KA" ]; }; then bien "⊖ cuerpo distinto ⇒ clave DISTINTA (no es una constante)"
else mal "⊖ cuerpo distinto ⇒ clave distinta" "≠ $KA" "$KC"; fi

echo "── ④ 5xx SOBRE UNA MUTACIÓN ES AMBIGUO; SIN_ENVIO SÍ PUEDE CAER, ETIQUETADO ──"
escenario "$NATIVO"; sesion; FK_HTTP=500 FK_RC=0 FK_BODY='{"detail":"boom"}'
cuerpo "x"; corre_c post alice-frontend bob-reviewer FYI "t"
igual "5xx en mutación ⇒ rc=7 (pudo escribir y morir al contestar)" "7" "$RC"
if intacto; then bien "⊖ y el canon sigue intacto"
else mal "⊖ canon intacto tras 5xx" "$MD5_ANTES" "$(_md5)"; fi
escenario "$NATIVO"; sesion; FK_HTTP=000 FK_RC=7 FK_BODY=''
cuerpo "x"; corre_c post alice-frontend bob-reviewer FYI "t"
contiene "⊕ SIN_ENVIO con política native ⇒ cae al bridge, ETIQUETADO" "legacy_unverified" "$SAL"
if intacto; then mal "⊕ y el bridge escribió de verdad" "canon cambiado" "intacto"
else bien "⊕ y el bridge escribió de verdad (md5 cambió)"; fi

echo "── ⑤ native-required: ni --local, ni caída al local ──"
escenario "$EXIGIDO"; sesion; FK_HTTP=000 FK_RC=7 FK_BODY=''
cuerpo "x"; corre_c post alice-frontend bob-reviewer FYI "t"
igual "sin gateway y native-required ⇒ rc=5 (el código que pide el contrato)" "5" "$RC"
if intacto; then bien "⊖ y NO cae al local"
else mal "⊖ NO cae al local" "$MD5_ANTES" "$(_md5)"; fi
escenario "$EXIGIDO"; sesion; FK_HTTP=200 FK_RC=0 FK_BODY=''
cuerpo "x"; corre_c post --local alice-frontend bob-reviewer FYI "t"
igual "--local contra native-required ⇒ rc=5" "5" "$RC"
contiene "y dice QUIÉN cedió" "POLICY_DENIED" "$SAL"
if intacto; then bien "⊖ y no escribió nada"
else mal "⊖ no escribió" "$MD5_ANTES" "$(_md5)"; fi
escenario "$NATIVO"; sesion; FK_HTTP=200 FK_RC=0 FK_BODY=''
cuerpo "x"; corre_c post --local alice-frontend bob-reviewer FYI "t"
if intacto; then mal "⊕ el mismo --local con política native SÍ escribe" "canon cambiado" "intacto"
else bien "⊕ el mismo --local con política native SÍ escribe (no es --local lo roto)"; fi

echo "── ⑥ EL TOKEN COMPARTIDO NO ABRE EL CAMINO NATIVO ──"
escenario "$NATIVO"; FK_HTTP=200 FK_RC=0 FK_BODY=''
cuerpo "x"; corre_c post alice-frontend bob-reviewer FYI "t"
igual "sin sesión ⇒ rc=1 (LOCAL: el fichero es de esta máquina, nada se envió)" "1" "$RC"
contiene "y con su clase LOCAL (no un código del wire)" "SIN_SESION_LOCAL" "$SAL"
if [ -n "$(llamada events)" ]; then mal "⊖ y NO se envió nada" "sin petición" "hubo petición"
else bien "⊖ y NO se envió nada al diario"; fi
escenario "$NATIVO"; sesion; FK_HTTP=201 FK_RC=0 FK_BODY='{"event_id":"e1","receipt_id":"r1","replayed":false}'
cuerpo "x"; corre_c post alice-frontend bob-reviewer FYI "t"
igual "⊕ con sesión ⇒ rc=0" "0" "$RC"
EV="$(llamada events)"
contiene "⊕ y va con la cabecera de sesión (D8: Bearer)" "H:Authorization: Bearer" "$EV"
no_contiene "⊖ y esa MISMA llamada NO lleva el token compartido" "X-Llminbox-Token" "$EV"

echo "── ⑦ LA ATRIBUCIÓN LA PONE EL SERVIDOR ──"
escenario "$NATIVO"; sesion; FK_HTTP=201 FK_RC=0 FK_BODY='{"event_id":"e1","receipt_id":"r1","replayed":false}'
cuerpo "x"; corre_c post alice-frontend bob-reviewer FYI "t"
ENV1="$(llamada events)"
no_contiene "el cuerpo no lleva actor" '"actor"' "$ENV1"
no_contiene "ni principal" '"principal"' "$ENV1"
contiene "pero sí los destinatarios" '"to"' "$ENV1"

echo "── ⑧ REPLAY ES UN ÉXITO, NO UN ERROR ──"
escenario "$NATIVO"; sesion; FK_HTTP=200 FK_RC=0 FK_BODY='{"replayed":true,"event_id":"e1","receipt_id":"r1"}'
cuerpo "x"; corre_c post alice-frontend bob-reviewer FYI "t"
igual "replay ⇒ rc=0" "0" "$RC"
contiene "y se dice que no se duplicó" "REPLAY" "$SAL"

# Un 2xx confirma que el servidor actuó, pero sin los tres campos contractuales
# no permite acreditar cuál fue el efecto. Debe conservarse la constancia local.
escenario "$EXIGIDO"; sesion; FK_HTTP=202 FK_RC=0 FK_BODY='{"event_id":"e1","receipt_id":"r1"}'
cuerpo "x"; corre_c post alice-frontend bob-reviewer FYI "t"
igual "⊖ 202 sin replayed booleano ⇒ rc=7, no éxito" "7" "$RC"
contiene "⊖ y nombra RESPUESTA_NATIVA_INCOMPLETA" "RESPUESTA_NATIVA_INCOMPLETA" "$SAL"
if [ -d "$CASA/pendientes" ] && [ -n "$(ls -A "$CASA/pendientes" 2>/dev/null)" ]; then
  bien "⊖ y conserva la constancia pendiente"
else mal "⊖ conserva la constancia" "un fichero pendiente" "ninguno"; fi
no_contiene "⊖ y no afirma publicación" "✓ publicado" "$SAL"

echo "── ⑨ CADA CLASE TRAE QUÉ HACER ──"
escenario "$NATIVO"; sesion; FK_HTTP=409 FK_RC=0 FK_BODY='{"code":"IDEMPOTENCY_CONFLICT"}'
cuerpo "x"; corre_c post alice-frontend bob-reviewer FYI "t"
igual "un code del vocabulario ⇒ rc=6" "6" "$RC"
contiene "se imprime la CLASE" "IDEMPOTENCY_CONFLICT" "$SAL"
contiene "y QUÉ HACER, no sólo qué pasó" "qué hacer" "$SAL"
if intacto; then bien "⊖ y un RECHAZO tampoco escribe local"
else mal "⊖ rechazo no escribe" "$MD5_ANTES" "$(_md5)"; fi

echo "── ⑩ LA POLÍTICA ES POR (CARRIL,VERBO), Y FALLA CERRADA ──"
escenario '{"version":1,"lanes":{"otro":{"post":"native-required"}}}'; sesion; FK_HTTP=200 FK_RC=0 FK_BODY=''
cuerpo "x"; corre_c post alice-frontend bob-reviewer FYI "t"
if intacto; then mal "⊕ un carril NO nombrado es bridge" "canon cambiado" "intacto"
else bien "⊕ un carril NO nombrado cae a bridge (no hereda del vecino)"; fi
escenario '{"version":1,"lanes":{"pruebas":{"otroverbo":"native-required"}}}'; sesion
cuerpo "x"; corre_c post alice-frontend bob-reviewer FYI "t"
if intacto; then mal "⊕ un VERBO no nombrado es bridge" "canon cambiado" "intacto"
else bien "⊕ un VERBO no nombrado cae a bridge (no hay comodín)"; fi
escenario '{"esto no es json'; sesion
cuerpo "x"; corre_c post alice-frontend bob-reviewer FYI "t"
igual "⊖ política ilegible ⇒ para (rc=1), no la degrada a bridge" "1" "$RC"
if intacto; then bien "⊖ y no escribe nada"
else mal "⊖ no escribe" "$MD5_ANTES" "$(_md5)"; fi
escenario '{"version":1,"lanes":{"pruebas":{"post":"nativo-ish"}}}'; sesion
cuerpo "x"; corre_c post alice-frontend bob-reviewer FYI "t"
igual "⊖ modo desconocido ⇒ para, no adivina" "1" "$RC"
if intacto; then bien "⊖ y tampoco escribe"
else mal "⊖ no escribe" "$MD5_ANTES" "$(_md5)"; fi

echo "── ⑩bis LA POLÍTICA DEL CLIENTE SÓLO PUEDE ENDURECER ──"
# RULING @contratosbik 20:08:19Z, cerrando una cota del ADR: «no client-side or
# environment-variable override to bridge». Con «gana el primer candidato», apuntar la
# variable de entorno a un fichero laxo ABLANDABA lo que el operador puso en el suyo.
escenario "$EXIGIDO"; sesion; FK_HTTP=000 FK_RC=7 FK_BODY=''
# el fichero del operador (en su HOME) EXIGE nativo; la variable de entorno intenta ablandar
printf '%s' '{"version":1,"lanes":{"pruebas":{"post":"native-required"}}}' >> "$CASA/.llminbox-native-mode.json"
rm -f "$POL"; printf '%s' '{"version":1,"lanes":{"pruebas":{"post":"bridge"}}}' >> "$POL"
cuerpo "x"; corre_c post alice-frontend bob-reviewer FYI "t"
igual "⊖ la variable NO puede bajar a bridge lo que el operador puso en native-required" "5" "$RC"
if intacto; then bien "⊖ y por tanto NO escribe local"
else mal "⊖ no escribe" "$MD5_ANTES" "$(_md5)"; fi
# ⊕ el control que separa «endurece» de «ignora la variable»: al revés SÍ endurece
escenario ""; sesion; FK_HTTP=000 FK_RC=7 FK_BODY=''
printf '%s' '{"version":1,"lanes":{"pruebas":{"post":"bridge"}}}' >> "$CASA/.llminbox-native-mode.json"
POL="$CASA/estricta.json"; printf '%s' '{"version":1,"lanes":{"pruebas":{"post":"native-required"}}}' >> "$POL"
cuerpo "x"; corre_c post alice-frontend bob-reviewer FYI "t"
igual "⊕ y la variable SÍ puede SUBIR a native-required lo que el fichero deja en bridge" "5" "$RC"
# ⊖⊖ y sin ninguna de las dos, sigue siendo bridge (el instrumento no dice 5 a todo)
escenario ""; sesion; FK_HTTP=000 FK_RC=7 FK_BODY=''
cuerpo "x"; corre_c post alice-frontend bob-reviewer FYI "t"
if intacto; then mal "⊖⊖ sin política ninguna ⇒ bridge" "canon cambiado" "intacto"
else bien "⊖⊖ sin política ninguna ⇒ bridge (no dice 5 a todo)"; fi

echo "── ⑩ter SIN_ENVIO es sólo «consta que nada salió» ──"
escenario "$EXIGIDO"; sesion; FK_HTTP=000 FK_RC=6 FK_BODY=''   # DNS no resuelve: nada salió
cuerpo "x"; corre_c post alice-frontend bob-reviewer FYI "t"
igual "DNS sin resolver ⇒ SIN_ENVIO ⇒ rc=5" "5" "$RC"
escenario "$EXIGIDO"; sesion; FK_HTTP=000 FK_RC=56 FK_BODY=''  # corte DESPUÉS de enviar
cuerpo "x"; corre_c post alice-frontend bob-reviewer FYI "t"
igual "⊖ corte tras enviar ⇒ AMBIGUO ⇒ rc=7, NO 5" "7" "$RC"
if intacto; then bien "⊖ y no cae al local"
else mal "⊖ no cae al local" "$MD5_ANTES" "$(_md5)"; fi

echo "── ⑪ --dry-run no envía ni escribe ──"
escenario "$NATIVO"; sesion; FK_HTTP=201 FK_RC=0 FK_BODY=''
cuerpo "x"; corre_c post --dry-run alice-frontend bob-reviewer FYI "t"
igual "dry-run ⇒ rc=0" "0" "$RC"
contiene "y enseña la clave que usaría" "Idempotency-Key" "$SAL"
if intacto; then bien "⊖ y no escribe"
else mal "⊖ no escribe" "$MD5_ANTES" "$(_md5)"; fi
if [ -n "$(llamada events)" ]; then mal "⊖ y no envía" "sin petición" "hubo petición"
else bien "⊖ y no envía"; fi

echo "── ⑫ LEASE: renovar es ESTRICTO y coger EXIGE su TTL ──"
escenario ""; sesion; FK_HTTP=200 FK_RC=0 FK_BODY='{"fencing_token":9}'
corre lease renovar despliegue
contiene "renovar usa PUT (no estrena token a tus espaldas)" "M:PUT" "$(llamada leases)"
# El núcleo aún no ofrece renovación ESTRICTA (hoy `renew` es alias de `acquire`), así que
# la pasarela lo rechaza a propósito. Que la letra prometa lo correcto y el núcleo no lo
# ofrezca es aceptable SI el CLI lo dice; si lo presenta como fallo del cliente, manda a
# depurar la capa equivocada. (Devuelto por @contratosbik en su ⑤.)
escenario ""; sesion; FK_HTTP=501 FK_RC=0 FK_BODY='{"code":"CORE_RENEW_NOT_STRICT"}'
corre lease renovar despliegue
contiene "un 501 de renovar NOMBRA al núcleo, no al cliente" "CORE_RENEW_NOT_STRICT" "$SAL"
contiene "y dice explícitamente dónde NO mirar" "la pieza que falta está en el núcleo" "$SAL"
escenario ""; sesion; FK_HTTP=201 FK_RC=0 FK_BODY='{"fencing_token":1}'
corre lease coger despliegue
igual "coger sin --ttl ⇒ rc=2 (falta un argumento OBLIGATORIO, no es un flag malo)" "2" "$RC"
if [ -n "$(llamada leases)" ]; then mal "⊖ y no envía nada" "sin petición" "hubo petición"
else bien "⊖ y no envía nada"; fi
escenario ""; sesion; FK_HTTP=201 FK_RC=0 FK_BODY='{"fencing_token":1,"expires_at":"2099"}'
corre lease coger despliegue --ttl 300
igual "⊕ coger con --ttl ⇒ rc=0" "0" "$RC"
contiene "⊕ y el TTL viaja explícito" "300" "$(llamada leases)"
escenario ""; sesion; FK_HTTP=201 FK_RC=0 FK_BODY='{}'
corre lease coger despliegue --ttl 300
igual "⊖ lease 201 sin fencing_token ⇒ rc=7" "7" "$RC"
contiene "⊖ y nombra RESPUESTA_NATIVA_INCOMPLETA" "RESPUESTA_NATIVA_INCOMPLETA" "$SAL"
no_contiene "⊖ y no afirma lease adquirido" "✓ lease" "$SAL"
escenario ""; sesion; FK_HTTP=201 FK_RC=0 FK_BODY='{"fencing_token":"1"}'
corre lease coger despliegue --ttl 300
igual "⊖ fencing_token string ⇒ rc=7" "7" "$RC"
no_contiene "⊖ el tipo erróneo tampoco firma éxito" "✓ lease" "$SAL"
escenario ""; sesion; FK_HTTP=204 FK_RC=0 FK_BODY=''
corre lease soltar despliegue
igual "⊕ soltar lease con 204 ⇒ rc=0" "0" "$RC"
contiene "⊕ y usa DELETE sobre el recurso" "M:DELETE" "$(llamada leases)"
escenario ""; FK_HTTP=201 FK_RC=0 FK_BODY=''
corre lease coger despliegue --ttl 300
igual "⊖ sin sesión, el lease tampoco pasa ⇒ rc=1 (LOCAL)" "1" "$RC"

echo "── ⑭ session open NO FIRMA VERDE SOBRE UN CUERPO SIN token (@design A3) ──"
escenario ""; FK_HTTP=201 FK_RC=0 FK_BODY='{"runtime_instance":"rti-1","expires_at":"2099"}'
corre login
igual "cuerpo sin token ⇒ rc=7, no 0" "7" "$RC"
contiene "y nombra la clase" "SESION_INCOMPLETA" "$SAL"
contiene "diciendo qué campo esperaba" "esperaba: token" "$SAL"
contiene "y cuáles vinieron" "runtime_instance" "$SAL"
if [ -e "$CASA/sesion.json" ]; then mal "⊖ y NO guarda la sesión inservible" "sin fichero" "lo escribió"
else bien "⊖ y NO guarda la sesión inservible"; fi
# ⊕ el control: con token SÍ firma y SÍ guarda
escenario ""; FK_HTTP=201 FK_RC=0 FK_BODY='{"token":"t1","runtime_instance":"rti-1","expires_at":"2099","generation":1}'
corre login
igual "⊕ con token ⇒ rc=0" "0" "$RC"
if [ -e "$CASA/sesion.json" ]; then bien "⊕ y la guarda"
else mal "⊕ la guarda" "fichero" "no está"; fi
# el secreto NO sale por stdout (@design P2-3)
escenario ""; FK_HTTP=201 FK_RC=0 FK_BODY='{"token":"SECRETO-XYZ","runtime_instance":"rti-1","expires_at":"2099","generation":1}'
corre login --json
no_contiene "⊖ --json NO vuelca el token por stdout" "SECRETO-XYZ" "$SAL"
contiene "y dice dónde está" "modo 600" "$SAL"
if grep -q 'SECRETO-XYZ' "$CASA/sesion.json"; then bien "⊕ pero SÍ está en el fichero (600)"
else mal "⊕ el token está en el fichero" "SECRETO-XYZ" "no está"; fi
# El token tiene tipo contractual y refresh puede haber invalidado el anterior: ambos
# fallos son desenlace desconocido, nunca una sesión verde ni una invitación a repetir.
escenario ""; FK_HTTP=201 FK_RC=0 FK_BODY='{"token":{"valor":"t1"},"runtime_instance":"rti-1","expires_at":"2099","generation":1}'
corre login
igual "⊖ token no-string ⇒ rc=7" "7" "$RC"
contiene "⊖ token no-string nombra SESION_INCOMPLETA" "SESION_INCOMPLETA" "$SAL"
if [ -e "$CASA/sesion.json" ]; then mal "⊖ token no-string no se persiste" "sin fichero" "lo escribió"
else bien "⊖ token no-string no se persiste"; fi
escenario ""; FK_HTTP=201 FK_RC=0 FK_BODY='{"token":"bad token","runtime_instance":"rti-1","expires_at":"2099","generation":1}'
corre login
igual "⊖ token string fuera de token68 ⇒ rc=7" "7" "$RC"
contiene "⊖ token68 inválido nombra SESION_INCOMPLETA" "SESION_INCOMPLETA" "$SAL"
if [ -e "$CASA/sesion.json" ]; then mal "⊖ token68 inválido no se persiste" "sin fichero" "lo escribió"
else bien "⊖ token68 inválido no se persiste"; fi
escenario ""; sesion; SES_ANTES="$(<"$CASA/sesion.json")"
FK_HTTP=200 FK_RC=0 FK_BODY='{"token":"rotado","runtime_instance":"rti-2","expires_at":"2099"}'
corre session refresh
igual "⊖ refresh 200 sin generation ⇒ rc=7" "7" "$RC"
contiene "⊖ refresh incompleto prohíbe repetir a ciegas" "NO repitas a ciegas" "$SAL"
if [ "$SES_ANTES" = "$(<"$CASA/sesion.json")" ]; then bien "⊖ y conserva el snapshot local anterior"
else mal "⊖ conserva el snapshot anterior" "$SES_ANTES" "$(<"$CASA/sesion.json")"; fi
# «no hay fichero» y «fichero sin token» son causas distintas
escenario "$NATIVO"; FK_HTTP=200 FK_RC=0 FK_BODY=''
printf '%s' '{"runtime_instance":"rti-1"}' >> "$CASA/sesion.json"; chmod 600 "$CASA/sesion.json"
cuerpo "x"; corre_c post alice-frontend bob-reviewer FYI "t"
contiene "⊕ fichero SIN token ⇒ SESION_INUTILIZABLE" "SESION_INUTILIZABLE" "$SAL"
escenario "$NATIVO"; FK_HTTP=200 FK_RC=0 FK_BODY=''
cuerpo "x"; corre_c post alice-frontend bob-reviewer FYI "t"
contiene "⊖ sin fichero ⇒ SIN_SESION_LOCAL (no un código del wire)" "SIN_SESION_LOCAL" "$SAL"

echo "── ⑭bis ADR-001:283 — native-required NUNCA cae, tampoco ante un AMBIGUO ──"
# Falsador PEDIDO por @cto-llminbox (R2, 21:09:27Z) y que yo NO tenía: mis ⊖ de
# native-required cubrían SIN_ENVIO, no AMBIGUO. Su lectura del ADR es correcta: un
# ambiguo no es una respuesta que autorice el fallback — es EXACTAMENTE el caso para el
# que native-required existe. Y su admisión también: el md5 del fichero, no el rc solo.
escenario "$EXIGIDO"; sesion; FK_HTTP=000 FK_RC=28 FK_BODY=''
cuerpo "x"; corre_c post alice-frontend bob-reviewer FYI "t"
igual "timeout bajo native-required ⇒ rc=7, no 0" "7" "$RC"
if intacto; then bien "⊖ y el canon NO crece (md5 idéntico) — ADR-001:283"
else mal "⊖ canon intacto" "$MD5_ANTES" "$(_md5)"; fi
no_contiene "⊖ y no imprime que haya tomado el bridge" "legacy_unverified" "$SAL"
escenario "$EXIGIDO"; sesion; FK_HTTP=503 FK_RC=0 FK_BODY='{"detail":"boom"}'
cuerpo "x"; corre_c post alice-frontend bob-reviewer FYI "t"
igual "5xx bajo native-required ⇒ rc=7" "7" "$RC"
if intacto; then bien "⊖ y tampoco crece"
else mal "⊖ canon intacto" "$MD5_ANTES" "$(_md5)"; fi
# ⊕ el control: bajo `native` (NO required) y con SIN_ENVIO, caer SÍ es lo contratado
escenario "$NATIVO"; sesion; FK_HTTP=000 FK_RC=7 FK_BODY=''
cuerpo "x"; corre_c post alice-frontend bob-reviewer FYI "t"
if intacto; then mal "⊕ native + SIN_ENVIO SÍ cae (si no, el ⊖ no discrimina)" "canon cambiado" "intacto"
else bien "⊕ native + SIN_ENVIO SÍ cae (el ⊖ discrimina, no dice «no» a todo)"; fi

echo "── ⑭ter NINGÚN rc DE curl SALE COMO rc DE llmi (@contratosbik, AMEND 21:17:07Z) ──"
# La regla la añadió quien puso el número: adjudicó rc=7 = AMBIGUO ⇒ REINTENTA, y en un CLI
# cuyo transporte es curl, 7 ya significaba lo contrario (conexión rechazada ⇒ SIN_ENVIO ⇒
# nada salió). Hoy no se tocan porque el rc de curl vive en $_RCC y se traduce a CLASE — pero
# «hoy no se tocan» es suerte, no contrato. Esto lo convierte en gate.
# 🔑 Los dos casos afilados son donde el número COINCIDE: curl 7 no puede salir 7, y curl 6
#    (DNS) no puede salir 6 (que es «el servidor te rechazó», lo contrario de «no salió»).
for par in "7:5" "6:5" "28:7" "56:7" "52:7"; do
  crc="${par%%:*}"; esperado="${par##*:}"
  escenario "$EXIGIDO"; sesion; FK_HTTP=000 FK_RC="$crc" FK_BODY=''
  cuerpo "x"; corre_c post alice-frontend bob-reviewer FYI "t"
  igual "curl rc=$crc ⇒ salida $esperado (traducida a clase, no propagada)" "$esperado" "$RC"
  intacto || mal "⊖ y nunca escribe local (curl rc=$crc)" "$MD5_ANTES" "$(_md5)"
done
# ⊖ el brazo que sólo caza el otro defecto: un 5xx REAL sigue siendo AMBIGUO con salida 7
escenario "$EXIGIDO"; sesion; FK_HTTP=503 FK_RC=0 FK_BODY='{"detail":"boom"}'
cuerpo "x"; corre_c post alice-frontend bob-reviewer FYI "t"
igual "⊖ 5xx real ⇒ AMBIGUO ⇒ salida 7 (el 7 legítimo sigue existiendo)" "7" "$RC"
if intacto; then bien "⊖ y tampoco escribe"
else mal "⊖ no escribe" "$MD5_ANTES" "$(_md5)"; fi

echo "── ⑮ EL 404 SE PARTE POR SUPERFICIE + CÓDIGO TIPADO (@design A4) ──"
escenario "$EXIGIDO"; sesion; FK_HTTP=404 FK_RC=0 FK_BODY='Not Found'
cuerpo "x"; corre_c post alice-frontend bob-reviewer FYI "t"
igual "404 sobre /events (debe existir siempre) ⇒ rc=5" "5" "$RC"
contiene "y se llama GATEWAY_AUSENTE" "GATEWAY_AUSENTE" "$SAL"
contiene "y dice dónde NO buscar" "no ha llegado a mirarse" "$SAL"
if intacto; then bien "⊖ y no cae al local"
else mal "⊖ no cae al local" "$MD5_ANTES" "$(_md5)"; fi
# ⊖ el control que separa las dos mitades del 404
escenario ""; sesion; FK_HTTP=404 FK_RC=0 FK_BODY='Not Found'
corre recibo e-que-no-existe
# Con D9 el recurso vive BAJO el prefijo, así que su 404 ya es GATEWAY_AUSENTE: mientras
# no haya pasarela, «no existe el recurso» es indistinguible de «no existe la superficie»,
# y decir lo segundo es lo honesto. La cota está escrita en el `case` de `_desenlace`.
igual "⊖ 404 de un recurso NATIVO ⇒ rc=3 (lectura)" "3" "$RC"
contiene "y se llama GATEWAY_AUSENTE, no NO_EXISTE" "GATEWAY_AUSENTE" "$SAL"

# Un 2xx con forma rota no acredita una lectura: imprimir campos vacíos era un verde mudo.
escenario ""; sesion; FK_HTTP=200 FK_RC=0 FK_BODY='{"receipt_id":"r1","state":"accepted"}'
corre recibo r1
igual "⊖ recibo 200 incompleto ⇒ rc=3, no éxito" "3" "$RC"
contiene "⊖ y nombra RESPUESTA_NATIVA_INCOMPLETA" "RESPUESTA_NATIVA_INCOMPLETA" "$SAL"
escenario ""; sesion; FK_HTTP=200 FK_RC=0 FK_BODY='{"receipt_id":"r1","state":"accepted","subject_type":"event","subject_id":"e1","event_id":"e1"}'
corre recibo r1
igual "⊕ recibo público completo ⇒ rc=0" "0" "$RC"
contiene "⊕ y presenta el sujeto tipado" "sujeto:     event · e1" "$SAL"
contiene "⊕ y conserva el alias de evento" "evento:     e1" "$SAL"
escenario ""; sesion; FK_HTTP=200 FK_RC=0 FK_BODY='{"receipt_id":"r-old","state":"accepted","event_id":"e-old"}'
corre recibo r-old
igual "⊕ el trío histórico de evento sigue aceptado" "0" "$RC"
contiene "⊕ y se interpreta honestamente como event" "sujeto:     event · e-old" "$SAL"
escenario ""; sesion; FK_HTTP=200 FK_RC=0 FK_BODY='{"receipt_id":"r2","state":"rejected","subject_type":"denial","subject_id":"dnl-2"}'
corre recibo r2
igual "⊕ recibo de fallo tipado ⇒ rc=0" "0" "$RC"
contiene "⊕ y no lo presenta como evento" "sujeto:     denial · dnl-2" "$SAL"
no_contiene "⊖ un denial no gana alias event_id" "evento:" "$SAL"

echo '── ⑰ llmi politica: ENSEÑA la vigente sin INVENTARLA (P2-5 de @design) ──'
pol_home() { printf '%s' "$1" >> "$CASA/.llminbox-native-mode.json"; }

# ⊕/⊖ EL NÚCLEO DEL VERBO: `bridge` DECLARADO y NO DECLARADO tienen el MISMO modo efectivo
# y NO pueden pintarse igual. Si se colapsan, el operador lee «alguien decidió bridge»
# donde lo que hay es el suelo — y esa diferencia es justo a lo que viene.
escenario '{"version":1,"lanes":{"pruebas":{"post":"bridge"}}}'
corre politica pruebas post
igual "⊕ bridge DECLARADO ⇒ rc=0" "0" "$RC"
contiene "⊕ y se dice que es una declaración" "(declaracion)" "$SAL"
contiene "⊕ y QUIÉN la pone" "lo pone:  $POL" "$SAL"
escenario '{"version":1,"lanes":{"pruebas":{"otroverbo":"native"}}}'
corre politica pruebas post
igual "⊖ NO declarado ⇒ rc=0 igualmente" "0" "$RC"
contiene "⊖ pero se dice que es el DEFECTO" "(defecto)" "$SAL"
contiene "⊖ y que no lo decidió nadie" "NADIE" "$SAL"
no_contiene "⊖ y NO se presenta como declaración" "(declaracion)" "$SAL"

# gana el más estricto Y SE VE CUÁL: sin esto, con dos fuentes nadie sabe cuál mandó
escenario '{"version":1,"lanes":{"pruebas":{"post":"bridge"}}}'
pol_home '{"version":1,"lanes":{"pruebas":{"post":"native-required"}}}'
corre politica pruebas post
contiene "gana el más estricto de las dos fuentes" "efectivo: native-required" "$SAL"
contiene "y se marca cuál ganó" "← gana" "$SAL"
contiene "nombrando su origen" "HOME" "$SAL"

# FAIL-CLOSED: con una fuente ilegible NO se da modo, porque podría ser la más estricta
escenario '{"esto no es json'
corre politica pruebas post
igual "⊖ fuente ilegible ⇒ rc=1" "1" "$RC"
no_contiene "⊖ y NO se aventura un modo efectivo" "efectivo:" "$SAL"
contiene "⊖ diciendo por qué callarse es lo correcto" "mas BLANDO que el real" "$SAL"
escenario '{"version":1,"lanes":{"pruebas":{"post":"nativo-ish"}}}'
corre politica pruebas post
igual "⊖ modo desconocido ⇒ rc=1" "1" "$RC"
no_contiene "⊖ y tampoco aquí se aventura un modo" "efectivo:" "$SAL"

# JSON LIMPIO: válido, y ante error stdout VACÍO (un consumidor no puede tragarse prosa)
escenario '{"version":1,"lanes":{"pruebas":{"post":"native"}}}'
J="$( PATH="$REPO/tests/falso:$PATH" HOME="$CASA" LLMINBOX_NATIVE_POLICY="$POL" \
      "$REPO/llmi" politica pruebas post --json 2>/dev/null )"
if printf '%s' "$J" | python3 -c 'import json,sys
d=json.load(sys.stdin); c=d["consultas"][0]
print("OK" if c["efectivo"]=="native" and c["declarado"] is True else "MAL")' 2>/dev/null \
   | grep -q OK; then bien "⊕ --json es JSON válido y trae el modo"
else mal "⊕ --json válido" "JSON con efectivo=native" "$(printf '%s' "$J" | head -c 80)"; fi
escenario '{roto'
J2="$( PATH="$REPO/tests/falso:$PATH" HOME="$CASA" LLMINBOX_NATIVE_POLICY="$POL" \
       "$REPO/llmi" politica pruebas post --json 2>/dev/null )"
igual "⊖ ante error, stdout VACÍO (0 bytes)" "0" "$(printf '%s' "$J2" | wc -c | tr -d ' ')"

# SÓLO LECTURA Y SIN RED: ni una petición, ni una escritura, y funciona con el servicio CAÍDO
escenario '{"version":1,"lanes":{"pruebas":{"post":"native"}}}'; FK_HEALTH_RC=7 FK_RC=7 FK_HTTP=000
corre politica pruebas post
igual "⊕ con el servicio CAÍDO sigue respondiendo (rc=0)" "0" "$RC"
if [ -n "$(llamada 'http')" ]; then mal "⊖ y no hace NINGUNA petición" "sin petición" "hubo petición"
else bien "⊖ y no hace NINGUNA petición"; fi
if intacto; then bien "⊖ y no escribe nada"
else mal "⊖ no escribe" "$MD5_ANTES" "$(_md5)"; fi

# CONCORDANCIA CON LO QUE `post` HACE DE VERDAD — el falsador contra «inventarla»:
# el verbo no vale si dice una cosa y el resolutor usa otra.
escenario '{"version":1,"lanes":{"pruebas":{"post":"native-required"}}}'; sesion
corre politica pruebas post
contiene "el verbo dice native-required…" "efectivo: native-required" "$SAL"
FK_HTTP=200 FK_RC=0 FK_BODY=''
cuerpo "x"; corre_c post --dry-run alice-frontend bob-reviewer FYI "t"
contiene "…y post --dry-run resuelve LO MISMO" "native-required" "$SAL"

# flags fail-closed también aquí, y sin argumentos de más
escenario '{"version":1,"lanes":{"pruebas":{"post":"native"}}}'
corre politica --ayuda
igual "\`politica --ayuda\` ⇒ rc=1 (opción LOCAL desconocida, nada enviado)" "1" "$RC"
escenario '{"version":1,"lanes":{"pruebas":{"post":"native"}}}'
corre politica a b c
igual "tres posicionales ⇒ rc=2" "2" "$RC"

echo "── ⑱ D8/D9: Bearer y el prefijo nativo, sin mover el legado ──"
escenario "$NATIVO"; sesion; FK_HTTP=201 FK_RC=0 FK_BODY='{"event_id":"e1","receipt_id":"r1","replayed":false}'
cuerpo "x"; corre_c post alice-frontend bob-reviewer FYI "t"
EV="$(llamada events)"
contiene "D8 · manda Authorization: Bearer" "H:Authorization: Bearer" "$EV"
no_contiene "⊖ y ya NO el esquema propio" "Llminbox-Session" "$EV"
case "$EV" in *"/native/v1/events"*) bien "D9 · /events sale como /native/v1/events" ;;
              *) mal "D9 prefijo en /events" "/native/v1/events" "$EV" ;; esac
# ⊖ EL CONTROL QUE MÁS IMPORTA DE ②: el LEGADO no se mueve. Si el prefijo hubiera ido a
# $API, la bandeja entera se habría mudado y esto lo caza.
escenario ""; FK_HTTP=200 FK_RC=0 FK_BODY='{"ok":1}'
corre stat
case "$(llamada stat)" in *"/native/v1"*) mal "⊖ el legado NO lleva prefijo" "sin /native/v1" "lo lleva" ;;
                          *) bien "⊖ el legado (stat) NO lleva prefijo" ;; esac
escenario ""; FK_HTTP=200 FK_RC=0 FK_BODY='✓ ok'
corre verify
case "$(llamada chain)" in *"/native/v1"*) mal "⊖ chain/verify sin prefijo" "sin /native/v1" "lo lleva" ;;
                           *) bien "⊖ chain/verify tampoco lo lleva" ;; esac
# ⊕ y el prefijo es inyectable, para que el test no dependa del valor por defecto
escenario "$NATIVO"; sesion; FK_HTTP=201 FK_RC=0 FK_BODY='{"event_id":"e1","receipt_id":"r1","replayed":false}'
cuerpo "x"
SAL="$( PATH="$REPO/tests/falso:$PATH" HOME="$CASA" LLMINBOX_SESSION_FILE="$CASA/sesion.json" \
        LLMINBOX_NATIVE_POLICY="$POL" LLMINBOX_NATIVE_PREFIX="/otro/pfx" \
        LLMI_MOUNTS="$CASA/mounts.json" LLMI_LEDGER=pruebas \
        LLMI_ROSTER="$REPO/roster.example.json" LLMINBOX_ROSTER="$REPO/roster.example.json" \
        BIK_CARRIL=pruebas FK_LOG="$FK_LOG" FK_HTTP=201 FK_RC=0 FK_BODY='{"event_id":"e1","receipt_id":"r1","replayed":false}' \
        FK_HEALTH_RC=0 "$REPO/llmi" post alice-frontend bob-reviewer FYI "t" < "$CASA/cuerpo.txt" 2>&1 )"
case "$(llamada events)" in *"/otro/pfx/events"*) bien "⊕ el prefijo es inyectable (no está cableado)" ;;
                            *) mal "⊕ prefijo inyectable" "/otro/pfx/events" "$(llamada events)" ;; esac

echo "── ⑲ D9: el 404 nativo es GATEWAY_AUSENTE por PREFIJO, no por lista ──"
# Antes la lista nombraba 4 rutas y las otras 6 decían NO_EXISTE («comprueba el
# identificador») cuando lo que falta es la superficie entera.
for r in events sessions whoami receipts leases; do
  escenario "$EXIGIDO"; sesion; FK_HTTP=404 FK_RC=0 FK_BODY='Not Found'
  case "$r" in
    events)   cuerpo "x"; corre_c post alice-frontend bob-reviewer FYI "t" ;;
    sessions) corre login ;;
    whoami)   corre whoami ;;
    receipts) corre recibo id-inventado ;;
    leases)   corre lease coger rec --ttl 30 ;;
  esac
  contiene "404 en $r ⇒ GATEWAY_AUSENTE (no NO_EXISTE)" "GATEWAY_AUSENTE" "$SAL"
  no_contiene "⊖ y NO manda a comprobar el identificador ($r)" "comprueba el identificador" "$SAL"
done
# Con la pasarela YA presente, un 404 tipado sí puede significar que el recurso
# consultado no existe. El code plano es el discriminante; el status solo no basta.
escenario ""; sesion; FK_HTTP=404 FK_RC=0 FK_BODY='{"code":"SUBJECT_NOT_FOUND","message":"sujeto no encontrado"}'
corre recibo rcp-inexistente
igual "404 nativo tipado SUBJECT_NOT_FOUND ⇒ rc=6 (rechazo), no gateway ausente" "6" "$RC"
contiene "⊕ y se nombra NO_EXISTE" "NO_EXISTE" "$SAL"
no_contiene "⊖ no culpa a una pasarela presente" "GATEWAY_AUSENTE" "$SAL"
# ⊖ el control: fuera del prefijo, un 404 SIGUE siendo NO_EXISTE
escenario ""; FK_HTTP=404 FK_RC=0 FK_BODY='Not Found'
corre lint
case "$SAL" in *GATEWAY_AUSENTE*) mal "⊖ una ruta del LEGADO no puede ser GATEWAY_AUSENTE" "sin GATEWAY_AUSENTE" "$(printf '%s' "$SAL" | head -1)" ;;
               *) bien "⊖ una ruta del LEGADO con 404 NO cae en la regla nativa" ;; esac
case "$SAL" in *NO_EXISTE*) bien "⊖ y se clasifica como NO_EXISTE" ;;
               *) mal "⊖ legado 404 ⇒ NO_EXISTE" "NO_EXISTE" "$(printf '%s' "$SAL" | head -1)" ;; esac

echo "── ⑳ D10: vocabulario del wire, 503 y REPLAY_UNVERIFIABLE ──"
escenario "$NATIVO"; sesion; FK_HTTP=401 FK_RC=0 FK_BODY='{"code":"SESSION_INVALID"}'
cuerpo "x"; corre_c post alice-frontend bob-reviewer FYI "t"
contiene "SESSION_INVALID tiene consejo propio" "tu sesión no vale" "$SAL"
no_contiene "⊖ y AUTH_REQUIRED ya no se emite" "· AUTH_REQUIRED ·" "$SAL"
escenario "$NATIVO"; sesion; FK_HTTP=503 FK_RC=0 FK_BODY='{"detail":"arrancando"}'
cuerpo "x"; corre_c post alice-frontend bob-reviewer FYI "t"
contiene "503 sin la tupla R3 ⇒ MUTATION_OUTCOME_UNKNOWN (§14.2: 5xx de mutación sin fase)" "MUTATION_OUTCOME_UNKNOWN" "$SAL"
igual "⊖ y SIGUE siendo ambiguo en una mutación ⇒ rc=7" "7" "$RC"
if intacto; then bien "⊖ y por tanto NO cae al local"
else mal "⊖ no cae al local" "$MD5_ANTES" "$(_md5)"; fi
escenario "$NATIVO"; sesion; FK_HTTP=409 FK_RC=0 FK_BODY='{"code":"REPLAY_UNVERIFIABLE"}'
cuerpo "x"; corre_c post alice-frontend bob-reviewer FYI "t"
contiene "REPLAY_UNVERIFIABLE dice ESCALA" "ESCALA al operador" "$SAL"
contiene "⊖ y prohíbe explícitamente la clave nueva" "NO cambies la clave" "$SAL"
# ⊕ el control de que la tabla no dice lo mismo a todo
escenario "$NATIVO"; sesion; FK_HTTP=409 FK_RC=0 FK_BODY='{"code":"LEASE_CONFLICT"}'
cuerpo "x"; corre_c post alice-frontend bob-reviewer FYI "t"
no_contiene "⊕ y otro código NO recibe ese consejo" "ESCALA al operador" "$SAL"

echo "── ⑯ FLAGS FAIL-CLOSED EN LOS 19 VERBOS, uno por uno ──"
# 🩸 ESTA SECCIÓN PASABA POR EL MOTIVO EQUIVOCADO, y lo cazó una auditoría independiente.
# Al cerrar SC2086 cambié `corre $v` por `corre "$v"`, y eso manda «whoami --ayuda» como UN
# SOLO argumento: el CLI no reconoce ese comando, sale `mal_uso` y da rc=2. El aserto seguía
# verde midiendo «un comando inexistente da 2» en vez de «un flag desconocido da 2».
# ⇒ El verde NO cambió al romperse, y por eso el mutante de `_solo_json` sobrevivía con 0
#   fallos: el test no llegaba nunca a `_solo_json`.
# Cura: argumentos SEPARADOS y un caso por subcomando, no un bucle sobre una cadena.
flag_malo() {   # $1=etiqueta · resto: verbo y argumentos, YA separados
  local et="$1"; shift
  escenario ""; sesion; FK_HTTP=200 FK_RC=0 FK_BODY='{"principal":"p","role":"r","lane":"l"}'
  corre "$@"
  igual "$et ⇒ rc=1 (opción LOCAL desconocida)" "1" "$RC"
  if [ -z "$(llamada 'http')" ]; then bien "⊖ y no llega a pedir nada ($et)"
  else mal "⊖ no pide nada ($et)" "sin petición" "hubo petición"; fi
}
flag_malo "whoami --ayuda"        whoami --ayuda
flag_malo "yo --ayuda"            yo --ayuda
flag_malo "login --ayuda"         login --ayuda
flag_malo "session open --ayuda"  session open --ayuda
flag_malo "recibo <id> --ayuda"   recibo id123 --ayuda
flag_malo "politica --ayuda"      politica --ayuda
flag_malo "stat --ayuda"          stat --ayuda
flag_malo "adopcion --ayuda"      adopcion --ayuda
flag_malo "verify --ayuda"        verify --ayuda
flag_malo "lint --ayuda"          lint --ayuda
flag_malo "doctor --ayuda"        doctor --ayuda
flag_malo "canon --ayuda"         canon --ayuda
flag_malo "wiki --ayuda"          wiki --ayuda
flag_malo "to <a> --ayuda"        to cto --ayuda
flag_malo "q <txt> --ayuda"       q texto --ayuda
flag_malo "peek <a> --ayuda"      peek fe --ayuda
# 🔑 `inbox` es el que más costaba: ignorando el flag ejecutaba Y AVANZABA EL CURSOR.
flag_malo "inbox <a> --ayuda"     inbox fe --ayuda
flag_malo "lease coger --ayuda"   lease coger rec --ttl 5 --ayuda
flag_malo "post --ayuda"          post --ayuda alice-frontend bob-reviewer FYI t

echo "── ⑯b ⊕ CONTROLES: la cura NO puede rechazar lo legítimo ──"
# Sin estos, «rechaza todo» pasaría el bloque de arriba entero y no probaría nada.
ok_flag() {   # $1=etiqueta · resto: verbo y argumentos
  local et="$1"; shift
  escenario ""; sesion; FK_HTTP=200 FK_RC=0 FK_BODY='{"principal":"p","role":"r","lane":"l","ok":1}'
  corre "$@"
  if [ "$RC" = "2" ]; then mal "⊕ $et sigue valiendo" "rc≠2" "rc=2 — la cura se pasó de frenada"
  else bien "⊕ $et sigue valiendo (rc=$RC)"; fi
}
ok_flag "stat --json"             stat --json
ok_flag "adopcion --json"         adopcion --json
ok_flag "inbox --carril"          inbox fe --carril pruebas
ok_flag "peek --carril"           peek fe --carril pruebas
ok_flag "whoami --json"           whoami --json
ok_flag "politica --json"         politica pruebas post --json
ok_flag "wiki citas (posicional)" wiki citas
ok_flag "doctor 7 (posicional)"   doctor 7
ok_flag "to cto 5 (posicionales)" to cto 5
escenario ""; FK_HTTP=200 FK_RC=0 FK_BODY='{}'
corre q -- -texto-con-guion
if [ "$RC" = "2" ]; then mal "⊕ el escape -- deja pasar un posicional con guion" "rc≠2" "rc=2"
else bien "⊕ el escape -- deja pasar un posicional con guion (rc=$RC)"; fi

echo "── ①b INBOX NO AUTO-CONFIRMA; ACK EXPLÍCITO DECLARA SU FALLO ──"
escenario ""; FK_HTTP=200; FK_LEIDO_HTTP=403
# Sobre sintético del protocolo ACK v1; sólo lo consume el curl del arnés.
# El contrato completo permite llegar al HTTP que estas pruebas clasifican.
python3 - "$CASA" <<'PY'
import base64,json,pathlib,sys,time
casa=pathlib.Path(sys.argv[1])
ack={"v":1,"grant":"A"*48,"principal":"fixture-fe","role":"fe",
     "lane":"pruebas","ledger":"pruebas","cursor_generation":0,
     "cursor_before":0,"allowed_arrivals":[1],"watermark":1,
     "expires_at":int(time.time())+3600}
grant=base64.urlsafe_b64encode(json.dumps(ack,sort_keys=True,
    separators=(",",":")).encode()).decode().rstrip("=")
casa.joinpath("ack.fixture").write_text(grant)
casa.joinpath("inbox.fixture").write_text(
    "── pruebas · 1 de 1 para ti (lo más reciente) ──\n"
    "  abc #1 L1 · cto [FYI]\n    titular\n\n"
    "marcar leído — pega esto tal cual:\n"
    f"  llmi ack fe 1 --carril pruebas --grant {grant}\n\n"
    "confirmación disponible — el CLI valida este sobre:\n"
    "  POST /inbox/fe/ack\n  "+json.dumps({"hasta":{"pruebas":1},"ack":ack}))
PY
FK_BODY="$(cat "$CASA/inbox.fixture")"
ACK_FIXTURE="$(cat "$CASA/ack.fixture")"
corre inbox fe --carril pruebas
igual "inbox validado no muta ⇒ rc=0" "0" "$RC"
if [ -z "$(llamada '/leido')$(llamada '/ack')" ]; then bien "⊖ inbox no llamó /leido ni /ack"
else mal "⊖ inbox no confirma" "sin POST" "hubo petición de confirmación"; fi
corre ack fe 1 --carril pruebas --grant "$ACK_FIXTURE"
igual "⊖ POLICY_DENIED en ACK explícito ⇒ rc=5" "5" "$RC"
contiene "⊖ y nombra la clase que requiere migración nativa" "POLICY_DENIED" "$SAL"
contiene "y el rechazo vino del endpoint ACK actual" "/inbox/fe/ack" "$(llamada '/ack')"

escenario ""; FK_HTTP=200; FK_LEIDO_HTTP=500
corre ack fe 1 --carril pruebas --grant "$ACK_FIXTURE"
igual "⊖ fallo no tipado en ACK ⇒ rc=3, nunca 0" "3" "$RC"
contiene "⊖ y declara que el cursor no avanzó" "cursor no avanzó" "$SAL"
contiene "y el fallo HTTP vino del endpoint ACK actual" "/inbox/fe/ack" "$(llamada '/ack')"

echo "── ㉑ P0/D8: el bootstrap va con Bearer de WORKLOAD, jamás con el token compartido ──"
escenario ""; FK_HTTP=201 FK_RC=0 FK_BODY='{"token":"ses-1","runtime_instance":"rti","expires_at":"2099","generation":1}'
corre login
igual "login con credencial de workload ⇒ rc=0" "0" "$RC"
LG="$(llamada sessions)"
contiene "y manda Authorization: Bearer" "H:Authorization: Bearer wl-cred" "$LG"
no_contiene "⊖ y NO manda el token compartido" "X-Llminbox-Token" "$LG"
# ⊖ sin credencial de workload NO se cae al compartido: se PARA
escenario ""; sin_workload; FK_HTTP=201 FK_RC=0 FK_BODY='{"token":"ses-1","runtime_instance":"rti","expires_at":"2099","generation":1}'
corre login
igual "⊖ sin credencial de workload ⇒ rc=1 (local)" "1" "$RC"
contiene "y lo nombra" "CREDENCIAL_WORKLOAD_AUSENTE" "$SAL"
contiene "diciendo por qué NO cae al compartido" "MISMO principal" "$SAL"
if [ -z "$(llamada sessions)" ]; then bien "⊖ y no llega a pedir sesión"
else mal "⊖ no pide sesión" "sin petición" "hubo petición"; fi
# ⊖ un fichero PRESENTE y VACÍO no es lo mismo que no tenerlo
escenario ""; sin_workload; printf '\n' >> "$CASA/workload.cred"; FK_HTTP=201 FK_RC=0 FK_BODY='{}'
corre login
igual "⊖ credencial presente y VACÍA ⇒ para (rc=1)" "1" "$RC"
# ⊕ y el token de SESIÓN, no el de workload, es el que muta
escenario "$NATIVO"; sesion; FK_HTTP=202 FK_RC=0 FK_BODY='{"event_id":"e1","receipt_id":"r1","replayed":false}'
cuerpo "x"; corre_c post alice-frontend bob-reviewer FYI "t"
EV="$(llamada events)"
contiene "⊕ /events va con el token de SESIÓN" "H:Authorization: Bearer tok-sesion" "$EV"
no_contiene "⊖ y no con la credencial de workload" "wl-cred" "$EV"
no_contiene "⊖ ni con el token compartido" "X-Llminbox-Token" "$EV"
# ⊖ ninguna ruta NATIVA cae al compartido, ni siquiera las de lectura
escenario ""; sin_workload; FK_HTTP=200 FK_RC=0 FK_BODY='{"principal":"p","role":"r","lane":"l"}'
corre whoami
igual "⊖ whoami sin sesión ni workload ⇒ rc=1 (local), no cae al legado" "1" "$RC"
if [ -z "$(llamada whoami)" ]; then bien "⊖ y no llega a pedir"
else mal "⊖ whoami no pide" "sin petición" "$(llamada whoami)"; fi

echo "── ㉑f Si no puede asegurar el 600, PARA ANTES de curl ──"
# `_cfg_auth` ya devolvía 1 al fallar el modo, pero sus dos llamadores lo IGNORABAN: la
# petición salía SIN `Authorization`, el servidor daba 401 y el CLI lo clasificaba como
# fallo REMOTO. Manda a mirar el servidor cuando el problema es local y el secreto ni se
# envió — la peor dirección posible para un error.
escenario ""; FK_HTTP=201 FK_RC=0 FK_BODY='{"token":"ses-1","runtime_instance":"rti","expires_at":"2099","generation":1}'
SAL="$( PATH="$REPO/tests/falso:$PATH" HOME="$CASA" LLMI_TEST_CHMOD_FALLA_EN=2 LLMI_TEST_CHMOD_CUENTA="$CASA/chmod.n" \
        LLMINBOX_WORKLOAD_FILE="$CASA/workload.cred" LLMINBOX_SESSION_FILE="$CASA/sesion.json" \
        FK_LOG="$FK_LOG" FK_HTTP=201 FK_RC=0 FK_BODY='{"token":"ses-1","runtime_instance":"rti","expires_at":"2099","generation":1}' FK_HEALTH_RC=0 \
        "$REPO/llmi" login 2>&1 )"; RC=$?
igual "chmod falla ⇒ rc=1 (local), no 0" "1" "$RC"
contiene "y lo nombra como problema LOCAL" "CREDENCIAL_NO_ASEGURABLE" "$SAL"
contiene "diciendo que no se envió nada" "NO se ha enviado NADA" "$SAL"
no_contiene "⊖ y NO culpa al servidor" "el servidor rechazó" "$SAL"
# 🔑 EL ASERTO QUE DE VERDAD LO PRUEBA: cero peticiones a la ruta nativa.
if [ -z "$(llamada sessions)" ]; then bien "⊖ y CERO curl a /sessions (no salió sin Authorization)"
else mal "⊖ cero curl con chmod roto" "sin petición" "$(llamada sessions)"; fi
# ⊕ el control: con el mismo PATH y sin forzar el fallo, SÍ pide
escenario ""; FK_HTTP=201 FK_RC=0 FK_BODY='{"token":"ses-1","runtime_instance":"rti","expires_at":"2099","generation":1}'
SAL="$( PATH="$REPO/tests/falso:$PATH" HOME="$CASA" \
        LLMINBOX_WORKLOAD_FILE="$CASA/workload.cred" LLMINBOX_SESSION_FILE="$CASA/sesion.json" \
        FK_LOG="$FK_LOG" FK_HTTP=201 FK_RC=0 FK_BODY='{"token":"ses-1","runtime_instance":"rti","expires_at":"2099","generation":1}' FK_HEALTH_RC=0 \
        "$REPO/llmi" login 2>&1 )"; RC=$?
igual "⊕ con el chmod falso INERTE ⇒ rc=0 (el falso no rompe por sí solo)" "0" "$RC"
if [ -n "$(llamada sessions)" ]; then bien "⊕ y sí pide (el ⊖ discrimina)"
else mal "⊕ pide con chmod sano" "hubo petición" "sin petición"; fi

echo "── ㉑c D8 canónico: SÓLO /sessions usa la credencial de workload ──"
# Leí «issuing or rotating» como que rotar era bootstrap. El canon es más estrecho, y el
# núcleo lo confirma: `refresh_session(token)` rota UNA sesión concreta.
escenario ""; sesion; FK_HTTP=201 FK_RC=0 FK_BODY='{"token":"ses-2","runtime_instance":"rti","expires_at":"2099","generation":2}'
corre session refresh
igual "refresh con token de SESIÓN ⇒ rc=0" "0" "$RC"
RF="$(llamada refresh)"
contiene "y manda el token de sesión" "H:Authorization: Bearer tok-sesion" "$RF"
no_contiene "⊖ y NO la credencial de workload" "wl-cred" "$RF"
# ⊖ sin sesión, refresh NO cae a la credencial de workload
escenario ""; FK_HTTP=201 FK_RC=0 FK_BODY='{"token":"ses-2","runtime_instance":"rti","expires_at":"2099","generation":2}'
corre session refresh
igual "⊖ refresh sin sesión ⇒ rc=1 (LOCAL, y no cae al workload)" "1" "$RC"
if [ -z "$(llamada refresh)" ]; then bien "⊖ y no llega a pedir"
else mal "⊖ refresh no pide" "sin petición" "$(llamada refresh)"; fi
# ⊕ el control que separa las dos puertas: /sessions SÍ va con workload
escenario ""; FK_HTTP=201 FK_RC=0 FK_BODY='{"token":"ses-1","runtime_instance":"rti","expires_at":"2099","generation":1}'
corre login
contiene "⊕ y /sessions sí va con la credencial de workload" "Bearer wl-cred" "$(llamada sessions)"

echo "── ㉑d El \`--\` se CONSUME, no se consulta ──"
escenario ""; FK_HTTP=200 FK_RC=0 FK_BODY='{}'
corre q -- -texto-con-guion
igual "\`q -- -texto\` ⇒ rc=0" "0" "$RC"
QQ="$(llamada entries)"
contiene "y busca el TEXTO (va en --data-urlencode, no en la URL)" "Q:q=-texto-con-guion" "$QQ"
no_contiene "⊖ y NO busca la cadena «--»" "q=--&" "$QQ"

echo "── ㉑g INYECCIÓN en el config de curl: comilla, backslash y CR/LF ──"
# `header = "…$secreto…"` interpola dentro de un valor entrecomillado. Con `"` se cierra la
# cadena; con CR/LF se abre una LÍNEA nueva, o sea una SEGUNDA directiva de curl. El secreto
# sale de un fichero que se escribe a mano: un pegado con salto de línea basta.
inyecta() {   # $1=etiqueta · $2=credencial hostil
  # CONTRATO: todo fallo LOCAL de credencial sale con rc=1 —forma del fichero o charset, da
  # igual cuál de las dos guardas lo cace—, porque las dos dicen lo mismo: la configuración
  # de ESTA máquina está rota y no se ha enviado nada. El 6 queda para «el servidor rechazó
  # con un code». Antes salían 1 y 6 según la guarda, y eso hacía parecer que unas eran del
  # servidor.
  escenario ""
  rm -f "$CASA/workload.cred"; printf '%s' "$2" > "$CASA/workload.cred"
  FK_HTTP=201 FK_RC=0 FK_BODY='{"token":"ses-1","runtime_instance":"rti","expires_at":"2099","generation":1}'
  corre login
  igual "$1 ⇒ rc=1 (config local rota, no se envía)" "1" "$RC"
  contiene "$1 · se nombra" "CREDENCIAL_MAL_FORMADA" "$SAL"
  if [ -z "$(llamada sessions)" ]; then bien "$1 · ⊖ CERO curl"
  else mal "$1 · ⊖ cero curl" "sin petición" "$(llamada sessions)"; fi
  # ⊖ y no aparece en argv: si estuviera, el falso lo habría registrado con A:
  if grep -q 'A:.*Authorization' "$FK_LOG" 2>/dev/null; then
    mal "$1 · ⊖ nunca en argv" "sin -H de Authorization" "estaba en la línea de comandos"
  else bien "$1 · ⊖ nunca en argv"; fi
  # ⊖ y NO se ha creado una segunda directiva: no queda config ninguno
  if [ -n "$(grep -oE 'CFG:[^ ]+' "$FK_LOG" 2>/dev/null | head -1)" ]; then
    mal "$1 · ⊖ ni se llegó a escribir el config" "sin CFG" "hubo config"
  else bien "$1 · ⊖ ni se llegó a escribir el config"; fi
}
inyecta "comilla"   'abc"def'
inyecta "backslash" 'abc\def'
inyecta "CR"        "$(printf 'abc\rdef')"
inyecta "LF y 2ª directiva" "$(printf 'abc\nheader = "X-Colada: si"')"
inyecta "espacio"   'abc def'
# ⊕ EL CONTROL: una credencial legítima (token68) SÍ pasa. Sin esto, «rechaza» podría ser
# «rechaza todo» y el bloque entero no probaría nada.
escenario ""; FK_HTTP=201 FK_RC=0 FK_BODY='{"token":"ses-1","runtime_instance":"rti","expires_at":"2099","generation":1}'
rm -f "$CASA/workload.cred"; printf 'AbC0-9._~+/=\n' > "$CASA/workload.cred"
corre login
igual "⊕ credencial token68 legítima ⇒ rc=0" "0" "$RC"
if [ -n "$(llamada sessions)" ]; then bien "⊕ y SÍ pide (el ⊖ discrimina)"
else mal "⊕ pide con credencial legítima" "hubo petición" "sin petición"; fi

echo "── ㉑h La carga NO concatena líneas (el LF se borraba ANTES de validar) ──"
# `tr -d '\n'` borraba TODOS los saltos antes del validador: `abc<LF>DEF` se cargaba como
# `abcDEF` —dos cosas pegadas en una credencial que nadie escribió— y encima podía pasar el
# charset, porque el carácter peligroso ya no estaba. Un validador de contenido no puede
# correr después de una transformación que borra justo lo que busca.
forma() {   # $1=etiqueta · $2=contenido (con %b) · $3=rc esperado
  escenario ""
  rm -f "$CASA/workload.cred"; printf '%b' "$2" > "$CASA/workload.cred"
  FK_HTTP=201 FK_RC=0 FK_BODY='{"token":"ses-1","runtime_instance":"rti","expires_at":"2099","generation":1}'
  corre login
  igual "$1 ⇒ rc=$3" "$3" "$RC"
  if [ "$3" != "0" ]; then
    if [ -z "$(llamada sessions)" ]; then bien "$1 · ⊖ CERO curl"
    else mal "$1 · ⊖ cero curl" "sin petición" "$(llamada sessions)"; fi
    no_contiene "$1 · ⊖ y NO concatena" "abcDEF" "$SAL"
  fi
}
forma "LF interno"            'abc\nDEF\n'   1
forma "multilínea"            'a\nb\nc\n'    1
forma "CR"                    'abc\rdef\n'   1
# ⊕ los DOS casos legítimos: con y sin salto final
forma "⊕ token68 + salto final" 'AbC0-9._~+/==\n' 0
forma "⊕ sin salto final"       'AbC123'         0

echo "── ㉑i token68: el \`=\` sólo es relleno FINAL ──"
forma "= en medio"            'ab=cd\n'      1
forma "⊕ = sólo al final"     'abcd==\n'     0

echo "── ㉑j El fallo de _cfg_auth también se propaga por la vía de SESIÓN ──"
# El mutante que ignoraba el fallo sobrevivía: mi único falsador usaba `login` (vía
# workload), y por la vía de SESIÓN nadie lo ejercitaba.
escenario "$NATIVO"; sesion; FK_HTTP=202 FK_RC=0 FK_BODY='{"event_id":"e1","receipt_id":"r1","replayed":false}'
cuerpo "x"
SAL="$( PATH="$REPO/tests/falso:$PATH" HOME="$CASA" LLMI_TEST_CHMOD_FALLA_EN=2 LLMI_TEST_CHMOD_CUENTA="$CASA/chmod.n" \
        LLMINBOX_SESSION_FILE="$CASA/sesion.json" LLMINBOX_WORKLOAD_FILE="$CASA/workload.cred" \
        LLMINBOX_NATIVE_POLICY="$POL" LLMI_MOUNTS="$CASA/mounts.json" LLMI_LEDGER=pruebas \
        LLMI_ROSTER="$REPO/roster.example.json" LLMINBOX_ROSTER="$REPO/roster.example.json" \
        BIK_CARRIL=pruebas FK_LOG="$FK_LOG" FK_HTTP=202 FK_RC=0 FK_BODY='{"event_id":"e1","receipt_id":"r1","replayed":false}' \
        FK_HEALTH_RC=0 "$REPO/llmi" post alice-frontend bob-reviewer FYI "t" < "$CASA/cuerpo.txt" 2>&1 )"; RC=$?
if [ "$RC" = "0" ]; then mal "chmod roto por la vía de sesión ⇒ no publica" "rc≠0" "rc=0"
else bien "chmod roto por la vía de sesión ⇒ rc=$RC, no publica"; fi
if [ -z "$(llamada events)" ]; then bien "⊖ y CERO curl a /events"
else mal "⊖ cero curl a /events" "sin petición" "$(llamada events)"; fi
if intacto; then bien "⊖ y tampoco cae al bridge local"
else mal "⊖ no cae al local" "$MD5_ANTES" "$(_md5)"; fi

echo "── ㉑k DISCRIMINANTE: local ⇒ 1 · servidor ⇒ 6, y no se confunden ──"
# Sin este par, «sale 1» podría ser «sale 1 a todo» y el contrato no diría nada.
escenario ""
rm -f "$CASA/workload.cred"; printf 'abc def\n' > "$CASA/workload.cred"
FK_HTTP=201 FK_RC=0 FK_BODY='{"token":"ses-1","runtime_instance":"rti","expires_at":"2099","generation":1}'
corre login
igual "LOCAL (credencial mal formada) ⇒ 1" "1" "$RC"
if [ -z "$(llamada sessions)" ]; then bien "⊖ y CERO curl: no llegó a preguntar"
else mal "⊖ cero curl en fallo local" "sin petición" "$(llamada sessions)"; fi
# ⊕ el otro lado: el SERVIDOR rechaza con un code ⇒ 6, y sí hubo petición
escenario "$NATIVO"; sesion; FK_HTTP=409 FK_RC=0 FK_BODY='{"code":"IDEMPOTENCY_CONFLICT"}'
cuerpo "x"; corre_c post alice-frontend bob-reviewer FYI "t"
igual "⊕ SERVIDOR (code del vocabulario) ⇒ 6" "6" "$RC"
if [ -n "$(llamada events)" ]; then bien "⊕ y SÍ hubo petición (el 6 dice que contestó alguien)"
else mal "⊕ hubo petición" "una llamada a /events" "ninguna"; fi

echo "── ㉑e El fichero con el secreto NO sobrevive al proceso ──"
# 🩸 LA PRIMERA VERSIÓN BARRÍA `/tmp` Y `/var/folders/*/*/T` ENTEROS con `grep -r`: más de
# 8.700 directorios, bloqueó la corrida y hubo que matarla a mano. Y era doblemente malo:
# un test JAMÁS debe rastrear temporales globales — ahí viven secretos de OTRAS sesiones,
# y leerlos es peor que el defecto que buscaba.
# Cura: el `curl` falso registra la RUTA del `--config`; se comprueba ESA ruta y ninguna más.
escenario ""; FK_HTTP=201 FK_RC=0 FK_BODY='{"token":"ses-1","runtime_instance":"rti","expires_at":"2099","generation":1}'
corre login
CFG=$(grep -oE 'CFG:[^ ]+' "$FK_LOG" | head -1 | sed 's/^CFG://')
if [ -z "$CFG" ]; then mal "el secreto viaja por --config" "una ruta CFG:" "ninguna"
elif [ -e "$CFG" ]; then mal "el fichero del secreto se borra al salir" "que NO exista" "sigue en $CFG"
else bien "el fichero del secreto ($CFG) ya no existe tras salir"; fi
# ⊕ el control: que la ruta EXISTÍA mientras corría — si no, «no existe» sería trivial
if [ -n "$CFG" ]; then bien "⊕ y el falso llegó a verla usada (CFG registrado)"
else mal "⊕ CFG registrado" "una ruta" "ninguna"; fi

echo "── ㉑b El secreto NO viaja en la línea de comandos ──"
# `-H "Authorization: Bearer <token>"` mete el secreto en el argv de curl, que lee
# cualquiera con `ps`. Va por un fichero de config con modo 600.
escenario ""; FK_HTTP=201 FK_RC=0 FK_BODY='{"token":"ses-1","runtime_instance":"rti","expires_at":"2099","generation":1}'
corre login
if grep -q 'A:.*Authorization' "$FK_LOG" 2>/dev/null; then
  mal "el secreto no va en argv" "sin -H de Authorization" "iba en la línea de comandos"
else bien "el secreto no va en argv (viaja por --config)"; fi
contiene "⊕ y aun así la cabecera LLEGA (si no, el ⊖ sería por ceguera)" "H:Authorization: Bearer" "$(llamada sessions)"

echo "── ⑬ WHOAMI NO PINTA NADA DE ESTA MÁQUINA ──"
escenario ""; FK_HEALTH_RC=7 FK_HTTP=000 FK_RC=7 FK_BODY=''
corre whoami
igual "sin servicio ⇒ rc=3" "3" "$RC"
no_contiene "⊖ y NO inventa un principal" "principal:" "$SAL"
escenario ""; sesion; FK_HEALTH_RC=0 FK_HTTP=200 FK_RC=0 \
  FK_BODY='{"principal":"alice-frontend","principal_source":"derived_from_role","role":"frontend","lane":"pruebas","runtime_instance":"rti-1"}'
corre whoami
igual "⊕ con servicio ⇒ rc=0" "0" "$RC"
contiene "⊕ y pinta lo que dijo el SERVIDOR" "principal:        alice-frontend" "$SAL"
contiene "⊕ y avisa de que el principal viene DERIVADO" "DERIVADO" "$SAL"
escenario ""; sesion; FK_HEALTH_RC=0 FK_HTTP=200 FK_RC=0 FK_BODY='{}'
corre whoami
igual "⊖ cuerpo sin NINGÚN campo de identidad ⇒ rc=3, no 0" "3" "$RC"
contiene "y se llama IDENTIDAD_VACIA" "IDENTIDAD_VACIA" "$SAL"
no_contiene "⊖ y no pinta un principal en blanco" "principal:        \n" "$SAL"
escenario ""; sesion; FK_HTTP=200 FK_RC=0 FK_BODY='{"principal":"alice-frontend","role":"frontend"}'
corre whoami
igual "⊕ con identidad parcial ⇒ rc=0" "0" "$RC"
contiene "⊕ y el campo que NO vino se DICE, no se deja en blanco" "no lo sé (el gateway no lo devolvió)" "$SAL"

R3BODY='{"code":"SERVICE_UNAVAILABLE","message":"el servicio no puede atender la peticion","receipt":"unavailable","retryable":true,"audit_suspended":true}'
MOUBODY='{"code":"MUTATION_OUTCOME_UNKNOWN","message":"no se pudo determinar el resultado de esta mutacion","receipt":"unavailable","retryable":false}'

echo "── 22 §14.1 · R3 SE CASA POR LA TUPLA COMPLETA, NO POR EL STATUS ──"
# El 5xx propio de la pasarela POSTERIOR al núcleo llega con el MISMO 503 y manda la acción
# CONTRARIA. Si «casar R3» se cumpliera casando todo 503, esta rama valdría cero.
escenario "$NATIVO"; sesion; cuerpo "x"; FK_HTTP=503 FK_RC=0 FK_BODY="$R3BODY"
corre_c post alice-frontend bob-reviewer FYI "t"
contiene "tupla COMPLETA ⇒ se nombra SERVICE_UNAVAILABLE" "SERVICE_UNAVAILABLE" "$SAL"
no_contiene "⊖ y NO manda a mirar el cuerpo (viene vacío A PROPÓSITO)" "mira el cuerpo" "$SAL"
contiene "⊖ y SIGUE siendo AMBIGUO: la fila nueva no relaja el anti-duplicado" "AMBIGUO" "$SAL"
if intacto; then bien "⊖ y NO cae a publicación local"; else mal "⊖ no cae al local" "$MD5_ANTES" "$(_md5)"; fi

escenario "$NATIVO"; sesion; cuerpo "x"; FK_HTTP=503 FK_RC=0 FK_BODY='{"code":"SERVICE_UNAVAILABLE","receipt":"unavailable"}'
corre_c post alice-frontend bob-reviewer FYI "t"
no_contiene "⊖ SIN retryable NO es R3 (sobra un campo y deja de serlo)" "el sustrato del servicio NO está disponible" "$SAL"

escenario "$NATIVO"; sesion; cuerpo "x"; FK_HTTP=503 FK_RC=0 FK_BODY="$MOUBODY"
corre_c post alice-frontend bob-reviewer FYI "t"
contiene "⊖ 503 con OTRO code ⇒ MUTATION_OUTCOME_UNKNOWN, no R3" "MUTATION_OUTCOME_UNKNOWN" "$SAL"

echo "── 23 §14.2 · «REPITE» SÓLO DONDE VIAJA LA CLAVE — discriminante por ENDPOINT ──"
# La frase se imprimía a las SIETE mutaciones y la Idempotency-Key viaja en UNA.
# Los casos JUNTOS: por separado, «no digas repite» se cumple callándose en las siete.
escenario "$NATIVO"; sesion; cuerpo "x"; FK_HTTP=000 FK_RC=28 FK_BODY=""
corre_c post alice-frontend bob-reviewer FYI "t"
contiene "⊕ POST /events sin envelope ⇒ SÍ aconseja repetir" "repite el MISMO comando" "$SAL"
contiene "⊕ y ENSEÑA la clave con la que repetir" "Idempotency-Key:" "$SAL"

escenario ""; sesion; FK_HTTP=000 FK_RC=28 FK_BODY=""
corre lease coger recurso-x --ttl 300
no_contiene "⊖ lease coger sin envelope ⇒ NO dice «repite»" "repite el MISMO comando" "$SAL"
contiene "⊖ y manda ESCALAR (sin prueba del resultado histórico)" "ESCALA al operador" "$SAL"
# COPY EXACTO: el riesgo se nombra como RIESGO, no como certeza. La frase decía «un segundo
# intento SERIA un SEGUNDO EFECTO» y eso se midió FALSO en la rama que toma el reintento
# inmediato (mismo `runtime_instance`, lease vivo ⇒ `acquire_lease` devuelve el MISMO
# fencing_token). Los tres asertos JUNTOS: el primero fija la palabra nueva, el segundo
# prohíbe la vieja, y los dos últimos comprueban que la CONDUCTA no se ha relajado — sin
# ellos, «quita la certeza» se cumpliría quitando también el «no repitas».
contiene "⊖ y nombra el riesgo con PUEDE, no con certeza" "PUEDE producir un SEGUNDO EFECTO" "$SAL"
no_contiene "⊖ y NO afirma la certeza («sería un SEGUNDO EFECTO»)" "sería un SEGUNDO EFECTO" "$SAL"
no_contiene "⊖ ni en la otra forma («es un SEGUNDO EFECTO»)" "es un SEGUNDO EFECTO" "$SAL"
contiene "⊖ y la conducta NO se relaja: sigue el «NO te digo que repitas»" "NO te digo que repitas" "$SAL"

escenario ""; FK_HTTP=000 FK_RC=28 FK_BODY=""
corre login
no_contiene "⊖ session open sin envelope ⇒ tampoco dice «repite»" "repite el MISMO comando" "$SAL"

# ⊖ PROCEDENCIA DE LA CLAVE: con --key la atadura clave/cuerpo la afirma el operador y este
# cliente no puede verificarla ⇒ tampoco aconseja repetir (§14.2, «misma key Y mismo cuerpo»).
escenario "$NATIVO"; sesion; cuerpo "x"; FK_HTTP=000 FK_RC=28 FK_BODY=""
corre_c post alice-frontend bob-reviewer FYI "t" --key clave-a-mano
no_contiene "⊖ con --key (no derivada) ⇒ NO aconseja repetir" "repite el MISMO comando" "$SAL"
contiene "⊖ y lo dice: la atadura con el cuerpo la afirmas tú" "la afirmas tú" "$SAL"

echo "── 24 §14.4 · MUTACIÓN SIN ENVELOPE ≡ DESENLACE DESCONOCIDO ──"
escenario "$NATIVO"; sesion; cuerpo "x"; FK_HTTP=000 FK_RC=28 FK_BODY=""
corre_c post alice-frontend bob-reviewer FYI "t"
contiene "corte de transporte ⇒ MUTATION_OUTCOME_UNKNOWN" "MUTATION_OUTCOME_UNKNOWN" "$SAL"
contiene "⊕ y dice que la ausencia de audit_suspended aquí no significa nada" "no significa «no suspendida»" "$SAL"
# ⊖ el control que impide «lo llama MOU a todo»: una LECTURA cortada NO es una mutación.
escenario ""; sesion; FK_HTTP=000 FK_RC=28 FK_BODY=""
corre whoami
no_contiene "⊖ una LECTURA cortada NO se llama MUTATION_OUTCOME_UNKNOWN" "MUTATION_OUTCOME_UNKNOWN" "$SAL"

echo "── 25 EL TOKEN LEGADO TAMPOCO VA EN argv ──"
# A: es lo que el falso vio en la LÍNEA DE COMANDOS (lo que ve ps); H: la cabecera que
# llegó por --config. Sin las dos, «no va en argv» se cumple no mandándola nunca.
escenario ""; printf 'tok-compartido-secreto\n' >> "$CASA/.llminbox.token"
FK_HTTP=200 FK_RC=0 FK_BODY='{"entries":[]}'
corre to alice-frontend
case "$(llamada entries)" in
  *"A:"*"tok-compartido-secreto"*) mal "el token legado NO va en argv" "sin A: con el secreto" "$(llamada entries)" ;;
  *) bien "el token legado NO va en argv (viaja por --config)" ;;
esac
case "$(llamada entries)" in
  *"H:X-Llminbox-Token: tok-compartido-secreto"*) bien "⊕ y aun así la cabecera LLEGA (el ⊖ no es por ceguera)" ;;
  *) mal "⊕ la cabecera llega" "H:X-Llminbox-Token: ..." "$(llamada entries)" ;;
esac

echo "── 26 Si no puede asegurar el 600 del TOKEN LEGADO, para ANTES de curl ──"
# El contador discrimina: la 1.ª llamada a chmod es la del legado, la 2.ª la de la sesión.
escenario ""; printf 'tok-compartido-secreto\n' >> "$CASA/.llminbox.token"
SAL="$( PATH="$REPO/tests/falso:$PATH" HOME="$CASA" \
        LLMI_TEST_CHMOD_FALLA_EN=1 LLMI_TEST_CHMOD_CUENTA="$CASA/chmod1.n" \
        FK_LOG="$FK_LOG" FK_HTTP=200 FK_RC=0 FK_BODY='{"entries":[]}' FK_HEALTH_RC=0 \
        "$REPO/llmi" to alice-frontend 2>&1 )"; RC=$?
igual "chmod del legado falla ⇒ rc=1 (LOCAL)" "1" "$RC"
contiene "y lo dice: no envío el secreto" "NO envío el secreto" "$SAL"
if [ -z "$(llamada entries)" ]; then bien "⊖ y CERO curl (no salió sin el token)"
else mal "⊖ cero curl con el chmod del legado roto" "sin petición" "$(llamada entries)"; fi

echo "── 27 F1 · EL FICHERO DE SESIÓN SE AUDITA ANTES DE ABRIRLO ──"
# `[ -r ]` sólo pregunta «¿puedo leerlo?», y eso lo cumple un fichero que puede leer TODO el
# mundo. El token MUTA en nombre del principal: si no es exclusivamente mío, ya está
# comprometido y usarlo propaga la fuga a la red.
ses_modo() { printf '{"token":"tok-sesion","runtime_instance":"rti-1","expires_at":"2099-01-01T00:00:00Z","generation":3}\n' > "$CASA/sesion.json"; chmod "$1" "$CASA/sesion.json"; }

# ⊕ POSITIVO: 600 pasa. Sin él, «rechaza lo inseguro» se cumple RECHAZÁNDOLO TODO.
escenario ""; ses_modo 600; FK_HTTP=200 FK_RC=0 FK_BODY='{"principal":"alice-frontend","role":"fe"}'
corre whoami
igual "⊕ sesión en 0600 ⇒ rc=0 (la guarda NO mata el camino bueno)" "0" "$RC"
if [ -n "$(llamada whoami)" ]; then bien "⊕ y SÍ pide (la petición sale)"
else mal "⊕ con 600 pide" "una petición" "ninguna"; fi

# ⊖ 0644: un fichero que puede leer cualquiera.
escenario ""; ses_modo 644; FK_HTTP=200 FK_RC=0 FK_BODY='{"principal":"alice-frontend"}'
corre whoami
igual "⊖ sesión en 0644 ⇒ rc=1 (LOCAL)" "1" "$RC"
contiene "⊖ y lo nombra SESION_INSEGURA con el modo" "está en modo 644, no 600" "$SAL"
contiene "⊖ y dice que nada salió" "NADA se ha enviado" "$SAL"
if [ -z "$(llamada whoami)" ]; then bien "⊖ y CERO petición"
else mal "⊖ cero petición con 0644" "ninguna" "$(llamada whoami)"; fi
no_contiene "⊖ y el token NO aparece en lo enviado" "tok-sesion" "$(cat "$FK_LOG")"

# ⊖ SYMLINK: quien controle el enlace elige qué fichero abro.
escenario ""; printf '{"token":"tok-sesion","runtime_instance":"rti-1"}\n' > "$CASA/real.json"
chmod 600 "$CASA/real.json"; rm -f "$CASA/sesion.json"; ln -s "$CASA/real.json" "$CASA/sesion.json"
FK_HTTP=200 FK_RC=0 FK_BODY='{"principal":"alice-frontend"}'
corre whoami
igual "⊖ sesión que es SYMLINK ⇒ rc=1" "1" "$RC"
contiene "⊖ y lo nombra" "ENLACE SIMBÓLICO" "$SAL"
if [ -z "$(llamada whoami)" ]; then bien "⊖ y CERO petición (ni con el destino en 600)"
else mal "⊖ cero petición con symlink" "ninguna" "$(llamada whoami)"; fi

# ⊖ TOCTOU · EL `stat` DE LA RUTA YA NO EXISTE, Y ESTE ES EL FALSADOR QUE LO PRUEBA.
# El `stat` falso SABOTEA: si alguien lo llama sobre `sesion.json`, sustituye el fichero por
# un impostor 0644 y devuelve el resultado del bueno — exactamente el TOCTOU que había
# (comprobar por RUTA y luego abrir por RUTA). Con el cargador de un solo `open`+`fstat`
# NADIE llama a `stat` sobre esa ruta, así que el sabotaje NO OCURRE y el run es normal.
escenario ""; ses_modo 600
SAL="$( PATH="$REPO/tests/falso:$PATH" HOME="$CASA" \
        LLMI_TEST_STAT_SWAP="$CASA/sesion.json" LLMI_TEST_STAT_LOG="$CASA/stat.log" \
        LLMINBOX_SESSION_FILE="$CASA/sesion.json" LLMINBOX_WORKLOAD_FILE="$CASA/workload.cred" \
        FK_LOG="$FK_LOG" FK_HTTP=200 FK_RC=0 FK_BODY='{"principal":"a"}' FK_HEALTH_RC=0 \
        "$REPO/llmi" whoami 2>&1 )"; RC=$?
igual "⊖ TOCTOU · con un \`stat\` saboteador el run NO se ve afectado ⇒ rc=0" "0" "$RC"
if [ ! -s "$CASA/stat.log" ]; then bien "⊖ y CERO \`stat\` sobre la ruta de la sesión (no se comprueba por NOMBRE)"
else mal "⊖ cero stat por ruta" "ninguna llamada" "$(cat "$CASA/stat.log")"; fi
if grep -q "tok-sesion" "$FK_LOG"; then bien "⊕ y viajó el token LEGÍTIMO, no el del impostor"
else mal "⊕ viaja el token legítimo" "tok-sesion en lo enviado" "$(cat "$FK_LOG")"; fi

# ⊖ SUSTITUCIÓN CONCURRENTE (inode swap): el fichero se cambia por un impostor 0644 DESPUÉS
# de que el CLI lo haya cargado. El snapshot vive en memoria y NADA se reabre por ruta, así
# que el intercambio no puede colarse. ⊕ el control es que el impostor SÍ existe y ES 0644:
# sin eso, «no se coló» se cumpliría porque no había nada que colar.
escenario ""; ses_modo 600
printf '{"token":"IMPOSTOR","runtime_instance":"rti-malo"}\n' > "$CASA/impostor.json"
chmod 644 "$CASA/impostor.json"
SAL="$( PATH="$REPO/tests/falso:$PATH" HOME="$CASA" \
        LLMINBOX_SESSION_FILE="$CASA/sesion.json" LLMINBOX_WORKLOAD_FILE="$CASA/workload.cred" \
        FK_LOG="$FK_LOG" FK_HTTP=200 FK_RC=0 FK_BODY='{"principal":"a"}' FK_HEALTH_RC=0 \
        "$REPO/llmi" whoami 2>&1 )"; RC=$?
mv "$CASA/impostor.json" "$CASA/sesion.json"      # el swap, ya con el snapshot cargado
igual "⊖ inode swap tras la carga ⇒ rc=0 (el snapshot no se reabre)" "0" "$RC"
no_contiene "⊖ y el token del IMPOSTOR nunca viaja" "IMPOSTOR" "$(cat "$FK_LOG")"
if [ "$(stat -f%Lp "$CASA/sesion.json" 2>/dev/null || stat -c%a "$CASA/sesion.json")" = "644" ]; then
  bien "⊕ control: el impostor EXISTE y es 0644 (había algo que colar)"
else mal "⊕ el impostor es 0644" "644" "otro modo"; fi

# ⊖ OWNER DISTINTO, con el hook ACOTADO del cargador: sólo cambia el uid CONTRA EL QUE SE
# COMPARA, así que únicamente puede provocar el RECHAZO — nunca hacer pasar un fichero malo.
escenario ""; ses_modo 600
SAL="$( PATH="$REPO/tests/falso:$PATH" HOME="$CASA" LLMI_TEST_FSTAT_UID_FALSO=1 \
        LLMINBOX_SESSION_FILE="$CASA/sesion.json" LLMINBOX_WORKLOAD_FILE="$CASA/workload.cred" \
        FK_LOG="$FK_LOG" FK_HTTP=200 FK_RC=0 FK_BODY='{"principal":"a"}' FK_HEALTH_RC=0 \
        "$REPO/llmi" whoami 2>&1 )"; RC=$?
igual "⊖ owner distinto ⇒ rc=1" "1" "$RC"
contiene "⊖ y lo nombra" "no al tuyo" "$SAL"
if [ -z "$(cat "$FK_LOG")" ]; then bien "⊖ y CERO peticiones — ni /health"
else mal "⊖ cero peticiones con owner ajeno" "ninguna" "$(cat "$FK_LOG")"; fi

# ⊖ AUSENTE no es INSEGURO: sin fichero hay «no hay sesión», no «sesión insegura».
escenario ""; rm -f "$CASA/sesion.json"; FK_HTTP=200 FK_RC=0 FK_BODY='{"principal":"a"}'
corre lease coger r --ttl 60
no_contiene "⊖ sesión AUSENTE ⇒ NO se llama insegura" "SESION_INSEGURA" "$SAL"
contiene "⊖ y sí SIN_SESION_LOCAL" "SIN_SESION_LOCAL" "$SAL"

echo "── 28 F2 · EL TRAP DE SALIDA SE ENCADENA, NO SE PISA ──"
# Se extrae la función REAL del fichero por su LITERAL (no por número de línea: las líneas
# se mueven y la cita caduca) y se le encadenan DOS limpiezas. Si `_encadena_trap_salida`
# pisara en vez de añadir, sólo correría la última — que es como un secreto se queda en
# /tmp o un cerrojo se queda huérfano.
DOS="$( sed -n '/^_encadena_trap_salida() {/,/^}/p' "$REPO/llmi" > "$BASE/helper.sh"
        { cat "$BASE/helper.sh"
          echo '_encadena_trap_salida "echo PRIMERA"'
          echo '_encadena_trap_salida "echo SEGUNDA"'
        } | bash 2>&1 )"
case "$DOS" in *PRIMERA*) bien "corren las DOS limpiezas: la PRIMERA sobrevive" ;;
               *) mal "la primera limpieza sobrevive" "PRIMERA en la salida" "$DOS" ;; esac
case "$DOS" in *SEGUNDA*) bien "⊕ y la SEGUNDA también (el ⊖ no es por no registrar ninguna)" ;;
               *) mal "la segunda limpieza corre" "SEGUNDA en la salida" "$DOS" ;; esac
# ⊖ CONTROL: un `trap` a pelo SÍ pisa — sin esto, «encadena» pasaría aunque el helper
# no hiciera nada, porque no habría con qué comparar.
PISA="$( { echo 'trap "echo PRIMERA" EXIT'; echo 'trap "echo SEGUNDA" EXIT'; } | bash 2>&1 )"
case "$PISA" in *PRIMERA*) mal "⊖ el control debe PERDER la primera" "sin PRIMERA" "$PISA" ;;
                *) bien "⊖ control: con \`trap\` a pelo la PRIMERA se PIERDE (el par discrimina)" ;; esac

echo "── 29 F1-bis · TOCTOU: symlink COLGANTE, no-regular, y NI /health SALE ──"
# `vivo` es un `GET /health` y corría ANTES de la puerta: con un fichero de sesión inseguro
# el CLI decía «NADA se ha enviado» habiendo ya producido tráfico. La frase tiene que ser
# LITERAL. Aquí se comprueba sobre el LOG ENTERO, no sobre la ruta del verbo: si sólo se
# mirara `llamada whoami`, un `/health` de más pasaría desapercibido.
cero_trafico() {   # $1 = etiqueta
  if [ ! -s "$FK_LOG" ]; then bien "$1 · CERO peticiones (ni /health ni la ruta)"
  else mal "$1 · cero peticiones" "log vacío" "$(cat "$FK_LOG")"; fi
}
sin_secreto() {   # $1 = etiqueta · $2 = salida
  no_contiene "$1 · el secreto NO sale por stdout/stderr" "tok-sesion" "$2"
  no_contiene "$1 · ni el secreto en argv" "A:" "$(cat "$FK_LOG")"
}

# ⊖ SYMLINK COLGANTE: el que un `[ -f ]` deja pasar como «no existe» y acaba en «no hay
# sesión» — o peor, en caída al workload. Tiene que ser rc=1, no rc de sesión ausente.
escenario ""; rm -f "$CASA/sesion.json"; ln -s "$CASA/no-existe-jamas.json" "$CASA/sesion.json"
FK_HTTP=200 FK_RC=0 FK_BODY='{"principal":"a"}'
corre whoami
igual "⊖ symlink COLGANTE ⇒ rc=1" "1" "$RC"
contiene "⊖ y lo nombra ENLACE SIMBÓLICO, no «no hay sesión»" "ENLACE SIMBÓLICO" "$SAL"
no_contiene "⊖ y NO cae al workload" "CREDENCIAL_WORKLOAD" "$SAL"
no_contiene "⊖ ni dice que no hay sesión" "SIN_SESION_LOCAL" "$SAL"
cero_trafico "symlink colgante"
sin_secreto "symlink colgante" "$SAL"

# ⊖ NO REGULAR: un fifo. Leerlo por ruta puede colgar o servir lo que otro escriba.
escenario ""; rm -f "$CASA/sesion.json"; mkfifo "$CASA/sesion.json" 2>/dev/null
if [ -p "$CASA/sesion.json" ]; then
  FK_HTTP=200 FK_RC=0 FK_BODY='{"principal":"a"}'
  corre whoami
  igual "⊖ sesión que es un FIFO ⇒ rc=1" "1" "$RC"
  contiene "⊖ y lo nombra" "no es un fichero regular" "$SAL"
  cero_trafico "fifo"
else
  bien "(mkfifo no disponible: caso no ejercitado, y se dice)"
fi

# ⊕ CONTROL que impide que todo lo de arriba pase por «rechaza siempre»: con 0600 legítimo
# el verbo funciona, SALE la petición y el token viaja por --config (nunca por argv).
escenario ""; ses_modo 600; FK_HTTP=200 FK_RC=0 FK_BODY='{"principal":"a","role":"r"}'
corre whoami
igual "⊕ 0600 ⇒ rc=0 (el ⊖ de arriba discrimina)" "0" "$RC"
if [ -n "$(llamada whoami)" ]; then bien "⊕ y SÍ sale la petición"
else mal "⊕ sale la petición con 0600" "una llamada" "ninguna"; fi
case "$(llamada whoami)" in
  *"A:"*) mal "⊕ el token de sesión NO va en argv" "sin A:" "$(llamada whoami)" ;;
  *"H:Authorization: Bearer tok-sesion"*) bien "⊕ y el token viaja por --config, no por argv" ;;
  *) mal "⊕ la cabecera llega por config" "H:Authorization: Bearer …" "$(llamada whoami)" ;;
esac
no_contiene "⊕ y el token tampoco sale por stdout" "tok-sesion" "$SAL"

echo "── 30 F1-ter · LA PUERTA EN LOS CUATRO VERBOS, CON SU CONTROL POR VERBO ──"
# «Medido en los cuatro» estaba ESCRITO en el mensaje del correctivo y ejercitado en UNO.
# La matriz existe para que esa frase deje de ser una afirmacion y pase a ser una medida:
# cada verbo con puerta corre el MISMO ⊖ (sesion insegura ⇒ rc=1 y CERO trafico) y su
# propio ⊕ (con sesion buena SI sale peticion) — sin el ⊕ por verbo, «cero trafico» lo
# cumpliria tambien un verbo que no llama a nadie, y eso pasaria por guarda sin serlo.
puerta_cerrada() {   # "$@" = el verbo con sus argumentos
  local eti="$*"
  escenario ""; rm -f "$CASA/sesion.json"
  ln -s "$CASA/no-existe-jamas.json" "$CASA/sesion.json"
  FK_HTTP=200 FK_RC=0 FK_BODY='{"principal":"a","receipt_id":"r1","lease_id":"l1","token":"t"}'
  corre "$@"
  igual "⊖ [$eti] sesión insegura ⇒ rc=1" "1" "$RC"
  contiene "⊖ [$eti] y lo nombra ENLACE SIMBÓLICO" "ENLACE SIMBÓLICO" "$SAL"
  cero_trafico "⊖ [$eti]"
}
puerta_abierta() {   # el MISMO verbo con una sesión legítima: el canal TIENE que llegar
  local eti="$*"
  escenario ""; sesion
  FK_HTTP=200 FK_RC=0 FK_BODY='{"principal":"a","role":"r","receipt_id":"r1","lease_id":"l1","holder":"a","expires_at":"2099-01-01T00:00:00Z","token":"tok-nuevo","runtime_instance":"rti-9","generation":7}'
  corre "$@"
  if [ -s "$FK_LOG" ]; then bien "⊕ [$eti] con sesión buena SÍ hay tráfico (el ⊖ discrimina)"
  else mal "⊕ [$eti] sale petición" "al menos una llamada" "ninguna"; fi
}
for _v in "login" "session refresh" "whoami" "recibo ev-1" "lease coger r --ttl 60"; do
  # shellcheck disable=SC2086
  puerta_cerrada $_v
  # shellcheck disable=SC2086
  puerta_abierta $_v
done

echo "── 31 EL HOOK DE UID SÓLO PUEDE RECHAZAR — NO ELEGIR EL UID ACEPTADO ──"
# El comentario del cargador prometía que el hook «únicamente puede provocar el RECHAZO», y
# era falso: `int(VARIABLE or getuid())` dejaba que la VARIABLE eligiera el uid ACEPTADO, o
# sea que quien pudiera ponerla hacía pasar el fichero de OTRO poniendo el uid de ese otro.
# El falsador es el valor que ANTES abría la puerta: el uid real. Hoy tiene que seguir
# rechazando — la dirección del hook ya no depende de su valor.
escenario ""; ses_modo 600
SAL="$( PATH="$REPO/tests/falso:$PATH" HOME="$CASA" LLMI_TEST_FSTAT_UID_FALSO="$(id -u)" \
        LLMINBOX_SESSION_FILE="$CASA/sesion.json" LLMINBOX_WORKLOAD_FILE="$CASA/workload.cred" \
        FK_LOG="$FK_LOG" FK_HTTP=200 FK_RC=0 FK_BODY='{"principal":"a"}' FK_HEALTH_RC=0 \
        "$REPO/llmi" whoami 2>&1 )"; RC=$?
igual "⊖ hook puesto AL UID QUE ANTES ACEPTABA ⇒ sigue rc=1" "1" "$RC"
contiene "⊖ y lo nombra" "no al tuyo" "$SAL"
if [ -z "$(cat "$FK_LOG")" ]; then bien "⊖ y CERO peticiones"
else mal "⊖ cero peticiones" "ninguna" "$(cat "$FK_LOG")"; fi
# ⊕ CONTROL: sin la variable, el MISMO fichero pasa. Sin esto, «rechaza» se cumpliría
# rechazando siempre y el hook podría estar muerto.
escenario ""; ses_modo 600; FK_HTTP=200 FK_RC=0 FK_BODY='{"principal":"a","role":"r"}'
corre whoami
igual "⊕ sin el hook, el MISMO fichero pasa ⇒ rc=0" "0" "$RC"

echo '── 32 LOS TRES FLAGS DEL open SON OBLIGATORIOS Y SU AUSENCIA FALLA CERRADO ──'
# `getattr(os, X, 0)` degradaba en SILENCIO: sin O_NOFOLLOW se sigue el enlace, sin
# O_NONBLOCK un fifo cuelga, sin O_CLOEXEC el descriptor viaja a `curl` — los tres defectos
# que este bloque existe para impedir, entrando por la puerta de atrás.
# Se ejercita el CÓDIGO REAL, extraído del fichero por su literal (no por número de línea:
# las líneas se mueven y la cita caduca) y corrido en un intérprete al que se le ha quitado
# el flag. Es la única forma de fabricar «esta plataforma no lo tiene» sin otra plataforma.
CARG="$BASE/cargador.py"
# Por `index`, no por expresión regular: el literal lleva `$(`, `"` y `<<'EOPY'`, y
# escaparlo bien en tres capas (shell, awk, ERE) es justo donde una aguja da 0 y el 0
# parece un hallazgo. Aquí el ⊕ de abajo lo cazó: la extracción salía VACÍA.
awk 'index($0, "salida=\"$(LLMI_SESFILE=") { d=1; next }
     d && $0 == "EOPY" { exit }
     d { print }' "$REPO/llmi" > "$CARG"
if [ -s "$CARG" ]; then
  bien "⊕ control del instrumento: el cargador se extrae y NO sale vacío ($(wc -l < "$CARG" | tr -d ' ') líneas)"
  escenario ""; ses_modo 600
  for _flag in O_NOFOLLOW O_CLOEXEC O_NONBLOCK; do
    SAL="$( LLMI_SESFILE="$CASA/sesion.json" LLMI_TEST_QUITA_FLAG="$_flag" python3 - "$CARG" <<'EOPY' 2>&1
import os, sys
delattr(os, os.environ["LLMI_TEST_QUITA_FLAG"])   # la plataforma que no lo tiene
exec(compile(open(sys.argv[1]).read(), sys.argv[1], "exec"), {"__name__": "__main__"})
EOPY
)"; RC=$?
    igual "⊖ sin $_flag ⇒ el cargador PARA (rc=1), no abre degradado" "1" "$RC"
    contiene "⊖ y dice cuál falta" "$_flag" "$SAL"
  done
  # ⊕ CONTROL: el MISMO bloque, con los tres flags puestos y el MISMO fichero, funciona y
  # emite el token. Sin él, «para sin el flag» se cumpliría con un bloque roto del todo.
  SAL="$( LLMI_SESFILE="$CASA/sesion.json" python3 - "$CARG" <<'EOPY' 2>&1
import sys
exec(compile(open(sys.argv[1]).read(), sys.argv[1], "exec"), {"__name__": "__main__"})
EOPY
)"; RC=$?
  igual "⊕ con los tres flags ⇒ rc=0 (el ⊖ no es un bloque muerto)" "0" "$RC"
  contiene "⊕ y emite el token del snapshot" "tok-sesion" "$SAL"
else
  mal "⊕ el cargador se extrae" "un bloque no vacío" "vacío: la extracción no encontró el heredoc"
fi

echo "── 33 INODE SWAP CON BARRERA REAL: LA SUSTITUCIÓN OCURRE DENTRO DE LA CORRIDA ──"
# El ⊖ anterior movía el impostor DESPUÉS de que el proceso hubiera terminado: no probaba
# que el snapshot no se reabre, probaba que un proceso muerto no lee. Aquí la sustitución la
# hace el `curl` falso en la PRIMERA llamada (`/health`), o sea con el snapshot ya cargado y
# con el token todavía por usar: si el CLI reabriera por ruta para construir la cabecera,
# viajaría el token del IMPOSTOR.
escenario ""; ses_modo 600
printf '{"token":"IMPOSTOR","runtime_instance":"rti-malo"}\n' > "$CASA/impostor.json"
chmod 644 "$CASA/impostor.json"
SWAPLOG="$CASA/swap.log"; : > "$SWAPLOG"
SAL="$( PATH="$REPO/tests/falso:$PATH" HOME="$CASA" \
        LLMI_TEST_SWAP_EN_LLAMADA="$CASA/impostor.json|$CASA/sesion.json" \
        LLMI_TEST_SWAP_LOG="$SWAPLOG" \
        LLMINBOX_SESSION_FILE="$CASA/sesion.json" LLMINBOX_WORKLOAD_FILE="$CASA/workload.cred" \
        FK_LOG="$FK_LOG" FK_HTTP=200 FK_RC=0 FK_BODY='{"principal":"a","role":"r"}' FK_HEALTH_RC=0 \
        "$REPO/llmi" whoami 2>&1 )"; RC=$?
igual "⊖ swap DURANTE la corrida ⇒ rc=0 (el snapshot aguanta)" "0" "$RC"
no_contiene "⊖ y el token del IMPOSTOR nunca viaja" "IMPOSTOR" "$(cat "$FK_LOG")"
contiene "⊖ y el que viaja es el LEGÍTIMO" "Bearer tok-sesion" "$(cat "$FK_LOG")"
# ⊕ CONTROL ①: la barrera se cruzó DE VERDAD — el swap está registrado, y en la llamada a
# `/health`, que es ANTES de la petición del verbo. Sin esto, «no se coló» se cumpliría
# porque la sustitución no llegó a ocurrir.
contiene "⊕ control: el swap OCURRIÓ, y en /health (antes de usar el token)" "/health" "$(cat "$SWAPLOG")"
# ⊕ CONTROL ②: y dejó el fichero de sesión sustituido de verdad, con el impostor dentro.
if grep -q "IMPOSTOR" "$CASA/sesion.json" 2>/dev/null; then
  bien "⊕ control: al acabar, la ruta de la sesión SÍ contiene al impostor (había qué colar)"
else mal "⊕ el impostor acaba en la ruta" "IMPOSTOR en sesion.json" "$(cat "$CASA/sesion.json" 2>/dev/null)"; fi
# ⊕ CONTROL ③: y hubo DOS llamadas — o sea que después del swap el CLI siguió trabajando.
if [ "$(grep -c '^CALL' "$FK_LOG")" -ge 2 ]; then bien "⊕ control: hubo ≥2 llamadas (el swap no fue la última palabra)"
else mal "⊕ ≥2 llamadas" "health + whoami" "$(cat "$FK_LOG")"; fi

echo "── 34 LA ESCRITURA DE LA SESIÓN ES ATÓMICA: O ENTERA O NADA ──"
# Lo que había tocaba el destino cinco veces por NOMBRE y, sobre todo, lo TRUNCABA (`: >`)
# antes de saber si podría terminar. El falsador es un directorio sin permiso de escritura
# con el fichero de sesión YA dentro: crear el temporal falla, y ahí se ve la diferencia —
# con truncado-en-sitio la sesión anterior queda mutilada; con temporal + `replace` sigue
# entera. Es conducta observable, no una lectura del fuente.
escenario ""; mkdir -p "$CASA/jaula"; SESJ="$CASA/jaula/sesion.json"
printf '{"token":"tok-VIEJO","runtime_instance":"rti-viejo"}\n' > "$SESJ"; chmod 600 "$SESJ"
ANTES_MD5="$(md5 -q "$SESJ" 2>/dev/null || md5sum "$SESJ" | cut -d' ' -f1)"
chmod 500 "$CASA/jaula"
SAL="$( PATH="$REPO/tests/falso:$PATH" HOME="$CASA" \
        LLMINBOX_SESSION_FILE="$SESJ" LLMINBOX_WORKLOAD_FILE="$CASA/workload.cred" \
        FK_LOG="$FK_LOG" FK_HTTP=200 FK_RC=0 FK_HEALTH_RC=0 \
        FK_BODY='{"token":"tok-NUEVO","runtime_instance":"rti-9","expires_at":"2099-01-01T00:00:00Z","generation":7}' \
        "$REPO/llmi" login 2>&1 )"; RC=$?
chmod 700 "$CASA/jaula"
igual "⊖ sin poder crear el temporal ⇒ rc=1 (NO guarda a medias)" "1" "$RC"
contiene "⊖ y dice que la anterior sigue intacta" "sigue intacta" "$SAL"
AHORA_MD5="$(md5 -q "$SESJ" 2>/dev/null || md5sum "$SESJ" | cut -d' ' -f1)"
igual "⊖ y la sesión ANTERIOR está INTACTA (ni truncada ni a medias)" "$ANTES_MD5" "$AHORA_MD5"
no_contiene "⊖ y el token nuevo NO se ha escrito en ningún sitio de esa ruta" "tok-NUEVO" "$(cat "$SESJ")"
# ⊕ CONTROL: con el directorio escribible el MISMO login SÍ guarda, en 0600, entero y sin
# dejar temporales. Sin esto, «no guarda a medias» lo cumpliría un login que no guarda nunca.
escenario ""; mkdir -p "$CASA/jaula2"; SESJ2="$CASA/jaula2/sesion.json"
SAL="$( PATH="$REPO/tests/falso:$PATH" HOME="$CASA" \
        LLMINBOX_SESSION_FILE="$SESJ2" LLMINBOX_WORKLOAD_FILE="$CASA/workload.cred" \
        FK_LOG="$FK_LOG" FK_HTTP=200 FK_RC=0 FK_HEALTH_RC=0 \
        FK_BODY='{"token":"tok-NUEVO","runtime_instance":"rti-9","expires_at":"2099-01-01T00:00:00Z","generation":7}' \
        "$REPO/llmi" login 2>&1 )"; RC=$?
igual "⊕ con el directorio escribible ⇒ rc=0" "0" "$RC"
contiene "⊕ y el fichero trae el token nuevo entero" "tok-NUEVO" "$(cat "$SESJ2" 2>/dev/null)"
igual "⊕ y queda en 0600" "600" "$(stat -f%Lp "$SESJ2" 2>/dev/null || stat -c%a "$SESJ2")"
if [ -z "$(find "$CASA/jaula2" -name '.sesion-*' 2>/dev/null)" ]; then
  bien "⊕ y NO queda ningún temporal huérfano"
else mal "⊕ sin temporales" "ninguno" "$(find "$CASA/jaula2" -name '.sesion-*')"; fi

echo "── 35 TRAS ESCRIBIR LA SESIÓN NO SE REABRE: EL SNAPSHOT SALE DEL CUERPO ──"
# Reabrir `$SESFILE` justo después de escribirlo es mirar por NOMBRE, en la ventana más
# corta y más golosa que hay, el único fichero que un tercero querría cambiar — y encima
# para IMPRIMIRLO. No deja rastro en la red ni en `stat`, así que se cuenta por el
# INTÉRPRETE: cada apertura del fichero de sesión es una invocación de `python3`, o con
# `LLMI_SESFILE` en el entorno (el cargador) o con la ruta en el argv (el redactado).
PYLOG="$CASA/py.log"
escenario ""; PYLOG="$CASA/py.log"; : > "$PYLOG"
SAL="$( PATH="$REPO/tests/falso-py:$REPO/tests/falso:$PATH" HOME="$CASA" \
        LLMI_TEST_PY_LOG="$PYLOG" \
        LLMINBOX_SESSION_FILE="$CASA/sesion.json" LLMINBOX_WORKLOAD_FILE="$CASA/workload.cred" \
        FK_LOG="$FK_LOG" FK_HTTP=200 FK_RC=0 FK_HEALTH_RC=0 \
        FK_BODY='{"token":"tok-NUEVO","runtime_instance":"rti-9","expires_at":"2099-01-01T00:00:00Z","generation":7}' \
        "$REPO/llmi" login --json 2>&1 )"; RC=$?
igual "login --json ⇒ rc=0" "0" "$RC"
# ⊕ CONTROL DEL INSTRUMENTO: el contador CUENTA. Si el log saliera vacío, todo lo de abajo
# sería un cero por ceguera — que es justo el fallo que este bloque persigue en otro sitio.
if [ -s "$PYLOG" ]; then bien "⊕ control: el contador de python3 registra ($(grep -c '^PY' "$PYLOG" | tr -d ' ') invocaciones)"
else mal "⊕ el contador registra" "≥1 línea" "log vacío"; fi
# El CARGADOR es el que trae ruta y NO trae cuerpo. El ESCRITOR trae las dos, y contar
# «lleva LLMI_SESFILE» los sumaba: la aguja medía otra cosa y daba 2 donde había 1.
igual "⊖ el CARGADOR corre EXACTAMENTE 1 vez (la puerta), no 2" \
      "1" "$(grep -c 'sesfile=[^ ][^ ]* cuerpo= ' "$PYLOG")"
igual "⊕ control: y el ESCRITOR corre 1 vez (el contador separa los dos, no los suma)" \
      "1" "$(grep -c 'sesfile=[^ ][^ ]* cuerpo=[^ ][^ ]* ' "$PYLOG")"
if grep -q "argv=.*$CASA/sesion.json" "$PYLOG"; then
  mal "⊖ ningún python3 recibe la ruta de la sesión en argv" "sin la ruta en argv" "$(grep "argv=.*sesion.json" "$PYLOG" | head -2)"
else bien "⊖ y NINGÚN python3 recibe la ruta de la sesión en argv (el redactado sale del cuerpo)"; fi
contiene "⊕ y el redactado SÍ sale, con el token omitido" "omitido" "$SAL"
no_contiene "⊕ y el token NO se imprime" "tok-NUEVO" "$SAL"

echo "── 36 EL fsync DEL DIRECTORIO PADRE FALLA CERRADO (DURABILIDAD, NO AVISO) ──"
# `rc=0` de `login` promete GUARDADA **Y** DURABLE. Lo que había fallaba abierto por los
# dos lados: los flags por `getattr(os, X, 0)` —sin `O_DIRECTORY` se sincroniza lo que haya
# en esa ruta aunque no sea el directorio, y el `fsync` dice «hecho»— y cualquier `OSError`
# sólo AVISABA por stderr con rc=0. Un aviso lo lee una persona; el `rc` lo lee el script.
# Mismo instrumento que el bloque 32: se ejercita el CÓDIGO REAL, extraído por su literal
# (no por número de línea: las líneas se mueven y la cita caduca) y corrido en un
# intérprete al que se le ha quitado el flag o roto el `fsync` DEL DIRECTORIO.
ESCR="$BASE/escritor.py"
awk 'index($0, "LLMI_CUERPO=\"$_BODYF\" python3") { d=1; next }
     d && $0 == "EOPW" { exit }
     d { print }' "$REPO/llmi" > "$ESCR"
if [ -s "$ESCR" ]; then
  bien "⊕ control del instrumento: el escritor se extrae y NO sale vacío ($(wc -l < "$ESCR" | tr -d ' ') líneas)"
  DUR="$BASE/dur"; mkdir -p "$DUR"
  printf '{"token":"tok-DUR","runtime_instance":"rti-d","expires_at":"2099-01-01T00:00:00Z","generation":9}\n' > "$DUR/cuerpo.json"
  # ⊖ a/b/c — TODOS LOS FLAGS SON OBLIGATORIOS, PERO NO CAEN DEL MISMO LADO.
  # 🔑 El `rc` no lo decide el flag: lo decide DÓNDE está la frontera de persistencia.
  #   O_NOFOLLOW · O_CLOEXEC -> se exigen al CREAR el temporal, ANTES del `os.replace`
  #                             ⇒ nada se persistió ⇒ rc=1 y reintentar es correcto
  #   O_DIRECTORY            -> sólo se usa al abrir el padre, DESPUÉS del `os.replace`
  #                             ⇒ el efecto ya está ⇒ rc=8 y reintentar HACE DAÑO
  # Meter los tres en la misma expectativa era mi error, y lo cazó esta misma corrida:
  # daba `1` donde yo esperaba `8` y el `1` era lo CORRECTO.
  _quita_flag() {   # $1=flag  → deja RC y SAL
    _d="$DUR/$1"; mkdir -p "$_d"
    SAL="$( LLMI_SESFILE="$_d/sesion.json" LLMI_CUERPO="$DUR/cuerpo.json" \
            LLMI_TEST_QUITA_FLAG="$1" python3 - "$ESCR" <<'EOPY' 2>&1
import os, sys
delattr(os, os.environ["LLMI_TEST_QUITA_FLAG"])   # la plataforma que no lo tiene
exec(compile(open(sys.argv[1]).read(), sys.argv[1], "exec"), {"__name__": "__main__"})
EOPY
)"; RC=$?
  }
  for _flag in O_NOFOLLOW O_CLOEXEC; do
    _quita_flag "$_flag"
    igual "⊖ sin $_flag (se exige al CREAR) ⇒ rc=1, nada persistido" "1" "$RC"
    contiene "⊖ y dice cuál falta" "$_flag" "$SAL"
    contiene "⊖ y dice que NO crea el fichero" "NO creo el fichero de sesión" "$SAL"
    no_contiene "⊖ y NO usa la copy de durabilidad (no hay nada guardado que proteger)" \
                "SESION_GUARDADA_SIN_DURABILIDAD" "$SAL"
    if [ -e "$DUR/$_flag/sesion.json" ]; then
      mal "⊖ sin $_flag no queda destino" "ausente" "existe"
    else bien "⊖ y el destino NO existe (la frontera no se cruzó)"; fi
  done
  _quita_flag O_DIRECTORY
  igual "⊖ sin O_DIRECTORY (se usa TRAS el replace) ⇒ rc=8, persistido sin acreditar" "8" "$RC"
  contiene "⊖ y dice cuál falta" "O_DIRECTORY" "$SAL"
  contiene "⊖ y NO manda reintentar (un login más abriría una segunda sesión)" "NO repitas" "$SAL"
  contiene "⊖ y lo dice en clave MÁQUINA, no sólo en prosa" "SESION_GUARDADA_SIN_DURABILIDAD" "$SAL"
  contiene "⊖ y el destino SÍ existe (por eso es 8 y no 1)" "tok-DUR" \
           "$(cat "$DUR/O_DIRECTORY/sesion.json" 2>/dev/null)"
  # ⊖ c — EL FALLO DEL fsync YA NO ES UN AVISO. El mutante rompe `fsync` SÓLO sobre un
  # descriptor de DIRECTORIO: el del FICHERO pasa, y por eso el `os.replace` llega a
  # ocurrir. La admisión es CONJUNCIÓN — rc **y** la línea **y** el EFECTO en disco.
  _d="$DUR/fsync"; mkdir -p "$_d"
  SAL="$( LLMI_SESFILE="$_d/sesion.json" LLMI_CUERPO="$DUR/cuerpo.json" \
          python3 - "$ESCR" <<'EOPY' 2>&1
import os, stat, sys
_real = os.fsync
def _rompe_solo_el_directorio(fd):
    if stat.S_ISDIR(os.fstat(fd).st_mode):
        raise OSError(22, "fsync de directorio no soportado")
    return _real(fd)
os.fsync = _rompe_solo_el_directorio
exec(compile(open(sys.argv[1]).read(), sys.argv[1], "exec"), {"__name__": "__main__"})
EOPY
)"; RC=$?
  igual "⊖ con el fsync del PADRE roto ⇒ rc=8 (era rc=0 con ⚠️, luego rc=1 colapsado)" "8" "$RC"
  contiene "⊖ y dice qué no pudo sincronizar" "no pude sincronizar el directorio" "$SAL"
  contiene "⊖ y lo dice en clave MÁQUINA" "SESION_GUARDADA_SIN_DURABILIDAD" "$SAL"
  contiene "⊖ y dice que la sesión es VÁLIDA y está en uso" "VÁLIDA y está en uso" "$SAL"
  contiene "⊖ y NO manda reintentar" "NO repitas" "$SAL"
  contiene "⊖ y autodescribe el rc para el script" "ÉXITO CON RESERVA" "$SAL"
  # ⊕ CONTROL DEL MUTANTE ①: el fallo es del DIRECTORIO y de nadie más. Si hubiera roto el
  # `fsync` del FICHERO, el `os.replace` no se habría ejecutado y el destino NO existiría:
  # que el destino exista CON el token nuevo prueba que se llegó hasta después de publicar.
  contiene "⊕ control: el destino existe y trae el token (se publicó; sólo faltó durar)" \
           "tok-DUR" "$(cat "$_d/sesion.json" 2>/dev/null)"
  # ⊕ CONTROL DEL MUTANTE ②: y no deja temporal huérfano — el `finally` distingue el éxito
  # de la publicación del fallo de la durabilidad.
  if [ -z "$(find "$_d" -name '.sesion-*' 2>/dev/null)" ]; then
    bien "⊕ control: y NO queda ningún temporal huérfano"
  else mal "⊕ sin temporales" "ninguno" "$(find "$_d" -name '.sesion-*')"; fi
  # ⊕ CONTROL DEL BLOQUE: el MISMO escritor, sin tocar nada, guarda y sale 0. Sin esto,
  # «falla cerrado» lo cumpliría un escritor roto que no guarda nunca.
  _d="$DUR/ok"; mkdir -p "$_d"
  SAL="$( LLMI_SESFILE="$_d/sesion.json" LLMI_CUERPO="$DUR/cuerpo.json" \
          python3 - "$ESCR" <<'EOPY' 2>&1
import sys
exec(compile(open(sys.argv[1]).read(), sys.argv[1], "exec"), {"__name__": "__main__"})
EOPY
)"; RC=$?
  igual "⊕ intacto ⇒ rc=0 (el ⊖ no es un escritor muerto)" "0" "$RC"
  contiene "⊕ y el fichero trae el token entero" "tok-DUR" "$(cat "$_d/sesion.json" 2>/dev/null)"
  igual "⊕ y queda en 0600" "600" "$(stat -f%Lp "$_d/sesion.json" 2>/dev/null || stat -c%a "$_d/sesion.json")"
else
  mal "⊕ el escritor se extrae" "un bloque no vacío" "vacío: la extracción no encontró el heredoc"
fi

echo "── 37 LOS DOS BRAZOS DEL FALLO TIENEN rc DISTINTO, Y SE MIDE END-TO-END ──"
# El bloque 36 mide el escritor EXTRAÍDO. Éste mide el BINARIO ENTERO, incluida la
# propagación por el shell — que es justo donde el `if ! … then exit 1` aplastaba el `8`
# contra el `1`. Un rc que vive en la corrida no lo acredita el fuente.
#
# 🔑 Los dos brazos se disparan con PERMISOS DE DIRECTORIO, sin mutantes ni monkeypatch:
#   0500 (r-x) -> no se puede CREAR el temporal        -> falla ANTES de persistir   -> rc=1
#   0300 (-wx) -> crear y `os.replace` SÍ van, pero `os.open(dir, O_RDONLY)` da EACCES
#                 -> falla DESPUÉS del `os.replace`     -> rc=8
# Es conducta observable del sistema de ficheros, no una lectura del código.
_body_ses='{"token":"tok-NUEVO","runtime_instance":"rti-9","expires_at":"2099-01-01T00:00:00Z","generation":7}'

# ── BRAZO A · falla ANTES de persistir ⇒ rc=1 y NO hay sesión nueva
escenario ""; mkdir -p "$CASA/brazoA"; SESA="$CASA/brazoA/sesion.json"
chmod 500 "$CASA/brazoA"
SAL_A="$( PATH="$REPO/tests/falso:$PATH" HOME="$CASA" \
        LLMINBOX_SESSION_FILE="$SESA" LLMINBOX_WORKLOAD_FILE="$CASA/workload.cred" \
        FK_LOG="$FK_LOG" FK_HTTP=200 FK_RC=0 FK_HEALTH_RC=0 FK_BODY="$_body_ses" \
        "$REPO/llmi" login 2>&1 )"; RC_A=$?
chmod 700 "$CASA/brazoA"
igual "⊖A fallo ANTES de persistir ⇒ rc=1" "1" "$RC_A"
if [ -e "$SESA" ]; then mal "⊖A y NO queda sesión nueva" "el destino ausente" "existe: $(cat "$SESA")"
else bien "⊖A y NO queda sesión nueva en el destino (nada se persistió)"; fi
contiene "⊖A y NO dice que esté guardada" "sigue intacta" "$SAL_A"

# ── BRAZO B · falla DESPUÉS del `os.replace` ⇒ rc=8 y la sesión SÍ está
escenario ""; mkdir -p "$CASA/brazoB"; SESB="$CASA/brazoB/sesion.json"
chmod 300 "$CASA/brazoB"
SAL_B="$( PATH="$REPO/tests/falso:$PATH" HOME="$CASA" \
        LLMINBOX_SESSION_FILE="$SESB" LLMINBOX_WORKLOAD_FILE="$CASA/workload.cred" \
        FK_LOG="$FK_LOG" FK_HTTP=200 FK_RC=0 FK_HEALTH_RC=0 FK_BODY="$_body_ses" \
        "$REPO/llmi" login 2>&1 )"; RC_B=$?
chmod 700 "$CASA/brazoB"
igual "⊖B fallo DESPUÉS de persistir ⇒ rc=8 (el shell lo PROPAGA, no lo aplasta)" "8" "$RC_B"
contiene "⊖B y el destino SÍ trae el token nuevo (el efecto está)" "tok-NUEVO" "$(cat "$SESB" 2>/dev/null)"
contiene "⊖B y lo dice en clave máquina" "SESION_GUARDADA_SIN_DURABILIDAD" "$SAL_B"
contiene "⊖B y NO manda reintentar" "NO repitas" "$SAL_B"
# 🩸 EL ASERTO QUE HABRÍA CAZADO EL COLAPSO. Sin esta línea, «rc=1» y «rc=8» pueden ser
# los dos correctos por separado y seguir siendo el MISMO número. Es el falsador que pidió
# @security: comparar el rc de los dos brazos, no mirarlos de uno en uno.
if [ "$RC_A" = "$RC_B" ]; then
  mal "⊖ los dos brazos NO comparten rc" "rc distintos (1 vs 8)" "los dos dan $RC_A"
else bien "⊖ los dos brazos tienen rc DISTINTO ($RC_A vs $RC_B): un script los separa"; fi
# ⊕ CONTROL: con el directorio normal, el MISMO login sale 0. Sin esto, «los brazos fallan»
# lo cumpliría un login que no funciona nunca.
escenario ""; mkdir -p "$CASA/brazoOK"; SESOK="$CASA/brazoOK/sesion.json"
SAL_OK="$( PATH="$REPO/tests/falso:$PATH" HOME="$CASA" \
        LLMINBOX_SESSION_FILE="$SESOK" LLMINBOX_WORKLOAD_FILE="$CASA/workload.cred" \
        FK_LOG="$FK_LOG" FK_HTTP=200 FK_RC=0 FK_HEALTH_RC=0 FK_BODY="$_body_ses" \
        "$REPO/llmi" login 2>&1 )"; RC_OK=$?
igual "⊕ directorio normal ⇒ rc=0 (los ⊖ no son un login muerto)" "0" "$RC_OK"
no_contiene "⊕ y sin aviso de durabilidad" "SESION_GUARDADA_SIN_DURABILIDAD" "$SAL_OK"
# ⊕ CONTROL DE LA TABLA: el `8` está DOCUMENTADO, no sólo emitido. Un código que sale por
# stderr y no está en la ayuda es un número que nadie puede consumir a propósito.
contiene "⊕ y el 8 está en la tabla de salidas de la ayuda" "8 EL EFECTO ESTÁ PERSISTIDO" \
         "$(PATH="$REPO/tests/falso:$PATH" HOME="$CASA" "$REPO/llmi" ayuda 2>&1; \
            PATH="$REPO/tests/falso:$PATH" HOME="$CASA" "$REPO/llmi" --help 2>&1)"

echo "── 38 EL REDACTADO SE CONSTRUYE POR ALLOWLIST CERRADA, NO TAPANDO UN CAMPO ──"
# `d["token"]="<omitido>"` + `json.dumps(d)` es una DENYLIST DE UN ELEMENTO: tapa lo que ya
# se conoce e imprime todo lo demás que mande el servidor — por stdout, que va a logs, a CI
# y a transcripciones. El LECTOR de este mismo binario usa allowlist (`CLAVES`); el escritor
# usaba lo contrario, y la asimetría ERA el defecto (`S-1` de @security).
# El falsador manda un campo que NINGUNA denylist plausible nombraría.
escenario ""; mkdir -p "$CASA/redact"; SESR="$CASA/redact/sesion.json"
SAL="$( PATH="$REPO/tests/falso:$PATH" HOME="$CASA" \
        LLMINBOX_SESSION_FILE="$SESR" LLMINBOX_WORKLOAD_FILE="$CASA/workload.cred" \
        FK_LOG="$FK_LOG" FK_HTTP=200 FK_RC=0 FK_HEALTH_RC=0 \
        FK_BODY='{"token":"tok-NUEVO","runtime_instance":"rti-9","expires_at":"2099-01-01T00:00:00Z","generation":7,"refresh_token":"SECRETO-REFRESH","capabilities":["event_writer"],"pepper":"SECRETO-PEPPER"}' \
        "$REPO/llmi" login --json 2>&1 )"; RC=$?
SAL_OUT="$( PATH="$REPO/tests/falso:$PATH" HOME="$CASA" \
        LLMINBOX_SESSION_FILE="$CASA/redact/s2.json" LLMINBOX_WORKLOAD_FILE="$CASA/workload.cred" \
        FK_LOG="$FK_LOG" FK_HTTP=200 FK_RC=0 FK_HEALTH_RC=0 \
        FK_BODY='{"token":"tok-NUEVO","runtime_instance":"rti-9","expires_at":"2099-01-01T00:00:00Z","generation":7,"refresh_token":"SECRETO-REFRESH","capabilities":["event_writer"],"pepper":"SECRETO-PEPPER"}' \
        "$REPO/llmi" login --json 2>/dev/null )"
igual "login --json con campos de más ⇒ rc=0" "0" "$RC"
no_contiene "⊖ el refresh_token NO sale por stdout" "SECRETO-REFRESH" "$SAL_OUT"
no_contiene "⊖ el pepper NO sale por stdout" "SECRETO-PEPPER" "$SAL_OUT"
no_contiene "⊖ ni el nombre del campo desconocido va en stdout" "refresh_token" "$SAL_OUT"
no_contiene "⊖ y el token tampoco" "tok-NUEVO" "$SAL_OUT"
# ⊕ CONTROL ①: los CUATRO campos permitidos SÍ salen. Sin esto, «no filtra» lo cumpliría un
# formateador que no imprime nada — que es el modo más fácil de pasar un test de fuga.
contiene "⊕ control: runtime_instance SÍ sale" "rti-9" "$SAL_OUT"
contiene "⊕ control: expires_at SÍ sale" "2099-01-01" "$SAL_OUT"
contiene "⊕ control: generation SÍ sale" "generation" "$SAL_OUT"
contiene "⊕ control: y el token sale REDACTADO, no ausente" "omitido" "$SAL_OUT"
# ⊕ CONTROL ②: sigue siendo JSON válido y con EXACTAMENTE cuatro claves.
igual "⊕ control: la salida es JSON con 4 claves exactas" "4" \
      "$(printf '%s' "$SAL_OUT" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))' 2>/dev/null)"
# ⊕ CONTROL ③: de los que sobran se dice el NOMBRE por stderr — informar sin publicar.
contiene "⊕ control: por stderr se avisa de los campos no emitidos, por NOMBRE" "no emito" "$SAL"

echo "── 39 EL VERBO QUE PUBLICA NO EMITE NI UNA PETICIÓN ANTES DE VALIDAR LA SESIÓN ──"
# `post` → `_aviso_identidad` → `_yo_y_carril` → `_carriles_conocidos` → `curl /carriles`
# CON EL TOKEN LEGADO, y sólo DESPUÉS `_post_nativo` auditaba la sesión. Con el fichero de
# sesión INSEGURO, el CLI ya había emitido tráfico cuando el cargador dice «⛔ NADA se ha
# enviado». La frase tiene que ser LITERAL. (`P0-B` de @design, P2 de @security.)
# ⚠️ Hace falta el `tmux` falso: sin él `_yo_y_carril` sale en su primera línea y el defecto
# es INALCANZABLE — por eso ningún bloque anterior lo cazó.
#
# 🩸 Y NO BASTA CON EL PATH: `_sesion_tmux` EXIGE `$TMUX` NO VACÍO **ANTES** DE LLAMAR AL
# BINARIO. Poner `tests/falso-tmux` en el PATH y no fijar `TMUX` deja el bloque colgando del
# AMBIENTE: en `fc50a410` (465 comprobaciones), dentro de una pestaña tmux el operador hereda
# `TMUX` y salían 465/465; desde un shell fuera de tmux la variable está vacía,
# `_sesion_tmux` corta en su primera línea, y los dos ⊕ de abajo se ponen ROJOS
# — que es exactamente lo que pasó y lo que este comentario
# existe para que no vuelva a pasar.
# Lo grave no es que el ⊕ falle: es que el ⊖ («0 peticiones») pasaba VACÍO — sin tráfico
# porque la identidad cortocircuita, no porque la puerta funcione. Un ⊖ cuyo verde depende
# de la pestaña en la que corres no mide el sujeto, mide dónde estás sentado.
# ⇒ El bloque FIJA su propio `TMUX` sintético en LOS DOS comandos, y no lee ninguno de fuera.
# El VALOR no lo mira nadie: el único lector de `$TMUX` en todo el árbol es `llmi:384`
# (`[ -n "${TMUX:-}" ] || return 1`) y el `tmux` falso lo ignora — censo:
#   grep -n 'TMUX' llmi tests/falso-tmux/* ⇒ 1 lectura, y es esa.
# Por eso es un LITERAL y no `"$CASA/..."`: colgarlo de `$CASA` lo ataría al escenario
# ANTERIOR (el `escenario ""` que lo renueva vive DENTRO de `_bloque39`, después de esta
# línea), y una ruta de otro escenario se lee como si nombrara el de aquí sin nombrarlo.
_TMUX_SINT="/no-existe/tmux-sintetico-bloque39,0,0"

# 🩸 EL CUERPO ENTRA POR FICHERO, NO POR `printf | llmi`, Y NO ES ESTILO. Con el mutante de
# `P0-B` puesto (`_aviso_identidad` DELANTE de la puerta) la ruta que se adelanta consume el
# extremo de la tubería y el `cat > "$CUERPOF"` de `post` se queda esperando un stdin que ya
# no va a llegar: el mutante NO se pone rojo, se CUELGA. Y colgado es peor que verde, porque
# el runner de mutantes mata por tope de tiempo y cuenta `fallos=0` sobre un registro
# truncado — o sea, lo lee como «arnés ciego» cuando el arnés sí discrimina. Medido: 6 min
# colgado en `cat`, con el ⊖ del tráfico YA en rojo por encima. Con `< "$CUERPO"` el stdin es
# un fichero regular: quien lo lea de más lee EOF, el verbo llega al aserto de tráfico y
# muere ahí. El falsador tiene que ser DETERMINISTA, no sólo correcto.
#
# El par ⊖/⊕ completo, parametrizado por CÓMO ESTÁ EL AMBIENTE al entrar. Se corre dos veces
# —sin `TMUX` y con un `TMUX` AJENO— y las dos tienen que dar lo MISMO: eso es la hermeticidad,
# y es una propiedad que sólo se puede afirmar midiéndola en los dos ambientes.
_bloque39() {   # $1 = etiqueta del ambiente   $2..$n = prefijo `env` que fabrica ese ambiente
  local etq="$1"; shift
  local amb=("$@") rc calls
  escenario ""; sesion; chmod 644 "$CASA/sesion.json"   # ⊖ el fichero de sesión es INSEGURO
  limpia_log; cuerpo "cuerpo"
  SAL="$( "${amb[@]}" \
          TMUX="$_TMUX_SINT" \
          PATH="$REPO/tests/falso-tmux:$REPO/tests/falso:$PATH" HOME="$CASA" \
          LLMINBOX_SESSION_FILE="$CASA/sesion.json" LLMINBOX_WORKLOAD_FILE="$CASA/workload.cred" \
          LLMI_MOUNTS="$CASA/mounts.json" LLMI_LEDGER=pruebas BIK_CARRIL=pruebas \
          LLMI_ROSTER="$REPO/roster.example.json" LLMINBOX_ROSTER="$REPO/roster.example.json" \
          FK_LOG="$FK_LOG" FK_HTTP=200 FK_RC=0 FK_HEALTH_RC=0 \
          "$REPO/llmi" post fe equipo FINDING "titular" < "$CUERPO" 2>&1 )"; rc=$?
  igual "[$etq] ⊖ sesión insegura + post ⇒ rc=1 (el cargador para)" "1" "$rc"
  igual "[$etq] ⊖ y PETICIONES YA SALIDAS = 0" "0" "$(grep -c '^CALL' "$FK_LOG")"
  contiene "[$etq] ⊖ y la frase «nada se ha enviado» es LITERAL" "NADA se ha enviado" "$SAL"
  # ⊕ CONTROL DEL INSTRUMENTO: con el MISMO tmux falso y una sesión SEGURA, `post` SÍ produce
  # tráfico. Sin este control el «0» de arriba sería un cero por ceguera.
  escenario ""; sesion; limpia_log; cuerpo "cuerpo"
  "${amb[@]}" \
      TMUX="$_TMUX_SINT" \
      PATH="$REPO/tests/falso-tmux:$REPO/tests/falso:$PATH" HOME="$CASA" \
      LLMINBOX_SESSION_FILE="$CASA/sesion.json" LLMINBOX_WORKLOAD_FILE="$CASA/workload.cred" \
      LLMI_MOUNTS="$CASA/mounts.json" LLMI_LEDGER=pruebas BIK_CARRIL=pruebas \
      LLMI_ROSTER="$REPO/roster.example.json" LLMINBOX_ROSTER="$REPO/roster.example.json" \
      FK_LOG="$FK_LOG" FK_HTTP=200 FK_RC=0 FK_HEALTH_RC=0 FK_BODY='{"event_id":"e1","receipt_id":"r1","replayed":false}' \
      "$REPO/llmi" post fe equipo FINDING "titular" < "$CUERPO" >/dev/null 2>&1
  calls="$(grep -c '^CALL' "$FK_LOG")"
  if [ "$calls" -ge 1 ]; then
    bien "[$etq] ⊕ control: con sesión SEGURA el mismo post SÍ emite ($calls llamadas) — el 0 era conducta"
  else mal "[$etq] ⊕ con sesión segura post emite" "≥1 llamada" "0: el instrumento no discrimina"; fi
  # ⊕ CONTROL ②: y el tmux falso SÍ se está usando — si no, `/carriles` no aparecería nunca.
  if grep -q 'carriles' "$FK_LOG"; then
    bien "[$etq] ⊕ control: el tmux falso surte efecto (hay una llamada a /carriles que antes iba PRE-AUTH)"
  else mal "[$etq] ⊕ el tmux falso surte efecto" "una llamada a /carriles" "$(cat "$FK_LOG")"; fi
  _B39_CALLS="$calls"
}

# ── FALSADOR DE AMBIENTE. Los dos extremos reales: el shell del operador FUERA de tmux
# (`env -u TMUX`, que es como se reprodujo el falso verde) y DENTRO (TMUX heredado, aquí uno
# AJENO a propósito para que no pueda confundirse con el sintético del bloque).
_bloque39 "sin TMUX" env -u TMUX
_B39_SIN="$_B39_CALLS"
_bloque39 "TMUX ajeno" env TMUX=/ambiente/heredado/ajeno,9,9
_B39_CON="$_B39_CALLS"
# 🔑 EL ASERTO QUE HABRÍA CAZADO EL FALSO VERDE. Cada ambiente por separado puede salir
# verde y aun así el bloque seguir colgando del entorno: lo que lo prueba es que el número
# medido NO SE MUEVA entre los dos.
igual "⊕ HERMÉTICO: el mismo número de llamadas con y sin TMUX en el ambiente" \
      "$_B39_SIN" "$_B39_CON"

echo "── 40 EL REVOKE NO BORRA POR PATHNAME EL FICHERO QUE YA VALIDÓ ──"
# `rm -f "$SESFILE"` volvía a mirar por NOMBRE lo que el cargador había auditado por
# DESCRIPTOR. Entre las dos cosas hay una ventana, y quien la gane elige qué fichero se
# borra — en el único camino que DESTRUYE. El falsador pone un enlace en la ruta: con `rm`
# se borraría el enlace (y con un `rm` que siguiera enlaces, el destino).
# 🩸 EL FALSADOR TIENE QUE CRUZAR LA VENTANA, NO DESCRIBIRLA. Mi primera versión ponía un
# enlace en la ruta DESDE EL PRINCIPIO: el cargador lo rechaza con ELOOP y la corrida ni
# llega al borrado, así que el ⊖ salía verde con la cura Y sin ella. Lo cazó el mutante M3,
# que sobrevivió — un ⊖ que no mata a su mutante no está midiendo su sujeto.
# Aquí la sustitución ocurre DENTRO de la corrida, en la llamada a la pasarela: la sesión se
# carga y se AUDITA bien, y sólo después el impostor ocupa la ruta. Es la ventana exacta.
escenario ""; mkdir -p "$CASA/rev"; SESREV="$CASA/rev/sesion.json"
printf '{"token":"tok-sesion","runtime_instance":"rti-1","expires_at":"2099-01-01T00:00:00Z","generation":3}\n' > "$SESREV"
chmod 600 "$SESREV"
printf 'NO-ME-BORRES\n' > "$CASA/rev/impostor.txt"; chmod 644 "$CASA/rev/impostor.txt"
SWAPLOG2="$CASA/rev/swap.log"; : > "$SWAPLOG2"
SAL="$( PATH="$REPO/tests/falso:$PATH" HOME="$CASA" \
        LLMI_TEST_SWAP_EN_LLAMADA="$CASA/rev/impostor.txt|$SESREV" \
        LLMI_TEST_SWAP_LOG="$SWAPLOG2" \
        LLMINBOX_SESSION_FILE="$SESREV" LLMINBOX_WORKLOAD_FILE="$CASA/workload.cred" \
        FK_LOG="$FK_LOG" FK_HTTP=200 FK_RC=0 FK_HEALTH_RC=0 \
        "$REPO/llmi" session revoke 2>&1 )"; RC=$?
# ⊕ CONTROL DEL INSTRUMENTO, PRIMERO: si el swap no ocurrió, todo lo de abajo es un verde
# por ceguera — que es exactamente cómo la versión anterior de este bloque pasaba.
if [ -s "$SWAPLOG2" ]; then bien "⊕ control: el swap OCURRIÓ dentro de la corrida ($(head -1 "$SWAPLOG2" | cut -c1-40)…)"
else mal "⊕ el swap ocurre" "una línea en el log" "vacío: el ⊖ no tiene sujeto"; fi
igual "⊖ revocar sigue saliendo 0 (el servidor SÍ revocó)" "0" "$RC"
if [ -n "$(llamada '/sessions/current')" ]; then
  bien "⊕ revoke selecciona la sesión propia por bearer (/sessions/current)"
else mal "⊕ revoke usa /sessions/current" "DELETE /sessions/current" "$(llamada sessions)"; fi
if [ -n "$(llamada '/sessions/rti-1')" ]; then
  mal "⊖ revoke NO manda runtime_instance en la ruta" "ninguna /sessions/rti-1" "$(llamada '/sessions/rti-1')"
else bien "⊖ revoke no abre un segundo selector con runtime_instance"; fi
if [ -e "$SESREV" ]; then
  bien "⊖ y el fichero que ocupó la ruta NO se borra (no es el que se validó)"
else mal "⊖ el impostor NO se borra" "sigue en la ruta" "borrado: se destruyó lo que no se auditó"; fi
contiene "⊖ y lo dice por stderr, con motivo" "no borro" "$SAL"
contiene "⊖ y NO afirma un borrado que no ocurrió" "NO se ha borrado" "$SAL"
# ⊕ CONTROL: el caso HONESTO sí borra. Sin él, «no borra el enlace» lo cumple un revoke que
# no borra nunca — que es exactamente cómo se pasa este test por accidente.
escenario ""; mkdir -p "$CASA/rev2"; SESV="$CASA/rev2/sesion.json"
printf '{"token":"tok-sesion","runtime_instance":"rti-1","expires_at":"2099-01-01T00:00:00Z","generation":3}\n' > "$SESV"
chmod 600 "$SESV"
SAL2="$( PATH="$REPO/tests/falso:$PATH" HOME="$CASA" \
        LLMINBOX_SESSION_FILE="$SESV" LLMINBOX_WORKLOAD_FILE="$CASA/workload.cred" \
        FK_LOG="$FK_LOG" FK_HTTP=200 FK_RC=0 FK_HEALTH_RC=0 \
        "$REPO/llmi" session revoke 2>&1 )"; RC2=$?
igual "⊕ control: el revoke honesto ⇒ rc=0" "0" "$RC2"
if [ -e "$SESV" ]; then mal "⊕ el revoke honesto SÍ borra" "el fichero ausente" "sigue ahí"
else bien "⊕ control: el revoke honesto SÍ borra el fichero (el ⊖ no es un revoke muerto)"; fi
contiene "⊕ y lo dice" "borrada" "$SAL2"

echo "── 41 ADMISSION USA SESIÓN EFÍMERA, WIRE EXACTO Y REVOKE ACREDITADO ──"
ADM_CLOSED='{"admissions":[{"lane":"pruebas","verb":"events.accept","state":"closed","epoch":0},{"lane":"pruebas","verb":"outbox.requeue","state":"closed","epoch":0}]}'
ADM_OPEN='{"admissions":[{"lane":"pruebas","verb":"events.accept","state":"open","epoch":1},{"lane":"pruebas","verb":"outbox.requeue","state":"open","epoch":1}]}'
WHO_OP='{"principal":"pilot-operator","role":"infra","lane":"pruebas","capabilities":["admission_operator"]}'
OPEN_OP='{"token":"operator-session-secret","runtime_instance":"rti-op","expires_at":"2099","generation":1}'

# ⊖ La credencial privilegiada se valida antes de tocar siquiera /health.
escenario ""; operador; chmod 644 "$CASA/operator.cred"; limpia_log
corre admission status
igual "⊖ admission rechaza OPFILE 0644" "1" "$RC"
igual "⊖ OPFILE inseguro produce cero red" "0" "$(grep -c '^CALL' "$FK_LOG")"

# ⊕ Camino honesto de lectura y revocación exacta 204.
escenario ""; operador
FK_OPEN_HTTP=201; FK_OPEN_BODY="$OPEN_OP"; FK_WHOAMI_BODY="$WHO_OP"
FK_GET_BODY="$ADM_CLOSED"; FK_REVOKE_HTTP=204
corre admission status
igual "⊕ admission status válido sale 0" "0" "$RC"
contiene "⊕ status muestra el carril derivado" "carril: pruebas" "$SAL"
if grep -q 'U:.*sessions/current.*M:DELETE' "$FK_LOG"; then
  bien "⊕ la sesión operator se revoca por /sessions/current"
else mal "⊕ revoke efímero" "DELETE /sessions/current" "$(cat "$FK_LOG")"; fi
if grep -q 'A:Authorization: Bearer operator-session-secret' "$FK_LOG"; then
  mal "⊖ token operator nunca va en argv" "sin A:Authorization" "$(cat "$FK_LOG")"
else bien "⊖ token operator nunca va en argv"; fi
no_contiene "⊖ token operator nunca se imprime" "operator-session-secret" "$SAL"

# ⊕ Escritura honesta: el resultado acredita destino y epoch+1.
escenario ""; operador
FK_OPEN_HTTP=201; FK_OPEN_BODY="$OPEN_OP"; FK_WHOAMI_BODY="$WHO_OP"
FK_GET_BODY="$ADM_CLOSED"; FK_ADM_HTTP=200; FK_ADM_BODY="$ADM_OPEN"; FK_REVOKE_HTTP=204
corre admission open ROLLOUT
igual "⊕ admission open exacto sale 0" "0" "$RC"
contiene "⊕ sólo anuncia éxito tras postcondición" "✓ admission open" "$SAL"

# ⊖ Un 200 con forma válida pero sin el efecto pedido no es éxito.
escenario ""; operador
FK_OPEN_HTTP=201; FK_OPEN_BODY="$OPEN_OP"; FK_WHOAMI_BODY="$WHO_OP"
FK_GET_BODY="$ADM_CLOSED"; FK_ADM_HTTP=200; FK_ADM_BODY="$ADM_CLOSED"; FK_REVOKE_HTTP=204
corre admission open ROLLOUT
igual "⊖ POST 200 sin target/epoch+1 sale 7" "7" "$RC"
no_contiene "⊖ no imprime éxito sobre efecto falso" "✓ admission" "$SAL"

# ⊖/⊕ La revocación exige la conjunción curl rc=0 + HTTP exacto 204.
escenario ""; operador
FK_OPEN_HTTP=201; FK_OPEN_BODY="$OPEN_OP"; FK_WHOAMI_BODY="$WHO_OP"; FK_GET_BODY="$ADM_CLOSED"
FK_REVOKE_HTTP=200
corre admission status
igual "⊖ revoke HTTP 200 escala sesión posiblemente viva" "8" "$RC"
contiene "⊖ revoke 200 deja aviso operativo" "ADMISSION_SESION_NO_REVOCADA" "$SAL"

escenario ""; operador
FK_OPEN_HTTP=201; FK_OPEN_BODY="$OPEN_OP"; FK_WHOAMI_BODY="$WHO_OP"; FK_GET_BODY="$ADM_CLOSED"
FK_REVOKE_HTTP=204; FK_REVOKE_RC=56
corre admission status
igual "⊖ revoke 204 con curl rc!=0 escala" "8" "$RC"
contiene "⊖ revoke ambiguo no queda mudo" "ADMISSION_SESION_NO_REVOCADA" "$SAL"

escenario ""; operador
FK_OPEN_HTTP=201; FK_OPEN_BODY="$OPEN_OP"; FK_WHOAMI_BODY="$WHO_OP"; FK_GET_BODY="$ADM_CLOSED"
FK_REVOKE_HTTP=204; FK_REVOKE_RC=0
corre admission status
igual "⊕ revoke 204 honesto conserva rc0" "0" "$RC"

echo "── 42 ROLLBACK NO RECOMPONE FOTOS NI CERTIFICA UN 200 MENTIROSO ──"
RB_STATUS_CLOSED='{"lane":"pruebas","admissions":[{"lane":"pruebas","verb":"events.accept","state":"closed","epoch":2},{"lane":"pruebas","verb":"outbox.requeue","state":"closed","epoch":2}],"outbox":{"lane":"pruebas","pending":0,"failed":0,"unresolved":0},"durable_v":6,"certifiable":false}'
RB_CERT='{"lane":"pruebas","admissions":[{"lane":"pruebas","verb":"events.accept","state":"sealed","epoch":3},{"lane":"pruebas","verb":"outbox.requeue","state":"sealed","epoch":3}],"outbox":{"lane":"pruebas","pending":0,"failed":0,"unresolved":0},"durable_v":6,"certified_at":"2026-09-07T02:00:00.000000Z"}'
RB_CERT_FALSE='{"lane":"pruebas","admissions":[{"lane":"pruebas","verb":"events.accept","state":"closed","epoch":2},{"lane":"pruebas","verb":"outbox.requeue","state":"closed","epoch":2}],"outbox":{"lane":"pruebas","pending":0,"failed":0,"unresolved":0},"durable_v":6,"certified_at":"2026-09-07T02:00:00.000000Z"}'

# ⊕ La foto sale de UN endpoint atómico; el cliente no vuelve a GET /admission.
escenario ""; operador
FK_OPEN_HTTP=201; FK_OPEN_BODY="$OPEN_OP"; FK_WHOAMI_BODY="$WHO_OP"
FK_ROLLBACK_STATUS_HTTP=200; FK_ROLLBACK_STATUS_BODY="$RB_STATUS_CLOSED"; FK_REVOKE_HTTP=204
corre admission rollback-status
igual "⊕ rollback-status válido sale 0" "0" "$RC"
contiene "⊕ rollback-status muestra que aún no certifica" "certificable:    no" "$SAL"
if grep -q 'U:.*operator/rollback/status.*M:GET' "$FK_LOG" && \
   ! grep -q 'U:.*operator/admission ' "$FK_LOG"; then
  bien "⊕ rollback-status usa una única foto server-side, no recompone GETs"
else mal "⊕ snapshot atómico" "GET /operator/rollback/status y ningún /operator/admission" "$(cat "$FK_LOG")"; fi

# ⊖ HTTP 200 no basta: si las puertas no están sealed, NO es certificado.
escenario ""; operador
FK_OPEN_HTTP=201; FK_OPEN_BODY="$OPEN_OP"; FK_WHOAMI_BODY="$WHO_OP"
FK_ROLLBACK_CERT_HTTP=200; FK_ROLLBACK_CERT_BODY="$RB_CERT_FALSE"; FK_REVOKE_HTTP=204
corre admission rollback-certify
igual "⊖ certificado 200 que no demuestra sealed sale 7" "7" "$RC"
no_contiene "⊖ no anuncia un certificado falso" "✓ rollback certificado" "$SAL"

# ⊕ Certificado honesto: POST estrecho, forma exacta, mismo carril y revocación.
escenario ""; operador
FK_OPEN_HTTP=201; FK_OPEN_BODY="$OPEN_OP"; FK_WHOAMI_BODY="$WHO_OP"
FK_ROLLBACK_CERT_HTTP=200; FK_ROLLBACK_CERT_BODY="$RB_CERT"; FK_REVOKE_HTTP=204
corre admission rollback-certify
igual "⊕ certificado sealed+cero sale 0" "0" "$RC"
contiene "⊕ sólo entonces anuncia certificado" "✓ rollback certificado" "$SAL"
if grep -q 'U:.*operator/rollback/certify.*M:POST' "$FK_LOG"; then
  bien "⊕ rollback-certify usa el endpoint POST dedicado"
else mal "⊕ endpoint de certificado" "POST /operator/rollback/certify" "$(cat "$FK_LOG")"; fi
if grep -q 'U:.*sessions/current.*M:DELETE' "$FK_LOG"; then
  bien "⊕ rollback también revoca la sesión operator efímera"
else mal "⊕ revoke tras rollback" "DELETE /sessions/current" "$(cat "$FK_LOG")"; fi

echo
if [ "$MALOS" = "0" ]; then echo "✅ $TOTAL comprobaciones, 0 fallos"; exit 0
else echo "❌ $MALOS de $TOTAL comprobaciones FALLAN"; exit 1; fi
