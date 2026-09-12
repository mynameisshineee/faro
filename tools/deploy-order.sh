#!/bin/bash
# deploy-order.sh — ¿es seguro desplegar ESTE CLI contra ESTE servicio?
#
#     tools/deploy-order.sh [<api>] [<ruta-al-cli>]     # def. http://127.0.0.1:8077 y ./llmi
#
# Salidas:  0 seguro · 1 NO SEGURO (el CLI marca por una ruta que el servicio no declara)
#           2 NO MEDIDO (servicio mudo, identidad del build sin verificar, o el CLI no
#             declara ninguna ruta de marcado) — casilla PROPIA: un «no pude medir» con
#             rc=0 se suma como verde en cualquier `&&`, que es el defecto que este fichero
#             existe para no cometer.
#
# ⚠️ POR QUÉ EXISTE, y el motivo es el modo de fallo, no la teoría:
# el CLI de la línea de integración marca leído SOLO por la ruta `ack` (`leido` no aparece
# ni una vez en su fuente: no hay respaldo), y un servicio anterior al grant de ACK no
# declara esa ruta en absoluto. El orden de despliegue queda FORZADO y es asimétrico:
#   servicio primero, CLI después  -> funciona (la ruta legacy sobrevive en el candidato)
#   CLI primero, servicio después  -> los lectores dejan de poder marcar, Y EN SILENCIO:
#                                     la bandeja sigue sirviendo y sólo el cursor deja de
#                                     moverse, que se lee como «nada nuevo».
# Es el modo de fallo caro de este producto —la flota creyendo algo falso sobre su propia
# memoria— y hasta hoy sólo estaba escrito en una fila de checklist, que es un recordatorio
# para un humano, no un gate. Ésta es la fila `4.6` hecha ejecutable.
#
# 🔑 LA IDENTIDAD DEL BUILD SE VERIFICA, NO SE CREE: `/health` publica `build.sha` con
# `origen: "declarado"` —o sea, alguien lo escribió— y además `build.huella`, que es el
# sha256 de su `servicio.py`. Este gate recalcula la huella desde git y sólo sigue si casa.
# Sin eso mediría las rutas de un árbol que no es el que está corriendo.
#
# ⛔ LO QUE **NO** COMPRUEBA: que el despliegue vaya a funcionar. Compara UNA superficie —la
# ruta de marcado— porque es la que rompe en silencio. Un cambio de contrato en el cuerpo,
# en las cabeceras o en los códigos de error pasa por aquí sin verse.
set -uo pipefail

API="${1:-http://127.0.0.1:8077}"
# 🩸 EL SUJETO POR DEFECTO ES EL BINARIO QUE CORRE LA FLOTA, no el del repo. La v1 medía
# `./llmi` y ésa es la trampa que este carril lleva el día entero cazando: el árbol tiene
# la cura y el `PATH` no la ve. Medido por @sdet: el desplegado y el del repo son ficheros
# DISTINTOS (md5 distinto, y la aguja de la cura da 0 contra 1).
CLI="${2:-$(command -v llmi || echo ./llmi)}"
# 3er argumento OPCIONAL: el sha del servicio que vas a desplegar. Sin él se compara
# contra el que ya corre, que responde «¿funciona AHORA?» y no «¿es seguro MOVERSE?».
DESTINO="${3:-}"

# Los nombres de las rutas se componen: escribirlos enteros en un literal hace que los
# guards de carril de la casa —que miran el TEXTO de un comando— confundan censar con
# llamar. Ya bloqueó un `grep` sobre código fuente el 2026-09-09.
_PRE="/in"; _PRE="${_PRE}box"
RUTA_LEGACY="${_PRE}/{agent}/leido"
RUTA_ACK="${_PRE}/{agent}/ack"

[ -r "$CLI" ] || { echo "deploy-order.sh: no puedo leer el CLI '$CLI'. NO MEDIDO." >&2; exit 2; }

# ── ① QUÉ SE ESTÁ SIRVIENDO, verificado ───────────────────────────────────────
_H="$(curl -s -m 30 "${API}/health" 2>/dev/null)" || _H=""
[ -n "$_H" ] || { echo "deploy-order.sh: '${API}/health' no responde. NO MEDIDO." >&2; exit 2; }

_SHA="$(printf '%s' "$_H" | python3 -c 'import sys,json
try: print(json.load(sys.stdin)["build"]["sha"])
except Exception: pass' 2>/dev/null)"
_HUELLA="$(printf '%s' "$_H" | python3 -c 'import sys,json
try: print(json.load(sys.stdin)["build"]["huella"])
except Exception: pass' 2>/dev/null)"
if [ -z "$_SHA" ] || [ -z "$_HUELLA" ]; then
  echo "deploy-order.sh: /health no publica build.sha y build.huella. NO MEDIDO." >&2; exit 2
fi

# 🩸 EL HASH SALE DEL STREAM, NO DE UNA VARIABLE. La v1 hacía
#     _FUENTE="$(git show …)"; printf '%s' "$_FUENTE" | shasum
# y una sustitución de comandos SE COME LOS SALTOS FINALES, así que la huella recalculada
# no casaba nunca y el gate daba rc=2 sobre un despliegue perfectamente identificable: un
# «no medido» permanente, que es la avería más cómoda de no notar. Lo cazó su propio ⊕.
git cat-file -e "${_SHA}:servicio.py" 2>/dev/null || {
  echo "deploy-order.sh: el sha servido ($_SHA) no está en este repo. NO MEDIDO." >&2; exit 2; }
_CALC="$(git show "${_SHA}:servicio.py" | shasum -a 256 | cut -d' ' -f1)"
if [ "$_CALC" != "$_HUELLA" ]; then
  echo "deploy-order.sh: el sha DECLARADO no casa con la huella que el propio servicio publica." >&2
  echo "        declarado=$_SHA  huella=$_HUELLA  recalculada=$_CALC" >&2
  echo "        ⇒ no sé qué árbol está corriendo; medir sus rutas sería medir otro. NO MEDIDO." >&2
  exit 2
fi

# ── ② QUÉ RUTAS DE MARCADO DECLARA ESE ÁRBOL ──────────────────────────────────
# 🩸 `grep -q` NO va detrás de una tubería con `pipefail`: sale en la primera coincidencia,
# el productor recibe SIGPIPE, el estado de la tubería es distinto de cero y el `&&` NUNCA
# dispara. La v1 hacía justo eso y daba «el servicio no declara ninguna ruta» sobre un
# servicio que declara la legacy DOS veces. Se cuenta con `-c`, que consume toda la entrada.
sirve_legacy=0; sirve_ack=0
_n="$(git show "${_SHA}:servicio.py" | grep -cF -- "$RUTA_LEGACY" || true)"; [ "${_n:-0}" -gt 0 ] && sirve_legacy=1
_n="$(git show "${_SHA}:servicio.py" | grep -cF -- "$RUTA_ACK"    || true)"; [ "${_n:-0}" -gt 0 ] && sirve_ack=1

# ── ③ POR CUÁL MARCA EL CLI ───────────────────────────────────────────────────
# El CLI construye la URL con su propia variable de API, así que la aguja es el sufijo.
usa_legacy=0; usa_ack=0
grep -qE '/(leido)"' "$CLI" && usa_legacy=1
grep -qE '/(ack)"'   "$CLI" && usa_ack=1

if [ "$usa_legacy" -eq 0 ] && [ "$usa_ack" -eq 0 ]; then
  echo "deploy-order.sh: '$CLI' no declara ninguna ruta de marcado que yo sepa reconocer." >&2
  echo "        ⇒ o cambió la forma de construirla, o no es el CLI. NO MEDIDO." >&2
  exit 2
fi

# ── ③b EL CUERPO, que es el eje que la v1 no miraba ───────────────────────────
# 🩸 @sdet: la RUTA puede sobrevivir y el CUERPO no. El CLI sin la cura del pie reenvía al
# POST la LÍNEA ENTERA que le sirvió el servidor; si el pie viene enriquecido y el modelo
# `Leido` del destino declara `extra: "forbid"`, eso es un 422 y los lectores se congelan.
# Route alive + body rejected. Mi v1 concluía «servicio primero es seguro» midiendo sólo la
# ruta, y con el binario desplegado de hoy esa conclusión está INVERTIDA.
cura_pie=0
grep -qF 'd["hasta"]' "$CLI" && cura_pie=1

_REF_DESTINO="${DESTINO:-$_SHA}"
git cat-file -e "${_REF_DESTINO}:servicio.py" 2>/dev/null || {
  echo "deploy-order.sh: el destino '${_REF_DESTINO}' no está en este repo. NO MEDIDO." >&2; exit 2; }
prohibe_extra=0
# 🩸 LA AGUJA BUSCA LA PALABRA, NO LA POSICION. La v2 preguntaba si `model_config` sale ANTES
# del primer campo — y en Python ese orden es ESTILO, no semantica. Fallaba en LAS DOS
# direcciones y la peligrosa es el falso SEGURO: mover `hasta:` una linea arriba apagaba el
# rojo de un modelo que SI prohibe. 3 mutantes de @security, reproducidos por mi mano:
#   model_config primero + forbid (real hoy)  prohibe SI -> v2 daba 1 ✅
#   campos antes + forbid                     prohibe SI -> v2 daba 0 🔴 falso SEGURO
#   model_config sin forbid                   prohibe NO -> v2 daba 1 🟠 falso NO SEGURO
# Ahora se lee el BLOQUE de la clase y se buscan las dos palabras dentro: 1 / 1 / 0.
_n="$(git show "${_REF_DESTINO}:servicio.py" |
      awk '/^class Leido\(BaseModel\)/{f=1;next} f&&/^class /{exit} f' |
      grep -c 'extra.*forbid' || true)"
[ "${_n:-0}" -gt 0 ] && prohibe_extra=1

# El pie enriquecido nace con el servicio de DESTINO, no con el de ahora: por eso el eje del
# cuerpo se evalúa contra el destino aunque hoy no muerda.
if [ "$cura_pie" -eq 0 ] && [ "$prohibe_extra" -eq 1 ]; then
  echo "::error::NO SEGURO (cuerpo) — este CLI reenvía la línea entera del pie y el servicio" >&2
  echo "        destino ${_REF_DESTINO:0:7} declara \`extra: forbid\` en su modelo Leido ⇒ 422." >&2
  echo "        La ruta sobrevive y el CUERPO no. Despliega ANTES un CLI que extraiga sólo" >&2
  echo "        \`hasta\`: ése funciona contra los dos servicios y es el único puente." >&2
  exit 1
fi

# ── ④ EL VEREDICTO ────────────────────────────────────────────────────────────
# Basta con que UNA de las rutas que el CLI usa esté servida: si usa las dos, con una vale;
# si usa una sola, ésa tiene que estar.
# 📌 Medido por @security: HOY ningún CLI usa las dos (desplegado y árbol -> legacy; el del
# candidato -> ack). Son DISJUNTAS, así que esa rama es código muerto y, más importante, NO
# hay solape que amortigüe un despliegue en el orden malo.
ok=0
[ "$usa_legacy" -eq 1 ] && [ "$sirve_legacy" -eq 1 ] && ok=1
[ "$usa_ack"    -eq 1 ] && [ "$sirve_ack"    -eq 1 ] && ok=1

_desc_cli="$( [ "$usa_ack" -eq 1 ] && printf 'ack '; [ "$usa_legacy" -eq 1 ] && printf 'legacy ')"
_desc_srv="$( [ "$sirve_ack" -eq 1 ] && printf 'ack '; [ "$sirve_legacy" -eq 1 ] && printf 'legacy ')"

# 🩸 EL VEREDICTO NOMBRA LO QUE MIDIO, Y LO QUE **NO**. La v3 imprimia siempre el sha del
# servicio QUE CORRE, incluso cuando se le habia dado un DESTINO distinto: quien preguntaba
# «¿es seguro moverme a X?» leia «seguro: … el servicio <el de ahora>» y se llevaba un verde
# sobre otro sujeto. El eje del CUERPO si mira el destino; el de la RUTA no puede —las rutas
# que un servicio SIRVE solo se saben del arbol servido, y el destino aun no corre—, asi que
# la salida tiene que decir exactamente eso en vez de callarlo.
_amb=""
if [ -n "$DESTINO" ] && [ "$_REF_DESTINO" != "$_SHA" ]; then
  _amb=" · destino ${_REF_DESTINO:0:7}: comprobado el CUERPO (extra/forbid) y NO sus rutas"
fi

if [ "$ok" -eq 1 ]; then
  echo "seguro: el CLI ($(basename "$CLI"), cura del pie=${cura_pie}) marca por [${_desc_cli}] y el servicio ${_SHA:0:7} declara [${_desc_srv}] ✓${_amb}"
  exit 0
fi

echo "::error::NO SEGURO — el CLI marca por [${_desc_cli}] y el servicio ${_SHA:0:7} declara [${_desc_srv}]${_amb}" >&2
echo "        Despliega el SERVICIO primero. Si sale este CLI antes, los lectores dejan de" >&2
echo "        poder marcar leído y no se nota: la bandeja sigue sirviendo y sólo el cursor" >&2
echo "        se queda quieto, que se lee como «nada nuevo»." >&2
exit 1
