"""Un alias que lee la bandeja pero al que NO se le puede escribir, sin decirlo.

Dos resolvedores que no se hablan:

    canon_identidad('em-64bis')  → 'engineering-manager'   ⇒ /inbox le contesta con su correo
    rol_de('em-64bis')           → 'em-64bis'              ⇒ `→ em-64bis` NO produce destinatario

Efecto: quien trabaja como `em-64bis` abre su bandeja, ve el correo de engineering-manager
y concluye que el nombre funciona. Quien le delega escribiendo `→ em-64bis` no recibe error
ninguno y la entrada queda huérfana. Los dos se van convencidos y el trabajo no llega.

`/doctor` ⑥ ya lo detecta y lo marca en rojo, pero lo lee el operador. Quien puede
arreglarlo —el que usa el alias— mira su BANDEJA, y ahí no se decía nada. El aviso va donde
está el engañado, no donde está quien podría auditarlo.

Impacto medido hoy: 0 entradas dirigidas a esos 9 alias. Es una trampa ARMADA, no un
incendio: se cura ahora porque el día que alguien la pise, el correo perdido no se recupera
y nadie sabrá por qué.
"""
from __future__ import annotations


import json

from fastapi.testclient import TestClient

from .conftest import construir
from .test_roles_alias import ROLES_ALIAS_FIXTURE


def _monta(tmp_path, monkeypatch):
    """Reusa el arnés de alias que ya existe. `solo-en-alias` es el caso exacto: resuelve
    por el fichero de alias montado y NO está en el roster, así que `→ solo-en-alias` no
    produce destinatario. No lo invento para el test — es la pareja que aquel fichero ya
    montaba para reproducir el caso bikeus real."""
    fich = tmp_path / "roles-por-alias.json"
    fich.write_text(json.dumps(ROLES_ALIAS_FIXTURE))
    s = construir(tmp_path, monkeypatch,
                  extra_env={"LLMINBOX_ROLES_ALIAS": str(fich)})
    c = TestClient(s.app); c.__enter__(); s.barrido()
    c.headers.update({"X-Llminbox-Token": "test-token"})
    return s, c


def test_la_bandeja_avisa_de_que_no_te_pueden_escribir(tmp_path, monkeypatch):
    s, c = _monta(tmp_path, monkeypatch)
    import ledger_parse as lp
    assert "solo-en-alias" not in lp.CANON, "el fixture ya no reproduce la asimetría"
    txt = c.get("/inbox/solo-en-alias?limit=1").text
    assert "NO ES DIRECCIONABLE" in txt, (
        "'solo-en-alias' lee la bandeja y no recibe si le escriben, y la bandeja no lo "
        "dice: quien la usa se va creyendo que el nombre funciona")
    assert "→" in txt, "no dice CON QUÉ nombre sí le llegaría"


def test_un_nombre_normal_NO_lleva_el_aviso(tmp_path, monkeypatch):
    """⊕ obligatorio: un aviso que sale siempre no avisa de nada."""
    s, c = _monta(tmp_path, monkeypatch)
    import ledger_parse as lp
    n = sorted(lp.CANON)[0]
    txt = c.get(f"/inbox/{n}?limit=1").text
    assert "NO ES DIRECCIONABLE" not in txt, (
        f"'{n}' es direccionable y aun así se le avisa: el aviso pierde su significado")
