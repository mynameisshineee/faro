"""P3 — el apunte que REGISTRA un destilado no es, él mismo, material a destilar.

La cola de `/canon/pendientes` se alimenta sola si no se excluye el acuse: el apunte
de canon va dirigido al destilador —tiene que ir, ES el acuse— así que vuelve a
entrar como pendiente y la cola nunca baja de uno.

El propio `servicio.py` narra esta invariante y su falsador P3 en un comentario, y
NO tenía ningún test detrás — ni de mutación. Clasificado el 2026-08-29 como el único
«invariante que necesita test» de los seis huecos de docstring del fichero.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import construir, db_directa

H = {"X-Llminbox-Token": "test-token"}
DEST = "destilador"


def montar(tmp_path, monkeypatch, cuerpo_acuse=None):
    """Un ledger con material pendiente y, opcionalmente, su acuse de destilado.

    El acuse va dirigido al DESTILADOR a propósito: si fuera a otro no reentraría en
    la cola y el test no probaría nada — sería un ⊖ disfrazado de ⊕.
    """
    md = tmp_path / "CANON.md"
    texto = f"### [cto-A → {DEST} · PRODUCED] material\ncuerpo a destilar\n"
    if cuerpo_acuse:
        texto += f"### [wiki-vault → {DEST} · INGESTED] acuse\n{cuerpo_acuse}\n"
    md.write_text(texto)
    # El destilador tiene que estar EN EL CENSO: `escuchados()` resuelve contra él, y
    # sin alta la cola sale vacía siempre — un falso verde que pasaría el test de
    # arriba sin probar nada. Lo caza el ⊖ de abajo, que exige ver el material.
    censo = {
        "agentes": [{"nombre": DEST, "humano": "operador", "clave": "", "rol": "destilador"},
                    {"nombre": "cto-A", "humano": "operador", "clave": "", "rol": "cto"},
                    {"nombre": "wiki-vault", "humano": "operador", "clave": "", "rol": "wiki"}],
        "humanos": [{"nombre": "operador", "alias": []}],
        "difusion": ["equipo"],
    }
    s = construir(tmp_path, monkeypatch, roster=censo, extra_env={
        "LLMINBOX_LEDGERS": f"canon={md}",
        "LLMINBOX_MOUNTS_JSON": "",
    })
    return s, md


def pendientes(c):
    r = c.get("/canon/pendientes", headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def indexar(s):
    con = db_directa(s)
    for nombre, ruta in s.LEDGERS.items():
        s.reindex(nombre, ruta, con)
    con.commit()
    con.close()


def test_el_acuse_no_reentra_en_la_cola(tmp_path, monkeypatch):
    """FALSADOR P3: con el material YA destilado, la cola tiene que quedar en cero.

    Si el acuse reentra, la cola nunca baja de uno y el destilador persigue un
    trabajo que ya hizo — exactamente el síntoma que describe el comentario:
    «con la cuenta de destiladas ya en 1, el pendiente no bajaba».
    """
    s, md = montar(tmp_path, monkeypatch)
    with TestClient(s.app) as c:
        indexar(s)
        eid = pendientes(c)["cola"][0]["eid"]
    # Ahora se escribe el acuse que marca ESE eid como destilado.
    s2, _ = montar(tmp_path, monkeypatch,
                   cuerpo_acuse=f"[destilado: {eid} -> wiki:paginas/x.md]")
    with TestClient(s2.app) as c:
        indexar(s2)
        cuerpo = pendientes(c)
    assert cuerpo["pendientes"] == 0 and cuerpo["cola"] == [], (
        f"la cola se autoalimenta: el acuse volvió a entrar como pendiente "
        f"(pendientes={cuerpo['pendientes']}, destiladas={cuerpo['destiladas']})")
    # Y QUE EL ACUSE SE CUENTE COMO DESTILADO, no sólo que la cola esté vacía: lo
    # señaló CodeRabbit y es el mismo razonamiento del ⊖ — una regresión que BORRARA
    # el material sin reconocer el acuse dejaría la cola vacía y pasaría el test.
    assert cuerpo["destiladas"] == 1, (
        "la cola está vacía pero el destilado no se contabilizó: puede que el material "
        "haya desaparecido en vez de haberse reconocido")


def test_material_sin_destilar_si_aparece(tmp_path, monkeypatch):
    """⊖ CONTROL — sin él, una cola vacía SIEMPRE pasaría el test de arriba y no
    sabríamos si excluye el acuse o si no ve nada en absoluto.
    """
    s, _ = montar(tmp_path, monkeypatch)
    with TestClient(s.app) as c:
        indexar(s)
        cuerpo = pendientes(c)
    assert cuerpo["pendientes"] == 1, "el material sin destilar debe estar en la cola"
    assert cuerpo["destiladas"] == 0
