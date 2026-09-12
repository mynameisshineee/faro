"""Falsadores focales para el coste del indexador y las lecturas de salud."""
from __future__ import annotations

from .conftest import construir


def test_reindex_calcula_cada_eid_una_sola_vez(tmp_path, monkeypatch):
    """Un append no debe rehashar varias veces cada entrada histórica.

    El ledger de `construir` contiene dos entradas. Instrumentar la propiedad real
    hace load-bearing la optimización: el código anterior la consultaba al menos
    tres veces por entrada; el nuevo snapshot debe consultarla exactamente una.
    """
    s = construir(tmp_path, monkeypatch)
    con = s.db()
    s._preparar_indice(con)

    original = s.lp.Entrada.sha.fget
    llamadas = 0

    def contar(entrada):
        nonlocal llamadas
        llamadas += 1
        return original(entrada)

    monkeypatch.setattr(s.lp.Entrada, "sha", property(contar))
    s.reindex("demo-ledger", s.LEDGERS["demo-ledger"], con)
    con.commit()
    assert llamadas == 2, f"dos entradas nuevas deberían producir dos hashes, no {llamadas}"

    # Segunda foto: obliga a pasar por la rama de entradas conocidas. La intención
    # durable fuerza además la rederivación de actor/destinatarios, que era donde
    # quedaban más accesos dispersos a `e.sha`.
    llamadas = 0
    s._programar_rederivacion(
        con, roster_v=s.huella_censo(), parser_v=str(s.lp.PARSER_V), mismatch=True
    )
    con.commit()
    s.reindex("demo-ledger", s.LEDGERS["demo-ledger"], con)
    con.rollback()
    con.close()

    assert llamadas == 2, f"dos entradas conocidas deberían producir dos hashes, no {llamadas}"


def test_db_ro_ve_wal_vivo_no_negocia_journal_y_falla_rapido(tmp_path, monkeypatch):
    """La conexión health ve commits en WAL, es RO y no negocia el journal."""
    s = construir(tmp_path, monkeypatch)
    con = s.db()
    s._preparar_indice(con)
    con.execute("PRAGMA wal_autocheckpoint=0")
    con.execute("CREATE TABLE health_wal_probe (valor TEXT NOT NULL)")
    con.execute("INSERT INTO health_wal_probe VALUES ('visible')")
    con.commit()

    conectar_real = s.sqlite3.connect
    aperturas = []
    sentencias = []

    class ConexionEspia(s.sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            sentencias.append(sql)
            return super().execute(sql, *args, **kwargs)

    def conectar(*args, **kwargs):
        aperturas.append((args, kwargs))
        return conectar_real(*args, factory=ConexionEspia, **kwargs)

    monkeypatch.setattr(s.sqlite3, "connect", conectar)
    lectura = s.db_ro()
    assert lectura.execute("SELECT valor FROM health_wal_probe").fetchone()[0] == "visible"
    lectura.close()
    con.close()

    assert len(aperturas) == 1
    args, kwargs = aperturas[0]
    assert "mode=ro" in args[0]
    assert "immutable=1" not in args[0], "la lectura normal ignoraría commits del WAL vivo"
    assert kwargs["uri"] is True
    assert kwargs["timeout"] <= 0.05
    assert not any("journal_mode" in sql.lower() for sql in sentencias), (
        "db_ro negoció el journal durante una lectura"
    )


def test_db_ro_degradada_conserva_snapshot_immutable(tmp_path, monkeypatch):
    """El modo de sólo lectura mantiene la semántica previa de snapshot immutable."""
    s = construir(tmp_path, monkeypatch)
    con = s.db()
    s._preparar_indice(con)
    con.commit(); con.close()

    conectar_real = s.sqlite3.connect
    aperturas = []

    def conectar(*args, **kwargs):
        aperturas.append((args, kwargs))
        return conectar_real(*args, **kwargs)

    monkeypatch.setattr(s.sqlite3, "connect", conectar)
    s.SOLO_LECTURA["activo"] = True
    lectura = s.db_ro()
    assert lectura.execute("SELECT 1").fetchone()[0] == 1
    lectura.close()

    assert len(aperturas) == 1
    assert "mode=ro&immutable=1" in aperturas[0][0][0]
