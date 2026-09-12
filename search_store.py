"""M2 · Almacén de búsqueda: acoplamiento estable, rebuild transaccional y consulta fija.

Sólo stdlib. No conoce FastAPI, no conoce `servicio.py` y no arranca nada por su cuenta.

## Las cuatro decisiones que sostienen el resto

**① `rid` propio, nunca `entries.rowid`.** `qa` midió 2.437 huecos en el `rowid` de
`entries` (101.019 filas entre 1 y 103.456) y la PK real es `(ledger, eid)`, así que el
`rowid` es implícito y **no sobrevive a un reindex**. Un FTS5 `content='entries'` se
acopla por `rowid`: tras reconstruir, el acoplamiento se rompe **en silencio** y la
búsqueda devuelve la fila EQUIVOCADA — no un error. Aquí el acoplamiento va por
`search_documents.rid`, `INTEGER PRIMARY KEY AUTOINCREMENT` con `UNIQUE(ledger, eid)`:
estable, y `AUTOINCREMENT` para que un `rid` liberado **no se reutilice** y una fila
borrada y reinsertada no herede los tokens de otra.

**② El `MATCH` no es la frontera.** Medido por `qa`: `MATCH ledger:"64bis-wiki"` devuelve
filas de `64bis-wiki-archivo` y `64bis-wiki-queue` porque el nombre se TOKENIZA. La
frontera es `= ?` exacta en SQL, y va **fija en la sentencia**, no como parámetro
opcional: no existe forma de construir la consulta sin ella. Y va dos veces —`d.ledger`
y `e.ledger`— porque son dos tablas y sólo una es la autoridad.

**③ La sentencia de búsqueda es UNA CONSTANTE.** Cero interpolación: los filtros
opcionales viajan como parámetros bajo `? IS NULL OR …`. Un compilador de consultas que
concatena texto acaba concatenando el texto del usuario.

**④ Sin `LIKE` de reserva.** Un fallback que se activa cuando el índice no está listo
convierte una indisponibilidad ruidosa en una lentitud silenciosa con otro recall. Si el
índice no está listo, esto lo DICE (`SearchNotReady`) y no sirve nada.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import math
import os
import secrets
import sqlite3
import time

import search_contract as sc
import search_cursor as scur

SEARCH_SCHEMA_V1 = 1
SEARCH_SCHEMA_V = 2
# `schema_v` vive como TEXT porque comparte el almacén clave/valor de `search_state`,
# pero semánticamente es un entero SQLite no negativo.  Acotarlo al rango de
# `PRAGMA user_version` evita que un decimal arbitrariamente grande parezca una versión
# futura legítima.  El parser además exige dígitos ASCII: `int()` acepta numerales
# Unicode, espacios y signos que este código nunca sella.
MAX_SEARCH_SCHEMA_V = 2 ** 31 - 1
SQLITE_MINIMO = (3, 37, 0)          # `integrity-check` con rank=1 (ver `_verificar_motor`)
FUSIBLE_POR_DEFECTO_S = 30.0
FUSIBLE_PASOS_POR_DEFECTO = 10_000
# Techos de tamaño de la RESPUESTA, en bytes. En bytes y no en filas porque el corpus
# tiene una entrada de 11,6 MiB: «40 filas» no acota nada.
#
# ⚠️ TODOS SON «INCLUIDO LO QUE SE AÑADE». La primera versión de esto cortaba el fragmento
# a 2.048 bytes y LUEGO le pegaba la elipsis: el resultado medía 2.051. Un techo que se
# aplica antes de terminar de construir el valor no es un techo, es una intención.
MARCA_RECORTE = "…"
MAX_SNIPPET_BYTES = 2 * 1024
MAX_RESPONSE_BYTES = 256 * 1024

# CAPS POR CAMPO. `head` sale del markdown y NO está acotado en `entries` —el límite de 200
# del modelo `Post` sólo gobierna lo que entra por `POST /append`, que es el 0,05 % de las
# escrituras (medido por `qa`); el resto lo escribe el indexador desde el fichero—. Sin cap
# por campo, UNA fila puede superar sola el techo de la respuesta, y entonces las dos
# salidas posibles son malas: servir de más, o servir cero filas con cursor y dejar al
# llamante en un bucle. Con cap, una fila cabe SIEMPRE y la paginación progresa.
# `", "` entre elementos del array de filas. Se cobra por fila, no por hueco: es una cota
# superior de un solo byte de más por respuesta y evita un `n-1` que se olvida al refactor.
SEPARADOR_FILA_BYTES = 2

MAX_HEAD_BYTES = 2 * 1024
MAX_ACTOR_BYTES = 128
MAX_TIPO_BYTES = 64

# Nombre de la UDF que proyecta el cuerpo. Va en el SQL de la vista y de los triggers, así
# que una conexión que no la registre NO puede ni leer la vista ni escribir en `entries`:
# el fallo es `no such function`, ruidoso e inmediato. Es la forma de que el contrato de
# «toda conexión escritora registra el normalizador» falle CERRADO en vez de indexar crudo.
UDF_PROYECTA = "llminbox_proyecta"

ESTADO_AUSENTE = "absent"
ESTADO_CONSTRUYENDO = "building"
ESTADO_LISTO = "ready"

# Nombres de objeto. En una constante porque la comprobación de presencia (readiness) los
# recorre: una lista escrita a mano en el sitio de la comprobación se desincroniza del
# esquema y entonces «faltan objetos» deja de poder dispararse.
TABLAS = ("search_documents", "search_state", "search_acl")
VISTAS = ("search_view",)
FTS = "search_fts"
TRIGGERS = ("search_ai", "search_ad", "search_au_body", "search_au_key",
            "search_au_arrival", "search_acl_ai", "search_acl_ad",
            "search_acl_au")

# El Journal/Core comparte la misma base, pero hoy NO declara ningún trigger propio
# (censo del repositorio: todos los ``CREATE TRIGGER`` de producción están en este
# módulo). Un trigger es código durable: aunque cuelgue de una tabla aparentemente ajena,
# su cuerpo puede escribir ``search_state``. Por eso los triggers compartidos se admiten
# sólo por DDL exacto en esta allowlist —vacía hoy—; tablas, vistas e índices legacy fuera
# del namespace ``search_*`` sí se toleran, salvo índices que cuelguen de tablas Search.
# Añadir un trigger de Core exige hacerlo explícito aquí y probar su composición.
TRIGGERS_COMPARTIDOS_TOLERADOS: dict[tuple[str, str, str], str] = {}
TABLAS_SEARCH_OWNED = frozenset((*TABLAS, FTS))

# Identidad de una fila de sqlite_master. ``name`` NO es una clave: SQLite mantiene
# namespaces distintos y admite, por ejemplo, una tabla y un trigger homónimos. La
# primera implementación los metía en ``dict[name]`` y el último ocultaba al primero.
ObjectIdentity = tuple[str, str, str, str]  # schema, type, name, tbl_name
_ASCII_MAYUSCULAS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_ASCII_MINUSCULAS = "abcdefghijklmnopqrstuvwxyz"
_SQLITE_IDENT_TRANSLATE = str.maketrans(_ASCII_MAYUSCULAS, _ASCII_MINUSCULAS)


def _identificador_sqlite(nombre: str) -> str:
    """Normaliza como SQLite sus identificadores: sólo ASCII case-insensitive.

    ``str.lower``/``casefold`` abarcan Unicode y producirían falsos positivos para
    nombres que SQLite considera distintos. Esta tabla hace únicamente A-Z → a-z.
    """
    return nombre.translate(_SQLITE_IDENT_TRANSLATE)


_TABLAS_SEARCH_OWNED_SQLITE = frozenset(
    _identificador_sqlite(nombre) for nombre in TABLAS_SEARCH_OWNED)


def _identidad_objeto(esquema: str, fila) -> ObjectIdentity:
    return (esquema, fila["type"], fila["name"], fila["tbl_name"])


def _identidad_sello(identidad: ObjectIdentity) -> str:
    """Clave JSON inyectiva para el sello durable; nombres arbitrarios no colisionan."""
    return json.dumps(identidad, ensure_ascii=False, separators=(",", ":"))


def _etiqueta_objeto(identidad: ObjectIdentity) -> str:
    esquema, tipo, nombre, tabla = identidad
    prefijo = "temp." if esquema == "temp" else ""
    return f"{prefijo}{nombre} ({tipo} sobre {tabla})"


def _temp_afecta_search(fila) -> bool:
    """Qué DDL TEMP preexistente puede cambiar resolución o escrituras de Search."""
    tipo, nombre, tabla = fila["type"], fila["name"], fila["tbl_name"]
    nombre_sqlite = _identificador_sqlite(nombre)
    tabla_sqlite = _identificador_sqlite(tabla)
    return (tipo == "trigger" or nombre_sqlite == "entries"
            or nombre_sqlite.startswith("search_")
            or (tipo == "index" and tabla_sqlite in _TABLAS_SEARCH_OWNED_SQLITE))

SCHEMA = """
-- ACOPLAMIENTO ESTABLE Y PERMANENTE. `rid` es del índice, no de `entries`: el `rowid` de
-- `entries` es implícito (la PK es `(ledger,eid)`), tiene huecos y no sobrevive a un
-- reindex.
--
-- LA FILA DE MAPEO NO SE BORRA NUNCA: al desaparecer la entrada se purgan sus TOKENS y la
-- fila queda como LÁPIDA. Así, una entrada que se borra y se reinserta —lo que hace el
-- reindexador del markdown cada vez que un fichero se reordena— recupera SU MISMO `rid`,
-- y ni el acoplamiento ni los cursores emitidos se mueven. Borrar el mapeo daría un `rid`
-- nuevo a la misma entrada de siempre.
--
-- `AUTOINCREMENT` sigue siendo necesario para lo otro: que un `rid` de una clave que ya no
-- vuelve no se le entregue a una clave DISTINTA.
CREATE TABLE IF NOT EXISTS search_documents (
  rid    INTEGER PRIMARY KEY AUTOINCREMENT,
  ledger TEXT NOT NULL,
  eid    TEXT NOT NULL,
  UNIQUE (ledger, eid));
CREATE INDEX IF NOT EXISTS i_sd_ledger ON search_documents(ledger);

-- LA VISTA ES EL CONTENIDO EXTERNO. Se une por CLAVE (`ledger`,`eid`), que es la PK real
-- de `entries`, y expone `rid` como `rowid`: así el índice se acopla a una identidad
-- estable y el texto sigue viviendo una sola vez en `entries` (313 MiB de cuerpo que
-- duplicar en un índice autónomo costaría más que la base entera).
CREATE VIEW IF NOT EXISTS search_view AS
  SELECT d.rid AS rowid, llminbox_proyecta(e.body) AS body
    FROM search_documents d
    JOIN entries e ON e.ledger = d.ledger AND e.eid = d.eid;

-- ESTADO PROPIO, no `meta`. El índice de búsqueda es reconstruible y su ciclo de vida no
-- es el de la base: mezclarlo con el sello de esquema del servicio haría que un rebuild
-- de búsqueda tocara la fila que gobierna las migraciones.
CREATE TABLE IF NOT EXISTS search_state (k TEXT PRIMARY KEY, v TEXT NOT NULL);

-- ACL DURABLE, y la autorización se aplica DENTRO DEL SQL (ver `SQL_BUSQUEDA`).
--
-- ⚠️ P0 REPRODUCIDO: `lane` era obligatorio, entraba en `filter_sha256` y ataba la firma
-- del cursor… y NO APARECÍA EN EL SQL. Medido: `lane="CARRIL-QUE-NO-EXISTE-NI-AUTORIZADO"`
-- sobre el mismo `ledger` devolvía EXACTAMENTE las mismas filas que el carril legítimo.
--
-- Una comprobación en Python delante de la consulta cierra el agujero de hoy y no el de
-- mañana: cualquier camino nuevo a `SQL_BUSQUEDA` —un endpoint, un job, un `EXPLAIN` de
-- diagnóstico— vuelve a servir sin autorizar, y nada falla. Por eso la frontera vive en
-- la sentencia: si el `EXISTS` no casa, NO HAY FILAS, venga la llamada por donde venga.
-- La comprobación de Python se conserva ADEMÁS, para dar un error tipado en vez de un
-- resultado vacío que se lee como «no hay nada».
CREATE TABLE IF NOT EXISTS search_acl (
  lane   TEXT NOT NULL,
  ledger TEXT NOT NULL,
  PRIMARY KEY (lane, ledger));
"""

# `body` es la ÚNICA columna indexada, y `head` NO va como columna aparte: `qa` midió que
# el cuerpo contiene la cabecera literal, así que indexar las dos duplicaría cada titular
# en el índice. Que ese supuesto siga siendo cierto lo comprueba el rebuild, globalmente,
# antes de sellar — no se da por bueno porque lo diga este comentario.
SQL_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
  body,
  content='search_view',
  content_rowid='rowid',
  tokenize="unicode61 remove_diacritics 2");
"""

# Los triggers mantienen el índice al día. El de UPDATE está PARTIDO en dos por el `WHEN`:
# un update que NO toca la clave tiene que CONSERVAR el `rid` (si lo recreara, cada
# reindexación del markdown movería el acoplamiento de todas las entradas tocadas), y uno
# que SÍ la toca es otra fila y merece `rid` nuevo.
SQL_TRIGGER_AU_KEY_V1 = """
CREATE TRIGGER IF NOT EXISTS search_au_key AFTER UPDATE ON entries
WHEN new.ledger <> old.ledger OR new.eid <> old.eid BEGIN
  INSERT INTO search_fts(search_fts, rowid, body)
    SELECT 'delete', rid, llminbox_proyecta(old.body) FROM search_documents
     WHERE ledger = old.ledger AND eid = old.eid;
  INSERT OR IGNORE INTO search_documents(ledger, eid) VALUES (new.ledger, new.eid);
  INSERT INTO search_fts(rowid, body)
    SELECT rid, llminbox_proyecta(new.body) FROM search_documents
     WHERE ledger = new.ledger AND eid = new.eid;
END;
"""

SQL_TRIGGER_AU_KEY_V2 = """
CREATE TRIGGER IF NOT EXISTS search_au_key AFTER UPDATE ON entries
WHEN new.ledger IS NOT old.ledger OR new.eid IS NOT old.eid BEGIN
  INSERT INTO search_fts(search_fts, rowid, body)
    SELECT 'delete', rid, llminbox_proyecta(old.body) FROM search_documents
     WHERE ledger = old.ledger AND eid = old.eid;
  INSERT OR IGNORE INTO search_documents(ledger, eid) VALUES (new.ledger, new.eid);
  INSERT INTO search_fts(rowid, body)
    SELECT rid, llminbox_proyecta(new.body) FROM search_documents
     WHERE ledger = new.ledger AND eid = new.eid;
  UPDATE search_state SET v = 'building' WHERE k = 'state';
  UPDATE search_state SET v = lower(hex(randomblob(16))) WHERE k = 'generation';
END;
"""

SQL_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS search_ai AFTER INSERT ON entries BEGIN
  INSERT OR IGNORE INTO search_documents(ledger, eid) VALUES (new.ledger, new.eid);
  INSERT INTO search_fts(rowid, body)
    SELECT rid, llminbox_proyecta(new.body) FROM search_documents
     WHERE ledger = new.ledger AND eid = new.eid;
END;

-- Purga los TOKENS y deja la LÁPIDA. Sin `DELETE FROM search_documents`: ese borrado es
-- justo lo que le cambiaría el `rid` a una entrada que vuelva.
CREATE TRIGGER IF NOT EXISTS search_ad AFTER DELETE ON entries BEGIN
  INSERT INTO search_fts(search_fts, rowid, body)
    SELECT 'delete', rid, llminbox_proyecta(old.body) FROM search_documents
     WHERE ledger = old.ledger AND eid = old.eid;
END;

CREATE TRIGGER IF NOT EXISTS search_au_body AFTER UPDATE ON entries
WHEN new.ledger = old.ledger AND new.eid = old.eid BEGIN
  INSERT INTO search_fts(search_fts, rowid, body)
    SELECT 'delete', rid, llminbox_proyecta(old.body) FROM search_documents
     WHERE ledger = old.ledger AND eid = old.eid;
  INSERT INTO search_fts(rowid, body)
    SELECT rid, llminbox_proyecta(new.body) FROM search_documents
     WHERE ledger = new.ledger AND eid = new.eid;
END;

-- LA CLAVE DE ORDEN ES INMUTABLE MIENTRAS HAYA CURSORES VIVOS, y esto lo IMPONE.
--
-- ⚠️ P0 REPRODUCIDO: con `arrival` mutable, mover una entrada DESPUES de servir la página 1
-- la sacaba del recorrido sin que nada fallara. Medido sobre `e0..e3` con `limit=2`:
-- página 1 `[e3, e2]`, se mueve `arrival` de `e1` de 2 a 9, página 2 devuelve `[e0]` y
-- `e1` NO APARECE NUNCA. `generation` no cambiaba, `readiness` seguía `true`, y el
-- llamante recibía un recorrido con un agujero que se lee como «ya no hay más».
--
-- El keyset `(arrival, eid)` sólo particiona si la clave no se mueve bajo los pies del
-- que pagina. Como `entries` es de otro (el indexador la escribe), no se puede PROHIBIR
-- el `UPDATE` sin romperle el trabajo: lo que se hace es INVALIDAR, y hacerlo DURABLE —
-- en la base, no en memoria— para que sobreviva a una caída:
--   · `state` -> `building`: `readiness` pasa a falso y `search` responde `SearchNotReady`.
--   · `generation` rotada: todo cursor emitido antes falla con un error TIPADO en vez de
--     seguir paginando sobre un orden que ya no existe.
-- Las dos son fail-closed. La alternativa —confiar en que nadie mueve `arrival`— es
-- exactamente el supuesto que el P0 falsó.
--
-- `IS NOT` y no `<>`: con `NULL` de un lado, `<>` da `NULL` y el `WHEN` no dispara. Poner
-- posición a una entrada que no la tenía TAMBIÉN cambia el orden —la mete en medio del
-- recorrido— así que también invalida.
--
-- `UPDATE … WHERE k=…` y no un `upsert`: si la fila no existe, es que NUNCA se selló nada,
-- y entonces no hay cursores ni estado que invalidar. Un `upsert` CREABA una generación en
-- un índice que jamás se construyó, y a partir de ahí «generación presente» dejaba de
-- significar «hubo un rebuild».
-- TOCAR LA ACL INVALIDA, igual que mover la clave de orden. Un cursor se emitió bajo una
-- autorización concreta; si la autorización cambia, seguir paginando con él serviría filas
-- que la ACL de AHORA no permite (o dejaría de servir las que sí, en silencio). Los tres
-- verbos, no sólo el `INSERT`: una política se aplica ENUMERANDO sus casos, y el que se
-- olvida siempre es el `DELETE`.
--
-- ⚠️ SÓLO LA GENERACIÓN, no el estado. Cambiar la ACL **no** ensucia el índice: el `EXISTS`
-- aplica la política nueva en la consulta siguiente, sin reindexar un solo token. Lo único
-- que queda inválido son los CURSORES ya emitidos, que se firmaron bajo otra autorización
-- — y para eso basta rotar la generación, que los hace fallar con un error tipado. Poner
-- `building` aquí tiraba el índice entero por un cambio de política: una invalidación
-- desproporcionada es una avería, no una precaución.
CREATE TRIGGER IF NOT EXISTS search_acl_ai AFTER INSERT ON search_acl BEGIN
  UPDATE search_state SET v = lower(hex(randomblob(16))) WHERE k = 'generation';
END;

CREATE TRIGGER IF NOT EXISTS search_acl_ad AFTER DELETE ON search_acl BEGIN
  UPDATE search_state SET v = lower(hex(randomblob(16))) WHERE k = 'generation';
END;

CREATE TRIGGER IF NOT EXISTS search_acl_au AFTER UPDATE ON search_acl BEGIN
  UPDATE search_state SET v = lower(hex(randomblob(16))) WHERE k = 'generation';
END;

CREATE TRIGGER IF NOT EXISTS search_au_arrival AFTER UPDATE OF arrival ON entries
WHEN new.arrival IS NOT old.arrival BEGIN
  UPDATE search_state SET v = 'building' WHERE k = 'state';
  UPDATE search_state SET v = lower(hex(randomblob(16))) WHERE k = 'generation';
END;

-- `eid` es el SEGUNDO componente del keyset y `ledger` decide su población. Cambiar
-- cualquiera tiene el mismo contrato que cambiar `arrival`: se mantiene el FTS para que
-- la transacción escritora sea coherente, pero se marca `building` y se rota la
-- generación. `IS NOT` hace que `SET eid=eid` / `SET ledger=ledger` sean no-op reales.
""" + SQL_TRIGGER_AU_KEY_V2

# LA CONSULTA, ENTERA Y CONSTANTE. Lo que la hace segura no es escapar bien: es que no
# hay nada que escapar. `d.ledger` y `e.ledger` van FIJOS — no como filtro opcional —
# porque el `MATCH` es selectividad y jamás frontera (§5 del contrato, medido).
SQL_BUSQUEDA = """
SELECT e.ledger, e.eid, e.arrival, e.ts, e.actor, e.tipo, e.head,
       snippet(search_fts, 0, char(57344), char(57345), '…', 12) AS fragmento
  FROM search_fts
  JOIN search_documents d ON d.rid = search_fts.rowid
  JOIN entries e ON e.ledger = d.ledger AND e.eid = d.eid
 WHERE search_fts MATCH ?
   AND EXISTS (SELECT 1 FROM search_acl a
                WHERE a.lane = ? AND a.ledger = e.ledger)
   AND d.ledger = ?
   AND e.ledger = ?
   AND e.arrival IS NOT NULL
   AND e.ausente IS NULL
   AND (? IS NULL OR e.actor = ?)
   AND (? IS NULL OR e.tipo  = ?)
   AND (? IS NULL OR e.ts   >= ?)
   AND (? IS NULL OR e.ts    < ?)
   AND (? = 0 OR (e.arrival, e.eid) < (?, ?))
 ORDER BY e.arrival DESC, e.eid DESC
 LIMIT ?
"""


class SearchError(Exception):
    code = "SEARCH"

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class SearchNotReady(SearchError):
    """Equivalente a un 503: el índice no está en condiciones de responder."""

    code = "SEARCH_NOT_READY"


class SearchStale(SearchNotReady):
    """El índice se construyó con un esquema ANTERIOR al de este código."""

    code = "SEARCH_STALE"


class SearchSchemaTooNew(SearchNotReady):
    code = "SEARCH_SCHEMA_TOO_NEW"


class SearchSchemaCorrupt(SearchNotReady):
    """El sello `schema_v` existe, pero no fue emitido por este formato.

    Es distinto de ausencia, stale y too-new: reparar una corrupción sobrescribiéndola
    durante un rebuild destruiría la evidencia que el operador necesita diagnosticar.
    """

    code = "SEARCH_SCHEMA_CORRUPT"


class SearchStateCorrupt(SearchNotReady):
    """Un sello crítico de ``search_state`` no conserva su forma durable."""

    code = "SEARCH_STATE_CORRUPT"


class SearchMigrationRejected(SearchNotReady):
    """La migración explícita no reconoce exactamente el origen que recibió."""

    code = "SEARCH_MIGRATION_REJECTED"


class RebuildFailed(SearchError):
    code = "REBUILD_FAILED"


class EngineUnsupported(SearchError):
    code = "ENGINE_UNSUPPORTED"


class StatementTimeout(SearchError):
    code = "STATEMENT_TIMEOUT"


@dataclasses.dataclass(frozen=True)
class BoundSearchScope:
    """ALCANCE ATADO. Lo emite el SERVIDOR desde un sujeto autenticado; no lo rellena
    quien llama.

    ⚠️ Un `lane` que viaja como `str` en la llamada es un campo que cualquier capa de
    arriba puede poner a lo que quiera —y el P0 medido fue exactamente eso: un carril
    inventado sobre el mismo ledger devolvía las mismas filas—. Un tipo no arregla eso por
    sí solo, pero sí hace que el alcance tenga que CONSTRUIRSE en un sitio: el que tiene
    delante la credencial. Un `str` no tiene ese sitio; aparece de la nada en cualquier
    línea.

    · `lane` es el carril del SUJETO, no el que pidió el cliente.
    · `ledgers` es lo que ese sujeto puede leer, resuelto por el servidor.
    · `principal_id` es QUIÉN, y viene EXPLÍCITO del registro autenticado.
    · `role` es QUÉ PUEDE HACER, y es otra cosa.

    ⚠️ LOS TRES SON DOMINIOS DISTINTOS Y NO SE DERIVAN UNO DE OTRO. `principal_id` no se
    fabrica a partir del rol ni del carril: dos agentes pueden compartir rol (`qa`) y
    carril (`64bis`) y no ser el mismo sujeto, así que un «principal» derivado de ellos
    hace indistinguibles a dos identidades — y entonces el registro de un rechazo apunta a
    un conjunto, no a alguien. Si el registro autenticado no trae `principal_id`, esto
    falla cerrado; no lo inventa.

    Congelado (`frozen=True`) a propósito: un alcance que se puede mutar después de
    emitirse no es un alcance, es una sugerencia.
    """

    lane: str
    ledgers: frozenset
    principal_id: str
    role: str | None = None

    def __post_init__(self):
        if not isinstance(self.lane, str) or not self.lane:
            raise ValueError("un alcance sin carril no autoriza nada")
        if isinstance(self.ledgers, (str, bytes)):
            raise ValueError(
                "`ledgers` tiene que ser una colección, no una cadena: una cadena se "
                "expandiría a sus LETRAS y autorizaría cualquier cosa")
        leds = frozenset(self.ledgers)
        if not leds or any(not isinstance(x, str) or not x for x in leds):
            raise ValueError(f"ledgers inválidos en el alcance: {self.ledgers!r}")
        object.__setattr__(self, "ledgers", leds)
        if not isinstance(self.principal_id, str) or not self.principal_id:
            raise ValueError(
                "un alcance sin `principal_id` no se puede atribuir, y NO se deriva del "
                "rol ni del carril: dos sujetos pueden compartir los dos")
        if self.role is not None and (not isinstance(self.role, str) or not self.role):
            raise ValueError("`role` tiene que ser texto no vacío o None")

    def permite(self, ledger: str) -> bool:
        return ledger in self.ledgers


class LaneNotAuthorized(SearchError):
    """El carril que pregunta no está autorizado a leer ese ledger.

    ⚠️ P0 REPRODUCIDO: `lane` era obligatorio en el contrato, entraba en `filter_sha256` y
    ataba la firma del cursor… y NO APARECÍA EN EL SQL. Medido: `lane="CARRIL-QUE-NO-
    EXISTE-NI-AUTORIZADO"` sobre el mismo `ledger` devolvía EXACTAMENTE las mismas filas
    que el carril legítimo. Atar el cursor no autoriza nada: impide reusar un cursor con
    otros filtros, que es otra cosa.

    La documentación decía que la regla la impondría «la capa de arriba». Una autorización
    que vive en un comentario no es una autorización: mientras esa capa no exista, este
    módulo NIEGA.
    """

    code = "LANE_NOT_AUTHORIZED"


def scope_desde_credencial(credencial, *, credenciales, carril_ledger,
                           token_compartido=None) -> BoundSearchScope:
    """Deriva el alcance del SUJETO AUTENTICADO. Todos los caminos fallan CERRADOS.

    Vive aquí y no en el servicio para poder falsarla sin levantar el servicio entero, y
    recibe los dos mapas como argumentos en vez de importarlos: así el test declara el
    censo que prueba en vez de heredar el del despliegue.

    Los cuatro «no» son los que importan y cada uno tuvo su motivo:

    · **Sin mapa de credenciales, NO.** Un servicio que no puede afirmar quién llama no
      puede acotar a nadie; caer a lectura global «mientras infra emite» es exactamente
      cómo un permiso temporal se vuelve permanente.
    · **El token COMPARTIDO no identifica un carril.** Lo tiene todo el mundo, así que
      aceptarlo daría a cualquiera el alcance del primero que resolviera.
    · **Credencial desconocida, NO** — y comparada en tiempo constante contra cada clave,
      no con `dict.get`: el tiempo de un fallo de diccionario depende del prefijo, y eso
      es un oráculo barato para ir adivinando.
    · **Carril que no resuelve a ledger, NO.** Tragárselo devolvería el corpus entero con
      cara de acotado.
    """
    if not credenciales:
        raise LaneNotAuthorized(
            "no hay mapa de credenciales: este servicio no puede afirmar quién llama, "
            "así que no acota a nadie. No caigo a lectura global")
    if not isinstance(credencial, str) or not credencial:
        raise LaneNotAuthorized("falta la credencial del sujeto")
    if token_compartido and hmac.compare_digest(credencial, token_compartido):
        raise LaneNotAuthorized(
            "el token COMPARTIDO no identifica un carril: lo tiene todo el mundo, así que "
            "con él no hay sujeto al que acotar")
    entrada = None
    for cred, v in credenciales.items():
        if hmac.compare_digest(credencial, cred):
            entrada = v
    if entrada is None:
        raise LaneNotAuthorized("credencial desconocida: no hay sujeto")
    carril = (entrada or {}).get("carril")
    if not isinstance(carril, str) or not carril:
        raise LaneNotAuthorized("la credencial no declara carril: no hay alcance que dar")
    # `principal_id` EXPLÍCITO. No se deriva del rol ni del carril: si el registro no lo
    # trae, este servicio no sabe QUIÉN llama —sólo QUÉ es— y eso no basta para atribuir
    # un rechazo. La migración del mapa legacy es de M1; aquí no se inventa.
    principal_id = (entrada or {}).get("principal_id")
    if not isinstance(principal_id, str) or not principal_id:
        raise LaneNotAuthorized(
            "la credencial no declara `principal_id`: el rol y el carril pueden ser los "
            "mismos para varios sujetos, así que no los uso para inventarlo")
    ledger = (carril_ledger or {}).get(carril)
    if not isinstance(ledger, str) or not ledger:
        raise LaneNotAuthorized(
            f"el carril {carril!r} no resuelve a ningún ledger de este servicio")
    rol = (entrada or {}).get("rol")
    return BoundSearchScope(lane=carril, ledgers=frozenset({ledger}),
                            principal_id=principal_id,
                            role=rol if isinstance(rol, str) and rol else None)


class ConnectionContractViolation(SearchError):
    """La conexión no cumple el contrato que este módulo necesita."""

    code = "CONNECTION_CONTRACT"


_DDL_TEMP_DENEGADO = frozenset({
    sqlite3.SQLITE_CREATE_TEMP_INDEX,
    sqlite3.SQLITE_CREATE_TEMP_TABLE,
    sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
    sqlite3.SQLITE_CREATE_TEMP_VIEW,
    sqlite3.SQLITE_DROP_TEMP_INDEX,
    sqlite3.SQLITE_DROP_TEMP_TABLE,
    sqlite3.SQLITE_DROP_TEMP_TRIGGER,
    sqlite3.SQLITE_DROP_TEMP_VIEW,
})
_ESQUEMA_ADJUNTO_DENEGADO = frozenset({sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH})


def guard_search_connection(con: sqlite3.Connection) -> None:
    """Cierra la superficie TEMP de UNA conexión operacional.

    ``sqlite_temp_master`` pertenece a la conexión, no al fichero. Por tanto esta función
    no promete observar TEMP DDL de otras conexiones: el contrato de integración es que
    **toda conexión que pueda escribir ``entries`` o los objetos Search pase por este
    guard antes de ejecutar SQL operacional**. ``servicio.db``/``_registra_udf_busqueda``
    deben cablearlo junto al registro de la UDF.

    Primero rechaza objetos TEMP ya instalados que puedan sombrear ``entries``/Search o
    ejecutar código durable. Después instala el authorizer: desde ese instante niega
    CREATE/DROP de cualquier objeto TEMP y ATTACH/DETACH. Negar todo el DDL TEMP futuro,
    no sólo nombres conocidos, evita una ventana de clasificación entre crear y usar.

    ``set_authorizer`` es la única ranura que ofrece sqlite3. Ningún consumidor puede
    sustituirlo después sin romper este contrato; esa dependencia también debe quedar
    falsada en la composición del servicio.
    """
    anterior = con.row_factory
    try:
        con.row_factory = sqlite3.Row
        peligrosos = [
            _identidad_objeto("temp", fila)
            for fila in con.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_temp_master"
                " WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'")
            if _temp_afecta_search(fila)
        ]
    except sqlite3.Error as e:
        raise ConnectionContractViolation(
            f"no puedo inventariar el esquema TEMP de la conexión ({e})") from e
    finally:
        con.row_factory = anterior
    if peligrosos:
        detalle = ", ".join(_etiqueta_objeto(i) for i in sorted(peligrosos))
        raise ConnectionContractViolation(
            "la conexión ya trae DDL TEMP que puede afectar Search: " + detalle)

    def autoriza(accion, _arg1, _arg2, _base, _origen):
        if accion in _DDL_TEMP_DENEGADO or accion in _ESQUEMA_ADJUNTO_DENEGADO:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    con.set_authorizer(autoriza)


class ResponseTooLarge(SearchError):
    """Ni una sola fila cabe en el techo de la respuesta.

    No debería poder ocurrir: los caps por campo acotan la fila muy por debajo. Existe como
    guarda FAIL-CLOSED del día que alguien suba un cap o añada una columna. La alternativa
    —servir cero filas con cursor y `hay_mas`— deja al llamante en un bucle infinito, así
    que aquí se prefiere un rechazo tipado que se ve.
    """

    code = "RESPONSE_TOO_LARGE"


def _nueva_generacion() -> str:
    """128 bits aleatorios. NO un contador.

    Un contador deja adivinar la generación siguiente, y con ella fabricar un cursor que
    sobrevive al rebuild que debía caducarlo — justo lo contrario de para lo que existe.
    """
    return secrets.token_hex(16)


class SearchStore:
    """Acceso al índice. Una instancia por conexión; la conexión la pone el llamante."""

    @staticmethod
    def register_udf(con: sqlite3.Connection) -> None:
        """Registra `llminbox_proyecta` Y NADA MÁS. Segura en CUALQUIER conexión.

        Obligatoria en TODA conexión que escriba en `entries` o lea `search_view`: sin
        ella, los triggers y la vista fallan con `no such function: llminbox_proyecta` —
        ruidoso e inmediato. Es deliberado: la alternativa a fallar cerrado sería que una
        conexión sin normalizador indexara el cuerpo CRUDO, y entonces el índice tendría a
        la vez texto normalizado y sin normalizar sin que nadie viera un error. Un índice
        medio normalizado responde menos y parece sano.

        ⚠️ **NO toca `isolation_level` y no exige nada de la transacción.** Antes esto
        estaba fundido con la preparación de la conexión de búsqueda, y enchufarlo a la
        conexión del servicio habría cambiado la ATOMICIDAD del código legacy: asignar
        `isolation_level` COMITEA la transacción abierta del llamante. Medido:

            con.execute("INSERT INTO t VALUES(1)")   -> in_transaction = True
            con.isolation_level = None               -> in_transaction = False
            otra_conexion.count(t)                   -> 1   (ya es visible)

        O sea: registrar el normalizador publicaría trabajo a medias que su dueño no había
        decidido publicar, y sin un solo error. Por eso son DOS funciones: ésta se puede
        llamar en cualquier sitio, y la de abajo exige una conexión dedicada.

        Esta función tampoco instala el authorizer: para una conexión operacional el
        integrador debe llamar además a ``guard_search_connection``. Mantener las dos
        operaciones nombradas evita esconder una política de DDL bajo el nombre
        ``register_udf``; la fábrica del servicio es quien tiene que acreditar ambas.
        """
        con.create_function(UDF_PROYECTA, 1, sc.project_body, deterministic=True)

    @staticmethod
    def prepare_search_connection(con: sqlite3.Connection) -> None:
        """Prepara una conexión DEDICADA a buscar: UDF + control explícito de transacción.

        Exige autocommit porque `search` abre su propia `BEGIN DEFERRED` para leer
        readiness, generación y filas de la MISMA instantánea; con el `isolation_level`
        por defecto, sqlite3 abre una transacción implícita antes de cada escritura y la
        deja viva, y ese `BEGIN` choca con «cannot start a transaction within a
        transaction».

        ⚠️ EL ORDEN NO ES ESTÉTICO: se comprueba ANTES de asignar `isolation_level`, por lo
        medido arriba. Si el llamante tiene trabajo a medias, este módulo NO le toca la
        conexión: se niega.
        """
        if con.in_transaction:
            raise ConnectionContractViolation(
                "esta conexión tiene una transacción abierta: no la preparo para buscar, "
                "porque fijar `isolation_level` COMITEARÍA tu trabajo a medias. Cierra la "
                "transacción (COMMIT/ROLLBACK) o dame una conexión dedicada")
        guard_search_connection(con)
        SearchStore.register_udf(con)
        con.isolation_level = None

    def __init__(self, con: sqlite3.Connection, *, cursor_key: bytes,
                 acl: dict | None = None,
                 fusible_s: float = FUSIBLE_POR_DEFECTO_S,
                 fusible_pasos: int = FUSIBLE_PASOS_POR_DEFECTO,
                 sensor=None,
                 clock=time.monotonic):
        self.con = con
        self.con.row_factory = sqlite3.Row
        # La clave se valida AL CONSTRUIR y no al firmar el primer cursor: una clave
        # corta descubierta en la primera búsqueda ya ha dejado pasar el arranque.
        self.cursor_key = scur._check_key(cursor_key)
        # ACL DEL CARRIL. `None` significa **NIEGA TODO**, no «permite todo»: el valor por
        # defecto de una autorización tiene que ser el que no autoriza. Una instancia sin
        # ACL puede construirse, reconstruir el índice y responder `readiness` — lo único
        # que no puede es SERVIR.
        self.acl = self._canon_acl(acl)
        self.prepare_search_connection(con)
        # FUSIBLE VALIDADO AL CONSTRUIR. `float("inf")`, `nan`, `0`, negativos y `True`
        # (que es un `int` y colaría como número de pasos) desactivan la guarda de formas
        # distintas y ninguna da error: un fusible mal configurado no se nota hasta que
        # hace falta, y entonces ya no está.
        if isinstance(fusible_s, bool) or not isinstance(fusible_s, (int, float)):
            raise ValueError("el fusible tiene que ser un número de segundos")
        fusible_s = float(fusible_s)
        if not math.isfinite(fusible_s) or fusible_s < 0:
            raise ValueError(f"segundos de fusible inválidos: {fusible_s!r}")
        if isinstance(fusible_pasos, bool) or not isinstance(fusible_pasos, int):
            raise ValueError("los pasos del fusible tienen que ser un entero")
        if fusible_pasos < 1:
            raise ValueError(f"pasos de fusible inválidos: {fusible_pasos!r}")
        self.fusible_s = float(fusible_s)
        # Cada cuantas instrucciones de la VM se mira el reloj. Es PARAMETRO y no una
        # constante enterrada: con el valor de produccion el manejador no llega a correr
        # sobre un corpus pequeno, asi que un fusible inajustable seria un fusible que
        # nadie puede ver dispararse -- y una guarda que no se puede ejercitar no esta
        # probada, esta declarada.
        self.fusible_pasos = int(fusible_pasos)
        self._clock = clock
        self._sensor = sensor

    # ── fusible ──────────────────────────────────────────────────────────────────
    def _fusible(self):
        """Corta la sentencia por TIEMPO, no por filas.

        `busy_timeout` no cubre esto: cubre esperar un cerrojo, no una consulta que sí
        está corriendo. Sin fusible, un término que obliga a barrido completo sobre un
        carril de 76 MiB cuelga el proceso entero y quien llama sólo ve que no vuelve.
        """
        limite = self._clock() + self.fusible_s
        estado = {"disparado": False}

        def handler():
            if self._clock() > limite:
                estado["disparado"] = True
                return 1                      # != 0 aborta la sentencia
            return 0

        return handler, estado

    def _ejecutar(self, sql: str, params=()):
        handler, estado = self._fusible()
        self.con.set_progress_handler(handler, self.fusible_pasos)
        try:
            return self.con.execute(sql, params).fetchall()
        except sqlite3.OperationalError as e:
            if estado["disparado"]:
                raise StatementTimeout(
                    f"la consulta pasó de {self.fusible_s:g}s y la corté: no la dejo "
                    f"colgada, y digo que la corté en vez de devolver una página corta"
                ) from e
            raise
        finally:
            self.con.set_progress_handler(None, 0)

    # ── estado ───────────────────────────────────────────────────────────────────
    @staticmethod
    def _search_state_ausente(e: sqlite3.OperationalError) -> bool:
        return str(e) == "no such table: search_state"

    def _raw_state(self, k: str) -> tuple[str, bytes] | None:
        """Lee un sello sin pedirle al driver que decodifique TEXT corrupto.

        SQLite permite almacenar BLOB en una columna TEXT y también construir TEXT que no
        sea UTF-8. Seleccionar ``v`` directamente hace que sqlite3 lance antes de que podamos
        clasificarlo; ``typeof`` + ``CAST AS BLOB`` conserva storage class y bytes exactos.
        Sólo la ausencia real de la tabla equivale a «todavía no existe el índice».
        """
        try:
            filas = self.con.execute(
                "SELECT typeof(v) AS storage, CAST(v AS BLOB) AS raw"
                " FROM search_state WHERE k=? LIMIT 2", (k,)).fetchall()
        except sqlite3.OperationalError as e:
            if self._search_state_ausente(e):
                return None
            clase = SearchSchemaCorrupt if k == "schema_v" else SearchStateCorrupt
            raise clase(f"{k} corrupto: no puedo leer search_state ({e})") from e
        if not filas:
            return None
        clase = SearchSchemaCorrupt if k == "schema_v" else SearchStateCorrupt
        if len(filas) != 1:
            raise clase(f"{k} corrupto: hay más de una fila durable para la misma clave")
        storage, raw = filas[0]["storage"], filas[0]["raw"]
        # ``CAST(NULL AS BLOB)`` devuelve None. Clasificar ANTES de ``bytes`` evita que
        # una corrupción durable se disfrace de TypeError de programación y tumbe Core.
        if storage != "text":
            raise clase(
                f"{k} corrupto: storage class {storage!r}, se esperaba 'text'")
        if not isinstance(raw, (bytes, bytearray, memoryview)):
            raise clase(f"{k} corrupto: representación durable no binaria")
        return storage, bytes(raw)

    def _get(self, k: str) -> str | None:
        """Texto UTF-8 durable; storage o bytes distintos son corrupción tipada."""
        bruto = self._raw_state(k)
        if bruto is None:
            return None
        _storage, raw = bruto  # `_raw_state` ya clasificó el storage de forma tipada.
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise SearchStateCorrupt(f"{k} corrupto: TEXT no es UTF-8 válido") from e

    def _set(self, k: str, v: str) -> None:
        self.con.execute("INSERT OR REPLACE INTO search_state(k,v) VALUES(?,?)", (k, v))

    def state(self) -> str:
        return self._get("state") or ESTADO_AUSENTE

    def generation(self) -> str | None:
        return self._get("generation")

    @staticmethod
    def _generation_problem(gen: str | None) -> str | None:
        if gen is None:
            return "sin generación sellada"
        if len(gen) != scur.GENERATION_HEX or any(
                c not in "0123456789abcdef" for c in gen):
            return (f"generación mal formada ({len(gen)} car., se esperan "
                    f"{scur.GENERATION_HEX} hex)")
        return None

    def schema_v(self) -> int | None:
        bruto = self._raw_state("schema_v")
        if bruto is None:
            return None
        _storage, raw = bruto
        # La clase de almacenamiento también forma parte del contrato. Un BLOB que por
        # casualidad contenga b"2" NO es un sello TEXT emitido por rebuild/migrate.
        try:
            crudo = raw.decode("ascii")
        except UnicodeDecodeError as e:
            raise SearchSchemaCorrupt("schema_v corrupto: TEXT no es ASCII") from e
        if (not crudo or not crudo.isdecimal()
                or (len(crudo) > 1 and crudo[0] == "0")):
            raise SearchSchemaCorrupt(
                "schema_v corrupto: se esperaba decimal ASCII canónico no negativo")
        # Cota LEXICAL antes de int(): Python rechaza enteros de miles de dígitos con un
        # ValueError propio y el sensor dejaría de ser total; además se evita parsear una
        # entrada arbitrariamente grande. Igual longitud se decide lexicográficamente.
        maximo = str(MAX_SEARCH_SCHEMA_V)
        if len(crudo) > len(maximo) or (len(crudo) == len(maximo) and crudo > maximo):
            raise SearchSchemaCorrupt(
                f"schema_v corrupto: queda fuera de 0..{MAX_SEARCH_SCHEMA_V}")
        return int(crudo)

    # ── esquema ──────────────────────────────────────────────────────────────────
    def ensure_schema(self) -> None:
        """Crea los objetos. NO construye el índice y NO toca la generación.

        Separado del rebuild a propósito: crear tablas vacías es barato y puede correr al
        abrir; poblar 313 MiB de cuerpo es una decisión de operación. Fundirlos metería
        un trabajo de minutos en el arranque del servicio, que es exactamente lo que el
        encargo prohíbe («rebuild explícito, no lifespan»).
        """
        self.con.executescript(SCHEMA)
        self.con.execute(SQL_FTS.strip().rstrip(";"))
        self.con.executescript(SQL_TRIGGERS)
        if self._get("state") is None:
            self._set("state", ESTADO_AUSENTE)

    def migrate_schema_v1_to_v2(self) -> str:
        """Migra EXPLÍCITAMENTE el único origen v1 reconocido, sin reconstruir el FTS.

        La operación no se llama desde ``ensure_schema`` ni desde ``rebuild``. Toma el
        lock antes de observar, exige versión, DDL y huellas v1 exactos y sólo entonces
        reemplaza ``search_au_key``. El índice v1 ya mantenía correctamente sus tokens;
        rotar la generación invalida los cursores emitidos bajo el keyset móvil. Cualquier
        origen desconocido se deshace completo y conserva sus bytes de diagnóstico.
        """
        self.con.execute("BEGIN IMMEDIATE")
        try:
            version = self.schema_v()
            if version != SEARCH_SCHEMA_V1:
                raise SearchMigrationRejected(
                    f"migración 1->2 exige schema_v=1 exacto; recibido {version!r}")
            estado = self.state()
            if estado != ESTADO_LISTO:
                raise SearchMigrationRejected(
                    f"migración 1->2 exige state=ready; recibido {estado!r}")
            generacion = self.generation()
            problema_gen = self._generation_problem(generacion)
            if problema_gen:
                raise SearchMigrationRejected(problema_gen)

            esperado_v1 = self.ddl_esperado_v1()
            vivo = self._ddl_vivo()
            if set(vivo) != set(esperado_v1):
                raise SearchMigrationRejected(
                    "DDL v1 desconocido: el juego de objetos no coincide exactamente")
            distintos = sorted(
                identidad for identidad in esperado_v1
                if vivo[identidad] != esperado_v1[identidad])
            if distintos:
                raise SearchMigrationRejected(
                    "DDL v1 desconocido; cuerpo distinto en: "
                    + ", ".join(_etiqueta_objeto(i) for i in distintos))

            huellas_raw = self._get("huellas")
            try:
                huellas = json.loads(huellas_raw) if huellas_raw is not None else None
            except (ValueError, TypeError):
                huellas = None
            huellas_v1 = self._huellas_v1_legacy(esperado_v1)
            if huellas != huellas_v1:
                raise SearchMigrationRejected(
                    "las huellas selladas no acreditan exactamente el DDL v1")
            if self._get("normalizador") != sc.normalizer_fingerprint():
                raise SearchMigrationRejected(
                    "la huella del normalizador no corresponde al código migrador")

            # El v1 y el v2 comparten contenido. Se prueba ese supuesto antes del DDL:
            # una base desacoplada necesita rebuild/recuperación, no una migración que la
            # bendiga cambiándole sólo el sello.
            try:
                self.con.execute(
                    "INSERT INTO search_fts(search_fts, rank) VALUES('integrity-check', 1)")
            except sqlite3.DatabaseError as e:
                raise SearchMigrationRejected(
                    "el FTS v1 no coincide con su contenido; no migro un índice roto") from e

            self.con.execute("DROP TRIGGER search_au_key")
            self.con.execute(SQL_TRIGGER_AU_KEY_V2.strip().rstrip(";"))
            nueva = _nueva_generacion()
            self._set("generation", nueva)
            self._set("schema_v", str(SEARCH_SCHEMA_V))
            self._set("huellas", json.dumps(self._huellas_objetos(), sort_keys=True))
            self.con.execute("COMMIT")
            return nueva
        except Exception:
            if self.con.in_transaction:
                self.con.execute("ROLLBACK")
            raise

    # ── verificaciones del entorno ───────────────────────────────────────────────
    def _verificar_motor(self) -> None:
        opciones = {r[0] for r in self.con.execute("PRAGMA compile_options")}
        if "ENABLE_FTS5" not in opciones:
            raise EngineUnsupported(
                "este SQLite no trae FTS5 compilado: sin él no hay índice de texto, y NO "
                "hay reserva con LIKE — un LIKE sobre 313 MiB no es el mismo producto")
        vers = tuple(int(x) for x in sqlite3.sqlite_version.split("."))
        if vers < SQLITE_MINIMO:
            raise EngineUnsupported(
                f"SQLite {sqlite3.sqlite_version} < {'.'.join(map(str, SQLITE_MINIMO))}: "
                f"por debajo, `integrity-check` con rank=1 no compara el índice contra su "
                f"contenido externo, así que la comprobación pasaría SIN mirar lo único "
                f"que importa")

    def _verificar_vfs(self) -> None:
        """El VFS tiene que poder durar. Comprueba el fichero y el modo de journal.

        Una base de fichero con `journal_mode=memory` NO es recuperable: una caída deja
        el índice a medias y el estado diciendo que está listo. Se comprueba aquí y no se
        supone porque el modo se puede cambiar en caliente desde cualquier conexión.
        """
        filas = self.con.execute("PRAGMA database_list").fetchall()
        principal = [f for f in filas if f[1] == "main"]
        if not principal:
            raise EngineUnsupported("no hay base `main` en esta conexión")
        ruta = principal[0][2]
        modo = self.con.execute("PRAGMA journal_mode").fetchone()[0].lower()
        if ruta:                                     # base de fichero
            if not os.path.exists(ruta):
                raise EngineUnsupported(f"la base `main` dice {ruta!r} y no existe")
            if modo not in ("wal", "delete", "truncate", "persist"):
                raise EngineUnsupported(
                    f"`journal_mode={modo}` sobre una base de FICHERO: una caída dejaría "
                    f"el índice a medias con el estado diciendo que está listo")
        elif modo not in ("memory", "wal", "delete", "truncate", "persist", "off"):
            raise EngineUnsupported(f"journal_mode inesperado en base de memoria: {modo}")

    # ── identidad del ESQUEMA contra un MANIFIESTO CANÓNICO DEL CÓDIGO ───────────
    #
    # ⚠️ ESTO REEMPLAZA UN SELLADO QUE SE AUTO-BENDECÍA, y el defecto era P0: yo fotografiaba
    # el DDL VIVO al reconstruir y luego lo comparaba conmigo mismo. Reproducido: con un
    # `search_ai` vaciado ANTES del rebuild, `readiness` daba **True**, las altas nuevas no
    # entraban al FTS, y `readiness` seguía **True** para siempre. Un sello que fotografía
    # lo que hay no acredita nada: bendice lo que encuentre, roto incluido.
    #
    # La autoridad ahora es el DDL que ESTE CÓDIGO declara. Y no se compara contra un
    # literal escrito a mano —que se desincroniza del `SCHEMA` real en el primer cambio—
    # sino contra lo que SQLite guardaría si lo creara: se levanta una base en memoria con
    # el mismo DDL y se lee su `sqlite_master`. Misma fuente, misma normalización, cero
    # transcripción.
    _ESPERADO: dict | None = None
    _ESPERADO_V1: dict | None = None
    _SOMBRAS_FTS: frozenset | None = None

    @staticmethod
    def _canon_sql(sql: str) -> str:
        """Espacios colapsados y sin `IF NOT EXISTS` (SQLite no lo guarda igual)."""
        plano = " ".join((sql or "").split())
        return plano.replace("IF NOT EXISTS ", "").rstrip(";")

    @classmethod
    def ddl_esperado(cls) -> dict[ObjectIdentity, str]:
        """`(schema,type,name,tbl_name) -> DDL` de TODOS los objetos del índice.

        La identidad completa es contractual. SQLite permite nombres repetidos entre
        namespaces (tabla+trigger, por ejemplo); reducirla a ``name`` pierde filas.
        """
        if cls._ESPERADO is None:
            tmp = sqlite3.connect(":memory:")
            tmp.row_factory = sqlite3.Row
            tmp.create_function(UDF_PROYECTA, 1, sc.project_body, deterministic=True)
            # `entries` mínima: los triggers la necesitan para poder crearse.
            tmp.execute("CREATE TABLE entries (ledger TEXT NOT NULL, eid TEXT NOT NULL,"
                        " arrival INTEGER, ts TEXT, actor TEXT, tipo TEXT, head TEXT,"
                        " body TEXT, ausente TEXT, PRIMARY KEY (ledger, eid))")
            tmp.executescript(SCHEMA)
            tmp.execute(SQL_FTS.strip().rstrip(";"))
            tmp.executescript(SQL_TRIGGERS)
            filas = tmp.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master"
                " WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            cls._SOMBRAS_FTS = frozenset(
                (r["type"], r["name"], r["tbl_name"])
                for r in filas if r["name"].startswith(FTS + "_"))
            cls._ESPERADO = {
                _identidad_objeto("main", r): cls._canon_sql(r["sql"])
                for r in filas if r["name"] != "entries"
                # Las tablas sombra que FTS5 crea solas (`search_fts_data`…) no se
                # comparan: son suyas y su forma es cosa de la versión del motor.
                if not r["name"].startswith(FTS + "_")}
            cls._ESPERADO.update({
                ("main", *identidad): cls._canon_sql(sql)
                for identidad, sql in TRIGGERS_COMPARTIDOS_TOLERADOS.items()})
            tmp.close()
        return dict(cls._ESPERADO)

    @classmethod
    def ddl_esperado_v1(cls) -> dict[ObjectIdentity, str]:
        """Manifiesto cerrado del único origen admitido por la migración 1→2."""
        if cls._ESPERADO_V1 is None:
            viejo = cls.ddl_esperado()
            claves = [identidad for identidad in viejo
                      if identidad[1:] == ("trigger", "search_au_key", "entries")]
            if len(claves) != 1:
                raise AssertionError(
                    "el manifiesto debe contener un único trigger search_au_key")
            viejo[claves[0]] = cls._canon_sql(SQL_TRIGGER_AU_KEY_V1)
            cls._ESPERADO_V1 = viejo
        return dict(cls._ESPERADO_V1)

    @staticmethod
    def _huellas_v1_legacy(
            manifiesto: dict[ObjectIdentity, str]) -> dict[str, str]:
        """Sello v1 publicado, que usaba nombre pero sólo sobre el canon cerrado.

        La compatibilidad no reabre el P0: el inventario vivo completo se compara antes
        y se exige que el canon tenga nombres únicos. Nunca se proyecta a nombre un
        conjunto vivo controlado por la base.
        """
        nombres = [identidad[2] for identidad in manifiesto]
        if len(nombres) != len(set(nombres)):
            raise AssertionError("el manifiesto v1 canónico no tiene nombres únicos")
        return {
            identidad[2]: hashlib.sha256(sql.encode("utf-8")).hexdigest()
            for identidad, sql in sorted(manifiesto.items())}

    @classmethod
    def _objeto_gobernado(cls, esquema: str, fila: sqlite3.Row) -> bool:
        """Decide qué objetos comparten la frontera de integridad de Search.

        No se pregunta sólo si el nombre era esperado: ése fue el falso verde B1. Todo
        trigger es código durable y se gobierna globalmente; un índice sobre una tabla
        propia también puede cambiar la semántica de escritura. Fuera de eso, las tablas,
        vistas e índices legacy quedan fuera mientras no ocupen el namespace reservado.
        Las sombras FTS exactas que creó ESTE motor son la única excepción `search_*`.
        """
        esperado = cls.ddl_esperado()
        tipo, nombre, tabla = fila["type"], fila["name"], fila["tbl_name"]
        identidad = _identidad_objeto(esquema, fila)
        if esquema == "temp":
            # Un TEMP TRIGGER puede actuar sobre `main.entries` pero no aparece en
            # sqlite_master. Ningún objeto temporal forma parte del esquema sellado.
            return _temp_afecta_search(fila)
        if identidad in esperado:
            return True
        if tipo == "trigger":
            return True
        if tipo == "index" and tabla in TABLAS_SEARCH_OWNED:
            return True
        if nombre.startswith("search_"):
            sombra = (tipo, nombre, tabla)
            return sombra not in (cls._SOMBRAS_FTS or frozenset())
        return False

    def _ddl_vivo(self) -> dict[ObjectIdentity, str]:
        """Inventario real, incluidos extras peligrosos y el esquema TEMP."""
        self.ddl_esperado()  # Inicializa también el censo exacto de sombras FTS.
        vivo = {}
        for esquema, maestro in (("main", "sqlite_master"),
                                 ("temp", "sqlite_temp_master")):
            for r in self.con.execute(
                    f"SELECT type, name, tbl_name, sql FROM {maestro}"
                    " WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"):
                if self._objeto_gobernado(esquema, r):
                    identidad = _identidad_objeto(esquema, r)
                    vivo[identidad] = self._canon_sql(r["sql"])
        return vivo

    def _desajustes_ddl(self) -> list[str]:
        """Comparación EXACTA y BIDIRECCIONAL contra el manifiesto del código.

        Bidireccional porque las dos direcciones son averías distintas: falta un objeto
        (el índice no se mantiene) o hay uno con OTRO cuerpo (se mantiene mal). Un
        `for n in esperados` sobre un manifiesto VACÍO no recorría nada y daba verde — es
        el segundo P0 de esta tanda, con `huellas='{}'` y `'[]'`.
        """
        esperado = self.ddl_esperado()
        vivo = self._ddl_vivo()
        problemas = []
        for identidad in sorted(set(esperado) - set(vivo)):
            problemas.append(f"falta {_etiqueta_objeto(identidad)}")
        for identidad in sorted(set(vivo) - set(esperado)):
            problemas.append(f"sobra {_etiqueta_objeto(identidad)}")
        for identidad in sorted(set(esperado) & set(vivo)):
            if vivo[identidad] != esperado[identidad]:
                problemas.append(f"{_etiqueta_objeto(identidad)} tiene OTRO cuerpo")
        return problemas

    def _huellas_objetos(self) -> dict:
        """`identidad JSON -> sha256(DDL)`. Registra; decide `_desajustes_ddl`."""
        return {_identidad_sello(identidad): hashlib.sha256(
                    sql.encode("utf-8")).hexdigest()
                for identidad, sql in sorted(self._ddl_vivo().items())}

    def _verificar_objetos(self) -> list[str]:
        presentes = {r["name"] for r in self.con.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view','trigger')")}
        faltan = [n for n in (*TABLAS, *VISTAS, FTS, *TRIGGERS) if n not in presentes]
        return faltan

    def _eids_largos(self) -> int:
        """Cuántos `eid` pasan de la frontera durable del cursor (`MAX_EID_BYTES`).

        Se comprueba en el rebuild y no al emitir el cursor: descubrirlo al paginar
        significaría servir una página que nadie puede continuar. Aquí es fail-closed —
        el índice no se sella— y el número dice cuántas filas lo causan.
        """
        return self.con.execute(
            "SELECT COUNT(*) c FROM entries WHERE length(CAST(eid AS BLOB)) > ?",
            (scur.MAX_EID_BYTES,)).fetchone()["c"]

    def _head_fuera_de_body(self) -> int:
        """Cuántas entradas tienen `head` que NO está dentro de `body`.

        El contrato indexa SÓLO `body` apoyándose en que el cuerpo contiene la cabecera
        literal. `qa` lo verificó en 250/250 de UNA muestra de UN carril con cuerpos
        <20 KB, y dejó dicho que sobre el corpus completo no terminó. Aquí se comprueba
        **globalmente** antes de sellar: si el supuesto es falso, indexar sólo `body`
        pierde recall en los titulares y el rebuild NO se sella.
        """
        # `rtrim` EN LOS DOS LADOS, corrección de `qa`: el proyector escribe la cabecera
        # con un espacio final real y el cuerpo sin él, así que un `instr` literal daba
        # falsos positivos sobre entradas perfectamente sanas. Lo que hay que comprobar es
        # que el TEXTO del titular está en el cuerpo, no que coincida byte a byte con su
        # espaciado. El control negativo —un `head` con palabras que NO están en el
        # cuerpo— sigue teniendo que dispararse: sin él, `rtrim` podría estar tapando el
        # caso real en vez de sólo el espacio.
        return self.con.execute(
            "SELECT COUNT(*) c FROM entries"
            " WHERE head IS NOT NULL AND rtrim(head) <> ''"
            "   AND (body IS NULL OR instr(rtrim(body), rtrim(head)) = 0)"
        ).fetchone()["c"]

    # ── rebuild ──────────────────────────────────────────────────────────────────
    def _tras_poblar(self) -> None:
        """No-op en produccion. Ver `rebuild`."""

    def rebuild(self) -> str:
        """Reconstruye el índice. Explícito, transaccional y con validación ANTES del sello.

        Contrato de fallo, que es la mitad que se olvida: **cualquier** fallo deja el
        estado en `building` y la generación **sin tocar**. Sólo el éxito completo escribe
        una generación nueva. Así, un índice a medias es visiblemente un índice a medias
        —la búsqueda responde `SearchNotReady`— en vez de responder pocas filas.
        """
        # Se lee ANTES de tocar esquema, estado o generación. Si el sello está corrupto
        # el error tipado sale sin "curarlo" ni destruir los bytes que permiten explicar
        # el fallo.
        v = self.schema_v()
        if v is not None and v > SEARCH_SCHEMA_V:
            raise SearchSchemaTooNew(
                f"el índice declara schema_v={v} y este código conoce {SEARCH_SCHEMA_V}: "
                f"no lo reconstruyo, porque lo escribiría con un esquema más viejo del "
                f"que ya tiene")
        if v is not None and v < SEARCH_SCHEMA_V:
            raise SearchStale(
                f"schema_v={v} requiere la migración administrativa explícita a "
                f"schema_v={SEARCH_SCHEMA_V}; rebuild no altera DDL versionado")
        self._verificar_motor()
        self._verificar_vfs()
        self.ensure_schema()

        # `building` se sella FUERA de la transacción de datos y se COMITEA: si el proceso
        # muere a mitad, el disco tiene que quedar diciendo «a medias». Escribirlo dentro
        # de la misma transacción que se va a deshacer lo dejaría diciendo lo de antes,
        # que es «listo» — un índice roto que se anuncia sano.
        self.con.execute("BEGIN IMMEDIATE")
        try:
            self._set("state", ESTADO_CONSTRUYENDO)
            self.con.execute("COMMIT")
        except Exception:
            self.con.execute("ROLLBACK")
            raise

        try:
            self.con.execute("BEGIN IMMEDIATE")
            # El índice de texto SÍ se rehace entero; el MAPEO no se toca más que para
            # AÑADIR lo que falte. Regenerarlo reasignaría `rid` a entradas que ya lo
            # tenían: cada rebuild movería el acoplamiento y caducaría cursores que no
            # tenían por qué caducar, además de tirar las lápidas que hacen estable el
            # `rid` de una entrada que va y vuelve.
            self.con.execute("DELETE FROM search_fts")
            self.con.execute(
                "INSERT OR IGNORE INTO search_documents(ledger, eid)"
                " SELECT ledger, eid FROM entries ORDER BY ledger, eid")
            self.con.execute(
                "INSERT INTO search_fts(rowid, body) SELECT rowid, body FROM search_view")
            # Costura de pruebas: corre ENTRE poblar y validar. Existe para que el
            # `integrity-check` de abajo se pueda ver disparar — una guarda cuyo camino de
            # captura nadie ejercita esta declarada, no probada.
            self._tras_poblar()

            faltan = self._verificar_objetos()
            if faltan:
                raise RebuildFailed(f"faltan objetos del índice: {', '.join(faltan)}")
            # EL DDL VIVO TIENE QUE SER EL DEL CÓDIGO, y se comprueba ANTES de sellar: si
            # se sellara primero, el rebuild bendeciría el trigger vaciado que encontró.
            desajustes = self._desajustes_ddl()
            if desajustes:
                raise RebuildFailed(
                    "el esquema vivo no es el que declara este código: "
                    + "; ".join(desajustes))

            largos = self._eids_largos()
            if largos:
                raise RebuildFailed(
                    f"{largos} entrada(s) con `eid` de más de {scur.MAX_EID_BYTES} bytes: "
                    f"su cursor no cabría en {scur.MAX_CURSOR_BYTES} y la página no se "
                    f"podría continuar. No sello un índice que emite cursores muertos")

            fuera = self._head_fuera_de_body()
            if fuera:
                raise RebuildFailed(
                    f"{fuera} entrada(s) tienen `head` que NO aparece dentro de `body`: "
                    f"indexar sólo `body` perdería esos titulares, así que no sello un "
                    f"índice que promete un recall que no da")

            n_entries = self.con.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"]
            # Se cuentan los mapeos VIVOS, no todas las filas: las lápidas son mapeos de
            # entradas que ya no están, y compararlas contra `entries` daría un desajuste
            # permanente en cuanto el corpus pierda una entrada.
            n_vivos = self.con.execute(
                "SELECT COUNT(*) c FROM search_documents d"
                " JOIN entries e ON e.ledger=d.ledger AND e.eid=d.eid").fetchone()["c"]
            if n_vivos != n_entries:
                raise RebuildFailed(f"mapeos vivos {n_vivos} != entradas {n_entries}")
            n_fts = self.con.execute("SELECT COUNT(*) c FROM search_fts").fetchone()["c"]
            if n_fts != n_vivos:
                raise RebuildFailed(f"filas de índice {n_fts} != mapeos vivos {n_vivos}")
            n_lapidas = self.con.execute(
                "SELECT COUNT(*) c FROM search_documents").fetchone()["c"] - n_vivos

            # `rank=1` NO es opcional: sin él, `integrity-check` valida la estructura
            # interna del índice y NO la compara con el contenido externo. Medido: con el
            # cuerpo cambiado por debajo, el `integrity-check` a secas pasa VERDE y el de
            # `rank=1` da `database disk image is malformed`. Es la diferencia entre
            # comprobar el acoplamiento y no comprobarlo.
            self.con.execute(
                "INSERT INTO search_fts(search_fts, rank) VALUES('integrity-check', 1)")

            gen = _nueva_generacion()
            self._set("schema_v", str(SEARCH_SCHEMA_V))
            self._set("generation", gen)
            self._set("state", ESTADO_LISTO)
            self._set("rebuilt_at", str(int(time.time())))
            self._set("huellas", json.dumps(self._huellas_objetos(), sort_keys=True))
            self._set("normalizador", sc.normalizer_fingerprint())
            self._set("documents", str(n_vivos))
            self._set("tombstones", str(n_lapidas))
            self.con.execute("COMMIT")
            return gen
        except Exception as e:
            try:
                self.con.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            # El estado queda en `building` (comiteado arriba) y la generación intacta.
            if isinstance(e, SearchError):
                raise
            raise RebuildFailed(f"rebuild abortado: {type(e).__name__}: {e}") from e

    # ── readiness ────────────────────────────────────────────────────────────────
    def readiness(self) -> dict:
        """Verde SÓLO si el estado lo dice Y los objetos están.

        El estado por sí solo no basta y la diferencia es medible: si alguien borra un
        trigger, `search_state` sigue diciendo `ready` y el índice deja de recibir altas
        **sin un error**. La comprobación mira las dos cosas.
        """
        problemas: list[str] = []
        try:
            estado = self.state()
        except SearchStateCorrupt as e:
            estado = None
            problemas.append(e.detail)
        faltan = self._verificar_objetos()
        schema_roto = None
        try:
            v = self.schema_v()
        except SearchSchemaCorrupt as e:
            # Salud NUNCA propaga corrupción de metadatos: responde rojo y conserva el
            # diagnóstico. Los caminos operacionales sí obtienen el error tipado mediante
            # `_exigir_listo`/`rebuild`.
            v = None
            schema_roto = e
        try:
            gen = self.generation()
            problema_gen = self._generation_problem(gen)
        except SearchStateCorrupt as e:
            gen = None
            problema_gen = e.detail
        if estado != ESTADO_LISTO:
            problemas.append(f"estado={estado}")
        if faltan:
            problemas.append("faltan objetos: " + ", ".join(faltan))
        # GENERACIÓN PRESENTE Y BIEN FORMADA. `ready` sin generación deja el cursor sin
        # nada que atar, y una generación que no es hex de 128 bits no es una que haya
        # emitido este código: en los dos casos el sello miente.
        if problema_gen:
            problemas.append(problema_gen)
        # DDL: contra el manifiesto DEL CÓDIGO, exacto y en las dos direcciones.
        problemas.extend(self._desajustes_ddl())
        # Y el sello, sólo para detectar que la base la construyó otro código. Parseo
        # FAIL-CLOSED: un valor ilegible, no-dict o vacío es un NO, nunca una excepción
        # que sube ni un verde. `json.loads` suelto lanzaba `JSONDecodeError` desde
        # `readiness`, que es lo último que puede permitirse una función de salud.
        try:
            sellado = self._get("huellas")
        except SearchStateCorrupt as e:
            sellado = None
            problemas.append(e.detail)
        if sellado is None:
            if estado == ESTADO_LISTO:
                problemas.append("sin huellas de esquema selladas")
        else:
            try:
                esperadas = json.loads(sellado)
            except (ValueError, TypeError):
                esperadas = None
            if not isinstance(esperadas, dict) or not esperadas:
                problemas.append("huellas selladas ilegibles o vacías")
            else:
                vivas = self._huellas_objetos()
                if set(esperadas) != set(vivas):
                    faltan_s = sorted(set(vivas) - set(esperadas))
                    sobran_s = sorted(set(esperadas) - set(vivas))
                    problemas.append(
                        f"el juego de huellas no cuadra (sin sellar: {faltan_s}; "
                        f"selladas y ausentes: {sobran_s})")
                else:
                    distintos = sorted(n for n in esperadas if vivas[n] != esperadas[n])
                    if distintos:
                        problemas.append("huella distinta en: " + ", ".join(distintos))
        # NORMALIZADOR: si cambió, el índice se construyó con otras reglas que la consulta.
        try:
            norma = self._get("normalizador")
        except SearchStateCorrupt as e:
            norma = None
            problemas.append(e.detail)
        if norma is not None and norma != sc.normalizer_fingerprint():
            problemas.append("el normalizador cambió desde el rebuild")
        elif norma is None and estado == ESTADO_LISTO:
            problemas.append("sin huella de normalizador")
        if schema_roto is not None:
            problemas.append(schema_roto.detail)
        elif v is None and estado == ESTADO_LISTO:
            problemas.append("sin schema_v sellado")
        elif v is not None and v < SEARCH_SCHEMA_V:
            problemas.append(f"schema_v={v} < {SEARCH_SCHEMA_V} (rebuild viejo)")
        elif v is not None and v > SEARCH_SCHEMA_V:
            problemas.append(f"schema_v={v} > {SEARCH_SCHEMA_V} (índice más nuevo)")
        return {
            "ready": not problemas,
            "state": estado,
            "schema_v": v,
            "schema_v_code": SEARCH_SCHEMA_V,
            "generation": gen,
            "missing": faltan,
            "problems": problemas,
        }

    def _exigir_listo(self) -> str:
        r = self.readiness()
        if r["ready"]:
            return r["generation"]
        # `readiness` absorbe deliberadamente la corrupción para seguir siendo un sensor
        # total. El camino operacional la vuelve a clasificar aquí como error tipado.
        v = self.schema_v()
        if v is not None and v > SEARCH_SCHEMA_V:
            raise SearchSchemaTooNew("; ".join(r["problems"]))
        if v is not None and v < SEARCH_SCHEMA_V:
            raise SearchStale("; ".join(r["problems"]))
        raise SearchNotReady("; ".join(r["problems"]))

    @staticmethod
    def _canon_ledgers(lane, ledgers):
        """Valida la colección de ledgers de un carril. Sirve al mapa y al resolutor."""
        # Un `str` ES iterable, así que `{"64bis": "64bis-wiki"}` produciría un
        # `frozenset` de LETRAS y autorizaría el ledger `"6"`. Se rechaza en vez de
        # aceptarse en silencio: una ACL mal escrita que «funciona» es peor que un error.
        if isinstance(ledgers, (str, bytes)):
            raise ValueError(
                f"los ledgers de {lane!r} tienen que ser una colección, no una cadena: "
                f"una cadena se expandiría a sus LETRAS y autorizaría cualquier cosa")
        permitidos = frozenset(ledgers)
        if any(not isinstance(x, str) or not x for x in permitidos):
            raise ValueError(f"ledgers inválidos para el carril {lane!r}: {ledgers!r}")
        return permitidos

    @staticmethod
    def _canon_acl(acl):
        """`{lane: {ledger, …}}` o un RESOLUTOR `lane -> ledgers`. `None` = NIEGA TODO.

        ⚠️ La ACL es del SERVIDOR y se fija al CONSTRUIR, no es un parámetro de `search`.
        Un alcance que viaja en la llamada lo elige quien llama, y entonces no autoriza
        nada: se autoriza a sí mismo. Por eso `search` recibe `lane` —quién dice ser— y la
        autorización sale de aquí, que es lo único que el llamante no controla.

        El RESOLUTOR existe para el caso en que la tabla no quepa en memoria o cambie en
        caliente (un servicio que la consulta a su propia fuente). Se valida su respuesta
        igual que el mapa: un resolutor que devuelva una cadena tiene el mismo agujero.
        """
        if acl is None:
            return None
        if callable(acl):
            return acl
        if not isinstance(acl, dict) or not acl:
            raise ValueError("`acl` tiene que ser un dict no vacío `lane -> ledgers`")
        canon = {}
        for lane, ledgers in acl.items():
            if not isinstance(lane, str) or not lane:
                raise ValueError(f"carril inválido en la ACL: {lane!r}")
            permitidos = SearchStore._canon_ledgers(lane, ledgers)
            if not permitidos:
                raise ValueError(f"ledgers inválidos para el carril {lane!r}: {ledgers!r}")
            canon[lane] = permitidos
        return canon

    def set_acl(self, mapa: dict) -> bool:
        """PERSISTE la ACL. Acto del SERVIDOR, en el arranque. Devuelve si CAMBIÓ algo.

        **Todo dentro del MISMO `BEGIN IMMEDIATE`**: se toma el lock de escritura, y sólo
        entonces se lee la ACL durable y se compara. Leer antes del lock era una carrera
        con nombre: otro proceso podía cambiar la política entre la lectura y el `return`
        del no-op, y esta instancia se quedaba con `self.acl` diciendo una cosa mientras el
        `EXISTS` del SQL aplicaba otra. La política que se compara tiene que ser la misma
        que se escribe, y eso sólo lo garantiza el lock.

        **Idempotente**: si la política que se pide es EXACTAMENTE la que hay —comprobado
        DENTRO del lock— no se escribe nada: ni `DELETE`, ni `INSERT`, ni disparo de
        trigger. Sin esto, volver a aplicar la MISMA configuración rotaba la generación y
        caducaba todos los cursores vivos: un arranque idéntico se comportaba como un
        cambio de política.

        **Deny-first**: se BORRA antes de insertar, así que no hay instante intermedio con
        una autorización de más.

        **Exige conexión sin transacción**. No es comodidad: dentro de una transacción
        ajena, `self.acl` se publicaría antes del `COMMIT` del llamante, y su `ROLLBACK`
        dejaría la ACL de memoria diciendo lo que la base ya no dice. Sin un hook de commit
        de verdad, la única postura honesta es negarse.

        **No exige `rebuild`**: la autorización la aplica el `EXISTS` de la sentencia en la
        consulta siguiente. Lo único que caduca son los cursores ya emitidos, y de eso se
        encargan los triggers rotando la generación.
        """
        canon = self._canon_acl(mapa)
        if canon is None or callable(canon):
            raise ValueError("`set_acl` persiste un MAPA; un resolutor no se puede sellar")
        if self.con.in_transaction:
            raise ConnectionContractViolation(
                "`set_acl` necesita abrir su propia transacción: dentro de la tuya, tu "
                "`ROLLBACK` dejaría la ACL en memoria diciendo lo que la base ya no dice. "
                "Cierra la transacción o dame una conexión dedicada")
        self.con.execute("BEGIN IMMEDIATE")
        try:
            # LECTURA Y COMPARACIÓN **DENTRO** DEL LOCK.
            cambia = self.acl_persistida() != canon
            if cambia:
                self.con.execute("DELETE FROM search_acl")          # deny-first
                self.con.executemany(
                    "INSERT INTO search_acl(lane, ledger) VALUES(?, ?)",
                    sorted((lane, led) for lane, leds in canon.items() for led in leds))
            self.con.execute("COMMIT")
        except Exception:
            self.con.execute("ROLLBACK")
            raise
        # Se PUBLICA en memoria sólo después de que el commit haya ido bien. Al revés, un
        # fallo al confirmar dejaba esta instancia autorizando por una política que la base
        # nunca llegó a tener.
        self.acl = canon
        return cambia

    def acl_persistida(self) -> dict:
        """Lo que hay EN LA BASE, que es lo que aplica el `EXISTS` de la sentencia."""
        fuera: dict = {}
        for r in self.con.execute("SELECT lane, ledger FROM search_acl ORDER BY 1, 2"):
            fuera.setdefault(r["lane"], set()).add(r["ledger"])
        return {k: frozenset(v) for k, v in fuera.items()}

    def _autoriza(self, lane: str, ledger: str) -> None:
        """LA FRONTERA DE AUTORIZACIÓN. Corre ANTES de tocar la base."""
        if self.acl is None:
            raise LaneNotAuthorized(
                "esta instancia no declara ACL de carriles, así que NIEGO TODO. `lane` "
                "sólo ataba la firma del cursor: eso impide reusar un cursor con otros "
                "filtros, no autoriza a leer un ledger")
        if callable(self.acl):
            # Un resolutor que revienta NIEGA. No se degrada a «permite»: un fallo de la
            # fuente de autorización no puede abrir el acceso.
            try:
                permitidos = self._canon_ledgers(lane, self.acl(lane) or ())
            except LaneNotAuthorized:
                raise
            except Exception as e:
                raise LaneNotAuthorized(
                    f"el resolutor de ACL falló para el carril {lane!r} "
                    f"({type(e).__name__}: {e}): niego") from None
            if ledger not in permitidos:
                raise LaneNotAuthorized(
                    f"el resolutor niega el ledger {ledger!r} al carril {lane!r}")
        # LA AUTORIDAD ES LA TABLA, SIEMPRE. El mapa que se pasó al construir es una
        # DECLARACIÓN, no una fuente: se publica en memoria sin haberse contrastado con el
        # disco, así que puede discrepar desde el primer segundo —y discrepa de verdad
        # cuando `set_acl` choca con otro escritor y no llega a aplicarse—. Medido con dos
        # conexiones: la instancia se quedaba autorizando por una política que la tabla no
        # tenía. Preguntarle a la misma tabla que aplica el `EXISTS` hace imposible que la
        # decisión de Python y la del SQL se separen: es una consulta por PK.
        fila = self.con.execute(
            "SELECT 1 FROM search_acl WHERE lane = ? AND ledger = ?",
            (lane, ledger)).fetchone()
        if fila is None:
            raise LaneNotAuthorized(
                f"el carril {lane!r} no está autorizado a leer el ledger {ledger!r}")

    # ── búsqueda ─────────────────────────────────────────────────────────────────
    # Unidad de los desplazamientos de `marcas`. Se DECLARA en la respuesta porque no hay
    # una sola respuesta correcta: para un emoji, Python cuenta puntos de código, UTF-8
    # cuenta bytes y JavaScript cuenta unidades UTF-16 — el mismo resaltado sale [1,5],
    # [4,8] o [2,6]. Un consumidor que suponga la suya recorta por el sitio equivocado y
    # parte un carácter. Aquí son PUNTOS DE CÓDIGO, y va escrito en cada respuesta.
    UNIDAD_MARCAS = "unicode_codepoint"

    @staticmethod
    def _recorta(txt, tope: int) -> tuple[str, bool, int]:
        """Acota un campo por BYTES, **marca incluida**.

        Se reserva el sitio de la marca ANTES de cortar: el valor devuelto —texto recortado
        MÁS `…`— cabe en `tope`. La versión anterior cortaba a `tope` y pegaba la elipsis
        después, así que devolvía `tope + 3`. Se corta en frontera de carácter (`ignore`
        sobre el decode) para no emitir UTF-8 partido.
        """
        if txt is None:
            return "", False, 0
        texto = str(txt)
        crudo = texto.encode("utf-8")
        if len(crudo) <= tope:
            return texto, False, len(texto)
        hueco = tope - len(MARCA_RECORTE.encode("utf-8"))
        if hueco <= 0:
            return "", True, 0
        # Se devuelve TAMBIÉN la frontera RETENIDA en puntos de código: cuántos caracteres
        # del original sobreviven, SIN contar la elipsis. Sin ese número, una marca que
        # caiga en la zona cortada se recolocaba sobre el `…` — reproducido: un `match` en
        # el primer carácter omitido quedaba en `[2045, 2046]`, señalando la elipsis en vez
        # de texto. La marca no se puede validar contra `len(fragmento)` porque el
        # fragmento YA lleva la marca de recorte dentro.
        retenido = crudo[:hueco].decode("utf-8", "ignore")
        return retenido + MARCA_RECORTE, True, len(retenido)

    @staticmethod
    def _marcas(txt: str) -> tuple[str, list]:
        """Convierte los centinelas del `snippet` en DESPLAZAMIENTOS y los retira.

        Marcar con `[` y `]` dentro del texto es ambiguo: un cuerpo que ya traiga
        corchetes produce un resaltado que el cliente no puede separar del contenido. Los
        centinelas son de uso privado (U+E000/U+E001) y el normalizador los BORRA del
        texto indexado, así que no pueden aparecer por contenido — y aquí se cambian por
        pares `[inicio, fin]` sobre el fragmento ya limpio.
        """
        fuera, marcas, ini = [], [], None
        pos = 0
        for ch in txt:
            if ch == sc.CENTINELA_INI:
                ini = pos
            elif ch == sc.CENTINELA_FIN:
                if ini is not None:
                    marcas.append([ini, pos])
                    ini = None
            else:
                fuera.append(ch)
                pos += 1
        return "".join(fuera), marcas

    @staticmethod
    def _serializa(obj) -> bytes:
        """Serialización de MEDIDA, deliberadamente la más GORDA de las plausibles.

        Con `separators` compactos salían ~1.000 bytes menos que con los de por defecto, y
        quien emite la respuesta es la capa HTTP, no este módulo: medir con la forma
        compacta y que el emisor use la ancha deja el techo superado por un margen que
        nadie ve. Se mide con los separadores por defecto —cota superior— para que el techo
        aguante sea cual sea el emisor.
        """
        return json.dumps(obj, ensure_ascii=False).encode("utf-8")

    def _bytes_fila(self, fila: dict) -> int:
        return len(self._serializa(fila))

    def _envelope_bytes(self, gen: str, fsha: str, orden: str) -> int:
        """Coste del SOBRE, medido serializándolo — no estimado.

        Se mide con el peor caso de cada campo variable: un cursor real (los suyos tienen
        longitud fija: `arrival` es el entero más largo del corpus, `eid` es un sha256 de
        64 hex, `generation` 32 y `filter_sha256` 64) y el `truncado` con todas sus claves.
        Reservar «un número redondo» sería volver a estimar, que es de donde venía el
        defecto: el acumulado contaba filas y el sobre viajaba gratis.
        """
        # PEOR CASO DE VERDAD: el `eid` más largo que la frontera PERMITE
        # (`MAX_EID_BYTES`), no el que suele haber (un sha256 de 64). Reservar 64 mientras
        # se aceptan 128 deja el sobre corto justo con las entradas que más pesan. Y
        # `arrival` al máximo de int64, no a `2**62`.
        cursor_peor = self._cursor_peor(gen, fsha)
        # Ancho FIJO y sobredimensionado en los contadores, no `MAX_RESPONSE_BYTES`: si el
        # coste del sobre dependiera del techo, bajar el techo encogería la reserva y el
        # presupuesto no bajaría lo mismo.
        peor_truncado = {"por": "bytes", "servidas": 10 ** 12, "recortadas": 10 ** 12,
                         "bytes": 10 ** 12, "bytes_filas": 10 ** 12,
                         "snippets_recortados": 10 ** 12,
                         "campos_recortados": 10 ** 12,
                         "bytes_presupuestados": 10 ** 12}
        return len(self._serializa(
            self._envoltura([], cursor_peor, True, peor_truncado, orden, gen, fsha)))

    def _cursor_peor(self, gen: str, fsha: str) -> str:
        """El cursor MÁS LARGO que este servicio puede emitir.

        `eid` al máximo que la FRONTERA permite (`MAX_EID_BYTES`), no al que suele haber
        (un sha256 de 64), y `arrival` al tope de int64. Reservar 64 mientras se aceptan
        128 deja el sobre corto justo con las entradas que más pesan. Está expuesto para
        que un test pueda comprobar que cubre al cursor real más largo — si sólo viviera
        dentro del cálculo, esa comprobación tendría que reimplementarlo.
        """
        return scur.encode(arrival=2 ** 63 - 1, eid="f" * scur.MAX_EID_BYTES,
                           generation=gen, filter_sha256=fsha, key=self.cursor_key)

    @staticmethod
    def _marcas_validas(marcas: list, retenido: int) -> list:
        """Marcas que caen DENTRO del texto retenido, sin tocar la marca de recorte.

        `retenido` y no `len(fragmento)`: el fragmento incluye la elipsis, así que medir
        contra él deja pasar una marca que señala el `…`. Es función aparte para poder
        falsarla con una marca justo en la frontera — por el camino de `search` ese caso
        depende de que el `snippet` coloque el resaltado en el sitio exacto.
        """
        return [m for m in marcas if m[1] <= retenido]

    @classmethod
    def _envoltura(cls, filas, cursor, hay_mas, truncado, orden, gen, fsha) -> dict:
        """LA FORMA de la respuesta, en UN solo sitio.

        La medida del sobre y la respuesta que sale se construyen con esta función, así que
        no pueden derivar. Antes eran dos literales distintos y derivaron: la respuesta ganó
        `unidad_marcas` y claves en `truncado`, la medida no se enteró, y la reserva quedó
        **10 bytes corta** — medido por un test que exigía `reserva >= sobre real`. Un
        presupuesto que se calcula sobre una forma que ya no es la que se emite es un
        presupuesto de otra cosa.
        """
        return {
            "filas": filas,
            "cursor": cursor,
            "hay_mas": hay_mas,
            "truncado": truncado,
            "orden": orden,
            "unidad_marcas": cls.UNIDAD_MARCAS,
            "generacion": gen,
            "filtro_sha256": fsha,
        }

    def search(self, *, lane: str | None = None, ledger: str, query: str,
               scope: BoundSearchScope | None = None, limit: int = 20,
               actor: str | None = None, tipo: str | None = None,
               desde: str | None = None, hasta: str | None = None,
               cursor: str | None = None) -> dict:
        """Borde instrumentado: una operación produce una señal, sin query ni payload."""
        sensor = self._sensor
        if sensor is not None and scope is not None:
            from telemetry_bridge import gateway_runtime_instance, sensor_matches
            if not sensor_matches(
                    sensor, principal=scope.principal_id, role=scope.role,
                    lane=scope.lane, runtime_instance=gateway_runtime_instance(),
                    credential_generation=0):
                sensor = None                 # Resource ajeno: nunca se mezcla
        try:
            respuesta = self._search_impl(
                lane=lane, ledger=ledger, query=query, scope=scope, limit=limit,
                actor=actor, tipo=tipo, desde=desde, hasta=hasta, cursor=cursor)
        except Exception as e:
            if sensor is not None:
                try:
                    sensor.count("tool.failures", tool_class="read", exit_class="error",
                                 outcome="error")
                    sensor.span("runtime.job", attributes={"operation": "search",
                                "outcome": "error", "error_code": type(e).__name__})
                except Exception:
                    pass
            raise
        if sensor is not None:
            try:
                sensor.span("runtime.job", attributes={"operation": "search",
                            "outcome": "ok", "has_more": bool(respuesta["hay_mas"])})
            except Exception:
                pass
        return respuesta

    def _search_impl(self, *, lane: str | None = None, ledger: str, query: str,
               scope: BoundSearchScope | None = None, limit: int = 20,
               actor: str | None = None, tipo: str | None = None,
               desde: str | None = None, hasta: str | None = None,
               cursor: str | None = None) -> dict:
        """Una página. `LIMIT n+1` para saber si hay más sin pagar un `COUNT(*)`.

        Dos techos, y los dos se DECLARAN: filas (`limit`) y bytes de respuesta. El de
        bytes existe porque el de filas no acota nada en este corpus — 40 entradas pueden
        ser 6 MB. Cuando corta el de bytes, el cursor apunta a la última fila SERVIDA, no
        a la última leída: si apuntara a la leída, las de en medio se perderían.
        """
        # UNA SOLA INSTANTÁNEA DE LECTURA para readiness, generación y SELECT.
        #
        # Antes cada uno abría su propia lectura: entre «estás listo» y el `SELECT` cabía
        # un `rebuild` entero, así que una página se podía servir con la generación de
        # antes sobre el índice de después — filas de un índice y cursor de otro, sin un
        # error. `BEGIN DEFERRED` fija la instantánea en la primera lectura y la mantiene
        # hasta el `COMMIT`: si alguien pasa a `building` a mitad, esta transacción sigue
        # viendo el estado con el que decidió, y la SIGUIENTE llamada ya lo ve.
        # EL ALCANCE MANDA SOBRE EL `lane` SUELTO. Si viene un `BoundSearchScope`, el
        # carril sale de él y el `ledger` pedido tiene que estar DENTRO — así el borde
        # HTTP no puede colar un carril, porque no es él quien construye el alcance.
        if scope is not None:
            if lane is not None and lane != scope.lane:
                raise LaneNotAuthorized(
                    f"el alcance atado dice carril {scope.lane!r} y la llamada pide "
                    f"{lane!r}: no elijo, rechazo")
            lane = scope.lane
            if not scope.permite(ledger):
                raise LaneNotAuthorized(
                    f"el sujeto {scope.principal_id!r} (carril {scope.lane!r}) no alcanza el "
                    f"ledger {ledger!r}: su alcance es {sorted(scope.ledgers)}")
        elif lane is None:
            raise LaneNotAuthorized(
                "hace falta un `scope` atado (o un `lane` interno): sin sujeto no hay "
                "autorización que comprobar")
        # AUTORIZACIÓN PRIMERO, antes de abrir nada: un carril no autorizado no llega ni
        # a costar una transacción, y el error no puede depender del estado del índice.
        # El alcance NO sustituye a la ACL durable: son dos fronteras y las dos aplican
        # (la del sujeto y la del servicio), y además el `EXISTS` vuelve a aplicarla en SQL.
        self._autoriza(lane, ledger)
        if self.con.in_transaction:
            raise SearchError(
                "hay una transacción abierta en esta conexión: `search` necesita abrir la "
                "suya para leer readiness, generación y filas de la MISMA instantánea")
        self.con.execute("BEGIN DEFERRED")
        try:
            gen = self._exigir_listo()
            n = sc.check_limit(limit)
            filtros = sc.canonical_filters(lane=lane, ledger=ledger, actor=actor,
                                           tipo=tipo, desde=desde, hasta=hasta,
                                           query=query, order=sc.ORDER_ARRIVAL_DESC)
            fsha = sc.filter_sha256(filtros)

            usar_cursor, c_arrival, c_eid = 0, None, ""
            if cursor is not None:
                c_arrival, c_eid = scur.decode(cursor, key=self.cursor_key,
                                               filter_sha256=fsha, generation=gen)
                usar_cursor = 1

            filas = self._ejecutar(SQL_BUSQUEDA, (
                filtros["query"],
                lane,                       # ← la ACL, DENTRO de la sentencia
                ledger, ledger,
                actor, actor,
                tipo, tipo,
                desde, desde,
                hasta, hasta,
                usar_cursor, c_arrival if usar_cursor else 0, c_eid,
                n + 1,
            ))
        finally:
            # Lectura pura: se cierra siempre y no hay nada que deshacer.
            self.con.execute("COMMIT")

        hay_mas_por_filas = len(filas) > n
        candidatas = filas[:n]

        # PRESUPUESTO REAL: el techo menos lo que ocupa el SOBRE. El acumulado contaba sólo
        # filas y el `cursor`, el `truncado` y los tres campos de cola viajaban gratis: una
        # respuesta «de 256 KiB» medía 256 KiB + el sobre.
        sobre = self._envelope_bytes(gen, fsha, filtros["order"])
        presupuesto = MAX_RESPONSE_BYTES - sobre

        page: list[dict] = []
        acumulado = 0
        snippets_recortados = 0
        campos_recortados = 0
        corte_por_bytes = False
        for cruda in candidatas:
            fila = dict(cruda)
            crudo_frag, marcas = self._marcas(str(fila.get("fragmento") or ""))
            fila["fragmento"], rec, retenido = self._recorta(crudo_frag,
                                                            MAX_SNIPPET_BYTES)
            # Contra la frontera RETENIDA, no contra `len(fragmento)`: el fragmento
            # incluye la elipsis, así que medir contra él dejaba pasar marcas que apuntan
            # a la propia marca de recorte.
            fila["marcas"] = self._marcas_validas(marcas, retenido)
            snippets_recortados += int(rec)
            # Caps CONTRACTUALES por campo. `head` es el que puede desbordar solo; `actor` y
            # `tipo` se acotan por la misma regla y no por miedo: los tres salen del
            # markdown y ninguno tiene cota en `entries`.
            for campo, tope in (("head", MAX_HEAD_BYTES), ("actor", MAX_ACTOR_BYTES),
                                ("tipo", MAX_TIPO_BYTES)):
                if fila.get(campo) is not None:
                    fila[campo], rec, _ = self._recorta(fila[campo], tope)
                    campos_recortados += int(rec)
            # SEPARADOR POR FILA. `json.dumps` mete `", "` entre elementos del array —
            # `2 * (n - 1)` bytes que la suma de filas sueltas NO ve. Con 100 filas son
            # ~198 bytes fuera del presupuesto, y ése era el margen que hacía que la
            # respuesta se pasara del techo declarándose dentro. Se presupuesta 2 por fila
            # (cota superior: cobra también la primera, que no lleva separador).
            coste = self._bytes_fila(fila) + SEPARADOR_FILA_BYTES
            if acumulado + coste > presupuesto:
                if not page:
                    # NI UNA fila cabe. No se devuelve cursor: con `hay_mas=true` y cero
                    # filas, el llamante pagina para siempre sin avanzar.
                    raise ResponseTooLarge(
                        f"la primera fila ocupa {coste} B y el presupuesto de filas es "
                        f"{presupuesto} B (techo {MAX_RESPONSE_BYTES} - sobre {sobre}): "
                        f"no sirvo cero filas con cursor, que sería un bucle")
                corte_por_bytes = True
                break
            page.append(fila)
            acumulado += coste

        hay_mas = hay_mas_por_filas or corte_por_bytes
        siguiente = None
        if hay_mas and page:
            ultima = page[-1]
            siguiente = scur.encode(arrival=ultima["arrival"], eid=ultima["eid"],
                                    generation=gen, filter_sha256=fsha,
                                    key=self.cursor_key)
        por = "bytes" if corte_por_bytes else ("filas" if hay_mas_por_filas else None)
        respuesta = self._envoltura(
            page, siguiente, hay_mas,
            # Se declara SIEMPRE, también en cero: quien llama tiene que poder distinguir
            # «no hay más» de «hay más y te di menos», y por CUÁL de los dos techos.
            {"por": por,
             "servidas": len(page),
             # La fila SONDA (`LIMIT n+1`) no es una fila recortada: existe sólo para saber
             # si hay más. Contarla decía «te recorté una» sobre una página completa.
             "recortadas": max(0, len(candidatas) - len(page)),
             "bytes": acumulado,
             # Lo que de verdad se gastó del presupuesto, separadores incluidos. Se publica
             # porque es la única cifra contra la que se puede falsar que los separadores
             # se presupuestan: `total <= techo` casi siempre sobra margen y deja pasar el
             # defecto hasta el día que no sobra.
             "bytes_filas": acumulado,
             "snippets_recortados": snippets_recortados,
             "campos_recortados": campos_recortados,
             "bytes_presupuestados": 0},
            filtros["order"], gen, fsha)
        # DEFENSA FINAL: si aun así no cabe, se RETIRAN filas por el final y se recalcula
        # el cursor, hasta caber. Antes esto lanzaba y ya está — un `500` donde había una
        # respuesta perfectamente servible con una fila menos. Lanzar se reserva para
        # cuando no queda ninguna: ahí sí es un rechazo tipado y SIN cursor, porque servir
        # cero filas con cursor deja al llamante en un bucle.
        for _ in range(len(page) + 4):
            for _ in range(4):                       # punto fijo del campo que se declara
                total = len(self._serializa(respuesta))
                if respuesta["truncado"]["bytes_presupuestados"] == total:
                    break
                respuesta["truncado"]["bytes_presupuestados"] = total
            else:
                raise ResponseTooLarge("el tamaño declarado no converge")
            if total <= MAX_RESPONSE_BYTES:
                break
            if len(respuesta["filas"]) <= 1:
                raise ResponseTooLarge(
                    f"ni una fila cabe: {total} B contra un techo de "
                    f"{MAX_RESPONSE_BYTES} B. No devuelvo cursor, que sería un bucle")
            respuesta["filas"].pop()
            ultima = respuesta["filas"][-1]
            respuesta["cursor"] = scur.encode(
                arrival=ultima["arrival"], eid=ultima["eid"], generation=gen,
                filter_sha256=fsha, key=self.cursor_key)
            respuesta["hay_mas"] = True
            respuesta["truncado"]["por"] = "bytes"
            respuesta["truncado"]["servidas"] = len(respuesta["filas"])
            respuesta["truncado"]["recortadas"] = max(
                0, len(candidatas) - len(respuesta["filas"]))
        return respuesta
