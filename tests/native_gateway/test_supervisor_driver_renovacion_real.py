"""Driver CONTINUO con renovación REAL del gateway: HTTP, Journal y proceso
hijo reales.

Sólo el reloj de sesión y la espera son controlados (`clock[0]` del Journal y
`espera` que lo avanza): la autenticación, el POST /sessions/refresh que ROTA
(sin sustituir `consulta` ni `refresca`), el whoami del hijo, el muestreo del
PID y el envío HTTP son los del producto. Acredita dos renovaciones REALES
consecutivas con autoridad conservada y observación posterior en filas
duras; no recuperación ni una flota desplegada.

⚠️ Este fichero NO corre en el Mac del carril (sentinela de capacidad): la
ejecuta root en GitHub. En el Mac sólo se verificó ast+census.
"""
from __future__ import annotations

import hashlib
import json
import stat
import subprocess
import sys

import pytest

import supervisor_driver as D
import supervisor_transport as T
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


def test_driver_continuo_renueva_dos_veces_con_el_gateway_real(
        servidor, fleet, tmp_path, capsys, monkeypatch):
    """Dos REFRESCOS REALES seguidos (el gateway rota token y rti hijo) con
    autoridad CONSERVADA — mismo principal/role/lane/capacidades/generación,
    exigida por la validación doble del driver — y observación POSTERIOR a la
    última rotación en filas durables. Sin dormir minutos: la espera del
    driver avanza el reloj controlado en rodajas."""
    base, journal, sessions = servidor
    clock = fleet[3]
    observer, target = sessions["observer-a"], sessions["target-a"]
    _declara_observador(journal, sessions)
    session_file = tmp_path / "driver-session.json"

    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert proc.poll() is None

        # Sonda sobre el envío HTTP REAL (se delega, no se sustituye): cuenta
        # observaciones y pide la señal tras la POSTERIOR a la 2ª rotación.
        envios = []
        real_envio = T.TransporteSupervisor._enviar_http

        def sonda(self, url, cuerpo, clave):
            resultado = real_envio(self, url, cuerpo, clave)
            envios.append((self._token, dict(cuerpo)))
            if len(envios) >= 3:
                driver._parar = True
            return resultado
        monkeypatch.setattr(T.TransporteSupervisor, "_enviar_http", sonda)

        # ttl 300 y margen 30 (margen < ttl). Tras cada observación hacemos
        # saltar el reloj controlado al margen de la sesión vigente; así la
        # siguiente vuelta fuerza una renovación real sin ejecutar miles de
        # vueltas ficticias ni acortar el contrato de producción.
        def espera_hasta_margen(_segundos):
            clock[0] = driver.deadline - driver._margen + 0.001

        driver = D.Driver(
            base, observer.token, [(target.runtime_instance, proc.pid)],
            fichero_sesion=str(session_file), continuo=True, ttl_s=300,
            margen_s=30.0, intervalo_s=0.01, timeout_s=2.0,
            reloj=lambda: clock[0], espera=espera_hasta_margen)
        codigo = driver.corre()

        assert codigo == D.EX_OK
        salida = capsys.readouterr()
        assert salida.out.count("REFRESCO:") == 2, "dos renovaciones reales"
        assert len(envios) == 3, "una observación por identidad (3 identidades)"
        assert driver.observer_rti != observer.runtime_instance, "rotó de verdad"

        filas = journal._connect().execute(
            "SELECT * FROM runtime_observations ORDER BY rowid").fetchall()
        assert len(filas) == 3
        # Secuencia NUEVA por identidad: 1 tras cada rotación, jamás 2 o 3.
        assert [r["supervisor_seq"] for r in filas] == [1, 1, 1]
        rtis = [r["observer_runtime"] for r in filas]
        assert len(set(rtis)) == 3, "cada observación bajo SU identidad"
        assert rtis[0] == observer.runtime_instance and rtis[2] == driver.observer_rti
        for fila in filas:
            # La autoridad conservada, DEMOSTRADA en filas durables: ni una
            # sola fila con principal, lane o generación del padre distinta.
            assert fila["observer_principal"] == observer.principal_id
            assert fila["lane"] == observer.lane
            assert fila["observer_generation"] == observer.generation
            assert fila["target_runtime_instance"] == target.runtime_instance
            assert fila["target_generation"] == target.generation
            assert fila["workload_id"] == "be-01"
        assert [r["reason_code"] for r in filas] == ["PROCESS_PRESENT"] * 3

        # El token HIJO (adoptado) sigue siendo una identidad viva con permiso
        # de lectura sobre el objetivo: posterioridad real, no declarada.
        status = journal.runtime_status(driver._token, target.runtime_instance)
        assert status["runtime_instance"] == target.runtime_instance

        libro = json.loads(session_file.read_text())
        assert libro["estado"] == "parado_por_senal"
        assert libro["vueltas_completadas"] == 3
        assert libro["objetivos"][0]["pid"] == proc.pid, "vínculo conservado"
        assert libro["objetivos"][0]["arranque"]
        assert libro["pendientes_al_cierre"] == []
        assert stat.S_IMODE(session_file.stat().st_mode) == 0o600
        todo = salida.out + salida.err + session_file.read_text()
        assert observer.token not in todo, "el token padre rota y no se publica"
        # Ni el padre ni NINGÚN hijo (patrón de token de sesión) en la salida.
        for token_enviado, _cuerpo in envios:
            assert token_enviado not in todo
    finally:
        _termina_hijo(proc)
    assert proc.poll() is not None
