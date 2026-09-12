"""Una cita rota deja de estarlo cuando la entrada citada aparece.

La wiki cita entradas del ledger; si la página se escribe ANTES que la entrada citada, la
cita queda marcada rota (`citas.eid IS NULL`). El código lo tenía previsto y lo dice:
«una página que cita una entrada recién escrita saldría rota durante UN CICLO».

Ya no era un ciclo, era PARA SIEMPRE. El atajo de «sin cambios» —añadido después, para
curar 62,70 s de bloqueo por reindexado— invalida la caché cuando cambian los FICHEROS de
la wiki o cuando `pages`/`citas` pierden filas, y NUNCA mira `entries`, que es la tabla
contra la que se resuelven las citas. Medido:

    ① antes de que exista la entrada:  {'citas': 1, 'rotas': 1}
    ② se escribe la entrada, se indexa: entries=2
    ③ reindex_wiki sin tocar la wiki:   {'rotas': 1, 'sin_cambios': True}   <- fosilizada
    ④ tras tocar el mtime de la página: {'rotas': 0}

② HAY CONSUMIDOR HUMANO: `GET /wiki` y `GET /wiki/citas` cuentan `eid IS NULL` de la tabla
persistida, y `llmi wiki` le imprime al operador «N citas · N ROTAS». Las dos superficies
dan el MISMO número equivocado, así que no se contradicen entre sí — es más difícil de
notar, no menos real.

Y una optimización que rompe una corrección en silencio es peor que la lentitud que curó:
el que la escribió no vio que estaba cambiando de tema.
"""
from __future__ import annotations
import os
import time
from .conftest import construir


def _monta(tmp_path, monkeypatch):
    s = construir(tmp_path, monkeypatch)
    led = tmp_path / "D.md"
    led.write_text("### [a → b · FYI] 2026-08-30T09:00:00Z — otra\ncuerpo\n")
    monkeypatch.setattr(s, "LEDGERS", {"demo": str(led)})
    w = tmp_path / "wiki"
    w.mkdir(exist_ok=True)
    (w / "p.md").write_text("# P\n\n[source: demo 2026-08-30T10:00:00]\n")
    monkeypatch.setattr(s, "WIKI", str(w))
    con = s.db()
    s._preparar_indice(con)
    return s, con, led, w


def test_la_cita_se_re_resuelve_cuando_llega_su_entrada(tmp_path, monkeypatch):
    s, con, led, w = _monta(tmp_path, monkeypatch)
    assert s.reindex_wiki(con)["rotas"] == 1, "el montaje no produce una cita rota"
    con.commit()

    led.write_text(led.read_text() +
                   "\n### [a → b · FYI] 2026-08-30T10:00:00Z — la citada\ncuerpo\n")
    s.reindex("demo", str(led), con)
    con.commit()

    r = s.reindex_wiki(con)
    con.close()
    assert r["rotas"] == 0, (
        f"la cita sigue rota ({r}) con la entrada ya en `entries`: `llmi wiki` le enseña "
        f"al operador una rotura que no existe, y no se cura sola nunca")


def test_el_atajo_SIGUE_atajando_cuando_no_hay_nada_roto(tmp_path, monkeypatch):
    """⊕ OBLIGATORIO Y EL QUE MANDA. La cura barata —mirar `entries` siempre— destruye el
    atajo: `entries` cambia con CADA publicación de la flota, así que la wiki se
    reindexaría en cada barrido y volverían las 791 esperas de lock que esta puerta vino a
    evitar. Curar la mentira rompiendo el motivo del mecanismo no es curarla.
    """
    s = construir(tmp_path, monkeypatch)
    led = tmp_path / "D.md"
    led.write_text("### [a → b · FYI] 2026-08-30T09:00:00Z — sola\ncuerpo\n")
    monkeypatch.setattr(s, "LEDGERS", {"demo": str(led)})
    w = tmp_path / "wiki"; w.mkdir(exist_ok=True)
    (w / "p.md").write_text("# P\n\nsin citas\n")     # 0 citas ⇒ 0 rotas
    monkeypatch.setattr(s, "WIKI", str(w))
    con = s.db(); s._preparar_indice(con)
    assert s.reindex_wiki(con)["rotas"] == 0
    con.commit()

    # llega tráfico nuevo al ledger, como pasa continuamente en la flota
    led.write_text(led.read_text() +
                   "\n### [a → b · FYI] 2026-08-30T11:00:00Z — nueva\ncuerpo\n")
    s.reindex("demo", str(led), con); con.commit()

    r = s.reindex_wiki(con)
    con.close()
    assert r.get("sin_cambios") is True, (
        "el atajo dejó de atajar por tráfico normal del ledger: cada barrido reindexa la "
        "wiki entera y vuelven los locks")


def test_una_cita_a_algo_que_no_existira_NO_reindexa_en_bucle(tmp_path, monkeypatch):
    """⊖ del coste: una cita rota PERMANENTE —a una entrada que nunca se escribirá— no
    puede condenar al servicio a reindexar la wiki en cada barrido. Se re-resuelve cuando
    `entries` ha CAMBIADO, no por el mero hecho de haber algo roto."""
    s, con, led, w = _monta(tmp_path, monkeypatch)
    assert s.reindex_wiki(con)["rotas"] == 1
    con.commit()
    r = s.reindex_wiki(con)          # nada ha cambiado: ni wiki ni entries
    con.close()
    assert r.get("sin_cambios") is True, (
        "una cita rota permanente hace reindexar en cada barrido: el coste que la puerta "
        "vino a evitar, reintroducido por la cura")
