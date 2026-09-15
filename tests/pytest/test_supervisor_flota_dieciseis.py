"""Ensayo operativo de supervisión externa sobre dieciséis procesos propios.

Los dieciséis procesos son hijos creados por este test y se registran mediante
su ``pid`` y arranque. El sensor de existencia sigue siendo real; sólo se
normaliza el RSS de procesos vivos para que la clasificación de recurso sea
determinista en CI y no dependa de la memoria disponible del runner.
"""
from __future__ import annotations

import subprocess
import sys
import time

import pytest

import process_sampler as P
import supervisor_composicion as CC
import supervisor_periodico as SP
import supervisor_registro as RG
import supervisor_transport as T


LANES = ("lane-a", "lane-b")
PER_LANE = 8
TOTAL = len(LANES) * PER_LANE
UMBRAL_RSS = 1_000


class Gateway:
    """Transporte doble que conserva el lane y la credencial usados."""

    def __init__(self) -> None:
        self.envios: list[dict] = []

    def transporte_de(self, vinculo: RG.Vinculo) -> T.TransporteSupervisor:
        lane = vinculo.workload_id.split("/", 1)[0]
        token = f"token-{lane}"

        def enviar(url: str, cuerpo: dict, clave: str):
            objetivo = url.split("/runtimes/", 1)[1].split("/observations", 1)[0]
            self.envios.append({
                "runtime_instance": objetivo,
                "workload_id": vinculo.workload_id,
                "lane": lane,
                "token": token,
                "kind": cuerpo["observation_kind"],
                "seq": cuerpo["supervisor_seq"],
                "clave": clave,
            })
            return (202, {
                "observation_id": f"obs-{objetivo}-{cuerpo['supervisor_seq']}",
                "workload_id": vinculo.workload_id,
                "runtime_instance": objetivo,
                "supervisor_seq": cuerpo["supervisor_seq"],
                "replayed": False,
            })

        return T.TransporteSupervisor(f"http://{lane}", token, enviar=enviar)


def _hijo() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _adopta_pronto(pid: int) -> P.Handle:
    limite = time.monotonic() + 5.0
    while True:
        try:
            return P.adopta(pid)
        except P.ProcesoDesconocido:
            if time.monotonic() >= limite:
                raise
            time.sleep(0.02)


@pytest.fixture
def flota_dieciseis(monkeypatch):
    procesos: dict[str, subprocess.Popen] = {}
    propios: set[int] = set()
    degradados: set[int] = set()
    registro = RG.RegistroDeSupervision()
    gateway = Gateway()

    for lane in LANES:
        for i in range(PER_LANE):
            rti = f"rti-{lane}-{i}"
            proceso = _hijo()
            procesos[rti] = proceso
            handle = _adopta_pronto(proceso.pid)
            propios.add(handle.pid)
            if i == 0:
                degradados.add(handle.pid)
            registro.registra(
                workload_id=f"{lane}/workload-{i}",
                runtime_instance=rti,
                pid=proceso.pid,
            )

    fase = {"nombre": "degrada"}
    real_muestrea = P.muestrea

    def muestrea_controlado(handle: P.Handle) -> P.Muestra:
        muestra = real_muestrea(handle)
        if not muestra.viva:
            return muestra
        rss = UMBRAL_RSS * 2 if fase["nombre"] == "degrada" \
            and handle.pid in degradados else 1
        return P.Muestra(viva=True, cpu_millis=muestra.cpu_millis, rss_bytes=rss)

    monkeypatch.setattr(P, "muestrea", muestrea_controlado)
    ciclos = CC.CiclosPorVinculo(gateway.transporte_de,
                                 umbral_rss_bytes=UMBRAL_RSS)
    periodico = SP.SupervisorPeriodico(registro, ciclos)

    try:
        yield {
            "procesos": procesos,
            "propios": propios,
            "degradados": degradados,
            "fase": fase,
            "registro": registro,
            "gateway": gateway,
            "periodico": periodico,
        }
    finally:
        for proceso in procesos.values():
            if proceso.poll() is None:
                proceso.kill()
            proceso.wait(timeout=10)


def test_dieciseis_procesos_propios_se_observan_por_lane_y_recuperan(
        flota_dieciseis):
    f = flota_dieciseis
    registro, gateway = f["registro"], f["gateway"]
    periodico, fase = f["periodico"], f["fase"]

    assert len(registro) == TOTAL
    assert {v.pid for v in registro.vinculos()} == f["propios"]

    primera = periodico.vuelta()
    assert primera.observados == [
        f"rti-{lane}-{i}" for lane in LANES for i in range(PER_LANE)]
    assert not primera.hubo_incidencias
    inicial = gateway.envios[-TOTAL:]
    assert sum(e["kind"] == "resource_degraded" for e in inicial) == 2
    assert sum(e["kind"] == "cycle_ack" for e in inicial) == TOTAL - 2
    assert {e["runtime_instance"] for e in inicial
            if e["kind"] == "resource_degraded"} == {
                "rti-lane-a-0", "rti-lane-b-0"}

    fase["nombre"] = "recupera"
    segunda = periodico.vuelta()
    assert not segunda.hubo_incidencias
    recuperacion = gateway.envios[-TOTAL:]
    assert sum(e["kind"] == "resource_recovered" for e in recuperacion) == 2
    assert {e["runtime_instance"] for e in recuperacion
            if e["kind"] == "resource_recovered"} == {
                "rti-lane-a-0", "rti-lane-b-0"}

    for envio in gateway.envios:
        partes = envio["runtime_instance"].split("-")
        lane = "-".join(partes[1:3])
        assert envio["runtime_instance"].startswith(f"rti-{lane}-")
        assert envio["token"] == f"token-{lane}"
    assert {e["token"] for e in gateway.envios
            if e["runtime_instance"].startswith("rti-lane-a-")} == {"token-lane-a"}
    assert {e["token"] for e in gateway.envios
            if e["runtime_instance"].startswith("rti-lane-b-")} == {"token-lane-b"}


def test_parada_real_de_cuatro_emite_exited_y_conserva_los_otros(
        flota_dieciseis):
    f = flota_dieciseis
    periodico, gateway = f["periodico"], f["gateway"]
    periodico.vuelta()
    f["fase"]["nombre"] = "recupera"
    periodico.vuelta()

    muertos = ["rti-lane-a-1", "rti-lane-a-2",
               "rti-lane-b-1", "rti-lane-b-2"]
    for rti in muertos:
        proceso = f["procesos"][rti]
        proceso.kill()
        proceso.wait(timeout=10)

    antes = len(gateway.envios)
    resultado = periodico.vuelta()
    nuevos = gateway.envios[antes:]
    assert not resultado.hubo_incidencias
    assert len(nuevos) == TOTAL
    assert {e["runtime_instance"] for e in nuevos if e["kind"] == "exited"} == set(muertos)
    assert sum(e["kind"] == "exited" for e in nuevos) == 4
    assert sum(e["kind"] != "exited" for e in nuevos) == TOTAL - 4
    assert {v.pid for v in f["registro"].vinculos()} == f["propios"]
