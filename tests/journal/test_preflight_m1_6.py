"""M1-6: el fichero no se abre con SQLite hasta saber qué es.

Casi todo aquí se mide en BYTES y en (dev, inode), no en excepciones: «no lo
toqué» es una afirmación sobre el fichero, y la única forma de comprobarla es
mirar el fichero antes y después.

Los fixtures que quedan huérfanos se limpian por PID PROPIO: este disco lo
comparten varias sesiones y borrar por patrón mata el trabajo del vecino.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import time

import pytest

import coordination as C
from ._arnes import GRAMATICA, INTENT, LANES, PEPPER, censo, journal, sesion

MIO = f"m16-{os.getpid()}"


@pytest.fixture(autouse=True)
def _limpia_lo_mio(tmp_path):
    """Borra SÓLO lo que abrió este proceso. Barrer por patrón en un disco
    compartido por varias sesiones mata corridas ajenas que nadie relaciona
    después con este test."""
    yield
    raiz = "/tmp"
    for nombre in os.listdir(raiz):
        if nombre.startswith(f"llminbox-preflight-") and MIO in nombre:
            shutil.rmtree(os.path.join(raiz, nombre), ignore_errors=True)


def _huella(ruta):
    out = {}
    for suf in C.SUFIJOS:
        p = ruta + suf
        if os.path.exists(p):
            st = os.lstat(p)
            with open(p, "rb") as fh:
                out[suf or "db"] = (st.st_size, st.st_mtime_ns,
                                    hashlib.sha256(fh.read()).hexdigest())
    return out


def _identidad(ruta):
    st = os.lstat(ruta)
    return (st.st_dev, st.st_ino)


def _base(tmp_path, *, sellar=None, modo="wal"):
    """Base nuestra, opcionalmente re-sellada y con el journal_mode que se pida."""
    j = journal(tmp_path)
    s = sesion(j)
    j.accept_event(s.token, idempotency_key="k", intent=INTENT, ledger="llminbox")
    j.close()
    ruta = str(tmp_path / "coordination.sqlite")
    con = sqlite3.connect(ruta)
    if sellar is not None:
        con.execute("UPDATE meta SET v=? WHERE k='durable_v'", (str(sellar),))
        con.commit()
    con.execute(f"PRAGMA journal_mode={modo}")
    con.close()
    return ruta, s


def _abre(ruta):
    return C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)


# ── ① FUTURA: NI UN BYTE, EN NINGÚN MODO ───────────────────────────────────

def test_01_futura_en_DELETE_queda_intacta(tmp_path):
    ruta, _ = _base(tmp_path, sellar=C.DURABLE_V + 1, modo="DELETE")
    antes, ident = _huella(ruta), _identidad(ruta)
    j = _abre(ruta)
    with pytest.raises(C.SchemaTooNew):
        j.initialize()
    j.close()
    assert _huella(ruta) == antes and _identidad(ruta) == ident


def test_02_futura_con_WAL_sobrevive_a_un_os_exit(tmp_path):
    """El proceso muere SIN cerrar nada. Si el arranque hubiera abierto el
    original, el `-wal` o el checkpoint quedarían escritos en el fichero ajeno."""
    ruta, _ = _base(tmp_path, sellar=C.DURABLE_V + 1, modo="WAL")
    # Se deja un `-wal` con contenido real y sin checkpoint.
    # La conexión se deja ABIERTA a propósito: al cerrarla, SQLite hace
    # checkpoint y el `-wal` desaparece — el escenario se desmontaba solo.
    viva = sqlite3.connect(ruta)
    viva.execute("PRAGMA journal_mode=WAL")
    viva.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('ruido','1')")
    viva.commit()
    antes = _huella(ruta)
    assert "-wal" in antes, "el escenario necesita un -wal presente"

    guion = (
        "import sys, os;"
        f"sys.path.insert(0, {os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))!r});"
        "import coordination as C;"
        f"j = C.Journal({ruta!r}, pepper={PEPPER!r}, lane_ledgers={LANES!r}, recipient_resolver=censo, grammar=GRAMATICA);"
        "\ntry:\n    j.initialize()\nexcept Exception:\n    pass\n"
        "os._exit(0)")
    subprocess.run([sys.executable, "-c", guion], timeout=60, check=False)
    try:
        assert _huella(ruta) == antes, "la muerte súbita dejó rastro en una base futura"
    finally:
        viva.close()


# ── ② SÓLO LECTURA Y WAL ───────────────────────────────────────────────────

def test_03_readonly_sin_shm_no_miente(tmp_path):
    """Sin `-shm` y sin poder crearlo, `immutable` haría INVISIBLE lo que quedó
    en el WAL — y la lectura saldría verde diciendo menos de lo que hay. El
    preflight recupera sobre la copia, así que lo del WAL SE VE."""
    ruta, s = _base(tmp_path, modo="WAL")
    con = sqlite3.connect(ruta)
    con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('marca_wal','presente')")
    con.commit()
    con.close()
    for p in (ruta + "-shm",):
        if os.path.exists(p):
            os.unlink(p)
    os.chmod(ruta, stat.S_IRUSR)
    os.chmod(tmp_path, stat.S_IRUSR | stat.S_IXUSR)
    try:
        j = _abre(ruta)
        j.initialize()                 # clasificar antes de leer: ya no hay atajo
        fila = j._connect().execute(
            "SELECT v FROM meta WHERE k='marca_wal'").fetchone()
        assert fila is not None and fila[0] == "presente", (
            "el contenido del WAL quedó invisible: la lectura mintió por omisión")
        j.close()
    finally:
        os.chmod(tmp_path, stat.S_IRWXU)
        os.chmod(ruta, stat.S_IRUSR | stat.S_IWUSR)


def test_04_un_frame_de_WAL_incompleto_se_ignora(tmp_path):
    """Un WAL con basura al final no puede colarse como dato: SQLite corta en el
    último frame válido, y lo que se sirve tiene que ser eso."""
    ruta, _ = _base(tmp_path, modo="WAL")
    con = sqlite3.connect(ruta)
    con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('bueno','si')")
    con.commit()
    con.close()
    with open(ruta + "-wal", "ab") as fh:
        fh.write(b"\x00" * 200)          # frame a medias
    j = _abre(ruta)
    j.initialize()
    assert j._connect().execute(
        "SELECT v FROM meta WHERE k='bueno'").fetchone()[0] == "si"
    j.close()


# ── ③ LO QUE NO SE PUEDE ACREDITAR ─────────────────────────────────────────

@pytest.mark.parametrize("como,error", [
    ("cero", C.PreflightRejected),
    ("sqlite_vacia", C.SchemaIndeterminate),
    ("meta_vacio", C.SchemaIndeterminate),
    ("meta_only", C.SchemaIndeterminate),
    ("ajena", C.SchemaIndeterminate),
])
def test_05_lo_que_no_se_acredita_se_rechaza_sin_tocarlo(tmp_path, como, error):
    ruta = str(tmp_path / "x.sqlite")
    if como == "cero":
        open(ruta, "wb").close()
    elif como == "sqlite_vacia":
        sqlite3.connect(ruta).close()
        con = sqlite3.connect(ruta)
        con.execute("CREATE TABLE t (x)")
        con.execute("DROP TABLE t")       # SQLite válida y sin tablas
        con.commit(); con.close()
    elif como == "meta_vacio":
        con = sqlite3.connect(ruta)
        con.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
        con.commit(); con.close()
    elif como == "meta_only":
        con = sqlite3.connect(ruta)
        con.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
        con.execute("INSERT INTO meta VALUES ('durable_v','3')")
        con.execute("INSERT INTO meta VALUES ('pepper_check','x')")
        con.execute("INSERT INTO meta VALUES ('generation','1')")
        con.commit(); con.close()
    else:
        con = sqlite3.connect(ruta)
        con.execute("CREATE TABLE cosas_de_otro (x)")
        con.execute("INSERT INTO cosas_de_otro VALUES (1)")
        con.commit(); con.close()
    antes = _huella(ruta)
    j = _abre(ruta)
    with pytest.raises(error):
        j.initialize()
    j.close()
    assert _huella(ruta) == antes, f"tocó una base `{como}`"


def test_06_un_sello_sin_forma_no_acredita(tmp_path):
    """`meta_only` con `durable_v=3` es el caso que separa «tiene sello» de «es
    lo que dice»: el manifiesto exige objetos y columnas, no una fila."""
    ruta = str(tmp_path / "y.sqlite")
    con = sqlite3.connect(ruta)
    con.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
    con.execute("INSERT INTO meta VALUES ('durable_v','3')")
    con.commit(); con.close()
    j = _abre(ruta)
    with pytest.raises(C.SchemaIndeterminate):
        j.initialize()
    assert j._veredicto[0] == "indeterminada"
    j.close()


# ── ④ CREACIÓN CRASH-SAFE ──────────────────────────────────────────────────

def test_07_una_creacion_interrumpida_no_deja_media_base(tmp_path):
    """Se mata el proceso a mitad de crear. Lo que quede NO puede ser un fichero
    a medias en la ruta final: o está entera o no está."""
    ruta = str(tmp_path / "coordination.sqlite")
    guion = (
        "import sys, os, threading;"
        f"sys.path.insert(0, {os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))!r});"
        "import coordination as C;"
        "threading.Timer(0.02, lambda: os._exit(1)).start();"
        f"j = C.Journal({ruta!r}, pepper={PEPPER!r}, lane_ledgers={LANES!r}, recipient_resolver=censo, grammar=GRAMATICA);"
        "j.initialize()")
    subprocess.run([sys.executable, "-c", guion], timeout=60, check=False)
    if not os.path.exists(ruta):
        return                        # murió antes de publicar: correcto
    j = _abre(ruta)
    assert j.initialize() == C.DURABLE_V     # publicada ⇒ completa y acreditable
    j.close()


def test_08_dos_inicializadores_convergen(tmp_path):
    """Publicación no-clobber: uno crea y el otro adopta. Con `rename` el segundo
    pisaba la base del primero."""
    ruta = str(tmp_path / "coordination.sqlite")
    salidas, errores = [], []

    def arranca():
        try:
            j = C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
            salidas.append(j.initialize())
            j.close()
        except Exception as e:                     # se REPORTA, no se traga
            errores.append(f"{type(e).__name__}: {e}")

    hilos = [threading.Thread(target=arranca) for _ in range(2)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(timeout=60)
    assert errores == [], errores
    assert salidas == [C.DURABLE_V, C.DURABLE_V]
    j = _abre(ruta)
    j.initialize()
    assert j.stored_durable_v() == C.DURABLE_V
    assert j._connect().execute("SELECT COUNT(*) c FROM meta").fetchone()["c"] > 0
    j.close()


# ── ⑤ IDENTIDAD DEL FICHERO ────────────────────────────────────────────────

def test_09_un_reemplazo_del_MISMO_tamano_y_otro_inode_se_caza(tmp_path):
    """El tamaño coincide y el contenido también podría: lo que cambia es el
    OBJETO. Sin latir (dev, inode), la reconexión de un hilo nuevo aterriza en el
    fichero de otro sin que nada lo diga."""
    ruta, _ = _base(tmp_path)
    j = _abre(ruta)
    j.initialize()                             # clasifica, valida y late la identidad
    gemelo = ruta + ".gemelo"
    shutil.copy2(ruta, gemelo)
    os.replace(gemelo, ruta)                   # mismo tamaño, INODE distinto
    j.close()                                  # fuerza reconexión de esta hebra
    with pytest.raises(C.IdentityChanged):
        j._connect()


def test_10_un_byte_mutado_con_mismo_tamano_y_mtime_no_pasa_por_igual(tmp_path):
    """La comparación no puede descansar en tamaño+mtime: los dos se conservan y
    el contenido no. Por eso el inventario lleva SHA-256."""
    ruta, _ = _base(tmp_path, modo="DELETE")
    st = os.stat(ruta)
    with open(ruta, "r+b") as fh:
        # En la CABECERA: mutar el último byte podía caer en una página sin usar
        # y no delatarse — el test habría medido la suerte del offset.
        fh.seek(20)
        b = fh.read(1)
        fh.seek(20)
        fh.write(bytes([b[0] ^ 0xFF]))
    os.utime(ruta, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert os.stat(ruta).st_size == st.st_size
    assert os.stat(ruta).st_mtime_ns == st.st_mtime_ns
    j = _abre(ruta)
    with pytest.raises(C.JournalError):        # corrupta o indeterminada
        j.initialize()
    j.close()


def test_11_dos_hilos_y_un_reemplazo_en_medio(tmp_path):
    """Cada conexión NUEVA revalida: el segundo hilo no puede heredar el permiso
    del primero sobre un fichero que ya no es el mismo."""
    ruta, _ = _base(tmp_path)
    j = _abre(ruta)
    j.initialize()
    gemelo = ruta + ".g"
    shutil.copy2(ruta, gemelo)
    os.replace(gemelo, ruta)
    fallo = []

    def otro_hilo():
        try:
            j._connect()
            fallo.append("el hilo nuevo se conectó al fichero reemplazado")
        except C.IdentityChanged:
            pass
    h = threading.Thread(target=otro_hilo)
    h.start(); h.join(timeout=30)
    assert fallo == [], fallo


# ── ⑥ SYMLINKS Y SIDECARS ──────────────────────────────────────────────────

@pytest.mark.parametrize("pieza", ["", "-wal"])
def test_12_un_symlink_se_rechaza(tmp_path, pieza):
    ruta, _ = _base(tmp_path, modo="DELETE" if pieza == "" else "WAL")
    destino = str(tmp_path / "otro.bin")
    with open(destino, "wb") as fh:
        fh.write(b"x" * 100)
    if pieza == "":
        os.unlink(ruta)
        os.symlink(destino, ruta)
    else:
        if os.path.exists(ruta + pieza):
            os.unlink(ruta + pieza)
        os.symlink(destino, ruta + pieza)
    j = _abre(ruta)
    with pytest.raises(C.PreflightRejected):
        j.initialize()
    j.close()


def test_13_un_sidecar_que_aparece_a_mitad_hace_la_foto_inestable(tmp_path):
    """Aparecer o desaparecer un `-wal` entre las dos fotos no es un detalle: la
    base que clasificamos no sería la que hay."""
    ruta, _ = _base(tmp_path, modo="DELETE")
    original = C._copiar_a
    estado = {"n": 0}

    def copiar_y_sabotear(base, destino):
        original(base, destino)
        estado["n"] += 1
        if estado["n"] <= C.PREFLIGHT_INTENTOS:
            with open(base + "-wal", "wb") as fh:
                fh.write(b"\x00" * 32)      # aparece un sidecar
    C._copiar_a = copiar_y_sabotear
    try:
        j = _abre(ruta)
        with pytest.raises(C.PreflightUnstable):
            j.initialize()
        j.close()
    finally:
        C._copiar_a = original


def test_14_un_escritor_activo_se_declara_y_no_se_le_gana_a_reintentos(tmp_path):
    """Acotado: 3 intentos o 2 s. Un escritor activo es un estado del mundo, y la
    respuesta correcta es decirlo, no bloquearse intentando."""
    ruta, _ = _base(tmp_path, modo="DELETE")
    original = C._copiar_a

    def copiar_y_tocar(base, destino):
        original(base, destino)
        with open(base, "r+b") as fh:
            fh.seek(0)
            fh.write(b"S")                  # cambia SIEMPRE
    C._copiar_a = copiar_y_tocar
    try:
        t0 = time.time()
        j = _abre(ruta)
        with pytest.raises(C.PreflightUnstable):
            j.initialize()
        assert time.time() - t0 < 30, "se quedó peleando con el escritor"
        j.close()
    finally:
        C._copiar_a = original


# ── ⑦ CONTROL POSITIVO DEL MECANISMO ───────────────────────────────────────

def test_15_la_copia_db_mas_wal_SIN_shm_recupera_una_futura(tmp_path):
    """⊕ del preflight entero: sobre una base futura con WAL sin checkpoint, la
    versión que se lee sale del WAL. Si copiáramos sólo el `.db`, el veredicto
    se tomaría sobre datos viejos; si copiáramos el `-shm`, arrastraríamos el
    estado de otro proceso."""
    ruta, _ = _base(tmp_path, modo="WAL")
    viva = sqlite3.connect(ruta)
    viva.execute("PRAGMA journal_mode=WAL")
    viva.execute("UPDATE meta SET v=? WHERE k='durable_v'", (str(C.DURABLE_V + 5),))
    viva.commit()                   # el sello nuevo vive en el -wal, SIN checkpoint
    assert os.path.exists(ruta + "-wal")
    j = _abre(ruta)
    assert j._veredicto is None
    with pytest.raises(C.SchemaTooNew):
        j.initialize()
    assert j._veredicto[0] == "futura" and j._veredicto[1] == C.DURABLE_V + 5, (
        "el veredicto se tomó sin recuperar el WAL")
    j.close()
    viva.close()


@pytest.mark.parametrize("version", [1, 2, C.DURABLE_V])
def test_16_los_manifiestos_acreditan_cada_forma(tmp_path, version):
    """El manifiesto es lo que separa «dice ser v2» de «es v2»."""
    if version == C.DURABLE_V:
        ruta, _ = _base(tmp_path)
        j = _abre(ruta)
        j.initialize()
        assert j._veredicto[0] == "conocida" \
            and j._veredicto[1] == C.DURABLE_V
        j.close()
        return
    from .test_migracion_v1_a_v2 import _fixture_v1, _fixture_v2
    if version == 1:
        ruta, *_ = _fixture_v1(tmp_path, "B")
    else:
        ruta, *_ = _fixture_v2(tmp_path)
    j = _abre(ruta)
    clase, v, detalle = (j._preflight(), j._veredicto)[1]
    assert clase == "conocida", detalle
    assert v == version
    j.close()


def test_17_una_forma_incompleta_que_dice_v3_no_acredita(tmp_path):
    """Se le quita UNA tabla a una v3 legítima: el sello sigue diciendo 3 y la
    forma ya no lo sostiene."""
    ruta, _ = _base(tmp_path, modo="DELETE")
    con = sqlite3.connect(ruta)
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("DROP TABLE event_acks")
    con.commit(); con.close()
    j = _abre(ruta)
    with pytest.raises(C.SchemaIndeterminate):
        j.initialize()
    assert "event_acks" in j._veredicto[2]
    j.close()


def test_18_stored_durable_v_no_confunde_bloqueo_con_ausencia(tmp_path):
    """Tragarse cualquier `OperationalError` convertía «no se deja leer» en «no
    tiene sello» — y «no tiene sello» invita a sellarla."""
    ruta, _ = _base(tmp_path)
    j = _abre(ruta)
    j.initialize()
    j._connect()

    class ConBloqueada:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("database is locked")
    j._local.con = ConBloqueada()
    with pytest.raises(sqlite3.OperationalError):
        j.stored_durable_v()
    j._local.con = None


# ── CIERRES SUELTOS DEL INFORME ────────────────────────────────────────────

def test_19_derived_a_explicit_con_el_mismo_nombre_ES_un_cambio(tmp_path):
    """Deja de ser un valor supuesto y pasa a ser uno declarado. Sin contarlo,
    la recarga que por fin nombra al principal se leía como «nada que hacer»."""
    j = journal(tmp_path)
    j.reload_credential_map({"cred-A": {"role": "backend", "lane": "llminbox"}})
    b1 = j.resolve_credential("cred-A")
    assert b1.principal_source == "derived_from_role" and b1.principal == "backend"
    gen = j.generation()
    assert j.reload_credential_map(
        {"cred-A": {"principal": "backend", "role": "backend",
                    "lane": "llminbox"}}) == gen + 1
    b2 = j.resolve_credential("cred-A")
    assert b2.principal_source == "explicit"
    assert b2.principal_id != b1.principal_id, "reescribió la ligadura en vez de crear"
    j.close()


@pytest.mark.parametrize("campo", ["principal", "lane"])
def test_20_la_migracion_nombra_QUE_contradice(tmp_path, campo):
    """Las v1 ya no entran en la migración: el rechazo común es anterior a
    validar cualquier contradicción de `principal` o `lane`. Ese contrato
    fail-closed es el que se debe medir hasta que exista el migrador offline.
    """
    from .test_migracion_v1_a_v2 import _fixture_v1
    ruta, b, s1, s2, ev, _ = _fixture_v1(tmp_path, "B")
    con = sqlite3.connect(ruta)
    con.execute("PRAGMA foreign_keys=OFF")
    if campo == "principal":
        con.execute("INSERT INTO principals(principal_id,principal,role,lane,"
                    "created_at) VALUES('prn_otro','otro','be','llminbox','t')")
        con.execute("UPDATE commands SET principal_id='prn_otro'")
    else:
        con.execute("UPDATE commands SET lane='carril-dos'")
    con.commit(); con.close()
    j = _abre(ruta)
    with pytest.raises(C.MigrationFailed) as exc:
        j.initialize()
    assert f"durable_v=1" in str(exc.value)
    assert "migrador offline" in str(exc.value)
    j.close()


def test_21_la_API_de_recibo_no_expone_la_clave_interna(tmp_path):
    j = journal(tmp_path)
    s = sesion(j)
    ev = j.accept_event(s.token, idempotency_key="k", intent=INTENT,
                        ledger="llminbox")
    rc = j.receipt_for_event(s.token, ev.event_id)
    assert "_transitions" not in rc
    assert "_transitions" not in j.receipt(s.token, rc["receipt_id"])
    assert j.transitions(s.token, rc["receipt_id"])        # ⊕ la vía buena sirve
    j.close()


def test_22_ligar_cred_B_invalida_la_sesion_YA_ABIERTA_de_cred_A(tmp_path):
    """Semántica, no efecto colateral: la generación es global."""
    j = journal(tmp_path)
    a = sesion(j, "cred-A", principal="p-a")
    assert j.authenticate(a.token) is not None
    j.bind_credential("cred-B", principal="p-b", role="be", lane="llminbox")
    assert j.authenticate(a.token) is None
    j.close()
