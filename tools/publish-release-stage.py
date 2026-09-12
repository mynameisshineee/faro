#!/usr/bin/env python3
"""Publica un stage plano creando cada destino una sola vez y con fsync."""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", type=pathlib.Path)
    parser.add_argument("destination", type=pathlib.Path)
    args = parser.parse_args()

    stage = args.stage.resolve(strict=True)
    if not stage.is_dir() or stage.is_symlink():
        raise SystemExit("stage must be a regular directory, not a symlink")
    entries = sorted(stage.iterdir())
    if not entries or any(not p.is_file() or p.is_symlink() for p in entries):
        raise SystemExit("stage must be non-empty, flat, and contain only regular files")

    destination = args.destination.absolute()
    # mkdir es el cerrojo: si otro proceso ganó la carrera no se mezcla ni se
    # sustituye un solo byte. Un fallo posterior deja un dist incompleto que el
    # builder se negará a reutilizar; ése es el estado fail-closed.
    destination.mkdir(mode=0o755)
    try:
        for source in entries:
            target = destination / source.name
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(target, flags, 0o644)
            try:
                with source.open("rb") as src, os.fdopen(fd, "wb", closefd=False) as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
                    dst.flush()
                    os.fsync(dst.fileno())
            finally:
                os.close(fd)
        dfd = os.open(destination, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except BaseException:
        # No se borra ni se reintenta: la evidencia parcial queda visible y una
        # nueva ejecución falla al ver el destino existente.
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
