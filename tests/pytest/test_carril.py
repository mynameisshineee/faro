"""③ cursor con ámbito de carril — `X-Llminbox-Carril` filtra qué CONSUME
`POST /leido`; sin cabecera (o con una que no resuelve), consume TODO como hoy.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import construir, db_directa


def test_carril_filtra_solo_su_ledger(cliente, servicio):
    """FALSADOR: si con el header puesto 'otro-ledger' se escribiera IGUAL, el
    filtro sería decorativo. Se comprueba `cursors` directamente, no sólo el
    JSON de respuesta (que podría mentir).
    """
    r = cliente.post(
        "/inbox/backend/leido",
        json={"hasta": {"demo-ledger": 1, "otro-ledger": 0}},
        headers={"X-Llminbox-Carril": "demo"},
    )
    assert r.status_code == 200
    body = r.json()
  # ⊂ y no ==: comparar el dict ENTERO mide la FORMA de la respuesta, no lo que
    # este test afirma (dónde estaba el cursor y dónde quedó). Un campo aditivo
    # —el aviso de `mas_alla_del_final`— rompía tres tests que no tenían nada que
    # ver con él. Un test que dice medir una cosa y compara todo es un falso
    # positivo esperando su turno.
    assert body["aplicados"]["demo-ledger"]["antes"] == -1
    assert body["aplicados"]["demo-ledger"]["ahora"] == 1
    assert body["fuera_de_carril"] == ["otro-ledger"]
    assert body["aviso"] is None

    con = db_directa(servicio)
    demo = con.execute(
        "SELECT last_arrival FROM cursors WHERE agent='be' AND ledger='demo-ledger'"
    ).fetchone()
    otro = con.execute(
        "SELECT last_arrival FROM cursors WHERE agent='be' AND ledger='otro-ledger'"
    ).fetchone()
    con.close()
    assert demo["last_arrival"] == 1
    assert otro is None            # NO se tocó — es el falsador literal del filtro


def test_sin_carril_consume_todo_y_avisa(cliente):
    """Conducta ACTUAL preservada: sin cabecera, ambos ledgers se aplican."""
    r = cliente.post(
        "/inbox/backend/leido",
        json={"hasta": {"demo-ledger": 1, "otro-ledger": 0}},
    )
    assert r.status_code == 200
    body = r.json()
    assert set(body["aplicados"]) == {"demo-ledger", "otro-ledger"}
    assert body["fuera_de_carril"] == []
    assert body["aviso"] == "sin carril: consumes TODOS los cursores"


def test_carril_declarado_sin_mapa_es_422_con_pista(tmp_path, monkeypatch):
    """CONTRATO CAMBIADO (hallazgo de fe·bikeus 2026-08-10, sustituye al test
    permisivo que vivía aquí): declarar carril donde NO hay mapa montado ya no
    degrada a drenar todo con un aviso que nadie lee — el servicio no puede
    honrar el ámbito que le pediste, y lo dice con 422 y el fix («quita la
    cabecera o monta carriles.tsv»). El caso «carril inválido CON mapa» está en
    test_carril_invalido_es_422_y_no_toca_cursores.

    FALSADOR: con la rama permisiva de antes, esto era 200 con los DOS ledgers
    en aplicados — las tres aserciones caen.
    """
    from fastapi.testclient import TestClient
    s = construir(tmp_path, monkeypatch,
                  extra_env={"LLMINBOX_CARRILES": "", "LLMINBOX_MOUNTS_JSON": ""})
    assert s.CARRIL_LEDGER == {}
    with TestClient(s.app) as c:
        s.barrido()
        c.headers.update({"X-Llminbox-Token": "test-token"})
        r = c.post("/inbox/backend/leido",
                   json={"hasta": {"demo-ledger": 5}},
                   headers={"X-Llminbox-Carril": "cualquiera"})
        assert r.status_code == 422
        assert "no tiene mapa de carriles montado" in r.json()["detail"]
        con = db_directa(s)
        filas = con.execute("SELECT COUNT(*) c FROM cursors WHERE agent='be'").fetchone()["c"]
        con.close()
        assert filas == 0


def test_carril_ledger_se_resuelve_de_verdad(servicio):
    """Verify-before-build del propio mapa: 'demo' (definido en el carriles.tsv
    de prueba, cruzado con mounts.json por ruta de host) SÍ resuelve a
    'demo-ledger' — si esto no resolviera, los tres tests de arriba estarían
    probando la rama de fallback permisivo sin saberlo.
    """
    assert servicio.CARRIL_LEDGER.get("demo") == "demo-ledger"


def test_carril_fuera_de_clase_se_descarta_no_crashea(tmp_path, monkeypatch):
    """RULING @cto (MARK:cto-contrato-del-envelope-lane-ledger-se-validan-con-el-mismo-
    regex-del-cli-en-el-servidor-fail-closed, 2026-09-07T17:39:35Z): `carriles.tsv` es
    TSV sin escapado — una fila con el carril fuera de `[A-Za-z0-9._-]` no se sanea, se
    DESCARTA con log, igual que ya hace el `except Exception` de `_cargar_carriles()`.
    El CLI (`llmi`) ya exigía esta clase en dos sitios; el servidor no la exigía.

    FALSADOR: sin el guard, la fila fuera de clase entraría tal cual en
    `CARRIL_LEDGER` (el bug que reportó @security: asimetría CLI/servidor) — este
    test cae si alguien quita el `re.fullmatch` o lo relaja para admitir `/`.
    """
    carriles = tmp_path / "carriles-mixto.tsv"
    carriles.write_text(
        "carril\tledger_path\twiki_path\testado\tnotas\n"
        f"demo\t{tmp_path / 'DEMO-LEDGER.md'}\t-\tcompleto\t-\n"
        f"mal/carril\t{tmp_path / 'OTRO-LEDGER.md'}\t-\tcompleto\t-\n"
    )
    s = construir(tmp_path, monkeypatch, extra_env={"LLMINBOX_CARRILES": str(carriles)})
    assert s.CARRIL_LEDGER.get("demo") == "demo-ledger", "la fila válida sigue resolviendo"
    assert "mal/carril" not in s.CARRIL_LEDGER, "la fila fuera de clase tenía que descartarse"


def test_carril_ledger_vacio_por_defecto_sin_mapa(tmp_path, monkeypatch):
    """(F) Camino DEFAULT real del compose: `LLMINBOX_CARRILES=""` y
    `LLMINBOX_MOUNTS_JSON=""` (docker-compose.yml sin override — ver ese
    fichero) ⇒ `CARRIL_LEDGER == {}` y el servicio arranca igual, sin ámbito
    de carril activado en silencio ni excepción al importar.

    FALSADOR: si `_cargar_carriles()` no tolerara las dos rutas vacías (p.ej.
    intentando abrir "" como fichero en vez de cortar por el `if not ruta_*`
    de antes), el import de `servicio` reventaría antes de llegar aquí, o
    `CARRIL_LEDGER` saldría con un mapa fantasma en vez de vacío.
    """
    s = construir(tmp_path, monkeypatch,
                  extra_env={"LLMINBOX_CARRILES": "", "LLMINBOX_MOUNTS_JSON": ""})
    assert s.CARRIL_LEDGER == {}
    with TestClient(s.app) as c:
        s.barrido()
        c.headers.update({"X-Llminbox-Token": "test-token"})
        r = c.get("/inbox/backend")
        assert r.status_code == 200


def test_carril_invalido_es_422_y_no_toca_cursores(cliente, servicio):
    """Fail-closed de ③ (hallazgo de fe·bikeus 2026-08-10): una cabecera de
    carril que NO resuelve devolvía ok:true y degradaba a consumir TODOS los
    cursores, delatándose solo en un `aviso` que ningún llamador parsea.
    Ahora: 422 que nombra los carriles válidos, y NI UN cursor tocado.

    FALSADOR: revertir el raise (volver a la rama del aviso) deja esto en 200
    con aplicados poblados — las tres aserciones caen.
    """
    r = cliente.post("/inbox/backend/leido",
                     headers={"X-Llminbox-Carril": "noexiste"},
                     json={"hasta": {"demo-ledger": 1, "otro-ledger": 0}})
    assert r.status_code == 422
    assert "noexiste" in r.json()["detail"] and "demo" in r.json()["detail"]
    con = db_directa(servicio)
    filas = con.execute("SELECT COUNT(*) c FROM cursors WHERE agent='be'").fetchone()["c"]
    con.close()
    assert filas == 0, "el 422 del carril escribió cursores — el fail-closed no cierra"


def test_carril_con_nombre_de_ledger_ensena_el_carril(cliente):
    """La trampa que fe señaló: el rótulo de sección de la bandeja es el nombre
    del LEDGER (`demo-ledger`), no del carril (`demo`) — es lo que un humano
    teclea. El 422 debe ENSEÑAR el fix, no solo rechazar.
    """
    r = cliente.post("/inbox/backend/leido",
                     headers={"X-Llminbox-Carril": "demo-ledger"},
                     json={"hasta": {"demo-ledger": 1}})
    assert r.status_code == 422
    assert "es un nombre de LEDGER" in r.json()["detail"]
    assert "'demo'" in r.json()["detail"]


def test_sin_carril_no_se_consume(tmp_path, monkeypatch):
    """⑱ El carril es OBLIGATORIO para consumir (decisión del operador 2026-08-16,
    tras descartar separar el servicio por flota).

    Antes, sin cabecera se drenaban TODOS los cursores con un `aviso` en el JSON
    — y un aviso que hay que parsear después de leer `ok:true` no protege a
    nadie. Ahora es 422 y no se mueve un cursor.

    FALSADOR: sin el gate, esto devuelve 200 y `cursors` acaba con filas.
    """
    from fastapi.testclient import TestClient
    s = construir(tmp_path, monkeypatch,
                  extra_env={"LLMINBOX_CARRIL_OBLIGATORIO": "1"})
    with TestClient(s.app) as c:
        s.barrido()
        c.headers.update({"X-Llminbox-Token": "test-token"})
        r = c.post("/inbox/backend/leido",
                   json={"hasta": {"demo-ledger": 1, "otro-ledger": 0}})
    servicio = s
    assert r.status_code == 422
    d = r.json()["detail"]
    assert "sin carril declarado" in d
    assert "demo" in d, "el error enumera los carriles válidos"
    assert "peek" in d, "y recuerda que LEER no exige carril"
    con = db_directa(servicio)
    n = con.execute("SELECT COUNT(*) c FROM cursors WHERE agent='be'").fetchone()["c"]
    con.close()
    assert n == 0, "no puede haber movido cursores"


def test_leer_sigue_sin_exigir_carril(cliente):
    """La otra mitad, y es la que evita que ⑱ sea un muro: `/inbox` MUESTRA la red
    entera sin carril. Se puede leer todo; lo que no se puede es vaciarle la
    bandeja a otra flota sin decir de cuál eres."""
    r = cliente.get("/inbox/backend")
    assert r.status_code == 200
    assert "── demo-ledger" in r.text


def test_apagado_por_defecto_la_conducta_no_cambia(cliente):
    """EL DEFECTO ES EL HALLAZGO: sólo 2 de ~20 herramientas de la flota mandan la
    cabecera hoy, y casi todas usan `curl -sf`, que se traga el 422 sin cuerpo.
    Encenderlo de golpe las dejaría sin consumir EN SILENCIO. Así que apagado por
    defecto: mismo `ok:true` y mismo aviso que hasta hoy.

    FALSADOR: invertir el default deja este test en 422 — y con él, 18 vigías."""
    r = cliente.post("/inbox/backend/leido", json={"hasta": {"demo-ledger": 1}})
    assert r.status_code == 200
    assert r.json()["aviso"] == "sin carril: consumes TODOS los cursores"


def test_sin_mapa_de_carriles_se_puede_consumir(tmp_path, monkeypatch):
    """Un despliegue SIN `carriles.tsv` no tiene carril que declarar: si el gate
    aplicara ahí, se quedaría sin poder marcar leído nada. El default del compose
    es exactamente ése, así que este camino es el de cualquiera que clone esto.

    FALSADOR: quitar `and CARRIL_LEDGER` de la condición deja este caso en 422 y
    rompe el despliegue limpio."""
    from fastapi.testclient import TestClient
    s = construir(tmp_path, monkeypatch,
                  extra_env={"LLMINBOX_CARRILES": "", "LLMINBOX_MOUNTS_JSON": ""})
    with TestClient(s.app) as c:
        s.barrido()
        c.headers.update({"X-Llminbox-Token": "test-token"})
        r = c.post("/inbox/backend/leido", json={"hasta": {"demo-ledger": 1}})
        assert r.status_code == 200
        assert r.json()["aviso"] == "sin carril: consumes TODOS los cursores"


def test_doctor_dice_si_se_puede_exigir_carril(tmp_path, monkeypatch):
    """⑱-b El número que decide CUÁNDO encender el gate, contando POST reales.

    Existe porque al ir a activarlo estimé la migración grepeando los scripts de
    la flota y el grep contó MENCIONES, no consumos: dio 18 donde había ~10 (la
    misma clase de error que el `LIKE '%contratos%'` que infló un recuento a 69
    cuando eran 0). Encender un gate con una estimación deja vigías mudos.

    FALSADOR: si `anota_consumo` no distinguiera con/sin carril, un rol que sólo
    consume sin cabecera no aparecería en la lista de mudos y el doctor diría que
    se puede encender.
    """
    from fastapi.testclient import TestClient
    s = construir(tmp_path, monkeypatch)
    with TestClient(s.app) as c:
        s.barrido()
        c.headers.update({"X-Llminbox-Token": "test-token"})
        # 'backend' consume SIN carril; nadie más consume
        c.post("/inbox/backend/leido", json={"hasta": {"demo-ledger": 1}})
        d = c.get("/doctor").text
        assert "⑤" in d
        assert "1 consumo(s) CON carril" not in d
        assert "consumen SIEMPRE sin carril" in d
        assert "be" in d.split("⑤")[1], "debe nombrar al rol que consume sin carril"
        # y ahora el mismo rol CON carril: deja de estar mudo
        c.post("/inbox/backend/leido", json={"hasta": {"demo-ledger": 1}},
               headers={"X-Llminbox-Carril": "demo"})
        d2 = c.get("/doctor").text.split("⑤")[1]
        assert "consumen SIEMPRE sin carril" not in d2
        assert "se puede plantear" in d2
