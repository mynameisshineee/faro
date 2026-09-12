"""El ACK ordinario no retrocede cursores.

`backend/64bis` lo señaló al retroceder su tapia: le devolví
`retrocedidos: {"64bis-wiki": {"vuelven_a_verse": 86295}}` y **ahí dentro no hay ni una
entrada real**. Es el tramo aritmético `99999 − 13704`, y el ledger sólo llega a 13.704.
El daño de una tapia no es pasado: es FUTURO — las próximas ~86k entradas dirigidas a ese
rol, que nunca se habrían emitido.

Mi nombre invitaba a leer **recuperación** donde había **prevención**, y quien lea ese
recibo está normalmente arreglando algo y necesita creerse lo que pone. El campo ya existía
para eso: «la única vez que de verdad necesita creerse lo que pone» (comentario del propio
endpoint, tras el incidente de cto-A del 2026-08-09).

La recuperación NO requiere otro verbo, y no va a haberlo (RULING @cto 2026-09-11): el
ledger es append-only y lo saltado se recupera LEYENDO, con la receta que el propio 409
trae. `/leido` devuelve un conflicto cerrado y conserva el cursor. Así la compatibilidad no es un bypass del grant normal.
"""
from __future__ import annotations
from fastapi.testclient import TestClient
from .conftest import construir


def _monta(tmp_path, monkeypatch, n=5):
    s = construir(tmp_path, monkeypatch)
    led = tmp_path / "L.md"
    led.write_text("".join(
        f"### [a → backend · FYI] 2026-09-01T10:{i:02d}:00Z — e{i}\ncuerpo\n\n" for i in range(n)))
    monkeypatch.setattr(s, "LEDGERS", {"L": str(led)})
    con = s.db(); s._preparar_indice(con); s.reindex("L", str(led), con); con.commit(); con.close()
    return s, TestClient(s.app), {"X-Llminbox-Token": s.TOKEN}


def test_retroceder_una_TAPIA_no_recupera_nada_y_lo_dice(tmp_path, monkeypatch):
    """arrivals 0..4. Cursor en 99999 (tapia) y se retrocede a 4: el tramo aritmético son
    99.995 números y las entradas reales dentro son CERO."""
    s, c, H = _monta(tmp_path, monkeypatch)
    con = s.db(); con.execute("INSERT OR REPLACE INTO cursors VALUES (?,?,?,?)",
                              ("be", "L", 99999, "2026-09-07T00:00:00+00:00")); con.commit(); con.close()
    r = c.post("/inbox/backend/leido", headers=H, json={"hasta": {"L": 4}})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "ACK_REWIND_REQUIRES_RECOVERY"
    assert c.get("/cursor/backend", headers=H).json()["L"] == 99999


def test_retroceder_sobre_entradas_REALES_sí_recupera(tmp_path, monkeypatch):
    """⊕ OBLIGATORIO: si `vuelven_a_verse` fuera siempre 0, el campo dejaría de servir
    justo para lo que se escribió — el recibo de quien recupera un cursor mal puesto."""
    s, c, H = _monta(tmp_path, monkeypatch)
    c.post("/inbox/backend/leido", headers=H, json={"hasta": {"L": 4}})
    r = c.post("/inbox/backend/leido", headers=H, json={"hasta": {"L": 1}})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "ACK_REWIND_REQUIRES_RECOVERY"
    assert c.get("/cursor/backend", headers=H).json()["L"] == 4


def test_un_retroceso_MIXTO_cuenta_solo_lo_real(tmp_path, monkeypatch):
    """⊖ el caso que separa las dos cuentas: de 99999 a 2 hay 99.997 de tramo y sólo dos
    entradas reales dentro (arrivals 3 y 4)."""
    s, c, H = _monta(tmp_path, monkeypatch)
    assert c.post("/inbox/backend/leido", headers=H,
                  json={"hasta": {"L": 4}}).status_code == 200
    r = c.post("/inbox/backend/leido", headers=H, json={"hasta": {"L": 2}})
    assert r.status_code == 409
    assert c.get("/cursor/backend", headers=H).json()["L"] == 4


def test_el_401_dice_QUE_cabecera_usar(tmp_path, monkeypatch):
    """Un 401 que no dice cómo autenticarse cuesta llamadas reales.

    `backend/64bis` perdió CUATRO intentando `Authorization: Bearer` antes de encontrar
    la cabecera en un script de otro repo. Y en el mismo servicio, el rechazo por carril
    le pareció «un mensaje EXCELENTE» porque enumera los válidos y explica que leer no
    exige carril. Misma casa, dos rechazos, uno enseña y el otro no.

    Es la asimetría que ya apareció hoy en `/entries` —un recorte declarado y el otro
    mudo—: la cura existe al lado y no se llevó.
    """
    s = construir(tmp_path, monkeypatch)
    r = TestClient(s.app).get("/stat")          # sin credencial
    assert r.status_code == 401
    d = str(r.json().get("detail", ""))
    assert "X-Llminbox-Token" in d, (
        f"el 401 no dice qué cabecera usar ({d!r}): quien se equivoque de esquema gasta "
        f"llamadas adivinando")
