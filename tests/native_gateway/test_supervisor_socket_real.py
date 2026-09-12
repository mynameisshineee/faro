"""`_enviar_http` contra un SOCKET real y el servidor ASGI de producción.

Cierra la cota que `test_supervisor_transport_real.py` declara en su cabecera:
allí el transporte pasa por `TestClient` (pila ASGI EN PROCESO), así que el
camino HTTP del cliente —`urllib`, cabeceras, construcción de URL, timeout,
clasificación `HTTPError` vs `URLError`— no lo ejercitaba NADIE. Aquí el
transporte se construye SIN `enviar`, o sea con `_enviar_http` de verdad, contra
`uvicorn` sirviendo `create_native_app`: el mismo servidor que corre en la
imagen, no un adaptador casero.

El socket se crea AQUÍ y se le pasa hecho a uvicorn (`run(sockets=[...])`): así
el puerto efímero se conoce ANTES de arrancar y no hay que hurgar en atributos
internos del servidor para averiguarlo.
"""
from __future__ import annotations

import socket
import threading
import time

import pytest
# Import LLANO, no `importorskip`: en un entorno sin el venv esto tiene que
# REVENTAR, no saltarse. Un skip condicionado a una dependencia ausente compra
# su propio verde, y el resto de `tests/native_gateway/` ya importa `fastapi`
# del mismo venv en la primera línea — si falta, faltan todos, y se ve.
import uvicorn

import native_gateway as G
import supervisor_adapter as A
import supervisor_transport as T
from tests.native_gateway.test_fleet_runtime_gateway import (  # noqa: F401
    fleet, _counts,
)

ARRANQUE_MAX_S = 10.0


@pytest.fixture
def servidor(fleet):
    """`create_native_app` real, sobre un socket real, en un hilo."""
    journal, _client, sessions, _clock = fleet
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    puerto = sock.getsockname()[1]

    server = uvicorn.Server(uvicorn.Config(
        G.create_native_app(journal), log_level="warning", access_log=False))
    hilo = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    # El try envuelve TAMBIÉN el arranque: si el servidor no llega a escuchar,
    # antes se salía por `pytest.fail` sin cerrar el socket ni parar el hilo.
    try:
        hilo.start()
        limite = time.monotonic() + ARRANQUE_MAX_S
        while not server.started:
            if time.monotonic() > limite or not hilo.is_alive():
                pytest.fail("el servidor ASGI no llegó a escuchar")
            time.sleep(0.02)      # espera de ARRANQUE, no una medida de tiempo
        yield f"http://127.0.0.1:{puerto}", journal, sessions
    finally:
        server.should_exit = True
        hilo.join(timeout=ARRANQUE_MAX_S)
        sock.close()
        # `join` con timeout NO falla si el hilo sigue vivo: se exige aparte,
        # o una fuga de hilo se iría en verde.
        assert not hilo.is_alive(), "el hilo del servidor ASGI no terminó"


def _obs(target_rti: str, **cambios) -> A.Observacion:
    base = dict(
        workload_id="be-01", runtime_instance=target_rti,
        idempotency_key="no-usada", supervisor_seq=1,
        observation_kind="cycle_ack", reason_code="PROCESS_PRESENT",
        heartbeat_age_ms=0)
    base.update(cambios)
    return A.Observacion(**base)


def test_el_cliente_HTTP_real_observa_por_socket_y_deja_UNA_fila(servidor):
    """El camino entero: urllib -> socket -> uvicorn -> FastAPI -> Journal."""
    base, journal, sessions = servidor
    rti = sessions["target-a"].runtime_instance
    t = T.TransporteSupervisor(base, sessions["observer-a"].token)   # SIN `enviar`
    assert t._enviar == t._enviar_http, "este test existe para ejercitar el HTTP real"

    antes = _counts(journal)[0]                      # [0] = runtime_observations
    seq = A.SecuenciaSupervisor(en_frio=True)
    respuesta = t.observar(_obs(rti), seq, clave_base="socket-1")

    assert respuesta["runtime_instance"] == rti
    assert respuesta["replayed"] is False
    assert _counts(journal)[0] == antes + 1, "exactamente UNA fila nueva por el socket"


def test_el_token_viaja_de_verdad_y_un_401_no_es_una_perdida(servidor):
    """Sin credencial válida el gateway RESPONDE; una respuesta no es silencio.

    Importa distinguirlo: `RespuestaPerdida` significa «pudo aplicarse», y un
    rechazo autenticado no es eso. Si `_enviar_http` clasificara el `HTTPError`
    como pérdida, el cliente reintentaría contra un servidor que ya dijo que no.
    """
    base, _journal, sessions = servidor
    rti = sessions["target-a"].runtime_instance
    t = T.TransporteSupervisor(base, "token-que-no-existe")
    with pytest.raises(T.ErrorDelGateway) as caso:
        t.observar(_obs(rti), A.SecuenciaSupervisor(en_frio=True), clave_base="socket-2")
    assert caso.value.status in (401, 403)
    assert t.pendiente is False, "un rechazo autenticado NO deja la petición en vuelo"


def test_nadie_escuchando_es_PERDIDA_y_no_un_rechazo():
    """Nadie acepta en ESE puerto: la petición pudo no llegar.

    Es el otro lado de la clasificación, y el que no se puede probar con un
    doble: `URLError` sólo lo levanta el socket de verdad.

    🔻 Antes esto usaba `127.0.0.1:1` «puerto reservado». Era falso y frágil:
    el 1 es un puerto ASIGNADO (tcpmux) que simplemente no suele tener a nadie
    — una suposición sobre ESTA máquina, no una garantía. Ahora el puerto se
    RESERVA aquí: se ata un socket propio y **no** se pone a escuchar, así que
    nadie más puede cogerlo y una conexión recibe RST de verdad. Se mantiene
    atado durante la petición y se cierra después.
    """
    reservado = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    reservado.bind(("127.0.0.1", 0))          # BIND sin LISTEN: nadie acepta
    puerto = reservado.getsockname()[1]
    try:
        t = T.TransporteSupervisor(f"http://127.0.0.1:{puerto}", "tok", timeout=2.0)
        with pytest.raises(T.RespuestaPerdida) as caso:
            t.observar(_obs("rti-inventado"), A.SecuenciaSupervisor(en_frio=True),
                       clave_base="socket-3")
        assert "127.0.0.1" not in str(caso.value), "el mensaje no puede filtrar la URL"
        assert str(puerto) not in str(caso.value)
        assert t.pendiente is True, "sin respuesta, la observación sigue en vuelo"
    finally:
        reservado.close()
