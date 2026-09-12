#!/usr/bin/env python3
"""Adaptador: liga ``Journal`` + ``MarkdownProjector`` al Protocol ``ProjectionBackend``.

``ActiveProjectorRunner`` (``projector_runner.py``) no ve el Journal ni el
proyector concreto: habla sólo con cuatro verbos. Este módulo es la única pieza
que sí los ve, y por construcción no expone del Journal nada que no sea abrir,
proyectar, contar y cerrar la sesión de worker.

Implementado contra el Protocol tal como quedó tras ``1ebb817``
(``fix(projector): outbox_counts_conforma indefinido y carrera de quiesce en
start()``, medido en este mismo checkout): ``outbox_counts`` es estructural
— ``OutboxCountsLike`` (``lane``/``pending``/``failed``), no una clase
concreta — y ``refresh_worker_session`` recibe ``ttl_s`` de palabra clave
PORQUE LO PIDE EL RUNNER (``_renovar_si_toca`` calcula
``ceil(lease_s + renew_margin_s) + 1``), no porque lo decida este backend.

El root puede seleccionar este backend en modo ``active`` únicamente con
principal, carril, ledger y frame key ya resueltos por configuración. Construir
no abre sesiones; el primer efecto continúa siendo ``runner.start()`` dentro
del lifespan.
"""
from __future__ import annotations

from typing import Iterable

import coordination as C
import projector as P
import projector_runner as PR


class ProjectionBackendConfigurationError(ValueError):
    """El modo o los argumentos no alcanzan para construir el backend."""


class JournalProjectionBackend:
    """``ProjectionBackend`` real sobre un ``Journal`` y una allowlist fija.

    Construcción PURA: no abre sesión, no toca el Journal. Todo efecto empieza
    en ``open_worker_session``, igual que ``ActiveProjectorRunner`` exige de su
    backend.
    """

    def __init__(
        self,
        journal: C.Journal,
        credential: str,
        targets: Iterable[P.LedgerTarget],
        *,
        frame_key: bytes,
        ttl_s: int = C.DEFAULT_SESSION_TTL_S,
        crash_hook=None,
    ):
        if type(credential) is not str or not credential:
            raise ProjectionBackendConfigurationError(
                "credential debe ser un texto no vacío")
        targets = tuple(targets)
        if not targets:
            raise ProjectionBackendConfigurationError(
                "targets no puede estar vacío: MarkdownProjector exige allowlist")
        # `type(x) is int`, no `isinstance`: `bool` ES subclase de `int`, y con
        # `isinstance` un `ttl_s=True` colaría como el entero 1 -exactamente el
        # descarte que ya aplica el resto del módulo (`lease_s`, `OutboxCounts`).
        if type(ttl_s) is not int or ttl_s <= 0:
            raise ProjectionBackendConfigurationError(
                f"ttl_s={ttl_s!r} debe ser un int positivo")
        self._journal = journal
        self._credential = credential
        self._targets = targets
        self._frame_key = frame_key
        self._ttl_s = ttl_s
        self._crash_hook = crash_hook

    def open_worker_session(self) -> C.IssuedSession:
        """Sesión EXACTA de ``outbox_worker``: la credencial la trae, esto no la amplía.

        No se pide ni se comprueba aquí el conjunto de capacidades — eso es
        ``ActiveProjectorRunner._check_session``, que exige IGUALDAD contra
        ``LEAST_PRIVILEGE_CAPABILITIES`` y aborta con
        ``CAPABILITIES_NOT_LEAST_PRIVILEGE`` si la credencial concede de más o
        de menos. Duplicar esa comprobación aquí sería una segunda fuente de la
        misma verdad; el backend sólo abre lo que la credencial ya autoriza.
        """
        return self._journal.open_session(self._credential, ttl_s=self._ttl_s)

    def projector_for(self, session: PR.WorkerSessionLike) -> P.MarkdownProjector:
        return P.MarkdownProjector(
            self._journal, session.token, self._targets,
            frame_key=self._frame_key, crash_hook=self._crash_hook)

    def outbox_counts(self, session: PR.WorkerSessionLike) -> C.OutboxCounts:
        """Devuelve la medida del núcleo TAL CUAL: el contrato ya es estructural.

        Desde ``1ebb817``, ``ProjectionBackend.outbox_counts`` declara
        ``OutboxCountsLike`` (Protocol: ``lane``/``pending``/``failed`` de sólo
        lectura) y ``_measure()`` lee esos tres atributos por duck typing, sin
        ``isinstance`` nominal contra la clase de ``projector_runner``.
        ``coordination.OutboxCounts`` ya conforma esa forma exacta —incluido
        ``lane=view.lane``, la sesión autenticada dentro de la misma
        transacción que cuenta— así que envolverla en un segundo DTO no
        añadía nada: sólo reenviaba los mismos tres campos con otro nombre de
        clase.
        """
        counts = self._journal.outbox_counts(session.token)
        if counts.lane != session.lane:
            raise ProjectionBackendConfigurationError(
                "el Journal midió un carril distinto al de la sesión")
        return counts

    def close_worker_session(self, session: PR.WorkerSessionLike) -> None:
        """Revoca la sesión propia. Idempotente, y SÓLO por ``AuthError``.

        ``revoke_current`` levanta ``AuthError`` tanto si el token ya está
        revocado como si ya venció: las dos son "ya no hay nada que cerrar", no
        un fallo del cierre. Cualquier otra excepción (fallo de base de datos,
        interrupción) se propaga tal cual — silenciarla ahí convertiría un
        cierre que de verdad falló en uno que ``ActiveProjectorRunner`` cuenta
        como éxito, y `_cerrar_sesion_una_vez` dejaría de poder distinguir el
        caso que sí necesita reintento.
        """
        try:
            self._journal.revoke_current(session.token, reason="projector_stop")
        except C.AuthError:
            pass

    def refresh_worker_session(
            self, session: PR.WorkerSessionLike, *, ttl_s: int) -> C.IssuedSession:
        """Rota con el TTL que PIDE EL RUNNER, no con el de apertura de este backend.

        Desde ``1ebb817`` el Protocol declara
        ``refresh_worker_session(self, session, *, ttl_s: int)``:
        ``_renovar_si_toca`` calcula ``ceil(lease_s + renew_margin_s) + 1`` —la
        holgura exacta que el ciclo siguiente necesita— y es quien decide
        cuánto pedir, no este backend. ``self._ttl_s`` sigue gobernando sólo
        ``open_worker_session``, la apertura inicial; una vez el runner vive,
        cada renovación lleva SU propio cálculo.
        """
        if type(ttl_s) is not int or ttl_s <= 0:
            raise ProjectionBackendConfigurationError(
                "ttl_s de renovación debe ser un entero positivo")
        return self._journal.refresh_session(session.token, ttl_s=ttl_s)


def projection_backend_for_mode(
    mode: str,
    *,
    journal: C.Journal | None = None,
    credential: str | None = None,
    targets: Iterable[P.LedgerTarget] = (),
    frame_key: bytes | None = None,
    ttl_s: int = C.DEFAULT_SESSION_TTL_S,
    crash_hook=None,
) -> JournalProjectionBackend | None:
    """Selector del ADAPTADOR, no del runner. ``mode`` es un argumento explícito.

    Deliberadamente no lee variables de entorno: el composition root entrega
    valores ya validados. Construir un backend ``active`` tampoco abre sesión
    ni crea una hebra por sí solo.

    ``disabled`` devuelve ``None``: no hay backend que abrir sesión ni contar.
    """
    if mode == "disabled" and isinstance(mode, str):
        return None
    if mode == "active" and isinstance(mode, str):
        missing = [name for name, value in (
            ("journal", journal), ("credential", credential),
            ("frame_key", frame_key),
        ) if value is None]
        if missing:
            raise ProjectionBackendConfigurationError(
                f"mode='active' exige: {', '.join(missing)}")
        return JournalProjectionBackend(
            journal, credential, targets, frame_key=frame_key,
            ttl_s=ttl_s, crash_hook=crash_hook)
    raise ProjectionBackendConfigurationError(
        f"modo de backend desconocido: {mode!r}; usa 'active' o 'disabled'")


def graceful_shutdown(
        runner: PR.ProjectorRunnerLike, *, timeout_s: float) -> PR.RunnerSnapshot:
    """Ciclo de parada canónico: ``quiesce`` primero, ``stop`` SIEMPRE después.

    ``quiesce`` cierra la admisión LOCAL y espera al ciclo en vuelo sin matar la
    hebra ni cerrar la sesión; ``stop`` es quien de verdad une la hebra y cierra
    la sesión de worker. Un ``quiesce()`` que levante -``RunnerConfigurationError``
    por un ``timeout_s`` inválido, o cualquier fallo inesperado del runner- NO
    debe impedir el intento de ``stop()``: la sesión de worker sigue viva y sin
    cerrar, y saltarse ``stop()`` la fugaría hasta su TTL. Si AMBOS fallan, los
    dos fallos se preservan -ninguno se pierde tapado por el otro- en un
    ``ExceptionGroup``, la misma forma que ya usa
    ``ActiveProjectorRunner._close_session_or_group``.
    """
    quiesce_snapshot: PR.RunnerSnapshot | None = None
    quiesce_failure: BaseException | None = None
    try:
        quiesce_snapshot = runner.quiesce(timeout_s=timeout_s)
    except BaseException as exc:
        quiesce_failure = exc
    try:
        runner.stop()
    except BaseException as stop_failure:
        if quiesce_failure is not None:
            raise BaseExceptionGroup(
                "falló quiesce() Y falló stop()",
                [quiesce_failure, stop_failure],
            ) from None
        raise
    if quiesce_failure is not None:
        raise quiesce_failure
    return quiesce_snapshot


_RUNNER_READY_STATES = (PR.RunnerState.IDLE, PR.RunnerState.PROJECTING)


def runner_counts_as_ready(snapshot: PR.RunnerSnapshot) -> bool:
    """Lectura de disponibilidad de UN runner, para componer con ``/ready``.

    Un projector ``disabled`` (``required=False``) no es parte del contrato de
    disponibilidad: no cuenta ni a favor ni en contra. Para uno ``required``,
    listo exige el CONJUNTO —ninguna condición sola basta—: sin ``fatal_code``,
    hebra viva, aceptando claims, sesión abierta, y en uno de los tres estados
    de trabajo normal.

    ``blocked`` no cuenta: aunque sea reintentable, el proceso no está
    proyectando con normalidad y readiness no debe convertir backoff en sano.
    ``draining``/``starting``/``new``/``stopped`` tampoco cuentan.
    """
    if not snapshot.required:
        return True
    if snapshot.fatal_code is not None:
        return False
    return (snapshot.state in _RUNNER_READY_STATES
            and snapshot.thread_alive
            and snapshot.accepting_claims
            and snapshot.session_open)


def ready_with_runner(coordination_ready: bool, snapshot: PR.RunnerSnapshot) -> bool:
    """``/ready`` compuesto: AND estricto con el runner del projector."""
    return coordination_ready and runner_counts_as_ready(snapshot)
