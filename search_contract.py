"""M2 · Contrato de búsqueda: compilación de la consulta y canonicalización de filtros.

Este módulo no toca SQLite. Su única responsabilidad es convertir lo que teclea una
persona en algo que el motor FTS5 pueda recibir sin reventar, y fijar la forma canónica
de los filtros con la que después se firma el cursor.

## Por qué se COMPILA y no se reenvía

Medido por `qa` sobre el motor real (contrato M2 §7.3): de 7 entradas crudas plausibles,
**5 revientan** (`unterminated string`, `syntax error near "AND"`, `unknown special
query`, `no such column`) y **una se acepta como álgebra booleana sin que el usuario lo
pida** (`a NOT b`). Un `OperationalError` que sube como 500 lo filtra el llamante y se
lee como «no hay resultados» — la avería que este repo ya tuvo con `GET /lint`.

⇒ Toda entrada se descompone en TÉRMINOS y se reensambla como conjunción de literales
citados. El usuario nunca nombra una columna del índice, nunca abre un paréntesis y
nunca ejecuta un operador que no pidió.

## Por qué NUNCA se trunca

Recortar un término a 64 bytes cambia LA PREGUNTA en silencio: quien busca un
identificador largo recibe los resultados de su prefijo y no tiene forma de saberlo.
Un término que no cabe es un `TermTooLong` con su número, no un término más corto.
Misma regla para la consulta entera y para el número de términos: **se rechaza con
motivo, jamás se recorta**.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import unicodedata

# ── Límites duros. Van en bytes UTF-8, no en caracteres: el corpus tiene entradas de
# 11,6 MiB y una cota en «caracteres» no acota memoria. ───────────────────────────────
MAX_QUERY_BYTES = 512
MAX_TERMS = 16
MAX_TERM_BYTES = 64

# COTA BARATA, ANTES de normalizar. `NFKC` sobre megabytes es trabajo que elige el
# llamante: se mira `len(bytes)` —O(1)— antes de tocar Unicode. El factor 4 es el peor
# crecimiento plausible de NFKC sobre texto real; por encima de eso ya no puede caber.
COTA_CRUDA = 4
MAX_FILTER_BYTES = 256          # lane · ledger · actor · tipo · desde · hasta

# ── Normalización COMPARTIDA con el índice ────────────────────────────────────────
# El tokenizador `unicode61` NO normaliza: `oﬃce` (U+FB03) no casa con `office`, y `ß` no
# se pliega a `ss`. Si el índice guarda el texto crudo y la consulta se normaliza, hay
# ASIMETRÍA — la consulta encuentra menos de lo que existe, y en silencio. Por eso esta
# función es la ÚNICA autoridad y la usan los dos lados: `search_store` la aplica a la
# proyección indexada y `compile_query` a lo que teclea la persona.
#
# Los centinelas de resaltado se retiran aquí: si pudieran aparecer en el contenido, las
# marcas del fragmento serían ambiguas y no se podrían convertir en desplazamientos.
CENTINELA_INI = "\ue000"
CENTINELA_FIN = "\ue001"


def normalize_text(texto: str) -> str:
    """NFKC + `casefold`, y fuera los centinelas. Misma función para índice y consulta.

    NFKC ANTES de `casefold` y no al revés: la descomposición canónica puede producir
    letras cuyo plegado cambie, y el orden inverso deja pares que el índice sí unifica y
    esta capa no.
    """
    limpio = texto.replace(CENTINELA_INI, "").replace(CENTINELA_FIN, "")
    return unicodedata.normalize("NFKC", limpio).casefold()

CONTRACT_VERSION = 1
ORDER_ARRIVAL_DESC = "arrival_desc"
ORDERS = frozenset({ORDER_ARRIVAL_DESC})

# ENTRADAS RETIRADAS: en v0.9 se excluyen SIEMPRE, y no hay perilla.
#
# Tuve un modo `incluir` y lo retiro: un filtro que casi nadie manda es un filtro que casi
# nadie revisa, y éste decide si la respuesta mezcla entradas vivas con retiradas. Mientras
# la única política sea «vivas», expresarla como parámetro sólo crea una segunda población
# alcanzable por descuido. `e.ausente IS NULL` va FIJO en el SQL, no como opción.


class SearchContractError(Exception):
    """Raíz de los rechazos tipados. Todo rechazo lleva `code` y `detail`.

    Tipado y no un `ValueError` suelto porque la capa de arriba tiene que poder mapear
    cada uno a su respuesta sin leer el mensaje: un rechazo que hay que distinguir por
    el texto se convierte en un `if "no such" in str(e)` a los dos meses.
    """

    code = "SEARCH_CONTRACT"

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class QueryTooLong(SearchContractError):
    code = "QUERY_TOO_LONG"


class TooManyTerms(SearchContractError):
    code = "TOO_MANY_TERMS"


class TermTooLong(SearchContractError):
    code = "TERM_TOO_LONG"


class NoSearchableTerms(SearchContractError):
    code = "NO_SEARCHABLE_TERMS"


class InvalidFilter(SearchContractError):
    code = "INVALID_FILTER"


class InvalidLimit(SearchContractError):
    code = "INVALID_LIMIT"


# Caracteres con significado sintáctico en FTS5. No se escapan uno a uno: se usan como
# SEPARADORES, igual que cualquier otro signo. Así `noexiste:foo` no es «una columna con
# un valor», es un solo término `noexistefoo` — el usuario no puede direccionar columnas
# ni siquiera por accidente, que es el requisito de frontera del contrato §5.
_FTS_SINTAXIS = set('"*:()^-+,{}[]~/\\!?=<>&|;%$#@')


def _es_separador(ch: str) -> bool:
    if ch in _FTS_SINTAXIS:
        return True
    if ch.isspace():
        return True
    # `Cc` de control y `Cf` de formato (incluye los bidi invisibles): separan. Dejarlos
    # dentro de un término los mete en el índice y hace que dos consultas visualmente
    # idénticas no casen.
    return unicodedata.category(ch) in ("Cc", "Cf", "Zs", "Zl", "Zp")


def _terminos(texto: str) -> list[str]:
    fuera: list[str] = []
    actual: list[str] = []
    for ch in texto:
        if _es_separador(ch):
            if actual:
                fuera.append("".join(actual))
                actual = []
        else:
            actual.append(ch)
    if actual:
        fuera.append("".join(actual))
    return fuera


def compile_query(crudo: str) -> tuple[str, list[str]]:
    """Devuelve `(expresión FTS5, términos)`. Nunca trunca; rechaza con tipo.

    NFKC antes de `casefold()` y no al revés: la descomposición canónica puede producir
    letras nuevas cuyo plegado cambie, y hacerlo en el otro orden deja pares que el
    tokenizador del índice sí unifica y el compilador no — dos consultas que el motor
    considera iguales y esta capa considera distintas romperían la firma del cursor.
    """
    if not isinstance(crudo, str):
        raise InvalidFilter("la consulta tiene que ser texto")
    # Cota BARATA primero: rechazar por tamaño no debe costar una normalización Unicode.
    bruto = len(crudo.encode("utf-8"))
    if bruto > MAX_QUERY_BYTES * COTA_CRUDA:
        raise QueryTooLong(
            f"la consulta cruda ocupa {bruto} bytes: por encima de "
            f"{MAX_QUERY_BYTES * COTA_CRUDA} no puede caber ni normalizada, así que la "
            f"rechazo sin normalizarla")
    normal = normalize_text(crudo)
    # Se mide DESPUÉS de normalizar porque es lo que de verdad se va a procesar, y el
    # NFKC puede alargar (una ligadura se expande). Medir antes dejaría pasar entradas
    # que crecen al normalizar.
    n = len(normal.encode("utf-8"))
    if n > MAX_QUERY_BYTES:
        raise QueryTooLong(
            f"la consulta ocupa {n} bytes y el máximo es {MAX_QUERY_BYTES}: "
            f"no la recorto, porque recortarla cambiaría tu pregunta sin decírtelo")
    terminos = _terminos(normal)
    if not terminos:
        raise NoSearchableTerms(
            "la consulta no deja ningún término buscable: todo lo que trae es sintaxis "
            "o separadores")
    if len(terminos) > MAX_TERMS:
        raise TooManyTerms(
            f"{len(terminos)} términos y el máximo es {MAX_TERMS}: no me quedo con los "
            f"primeros, porque eso respondería a otra pregunta")
    for t in terminos:
        b = len(t.encode("utf-8"))
        if b > MAX_TERM_BYTES:
            raise TermTooLong(
                f"el término número {terminos.index(t) + 1} ocupa {b} bytes y el máximo "
                f"es {MAX_TERM_BYTES}: no lo trunco, porque un prefijo encuentra otra "
                f"cosa y no habría forma de que lo supieras")
    # Comillas dobles duplicadas: es el escape de FTS5 dentro de una cadena citada. Sin
    # esto, un término que conservara una comilla cerraría el literal — pero además
    # `"` ya es separador arriba, así que esto es defensa en profundidad para el día que
    # alguien toque `_FTS_SINTAXIS`.
    citados = ['"' + t.replace('"', '""') + '"' for t in terminos]
    return " AND ".join(citados), terminos


# ── Framing de ADR-001, FUERA de la proyección indexada ───────────────────────────
# El proyector del outbox envuelve cada entrada con marcadores que llevan `event_id` y
# `payload_sha`. Esas líneas viven DENTRO de `entries.body`, así que indexarlas mete en el
# índice tokens que no son del texto: buscar `event_id`, `payload_sha` o `llminbox` casaría
# con TODA entrada nativa. Sanear sólo el fragmento no arregla eso — el falso positivo ya
# ocurrió en el `MATCH`; lo que hay que quitar es el token, no su rastro visible.
#
# Se retira toda LÍNEA COMPLETA que sea un marcador, sangrada o no. Una línea con texto
# DETRÁS del marcador no lo es y se conserva — ahí el marcador es contenido citado dentro
# de una frase. La sangría NO salva: el proyector escribe sin sangrar, así que una línea
# sangrada que por lo demás es un marcador entero sigue siendo metadato, y el requisito es
# que el índice no tenga NUNCA esos tokens. Una trama incompleta —`BEGIN` sin `END`— se
# retira igual: sus tokens no son texto ni cuando el proyector se quedó a medias.
# ⚠️ FIN DE LÍNEA: `LF`, `CRLF` **y** `CR` sueltos. Partir por `"\n"` dejaba el `\r` pegado
# al final del marcador, el regex no casaba y `event_id`/`payload_sha` entraban al índice —
# reproducido: `project_body` sobre un cuerpo con `CRLF` conservaba los dos tokens. El
# corpus viene de ficheros escritos por herramientas distintas y en máquinas distintas, así
# que asumir `LF` es asumir el terminador de quien escribió el código.
#
# `[\s]*` al final y no `[ \t]*` por lo mismo: cualquier blanco de cola, `\r` incluido.
_MARCADOR = re.compile(
    r"^[ \t]*<!--[ \t]*LLMINBOX-EVENT-(?:BEGIN|END)\b[^\n>]*-->\s*$")


def strip_framing(texto: str) -> str:
    """Quita las líneas de marcador de ADR-001. Idempotente y agnóstica del salto.

    `splitlines()` y no `split("\n")`: parte por `LF`, `CRLF` y `CR`. Se reensambla con
    `\n` a propósito — el texto que entra al ÍNDICE se normaliza también en esto, así que
    dos entradas iguales escritas con terminadores distintos producen los mismos tokens.
    """
    return "\n".join(l for l in texto.splitlines() if not _MARCADOR.match(l))


def project_body(body) -> str:
    """LA PROYECCIÓN INDEXADA: sin framing y con la MISMA normalización que la consulta.

    Es la única función que decide qué texto entra al índice, y la usan la vista y los
    triggers. Que sea una sola pieza es lo que impide la asimetría: si el índice guardara
    el texto crudo y la consulta se normalizara, `office` no encontraría `oﬃce` y nadie
    vería un error — sólo menos resultados.
    """
    if body is None:
        return ""
    return normalize_text(strip_framing(str(body)))


def normalizer_fingerprint() -> str:
    """Huella del NORMALIZADOR. Un índice construido con otra versión de esta función no
    es comparable con las consultas que compila la actual, y eso tiene que caducar el
    sello igual que lo caduca un cambio de esquema."""
    fuente = (inspect.getsource(normalize_text) + inspect.getsource(strip_framing)
              + inspect.getsource(project_body) + _MARCADOR.pattern
              + CENTINELA_INI + CENTINELA_FIN)
    return hashlib.sha256(fuente.encode("utf-8")).hexdigest()


def canonical_filters(*, lane: str, ledger: str, actor: str | None = None,
                      tipo: str | None = None, desde: str | None = None,
                      hasta: str | None = None,
                      query: str, order: str = ORDER_ARRIVAL_DESC) -> dict:
    """Forma canónica del RECORTE. Es lo que firma el cursor, así que tiene que
    contener TODO lo que cambia la población — incluidos `hasta` y `ausente`.

    `hasta` se olvida por ser el que casi nadie manda: un cursor que no lo ata deja
    paginar de un recorte acotado a otro sin acotar, y la avería es silenciosa — hay
    filas, y son de otra pregunta. `lane` se olvida por parecer redundante con `ledger`,
    y no lo es (ver arriba).
    """
    # LANE Y LEDGER SON DOS COSAS, y atar sólo una deja la otra suelta. El mapa
    # carril→ledger no es inyectivo en el tiempo: si mañana dos carriles apuntan al mismo
    # fichero, un cursor emitido para el carril A seguiría sirviendo al carril B sobre el
    # mismo `ledger` y el cursor no se enteraría. El carril es QUIÉN pregunta; el ledger es
    # DÓNDE se busca. Los dos entran en la firma.
    if not isinstance(lane, str) or not lane:
        raise InvalidFilter("`lane` es obligatorio: identifica al carril que pregunta")
    if not isinstance(ledger, str) or not ledger:
        raise InvalidFilter("`ledger` es obligatorio: es la FRONTERA, no un filtro más")
    # COTAS BARATAS DE LOS FILTROS, antes de compilar la consulta. Van aquí y no dentro de
    # cada uso porque el coste que evitan es el de la ruta ENTERA: sin ellas, un `actor` de
    # un megabyte llega hasta el `?` de SQLite habiendo pasado por la normalización.
    for nombre, valor in (("lane", lane), ("ledger", ledger), ("actor", actor),
                          ("tipo", tipo), ("desde", desde), ("hasta", hasta)):
        if valor is None:
            continue
        if not isinstance(valor, str):
            raise InvalidFilter(f"`{nombre}` tiene que ser texto o None")
        n = len(valor.encode("utf-8"))
        if n > MAX_FILTER_BYTES:
            raise InvalidFilter(
                f"`{nombre}` ocupa {n} bytes y el máximo son {MAX_FILTER_BYTES}")
    if order not in ORDERS:
        raise InvalidFilter(f"orden desconocido: {order!r}; válidos: {sorted(ORDERS)}")
    expr, terminos = compile_query(query)
    return {
        "version": CONTRACT_VERSION,
        "lane": lane,
        "ledger": ledger,
        "actor": actor,
        "tipo": tipo,
        "desde": desde,
        "hasta": hasta,
        "order": order,
        "query": expr,
        "terms": terminos,
    }


def filter_sha256(filtros: dict) -> str:
    """SHA-256 **completo** (64 hex) de los filtros canónicos.

    Completo y no un prefijo: el cursor lo lleva dentro y su trabajo es que un cursor de
    otro recorte no se acepte. Un prefijo corto abarata encontrar dos recortes que
    colisionen, y lo que se gana es longitud de cadena.
    """
    crudo = json.dumps(filtros, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False)
    return hashlib.sha256(crudo.encode("utf-8")).hexdigest()


MAX_LIMIT = 100


def check_limit(n: int, *, maximo: int = MAX_LIMIT) -> int:
    if not isinstance(n, int) or isinstance(n, bool):
        raise InvalidLimit("el límite tiene que ser un entero")
    if n < 1 or n > maximo:
        raise InvalidLimit(f"límite fuera de rango: 1..{maximo}")
    return n
