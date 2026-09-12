#!/usr/bin/env bash
# Prueba de humo: levanta el servicio de cero y comprueba las propiedades que este
# producto promete. Corre en CI y en local; no necesita nada del disco del operador.
#
# Cada comprobación tiene su FALSADOR escrito al lado: qué se vería si la propiedad
# estuviera rota. Una comprobación que no puede fallar no comprueba nada — la
# lección más cara de este proyecto.
set -uo pipefail
PUERTO="${PUERTO:-8199}"; NOMBRE="humo-$$"; TOK="humo-$(date +%s)"; WTOK="watcher-$TOK"
TMP="$(mktemp -d)"; FALLOS=0
# `mktemp -d` crea el directorio en 700 y propiedad de quien lo llama. En Linux eso
# significa que el contenedor —que corre como uid 1000, no como root— NO puede leer
# el ledger montado dentro, y el servicio indexa CERO sin que el test sepa por qué.
# En macOS no se nota: Docker Desktop virtualiza la propiedad (`fakeowner`) y todo
# se lee. Cazado por el CI en la primera corrida real, que es exactamente para lo
# que existe un gate que corre en la máquina de otro.
# LA IMAGEN TIENE QUE SER DEL CÓDIGO QUE ESTÁS PROBANDO, y hasta hoy no se comprobaba.
# `IMAGEN` cae por defecto a `llminbox:test`, así que en local el humo corría CUALQUIER
# imagen que existiera con ese nombre. El 2026-09-01 dio `1 fallo` contra un servicio
# sano: la imagen tenía TRECE DÍAS y devolvía el contrato viejo de `retrocedidos`. El
# arnés no medía el commit, medía el disco — y el error real se lo tragaba un
# `2>/dev/null` de la extracción.
#
# En la CI no muerde porque construye `llminbox:test` del checkout justo antes. Muerde
# EN LOCAL, que es donde se corre antes de abrir una PR: un verde ahí puede ser de otra
# semana, y un rojo puede no ser tuyo.
#
# No se compara el código —el humo no sabe qué commit esperas— sino la EDAD: una imagen
# de días no puede ser del árbol que acabas de tocar. Avisa y sigue; abortar rompería a
# quien prueba a propósito contra una imagen guardada.
chmod 755 "$TMP"
DATOS="$(mktemp -d)"; chmod 777 "$DATOS"   # BD FUERA del contenedor: tiene que sobrevivir a un reinicio
limpiar() { docker rm -f "$NOMBRE" >/dev/null 2>&1; rm -rf "$TMP" "$DATOS"; }
trap limpiar EXIT

# Las guardas del arnés viven aparte para poder probarlas con su entrada mala:
# `tests/guardas.sh`. Ver la cabecera de ese fichero — nació de 8 puertas que
# certificaron en verde dos días algo que no ocurría, sin que ningún mutante lo viera.
. "$(dirname "$0")/guardas.lib.sh"
aviso_imagen_rancia   # necesita la lib cargada: llamarla antes la deja muda

printf '# t\n\n### [alice-backend → bob-reviewer · REQUEST] primera\ncuerpo uno\n' > "$TMP/l.md"
printf '\n### [bob-reviewer → alice-backend · ACK] segunda\ncuerpo dos\n' >> "$TMP/l.md"

docker run -d --name "$NOMBRE" -p "127.0.0.1:$PUERTO:8077" \
  -e LLMINBOX_LEDGERS="t=/l/l.md" -e LLMINBOX_TOKEN="$TOK" \
  -e LLMINBOX_WATCHER_TOKEN="$WTOK" \
  -e LLMINBOX_DB=/datos/h.sqlite -e LLMINBOX_POLL=1 -e LLMINBOX_ROSTER=/censo.json \
  -v "$TMP:/l:ro" -v "$DATOS:/datos" -v "$PWD/roster.example.json:/censo.json:ro" \
  "${IMAGEN:-llminbox:test}" >/dev/null || { echo "no arrancó el contenedor"; exit 1; }

esperar_salud "http://127.0.0.1:$PUERTO" 40 "el contenedor principal"
sleep 3
A=(-H "X-Llminbox-Token: $TOK"); U="http://127.0.0.1:$PUERTO"

echo "── autenticación ──"
# falsador: si el gate no existiera, esto daría 200 y serviría el canon entero
comp "sin token → 401"        "401" "$(curl -s -o /dev/null -w '%{http_code}' "$U/stat")"
comp "token malo → 401"       "401" "$(curl -s -o /dev/null -w '%{http_code}' -H 'X-Llminbox-Token: no' "$U/stat")"
comp "token bueno → 200"      "200" "$(curl -s -o /dev/null -w '%{http_code}' "${A[@]}" "$U/stat")"
# falsador: /docs abierto fue un agujero real; si vuelve, esto da 200
comp "/docs cerrado → 404"    "404" "$(curl -s -o /dev/null -w '%{http_code}' "$U/docs")"
comp "/openapi cerrado → 404" "404" "$(curl -s -o /dev/null -w '%{http_code}' "$U/openapi.json")"

# El health incluye la vigilancia en su invariante. El smoke tiene que construir un
# servicio ARMABLE aunque todavía no haya arrancado ningún watcher: sin credencial,
# `inarmable` es correctamente rojo y cualquier prueba posterior de indexador mediría
# ese fallo de configuración en vez del sujeto. Este brazo impide arreglar el rojo
# rebajando `/health` o dejando el token sin llegar al proceso.
VIG=$(curl -s "$U/health" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["watcher_auth"], d["vigilancia"]["estado"])')
comp "el smoke arma la vigilancia sin fabricar un latido" "True sin-armar" "$VIG"

# Antes que nada: si el servicio no puede LEER un ledger, todo lo de abajo da cero
# y el test culpa al indexado. `/health` ya lo dice —cada ledger roto con su motivo—;
# lo que faltaba era mirarlo.
# ⚠️ `-z` sobre una respuesta muerta decía «ningún ledger roto»: el silencio del
# servicio se leía como la mejor noticia posible. Ahora la respuesta se mide antes.
SALUD=$(cuerpo "$U/health")
case "$SALUD" in
  NO-MEDIDO:*) no_medido "ningún ledger roto" "${SALUD#NO-MEDIDO:}" ;;
  *) ROTOS=$(printf '%s' "$SALUD" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("rotos") or "")' 2>/dev/null)
     [ -z "$ROTOS" ] && ok "ningún ledger roto" || fallo "ningún ledger roto" "ninguno" "$ROTOS" ;;
esac

echo "── indexado ──"
comp "indexa las 2 entradas" "2" "$(curl -s "${A[@]}" "$U/stat" | python3 -c 'import sys,json;print(json.load(sys.stdin)[0]["entradas"])')"
# falsador: sin censo cargado el extractor devolvería actor=None y esto sería 0
# Se piden LAS DOS y se comparan ordenadas: con limit=1 esta prueba comparaba la
# entrada más reciente contra el actor de la otra y fallaba por culpa del test.
comp "extrae los dos actores" "alice-backend,bob-reviewer" \
  "$(curl -s "${A[@]}" "$U/entries?ledger=t" | python3 -c 'import sys,json;print(",".join(sorted({x["actor"] or "?" for x in json.load(sys.stdin)})))')"
# falsador: sin censo cargado, el extractor devuelve None y esto sería "?"
comp "extrae destinatarios" "alice-backend,bob-reviewer" \
  "$(curl -s "${A[@]}" "$U/entries?ledger=t" | python3 -c 'import sys,json;print(",".join(sorted({w for x in json.load(sys.stdin) for w in (x.get("to") or [])})))')"

echo "── la bandeja, que es el producto ──"
n=$(contar "para ti" "${A[@]}" "$U/inbox/alice-backend")
comp "alice tiene bandeja" "1" "$n"
# falsador: si el GET mutara el cursor (bug real, corregido), la 2ª lectura saldría vacía
n2=$(contar "para ti" "${A[@]}" "$U/inbox/alice-backend")
comp "el GET no consume" "1" "$n2"
# y el POST sí avanza
curl -s "${A[@]}" -H 'Content-Type: application/json' -d '{"hasta":{"t":99}}' -o /dev/null "$U/inbox/alice-backend/leido"
comp "el POST sí avanza" "0" "$(contar 'para ti' "${A[@]}" "$U/inbox/alice-backend")"
# LA MISMA BANDEJA ESCRITA EN MAYÚSCULAS ES LA MISMA BANDEJA. El destinatario se
# empareja por el nombre canónico (ignora la caja) pero el cursor se guardaba con la
# cadena literal ⇒ `/inbox/ALICE-BACKEND` no encontraba cursor, leía desde -1 y
# devolvía una bandeja SOMBRA que nadie drenaba; y el POST con esa grafía contestaba
# `ok:true` escribiendo el cursor de un agente inexistente. Medido en producción el
# 2026-08-08: 6 secciones en minúsculas contra 7 en mayúsculas.
# falsador: sin canonizar el cursor, esto devuelve las entradas otra vez (>0).
comp "y la MISMA bandeja en MAYÚSCULAS está igual de drenada" "0" \
  "$(contar 'para ti' "${A[@]}" "$U/inbox/ALICE-BACKEND")"
# El otro brazo: que el POST en mayúsculas NO cree un cursor paralelo. Se comprueba
# por el efecto —el cursor canónico se mueve— y no por la respuesta, que dice ok:true
# en los dos casos: un campo que refleja tu entrada no es una verificación.
curl -s "${A[@]}" -H 'Content-Type: application/json' -d '{"hasta":{"t":5}}' -o /dev/null "$U/inbox/ALICE-BACKEND/leido"
comp "y un POST en MAYÚSCULAS mueve el cursor del canónico" "5" \
  "$(curl -s "${A[@]}" "$U/cursor/alice-backend" | python3 -c 'import sys,json;print(json.load(sys.stdin)["t"])')"
curl -s "${A[@]}" -H 'Content-Type: application/json' -d '{"hasta":{"t":99}}' -o /dev/null "$U/inbox/alice-backend/leido"

# DRENAR TIENE QUE PODER FALLAR A LA VISTA. Este endpoint devolvía
# `{"ok":true,"cursores":<tu propia entrada>}` pasara lo que pasara: un ledger mal
# escrito se saltaba con un `continue` mudo. Medido sobre los transcripts de la flota
# el 2026-08-08: 24.723 llamadas a /leido y 74.525 entradas aún sin drenar. No era
# desidia — era esta respuesta.
# falsador: con el eco de vuelta, un ledger inexistente devuelve ok:true y esto pasa.
R=$(curl -s "${A[@]}" -H 'Content-Type: application/json' -d '{"hasta":{"no-existe":5}}' "$U/inbox/alice-backend/leido")
comp "un ledger DESCONOCIDO no puede dar ok:true" "False" \
  "$(printf '%s' "$R" | python3 -c 'import sys,json;print(json.load(sys.stdin)["ok"])' 2>/dev/null)"
comp "y la respuesta lo NOMBRA en ignorados" "no-existe" \
  "$(printf '%s' "$R" | python3 -c 'import sys,json;print(",".join(json.load(sys.stdin)["ignorados"]))' 2>/dev/null)"
# CONTROL POSITIVO: uno REAL sí tiene que aplicarse, o lo de arriba pasaría con el
# endpoint roto del todo.
R2=$(curl -s "${A[@]}" -H 'Content-Type: application/json' -d '{"hasta":{"t":7}}' "$U/inbox/alice-backend/leido")
comp "un ledger REAL sí se aplica (el gate no está mudo)" "True" \
  "$(printf '%s' "$R2" | python3 -c 'import sys,json;print(json.load(sys.stdin)["ok"])' 2>/dev/null)"
comp "y trae el ANTES y el AHORA, no un eco" "7" \
  "$(printf '%s' "$R2" | python3 -c 'import sys,json;print(json.load(sys.stdin)["aplicados"]["t"]["ahora"])' 2>/dev/null)"
# RETROCEDER NO ES «SIN EFECTO», aunque el tramo sea sólo preventivo. Este ledger
# tiene arrivals reales 0 y 1: al mover 9 → 4 el tramo aritmético mide 5, pero no
# vuelve a verse ninguna entrada. Contar 5 aquí era el defecto corregido por #61:
# confundía posiciones inexistentes —o correo ajeno— con correo recuperado.
# Se exigen LOS DOS valores y el objeto `retrocedidos`: usar `.get(..., 0)` sólo
# para esperar cero dejaría pasar la desaparición completa del contrato.
curl -s "${A[@]}" -H 'Content-Type: application/json' -d '{"hasta":{"t":9}}' -o /dev/null "$U/inbox/alice-backend/leido"
RET=$(curl -s "${A[@]}" -H 'Content-Type: application/json' -d '{"hasta":{"t":4}}' "$U/inbox/alice-backend/leido" \
      | python3 -c 'import sys,json;d=json.load(sys.stdin)["retrocedidos"]["t"];print("{}:{}".format(d["tramo"],d["vuelven_a_verse"]))' 2>/dev/null)
comp "retroceder declara el tramo y no fabrica entradas que vuelven" "5:0" "${RET:-sin-contrato}"
curl -s "${A[@]}" -H 'Content-Type: application/json' -d '{"hasta":{"t":99}}' -o /dev/null "$U/inbox/alice-backend/leido"
# Y el cuerpo que sugiere la bandeja tiene que ser PEGABLE, no una taquigrafía: es
# donde se rompían los 24.723 intentos.
SUG=$(curl -s "${A[@]}" "$U/inbox/bob-reviewer" | grep -oE '\{"hasta":\{.*\}\}' | tail -1)
comp "la bandeja sugiere un cuerpo JSON válido" "ok" \
  "$(printf '%s' "${SUG:-x}" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("ok" if "hasta" in d else "no")' 2>/dev/null || echo "no-json")"
curl -s "${A[@]}" -H 'Content-Type: application/json' -d '{"hasta":{"t":99}}' -o /dev/null "$U/inbox/alice-backend/leido"

echo "── integridad: distinguir escribir de borrar ──"
printf '\n### [alice-backend → bob-reviewer · FYI] tercera a medias\n' >> "$TMP/l.md"; sleep 3
printf 'el cuerpo llega tarde\n' >> "$TMP/l.md"; sleep 3
# falsador: antes esto gritaba «entrada que ESTUVO y ya no está» por escribir en 2 pasos
comp "escribir a trozos NO alarma" "0" "$(contar '✗' "${A[@]}" "$U/chain/verify")"
# Y NO BASTA CON QUE NO ALARME: hay que contar FILAS. Si la limpieza de provisionales
# se rompe, la versión a medias sobrevive junto a la completa y deja una fila
# fantasma — medido con un mutante el 2026-08-08: 3 filas donde el fichero tenía 2
# entradas, y NINGUNA de las 64 comprobaciones se enteraba, porque las dos que
# vigilan esta zona miran la ALARMA y esa fila queda con `ausente = NULL`, o sea
# indistinguible de una viva. En producción: conteos inflados y titulares viejos
# servidos en bandeja, en silencio. La suite comprobaba que la limpieza no alarmara
# de más; no que la limpieza OCURRA.
# falsador: con la limpieza rota, esto da 4 en vez de 3.
comp "y el índice tiene UNA fila por entrada (sin fantasma de la escritura a trozos)" "3" \
  "$(curl -s "${A[@]}" "$U/stat" | python3 -c 'import sys,json;print(json.load(sys.stdin)[0]["entradas"])' 2>/dev/null)"

paso "el borrado de entradas del ledger" python3 -c "
p='$TMP/l.md'; L=open(p).read().split(chr(10)); open(p,'w').write(chr(10).join(L[:4]))"; sleep 4
# falsador: si no detectara borrados, esto seguiría en 0 y la promesa del producto es falsa
ALARMAS=$(contar '✗' "${A[@]}" "$U/chain/verify")
case "$ALARMAS" in
  NO-MEDIDO:*) no_medido "borrar SÍ alarma" "${ALARMAS#NO-MEDIDO:}" ;;
  *) [ "$ALARMAS" -ge 1 ] && ok "borrar SÍ alarma" \
       || fallo "borrar SÍ alarma" "≥1 línea con ✗" "$ALARMAS" ;;
esac

echo "── dar de alta a un agente NO le vacía la bandeja al equipo ──"
# Falsador: el cursor se planta a 99, se añade un agente al censo y se reinicia. Si
# vuelve a -1, dar de alta a un compañero le borra el estado de lectura a todos —
# que es lo que hacía hasta el 2026-07-28 (reproducido: 5 → -1), porque la huella
# del esquema y la del censo eran la misma y el censo tiraba la tabla `cursors`.
paso "la escritura del censo con el agente nuevo" python3 -c "
import json,sys
d=json.load(open('$PWD/roster.example.json',encoding='utf-8'))
d['agentes'].append({'nombre':'zz-alta-tardia','humano':'alice','clave':''})
json.dump(d,open('$TMP/censo2.json','w',encoding='utf-8'),ensure_ascii=False)"
ANTES=$(curl -s "${A[@]}" "$U/cursor/alice-backend" | python3 -c 'import json,sys;print(json.load(sys.stdin)["t"])')
docker rm -f "$NOMBRE" >/dev/null 2>&1
docker run -d --name "$NOMBRE" -p "127.0.0.1:$PUERTO:8077" \
  -e LLMINBOX_LEDGERS="t=/l/l.md" -e LLMINBOX_TOKEN="$TOK" \
  -e LLMINBOX_WATCHER_TOKEN="$WTOK" \
  -e LLMINBOX_DB=/datos/h.sqlite -e LLMINBOX_POLL=1 -e LLMINBOX_ROSTER=/censo.json \
  -v "$TMP:/l:ro" -v "$DATOS:/datos" -v "$TMP/censo2.json:/censo.json:ro" \
  "${IMAGEN:-llminbox:test}" >/dev/null
esperar_salud "http://127.0.0.1:$PUERTO" 40 "el contenedor tras el alta de un agente"
sleep 3
DESPUES=$(curl -s "${A[@]}" "$U/cursor/alice-backend" | python3 -c 'import json,sys;print(json.load(sys.stdin)["t"])')
comp "el cursor sobrevive al alta de un agente" "$ANTES" "$DESPUES"
# EL OTRO BRAZO, y es el que de verdad falsa el arreglo: al cambiar el censo se
# BORRAN los destinatarios para re-derivarlos. Si la re-derivación no ocurriera, el
# cursor sobreviviría igual —«no toco nada» también lo conserva— pero las entradas
# se quedarían SIN destinatarios y la bandeja de todos saldría vacía: un arreglo
# peor que el defecto, y verde en el primer brazo. Se comprueba que vuelven.
# Se espera SÓLO bob-reviewer: la prueba de borrado de arriba dejó el ledger con una
# entrada viva (`alice-backend → bob-reviewer`), y las desaparecidas no se sirven. La
# expectativa era del estado ANTERIOR al borrado — el falsador cazó mi despiste, que
# es exactamente para lo que está. Lo que falsa el arreglo sigue en pie: si la
# re-derivación no ocurriera, esto saldría VACÍO.
comp "y los destinatarios se RE-DERIVAN (no se quedan borrados)" "bob-reviewer" \
  "$(curl -s "${A[@]}" "$U/entries?ledger=t" | python3 -c 'import sys,json;print(",".join(sorted({w for x in json.load(sys.stdin) for w in (x.get("to") or [])})))')"

echo "── los informes de sólo-lectura CONTESTAN ──"
# Falsador: `/lint` estuvo devolviendo 500 sin que nadie se enterara —emparejaba por
# una columna que la migración a identidad-por-contenido había retirado— y quien lo
# llamaba filtraba su salida por prefijo de línea, así que un error no casaba el
# filtro y el hueco se leía como «sin hallazgos». Aquí se comprueba el CÓDIGO y que
# la respuesta lleve la cabecera de un ledger, no que devuelva algo.
for ruta in lint canon/pendientes chain/verify stat; do
  cod="$(curl -s -o /tmp/humo-r.$$ -w '%{http_code}' -m 90 "${A[@]}" "$U/$ruta")"
  cuerpo="$(head -c 400 /tmp/humo-r.$$ 2>/dev/null)"; rm -f /tmp/humo-r.$$
  case "$ruta:$cod" in
    lint:200)             grep -q '──' <<<"$cuerpo" && ok "/lint contesta y trae ledgers" || fallo "/lint" "líneas ──" "$cuerpo" ;;
    canon/pendientes:200) grep -q 'escuchando' <<<"$cuerpo" && ok "/canon/pendientes contesta" || fallo "/canon/pendientes" "escuchando" "$cuerpo" ;;
    chain/verify:200)     ok "/chain/verify contesta" ;;
    stat:200)             ok "/stat contesta" ;;
    *) fallo "$ruta" "200" "$cod" ;;
  esac
done

echo "── el barrido lento y el índice corrupto (un ledger grande, aparte) ──"
# Las dos propiedades de abajo necesitan un corpus donde un barrido CUESTE, así que
# van en su propio contenedor con un ledger de 10.000 entradas.
NOM2="humo2-$$"; P2=$((PUERTO+1)); TMP2="$(mktemp -d)"; D2="$(mktemp -d)"
chmod 755 "$TMP2"; chmod 777 "$D2"

limpiar2() { docker rm -f "$NOM2" >/dev/null 2>&1; rm -rf "$TMP2" "$D2"; }
trap 'limpiar; limpiar2' EXIT
paso "la generación del ledger de 30.000 entradas" python3 -c "
with open('$TMP2/g.md','w') as f:
    f.write('# grande' + chr(10))
    for i in range(30000):
        f.write(chr(10) + f'### [alice-backend → bob-reviewer · FYI] entrada {i}' + chr(10) + f'cuerpo {i}' + chr(10))"
docker run -d --name "$NOM2" -p "127.0.0.1:$P2:8077" \
  -e LLMINBOX_LEDGERS="g=/l/g.md" -e LLMINBOX_TOKEN="$TOK" \
  -e LLMINBOX_WATCHER_TOKEN="$WTOK" \
  -e LLMINBOX_DB=/datos/h.sqlite -e LLMINBOX_POLL=0.02 -e LLMINBOX_ROSTER=/censo.json \
  -e LLMINBOX_CHEQUEO=2 -e LLMINBOX_SUELO_RECONSTRUCCION=5 \
  -v "$TMP2:/l" -v "$D2:/datos" -v "$PWD/roster.example.json:/censo.json:ro" \
  "${IMAGEN:-llminbox:test}" >/dev/null
esperar_salud "http://127.0.0.1:$P2" 60 "el contenedor del ledger grande"
B=(-H "X-Llminbox-Token: $TOK"); V="http://127.0.0.1:$P2"
# Mismo motivo que abajo: el runner de CI es más lento que este Mac y 60 s no
# bastaban para indexar 30.000 entradas. Un techo corto convierte una prueba de
# corrección en una prueba de velocidad de la máquina.
for _ in $(seq 1 240); do
  [ "$(curl -s "${B[@]}" "$V/stat" | python3 -c 'import sys,json;print(json.load(sys.stdin)[0]["entradas"])' 2>/dev/null)" = "30000" ] && break
  sleep 1
done

# ── ② el rojo que aparecía solo ────────────────────────────────────────────────
# El umbral era `POLL*6` contra el tiempo desde el último barrido COMPLETO, lo cual
# sólo se sostiene si un barrido dura ~0. Con el corpus real (82 MB, un LEDGER.md de
# 75 MB) dura ~17,6 s: `hace_s` subía 0,6 → 17,0 y reiniciaba, y `/health` decía
# `ok:false` en 3 de cada 8 muestras con TODO sano. El hook de arranque de un agente
# leía justo eso y anunciaba «llminbox no responde» sobre un servicio sano.
# Aquí en pequeño: POLL 0,02 s ⇒ el techo VIEJO son 0,12 s.
ROJOS=0; MUESTRAS=0; LEIDAS=0; DUROS="$TMP2/duraciones.txt"; : > "$DUROS"
# ESCRITOR DE FONDO: sin cambios en el fichero, `barrido` sale por la vía rápida y
# no cuesta nada. La primera versión tocaba el ledger una vez por segundo y con
# POLL a 0,02 s eso deja 1 barrido caro de cada 50: el muestreo casi nunca lo
# pillaba, `barrido_s` salía 0,0 en las 12 muestras y el control cazó que los «0
# rojos» no medían nada. Escribiendo sin parar, TODOS los barridos re-parsean.
( while :; do echo "" >> "$TMP2/g.md"; sleep 0.05; done ) & ESCRITOR=$!
for _ in $(seq 1 40); do
  o=$(curl -s -m 5 "$V/health" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["ok"], d["indexador"]["barrido_s"] or 0)' 2>/dev/null)
  MUESTRAS=$((MUESTRAS+1))
  # Una muestra que no se pudo LEER no es una muestra verde. Sin esto, un curl que
  # agote el tiempo o un JSON sin las claves deja `$o` vacío, no casa `False*`, y
  # cuenta como «sin rojos»: el contador diría 0 aunque el servicio estuviera mudo.
  case "$o" in True*|False*) LEIDAS=$((LEIDAS+1));; esac
  case "$o" in False*) ROJOS=$((ROJOS+1));; esac
  echo "${o#* }" >> "$DUROS"
  sleep 0.25
done
kill "$ESCRITOR" 2>/dev/null; wait "$ESCRITOR" 2>/dev/null
# El MÁXIMO observado DURANTE el muestreo, no el valor de después. La primera
# versión de esta prueba leía `barrido_s` al terminar el bucle, cuando el último
# barrido ya había salido por la vía rápida: daba 0 y el control cazó que los
# «0 rojos» de arriba no medían nada. Con POLL a 0,05 s la mayoría de barridos son
# de vía rápida —el fichero sólo cambia una vez por segundo—, así que el que cuesta
# hay que pillarlo al vuelo.
DUR=$(python3 -c "
vals=[float(x) for x in open('$DUROS') if x.strip()]
print(max(vals) if vals else 0)")
# Falsador: con el techo viejo esto daría rojo en casi todas las muestras.
comp "ningún rojo con el barrido lento ($LEIDAS de $MUESTRAS muestras leídas)" "0" "$ROJOS"
[ "$LEIDAS" -ge 30 ] && ok "y se leyeron $LEIDAS de $MUESTRAS: el contador midió algo" \
  || fallo "hay que poder leer las muestras o el 0 de arriba no significa nada" "≥30 leídas" "$LEIDAS"
# CONTROL DE LA PRUEBA, y sin él lo de arriba no vale: si el barrido saliera
# instantáneo, cero rojos sería verde por no haber medido nada.
TECHO_VIEJO=$(python3 -c "print(0.02*6)")
python3 -c "import sys; sys.exit(0 if float('$DUR') > float('$TECHO_VIEJO') else 1)" \
  && ok "y el barrido duró $DUR s > $TECHO_VIEJO s (POLL*6, el techo viejo) — la prueba muerde" \
  || fallo "el barrido tiene que costar más que el techo VIEJO o esto no prueba nada" \
           ">$TECHO_VIEJO" "$DUR"

# ══ FRONTERA: DOS PREGUNTAS DISTINTAS, Y MEZCLARLAS DABA SEIS ROJOS FALSOS ══════
#
# Arriba se pregunta «¿aguanta el barrido lento sin ponerse rojo?», y para eso hace
# falta el poll patológico: POLL=0.02 con sonda de integridad cada 2 s. Abajo se
# pregunta «¿se reconstruye bien el índice y sobrevive el cursor?», y eso AFIRMA
# CONTEOS EXACTOS — que es justo lo que el poll de arriba impide.
#
# Medido el 2026-08-08: bajo esos ajustes la sonda dispara reconstrucciones MIENTRAS
# el indexador reescribe las 30.000 posiciones del reordenado, y el índice se queda
# en 29.999 sin converger nunca. Los seis rojos de CI decían «la reconstrucción
# devolvió 29.999 de 30.050» y no describían ningún defecto del producto: describían
# este montaje. Verificado por separado — el mismo ledger de 19 MB, con ajustes
# normales, indexa el reordenado en 1,29 s y converge a 30.050 en ~6 s.
#
# Así que el contenedor se reinicia aquí con POLL realista, CONSERVANDO LA BASE
# (mismo volumen `$D2`): lo de abajo mide corrección, no velocidad de la máquina.
# Esto NO baja el listón — separa dos preguntas que no caben en el mismo montaje.
#
# ⚠️ Lo que se cambia es SÓLO el poll. La sonda de integridad se queda en 2 s porque
# ES EL MECANISMO BAJO PRUEBA: mi primer intento la puso en 3600 «para que no
# interfiriera» y con eso deshabilité justo lo que el test de corrupción verifica —
# cinco rojos nuevos, y ninguno del producto. Al acotar un test hay que saber cuál
# de los ajustes es el ruido y cuál es el sujeto.
#
# ⚠️ Queda un hallazgo REAL sin gate, y se anota para no perderlo: bajo sonda
# agresiva el índice puede no converger nunca. Eso merece su propia comprobación —
# de CONVERGENCIA, no de conteo — y hoy no existe.
docker rm -f "$NOM2" >/dev/null 2>&1
docker run -d --name "$NOM2" -p "127.0.0.1:$P2:8077" \
  -e LLMINBOX_LEDGERS="g=/l/g.md" -e LLMINBOX_TOKEN="$TOK" \
  -e LLMINBOX_WATCHER_TOKEN="$WTOK" \
  -e LLMINBOX_DB=/datos/h.sqlite -e LLMINBOX_POLL=1 -e LLMINBOX_ROSTER=/censo.json \
  -e LLMINBOX_CHEQUEO=2 -e LLMINBOX_SUELO_RECONSTRUCCION=5 \
  -v "$TMP2:/l" -v "$D2:/datos" -v "$PWD/roster.example.json:/censo.json:ro" \
  "${IMAGEN:-llminbox:test}" >/dev/null
esperar_salud "http://127.0.0.1:$P2" 60 "el contenedor tras el reinicio con ajustes realistas"
sleep 3
# CONTROL de la frontera: si el reinicio no conservó la base, lo de abajo mediría
# una reconstrucción desde cero y daría verde por el motivo equivocado.
NB=$(curl -s "${B[@]}" "$V/stat" | python3 -c 'import sys,json;print(json.load(sys.stdin)[0]["entradas"])' 2>/dev/null)
[ "${NB:-0}" = 30000 ] \
  && ok "el reinicio con ajustes realistas conserva el índice completo ($NB entradas)" \
  || fallo "el índice heredado tiene que haber convergido antes del reordenado" "30000" "${NB:-sin dato}"

# ── ① la corrupción que nadie curaba ───────────────────────────────────────────
# El 2026-08-01 el B-tree de `entries` se corrompió: el servicio lo DETECTÓ, lo
# anotó como «ledger roto» y se rindió. `entries` es derivada del markdown y se
# re-deriva en 15 s sobre el corpus real — rendirse ahí es rendirse ante nada.
curl -s "${B[@]}" -H 'Content-Type: application/json' -d '{"hasta":{"g":500}}' \
  -o /dev/null "$V/inbox/alice-backend/leido"
CUR_ANTES=$(curl -s "${B[@]}" "$V/cursor/alice-backend" | python3 -c 'import json,sys;print(json.load(sys.stdin)["g"])')
# SE REORDENA EL LEDGER, metiendo entradas POR DELANTE. Es lo que hace un merge de
# git —el esquema de este repo lo documenta como el caso normal en equipo— y es lo
# único que distingue las dos cosas que aquí se pueden llamar «el cursor sobrevive»:
#   · sobrevive la FILA    → el número 500 sigue guardado
#   · sobrevive el SIGNIFICADO → sigue apuntando A LA MISMA ENTRADA
# `arrival` es «el orden en que ESTA instancia vio la entrada»; una base
# reconstruida de cero las ve todas a la vez, o sea en ORDEN DE FICHERO. Con
# entradas nuevas por delante los dos órdenes dejan de coincidir y restaurar el
# número tal cual mueve el cursor a otra entrada — hacia atrás el agente relee,
# hacia adelante SE SALTA correo dirigido a él y nada lo dice. Sin este reordenado
# la prueba pasaba con el arreglo y sin él.
python3 -c "
p='$TMP2/g.md'; txt=open(p).read(); i=txt.index(chr(10)+'###')
nuevas=''.join(chr(10)+f'### [bob-reviewer → alice-backend · FYI] llegada por merge {k}'+chr(10)+f'cuerpo merge {k}'+chr(10) for k in range(50))
open(p,'w').write(txt[:i]+nuevas+txt[i:])"
# ESPERA CON TECHO GENEROSO Y FALLO HONESTO. Insertar 50 entradas POR DELANTE mueve
# la posición de las 30.000, así que la pasada siguiente reescribe el ledger entero:
# en el runner de CI eso no cabía en 60 s. Y el efecto era peor que la lentitud — los
# tests de corrupción seguían corriendo sobre un índice a medio hacer y reportaban
# «la reconstrucción devolvió 30.000 de 30.050», culpando a la reconstrucción de un
# fallo que era del MONTAJE. Un test que atribuye mal es peor que uno que falla:
# manda a arreglar donde no está roto. Medido el 2026-08-08: verde en macOS, 6 rojos
# en Linux, y ninguno de los 6 era el defecto que nombraban.
LISTO=""
for _ in $(seq 1 240); do
  N_AHORA="$(curl -s "${B[@]}" "$V/stat" | python3 -c 'import sys,json;print(json.load(sys.stdin)[0]["entradas"])' 2>/dev/null)"
  [ "$N_AHORA" = "30050" ] && { LISTO="sí"; break; }
  sleep 1
done
[ -n "$LISTO" ] \
  && ok "el reordenado (50 entradas por delante) queda indexado: 30.050" \
  || fallo "PRECONDICIÓN: sin el reordenado indexado, lo que sigue mide otra cosa" \
           "30050 entradas" "${N_AHORA:-sin respuesta} tras 240 s"

# GUARDA: si la precondición de arriba no se cumplió, este bloque entero mide otra
# cosa. Sin ella, un índice a medio construir producía CUATRO rojos que nombraban
# defectos inexistentes —«la reconstrucción devolvió 29.999», «el cursor no sigue
# ahí»— y mandaban a arreglar la cura de corrupción, que estaba sana. Cuatro rojos
# que mienten son peores que uno que dice la verdad: el que lee arregla lo que no
# está roto y el defecto real se queda dentro del ruido.
if [ -z "$LISTO" ]; then
  echo "  ⏭️  bloque de corrupción SALTADO: la precondición no se cumplió (ver arriba)."
  echo "     No se marcan rojos aquí — no se ha medido nada, y un rojo sin medición"
  echo "     es una acusación sin prueba."
else

# También LEER va por el contenedor, y esto es la causa que costó tres corridas de CI
# encontrar: abrir en `mode=ro` una base en WAL **ESCRIBE EN EL DISCO** — SQLite crea
# `-wal` y `-shm` al lado, propiedad de QUIEN CONECTA. Leyéndola desde el host, el
# runner (uid 1001) las dejaba suyas junto a una base del servicio (uid 1000), y a
# partir de ahí NADIE más podía escribir: el `INSERT` del veneno daba `readonly
# database` con el 0644 delante, y el arranque siguiente moría con exit 3. El
# comentario que había aquí —«leer se hace desde el host: un 0644 lo lee cualquiera»—
# era la afirmación falsa que sostenía el error. Leer esa base es escribir.
lee_eid() {   # a qué entrada apunta un cursor, en la base que haya ahora mismo
  en_contenedor "$D2" "
import sqlite3
c=sqlite3.connect('file:/datos/h.sqlite?mode=ro',uri=True,timeout=20)
r=c.execute('SELECT eid FROM entries WHERE ledger=\"g\" AND arrival<=? ORDER BY arrival DESC LIMIT 1',($1,)).fetchone()
print(r[0] if r else 'NINGUNA')" 2>/dev/null
}
EID_ANTES=$(lee_eid "$CUR_ANTES")
# Se ENVENENA la huella de esquema antes de romper nada, y tiene motivo: sin esto,
# la huella rescatada de la base rota coincide con la actual y la comprobación de
# más abajo pasaría con el arreglo puesto Y sin él. El veneno crea el caso que sí
# distingue — el de un índice corrupto que viene de una versión anterior.
MONTADO="sí"
propiedad_datos "$D2" "la base del servicio, ANTES de tocarla"
# La guarda va ANTES de envenenar, no sólo después: los ficheros de un uid ajeno los
# puede dejar una LECTURA (ver `lee_eid`), o sea que pueden estar ahí antes de que este
# bloque escriba nada. Detectarlo al final decía «no medido» con razón pero tres pasos
# tarde, y lo que se veía por delante era `readonly database` en el sitio equivocado.
ajenos_en_datos "$D2" || MONTADO=""
paso "el envenenado de la huella de esquema" en_contenedor "$D2" "
import sqlite3
c=sqlite3.connect('/datos/h.sqlite',timeout=20)
c.execute(\"INSERT OR REPLACE INTO meta VALUES ('schema_v','huella-obsoleta')\"); c.commit()
r=c.execute(\"SELECT v FROM meta WHERE k='schema_v'\").fetchone(); c.close()
# CONTROL POSITIVO, y es el que faltaba: salir con 0 dice que el programa no
# reventó, NO que el dato haya aterrizado. Se relee lo escrito o esto no vale.
assert r and r[0] == 'huella-obsoleta', f'el veneno no aterrizo: {r}'
print('  (huella envenenada)')" || MONTADO=""
# Se daña la FRANJA CENTRAL (25 %–65 %), no la cabecera ni la cola, y la elección
# tiene motivo: así se reproduce la forma REAL del incidente —páginas de una tabla
# grande ilegibles con el resto intacto, que es lo que permitió rescatar 5 cursores
# y 13 lecturas de la base rota—. Reventar la cabecera haría irrecuperable el estado
# por construcción y el brazo del cursor fallaría por culpa del test, no del código.
paso "el daño a la franja central del índice" en_contenedor "$D2" "
import os
f='/datos/h.sqlite'
for resto in (f+'-wal', f+'-shm'):
    try: os.unlink(resto)
    except FileNotFoundError: pass
n=os.path.getsize(f); a=int(n*0.25); b=int(n*0.65)
basura=os.urandom(b-a)
with open(f,'r+b') as h:
    h.seek(a); h.write(basura); h.flush(); os.fsync(h.fileno())
    h.seek(a); leido=h.read(b-a)
# Mismo control que arriba: se relee la franja escrita. Sin esto, un fallo de
# permisos que no levante excepción dejaría el bloque midiendo una base sana.
assert leido == basura, 'la basura no aterrizo en el fichero'
print(f'  (dañados {(b-a)//1024} KB de {n//1024} KB, franja {a}-{b})')" || MONTADO=""
# ⚠️ EL PASO 0 DE TODO ESTE BLOQUE. Hasta el 2026-08-11 los DOS pasos de arriba
# escribían desde el host y en Linux fallaban los dos —`h.sqlite` lo crea el
# contenedor como uid 1000 en 0644, y `chmod 777` sobre el DIRECTORIO no alcanza al
# FICHERO—: 5 de las 7 puertas de este bloque certificaban en VERDE una
# reconstrucción que nunca ocurrió, y en macOS no se veía porque Docker Desktop
# virtualiza la propiedad. Ahora escriben con `en_contenedor` (ver su cabecera) y
# releen lo escrito. Esta guarda se queda igual: es la que separa «midió y salió
# mal» de «no llegó a medirse», y esa distinción no la arregla ningún permiso.
if [ -z "$MONTADO" ]; then
  echo "  ⏭️  el índice NUNCA se llegó a dañar: lo que sigue NO mide la cura de corrupción."
fi
if [ -n "$MONTADO" ]; then
  ajenos_en_datos "$D2" || MONTADO=""
fi
# Falsador: sin auto-reparación esto se queda sirviendo 500 —o con el ledger en
# `rotos`— para siempre, que es literalmente lo que hacía antes.
CURADO=""; cod=""; ROTOS2="?"
for _ in $(seq 1 40); do
  cod=$(curl -s -o /dev/null -w '%{http_code}' -m 10 "${B[@]}" "$V/entries?ledger=g&limit=1")
  if [ "$cod" = "200" ]; then
    ROTOS2=$(curl -s "$V/health" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("rotos") or "")')
    N=$(curl -s "${B[@]}" "$V/stat" | python3 -c 'import sys,json;print(json.load(sys.stdin)[0]["entradas"])' 2>/dev/null)
    [ -z "$ROTOS2" ] && [ "${N:-0}" -ge 30050 ] && { CURADO="sí"; break; }
  fi
  sleep 1
done
# DIAGNÓSTICO CUANDO FALLA, porque este rojo sólo aparece en Linux y no se
# reproduce en el Mac de quien lo mantiene. Sin esto, el informe dice QUÉ falló y
# nunca POR QUÉ, y cada push es otra ronda de adivinar. El servicio ya imprime lo
# que hace al reconstruir: basta traerlo al informe.
if [ -z "$CURADO" ]; then
  echo "  ── diagnóstico (últimas líneas del servicio) ──"
  docker logs --tail 25 "$NOM2" 2>&1 | grep -iE 'reconstru|corrupt|ilegible|malformed|error|suelo' \
    | tail -8 | sed 's/^/     /'
  echo "     estado: cod=$cod · rotos=«$ROTOS2» · entradas=${N:-?}"
fi
if [ -z "$MONTADO" ]; then
  no_medido "el índice corrupto se reconstruye solo" "el índice nunca se llegó a dañar"
elif [ -n "$CURADO" ]; then
  ok "el índice corrupto se reconstruye solo (y vuelven las 30.050)"
else
  fallo "el índice corrupto se reconstruye solo" "200, sin rotos y 30.050 entradas" \
        "cod=$cod rotos=$ROTOS2 entradas=${N:-?}"
fi
# EL OTRO BRAZO, y es el que separa una cura de una cura que sale cara: reconstruir
# es trivial si se tira todo. `cursors` y `lecturas` NO salen del markdown — sin
# rescate, cada hipo del disco le vaciaría la bandeja al equipo. Mismo daño que se
# arregló el 28-jul por el lado del censo, ahora por el lado de la corrupción.
CUR_DESPUES=$(curl -s "${B[@]}" "$V/cursor/alice-backend" | python3 -c 'import json,sys;print(json.load(sys.stdin)["g"])')
# Va CONDICIONADA a que la reconstrucción haya ocurrido de verdad, y no es celo:
# tal como estaba escrita salía VERDE contra el mutante que desactiva la cura —si
# nadie reconstruye, nadie toca el cursor y «sobrevive» trivialmente—. Una
# comprobación que pasa tanto si el rescate funciona como si no pasa nada no
# comprueba el rescate. Cazado corriendo el humo contra el mutante, que es para lo
# que existe el mutante.
EID_DESPUES=$(lee_eid "$CUR_DESPUES")
# El CONTROL manda sobre la propiedad, y por eso se mira antes: «el número SÍ
# cambió» es lo único que distingue «hubo renumeración y el re-anclaje la
# compensó» de «no pasó nada y por eso coinciden». Con el segundo caso, comparar
# los dos `eid` no prueba el rescate: lo pasaría igual con la cura puesta Y sin
# ella.
# 🔴 Y el PRIMER guarda es `MONTADO`, no `CURADO`, porque aquí `CURADO` MIENTE:
# sin daño el índice está sano y la sonda dice «curado» igual. Con `CURADO` de
# guarda, el caso REAL del CI de Linux —los dos `python3 -c` de arriba revientan
# por permisos y `MONTADO` queda vacío— caía en la rama de abajo y publicaba
# `✗ … esperado: ≠ 500 · obtenido: 500`: una discrepancia MEDIDA, cuando no se
# había medido nada. O sea, la avería que este parche existe para matar, viva
# DENTRO del parche. El hermano de abajo (`verify`) ya guardaba con `MONTADO`;
# esto era una inconsistencia, no una decisión. Cazado en revisión adversarial y
# reproducido ejecutando el bloque con MONTADO="" · CURADO="sí" · 500 = 500.
if [ -z "$MONTADO" ]; then
  no_medido "y el cursor apunta A LA MISMA ENTRADA tras reconstruir" \
            "el índice nunca se llegó a dañar: no hubo reconstrucción que observar"
  no_medido "el número tenía que cambiar o esto no prueba nada" \
            "el índice nunca se llegó a dañar: no hubo renumeración que observar"
elif [ -z "$CURADO" ]; then
  no_medido "y el cursor apunta A LA MISMA ENTRADA tras reconstruir" \
            "no hubo reconstrucción: no hay nada que re-anclar"
  no_medido "el número tenía que cambiar o esto no prueba nada" \
            "no hubo reconstrucción: no hay renumeración que observar"
elif [ "$CUR_ANTES" = "$CUR_DESPUES" ]; then
  no_medido "y el cursor apunta A LA MISMA ENTRADA tras reconstruir" \
            "su CONTROL falla justo debajo: sin renumeración que compensar, comparar los dos eid es vacuo"
  fallo "el número tenía que cambiar o esto no prueba nada" "≠ $CUR_ANTES" "$CUR_DESPUES"
else
  comp "y el cursor apunta A LA MISMA ENTRADA tras reconstruir" "$EID_ANTES" "$EID_DESPUES"
  ok "y el número SÍ cambió ($CUR_ANTES → $CUR_DESPUES): hubo renumeración que compensar"
fi
# Falsador: si la reconstrucción no dejara rastro, `verify` diría «sin pérdidas»
# sobre una ventana que este servicio no vigiló.
if [ -z "$MONTADO" ]; then
  no_medido "la reconstrucción queda registrada en verify" "no hubo reconstrucción que registrar"
else
  VERI=$(cuerpo "${B[@]}" "$V/chain/verify")
  case "$VERI" in
    NO-MEDIDO:*) no_medido "la reconstrucción queda registrada en verify" "${VERI#NO-MEDIDO:}" ;;
    *) grep -q 'reconstrucción' <<<"$VERI" \
         && ok "la reconstrucción queda registrada en verify" \
         || fallo "la reconstrucción queda registrada en verify" "línea con «reconstrucción»" "no aparece" ;;
  esac
fi
# Y el reverso: haberse curado NO puede dejar el servicio en rojo permanente. Un
# rojo que no se puede apagar enseña a apagar la alarma.
comp "y /health NO se queda rojo por haberse curado" "True" \
  "$(curl -s "$V/health" | python3 -c 'import sys,json;print(json.load(sys.stdin)["ok"])')"

# EL RESCATE TIENE QUE SOBREVIVIR AL REINICIO SIGUIENTE, y esto es lo que separa un
# arreglo de un arreglo que se deshace solo. La base reconstruida lleva la huella de
# esquema de AHORA, no la que se rescató de la base rota: si llevara la vieja —o
# ninguna—, el arranque siguiente vería «esquema cambiado» y haría `DROP TABLE
# cursors`, o sea le quitaría al equipo el estado de lectura que se acababa de
# rescatar, y en un reinicio que nadie relaciona con la corrupción de ayer. Es el
# daño del arreglo del 2026-07-28 entrando por la puerta de al lado.
HUELLA=$(python3 -c "
import hashlib,re
v=re.search(r'^SCHEMA_V = (\d+)', open('$PWD/servicio.py').read(), re.M).group(1)
print(hashlib.sha256(v.encode()).hexdigest()[:16])")
GUARDADA=$(en_contenedor "$D2" "
import sqlite3
c=sqlite3.connect('file:/datos/h.sqlite?mode=ro',uri=True,timeout=20)
r=c.execute(\"SELECT v FROM meta WHERE k='schema_v'\").fetchone()
print(r[0] if r else 'NINGUNA')")
# Falsador: sin el arreglo aquí saldría `huella-obsoleta`, la que se envenenó arriba.
if [ -z "$MONTADO" ]; then
  no_medido "la base nueva lleva la huella de esquema de AHORA" \
            "la huella no se llegó a envenenar: pasa igual con el arreglo y sin él"
else
  comp "la base nueva lleva la huella de esquema de AHORA" "$HUELLA" "$GUARDADA"
fi
docker restart "$NOM2" >/dev/null 2>&1
esperar_salud "$V" 60 "el contenedor tras 'docker restart'" "$NOM2"
sleep 3
# Falsador: si el arranque hubiera entrado por la rama de «esquema cambiado», esto
# volvería a -1 — que es el número exacto del defecto que este repo ya arregló una vez.
CUR_REINICIO=$(curl -s "${B[@]}" "$V/cursor/alice-backend" | python3 -c 'import json,sys;print(json.load(sys.stdin)["g"])')
comp "y el cursor SIGUE ahí tras reiniciar" "$CUR_DESPUES" "$CUR_REINICIO"
# ⚠️ Si `docker logs` viene vacío —contenedor muerto, nombre mal— el `grep` falla
# y esto salía VERDE: un log que no se puede leer no absuelve a nadie.
LOGS_ARR=$(docker logs "$NOM2" 2>&1)
if [ -z "$LOGS_ARR" ]; then
  no_medido "el arranque no debe ver «esquema cambiado»" "docker logs vino VACÍO"
elif grep -q "ESQUEMA cambiado" <<<"$LOGS_ARR"; then
  fallo "el arranque no debe ver «esquema cambiado»" "sin esa línea" "la imprimió"
else
  ok "y el arranque no vio «esquema cambiado» (no había por qué tirar nada)"
fi

# EL CAMINO DE ARRANQUE, que hasta aquí no ejercitaba ningún caso. Todo lo de arriba
# corrompe con el servicio VIVO, y entonces quien cura es el vigilante. Pero el
# incidente real se vio de la OTRA forma: el proceso arrancó sobre una base que YA
# estaba corrupta y siguió catorce horas sirviendo a medias. Esa cura la hace
# `lifespan`, que es otro camino —corre antes de la migración de esquema, sin
# vigilante y sin `/health` todavía en pie—, y un camino que sólo se recorre el peor
# día del servicio es justo el que no se puede dejar sin probar.
docker stop "$NOM2" >/dev/null 2>&1
MONTADO2="sí"
paso "el daño al índice ANTES del arranque" en_contenedor "$D2" "
import os
f='/datos/h.sqlite'
for resto in (f+'-wal', f+'-shm'):
    try: os.unlink(resto)
    except FileNotFoundError: pass
n=os.path.getsize(f); a=int(n*0.25); b=int(n*0.65)
basura=os.urandom(b-a)
with open(f,'r+b') as h:
    h.seek(a); h.write(basura); h.flush(); os.fsync(h.fileno())
    h.seek(a); leido=h.read(b-a)
assert leido == basura, 'la basura no aterrizo en el fichero'" || MONTADO2=""
docker start "$NOM2" >/dev/null 2>&1
ARR=""
for _ in $(seq 1 60); do
  cod=$(curl -s -o /dev/null -w '%{http_code}' -m 10 "${B[@]}" "$V/entries?ledger=g&limit=1")
  if [ "$cod" = "200" ]; then
    N2=$(curl -s "${B[@]}" "$V/stat" | python3 -c 'import sys,json;print(json.load(sys.stdin)[0]["entradas"])' 2>/dev/null)
    [ "${N2:-0}" -ge 30050 ] && { ARR="sí"; break; }
  fi
  sleep 1
done
# Falsador: sin la sonda de `lifespan`, el arranque se come la base corrupta — o
# revienta en la migración de esquema y el contenedor no llega ni a contestar.
if [ -z "$MONTADO2" ]; then
  no_medido "un índice corrupto en el arranque se cura" "el índice nunca se llegó a dañar"
elif [ -n "$ARR" ]; then
  ok "y un índice corrupto EN EL ARRANQUE también se cura"
else
  fallo "un índice corrupto en el arranque se cura" "200 con 30.050 entradas" "cod=$cod entradas=${N2:-?}"
fi
fi   # cierra la guarda de la precondición (bloque de corrupción)

limpiar2

echo "── una cita a la COLA no puede aterrizar en el ledger de al lado ──"
# El gate de citas de la wiki resolvía el destino por PARECIDO DE NOMBRE, y dos
# ledgers del mismo repo comparten prefijo: `repo/LEDGER.md` y `repo/WIKI-QUEUE.md`.
# Aplastado a una pista (`/`→`-`, sin `.md`), `repo/WIKI-QUEUE.md` da
# `repo-wiki-queue`, donde el nombre `repo` SÍ es subcadena y `repo-queue` NO —sobra
# un `wiki-` de juntar repo y fichero—, así que la cita a la cola se resolvía contra
# el ledger vecino y salía ROTA. Medido en el corpus real: 39 de 56 «citas rotas»
# eran esto, no la wiki. Es la SEGUNDA vez que el atajo del parecido produce el mismo
# error con otra forma; por eso ahora se resuelve por la RUTA del montaje.
NOM3="humo3-$$"; P3=$((PUERTO+2)); TMP3="$(mktemp -d)"; D3="$(mktemp -d)"; W3="$(mktemp -d)"
chmod 755 "$TMP3" "$W3"; chmod 777 "$D3"; mkdir -p "$TMP3/repo"
limpiar3() { docker rm -f "$NOM3" >/dev/null 2>&1; rm -rf "$TMP3" "$D3" "$W3"; }
trap 'limpiar; limpiar2; limpiar3' EXIT
printf '# tronco\n\n### [alice-backend → bob-reviewer · FYI] en el tronco 2026-07-01T10:00:00Z\ncuerpo tronco\n' > "$TMP3/repo/LEDGER.md"
# CABECERA PARTIDA — el caso real que rompía el gate. Una cabecera de 5.352
# caracteres, partida por su autor, dejaba el sello en la SEGUNDA línea; el gate
# exigía que el ancla estuviera en la PRIMERA y declaraba rota una cita perfecta.
# 17 de las 22 «rotas» que quedaban en el corpus real eran esto.
printf '\n### [alice-backend → bob-reviewer · FYI] cabecera larga que su autor partió\n 2026-07-05T08:08:08Z — MARK:cabecera-partida\ncuerpo de la partida\n' >> "$TMP3/repo/LEDGER.md"
# ECO EN LA PROSA — el reverso, y sin él lo de arriba es peligroso. Esta entrada
# MENCIONA un sello en su cuerpo, después de una línea en blanco. Citarlo NO puede
# valer: el eco de un ancla no es el ancla, y aflojar el gate para tragar cabeceras
# largas no puede aflojarlo también para la prosa.
printf '\n### [bob-reviewer → alice-backend · FYI] la que habla de otra 2026-07-06T07:07:07Z\n\nComo dije el 2026-07-09T09:09:09Z, aquello quedó pendiente.\n' >> "$TMP3/repo/LEDGER.md"
printf '# cola\n\n### [alice-backend → bob-reviewer · FYI] en la cola 2026-07-02T11:22:33Z\ncuerpo cola\n' > "$TMP3/repo/WIKI-QUEUE.md"
# La página cita a la COLA. El sello SÓLO existe ahí: si el destino se resuelve al
# tronco, no hay forma de que resuelva — que es exactamente el defecto.
printf -- '---\ntitle: p\n---\n\n# p\n\nUn hecho. [source: repo/WIKI-QUEUE.md 2026-07-02T11:22:33Z]\n' > "$W3/p.md"
docker run -d --name "$NOM3" -p "127.0.0.1:$P3:8077" \
  -e LLMINBOX_LEDGERS="repo-tronco=/l/repo/LEDGER.md,repo-tronco-queue=/l/repo/WIKI-QUEUE.md" \
  -e LLMINBOX_TOKEN="$TOK" -e LLMINBOX_WATCHER_TOKEN="$WTOK" \
  -e LLMINBOX_DB=/datos/h.sqlite -e LLMINBOX_POLL=1 \
  -e LLMINBOX_ROSTER=/censo.json -e LLMINBOX_WIKI=/wiki \
  -v "$TMP3:/l:ro" -v "$D3:/datos" -v "$W3:/wiki:ro" \
  -v "$PWD/roster.example.json:/censo.json:ro" "${IMAGEN:-llminbox:test}" >/dev/null
esperar_salud "http://127.0.0.1:$P3" 40 "el contenedor de las citas"
sleep 4
C3=(-H "X-Llminbox-Token: $TOK"); Y="http://127.0.0.1:$P3"
# Falsador: con el mapeo por parecido esto sale con una línea ✗ («NO existe esa
# entrada»), porque busca el sello de la cola dentro del tronco.
SAL=$(cuerpo -m 30 "${C3[@]}" "$Y/wiki/citas?solo_rotas=true")
case "$SAL" in
  NO-MEDIDO:*) no_medido "la cita a la cola resuelve" "${SAL#NO-MEDIDO:}" ;;
  *) grep -q '✗' <<<"$SAL" \
       && fallo "la cita a la cola resuelve" "sin ✗" "$(grep -m1 '✗' <<<"$SAL")" \
       || ok "la cita a la cola resuelve contra la COLA, no contra el tronco" ;;
esac
# Falsador: si el gate volviera a exigir el ancla en la PRIMERA línea, esta cita —a
# una cabecera que su autor partió— saldría rota, que es lo que hacía.
printf -- '\nCabecera partida. [source: repo/LEDGER.md 2026-07-05T08:08:08Z]\n' >> "$W3/p.md"; sleep 4
SAL2=$(cuerpo -m 30 "${C3[@]}" "$Y/wiki/citas?solo_rotas=true")
case "$SAL2" in
  NO-MEDIDO:*) no_medido "una cabecera PARTIDA resuelve igual" "${SAL2#NO-MEDIDO:}" ;;
  *) grep -q '2026-07-05T08:08:08' <<<"$SAL2" \
       && fallo "una cabecera PARTIDA resuelve igual" "sin ✗ para ese sello" "sale rota" \
       || ok "una cabecera PARTIDA en dos líneas resuelve igual" ;;
esac
# Y EL REVERSO, que es lo que impide que el arreglo se coma el gate: un sello que
# sólo aparece en la PROSA de una entrada NO vale como cita. Sin esta comprobación,
# relajar la regla para tragar cabeceras largas se lleva por delante la garantía.
printf -- '\nEco en la prosa. [source: repo/LEDGER.md 2026-07-09T09:09:09Z]\n' >> "$W3/p.md"; sleep 4
SAL3=$(cuerpo -m 30 "${C3[@]}" "$Y/wiki/citas?solo_rotas=true")
case "$SAL3" in
  NO-MEDIDO:*) no_medido "un sello que sólo está en la prosa no puede resolver" "${SAL3#NO-MEDIDO:}" ;;
  *) grep -q '2026-07-09T09:09:09' <<<"$SAL3" \
       && ok "y un sello citado desde la PROSA sigue sin valer (el eco no es el ancla)" \
       || fallo "un sello que sólo está en la prosa no puede resolver" "sale roto" "resolvió" ;;
esac
# CONTROL: que el gate sepa decir ✗ cuando toca. Sin esto, un gate que nunca ve
# nada también pasaría la comprobación de arriba — el verde vacío de siempre.
printf -- '\nOtro hecho. [source: repo/WIKI-QUEUE.md 2026-01-01T00:00:00Z]\n' >> "$W3/p.md"; sleep 4
# Éste es el CONTROL de que el gate no está mudo: si se mide sobre una
# respuesta muerta, dice «el gate está mudo» y acusa al producto de lo
# contrario de lo que pasa.
SAL4=$(cuerpo -m 30 "${C3[@]}" "$Y/wiki/citas?solo_rotas=true")
case "$SAL4" in
  NO-MEDIDO:*) no_medido "un sello inventado tiene que salir roto" "${SAL4#NO-MEDIDO:}" ;;
  *) grep -q '✗' <<<"$SAL4" \
       && ok "y un sello inventado SÍ sale roto (el gate no está mudo)" \
       || fallo "un sello inventado tiene que salir roto" "una línea ✗" "ninguna" ;;
esac
limpiar3

echo "── el cerrojo de escritura: pasada corta y lectura que no depende de ella ──"
# Contenedor PROPIO, y no es ceremonia. La primera versión de estas dos
# comprobaciones colgaba de `$NOMBRE`, cuyo ledger ya habían reescrito los tests de
# integridad de más arriba: pasaban IGUAL con el código roto —las corrí contra una
# imagen rota a propósito y salieron verdes— porque leían una línea de log que no
# era la del apéndice. Un gate que hereda estado ajeno mide el estado ajeno.
T4=$(mktemp -d); chmod 755 "$T4"   # 700 + uid 1000 = el contenedor no lee (ver cabecera)
NOM4="humo4-$$"; P4=$((PUERTO+3))
limpiar4() { docker rm -f "$NOM4" >/dev/null 2>&1; rm -rf "$T4"; }
# La base va DENTRO del contenedor (`/tmp`), no en un bind-mount del host, y esto es
# la diferencia entre medir y no medir: sobre el montaje de macOS los locks POSIX no
# se propagan entre procesos —comprobado: con una transacción `BEGIN IMMEDIATE`
# abierta, otro proceso escribía tan tranquilo— así que una prueba de contención
# montada ahí sale verde SIEMPRE. En producción la base vive en un volumen de Docker
# (ext4 dentro de la VM), donde el cerrojo sí es real: por eso el fallo existe.
printf '# t\n\n### [alice-backend → bob-reviewer · REQUEST] una\ncuerpo uno\n' > "$T4/l.md"
printf '\n### [bob-reviewer → alice-backend · ACK] dos\ncuerpo dos\n' >> "$T4/l.md"
docker run -d --name "$NOM4" -p "127.0.0.1:$P4:8077" \
  -e LLMINBOX_LEDGERS="t=/l/l.md" -e LLMINBOX_TOKEN="$TOK" \
  -e LLMINBOX_WATCHER_TOKEN="$WTOK" \
  -e LLMINBOX_DB=/tmp/h.sqlite -e LLMINBOX_POLL=1 -e LLMINBOX_ROSTER=/censo.json \
  -v "$T4:/l:ro" -v "$PWD/roster.example.json:/censo.json:ro" \
  "${IMAGEN:-llminbox:test}" >/dev/null || { echo "no arrancó $NOM4"; FALLOS=$((FALLOS+1)); }
esperar_salud "http://127.0.0.1:$P4" 40 "el contenedor del cerrojo"
sleep 3
U4="http://127.0.0.1:$P4"

# (1) El 2026-08-08 el indexador emitía UNA `UPDATE` por entrada YA CONOCIDA en cada
# pasada: para meter una entrada nueva en un ledger de 32.761 reescribía las 32.761
# con los mismos valores. Como la transacción se abre en la primera escritura y no se
# cierra hasta el `commit` final, el cerrojo quedaba tomado la pasada entera —39,04 s
# medidos— y los demás escritores morían con «database is locked».
# falsador: con las escrituras no-op de vuelta esto sale 2, no ≤1. COMPROBADO contra
# una imagen rota a propósito: da `1 nuevas, 2 refrescadas`.
printf '\n### [alice-backend → bob-reviewer · FYI] tres\ncuerpo tres\n' >> "$T4/l.md"
sleep 4
LINEA=$(docker logs "$NOM4" 2>&1 | grep -E '\[vigilante\] t: 1 nuevas,' | tail -1)
REFR=$(printf '%s' "$LINEA" | sed -nE 's/.*, ([0-9]+) refrescadas.*/\1/p')
# CONTROL primero: sin la línea del apéndice, lo de abajo no mide nada.
[ -n "$REFR" ] && ok "el vigilante publica 'N refrescadas' tras el apéndice (hay qué medir)" \
  || fallo "hace falta la línea del apéndice en el log" "'1 nuevas, N refrescadas'" "${LINEA:-ninguna}"
[ -n "$REFR" ] && [ "$REFR" -le 1 ] \
  && ok "y un apéndice puro refresca $REFR fila(s) (≤1: sólo la que deja de ser la última)" \
  || fallo "un apéndice no puede reescribir las filas ya conocidas" "≤1" "${REFR:-sin dato}"

# (2) Con la base tomada por otro escritor, `GET /inbox` escribía su fila de telemetría,
# agotaba la espera y devolvía 500: un dato accesorio dejaba sin bandeja a quien venía
# a leerla (212 peticiones caídas ese día). Ahora esa escritura se rinde en 250 ms.
# `BEGIN IMMEDIATE` explícito, y con `isolation_level=None` es obligatorio: en
# autocommit un `INSERT` suelto COMMITEA solo y suelta el cerrojo en el mismo
# instante, así que el tomador no tomaba nada. Me pasó, y lo cazó el control de
# abajo diciendo LIBRE — sin él habría publicado un 200 que no probaba nada.
docker exec -d "$NOM4" python3 -c "
import sqlite3,time
c=sqlite3.connect('/tmp/h.sqlite',timeout=30,isolation_level=None)
c.execute('BEGIN IMMEDIATE')
time.sleep(10)          # transacción ABIERTA: el cerrojo de escritura sigue tomado
" || { echo "  (el exec del cerrojo no arrancó)"; FALLOS=$((FALLOS+1)); }
sleep 2
# CONTROL POSITIVO, la pieza que faltaba: probar que el cerrojo ESTÁ tomado de verdad.
# Sin esto, un `docker exec` que falla en silencio deja la lectura sin competencia y
# el 200 de abajo no prueba nada — que es exactamente como se me coló la vez anterior.
TOMADO=$(docker exec "$NOM4" python3 -c "
import sqlite3
c=sqlite3.connect('/tmp/h.sqlite',timeout=1)
try:
    c.execute(\"INSERT INTO lecturas (agent,primera,ultima,veces) VALUES ('probe','x','x',1) \"
              'ON CONFLICT(agent) DO UPDATE SET veces=veces+1'); c.commit(); print('LIBRE')
except sqlite3.OperationalError: print('TOMADO')
" 2>/dev/null)
comp "el cerrojo de escritura está tomado (control del propio falsador)" "TOMADO" "$TOMADO"
T0=$(date +%s%N)
COD=$(curl -s -o /dev/null -m 25 -w '%{http_code}' "${A[@]}" "$U4/inbox/bob-reviewer")
MS=$(( ($(date +%s%N) - T0) / 1000000 ))
# El 200 por sí solo NO falsa nada, y conviene decirlo: contra la imagen rota a
# propósito también sale 200, porque el cerrojo se suelta a los 10 s y la espera de
# 30 s de la conexión acaba ganando. El 500 sólo aparece cuando el cerrojo dura MÁS
# que esa espera —en producción, una pasada de 39 s— y montar eso aquí serían 30 s de
# test por una segunda señal. Quien muerde es el CRONÓMETRO de abajo: 6.917 ms con el
# código roto contra 303 ms con el bueno, medido. El código se queda como la mitad
# barata del par: si algún día devuelve 500, esto lo canta sin esperar.
comp "con la base ocupada, /inbox sigue dando 200" "200" "$COD"
[ "$MS" -le 3000 ] \
  && ok "y contesta en ${MS} ms (ninguna escritura suya en el camino)" \
  || fallo "la lectura no puede esperar a una escritura" "≤3000 ms" "${MS} ms"

# (3) …y el contador NO puede mentir por ello. La primera cura —proteger la escritura
# y rendirse en 250 ms— salvaba la lectura y dejaba el contador perdiendo cuentas: 51
# en 5 minutos, medido en producción. Salvar el dato principal tirando el accesorio no
# es arreglarlo, es mover el fallo a donde no se mira. Ahora se acumula en memoria y lo
# vuelca el barrido, así que la cuenta tiene que salir EXACTA.
# falsador: con la escritura de vuelta en el camino de lectura y una espera corta,
# bajo el cerrojo se pierden lecturas y este delta sale menor que 5.
veces() { docker exec "$NOM4" python3 -c "
import sqlite3
c=sqlite3.connect('file:/tmp/h.sqlite?mode=ro',uri=True)
r=c.execute(\"SELECT veces FROM lecturas WHERE agent='alice-backend'\").fetchone()
print(r[0] if r else 0)" 2>/dev/null; }
sleep 9                       # que suelte el cerrojo anterior y se vuelque lo pendiente
V0=$(veces)
# Las 5 lecturas van CON EL CERROJO TOMADO, y ahí está el filo del falsador: medirlas
# con la base libre las contaría bien en los DOS diseños, y el gate no distinguiría
# nada. Es justo bajo contención donde la versión que escribía en el camino de lectura
# se rendía a los 250 ms y perdía la cuenta.
docker exec -d "$NOM4" python3 -c "
import sqlite3,time
c=sqlite3.connect('/tmp/h.sqlite',timeout=30,isolation_level=None)
c.execute('BEGIN IMMEDIATE')
time.sleep(6)
"
sleep 1
for _ in 1 2 3 4 5; do curl -s -o /dev/null -m 10 "${A[@]}" "$U4/inbox/alice-backend"; done
sleep 8                       # suelta el cerrojo + un barrido, que es quien vuelca
V1=$(veces)
comp "5 lecturas BAJO CERROJO → el contador sube exactamente 5" "5" "$(( ${V1:-0} - ${V0:-0} ))"
limpiar4

echo "── el servicio sabe decir lo que cuesta ──"
# La métrica de éxito no son MB indexados: son tokens por lectura. Sin esto,
# «¿cuánto ahorra?» se contestó el 2026-08-08 grepeando 26 GB de transcripts, y el
# número salió mal DOS veces por dividir entre el endpoint equivocado.
# falsador: sin el middleware, /adopcion no trae la sección y esto sale 0.
curl -s "${A[@]}" "$U/inbox/alice-backend" >/dev/null
curl -s "${A[@]}" "$U/inbox/bob-reviewer"  >/dev/null
sleep 4          # el volcado lo hace el barrido, como la telemetría de lecturas
ADOP=$(curl -s -m 20 "${A[@]}" "$U/adopcion")
printf '%s' "$ADOP" | grep -q 'coste por endpoint' \
  && ok "/adopcion publica el coste por endpoint" \
  || fallo "el servicio tiene que saber lo que cuesta" "sección de coste" "no está"
# CONTROL: que la cifra venga de las llamadas de verdad y no de una fila vacía.
printf '%s' "$ADOP" | grep -qE 'GET /inbox/\{agent\}\s+[1-9]' \
  && ok "y cuenta las llamadas REALES a la bandeja (no una fila en blanco)" \
  || fallo "el contador tiene que haber contado" "≥1 llamada a /inbox/{agent}" "0 o ausente"
# Y LOS BYTES, que es la columna que importa. La primera versión contaba llamadas
# bien y registraba CERO bytes en las siete rutas —`resp.body` no existe con
# BaseHTTPMiddleware—, y este gate pasaba igual porque sólo miraba las llamadas.
# Una tabla de coste con la columna de coste a cero parece medida y no lo está.
# falsador: con `.body` de vuelta, la columna de bytes sale 0 y esto falla.
printf '%s' "$ADOP" | grep -E 'GET /inbox/\{agent\}' | awk '{print $4+0}' | grep -qvE '^0$' \
  && ok "y los BYTES servidos no son cero (la columna de coste mide algo)" \
  || fallo "una columna de coste a cero parece medida y no lo está" ">0 bytes" "0"

echo "── pedir cuerpos no puede vaciar el canal ──"
# `/entries?cuerpo=true` no capaba `limit`: medido el 2026-08-08, las 500 entradas
# más grandes de un índice real suman 6,2 MB ≈ 1.546.628 tokens EN UNA RESPUESTA, y
# la ruta acumulaba 14,1 M de tokens contra 0,45 M de toda la bandeja junta — 32×.
# ⚠️ Ledger de 25 entradas A PROPÓSITO: la primera versión de esta comprobación vivía
# en el contenedor de 4 y pedía `limit=500`, así que devolvía 4 con el tope y 4 sin
# él. Verde en los dos casos: no podía fallar. Un gate que no discrimina es teatro,
# y éste lo era hasta que se le dio un ledger más grande que su propio tope.
T7=$(mktemp -d); chmod 755 "$T7"; NOM7="humo7-$$"; P7=$((PUERTO+6))
limpiar7() { docker rm -f "$NOM7" >/dev/null 2>&1; rm -rf "$T7"; }
python3 -c "
p='$T7/g.md'
open(p,'w').write('# g' + ''.join(chr(10)*2 + f'### [alice-backend → bob-reviewer · FYI] entrada {i}' + chr(10) + 'cuerpo ' + 'x'*400 for i in range(25)) + chr(10))"
docker run -d --name "$NOM7" -p "127.0.0.1:$P7:8077" \
  -e LLMINBOX_LEDGERS="g=/l/g.md" -e LLMINBOX_TOKEN="$TOK" \
  -e LLMINBOX_WATCHER_TOKEN="$WTOK" \
  -e LLMINBOX_DB=/tmp/h.sqlite -e LLMINBOX_POLL=1 -e LLMINBOX_ROSTER=/censo.json \
  -v "$T7:/l:ro" -v "$PWD/roster.example.json:/censo.json:ro" \
  "${IMAGEN:-llminbox:test}" >/dev/null || { echo "no arrancó $NOM7"; FALLOS=$((FALLOS+1)); }
esperar_salud "http://127.0.0.1:$P7" 40 "el contenedor del tope de cuerpos"
sleep 3
V7="http://127.0.0.1:$P7"
# CONTROL primero: sin cuerpos el limit alto SÍ se respeta. Sin esto, un endpoint
# que devolviera poco por cualquier motivo daría verde abajo.
NS=$(curl -s -m 20 "${A[@]}" "$V7/entries?ledger=g&limit=500" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)))' 2>/dev/null)
[ "${NS:-0}" -ge 20 ] \
  && ok "sin cuerpos, limit=500 devuelve las $NS que hay (hay qué capar)" \
  || fallo "el control tiene que traer más de 10 filas" "≥20" "${NS:-sin dato}"
# falsador: sin el min(), esto devuelve 25.
NF=$(curl -s -m 20 "${A[@]}" "$V7/entries?ledger=g&limit=500&cuerpo=true" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)))' 2>/dev/null)
[ "${NF:-99}" -le 10 ] \
  && ok "y pidiendo cuerpos se capa a 10 (pediste 500, te dan $NF)" \
  || fallo "cuerpo=true tiene que capar el limit" "≤10 filas" "${NF:-sin dato}"
limpiar7

echo "── un ledger de ARCHIVO no se cobra en cada lectura ──"
# Un archivo guarda historia cerrada, pero sus entradas siguen dirigidas a gente: la
# bandeja las sirve para siempre. Medido sobre cuatro bandejas reales el 2026-08-08,
# pesaba entre el 0 % y el 33 % de cada lectura. Contenedor propio con DOS ledgers,
# uno excluido, para poder ver la diferencia.
T6=$(mktemp -d); chmod 755 "$T6"; NOM6="humo6-$$"; P6=$((PUERTO+5))
limpiar6() { docker rm -f "$NOM6" >/dev/null 2>&1; rm -rf "$T6"; }
printf '# vivo\n\n### [alice-backend → bob-reviewer · REQUEST] del vivo\ncuerpo\n' > "$T6/vivo.md"
printf '# viejo\n\n### [alice-backend → bob-reviewer · FYI] del archivo\ncuerpo\n' > "$T6/arch.md"
docker run -d --name "$NOM6" -p "127.0.0.1:$P6:8077" \
  -e LLMINBOX_LEDGERS="vivo=/l/vivo.md,arch=/l/arch.md" -e LLMINBOX_TOKEN="$TOK" \
  -e LLMINBOX_WATCHER_TOKEN="$WTOK" \
  -e LLMINBOX_INBOX_EXCLUIR="arch" \
  -e LLMINBOX_DB=/tmp/h.sqlite -e LLMINBOX_POLL=1 -e LLMINBOX_ROSTER=/censo.json \
  -v "$T6:/l:ro" -v "$PWD/roster.example.json:/censo.json:ro" \
  "${IMAGEN:-llminbox:test}" >/dev/null || { echo "no arrancó $NOM6"; FALLOS=$((FALLOS+1)); }
esperar_salud "http://127.0.0.1:$P6" 40 "el contenedor del ledger de archivo"
sleep 3
B6=$(curl -s -m 20 "${A[@]}" "http://127.0.0.1:$P6/inbox/bob-reviewer")
# CONTROL primero: el ledger VIVO tiene que estar, o lo de abajo pasaría con el
# servicio indexando cero — que es como el CI de Linux me pilló el 2026-08-08.
printf '%s' "$B6" | grep -q 'del vivo' \
  && ok "el ledger vivo sigue en la bandeja (hay qué medir)" \
  || fallo "el ledger vivo tiene que aparecer" "'del vivo'" "no aparece"
# falsador: sin la exclusión, el archivo aparece y esto falla.
printf '%s' "$B6" | grep -q 'del archivo' \
  && fallo "el ledger de archivo NO puede servirse en la bandeja" "sin 'del archivo'" "aparece" \
  || ok "y el de ARCHIVO no se sirve"
# Y no se oculta en silencio: esconder correo sin avisar sería peor que el ahorro.
printf '%s' "$B6" | grep -q 'fuera de la bandeja por archivo: arch' \
  && ok "y la bandeja DECLARA lo que ha dejado fuera" \
  || fallo "excluir en silencio es esconder correo" "línea 'fuera de la bandeja'" "no está"
limpiar6

echo "── una cabecera SIN flecha que nombra a @alguien SÍ se le entrega ──"
# 995 de 1.350 cabeceras sin flecha nombraban con `@` a un agente del censo, y
# llminbox no se las entregaba a NADIE: el mensaje dirigido no llegaba dirigido, y por
# eso todos leían el canal entero y cualquiera cogía el trabajo. Contenedor propio y
# corte de fecha propio, para medir las DOS mitades: que entregue, y que el corte corte.
T5=$(mktemp -d); chmod 755 "$T5"   # idem: sin esto el CI de Linux indexa CERO
NOM5="humo5-$$"; P5=$((PUERTO+4))
limpiar5() { docker rm -f "$NOM5" >/dev/null 2>&1; rm -rf "$T5"; }
printf '# t\n\n### [alice-backend habla del tema y avisa a @bob-reviewer] 2026-08-09T10:00:00Z — nueva\ncuerpo nuevo\n' > "$T5/l.md"
printf '\n### [alice-backend habla de lo viejo y avisa a @bob-reviewer] 2026-08-01T10:00:00Z — vieja\ncuerpo viejo\n' >> "$T5/l.md"

docker run -d --name "$NOM5" -p "127.0.0.1:$P5:8077" \
  -e LLMINBOX_LEDGERS="t=/l/l.md" -e LLMINBOX_TOKEN="$TOK" -e LLMINBOX_ARROBA_DESDE="2026-08-05" \
  -e LLMINBOX_WATCHER_TOKEN="$WTOK" \
  -e LLMINBOX_DB=/tmp/h.sqlite -e LLMINBOX_POLL=1 -e LLMINBOX_ROSTER=/censo.json \
  -v "$T5:/l:ro" -v "$PWD/roster.example.json:/censo.json:ro" \
  "${IMAGEN:-llminbox:test}" >/dev/null || { echo "no arrancó $NOM5"; FALLOS=$((FALLOS+1)); }
esperar_salud "http://127.0.0.1:$P5" 40 "el contenedor del router por @"
sleep 3
# SIN SELLO, Y APENDIZADA DESPUÉS DE LA PRIMERA INDEXACIÓN — que es el escenario
# real y el único que distingue las dos cosas. El 15,7 % del corpus no trae sello
# (8.550 entradas) y la primera versión del router les exigía uno ⇒ ni las miraba.
# Pero relajarlo a secas volcaba el histórico entero en las bandejas, así que lo que
# manda no es «¿es nueva?» —en la primera pasada todo lo es— sino si el ledger ya
# estaba indexado. Por eso esta línea va DESPUÉS de esperar a que se indexe.
printf '\n### [alice-backend escribe sin sello y avisa a @bob-reviewer] — sinsello\ncuerpo sin sello\n' >> "$T5/l.md"
sleep 4
BAND=$(curl -s -m 20 "${A[@]}" "http://127.0.0.1:$P5/inbox/bob-reviewer")
# CONTROL primero: si el ledger no se indexó, los dos brazos de abajo son vacío puro.
comp "el ledger de la prueba se indexó (hay qué entregar)" "3" \
  "$(curl -s "${A[@]}" "http://127.0.0.1:$P5/stat" | python3 -c 'import sys,json;print(json.load(sys.stdin)[0]["entradas"])' 2>/dev/null)"
# falsador ①: sin leer los `@`, bob no recibe NADA y esto sale 0.
printf '%s' "$BAND" | grep -q 'nueva' \
  && ok "la entrada NUEVA sin flecha llega a @bob-reviewer" \
  || fallo "una cabecera sin flecha con @nombre tiene que entregarse" "aparece 'nueva'" "no aparece"
# falsador ②, y es el que protege al operador: sin el corte por fecha, el historial
# entero se vuelca de golpe en las bandejas (12.442 entradas medidas en 25 agentes).
printf '%s' "$BAND" | grep -q 'vieja' \
  && fallo "el corte por fecha tiene que dejar fuera lo anterior" "sin 'vieja'" "aparece 'vieja'" \
  || ok "y la ANTERIOR al corte NO se entrega (el historial no inunda)"
# falsador: con la exigencia de sello de vuelta, esta entrada no llega a nadie.
printf '%s' "$BAND" | grep -q 'sinsello' \
  && ok "y una entrada NUEVA SIN SELLO también se entrega (es correo de ahora)" \
  || fallo "una entrada nueva sin sello tiene que entregarse igual" "aparece 'sinsello'" "no aparece"
echo "── reparto de trabajo: 1 coge · 3 revisan · nadie duplica ──"
# La queja medida del operador: un símbolo técnico lo tocaban entre 12 y 21 agentes,
# contra un techo de 4. Se reutiliza $NOM5 —recién creado y mío— y no el contenedor
# compartido: la tabla `claims` es ortogonal al ledger, pero el estado ajeno ya me
# vació dos gates hoy y no repito la vía.
C5=(-H "X-Llminbox-Token: $TOK" -H 'Content-Type: application/json'); V5="http://127.0.0.1:$P5"
# Los nombres salen del censo de ejemplo, no inventados: desde que un agente fuera
# del censo no puede coger trabajo, un `ag-7` cualquiera recibe su NO y la carrera
# mediría el rechazo en vez de la exclusión. Doce peticiones sobre cinco nombres:
# lo que se comprueba sigue siendo que sólo UNO acaba siendo el dueño.
# `bob-research` queda FUERA de la rotación a propósito: se reserva para la
# comprobación de normalización de más abajo. Con él dentro, el test era FLAKY por
# diseño — si ganaba la carrera, su claim posterior sobre el mismo tema normalizado
# devolvía `ok:true` («ya era tuyo»), que es la respuesta CORRECTA. Verde en este
# Mac, rojo en el CI de Linux, y el color dependía de quién ganase una carrera de
# doce. Un gate cuyo resultado depende del azar no distingue arreglado de roto.
CENSADOS=(alice-backend alice-frontend bob-reviewer destilador)
for i in $(seq 0 11); do (curl -s -m 10 "${C5[@]}" -d "{\"tema\":\"Escrow Freeze\",\"agent\":\"${CENSADOS[$((i%4))]}\"}" "$V5/claim" >/dev/null &); done
sleep 3
GAN=$(curl -s -m 10 "${A[@]}" "$V5/claim/escrow_freeze" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(1 if d.get("ejecuta") else 0)' 2>/dev/null)
# CONTROL primero: si no ganó NADIE, lo de abajo no distingue exclusión de servicio caído.
comp "de 12 que lo cogen a la vez, hay UN dueño" "1" "${GAN:-0}"
# falsador: sin el índice parcial `claims_uno_ejecuta`, entran varios y esto sale >1.
DUENOS=$(curl -s -m 10 "${A[@]}" "$V5/claims" | python3 -c 'import sys,json;print(sum(1 for c in json.load(sys.stdin)["claims"] if c["rol"]=="ejecuta"))' 2>/dev/null)
comp "y en la tabla hay exactamente 1 fila 'ejecuta'" "1" "${DUENOS:-0}"
# El tope de 3 se comprueba DENTRO de la sentencia: en dos pasos (contar y luego
# insertar) el 4º y el 5º se cuelan cuando llegan a la vez.
for i in $(seq 0 8); do (curl -s -m 10 "${C5[@]}" -d "{\"tema\":\"escrow_freeze\",\"agent\":\"${CENSADOS[$((i%4))]}\",\"rol\":\"revisa\"}" "$V5/claim" >/dev/null &); done
sleep 3
REV=$(curl -s -m 10 "${A[@]}" "$V5/claim/escrow_freeze" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["revisan"]))' 2>/dev/null)
# falsador: sin el tope en la sentencia, de 9 entran 4, 5 o los 9.
comp "de 9 que quieren revisar, entran exactamente 3" "3" "${REV:-0}"
# Y la normalización, que es lo que hace que el cerrojo cierre algo: sin ella
# `Escrow Freeze` y `escrow-freeze` son dos temas y cada uno coge el suyo.
OTRO=$(curl -s -m 10 "${C5[@]}" -d '{"tema":"escrow-freeze","agent":"bob-research"}' "$V5/claim" | python3 -c 'import sys,json;print(json.load(sys.stdin)["ok"])' 2>/dev/null)
comp "el mismo tema escrito distinto CHOCA igual (normaliza)" "False" "${OTRO:-?}"
# Un nombre FUERA del censo no puede coger trabajo: un dedazo (`securty`) llenaría de
# fantasmas la tabla que existe para auditar el reparto, y un claim a nombre de nadie
# no se lo puede reclamar nadie.
# falsador: sin la comprobación esto devuelve ok:true y el fantasma se queda dentro.
FANT=$(curl -s -m 10 "${C5[@]}" -d '{"tema":"tema-fantasma","agent":"securty"}' "$V5/claim" | python3 -c 'import sys,json;print(json.load(sys.stdin)["ok"])' 2>/dev/null)
comp "un agente que NO está en el censo no puede coger trabajo" "False" "${FANT:-?}"
# CONTROL POSITIVO del mismo gate: uno que SÍ está en el censo tiene que poder.
BUENO=$(curl -s -m 10 "${C5[@]}" -d '{"tema":"tema-de-control","agent":"bob-reviewer"}' "$V5/claim" | python3 -c 'import sys,json;print(json.load(sys.stdin)["ok"])' 2>/dev/null)
comp "y uno del censo SÍ puede (el gate no está mudo)" "True" "${BUENO:-?}"
# Y que mirar no escriba: hoy una fila de telemetría en un GET tumbó 212 lecturas.
ANT=$(curl -s -m 10 "${A[@]}" "$V5/claims" | python3 -c 'import sys,json;print(json.load(sys.stdin)["abiertos"])')
curl -s -m 10 "${A[@]}" "$V5/claim/un-tema-que-no-existe" >/dev/null
DES=$(curl -s -m 10 "${A[@]}" "$V5/claims" | python3 -c 'import sys,json;print(json.load(sys.stdin)["abiertos"])')
comp "mirar un claim NO lo crea (el GET no escribe)" "$ANT" "$DES"
limpiar5

echo "── nadie se bloquea si el servicio muere ──"
docker stop "$NOMBRE" >/dev/null 2>&1
tail -2 "$TMP/l.md" >/dev/null 2>&1 && ok "tail sigue funcionando con el servicio parado" \
  || fallo "tail sigue funcionando" "sí" "no"

echo
[ "$FALLOS" -eq 0 ] && { echo "humo: TODO VERDE"; exit 0; } || { echo "humo: $FALLOS fallo(s)"; exit 1; }
