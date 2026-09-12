"""Falsador del transporte de `latest` en `ObservationSequenceConflict`.

El rechazo REGRESIVO/REPETIDO de `record_runtime_observation` ya calculaba
DENTRO de la misma transacción el `MAX(supervisor_seq)` para la tupla exacta
(observer_principal, observer_runtime, observer_generation, lane,
target_runtime_instance, target_generation). Ese máximo viaja AHORA en la
propia excepción (`exc.latest`) para que el gateway lo publique como
`max_supervisor_seq` y el supervisor se re-alinee sin segunda consulta.

Lo que este falsador exige, dimensione a dimensione de la tupla:
- OBSERVER: dos observadores sobre el MISMO target llevan máximos DISTINTOS
  y cada rechazo transporta el SUYO (un MAX global colapsaría aquí).
- PAR (TARGET_RUNTIME, TARGET_GENERATION): tras activar la revisión 2, el
  máximo observado contra el par de la revisión 1 NO bloquea ni viaja — el
  `latest` es el del par ACTIVO. Matiz honesto (corrige el claim previo):
  `open_session` cambia `runtime_instance`, NO `credential_generation` —
  sólo una ligadura efectiva sube la generación — así que entre ambos pares
  la variación REAL es el runtime. La generación AISLADA (mismo runtime,
  otra generación) no es ejercitable NI por SQL: la FK compuesta
  (coordination.py:1764) ata cada fila de runtime_observations al par
  (runtime_instance, generation) de una sesión REAL. El par es la unidad
  mínima que el esquema permite variar, y es exactamente la dimensión que
  este falsador ejerce y reclama — ni más ni menos.
- FUERA DE RANGO: la otra rama del mismo tipo (entrada fuera de rango) se
  construye SIN `latest` — `exc.latest is None`, tal como pide el contrato
  HTTP (`max_supervisor_seq` sólo existe en el rechazo regresivo/repetido).

0 DDL: sólo el constructor y el `raise` ya existente cambian en el kernel.
"""
from __future__ import annotations

import pytest

import coordination as C
from ._arnes import GRAMATICA, LANES, PEPPER

_ROLES = [
    {"role": "cto", "layer": 0, "policy_code": "STANDARD"},
    {"role": "infra", "layer": 1, "policy_code": "STANDARD"},
    {"role": "be", "layer": 2, "policy_code": "REVIEW_REQUIRED"},
]
_REPORTS = [{"role": "infra", "reports_to": "cto"},
            {"role": "be", "reports_to": "infra"}]


def _journal(path):
    return C.Journal(str(path), pepper=PEPPER, lane_ledgers=LANES,
                     recipient_resolver=lambda value: (value, False),
                     grammar=GRAMATICA)


def _flota(tmp_path):
    """org + DOS observadores (sup, sup2) + target be-01 activo.

    Misma forma que `test_schema_v7_fleet_control._fleet`, con un segundo
    observador para poder exigir que el máximo transportado es el DEL
    OBSERVER que pregunta, no el del target.
    """
    j = _journal(tmp_path / "coordination.sqlite")
    j.initialize()
    credenciales = [
        ("org", "org", "cto", "llminbox",
         (C.CAP_ORGANIZATION_ACTIVATE, C.CAP_ORGANIZATION_READ,
          C.CAP_ADMISSION_OPERATOR)),
        ("sup", "sup", "infra", "llminbox", (C.CAP_RUNTIME_OBSERVE,)),
        ("sup2", "sup2", "infra", "llminbox", (C.CAP_RUNTIME_OBSERVE,)),
        ("target", "target", "be", "llminbox", ()),
    ]
    bindings, sessions = {}, {}
    for cred, principal, role, lane, capabilities in credenciales:
        bindings[cred] = j.bind_credential(cred, principal=principal, role=role,
                                           lane=lane, capabilities=capabilities)
    sessions = {cred: j.open_session(cred) for cred, *_ in credenciales}
    j.activate_organization(
        sessions["org"].token, revision=1, source_sha256="a" * 64,
        attestation_state="attested", roles=_ROLES, reports=_REPORTS,
        reviewers=[], escalations=[],
        workloads=[{"workload_id": "be-01", "role": "be",
                    "principal_id": bindings["target"].principal_id,
                    "runtime_instance": sessions["target"].runtime_instance,
                    "credential_generation": sessions["target"].generation}])
    return j, bindings, sessions


def _observa(j, token, *, key, seq):
    return j.record_runtime_observation(
        token, workload_id="be-01",
        runtime_instance=j._connect().execute(
            "SELECT w.runtime_instance FROM expected_workloads w"
            " JOIN organization_revisions o ON o.lane=w.lane"
            " AND o.revision=w.organization_revision"
            " WHERE w.lane='llminbox' AND w.workload_id='be-01'"
            " AND o.active=1").fetchone()[0],
        idempotency_key=key, supervisor_seq=seq,
        observation_kind="cycle_ack", reason_code="PROCESS_PRESENT",
        heartbeat_age_ms=0)


def test_cada_observador_transporta_su_propio_max(tmp_path):
    j, _, sesiones = _flota(tmp_path)
    _observa(j, sesiones["sup"].token, key="sup-5", seq=5)
    _observa(j, sesiones["sup2"].token, key="sup2-2", seq=2)

    # REGRESIVO de sup: 3 < 5 → 409 y `latest` = 5 (el de sup; ni el 2 de
    # sup2 ni otro número).
    with pytest.raises(C.ObservationSequenceConflict) as exc:
        _observa(j, sesiones["sup"].token, key="sup-regresivo", seq=3)
    assert exc.value.latest == 5, (
        f"latest={exc.value.latest!r}: el máximo transportado no es el del "
        f"observer que pregunta")

    # REPETIDO de sup2: 2 == 2 → 409 y `latest` = 2 (el de sup2, no el 5).
    with pytest.raises(C.ObservationSequenceConflict) as exc:
        _observa(j, sesiones["sup2"].token, key="sup2-repetido", seq=2)
    assert exc.value.latest == 2

    # El tipo NO muta entre rechazos: cada excepción es independiente.
    with pytest.raises(C.ObservationSequenceConflict) as exc_sup:
        _observa(j, sesiones["sup"].token, key="sup-otra-vez", seq=5)
    assert exc_sup.value.latest == 5


def test_el_max_del_par_target_anterior_no_viaja_tras_revision_2(tmp_path):
    """La tupla del MAX escopa por (target_runtime_instance,
    target_generation): el máximo del par de la revisión 1 no viaja a la 2.

    Reclamo exacto (corregido tras revisión de f12c506): `open_session`
    cambia runtime_instance, NO credential_generation — la variación real
    entre el par antiguo y el activo es el runtime (la generación numérica
    es la misma); y la generación aislada no es ejercitable ni por SQL por
    la FK compuesta coordination.py:1764. Esto acredita la escopa del PAR,
    no una dimensión de generación aislada.
    """
    j, bindings, sesiones = _flota(tmp_path)
    _observa(j, sesiones["sup"].token, key="g1-5", seq=5)

    # Revisión 2 del target: `open_session` crea runtime_instance NUEVO;
    # credential_generation NO cambia (sólo una ligadura efectiva la sube).
    base = j._clock()
    j._clock = lambda: base + 50
    nuevo_target = j.open_session("target")
    j.activate_organization(
        sesiones["org"].token, revision=2, source_sha256="f" * 64,
        attestation_state="attested", roles=_ROLES, reports=_REPORTS,
        reviewers=[], escalations=[],
        workloads=[{"workload_id": "be-01", "role": "be",
                    "principal_id": bindings["target"].principal_id,
                    "runtime_instance": nuevo_target.runtime_instance,
                    "credential_generation": nuevo_target.generation}])

    # El máximo del PAR anterior (runtime de la revisión 1) NO bloquea en
    # el par activo: seq 1 se acepta.
    _observa(j, sesiones["sup"].token, key="g2-1", seq=1)
    _observa(j, sesiones["sup"].token, key="g2-4", seq=4)

    # Y el rechazo regresivo del par activo transporta el máximo del PAR
    # ACTIVO (4), no el del par de la revisión 1 (5): la tupla exacta
    # escopa por target_runtime_instance y target_generation.
    with pytest.raises(C.ObservationSequenceConflict) as exc:
        _observa(j, sesiones["sup"].token, key="g2-regresivo", seq=2)
    assert exc.value.latest == 4, (
        f"latest={exc.value.latest!r}: el máximo viaja del par de la "
        f"revisión anterior, no del par activo")


def test_fuera_de_rango_se_lanza_sin_latest(tmp_path):
    j, _, sesiones = _flota(tmp_path)
    _observa(j, sesiones["sup"].token, key="sup-3", seq=3)
    for mala in (C.MAX_SUPERVISOR_SEQ + 1, 0, True):
        with pytest.raises(C.ObservationSequenceConflict) as exc:
            _observa(j, sesiones["sup"].token, key=f"fuera-{mala!r}", seq=mala)
        assert exc.value.latest is None, (
            f"seq={mala!r}: la rama fuera-de-rango NO transporta latest "
            f"(el contrato HTTP sólo publica max_supervisor_seq en el "
            f"rechazo regresivo/repetido)")


def test_constructor_cuida_su_propio_contrato():
    """`latest` existe para transportar un MAX real: el kernel no deja
    entrar bool/None-implícito/fuera de rango — el mismo predicado que el
    campo HTTP `max_supervisor_seq` exigirá (`type(int)`, nunca bool)."""
    with pytest.raises(ValueError):
        C.ObservationSequenceConflict("x", latest=True)
    with pytest.raises(ValueError):
        C.ObservationSequenceConflict("x", latest=0)
    with pytest.raises(ValueError):
        C.ObservationSequenceConflict("x", latest="3")
    with pytest.raises(ValueError):
        C.ObservationSequenceConflict("x", latest=C.MAX_SUPERVISOR_SEQ + 1)
    assert C.ObservationSequenceConflict("x").latest is None
    assert C.ObservationSequenceConflict(
        "x", latest=7).latest == 7
