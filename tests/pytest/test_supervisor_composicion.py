"""Composición: registro + periódico + `CicloSupervisor` REAL por vínculo.

Lo que estos casos acreditan y los de `test_supervisor_periodico.py` no podían:
que la secuencia y la observación pendiente **sobreviven entre pasadas**, que un
objetivo enfermo no bloquea al otro, y que lo pendiente se REENVÍA tal cual.

Transporte REAL (`TransporteSupervisor`) con el `enviar` guionizado: el objeto
bajo prueba es el de producción; lo que se sustituye es el socket, que ya tiene
su propio fichero contra `uvicorn`. Cero procesos y cero red.
"""
from __future__ import annotations

import pytest

import process_sampler as P
import supervisor_composicion as CC
import supervisor_periodico as SP
import supervisor_registro as RG
import supervisor_transport as T


class Servidor:
    """Guion por objetivo. Anota (rti, clave, seq) de cada intento que recibe.

    El `runtime_instance` se saca de la RUTA, no del cuerpo: el transporte lo
    quita del JSON a propósito (`_cuerpo`) porque viaja en la URL. Leerlo del
    cuerpo habría reventado con `KeyError` y me habría hecho creer que el doble
    estaba mal cuando el equivocado era yo.
    """

    def __init__(self, guion_por_rti):
        self.guion = {k: list(v) for k, v in guion_por_rti.items()}
        self.intentos = []

    def __call__(self, url, cuerpo, clave):
        rti = url.split("/runtimes/", 1)[1].rsplit("/observations", 1)[0]
        # Se guarda el CUERPO COMPLETO, no sólo (rti, clave, seq): comparar tres
        # campos dejaba pasar un reenvío con el MISMO trío y otras métricas
        # dentro, que es justo lo que un reenvío no puede ser. `dict(...)`
        # porque el transporte reutiliza y muta el suyo entre intentos.
        self.intentos.append((rti, clave, dict(cuerpo)))
        a = self.guion[rti].pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    def de(self, rti):
        return [(c, cu) for r, c, cu in self.intentos if r == rti]

    def seqs(self, rti):
        return [cu["supervisor_seq"] for _c, cu in self.de(rti)]


def _ok(seq, rti):
    return (202, {"observation_id": f"o{seq}", "workload_id": "w",
                  "runtime_instance": rti, "supervisor_seq": seq, "replayed": False})


@pytest.fixture
def dos_objetivos(monkeypatch):
    monkeypatch.setattr(P, "adopta",
                        lambda pid: P.Handle(pid=pid, arranque="t0", backend="ps"))
    monkeypatch.setattr(P, "muestrea",
                        lambda _h: P.Muestra(viva=True, rss_bytes=10, cpu_millis=5))
    r = RG.RegistroDeSupervision()
    r.registra(workload_id="w", runtime_instance="rti-a", pid=1)
    r.registra(workload_id="w", runtime_instance="rti-b", pid=2)
    return r


def _composicion(srv, registro):
    ciclos = CC.CiclosPorVinculo(
        lambda v: T.TransporteSupervisor("http://x", "tok", enviar=srv))
    return ciclos, SP.SupervisorPeriodico(registro, ciclos)


def test_la_SECUENCIA_avanza_entre_pasadas_y_no_renace(dos_objetivos):
    """Si el ciclo se recreara cada vuelta, la seq volvería a 1 y el adaptador
    quedaría mudo. Aquí se exige 1,2,3 POR OBJETIVO a lo largo de tres vueltas.
    """
    srv = Servidor({"rti-a": [_ok(1, "rti-a"), _ok(2, "rti-a"), _ok(3, "rti-a")],
                    "rti-b": [_ok(1, "rti-b"), _ok(2, "rti-b"), _ok(3, "rti-b")]})
    ciclos, periodico = _composicion(srv, dos_objetivos)

    for _ in range(3):
        assert not periodico.vuelta().hubo_incidencias

    assert srv.seqs("rti-a") == [1, 2, 3], "la secuencia es POR OBJETIVO y persiste"
    assert srv.seqs("rti-b") == [1, 2, 3]
    # El nonce identifica al CICLO: no puede cambiar entre pasadas del mismo.
    v_a = [v for v in dos_objetivos.vinculos() if v.runtime_instance == "rti-a"][0]
    nonce = ciclos.nonce_de(v_a)
    claves = [c for c, _cu in srv.de("rti-a")]
    assert all(nonce in c for c in claves), "la clave lleva el nonce del ciclo"
    assert len(set(claves)) == 3, "cada observación NUEVA estrena clave"
    assert len(ciclos) == 2, "dos ciclos, creados UNA vez"


def test_un_objetivo_que_falla_NO_bloquea_al_otro_y_el_sano_sigue_avanzando(dos_objetivos):
    srv = Servidor({
        "rti-a": [T.RespuestaPerdida("sin respuesta"), T.RespuestaPerdida("sin respuesta"),
                  _ok(1, "rti-a")],
        "rti-b": [_ok(1, "rti-b"), _ok(2, "rti-b")]})
    ciclos, periodico = _composicion(srv, dos_objetivos)

    primera = periodico.vuelta()
    assert primera.observados == ["rti-b"], "el sano se observa igual"
    assert primera.fallos == [("rti-a", "RespuestaPerdida")]
    assert ciclos.pendientes() == ("rti-a",)
    assert len(dos_objetivos) == 2, "una pérdida no retira a nadie"

    segunda = periodico.vuelta()
    assert segunda.observados == ["rti-a", "rti-b"], "el enfermo se recupera solo"
    assert srv.seqs("rti-b") == [1, 2], "el sano no se enteró del problema del otro"


def test_lo_PENDIENTE_se_reenvia_TAL_CUAL_y_no_se_muestrea_de_nuevo(dos_objetivos,
                                                                    monkeypatch):
    """Dos pérdidas dejan una observación en el aire; la vuelta siguiente manda
    ESOS MISMOS bytes y esa misma clave, aunque el proceso haya cambiado de RSS.
    """
    srv = Servidor({"rti-a": [T.RespuestaPerdida("x"), T.RespuestaPerdida("x"),
                              _ok(1, "rti-a")],
                    "rti-b": [_ok(1, "rti-b"), _ok(2, "rti-b")]})
    ciclos, periodico = _composicion(srv, dos_objetivos)

    # Se CUENTAN los muestreos del objetivo A: la promesa del runner no es sólo
    # «manda los mismos bytes», es «no vuelve a mirar el proceso». Sin contarlo,
    # una implementación que remuestreara y por casualidad midiera lo mismo
    # pasaría el test igual.
    muestreos = {"n": 0}

    def cuenta(h):
        if h.pid == 1:                       # el pid de rti-a en la fixture
            muestreos["n"] += 1
        return P.Muestra(viva=True, rss_bytes=10, cpu_millis=5)

    monkeypatch.setattr(P, "muestrea", cuenta)
    periodico.vuelta()

    # El proceso cambia de métricas entre vueltas: si se remuestreara, el cuerpo
    # sería otro y la clave también.
    def cuenta_distinto(h):
        if h.pid == 1:
            muestreos["n"] += 1
        return P.Muestra(viva=True, rss_bytes=999_999, cpu_millis=77)

    monkeypatch.setattr(P, "muestrea", cuenta_distinto)
    periodico.vuelta()

    de_a = srv.de("rti-a")
    assert de_a[0] == de_a[1] == de_a[2], (
        "la reanudación reenvía la MISMA clave y el MISMO cuerpo, byte a byte")
    assert muestreos["n"] == 1, (
        "A no se vuelve a muestrear mientras su observación sigue sin resolver")
    assert ciclos.pendientes() == (), "aceptado: deja de estar pendiente"


def test_un_objetivo_RETIRADO_no_lega_su_secuencia_al_siguiente(dos_objetivos, monkeypatch):
    """Reusar `runtime_instance` para OTRO proceso no puede heredar el estado.

    La clave del ciclo es (rti, pid, arranque): un proceso distinto es un ciclo
    distinto por construcción, sin depender de que alguien limpie.
    """
    srv = Servidor({"rti-a": [_ok(1, "rti-a"), _ok(1, "rti-a")], "rti-b": [_ok(1, "rti-b")]})
    ciclos, periodico = _composicion(srv, dos_objetivos)
    periodico.vuelta()
    assert len(ciclos) == 2

    dos_objetivos.olvida("rti-a")
    # Mismo nombre, OTRO proceso: se registra por la vía PÚBLICA con otro pid y
    # otro arranque. Tocar `_por_objetivo` a mano fabricaría un estado que la
    # API no permite, y el registro es justo lo que se está probando.
    monkeypatch.setattr(P, "adopta",
                        lambda _pid: P.Handle(pid=99, arranque="t9", backend="ps"))
    nuevo = dos_objetivos.registra(workload_id="w", runtime_instance="rti-a", pid=99)

    ciclos(nuevo)
    assert srv.seqs("rti-a") == [1, 1], "empieza su secuencia, no hereda la del muerto"
    claves = [c for c, _cu in srv.de("rti-a")]
    assert claves[0] != claves[1], (
        "🔑 el proceso nuevo NO puede reusar la clave del muerto: seria un replay")
    assert ciclos.retira("rti-a") >= 1
