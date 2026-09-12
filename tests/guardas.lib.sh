#!/usr/bin/env bash
# ── LAS GUARDAS DEL ARNÉS ─────────────────────────────────────────────────────
# Vive aparte de `humo.sh` por una razón concreta y cara: el 2026-08-11 se descubrió
# que 8 comprobaciones del humo llevaban DOS DÍAS certificando en verde una
# reconstrucción que nunca ocurría, y ninguno de los 16 mutantes lo cazó — los 16
# mutan `servicio.py` o `ledger_parse.py`, o sea EL PRODUCTO. **Nadie muta el
# ANDAMIO.** Un mutante no encuentra una puerta que no llega a ejecutarse.
#
# Con las guardas en un fichero sourceable, `tests/guardas.sh` puede darles su
# ENTRADA MALA y exigir que se quejen. Quien las cambie, que rompa ese fichero.
#
# Se `source`a desde humo.sh (que aporta FALLOS e IMAGEN) y desde guardas.sh.

ok()   { printf "  ✓ %s\n" "$1"; }
fallo(){ printf "  ✗ %s\n     esperado: %s · obtenido: %s\n" "$1" "$2" "$3"; FALLOS=$((FALLOS+1)); }

# ── «NO SE MIDIÓ» no es «el valor está mal» ───────────────────────────────────
# Los dos entraban por la misma puerta y el informe mandaba a arreglar lo que no
# estaba roto. Medido el 2026-08-09 sobre dos corridas de CI (31309566135 y
# 31307587216): de los 3 rojos del bloque de corrupción, uno decía «el cursor
# cambió: obtenido ⟨vacío⟩» y lo que había pasado es que el servicio NO CONTESTÓ
# —`JSONDecodeError: Expecting value: line 1 column 1`, o sea cuerpo vacío— tras
# agotar en silencio los 60 intentos de la espera a /health (63,97 s medidos).
#
# `NO-MEDIDO` SIGUE CONTANDO COMO FALLO, y eso no es negociable: si no contara,
# romper el transporte apagaría la suite entera, que es la avería que elegiría un
# despiste — o alguien con prisa por poner el CI en verde.
no_medido() {
  printf "  ⚠️  %s — NO SE MIDIÓ (%s)\n     no se afirma nada sobre el valor · cuenta como fallo\n" "$1" "$2"
  FALLOS=$((FALLOS+1))
}

comp() {
  case "${3-}" in NO-MEDIDO:*) no_medido "$1" "${3#NO-MEDIDO:}"; return ;; esac
  case "${2-}" in NO-MEDIDO:*) no_medido "$1" "${2#NO-MEDIDO:}"; return ;; esac
  # Un lado VACÍO no es «otro valor»: es que el `python3` de la tubería reventó
  # (clave ausente, JSON roto, cuerpo vacío) y `stdout` salió en blanco. Esta
  # línea cubre de golpe las 37 tuberías `curl | python3` del fichero.
  # Y con los DOS lados vacíos `comp` daba VERDE por igualdad vacua, que es lo
  # peor de todo: dos no-mediciones se confirmaban la una a la otra.
  if [ -z "${2-}" ] || [ -z "${3-}" ]; then
    no_medido "$1" "un lado de la comparación vino VACÍO"; return
  fi
  [ "$2" = "$3" ] && ok "$1" || fallo "$1" "$2" "$3"
}

# Devuelve el CUERPO de la respuesta, o `NO-MEDIDO:<motivo>`. Es la pieza que
# separa «contestó algo que no esperaba» de «no contestó».
cuerpo() {                      # cuerpo <args-de-curl…>
  local tmp cod rc; tmp="$(mktemp)"
  cod="$(curl -s -o "$tmp" -w '%{http_code}' "$@")"; rc=$?
  if [ "$rc" -ne 0 ];           then rm -f "$tmp"; printf 'NO-MEDIDO:curl-rc%s' "$rc"; return; fi
  if [ "${cod:-000}" -ge 400 ]; then rm -f "$tmp"; printf 'NO-MEDIDO:http-%s' "$cod"; return; fi
  if [ ! -s "$tmp" ];           then rm -f "$tmp"; printf 'NO-MEDIDO:cuerpo-vacio'; return; fi
  cat "$tmp"; rm -f "$tmp"
}

# `grep -c` sobre una respuesta muerta devuelve 0, y 0 era justo lo que varias
# comprobaciones esperaban ⇒ salían VERDES sin servicio. Aquí, si no hubo
# respuesta, no hay número: hay centinela.
contar() {                      # contar <patrón> <args-de-curl…>
  local pat="$1"; shift
  local c; c="$(cuerpo "$@")"
  case "$c" in NO-MEDIDO:*) printf '%s' "$c"; return ;; esac
  printf '%s' "$c" | grep -c "$pat"
}

# La espera a /health TIENE que saber fallar. Las diez que había eran
# `for … curl -sf … && break; sleep 1; done` sin brazo de `||`: si el servicio no
# llega, el bucle se agota EN SILENCIO y todo lo que viene después mide un
# servicio que no está — y lo reporta como valores incorrectos del producto.
esperar_salud() {               # esperar_salud <url-base> <intentos> <etiqueta> [contenedor]
  local u="$1" n="${2:-40}" et="${3:-el servicio}" cont="${4:-}" i=0
  while [ "$i" -lt "$n" ]; do
    curl -sf -m 2 "$u/health" >/dev/null 2>&1 && return 0
    i=$((i+1)); sleep 1
  done
  # DIAGNÓSTICO, porque «no contestó» tiene causas opuestas —murió al arrancar,
  # o sigue vivo indexando— y sin distinguirlas cada corrida de CI es otra ronda
  # de adivinar desde un Mac donde esto no se reproduce. El estado del contenedor
  # separa las dos en una línea.
  if [ -n "$cont" ]; then
    echo "  ── diagnóstico de «$et» ──"
    docker inspect -f '     estado={{.State.Status}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}} arrancado={{.State.StartedAt}}' "$cont" 2>&1 | tail -1
    docker logs --tail 15 "$cont" 2>&1 | sed 's/^/     /'
  fi
  no_medido "$et" "no contestó a /health en $n intentos — lo que siga NO mide el producto"
  return 1
}

# Un PASO DE PREPARACIÓN que falla sin abortar deja el bloque entero VERDE sobre
# un experimento que no ocurrió. Es la avería exacta del 2026-08-09: los dos
# `python3 -c` que envenenaban la huella y corrompían la base fallaban en Linux
# (`readonly database` y `PermissionError`) y el script seguía como si nada,
# porque corre con `set -uo pipefail` SIN `-e`. Salieron 5 verdes falsos.
paso() {                        # paso <etiqueta> <comando…>
  local et="$1"; shift
  if "$@"; then return 0; fi
  no_medido "$et" "el PASO DE PREPARACIÓN falló — lo que dependía de él no se ha medido"
  return 1
}

# ── TOCAR la base del servicio: SIEMPRE dentro de un contenedor y como su DUEÑO ──
#
# 🔑 SOBRE UNA BASE EN WAL NO EXISTE «SÓLO LEER». Abrirla con `mode=ro` CREA `-wal` y
# `-shm` al lado, propiedad de QUIEN CONECTA. El test las leía desde el host, así que
# en el runner quedaban del usuario del runner (uid 1001) pegadas a una base del
# servicio (uid 1000), y desde ese momento NADIE más podía escribir: el `INSERT` del
# veneno daba `attempt to write a readonly database` con el 0644 delante, y el arranque
# siguiente moría con exit 3. Costó TRES corridas de CI porque el síntoma sale tres
# pasos más allá del acto que lo causa, y porque en macOS no existe: Docker Desktop
# virtualiza la propiedad y ahí todo se ve del uid de quien mira. Medido:
#     /datos      modo 0777 uid 1001
#     h.sqlite    uid=1000  escribible=True
#     h.sqlite-wal / -shm   uid=1001  escribible=False   ← las dejó una LECTURA
#
# De ahí las tres decisiones de esta función, cada una pagada:
#   ① lectura y escritura pasan las DOS por aquí. No hay operación inocua.
#   ② `docker run --rm`, no `docker exec`: el daño del camino de ARRANQUE se hace con
#      el contenedor PARADO, y ahí `exec` no existe.
#   ③ el uid NO se elige, SE LEE del fichero. Las dos elecciones «de sentido común»
#      fallaron, cada una por un lado y sólo en la máquina de otro:
#        `-u 0`   → escribe siempre… y deja los laterales de root: mismo bloqueo, misma
#                   muerte al arrancar. Tapaba el defecto en vez de arreglarlo.
#        uid 1000 (el `USER` de la imagen) → verde aquí, `readonly database` en Linux.
#      Escribiendo como el dueño real deja de haber una suposición sobre uids que sólo
#      se comprueba en CI. Y si el dueño fuera root, la guarda de propiedad de más
#      abajo declara el bloque NO MEDIDO en vez de disimularlo.
en_contenedor() {               # en_contenedor <dir-datos> <programa-python>
  local d="$1" prog="$2" dueno
  dueno=$(docker run --rm -v "$d:/datos" "${IMAGEN:-llminbox:test}" python3 -c \
    "import os; print(os.stat('/datos/h.sqlite').st_uid)" 2>/dev/null)
  docker run --rm ${dueno:+-u "$dueno"} -v "$d:/datos" "${IMAGEN:-llminbox:test}" python3 -c "$prog"
}

# Quién es dueño de qué y quién puede escribir qué, dicho por un proceso DENTRO del
# contenedor. Existe porque dos corridas de CI se fueron en adivinarlo desde un Mac
# que virtualiza la propiedad: aquí este volcado es decorativo, en Linux es la prueba.
# GUARDA DE PROPIEDAD: junto a la base no puede haber un fichero de otro uid. Si lo
# hay —lo deja una escritura con el uid equivocado, o simplemente una LECTURA desde el
# host, que crea `-wal`/`-shm` a nombre de quien conecta—, el servicio no podrá
# escribirlos y todo lo que siga mide UN ARTEFACTO DEL TEST disfrazado de avería del
# producto. Es exactamente lo que pasó el 2026-08-11: el informe acusaba al arranque de
# un `readonly database` que había puesto el andamio. Devuelve 1 y NOMBRA a los
# intrusos; quien la llama declara el bloque NO MEDIDO, que es lo contrario de acusar.
# ⚠️ ALCANCE DECLARADO: sobre bind-mounts de macOS es INERTE — Docker Desktop
# virtualiza la propiedad y todo se ve del uid de quien mira, así que aquí no dirá
# nada nunca. Muerde en Linux, que es donde vive la avería. Falsada fuera del camino
# del test, sobre un volumen NOMBRADO (propiedad real, como en el runner): un fichero
# escrito con `-u 0` sale `de-root(uid=0)`; sin él, `NINGUNO`. Y de paso se vio el
# mecanismo entero, peor de lo que parecía: con propiedad real root deja el DIRECTORIO
# suyo y el uid del servicio ya no puede ni crear un fichero dentro.
ajenos_en_datos() {             # ajenos_en_datos <dir-datos> → 0 limpio · 1 hay intrusos
  local malos
  malos=$(docker run --rm -v "$1:/datos" "${IMAGEN:-llminbox:test}" python3 -c "
import os
dueno = os.stat('/datos/h.sqlite').st_uid          # el uid del SERVICIO, no el mío
print(' '.join(f'{n}(uid={os.stat(\"/datos/\"+n).st_uid})'
                for n in sorted(os.listdir('/datos'))
                if os.stat('/datos/'+n).st_uid != dueno))" 2>/dev/null)
  [ -z "$malos" ] && return 0
  echo "  ⏭️  junto a la base hay ficheros de OTRO uid ($malos): el servicio no podrá"
  echo "     escribirlos, y lo que siga mediría un artefacto del test, no el producto."
  return 1
}

propiedad_datos() {             # propiedad_datos <dir-datos> <etiqueta>
  echo "  ── propiedad de $2 (vista desde dentro del contenedor) ──"
  docker run --rm -v "$1:/datos" "${IMAGEN:-llminbox:test}" python3 -c "
import os
print('     yo=uid', os.getuid(), '· /datos modo', oct(os.stat('/datos').st_mode)[-4:],
      'uid', os.stat('/datos').st_uid, '· escribible', os.access('/datos', os.W_OK))
for n in sorted(os.listdir('/datos')):
    st = os.stat('/datos/' + n)
    print(f'     {n}: uid={st.st_uid} gid={st.st_gid} modo={oct(st.st_mode)[-4:]} '
          f'escribible={os.access(\"/datos/\" + n, os.W_OK)}')" 2>&1 | head -12
}

# ── LA IMAGEN TIENE QUE SER DEL CÓDIGO QUE PRUEBAS ───────────────────────────────
# `IMAGEN` cae por defecto a `llminbox:test`, así que EN LOCAL los arneses corrían
# cualquier imagen que existiera con ese nombre. El 2026-09-01 el humo dio «1 fallo»
# contra un servicio SANO: la imagen tenía TRECE DÍAS y devolvía un contrato viejo. El
# arnés no medía el commit, medía el disco.
#
# La CI no muerde —construye `llminbox:test` del checkout justo antes— y por eso llevaba
# tiempo invisible. Muerde en local, que es donde se corre antes de abrir una PR: un
# verde puede ser de otra semana y un rojo puede no ser tuyo.
#
# Se comprueba la EDAD y no el código: el arnés no sabe qué commit esperas, pero una
# imagen de días no puede ser del árbol que acabas de tocar. AVISA Y SIGUE — abortar
# rompería a quien prueba a propósito contra una imagen guardada.
aviso_imagen_rancia() {
  local img="${IMAGEN:-llminbox:test}" creada hoy
  creada=$(docker image inspect "$img" --format '{{.Created}}' 2>/dev/null | cut -c1-10)
  [ -n "$creada" ] || return 0
  hoy=$(date +%Y-%m-%d)
  [ "$creada" = "$hoy" ] && return 0
  echo "⚠️  la imagen '$img' es del $creada y hoy es $hoy." >&2
  echo "    Este arnés va a medir ESA imagen, no tu árbol. Si querías probar lo que" >&2
  echo "    acabas de escribir:  docker build -t $img ." >&2
  echo "    (la CI la construye siempre; en local se hereda la que haya)" >&2
}
