"""La composición contra Journal + gateway REALES: reinicio y pid nuevo.

Cierra el hallazgo de `MARK:astra-composicion-manual-review1837`: la clave de
idempotencia se repetía cuando la fábrica se reconstruía o cuando otro pid tomaba
el mismo `runtime_instance`, porque el contador de pasadas renacía en 0. Contra
el kernel de verdad eso no es un detalle: la PK es
`(principal_id, lane, verb, key)` —**el target no entra**— así que la primera
observación del proceso nuevo caía sobre la fila del muerto.

Aquí no se simula el veredicto: se cuenta `runtime_observations` en la BD y se
mira `replayed` que devuelve el kernel. `TestClient` basta —el socket ya tiene su
propio fichero contra `uvicorn`—; lo que se ejercita es el PROTOCOLO.
"""
from __future__ import annotations

import pytest

import process_sampler as P
import supervisor_composicion as CC
import supervisor_registro as RG
import supervisor_transport as T
from tests.native_gateway.test_fleet_runtime_gateway import (  # noqa: F401
    fleet, _auth, _counts,
)


@pytest.fixture
def sin_procesos(monkeypatch):
    """Identidad y métricas de mentira; el sujeto es la composición, no el sensor."""
    monkeypatch.setattr(P, "muestrea",
                        lambda _h: P.Muestra(viva=True, rss_bytes=10, cpu_millis=5))


def _registro(rti, *, pid, arranque, monkeypatch):
    monkeypatch.setattr(P, "adopta",
                        lambda _p: P.Handle(pid=pid, arranque=arranque, backend="ps"))
    r = RG.RegistroDeSupervision()
    r.registra(workload_id="be-01", runtime_instance=rti, pid=pid)
    return r


def _fabrica(client, sessions):
    """Fábrica NUEVA cada llamada: es justo lo que simula el reinicio."""
    def enviar(url, cuerpo, clave):
        respuesta = client.post(
            url, headers=_auth(sessions, "observer-a", **{"Idempotency-Key": clave}),
            json=cuerpo)
        return respuesta.status_code, respuesta.json()

    return CC.CiclosPorVinculo(
        lambda v: T.TransporteSupervisor("", "sin-uso", enviar=enviar))


@pytest.mark.parametrize("segundo_pid,segundo_arranque", [
    (4321, "t0"),      # MISMO proceso: sólo se reinició la fábrica
    (9999, "t9"),      # OTRO proceso bajo el MISMO runtime_instance
])
def test_reiniciar_la_fabrica_no_replaya_la_observacion_del_ciclo_anterior(
        fleet, sin_procesos, monkeypatch, segundo_pid, segundo_arranque):
    journal, client, sessions, _clock = fleet
    rti = sessions["target-a"].runtime_instance

    antes = _counts(journal)[0]
    r1 = _registro(rti, pid=4321, arranque="t0", monkeypatch=monkeypatch)
    f1 = _fabrica(client, sessions)
    primera = f1(r1.vinculos()[0])
    assert primera["replayed"] is False
    assert _counts(journal)[0] == antes + 1, "la primera deja UNA fila"
    nonce_1 = f1.nonce_de(r1.vinculos()[0])

    # ── REINICIO: fábrica nueva (y, en el 2º caso, otro proceso con el mismo rti)
    r2 = _registro(rti, pid=segundo_pid, arranque=segundo_arranque,
                   monkeypatch=monkeypatch)
    f2 = _fabrica(client, sessions)
    segunda = f2(r2.vinculos()[0])

    assert f2.nonce_de(r2.vinculos()[0]) != nonce_1, (
        "un ciclo nuevo estrena nonce: si lo heredara, heredaría el espacio de claves")
    # Lo que el hallazgo temía: que ESTA volviera como la de antes.
    assert segunda["replayed"] is False, (
        "🔑 replay de la observación del ciclo anterior: el supervisor informaría "
        "de un proceso viejo como si acabara de mirarlo")
    assert segunda["observation_id"] != primera["observation_id"]
    assert _counts(journal)[0] == antes + 2, "hay DOS observaciones, no una replayada"


def test_la_secuencia_se_resincroniza_con_la_autoridad_tras_el_reinicio(
        fleet, sin_procesos, monkeypatch):
    """La fábrica nueva arranca en frío (seq provisional 1) contra un target que
    ya va por 1: el kernel devuelve `409` con `max_supervisor_seq` y el
    transporte reintenta UNA vez en max+1 con clave nueva. No hay estado durable
    del supervisor: el número lo pone la única autoridad.
    """
    journal, client, sessions, _clock = fleet
    rti = sessions["target-a"].runtime_instance

    r1 = _registro(rti, pid=4321, arranque="t0", monkeypatch=monkeypatch)
    f1 = _fabrica(client, sessions)
    primera = f1(r1.vinculos()[0])

    r2 = _registro(rti, pid=4321, arranque="t0", monkeypatch=monkeypatch)
    segunda = _fabrica(client, sessions)(r2.vinculos()[0])

    assert segunda["supervisor_seq"] > primera["supervisor_seq"], (
        "la seq avanza sobre lo que dice la autoridad, no sobre memoria propia")
    assert segunda["replayed"] is False
