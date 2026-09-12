"""🔻 RETIRADO EL SUJETO (c3ca365, ratificado por @cto 2026-09-11T18:32Z; cura adjudicada
por @qa el mismo dia): un `hasta` menor que el cursor ya NO retrocede — da 409
ACK_REWIND_REQUIRES_RECOVERY y el cursor NO se mueve. Lo que estos tres median
(`retrocedidos.vuelven_a_verse` por DUENO) vive en un campo que el contrato dejo sin camino.

Los tres se quedan con su montaje y aseveran la RETIRADA — y sus HERMANOS ya estan aqui,
porque no esperan a nada: NO va a existir un verbo de rewind (RULING @cto 2026-09-11) y el 409
ya no lo nombra. El hermano asevera lo que existe HOY: que el error dice el cursor VERDADERO
(`detail["cursor_actual"]` contra `GET /cursor`), con estas mismas fixtures.

⛔ y fuera las lecturas de `.json()["retrocedidos"]`: leer un campo de EXITO sobre un CUERPO
DE ERROR era el defecto mas profundo del fichero — un 409 no trae recibo.

--- lo que decia el docstring original, que sigue siendo la historia del campo ---
`vuelven_a_verse` contaba las entradas dirigidas a CUALQUIERA, no a quien retrocede.

Lo cazó `cto-biklabs-4b` con un `+1` que no se explicaba: predijo 3 antes de correr,
midió 3 por segunda vía contra el índice, y el servicio dijo 4. Su hipótesis («¿cuentas
contra el techo vivo?») era razonable y falsa; la causa es más simple y peor.

Medido en el tramo (17990, 18018] de `bik-marketing-web`:

    17994 to=['FLOTA']      17996 to=['FLOTA']      18018 to=['FLOTA']    <- suyas
    18013 to=['marketing']                                               <- de OTRO

Mi consulta unía `entries` con `recipients` por `(ledger, eid)` y **no filtraba por quién**:
contaba las 4 con destinatario, incluida la que iba a `marketing`. El campo dice «vuelven a
verse» a quien retrocede, y le prometía una entrada que no es suya y que no va a ver.

Es la misma clase que arreglé hoy en `/pendientes` —unir por la tabla correcta y olvidar el
sujeto— cometida dos veces en el mismo endpoint y en el mismo día. Y en el campo que le
estoy pidiendo a la flota que se crea al destapar una tapia: ahí la diferencia entre 3 y 4
es la diferencia entre «prevención» y «recuperé una entrada».
"""
from __future__ import annotations
from fastapi.testclient import TestClient
from .conftest import construir


def _monta(tmp_path, monkeypatch):
    s = construir(tmp_path, monkeypatch)
    led = tmp_path / "L.md"
    # 0 y 2 para `backend`; 1 para OTRO agente del censo; 3 sin destinatario
    led.write_text(
        "### [a → backend · FYI] 2026-09-01T10:00:00Z — mía 1\ncuerpo\n\n"
        "### [a → operador · FYI] 2026-09-01T10:01:00Z — de otro\ncuerpo\n\n"
        "### [a → backend · FYI] 2026-09-01T10:02:00Z — mía 2\ncuerpo\n\n"
        "### [a · FYI] 2026-09-01T10:03:00Z — sin destinatario\ncuerpo\n\n")
    monkeypatch.setattr(s, "LEDGERS", {"L": str(led)})
    con = s.db(); s._preparar_indice(con); s.reindex("L", str(led), con); con.commit(); con.close()
    return s, TestClient(s.app), {"X-Llminbox-Token": s.TOKEN}


def test_un_retroceso_de_backend_se_RECHAZA_y_su_cursor_no_se_mueve(tmp_path, monkeypatch):
    """🔻 antes: `test_solo_cuenta_las_dirigidas_a_QUIEN_retrocede` (esperaba 2 de 4).
    HERMANO PENDIENTE: con el verbo de recuperacion, `backend` recupera 2 y no 4."""
    s, c, H = _monta(tmp_path, monkeypatch)
    assert c.post("/inbox/backend/leido", headers=H,
                  json={"hasta": {"L": 3}}).status_code == 200
    r = c.post("/inbox/backend/leido", headers=H, json={"hasta": {"L": -1}})
    assert r.status_code == 409
    d = r.json()["detail"]
    assert d["code"] == "ACK_REWIND_REQUIRES_RECOVERY"
    vigente = c.get("/cursor/backend", headers=H).json()["L"]
    assert vigente == 3, "efecto PARCIAL: movio el cursor"
    # HERMANO (adjudicado por @qa 2026-09-11T22:53Z): la recuperacion del operador parte
    # del dato del PROPIO error, asi que el 409 tiene que decir el cursor VERDADERO — no un
    # literal ni el `hasta` que rechazo. Leer los campos de CONTRATO del error es legitimo;
    # lo prohibido era leer campos de EXITO (`retrocedidos`) sobre un cuerpo de error.
    assert d["cursor_actual"] == vigente, f"el 409 miente sobre el cursor: {d} vs {vigente}"


def test_un_retroceso_de_otro_dueno_se_RECHAZA_igual(tmp_path, monkeypatch):
    """🔻 antes: `test_quien_no_tiene_NINGUNA_en_el_tramo_recupera_cero`. El rechazo NO
    depende de lo que hubiera en el tramo: `operador` tiene una sola entrada y da lo mismo.
    HERMANO PENDIENTE: con el verbo, aqui recupera 0."""
    s, c, H = _monta(tmp_path, monkeypatch)
    assert c.post("/inbox/operador/leido", headers=H,
                  json={"hasta": {"L": 3}}).status_code == 200
    r = c.post("/inbox/operador/leido", headers=H, json={"hasta": {"L": 1}})
    assert r.status_code == 409
    d = r.json()["detail"]
    assert d["code"] == "ACK_REWIND_REQUIRES_RECOVERY"
    vigente = c.get("/cursor/operador", headers=H).json()["L"]
    assert vigente == 3, "efecto PARCIAL: movio el cursor"
    assert d["cursor_actual"] == vigente, f"el 409 miente sobre el cursor: {d} vs {vigente}"


def test_el_rechazo_es_POR_CURSOR_y_no_contamina_al_otro(tmp_path, monkeypatch):
    """🔻 antes: `test_cada_uno_recupera_LAS_SUYAS`. ⊕ OBLIGATORIO hoy: que el 409 de uno
    no mueva ni bloquee el cursor del otro. HERMANO PENDIENTE: 2 y 1 con el verbo."""
    s, c, H = _monta(tmp_path, monkeypatch)
    for quien in ("backend", "operador"):
        assert c.post(f"/inbox/{quien}/leido", headers=H,
                      json={"hasta": {"L": 3}}).status_code == 200
        r = c.post(f"/inbox/{quien}/leido", headers=H, json={"hasta": {"L": -1}})
        assert r.status_code == 409, f"{quien}: {r.status_code}"
        d = r.json()["detail"]
        assert d["code"] == "ACK_REWIND_REQUIRES_RECOVERY"
        vigente = c.get(f"/cursor/{quien}", headers=H).json()["L"]
        assert vigente == 3, f"{quien}: movio el cursor"
        assert d["cursor_actual"] == vigente, f"{quien}: el 409 miente sobre el cursor: {d}"
