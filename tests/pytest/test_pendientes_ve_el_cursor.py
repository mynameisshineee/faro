"""`/pendientes` unía por NOMBRE y los cursores viven por ROL: nunca los encontraba.

Medido en producción el 2026-09-01, con el endpoint ya desplegado:

    quien           n pendientes    cursor real en ese ledger
    backend               3.624     99999   (o sea: ya consumió TODO)
    cto-A                 5.249     13483
    cto-biklabs           3.094     28744

El `POST /inbox/{a}/leido` avanza bien —lo comprobé: devuelve `{'agent': 'be',
'pediste': 'backend', 'aplicados': {...'ahora': 4}}` y escribe `cursors.agent='be'`—
pero `/pendientes` hacía `LEFT JOIN cursors cu ON cu.agent = r.who`, y `r.who` es el
nombre con el que llegó la entrada (`backend`), no la clave del cursor (`be`).

⇒ el JOIN no casaba NUNCA para quien tuviera nombre distinto de su rol, así que el
agregado reportaba como pendiente lo YA CONSUMIDO, y de forma permanente: ninguna lectura
podía bajar ese número. Un watcher que actuara sobre él despertaría a `backend` por 3.624
entradas leídas — exactamente la amplificación ×N que este endpoint vino a evitar.

Y la cura ya estaba en la casa: `clave_cursor()` existe desde antes y `marcar_leido` la
usa en su primera línea. Es el mismo defecto que llevo dos días cazando —una cura que no
se llevó al vecino— cometido por mí en el endpoint que escribí para el vecino.
"""
from __future__ import annotations
from fastapi.testclient import TestClient
from .conftest import construir

WT = "w"


def _monta(tmp_path, monkeypatch, dest, n=5):
    monkeypatch.setenv("LLMINBOX_WATCHER_TOKEN", WT)
    s = construir(tmp_path, monkeypatch)
    led = tmp_path / "L.md"
    led.write_text("".join(
        f"### [a → {dest} · FYI] 2026-09-01T10:{i:02d}:00Z — e{i}\ncuerpo {i}\n\n"
        for i in range(n)))
    monkeypatch.setattr(s, "LEDGERS", {"L": str(led)})
    con = s.db(); s._preparar_indice(con); s.reindex("L", str(led), con); con.commit()
    return s, con, TestClient(s.app), {"X-Llminbox-Token": s.TOKEN, "X-Llminbox-Watcher": WT}


def _fila(c, H, quien):
    return [x for x in c.get("/pendientes", headers=H).json()["pendientes"]
            if x["quien"] == quien]


def test_consumir_baja_el_pendiente_aunque_el_nombre_no_sea_el_rol(tmp_path, monkeypatch):
    """`backend` tiene rol `be`: el nombre con el que llega la entrada y la clave del
    cursor son DISTINTOS, que es justo el caso que el JOIN no cubría."""
    s, con, c, H = _monta(tmp_path, monkeypatch, "backend")
    f0 = _fila(c, H, "backend")[0]
    assert f0["n"] == 5, "el montaje no produce pendientes"

    r = c.post("/inbox/backend/leido", headers=H, json={"hasta": {"L": f0["tope"]}})
    assert r.status_code == 200 and r.json()["aplicados"]["L"]["ahora"] == f0["tope"]
    # el cursor se guardó con la clave de ROL, no con el nombre
    guardado = con.execute("SELECT agent FROM cursors").fetchone()["agent"]
    assert guardado != "backend", f"el montaje no reproduce el caso (guardó {guardado!r})"

    assert not _fila(c, H, "backend"), (
        f"tras consumir hasta el tope, `/pendientes` sigue reportando "
        f"{_fila(c, H, 'backend')[0]['n']} pendientes: une por nombre y el cursor vive "
        f"por rol, así que el agregado no baja NUNCA")


def test_un_consumo_PARCIAL_deja_lo_que_falta(tmp_path, monkeypatch):
    """⊕ obligatorio: la cura barata —ignorar el cursor, o unir por cualquier cosa— haría
    que el agregado bajara a cero o se quedara fijo. Tiene que reflejar lo que QUEDA."""
    s, con, c, H = _monta(tmp_path, monkeypatch, "backend")
    c.post("/inbox/backend/leido", headers=H, json={"hasta": {"L": 2}})
    f = _fila(c, H, "backend")
    assert f and f[0]["n"] == 2, (
        f"consumidas 3 de 5, deberían quedar 2 y quedan {f[0]['n'] if f else 0}")


def test_quien_NO_ha_consumido_sigue_con_todo(tmp_path, monkeypatch):
    """⊖ de alcance: la cura no puede hacer desaparecer pendientes reales. `operador` no
    consume nada y debe seguir con sus cinco."""
    s, con, c, H = _monta(tmp_path, monkeypatch, "operador")
    # el censo canoniza `operador` -> `OPERADOR`, así que se busca por lo que el endpoint
    # DEVUELVE, no por lo que yo escribí en el ledger. Mi primera versión buscaba la
    # minúscula, no encontraba fila y el test fallaba por el montaje, no por el producto.
    todas = c.get("/pendientes", headers=H).json()["pendientes"]
    f = [x for x in todas if x["n"] == 5]
    assert f, f"nadie tiene las 5 pendientes: {[(x['quien'], x['n']) for x in todas]}"
