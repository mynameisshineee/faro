"""El aviso «fuera de la bandeja por archivo» era inalcanzable justo cuando ERA la
única noticia.

`/inbox` excluye los ledgers de `LLMINBOX_INBOX_EXCLUIR` y lo DECLARA al pie — el
commit que lo introdujo (de41b180, 2026-08-08) escribió la intención en una línea: «No
se excluye en silencio — la bandeja declara al pie lo que deja fuera».

Pero el `return` corto va ANTES:

    if not out:
        return f"(nada nuevo para {agent})\\n"     ← se va por aquí
    ...
    if excluidos:                                  ← nunca llega

O sea: mientras hay algo que enseñar, la bandeja avisa de lo que deja fuera; cuando lo
ÚNICO que tiene que decir es «hay un ledger entero que no estoy mirando», se calla y
contesta «nada nuevo». El caso en que el aviso es la noticia completa es exactamente el
caso en que no sale.

No es teórico: en la instalación donde se midió, un ledger de archivo excluido tenía
28.745 entradas, y cualquier agente al día leía «nada nuevo» sin saber que había un
canal que su bandeja no miraba. Ese ledger vivía en el DEFAULT del compose; ese
default ya está vacío, pero la exclusión la sigue pudiendo poner cualquiera — y por
eso el pie es obligatorio, no opcional.

Consumidor: `llmi inbox` y `llmi peek` imprimen este texto tal cual (llmi:858, 932).
"""
from __future__ import annotations

import pytest


@pytest.fixture
def ambos_excluidos(tmp_path, monkeypatch):
    from .conftest import construir
    from fastapi.testclient import TestClient
    s = construir(tmp_path, monkeypatch,
                  extra_env={"LLMINBOX_INBOX_EXCLUIR": "demo-ledger,otro-ledger"})
    c = TestClient(s.app)
    c.__enter__()
    s.barrido()
    c.headers.update({"X-Llminbox-Token": "test-token"})
    return c


def test_con_todo_excluido_la_bandeja_no_puede_decir_solo_nada_nuevo(ambos_excluidos):
    c = ambos_excluidos
    # ⊕ CONTROL: hay correo de verdad detrás de la exclusión. Sin esto, el ⊖ de abajo
    # probaría que sale un texto, no que tapaba algo.
    real = c.get("/inbox/backend", params={"only": "demo-ledger"}).text
    assert "nada nuevo" not in real, "el arnés no tiene nada que esconder"

    txt = c.get("/inbox/backend").text
    assert "fuera de la bandeja por archivo" in txt, (
        f"la bandeja calló los ledgers excluidos justo cuando eran la única "
        f"noticia: {txt!r}")
    assert "demo-ledger" in txt and "otro-ledger" in txt, txt


def test_el_aviso_dice_lo_mismo_haya_o_no_correo(ambos_excluidos, tmp_path, monkeypatch):
    """Dos redacciones del mismo aviso se separan con el tiempo. Se comprueba que la
    rama vacía y la rama con correo emiten EL MISMO texto, no uno parecido."""
    from .conftest import construir
    from fastapi.testclient import TestClient
    vacia = ambos_excluidos.get("/inbox/backend").text

    otro = tmp_path / "b"; otro.mkdir()
    s2 = construir(otro, monkeypatch,
                   extra_env={"LLMINBOX_INBOX_EXCLUIR": "otro-ledger"})
    c2 = TestClient(s2.app); c2.__enter__(); s2.barrido()
    c2.headers.update({"X-Llminbox-Token": "test-token"})
    con_correo = c2.get("/inbox/backend").text
    assert "fuera de la bandeja por archivo" in con_correo

    def aviso(t):
        return next(l for l in t.splitlines() if "fuera de la bandeja" in l)
    # Mismo molde: sólo cambia la lista de ledgers.
    assert (aviso(vacia).replace("demo-ledger, ", "") == aviso(con_correo)), (
        f"dos redacciones distintas:\n  vacía:  {aviso(vacia)!r}\n"
        f"  correo: {aviso(con_correo)!r}")


def test_sin_exclusiones_la_respuesta_vacia_sigue_siendo_la_de_siempre(cliente):
    """⊖ de no pasarse: sin nada excluido, «nada nuevo» se queda como estaba. Un
    aviso que saliera siempre sería ruido en la bandeja de las ~71 sesiones."""
    c = cliente
    txt = c.get("/inbox/backend").text
    c.post("/inbox/backend/leido", json={"hasta": {"demo-ledger": 1, "otro-ledger": 0}})
    vacia = c.get("/inbox/backend").text
    assert vacia == "(nada nuevo para backend)\n", repr(vacia)
