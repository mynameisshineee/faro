"""Una sola autoridad de tipos para los dos escritores validados.

El publicador local ya delegaba en ``canonical_tipo``; la API conservaba un
regex de ocho valores. Estos casos recorren el dominio completo para que una
lista paralela vuelva a teñir la suite de rojo.
"""
from __future__ import annotations

import os


def _payload(tipo: str) -> dict:
    return {
        "ledger": "demo-ledger",
        "actor": "cto-A",
        "tipo": tipo,
        "to": ["backend"],
        "head": f"autoridad única de tipo {tipo}",
    }


def test_append_acepta_todo_el_dominio_de_canonical_tipo(cliente, servicio):
    for raw in sorted(servicio.lp.CANON_TIPOS | set(servicio.lp.ALIASES)):
        r = cliente.post("/append", json=_payload(raw))
        assert r.status_code == 200, (raw, r.text)

    servicio.barrido()
    filas = cliente.get("/entries", params={"ledger": "demo-ledger", "limit": 100}).json()
    for raw in sorted(servicio.lp.CANON_TIPOS | set(servicio.lp.ALIASES)):
        titular = f"autoridad única de tipo {raw}"
        fila = next(f for f in filas if titular in f["head"])
        assert fila["tipo"] == servicio.lp.canonical_tipo(raw)


def test_append_canoniza_alias_y_mayusculas(cliente, servicio):
    r = cliente.post("/append", json=_payload("mEdIdO"))
    assert r.status_code == 200, r.text

    servicio.barrido()
    filas = cliente.get("/entries", params={"ledger": "demo-ledger", "limit": 100}).json()
    fila = next(f for f in filas if "autoridad única de tipo mEdIdO" in f["head"])
    assert fila["raw_tipo"] == "MEASURED"
    assert fila["tipo"] == "MEASURED"


def test_append_rechaza_desconocido_sin_escribir(cliente, servicio):
    path = servicio.LEDGERS["demo-ledger"]
    antes = os.path.getsize(path)

    r = cliente.post("/append", json=_payload("FOOBAR"))

    assert r.status_code == 422
    assert "tipo no canónico" in r.text
    assert os.path.getsize(path) == antes
