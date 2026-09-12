"""Transporte contra Journal + gateway REALES (no dobles).

Reusa el arnés `fleet` de `test_fleet_runtime_gateway.py` en vez de copiarlo: un
fixture duplicado diverge, y hoy ya me costó tres veces reimplementar lo que la
casa tenía. Importarlo también ata este test a la forma real del contrato.

Lo que SÍ acredita: el protocolo del cliente contra el kernel de verdad —
`409` real con `max_supervisor_seq` real, replay real por `idempotency_key`, y
el recuento REAL de filas en `runtime_observations`.

⛔ Cota declarada: `TestClient` ejercita la pila ASGI en proceso, así que NO
   ejercita `_enviar_http` (redirects, límite de lectura, quoting, supresión del
   texto de red). Eso está cubierto por los dobles y por aserciones sobre el
   fuente. Aquí se prueba el PROTOCOLO contra el Journal, no el socket.
"""
from __future__ import annotations

import pytest

import supervisor_adapter as A
import supervisor_transport as T
# El fixture viene del arnés real del gateway. pytest lo recoge del namespace.
from tests.native_gateway.test_fleet_runtime_gateway import (  # noqa: F401
    fleet, _auth, _counts, _path,
)


def _obs(target_rti: str) -> A.Observacion:
    return A.Observacion(
        workload_id="be-01", runtime_instance=target_rti,
        idempotency_key="no-usada", supervisor_seq=1,
        observation_kind="cycle_ack", reason_code="PROCESS_PRESENT",
        heartbeat_age_ms=0)


def _transporte(client, sessions, *, name="observer-a", perdidas=0):
    """Transporte real cuyo envío pasa por el TestClient del gateway.

    `perdidas` simula el caso feo de verdad: el servidor SÍ recibe y aplica la
    petición, y el cliente NO se entera. Un doble que no entregara la petición
    probaría otra cosa mucho más fácil.
    """
    estado = {"restantes": perdidas}

    def enviar(url, cuerpo, clave):
        respuesta = client.post(
            url, headers=_auth(sessions, name, **{"Idempotency-Key": clave}),
            json=cuerpo)
        if estado["restantes"] > 0:
            estado["restantes"] -= 1
            raise T.RespuestaPerdida("sin respuesta del gateway")
        return respuesta.status_code, respuesta.json()

    t = T.TransporteSupervisor("", "sin-uso", enviar=enviar)
    return t


def test_conflicto_real_trae_max_supervisor_seq_y_el_cliente_reintenta_una_vez(fleet):
    journal, client, sessions, _ = fleet
    rti = sessions["target-a"].runtime_instance

    # El observador ya dejó historial en la autoridad.
    viva = A.SecuenciaSupervisor(en_frio=True)
    t = _transporte(client, sessions)
    for _ in range(3):
        t.observar(_obs(rti), viva, clave_base=f"base-{viva.ultimo}")
    antes = _counts(journal)[0]

    # Un proceso que reinicia arranca en 1 contra un target CON historial.
    reiniciado = A.SecuenciaSupervisor(en_frio=True)
    t2 = _transporte(client, sessions)
    respuesta = t2.observar(_obs(rti), reiniciado, clave_base="tras-reinicio")

    assert respuesta["supervisor_seq"] == antes + 1, (
        "el retry tiene que entrar en max+1 real, no en 1")
    assert _counts(journal)[0] == antes + 1, "exactamente UNA fila nueva"
    assert reiniciado.ultimo == antes + 1


def test_dos_perdidas_con_el_servidor_aplicando_producen_UNA_sola_fila(fleet):
    """El caso que motivó el correctivo: el servidor aplicó, el cliente no oyó."""
    journal, client, sessions, _ = fleet
    rti = sessions["target-a"].runtime_instance
    antes = _counts(journal)[0]

    seq = A.SecuenciaSupervisor(en_frio=True)
    t = _transporte(client, sessions, perdidas=2)   # las 2 SÍ llegan al Journal

    with pytest.raises(T.RespuestaPerdida):
        t.observar(_obs(rti), seq, clave_base="perdida")
    assert t.pendiente is True, "queda pendiente: no se resolvió"

    resuelta = t.observar(_obs(rti), seq, clave_base="perdida")   # reanuda

    assert _counts(journal)[0] == antes + 1, (
        "tres envíos del MISMO hecho con la MISMA clave: D4 deduplica y sólo "
        "puede haber UNA fila")
    assert resuelta["replayed"] is True
    assert t.pendiente is False


def test_una_observacion_incompatible_con_pendiente_no_se_cuela(fleet):
    journal, client, sessions, _ = fleet
    rti = sessions["target-a"].runtime_instance
    seq = A.SecuenciaSupervisor(en_frio=True)
    t = _transporte(client, sessions, perdidas=2)
    with pytest.raises(T.RespuestaPerdida):
        t.observar(_obs(rti), seq, clave_base="p")
    antes = _counts(journal)[0]

    otra = A.Observacion(
        workload_id="be-01", runtime_instance=rti, idempotency_key="x",
        supervisor_seq=1, observation_kind="exited",
        reason_code="PROCESS_EXITED", exit_code=0)
    with pytest.raises(T.PeticionPendiente):
        t.observar(otra, seq, clave_base="p")
    assert _counts(journal)[0] == antes, "no escribe nada mientras hay pendiente"


def test_target_de_otro_carril_da_404_y_no_escribe(fleet):
    """Ajeno y desconocido son indistinguibles; ninguno se reintenta."""
    journal, client, sessions, _ = fleet
    ajeno = sessions["target-b"].runtime_instance      # otra lane
    antes = _counts(journal)[0]

    seq = A.SecuenciaSupervisor(en_frio=True)
    t = _transporte(client, sessions)
    with pytest.raises(T.ErrorDelGateway) as exc:
        t.observar(_obs(ajeno), seq, clave_base="ajeno")

    assert exc.value.status == 404
    assert exc.value.code == T.CODIGO_SUJETO_AUSENTE
    assert _counts(journal)[0] == antes, "un target ajeno no escribe observación"


def test_el_cliente_nunca_usa_status_seq_del_estado_real(fleet):
    """`GET /runtimes/{id}` trae `status_seq`; no es la secuencia del observador."""
    journal, client, sessions, _ = fleet
    rti = sessions["target-a"].runtime_instance
    seq = A.SecuenciaSupervisor(en_frio=True)
    t = _transporte(client, sessions)
    t.observar(_obs(rti), seq, clave_base="uno")

    estado = client.get(_path(sessions), headers=_auth(sessions)).json()
    assert "status_seq" in estado, "el campo confundible existe de verdad"
    assert "supervisor_seq" not in estado, (
        "si algún día apareciera aquí, este test obliga a revisar de dónde "
        "rehidrata el adaptador")
    assert seq.ultimo == 1, "la secuencia del cliente no salió de esa lectura"


# ── el ciclo del runner contra Journal + gateway REALES ─────────────────────

def _runner(client, sessions, *, perdidas=0, umbral=None, estado=None):
    import process_sampler as P
    import supervisor_runner as R
    t = _transporte(client, sessions, perdidas=perdidas)
    return R.CicloSupervisor(
        t, A.SecuenciaSupervisor(en_frio=True), workload_id="be-01",
        runtime_instance=sessions["target-a"].runtime_instance,
        umbral_rss_bytes=umbral,
        estado=estado if estado is not None else P.EstadoDelMuestreador())


def test_dos_perdidas_reanudacion_UNA_fila_y_luego_el_ciclo_sigue(fleet, monkeypatch):
    """Trayecto completo contra el Journal real: dos pérdidas con el servidor
    aplicando, el proceso CAMBIA, la reanudación confirma UNA sola fila, y el
    ciclo siguiente ya observa el cambio."""
    import process_sampler as P
    journal, client, sessions, _ = fleet
    antes = _counts(journal)[0]
    h = P.Handle(pid=1, arranque="x", backend="ps")

    monkeypatch.setattr(P, "muestrea", lambda _h: P.Muestra(viva=True, rss_bytes=10))
    c = _runner(client, sessions, perdidas=2)
    with pytest.raises(T.RespuestaPerdida):
        c.ciclo(h, clave_base="ciclo-real")
    assert c.pendiente is True

    # el proceso cambia de métricas ANTES de reanudar
    monkeypatch.setattr(P, "muestrea", lambda _h: P.Muestra(viva=True, rss_bytes=777_777))
    resuelta = c.ciclo(h, clave_base="ciclo-real")
    assert resuelta["replayed"] is True
    assert _counts(journal)[0] == antes + 1, "los 3 envíos son UNA sola observación"
    assert c.pendiente is False

    # y sólo AHORA el ciclo toma la muestra nueva
    siguiente = c.ciclo(h, clave_base="ciclo-real-2")
    assert _counts(journal)[0] == antes + 2
    assert siguiente["supervisor_seq"] > resuelta["supervisor_seq"]


def test_registra_aceptada_solo_con_fila_real_en_el_journal(fleet, monkeypatch):
    """El estado del muestreador sólo avanza si el Journal aceptó de verdad."""
    import process_sampler as P
    journal, client, sessions, _ = fleet
    est = P.EstadoDelMuestreador()
    rti = sessions["target-a"].runtime_instance
    monkeypatch.setattr(P, "muestrea",
                        lambda _h: P.Muestra(viva=True, rss_bytes=10_000_000))

    c = _runner(client, sessions, umbral=1, estado=est)
    c.ciclo(P.Handle(pid=1, arranque="x", backend="ps"), clave_base="deg-real")
    assert est.degradado_por_mi("be-01", rti, umbral_rss_bytes=1) is True
