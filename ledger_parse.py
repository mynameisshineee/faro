#!/usr/bin/env python3
"""
ledger_parse — el troceador de entradas de la red de ledgers, medido contra el corpus real.

## Por qué se reescribe

Una versión anterior de este mismo sellador declaró «2.076 entradas selladas en 6
ledgers». Ese número era del INSTRUMENTO, no del corpus: su regex solo reconocía
cabeceras `## `, y la convención que la flota usa de verdad es `### [...]`. Medido
sobre el corpus real (2026-07-27): 37.126 entradas reales, 2.076 selladas = **5,6% de
cobertura**. En el ledger de mayor volumen de escritura de un solo agente selló UNA.
El hook de arranque imprimía «cadena de hashes íntegra» sobre ese 5,6% sin decir el
denominador.

## Las convenciones que existen de verdad (censo 2026-07-27)

| convención                | entradas | dónde                                          |
|----------------------------|---------|--------------------------------------------------|
| `### [...]`                |  35.030 | la mayoría de los ledgers de este despliegue      |
| `## <ISO> · a → b · T`     |     365 | solo uno de ellos (el de un único escritor spoke) |
| `## [...]` / `## <fecha>`  |   1.717 | el más grande, forma histórica                    |

Ver `PROTOCOL.md`, en este mismo repo, para la especificación completa de lo que el
troceador reconoce. Antes solo existía documentado el formato de UN ledger (el
spoke de un único escritor) — el que usa ~1% del tráfico real. No se corrige la
flota para que encaje con el documento: se reconoce lo que la flota escribe de
verdad, y el documento se ajusta después.

## Lo que este módulo NO hace

No adivina. Cuando no puede extraer un campo (actor, destinatarios, tipo) lo deja a
`None` y lo CUENTA. La cobertura por campo es una salida de primera clase, porque es
la medida de cuánto trabajo queda para tipar el ledger — no un detalle de implementación.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import os
import re
import threading as _threading
import unicodedata
from dataclasses import dataclass, field

# Versión del TROCEADOR. Súbela con CUALQUIER cambio que altere qué se extrae
# (nuevos agentes, nuevas etiquetas, otro corte de destinatarios). El servicio la
# compara al arrancar y reconstruye el índice entero si no coincide.
#
# Existe porque hoy me pasó: arreglé el extractor (HEARTBEAT, destinatarios,
# mayúsculas) y el índice siguió sirviendo los datos VIEJOS — el fichero no había
# cambiado, así que no se reindexaba, y el troceador nuevo solo se aplicaba a lo
# que llegara después. Un `lint` verde sobre datos de la versión anterior es la
# misma clase de mentira que llevo cazando todo el día.
#
# v5 (2026-07-27): coautoría `CTO+BE →` ya no tira al coautor (7 cabeceras del
# corpus real cambian de actor); difusión (FLOTA/equipo/todos) sale de `to` a
# su propio campo (1.915 entradas cambian de forma, ninguna de cobertura real
# perdida — ver PROTOCOL.md, sección "Qué NO garantiza el formato hoy").
PARSER_V = 11  # 11: un sello con FORMA de fecha pero imposible (`9999-99-99T99:99:99Z`,
               #    `2026-13-45…`) deja de contar como sello. Se sube por el MISMO
               #    motivo que la 10, y me costó dos despliegues aprenderlo: curé el
               #    parser, lo fusioné, lo desplegué… y los tres sellos seguían
               #    encabezando `/entries?orden=ts`, porque `barrido()` salta el ledger
               #    cuyo tamaño y mtime no cambian. El arreglo valía sólo para lo que se
               #    escribiera a partir de ese momento — que es literalmente lo que este
               #    comentario ya advertía una versión más arriba.
               # 10: la ranura de tipo sólo existe si la cabecera DIRIGE. Sin este
               #    gate, las 58 entradas que declaraban el CARRIL como raw_tipo
               #    (`### [wiki-vault·64bis]`) y los ~35 sellos de fecha seguirían
               #    ahí: `barrido()` salta el ledger cuyo tamaño y mtime no
               #    cambian, así que el arreglo del parser sin subir esto sólo
               #    valdría para lo que se escriba a partir de hoy.
               # 9: la difusión sobrevive a una re-derivación — sin esto, cada
               #    arranque con censo o parser nuevo borraba las filas de
               #    entrega de FLOTA/equipo/todos de TODO el histórico (6.220
               #    entradas del corpus, 32 filas vivas al medirlo). Se sube
               #    aquí porque el gate de este número es lo que dispara la
               #    re-derivación que las recupera: el arreglo sin el gate
               #    sólo valdría para el correo futuro.
               #    (8: la flecha de RUTA sólo cuenta dentro del corchete · 7: latidos)
               #    (6: la difusión se persiste en recipients — ⑩) — ver reindex()

# Una entrada empieza en una cabecera de cualquiera de las convenciones vivas.
# Orden importante: `### [` es la dominante (94% del corpus).
H_ENTRY = re.compile(
    r"^(?:"
    r"### \["                                        # dominante: ### [cabecera libre]
    r"|## \d{4}-\d{2}-\d{2}T[\d:]+Z\s*·"             # spoke: ## <ISO> · a → b · TIPO
    r"|## \["                                        # forma histórica: ## [agente TS]
    r"|## \d{4}-\d{2}-\d{2}"                         # forma histórica: ## <fecha> [...]
    r")"
)

TS = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z")


def _sello_valido(bruto: str) -> str | None:
    """El sello, o None si tiene FORMA de fecha pero no ES una fecha.

    `TS` casa DÍGITOS, no fechas: `9999-99-99T99:99:99Z` —mes 99, día 99, hora 99— la
    pasaba entera y se guardaba como el sello de la entrada.

    MEDIDO contra el índice vivo el 2026-09-04, con el consumidor por DEFECTO
    (`GET /entries?orden=ts`, que es lo que pide la interfaz):

        9999-99-99T99:99:99   db-mig     «UN GATE PASA UN SELLO IMPOSIBLE»…
        2026-13-45T99:99:99   qa         «CENSO DE MIS 9 GATES…»
        2026-10-17T23:30:00   security   «…congela el reloj en 2026-10-17T23:30:00Z»

    Tres entradas clavadas en la cabeza de CADA listado por defecto, y `MAX(ts)` del
    corpus entero valiendo 9999. La tercera enseña de dónde salen: son entradas que
    HABLAN de sellos imposibles y de congelar relojes, y el ejemplo que citan en su
    propio titular acabó siendo su sello.

    SIN SELLO, NO SIN ENTRADA. Este corpus ya trata la ausencia de fecha como un estado
    normal y soportado —10.434 entradas sin sello, con su propia línea en `/doctor`—,
    así que devolver None las deja consultables y deja de mentir sobre cuándo pasaron.
    Tirar la entrada sería peor que el defecto.
    """
    try:
        _dt.datetime.strptime(bruto, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return bruto
TIPOS = ("PRODUCED", "INGESTED", "FYI", "REQUEST", "ACK", "HELD", "AMEND", "DELTA")

# ── CANON v1 ──────────────────────────────────────────────────────────────────
# El contrato, adjudicado el 2026-08-20 sobre el corpus rederivado:
#
#     raw_tipo = el lexema EXACTO que escribió el autor · nunca se destruye
#     tipo     = normalización canónica · conjunto pequeño y GOBERNADO
#
# Los cuatro nuevos entran por USO MEDIDO, no por parecido con nada:
#     MEASURED  580 usos · 17 autores · 14% el primero   → vocabulario de flota
#     RESP      735 usos · 31 autores · 23%
#     RULING     98 usos ·  4 autores · 59%
#     FINDING    54 usos · 12 autores · 17%
#
# Y LOS QUE SE QUEDAN FUERA, cada uno con su motivo medido — porque la frecuencia
# sola no basta y aquí está la prueba:
#     WAKE      97 usos y UN SOLO AUTOR (marketing), todas el mismo texto
#               automático de heartbeat rancio. Es tooling de un agente, no
#               contrato de flota. Estaba en la lista y se retiró al medirlo.
#     HARNESS   23/23 de un solo autor. Mismo patrón.
#     EXECUTED  CERO usos. Crear un canónico que nadie ha escrito para normalizar
#               21 EJECUTADO sería diseñar el corpus hacia la taxonomía.
#     DECIDED   contraejemplo real: sus textos empiezan por «acepto tu corrección»
#               y «mi requisito era INSUFICIENTE» — eso es ACK/AMEND, no adjudicar.
#     DONE      colisiona con el estado de Job de Agent OS (ACCEPTED ≡ DONE).
#     CLAIM     colisiona con la operación de lease del control plane.
#     MSG       no clasifica el acto: decir «es un mensaje» no dice nada.
#     ASK · AVISO · STATUS   medidos y MIXTOS: AVISO mezcla alertas con
#               RETRACTACIONES, ASK mezcla estado con escalación. Quedan raw.
#
# Tres ontologías que NO se mezclan, y por eso DONE y CLAIM no entran:
#     tipo de ledger  = qué acto comunicativo ocurrió
#     estado de Job   = en qué punto está el trabajo
#     operación       = CLAIM / LEASE / REVIEW del control plane
# DELIVERED: el recibo de entrega del canon operativo. Va AQUÍ y no en `TIPOS`, y el
# motivo está tres líneas más arriba en `publicar.py`: «No se amplía TIPOS: eso sería
# volver a tener dos listas que hay que sincronizar a mano». `TIPOS` son los lexemas del
# parser; `CANON_TIPOS` es la autoridad de la PUERTA, y es donde entró `FINDING` cuando
# pasó exactamente esto —el 21-ago-2026 dos hallazgos reales se publicaron como PRODUCED
# porque FINDING no pasaba—.
#
# Sin él, quien entrega tiene que ENVOLVER el recibo dentro de otro tipo y citarlo con
# `> `: dos entradas donde debería haber una, y un validador que no lo reconoce. Medido
# por @harness en el piloto.
#
# NO es alias de PRODUCED, y la distinción no es cosmética: como alias se guardaría con
# `tipo=PRODUCED` y el censo de recibos mezclaría entregas con publicaciones — las 6.635
# PRODUCED del corpus contaminarían la línea base que el shadow existe para medir.
CANON_TIPOS = frozenset(TIPOS) | {"MEASURED", "RESP", "FINDING", "RULING", "DELIVERED"}

# UN SOLO ALIAS, y es el único con evidencia que resiste: 14 de los 15 autores de
# `MEDIDO` escriben TAMBIÉN `MEASURED` (93%). Si fueran semánticas distintas, los
# autores se habrían especializado; no lo hacen. `ADJUDICADO` y `VEREDICTO` quedan
# PENDIENTES —no rechazados—: no encontré contraejemplo, pero tampoco un test que
# lo hubiera encontrado, y eso no es equivalencia probada.
ALIASES = {"MEDIDO": "MEASURED"}


def canonical_tipo(raw: str | None) -> str | None:
    """La ÚNICA derivación de `tipo`, y sale del lexema — nunca de la prosa.

    Lo anterior era `next((t for t in TIPOS if t in head), None)`: subcadena sobre
    la cabecera entera. Una cabecera que MENCIONA «FYI» recibía tipo FYI, y la
    columna quedó inflada — medido: 13 valores fuera de `TIPOS` en 22.882
    entradas. Añadir los canónicos nuevos por esa vía habría sido peor todavía:
    `RESP` aparece como subcadena en 2.630 cabeceras que no lo son, `RULING` en
    1.577.

    Por eso esta función NO recibe `head`: no puede caer en esa tentación.
    """
    if not raw:
        return None
    t = raw.strip().upper()
    t = ALIASES.get(t, t)
    return t if t in CANON_TIPOS else None

# Los nombres de agente vivos en la flota. Se listan explícitamente en vez de
# inferirse: inferir "la primera palabra" da basura sobre cabeceras que empiezan
# por emoji, verbo o adjetivo, y una atribución equivocada es peor que ninguna
# (lección propia, dos veces: 07-18 y 07-25).
# El CENSO vive en `roster.json`, no aquí. Era una constante de Python, y eso solo
# aguanta con un humano: en cuanto hay varios, cada uno con sus agentes, dar de alta
# a alguien obligaba a editar el troceador y redesplegar el servicio. Medido en el
# ensayo de equipo (2026-07-27): con un agente y un humano dados de alta SOLO en
# el censo local de otro, el extractor devolvía actor=None y destinatarios=[] —
# la bandeja salía vacía teniendo correo.
#
# El fichero lleva además, por agente, el humano que responde de él y un campo de
# clave vacío. Hoy nadie verifica esa clave; existe para que el día que el ledger
# tenga que servir de evidencia no haya que rehacer el formato ni reindexar.
def _censo():
    import json
    import os as _os
    ruta = _os.environ.get("LLMINBOX_ROSTER") or _os.path.join(
        _os.path.dirname(_os.path.abspath(__file__)), "roster.json")
    try:
        with open(ruta, encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception as e:                    # sin censo se sigue, pero se DICE
        print(f"[censo] no pude leer {ruta}: {e} — el extractor no reconocerá a nadie")
        return [], {}, [], {}, {}
    nombres = [a["nombre"] for a in d.get("agentes", [])]
    for h in d.get("humanos", []):
        nombres.append(h["nombre"])
        nombres.extend(h.get("alias", []))
    dueno = {a["nombre"].lower(): a.get("humano") for a in d.get("agentes", [])}
    # ESCUCHA — un agente puede recibir, además de lo suyo, el flujo dirigido a OTRO
    # nombre. Suena a alias y no lo es: el cursor sigue siendo suyo. Esa es toda la
    # diferencia entre *leer* un flujo y *consumirlo*, y es lo que permite que un
    # destilador trabaje la cola de la wiki sin vaciarle la bandeja a la sesión que
    # atiende esa misma wiki. Sin este campo, un segundo lector del mismo flujo
    # obliga a elegir entre compartir cursor (se pisan) o duplicar el destinatario
    # en cada entrada (obliga a cambiar cómo escriben los demás, que es justo lo que
    # este producto promete no hacer).
    escucha = {a["nombre"].lower(): list(a.get("escucha", []))
               for a in d.get("agentes", []) if a.get("escucha")}
    # ESCUCHA_AUTOR — el espejo del anterior, y NO es el mismo campo con otro nombre.
    # `escucha` entrega lo dirigido A alguien (filtra `recipients.who`); esto entrega
    # lo ESCRITO POR alguien (filtra `entries.actor`). La diferencia es la que separa
    # «me entero de lo que le piden al CPO» de «me entero de lo que el CPO decide», y
    # sólo el segundo sirve para que QA valide contra el criterio de producto en vez
    # de contra lo que el implementador entendió (orden del operador, 2026-08-13).
    #
    # Y baja el fan-out en lugar de subirlo: la alternativa era que el CPO pusiera a
    # QA en copia de todo. Medido el 2026-08-13 sobre el despliegue real, el CPO ya
    # iba a 6,62 destinatarios por envío — suscribirse cuesta cero destinatarios.
    escucha_autor = {a["nombre"].lower(): list(a.get("escucha_autor", []))
                     for a in d.get("agentes", []) if a.get("escucha_autor")}
    return nombres, dueno, d.get("difusion", []), escucha, escucha_autor


AGENTES, DUENO, DIFUSION, ESCUCHA, ESCUCHA_AUTOR = _censo()


def _roles():
    """nombre → ROL, leído del censo. Sin heurística de sufijos: es DATO.

    El mismo rol vive en el censo bajo varios nombres (`qa` y `qa-2`, `cto` y
    `cto-b`: en el censo que motivó esto, 13 de 27 roles tenían más de uno). Para el reparto de
    trabajo eso es una fuga real — el tope triadversarial de 3 contaría 3 NOMBRES y
    dejaría que un mismo rol ocupe dos plazas de revisión.

    Se resuelve con un campo `rol` OPCIONAL en `roster.json`. Si no está, el nombre
    ES su rol y el comportamiento no cambia: nadie se encuentra una conducta nueva
    por actualizar. Deliberadamente NO se agrupa por parecido de nombre — la
    identidad se declara, no se adivina; adivinarla es cómo `cto-cfo-cockpit` acaba
    contando como `cto` el día que sean dos personas distintas.
    """
    import json as _json
    import os as _os
    ruta = _os.environ.get("LLMINBOX_ROSTER") or _os.path.join(
        _os.path.dirname(_os.path.abspath(__file__)), "roster.json")
    try:
        with open(ruta, encoding="utf-8") as fh:
            d = _json.load(fh)
    except Exception:
        return {}
    return {a["nombre"].lower(): (a.get("rol") or a["nombre"])
            for a in d.get("agentes", []) if a.get("nombre")}


ROL_DE = _roles()


def rol_de(nombre: str) -> str:
    """El rol de un agente. Si el censo no lo declara, su rol es él mismo."""
    return ROL_DE.get((nombre or "").lower(), canonico(nombre))
# Conjunto en minúsculas para separar, al leer destinatarios, quién es un agente
# concreto de quién es un destino de difusión (equipo/FLOTA/todos) — ver DIFSET
# más abajo, usado en `_campos` para no mezclarlos en `to`.
DIFSET = {d.lower() for d in DIFUSION}
AGENTES = AGENTES + DIFUSION

# minúsculas → nombre CANÓNICO. Sin esto, `IGNORECASE` parte la identidad: se
# devolvía el texto encontrado, así que `ADA`, `Ada` y `ada` entraban como tres
# actores distintos y un filtro por uno perdía a los otros dos. Salió al ver el
# desplegable de la interfaz lleno de parejas.
CANON = {a.lower(): a for a in AGENTES}


# ── Identidad para RESOLUCIÓN (llminbox ①/②) — DISTINTA de CANON/AGENTES/RE_AGENTE ──
# CANON/AGENTES son para PARSEAR el markdown (quién firma, a quién va dirigido).
# Esto es para decidir si un {agent} de una URL EXISTE. Mezclarlas ensancharía el
# enrutado de correo sin que nadie lo haya pedido — "fe" es palabra española común,
# "be" casaría con cualquier frase que la contenga. Nunca se usa para leer texto.
ROLES_VALIDOS = {r.lower() for r in ROL_DE.values()}   # ~14 tokens: be,cto,fe,wiki...


def _cargar_roles_por_alias():
    """Lee `roles-por-alias.json` — la fuente FIRMADA del censo (el operador, ver
    `_firmado_por`/`_orden` del propio fichero) — si está montada.

    Antes, `canon_identidad()` derivaba su unión de `roster.json` en exclusiva.
    Los dos ficheros eran byte-idénticos el 2026-08-10, pero sin ningún gate que
    lo garantice: un alta firmada AQUÍ sin tocar `roster.json` (que cada operador
    edita a mano en su máquina) devolvía 422 a una identidad legítima. Mismo
    patrón que `_cargar_carriles()` en servicio.py: env var vacía por defecto
    (`LLMINBOX_ROLES_ALIAS`) + mount read-only del fichero real — quien clone
    esto no se encuentra una fuente ajena montada sola.

    Devuelve None si la env var está vacía o el fichero no se deja leer — la
    señal de "no montado" que `canon_identidad()` usa para DEGRADAR a la
    conducta de hoy (roster.json) en vez de fallar abierto.

    ⚠️ `sin_rol` NO se carga como veto, y no es un descuido. Un primer intento
    de este fix devolvía 422 a todo nombre listado en `sin_rol`, leyendo su
    nombre como "no es identidad". Falsado contra el índice vivo el 2026-08-10:
    los 13 nombres de `sin_rol` están TODOS en `roster.json`, y dos tienen
    cursores ACTIVOS (`bikeus` en 2 ledgers, `lead-b-cfo-cockpit` en 1) —
    vetarlos habría roto la bandeja del carril bik.eus en el deploy. `sin_rol`
    significa "sin ROL que agrupe" (no colapsan en la migración ②, cada uno es
    su propia clave de cursor vía `rol_de()`), no "sin bandeja".
    """
    import json as _json
    import os as _os
    ruta = _os.environ.get("LLMINBOX_ROLES_ALIAS", "")
    if not ruta:
        return None
    try:
        with open(ruta, encoding="utf-8") as fh:
            d = _json.load(fh)
    except Exception as e:
        print(f"[roles-alias] no pude leer {ruta}: {e} — degrado a roster.json "
              f"(riesgo documentado: un alta firmada aquí sin tocar roster.json "
              f"no se vería hasta que el fichero vuelva a leerse)", flush=True)
        return None
    # Valores TAMBIÉN en minúsculas: `canon_identidad()` compara el nombre ya
    # bajado contra `.values()`, y un `"BE"` en el fichero firmado (que se edita
    # a mano, fuera de este repo) dejaría ese rol sin matchear en silencio.
    return _mapa_alias(d)


def _mapa_alias(d: dict) -> dict:
    return {k.lower(): v.lower() for k, v in d.get("rol_por_alias", {}).items()}


def _mapa_jerarquia(d: dict) -> dict:
    return {k.lower(): v for k, v in d.get("jerarquia", {}).items()}


ROLES_ALIAS = _cargar_roles_por_alias()


def _cargar_jerarquia() -> dict:
    """`rol → {reporta_a, capa, gatea, criterios_de}` del mismo fichero firmado.

    Vive junto a `rol_por_alias` porque es el mismo dato firmado por el mismo
    responsable, y **es rol→rol**, que es la forma correcta: `alias → rol` es
    muchos-a-uno y no puede sostener una jerarquía, igual que no puede sostener
    el mapa `rol × carril → bandeja` que ORGANIGRAMA.md §4 ya declara que no cabe
    aquí. Son tres datos con tres formas distintas; sólo dos comparten fichero.

    Diccionario vacío = no montado. Es una AUSENCIA que hay que decir en voz alta,
    no un organigrama plano: un agente que lee «no reportas a nadie» concluye lo
    contrario de la verdad. Quien sirva esto tiene que distinguir los dos casos.
    """
    import json as _json
    import os as _os
    ruta = _os.environ.get("LLMINBOX_ROLES_ALIAS", "")
    if not ruta:
        return {}
    try:
        with open(ruta, encoding="utf-8") as fh:
            return _mapa_jerarquia(_json.load(fh))
    except Exception as e:
        print(f"[jerarquia] no pude leer {ruta}: {e} — se sirve vacía y se avisa",
              flush=True)
        return {}


JERARQUIA = _cargar_jerarquia()

# Lo que está EFECTIVAMENTE cargado, para poder contestar «¿de qué bytes hablo?».
ORG_SHA: str | None = None
ORG_CARGADO_EN: str | None = None
ORG_REVISION = None


_ORG_CERROJO = _threading.Lock()


def _foto_org(montada: bool, source: str | None) -> dict:
    """Instantánea INMUTABLE del organigrama. Se construye SIEMPRE dentro del
    cerrojo, y el llamante no vuelve a mirar los globales.

    Sin esto había una carrera —la misma del sello del PR #5, un piso más arriba—:
    el refresco devolvía el hash de una revisión y el endpoint leía `JERARQUIA` y
    `ORG_REVISION` de otra si una petición concurrente recargaba en medio. Servir
    el hash de una con la jerarquía de otra no es un organigrama viejo: es uno
    IMPOSIBLE, y firmado.
    """
    return {"montada": montada, "source_sha256": source,
            "loaded_sha256": ORG_SHA, "jerarquia": dict(JERARQUIA),
            # `rol_por_alias` va en la MISMA foto: son proyecciones distintas del
            # mismo fichero y compararlas leyendo una de la foto y otra del global
            # reabriría la carrera por la puerta de al lado.
            "roles_alias": dict(ROLES_ALIAS) if ROLES_ALIAS is not None else None,
            "revision": ORG_REVISION, "cargado_en": ORG_CARGADO_EN}


# Si la fuente se vuelve ilegible, se avisa UNA vez. `refrescar_organigrama()`
# corre en cada petición: sin este marcador una sola avería imprime una línea por
# petición y entierra el resto del log. Se rearma al recuperarse — si no, la
# SIGUIENTE caída sería la que pasa desapercibida.
_ORG_FALLO_AVISADO = False


def refrescar_organigrama() -> dict:
    """Relee la fuente firmada si sus bytes cambiaron y devuelve UNA instantánea.

    Nació de un fallo de producción (2026-08-18): el bind-mount de FICHERO ÚNICO
    quedó apuntando a un inodo borrado cuando el host reemplazó el fichero por
    rename, y `/organigrama` siguió sirviendo 15 roles de hacía dos días **sin
    avisar** — porque el fichero sí se había leído al arrancar: se leyó el viejo.

    Tres decisiones que salen de ahí:

    · **Se abre por RUTA en cada petición.** Resolver la ruta de nuevo es lo que
      derrota al inodo borrado: con el mount roto, `open()` da ENOENT y el fallo
      se vuelve visible en vez de silencioso. Cachear un descriptor lo reeditaría.
    · **Sin TTL.** Una ventana en la que la fuente ya cambió y esto contesta
      «fresco» es exactamente la mentira que había. Hashear 6 KB por petición
      cuesta menos que afirmar frescura que no se tiene.
    · **La FORMA se valida dentro del bloque protegido.** `json.loads` acepta `[]`
      y `"texto"`: son JSON válido y no son un organigrama. Derivar los mapas
      fuera del `try` convertía eso en un 500, y un 500 no es «rancio» — es que el
      endpoint se cae. Una fuente con forma ajena es indistinguible de una
      ilegible: no se puede derivar nada de ella, y se trata igual.

    Si la fuente no se deja leer se CONSERVA lo último bueno y se marca rancio:
    servir una jerarquía vacía sería peor —el agente leería «no reporto a nadie»,
    que es lo contrario de la verdad—, pero servirla como buena es lo que falló.
    """
    global ROLES_ALIAS, JERARQUIA, ORG_SHA, ORG_CARGADO_EN, ORG_REVISION, _ORG_FALLO_AVISADO
    import hashlib as _h
    import json as _json
    import os as _os
    from datetime import datetime as _dt, timezone as _tz

    ruta = _os.environ.get("LLMINBOX_ROLES_ALIAS", "")
    with _ORG_CERROJO:
        if not ruta:
            return _foto_org(False, None)
        try:
            with open(ruta, "rb") as fh:
                crudo = fh.read()
            sha = _h.sha256(crudo).hexdigest()
            d = _json.loads(crudo.decode("utf-8"))
            # LOS MAPAS SE DERIVAN AQUÍ DENTRO, y eso es todo lo que hace falta:
            # si `d` es `[]`, `"texto"` o `5`, o si sus campos no son mapas, el
            # `.get()`/`.items()` revienta AQUÍ y lo recoge el `except`. Un
            # `isinstance(d, dict)` explícito delante sería código muerto — lo
            # escribí, y el mutante que lo quitaba sobrevivió: no cambiaba nada.
            # Una comprobación sin falsador es adorno.
            alias, jer = _mapa_alias(d), _mapa_jerarquia(d)
        except Exception as e:
            # Ilegible, corrupta o con forma ajena: NO se toca el estado bueno.
            if not _ORG_FALLO_AVISADO:
                _ORG_FALLO_AVISADO = True
                print(f"[organigrama] la fuente firmada dejó de leerse ({ruta}): "
                      f"{type(e).__name__}: {e} — sirvo lo último bueno MARCADO "
                      f"como rancio. No vuelvo a repetir este aviso hasta que se "
                      f"recupere.", flush=True)
            return _foto_org(True, None)
        if sha != ORG_SHA:
            # Los DOS mapas salen del MISMO fichero, así que se publican juntos.
            # Si sólo se refrescara la jerarquía, `engineering-manager` seguiría
            # saliendo en el organigrama y dando 422 en la bandeja: un rol que no
            # puede recibir trabajo. Ese caso está medido, no es hipotético.
            ROLES_ALIAS, JERARQUIA = alias, jer
            ORG_REVISION = d.get("_revision")
            ORG_SHA = sha
            ORG_CARGADO_EN = _dt.now(_tz.utc).isoformat(timespec="seconds")
        if _ORG_FALLO_AVISADO:
            _ORG_FALLO_AVISADO = False
            print(f"[organigrama] la fuente firmada vuelve a leerse ({ruta})",
                  flush=True)
        return _foto_org(True, sha)


def canon_identidad(nombre: str) -> str | None:
    """Forma canónica de `nombre` para identidad de cursor, o None si no resuelve.

    Con `roles-por-alias.json` MONTADO (`LLMINBOX_ROLES_ALIAS`), la unión del
    encargo: `rol_por_alias` (claves = alias, valores = rol) ∪ sus propios
    tokens de rol sueltos ∪ agentes/humanos/difusión de roster.json (CANON)
    ∪ ROLES_VALIDOS. Esa última pata no sobra: montar el fichero firmado tiene
    que AÑADIR identidades, nunca quitarlas (re-review×3: un rol de roster sin
    representación como valor en `rol_por_alias` pasaba de 200 a 422 justo al
    montar — la migración ② escribe cursores bajo esos tokens vía `rol_de()`,
    así que quitarles la lectura rompería lo que ② acaba de fusionar).
    Los nombres de `sin_rol` NO vetan nada: si están en roster resuelven como
    ellos mismos (caso `bikeus`, con cursores vivos — ver docstring de
    `_cargar_roles_por_alias`), y si no están en ninguna fuente no resuelven,
    como cualquier otro nombre desconocido.

    SIN montar (env var vacía o fichero ilegible): degrada a la conducta de
    hoy — sólo roster.json (CANON ∪ ROLES_VALIDOS) — SIN fail-open. El riesgo
    de que las dos fuentes diverjan queda documentado, no resuelto: mientras
    no esté montado, un alta que sólo exista en roles-por-alias.json sigue sin
    resolver, igual que antes de este fix.
    """
    if not nombre:
        return None
    bajo = nombre.lower()
    if ROLES_ALIAS is not None:
        # CANON PRIMERO, y el orden es una cicatriz, no una preferencia: con
        # ROLES_ALIAS delante, 'wiki-vault' resolvía a su rol 'wiki' y TODO el
        # camino aguas abajo que trabaja con el nombre de agente —escuchados()
        # busca en `recipients` lo que el parser escribió, y el parser escribe
        # 'wiki-vault', jamás 'wiki'— devolvía bandeja VACÍA para toda identidad
        # con alias en el fichero firmado. Lo cazó el falsador vivo post-deploy
        # («nada nuevo» con 500+ entradas pendientes), no la suite: los tests
        # asertaban códigos y cursores, no contenido — ver
        # test_inbox_montado_muestra_contenido. El cursor no cambia con el
        # orden: clave_cursor() pasa por rol_de() igual para 'wiki-vault' que
        # para 'wiki'.
        if bajo in CANON:
            return CANON[bajo]
        if bajo in ROLES_ALIAS:
            return ROLES_ALIAS[bajo]
        if bajo in ROLES_ALIAS.values():
            return bajo
        if bajo in ROLES_VALIDOS:
            return bajo
        return None
    if bajo in CANON:
        return CANON[bajo]
    if bajo in ROLES_VALIDOS:
        return bajo
    return None


# ── Direccionamiento por `@nombre` en cabeceras SIN flecha ────────────────────
#
# Sin flecha, `to` se quedaba vacío. Y eso NO significaba «no iba a nadie»: medido
# sobre 64bis en agosto, de 1.350 cabeceras sin flecha **995 nombraban con `@` a un
# agente DEL CENSO** y llminbox no se lo entregaba a ninguno. El mensaje dirigido no
# llegaba dirigido ⇒ todos leían el canal entero ⇒ cualquiera cogía el trabajo. La
# duplicación que el operador nos reprocha la fabricaba este hueco, no la disciplina.
#
# Va con CORTE POR FECHA y apagado por defecto, y el motivo está medido: aplicarlo
# retroactivo haría aparecer **12.442 entradas de golpe** repartidas en 25 agentes
# —`qa` se despertaría con 1.977 sin leer—, y una bandeja de dos mil devuelve a
# cualquiera a leer el canal entero, que es justo lo que esto viene a quitar.
# `LLMINBOX_ARROBA_DESDE=YYYY-MM-DD` ⇒ sólo las entradas con sello IGUAL O POSTERIOR.
# Vacío ⇒ desactivado: quien clone esto no se encuentra un cambio de conducta.
ARROBA_DESDE = os.environ.get("LLMINBOX_ARROBA_DESDE", "").strip()
# PRESENTE E INVÁLIDO ⇒ RUIDOSO. Se compara como TEXTO contra `e.ts`, que siempre viene con
# cero-relleno, así que un valor mal escrito no falla: pierde SIEMPRE la comparación y apaga
# el enrutado por `@` entero, en silencio. Medido:
#
#     '2026-8-8'   vs ts '2026-08-09T12:00:00'  →  no enruta   ← el typo que cualquiera hace
#     '8/8/2026'                                →  no enruta
#     'ayer'                                    →  no enruta
#     '2026-08-08'                              →  enruta
#
# Y esta variable existe porque hubo 995 destinatarios por arroba sin entregar: apagarla
# calladamente devuelve ese fallo exacto, con el operador convencido de haberla puesto.
# `LLMINBOX_WIP_GLOBAL` ya aborta si su valor no vale; el patrón hermano estaba en el repo.
#
# AUSENTE sigue siendo legítimo y silencioso: significa «sin corte».
if ARROBA_DESDE:
    import datetime as _dt
    try:
        if _dt.date.fromisoformat(ARROBA_DESDE).isoformat() != ARROBA_DESDE:
            raise ValueError("no es la forma canónica")
    except ValueError:
        raise SystemExit(
            f"[arranque] LLMINBOX_ARROBA_DESDE={ARROBA_DESDE!r} no es una fecha "
            f"AAAA-MM-DD con cero-relleno. Se compara como TEXTO contra el sello de "
            f"cada entrada, así que un valor mal escrito NO falla: apaga el enrutado "
            f"por `@` sin decir nada. Escribe 2026-08-08, o no declares la variable.")
# El sigilo `@` ES la señal, y por eso no se leen los nombres a secas: mencionar a un
# agente («como midió backend…») es cita, no destino. Confundirlas convertiría cada
# cita en una entrega y multiplicaría el correo en vez de dirigirlo.
RE_ARROBA = re.compile(r"@([A-Za-z][A-Za-z0-9_-]{1,24})")


def canonico(nombre):
    return CANON.get(nombre.lower(), nombre) if nombre else nombre


def escuchados(agent: str) -> list[str]:
    """Los nombres cuyo correo cae en la bandeja de `agent`: el suyo, los que
    escuche, y TODAS las firmas censadas de sus roles (2026-08-16).

    Canónicos y sin repetir, con el propio SIEMPRE primero. Un nombre que el censo no
    conoce se devuelve tal cual: alguien puede tener bandeja antes de estar dado de
    alta, y negársela por eso sería esconder correo que existe.

    La expansión por rol cierra la asimetría que midió infra (MARK:infra-el-
    cursor-va-por-rol-y-el-emparejado-por-alias…): el CURSOR ya colapsa por rol
    (`migrar_alias_a_rol`), pero este match iba por literal — así que drenar por
    un alias adelantaba el cursor del ROL por encima de entradas que sólo se
    veían desde el alias hermano, sin error visible. Daño medido en vivo: a cto
    su alias estrecho le enseñaba 3 de 6 pendientes, y @backend le había
    contestado a una pregunta que nunca vio. Es el mismo arreglo que
    `escuchados_autor()` lleva desde el 08-13 vía `firmas_del_rol()`, y en la
    MISMA capa (una, no cada emisor — la condición de infra). Dirección de
    fallo: un rol ve MÁS de su propio correo, jamás menos, y `firmas_del_rol`
    filtra por rol exacto, así que jamás el de otro rol.
    """
    fuera, vistos = [], set()

    def mete(nombre: str) -> None:
        if nombre.lower() not in vistos:
            vistos.add(nombre.lower())
            fuera.append(nombre)

    for n in [agent] + list(ESCUCHA.get(agent.lower(), [])):
        mete(canonico(n))                      # el propio literal, SIEMPRE antes
        for firma in firmas_del_rol(rol_de(n)):  # …y sus hermanos de rol
            mete(firma)
    return fuera


def firmas_del_rol(nombre: str) -> list[str]:
    """Todas las firmas censadas de un ROL. Si no es un rol, el nombre canónico.

    Existe porque en esta casa un rol NO firma con un nombre. Medido el
    2026-08-13 sobre los ledgers vivos: `cpo` firma como `cpo` (778),
    `cpo-biklabs` (265) y `cpo-cfo-cockpit` (84); `cto` con CINCO, y la más
    frecuente no es `cto` sino `cto-A`. Resolver una suscripción por el nombre
    literal entrega el 69 % del flujo y pierde un carril entero — sin error,
    sin hueco visible, con la bandeja en verde. Ese es justo el fallo que no se
    detecta leyendo la salida.
    """
    objetivo = (nombre or "").lower()
    firmas = [canonico(n) for n, r in ROL_DE.items() if str(r).lower() == objetivo]
    return firmas or [canonico(nombre)]


def escuchados_autor(agent: str) -> list[str]:
    """Los AUTORES a los que `agent` está suscrito, expandidos por rol.

    Se devuelve canónico porque `entries.actor` guarda lo que el parser resolvió
    contra el censo, no lo que tecleó quien firmó: comparar sin canonizar dejaría
    fuera a un `CPO` en mayúsculas y la suscripción no entregaría nada, en silencio.

    A diferencia de `escuchados()`, el propio agente NO se incluye: uno no se
    suscribe a sí mismo, y meterlo aquí duplicaría cada entrada suya que ya le
    llega por destinatario.
    """
    fuera, vistos = [], set()
    for n in ESCUCHA_AUTOR.get(agent.lower(), []):
        for c in firmas_del_rol(n):
            if c.lower() not in vistos:
                vistos.add(c.lower())
                fuera.append(c)
    return fuera
# Se ordenan de más largo a más corto para que `alice-backend` gane a `cto`.
AGENTES.sort(key=len, reverse=True)
# IGNORECASE: la convención histórica `## <fecha> [BE→CTO]` va en mayúsculas y sin
# esto no casaba ninguna — 1.717 entradas invisibles al extractor.
# Con el censo VACÍO —el día uno de cualquiera que instale esto— una alternancia sin
# ramas casa la cadena vacía en todas partes y devolvía `to=['']`: destinatarios
# fantasma en cada entrada. Un patrón que no puede casar nunca es la respuesta.
RE_AGENTE = re.compile(
    (r"(?<![\w-])(" + "|".join(re.escape(a) for a in AGENTES) + r")(?![\w-])")
    if AGENTES else r"(?!)", re.IGNORECASE)

# ETIQUETAS que NO son agentes aunque abran la cabecera. `HEARTBEAT` estaba en la
# lista de agentes y ganaba la carrera por posición: `### [HEARTBEAT alice-backend]`
# se atribuía a "HEARTBEAT" en vez de a alice-backend. Medido sobre el corpus real:
# **3.441 de 23.496 entradas (14,6%)** con el actor sustituido por la etiqueta, así
# que `/entries?actor=alice-backend` perdía todos sus heartbeats. Se saltan antes de
# buscar el actor, y se recogen como tipo.
# La posición canónica del tipo: `### [origen → destino · TIPO] titular`. Se
# captura el lexema TAL CUAL —sin validarlo contra `TIPOS` y SIN normalizarlo—,
# porque validar aquí es lo que hacía que 641 entradas perdieran lo que sí habían
# declarado, y normalizar es ya interpretar: decidir que `Medido` y `MEDIDO` son
# la misma palabra le toca al registro canónico, con su revisión anotada, no al
# troceador por su cuenta.
#
# Sin clase de caracteres «permitidos»: la primera versión aceptaba sólo letras,
# `_`, `/` y `-`, o sea traía su propio vocabulario implícito y perdía `REVIEW.V2`
# — el mismo fallo una capa más abajo. Se acota por LONGITUD, no por alfabeto.
#
# `·` sí queda fuera de la clase, y eso no es vocabulario: hace que capture el
# ÚLTIMO campo de la cabecera. Sin esa exclusión, un `a · b · TIPO]` devolvería
# «b · TIPO».
RAW_TIPO = re.compile(r"·\s*([^·\]\r\n]{1,64}?)\s*\]")

# Operadores de RUTA del propio formato. Excluirlos no es vocabulario de palabras:
# es respetar la sintaxis de la cabecera, donde `→`/`∧` separan actores.
OPERADORES_RUTA = ("→", "->", "←", "<-", "∧", "&")
# DOS CONSTANTES, DOS PREGUNTAS. Reutilizar una para la otra ya produjo dos
# defectos seguidos en esta misma función, así que la diferencia va escrita:
#
#   OPERADORES_RUTA  ¿qué caracteres prueban que un candidato NO es un tipo
#                    atómico? Ahí entran las conjunciones (`a ∧ b` tampoco lo es)
#                    y las flechas inversas: cualquiera de los seis descalifica.
#
#   FLECHAS_RUTA     ¿qué sintaxis de routing SOPORTA este parser? Sólo `→` y
#                    `->`, que son las que consume `FLECHA` en `_campos()`.
#
# `←` y `<-` quedan FUERA a propósito, y no por omisión: `_campos()` no las
# reconoce, así que tratarlas como ruta aquí crea la incoherencia de que
# `[a ← b · 64bis]` tenga ranura de tipo para una función y no para la otra —
# medido: `raw_tipo_de` devolvía `64bis` mientras `_campos` tomaba `64bis` como
# ACTOR. Soportarlas de verdad exige decidir quién es actor y quién destinatario
# cuando la dirección se invierte, y eso es semántica nueva, no dos caracteres
# más en un regex. Otro trabajo, con su medición del corpus.
#
# Y `∧`/`&` tampoco: JUNTAN participantes, no acreditan dirección. Con ellas
# dentro, `[wiki-vault ∧ qa · 64bis]` volvía a dar `64bis`.
FLECHAS_RUTA = ("→", "->")


def _es_token_de_tipo(v: str) -> bool:
    """Un tipo es UN SOLO TOKEN. Espacios y operadores de ruta lo descalifican.

    Nace de medir la otra cara del filo: liberar el capturador de su vocabulario
    cerrado (que perdía 641 tipos reales) lo dejó capturando 38.848 valores, de
    los que **7.570 eran RUTAS o prosa** — `bikeus→security ∧ <humano>`, `BARRIDO
    CERRADO security→bikeus`—, porque `·` se usa como separador general y el
    último campo no siempre es el tipo.

    La regla NO es una lista negra de flechas: eso deja pasar 3.855 rutas con
    espacios y sin flecha. Es la FORMA. Medido: con esta guarda sobreviven los
    31.273 que sí lo son, y los tipos del hallazgo enteros (MEDIDO 376,
    MEASURED 340, ADJUDICADO 90, VEREDICTO 27).
    """
    return bool(v) and not re.search(r"\s", v) and not any(o in v for o in OPERADORES_RUTA)
ETIQUETAS = re.compile(r"^\s*(HEARTBEAT|CARRY-FORWARD|CLAIM|CANON|CERT|DONE|RESP|AVISO|MSG|ASK|ACK|STATUS|INFO|HANDOFF)"
                       r"(?:[/·\-]\s*(?:HEARTBEAT|CARRY-FORWARD|CLAIM|CANON|CERT|DONE|RESP|AVISO|MSG|ASK|ACK|STATUS|INFO|HANDOFF))*\s*",
                       re.IGNORECASE)

# Anotación entre paréntesis: los agentes explican CADA destinatario con su motivo,
# y esas explicaciones llevan dentro los mismos separadores (`·`, `—`, `]`) que se
# usaban para cortar la lista. Cortar en el primero perdía TODOS los destinatarios
# posteriores — medido a mano sobre 30 cabeceras: de 14 con 3+ destinatarios, 12
# perdieron al menos uno. Se quitan las anotaciones ANTES de leer la lista.
PARENT = re.compile(r"\([^()]*\)|\[[^\[\]]*\]")

# Separadores de destinatario usados de verdad en las cabeceras: →, ->, ∧, +, y
FLECHA = re.compile(r"\s*(?:→|->)\s*")

# Hueco entre dos agentes PEGADOS justo antes de la flecha (`CTO+BE →`, `BE/CTO
# →`): si entre el fin de uno y el principio del otro no hay nada más que este
# separador, son coautores, no "el segundo tapa al primero". Ver _campos().
JUNTA = re.compile(r"^\s*[+/∧]\s*$")

# La coautoría solo se lee cuando la flecha está CERCA del principio de la
# cabecera. Medido sobre el corpus real (2026-07-27): en las 7 cabeceras
# genuinas de coautoría (`[CTO+BE → ...]`), la flecha cae a 21-23 caracteres
# del principio. Pero hay cabeceras HEARTBEAT larguísimas —una sola línea de
# hasta 1.120 caracteres— donde aparece una flecha SUELTA dentro de la prosa
# libre (p.ej. "cutover B-1 (be/cto/sec/fe) → ..."), y justo antes por
# casualidad caen dos nombres de agente pegados: sin este tope, 17 de las 24
# cabeceras que cambiaban de actor eran ESTE ruido, no coautoría real — la
# más cercana de las falsas cae a 96 caracteres, así que 40 deja margen de
# sobra a un lado y al otro.
COAUTOR_MAX_IZQ = 40


def _sin_emoji(s: str) -> str:
    """Quita símbolos/pictogramas para que el extractor vea el texto."""
    return "".join(c for c in s if unicodedata.category(c) not in ("So", "Sk", "Cs"))


@dataclass
class Entrada:
    seq: int
    line_no: int
    byte_off: int
    head: str
    text: str
    ts: str | None = None
    actor: str | None = None
    to: list[str] = field(default_factory=list)
    # ¿Los destinatarios salieron de un `@` en una cabecera SIN flecha? Se marca
    # porque quien indexa necesita distinguirlo: para una entrada NUEVA se enrutan
    # siempre —es correo de ahora—, pero re-derivar el histórico añadiría 1.679
    # entradas de golpe a las bandejas. La decisión no la puede tomar el troceador,
    # que no sabe si la entrada es nueva; la toma `reindex`, que sí.
    por_arroba: bool = False
    # Destinos de DIFUSIÓN (equipo/FLOTA/todos — ver roster.json), separados de
    # `to` a propósito: no son un agente al que dirigir una bandeja individual.
    # Campo nuevo (PARSER_V 5); servicio.py de hoy no lo lee — es aditivo, no
    # rompe a quien solo mire `.to`.
    difusion: list[str] = field(default_factory=list)
    tipo: str | None = None
    # El lexema escrito en una posición COMPATIBLE CON LA GRAMÁTICA DE TIPO — no
    # «todo lo escrito», que es distinto y se documenta en `raw_tipo_de`. `tipo` es
    # lo que el sistema INTERPRETA de él. Existen los dos porque medí que no
    # coinciden: 641 entradas del ledger piloto (8,2 %) declaran un tipo en posición
    # canónica que este parser tiraba —MEDIDO, MEASURED, ADJUDICADO, VEREDICTO…—, y
    # `lint` las contaba como «no declaran nada» cuando declaran de sobra.
    # El literal íntegro no se pierde: `head` se guarda entero.
    raw_tipo: str | None = None

    @property
    def sha(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


# La misma alternativa spoke que ya lleva `H_ENTRY`, aparte para que las dos no
# puedan divergir en silencio.
_SPOKE = re.compile(r"^## \d{4}-\d{2}-\d{2}T[\d:]+Z\s*·")


def raw_tipo_de(head: str) -> str | None:
    """El lexema escrito en una posición COMPATIBLE CON LA GRAMÁTICA DE TIPO.

    El nombre importa: esto no es «todo lo que había escrito». Es el último campo
    de la cabecera **si además tiene forma de token de tipo** (ver
    `_es_token_de_tipo`), que ya es una clasificación SINTÁCTICA — no semántica,
    pero clasificación. Llamarlo «el literal, sin más» sería afirmar de más:
    `bikeus→security ∧ <humano>` también estaba escrito ahí y aquí devuelve None.

    El literal COMPLETO no se pierde: vive en `head`, que se guarda entero y
    permite reproducir esta clasificación en cualquier momento. Por eso no hace
    falta una columna más para conservarlo.

    Las capas, para que nadie las mezcle:

        head            lo escrito, íntegro
          ↓
        último campo    candidato tras el `·`
          ↓ forma
        raw_tipo        el candidato SI tiene forma de tipo (esto)
          ↓ registro versionado
        canonical_kind  semántica del Agent OS   (+ kind_registry_rev)

        tipo            aparte: el vocabulario legacy que este servicio reconoce

    UNA sola fuente para la primera flecha.

    Se expone aparte de `_campos` porque el backfill del corpus ya indexado lo
    necesita **sin volver a leer el fichero**: `head` está guardado en la base, y
    `barrido()` salta un ledger entero cuando su tamaño y mtime no cambiaron —
    así que confiar la migración a una re-indexación dejaría sin rellenar todos
    los ledgers dormidos, para siempre.
    """
    inner = head
    mb = re.match(r"^#{2,3} \[(.*)$", head or "")
    if mb:
        inner = mb.group(1)
    inner = _sin_emoji(inner)
    cierre = inner.find("]")
    inner = inner[:cierre + 1] if cierre != -1 else inner
    # LA RANURA DE TIPO EXISTE CUANDO LA GRAMÁTICA LA TIENE, no cuando hay un `·`.
    # `### [wiki-vault·64bis]` es `agente·carril` —dos campos, sin ruta— y esta
    # rama tomaba el último igualmente: devolvía el CARRIL como tipo, 58 veces en
    # el corpus vivo, y `64bis` iba camino de colarse en el canon como si fuera
    # vocabulario. Lo mismo con `## [qa-biklabs · 2026-07-18T21:45:15Z]`, que
    # capturaba la FECHA (~35 más).
    #
    # El discriminante es la FLECHA, y tenía que ser estructural: mirar
    # si el candidato «parece un carril» o «parece un agente» no decide semántica,
    # y está demostrado aquí mismo — `HARNESS` es un nombre de agente Y está
    # legítimamente en posición de tipo. Un `if candidato in CARRILES: return None`
    # arreglaría el corpus de hoy, no la sintaxis.
    #
    #     [actor · carril]           → NO hay ranura      ← esto se estaba leyendo mal
    #     [actor → destino · TIPO]   → sí la hay
    #     [TIPO actor → destino]     → tipo FRONTAL, otra rama (ETIQUETAS, abajo)
    #
    # Sin operador de ruta la cabecera no dirige a nadie, y su último campo es un
    # calificador —carril, sello, nota—, no un tipo. Medido antes de aplicarlo:
    # 10.512 capturas de esta rama llevan ruta y 270 no; de esas 270, la inmensa
    # mayoría es carril, fecha, nombre de agente o prosa. El daño colateral se
    # declara: algún calificador que sí parecía tipo (`TRIAGE-PARKED`, `MÉTODO`)
    # deja de extraerse. El lexema NO se pierde — sigue entero en `head`, que es
    # de donde se deriva todo esto.
    _dirigida = any(f in inner for f in FLECHAS_RUTA)   # FLECHAS, no conjunciones
    m = RAW_TIPO.search(inner) if _dirigida else None
    if m and _es_token_de_tipo(m.group(1)):
        return m.group(1)
    # La forma SPOKE no lleva corchetes: `## <ISO> · a → b · TIPO`. `H_ENTRY` la
    # acepta, pero `RAW_TIPO` exige `]`, así que una cabecera válida devolvía None
    # y quedaba en NULL para siempre —ni `reindex()` ni la migración pueden
    # rellenar lo que esta función no ve—, y `/lint` la contaba como «sin tipo
    # declarado» teniéndolo escrito en su posición canónica. Medido el 2026-08-19
    # sobre producción: 556 cabeceras spoke sin corchetes, 66 con tipo válido
    # perdido. Lo señaló CodeRabbit revisando #9, ya desplegada.
    #
    # La precedencia la da el ORDEN, no un guarda: va DESPUÉS de `RAW_TIPO`, así
    # que la forma con corchetes manda —su tipo está DENTRO, no en el último campo
    # de la línea—. Escribí además un `mb is None` delante y el mutante que lo
    # quitaba SOBREVIVIÓ: `mb` exige `[` justo tras `##` y `_SPOKE` exige un
    # dígito ahí, así que no pueden casar a la vez y la condición no decidía nada.
    # Fuera: una comprobación sin falsador es adorno. Pasa por el MISMO guarda —
    # sin él esta rama se tragaría las rutas, que es el 7.570 medido que dio
    # origen a `_es_token_de_tipo`.
    if _SPOKE.match(inner):
        cand = inner.rsplit("·", 1)[-1].strip()
        if 0 < len(cand) <= 64 and _es_token_de_tipo(cand):
            return cand
    # Si el último campo NO era un tipo, la cabecera todavía puede declararlo al
    # frente (`### [DONE algo · bikeus→security ∧ <humano>]`). Cortar en seco aquí
    # sería descartar de más por el otro lado.
    et = ETIQUETAS.match(inner)
    return et.group(1) if et else None


def _campos(head: str, cola: str) -> tuple[
        str | None, str | None, list[str], list[str], str | None, bool, str | None]:
    """Extrae (ts, actor, destinatarios, difusion, tipo, por_arroba, raw_tipo).

    Devuelve None en lo que no se pueda leer. `por_arroba` dice si los destinatarios
    salieron de un `@` en una cabecera sin flecha — ver el campo del mismo nombre.
    `raw_tipo` es el LEXEMA escrito en la posición del tipo, se entienda o no
    (ver `raw_tipo_de`); `tipo` es lo que el sistema interpreta de él.
    """
    m = TS.search(head) or TS.search(cola)
    ts = _sello_valido(m.group(1)) if m else None

    # El cuerpo de la cabecera: lo de dentro del primer corchete si lo hay.
    inner = head
    mb = re.match(r"^#{2,3} \[(.*)$", head)
    if mb:
        inner = mb.group(1)
    inner = _sin_emoji(inner)

    # Las etiquetas de apertura (HEARTBEAT, CARRY-FORWARD…) se retiran antes de
    # buscar el actor: son el TIPO de la entrada, no quien la escribe.
    etiqueta = ETIQUETAS.match(inner)
    if etiqueta:
        inner = inner[etiqueta.end():]

    to: list[str] = []
    difusion: list[str] = []
    por_arroba = False
    actor = None
    # LA FLECHA DE RUTA VIVE DENTRO DEL CORCHETE. El comentario de arriba decía «lo de
    # dentro del primer corchete» y el regex captura hasta FIN DE LÍNEA: la intención
    # estaba escrita y no implementada. Consecuencia medida (2026-08-11, red entera):
    # una flecha usada como PUNTUACIÓN en la prosa —`cdfaf09b→c48021ca→dee19a1a`, una
    # cadena de SHAs— partía la cabecera ahí y convertía el resto del titular en lista
    # de destinatarios. **1.870 filas de destinatario fabricadas así**, 1.040 de ellas
    # en latidos que se colaban en bandejas ajenas cada 15 minutos.
    # ⚠️ El acotado es SÓLO para la flecha. La cosecha de `@` sigue leyendo el titular
    # ENTERO, y eso no es descuido: 661 destinatarios vivos hoy tienen su `@` DESPUÉS
    # del `]`. Acotar las dos cosas a la vez habría dado de baja ese correo en
    # silencio — arreglar 1.870 rompiendo 661 no es arreglar.
    cierre = inner.find("]")
    inner_ruta = inner[:cierre] if cierre != -1 else inner
    if FLECHA.search(inner_ruta):
        izq, der = FLECHA.split(inner_ruta, 1)
        # El actor es el nombre PEGADO a la flecha, no el primero que se mencione:
        # una cabecera que MENCIONA a otro agente antes de la flecha (`escritor
        # ... cita a @lector ... → destinatarios`) se atribuía a "lector" por ir
        # antes en el texto. Se busca por la derecha.
        izq_limpio = PARENT.sub(" ", izq)
        ma = list(RE_AGENTE.finditer(izq_limpio))
        if ma:
            # Autoría conjunta: dos agentes PEGADOS justo antes de la flecha
            # (`CTO+BE →`, `BE/CTO →`), sin nada más entre ellos que el
            # separador. Medido sobre el corpus real (2026-07-27): 7 cabeceras
            # así en 37k entradas; tomar solo el último match (`ma[-1]`) tiraba
            # al coautor — `CTO+BE → FLOTA` quedaba con actor="BE" y "CTO" se
            # perdía sin dejar rastro. Se juntan con "+" SOLO cuando el hueco
            # entre los dos últimos matches es justo ese separador: si hay
            # cualquier otra cosa en medio (p.ej. "SECURITY · FE", una etiqueta
            # de tema seguida del actor real) el comportamiento no cambia — el
            # último gana, que es lo correcto ahí.
            if (len(ma) >= 2 and len(izq_limpio) <= COAUTOR_MAX_IZQ
                    and JUNTA.fullmatch(izq_limpio[ma[-2].end():ma[-1].start()])):
                actor = canonico(ma[-2].group(1)) + "+" + canonico(ma[-1].group(1))
            else:
                actor = canonico(ma[-1].group(1))
        # Se quitan las anotaciones entre paréntesis/corchetes y se lee la lista
        # ENTERA hasta el cierre de la cabecera, no hasta el primer separador.
        der = PARENT.sub(" ", der)
        der = re.split(r"\]\s|\s—\s|\s–\s", der, maxsplit=1)[0]
        vistos = set()
        for m in RE_AGENTE.finditer(der):
            n = canonico(m.group(1))
            if n.lower() not in vistos:
                vistos.add(n.lower())
                # Los destinos de DIFUSIÓN (equipo/FLOTA/todos, ver roster.json)
                # no son un agente concreto: mezclados en `to` hacían
                # indistinguible "esto iba a una persona" de "esto iba a todo
                # el mundo". Medido: 1.915 de 37k entradas tenían ambos tipos
                # en la misma lista — `/lint` no podía separar deuda real
                # (nadie nombrado) de aviso general (nombrado el equipo entero).
                if n.lower() in DIFSET:
                    difusion.append(n)
                else:
                    to.append(n)
    else:
        ma = RE_AGENTE.search(PARENT.sub(" ", inner))
        actor = canonico(ma.group(1)) if ma else None
        # Sin flecha no hay lista de destino que leer… salvo los `@` (ver arriba).
        # Se calculan SIEMPRE que la función esté activa. El corte por fecha ya no
        # se aplica aquí: exigir sello dejaba fuera el 15,7 % de las entradas —8.550
        # sin `ts`— y a ésas el router ni las miraba. Quien decide es `reindex`.
        if ARROBA_DESDE:
            vistos = {actor.lower()} if actor else set()
            for m in RE_ARROBA.finditer(inner):
                bruto = m.group(1)
                # La pertenencia al censo se comprueba AQUÍ y no se delega en
                # `canonico`, que devuelve tal cual lo que no conoce: sin esta línea
                # un `@deploy` o un `@media` de CSS entraría como destinatario.
                if bruto.lower() not in CANON or bruto.lower() in vistos:
                    continue
                vistos.add(bruto.lower())
                n = canonico(bruto)
                (difusion if n.lower() in DIFSET else to).append(n)
                por_arroba = True

    # Lo escrito, se entienda o no. Se mira la posición canónica (`· TOKEN]`)
    # antes que la etiqueta al frente, porque es donde la flota lo pone.
    raw = raw_tipo_de(head)
    # Y EL TIPO SALE DE AHÍ, no de buscar palabras en la cabecera. Lo anterior era
    # `next((t for t in TIPOS if t in head), None)` seguido de aceptar CUALQUIER
    # etiqueta frontal, y por eso la columna acabó con 13 valores fuera del canon
    # en 22.882 entradas: una cabecera que MENCIONA «FYI» recibía tipo FYI.
    #
    # Esto sólo gobierna lo que se INDEXA a partir de ahora: `reindex()` no
    # reescribe `tipo` de las filas que ya existen —su UPDATE toca seq, line_no,
    # byte_off, ausente, provisional y raw_tipo, y nada más—, así que el histórico
    # inflado no se mueve aquí. Sanearlo es otra PR: son 31.077 filas y tiene
    # consumidores vivos.
    tipo = canonical_tipo(raw)
    return ts, actor, to, difusion, tipo, por_arroba, raw


def parse(path: str, desde_byte: int = 0):
    """Trocea el ledger en entradas. `desde_byte` permite ingesta incremental.

    Devuelve (entradas, byte_final). Las entradas se cortan en la SIGUIENTE cabecera,
    así que la última entrada de un fichero que está creciendo puede estar a medias:
    el llamante decide si la usa (leer) o la ignora (sellar).
    """
    with open(path, "rb") as fh:
        fh.seek(desde_byte)
        raw = fh.read()
    txt = raw.decode("utf-8", errors="replace")
    lines = txt.splitlines(keepends=True)

    offs, acc = [], desde_byte
    for l in lines:
        offs.append(acc)
        acc += len(l.encode("utf-8"))

    starts = [i for i, l in enumerate(lines) if H_ENTRY.match(l)]
    out = []
    for n, i in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        text = "".join(lines[i:end]).rstrip() + "\n"
        head = lines[i].rstrip("\n")
        cola = "".join(lines[i + 1:i + 4])
        ts, actor, to, difusion, tipo, por_arroba, raw_tipo = _campos(head, cola)
        out.append(Entrada(seq=n, line_no=i + 1, byte_off=offs[i], head=head,
                           text=text, ts=ts, actor=actor, to=to, difusion=difusion,
                           tipo=tipo, por_arroba=por_arroba, raw_tipo=raw_tipo))
    return out, acc


if __name__ == "__main__":
    import os
    import sys

    def _rutas_por_defecto():
        """Sin argumentos en la línea de comandos, lee `LLMINBOX_LEDGERS` — mismo
        formato `nombre=ruta,nombre=ruta` que `docker-compose.yml` — para que este
        módulo no lleve cableados los ledgers de un despliegue concreto. Antes
        (PARSER_V<5) la lista por defecto era literalmente la de este despliegue;
        eso no vale para un repo publicado, que no conoce los ledgers de nadie."""
        crudo = os.environ.get("LLMINBOX_LEDGERS", "")
        return [ruta for _, ruta in (p.split("=", 1) for p in crudo.split(",") if "=" in p)]

    rutas = sys.argv[1:] or _rutas_por_defecto()
    if not rutas:
        print("uso: python3 ledger_parse.py <ledger.md> [otro.md ...]")
        print("     (o exporta LLMINBOX_LEDGERS='nombre=/ruta/a/ledger.md,...')")
        sys.exit(1)
    tot = 0
    print(f"{'ledger':<32}{'entradas':>9}{'ts':>7}{'actor':>7}{'to':>7}{'difusion':>9}{'tipo':>7}")
    for r in rutas:
        p = os.path.expanduser(r)
        if not os.path.exists(p):
            continue
        ents, _ = parse(p)
        tot += len(ents)
        n = max(len(ents), 1)
        pct = lambda f: f"{100 * sum(1 for e in ents if f(e)) // n}%"  # noqa: E731
        print(f"{os.path.basename(p):<32}{len(ents):>9}"
              f"{pct(lambda e: e.ts):>7}{pct(lambda e: e.actor):>7}"
              f"{pct(lambda e: e.to):>7}{pct(lambda e: e.difusion):>9}{pct(lambda e: e.tipo):>7}")
    print(f"{'TOTAL':<32}{tot:>9}")
