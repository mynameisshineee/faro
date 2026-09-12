"""El `actor` de una entrada es una ATRIBUCIÓN DECLARADA, y la salida tiene que
decirlo.

Hallazgo de `infra` (2026-08-20, `MARK:infra-el-actor-del-indice-es-autodeclarado`),
convergido con `security` y `cto`: hay **un solo token de flota para ~60 sesiones**
—`~/.llminbox.token`, home compartido— y el valor de `actor` se parsea de la firma
que el autor TECLEA en la cabecera. De punta a punta es autodeclarado.

El riesgo no es el canal: es que un campo ESTRUCTURADO de un índice consultable
«se lee como hecho del sistema» mucho más que una firma al pie, y sin desmentido
se lee como afirmado. `cto` ya declaró que no apoyará registro (Art. 30) sobre esa
columna como si estuviera comprobada.

LA DISTINCIÓN QUE ESTA SUITE ATA, y son dos preguntas distintas que estaban
mezcladas en una:

    el ACTO ................. autenticado por el canal (token)     → sí
    la ATRIBUCIÓN del actor . verificada individualmente           → NO
    la etiqueta `actor` ..... procedencia                          → autodeclarada

Por eso NO se marca `authenticated: false` a secas: el acto sí pasó por un canal
autorizado. Lo que no está autenticado es que quien lo firmó sea quien dice.

FUERA DE ALCANCE, deliberadamente (adjudicado por el operador 2026-08-20): no se
toca el routing, ni los cursores, ni `canon_identidad()`, ni se reescribe una sola
de las 31.463 entradas históricas cuyo `actor` es un alias. El literal crudo es
EVIDENCIA y se conserva.
"""
from __future__ import annotations


def test_entries_declara_que_el_actor_es_autodeclarado(cliente):
    """Un consumidor razonable no puede seguir leyendo `actor` como identidad
    autenticada, y para eso la respuesta tiene que DESMENTIRLO explícitamente.

    FALSADOR: sin los campos de procedencia, la fila es indistinguible de una
    donde el sistema hubiera comprobado quién escribió.
    """
    r = cliente.get("/entries?limit=1")
    assert r.status_code == 200
    fila = r.json()[0]
    assert fila["actor_provenance"] == "self_declared", fila
    assert fila["actor_identity_verified"] is False, fila


def test_el_raw_intacto_Y_la_derivacion_correcta_a_la_vez(tmp_path, monkeypatch):
    """El caso sembrado, que es el único que falsa las DOS mitades a la vez.

    Fuente: `rol_por_alias: {"cto-A": "cto"}`.  Entrada: `actor` crudo = `cto-A`.

    FALSADOR ①: si alguien «arregla» el actor sobrescribiéndolo con su rol
    canónico, `actor == "cto-A"` cae — y con ella la evidencia histórica.
    FALSADOR ②: si deja de derivar, `derived_role == "cto"` cae.

    Las dos versiones anteriores de este test eran TAUTOLÓGICAS y las escribí yo:
    la primera comparaba `f["actor"] == f["actor"]`; la segunda,
    `A != B or A == B`, que es cierto siempre. Un test que no puede fallar mide
    cero y encima ocupa el sitio del que sí mediría. Lo cazó el operador las dos
    veces.
    """
    import json
    from fastapi.testclient import TestClient
    from .conftest import construir

    org = tmp_path / "roles-por-alias.json"
    org.write_text(json.dumps({"rol_por_alias": {"cto-A": "cto"},
                               "jerarquia": {"cto": {"capa": "direccion"}}}))
    s_ = construir(tmp_path, monkeypatch,
                   extra_env={"LLMINBOX_ROLES_ALIAS": str(org)})
    (tmp_path / "DEMO-LEDGER.md").write_text(
        "### [cto-A → backend · MEDIDO] 2026-08-20T10:00:00Z — sembrada\n\ncuerpo\n")
    c = TestClient(s_.app)
    c.headers.update({"X-Llminbox-Token": "test-token"})
    with c:
        s_.barrido()
        filas = [f for f in c.get("/entries?limit=50").json() if f["actor"] == "cto-A"]

    assert filas, "la entrada sembrada no se indexó: el test no mide nada"
    f = filas[0]
    assert f["actor"] == "cto-A", f                       # ① el crudo, INTACTO
    assert f["derived_role"] == "cto", f                  # ② la derivación, correcta
    assert f["derived_role_source"] == "org_alias_map", f
    assert f["actor_provenance"] == "self_declared", f
    assert f["actor_identity_verified"] is False, f
    # LA PROCEDENCIA, EXACTA: no «que haya algo», sino que sea el sha de ESTOS
    # bytes. `assert x` sólo comprueba que no está vacío, y con eso un hash de
    # cualquier otra cosa —o una constante— pasaría igual.
    import hashlib
    assert f["role_mapping_sha256"] == hashlib.sha256(org.read_bytes()).hexdigest(), f


def test_sin_fuente_fresca_NO_se_afirma_la_derivacion(tmp_path, monkeypatch):
    """`derived_role` es enriquecimiento: con la fuente ilegible se calla, y el
    `actor` crudo se sigue sirviendo porque es evidencia y no depende del mapa.

    FALSADOR: sin el guarda de frescura, se sirve la derivación de la última foto
    buena como si fuera de ahora — una clasificación organizativa apoyada en bytes
    que ya no están.
    """
    import json
    from fastapi.testclient import TestClient
    from .conftest import construir

    org = tmp_path / "roles-por-alias.json"
    org.write_text(json.dumps({"rol_por_alias": {"cto-A": "cto"}, "jerarquia": {}}))
    s_ = construir(tmp_path, monkeypatch,
                   extra_env={"LLMINBOX_ROLES_ALIAS": str(org)})
    (tmp_path / "DEMO-LEDGER.md").write_text(
        "### [cto-A → backend · MEDIDO] 2026-08-20T10:00:00Z — sembrada\n\ncuerpo\n")
    c = TestClient(s_.app)
    c.headers.update({"X-Llminbox-Token": "test-token"})
    with c:
        s_.barrido()
        assert [f for f in c.get("/entries?limit=50").json()
                if f["actor"] == "cto-A" and f["derived_role"] == "cto"], "control: derivaba"
        org.write_text("{ roto")                       # la fuente deja de leerse
        f = [x for x in c.get("/entries?limit=50").json() if x["actor"] == "cto-A"][0]

    assert f["actor"] == "cto-A", f                    # el crudo sigue
    # LOS TRES CAMPOS DERIVADOS SE CALLAN, no dos. `derived_role_source` es
    # metadata de la derivación: dejarlo poblado mientras los otros dos van a
    # null afirma que hubo una derivación por mapa de alias y luego no dice cuál.
    # Mi versión anterior no lo comprobaba, así que ese fallo pasaba.
    assert f["derived_role"] is None, f
    assert f["derived_role_source"] is None, f
    assert f["role_mapping_sha256"] is None, f


def test_el_banner_de_inbox_declara_que_la_firma_no_esta_verificada(cliente):
    """`/inbox` devuelve `text/plain`, así que la semántica no cabe en un campo:
    va en el banner, que es lo único que TODO consumidor de esa ruta lee.

    El banner ya decía que el contenido es dato y no instrucción. Le faltaba la
    otra mitad, que es de quién viene: la firma de la cabecera la teclea el autor
    y no la comprueba nadie. Un lector que confía en el «dato, no instrucción»
    puede seguir creyendo que al menos sabe QUIÉN se lo dijo.

    Y las DOS afirmaciones, porque son dos preguntas distintas: el acto sí pasó
    por un canal autorizado; lo que no está verificado es la atribución.

    FALSADOR: con el banner anterior, ninguna de las dos aserciones se cumple.
    """
    r = cliente.get("/inbox/backend")
    assert r.status_code == 200
    t = r.text.lower()
    assert "dato, no instrucción" in t, t[:200]          # control: lo de siempre sigue
    assert "autodeclarad" in t or "no verificad" in t, t[:300]
    assert "canal" in t, t[:300]


def test_una_fuente_cambiada_RECARGA_y_por_eso_la_comparacion_de_hashes_es_redundante(
        tmp_path, monkeypatch):
    """El otro brazo de `_fresco` es INALCANZABLE hoy, y esto lo ata.

    `_fresco` exige dos cosas: `source_sha256 is not None` **y** que coincida con
    `loaded_sha256`. CodeRabbit pidió un falsador para la segunda. No se puede
    escribir honestamente: medido, una fuente legible pero CAMBIADA provoca la
    recarga inmediata en `refrescar_organigrama()` —`if sha != ORG_SHA: … ORG_SHA
    = sha`—, así que con `source` no nulo los dos hashes coinciden SIEMPRE. El
    único camino a «no fresco» es que la fuente no se deje leer.

    Un test que forzara ese estado a mano estaría probando algo que el sistema no
    alcanza. Lo que sí se puede falsar es el MECANISMO que lo hace inalcanzable —
    y es el que importa, porque el día que alguien meta un TTL o una carga
    perezosa, la comparación deja de ser redundante y pasa a ser la que sostiene
    el contrato.

    FALSADOR: romper la recarga (que `sha != ORG_SHA` no publique el mapa nuevo)
    deja la derivación sirviendo el alias VIEJO, y la última aserción cae.
    """
    import json
    from fastapi.testclient import TestClient
    from .conftest import construir

    org = tmp_path / "roles-por-alias.json"
    org.write_text(json.dumps({"rol_por_alias": {"cto-A": "cto"}, "jerarquia": {}}))
    s_ = construir(tmp_path, monkeypatch,
                   extra_env={"LLMINBOX_ROLES_ALIAS": str(org)})
    (tmp_path / "DEMO-LEDGER.md").write_text(
        "### [cto-A → backend · MEDIDO] 2026-08-20T10:00:00Z — sembrada\n\ncuerpo\n")
    c = TestClient(s_.app)
    c.headers.update({"X-Llminbox-Token": "test-token"})
    with c:
        s_.barrido()
        f = [x for x in c.get("/entries?limit=50").json() if x["actor"] == "cto-A"][0]
        assert f["derived_role"] == "cto", f            # control: deriva con el mapa viejo
        sha_viejo = f["role_mapping_sha256"]

        # MISMA fuente, JSON VÁLIDO, mapeo DISTINTO. Nada de romperla.
        org.write_text(json.dumps({"rol_por_alias": {"cto-A": "vision"}, "jerarquia": {}}))
        g = [x for x in c.get("/entries?limit=50").json() if x["actor"] == "cto-A"][0]

    assert g["actor"] == "cto-A", g                      # el crudo, intacto
    assert g["role_mapping_sha256"] != sha_viejo, g      # la foto se movió
    assert g["derived_role"] == "vision", g              # ← y la derivación es la NUEVA
