"""Journal de SÓLO LECTURA: sirve lecturas, rechaza mutaciones, no se cae.

ADR-001 §Storage and recovery boundary: *«A read-only or unavailable journal
serves reads but rejects mutations with a distinct `JOURNAL_READ_ONLY` response
instead of entering a restart loop»*. La segunda mitad no es cosmética: este
repo ya tumbó el bus 11 veces por negarse a arrancar.
"""
from __future__ import annotations

import os
import stat

import pytest

import coordination as C
from ._arnes import GRAMATICA, INTENT, LANES, PEPPER, censo, journal, sesion


def _congelar(tmp_path):
    """Deja el directorio y sus ficheros sin permiso de escritura.

    Se congela el DIRECTORIO además del fichero a propósito: en WAL, SQLite
    necesita crear/escribir `-wal` y `-shm`, así que un fichero de sólo lectura
    dentro de un directorio escribible no reproduce el caso real (volumen `:ro`).
    """
    for nombre in os.listdir(tmp_path):
        os.chmod(tmp_path / nombre, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    os.chmod(tmp_path, stat.S_IRUSR | stat.S_IXUSR)


def _descongelar(tmp_path):
    os.chmod(tmp_path, stat.S_IRWXU)
    for nombre in os.listdir(tmp_path):
        os.chmod(tmp_path / nombre, stat.S_IRUSR | stat.S_IWUSR)


def test_con_el_journal_congelado_las_LECTURAS_siguen_y_las_mutaciones_se_niegan(tmp_path):
    j = journal(tmp_path)
    s = sesion(j)
    a = j.accept_event(s.token, idempotency_key="k", intent=INTENT, ledger="llminbox")
    j.close()
    _congelar(tmp_path)
    try:
        ro = C.Journal(str(tmp_path / "coordination.sqlite"), pepper=PEPPER,
                    lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
        # Clasificar NO es estar listo: hay que pasar por la transición, que en
        # un volumen congelado acaba en READ_ONLY_READY tras validar el pepper.
        ro.initialize()

        # ① LECTURAS: disponibles. Es la mitad que no puede perderse.
        assert ro.stored_durable_v() == C.DURABLE_V
        assert ro.receipt_for_event(s.token, a.event_id)["current_state"] == "accepted"
        assert ro.authenticate(s.token) is not None
        assert ro.pending_outbox(s.token) == 1

        # ② MUTACIONES: error PROPIO y distinguible, no un OperationalError suelto.
        with pytest.raises(C.JournalReadOnly):
            ro.accept_event(s.token, idempotency_key="k2", intent=INTENT, ledger="l")
        with pytest.raises(C.JournalReadOnly):
            ro.mark_indexed(s.token, a.event_id)
        with pytest.raises(C.JournalReadOnly):
            ro.acquire_lease(s.token, "r")

        # ③ SALUD: lo dice en voz alta en vez de aparentar normalidad.
        salud = ro.health()
        assert salud["writable"] is False and salud["durable_v_stored"] == C.DURABLE_V
        ro.close()
    finally:
        _descongelar(tmp_path)


def test_al_descongelar_vuelve_a_aceptar_sin_perder_nada(tmp_path):
    """⊕ del anterior: sin esto, un journal roto de otra forma también pasaría."""
    j = journal(tmp_path)
    s = sesion(j)
    a = j.accept_event(s.token, idempotency_key="k", intent=INTENT, ledger="llminbox")
    j.close()
    _congelar(tmp_path)
    _descongelar(tmp_path)
    j2 = C.Journal(str(tmp_path / "coordination.sqlite"), pepper=PEPPER,
                    lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
    j2.initialize()
    assert j2.health()["writable"] is True
    assert j2.receipt_for_event(s.token, a.event_id)["current_state"] == "accepted"
    b = j2.accept_event(s.token, idempotency_key="k2",
                        intent={**INTENT, "head": "otro"}, ledger="llminbox")
    assert b.replayed is False
    assert j2._connect().execute("SELECT COUNT(*) c FROM events").fetchone()["c"] == 2
    j2.close()
