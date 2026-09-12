"""Arnés de los tests de M2. Importa PRODUCCIÓN: nada aquí reimplementa el sujeto.

Un arnés que se escribe su propio compilador, su propio cursor o su propio SQL mide el
arnés. Todo lo que se prueba sale de `search_contract`, `search_cursor` y `search_store`.
"""

import os
import sqlite3
import sys

import pytest

RAIZ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if RAIZ not in sys.path:
    sys.path.insert(0, RAIZ)

import search_contract as sc      # noqa: E402
import search_cursor as scur      # noqa: E402
import search_store as ss         # noqa: E402

CLAVE = b"clave-de-cursor-de-32-bytes-o-mas!!"

# Los nombres son los REALES del despliegue medidos por `qa`. Con `llminbox` (que no
# tiene hermanos) el falsador de frontera NO discrimina: hay que usar una familia con
# prefijo compartido por tokens, que es donde el `MATCH` deja de aislar.
CARRIL = "64bis"
LANE = "64bis-wiki"
HERMANOS = ("64bis-wiki-archivo", "64bis-wiki-queue")
ACTOR = "cto"
ACTORES_HERMANOS = ("cto-A", "cto-64bis", "cto-biklabs")

# ACL DEL CARRIL, explícita. No hay valor por defecto que autorice: el arnés declara qué
# puede leer cada carril, igual que tendrá que hacerlo el llamante de producción. Los
# carriles hermanos entran en la ACL A PROPÓSITO — si no estuvieran, el aislamiento
# cross-lane saldría verde por la autorización y no por la frontera `= ?` del SQL, que es
# lo que esos tests dicen medir.
# DOS carriles autorizados sobre los MISMOS ledgers, y uno que NO lo está. Los tres hacen
# falta y cada uno mide algo distinto:
#   · `CARRIL` y `CARRIL_HERMANO` autorizados ⇒ el cursor sigue teniendo que atarse al
#     carril: dos carriles legítimos no comparten paginación. Sin el segundo autorizado,
#     ese test daría verde por la ACL y no por la firma, que es lo que dice medir.
#   · `CARRIL_NO_AUTORIZADO` ⇒ el falsador de la ACL. No aparece en el mapa a propósito.
CARRIL_HERMANO = "64bis-lectura"
CARRIL_NO_AUTORIZADO = "carril-inventado"
ACL = {CARRIL: {LANE, *HERMANOS}, CARRIL_HERMANO: {LANE, *HERMANOS}}

ESQUEMA_ENTRIES = """
CREATE TABLE entries (
  ledger TEXT NOT NULL, eid TEXT NOT NULL,
  arrival INTEGER, seq INTEGER, line_no INTEGER, byte_off INTEGER,
  ts TEXT, actor TEXT, tipo TEXT, head TEXT, body TEXT,
  visto TEXT, ausente TEXT, provisional INTEGER DEFAULT 0,
  PRIMARY KEY (ledger, eid));
CREATE INDEX i_arr ON entries(ledger, arrival);
"""


def nueva_con(tmp_path, nombre="m2.sqlite"):
    con = sqlite3.connect(str(tmp_path / nombre))
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(ESQUEMA_ENTRIES)
    return con


def mete(con, ledger, eid, arrival, *, actor="backend", tipo="FYI", ts="2026-09-05",
         cuerpo="born red", ausente=None, head=None):
    """El `head` va DENTRO del `body` por defecto: es la premisa que sostiene indexar
    sólo `body`, y el rebuild la comprueba globalmente. Un arnés que la rompiera sin
    querer haría fallar el rebuild por un motivo que no es el del test."""
    head = head if head is not None else f"titular {eid}"
    body = f"{head}\n{cuerpo}"
    con.execute(
        "INSERT INTO entries(ledger,eid,arrival,ts,actor,tipo,head,body,ausente)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (ledger, eid, arrival, ts, actor, tipo, head, body, ausente))


@pytest.fixture
def store(tmp_path):
    con = nueva_con(tmp_path)
    st = ss.SearchStore(con, cursor_key=CLAVE, acl=ACL)
    st.ensure_schema()
    # La ACL se PERSISTE: es la que aplica el `EXISTS` de la sentencia. La de Python sola
    # dejaría el `EXISTS` sin filas y todo saldría vacío — que es el fallo correcto, pero
    # no el que estos tests quieren medir.
    st.set_acl(ACL)
    return st


@pytest.fixture
def poblado(store):
    """Familia de carriles hermanos + familia de actores hermanos, con el mismo texto.

    El mismo cuerpo en los tres carriles es lo que hace discriminante al falsador: si
    cada carril tuviera texto distinto, el `MATCH` los separaría por contenido y el test
    daría verde sin que la frontera hiciera nada.
    """
    con = store.con
    n = 0
    for ledger in (LANE, *HERMANOS):
        for i in range(6):
            n += 1
            mete(con, ledger, f"{ledger}-e{i}", n, actor=ACTOR, cuerpo="born red comun")
    for actor in ACTORES_HERMANOS:
        n += 1
        mete(con, LANE, f"{LANE}-{actor}", n, actor=actor, cuerpo="born red comun")
    con.commit()
    store.rebuild()
    return store
