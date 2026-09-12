"""M2 no puede convertir una avería de Search en una parada de Core.

El FTS es reconstruible; el Journal no. Esta costura ejercita el arranque real de
FastAPI y el siguiente commit durable del kernel en el mismo proceso para que la
separación no quede demostrada sólo por dos unit tests sin composición.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import coordination as C
import observability as O
import search_store as S
from tests.journal._arnes import (GRAMATICA, INTENT, LANES, PEPPER,
                                 abre_admision, censo, sesion)
from tests.pytest.conftest import construir


@pytest.mark.parametrize(
    ("averia", "readiness", "detail"),
    [
        ("ausente", None, "índice de búsqueda no listo"),
        ("corrupto", lambda _store: (_ for _ in ()).throw(
            sqlite3.DatabaseError("database disk image is malformed")),
         "índice de búsqueda no disponible"),
        ("solo_lectura", lambda _store: (_ for _ in ()).throw(
            sqlite3.OperationalError("attempt to write a readonly database")),
         "índice de búsqueda no disponible"),
    ],
)
def test_search_degradado_no_impide_el_siguiente_evento_y_su_recibo(
        tmp_path, monkeypatch, averia, readiness, detail):
    raiz_servicio = tmp_path / "search"
    raiz_servicio.mkdir()
    servicio = construir(
        raiz_servicio, monkeypatch,
        extra_env={"LLMINBOX_SEARCH_CURSOR_KEY": "k" * 32})
    monkeypatch.setattr(servicio, "CREDENCIALES", {
        "cred-http": {"rol": "be", "carril": "demo", "principal_id": "p-http"}})

    # Si el lifespan vuelve a reconstruir, este test falla aunque el rebuild pareciera
    # terminar rápido sobre el corpus diminuto del arnés.
    monkeypatch.setattr(S.SearchStore, "rebuild", lambda _store: (_ for _ in ()).throw(
        AssertionError("el rebuild de Search no pertenece al lifespan")))
    if readiness is not None:
        monkeypatch.setattr(S.SearchStore, "readiness", readiness)

    raiz_core = tmp_path / "core"
    raiz_core.mkdir()
    journal = C.Journal(str(raiz_core / "coordination.sqlite"), pepper=PEPPER,
                        lane_ledgers=LANES, grammar=GRAMATICA,
                        recipient_resolver=censo)
    journal.initialize()
    # La barrera nace cerrada y este test ACEPTA un evento: se abre como
    # operador, antes de emitir la sesión de runtime (ligar sube generación).
    abre_admision(journal, lanes=["llminbox"])
    runtime = sesion(journal)

    try:
        with TestClient(servicio.app) as cliente:
            degradada = cliente.get(
                "/search?q=prueba",
                headers={"X-Llminbox-Token": "cred-http"})
            assert degradada.status_code == 503, averia
            assert degradada.json() == {"detail": detail}

            aceptado = journal.accept_event(
                runtime.token, idempotency_key=f"despues-search-{averia}",
                intent=INTENT, ledger="llminbox")
            recibo = journal.receipt_for_event(runtime.token, aceptado.event_id)

        assert recibo["receipt_id"] == aceptado.receipt_id
        assert recibo["current_state"] == "accepted"
        assert journal.health()["writable"] is True
        assert servicio.SOLO_LECTURA["activo"] is False
    finally:
        journal.close()


def test_la_sonda_de_arranque_es_inmutable_y_no_crea_sidecars(tmp_path, monkeypatch):
    raiz_servicio = tmp_path / "servicio con ? y #"
    raiz_servicio.mkdir()
    servicio = construir(
        raiz_servicio, monkeypatch,
        extra_env={"LLMINBOX_SEARCH_CURSOR_KEY": "k" * 32})
    ruta = os.environ["LLMINBOX_DB"]
    with TestClient(servicio.app):
        servicio.barrido()
        con = servicio.db()
        try:
            servicio.preparar_busqueda_publica(con, reconstruir=True)
            con.execute("CREATE TABLE control (v TEXT)")
            con.execute("INSERT INTO control VALUES ('intacto')")
            con.commit()
        finally:
            con.close()
    antes = {p.name: hashlib.sha256(p.read_bytes()).digest()
             for p in raiz_servicio.iterdir() if p.is_file()}

    sonda = servicio._conexion_busqueda_solo_lectura()
    try:
        assert sonda.execute("PRAGMA query_only").fetchone()[0] == 1
        assert sonda.execute("SELECT v FROM control").fetchone()[0] == "intacto"
        assert servicio._comprueba_busqueda_publica(sonda) == "ready"
        with pytest.raises(sqlite3.OperationalError):
            sonda.execute("INSERT INTO control VALUES ('mutado')")
    finally:
        sonda.close()

    despues_sonda = {p.name: hashlib.sha256(p.read_bytes()).digest()
                     for p in raiz_servicio.iterdir() if p.is_file()}
    assert despues_sonda == antes

    # La operación mutable sobre una conexión físicamente RO falla de verdad; la sonda
    # no confunde ese fallo con el estado del Journal ni altera un solo byte de Search.
    db_antes_ro = hashlib.sha256(Path(ruta).read_bytes()).digest()
    ro = sqlite3.connect(Path(ruta).resolve().as_uri() + "?mode=ro", uri=True)
    ro.row_factory = sqlite3.Row
    servicio._registra_udf_busqueda(ro)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            servicio.preparar_busqueda_publica(ro, reconstruir=True)
    finally:
        ro.close()
    assert hashlib.sha256(Path(ruta).read_bytes()).digest() == db_antes_ro


def test_sonda_de_path_ausente_no_crea_db_wal_ni_shm(tmp_path, monkeypatch):
    raiz = tmp_path / "ausente con ? y #"
    raiz.mkdir()
    servicio = construir(
        raiz, monkeypatch,
        extra_env={"LLMINBOX_SEARCH_CURSOR_KEY": "k" * 32})
    ruta = raiz / "todavia no existe.sqlite"
    monkeypatch.setattr(servicio, "DB", str(ruta))

    assert servicio._sonda_busqueda_publica() == "unavailable"
    assert not ruta.exists()
    assert not Path(str(ruta) + "-wal").exists()
    assert not Path(str(ruta) + "-shm").exists()


def test_solo_el_arco_schema_1_a_2_se_clasifica_como_stale_migrable():
    import servicio as servicio_estado

    base = {"ready": False, "state": "ready", "generation": "a" * 32}
    assert servicio_estado._clasifica_readiness_busqueda(
        {**base, "schema_v": 1, "schema_v_code": 2}) == "stale"
    assert servicio_estado._clasifica_readiness_busqueda(
        {**base, "schema_v": 0, "schema_v_code": 1}) == "corrupt"
    assert servicio_estado._clasifica_readiness_busqueda(
        {**base, "schema_v": 0, "schema_v_code": 2}) == "corrupt"


def test_conexion_cierra_descriptor_si_falla_su_configuracion(tmp_path, monkeypatch):
    raiz = tmp_path / "descriptor"
    raiz.mkdir()
    servicio = construir(
        raiz, monkeypatch,
        extra_env={"LLMINBOX_SEARCH_CURSOR_KEY": "k" * 32})
    conectar_real = sqlite3.connect
    observada = {"fallo_post_guard": False, "temp_denegada": False}

    class ConexionFallaPostGuard(sqlite3.Connection):
        """Inyecta el fallo al llegar al PRAGMA posterior al helper Search.

        No inspecciona callbacks ni fuente: antes de fallar intenta CREATE TEMP. Con
        schema v5, que esa sentencia sea denegada es la prueba conductual de que el
        guard ya instaló/reemplazó el authorizer anterior.
        """
        def execute(self, sql, parameters=(), /):
            if sql == "PRAGMA query_only=ON":
                try:
                    super().execute(
                        "CREATE TEMP TABLE lifecycle_guard_probe(v INTEGER)")
                except sqlite3.DatabaseError as e:
                    assert "not authorized" in str(e)
                    observada["temp_denegada"] = True
                else:
                    # La rama aislada conserva schema v1 y no publica guard. Se limpia
                    # el control antes de inyectar el mismo fallo post-configuración;
                    # el gate compuesto v5 exige abajo la denegación real.
                    super().execute("DROP TABLE temp.lifecycle_guard_probe")
                observada["fallo_post_guard"] = True
                raise sqlite3.OperationalError("fallo determinista post-guard")
            return super().execute(sql, parameters)

    abierta = {}

    def conexion_que_falla_despues_del_guard(*_args, **_kwargs):
        con = conectar_real(":memory:", factory=ConexionFallaPostGuard)
        abierta["con"] = con
        return con

    monkeypatch.setattr(servicio.sqlite3, "connect", conexion_que_falla_despues_del_guard)
    with pytest.raises(sqlite3.OperationalError, match="fallo determinista post-guard"):
        servicio._conexion_busqueda_solo_lectura()
    assert observada["fallo_post_guard"] is True
    if getattr(S, "SEARCH_SCHEMA_V", 1) >= 2:
        assert observada["temp_denegada"] is True
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        abierta["con"].execute("SELECT 1")


def test_cli_explicita_convierte_search_fresco_de_503_a_200(tmp_path, monkeypatch):
    raiz = tmp_path / "operador"
    raiz.mkdir()
    servicio = construir(
        raiz, monkeypatch,
        extra_env={"LLMINBOX_SEARCH_CURSOR_KEY": "k" * 32})
    monkeypatch.setattr(servicio, "CREDENCIALES", {
        "cred-http": {"rol": "be", "carril": "demo", "principal_id": "p-http"}})

    with TestClient(servicio.app) as cliente:
        servicio.barrido()
        antes = cliente.get("/search?q=cuerpo",
                            headers={"X-Llminbox-Token": "cred-http"})
        assert antes.status_code == 503
        con = sqlite3.connect(os.environ["LLMINBOX_DB"])
        try:
            objetos = con.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'search_%'").fetchall()
        finally:
            con.close()
        assert objetos == [], "el lifespan creó objetos Search antes de la orden"

        operacion = subprocess.run(
            [sys.executable, "-m", "servicio", "search", "rebuild"],
            cwd=Path(__file__).resolve().parents[2], env=os.environ.copy(),
            capture_output=True, text=True, timeout=10)
        assert operacion.returncode == 0, operacion.stderr
        salida = json.loads(operacion.stdout)
        assert salida["ok"] is True and salida["operation"] == "search.rebuild"
        assert salida["state"] == "ready" and salida["schema_v"] == S.SEARCH_SCHEMA_V

    # Segundo arranque REAL: una Search configurada y válida no se degrada a 503.
    with TestClient(servicio.app) as cliente:
        despues = cliente.get("/search?q=cuerpo",
                              headers={"X-Llminbox-Token": "cred-http"})
        assert despues.status_code == 200, despues.text
        assert despues.json()["filas"]


def test_objeto_search_roto_real_degrada_emite_sensor_y_core_acepta(
        tmp_path, monkeypatch):
    raiz = tmp_path / "search-real"
    raiz.mkdir()
    servicio = construir(
        raiz, monkeypatch,
        extra_env={"LLMINBOX_SEARCH_CURSOR_KEY": "k" * 32})
    monkeypatch.setattr(servicio, "CREDENCIALES", {
        "cred-http": {"rol": "be", "carril": "demo", "principal_id": "p-http"}})

    with TestClient(servicio.app):
        servicio.barrido()
        con = servicio.db()
        try:
            servicio.preparar_busqueda_publica(con, reconstruir=True)
        finally:
            con.close()
    con = sqlite3.connect(os.environ["LLMINBOX_DB"])
    con.execute("DROP TRIGGER search_ai")
    con.commit()
    con.close()

    exp = O.MemoryExporter()
    servicio.configura_observabilidad(exporter=exp, enabled=True)
    raiz_core = tmp_path / "core-real"
    raiz_core.mkdir()
    journal = C.Journal(str(raiz_core / "coordination.sqlite"), pepper=PEPPER,
                        lane_ledgers=LANES, grammar=GRAMATICA,
                        recipient_resolver=censo)
    journal.initialize()
    # La barrera nace cerrada y este test ACEPTA un evento: se abre como
    # operador, antes de emitir la sesión de runtime (ligar sube generación).
    abre_admision(journal, lanes=["llminbox"])
    runtime = sesion(journal)
    try:
        with TestClient(servicio.app) as cliente:
            degradada = cliente.get(
                "/search?q=cuerpo", headers={"X-Llminbox-Token": "cred-http"})
            assert degradada.status_code == 503
            assert degradada.json() == {"detail": "índice de búsqueda no listo"}
            aceptado = journal.accept_event(
                runtime.token, idempotency_key="despues-trigger-roto",
                intent=INTENT, ledger="llminbox")
            recibo = journal.receipt_for_event(runtime.token, aceptado.event_id)
        assert recibo["receipt_id"] == aceptado.receipt_id
        assert journal.health()["writable"] is True
        logs = [s for s in exp.logs() if s.nombre == "repair.required"]
        assert len(logs) == 1
        assert logs[0].attributes == {
            "component": "search", "state": "corrupt", "action": "search_rebuild"}
        assert logs[0].resource["llminbox.principal"] == "llminbox-gateway"
        assert logs[0].resource["llminbox.lane"] == "control"
    finally:
        journal.close()


def test_schema_too_new_fisico_da_503_y_sensor_exacto(tmp_path, monkeypatch):
    schema_v = S.SEARCH_SCHEMA_V + 1
    estado_sensor = "too_new"
    raiz = tmp_path / f"schema-{estado_sensor}"
    raiz.mkdir()
    servicio = construir(
        raiz, monkeypatch,
        extra_env={"LLMINBOX_SEARCH_CURSOR_KEY": "k" * 32})
    monkeypatch.setattr(servicio, "CREDENCIALES", {
        "cred-http": {"rol": "be", "carril": "demo", "principal_id": "p-http"}})

    with TestClient(servicio.app):
        servicio.barrido()
        con = servicio.db()
        try:
            servicio.preparar_busqueda_publica(con, reconstruir=True)
        finally:
            con.close()
    con = sqlite3.connect(os.environ["LLMINBOX_DB"])
    con.execute("UPDATE search_state SET v=? WHERE k='schema_v'", (str(schema_v),))
    con.commit()
    con.close()

    exp = O.MemoryExporter()
    servicio.configura_observabilidad(exporter=exp, enabled=True)
    with TestClient(servicio.app) as cliente:
        respuesta = cliente.get(
            "/search?q=cuerpo", headers={"X-Llminbox-Token": "cred-http"})
    assert respuesta.status_code == 503
    assert respuesta.json() == {"detail": "índice de búsqueda no listo"}
    logs = [s for s in exp.logs() if s.nombre == "repair.required"]
    assert len(logs) == 1
    assert logs[0].attributes == {
        "component": "search", "state": estado_sensor,
        "action": ("search_migrate_1_2" if estado_sensor == "stale"
                   else "search_rebuild")}


def test_hunk_portable_llmi_deja_operaciones_search_alcanzables(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    binario = tmp_path / "bin"
    binario.mkdir()
    registro = tmp_path / "docker.log"
    docker = binario / "docker"
    docker.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$DOCKER_LOG\"\nexit 0\n")
    docker.chmod(0o755)
    casa = tmp_path / "home"
    casa.mkdir()
    env = os.environ.copy()
    env.update({"HOME": str(casa), "PATH": f"{binario}:{env['PATH']}",
                "DOCKER_LOG": str(registro), "LLMINBOX_NAME": "instancia-search"})

    ayuda = subprocess.run(
        [str(repo / "llmi"), "--help"], cwd=repo, env=env,
        capture_output=True, text=True, timeout=10)
    assert ayuda.returncode == 0
    assert "llmi search rebuild" in ayuda.stdout
    assert "llmi search migrate 1 2" in ayuda.stdout

    for argumentos in (("rebuild",), ("migrate", "1", "2")):
        resultado = subprocess.run(
            [str(repo / "llmi"), "search", *argumentos], cwd=repo, env=env,
            capture_output=True, text=True, timeout=10)
        assert resultado.returncode == 0, resultado.stderr
    assert registro.read_text().splitlines() == [
        "inspect instancia-search",
        "exec instancia-search python3 -m servicio search rebuild",
        "inspect instancia-search",
        "exec instancia-search python3 -m servicio search migrate 1 2",
    ]
    invalida = subprocess.run(
        [str(repo / "llmi"), "search", "migrate", "2", "3"], cwd=repo, env=env,
        capture_output=True, text=True, timeout=10)
    assert invalida.returncode == 2
    assert registro.read_text().splitlines()[-1] == (
        "exec instancia-search python3 -m servicio search migrate 1 2")


def test_operacion_migrate_despacha_arco_explicito_y_devuelve_json_estable(
        tmp_path, monkeypatch, capsys):
    raiz = tmp_path / "migrate-dispatch"
    raiz.mkdir()
    servicio = construir(
        raiz, monkeypatch,
        extra_env={"LLMINBOX_SEARCH_CURSOR_KEY": "k" * 32})
    llamadas = []

    def migrar(_store):
        llamadas.append("1->2")
        return "a" * 32

    def listo(_store):
        return {"ready": True, "state": "ready", "schema_v": 2,
                "schema_v_code": 2, "generation": "a" * 32}

    monkeypatch.setattr(S.SearchStore, "migrate_schema_v1_to_v2", migrar,
                        raising=False)
    monkeypatch.setattr(S.SearchStore, "readiness", listo)
    assert servicio.main_administracion(["search", "migrate", "1", "2"]) == 0
    assert llamadas == ["1->2"]
    assert json.loads(capsys.readouterr().out) == {
        "ok": True, "operation": "search.migrate", "from": 1, "to": 2,
        "state": "ready", "schema_v": 2, "generation": "a" * 32,
    }


def test_operacion_migrate_sin_schema_compatible_falla_cerrada(
        tmp_path, monkeypatch, capsys):
    raiz = tmp_path / "migrate-unsupported"
    raiz.mkdir()
    servicio = construir(
        raiz, monkeypatch,
        extra_env={"LLMINBOX_SEARCH_CURSOR_KEY": "k" * 32})
    monkeypatch.delattr(S.SearchStore, "migrate_schema_v1_to_v2", raising=False)

    assert servicio.main_administracion(["search", "migrate", "1", "2"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "ok": False, "operation": "search.migrate",
        "error": "search_migration_unsupported",
    }


def test_operacion_migrate_cierra_conexion_si_el_dominio_rechaza(
        tmp_path, monkeypatch, capsys):
    raiz = tmp_path / "migrate-close"
    raiz.mkdir()
    servicio = construir(
        raiz, monkeypatch,
        extra_env={"LLMINBOX_SEARCH_CURSOR_KEY": "k" * 32})
    con = sqlite3.connect(raiz / "admin.sqlite")
    con.row_factory = sqlite3.Row
    monkeypatch.setattr(servicio, "db", lambda: con)

    def rechaza(_store):
        raise S.SearchStale("schema v1 exige migración")

    monkeypatch.setattr(S.SearchStore, "migrate_schema_v1_to_v2", rechaza,
                        raising=False)
    assert servicio.main_administracion(["search", "migrate", "1", "2"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "ok": False, "operation": "search.migrate", "error": "SEARCH_STALE"}
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        con.execute("SELECT 1")


@pytest.mark.parametrize("tipo", [AssertionError, ValueError])
def test_un_fallo_de_programacion_en_search_no_se_disfraza_de_degradacion(
        tmp_path, monkeypatch, tipo):
    raiz_servicio = tmp_path / "search"
    raiz_servicio.mkdir()
    servicio = construir(
        raiz_servicio, monkeypatch,
        extra_env={"LLMINBOX_SEARCH_CURSOR_KEY": "k" * 32})

    def defecto(_store):
        raise tipo("defecto de programación")

    monkeypatch.setattr(S.SearchStore, "readiness", defecto)
    with pytest.raises(tipo, match="defecto de programación"):
        with TestClient(servicio.app):
            pass


def test_solo_el_journal_ro_rechaza_la_escritura_core(tmp_path):
    """Control discriminante: Search roto no basta; Journal RO sí bloquea Core."""
    raiz = tmp_path / "journal-ro"
    raiz.mkdir()
    journal = C.Journal(str(raiz / "coordination.sqlite"), pepper=PEPPER,
                        lane_ledgers=LANES, grammar=GRAMATICA,
                        recipient_resolver=censo)
    journal.initialize()
    # La barrera nace cerrada y este test ACEPTA un evento: se abre como
    # operador, antes de emitir la sesión de runtime (ligar sube generación).
    abre_admision(journal, lanes=["llminbox"])
    runtime = sesion(journal)
    journal.accept_event(runtime.token, idempotency_key="antes-ro",
                         intent=INTENT, ledger="llminbox")
    journal.close()
    for fichero in raiz.iterdir():
        fichero.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    raiz.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        journal = C.Journal(str(raiz / "coordination.sqlite"), pepper=PEPPER,
                            lane_ledgers=LANES, grammar=GRAMATICA,
                            recipient_resolver=censo)
        journal.initialize()
        assert journal.health()["writable"] is False
        with pytest.raises(C.JournalReadOnly):
            journal.accept_event(runtime.token, idempotency_key="journal-ro",
                                 intent=INTENT, ledger="llminbox")
    finally:
        journal.close()
        raiz.chmod(stat.S_IRWXU)
        for fichero in raiz.iterdir():
            fichero.chmod(stat.S_IRUSR | stat.S_IWUSR)
