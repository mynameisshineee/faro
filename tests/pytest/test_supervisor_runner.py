"""El runner: `registra_aceptada` SÓLO tras un 202 validado, y no antes."""
from __future__ import annotations

import subprocess
import sys
import time

import pytest

import process_sampler as P
import supervisor_adapter as A
import supervisor_runner as R
import supervisor_transport as T


@pytest.fixture
def ensayo():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            P.adopta(proc.pid); break
        except P.ProcesoDesconocido:
            time.sleep(0.05)
    try:
        yield proc
    finally:
        proc.kill(); proc.wait(timeout=5)


class Servidor:
    def __init__(self, guion):
        self.guion = list(guion); self.intentos = []

    def __call__(self, url, cuerpo, clave):
        self.intentos.append((clave, cuerpo["supervisor_seq"]))
        a = self.guion.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


def _ok(seq, **c):
    base = {"observation_id": "o", "workload_id": "w", "runtime_instance": "r",
            "supervisor_seq": seq, "replayed": False}
    base.update(c)
    return (202, base)


def _ciclo(srv, estado=None, umbral=None):
    t = T.TransporteSupervisor("http://x", "tok", enviar=srv)
    return R.CicloSupervisor(t, A.SecuenciaSupervisor(en_frio=True),
                             workload_id="w", runtime_instance="r",
                             umbral_rss_bytes=umbral, estado=estado)


def test_una_degradacion_ACEPTADA_habilita_la_recuperacion(ensayo):
    est = P.EstadoDelMuestreador()
    h = P.adopta(ensayo.pid)
    c1 = _ciclo(Servidor([_ok(1)]), estado=est, umbral=1)      # todo supera 1 byte
    c1.ciclo(h, clave_base="k1")
    assert est.degradado_por_mi("w", "r", umbral_rss_bytes=1) is True


def test_una_perdida_NO_registra_nada(ensayo):
    """Si el envío no se resuelve, el ciclo no puede dar por hecha la aceptación."""
    est = P.EstadoDelMuestreador()
    h = P.adopta(ensayo.pid)
    c = _ciclo(Servidor([T.RespuestaPerdida("t"), T.RespuestaPerdida("t")]),
               estado=est, umbral=1)
    with pytest.raises(T.RespuestaPerdida):
        c.ciclo(h, clave_base="k")
    assert est.degradado_por_mi("w", "r", umbral_rss_bytes=1) is False, (
        "una pérdida no acredita degradación registrada")


def test_un_202_INVALIDO_no_registra(ensayo):
    est = P.EstadoDelMuestreador()
    h = P.adopta(ensayo.pid)
    c = _ciclo(Servidor([(202, {"observation_id": None, "workload_id": "w",
                                "runtime_instance": "r", "supervisor_seq": 1,
                                "replayed": False})]), estado=est, umbral=1)
    with pytest.raises(T.RespuestaInvalida):
        c.ciclo(h, clave_base="k")
    assert est.degradado_por_mi("w", "r", umbral_rss_bytes=1) is False


def test_un_rechazo_del_gateway_no_registra(ensayo):
    est = P.EstadoDelMuestreador()
    h = P.adopta(ensayo.pid)
    c = _ciclo(Servidor([(404, {"code": T.CODIGO_SUJETO_AUSENTE})]),
               estado=est, umbral=1)
    with pytest.raises(T.ErrorDelGateway):
        c.ciclo(h, clave_base="k")
    assert est.degradado_por_mi("w", "r", umbral_rss_bytes=1) is False


def test_un_umbral_distinto_no_cierra_la_degradacion(ensayo):
    """Antes se llamaba `..._degrada_y_luego_recupera` y NUNCA llamaba a c2.ciclo:
    sólo comprobaba un cambio de umbral. El nombre prometía un trayecto que el
    cuerpo no recorría."""
    est = P.EstadoDelMuestreador()
    h = P.adopta(ensayo.pid)
    _ciclo(Servidor([_ok(1)]), estado=est, umbral=1).ciclo(h, clave_base="k1")
    assert est.degradado_por_mi("w", "r", umbral_rss_bytes=10**15) is False


def test_trayecto_REAL_degrade_recovered_cycle_ack(ensayo, monkeypatch):
    """El trayecto que el nombre viejo prometía, ahora recorrido de verdad.

    Mismo umbral en los tres ciclos; lo que cambia es el RSS medido.
    """
    est = P.EstadoDelMuestreador()
    h = P.adopta(ensayo.pid)
    UMBRAL = 1_000_000
    emitidas = []

    # La real se guarda en un LOCAL, no en un atributo del módulo: `monkeypatch`
    # revierte lo que parchea, pero no borra un nombre que yo AÑADO — y
    # `P._observacion_real` sobrevivía al test y quedaba en el módulo para el
    # resto de la sesión (NIT 2 de @qa). Un cierre no ensucia nada.
    real = P.observacion_de

    def espia(muestra, **kw):
        obs = real(muestra, **kw)
        emitidas.append(obs.observation_kind)
        return obs

    monkeypatch.setattr(P, "observacion_de", espia)

    # 1) RSS por encima -> degrada
    monkeypatch.setattr(P, "muestrea", lambda _h: P.Muestra(viva=True, rss_bytes=UMBRAL * 2))
    _ciclo(Servidor([_ok(1)]), estado=est, umbral=UMBRAL).ciclo(h, clave_base="k1")
    # 2) RSS realmente bajo, MISMO umbral -> recupera
    monkeypatch.setattr(P, "muestrea", lambda _h: P.Muestra(viva=True, rss_bytes=10))
    _ciclo(Servidor([_ok(1)]), estado=est, umbral=UMBRAL).ciclo(h, clave_base="k2")
    # 3) sigue bajo -> ya no repite recuperación
    _ciclo(Servidor([_ok(1)]), estado=est, umbral=UMBRAL).ciclo(h, clave_base="k3")

    assert emitidas == ["resource_degraded", "resource_recovered", "cycle_ack"]


# ── el atasco: dos pérdidas y el proceso cambia ─────────────────────────────

def test_dos_perdidas_y_metricas_CAMBIADAS_no_atascan_el_ciclo(ensayo, monkeypatch):
    """🔻 Reproducido antes de corregir: `ciclo()` siempre muestreaba, así que el
    payload nuevo chocaba con el pendiente y no había salida."""
    est = P.EstadoDelMuestreador()
    h = P.adopta(ensayo.pid)
    monkeypatch.setattr(P, "muestrea", lambda _h: P.Muestra(viva=True, rss_bytes=10))
    srv = Servidor([T.RespuestaPerdida("t"), T.RespuestaPerdida("t")])
    t = T.TransporteSupervisor("http://x", "tok", enviar=srv)
    c = R.CicloSupervisor(t, A.SecuenciaSupervisor(en_frio=True),
                          workload_id="w", runtime_instance="r", estado=est)
    with pytest.raises(T.RespuestaPerdida):
        c.ciclo(h, clave_base="k")
    assert c.pendiente is True

    # el proceso cambia de métricas ENTRE ciclos
    monkeypatch.setattr(P, "muestrea", lambda _h: P.Muestra(viva=True, rss_bytes=999_999))
    srv.guion.append(_ok(1, replayed=True))
    resuelta = c.ciclo(h, clave_base="k")          # reanuda, NO muestrea
    assert resuelta["replayed"] is True
    assert c.pendiente is False


def test_al_reanudar_NO_se_muestrea_el_proceso(ensayo, monkeypatch):
    est = P.EstadoDelMuestreador()
    h = P.adopta(ensayo.pid)
    veces = []
    monkeypatch.setattr(P, "muestrea",
                        lambda _h: (veces.append(1), P.Muestra(viva=True, rss_bytes=10))[1])
    srv = Servidor([T.RespuestaPerdida("t"), T.RespuestaPerdida("t")])
    t = T.TransporteSupervisor("http://x", "tok", enviar=srv)
    c = R.CicloSupervisor(t, A.SecuenciaSupervisor(en_frio=True),
                          workload_id="w", runtime_instance="r", estado=est)
    with pytest.raises(T.RespuestaPerdida):
        c.ciclo(h, clave_base="k")
    assert len(veces) == 1
    srv.guion.append(_ok(1, replayed=True))
    c.ciclo(h, clave_base="k")
    assert len(veces) == 1, "la reanudación no vuelve a mirar el proceso"


def test_el_proceso_DESAPARECE_durante_la_reanudacion(ensayo, monkeypatch):
    """La reanudación no depende del proceso: el hecho pendiente es del pasado."""
    est = P.EstadoDelMuestreador()
    h = P.adopta(ensayo.pid)
    monkeypatch.setattr(P, "muestrea", lambda _h: P.Muestra(viva=True, rss_bytes=10))
    srv = Servidor([T.RespuestaPerdida("t"), T.RespuestaPerdida("t")])
    t = T.TransporteSupervisor("http://x", "tok", enviar=srv)
    c = R.CicloSupervisor(t, A.SecuenciaSupervisor(en_frio=True),
                          workload_id="w", runtime_instance="r", estado=est)
    with pytest.raises(T.RespuestaPerdida):
        c.ciclo(h, clave_base="k")
    def muerto(_h):
        raise P.ProcesoDesconocido("se fue")
    monkeypatch.setattr(P, "muestrea", muerto)
    srv.guion.append(_ok(1, replayed=True))
    assert c.ciclo(h, clave_base="k")["replayed"] is True


def test_un_rechazo_definitivo_NO_deja_pendiente(ensayo, monkeypatch):
    est = P.EstadoDelMuestreador()
    h = P.adopta(ensayo.pid)
    monkeypatch.setattr(P, "muestrea", lambda _h: P.Muestra(viva=True, rss_bytes=10))
    c = _ciclo(Servidor([(404, {"code": T.CODIGO_SUJETO_AUSENTE})]), estado=est)
    with pytest.raises(T.ErrorDelGateway):
        c.ciclo(h, clave_base="k")
    assert c.pendiente is False, "un 404 cierra el asunto, no lo deja en el aire"


def test_una_identidad_CAMBIADA_al_muestrear_sube_y_no_llega_al_transporte(
        ensayo, monkeypatch):
    """La mitad del MUESTREO también sube, y eso no lo cubría nadie.

    Sustituye a `test_el_runner_no_traga_excepciones`, que afirmaba una
    propiedad del CÓDIGO midiéndola sobre el TEXTO (`"except" not in
    getsource(ciclo)`): pasaba de milagro —el docstring dice «excepciones»,
    que no contiene «except»— y se habría puesto rojo por editar la prosa.
    Es el mismo germen que mató la guarda textual del muestreador; lo cazó
    `@qa` en mi fichero (NIT 1 de MARK:qa-go-bf3f186-ef80dd3-a33a4c4-20260908).

    Los otros siete casos inyectan por el TRANSPORTE y ninguno hace que
    `muestrea` LEVANTE: la propagación de la mitad del sensor estaba sin
    interrogar, que es justo lo que el test textual aparentaba cubrir.
    """
    srv = Servidor([])                    # guion VACÍO: llegar aquí sería el fallo
    c = _ciclo(srv)

    def identidad_cambiada(_h):
        raise P.IdentidadDeProcesoCambiada("el pid se reusó entre ciclos")

    monkeypatch.setattr(P, "muestrea", identidad_cambiada)
    with pytest.raises(P.IdentidadDeProcesoCambiada):
        c.ciclo(P.adopta(ensayo.pid), clave_base="k")
    assert srv.intentos == [], "no se puede observar un proceso cuya identidad cambió"
    assert c.pendiente is False, "nada se envió: nada puede quedar pendiente"
