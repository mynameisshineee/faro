"""⑦a — rename `seq`→`arrival_hasta` en `marcar_leido`: higiene de nombre, CERO
cambio de comportamiento. El propio nombre del parámetro no es observable desde
fuera de la función (el contrato JSON sigue siendo `{"hasta": {ledger: int}}`),
así que el falsador real es: todo lo que ya pasaba en ①/③ sobre `POST /leido`
sigue pasando exactamente igual — mismos valores de `aplicados`/`retrocedidos`/
`sin_cambio`.

FALSADOR: si algún test de ① o ③ EMPEZARA a fallar sólo por este cambio, no era
un rename — alguien cambió semántica además del nombre, y eso no estaba pedido.
"""
from __future__ import annotations

from .conftest import db_directa


def test_avanzar_sin_cambio_y_rewind_cerrado(cliente, servicio):
    r1 = cliente.post("/inbox/backend/leido", json={"hasta": {"demo-ledger": 1}})
    assert r1.status_code == 200
    b1 = r1.json()
    _ap = b1["aplicados"]["demo-ledger"]
    assert (_ap["antes"], _ap["ahora"]) == (-1, 1)
    assert b1["retrocedidos"] == {}
    assert b1["sin_cambio"] == []

    r2 = cliente.post("/inbox/backend/leido", json={"hasta": {"demo-ledger": 1}})
    b2 = r2.json()
    _ap2 = b2["aplicados"]["demo-ledger"]
    assert (_ap2["antes"], _ap2["ahora"]) == (1, 1)
    assert b2["sin_cambio"] == ["demo-ledger"]
    assert b2["retrocedidos"] == {}

    r3 = cliente.post("/inbox/backend/leido", json={"hasta": {"demo-ledger": 0}})
    assert r3.status_code == 409
    assert r3.json()["detail"]["code"] == "ACK_REWIND_REQUIRES_RECOVERY"

    con = db_directa(servicio)
    fila = con.execute(
        "SELECT last_arrival FROM cursors WHERE agent='be' AND ledger='demo-ledger'"
    ).fetchone()
    con.close()
    assert fila["last_arrival"] == 1
