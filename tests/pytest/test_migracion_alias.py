"""② migración alias→rol de `cursors` — colapsa por MIN, con backup, idempotente.

Arnés: `sembrar_schema_y_meta()` crea el esquema COMPLETO de producción (via
`servicio.SCHEMA`) y declara `meta['schema_v']`/`['roster_v']` YA al día ANTES de
sembrar filas de `cursors` a mano. Es necesario para que la migración vea de
verdad las filas sembradas — ver el docstring de esa función en `conftest.py`
para el porqué (spoiler: sin esto, "no such table: entries" dispara la
reconstrucción-por-corrupción, un camino real pero DISTINTO, antes de que la
migración llegue a correr).
"""
from __future__ import annotations

import glob
import os
import sqlite3

from fastapi.testclient import TestClient

from .conftest import construir, db_directa, sembrar_schema_y_meta


def test_migracion_min_no_max(servicio):
    """MIN, no MAX: perder correo por adelantar el cursor de golpe es peor que
    volver a ver algo ya leído.

    FALSADOR: si quedara 100 (el MAX) o dos filas separadas, la migración no
    colapsó de verdad / no usó MIN.
    """
    con = sembrar_schema_y_meta(servicio)
    con.execute("INSERT INTO cursors VALUES ('backend','demo-ledger',100,'x')")
    con.execute("INSERT INTO cursors VALUES ('backend-biklabs','demo-ledger',40,'x')")
    con.commit()
    con.close()

    with TestClient(servicio.app):
        pass  # el 'with' ya disparó lifespan → migración

    con = db_directa(servicio)
    filas = con.execute(
        "SELECT agent, last_arrival FROM cursors WHERE ledger='demo-ledger'"
    ).fetchall()
    con.close()
    assert [(f["agent"], f["last_arrival"]) for f in filas] == [("be", 40)]


def test_migracion_backup_existe_y_es_sqlite_valido(servicio):
    """FALSADOR (del propio test, no sólo del código): si se borra/renombra el
    backup a mano y se repite la comprobación con el mismo path, debe fallar —
    si no, el test estaba mirando otra cosa que no fuera el fichero real.
    """
    con = sembrar_schema_y_meta(servicio)
    con.execute("INSERT INTO cursors VALUES ('backend','demo-ledger',100,'x')")
    con.execute("INSERT INTO cursors VALUES ('backend-biklabs','demo-ledger',40,'x')")
    con.commit()
    con.close()

    with TestClient(servicio.app):
        pass

    con = db_directa(servicio)
    backup_path = con.execute(
        "SELECT v FROM meta WHERE k='cursores_migracion_backup'"
    ).fetchone()["v"]
    con.close()

    assert os.path.exists(backup_path)
    bak = sqlite3.connect(backup_path)
    assert bak.execute("SELECT 1").fetchone() == (1,)
    bak.close()

    # Falsador del test: si el backup desaparece, la comprobación de existencia
    # tiene que dejar de pasar — si no, no estaba mirando el fichero real.
    os.remove(backup_path)
    assert not os.path.exists(backup_path)


def test_migracion_idempotente_no_repite_sin_gate_reset(servicio):
    """Dos arranques seguidos sobre la MISMA base ya migrada: la segunda vez NO
    se crea un segundo backup (gate por `meta['cursores_migrados_v']`).

    Y el falsador exigido por el spec: forzando el gate a un valor viejo ENTRE
    arranque 1 y 2, el segundo SÍ debe volver a migrar y crear un segundo
    backup — si no lo hace, el gate está mal (falso negativo, no sólo falso
    positivo).

    Nota de diseño: `SELECT COUNT(*) FROM meta WHERE k=...` no puede discriminar
    esto por construcción — `meta` tiene PRIMARY KEY(k) y `INSERT OR REPLACE`
    nunca sube esa cuenta por encima de 1, haya corrido la migración una vez o
    dos. La señal que sí distingue "volvió a migrar" de "no volvió" es el propio
    VALOR (la ruta del backup cambia) y el recuento de ficheros físicos en disco.
    """
    con = sembrar_schema_y_meta(servicio)
    con.execute("INSERT INTO cursors VALUES ('backend','demo-ledger',100,'x')")
    con.execute("INSERT INTO cursors VALUES ('backend-biklabs','demo-ledger',40,'x')")
    con.commit()
    con.close()

    with TestClient(servicio.app):
        pass
    con = db_directa(servicio)
    backup_1 = con.execute(
        "SELECT v FROM meta WHERE k='cursores_migracion_backup'"
    ).fetchone()["v"]
    con.close()
    patron = os.path.join(os.path.dirname(os.environ["LLMINBOX_DB"]),
                           "llminbox.sqlite.bak-migracion-alias-*")
    assert len(glob.glob(patron)) == 1

    # Segundo arranque, SIN tocar el gate: no debe volver a migrar.
    with TestClient(servicio.app):
        pass
    con = db_directa(servicio)
    backup_2 = con.execute(
        "SELECT v FROM meta WHERE k='cursores_migracion_backup'"
    ).fetchone()["v"]
    con.close()
    assert backup_2 == backup_1
    assert len(glob.glob(patron)) == 1

    # Forzar el gate a un valor viejo: el TERCER arranque SÍ debe volver a migrar.
    con = db_directa(servicio)
    con.execute("UPDATE meta SET v='0' WHERE k='cursores_migrados_v'")
    con.commit()
    con.close()
    with TestClient(servicio.app):
        pass
    con = db_directa(servicio)
    backup_3 = con.execute(
        "SELECT v FROM meta WHERE k='cursores_migracion_backup'"
    ).fetchone()["v"]
    con.close()
    assert backup_3 != backup_1
    assert len(glob.glob(patron)) == 2


def test_migracion_no_borra_fantasmas(servicio):
    """Una fila cuyo `agent` no resuelve en el censo (typo, agente dado de baja)
    se deja TAL CUAL — ni se borra ni se fusiona con nada parecido.

    FALSADOR: si la fila desaparece o se fusiona con 'wiki' (parecido léxico a
    'wikivault'), la migración se excedió de su alcance declarado (borrado de
    datos no autorizado en este spec).
    """
    con = sembrar_schema_y_meta(servicio)
    con.execute("INSERT INTO cursors VALUES ('wikivault','demo-ledger',7,'x')")
    con.commit()
    con.close()

    with TestClient(servicio.app):
        pass

    con = db_directa(servicio)
    fila = con.execute(
        "SELECT agent, last_arrival FROM cursors WHERE ledger='demo-ledger' "
        "AND agent='wikivault'"
    ).fetchone()
    con.close()
    assert fila is not None
    assert fila["last_arrival"] == 7


def test_interaccion_leido_por_rol_tras_migrar(servicio):
    """Interacción ①×②: la migración deja filas bajo `agent='be'`; leer/escribir
    después con el nombre de ROL tiene que funcionar sobre esa MISMA fila.
    """
    con = sembrar_schema_y_meta(servicio)
    con.execute("INSERT INTO cursors VALUES ('backend','demo-ledger',0,'x')")
    con.commit()
    con.close()

    with TestClient(servicio.app) as c:
        # c3ca365: el ack va contra lo CONCEDIDO, asi que el ledger tiene que estar indexado
        # antes del POST o `hasta:1` da 409 ACK_BEYOND_LEDGER. El sujeto aqui es el ALIAS.
        servicio.barrido()
        c.headers.update({"X-Llminbox-Token": "test-token"})
        r = c.post("/inbox/be/leido", json={"hasta": {"demo-ledger": 1}})
        assert r.status_code == 200
        assert r.json()["aplicados"]["demo-ledger"] == {"antes": 0, "ahora": 1}

    con = db_directa(servicio)
    fila = con.execute(
        "SELECT last_arrival FROM cursors WHERE agent='be' AND ledger='demo-ledger'"
    ).fetchone()
    con.close()
    assert fila["last_arrival"] == 1


def test_interaccion_leido_por_rol_sin_migracion(servicio, monkeypatch):
    """① no depende de que ② haya corrido: con la migración desactivada a mano
    (fila nunca existió, migrar_alias_a_rol es no-op), el mismo POST sigue
    dando 200 y crea la fila 'be' desde cero — son piezas independientes en el
    código aunque coordinadas en el deploy.
    """
    def _no_migrar(con):
        return

    monkeypatch.setattr(servicio, "migrar_alias_a_rol", _no_migrar)

    with TestClient(servicio.app) as c:
        # c3ca365: el ack va contra lo CONCEDIDO, asi que el ledger tiene que estar indexado
        # antes del POST o `hasta:1` da 409 ACK_BEYOND_LEDGER. El sujeto aqui es el ALIAS.
        servicio.barrido()
        c.headers.update({"X-Llminbox-Token": "test-token"})
        r = c.post("/inbox/be/leido", json={"hasta": {"demo-ledger": 1}})
        assert r.status_code == 200
        _ap = r.json()["aplicados"]["demo-ledger"]
        assert (_ap["antes"], _ap["ahora"]) == (-1, 1)

    con = db_directa(servicio)
    fila = con.execute(
        "SELECT last_arrival FROM cursors WHERE agent='be' AND ledger='demo-ledger'"
    ).fetchone()
    con.close()
    assert fila["last_arrival"] == 1


def test_migracion_no_fija_flag_con_censo_vacio(tmp_path, monkeypatch):
    """(D) El flag de idempotencia NO se fija si el censo llegó vacío — el
    falsador exacto del review×3: un fallo transitorio leyendo roster.json en
    el primer arranque no puede dejar la fusión cancelada PARA SIEMPRE.

    Boot 1 (LLMINBOX_ROSTER → ruta inexistente): la migración no agrupa nada
    (censo vacío ⇒ todo cae por la rama fantasma) y NO escribe
    meta['cursores_migrados_v'] — las filas alias quedan separadas.
    Boot 2 (roster sano): la migración SÍ corre y fusiona por MIN.

    FALSADOR del propio test: revertir el guard `censo_valido` en
    migrar_alias_a_rol (fijar el flag incondicionalmente, como antes del fix)
    deja el boot 2 sin reintentar y la aserción final cae con dos filas.
    """
    s1 = construir(tmp_path, monkeypatch,
                   extra_env={"LLMINBOX_ROSTER": str(tmp_path / "no-existe.json")})
    con = sembrar_schema_y_meta(s1)
    con.execute("INSERT INTO cursors VALUES ('backend','demo-ledger',100,'x')")
    con.execute("INSERT INTO cursors VALUES ('backend-biklabs','demo-ledger',40,'x')")
    con.commit()
    con.close()

    with TestClient(s1.app):
        pass

    con = db_directa(s1)
    flag = con.execute("SELECT v FROM meta WHERE k='cursores_migrados_v'").fetchone()
    filas = con.execute(
        "SELECT agent FROM cursors WHERE ledger='demo-ledger' ORDER BY agent"
    ).fetchall()
    con.close()
    assert flag is None, "el flag se fijó con censo vacío — la fusión no se reintentaría jamás"
    assert [f["agent"] for f in filas] == ["backend", "backend-biklabs"]

    # Y el falsador de security del re-review×3: con el censo roto NO se crea
    # backup — el corte va antes del con.backup(). Sin esto, un roster roto
    # persistente + crash-loop acumulaba un .bak-* por reinicio, sin límite.
    patron = os.path.join(os.path.dirname(os.environ["LLMINBOX_DB"]),
                          "llminbox.sqlite.bak-migracion-alias-*")
    assert glob.glob(patron) == [], "boot con censo vacío generó backup — acumulación en crash-loop"

    # Boot 2: mismo tmp_path (misma BD), roster ya sano (construir lo escribe y
    # apunta LLMINBOX_ROSTER de vuelta a él). El cambio de huella de censo NO
    # toca cursores (ver [arranque] CENSO cambiado en servicio.py) — lo que
    # fusiona aquí es la migración reintentada, no un reset.
    s2 = construir(tmp_path, monkeypatch)
    with TestClient(s2.app):
        pass

    con = db_directa(s2)
    filas = con.execute(
        "SELECT agent, last_arrival FROM cursors WHERE ledger='demo-ledger'"
    ).fetchall()
    con.close()
    assert [(f["agent"], f["last_arrival"]) for f in filas] == [("be", 40)]
