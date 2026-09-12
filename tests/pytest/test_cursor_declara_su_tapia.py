"""`/cursor` decía el número y no la referencia contra la que juzgarlo.

Una tapia —cursor por encima del máximo real— produce **ceguera permanente**: `/inbox`
sólo emite lo que está por encima del cursor, así que ninguna entrada, presente o futura,
vuelve a aparecer. Y no se ve desde dentro: la bandeja no dice «tapiado», dice «nada
nuevo». Medidas hoy en producción: CUATRO roles ciegos a la vez, ninguno enterado.

El detector existía en la flota —`ledger-vigia.sh` tiene `🛑 CURSOR FUERA DE RANGO`— y
está INALCANZABLE: cuelga de un contador que sólo sube cuando faltan secciones, y con
`?only=` no puede subir nunca. 124 vigías vivos con esa alarma documentada y muerta,
medido por @cto y verificado por @sdet provocándolo. Ese script no es mío y no lo toco.

⇒ pero la detección no necesita vivir en 124 procesos: el servicio TIENE los dos números.
`/cursor` daba uno solo, y el consumidor tenía que traerse los topes por su cuenta para
saber si estaba ciego — o sea, el mismo dato que ya está aquí, reconstruido fuera y por
todos. Es la clase de la noche una vez más: **un indicador sin la referencia que lo hace
legible**.

⚠️ COMPATIBILIDAD: el cuerpo actual es un mapa plano `{ledger: n}` que parsean el vigía de
la flota y `llmi`. Añadir claves rompería a quien itere el mapa esperando enteros, así que
el diagnóstico va en una CABECERA — quien no la mire sigue funcionando igual.
"""
from __future__ import annotations
from fastapi.testclient import TestClient
from .conftest import construir


def _monta(tmp_path, monkeypatch, n=4):
    s = construir(tmp_path, monkeypatch)
    led = tmp_path / "L.md"
    led.write_text("".join(
        f"### [a → backend · FYI] 2026-09-01T10:{i:02d}:00Z — e{i}\ncuerpo\n\n" for i in range(n)))
    monkeypatch.setattr(s, "LEDGERS", {"L": str(led)})
    con = s.db(); s._preparar_indice(con); s.reindex("L", str(led), con); con.commit(); con.close()
    return s, TestClient(s.app), {"X-Llminbox-Token": s.TOKEN}


def _planta_cursor(s, ledger, arrival):
    """Una tapia histórica/corrupta aún debe diagnosticarse aunque ya no pueda crearse por API."""
    con = s.db()
    con.execute("INSERT OR REPLACE INTO cursors VALUES (?,?,?,?)",
                ("be", ledger, arrival, "2026-09-07T00:00:00+00:00"))
    con.commit(); con.close()


def test_un_cursor_TAPIADO_se_declara(tmp_path, monkeypatch):
    s, c, H = _monta(tmp_path, monkeypatch)
    _planta_cursor(s, "L", 99999)
    r = c.get("/cursor/backend", headers=H)
    assert r.status_code == 200
    assert r.headers.get("X-Cursor-Tapiado") == "L", (
        "el cursor está 99.996 por encima del máximo y la respuesta no lo dice: el "
        "consumidor tiene que traerse los topes por su cuenta para saber que está ciego")


def test_un_cursor_SANO_no_declara_nada(tmp_path, monkeypatch):
    """⊕ OBLIGATORIO: una cabecera que sale siempre no informa y enseña a ignorarla — la
    misma avería que un rojo permanente."""
    s, c, H = _monta(tmp_path, monkeypatch)
    c.post("/inbox/backend/leido", headers=H, json={"hasta": {"L": 3}})
    r = c.get("/cursor/backend", headers=H)
    assert "X-Cursor-Tapiado" not in r.headers


def test_el_cuerpo_NO_cambia(tmp_path, monkeypatch):
    """⊖ de compatibilidad: `llmi` y los 124 vigías parsean el mapa plano. Si el
    diagnóstico se colara en el cuerpo, rompería a quien itere esperando enteros."""
    s, c, H = _monta(tmp_path, monkeypatch)
    _planta_cursor(s, "L", 99999)
    cuerpo = c.get("/cursor/backend", headers=H).json()
    assert cuerpo == {"L": 99999}, f"el cuerpo cambió de forma: {cuerpo}"
    assert all(isinstance(v, int) for v in cuerpo.values())


def test_varios_ledgers_tapiados_se_listan(tmp_path, monkeypatch):
    """⊖ el que separa «hay tapia» de «cuál»: con dos ledgers, la cabecera nombra los dos
    y no un booleano. Saber que estás ciego sin saber dónde no sirve de nada."""
    s = construir(tmp_path, monkeypatch)
    for nom in ("L", "M"):
        p = tmp_path / f"{nom}.md"
        p.write_text(f"### [a → backend · FYI] 2026-09-01T10:00:00Z — {nom}\ncuerpo\n\n")
        monkeypatch.setitem(s.LEDGERS, nom, str(p))
    con = s.db(); s._preparar_indice(con)
    for nom in ("L", "M"):
        s.reindex(nom, str(tmp_path / f"{nom}.md"), con)
    con.commit(); con.close()
    c = TestClient(s.app); H = {"X-Llminbox-Token": s.TOKEN}
    _planta_cursor(s, "L", 99999)
    _planta_cursor(s, "M", 88888)
    cab = c.get("/cursor/backend", headers=H).headers.get("X-Cursor-Tapiado", "")
    assert set(cab.split(",")) == {"L", "M"}, f"no nombra los dos: {cab!r}"
