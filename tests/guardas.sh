#!/usr/bin/env bash
# ── LAS GUARDAS DEL ARNÉS, PROBADAS CON SU ENTRADA MALA ───────────────────────
#
# Por qué existe este fichero, con su fecha y su coste: el 2026-08-11, ocho
# comprobaciones del humo llevaban DOS DÍAS certificando en verde una reconstrucción
# del índice que nunca ocurría. Los pasos de preparación reventaban en Linux y el
# bloque seguía corriendo. Y los 16 mutantes que este repo mantiene no lo vieron
# nunca, por una razón estructural: **los 16 mutan `servicio.py` o `ledger_parse.py`
# — el PRODUCTO. Nadie mutaba el ANDAMIO.** Un mutante no encuentra una puerta que no
# se llega a ejecutar.
#
# Así que aquí no se prueba el producto: se prueba que **las guardas del arnés saben
# decir que NO** cuando les llega su entrada mala. Cada una con su control positivo al
# lado, porque una guarda que dice «mal» a todo tampoco sirve.
#
# Rápido a propósito (<1 min, sin levantar el servicio): una prueba de los
# instrumentos que tarda como la de la corrección se salta igual que ella.
set -uo pipefail
cd "$(dirname "$0")/.."
IMAGEN="${IMAGEN:-llminbox:test}"
FALLOS=0
. tests/guardas.lib.sh
aviso_imagen_rancia

# `ok`/`fallo`/`no_medido` escriben en FALLOS, así que las pruebas de ABAJO no pueden
# usarlas para juzgar: se juzgarían con lo que están midiendo. Contadores aparte.
MALOS=0
bien() { printf "  ✓ %s\n" "$1"; }
mal()  { printf "  ✗ %s\n     esperado: %s · obtenido: %s\n" "$1" "$2" "$3"; MALOS=$((MALOS+1)); }
igual(){ [ "$2" = "$3" ] && bien "$1" || mal "$1" "$2" "$3"; }

echo "── un paso de preparación que falla NO puede pasar desapercibido ──"
# La avería exacta del 2026-08-09: `set -uo pipefail` sin `-e`, un `python3 -c` que
# revienta, y el bloque siguiendo como si nada. 5 verdes falsos.
SAL=$( FALLOS=0; paso "un paso inventado" false 2>&1; echo "rc=$?" )
case "$SAL" in *"NO SE MIDIÓ"*) bien "un paso que falla se declara NO MEDIDO" ;;
               *) mal "un paso que falla se declara NO MEDIDO" "NO SE MIDIÓ" "$SAL" ;; esac
case "$SAL" in *"rc=1"*) bien "y devuelve 1, para que quien llama pueda cortar" ;;
               *) mal "y devuelve 1" "rc=1" "$SAL" ;; esac
# CONTROL POSITIVO: si dijera «no medido» también con un paso BUENO, no distinguiría nada.
SAL=$( FALLOS=0; paso "un paso que va" true 2>&1; echo "rc=$?" )
case "$SAL" in *"NO SE MIDIÓ"*) mal "un paso que VA no dice nada" "silencio" "$SAL" ;;
               *"rc=0"*) bien "y un paso que VA pasa callado (la guarda no muerde a todo)" ;;
               *) mal "un paso que VA devuelve 0" "rc=0" "$SAL" ;; esac

echo "── «no medido» tiene que CONTAR como fallo ──"
# Si no contara, romper el transporte apagaría la suite entera: la avería que
# elegiría alguien con prisa por poner el CI en verde.
N=$( FALLOS=0; no_medido "x" "y" >/dev/null; echo "$FALLOS" )
igual "un NO MEDIDO incrementa el contador de fallos" "1" "$N"

echo "── comparar contra el vacío no es comparar ──"
SAL=$( FALLOS=0; comp "algo" "" "" 2>&1 )
case "$SAL" in *"NO SE MIDIÓ"*) bien "dos lados vacíos NO se confirman entre sí" ;;
               *) mal "dos lados vacíos dan NO MEDIDO" "NO SE MIDIÓ" "$SAL" ;; esac
SAL=$( FALLOS=0; comp "algo" "5" "" 2>&1 )
case "$SAL" in *"NO SE MIDIÓ"*) bien "y un lado vacío tampoco es «otro valor»" ;;
               *) mal "un lado vacío da NO MEDIDO" "NO SE MIDIÓ" "$SAL" ;; esac
SAL=$( FALLOS=0; comp "algo" "5" "5" 2>&1 )
case "$SAL" in *"✓"*) bien "y dos valores iguales sí comparan (no muerde a todo)" ;;
               *) mal "dos iguales dan verde" "✓" "$SAL" ;; esac

echo "── la espera a /health sabe agotarse ──"
# Un puerto donde no hay nadie. Antes eran bucles `for … && break` sin brazo de `||`:
# se agotaban EN SILENCIO y lo que venía después medía un servicio ausente.
SAL=$( FALLOS=0; esperar_salud "http://127.0.0.1:1" 2 "un servicio que no existe" 2>&1; echo "rc=$?" )
case "$SAL" in *"NO SE MIDIÓ"*) bien "contra un puerto muerto se declara NO MEDIDO" ;;
               *) mal "puerto muerto da NO MEDIDO" "NO SE MIDIÓ" "$SAL" ;; esac
case "$SAL" in *"rc=1"*) bien "y devuelve 1" ;; *) mal "y devuelve 1" "rc=1" "$SAL" ;; esac

echo "── junto a la base no puede haber ficheros de otro uid ──"
# ⚠️ ALCANCE: sobre bind-mounts de macOS esto sería INERTE (Docker Desktop virtualiza
# la propiedad). Por eso la prueba usa un VOLUMEN NOMBRADO, donde la propiedad es real
# igual que en el runner de Linux: la guarda se ejercita de verdad en las dos
# plataformas. Es la guarda que el 2026-08-11 no existía, y la que habría convertido
# tres corridas de CI en una.
VOL="guardas-$$"
if docker volume create "$VOL" >/dev/null 2>&1; then
  docker run --rm -u 0 -v "$VOL:/datos" "$IMAGEN" sh -c \
    'python3 -c "open(\"/datos/h.sqlite\",\"w\").write(\"x\")" && chown 1000:1000 /datos/h.sqlite && chmod 777 /datos' >/dev/null 2>&1
  # ① control POSITIVO primero: sin intrusos tiene que callarse. Si esto falla, el ✗
  #    de abajo no probaría nada — sería una guarda que se queja de todo.
  if ajenos_en_datos "$VOL" >/dev/null 2>&1; then
    bien "sin intrusos, la guarda calla"
  else
    mal "sin intrusos la guarda calla" "silencio + rc=0" "$(ajenos_en_datos "$VOL" 2>&1)"
  fi
  # ② y ahora el intruso: un fichero de root al lado de una base del uid 1000.
  docker run --rm -u 0 -v "$VOL:/datos" "$IMAGEN" \
    python3 -c 'open("/datos/intruso","w").write("x")' >/dev/null 2>&1
  SAL=$(ajenos_en_datos "$VOL" 2>&1; echo "rc=$?")
  case "$SAL" in *"intruso(uid=0)"*) bien "con un fichero de root al lado, lo NOMBRA" ;;
                 *) mal "nombra al intruso" "intruso(uid=0)" "$SAL" ;; esac
  case "$SAL" in *"rc=1"*) bien "y devuelve 1, para declarar el bloque NO MEDIDO" ;;
                 *) mal "y devuelve 1" "rc=1" "$SAL" ;; esac
  docker volume rm "$VOL" >/dev/null 2>&1
else
  mal "la guarda de propiedad" "un volumen para probarla" "docker volume create falló"
fi

echo "── escribir la base del servicio se hace COMO SU DUEÑO ──"
# El uid no se elige: se lee del fichero. Se prueba con un dueño RARO (4242), que no
# es ni root ni el `USER` de la imagen: si la función tuviera cableado cualquiera de
# los dos, aquí se rompe. Las dos opciones «de sentido común» fallaron en producción.
VOL2="guardas2-$$"
if docker volume create "$VOL2" >/dev/null 2>&1; then
  docker run --rm -u 0 -v "$VOL2:/datos" "$IMAGEN" sh -c \
    'python3 -c "open(\"/datos/h.sqlite\",\"w\").write(\"\")" && chown 4242:4242 /datos/h.sqlite && chmod 755 /datos && chmod 600 /datos/h.sqlite' >/dev/null 2>&1
  QUIEN=$(en_contenedor "$VOL2" "import os; print(os.getuid())" 2>/dev/null)
  igual "escribe con el uid que es DUEÑO de la base, no con el suyo" "4242" "$QUIEN"
  # Y que además pueda escribirla de verdad: un 0600 de 4242 no lo toca nadie más.
  SAL=$(en_contenedor "$VOL2" "
open('/datos/h.sqlite','w').write('tocado')
print(open('/datos/h.sqlite').read())" 2>&1)
  igual "y la escribe de verdad (0600 de un dueño ajeno)" "tocado" "$(printf '%s' "$SAL" | tail -1)"
  docker volume rm "$VOL2" >/dev/null 2>&1
else
  mal "el descubridor de dueño" "un volumen para probarlo" "docker volume create falló"
fi

echo
# ── El contrato de la flota no puede evaporarse entre dos arranques ──────────────
# `LLMINBOX_POLICY_DIR` exportada NO sobrevive: cada `llmi up` es una shell nueva. Sin
# persistencia, `campos_origen` volvia de `politica` a `ausente` en el arranque siguiente,
# en silencio, y el gate de alineacion de la flota se caia sin que nadie tocara codigo.
# Mismo modo de fallo que ya mordio con el token del watcher.
#
# Estas dos lineas vivieron un rato DESPUES del `exit` de este fichero, o sea como codigo
# muerto que daba verde sin ejecutarse nunca. Van aqui, en el acumulador, por eso.
grep -q 'ENVFILE=' llmi \
  && bien "llmi lee un fichero unico de configuracion del operador" \
  || mal "llmi lee un fichero unico de configuracion del operador" "ENVFILE" "ausente: una variable exportada no sobrevive al siguiente arranque, y un dotfile por perilla no es consolidar"
grep -q 'no empieza por LLMINBOX_' llmi \
  && bien "y rechaza una variable ajena en ese fichero" \
  || mal "y rechaza una variable ajena en ese fichero" "guarda de prefijo" "ausente: una variable que nadie lee se queda puesta creyendo que aplica"
grep -q 'PDIRFILE=' llmi \
  && bien "llmi persiste donde esta la politica" \
  || mal "llmi persiste donde esta la politica" "PDIRFILE" "ausente: una variable exportada no sobrevive al siguiente arranque"
grep -q 'no es un directorio' llmi \
  && bien "llmi para si la politica apunta a la nada" \
  || mal "llmi para si la politica apunta a la nada" "guarda de ruta" "ausente: configurado-y-roto pasaria por sin-configurar"

echo
# ── Dos fallos que salian con codigo 0 ────────────────────────────────────────────
# Guardas de COMPORTAMIENTO, no `grep`: se ejecuta el codigo real en un sandbox y se
# mira el CODIGO DE SALIDA. Un `grep` de "return 1" habria pasado con el arreglo mal
# puesto — que es justo como llego el defecto.
#
# VAN AQUI, en el acumulador y ANTES del exit: dos guardas de este mismo fichero
# vivieron un rato DESPUES del `exit`, o sea codigo muerto dando verde. Tercera vez que
# pasa en este repo; por eso esta escrito.
echo "── un fallo del init no puede salir con codigo 0 ──"
SBX="$(mktemp -d)"
cp llmi "$SBX/llmi"; cp roster.example.json "$SBX/" 2>/dev/null || true
mkdir -p "$SBX/casa" "$SBX/falso-bin"
: > "$SBX/casa/.llminbox.token"          # el token ya existe: ese paso se salta
# python3 que revienta SOLO al leer de stdin, que es como se invoca el heredoc del init.
# Asi el ⊖ apunta al heredoc y no a cualquier otro uso de python del script.
cat > "$SBX/falso-bin/python3" <<'STUB'
#!/bin/sh
[ "$1" = "-" ] && { echo "boom (stub del test)" >&2; exit 3; }
exec /usr/bin/env -i PATH=/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin python3 "$@"
STUB
chmod +x "$SBX/falso-bin/python3"
(cd "$SBX" && HOME="$SBX/casa" PATH="$SBX/falso-bin:$PATH" ./llmi init >/dev/null 2>&1)
RC_INIT=$?
[ "$RC_INIT" -ne 0 ] \
  && bien "un fallo del python de init sale con codigo != 0 (rc=$RC_INIT)" \
  || mal "un fallo del python de init sale con codigo != 0" "!= 0" "0: 'llmi init && llmi up' encadenaria sobre una config a medias"

echo "── un .llmi-mounts.json corrupto NO puede leerse como 'sin deriva' ──"
# Se extrae `deriva()` tal cual del fichero real —no una copia a mano, que envejece— y
# se corre contra un JSON truncado. Sin curar devuelve 0 (= sin deriva); curada, 2.
D2="$(mktemp -d)"
printf '{"foo": "/tmp/algo.md", "bar": ' > "$D2/mounts.json"
sed -n '/^deriva() {/,/^}/p' llmi > "$D2/fn.sh"
RC_DERIVA=$( bash -c "
  set -uo pipefail
  # H con un elemento a proposito: el bash 3.2 de macOS revienta con \"\${H[@]}\" y
  # array VACIO bajo \`set -u\`, y ese error se leia como fallo del codigo bajo prueba.
  MOUNTS='$D2/mounts.json'; H=(-s); API='http://127.0.0.1:0'
  _bytes() { echo 1; }
  sin_servicio() { return 3; }
  curl() { echo '[]'; }        # el servicio contesta: lo unico roto es el JSON local
  source '$D2/fn.sh'
  deriva >/dev/null 2>&1; echo \$?" )
[ "$RC_DERIVA" = "2" ] \
  && bien "con el JSON de montajes corrupto, deriva declara NO MEDIDO (rc=2)" \
  || mal "con el JSON de montajes corrupto, deriva declara NO MEDIDO" "2" "$RC_DERIVA: 0 seria 'los montajes miran al mismo inodo' sin haber mirado"
rm -rf "$SBX" "$D2"

# ── un mapa de credenciales VALIDO nunca puede dar ⛔ ─────────────────────────────
# La invariante, y esta escrita asi porque tiene que sobrevivir a que la imagen cambie:
#   rc=0  imagen con V8 + mapa valido        → correcto
#   rc=3  imagen anterior a V8 (NO MEDIDO)   → correcto, y NO es un veredicto del mapa
#   rc=1  ⛔ «no lo despliegues»              → FALSO ROJO sobre un mapa que si vale
#
# Dos falsos rojos en diez minutos escribiendo este comando, los dos dando ⛔ a un mapa
# bueno: primero por correrlo con el `python3` del HOST (sin fastapi) y luego por no
# distinguir «la imagen no conoce V8» de «tu fichero esta mal». Un validador que dice
# que no a todo o te bloquea un despliegue bueno o te ensena a ignorarlo.
echo "── un mapa de credenciales valido nunca da ⛔ ──"
# `mktemp` da 0600, que es COMO SE CREA un fichero de credenciales de verdad — y es
# exactamente el caso que destapo el cuarto falso rojo: `docker cp` conservaba ese 0600
# y el contenedor, con otro uid, no podia leerlo. Se deja asi a proposito.
CRD="$(mktemp)"
chmod 600 "$CRD"
printf '{"cred-de-prueba-000000": {"rol": "be", "carril": "llminbox"}}' > "$CRD"
./llmi credenciales "$CRD" >/dev/null 2>&1
RC_CRD=$?
[ "$RC_CRD" != "1" ] \
  && bien "un mapa valido no se declara indesplegable (rc=$RC_CRD: 0=ok, 3=no medido)" \
  || mal "un mapa valido no se declara indesplegable" "0 o 3" "1 (⛔ sobre un mapa que si vale)"
# Y con el servicio ARRIBA la exigencia sube a 0 exacto: el 3 sólo vale como «no lo sé»
# cuando no hay contra qué medir. Esta linea es la que caza el tercer falso rojo que
# tuvo este comando —validar contra un contenedor NUEVO sin `/state`, o sea sin censo,
# rechazaba mapas perfectamente validos— porque ahi el rc era 1, no 3.
if docker inspect llminbox >/dev/null 2>&1; then
  [ "$RC_CRD" -eq 0 ] \
    && bien "y con el servicio arriba da 0, no un 'no medido' de conveniencia" \
    || mal "con el servicio arriba, un mapa valido da 0" "0" "$RC_CRD"
fi
./llmi credenciales "$CRD.no-existe" >/dev/null 2>&1
[ $? -eq 2 ] \
  && bien "y un fichero que no esta se distingue del mapa invalido (rc=2)" \
  || mal "un fichero ausente tiene su propio codigo" "2" "$?"
rm -f "$CRD"


echo
# ── la suite tiene que pasar en un CLON LIMPIO, no solo en mi checkout ────────────
# `roster.json` esta en .gitignore (lleva la flota real), asi que un test que resuelva
# `actor`/`to` sin FIJAR su censo pasa aqui y falla en cualquier clon — y en el CI.
#
# Ha pasado DOS VECES el mismo dia: PR #101 lo curo por la manana y volvi a cometerlo
# por la tarde en un fichero nuevo, tres horas despues. Un aviso en un commit no es una
# guarda; esto si.
#
# ⚠️ MIDE **HEAD**, NO EL ARBOL DE TRABAJO. Es lo correcto —el CI tambien mide el
# commit, no lo que tengas sin guardar— pero confunde la primera vez: si acabas de
# arreglar algo y no lo has commiteado, esta guarda sigue roja con razon.
#
# Se hace con un worktree DESECHABLE de HEAD: es la unica forma de reproducir «lo que
# ve alguien que clona», y es exactamente lo que hace el job de CI (checkout + pip +
# pytest, sin fabricar roster.json). Con Actions bloqueado por facturacion desde el
# 2026-09-03, esta es la unica red que queda.
echo "── la suite pasa en un clon limpio (sin roster.json) ──"
WT_LIMPIO="$(mktemp -d)/wt"
if git worktree add -q --detach "$WT_LIMPIO" HEAD 2>/dev/null; then
  PY_L="${PY_BIN:-$PWD/.venv-test/bin/python}"
  LOG_LIMPIO="$(mktemp)"
  (cd "$WT_LIMPIO" && PYTHONDONTWRITEBYTECODE=1 "$PY_L" -m pytest tests/pytest -q \
      -p no:cacheprovider >"$LOG_LIMPIO" 2>&1)
  RC_LIMPIO=$?
  git worktree remove --force "$WT_LIMPIO" >/dev/null 2>&1
  if [ "$RC_LIMPIO" -eq 0 ]; then
    bien "la suite pasa sin roster.json (como en un clon o en CI)"
  else
    mal "la suite pasa sin roster.json" "rc=0" "pytest del clon limpio devolvio rc=$RC_LIMPIO"
    tail -n 80 "$LOG_LIMPIO"
  fi
  rm -f "$LOG_LIMPIO"
else
  mal "poder crear el worktree de comprobacion" "worktree creado" "no se pudo: NO MEDIDO"
fi

echo
# ── dos despliegues del MISMO servicio no pueden pelearse por el contenedor ───────
# MEDIDO el 2026-09-04: la bandeja se destruyo y reconstruyo 3 veces en 108 s,
# alternando las imagenes `llminbox-llminbox` y `1bb10f55e868-llminbox`. La causa:
# `container_name` es FIJO (`llminbox`) pero el PROYECTO de compose lo deriva Docker
# del nombre del DIRECTORIO, y hay dos: este repo y el worktree de despliegue que
# cuelga de `~/.local/bin/llmi` (`…/llminbox-deploy/1bb10f55e868`). Dos proyectos que
# reclaman el mismo nombre de contenedor ⇒ cada `up` destruye el del otro.
#
# La guarda corre `llmi` desde un directorio con OTRO nombre y mira que el proyecto
# que le pide a Docker siga siendo el mismo. `grep` no valdria: lo que importa es lo
# que llega a `docker`, no lo que dice el fichero.
echo "── el proyecto de compose no depende del directorio desde el que corras ──"
SBX3="$(mktemp -d)/1bb10f55e868"       # el nombre REAL del worktree de despliegue
mkdir -p "$SBX3/falso-bin" "$SBX3/casa"
cp llmi "$SBX3/llmi"; cp docker-compose.yml "$SBX3/" 2>/dev/null || true
: > "$SBX3/casa/.llminbox.token"
cat > "$SBX3/falso-bin/docker" <<'STUB'
#!/bin/sh
# Sólo delata el proyecto que se le pide y corta: no toca ningún Docker de verdad.
printf 'PROYECTO=%s\n' "${COMPOSE_PROJECT_NAME:-<derivado-del-directorio>}"
exit 97
STUB
chmod +x "$SBX3/falso-bin/docker"
SALIDA3="$( (cd "$SBX3" && HOME="$SBX3/casa" PATH="$SBX3/falso-bin:$PATH" \
             ./llmi down 2>&1) )"
case "$SALIDA3" in
  *PROYECTO=llminbox*) bien "el proyecto de compose queda fijado a 'llminbox' desde cualquier ruta" ;;
  *) mal "el proyecto de compose queda fijado a 'llminbox'" "PROYECTO=llminbox" \
         "$(printf %s "$SALIDA3" | tr '\n' ' ' | cut -c1-120) — dos rutas = dos proyectos = se destruyen el contenedor" ;;
esac
# ⊖ QUE PROTEGE LA VALVULA: `docker-compose.yml` documenta que con `LLMINBOX_NAME` se
# levanta una SEGUNDA instancia sin tumbar la primera. Fijar el proyecto a secas la
# habria matado en silencio; se deriva de esa misma variable.
SALIDA3B="$( (cd "$SBX3" && HOME="$SBX3/casa" PATH="$SBX3/falso-bin:$PATH" \
              LLMINBOX_NAME=otra-instancia ./llmi down 2>&1) )"
case "$SALIDA3B" in
  *PROYECTO=otra-instancia*) bien "y con LLMINBOX_NAME sigue habiendo segunda instancia (no la mata el pin)" ;;
  *) mal "LLMINBOX_NAME sigue dando una segunda instancia" "PROYECTO=otra-instancia" \
         "$(printf %s "$SALIDA3B" | tr '\n' ' ' | cut -c1-120)" ;;
esac
rm -rf "$SBX3"

echo
# ── un `up` no puede APAGAR V8 sin decirlo ────────────────────────────────────────
# `LLMINBOX_CREDENCIALES` sale SOLO del shell de quien corre `llmi up`; el compose hace
# `${LLMINBOX_CREDENCIALES:+…}`, asi que sin la variable el servicio arranca con
# `v8.configurado=false` — el gate de identidad APAGADO, sin un aviso. Cualquiera de la
# flota que hiciera `llmi up` devolvia el bus sin V8 y nadie se enteraba.
# Es la clase «un gate es una dependencia, no un flag»: no puede colgar del ambiente.
#
# Se extrae `v8_se_apaga()` del fichero real (no una copia a mano, que envejece).
echo "── el estado de V8 distingue CUATRO cosas, y 'no medido' no es 'apagado' ──"
# Security reprodujo el P0: `hay_contenedor || return 1` trataba CUALQUIER fallo de
# `docker inspect` como «no hay contenedor» ⇒ arranque en frío ⇒ V8 apagado. Un daemon
# que tarda, un socket que se reconecta o un rate-limit fallan igual, y compose se
# recupera un segundo después. Sólo el mensaje explícito de Docker acredita la ausencia.
D4="$(mktemp -d)"
sed -n '/^_estado_v8() {/,/^}/p' llmi > "$D4/fn.sh"
if [ ! -s "$D4/fn.sh" ]; then
  mal "existe _estado_v8() en llmi" "la funcion" "no esta: el up no puede medir V8"
else
  _estado() {   # $1 = stdout del inspect · $2 = stderr · $3 = rc
    bash -c "
      set -uo pipefail
      LLMINBOX_NAME=llminbox
      docker() { [ -n '$1' ] && printf '%s\n' '$1'; [ -n '$2' ] && printf '%s\n' '$2' >&2; return $3; }
      source '$D4/fn.sh'
      _estado_v8"
  }
  for caso in \
    "LLMINBOX_CREDENCIALES=/credenciales/mapa.json||0|on|V8 encendido en el contenedor" \
    "LLMINBOX_DB=/data/x||0|off|contenedor sin credencial" \
    "|Error: No such object: llminbox|1|ausente|Docker DICE que no existe" \
    "||1|nomedido|el inspect falla y Docker NO dice que no exista" \
    "||0|nomedido|existe y sin Config.Env: estado que Docker no produce"
  do
    IFS='|' read -r _o _e _rc _esp _que <<< "$caso"
    _got="$(_estado "$_o" "$_e" "$_rc")"
    [ "$_got" = "$_esp" ] \
      && bien "$_que ⇒ $_esp" \
      || mal "$_que ⇒ $_esp" "$_esp" "$_got"
  done
fi
rm -rf "$D4"

echo
# ── el cerrojo nombra la INSTANCIA, no el checkout ────────────────────────────────
echo "── el cerrojo del ciclo de vida es por instancia, no por directorio ──"
D5="$(mktemp -d)"
sed -n '/^_tomar_cerrojo() {/,/^}/p' llmi > "$D5/fn.sh"
_ruta_lock() {
  bash -c "
    set -uo pipefail
    DIR='/un/checkout/cualquiera'; LLMINBOX_NAME='$1'; LLMI_LOCK_DIR='$D5'
    source '$D5/fn.sh'
    mkdir() { :; }; trap() { :; }
    _tomar_cerrojo >/dev/null 2>&1
    echo \"\$_LOCK\""
}
L1="$(_ruta_lock llminbox)"; L2="$(_ruta_lock otra-instancia)"
case "$L1" in
  */un/checkout/*) mal "el cerrojo no cuelga del checkout" "una ruta por instancia" "$L1" ;;
  *llminbox*)      bien "el cerrojo nombra la instancia y no el directorio ($L1)" ;;
  *)               mal "el cerrojo nombra la instancia" "ruta con 'llminbox'" "$L1" ;;
esac
[ "$L1" != "$L2" ] \
  && bien "⊖ dos instancias distintas NO comparten cerrojo" \
  || mal "dos instancias tienen cerrojos distintos" "rutas distintas" "las dos: $L1"
rm -rf "$D5"


[ "$MALOS" -eq 0 ] && echo "guardas: TODO VERDE" || echo "guardas: $MALOS fallo(s)"
exit $((MALOS > 0))
