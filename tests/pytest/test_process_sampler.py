"""Muestreo externo sobre un proceso de ENSAYO creado por el propio test.

⛔ Nada de esto toca la flota: el único proceso observado lo crea y lo termina
   este fichero. No hay kill/señal a nada ajeno, ni siquiera lectura de otros
   pids salvo el `os.getpid()` del propio pytest, que sólo se lee.
"""
from __future__ import annotations

import os
import ctypes
import errno
import io
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import process_sampler as P
import supervisor_adapter as A


@pytest.fixture
def ensayo():
    """Un proceso propio, vivo hasta que el test lo termina."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):                      # espera a que `ps` lo vea
        if P._ps(proc.pid, "lstart=") is not None:
            break
        time.sleep(0.05)
    try:
        yield proc
    finally:
        proc.kill()                          # sólo el que YO creé
        proc.wait(timeout=5)


# ── identidad: pid + arranque, y el reuso se rechaza ────────────────────────

def test_adopta_un_proceso_vivo_y_lo_identifica_por_pid_y_arranque(ensayo):
    h = P.adopta(ensayo.pid)
    assert h.pid == ensayo.pid
    assert h.arranque.strip(), "el arranque tiene que venir del sistema"


def test_un_pid_con_OTRO_arranque_es_otro_proceso(ensayo):
    """El reuso de pid es el modo de fallo que este handle existe para cerrar."""
    real = P.adopta(ensayo.pid)
    falso = P.Handle(pid=ensayo.pid, arranque="Thu Jan  1 00:00:00 1970")
    assert falso.arranque != real.arranque
    with pytest.raises(P.IdentidadDeProcesoCambiada, match="otro proceso"):
        P.muestrea(falso)


def test_pid_terminado_no_es_adoptable(ensayo):
    ensayo.kill()
    ensayo.wait(timeout=5)
    with pytest.raises(P.ProcesoDesconocido):
        P.adopta(ensayo.pid)


@pytest.mark.parametrize("pid", [0, -1, True, "123", 1 << 31])
def test_handle_rechaza_pid_invalido(pid):
    with pytest.raises(ValueError):
        P.Handle(pid=pid, arranque="x")


# ── muestreo: hechos typados, nunca prosa ───────────────────────────────────

def test_muestra_de_proceso_vivo_trae_medidas_typadas_y_acotadas(ensayo):
    m = P.muestrea(P.adopta(ensayo.pid))
    assert m.viva is True
    assert m.rss_bytes is None or (type(m.rss_bytes) is int
                                   and 0 <= m.rss_bytes <= A.MAX_RSS_BYTES)
    assert m.cpu_millis is None or (type(m.cpu_millis) is int
                                     and 0 <= m.cpu_millis <= A.MAX_CPU_MILLIS)


def test_proceso_terminado_se_ve_ausente(ensayo):
    h = P.adopta(ensayo.pid)
    ensayo.kill(); ensayo.wait(timeout=5)
    for _ in range(50):
        if not P.muestrea(h).viva:
            break
        time.sleep(0.05)
    assert P.muestrea(h).viva is False


# ── traducción al vocabulario cerrado ───────────────────────────────────────

def test_proceso_vivo_produce_cycle_ack(ensayo):
    obs = P.observacion_de(
        P.muestrea(P.adopta(ensayo.pid)), workload_id="w1",
        runtime_instance="r1", idempotency_key="k", seq=1)
    assert obs.observation_kind == "cycle_ack"
    assert obs.reason_code == "PROCESS_PRESENT"


def test_por_encima_del_umbral_produce_resource_degraded(ensayo):
    obs = P.observacion_de(
        P.muestrea(P.adopta(ensayo.pid)), workload_id="w1",
        runtime_instance="r1", idempotency_key="k", seq=1,
        umbral_rss_bytes=1)                  # cualquier proceso lo supera
    assert obs.observation_kind == "resource_degraded"
    assert obs.reason_code == "MEMORY_SATURATED"
    assert type(obs.rss_bytes) is int


def test_ausencia_confirmada_acredita_exited_con_exit_code_None():
    """🔻 Corregido: el kernel admite `exit_code` NULL (coordination.py:1739).

    Antes me negaba a emitir parada sin código, que era MÁS estricto que el
    contrato. `None` dice «se fue»; lo prohibido sigue siendo inventar un `0`,
    que diría «se fue BIEN» sin que nadie lo mirara.
    """
    obs = P.observacion_de(P.Muestra(viva=False), workload_id="w1",
                           runtime_instance="r1", idempotency_key="k", seq=1)
    assert obs.observation_kind == "exited"
    assert obs.reason_code == "PROCESS_EXITED"
    assert obs.exit_code is None, "no se fabrica un 0"


def test_ausente_CON_exit_code_observado_produce_exited():
    obs = P.observacion_de(
        P.Muestra(viva=False, exit_code=137), workload_id="w1",
        runtime_instance="r1", idempotency_key="k", seq=1)
    assert obs.observation_kind == "exited"
    assert obs.reason_code == "PROCESS_EXITED"
    assert obs.exit_code == 137


# ── lo que el módulo NO puede hacer ─────────────────────────────────────────

def test_el_camino_de_muestreo_lee_identidad_y_contadores_sin_senalar(monkeypatch):
    """Comprueba las llamadas del camino real, sin buscar palabras en comentarios."""
    llamadas = []
    def leer(argv, **opciones):
        llamadas.append(argv)
        assert opciones.get("shell", False) is False
        return SimpleNamespace(returncode=0, stdout="S 0:00.03 4", stderr="")
    monkeypatch.setattr(P, "_backend_de_plataforma", lambda: "libproc")
    monkeypatch.setattr(P, "_LIBPROC", _libproc_doble())
    monkeypatch.setattr(P.subprocess, "run", leer)
    monkeypatch.setattr(P.os, "kill", lambda *a: pytest.fail("el sensor envió una señal"))
    muestra = P.muestrea(P.adopta(123))
    assert muestra == P.Muestra(viva=True, cpu_millis=30, rss_bytes=4096)
    assert llamadas == [["ps", "-p", "123", "-o", "stat=,time=,rss="]]


# ── #1 avería del sensor ≠ ausencia ─────────────────────────────────────────

def test_timeout_del_sensor_no_acredita_parada(monkeypatch):
    import subprocess as sp
    def revienta(*a, **k):
        raise sp.TimeoutExpired(cmd="ps", timeout=5)
    monkeypatch.setattr(P.subprocess, "run", revienta)
    with pytest.raises(P.SensorNoDisponible, match="no respondió"):
        P.muestrea(P.Handle(pid=os.getpid(), arranque="test", backend="ps"))


def test_fila_ilegible_no_acredita_parada(ensayo, monkeypatch):
    h = P.adopta(ensayo.pid)
    monkeypatch.setattr(P, "_ps", lambda pid, campos: "basura-sin-columnas")
    with pytest.raises(P.SensorNoDisponible, match="no interpretable"):
        P.muestrea(h)


def test_ps_indisponible_no_es_ProcesoDesconocido(monkeypatch):
    """La distinción que importa: no se puede confundir con ausencia."""
    def revienta(*a, **k):
        raise OSError("no hay ps")
    monkeypatch.setattr(P.subprocess, "run", revienta)
    with pytest.raises(P.SensorNoDisponible):
        P.muestrea(P.Handle(pid=os.getpid(), arranque="test", backend="ps"))
    assert not issubclass(P.SensorNoDisponible, P.ProcesoDesconocido)


# ── #2 tiempos: fracciones, días, cotas, negativos ──────────────────────────

@pytest.mark.parametrize("texto,millis", [
    ("0:00.03", 30),                       # macOS, con FRACCIÓN: antes daba None
    ("0:01", 1000),
    ("1:00", 60_000),
    ("1:00:00", None),                   # supera la cota del contrato
    ("0:16.00", 16_000),
    ("0:29.97", 29_970),                 # no perder un ms por redondeo binario
])
def test_tiempo_de_cpu_se_interpreta(texto, millis):
    assert P._tiempo_a_millis(texto) == millis


def test_un_dia_no_son_sesenta_horas(monkeypatch):
    """El plegado viejo trataba los días como otro grupo de 60.

    Se amplía sólo la cota del conversor para observar la aritmética; la prueba
    separada de fuera-de-rango conserva el límite real del contrato.
    """
    monkeypatch.setattr(P, "MAX_CPU_MILLIS", 200_000_000)
    assert P._tiempo_a_millis("1-02:03:04.05") == 93_784_050


@pytest.mark.parametrize("texto", ["", "  ", "-1:00", "x:y", "1.5:00:00", None,
                                  "0:60", "1:60:00", "0:1e3", "0:+1"])
def test_tiempos_invalidos_dan_None(texto):
    assert P._tiempo_a_millis(texto) is None


def test_cpu_fuera_de_rango_NO_se_recorta_al_tope():
    """🩸 Recortar daría un número legal e indistinguible de una medida real.

    `MAX_CPU_MILLIS` son ~16,7 min: cualquier proceso longevo lo supera. Un
    `1_000_000` recortado no se distingue de un `1_000_000` medido, así que el
    campo se omite en vez de mentir con un valor plausible.
    """
    assert P._tiempo_a_millis("999-00:00:00") is None
    assert P._tiempo_a_millis("1:00:00") is None      # 1 h ya pasa el tope


def test_proceso_vivo_da_una_medida_de_cpu_REAL_no_None(ensayo):
    """Antes el test admitía None y tapaba justo el bug de la fracción."""
    m = P.muestrea(P.adopta(ensayo.pid))
    assert type(m.cpu_millis) is int and m.cpu_millis >= 0


# ── #3 la cota del arranque se declara, no se disfraza ──────────────────────

def test_la_precision_del_arranque_esta_declarada():
    assert 0 < P.PRECISION_ARRANQUE_S <= 1


# ── #5 transición a resource_recovered ──────────────────────────────────────

@pytest.mark.parametrize("rss_recuperada", [0, 1])
def test_tras_degradar_la_bajada_emite_resource_recovered(rss_recuperada):
    """Sólo una medida que baja hasta el MISMO umbral acredita recuperación."""
    est = P.EstadoDelMuestreador()
    alta = P.Muestra(viva=True, rss_bytes=10_000_000)
    deg = P.observacion_de(alta, workload_id="w", runtime_instance="r",
                           idempotency_key="k", seq=1, umbral_rss_bytes=1,
                           estado=est)
    assert deg.observation_kind == "resource_degraded"
    est.registra_aceptada(deg, umbral_rss_bytes=1)
    baja = P.Muestra(viva=True, rss_bytes=rss_recuperada)
    rec = P.observacion_de(baja, workload_id="w", runtime_instance="r",
                           idempotency_key="k", seq=2,
                           umbral_rss_bytes=1, estado=est)
    assert rec.observation_kind == "resource_recovered"
    assert rec.reason_code == "HEARTBEAT_RECOVERED"


def test_sin_degradacion_MIA_previa_no_se_emite_recuperacion():
    """🔑 Evita convertir una recuperación manual PENDIENTE en un éxito.

    `resource_recovered` mapea a `fresh` y sacaría también de `recovering`.
    """
    est = P.EstadoDelMuestreador()
    obs = P.observacion_de(P.Muestra(viva=True, rss_bytes=10), workload_id="w",
                           runtime_instance="r", idempotency_key="k", seq=1,
                           estado=est)
    assert obs.observation_kind == "cycle_ack"


def test_la_recuperacion_se_emite_UNA_vez():
    est = P.EstadoDelMuestreador()
    alta = P.Muestra(viva=True, rss_bytes=10)
    baja = P.Muestra(viva=True, rss_bytes=1)
    d = P.observacion_de(alta, workload_id="w", runtime_instance="r",
                         idempotency_key="k", seq=1, umbral_rss_bytes=1, estado=est)
    assert d.observation_kind == "resource_degraded"
    est.registra_aceptada(d, umbral_rss_bytes=1)
    r = P.observacion_de(baja, workload_id="w", runtime_instance="r",
                         idempotency_key="k", seq=2, umbral_rss_bytes=1, estado=est)
    assert r.observation_kind == "resource_recovered"
    est.registra_aceptada(r, umbral_rss_bytes=1)
    tercera = P.observacion_de(baja, workload_id="w", runtime_instance="r",
                               idempotency_key="k", seq=3, umbral_rss_bytes=1,
                               estado=est)
    assert tercera.observation_kind == "cycle_ack", "no se repite la recuperación"


# ── #1 clasificación exacta de ps ───────────────────────────────────────────

def test_campo_invalido_de_ps_no_es_ausencia():
    """rc=1 CON diagnóstico es sensor roto; rc=1 limpio es ausencia."""
    with pytest.raises(P.SensorNoDisponible, match="no acredita"):
        P._ps(os.getpid(), "campoquenoexiste=")


def test_ausencia_real_si_devuelve_None():
    with patch.object(P.subprocess, "run", return_value=SimpleNamespace(
            returncode=1, stdout="", stderr="")):
        assert P._ps(123, "lstart=") is None


@pytest.mark.parametrize("texto", ["nan", "inf", "-inf", "0:nan", "0:inf"])
def test_nan_e_inf_no_revientan_y_dan_None(texto):
    assert P._tiempo_a_millis(texto) is None


def test_rss_negativo_no_llega_a_la_muestra(ensayo, monkeypatch):
    h = P.adopta(ensayo.pid)
    monkeypatch.setattr(P, "_ps", lambda pid, c: "S 0:01.00 -5")
    assert P.muestrea(h).rss_bytes is None, "una medida negativa no es una medida"


# ── #2 el estado va por objetivo y sólo con aceptaciones ────────────────────

def test_el_estado_no_se_reutiliza_entre_objetivos():
    est = P.EstadoDelMuestreador()
    alta = P.Muestra(viva=True, rss_bytes=10_000_000)
    dA = P.observacion_de(alta, workload_id="wA", runtime_instance="rA",
                          idempotency_key="k", seq=1, umbral_rss_bytes=1, estado=est)
    est.registra_aceptada(dA, umbral_rss_bytes=1)
    obsB = P.observacion_de(alta, workload_id="wB", runtime_instance="rB",
                            idempotency_key="k", seq=1, umbral_rss_bytes=1,
                            estado=est)
    assert obsB.observation_kind == "resource_degraded", (
        "la degradación de A no puede recuperar a B")


def test_un_intento_NO_aceptado_no_habilita_recuperacion():
    """Sólo un 202 del Journal cuenta: registrar intentos falsearía el ciclo."""
    est = P.EstadoDelMuestreador()
    alta = P.Muestra(viva=True, rss_bytes=10_000_000)
    P.observacion_de(alta, workload_id="w", runtime_instance="r",
                     idempotency_key="k", seq=1, umbral_rss_bytes=1, estado=est)
    # no se llama registra_aceptada: el Journal nunca lo aceptó
    obs = P.observacion_de(alta, workload_id="w", runtime_instance="r",
                           idempotency_key="k", seq=2, umbral_rss_bytes=1,
                           estado=est)
    assert obs.observation_kind == "resource_degraded"


def test_sin_rss_conocida_no_se_emite_recuperacion():
    est = P.EstadoDelMuestreador()
    d = P.observacion_de(P.Muestra(viva=True, rss_bytes=10_000_000),
                         workload_id="w", runtime_instance="r",
                         idempotency_key="k", seq=1, umbral_rss_bytes=1, estado=est)
    est.registra_aceptada(d, umbral_rss_bytes=1)
    obs = P.observacion_de(P.Muestra(viva=True, rss_bytes=None), workload_id="w",
                           runtime_instance="r", idempotency_key="k", seq=2,
                           umbral_rss_bytes=1, estado=est)
    assert obs.observation_kind == "cycle_ack", (
        "sin medir el recurso no se declara que bajó")


def test_umbral_distinto_no_acredita_recuperacion():
    est = P.EstadoDelMuestreador()
    alta = P.Muestra(viva=True, rss_bytes=10_000_000)
    d = P.observacion_de(alta, workload_id="w", runtime_instance="r",
                         idempotency_key="k", seq=1, umbral_rss_bytes=1, estado=est)
    est.registra_aceptada(d, umbral_rss_bytes=1)
    obs = P.observacion_de(P.Muestra(viva=True, rss_bytes=5), workload_id="w",
                           runtime_instance="r", idempotency_key="k", seq=2,
                           umbral_rss_bytes=99, estado=est)
    assert obs.observation_kind == "cycle_ack"


# ── #3 la resolución es un DATO, no un comentario ───────────────────────────

def test_la_resolucion_del_arranque_es_consultable():
    assert isinstance(P.PRECISION_ARRANQUE_S, float)
    assert P.RESOLUCION_SUBSEGUNDO_CONFIGURADA is (P.PRECISION_ARRANQUE_S < 1.0)


def test_dos_procesos_reales_conservan_sus_identidades_al_muestrear():
    procs = [subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"],
                              stdout=subprocess.DEVNULL) for _ in range(2)]
    try:
        handles = [P.adopta(p.pid) for p in procs]
        assert len({(h.pid, h.arranque) for h in handles}) == 2
        for h in handles:
            assert P.adopta(h.pid) == h
            assert P.muestrea(h).viva is True
    finally:
        for p in procs:
            p.kill(); p.wait(timeout=5)


# ── el bug determinista de Linux: backends que no se mezclan ────────────────

def test_adopta_recuerda_su_backend(ensayo):
    h = P.adopta(ensayo.pid)
    assert h.backend in P.BACKENDS


def test_handle_rechaza_un_backend_desconocido():
    with pytest.raises(ValueError, match="backend desconocido"):
        P.Handle(pid=1, arranque="x", backend="inventado")


def test_adopta_y_muestrea_no_mezclan_FORMATOS(ensayo, monkeypatch):
    """🔻 El defecto de a31f695, reproducido SIN depender de la plataforma.

    Se simulan los dos sensores a la vez: /proc devuelve ticks y ps un lstart
    humano. Antes, `adopta` guardaba los ticks y `muestrea` comparaba además el
    lstart contra ellos, así que TODO proceso vivo con /proc caía en
    IdentidadDeProcesoCambiada. Un skip de plataforma no lo detectaba.
    """
    monkeypatch.setattr(P, "_backend_de_plataforma", lambda: "proc")
    monkeypatch.setattr(P, "_arranque_proc", lambda pid: "8846320")
    monkeypatch.setattr(P, "_ps",
                        lambda pid, campos: ("Tue Sep  8 18:54:07 2026"
                                             if "lstart" in campos else "S 0:01.00 4096"))
    h = P.adopta(ensayo.pid)
    assert h.backend == "proc" and h.arranque == "8846320"
    m = P.muestrea(h)                       # antes: IdentidadDeProcesoCambiada
    assert m.viva is True


def test_sensor_indisponible_no_es_ausencia_ni_dispara_fallback(monkeypatch):
    h = P.Handle(pid=123, arranque="8846320", backend="proc")
    def no_disponible(pid):
        raise P.SensorNoDisponible("sin permiso")
    monkeypatch.setattr(P, "_arranque_proc", no_disponible)
    monkeypatch.setattr(P, "_ps", lambda *a: pytest.fail("fallback indebido"))
    with pytest.raises(P.SensorNoDisponible, match="sin permiso"):
        P.muestrea(h)


def test_identidad_que_cambia_ENTRE_las_dos_lecturas_invalida_la_muestra(ensayo, monkeypatch):
    h = P.adopta(ensayo.pid)
    lecturas = [h.arranque, "9999999.000000"]      # cambia justo tras medir
    monkeypatch.setattr(P, "_arranque_por_backend",
                        lambda pid, backend: lecturas.pop(0) if lecturas else None)
    with pytest.raises(P.IdentidadDeProcesoCambiada, match="mientras se medía"):
        P.muestrea(h)


# ── macOS SÍ tiene microsegundos ────────────────────────────────────────────

@pytest.mark.skipif(P._LIBPROC is None, reason="sin libproc (no es macOS)")
def test_libproc_da_arranque_con_microsegundos():
    valor = P._arranque_libproc(os.getpid())
    assert valor is not None and "." in valor
    assert len(valor.split(".")[1]) == 6, "microsegundos, no segundos"


@pytest.mark.skipif(P._LIBPROC is None, reason="sin libproc (no es macOS)")
def test_macos_adopta_con_libproc_y_unidad_de_microsegundos():
    assert P.RESOLUCION_SUBSEGUNDO_CONFIGURADA is True
    assert P.PRECISION_ARRANQUE_S <= 1e-6
    assert P.adopta(os.getpid()).backend == "libproc"


@pytest.mark.parametrize("codigo", [0, errno.EPERM, errno.EACCES, errno.EIO, errno.EINVAL])
def test_error_libproc_no_acredita_parada(codigo, monkeypatch):
    def falla(*args):
        ctypes.set_errno(codigo)
        return 0
    monkeypatch.setattr(P, "_LIBPROC", SimpleNamespace(proc_pidinfo=falla))
    with pytest.raises(P.SensorNoDisponible):
        P.muestrea(P.Handle(pid=123, arranque="1000.000001", backend="libproc"))


def test_libproc_esrch_acredita_ausencia_y_no_inventa_exit_code(monkeypatch):
    def ausente(*args):
        ctypes.set_errno(errno.ESRCH)
        return 0
    monkeypatch.setattr(P, "_LIBPROC", SimpleNamespace(proc_pidinfo=ausente))
    muestra = P.muestrea(P.Handle(pid=123, arranque="1000.000001", backend="libproc"))
    assert muestra.viva is False and muestra.exit_code is None


def _libproc_doble(*, pid=123, segundos=1000, micros=123456, tamano=None):
    def lee(_pid, _flavor, _arg, buffer, capacidad):
        info = ctypes.cast(buffer, ctypes.POINTER(P._ProcBsdInfo)).contents
        info.pbi_pid = pid
        info.pbi_start_tvsec = segundos
        info.pbi_start_tvusec = micros
        return capacidad if tamano is None else tamano
    return SimpleNamespace(proc_pidinfo=lee)


@pytest.mark.parametrize("valores", [
    {"pid": 999}, {"segundos": 0}, {"micros": 1_000_000}, {"tamano": 8},
])
def test_libproc_rechaza_buffer_o_identidad_incoherentes(valores, monkeypatch):
    monkeypatch.setattr(P, "_LIBPROC", _libproc_doble(**valores))
    with pytest.raises(P.SensorNoDisponible):
        P._arranque_libproc(123)


def test_cambio_de_arranque_en_mismo_segundo_se_detecta_por_dato(monkeypatch):
    monkeypatch.setattr(P, "_backend_de_plataforma", lambda: "libproc")
    monkeypatch.setattr(P, "_LIBPROC", _libproc_doble(micros=123456))
    handle = P.adopta(123)
    assert handle.arranque == "1000.123456"
    monkeypatch.setattr(P, "_LIBPROC", _libproc_doble(micros=123457))
    with pytest.raises(P.IdentidadDeProcesoCambiada):
        P.muestrea(handle)


@pytest.mark.parametrize("codigo", [errno.EACCES, errno.EIO])
def test_procfs_error_de_lectura_no_acredita_parada(codigo):
    with patch.object(P.Path, "is_file", return_value=True), patch(
            "builtins.open", side_effect=OSError(codigo, "fallo de lectura")):
        with pytest.raises(P.SensorNoDisponible):
            P.muestrea(P.Handle(pid=123, arranque="1000", backend="proc"))


def test_procfs_no_montado_no_acredita_parada():
    with patch.object(P.Path, "is_file", return_value=False):
        with pytest.raises(P.SensorNoDisponible, match="procfs"):
            P._arranque_proc(123)


def test_procfs_enoent_si_acredita_ausencia():
    with patch.object(P.Path, "is_file", return_value=True), patch(
            "builtins.open", side_effect=FileNotFoundError(errno.ENOENT, "ausente")):
        assert P._arranque_proc(123) is None


@pytest.mark.parametrize("datos", [b"", b"incompleto", b"123 (x) R 1", b"x" * 16_384])
def test_procfs_ilegible_no_acredita_parada(datos):
    with patch.object(P.Path, "is_file", return_value=True), patch(
            "builtins.open", return_value=io.BytesIO(datos)):
        with pytest.raises(P.SensorNoDisponible):
            P._arranque_proc(123)


def test_procfs_extrae_starttime_con_nombre_que_contiene_parentesis():
    datos = b"123 (worker (con espacios)) " + b" ".join([b"S", *([b"0"] * 18), b"8846320"])
    with patch.object(P.Path, "is_file", return_value=True), patch(
            "builtins.open", return_value=io.BytesIO(datos)):
        assert P._arranque_proc(123) == "8846320"


@pytest.mark.parametrize("rc,out,err", [
    (0, "S 0:00 1", "diagnóstico"), (0, "", ""), (2, "", ""),
    (-9, "", ""), (1, "", "sin permiso"), (0, "fila1\nfila2", ""),
])
def test_ps_error_o_respuesta_ambigua_no_acredita_ausencia(rc, out, err, monkeypatch):
    monkeypatch.setattr(P.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=rc, stdout=out, stderr=err))
    with pytest.raises(P.SensorNoDisponible):
        P._ps(123, "stat=,time=,rss=")


def test_zombie_es_parada_aunque_el_pid_siga_presente(monkeypatch):
    monkeypatch.setattr(P, "_arranque_por_backend", lambda *a: "1000.000001")
    monkeypatch.setattr(P, "_ps", lambda *a: "Z 0:00.03 0")
    muestra = P.muestrea(P.Handle(pid=123, arranque="1000.000001", backend="libproc"))
    assert muestra.viva is False and muestra.exit_code is None
    obs = P.observacion_de(muestra, workload_id="w", runtime_instance="r", idempotency_key="k", seq=1)
    assert obs.observation_kind == "exited" and obs.exit_code is None


@pytest.mark.parametrize("valores", [
    {"viva": None}, {"viva": 1}, {"viva": True, "rss_bytes": -1},
    {"viva": True, "cpu_millis": True}, {"viva": False, "exit_code": "0"},
])
def test_muestra_invalida_no_puede_convertirse_en_observacion(valores):
    with pytest.raises(P.SensorNoDisponible):
        P.Muestra(**valores)
