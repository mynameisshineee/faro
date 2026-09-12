"""El registro sólo conoce lo que alguien le dio: ni descubre ni adivina.

Sin procesos de verdad: `adopta` se sustituye. Lo que se prueba es la política
del registro, no el sensor —que ya tiene su fichero— y así esto corre sin crear
un solo hijo.
"""
from __future__ import annotations

import pytest

import process_sampler as P
import supervisor_registro as RG


def _handle(pid, arranque="t0"):
    return P.Handle(pid=pid, arranque=arranque, backend="ps")


@pytest.fixture
def adopta_falsa(monkeypatch):
    """`adopta` que responde por pid, sin tocar la máquina."""
    vistos = []

    def adopta(pid):
        vistos.append(pid)
        return _handle(pid)

    monkeypatch.setattr(P, "adopta", adopta)
    return vistos


def test_registrar_ata_la_IDENTIDAD_no_solo_el_numero(adopta_falsa):
    r = RG.RegistroDeSupervision()
    v = r.registra(workload_id="be-01", runtime_instance="rti-a", pid=4321)
    assert v.pid == 4321 and v.handle.arranque == "t0"
    assert adopta_falsa == [4321], "la identidad se captura AL REGISTRAR"
    assert r.vinculos() == (v,) and "rti-a" in r


def test_el_mismo_objetivo_dos_veces_se_rechaza(adopta_falsa):
    r = RG.RegistroDeSupervision()
    r.registra(workload_id="be-01", runtime_instance="rti-a", pid=1)
    with pytest.raises(RG.YaRegistrado):
        r.registra(workload_id="be-01", runtime_instance="rti-a", pid=2)
    assert len(r) == 1


def test_el_mismo_PROCESO_bajo_otro_nombre_se_rechaza(adopta_falsa):
    """Dos vínculos al mismo pid serían dos observadores del mismo hecho, con
    dos secuencias distintas sobre un solo proceso."""
    r = RG.RegistroDeSupervision()
    r.registra(workload_id="be-01", runtime_instance="rti-a", pid=7)
    with pytest.raises(RG.YaRegistrado, match="ya está registrado"):
        r.registra(workload_id="be-02", runtime_instance="rti-b", pid=7)
    assert len(r) == 1


@pytest.mark.parametrize("fallo", [
    P.ProcesoDesconocido("no existe"),
    P.SensorNoDisponible("sin permiso"),
])
def test_si_no_se_puede_adoptar_NO_queda_registro_a_medias(fallo, monkeypatch):
    """Un vínculo sin identidad sería un objetivo que PARECE supervisado."""
    def adopta(_pid):
        raise fallo

    monkeypatch.setattr(P, "adopta", adopta)
    r = RG.RegistroDeSupervision()
    with pytest.raises(type(fallo)):
        r.registra(workload_id="be-01", runtime_instance="rti-a", pid=9)
    assert len(r) == 0 and r.vinculos() == ()


def test_olvidar_es_explicito_y_solo_saca_lo_pedido(adopta_falsa):
    r = RG.RegistroDeSupervision()
    r.registra(workload_id="be-01", runtime_instance="rti-a", pid=1)
    b = r.registra(workload_id="be-02", runtime_instance="rti-b", pid=2)
    fuera = r.olvida("rti-a")
    assert fuera.runtime_instance == "rti-a"
    assert r.vinculos() == (b,), "el otro vínculo no se toca"
    with pytest.raises(RG.NoRegistrado):
        r.olvida("rti-a")


def test_un_proceso_VIVO_que_nadie_registro_es_INVISIBLE(adopta_falsa):
    """La propiedad central, por conducta: el registro no tiene ninguna vía de
    descubrimiento, así que un proceso real y sano —este mismo intérprete— no
    aparece por su cuenta. Se afirma sobre el CONTENIDO del registro, no sobre
    el texto del fuente: una aserción textual ya me salió mal dos veces hoy.
    """
    import os
    r = RG.RegistroDeSupervision()
    assert r.vinculos() == () and len(r) == 0
    r.registra(workload_id="be-01", runtime_instance="rti-a", pid=1)
    pids = {v.pid for v in r.vinculos()}
    assert pids == {1}, "sólo entra lo que se registró"
    assert os.getpid() not in pids, "un proceso sano no entra solo"
