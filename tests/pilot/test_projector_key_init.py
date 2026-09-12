from __future__ import annotations

import os
import pathlib
import stat
import sys

import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ / "tools"))

import pilot_key_init as key_init  # noqa: E402


def source(tmp_path, data=b"k" * 48):
    path = tmp_path / "source.key"
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def test_instala_atomico_0600_y_es_idempotente(tmp_path):
    src = source(tmp_path)
    target = tmp_path / "volume"
    target.mkdir()
    uid, gid = os.geteuid(), os.getegid()
    assert key_init.install(str(src), str(target), uid=uid, gid=gid) == "installed"
    installed = target / "frame.key"
    assert installed.read_bytes() == b"k" * 48
    assert stat.S_IMODE(installed.stat().st_mode) == 0o600
    assert installed.stat().st_uid == uid and installed.stat().st_gid == gid
    assert key_init.install(str(src), str(target), uid=uid, gid=gid) == "unchanged"
    assert list(target.iterdir()) == [installed]


def test_puede_instalar_el_pepper_con_nombre_cerrado(tmp_path):
    src = source(tmp_path, b"p" * 48)
    target = tmp_path / "volume"
    target.mkdir()
    key_init.install(
        str(src), str(target), uid=os.geteuid(), gid=os.getegid(),
        target_name="journal.pepper")
    assert (target / "journal.pepper").read_bytes() == b"p" * 48


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "a\\b"])
def test_nombre_destino_no_puede_escapar_del_volumen(tmp_path, name):
    target = tmp_path / "volume"
    target.mkdir()
    with pytest.raises(key_init.InitError, match="segmento"):
        key_init.install(
            str(source(tmp_path)), str(target), uid=os.geteuid(), gid=os.getegid(),
            target_name=name)


def test_no_rota_en_silencio_un_key_distinto(tmp_path):
    target = tmp_path / "volume"
    target.mkdir()
    uid, gid = os.geteuid(), os.getegid()
    key_init.install(str(source(tmp_path, b"a" * 48)), str(target), uid=uid, gid=gid)
    (tmp_path / "source.key").write_bytes(b"b" * 48)
    with pytest.raises(key_init.InitError, match="OTRO"):
        key_init.install(str(tmp_path / "source.key"), str(target), uid=uid, gid=gid)
    assert (target / "frame.key").read_bytes() == b"a" * 48


def test_dos_inicializadores_no_pueden_reemplazarse_en_la_publicacion(
        tmp_path, monkeypatch):
    src = source(tmp_path, b"a" * 48)
    target = tmp_path / "volume"
    target.mkdir()
    uid, gid = os.geteuid(), os.getegid()

    def gana_otro(_source, _target, **_kwargs):
        winner = target / "frame.key"
        winner.write_bytes(b"b" * 48)
        winner.chmod(0o600)
        raise FileExistsError

    monkeypatch.setattr(key_init.os, "link", gana_otro)
    with pytest.raises(key_init.InitError, match="OTRO"):
        key_init.install(str(src), str(target), uid=uid, gid=gid)
    assert (target / "frame.key").read_bytes() == b"b" * 48
    assert list(target.glob(".*.tmp")) == []


@pytest.mark.parametrize("kind", ["short", "mode", "symlink", "directory"])
def test_fuente_insegura_falla_sin_publicar(tmp_path, kind):
    src = source(tmp_path)
    if kind == "short":
        src.write_bytes(b"short")
    elif kind == "mode":
        src.chmod(0o644)
    elif kind == "symlink":
        real = src
        src = tmp_path / "alias.key"
        src.symlink_to(real)
    else:
        src.unlink()
        src.mkdir()
    target = tmp_path / "volume"
    target.mkdir()
    with pytest.raises(key_init.InitError):
        key_init.install(str(src), str(target), uid=os.geteuid(), gid=os.getegid())
    assert list(target.iterdir()) == []


def test_destino_existente_con_dueño_equivocado_falla(tmp_path, monkeypatch):
    src = source(tmp_path)
    target = tmp_path / "volume"
    target.mkdir()
    installed = target / "frame.key"
    installed.write_bytes(b"k" * 48)
    installed.chmod(0o600)
    with pytest.raises(key_init.InitError, match="uid"):
        key_init.install(
            str(src), str(target), uid=os.geteuid() + 1, gid=os.getegid())


def test_no_deja_temporal_si_falla_el_chown(tmp_path, monkeypatch):
    src = source(tmp_path)
    target = tmp_path / "volume"
    target.mkdir()
    monkeypatch.setattr(key_init.os, "fchown", lambda *_: (_ for _ in ()).throw(
        PermissionError("denied")))
    with pytest.raises(PermissionError):
        key_init.install(str(src), str(target), uid=123, gid=456)
    assert list(target.iterdir()) == []
