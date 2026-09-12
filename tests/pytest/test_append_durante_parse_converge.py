"""Un append durante el parseo no puede quedar cubierto por una firma posterior.

El fallo apareció de forma intermitente en el smoke de 30.000 entradas: el parser
leía una foto, el escritor añadía bytes y `reindex()` guardaba después el size/mtime
nuevo. La pasada siguiente saltaba el ledger como «sin cambios» aunque el índice
tenía 29.999 de 30.000 entradas.
"""
from fastapi.testclient import TestClient

from .conftest import construir


def test_append_entre_parse_y_commit_obliga_a_otra_pasada(tmp_path, monkeypatch):
    s = construir(tmp_path, monkeypatch)
    ledger = tmp_path / "DEMO-LEDGER.md"
    parse_real = s.lp.parse
    apendido = False

    def parse_con_append(path):
        nonlocal apendido
        foto = parse_real(path)
        if not apendido and str(path) == str(ledger):
            with ledger.open("a") as out:
                out.write("### [cto-A → backend · REQUEST] tercera\ncuerpo tres\n")
            apendido = True
        return foto

    monkeypatch.setattr(s.lp, "parse", parse_con_append)
    with TestClient(s.app):
        con = s.db()
        s._preparar_indice(con)
        primera = s.reindex("demo-ledger", str(ledger), con)
        con.close()
        assert primera["entries"] == 2, "el montaje no creó una foto vieja"

        # El segundo barrido tiene que ver la discrepancia de firma y converger.
        # Con la firma tomada después de parsear, salta el fichero y deja 2.
        s.barrido()
        con = s.db()
        total = con.execute(
            "SELECT COUNT(*) FROM entries WHERE ledger=? AND ausente IS NULL",
            ("demo-ledger",),
        ).fetchone()[0]
        con.close()

    assert total == 3, f"el append ocurrido durante parse quedó congelado: {total}/3"
