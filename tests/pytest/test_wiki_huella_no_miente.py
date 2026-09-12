"""La puerta de «sin cambios» valida `pages` y NO `citas`, pero responde por las dos.

El control ya existe y su autor escribió por qué: «una ausencia ("no hay cambios") no
puede confundirse con otra ("no hay nada")». Cubre `pages` —cuenta que sigan ahí— y deja
fuera `citas`, aunque el atajo devuelva el recuento de citas CACHEADO en memoria.

② HAY CONSUMIDOR, que es lo que hace que esto se arregle en vez de documentarse: `llmi`
le imprime a un humano «N citas · N ROTAS» desde esta respuesta (`llmi:905`), mientras
`llmi wiki citas` consulta la TABLA. Con el índice de citas vaciado, una superficie dice
2 y la otra 0 — y el humano actúa sobre la primera.

Alcanzabilidad, dicha entera: NO hay hoy un camino en el código que borre `citas` dejando
`pages` intacta. Este control, igual que el hermano que ya estaba, protege contra estado
que llega de FUERA —restauración parcial, índice corrupto, mano humana—, que es
exactamente el escenario para el que se escribió el de `pages`.
"""
from __future__ import annotations
from .conftest import construir

PAGINA = ("# A\n\n[source: 64bis-wiki:deadbeefcafe1234]\n"
          "[source: 64bis-wiki 2026-08-30T10:00:00]\n")


def _monta(tmp_path, monkeypatch):
    s = construir(tmp_path, monkeypatch)
    w = tmp_path / "wiki"
    w.mkdir(exist_ok=True)
    (w / "a.md").write_text(PAGINA)
    monkeypatch.setattr(s, "WIKI", str(w))
    con = s.db()
    s._preparar_indice(con)
    return s, con


def test_el_atajo_no_puede_declarar_citas_que_ya_no_estan(tmp_path, monkeypatch):
    s, con = _monta(tmp_path, monkeypatch)
    primera = s.reindex_wiki(con)
    con.commit()
    assert primera["citas"] > 0, "el montaje no genera citas: el ⊖ no distinguiría nada"

    con.execute("DELETE FROM citas")
    con.commit()
    real = con.execute("SELECT COUNT(*) c FROM citas").fetchone()["c"]
    assert real == 0

    segunda = s.reindex_wiki(con)
    con.close()
    assert segunda.get("citas") == 0 or not segunda.get("sin_cambios"), (
        f"el atajo declara {segunda.get('citas')} citas con la tabla en {real}: "
        f"`llmi` le imprime ese número a un humano que luego consulta la tabla y ve otro")


def test_el_atajo_SIGUE_funcionando_cuando_no_hay_cambios(tmp_path, monkeypatch):
    """⊕ OBLIGATORIO. Sin él, la cura barata —reindexar siempre— pasa el ⊖ de arriba y
    firma verde tirando a la basura las 791 esperas de lock que la puerta vino a evitar.
    """
    s, con = _monta(tmp_path, monkeypatch)
    s.reindex_wiki(con)
    con.commit()
    otra = s.reindex_wiki(con)
    con.close()
    assert otra.get("sin_cambios") is True, (
        "la puerta dejó de atajar: cada barrido vuelve a reindexar la wiki entera")
