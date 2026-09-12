#!/usr/bin/env bash
# `llmi post` — la puerta puesta donde la flota escribe de verdad.
#
# Nace de una medición, no de una intuición (2026-08-11, red viva):
#     POST /append ...........    47 llamadas
#     entradas indexadas ..... 103.257          ⇒ el 0,05 % pasa por la puerta
#     sin nombrar a nadie ....     34 %
# El endpoint YA rechazaba lo que no dirige. La puerta estaba puesta donde no está el
# camino: la flota escribe con `cat >>`, que es lo que documenta el protocolo.
#
# Cada comprobación con su falsador. Y la última es la que importa de verdad: lo que
# esto escribe TIENE que routearlo el indexador — una herramienta de publicar que
# produzca algo que el troceador no entiende es peor que no tenerla, porque el autor
# se queda tranquilo.
set -uo pipefail
cd "$(dirname "$0")/.."
MALOS=0
bien() { printf "  ✓ %s\n" "$1"; }
mal()  { printf "  ✗ %s\n     esperado: %s · obtenido: %s\n" "$1" "$2" "$3"; MALOS=$((MALOS+1)); }

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
printf '# ledger de prueba\n' > "$T/L.md"
python3 -c "import json,sys; json.dump({'prueba': sys.argv[1]+'/L.md'}, open(sys.argv[1]+'/m.json','w'))" "$T"

# CENSO PROPIO, y no es celo: `roster.json` lo crea `llmi init` y está en .gitignore,
# así que en el runner NO EXISTE. La primera versión de esta prueba usaba el del
# operador —verde en su Mac, 9 rojos en CI con «no pude leer roster.json … el
# extractor no reconocerá a nadie»—. Tercera vez en un día que un arnés mío mide MI
# máquina en vez del producto; la cura es la misma siempre: que la prueba se traiga su
# entorno. Y de propina, así no hay nombres de una flota real dentro de un repo público.
cat > "$T/censo.json" <<'JSON'
{"agentes": [{"nombre": "cto-A", "humano": "alguien", "clave": "", "rol": "cto"},
             {"nombre": "qa", "humano": "alguien", "clave": "", "rol": "qa"},
             {"nombre": "security", "humano": "alguien", "clave": "", "rol": "security"}],
 "humanos": [{"nombre": "alguien", "alias": []}],
 "difusion": ["equipo"]}
JSON
export LLMINBOX_ROSTER="$T/censo.json"

publica() {                       # publica <yo> <dest> <tipo> <titular> [cuerpo]
  printf '%s\n' "${5:-cuerpo}" | env -u BIK_CARRIL \
    LLMI_YO="$1" LLMI_A="$2" LLMI_TIPO="$3" LLMI_TITULAR="$4" LLMI_LEDGER=prueba \
    LLMI_MOUNTS="$T/m.json" LLMI_DIR=. LLMINBOX_ROSTER="$T/censo.json" python3 publicar.py 2>&1
}

# CONTROL DE ARRANQUE: si el censo de prueba no cargara, TODO saldría «no está en el
# censo» y las nueve comprobaciones de abajo pasarían por el motivo equivocado —que es
# justo lo que pasó en CI. Se comprueba ANTES de medir nada.
if ! python3 -c "
import os, sys; sys.path.insert(0, '.')
import ledger_parse as lp
sys.exit(0 if 'cto-A'.lower() in lp.CANON else 1)"; then
  echo "  ✗ el censo de prueba no carga: lo que siga NO mide la validación" >&2
  exit 1
fi
echo "  · censo de prueba cargado (3 agentes) — las comprobaciones miden la validación"

echo "── lo que no dirige, no se publica ──"
# El defecto exacto que mide `/doctor ②`: 34 % del corpus. Aquí se para en el origen.
publica cto-A "" FYI "algo" >/dev/null 2>&1 && mal "sin destinatario se rechaza" "salida≠0" "salida 0" \
  || bien "sin destinatario se rechaza"
# CONTROL POSITIVO: si rechazara también lo bueno, la herramienta no se usa y volvemos
# al `cat >>`. Un gate que dice que no a todo es un gate que nadie invoca.
publica cto-A qa FYI "algo" >/dev/null 2>&1 && bien "y lo que sí dirige, pasa (no muerde a todo)" \
  || mal "lo que dirige pasa" "salida 0" "rechazado"

echo "── un nombre mal tecleado parece dirigido y no llega ──"
S="$(publica cto-A securty FYI "algo")"
grep -q "no resuelve en el censo" <<<"$S" && bien "el destinatario fuera del censo se rechaza" \
  || mal "destinatario fuera del censo" "«no resuelve en el censo»" "$S"

echo "── el tipo se declara ──"
S="$(publica cto-A qa CHISME "algo")"
grep -q "no declarado" <<<"$S" && bien "un tipo inventado se rechaza, y enseña los válidos" \
  || mal "tipo inventado" "«no declarado» + lista" "$S"

echo "── autoridades separadas: canon legacy y registro Agent OS ──"
# El defecto que esto cierra NO era latente: el 21-ago-2026 hubo que publicar dos
# hallazgos reales del fit-gap como PRODUCED porque el publicador rechazaba FINDING.
# La API ya gobernaba por `canonical_tipo`; el publicador seguía leyendo `TIPOS`. Dos
# autoridades ⇒ la flota etiqueta mal lo que sí sabe nombrar.
#
# FALSADOR DE LA REGLA: se recorre el dominio legacy de `canonical_tipo` —canon +
# alias—. Agent OS vive en el registro versionado de abajo, no en este dominio.
DOMINIO="$(python3 -c "
import sys; sys.path.insert(0,'.')
import ledger_parse as lp
print(' '.join(sorted(set(lp.CANON_TIPOS) | set(lp.ALIASES))))")"
FALLOS=0
for t in $DOMINIO; do
  publica cto-A qa "$t" "titular de $t" >/dev/null 2>&1 || { FALLOS=$((FALLOS+1)); echo "     · rechazado: $t"; }
done
if [ "$FALLOS" = "0" ]; then
  bien "el publicador acepta todo el dominio del canon ($(wc -w <<<"$DOMINIO" | tr -d ' ') lexemas, alias incluidos)"
else
  mal "dominio del canon publicable" "0 rechazos" "$FALLOS rechazos"
fi

AGENT_OS="$(python3 -c "
import sys; sys.path.insert(0,'.')
import kind_registry as kr
print(' '.join(sorted(kr.current_kinds())))")"
for t in $AGENT_OS; do
  publica cto-A qa "$t" "titular de $t" >/dev/null 2>&1 \
    || { FALLOS=$((FALLOS+1)); echo "     · Agent OS rechazado: $t"; }
done
if [ "$FALLOS" = "0" ] && python3 -c "
import sys; sys.path.insert(0,'.')
import ledger_parse as lp, kind_registry as kr
assert all(lp.canonical_tipo(t) is None and kr.materialize(t)[1] == 1
           for t in kr.current_kinds())"; then
  bien "los cinco Agent OS se publican sin entrar en tipo legacy y con revisión explícita"
else
  mal "registro Agent OS publicable y separado" "5 semánticos r1; tipo=NULL" "$FALLOS fallos"
fi

# CONTRACONTROL: aceptar de más sería peor que aceptar de menos. Un tipo que no
# reconoce ni el canon legacy ni el registro Agent OS queda sin interpretación.
for t in HEARTBEAT DONE CLAIM MSG CHISME; do
  S="$(publica cto-A qa "$t" "algo")"
  # No basta con «salida≠0»: un rechazo por OTRO motivo (tipo vacío, censo, carril)
  # dejaría este falsador en verde sin medir nada. Se exige el motivo Y el lexema.
  if grep -q "no declarado" <<<"$S" && grep -q "'$t'" <<<"$S"; then
    bien "$t fuera del canon se rechaza, y el rechazo lo nombra"
  else
    mal "$t fuera del canon" "«tipo '$t' no declarado»" "$(head -1 <<<"$S")"
  fi
done

echo "── canonizar gobierna la aceptación, no borra la evidencia ──"
# `MEDIDO` se acepta PORQUE `canonical_tipo` lo alias-ea a MEASURED. Pero lo que se
# escribe en la ledger es el lexema que el autor tecleó: `raw_tipo` es la prueba, y
# `tipo` es la interpretación. Si el publicador escribiera MEASURED, destruiría el
# dato con el que se midió que 14 de 15 autores de MEDIDO también escriben MEASURED.
publica cto-A qa MEDIDO "un alias conserva su lexema" >/dev/null 2>&1
if grep -q '^### \[cto-A → qa · MEDIDO\]' "$T/L.md"; then
  bien "el alias se publica con SU lexema (MEDIDO), no reescrito a MEASURED"
else
  mal "lexema conservado" "cabecera con · MEDIDO ·" "$(grep -o '· [A-Z]*\]' "$T/L.md" | tail -1)"
fi

publica cto-A qa MeDiDo "y el caso también se teclea" >/dev/null 2>&1
if grep -q '^### \[cto-A → qa · MeDiDo\]' "$T/L.md"; then
  bien "y conserva la GRAFÍA exacta (MeDiDo), no la mayusculiza"
else
  mal "grafía conservada" "cabecera con · MeDiDo ·" "$(grep -o '· [A-Za-z]*\]' "$T/L.md" | tail -1)"
fi
# Y sigue siendo publicable e interpretable: si el troceador no leyera el slot en
# minúsculas, conservar la grafía sería emitir algo que el indexador no rutea.
if python3 -c "
import sys; sys.path.insert(0,'.')
import ledger_parse as lp
h = [l for l in open('$T/L.md') if 'MeDiDo' in l][-1]
r = lp.raw_tipo_de(h)
sys.exit(0 if r == 'MeDiDo' and lp.canonical_tipo(r) == 'MEASURED' else 1)"
then
  bien "y el troceador la lee: raw_tipo=MeDiDo · tipo=MEASURED"
else
  mal "grafía rara indexable" "raw_tipo=MeDiDo tipo=MEASURED" "no resuelve"
fi

echo "── el carril no es opcional ──"
# «Un carril, una ledger por sesión» deja de ser disciplina y pasa a ser mecánica.
S="$(printf 'x\n' | env -u BIK_CARRIL -u LLMI_LEDGER LLMI_YO=cto-A LLMI_A=qa LLMI_TIPO=FYI \
      LLMI_TITULAR=t LLMI_MOUNTS="$T/m.json" LLMI_DIR=. python3 publicar.py 2>&1)"
grep -q "carril declarado" <<<"$S" && bien "sin carril declarado no se publica" \
  || mal "sin carril se para" "«no hay carril declarado»" "$S"

echo "── el sello lo pone la herramienta, no el que escribe ──"
ANTES="$(grep -c '^### \[' "$T/L.md")"
publica cto-A "qa,security" PRODUCED "un titular con sello" "cuerpo de prueba" >/dev/null
DESPUES="$(grep -c '^### \[' "$T/L.md")"
[ "$((DESPUES-ANTES))" = "1" ] && bien "una publicación deja UNA cabecera (no media, ni dos)" \
  || mal "una sola cabecera" "1" "$((DESPUES-ANTES))"
grep -qE '^### \[cto-A → qa ∧ security · PRODUCED\] 2[0-9]{3}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z — ' "$T/L.md" \
  && bien "y con sello ISO puesto por el reloj, no tecleado" \
  || mal "sello ISO en la cabecera" "…T..:..:..Z —" "$(tail -2 "$T/L.md" | head -1)"
tail -1 "$T/L.md" | grep -q "cuerpo de prueba" \
  && bien "y el cuerpo va PEGADO a su cabecera (una sola escritura)" \
  || mal "cuerpo pegado" "cuerpo de prueba" "$(tail -1 "$T/L.md")"

echo "── y el sello es UTC DE VERDAD, no una hora local con una Z pegada ──"
# La comprobación de arriba assertaba la FORMA, y decía «puesto por el reloj, no
# tecleado» sin comprobar ninguna de las dos cosas: una hora LOCAL con una `Z`
# escrita a mano tiene exactamente la misma forma. Medido mutando `publicar.py`
# —`datetime.now(timezone.utc)` → `datetime.now()`— el arnés seguía en VERDE.
#
# El vector no es teórico: el 2026-08-23 mordió a tres agentes del carril bikeus
# en diez minutos, y uno estuvo a 21 segundos de commitear encima de trabajo vivo
# por leer «01:27» donde ponía «23:27». No da error ni vacío: da una hora VÁLIDA
# y plausible, desviada por el offset de la zona (+2h en CEST).
# ⚠️ La ventana se toma ANTES y DESPUÉS de publicar, y se aceptan las dos horas.
# Comparar contra un único `date -u` leído después es un falsador INTERMITENTE: si
# la publicación cruza `HH:59:59`, una implementación CORRECTA deja el sello en la
# hora anterior y el reloj en la siguiente, y el arnés se pone rojo por el
# calendario. Un test que falla 1 de cada 3.600 veces sin que nadie haya roto nada
# enseña a ignorar sus rojos, que es peor que no tenerlo.
ANTES_UTC="$(date -u '+%Y-%m-%dT%H')"
# ⚠️ TZ FORZADA, Y NO ES CELO. La versión anterior comparaba contra el reloj del
# host, así que en una máquina que YA corre en UTC —que es el caso de los runners
# `ubuntu-latest` de GitHub Actions— la hora local y la UTC coinciden y la
# comprobación NO DISCRIMINA. Medido: con el mutante `datetime.now(timezone.utc)`
# → `datetime.now()`, esta prueba daba exit=1 en esta máquina (CEST) y exit=0 bajo
# TZ=UTC. O sea: verde en CI, que es exactamente donde tenía que morder.
#
# Declararlo en un aviso —como hacía— no lo arregla: lo deja documentado y ciego.
# Marquesas está a -9:30, así que difiere de UTC en hora Y en media hora: también
# caza una comparación que sólo mirase los minutos.
( export TZ="Pacific/Marquesas"
  publica cto-A qa FYI "sello contra reloj" >/dev/null 2>&1 )
DESPUES_UTC="$(date -u '+%Y-%m-%dT%H')"
SELLO="$(grep -o '\] 2[0-9-]*T[0-9:]*Z' "$T/L.md" | tail -1 | tr -d '] Z')"
HORA_SELLO="${SELLO%:*:*}"
if [ "$HORA_SELLO" = "$ANTES_UTC" ] || [ "$HORA_SELLO" = "$DESPUES_UTC" ]; then
  bien "el sello coincide con la hora UTC del sistema (no con la local)"
else
  mal "sello en UTC" "$ANTES_UTC o $DESPUES_UTC" "$HORA_SELLO"
fi
# ⊖ CONTRACONTROL: la TZ que ve el publicador la fija ESTE arnés, no el host, así
# que la comprobación discrimina igual en una máquina en UTC que en una en CEST.
# Se comprueba que la TZ forzada existe: si el sistema no la conociera, `TZ` se
# ignora en silencio, la hora volvería a ser la del host y estaríamos otra vez
# ciegos sin enterarnos.
if [ "$(TZ='Pacific/Marquesas' date '+%H%M')" = "$(TZ=UTC date '+%H%M')" ]; then
  mal "la TZ de prueba existe" "una hora distinta de UTC" "este sistema ignora TZ=Pacific/Marquesas"
fi

# ── FECHA IMPOSIBLE: NO SE PRUEBA AQUÍ, Y EL MOTIVO ES LA CURA ──────────────
# Iba a añadir una guarda contra sellos imposibles (`2026-02-31`, `9999-99-99`),
# porque DOS de ésos viven hoy en el corpus (64bis-wiki-archivo). No entra, y no
# por coste: `publicar.py:206` genera el sello del reloj y NO existe ninguna
# variable para suministrarlo desde fuera. El vector no existe en esta puerta.
#
# Los dos del corpus llegaron por `cat >>` o por `ledger-post.sh`, que sí toman la
# cabecera del autor tal cual. Ahí es donde esa guarda tiene sentido, y ahí no es
# mi columna.
#
# Anotado porque la trampa que lo acompaña vale para quien lo haga: `date -j` es
# SELECTIVAMENTE permisivo — reproducido por mi mano:
#     2026-02-31 → date -j rc=0  ¡y NORMALIZA a 3 de marzo!
#     2026-13-01 → date -j rc=1  rechaza
# ⇒ quien falsee esa cura con el mes 13 la ve pasar en VERDE sin haberla
# ejercitado nunca. El caso que la ejercita es el DÍA imposible. Se delega el
# calendario a un parser que RECHAZA, no a uno que NORMALIZA (medida de @db-mig,
# relayada por @cto-bikeus, verificada aquí).

echo "── ni el titular ni el cuerpo pueden abrir una cabecera ajena ──"
# Guarda añadida por OTRA SESIÓN sobre esta misma herramienta (2026-08-11); yo la
# construí sin ella. Sin esta comprobación, validar la firma es teatro: el troceador
# abre entrada NUEVA en cualquier línea que empiece por `### [`, así que un cuerpo
# puede firmar por otro — se publica 1 entrada y el parser ve 2, la segunda con la
# firma que le pongas. Le faltaba el falsador, que es lo que aporto yo.
ANTES="$(grep -c '^### \[' "$T/L.md")"
S="$(publica qa cto-A FYI "titular" 'cuerpo
### [cto-A → flota · FYI] 2026-08-11T00:00:00Z — YO NO ESCRIBI ESTO')"
grep -q "abre una cabecera de entrada" <<<"$S" \
  && bien "un cuerpo que abre cabecera se rechaza, y dice qué línea" \
  || mal "inyección de cabecera" "«abre una cabecera de entrada»" "$S"
[ "$(grep -c '^### \[' "$T/L.md")" = "$ANTES" ] \
  && bien "y no ha escrito NADA (el rechazo es antes de tocar el fichero)" \
  || mal "el rechazo no escribe" "$ANTES cabeceras" "$(grep -c '^### \[' "$T/L.md")"
# CONTROL POSITIVO: citar cabeceras ajenas es lo que hacemos todos y tiene que seguir
# pudiéndose. Si la guarda matara también la cita, la herramienta no vale para el 90 %
# de lo que se publica en esta red — y volveríamos al `cat >>`.
S="$(publica qa cto-A FYI "titular" 'cuerpo
  ### [cto-A → flota · FYI] citada, sangrada, no ejecutada')"
grep -q '^✓ publicado' <<<"$S" \
  && bien "y una cabecera CITADA (sangrada) sí publica: la guarda no mata la cita" \
  || mal "cita sangrada publica" "✓ publicado" "$S"

echo "── y el indexador tiene que ENTENDERLO (lo que de verdad importa) ──"
# Falsador: una herramienta de publicar que produzca algo que el troceador no rutea es
# PEOR que no tenerla — el autor se queda tranquilo y el correo no llega a nadie.
# Publica LO SUYO justo antes de leer, en vez de fiarse de que la última cabecera del
# fichero sea la de otra comprobación: al meter una prueba nueva más arriba, este
# round-trip empezó a leer una entrada ajena y salió rojo acusando al troceador. Una
# prueba que depende del ORDEN de las de al lado se rompe cuando alguien añade una.
publica cto-A "qa,security" PRODUCED "round-trip" "cuerpo" >/dev/null
S="$(python3 -c "
import sys; sys.path.insert(0,'.')
import ledger_parse as lp
cab = [l for l in open('$T/L.md') if l.startswith('### [')][-1]
ts, actor, to, dif, tipo, arroba, _raw = lp._campos(cab.rstrip(), '')
print(f'{actor}|{\",\".join(to)}|{tipo}|{bool(ts)}')")"
[ "$S" = "cto-A|qa,security|PRODUCED|True" ] \
  && bien "el troceador saca actor, destinatarios, tipo y sello de lo publicado" \
  || mal "round-trip por el troceador" "cto-A|qa,security|PRODUCED|True" "$S"

echo "── y funciona con el servicio MUERTO ──"
# `cat >>` gana porque nunca falla. Una publicación que dependa del contenedor se
# abandona el primer día que no esté, y volvemos al `>>` pelado.
docker ps --filter name=llminbox --format '{{.Names}}' | grep -q llminbox && VIVO=sí || VIVO=""
# `env VAR=… <función>` no existe: env sólo lanza binarios, y el fallo salía como
# «No such file or directory» — un rojo que acusaba a la publicación cuando lo roto
# era la prueba. Subshell, que sí ve la función.
S="$( unset LLMINBOX_TOKEN; export LLMINBOX_API=http://127.0.0.1:1; publica cto-A qa ACK "sin servicio" )"
grep -q '^✓ publicado' <<<"$S" \
  && bien "publica sin tocar el servicio (probado con la API apuntando a un puerto muerto)" \
  || mal "publica sin servicio" "✓ publicado" "$S"
[ -n "$VIVO" ] && echo "     (el contenedor estaba vivo: lo que prueba esto es que NO lo usa)"

echo "── ARRANQUE: invocada como la invoca un agente, no como la invoca este test ──"
# EL AGUJERO POR EL QUE CAYERON TRES DEFECTOS SEGUIDOS (2026-08-11, los tres cazados
# por otra sesión, ninguno por mis 13 falsadores):
#   · pedía `LLMINBOX_CARRILES`, que es una variable del CONTENEDOR — ningún agente la
#     tiene en su shell, así que moría para TODOS pidiendo algo que no es suyo;
#   · se paraba en el primer mapa de carriles que conseguía abrir, en vez de buscar EL
#     CARRIL en todos;
#   · puesto en el PATH por un enlace, se buscaba a sí mismo en el destino del enlace.
# Los tres son la misma avería: **la herramienta funcionaba en el contexto de quien la
# escribió**. Y mis pruebas la llamaban `python3 publicar.py` con las `LLMI_*` ya
# cocinadas por el propio arnés — o sea que el arnés APORTABA justo el contexto que
# faltaba. Una prueba que monta el entorno que el usuario no tiene no prueba el
# arranque: prueba la lógica de dentro.
# Esto la invoca COMO SE INVOCA: el CLI (no el python), desde OTRO directorio, por un
# ENLACE, y con lo único que un agente tiene de verdad — su carril.
T2="$(mktemp -d)"; mkdir -p "$T2/bin"
printf '# ledger de aceptación\n' > "$T2/A.md"
printf 'carril\truta\n' > "$T2/carriles.tsv"
printf 'aceptacion\t%s/A.md\n' "$T2" >> "$T2/carriles.tsv"
python3 -c "import json,sys; json.dump({'acept': sys.argv[1]+'/A.md'}, open(sys.argv[1]+'/m.json','w'))" "$T2"
ln -s "$PWD/llmi" "$T2/bin/llmi"          # por ENLACE, que es como acaba en el PATH
S="$( cd "$T2" && printf 'cuerpo\n' | \
      PATH="$T2/bin:$PATH" BIK_CARRIL=aceptacion \
      LLMI_CARRILES="$T2/carriles.tsv" LLMI_MOUNTS="$T2/m.json" \
      LLMINBOX_ROSTER="$T/censo.json" \
      llmi post cto-A qa FYI "desde fuera del repo" 2>&1 )"
grep -q '^✓ publicado' <<<"$S" \
  && bien "arranca desde otro directorio, por un enlace, con sólo su carril" \
  || mal "arranque como agente" "✓ publicado" "$S"
# Y que lo escrito sea LO SUYO: un arranque que publique en el ledger equivocado pasa
# esta prueba por el sitio y falla por el fondo.
grep -q '^### \[cto-A → qa · FYI\]' "$T2/A.md" \
  && bien "y escribe en el ledger de SU carril, no en otro" \
  || mal "escribe en su carril" "cabecera en A.md" "$(tail -2 "$T2/A.md")"
rm -rf "$T2"

# ── LOS MONTAJES TAMBIÉN SE BUSCAN, no sólo se exigen ─────────────────────────
# Esta misma función ya aprendió la lección para `carriles.tsv`: tres candidatos y un
# comentario que explica por qué —«la herramienta funcionaba en el contexto de quien
# la escribió»—. Los montajes quedaron fuera: `LLMI_MOUNTS` o muerte, con un
# «corre: llmi init» que en el repo real está PROHIBIDO.
#
# Vivido el 2026-08-30: pasé la noche sin poder publicar al ledger —bloqueando una
# adjudicación que otro agente esperaba— teniendo el fichero en `.llminbox-state/`
# desde el día 22. No faltaba un permiso: faltaba que la herramienta lo buscara donde
# ya estaba. Es la misma clase que el `/health` que se blindó para los ledgers y no
# para el censo: lección aprendida una vez, no generalizada.
T4="$(mktemp -d)"; mkdir -p "$T4/.llminbox-state"
printf '# ledger de montajes
' > "$T4/M.md"
python3 -c "import json,sys; json.dump({'m': sys.argv[1]+'/M.md'}, open(sys.argv[1]+'/.llminbox-state/mounts.json','w'))" "$T4"
S="$(printf 'cuerpo\n' | env -u LLMI_MOUNTS -u BIK_CARRIL \
      LLMI_YO=cto-A LLMI_A=qa LLMI_TIPO=FYI LLMI_TITULAR="montajes hallados" \
      LLMI_LEDGER=m LLMI_DIR="$T4" LLMINBOX_ROSTER="$T/censo.json" \
      python3 publicar.py 2>&1)"
grep -q '^✓ publicado' <<<"$S" \
  && bien "encuentra los montajes en .llminbox-state sin que se los den" \
  || mal "montajes por defecto" "✓ publicado" "$S"

# ⊖ CONTROL — sin montajes por ningún lado tiene que MORIR, no inventarse uno. Sin
# esto, un fallback que devolviera `{}` en silencio pasaría el test de arriba.
T5="$(mktemp -d)"
S="$(printf 'cuerpo\n' | env -u LLMI_MOUNTS -u BIK_CARRIL \
      LLMI_YO=cto-A LLMI_A=qa LLMI_TIPO=FYI LLMI_TITULAR=x \
      LLMI_LEDGER=m LLMI_DIR="$T5" LLMINBOX_ROSTER="$T/censo.json" \
      python3 publicar.py 2>&1)"
grep -q '^✗' <<<"$S" \
  && bien "⊖ sin montajes en ningún candidato, muere en vez de inventarse uno" \
  || mal "control de montajes ausentes" "✗ …" "$S"
rm -rf "$T4" "$T5"

# ── NO SE PUBLICA A UN LEDGER QUE NO EXISTE ───────────────────────────────────
# `open(ruta, "a")` CREA el fichero. Así que un carril declarado en el mapa cuyo
# fichero ya no está —borrado, volumen sin montar, ruta que cambió— no falla: se
# inventa un ledger vacío, mete la entrada dentro, y devuelve «✓ publicado». El
# agente cree que ha hablado y nadie lee ese fichero. Publicar a un sitio que nadie
# lee es pérdida silenciosa, no un error de escritura.
#
# El mapa dice DÓNDE debería estar, no que esté. Lo señaló `harness` desde su lente
# de guards, y al medirlo salió peor de lo que él describía: no es que no compruebe,
# es que el modo de apertura fabrica el destino.
T6="$(mktemp -d)"
printf 'carril	ruta
'              > "$T6/c.tsv"
printf 'fantasma	%s/L.md
' "$T6"  >> "$T6/c.tsv"
python3 -c "import json,sys; json.dump({'f': sys.argv[1]+'/L.md'}, open(sys.argv[1]+'/m.json','w'))" "$T6"
# El fichero NO se crea: el mapa lo declara y el disco no lo tiene.
S="$(printf 'cuerpo\n' | env -u LLMI_LEDGER BIK_CARRIL=fantasma \
      LLMI_YO=cto-A LLMI_A=qa LLMI_TIPO=FYI LLMI_TITULAR=x \
      LLMI_CARRILES="$T6/c.tsv" LLMI_MOUNTS="$T6/m.json" LLMI_DIR=. \
      LLMINBOX_ROSTER="$T/censo.json" python3 publicar.py 2>&1)"
grep -q '^✗' <<<"$S" \
  && bien "no publica a un ledger cuyo fichero no existe" \
  || mal "ledger inventado" "✗ …" "$S"
[ ! -f "$T6/L.md" ] \
  && bien "y NO lo crea: el mapa dice dónde debería estar, no que esté" \
  || mal "no fabricar el destino" "sin fichero" "creado con $(wc -c <"$T6/L.md") bytes"

# ⊖ CONTROL — con el fichero presente publica igual que siempre. Sin esto, un
# `muere()` incondicional pasaría los dos de arriba y rompería a toda la flota.
printf '# ledger vivo\n' > "$T6/L.md"
S="$(printf 'cuerpo\n' | env -u LLMI_LEDGER BIK_CARRIL=fantasma \
      LLMI_YO=cto-A LLMI_A=qa LLMI_TIPO=FYI LLMI_TITULAR=x \
      LLMI_CARRILES="$T6/c.tsv" LLMI_MOUNTS="$T6/m.json" LLMI_DIR=. \
      LLMINBOX_ROSTER="$T/censo.json" python3 publicar.py 2>&1)"
grep -q '^✓ publicado' <<<"$S" \
  && bien "⊖ con el fichero presente, publica con normalidad" \
  || mal "control de ledger vivo" "✓ publicado" "$S"

# Y LA MISMA GUARDA PARA EL LEDGER FORZADO — lo señaló CodeRabbit: `LLMI_LEDGER`
# retorna ANTES de la comprobación del carril, así que un montaje forzado cuyo fichero
# ya no está seguía llegando al `open(...,"a")` y fabricando el ledger. Arreglar una
# rama y dejar la otra es peor que no arreglar ninguna: da la sensación de cubierto.
rm -f "$T6/L.md"
S="$(printf 'cuerpo\n' | env BIK_CARRIL= LLMI_LEDGER=f \
      LLMI_YO=cto-A LLMI_A=qa LLMI_TIPO=FYI LLMI_TITULAR=x \
      LLMI_CARRILES="$T6/c.tsv" LLMI_MOUNTS="$T6/m.json" LLMI_DIR=. \
      LLMINBOX_ROSTER="$T/censo.json" python3 publicar.py 2>&1)"
grep -q '^✗' <<<"$S" \
  && bien "tampoco publica al ledger FORZADO si su fichero no existe" \
  || mal "ledger forzado inventado" "✗ …" "$S"
[ ! -f "$T6/L.md" ] \
  && bien "y tampoco lo crea por la vía forzada" \
  || mal "no fabricar por la vía forzada" "sin fichero" "creado"
rm -rf "$T6"
# ── LA FIRMA SE DERIVA DEL CARRIL, cuando el censo la conoce ───────────────────
# El defecto medido (2026-08-28): los 4 alias `cto-<carril>` se dieron de alta en el
# censo el 23 y a los cinco días llevaban 423 entradas publicadas y CERO usos — los
# cinco carriles firmando `cto`. El alta era correcta; lo que faltaba es que alguien
# los EMITIERA. Quien firma es `LLMI_YO`, y `LLMI_YO` no se exporta en ningún fichero
# de aprovisionamiento: cada agente lo teclea. Pedir a cinco sesiones que «se acuerden»
# reproduce el fallo dentro de un mes; derivarlo del carril no tiene nada que recordar.
#
# CONDICIÓN DE LA ADJUDICACIÓN (cto, 2026-08-28): se deriva SÓLO si el alias derivado
# está en el censo. Si no, se cae al nombre crudo. Lo que cierra es que `wiki-vault` en
# el carril PM se convierta en `wiki-vault-PM`, un alias que nadie dio de alta ⇒ 422
# para quien hoy publica bien. La derivación no puede romper a quien no pidió nada.
T3="$(mktemp -d)"
printf '# ledger de derivacion\n' > "$T3/D.md"
printf 'carril\truta\n'            > "$T3/carriles.tsv"
printf 'acept\t%s/D.md\n' "$T3"   >> "$T3/carriles.tsv"
printf 'huerfano\t%s/D.md\n' "$T3">> "$T3/carriles.tsv"
python3 -c "import json,sys; json.dump({'d': sys.argv[1]+'/D.md'}, open(sys.argv[1]+'/m.json','w'))" "$T3"
# Censo con `cto` Y `cto-acept`, pero SIN `cto-huerfano`: así el mismo censo sirve para
# el caso que deriva y para el control que no debe derivar.
cat > "$T3/censo.json" <<'JSON'
{"agentes": [{"nombre": "cto", "humano": "alguien", "clave": "", "rol": "cto"},
             {"nombre": "cto-acept", "humano": "alguien", "clave": "", "rol": "cto"},
             {"nombre": "qa", "humano": "alguien", "clave": "", "rol": "qa"}],
 "humanos": [{"nombre": "alguien", "alias": []}],
 "difusion": ["equipo"]}
JSON

firma_con_carril() {              # firma_con_carril <carril> <yo>
  # `LLMI_LEDGER=` VACÍO A PROPÓSITO (lo señaló CodeRabbit): si el proceso padre lo
  # trae, `ledger_del_carril()` lo usa ANTES que `BIK_CARRIL` y el caso deja de
  # ejercitar la resolución por carril — pasaría sin probar lo que dice probar.
  printf 'cuerpo\n' | env LLMI_LEDGER= BIK_CARRIL="$1" LLMI_YO="$2" LLMI_A=qa LLMI_TIPO=FYI \
    LLMI_TITULAR="firma derivada" LLMI_CARRILES="$T3/carriles.tsv" \
    LLMI_MOUNTS="$T3/m.json" LLMI_DIR=. LLMINBOX_ROSTER="$T3/censo.json" \
    python3 publicar.py >/dev/null 2>&1
  tail -20 "$T3/D.md" | grep -o '^### \[[^ ]*' | tail -1 | tr -d '#[ '
}

F="$(firma_con_carril acept cto)"
[ "$F" = "cto-acept" ] \
  && bien "la firma se deriva del carril cuando el censo conoce el alias" \
  || mal "derivacion de firma" "cto-acept" "$F"

# ⊖ CONTROL, y es el que sostiene la condición: sin alias derivado en el censo NO se
# deriva, se firma crudo y se publica igual. Si esto sale `cto-huerfano`, la derivación
# está inventando identidad; si sale vacío, la ha roto para quien funcionaba.
F="$(firma_con_carril huerfano cto)"
[ "$F" = "cto" ] \
  && bien "⊖ sin alias en el censo cae al nombre crudo, y publica igual" \
  || mal "control de fallback" "cto" "$F"
rm -rf "$T3"

# ── LOS MONTAJES TAMBIÉN SE BUSCAN, no sólo se exigen ─────────────────────────
# Esta misma función ya aprendió la lección para `carriles.tsv`: tres candidatos y un
# comentario que explica por qué —«la herramienta funcionaba en el contexto de quien
# la escribió»—. Los montajes quedaron fuera: `LLMI_MOUNTS` o muerte, con un
# «corre: llmi init» que en el repo real está PROHIBIDO.
#
# Vivido el 2026-08-30: pasé la noche sin poder publicar al ledger —bloqueando una
# adjudicación que otro agente esperaba— teniendo el fichero en `.llminbox-state/`
# desde el día 22. No faltaba un permiso: faltaba que la herramienta lo buscara donde
# ya estaba. Es la misma clase que el `/health` que se blindó para los ledgers y no
# para el censo: lección aprendida una vez, no generalizada.
T4="$(mktemp -d)"; mkdir -p "$T4/.llminbox-state"
printf '# ledger de montajes
' > "$T4/M.md"
python3 -c "import json,sys; json.dump({'m': sys.argv[1]+'/M.md'}, open(sys.argv[1]+'/.llminbox-state/mounts.json','w'))" "$T4"
S="$(printf 'cuerpo\n' | env -u LLMI_MOUNTS -u BIK_CARRIL \
      LLMI_YO=cto-A LLMI_A=qa LLMI_TIPO=FYI LLMI_TITULAR="montajes hallados" \
      LLMI_LEDGER=m LLMI_DIR="$T4" LLMINBOX_ROSTER="$T/censo.json" \
      python3 publicar.py 2>&1)"
grep -q '^✓ publicado' <<<"$S" \
  && bien "encuentra los montajes en .llminbox-state sin que se los den" \
  || mal "montajes por defecto" "✓ publicado" "$S"

# ⊖ CONTROL — sin montajes por ningún lado tiene que MORIR, no inventarse uno. Sin
# esto, un fallback que devolviera `{}` en silencio pasaría el test de arriba.
T5="$(mktemp -d)"
S="$(printf 'cuerpo\n' | env -u LLMI_MOUNTS -u BIK_CARRIL \
      LLMI_YO=cto-A LLMI_A=qa LLMI_TIPO=FYI LLMI_TITULAR=x \
      LLMI_LEDGER=m LLMI_DIR="$T5" LLMINBOX_ROSTER="$T/censo.json" \
      python3 publicar.py 2>&1)"
grep -q '^✗' <<<"$S" \
  && bien "⊖ sin montajes en ningún candidato, muere en vez de inventarse uno" \
  || mal "control de montajes ausentes" "✗ …" "$S"
rm -rf "$T4" "$T5"


# ── un mapa de montajes EXPLICITO y roto no puede caer al siguiente candidato ─────
# Elegir DONDE SE ESCRIBE con un fichero que nadie pidio puede meter la entrada en el
# canon de otro carril, y sin que nadie vea un error. Hoy los dos mapas del repo
# coinciden (12 ledgers, cero diferencias) asi que no hay dano medido — pero tienen
# mtime distinto, o sea que pueden separarse.
#
# La regla es la misma que en `_credenciales()` y en `deriva()`: AUSENTE prueba el
# siguiente candidato; PRESENTE Y ROTO para.
T6="$(mktemp -d)"
printf '{"m": "%s/l.md", ' "$T6" > "$T6/roto.json"          # JSON truncado a proposito
mkdir -p "$T6/.llminbox-state"
printf '{"m": "%s/l.md"}' "$T6" > "$T6/.llminbox-state/mounts.json"   # candidato 2, VALIDO
: > "$T6/l.md"
S="$(printf 'cuerpo\n' | env -u BIK_CARRIL LLMI_YO=cto-A LLMI_A=qa LLMI_TIPO=FYI \
      LLMI_TITULAR=x LLMI_LEDGER=m LLMI_MOUNTS="$T6/roto.json" LLMI_DIR="$T6" \
      LLMINBOX_ROSTER="$T/censo.json" python3 publicar.py 2>&1)"
grep -q 'esta roto\|está roto' <<<"$S" \
  && bien "un mapa explicito ROTO para, en vez de caer a otro fichero" \
  || mal "mapa explicito roto" "muere nombrandolo" "$S"
grep -q '^✓ publicado' <<<"$S" \
  && mal "⊖ NO puede publicar con el mapa explicito roto" "sin publicar" "publico igual" \
  || bien "⊖ y no publica nada: la entrada no acaba en un ledger que nadie eligio"

# ⊕ CONTROL: si el explicito NO EXISTE (no roto), sigue cayendo al siguiente — que es
# para lo que existen los tres candidatos, y una sesion entera se perdio por no tenerlo.
S="$(printf 'cuerpo\n' | env -u BIK_CARRIL LLMI_YO=cto-A LLMI_A=qa LLMI_TIPO=FYI \
      LLMI_TITULAR=x LLMI_LEDGER=m LLMI_MOUNTS="$T6/no-existe.json" LLMI_DIR="$T6" \
      LLMINBOX_ROSTER="$T/censo.json" python3 publicar.py 2>&1)"
grep -q '^✓ publicado' <<<"$S" \
  && bien "⊕ y un explicito AUSENTE sigue cayendo al candidato de al lado" \
  || mal "explicito ausente" "✓ publicado" "$S"
rm -rf "$T6"

echo
[ "$MALOS" -eq 0 ] && echo "publicar: TODO VERDE" || echo "publicar: $MALOS fallo(s)"
exit $((MALOS > 0))
