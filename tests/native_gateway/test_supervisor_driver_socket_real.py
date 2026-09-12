"""Driver completo con HTTP, Journal y proceso hijo reales.

Sólo el reloj de sesión y la espera son controlados. La autenticación, la
lectura del objetivo, el muestreo del PID y el envío HTTP son los del producto.
Estos casos acreditan vueltas finitas y detección de salida; no renovación de
sesión, recuperación ni una flota desplegada.
"""
from __future__ import annotations

import hashlib
import json
import stat
import subprocess
import sys

import pytest

import supervisor_driver as D
from tests.native_gateway.test_supervisor_socket_real import (  # noqa: F401
    fleet, servidor,
)


def _declara_observador(journal, sessions):
    """El driver publicado exige que también conste su runtime en la revisión."""
    operator = sessions["operator-a"]
    observer = sessions["observer-a"]
    snapshot = journal.organization(operator.token)
    keys = {
        "roles": ("role", "layer", "policy_code"),
        "reports": ("role", "reports_to"),
        "reviewers": ("role", "reviewer_role"),
        "escalations": ("role", "trigger_code", "target_role"),
        "workloads": ("workload_id", "role", "principal_id",
                      "runtime_instance", "credential_generation"),
    }
    records = {
        section: [{key: row[key] for key in fields} for row in snapshot[section]]
        for section, fields in keys.items()
    }
    records["workloads"].append({
        "workload_id": "observer-01", "role": observer.role,
        "principal_id": observer.principal_id,
        "runtime_instance": observer.runtime_instance,
        "credential_generation": observer.generation,
    })
    digest = hashlib.sha256(json.dumps(
        records, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    journal.activate_organization(
        operator.token, revision=snapshot["revision"]["revision"] + 1,
        source_sha256=digest, attestation_state="attested", **records)


def _termina_hijo(proc):
    """Cierra sólo el proceso creado por este test, también ante una aserción."""
    if not proc.stdin.closed:
        proc.stdin.close()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


@pytest.mark.parametrize("salida_entre_vueltas", [False, True], ids=["vivo", "salida"])
def test_driver_observa_proceso_real_y_persiste_estado_por_http(
        servidor, fleet, tmp_path, capsys, salida_entre_vueltas):
    base, journal, sessions = servidor
    clock = fleet[3]
    observer, target = sessions["observer-a"], sessions["target-a"]
    _declara_observador(journal, sessions)
    session_file = tmp_path / "driver-session.json"

    # El hijo permanece vivo hasta EOF en su stdin: no depende de un sleep largo.
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert proc.poll() is None

        def espera(seconds):
            if salida_entre_vueltas and proc.poll() is None:
                proc.stdin.close()
                assert proc.wait(timeout=5) == 0
            clock[0] += seconds

        driver = D.Driver(
            base, observer.token, [(target.runtime_instance, proc.pid)],
            fichero_sesion=str(session_file), vueltas=2, intervalo_s=0.01,
            timeout_s=2.0, reloj=lambda: clock[0], espera=espera)
        assert driver.corre() == D.EX_OK

        rows = journal._connect().execute(
            "SELECT * FROM runtime_observations ORDER BY supervisor_seq").fetchall()
        assert len(rows) == 2
        assert [row["supervisor_seq"] for row in rows] == [1, 2]
        assert [row["observation_kind"] for row in rows] == [
            "cycle_ack", "exited" if salida_entre_vueltas else "cycle_ack"]
        for row in rows:
            assert row["lane"] == observer.lane
            assert row["observer_principal"] == observer.principal_id
            assert row["observer_runtime"] == observer.runtime_instance
            assert row["observer_generation"] == observer.generation
            assert row["target_runtime_instance"] == target.runtime_instance
            assert row["target_generation"] == target.generation
            assert row["workload_id"] == "be-01"
        assert rows[0]["reason_code"] == "PROCESS_PRESENT"
        if salida_entre_vueltas:
            assert rows[1]["reason_code"] == "PROCESS_EXITED"
            # El sampler no es el padre que recogió el exit code: no lo inventa.
            assert rows[1]["exit_code"] is None
            assert proc.returncode == 0
        else:
            assert proc.poll() is None

        status = journal.runtime_status(observer.token, target.runtime_instance)
        assert status["status"] == ("stopped" if salida_entre_vueltas else "fresh")
        session = json.loads(session_file.read_text())
        assert session["estado"] == "completada"
        assert session["vueltas_completadas"] == 2
        assert session["objetivos"][0]["pid"] == proc.pid
        assert session["objetivos"][0]["arranque"]
        assert stat.S_IMODE(session_file.stat().st_mode) == 0o600
        output = capsys.readouterr()
        assert observer.token not in output.out + output.err + session_file.read_text()
    finally:
        _termina_hijo(proc)
    assert proc.poll() is not None
