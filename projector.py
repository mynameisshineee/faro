#!/usr/bin/env python3
"""Proyeccion durable del outbox nativo a un ledger Markdown.

La autoridad vive en :mod:`coordination`, no en este fichero ni en un comentario
Markdown.  Este modulo hace una sola cosa: reclama un ``ProjectionJob`` con una
sesion de worker, escribe un frame canonico bajo ``flock`` y solo entonces
declara ``materialized`` con la huella que acaba de medir.

La prueba HMAC del frame no concede autoridad. Sirve para distinguir, despues de
un crash, bytes que escribio este proyector de una marca copiada o fabricada. La
clave debe ser un secreto durable del servidor: perderla obliga a intervencion
operacional sobre los items que quedaron entre ``fsync`` y ``materialized``.

Restore fail-closed: junto a cada snapshot/backup se genera ANTES de copiar un
``RestorePermit`` con :meth:`MarkdownProjector.create_restore_permit`. Tras
restaurar el snapshot en el mismo path, el worker normal rechazara los frames
pendientes porque cambio el inode. Un operador entrega el permit de ESE snapshot
a :meth:`MarkdownProjector.reconcile_restored_next`, que verifica HMAC, path,
carril, ledger, hash y tamano del snapshot completo antes de reconocer un frame
ligado al inode anterior. Sin permit exacto no hay reconciliacion ni ``mark``.
"""
from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator, Mapping

import coordination as C


_EVENT_ID = re.compile(r"^evt_[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OCCURRED_AT = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
_HEADER_START = re.compile(br"(?m)^### \[")
_MARKER_PREFIX = b"<!-- LLMINBOX-EVENT-"
_FRAME_DOMAIN = b"llminbox-markdown-projection-v1\0"
_RESTORE_DOMAIN = b"llminbox-markdown-restore-permit-v1\0"


class ProjectionError(Exception):
    """Raiz de los rechazos deliberados del proyector."""


class ProjectionConfigError(ProjectionError):
    """La allowlist del servidor es ambigua o insegura."""


class FrameConflict(ProjectionError):
    """Hay una marca incompleta, fabricada, copiada o alterada."""


class ProjectionIOError(ProjectionError):
    """El fichero cambio durante una lectura/escritura que debia ser estable."""


def _durable_failure_code(exc: BaseException) -> str:
    """Traduce detalle local a un código cerrado apto para el journal."""
    if isinstance(exc, ProjectionConfigError):
        return "PROJECTOR_CONFIG_ERROR"
    if isinstance(exc, FrameConflict):
        return "PROJECTOR_FRAME_CONFLICT"
    if isinstance(exc, ProjectionIOError):
        return "PROJECTOR_IO_ERROR"
    if isinstance(exc, OSError):
        return "PROJECTOR_OS_ERROR"
    return "PROJECTOR_ERROR"


@dataclass(frozen=True)
class LedgerTarget:
    """Entrada de la allowlist configurada por el servidor."""

    lane: str
    ledger: str
    path: str


@dataclass(frozen=True)
class ProjectionResult:
    event_id: str
    ledger: str
    entry_eid: str
    byte_off: int
    appended: bool
    recovered: bool
    restored: bool = False


@dataclass(frozen=True)
class RestorePermit:
    """Autorizacion firmada para UN snapshot restaurado en el mismo path."""

    version: int
    lane: str
    ledger: str
    path: str
    source_dev: int
    source_ino: int
    size: int
    ledger_sha256: str
    proof: str

    def to_json(self) -> str:
        return json.dumps(self.__dict__, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> "RestorePermit":
        try:
            value = json.loads(raw)
            if set(value) != set(cls.__dataclass_fields__):
                raise ValueError("campos inesperados")
            return cls(**value)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProjectionConfigError("restore permit malformado") from exc


@dataclass(frozen=True)
class _Frame:
    data: bytes
    entry_eid: str
    byte_off: int


def _stat_signature(st: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (st.st_dev, st.st_ino, st.st_mode, st.st_size,
            st.st_mtime_ns, st.st_ctime_ns)


def _file_identity(st: os.stat_result) -> tuple[int, int]:
    return st.st_dev, st.st_ino


def _safe_slot(value: object, field: str) -> str:
    text = str(value)
    if (not text or len(text) > 256 or any(ord(ch) < 32 for ch in text)
            or any(token in text for token in ("[", "]", "\u2192", "->", "\u00b7"))):
        raise FrameConflict(f"{field} no cabe con seguridad en la cabecera canonica")
    return text


def _safe_head(value: object) -> str:
    text = " / ".join(str(value).splitlines()).strip()
    if not text:
        return "evento nativo"
    if "\x00" in text:
        raise FrameConflict("head contiene NUL")
    return text


def _safe_body(value: object) -> str:
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    if "\x00" in text:
        raise FrameConflict("body contiene NUL")
    lines = []
    for line in text.split("\n"):
        # Un cuerpo no puede abrir otra entrada ni fabricar un marcador. Se
        # conserva como cita visible en vez de convertirlo en estructura.
        if re.match(r"^(?:###? \[|## \d{4}-\d{2}-\d{2})", line) \
                or line.startswith(_MARKER_PREFIX.decode("ascii")):
            line = "> " + line
        lines.append(line)
    return "\n".join(lines).rstrip()


def _validate_job(job: C.ProjectionJob) -> None:
    if not _EVENT_ID.fullmatch(job.event_id):
        raise FrameConflict("event_id no canonico")
    if not _SHA256.fullmatch(job.payload_sha256):
        raise FrameConflict("payload_sha no canonico")
    if not _OCCURRED_AT.fullmatch(job.occurred_at):
        raise FrameConflict("occurred_at no canonico")
    expected_sha = hashlib.sha256(json.dumps({
        "intent": dict(job.intent), "principal": job.principal,
        "role": job.role, "lane": job.lane,
        "runtime_instance": job.runtime_instance,
        "occurred_at": job.occurred_at,
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")).hexdigest()
    if not hmac.compare_digest(expected_sha, job.payload_sha256):
        raise FrameConflict("payload_sha no corresponde al ProjectionJob")
    intent_recipients = job.intent.get("to")
    if (type(intent_recipients) is not list
            or any(type(recipient) is not str for recipient in intent_recipients)):
        raise FrameConflict("intent.to debe ser una lista canonica de strings")
    # Igualdad de secuencia, no de set: tipo, orden y duplicados quedan ligados
    # a los bytes firmados del intent y no se normalizan en el proyector.
    if tuple(intent_recipients) != job.recipients:
        raise FrameConflict("recipients diverge de intent.to")
    _safe_slot(job.principal, "principal")
    if not job.recipients:
        raise FrameConflict("un evento proyectado necesita destinatario")
    for recipient in job.recipients:
        _safe_slot(recipient, "recipient")


def _unsigned_entry(job: C.ProjectionJob, byte_off: int) -> bytes:
    actor = _safe_slot(job.principal, "principal")
    recipients = " \u2227 ".join(_safe_slot(r, "recipient") for r in job.recipients)
    kind = _safe_slot(job.intent.get("canonical_kind") or
                      job.intent.get("kind") or "EVENT", "kind")
    head = _safe_head(job.intent.get("head", ""))
    body = _safe_body(job.intent.get("body", ""))
    begin = (f"<!-- LLMINBOX-EVENT-BEGIN v=1 event_id={job.event_id} "
             f"payload_sha={job.payload_sha256} byte_off={byte_off} -->")
    end = f"<!-- LLMINBOX-EVENT-END v=1 event_id={job.event_id} -->"
    parts = [
        f"### [{actor} \u2192 {recipients} \u00b7 {kind}] {job.occurred_at} \u2014 {head}",
        begin,
    ]
    if body:
        parts.append(body)
    parts.append(end)
    return ("\n".join(parts) + "\n").encode("utf-8")


def _canonical_frame(job: C.ProjectionJob, target: LedgerTarget,
                     file_id: tuple[int, int], byte_off: int,
                     frame_key: bytes) -> _Frame:
    unsigned = _unsigned_entry(job, byte_off)
    binding = (f"{job.lane}\0{target.ledger}\0{file_id[0]}:{file_id[1]}\0"
               ).encode("utf-8")
    proof = hmac.new(frame_key, _FRAME_DOMAIN + binding + unsigned,
                     hashlib.sha256).hexdigest()
    needle = f" byte_off={byte_off} -->".encode("ascii")
    replacement = f" byte_off={byte_off} proof={proof} -->".encode("ascii")
    data = unsigned.replace(needle, replacement, 1)
    return _Frame(data=data,
                  entry_eid=hashlib.sha256(data).hexdigest(),
                  byte_off=byte_off)


def _restore_payload(*, lane: str, ledger: str, path: str,
                     source_dev: int, source_ino: int, size: int,
                     ledger_sha256: str) -> bytes:
    return json.dumps({
        "v": 1, "lane": lane, "ledger": ledger, "path": path,
        "source_dev": source_dev, "source_ino": source_ino,
        "size": size, "ledger_sha256": ledger_sha256,
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _make_restore_permit(target: LedgerTarget, file_id: tuple[int, int],
                         raw: bytes, frame_key: bytes) -> RestorePermit:
    digest = hashlib.sha256(raw).hexdigest()
    payload = _restore_payload(
        lane=target.lane, ledger=target.ledger, path=target.path,
        source_dev=file_id[0], source_ino=file_id[1], size=len(raw),
        ledger_sha256=digest)
    proof = hmac.new(frame_key, _RESTORE_DOMAIN + payload,
                     hashlib.sha256).hexdigest()
    return RestorePermit(
        1, target.lane, target.ledger, target.path, file_id[0], file_id[1],
        len(raw), digest, proof)


def _read_stable(fd: int) -> tuple[bytes, os.stat_result]:
    before = os.fstat(fd)
    chunks = []
    offset = 0
    while offset < before.st_size:
        chunk = os.pread(fd, min(1024 * 1024, before.st_size - offset), offset)
        if not chunk:
            raise ProjectionIOError("lectura corta del ledger")
        chunks.append(chunk)
        offset += len(chunk)
    after = os.fstat(fd)
    if _stat_signature(before) != _stat_signature(after):
        raise ProjectionIOError("el ledger cambio durante la lectura")
    return b"".join(chunks), after


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise ProjectionIOError("escritura corta sin progreso")
        view = view[written:]


def _marker_positions(raw: bytes, event_id: str) -> list[int]:
    escaped = re.escape(event_id.encode("ascii"))
    pattern = re.compile(
        br"(?m)^<!-- LLMINBOX-EVENT-(?:BEGIN|END)[^\r\n]*event_id="
        + escaped + br"(?:[ >]|$)")
    return [match.start() for match in pattern.finditer(raw)]


def _segments(raw: bytes) -> Iterator[tuple[int, int, bytes]]:
    starts = [match.start() for match in _HEADER_START.finditer(raw)]
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(raw)
        yield start, end, raw[start:end]


def _inspect_frames(raw: bytes, job: C.ProjectionJob, target: LedgerTarget,
                    file_id: tuple[int, int], frame_key: bytes
                    ) -> tuple[list[_Frame], list[tuple[int, int]]]:
    valid: list[_Frame] = []
    residues: list[tuple[int, int]] = []
    recognized: list[tuple[int, int]] = []
    for start, end, segment in _segments(raw):
        expected = _canonical_frame(job, target, file_id, start, frame_key)
        candidates = [(segment, end)]
        # Si el proceso murio sin terminar una linea, el siguiente append puso
        # exactamente un LF separador antes de su nueva cabecera.
        if segment.endswith(b"\n"):
            candidates.append((segment[:-1], end - 1))
        matched = False
        for candidate, candidate_end in candidates:
            if candidate == expected.data:
                valid.append(expected)
                recognized.append((start, candidate_end))
                matched = True
                break
            # Un SIGKILL puede caer dentro del primer write, incluso antes de
            # completar la linea de prueba. Un prefijo byte-a-byte del frame
            # esperado se tolera como RESIDUO, nunca como evidencia: siempre se
            # anexa y verifica otro frame completo. Lo fabricado que diverge un
            # solo byte queda fuera y el marcador suelto lo vuelve conflicto.
            if (candidate and len(candidate) < len(expected.data)
                    and expected.data.startswith(candidate)):
                residues.append((start, candidate_end))
                recognized.append((start, candidate_end))
                matched = True
                break
        if matched:
            continue

    def covered(position: int) -> bool:
        return any(start <= position < end for start, end in recognized)

    stray = [position for position in _marker_positions(raw, job.event_id)
             if not covered(position)]
    if stray:
        raise FrameConflict(
            "hay un frame incompleto, fabricado, copiado o alterado para el evento")
    if len(valid) > 1:
        raise FrameConflict("hay mas de un frame completo para el evento")
    return valid, residues


def _repair_evidence(raw: bytes, residues: list[tuple[int, int]]) \
        -> C.ProjectionRepairEvidence | None:
    """Cita residuos sin sacar sus bytes ni el path al journal observable."""
    if not residues:
        return None
    # El exceso es un conflicto operacional cerrado, no un error del value
    # object que escaparía del circuito durable de ``project_next`` y dejaría
    # el claim vivo repitiéndose para siempre tras cada vencimiento.
    if len(residues) > C.MAX_PROJECTION_REPAIR_RESIDUES:
        raise FrameConflict("demasiados residuos autenticados para reparar")
    return C.ProjectionRepairEvidence(tuple(
        (start, end - start, hashlib.sha256(raw[start:end]).hexdigest())
        for start, end in residues
    ))


class MarkdownProjector:
    """Worker de un carril con destinos fijados por configuracion del servidor.

    ``project_next`` no acepta actor, carril, ledger, ruta, ``entry_eid`` ni
    offset. Los primeros salen del ``ProjectionJob`` autorizado y los dos
    ultimos se miden sobre el descriptor que acaba de hacer ``fsync``.
    """

    def __init__(self, journal: C.Journal, worker_token: str,
                 targets: Iterable[LedgerTarget], *, frame_key: bytes,
                 crash_hook: Callable[[str], None] | None = None):
        if not isinstance(frame_key, bytes) or len(frame_key) < 32:
            raise ProjectionConfigError("frame_key necesita al menos 32 bytes")
        self._journal = journal
        self._token = worker_token
        self._frame_key = frame_key
        self._crash_hook = crash_hook
        by_key: dict[tuple[str, str], LedgerTarget] = {}
        identities: dict[tuple[int, int], tuple[str, str]] = {}
        for target in targets:
            if (not target.lane or not target.ledger
                    or "\0" in target.lane or "\0" in target.ledger
                    or not os.path.isabs(target.path)):
                raise ProjectionConfigError(
                    "cada destino necesita lane/ledger y path absoluto")
            key = (target.lane, target.ledger)
            if key in by_key:
                raise ProjectionConfigError(f"destino duplicado para {key!r}")
            try:
                st = os.stat(target.path, follow_symlinks=False)
            except OSError as exc:
                raise ProjectionConfigError(
                    f"destino no accesible: {target.path}: {exc}") from exc
            if not stat.S_ISREG(st.st_mode):
                raise ProjectionConfigError(
                    f"destino no es fichero regular: {target.path}")
            identity = _file_identity(st)
            if identity in identities:
                raise ProjectionConfigError(
                    f"dos destinos comparten inode: {identities[identity]!r} y {key!r}")
            identities[identity] = key
            by_key[key] = target
        if not by_key:
            raise ProjectionConfigError("allowlist de proyeccion vacia")
        self._targets: Mapping[tuple[str, str], LedgerTarget] = by_key

    def _assert_unique_inode(self, selected: LedgerTarget,
                             identity: tuple[int, int]) -> None:
        """Revalida alias despues de construir: la configuracion tambien cambia."""
        for target in self._targets.values():
            if target == selected:
                continue
            try:
                st = os.stat(target.path, follow_symlinks=False)
            except OSError as exc:
                raise ProjectionConfigError(
                    f"destino de allowlist dejo de ser accesible: {target.path}: {exc}") from exc
            if not stat.S_ISREG(st.st_mode):
                raise ProjectionConfigError(
                    f"destino de allowlist dejo de ser regular: {target.path}")
            if _file_identity(st) == identity:
                raise ProjectionConfigError(
                    "dos destinos de la allowlist pasaron a compartir inode")

    def _hook(self, stage: str) -> None:
        if self._crash_hook is not None:
            self._crash_hook(stage)

    def _target_for(self, view: C.SessionView, job: C.ProjectionJob) -> LedgerTarget:
        if job.lane != view.lane:
            raise ProjectionConfigError("el claim devolvio trabajo de otro carril")
        target = self._targets.get((view.lane, job.ledger))
        if target is None:
            raise ProjectionConfigError(
                f"{job.ledger!r} no esta en la allowlist server-side de {view.lane!r}")
        return target

    def _configured_target(self, lane: str, ledger: str) -> LedgerTarget:
        target = self._targets.get((lane, ledger))
        if target is None:
            raise ProjectionConfigError(
                f"{ledger!r} no esta en la allowlist server-side de {lane!r}")
        return target

    def _verify_restore_permit(self, permit: RestorePermit, target: LedgerTarget,
                               raw: bytes, current_id: tuple[int, int]) -> bytes:
        if (type(permit.version) is not int or permit.version != 1
                or type(permit.source_dev) is not int
                or type(permit.source_ino) is not int
                or type(permit.size) is not int or permit.size < 0
                or type(permit.ledger_sha256) is not str
                or type(permit.proof) is not str
                or not _SHA256.fullmatch(permit.ledger_sha256)
                or not _SHA256.fullmatch(permit.proof)):
            raise ProjectionConfigError("restore permit malformado")
        if (permit.lane, permit.ledger, permit.path) != (
                target.lane, target.ledger, target.path):
            raise ProjectionConfigError(
                "restore permit no corresponde al destino configurado")
        if (permit.source_dev, permit.source_ino) == current_id:
            raise ProjectionConfigError(
                "el inode no cambio: use project_next, no reconciliacion de restore")
        payload = _restore_payload(
            lane=permit.lane, ledger=permit.ledger, path=permit.path,
            source_dev=permit.source_dev, source_ino=permit.source_ino,
            size=permit.size, ledger_sha256=permit.ledger_sha256)
        expected = hmac.new(self._frame_key, _RESTORE_DOMAIN + payload,
                            hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, permit.proof):
            raise ProjectionConfigError("restore permit no autentico")
        if len(raw) < permit.size:
            raise ProjectionConfigError(
                "el snapshot restaurado no coincide byte-a-byte con el permit")
        base = raw[:permit.size]
        if not hmac.compare_digest(
                hashlib.sha256(base).hexdigest(), permit.ledger_sha256):
            raise ProjectionConfigError(
                "el snapshot restaurado no coincide byte-a-byte con el permit")
        return base

    @staticmethod
    def _authenticated_old_residue(
            raw: bytes, residues: list[tuple[int, int]], job: C.ProjectionJob,
            target: LedgerTarget, old_id: tuple[int, int], frame_key: bytes) -> bool:
        """El residuo contiene la linea BEGIN completa y su prueba del inode viejo."""
        for start, end in residues:
            expected = _canonical_frame(job, target, old_id, start, frame_key).data
            first_lf = expected.find(b"\n")
            begin_end = expected.find(b"\n", first_lf + 1) + 1
            segment = raw[start:end]
            if (begin_end > 0 and len(segment) >= begin_end
                    and segment[:begin_end] == expected[:begin_end]):
                return True
        return False

    @contextmanager
    def _locked(self, target: LedgerTarget):
        parent, name = os.path.split(target.path)
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fd = None
        try:
            parent_opened = os.fstat(parent_fd)
            parent_named = os.stat(parent, follow_symlinks=False)
            if (_file_identity(parent_opened) != _file_identity(parent_named)
                    or not stat.S_ISDIR(parent_opened.st_mode)):
                raise ProjectionConfigError("el directorio padre cambio al abrirlo")
            before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode):
                raise ProjectionConfigError("el destino dejo de ser fichero regular")
            fd = os.open(name, os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW,
                         dir_fd=parent_fd)
            opened = os.fstat(fd)
            if _file_identity(before) != _file_identity(opened) \
                    or not stat.S_ISREG(opened.st_mode):
                raise ProjectionConfigError("el destino cambio entre stat y open")
            fcntl.flock(fd, fcntl.LOCK_EX)
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if _file_identity(current) != _file_identity(opened):
                raise ProjectionConfigError("el nombre del ledger ya no apunta al descriptor")
            self._assert_unique_inode(target, _file_identity(opened))
            yield (parent_fd, parent, _file_identity(parent_opened), name, fd,
                   _file_identity(opened))
        finally:
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
            os.close(parent_fd)

    def _project_claimed(self, view: C.SessionView, job: C.ProjectionJob,
                         target: LedgerTarget) -> ProjectionResult:
        _validate_job(job)
        with self._locked(target) as (
                parent_fd, parent, parent_id, name, fd, file_id):
            raw, _ = _read_stable(fd)
            valid, residues = _inspect_frames(
                raw, job, target, file_id, self._frame_key)
            repair = _repair_evidence(raw, residues)
            appended = False
            if valid:
                frame = valid[0]
                recovered = True
                # Un frame completo puede proceder de un proceso que murio
                # despues de su ultimo write pero antes de acreditar el fsync.
                # Repetir fsync es barato y evita convertir page-cache visible
                # en prueba de durabilidad.
                os.fsync(fd)
            else:
                prefix = b"" if not raw or raw.endswith(b"\n") else b"\n"
                byte_off = len(raw) + len(prefix)
                frame = _canonical_frame(job, target, file_id, byte_off,
                                         self._frame_key)
                self._hook("before_append")
                # El primer tramo siempre contiene la prueba completa. Asi un
                # SIGKILL en ``mid_append`` deja un residuo autenticable, no un
                # prefijo indistinguible de una marca inventada.
                first_lf = frame.data.find(b"\n")
                begin_lf = frame.data.find(b"\n", first_lf + 1) + 1
                cut = max(begin_lf, len(frame.data) // 2)
                _write_all(fd, prefix + frame.data[:cut])
                self._hook("mid_append")
                _write_all(fd, frame.data[cut:])
                os.fsync(fd)
                self._hook("after_fsync")
                appended = True
                recovered = bool(residues)

            # El hecho se mide de nuevo desde EL MISMO descriptor. Ni el hash ni
            # el offset son telemetria declarada por el llamante.
            after, _ = _read_stable(fd)
            verified, _ = _inspect_frames(
                after, job, target, file_id, self._frame_key)
            if len(verified) != 1 or verified[0].data != frame.data:
                raise ProjectionIOError("el frame no esta completo despues de fsync")
            if os.pread(fd, len(frame.data), frame.byte_off) != frame.data:
                raise ProjectionIOError("los bytes medidos no son el frame canonico")
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if _file_identity(current) != file_id:
                raise ProjectionIOError("el path del ledger cambio antes de materializar")
            current_parent = os.stat(parent, follow_symlinks=False)
            if _file_identity(current_parent) != parent_id:
                raise ProjectionIOError(
                    "el directorio padre cambio antes de materializar")
            # Repetida en la ultima frontera: un hook/escritor pudo crear un
            # hardlink cross-lane despues de la comprobacion hecha al abrir.
            self._assert_unique_inode(target, file_id)

            # Se mantiene flock hasta que el Journal registra el hecho. No hay
            # una transaccion SQLite abierta durante el append/fsync.
            self._journal.mark_materialized(
                self._token, job.event_id, entry_eid=frame.entry_eid,
                ledger=target.ledger, claim_token=job.claim_token,
                byte_off=frame.byte_off, repair=repair)
            return ProjectionResult(
                event_id=job.event_id, ledger=target.ledger,
                entry_eid=frame.entry_eid, byte_off=frame.byte_off,
                appended=appended, recovered=recovered)

    def create_restore_permit(self, lane: str, ledger: str) -> RestorePermit:
        """Sella el snapshot que el operador va a respaldar.

        El permit se guarda JUNTO al backup, no dentro del ledger. Sólo cubre el
        path configurado y sus bytes completos en ese instante.
        """
        view = self._journal.authenticate(self._token)
        if view is None or C.CAP_OUTBOX_WORKER not in view.capabilities:
            raise C.AuthError("se requiere sesion de worker valida")
        if view.lane != lane:
            raise ProjectionConfigError("la sesion no gobierna ese carril")
        target = self._configured_target(lane, ledger)
        with self._locked(target) as (
                _parent_fd, _parent, _parent_id, _name, fd, file_id):
            raw, _ = _read_stable(fd)
            return _make_restore_permit(target, file_id, raw, self._frame_key)

    def _reconcile_restored_claimed(
            self, view: C.SessionView, job: C.ProjectionJob,
            target: LedgerTarget, permit: RestorePermit) -> ProjectionResult:
        _validate_job(job)
        with self._locked(target) as (
                parent_fd, parent, parent_id, name, fd, current_id):
            raw, _ = _read_stable(fd)
            base = self._verify_restore_permit(permit, target, raw, current_id)
            old_id = (permit.source_dev, permit.source_ino)
            old_valid, old_residues = _inspect_frames(
                base, job, target, old_id, self._frame_key)
            repair = _repair_evidence(base, old_residues)
            appended = False
            if old_valid:
                if len(raw) != len(base):
                    raise ProjectionConfigError(
                        "el snapshot restaurado tiene bytes extra no cubiertos")
                frame = old_valid[0]
            elif self._authenticated_old_residue(
                    base, old_residues, job, target, old_id, self._frame_key):
                # El snapshot termina en un BEGIN autentico sin END. Se deja
                # intacto y se escribe un frame nuevo ligado al inode actual.
                prefix = b"" if not base or base.endswith(b"\n") else b"\n"
                frame = _canonical_frame(
                    job, target, current_id, len(base) + len(prefix), self._frame_key)
                expected_tail = prefix + frame.data
                tail = raw[len(base):]
                if not expected_tail.startswith(tail):
                    raise FrameConflict(
                        "bytes posteriores al snapshot no son residuo del frame reconciliado")
                if tail != expected_tail:
                    remaining = expected_tail[len(tail):]
                    self._hook("before_restore_append")
                    cut = max(1, len(remaining) // 2)
                    _write_all(fd, remaining[:cut])
                    self._hook("mid_restore_append")
                    _write_all(fd, remaining[cut:])
                    appended = True
            else:
                raise FrameConflict(
                    "el snapshot autorizado no contiene frame completo ni residuo autentico")
            # La copia puede ser visible sin ser durable en el destino nuevo.
            os.fsync(fd)
            os.fsync(parent_fd)
            self._hook("after_restore_fsync")
            after, _ = _read_stable(fd)
            verified_base = self._verify_restore_permit(
                permit, target, after, current_id)
            if old_valid:
                if after != verified_base:
                    raise ProjectionIOError(
                        "el snapshot completo cambio durante la reconciliacion")
            else:
                expected_after = verified_base + prefix + frame.data
                if after != expected_after:
                    raise ProjectionIOError(
                        "el frame nuevo no quedo exacto tras reconciliar el residuo")
            if os.pread(fd, len(frame.data), frame.byte_off) != frame.data:
                raise ProjectionIOError("el frame restaurado cambio antes del mark")
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            current_parent = os.stat(parent, follow_symlinks=False)
            if (_file_identity(current) != current_id
                    or _file_identity(current_parent) != parent_id):
                raise ProjectionIOError(
                    "el destino restaurado cambio antes de materializar")
            self._assert_unique_inode(target, current_id)
            self._journal.mark_materialized(
                self._token, job.event_id, entry_eid=frame.entry_eid,
                ledger=target.ledger, claim_token=job.claim_token,
                byte_off=frame.byte_off, repair=repair)
            return ProjectionResult(
                event_id=job.event_id, ledger=target.ledger,
                entry_eid=frame.entry_eid, byte_off=frame.byte_off,
                appended=appended, recovered=True, restored=True)

    def project_next(self, *, lease_s: int = 60) -> ProjectionResult | None:
        view = self._journal.authenticate(self._token)
        if view is None:
            raise C.AuthError("se requiere sesion de worker valida")
        job = self._journal.claim_outbox(self._token, lease_s=lease_s)
        if job is None:
            return None
        try:
            target = self._target_for(view, job)
            return self._project_claimed(view, job, target)
        except (ProjectionError, OSError) as exc:
            # Si el proceso muere, BaseException/SIGKILL no pasa por aqui: el
            # lease vence y el siguiente worker inspecciona el efecto real.
            self._journal.mark_outbox_failed(
                self._token, job.event_id, error=_durable_failure_code(exc),
                claim_token=job.claim_token)
            raise

    def reconcile_restored_next(
            self, permit: RestorePermit | str, *, lease_s: int = 60
            ) -> ProjectionResult | None:
        """Reconcilia un frame pendiente tras restaurar un snapshot a otro inode.

        Procedimiento operacional fail-closed:

        1. antes del backup, guardar ``create_restore_permit(...).to_json()``
           junto al snapshot;
        2. restaurar el snapshot en el MISMO path configurado;
        3. cargarlo con ``RestorePermit.from_json`` y llamar a este metodo;
        4. conservar el permit hasta drenar todos los frames pendientes que
           pertenecian a ese snapshot. ``None`` confirma que ya no queda claim.

        Un permit alterado, de otro path/snapshot o emitido despues del restore
        falla antes de ``mark_materialized``.
        """
        if isinstance(permit, str):
            permit = RestorePermit.from_json(permit)
        if not isinstance(permit, RestorePermit):
            raise ProjectionConfigError("restore permit malformado")
        view = self._journal.authenticate(self._token)
        if view is None:
            raise C.AuthError("se requiere sesion de worker valida")
        job = self._journal.claim_outbox(self._token, lease_s=lease_s)
        if job is None:
            return None
        try:
            target = self._target_for(view, job)
            return self._reconcile_restored_claimed(view, job, target, permit)
        except (ProjectionError, OSError) as exc:
            self._journal.mark_outbox_failed(
                self._token, job.event_id, error=_durable_failure_code(exc),
                claim_token=job.claim_token)
            raise
