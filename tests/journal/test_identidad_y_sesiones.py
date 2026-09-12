"""Identidad: `credential_ref`, principal fijo, sesiones hash-only, generación."""
from __future__ import annotations

import sqlite3

import pytest

import coordination as C
from ._arnes import PEPPER, Reloj, journal, sesion


def test_dos_principals_con_el_MISMO_rol_siguen_siendo_distinguibles(tmp_path):
    """Falsador 1 del ADR. Es el que mata `principal = role`."""
    j = journal(tmp_path)
    a = j.bind_credential("cred-A", principal="backend-1", role="be", lane="llminbox")
    b = j.bind_credential("cred-B", principal="backend-2", role="be", lane="llminbox")
    assert a.principal_id != b.principal_id
    assert a.role == b.role == "be"
    j.close()


def test_el_principal_derivado_del_rol_queda_FIJADO_contra_su_credential_ref(tmp_path):
    """El principal se fija la primera vez y no se re-deriva.

    Y cuando el mapa añade el principal explícito, la ligadura vieja NO se
    reescribe: se retira. Los eventos ya escritos conservan su atribución, que
    es el defecto de migración que esto existe para cerrar.
    """
    j = journal(tmp_path, admision=False)  # `admision=False`: este test CUENTA filas de identidad, y las
    # credenciales/sesiones que el arnés usa para abrir la barrera son
    # filas como cualquier otra. No acepta eventos, así que no necesita
    # la puerta abierta.
    derivado = j.bind_credential("cred-A", principal=None, role="be", lane="llminbox")
    assert derivado.principal_source == "derived_from_role"
    assert j.bind_credential("cred-A", principal=None, role="be",
                             lane="llminbox").principal_id == derivado.principal_id

    explicito = j.bind_credential("cred-A", principal="backend", role="be",
                                  lane="llminbox")
    assert explicito.principal_source == "explicit"
    assert explicito.principal_id != derivado.principal_id      # principal NUEVO
    # La ligadura anterior sigue en la base, retirada, no borrada.
    con = j._connect()
    filas = con.execute("SELECT retired_at FROM credential_bindings"
                        " ORDER BY bound_at").fetchall()
    assert len(filas) == 2 and filas[0]["retired_at"] is not None
    assert j.resolve_credential("cred-A").principal_id == explicito.principal_id
    j.close()


def test_el_credential_ref_es_opaco_y_la_credencial_no_entra_en_la_base(tmp_path):
    j = journal(tmp_path)
    j.bind_credential("credencial-secreta-123", principal="be", role="be",
                      lane="llminbox")
    crudo = _volcado(j)
    assert "credencial-secreta-123" not in crudo          # ⊖ el secreto NO está
    assert j.credential_ref("credencial-secreta-123") in crudo   # ⊕ su ref SÍ
    j.close()


def test_el_token_de_sesion_NO_es_recuperable_de_la_base(tmp_path):
    """Falsador 15. Con su control positivo: sin él, un journal que no guardara
    NADA pasaría este test sin almacenar identidad ninguna."""
    j = journal(tmp_path)
    s = sesion(j)
    crudo = _volcado(j)
    assert s.token not in crudo                                   # ⊖ el token no está
    assert C._sha256(s.token) in crudo                            # ⊕ su hash sí
    j.close()


def test_dos_sesiones_de_un_principal_tienen_runtime_instances_distintos(tmp_path):
    """Falsador 3: `principal` y `runtime_instance` no se colapsan."""
    j = journal(tmp_path)
    j.bind_credential("cred-A", principal="backend", role="be", lane="llminbox")
    a, b = j.open_session("cred-A"), j.open_session("cred-A")
    assert a.runtime_instance != b.runtime_instance
    assert a.principal_id == b.principal_id
    assert a.token != b.token
    j.close()


def test_rotar_la_sesion_invalida_el_token_viejo_de_inmediato(tmp_path):
    """Falsador 14. La revocación del viejo y la emisión del nuevo van en la
    MISMA transacción: no hay ventana con dos tokens válidos."""
    j = journal(tmp_path)
    vieja = sesion(j)
    nueva = j.refresh_session(vieja.token)
    assert j.authenticate(vieja.token) is None
    assert j.authenticate(nueva.token) is not None
    assert nueva.runtime_instance != vieja.runtime_instance
    j.close()


def test_revocar_expirar_y_rotar_el_mapa_apagan_la_sesion(tmp_path):
    """Tres caminos de invalidación, cada uno con su ⊕ de que antes valía."""
    reloj = Reloj()
    j = journal(tmp_path, reloj=reloj)

    revocada = sesion(j, "cred-1", principal="p1")
    assert j.authenticate(revocada.token) is not None            # ⊕
    j.revoke_current(revocada.token, "prueba")
    assert j.authenticate(revocada.token) is None

    caduca = sesion(j, "cred-2", principal="p2", ttl_s=60)
    assert j.authenticate(caduca.token) is not None              # ⊕
    reloj.avanza(61)
    assert j.authenticate(caduca.token) is None

    viva = sesion(j, "cred-3", principal="p3")
    assert j.authenticate(viva.token) is not None                # ⊕
    j.rotate_map_generation()
    assert j.authenticate(viva.token) is None                    # falsador 19
    j.close()


def test_una_credencial_desconocida_no_crea_filas_de_coordinacion(tmp_path):
    """Falsador 10, mitad del desconocido: el contador está ACOTADO en filas."""
    j = journal(tmp_path, unknown_rows_max=4, admision=False)  # `admision=False`: este test CUENTA filas de identidad, y las
    # credenciales/sesiones que el arnés usa para abrir la barrera son
    # filas como cualquier otra. No acepta eventos, así que no necesita
    # la puerta abierta.
    for i in range(50):
        with pytest.raises(C.AuthError):
            j.open_session(f"desconocida-{i}")
    con = j._connect()
    assert con.execute("SELECT COUNT(*) c FROM principals").fetchone()["c"] == 0
    assert con.execute("SELECT COUNT(*) c FROM runtime_sessions").fetchone()["c"] == 0
    n = con.execute("SELECT COUNT(*) c FROM unknown_credentials").fetchone()["c"]
    assert n <= 5, f"cota de filas rota: {n}"        # 4 + fila de desborde
    total = con.execute("SELECT SUM(count) s FROM unknown_credentials").fetchone()["s"]
    assert total == 50                               # ⊕ contó las 50 sin una fila por cada una
    j.close()


def _volcado(j) -> str:
    """Todo el texto de todas las tablas, para buscar secretos dentro."""
    con = j._connect()
    trozos = []
    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        for fila in con.execute(f"SELECT * FROM {t}"):
            trozos.append("|".join("" if v is None else str(v) for v in tuple(fila)))
    return "\n".join(trozos)
