"""M1-7: los tres bypass reproducidos por la auditoría, con huella de artefactos.

Cada falsador compara CONJUNTO + tamaño + SHA-256 de `db`, `-wal` y `-shm` antes
y después. Un veredicto correcto acompañado de un `-wal` nuevo sigue siendo una
escritura sobre la base de otro.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import threading

import pytest

import coordination as C
from ._arnes import GRAMATICA, INTENT, LANES, PEPPER, censo, journal, sesion

RAIZ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _artefactos(ruta):
    """Conjunto + tamaño + SHA de db/wal/shm. El CONJUNTO es la mitad del dato:
    que aparezca un `-shm` donde no había es un cambio aunque el `.db` no varíe."""
    out = {}
    for suf in ("", "-wal", "-shm"):
        p = ruta + suf
        if os.path.exists(p):
            with open(p, "rb") as fh:
                b = fh.read()
            out[suf or "db"] = (len(b), hashlib.sha256(b).hexdigest())
    return out


def _base_ajena(ruta, *, sello=None, modo="DELETE", meta_only=False):
    con = sqlite3.connect(ruta)
    if meta_only:
        con.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
        if sello is not None:
            con.execute("INSERT INTO meta VALUES ('durable_v',?)", (str(sello),))
    else:
        con.execute("CREATE TABLE cosas_de_otro (x)")
        con.execute("INSERT INTO cosas_de_otro VALUES (1)")
    con.commit()
    con.execute(f"PRAGMA journal_mode={modo}")
    con.close()


# ── ① EL FICHERO APARECE ENTRE __init__ Y initialize ───────────────────────

@pytest.mark.parametrize("como,error", [
    ("cero", C.PreflightRejected),
    ("meta_only", C.SchemaIndeterminate),
    ("v3_ajena", C.SchemaIndeterminate),
])
def test_01_lo_que_aparece_DESPUES_de_construir_se_clasifica_igual(tmp_path, como, error):
    """`_preexistia` se calculaba en `__init__` y decidía si clasificar. Entre
    construir el objeto y usarlo cabe que otro actor cree el fichero: con la foto
    vieja, el preflight se saltaba entero y el original se abría y se mutaba.

    La ruta NO existe cuando nace el `Journal`. Aparece después.
    """
    ruta = str(tmp_path / "coordination.sqlite")
    j = C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)      # aún no existe
    assert not os.path.exists(ruta)

    if como == "cero":
        open(ruta, "wb").close()
    elif como == "meta_only":
        _base_ajena(ruta, sello=3, meta_only=True)
    else:
        _base_ajena(ruta)
    antes = _artefactos(ruta)

    with pytest.raises(error):
        j.initialize()
    j.close()
    assert _artefactos(ruta) == antes, (
        f"tocó un fichero `{como}` que apareció tras construir el Journal")


def test_02_una_FUTURA_en_WAL_que_aparece_despues_no_se_toca(tmp_path):
    """El caso más fino de ①: el sello futuro vive en el `-wal`, así que sólo se
    ve recuperando — y recuperar sobre el original es escribir en él."""
    ruta = str(tmp_path / "coordination.sqlite")
    j = C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)      # aún no existe
    assert not os.path.exists(ruta)

    (tmp_path / "cuna").mkdir()
    otro = journal(tmp_path / "cuna")
    otro.close()
    shutil.copy((tmp_path / "cuna" / "coordination.sqlite"), ruta)
    viva = sqlite3.connect(ruta)
    viva.execute("PRAGMA journal_mode=WAL")
    viva.execute("UPDATE meta SET v=? WHERE k='durable_v'", (str(C.DURABLE_V + 1),))
    viva.commit()                     # el sello v4 está SÓLO en el -wal
    antes = _artefactos(ruta)
    assert "-wal" in antes and antes.get("-shm") is not None

    try:
        with pytest.raises(C.SchemaTooNew):
            j.initialize()
        assert j._veredicto[1] == C.DURABLE_V + 1, (
            "no recuperó el WAL para clasificar: leyó el sello viejo del .db")
        j.close()
        despues = _artefactos(ruta)
        assert set(despues) == set(antes), "cambió el CONJUNTO de artefactos"
        assert despues["db"] == antes["db"], "escribió en el .db de una base futura"
        assert despues["-wal"] == antes["-wal"], "tocó el -wal ajeno"
    finally:
        viva.close()


# ── ② PREGUNTAR NO PUEDE CREAR ─────────────────────────────────────────────

@pytest.mark.parametrize("verbo", ["stored_durable_v", "health", "_assert_pragmas"])
def test_03_preguntar_antes_de_initialize_no_deja_artefactos(tmp_path, verbo):
    """`stored_durable_v()`, `health()` y `_assert_pragmas()` abrían la base para
    poder contestar — y abrirla la CREA, con su `-wal` y su `-shm`. Un chequeo de
    salud que fabrica el sujeto que examina siempre encuentra algo."""
    ruta = str(tmp_path / "coordination.sqlite")
    j = C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
    antes = set(os.listdir(tmp_path))

    if verbo == "health":
        salud = j.health()
        assert salud["initialized"] is False and salud["estado"] == "nueva"
        assert salud["pragmas"] is None
    else:
        with pytest.raises(C.JournalNotInitialized):
            getattr(j, verbo)()
    j.close()
    assert set(os.listdir(tmp_path)) == antes, (
        f"`{verbo}` dejó artefactos: {set(os.listdir(tmp_path)) - antes}")
    assert not os.path.exists(ruta)


# ── ③ NI mode=ro NI immutable SOBRE EL ORIGINAL ────────────────────────────

def test_04_en_solo_lectura_se_sirve_el_SNAPSHOT_no_el_original_rancio(tmp_path):
    """`immutable` sobre una base en WAL NO aplica el `-wal`, así que el original
    servía una `generation` VIEJA mientras el commit durable estaba en el WAL.

    ⚠️ El fallo de apertura se FUERZA en vez de simularlo con permisos: con
    `chmod` el resultado depende de la plataforma —aquí el `open` RW llegó a
    tener éxito— y el test habría medido el sistema de ficheros en vez del
    camino de fallback. Lo que se prueba es la RAMA: cuando el original no se
    puede abrir para escribir, lo que se sirve es la copia recuperada.
    """
    (tmp_path / "cuna").mkdir()
    j = journal(tmp_path / "cuna")
    sesion(j)
    j.rotate_map_generation()
    gen_antes = j.generation()
    j.close()
    ruta = str(tmp_path / "coordination.sqlite")
    shutil.copy((tmp_path / "cuna" / "coordination.sqlite"), ruta)

    # Commit durable que vive SÓLO en el `-wal`, sin checkpoint.
    viva = sqlite3.connect(ruta)
    viva.execute("PRAGMA journal_mode=WAL")
    viva.execute("UPDATE meta SET v=? WHERE k='generation'", (str(gen_antes + 7),))
    viva.commit()
    try:
        real = sqlite3.connect
        original = os.path.realpath(ruta)

        def connect_falla_en_el_original(destino, *a, **k):
            if isinstance(destino, str) and os.path.realpath(destino) == original:
                raise sqlite3.OperationalError("attempt to write a readonly database")
            return real(destino, *a, **k)

        ro = C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
        ro.initialize()                       # clasifica leyendo, aún sin parchear
        ro.close()
        C.sqlite3.connect = connect_falla_en_el_original
        try:
            # CONTRATO NUEVO: `_connect()` NO degrada por su cuenta. Degradar
            # ahí exigía un upgrade SH→EX que se colgaba contra sí mismo
            # (medido: 890/890 frames en `fcntl.flock`), así que falla CERRADO
            # y la transición a sólo lectura es un acto POSTERIOR y explícito.
            with pytest.raises(C.JournalReadOnly):
                ro._connect()
            assert ro._degradacion_pendiente is True

            # Y el acto explícito: `initialize()` la consume y degrada.
            ro.initialize()
            assert ro.health()["writable"] is False

            # EL PUNTO ORIGINAL DEL TEST SIGUE EN PIE, y es el que importa: lo
            # que se sirve NO es el original en `immutable` —que no aplica el
            # `-wal` y daría la `generation` VIEJA—, sino la copia recuperada.
            servida = ro._connect().execute("PRAGMA database_list").fetchall()[0][2]
            assert os.path.realpath(servida) != original, (
                "cayó al original degradado en vez de a la copia recuperada")
            assert ro.generation() == gen_antes + 7, (
                "sirvió la generación RANCIA: el snapshot no traía el WAL aplicado")
            assert ro._connect().execute("PRAGMA query_only").fetchone()[0] == 1
        finally:
            C.sqlite3.connect = real
            ro.close()
    finally:
        viva.close()


def _hijo_escritor(ruta, valor, *, limpio: bool):
    """Escribe un COMMIT durable en otro proceso.

    `limpio=False` es un CRASH DE VERDAD: `wal_autocheckpoint=0` para que el
    commit NO baje al `.db`, `COMMIT`, y `os._exit` —que no cierra la conexión,
    no corre el checkpoint y no ejecuta `atexit`—. Lo que queda en disco es lo
    que queda tras un `kill -9` con trabajo durable en el `-wal`.

    `limpio=True` es el CONTROL POSITIVO: mismo commit, pero cerrando. El
    checkpoint baja el WAL al `.db`. Sin este brazo el test no discrimina: no
    sabríamos si el valor se ve por el WAL o porque ya estaba en el `.db`.
    """
    guion = (
        "import os, sqlite3\n"
        f"con = sqlite3.connect({ruta!r}, isolation_level=None)\n"
        "con.execute('PRAGMA journal_mode=WAL')\n"
        "con.execute('PRAGMA wal_autocheckpoint=0')\n"
        "con.execute('BEGIN IMMEDIATE')\n"
        f"con.execute(\"UPDATE meta SET v=? WHERE k='generation'\", ({valor!r},))\n"
        "con.execute('COMMIT')\n"
        + ("con.close()\nos._exit(0)\n" if limpio else "os._exit(0)\n"))
    r = subprocess.run([sys.executable, "-c", guion], timeout=120,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-400:]


def test_05_un_CRASH_REAL_deja_el_WAL_en_disco_y_el_journal_lo_lee(tmp_path):
    """Crash de verdad, no un `-wal` fabricado a mano.

    El test anterior escribía, CERRABA (checkpoint) y restauraba los bytes del
    `-wal` con un `open(...,'wb')`. Eso produce un fichero con la forma de un
    WAL, pero el estado del proceso —y el `-shm`— eran los de un cierre limpio:
    medía una reconstrucción del test, no una recuperación.
    """
    (tmp_path / "cuna").mkdir()
    otro = journal(tmp_path / "cuna")
    otro.dispose()
    ruta = str(tmp_path / "coordination.sqlite")
    shutil.copy((tmp_path / "cuna" / "coordination.sqlite"), ruta)

    _hijo_escritor(ruta, "77777", limpio=False)

    # ① EL WAL PERSISTE: es la premisa del test, y si no se cumple el resto no
    #    mide nada — habría medido un `.db` ya actualizado.
    assert os.path.exists(ruta + "-wal"), "no hubo crash: el WAL no sobrevivió"
    assert os.path.getsize(ruta + "-wal") > 0
    crudo = sqlite3.connect("file:" + ruta + "?immutable=1", uri=True)
    try:
        en_db = crudo.execute("SELECT v FROM meta WHERE k='generation'").fetchone()[0]
    finally:
        crudo.close()
    assert en_db != "77777", (
        "el commit ya estaba en el .db: `wal_autocheckpoint=0` no surtió efecto "
        "y este test no distingue leer-el-WAL de leer-el-db")

    # ② Y EL JOURNAL SÍ LO VE: la copia recuperada aplica el `-wal`.
    with C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA) as j:
        j.initialize()
        assert j.generation() == 77777


def test_05b_CONTROL_POSITIVO_el_cierre_limpio_baja_el_WAL_y_no_deja_rastro(tmp_path):
    """Mismo commit, cerrando. Si este brazo no se distinguiera del de arriba,
    el par no probaría nada sobre la recuperación."""
    (tmp_path / "cuna").mkdir()
    otro = journal(tmp_path / "cuna")
    otro.dispose()
    ruta = str(tmp_path / "coordination.sqlite")
    shutil.copy((tmp_path / "cuna" / "coordination.sqlite"), ruta)

    _hijo_escritor(ruta, "88888", limpio=True)

    assert os.path.getsize(ruta + "-wal") == 0 if os.path.exists(ruta + "-wal") else True
    crudo = sqlite3.connect("file:" + ruta + "?immutable=1", uri=True)
    try:
        assert crudo.execute(
            "SELECT v FROM meta WHERE k='generation'").fetchone()[0] == "88888"
    finally:
        crudo.close()
    with C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA) as j:
        j.initialize()
        assert j.generation() == 88888


def test_06_sin_veredicto_no_hay_conexion_ni_en_otra_hebra(tmp_path):
    """La máquina de estados se bloquea UNA vez por `Journal`: ninguna hebra
    puede abrir por su cuenta lo que la instancia aún no ha clasificado."""
    ruta = str(tmp_path / "coordination.sqlite")
    j = C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
    fallos = []

    def intenta():
        try:
            j._connect()
            fallos.append("una hebra abrió sin veredicto")
        except C.JournalNotInitialized:
            pass

    h = threading.Thread(target=intenta)
    h.start(); h.join(timeout=30)
    assert fallos == [], fallos
    assert not os.path.exists(ruta), "la hebra creó el fichero"
