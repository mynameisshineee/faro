#!/usr/bin/env python3
"""Copia un secreto del host a un volumen Docker con dueño estable.

Docker Desktop presenta los bind mounts como ``root:root`` aunque el fichero
pertenezca al usuario del host. El gateway no relaja por ello su contrato de
propiedad: este inicializador, ejecutado de forma explícita y sin red, copia una
vez el secreto a un volumen nombrado y lo deja 0600 bajo el uid/gid del runtime.
Una segunda ejecución sólo acepta exactamente los mismos bytes.
"""
from __future__ import annotations

import errno
import os
import stat
import sys
import uuid

MIN_BYTES = 32
MAX_BYTES = 4096


class InitError(RuntimeError):
    pass


def _flags(*names: str) -> int:
    if any(not hasattr(os, name) for name in names):
        raise InitError("la plataforma no ofrece los flags de apertura segura")
    value = 0
    for name in names:
        value |= getattr(os, name)
    return value


def _stable_read(fd: int, *, expected_uid: int | None = None) -> bytes:
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode):
        raise InitError("el secreto no es un fichero regular")
    if stat.S_IMODE(before.st_mode) != 0o600:
        raise InitError("el secreto exige modo 0600 exacto")
    if expected_uid is not None and before.st_uid != expected_uid:
        raise InitError("el secreto instalado no pertenece al uid del runtime")
    if not MIN_BYTES <= before.st_size <= MAX_BYTES:
        raise InitError("el secreto queda fuera del tamaño permitido")
    chunks, remaining = [], before.st_size
    while remaining:
        chunk = os.read(fd, min(remaining, 64 * 1024))
        if not chunk:
            raise InitError("el secreto se cortó durante la lectura")
        chunks.append(chunk)
        remaining -= len(chunk)
    after = os.fstat(fd)
    identity = lambda value: (
        value.st_dev, value.st_ino, value.st_size,
        value.st_mtime_ns, value.st_ctime_ns,
    )
    if identity(before) != identity(after):
        raise InitError("el secreto cambió durante la lectura")
    return b"".join(chunks)


def _read_source(path: str) -> bytes:
    flags = os.O_RDONLY | _flags("O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC")
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise InitError("el secreto fuente es un enlace simbólico") from exc
        raise InitError("el secreto fuente no se puede abrir") from exc
    try:
        return _stable_read(fd)
    finally:
        os.close(fd)


def install(source: str, target_dir: str, *, uid: int, gid: int,
            target_name: str = "frame.key") -> str:
    if (not target_name or target_name in {".", ".."}
            or "/" in target_name or "\\" in target_name):
        raise InitError("el nombre destino no es un único segmento")
    data = _read_source(source)
    dir_flags = os.O_RDONLY | _flags("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
    try:
        dirfd = os.open(target_dir, dir_flags)
    except OSError as exc:
        raise InitError("el volumen destino no es un directorio seguro") from exc
    try:
        read_flags = os.O_RDONLY | _flags("O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC")
        try:
            current_fd = os.open(target_name, read_flags, dir_fd=dirfd)
        except FileNotFoundError:
            current_fd = None
        except OSError as exc:
            raise InitError("el secreto instalado no se puede abrir") from exc
        if current_fd is not None:
            try:
                current = _stable_read(current_fd, expected_uid=uid)
                installed = os.fstat(current_fd)
                if installed.st_gid != gid:
                    raise InitError(
                        "el secreto instalado no pertenece al gid esperado")
            finally:
                os.close(current_fd)
            if current != data:
                raise InitError(
                    "el volumen ya contiene OTRO secreto; la rotación exige un flujo explícito")
            return "unchanged"

        temporary = f".{target_name}.{uuid.uuid4().hex}.tmp"
        write_flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
                       | _flags("O_NOFOLLOW", "O_CLOEXEC"))
        fd = os.open(temporary, write_flags, 0o600, dir_fd=dirfd)
        temporary_removed = False
        result = "installed"
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise InitError("el secreto no se pudo escribir completo")
                view = view[written:]
            os.fchmod(fd, 0o600)
            os.fchown(fd, uid, gid)
            os.fsync(fd)
            os.close(fd)
            fd = -1
            # Publicación sin reemplazo. ``os.replace`` era atómico para el
            # lector, pero no para la AUTORIDAD: dos inicializadores que vieran
            # el destino ausente podían publicar bytes distintos y el último
            # sustituía al primero. El hard-link crea el nombre sólo si sigue
            # ausente. Si otro ganó, se verifica su fichero antes de aceptar la
            # corrida como idempotente.
            try:
                os.link(temporary, target_name,
                        src_dir_fd=dirfd, dst_dir_fd=dirfd,
                        follow_symlinks=False)
            except FileExistsError:
                try:
                    winner_fd = os.open(target_name, read_flags, dir_fd=dirfd)
                except OSError as exc:
                    raise InitError(
                        "el secreto publicado por otro inicializador no se puede abrir"
                    ) from exc
                try:
                    winner = _stable_read(winner_fd, expected_uid=uid)
                    winner_stat = os.fstat(winner_fd)
                    if winner_stat.st_gid != gid:
                        raise InitError(
                            "el secreto publicado por otro inicializador no pertenece "
                            "al gid esperado")
                finally:
                    os.close(winner_fd)
                if winner != data:
                    raise InitError(
                        "otro inicializador publicó OTRO secreto; no se sustituye")
                result = "unchanged"
            os.unlink(temporary, dir_fd=dirfd)
            temporary_removed = True
            os.fsync(dirfd)
        finally:
            if fd >= 0:
                os.close(fd)
            if not temporary_removed:
                try:
                    os.unlink(temporary, dir_fd=dirfd)
                except FileNotFoundError:
                    pass
        return result
    finally:
        os.close(dirfd)


def main() -> int:
    try:
        uid = int(os.environ.get("LLMINBOX_SECRET_UID", "1000"))
        gid = int(os.environ.get("LLMINBOX_SECRET_GID", "1000"))
        if uid < 0 or gid < 0:
            raise ValueError
        target_name = os.environ.get("LLMINBOX_SECRET_TARGET_NAME", "frame.key")
        result = install(
            os.environ.get("LLMINBOX_SECRET_SOURCE", "/source/secret"),
            os.environ.get("LLMINBOX_SECRET_TARGET_DIR", "/run/secrets"),
            uid=uid, gid=gid,
            target_name=target_name,
        )
    except (InitError, ValueError) as exc:
        print(f"🔴 SECRETO NO INSTALADO — {exc}", file=sys.stderr)
        return 1
    print(f"✅ {target_name} {result}; bytes no expuestos")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
