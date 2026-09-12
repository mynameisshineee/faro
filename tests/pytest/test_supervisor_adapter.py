"""Pruebas sintéticas del adaptador. Sin red, sin Journal, sin Docker.

Cada ⊖ ataca una prohibición de ADR-002; cada ⊕ evita que el módulo pase por
estar trabado en rojo. El control que más importa es
`test_el_vocabulario_no_es_una_copia`: si el adaptador se copiara el vocabulario
en vez de importarlo, ese test dejaría de detectar una divergencia futura, que es
el defecto exacto que motivó reescribir la propuesta.
"""
from __future__ import annotations

import pytest

import coordination as C
import supervisor_adapter as A


# ── ⊕ controles positivos ────────────────────────────────────────────────────

def test_latido_valido_sale_con_los_campos_del_kernel():
    obs = A.latido(workload_id="w1", runtime_instance="r1",
                   idempotency_key="k1", seq=1, heartbeat_age_ms=1200,
                   detector_state="viva-con-progreso")
    kwargs = obs.como_kwargs()
    assert kwargs["observation_kind"] == "cycle_ack"
    assert kwargs["reason_code"] == "PROCESS_PRESENT"
    assert kwargs["heartbeat_age_ms"] == 1200
    # los kwargs tienen que ser aceptables por la firma real del kernel
    import inspect
    firma = inspect.signature(C.Journal.record_runtime_observation).parameters
    assert set(kwargs) <= set(firma), f"campos que el kernel no acepta: {set(kwargs) - set(firma)}"


def test_degradacion_por_memoria_es_valida():
    obs = A.degradacion_por_recurso(
        workload_id="w1", runtime_instance="r1", idempotency_key="k2", seq=2,
        reason_code="MEMORY_SATURATED", rss_bytes=1_500_000_000)
    assert obs.como_kwargs()["reason_code"] == "MEMORY_SATURATED"


# ── ⊖ el vocabulario es UNO, no dos ──────────────────────────────────────────

def test_el_vocabulario_no_es_una_copia():
    """Si alguien re-declara el vocabulario en el adaptador, esto lo caza."""
    assert A.RUNTIME_OBSERVATION_KINDS is C.RUNTIME_OBSERVATION_KINDS
    assert A._OBSERVATION_REASONS is C._OBSERVATION_REASONS
    assert A.DETECTOR_STATES is C.DETECTOR_STATES
    assert A.KINDS_EMITIBLES < C.RUNTIME_OBSERVATION_KINDS


def test_los_dos_kinds_excluidos_siguen_existiendo_en_el_kernel():
    """`KINDS_RECUPERACION` es el ÚNICO literal del adaptador: 2 nombres a mano.

    Si el kernel los renombrara, la exclusión dejaría de aplicarse EN SILENCIO
    y el adaptador empezaría a emitir verbos que no son suyos. Esta guarda
    convierte ese silencio en un rojo.
    """
    assert A.KINDS_RECUPERACION <= C.RUNTIME_OBSERVATION_KINDS, (
        "el kernel renombró los verbos de recuperación: la exclusión del "
        "adaptador ya no los cubre")


# ── ⊖ las cuatro prohibiciones ───────────────────────────────────────────────

@pytest.mark.parametrize("kind,reason", [
    ("recovery_succeeded", "RECOVERY_SUCCEEDED"),
    ("recovery_failed", "RECOVERY_FAILED"),
])
def test_no_emite_resultados_de_recuperacion(kind, reason):
    """observe no implica recover: estos dos verbos no son suyos."""
    with pytest.raises(A.ObservacionInvalida, match="fuera del alcance"):
        A.valida(A.Observacion(
            workload_id="w1", runtime_instance="r1", idempotency_key="k",
            supervisor_seq=1, observation_kind=kind, reason_code=reason))


def test_no_existe_forma_de_pedir_recuperacion():
    """El adaptador no debe exponer NINGÚN camino a runtime.recover."""
    superficie = " ".join(dir(A)).lower()
    assert "recover" not in superficie.replace("recovery_succeeded", "").replace(
        "recovery_failed", "").replace("kinds_recuperacion", "")


def test_no_viajan_campos_derivados_por_el_servidor():
    obs = A.latido(workload_id="w1", runtime_instance="r1",
                   idempotency_key="k", seq=1)
    assert not (A.CAMPOS_DERIVADOS_POR_EL_SERVIDOR & set(obs.como_kwargs()))


def test_el_dataclass_no_tiene_campo_de_texto_libre():
    """Ni prosa, ni logs, ni cuota: la fila durable no los admite."""
    campos = set(A.Observacion.__dataclass_fields__)
    prohibidos = {"message", "log", "stderr", "quota", "provider", "note", "detail"}
    assert not (campos & prohibidos)


# ── ⊖ vocabulario y rangos ───────────────────────────────────────────────────

def test_reason_code_que_no_corresponde_a_su_kind():
    with pytest.raises(A.ObservacionInvalida, match="no corresponde"):
        A.valida(A.Observacion(
            workload_id="w", runtime_instance="r", idempotency_key="k",
            supervisor_seq=1, observation_kind="cycle_ack",
            reason_code="CPU_SATURATED"))


def test_detector_state_fuera_del_vocabulario_m3():
    with pytest.raises(A.ObservacionInvalida, match="M3"):
        A.latido(workload_id="w", runtime_instance="r", idempotency_key="k",
                 seq=1, detector_state="inventado")


@pytest.mark.parametrize("campo,valor", [
    ("cpu_millis", C.MAX_CPU_MILLIS + 1),
    ("rss_bytes", C.MAX_RSS_BYTES + 1),
    ("heartbeat_age_ms", C.MAX_HEARTBEAT_AGE_MS + 1),
    ("cpu_millis", -1),
])
def test_medidas_fuera_de_rango(campo, valor):
    with pytest.raises(A.ObservacionInvalida, match="fuera de"):
        A.valida(A.Observacion(
            workload_id="w", runtime_instance="r", idempotency_key="k",
            supervisor_seq=1, observation_kind="resource_degraded",
            reason_code="CPU_SATURATED", **{campo: valor}))


def test_bool_no_cuela_como_entero():
    """En Python `True` es 1: si no se excluye, un flag entra como medida."""
    with pytest.raises(A.ObservacionInvalida, match="int"):
        A.valida(A.Observacion(
            workload_id="w", runtime_instance="r", idempotency_key="k",
            supervisor_seq=1, observation_kind="resource_degraded",
            reason_code="CPU_SATURATED", cpu_millis=True))


# ── ⊖ secuencia ──────────────────────────────────────────────────────────────

def test_secuencia_es_monotona_desde_el_maximo_adoptado():
    s = A.SecuenciaSupervisor(maximo_observado=0)
    assert [s.siguiente() for _ in range(3)] == [1, 2, 3]


def test_sin_rehidratar_no_entrega_secuencia():
    """🔻 SUSTITUYE al test que FIJABA el defecto.

    La versión anterior afirmaba que reiniciar y volver a 0 era correcto
    («el kernel deduplica»). Es falso: `coordination.py:8549-8558` rechaza
    `supervisor_seq <= MAX(...)` con `ObservationSequenceConflict`, y la
    deduplicación por idempotency_key NO restaura la secuencia. Arrancar en 0
    dejaba el adaptador mudo para esa generación.
    """
    s = A.SecuenciaSupervisor()
    assert s.rehidratada is False
    with pytest.raises(A.SinRehidratar, match="no rehidratada"):
        s.siguiente()


def test_reinicio_rehidratado_continua_por_encima_del_maximo():
    """El reinicio correcto: adoptar el máximo de la autoridad y seguir."""
    viva = A.SecuenciaSupervisor(maximo_observado=0)
    for _ in range(5):
        viva.siguiente()          # la autoridad ya tiene hasta 5
    reiniciada = A.SecuenciaSupervisor()
    reiniciada.adoptar(viva.ultimo)
    assert reiniciada.siguiente() == 6, "tiene que superar el MAX ya observado"


def test_adoptar_un_maximo_menor_se_rechaza():
    """Retroceder reintroduce exactamente el conflicto que esto corrige."""
    s = A.SecuenciaSupervisor(maximo_observado=10)
    with pytest.raises(A.ObservacionInvalida, match="menor"):
        s.adoptar(4)


def test_la_rehidratacion_no_es_una_segunda_autoridad():
    """El adaptador no guarda el máximo: se lo dan. Sin adoptar, no funciona."""
    s = A.SecuenciaSupervisor()
    assert s.ultimo is None


def test_supervisor_seq_cero_rechazada():
    with pytest.raises(A.ObservacionInvalida, match="supervisor_seq"):
        A.latido(workload_id="w", runtime_instance="r", idempotency_key="k", seq=0)


def test_la_secuencia_no_se_hereda_entre_identidades():
    """`refresh_session` acuña runtime_instance nuevo (coordination.py:5866) y ése
    entra en la clave del MAX (:8551): el contador NO cruza sesiones."""
    ctx_viejo = ("rti-1", 3, "r-target", 7)
    s = A.SecuenciaSupervisor(en_frio=True, contexto=ctx_viejo)
    s.siguiente()
    s.exige_contexto(ctx_viejo)                      # misma identidad: pasa
    with pytest.raises(A.IdentidadCambiada, match="NO se hereda"):
        s.exige_contexto(("rti-2", 3, "r-target", 7))  # tras refresh: rti nuevo
