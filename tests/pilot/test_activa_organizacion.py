"""Comprobaciones del activador de organización y su contrato de operador.

Ejercitan sesiones reales en bases temporales: apertura restringida sin crear
ni migrar, revisión y workload durables, monotonía, digest canónico y lectura
nativa opcional. Los errores locales se comprueban antes de abrir el Journal,
y los fallos de apertura deben cerrar sus recursos.

La CI publica los resultados sobre el SHA ejecutado. Estos casos no acreditan
una activación sobre un despliegue real ni la operación de una flota por red.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sqlite3
import sys

import pytest

_RAIZ = pathlib.Path(__file__).resolve().parents[2]
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))

import coordination as C  # noqa: E402
from tests.journal._arnes import GRAMATICA, LANES, PEPPER, censo  # noqa: E402
from tests.journal.test_schema_v7_fleet_control import _fixture_v6  # noqa: E402

import tools.activa_organizacion as activador  # noqa: E402

LANE_OPERADOR = "llminbox"


def _journal_v7(path: pathlib.Path) -> C.Journal:
    """Base v7 con el constructor ESTÁNDAR (representa a cualquier llamante)."""
    j = C.Journal(str(path), pepper=PEPPER, lane_ledgers=LANES,
                  recipient_resolver=censo, grammar=GRAMATICA)
    j.initialize()
    return j


def _sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _organigrama(*, revision: int = 1, lane_ajena: bool = False) -> dict:
    """Snapshot completo conforme a los vocabularios cerrados del kernel."""
    doc = {
        "revision": revision,
        "attestation_state": "attested",
        "roles": [
            {"role": "cto", "layer": 0, "policy_code": "STANDARD"},
            {"role": "infra", "layer": 1, "policy_code": "STANDARD"},
            {"role": "be", "layer": 2, "policy_code": "REVIEW_REQUIRED"},
        ],
        "reports": [{"role": "infra", "reports_to": "cto"},
                    {"role": "be", "reports_to": "infra"}],
        "reviewers": [{"role": "be", "reviewer_role": "infra"}],
        "escalations": [{"role": "be", "trigger_code": "BLOCKED",
                         "target_role": "infra"}],
        "workloads": [],
    }
    if lane_ajena:
        doc["lane"] = "carril-ajeno"  # ⛔ fuera del vocabulario: fail closed
    return doc


def _ficheros(tmp_path: pathlib.Path, doc: dict,
              credencial: str = "op",
              pepper: bytes = PEPPER) -> dict:
    db = tmp_path / "coordination.sqlite"
    pp = tmp_path / "pepper.file"
    sn = tmp_path / "snapshot.json"
    cr = tmp_path / "credencial.file"
    pp.write_bytes(pepper)
    sn.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    cr.write_text(credencial + "\n", encoding="utf-8")
    return {"db": db, "pp": pp, "sn": sn, "cr": cr}


def _corre(f: dict, *, capsys) -> int:
    return activador.main(["--db", str(f["db"]), "--pepper-file", str(f["pp"]),
                           "--snapshot", str(f["sn"]),
                           "--credencial-file", str(f["cr"])])


def _recibo(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def _operador_ligado(j: C.Journal, *, credencial: str = "op",
                     capabilities=(C.CAP_ORGANIZATION_ACTIVATE,
                                   C.CAP_ORGANIZATION_READ)):
    j.bind_credential(credencial, principal="operador", role="cto",
                      lane=LANE_OPERADOR, capabilities=capabilities)


# ── 1 · el CLI no fabrica bases ────────────────────────────────────────────
def test_base_inexistente_no_se_crea(tmp_path, capsys):
    f = _ficheros(tmp_path, _organigrama())
    assert not f["db"].exists()
    rc = _corre(f, capsys=capsys)
    assert rc == 3
    assert "no existe" in capsys.readouterr().err
    assert not f["db"].exists(), "el activador CREÓ la base: promesa rota"


# ── 2 · v6 rechazada vía CLI, original byte a byte ─────────────────────────
def test_base_v6_rechazada_bytes_intactos(tmp_path, capsys):
    db = _fixture_v6(tmp_path)          # v6 GENUINA (técnica del canon v7)
    antes = _sha(db)
    f = _ficheros(tmp_path, _organigrama())
    f["db"] = db                        # apunta el CLI a la v6
    rc = _corre(f, capsys=capsys)
    assert rc == 3
    assert "MIGRAR" in capsys.readouterr().err
    assert _sha(db) == antes, "el rechazo ALTERÓ la base v6"
    assert not pathlib.Path(str(db) + "-wal").exists()
    assert not pathlib.Path(str(db) + "-shm").exists()


# ── 3 · fichero sin sello: initialize() no decide por el CLI ───────────────
def test_base_sin_sello_rechazada(tmp_path, capsys):
    db = tmp_path / "coordination.sqlite"
    # connect/close deja CERO bytes, que el preflight rechaza antes de leer
    # un sello. VACUUM materializa una SQLite válida y vacía, sin tablas.
    con = sqlite3.connect(db)
    con.execute("VACUUM")
    con.close()
    assert db.stat().st_size > 0
    antes = _sha(db)
    f = _ficheros(tmp_path, _organigrama())
    f["db"] = db
    rc = _corre(f, capsys=capsys)
    assert rc == 3
    assert "sello" in capsys.readouterr().err
    assert _sha(db) == antes


# ── 4 · base futura: ni se mira ────────────────────────────────────────────
def test_base_futura_rechazada(tmp_path, capsys):
    db = tmp_path / "coordination.sqlite"
    j = _journal_v7(db)
    j.close()
    con = sqlite3.connect(db)
    con.execute("UPDATE meta SET v='8' WHERE k='durable_v'")
    con.commit()
    con.close()
    antes = _sha(db)
    f = _ficheros(tmp_path, _organigrama())
    f["db"] = db
    rc = _corre(f, capsys=capsys)
    assert rc == 3
    assert "futura" in capsys.readouterr().err
    assert _sha(db) == antes


# ── 5 · camino felix: sesión real, recibo nativo, lane de la credencial ────
def test_camino_felix_recibo_nativo_revision_monotona(tmp_path, capsys):
    db = tmp_path / "coordination.sqlite"
    j = _journal_v7(db)
    _operador_ligado(j)
    j.bind_credential("destino", principal="be-01", role="be",
                      lane=LANE_OPERADOR, capabilities=())
    ligadura = j.bind_credential("destino", principal="be-01", role="be",
                                 lane=LANE_OPERADOR, capabilities=())
    destino = j.open_session("destino")
    doc = _organigrama(revision=1)
    doc["workloads"] = [{
        "workload_id": "be-01", "role": "be",
        "principal_id": ligadura.principal_id,
        "runtime_instance": destino.runtime_instance,
        "credential_generation": destino.generation,
    }]
    j.close()
    f = _ficheros(tmp_path, doc)

    assert _corre(f, capsys=capsys) == 0
    recibo = _recibo(capsys)
    activa = recibo["organizacion_activa"]
    assert activa["authority"] is True
    rev = activa["revision"]            # REGISTRO durable, no un entero
    assert rev["revision"] == 1
    assert rev["lane"] == LANE_OPERADOR, "el lane NO venía del carril de la credencial"
    assert rev["attestation_state"] == "attested"
    assert rev["active"] == 1
    assert rev["source_sha256"] == recibo["activacion"]["source_sha256_canonico"], \
        "el digest DURABLE no es el canónico del recibo"
    assert "lane" not in json.loads(f["sn"].read_text()), "premisa: el JSON no trae lane"
    [wl] = activa["workloads"]          # forma durable real: fila completa
    assert wl["workload_id"] == "be-01" and wl["role"] == "be"
    assert wl["principal_id"] == ligadura.principal_id
    assert wl["runtime_instance"] == destino.runtime_instance
    assert wl["credential_generation"] == destino.generation
    assert wl["lane"] == LANE_OPERADOR and wl["organization_revision"] == 1
    assert recibo["activacion"]["source_sha256_canonico"] != \
        recibo["activacion"]["snapshot_archivo_sha256"]

    # monotonía: repetir revision=1 lo niega el KERNEL (rc1), no el CLI
    assert _corre(f, capsys=capsys) == 1
    assert "no mon" in capsys.readouterr().err

    doc2 = dict(doc, revision=2)
    f2 = _ficheros(tmp_path, doc2)
    f2["db"], f2["pp"], f2["cr"] = f["db"], f["pp"], f["cr"]
    assert _corre(f2, capsys=capsys) == 0
    assert _recibo(capsys)["organizacion_activa"]["revision"]["revision"] == 2


# ── 6 · sin capacidad: el kernel niega y aquí no se liga nada ──────────────
def test_sin_capacidad_kernel_niega_ligaduras_intactas(tmp_path, capsys):
    db = tmp_path / "coordination.sqlite"
    j = _journal_v7(db)
    j.bind_credential("op", principal="operador", role="cto",
                      lane=LANE_OPERADOR, capabilities=())
    j.close()
    con = sqlite3.connect(db)
    antes_ligaduras = con.execute(
        "SELECT credential_ref, capabilities FROM credential_bindings"
    ).fetchall()
    con.close()
    f = _ficheros(tmp_path, _organigrama(revision=1))
    f["db"] = db
    rc = _corre(f, capsys=capsys)
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("KERNEL_NEGADO")
    assert "Traceback" not in err, "salida CRUDA de la excepción por stderr"
    con = sqlite3.connect(db)
    assert con.execute(
        "SELECT credential_ref, capabilities FROM credential_bindings"
    ).fetchall() == antes_ligaduras
    assert con.execute("SELECT COUNT(*) FROM organization_revisions"
                       ).fetchone()[0] == 0
    con.close()


# ── 7 · digest canónico: claves/espacio sí, listas y valores NO ────────────
def test_digest_canonico_ante_reordenacion(tmp_path):
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    c = tmp_path / "c.json"
    doc = _organigrama()
    a.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    # sort_keys ordena CLAVES de objetos, jamás listas: la igualdad canónica
    # es reordenar claves + variar espacios, no reordenar roles.
    reordenado = {k: doc[k] for k in reversed(list(doc))}
    b.write_text(json.dumps(reordenado, separators=(", ", ": ")),
                 encoding="utf-8")
    _, canon_a, file_a = activador._carga_snapshot(a)
    _, canon_b, file_b = activador._carga_snapshot(b)
    assert canon_a == canon_b, "el digest NO es estable ante claves/espacio"
    assert file_a != file_b, "el sha del FICHERO debe ser su propia cifra"
    # y un cambio de VALOR sí cambia el digest: el canón no absuelve contenido
    distinto = json.loads(json.dumps(doc))
    distinto["roles"][1]["layer"] = 9
    c.write_text(json.dumps(distinto), encoding="utf-8")
    _, canon_c, _ = activador._carga_snapshot(c)
    assert canon_c != canon_a, "el digest NO ve un cambio de valor"
    assert len(canon_a) == 64 and all(ch in "0123456789abcdef" for ch in canon_a)


# ── 8 · LA PROMESA DE PRODUCTO: rechazo en la decisión, original intacto,
#        defaults de los demás llamantes preservados ────────────────────────
def test_garantia_en_la_decision_bajo_cerrojo_y_preservacion_del_original(
        tmp_path):
    db = _fixture_v6(tmp_path)          # v6 genuina
    antes = _sha(db)

    # (a) kernel-level: el rechazo sale de initialize() bajo el cerrojo,
    #     y NO dejó retención de instantánea ni sonda WAL en el original.
    restringido = C.Journal(str(db), pepper=PEPPER,
                            open_mode=C.OPEN_MODE_SOLO_EXISTENTE_V7)
    with pytest.raises(C.OpenModeRestricted):
        restringido.initialize()
    restringido.close()
    assert _sha(db) == antes, "OpenModeRestricted llegó tras TOCAR la base"

    # (b) preservación del original: el modo ESTÁNDAR conserva su camino —
    #     la misma v6 SÍ migra para un llamante por defecto.
    copia = tmp_path / "copia-estandar.sqlite"
    copia.write_bytes(db.read_bytes())
    estandar = C.Journal(str(copia), pepper=PEPPER, lane_ledgers=LANES,
                         recipient_resolver=censo, grammar=GRAMATICA)
    assert estandar.initialize() == C.DURABLE_V, "el default DEJÓ de migrar"
    estandar.close()

    # (c) y una base NUEVA también se crea en modo estándar (rama de creación
    #     intacta), mientras el modo restringido la habría rechazado.
    nueva = tmp_path / "nueva-estandar.sqlite"
    estandar2 = C.Journal(str(nueva), pepper=PEPPER)
    assert estandar2.initialize() == C.DURABLE_V
    estandar2.close()


# ── 9 · clave ajena al vocabulario: fail closed ────────────────────────────
def test_campo_lane_del_json_rechazado(tmp_path, capsys):
    db = tmp_path / "coordination.sqlite"
    j = _journal_v7(db)
    _operador_ligado(j)
    j.close()
    f = _ficheros(tmp_path, _organigrama(lane_ajena=True))
    f["db"] = db
    rc = _corre(f, capsys=capsys)
    assert rc == 3
    assert "lane" in capsys.readouterr().err
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM organization_revisions"
                       ).fetchone()[0] == 0
    con.close()


# ── 10 · piso del pepper: bytes ÚTILES tras strip() ────────────────────────
def test_pepper_corto_rechazado(tmp_path, capsys):
    db = tmp_path / "coordination.sqlite"
    j = _journal_v7(db)
    j.close()
    f = _ficheros(tmp_path, _organigrama(), pepper=b"pepper-corto-de-20b")
    f["db"] = db
    assert _corre(f, capsys=capsys) == 3
    assert "32 bytes" in capsys.readouterr().err


# ── 11 · credencial no UTF-8: rc 3 ANTES de abrir la base ──────────────────
def test_credencial_no_utf8_rechazada_antes_de_abrir(tmp_path, capsys):
    db = tmp_path / "coordination.sqlite"
    j = _journal_v7(db)
    _operador_ligado(j)
    j.close()
    antes = _sha(db)
    f = _ficheros(tmp_path, _organigrama())
    f["db"] = db
    f["cr"].write_bytes(b"\xff\xfe\x00no-es-texto")
    assert _corre(f, capsys=capsys) == 3
    err = capsys.readouterr().err
    assert err.startswith("PRECONDICION_FALLIDA")
    assert "Traceback" not in err, "UnicodeDecodeError cruda por stderr"
    assert _sha(db) == antes, "un fallo de credencial LOCAL tocó la base"


# ── 12 · attestation_state de tipo no textual: rc 3, no TypeError ──────────
def test_attestation_state_no_textual_rechazada(tmp_path, capsys):
    doc = _organigrama()
    doc["attestation_state"] = ["attested"]   # hashable NO:TypeError en un set
    f = _ficheros(tmp_path, doc)
    assert _corre(f, capsys=capsys) == 3
    err = capsys.readouterr().err
    assert "attestation_state" in err
    assert "Traceback" not in err


# ── 13 · fichero ilegible (OSError): rc 3 por el MOTIVO de lectura ─────────
@pytest.mark.skipif(os.geteuid() == 0, reason="root lee cualquier permiso")
def test_fichero_ilegible_rc3_controlado(tmp_path, capsys, monkeypatch):
    f = _ficheros(tmp_path, _organigrama())
    f["pp"].chmod(0o000)

    def _prohibido(*args, **kwargs):
        raise AssertionError("un fallo de E/S LOCAL abrió el Journal")

    monkeypatch.setattr(activador.C, "Journal", _prohibido)
    try:
        assert _corre(f, capsys=capsys) == 3
    finally:
        f["pp"].chmod(0o600)   # el tmp_path tiene que poder borrarse
    err = capsys.readouterr().err
    # el MOTIVO concreto: lectura del pepper, no cualquier rc 3
    assert "no se pudo leer" in err and str(f["pp"]) in err
    assert "Traceback" not in err


# ── 14 · cierre de recursos: ningún fallo previo deja el journal abierto ───
def test_fallo_previo_cierra_el_journal(tmp_path, monkeypatch):
    cerrados = []

    class _SelloIlegible:
        def __init__(self, *args, **kwargs):   # firma compatible con Journal
            pass

        def stored_durable_v(self):
            raise C.JournalError("sello ilegible")   # lo propaga _abre_journal

        def close(self):
            cerrados.append("stored_durable_v")

    class _InicializaMuerto:   # fallo de initialize ajeno a OpenModeRestricted
        def __init__(self, *args, **kwargs):
            pass

        def stored_durable_v(self):
            return C.DURABLE_V   # el sello pasa: el fallo llega DESPUÉS

        def initialize(self):
            raise C.JournalReadOnly("ventana congelada")

        def close(self):
            cerrados.append("initialize")

    db = tmp_path / "finta.sqlite"
    db.write_bytes(b"no-es-sqlite-pero-is_file()")   # pasa la puerta del CLI
    monkeypatch.setattr(activador.C, "Journal", _SelloIlegible)
    # JournalError se PROPAGA (main lo clasifica rc 1); no es PrecondicionFallida
    with pytest.raises(C.JournalError):
        activador._abre_journal_v7(db, PEPPER)
    assert cerrados == ["stored_durable_v"]
    monkeypatch.setattr(activador.C, "Journal", _InicializaMuerto)
    with pytest.raises(C.JournalReadOnly):
        activador._abre_journal_v7(db, PEPPER)   # al main llega rc 1
    assert cerrados == ["stored_durable_v", "initialize"]


def test_activacion_sin_lectura_declara_el_limite_y_conserva_la_revision(
        tmp_path, capsys):
    """La lectura opcional no convierte una activación durable en fallo."""
    db = tmp_path / "coordination.sqlite"
    j = _journal_v7(db)
    _operador_ligado(j, capabilities=(C.CAP_ORGANIZATION_ACTIVATE,))
    j.bind_credential("lector", principal="lector", role="infra",
                      lane=LANE_OPERADOR,
                      capabilities=(C.CAP_ORGANIZATION_READ,))
    j.close()
    f = _ficheros(tmp_path, _organigrama())

    assert _corre(f, capsys=capsys) == 0
    recibo = _recibo(capsys)
    assert recibo["activacion"]["revision"] == 1
    assert "organizacion_activa" not in recibo
    assert recibo["lectura_nativa"].startswith("denegada:")

    j = _journal_v7(db)
    try:
        lector = j.open_session("lector")
        vigente = j.organization(lector.token)["revision"]
        assert vigente["revision"] == 1
        assert vigente["lane"] == LANE_OPERADOR
        assert vigente["source_sha256"] == recibo["activacion"][
            "source_sha256_canonico"]
    finally:
        j.close()
