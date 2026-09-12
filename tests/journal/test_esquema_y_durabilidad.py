"""Esquema durable: PRAGMAs REALES, `durable_v`, esquema futuro y migración."""
from __future__ import annotations

import os
import sqlite3

import pytest

import coordination as C
from ._arnes import GRAMATICA, PEPPER, censo, journal


def test_pragmas_son_los_declarados_y_se_miden_no_se_suponen(tmp_path):
    """⊕/⊖ del contrato de durabilidad.

    Se leen los PRAGMAs de la conexión VIVA en vez de confiar en que la línea
    que los pone se ejecutó. FALSADOR: quitar `PRAGMA synchronous=FULL` del
    módulo tiene que romper esta aserción — si no la rompe, el test no mide.
    """
    j = journal(tmp_path)
    p = j._assert_pragmas()
    assert p["foreign_keys"] == 1
    assert p["journal_mode"] == "wal"
    assert p["synchronous"] == 2          # 2 == FULL
    assert p["busy_timeout"] == C.DEFAULT_BUSY_TIMEOUT_MS
    j.close()


def test_las_claves_foraneas_estan_ENCENDIDAS_de_verdad(tmp_path):
    """`PRAGMA foreign_keys` puede leer 1 y aun así no estarse aplicando si se
    puso dentro de una transacción. Aquí se prueba el EFECTO: una FK rota tiene
    que reventar."""
    j = journal(tmp_path)
    con = j._connect()
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO event_causes(event_id,cause_event_id)"
                    " VALUES('evt_inventado','evt_tampoco')")
    j.close()


def test_durable_v_queda_escrito_y_la_migracion_es_idempotente(tmp_path):
    j = journal(tmp_path)
    assert j.stored_durable_v() == C.DURABLE_V
    assert j.initialize() == C.DURABLE_V  # segunda pasada: no rompe, no duplica
    assert j.stored_durable_v() == C.DURABLE_V
    j.close()


def test_una_base_de_version_SUPERIOR_se_rechaza_sin_tocarla(tmp_path):
    """Rollback: un binario viejo NO migra hacia atrás una base nueva.

    Además se comprueba que la base no quedó tocada — el rechazo no puede ser
    «me quejo y de paso escribo».
    """
    j = journal(tmp_path)
    con = j._connect()
    con.execute("UPDATE meta SET v='99' WHERE k='durable_v'")
    j.close()

    j2 = C.Journal(str(tmp_path / "coordination.sqlite"), pepper=PEPPER, recipient_resolver=censo, grammar=GRAMATICA)
    with pytest.raises(C.SchemaTooNew):
        j2.initialize()
    # LECTURAS disponibles y salud legible: rechazar mutaciones no es apagarse.
    assert j2.health()["schema_too_new"] is True
    assert j2.stored_durable_v() == 99
    j2.close()


def test_con_esquema_futuro_tampoco_MUTA(tmp_path):
    """El guardián no está sólo en `initialize()`: cualquier mutación se niega.

    ⊖: si la comprobación viviera únicamente en el arranque, un proceso ya
    levantado seguiría escribiendo en una base que no entiende.
    """
    j = journal(tmp_path)
    j._connect().execute("UPDATE meta SET v='99' WHERE k='durable_v'")
    with pytest.raises(C.SchemaTooNew):
        j.bind_credential("cred-X", principal="be", role="be", lane="llminbox")
    j.close()


def test_un_pepper_distinto_no_fragmenta_la_identidad_en_silencio(tmp_path):
    j = journal(tmp_path)
    j.close()
    otro = C.Journal(str(tmp_path / "coordination.sqlite"), pepper=b"otro-pepper", recipient_resolver=censo, grammar=GRAMATICA)
    with pytest.raises(C.PepperMismatch):
        otro.initialize()
    otro.close()


def test_la_migracion_es_transaccional(tmp_path):
    """Si el script de esquema falla a mitad, no queda media base.

    Se fuerza el fallo con un SCHEMA saboteado y se comprueba que `durable_v`
    NO quedó escrito: la marca de versión y las tablas viajan juntas o no viajan.
    """
    ruta = str(tmp_path / "coordination.sqlite")
    original = C.SCHEMA
    try:
        C.SCHEMA = original + "\nCREATE TABLE roto (x INTEGER NOT NULL REFERENCES no_existe(y));\n" \
                              "\nINSERT INTO roto(x) VALUES (1);\n"
        j = C.Journal(ruta, pepper=PEPPER, recipient_resolver=censo, grammar=GRAMATICA)
        with pytest.raises(Exception):
            j.initialize()
        j.close()
    finally:
        C.SCHEMA = original
    # Desde M1-6/M1-7 la creación es crash-safe: una que falla NO deja fichero
    # en la ruta final. Antes quedaba media base sellable; ahora no queda nada,
    # y preguntarle a una ruta ausente es `JournalNotInitialized`, no `None`.
    j2 = C.Journal(ruta, pepper=PEPPER, recipient_resolver=censo, grammar=GRAMATICA)
    with pytest.raises(C.JournalNotInitialized):
        j2.stored_durable_v()
    assert not os.path.exists(ruta), "la creación fallida dejó un fichero a medias"
    j2.close()


def test_la_transaccion_toma_el_cerrojo_de_ESCRITURA_al_abrir(tmp_path):
    """Discrimina `BEGIN IMMEDIATE` de `BEGIN` (diferida), que es donde vive la
    idempotencia.

    Sin esto la regla era una AFIRMACIÓN: el test de 20 procesos pasa con las dos
    formas, así que no la prueba. Aquí sí: con IMMEDIATE el cerrojo de escritura
    se toma AL ABRIR, así que un segundo escritor no puede abrir la suya; con
    diferida, la segunda `BEGIN` entra tan campante y el conflicto se descubre
    tarde — que es exactamente cuando `busy_timeout` ya no reintenta
    (`SQLITE_BUSY_SNAPSHOT`).
    """
    import sqlite3
    j1 = journal(tmp_path, busy_timeout_ms=300)
    j2 = C.Journal(str(tmp_path / "coordination.sqlite"), pepper=PEPPER,
                   busy_timeout_ms=300, recipient_resolver=censo, grammar=GRAMATICA)
    j2.initialize()          # clasificar antes de tocar: ya no hay atajo
    tx = j1._tx()
    tx.__enter__()
    try:
        with pytest.raises(sqlite3.OperationalError):
            with j2._tx():
                pass
    finally:
        tx.__exit__(None, None, None)
        j1.close()
        j2.close()
