"""Las vueltas periódicas: qué se observa, qué se retira y qué NO se hace.

Cero procesos y cero esperas reales: el reloj y la espera se inyectan, así que
la periodicidad se COMPRUEBA en vez de aguantarla. Un test que duerme mide la
paciencia del CI.
"""
from __future__ import annotations

import pytest

import process_sampler as P
import supervisor_periodico as SP
import supervisor_registro as RG


@pytest.fixture
def registro(monkeypatch):
    monkeypatch.setattr(P, "adopta",
                        lambda pid: P.Handle(pid=pid, arranque="t0", backend="ps"))
    r = RG.RegistroDeSupervision()
    r.registra(workload_id="be-01", runtime_instance="rti-a", pid=1)
    r.registra(workload_id="be-02", runtime_instance="rti-b", pid=2)
    return r


class Reloj:
    """Reloj inyectable: avanza cuando se le dice, nunca solo."""

    def __init__(self):
        self.t = 1_000.0
        self.esperas = []

    def __call__(self):
        return self.t

    def espera(self, s):
        self.esperas.append(s)
        self.t += s


def test_una_vuelta_observa_TODOS_los_vinculos_registrados(registro):
    vistos = []
    s = SP.SupervisorPeriodico(registro, lambda v: vistos.append(v.runtime_instance))
    r = s.vuelta()
    assert vistos == ["rti-a", "rti-b"], "orden de registro, estable"
    assert r.observados == ["rti-a", "rti-b"] and not r.hubo_incidencias


def test_un_registro_VACIO_no_observa_nada():
    """Sin vínculos no hay a quién mirar: no hay descubrimiento que rellene."""
    vistos = []
    s = SP.SupervisorPeriodico(RG.RegistroDeSupervision(), lambda v: vistos.append(v))
    r = s.vuelta()
    assert vistos == [] and r.observados == [] and not r.hubo_incidencias


def test_un_vinculo_ENFERMO_no_impide_mirar_a_los_demas(registro):
    vistos = []

    SECRETO = "https://gateway.interno/obs?token=abc123"

    def ciclo(v):
        if v.runtime_instance == "rti-a":
            raise RuntimeError(f"el transporte se cayó contra {SECRETO}")
        vistos.append(v.runtime_instance)

    r = SP.SupervisorPeriodico(registro, ciclo).vuelta()
    assert vistos == ["rti-b"], "el segundo se mira igual"
    assert r.observados == ["rti-b"]
    # SÓLO el tipo: el texto es de un tercero y puede arrastrar URL o credencial.
    assert r.fallos == [("rti-a", "RuntimeError")]
    assert SECRETO not in repr(r), "el texto ajeno no puede viajar en el resultado"
    assert len(registro) == 2, "un fallo de transporte no dice NADA del proceso"


def test_identidad_CAMBIADA_retira_el_vinculo_y_no_se_readopta(registro):
    """El pid se reusó: ese proceso no es el objetivo, y NO lo re-adoptamos.

    Re-adoptar el mismo número sería inferir que el proceso nuevo «es» el
    de antes — exactamente lo que este supervisor no hace.
    """
    def ciclo(v):
        if v.runtime_instance == "rti-a":
            raise P.IdentidadDeProcesoCambiada("pid reusado")

    r = SP.SupervisorPeriodico(registro, ciclo).vuelta()
    assert r.retirados == [("rti-a", "IdentidadDeProcesoCambiada: pid reusado")]
    assert "rti-a" not in registro and len(registro) == 1
    assert [v.runtime_instance for v in registro.vinculos()] == ["rti-b"]


def test_ausencia_CONFIRMADA_retira_pero_sensor_caido_CONSERVA(registro):
    """La distinción que sostiene todo: no saber mirar no es haber muerto."""
    def ciclo(v):
        if v.runtime_instance == "rti-a":
            raise P.ProcesoDesconocido("ya no existe")
        raise P.SensorNoDisponible("sin permiso para leer")

    r = SP.SupervisorPeriodico(registro, ciclo).vuelta()
    assert [n for n, _ in r.retirados] == ["rti-a"]
    assert [n for n, _ in r.sensor_caido] == ["rti-b"]
    assert "rti-a" not in registro
    assert "rti-b" in registro, "un sensor caído NO retira al objetivo"


def test_espera_el_RESTO_del_intervalo_y_nunca_un_negativo(registro):
    """La cadencia se mide, no se sufre: reloj y espera inyectados."""
    reloj = Reloj()
    lento = {"n": 0}

    def ciclo(_v):
        lento["n"] += 1
        if lento["n"] > 2:          # la 2ª vuelta tarda MÁS que el intervalo
            reloj.t += 30.0

    s = SP.SupervisorPeriodico(registro, ciclo, intervalo_s=10.0,
                               reloj=reloj, espera=reloj.espera)
    s.corre(vueltas=3)
    assert len(reloj.esperas) == 2, "se espera ENTRE vueltas, no tras la última"
    assert reloj.esperas[0] == 10.0
    assert reloj.esperas[1] == 0.0, "una vuelta que se pasó no duerme en negativo"


def test_no_hay_ningun_verbo_que_actue_sobre_el_objetivo(registro, monkeypatch):
    """Por conducta: si el periódico tocara el proceso, esto lo cazaría.

    `os.kill` es la vía por la que se señala o se comprueba a un proceso; que
    una vuelta entera no la use es la forma observable de «sólo lee».
    """
    monkeypatch.setattr(SP.P.os, "kill",
                        lambda *a: pytest.fail("el periódico señaló a un proceso"))
    SP.SupervisorPeriodico(registro, lambda v: None).vuelta()


@pytest.mark.parametrize("malo", [
    0, -1.0,
    float("nan"),   # toda comparación con nan es FALSA: `<= 0` lo dejaba pasar
    float("inf"),   # pasaba, y dormiría para siempre
    True,           # un bool ES un int en Python: `True <= 0` es falso
    "10",
])
def test_un_intervalo_que_no_es_finito_y_positivo_se_rechaza(registro, malo):
    with pytest.raises(ValueError):
        SP.SupervisorPeriodico(registro, lambda v: None, intervalo_s=malo)


def test_pedir_cero_vueltas_se_rechaza(registro):
    with pytest.raises(ValueError):
        SP.SupervisorPeriodico(registro, lambda v: None).corre(vueltas=0)
