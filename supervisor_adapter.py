"""Adaptador externo del supervisor: señales de host -> observaciones del Journal único.

ADR-002 D2/D3/D4. Este módulo NO define vocabulario propio: lo IMPORTA de
`coordination`. Un vocabulario declarado en dos sitios diverge, y la primera
propuesta de este adaptador cometió exactamente ese error (inventó
verb/generation/measurements cuando el kernel ya expone observation_kind,
supervisor_seq, cpu_millis, rss_bytes, heartbeat_age_ms y exit_code).

Lo que este adaptador NO hace, por diseño y no por omisión:
  · no persiste verdad durable (D3: la autoridad es coordination.sqlite)
  · no repara: `runtime.observe` no implica `runtime.recover` (D2)
  · no envía identidad, lane, confianza ni reloj propio (D2: los deriva el
    gateway y los rechaza si se suministran)
  · no emite recovery_succeeded / recovery_failed: son el resultado de una
    recuperación que este adaptador no ejecuta y cuyo command_id no posee
"""
from __future__ import annotations

from dataclasses import dataclass

from coordination import (
    DETECTOR_STATES,
    MAX_CPU_MILLIS,
    MAX_HEARTBEAT_AGE_MS,
    MAX_RSS_BYTES,
    MAX_SUPERVISOR_SEQ,
    RUNTIME_OBSERVATION_KINDS,
    _OBSERVATION_REASONS,
)

# Los dos verbos de resultado de recuperación quedan fuera del alcance de este
# adaptador (§6 del contrato). Se derivan del kernel, no se listan a mano.
KINDS_RECUPERACION = frozenset({"recovery_succeeded", "recovery_failed"})
KINDS_EMITIBLES = RUNTIME_OBSERVATION_KINDS - KINDS_RECUPERACION

# Campos que el gateway deriva y RECHAZA si se suministran (D2), en cuerpo,
# cabeceras o query. El adaptador los trata como prohibidos en origen para que
# el rechazo no dependa de que el servidor esté bien configurado.
CAMPOS_DERIVADOS_POR_EL_SERVIDOR = frozenset({
    "principal", "observer_principal", "lane", "trust", "observed_at",
    "observer_runtime", "observer_generation", "supervisor_seq_asignado",
})

_LIMITES = {
    "cpu_millis": (0, MAX_CPU_MILLIS),
    "rss_bytes": (0, MAX_RSS_BYTES),
    "heartbeat_age_ms": (0, MAX_HEARTBEAT_AGE_MS),
    "exit_code": (-(1 << 31), (1 << 31) - 1),
}


class ObservacionInvalida(ValueError):
    """Rechazo LOCAL, antes de tocar la red."""


@dataclass(frozen=True)
class Observacion:
    """Exactamente los parámetros de `Journal.record_runtime_observation`.

    Sin campos extra: si aquí apareciera uno que la firma del kernel no acepta,
    sería un protocolo paralelo, que es lo que este adaptador existe para evitar.
    """
    workload_id: str
    runtime_instance: str
    idempotency_key: str
    supervisor_seq: int
    observation_kind: str
    reason_code: str
    detector_state: str | None = None
    cpu_millis: int | None = None
    rss_bytes: int | None = None
    heartbeat_age_ms: int | None = None
    exit_code: int | None = None

    def como_kwargs(self) -> dict:
        """Kwargs para el kernel. `recovery_command_id` NUNCA: ver cabecera."""
        d = {
            "workload_id": self.workload_id,
            "runtime_instance": self.runtime_instance,
            "idempotency_key": self.idempotency_key,
            "supervisor_seq": self.supervisor_seq,
            "observation_kind": self.observation_kind,
            "reason_code": self.reason_code,
        }
        for campo in ("detector_state", "cpu_millis", "rss_bytes",
                      "heartbeat_age_ms", "exit_code"):
            valor = getattr(self, campo)
            if valor is not None:
                d[campo] = valor
        return d


def _entero_en_rango(nombre: str, valor: int | None) -> None:
    if valor is None:
        return
    if type(valor) is not int or isinstance(valor, bool):
        raise ObservacionInvalida(f"{nombre} tiene que ser int, no {type(valor).__name__}")
    bajo, alto = _LIMITES[nombre]
    if not bajo <= valor <= alto:
        raise ObservacionInvalida(f"{nombre}={valor} fuera de [{bajo}, {alto}]")


def valida(obs: Observacion) -> Observacion:
    """Rechaza en origen lo que el kernel rechazaría, con el MISMO vocabulario.

    No es una segunda validación divergente: los conjuntos vienen importados.
    Sirve para fallar antes de gastar una petición, no para decidir en su lugar.
    """
    if obs.observation_kind in KINDS_RECUPERACION:
        raise ObservacionInvalida(
            f"{obs.observation_kind} está fuera del alcance de este adaptador: "
            "no ejecuta recuperación y no posee el command_id que la fila exige")
    if obs.observation_kind not in KINDS_EMITIBLES:
        raise ObservacionInvalida(
            f"observation_kind '{obs.observation_kind}' fuera del vocabulario")
    if obs.reason_code not in _OBSERVATION_REASONS[obs.observation_kind]:
        raise ObservacionInvalida(
            f"reason_code '{obs.reason_code}' no corresponde a "
            f"'{obs.observation_kind}'")
    if obs.detector_state is not None and obs.detector_state not in DETECTOR_STATES:
        raise ObservacionInvalida(
            f"detector_state '{obs.detector_state}' fuera del vocabulario M3")
    if (type(obs.supervisor_seq) is not int or isinstance(obs.supervisor_seq, bool)
            or not 0 < obs.supervisor_seq <= MAX_SUPERVISOR_SEQ):
        raise ObservacionInvalida("supervisor_seq fuera de rango")
    for nombre in ("cpu_millis", "rss_bytes", "heartbeat_age_ms", "exit_code"):
        _entero_en_rango(nombre, getattr(obs, nombre))
    prohibidos = CAMPOS_DERIVADOS_POR_EL_SERVIDOR & set(obs.como_kwargs())
    if prohibidos:
        raise ObservacionInvalida(
            f"campos derivados por el servidor no viajan: {sorted(prohibidos)}")
    return obs


class SinRehidratar(ObservacionInvalida):
    """Se pidió una secuencia sin haber adoptado el máximo de la autoridad."""


class IdentidadCambiada(ObservacionInvalida):
    """La sesión se refrescó: `runtime_instance` nuevo ⇒ otra tupla, otro contador."""


class ConflictoConcurrente(ObservacionInvalida):
    """Segundo 409 seguido: otro observador avanza a la vez. NO se reintenta.

    Se distingue de `ObservacionInvalida` a propósito: el primer conflicto es
    desfase de secuencia y se cura sola; el segundo es concurrencia y tiene que
    quedar VISIBLE, que es lo que impide el bucle.
    """


class SecuenciaSupervisor:
    """`supervisor_seq` monótona, REHIDRATADA desde la autoridad al arrancar.

    🔻 CORRIGE UN DEFECTO REAL de la primera versión (hallazgo de
    `@codex-llminbox`, 2026-09-08T15:36:13Z). Aquella arrancaba en 0 tras cada
    reinicio y yo lo defendía como cumplimiento de D3, apoyado en que el kernel
    deduplica. **Confundí dos mecanismos distintos**:

      · `idempotency_key` (D4) deduplica RE-ENVÍOS del mismo hecho.
      · `supervisor_seq` tiene su PROPIA guarda, y la deduplicación no la
        restaura: `coordination.py:8549-8558` hace
        `SELECT MAX(supervisor_seq) WHERE observer_principal, observer_runtime,
        observer_generation, lane, target_runtime_instance, target_generation`
        y lanza `ObservationSequenceConflict` si `supervisor_seq <= MAX`.

    ⇒ Reiniciar y volver a 1 **no era neutro: dejaba el adaptador MUDO** para esa
    generación, rechazado en cada observación. Peor: la primera versión tenía un
    test que ASEGURABA ese reinicio como correcto, o sea que fijaba el defecto.

    La rehidratación NO es una segunda autoridad durable: es preguntarle a la
    ÚNICA autoridad por dónde iba, con `runtime.read`, que ya está entre las
    capacidades pedidas. El adaptador sigue sin persistir nada propio.
    """

    def __init__(self, *, maximo_observado: int | None = None,
                 en_frio: bool = False, contexto: tuple | None = None) -> None:
        # `contexto` = la tupla de identidad a la que pertenece ESTA secuencia,
        # normalmente (observer_runtime, observer_generation, target_runtime_instance,
        # target_generation). Aviso de `@codex-llminbox` 2026-09-08T16:03:21Z,
        # verificado por mí: `refresh_session` conserva principal/lane pero acuña
        # `rti = _new_id("rti")` (coordination.py:5866), y `observer_runtime` ENTRA
        # en la clave del MAX (:8551). ⇒ tras un refresh la tupla es OTRA y el
        # contador NO se hereda. Guardarlo permite que reusar la secuencia entre
        # identidades distintas sea un error ruidoso y no una suposición callada.
        self._contexto = contexto
        """`maximo_observado` = MAX(supervisor_seq) conocido de la autoridad.

        🔻 SEGUNDA CORRECCIÓN (adjudicación `@cto` 2026-09-08T15:57:03Z, opción
        (b)). Mi versión anterior EXIGÍA rehidratar antes de observar. Con el
        mecanismo adjudicado eso es imposible por construcción: el máximo se
        aprende del `409` con `details.max_supervisor_seq`, o sea **de un intento
        rechazado**. Bloquear el primer intento bloqueaba el único camino que
        enseña el número.

        ⇒ `en_frio=True` arranca provisional en 1:
          · target SIN historial (`latest is None` en el kernel) -> pasa directo
          · target CON historial -> 409 con el máximo -> `adoptar()` -> 1 reintento

        Lo que NO vuelve: el defecto original no era empezar en 1, era **empezar
        en 1 y no enterarse nunca**. Por eso `conflicto()` es obligatorio para
        seguir y `reintentos_agotados` corta el bucle.
        """
        if maximo_observado is None:
            self._ultimo: int | None = 0 if en_frio else None
            self._provisional = en_frio
            self._reintentos = 0
            return
        self._provisional = False
        self._reintentos = 0
        if (type(maximo_observado) is not int or isinstance(maximo_observado, bool)
                or not 0 <= maximo_observado <= MAX_SUPERVISOR_SEQ):
            raise ObservacionInvalida("máximo observado inválido")
        self._ultimo = maximo_observado

    def conflicto(self, max_supervisor_seq: int | None) -> int:
        """Maneja un `409 OBSERVATION_SEQUENCE_CONFLICT` y devuelve el seq del retry.

        `max_supervisor_seq` es `details.max_supervisor_seq` del 409 (adjudicado
        por `@cto`). Un 409 SIN ese campo no se puede curar adivinando: se
        propaga, porque reintentar a ciegas es exactamente lo que esta clase
        existe para no hacer.

        UN solo reintento: si el segundo intento vuelve a conflictuar, el
        problema no es la secuencia y seguir insistiendo sería martillear.
        """
        if self._reintentos >= 1:
            raise ConflictoConcurrente(
                "segundo conflicto consecutivo tras adoptar: es concurrencia, no "
                "desfase de secuencia — queda visible y no se reintenta más")
        # El campo llega TOP-LEVEL en el cuerpo del error (`_error_response` hace
        # `error.update(details)`, no crea subobjeto). Contrato: int, acotado,
        # nunca bool ni None — adjudicado 2026-09-08T15:57Z.
        if (max_supervisor_seq is None or type(max_supervisor_seq) is not int
                or isinstance(max_supervisor_seq, bool)
                or not 1 <= max_supervisor_seq <= MAX_SUPERVISOR_SEQ):
            raise ObservacionInvalida(
                "409 sin max_supervisor_seq válido (int acotado): no se adivina "
                "la secuencia, y `status_seq` NO sirve para esto")
        self.adoptar(max_supervisor_seq)
        self._provisional = False
        self._reintentos += 1
        return self.siguiente()

    def exige_contexto(self, actual: tuple) -> None:
        """Falla si la identidad cambió bajo la secuencia (p. ej. tras refresh).

        Un `refresh_session` acuña `runtime_instance` nuevo, y ése entra en la
        clave del MAX. Seguir contando sobre la tupla vieja no rompe la escritura
        —sin historial el kernel acepta cualquier seq— pero deja al adaptador
        REPORTANDO una posición que no tiene relación con la autoridad de la
        tupla nueva. Se convierte en rojo en vez de en suposición.
        """
        if self._contexto is not None and actual != self._contexto:
            # Sin VALORES: los nombres de la tupla bastan para diagnosticar y
            # no publican identidad de nadie en un log.
            raise IdentidadCambiada(
                "la identidad autenticada cambió bajo esta secuencia "
                "(observer_runtime/observer_generation/target/target_generation): "
                "el contador NO se hereda — crea una secuencia para la tupla nueva")

    def exito(self) -> None:
        """Una observación aceptada confirma la posición: deja de ser provisional."""
        self._provisional = False
        self._reintentos = 0

    @property
    def provisional(self) -> bool:
        """`True` mientras la posición sea una suposición en frío, no confirmada."""
        return self._provisional

    def adoptar(self, maximo_observado: int) -> None:
        """Rehidrata tras arrancar o tras un `ObservationSequenceConflict`.

        Nunca retrocede: adoptar un máximo menor que el que ya se lleva sería
        volver a fabricar el conflicto.
        """
        if (type(maximo_observado) is not int or isinstance(maximo_observado, bool)
                or not 0 <= maximo_observado <= MAX_SUPERVISOR_SEQ):
            raise ObservacionInvalida("máximo observado inválido")
        if self._ultimo is not None and maximo_observado < self._ultimo:
            raise ObservacionInvalida(
                "adoptar un máximo menor reintroduce el conflicto de secuencia")
        self._ultimo = maximo_observado

    @property
    def contexto(self) -> tuple | None:
        """Tupla a la que está ligada, o `None` si no se ató ninguna."""
        return self._contexto

    @property
    def rehidratada(self) -> bool:
        return self._ultimo is not None

    def siguiente(self) -> int:
        if self._ultimo is None:
            raise SinRehidratar(
                "secuencia no rehidratada: lee MAX(supervisor_seq) de la autoridad "
                "y llama adoptar() antes de observar — arrancar en 0 deja al "
                "adaptador mudo para esta generación")
        if self._ultimo >= MAX_SUPERVISOR_SEQ:
            raise ObservacionInvalida("supervisor_seq agotada")
        self._ultimo += 1
        return self._ultimo

    @property
    def ultimo(self) -> int | None:
        return self._ultimo


def degradacion_por_recurso(*, workload_id: str, runtime_instance: str,
                            idempotency_key: str, seq: int, reason_code: str,
                            cpu_millis: int | None = None,
                            rss_bytes: int | None = None,
                            heartbeat_age_ms: int | None = None,
                            detector_state: str | None = None) -> Observacion:
    """Señal de recurso -> observación typada.

    Aquí es donde las cifras que un SRE narraría (`swap libre 1,13 GiB`,
    `load/core 6,65`) tienen que entrar como enteros acotados o no entrar. El
    kernel no acepta prosa y este adaptador tampoco la construye.
    """
    return valida(Observacion(
        workload_id=workload_id, runtime_instance=runtime_instance,
        idempotency_key=idempotency_key, supervisor_seq=seq,
        observation_kind="resource_degraded", reason_code=reason_code,
        detector_state=detector_state, cpu_millis=cpu_millis,
        rss_bytes=rss_bytes, heartbeat_age_ms=heartbeat_age_ms))


def latido(*, workload_id: str, runtime_instance: str, idempotency_key: str,
           seq: int, heartbeat_age_ms: int | None = None,
           detector_state: str | None = None) -> Observacion:
    return valida(Observacion(
        workload_id=workload_id, runtime_instance=runtime_instance,
        idempotency_key=idempotency_key, supervisor_seq=seq,
        observation_kind="cycle_ack", reason_code="PROCESS_PRESENT",
        detector_state=detector_state, heartbeat_age_ms=heartbeat_age_ms))


def salida(*, workload_id: str, runtime_instance: str, idempotency_key: str,
           seq: int, exit_code: int,
           detector_state: str | None = None) -> Observacion:
    return valida(Observacion(
        workload_id=workload_id, runtime_instance=runtime_instance,
        idempotency_key=idempotency_key, supervisor_seq=seq,
        observation_kind="exited", reason_code="PROCESS_EXITED",
        detector_state=detector_state, exit_code=exit_code))
