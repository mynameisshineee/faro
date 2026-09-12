"""Falsificadores de la frontera pública: la autoridad vive en el servidor."""

import builtins
import inspect

from fastapi.testclient import TestClient
import pytest

from .conftest import CLAVE, mete, nueva_con
import search_store as ss
import servicio


LEDGER_A = "carril-a-ledger"
LEDGER_B = "carril-b-ledger"
MAPA = {"carril-a": LEDGER_A, "carril-b": LEDGER_B}
CREDENCIALES = {
    "cred-a-1": {"rol": "backend", "carril": "carril-a", "principal_id": "agente-1"},
    "cred-a-2": {"rol": "backend", "carril": "carril-a", "principal_id": "agente-2"},
    "cred-b": {"rol": "backend", "carril": "carril-b", "principal_id": "agente-b"},
}
# Contrato del falsador, deliberadamente INDEPENDIENTE de la constante de producción.
# Si producción olvida un nombre reservado, parametrizar desde esa misma constante sólo
# repetiría la omisión y dejaría el test verde.
PARAMETROS_AUTORIDAD_CLIENTE = (
    "carril", "lane", "ledger", "actor", "principal", "principal_id", "role",
    "runtime", "runtime_instance", "capabilities", "source",
)


@pytest.fixture
def cliente_http(tmp_path, monkeypatch):
    con = nueva_con(tmp_path)
    mete(con, LEDGER_A, "a-1", 1, actor="alice", cuerpo="aguja comun")
    mete(con, LEDGER_A, "a-2", 2, actor="bob", cuerpo="aguja comun")
    mete(con, LEDGER_B, "b-1", 3, cuerpo="aguja comun")
    con.commit()
    st = ss.SearchStore(con, cursor_key=CLAVE,
                        acl={"carril-a": {LEDGER_A}, "carril-b": {LEDGER_B}})
    st.ensure_schema()
    st.set_acl({"carril-a": {LEDGER_A}, "carril-b": {LEDGER_B}})
    st.rebuild()
    con.close()

    monkeypatch.setattr(servicio, "DB", str(tmp_path / "m2.sqlite"))
    monkeypatch.setattr(servicio, "TOKEN", "token-compartido")
    monkeypatch.setattr(servicio, "CARRIL_LEDGER", MAPA)
    monkeypatch.setattr(servicio, "CREDENCIALES", CREDENCIALES)
    monkeypatch.setitem(servicio.SOLO_LECTURA, "activo", False)
    monkeypatch.setenv("LLMINBOX_SEARCH_CURSOR_KEY", CLAVE.decode())
    return TestClient(servicio.app)


def test_operacion_explicita_construye_indice_y_acl(tmp_path, monkeypatch):
    con = nueva_con(tmp_path, "arranque.sqlite")
    mete(con, LEDGER_A, "a-1", 1, cuerpo="aguja comun")
    con.commit()
    monkeypatch.setattr(servicio, "CARRIL_LEDGER", {"carril-a": LEDGER_A})
    monkeypatch.setenv("LLMINBOX_SEARCH_CURSOR_KEY", CLAVE.decode())
    servicio.preparar_busqueda_publica(con)
    st = ss.SearchStore(con, cursor_key=CLAVE, acl={"carril-a": {LEDGER_A}})
    assert st.readiness()["ready"] is True
    assert st.acl_persistida() == {"carril-a": frozenset({LEDGER_A})}
    con.close()


def test_clave_presente_pero_corta_aborta_operacion_explicita(tmp_path, monkeypatch):
    con = nueva_con(tmp_path, "clave-corta.sqlite")
    monkeypatch.setattr(servicio, "CARRIL_LEDGER", {"carril-a": LEDGER_A})
    monkeypatch.setenv("LLMINBOX_SEARCH_CURSOR_KEY", "demasiado-corta")
    with pytest.raises(servicio.HTTPException) as e:
        servicio.preparar_busqueda_publica(con)
    assert e.value.status_code == 503
    con.close()


def test_operacion_vieja_no_toca_acl_ni_generacion_de_schema_mas_nuevo(
        tmp_path, monkeypatch):
    con = nueva_con(tmp_path, "too-new.sqlite")
    mete(con, LEDGER_A, "a-1", 1, cuerpo="aguja comun")
    con.commit()
    monkeypatch.setattr(servicio, "CARRIL_LEDGER", {"carril-a": LEDGER_A})
    monkeypatch.setenv("LLMINBOX_SEARCH_CURSOR_KEY", CLAVE.decode())
    servicio.preparar_busqueda_publica(con, reconstruir=True)
    st = ss.SearchStore(con, cursor_key=CLAVE, acl={"carril-a": {LEDGER_A}})
    generacion = st.generation()
    acl = st.acl_persistida()
    con.execute("UPDATE search_state SET v=? WHERE k='schema_v'",
                (str(ss.SEARCH_SCHEMA_V + 1),))
    con.commit()

    with pytest.raises(ss.SearchSchemaTooNew):
        servicio.preparar_busqueda_publica(con, reconstruir=True)
    assert st.generation() == generacion
    assert st.acl_persistida() == acl
    assert st.schema_v() == ss.SEARCH_SCHEMA_V + 1
    con.close()


def test_http_no_expone_identidad_ni_autoridad_como_parametros(cliente_http):
    firma = inspect.signature(servicio.busqueda_publica)
    for reservado in PARAMETROS_AUTORIDAD_CLIENTE:
        assert reservado not in firma.parameters
    r = cliente_http.get("/search?q=aguja",
                         headers={"X-Llminbox-Token": "cred-a-1"})
    assert r.status_code == 200, r.text
    assert {f["ledger"] for f in r.json()["filas"]} == {LEDGER_A}
    assert len(r.content) <= ss.MAX_RESPONSE_BYTES


@pytest.mark.parametrize("reservado", PARAMETROS_AUTORIDAD_CLIENTE)
def test_http_rechaza_en_vez_de_ignorar_cualquier_query_reservada(
        cliente_http, reservado):
    r = cliente_http.get("/search", params={"q": "aguja", reservado: "inventado"},
                         headers={"X-Llminbox-Token": "cred-a-1"})
    assert r.status_code == 422 and reservado in r.text
    assert "filas" not in r.json() and LEDGER_A not in r.text and LEDGER_B not in r.text


def test_http_author_es_filtro_no_identidad(cliente_http):
    r = cliente_http.get("/search", params={"q": "aguja", "author": "alice"},
                         headers={"X-Llminbox-Token": "cred-a-1"})
    assert r.status_code == 200, r.text
    assert [f["actor"] for f in r.json()["filas"]] == ["alice"]
    assert r.headers["X-Llminbox-Principal"] == "agente-1"


@pytest.mark.parametrize("repetido", ["q", "author", "cursor"])
def test_http_rechaza_parametros_repetidos_ambiguos(cliente_http, repetido):
    params = [("q", "aguja")]
    if repetido == "q":
        params.append(("q", "otra"))
    else:
        params.extend([(repetido, "uno"), (repetido, "dos")])
    r = cliente_http.get("/search", params=params,
                         headers={"X-Llminbox-Token": "cred-a-1"})
    assert r.status_code == 422 and repetido in r.text


def test_http_token_compartido_y_mapa_incompleto_fallan_cerrado(
        cliente_http, monkeypatch):
    r = cliente_http.get("/search?q=aguja",
                         headers={"X-Llminbox-Token": "token-compartido"})
    assert r.status_code == 403

    incompleto = {"sin-principal": {"rol": "backend", "carril": "carril-a"}}
    monkeypatch.setattr(servicio, "CREDENCIALES", incompleto)
    r = cliente_http.get("/search?q=aguja",
                         headers={"X-Llminbox-Token": "sin-principal"})
    assert r.status_code == 403 and "principal_id" in r.text


def test_http_principales_distintos_aunque_rol_y_carril_sean_iguales(cliente_http):
    r1 = cliente_http.get("/search?q=aguja",
                          headers={"X-Llminbox-Token": "cred-a-1"})
    r2 = cliente_http.get("/search?q=aguja",
                          headers={"X-Llminbox-Token": "cred-a-2"})
    assert r1.status_code == r2.status_code == 200
    assert r1.headers["X-Llminbox-Principal"] == "agente-1"
    assert r2.headers["X-Llminbox-Principal"] == "agente-2"


def test_http_sin_clave_de_cursor_no_busca(cliente_http, monkeypatch):
    monkeypatch.delenv("LLMINBOX_SEARCH_CURSOR_KEY")
    r = cliente_http.get("/search?q=aguja",
                         headers={"X-Llminbox-Token": "cred-a-1"})
    assert r.status_code == 503


def test_registro_udf_solo_absorbe_ausencia_del_modulo(monkeypatch):
    real = builtins.__import__

    def ausente(name, *args, **kwargs):
        if name == "search_store":
            raise ModuleNotFoundError("sin search_store", name="search_store")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", ausente)
    servicio._registra_udf_busqueda(object())  # compatibilidad sin módulo opcional

    def dependencia_rota(name, *args, **kwargs):
        if name == "search_store":
            raise ModuleNotFoundError("falta dependencia", name="dependencia_interna")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", dependencia_rota)
    with pytest.raises(ModuleNotFoundError, match="dependencia"):
        servicio._registra_udf_busqueda(object())
