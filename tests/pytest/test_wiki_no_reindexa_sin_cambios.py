"""`reindex_wiki` recorría la wiki entera en UNA transacción, cambiara o no.

Medido el 2026-08-30 sobre el contenedor en producción: 791 de 791 errores
`sqlite3.OperationalError: database is locked` salen de `marcar_leido`, y 112 de 112
ráfagas grandes caen en huecos sin salida del vigilante. La transacción de
`reindex_wiki` —747 consultas con `body LIKE` sin índice— se midió en vivo en 62,70 s,
más del doble del `timeout=30` con que abre la conexión el que quiere escribir.

O sea: quien marca leído espera detrás de un barrido de wiki que casi siempre no tenía
nada que reindexar, agota los 30 s y recibe un 500 opaco.
"""
from __future__ import annotations

import os
import time

from fastapi.testclient import TestClient

from .conftest import construir, db_directa


def wiki_con(tmp_path, monkeypatch, n=3):
    w = tmp_path / "wiki"; w.mkdir()
    for i in range(n):
        (w / f"p{i}.md").write_text(f"# pagina {i}\ncuerpo\n")
    return construir(tmp_path, monkeypatch, extra_env={"LLMINBOX_WIKI": str(w)}), w


def test_segunda_pasada_sin_cambios_no_reindexa(tmp_path, monkeypatch):
    """FALSADOR: sin cambios en disco, la segunda pasada no puede volver a recorrer
    ni a escribir. Si lo hace, sigue abriendo la transacción larga que bloquea a quien
    marca leído — que es el defecto entero.
    """
    s, _ = wiki_con(tmp_path, monkeypatch)
    with TestClient(s.app):          # el arranque crea el esquema; fuera no hay tablas
        con = db_directa(s)
        primera = s.reindex_wiki(con)
        segunda = s.reindex_wiki(con)
        con.close()
    assert primera["paginas"] == 3, "el montaje no reproduce una wiki con páginas"
    assert segunda.get("sin_cambios") is True, (
        f"volvió a recorrer la wiki sin que cambiara nada: {segunda}")


def test_un_cambio_la_reabre(tmp_path, monkeypatch):
    """⊖ CONTROL — sin él, una puerta que dijera «sin cambios» SIEMPRE pasaría el test
    de arriba y dejaría la wiki sin indexar para siempre, que es peor que el defecto.
    """
    s, w = wiki_con(tmp_path, monkeypatch)
    with TestClient(s.app):
        con = db_directa(s)
        s.reindex_wiki(con)
        time.sleep(0.01)
        (w / "nueva.md").write_text("# nueva\ncuerpo\n")
        os.utime(w / "nueva.md", None)
        tras = s.reindex_wiki(con)
        con.close()
    assert not tras.get("sin_cambios"), "apareció una página y la puerta no se abrió"
    assert tras["paginas"] == 4, f"no indexó la página nueva: {tras}"


def test_indice_vaciado_reabre_la_puerta(tmp_path, monkeypatch):
    """⊖ DEL ⊖, y es el que impide el fallo PEOR de todos.

    Si el índice se reconstruye o se vacía, la wiki NO ha cambiado — así que una
    puerta que mirase sólo la huella diría «sin cambios» y dejaría la wiki sin
    indexar PARA SIEMPRE, con el sistema en verde. Sería cambiar un fallo ruidoso
    (500 al marcar leído) por uno silencioso (la wiki desaparece del índice), que es
    exactamente el intercambio que llevamos toda la semana rechazando.

    Una ausencia («no hay cambios») no puede confundirse con otra («no hay nada»).
    """
    s, _ = wiki_con(tmp_path, monkeypatch)
    with TestClient(s.app):
        con = db_directa(s)
        s.reindex_wiki(con)
        assert s.reindex_wiki(con).get("sin_cambios") is True
        con.execute("DELETE FROM pages"); con.commit()      # el índice se vacía
        tras = s.reindex_wiki(con)
        con.close()
    assert not tras.get("sin_cambios"), "índice vacío y la puerta lo dio por hecho"
    assert tras["paginas"] == 3, f"no reindexó tras vaciarse: {tras}"
