"""El banco sólo acredita recovery cuando hay bytes durables y no duplicados."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from bench import scale_envelope_bench as B
import ledger_parse
import projector
from ._arnes import INTENT, journal, sesion


@pytest.mark.parametrize("filas", [37, 137])
def test_indice_banco_construye_corpus_y_mide_acl_con_control_positivo(tmp_path, filas):
    """Ejercita construcción e índice reales, incluidos los documentos ajenos.

    El smoke d26f5aa abortó antes de buscar: el hash de las agujas ajenas
    recibía texto sin codificar. Este control recorre también la búsqueda
    real para evitar certificar un constructor que omita el corpus ajeno.
    El caso de 137 filas exige paginar con el límite público de 100.
    """
    st, con, construccion = B._construye_indice(tmp_path / "indice.sqlite", 7, filas)
    try:
        assert construccion["agujas_ajenas"] == B.AJENAS_N
        assert con.execute("SELECT COUNT(*) FROM entries WHERE ledger=?",
                           (B.LEDGER_AJENO,)).fetchone()[0] == B.AJENAS_N
        # El titular pertenece al cuerpo completo que se indexa. Mantener
        # la guarda de integridad del producto; corregir el corpus sintético.
        assert con.execute("SELECT COUNT(*) FROM entries WHERE instr(body,head)=0").fetchone()[0] == 0
        ajena_autorizada = st.search(lane=B.CARRIL_A, ledger=B.LEDGER_AJENO,
                                    query=B.AGUJA_RARA, limit=1)
        assert len(ajena_autorizada["filas"]) == 1, "el corpus ajeno también está indexado"
        medido = B._latencia_y_recall(st, B._manifiesto_corpus(7, filas), 2)
        assert medido["encontrados"] == medido["esperados"] == filas
        assert medido["recall"] == 1.0
        assert medido["fugas_cross_lane_en_B"] == 0
        assert medido["acceso_B_ledger_ajeno_denegado"] is True
        assert medido["visibilidad_A_ledger_ajeno"] == 1
        assert medido["visibilidad_B_ledger_propio"] > 0
        assert medido["paginacion_duplicados"] == medido["paginacion_huecos"] == 0
        assert medido["orden_estricto_desc"] is True
    finally:
        con.close()


@pytest.mark.parametrize("caso,gate", [
    ("B-vacia-sin-denegar", "acceso_B_ledger_ajeno_denegado"),
    ("B-con-fuga", "fugas_cross_lane"),
    ("A-sin-control-positivo", "visibilidad_A_ledger_ajeno"),
])
def test_banco_no_certifica_una_frontera_sin_denegacion_y_datos(tmp_path, caso, gate):
    st, con, _ = B._construye_indice(tmp_path / "indice.sqlite", 7, 37)

    class FronteraRota:
        def search(self, **kw):
            if kw["ledger"] == B.LEDGER_AJENO:
                if kw["lane"] == B.CARRIL_B and caso.startswith("B-"):
                    return {"filas": [] if caso == "B-vacia-sin-denegar" else [{"eid": "fuga"}]}
                if kw["lane"] == B.CARRIL_A and caso == "A-sin-control-positivo":
                    return {"filas": []}
            return st.search(**kw)

    try:
        medido = B._latencia_y_recall(FronteraRota(), B._manifiesto_corpus(7, 37), 1)
        veredictos = B._veredictos({
            "latencia": medido,
            "concurrencia": {"claims_cruzados_de_lane": 0, "rss_flota_cota_superior_gib": 1},
            "recovery": {"segundos_hasta_drenado": 1, "pendiente_tras_drenar": 0},
            "limites": {"rss_pico_gib": 1, "rss_pico_hijos_gib": 1},
        })
        assert veredictos[gate]["veredicto"] == "FALLA"
    finally:
        con.close()


def test_error_de_busqueda_no_se_convierte_en_denegacion(tmp_path):
    st, con, _ = B._construye_indice(tmp_path / "indice.sqlite", 7, 37)

    class FronteraIndisponible:
        def search(self, **kw):
            if kw["lane"] == B.CARRIL_B and kw["ledger"] == B.LEDGER_AJENO:
                raise OSError("instrumento caído")
            return st.search(**kw)

    try:
        with pytest.raises(OSError, match="instrumento caído"):
            B._latencia_y_recall(FronteraIndisponible(), B._manifiesto_corpus(7, 37), 1)
    finally:
        con.close()


def test_banco_no_sustituye_una_base_de_evidencia_existente(tmp_path):
    ruta = tmp_path / "conservada.sqlite"
    original = b"evidencia de un ensayo anterior"
    ruta.write_bytes(original)
    with pytest.raises(B.BancoNoEjecutable, match="ya existe"):
        B._construye_indice(ruta, 7, 37)
    assert ruta.read_bytes() == original


def test_recovery_banco_drena_previo_y_proyecta_una_vez(tmp_path):
    j = journal(tmp_path)
    worker = sesion(j, "cred-recovery", principal="victima")
    previo = j.accept_event(worker.token, idempotency_key="previo", intent=INTENT, ledger="l")
    db = j.path
    j.close()

    resultado = B._fase_recovery(db, worker.token, lane="llminbox")

    raw = Path(resultado["ledger_path"]).read_bytes()
    entradas, _ = ledger_parse.parse(resultado["ledger_path"])
    assert len(entradas) == 2
    assert resultado["proyecciones_previas"] == 1
    assert resultado["event_id"] != previo.event_id
    assert resultado["receipt_id"] != previo.receipt_id
    assert raw.count(f"LLMINBOX-EVENT-END v=1 event_id={resultado['event_id']}".encode()) == 1
    assert raw.count(f"LLMINBOX-EVENT-END v=1 event_id={previo.event_id}".encode()) == 1
    assert resultado["entry_eid"] == entradas[1].sha
    assert resultado["ledger_sha256"] == hashlib.sha256(raw).hexdigest()
    assert resultado["bytes_ledger"] == len(raw)
    assert resultado["pendiente_tras_drenar"] == resultado["sin_resolver_tras_drenar"] == 0
    assert resultado["estado_acreditado"] == "materialized"
    assert resultado["replay_sin_escritura"] is True


def test_banco_rechaza_entrada_duplicada_aunque_kernel_materialice(tmp_path, monkeypatch):
    j = journal(tmp_path)
    worker = sesion(j, "cred-recovery", principal="victima")
    db = j.path
    j.close()
    real = projector.MarkdownProjector.project_next
    ruta = Path(db).resolve().with_suffix(".recovery-ledger.md")

    def duplicar(self, **kwargs):
        resultado = real(self, **kwargs)
        if resultado is not None:
            contenido = ruta.read_bytes()
            with ruta.open("ab") as archivo:
                archivo.write(contenido)
        return resultado

    monkeypatch.setattr(projector.MarkdownProjector, "project_next", duplicar)
    with pytest.raises(B.BancoNoEjecutable, match="bytes, hash, offset o unicidad"):
        B._fase_recovery(db, worker.token, lane="llminbox")


def test_prepara_db_journal_hermano_fresco_distinto_y_con_evidencia_intacta(tmp_path, monkeypatch):
    """El Journal necesita SU fichero: hermano fresco, jamás el de búsqueda.

    El humo 30c084e crashó en `Journal.initialize()` sobre la db del corpus:
    el banco pasaba UN `--db` a SearchStore y Journal. La cura deriva un
    hermano; este control fija las tres propiedades: nombre derivado, rechazo
    de un resto previo SIN tocarlo, y —el falsador del defecto original—
    rechazo con error PROPIO del banco si alguien vuelve a compartir fichero.
    """
    db = tmp_path / "g10.sqlite"
    hermano = B._prepara_db_journal(db)
    assert hermano.name == "g10.journal.sqlite"
    hermano.write_bytes(b"resto de una corrida anterior")
    with pytest.raises(B.BancoNoEjecutable, match="Journal ya existe"):
        B._prepara_db_journal(db)
    assert hermano.read_bytes() == b"resto de una corrida anterior"
    # Re-cablear el banco a una sola db (el defecto original) ya no pasa
    # en silencio hasta el crash del kernel: la guarda lo rechaza antes.
    monkeypatch.setattr(B, "_deriva_db_journal", lambda ruta: ruta)
    with pytest.raises(B.BancoNoEjecutable, match="mismo fichero"):
        B._prepara_db_journal(db)


def test_el_journal_no_coexiste_con_el_esquema_de_busqueda(tmp_path):
    """Contrato del kernel: el manifiesto v7 es dueño EXCLUSIVO de su fichero.

    Es el camino que el banco asumía sin haberlo ejecutado nunca: su crash
    `SchemaIndeterminate: tablas sin 'meta'` era el contrato negándose a
    certificar un esquema ambiguo — misma disciplina que `_head_fuera_de_body`.
    La cura separa ficheros; este control deja el POR QUÉ ejecutable.
    """
    import coordination as C
    from tests.journal._arnes import GRAMATICA, censo

    st, con, _ = B._construye_indice(tmp_path / "indice.sqlite", 7, 37)
    con.close()
    j = C.Journal(str(tmp_path / "indice.sqlite"),
                  pepper=b"pepper-de-pruebas-no-es-un-secreto-real",
                  lane_ledgers=B.LANE_LEDGERS, recipient_resolver=censo,
                  grammar=GRAMATICA)
    with pytest.raises(C.SchemaIndeterminate, match="meta"):
        j.initialize()


@pytest.mark.parametrize("valor", [None, 0, -1, True, "1024"])
def test_memoria_incompleta_no_se_cuenta_como_cero(valor):
    assert B._cota_rss_flota([{"rss_bytes": 1024}, {"rss_bytes": valor}]) is None


def test_cota_memoria_completa_y_flota_vacia():
    assert B._cota_rss_flota([]) is None
    assert B._cota_rss_flota([{"rss_bytes": 2 ** 29}, {"rss_bytes": 2 ** 28}]) == 0.75
