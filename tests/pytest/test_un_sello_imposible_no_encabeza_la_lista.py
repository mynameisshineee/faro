"""Un sello con forma de fecha pero imposible encabezaba `/entries` para siempre.

`TS = re.compile(r"(\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2})Z")` casa DÍGITOS, no
fechas. Así que `9999-99-99T99:99:99Z` —mes 99, día 99, hora 99— pasa y se guarda como
el sello de la entrada.

MEDIDO contra el índice vivo el 2026-09-04, con el consumidor por defecto:

    GET /entries?limit=3&orden=ts     ← `orden=ts` es el DEFECTO, y lo que pide la web
      9999-99-99T99:99:99  db-mig    «UN GATE PASA UN SELLO IMPOSIBLE»…
      2026-13-45T99:99:99  qa        «CENSO DE MIS 9 GATES…»
      2026-10-17T23:30:00  security  «…congela el reloj en 2026-10-17T23:30:00Z»

    MAX(ts) de TODO el corpus = 9999-99-99T99:99:99

Tres entradas clavadas en la cabeza de cada listado por defecto, y `MAX(ts)` del corpus
entero envenenado. La tercera enseña de dónde salen: son entradas que HABLAN de sellos
imposibles y de congelar relojes, y el ejemplo que citan en su propio titular se
convirtió en su sello.

El corpus ya trataba la ausencia de sello como un estado normal y soportado —10.434
entradas sin fecha, con su propia línea en `/doctor`—, así que un sello que no es una
fecha se resuelve como lo que es: SIN SELLO. Y así la entrada sigue estando, sigue
siendo consultable y deja de mentir sobre cuándo pasó.
"""
from __future__ import annotations

import ledger_parse as lp


CASOS_IMPOSIBLES = [
    ("9999-99-99T99:99:99Z", "mes 99, día 99, hora 99"),
    ("2026-13-45T99:99:99Z", "mes 13, día 45"),
    ("2026-02-30T10:00:00Z", "30 de febrero"),
    ("2026-00-10T10:00:00Z", "mes 0"),
    ("2026-01-32T10:00:00Z", "día 32"),
    ("2026-01-01T25:00:00Z", "hora 25"),
    ("2026-01-01T10:61:00Z", "minuto 61"),
]


def test_un_sello_imposible_se_lee_como_SIN_sello(tmp_path):
    for sello, por_que in CASOS_IMPOSIBLES:
        p = tmp_path / "L.md"
        p.write_text(f"\n### [cto-A → backend · FYI] {sello} — titular\ncuerpo\n")
        ents, _ = lp.parse(str(p))
        assert len(ents) == 1, (sello, len(ents))
        assert ents[0].ts is None, (
            f"{sello} ({por_que}) se guardó como sello: encabezaría /entries?orden=ts")


def test_un_sello_de_verdad_sigue_valiendo(tmp_path):
    """⊕ el control que impide curar de más: rechazar por parecerse a una fecha rara
    tiraría sellos legítimos, y `ts` es de lo que más se filtra en este servicio."""
    for bueno in ("2026-09-04T18:01:45Z", "2026-02-29T00:00:00Z",   # 2026 no es bisiesto…
                  "2024-02-29T23:59:59Z",                            # …2024 sí
                  "2026-12-31T23:59:59Z", "2026-01-01T00:00:00Z"):
        p = tmp_path / "L.md"
        p.write_text(f"\n### [cto-A → backend · FYI] {bueno} — titular\ncuerpo\n")
        ents, _ = lp.parse(str(p))
        esperado = None if bueno.startswith("2026-02-29") else bueno[:-1]
        assert ents[0].ts == esperado, (bueno, ents[0].ts)


def test_la_entrada_no_desaparece_por_no_tener_sello(tmp_path, monkeypatch):
    """⊖ de no pasarse: sin sello la entrada SIGUE, con su actor y su destinatario.
    Tirar la entrada entera sería peor que el defecto — el corpus ya vive con 10.434
    entradas sin fecha y las cuenta aparte.

    EL CENSO SE FIJA AQUÍ, y es la SEGUNDA vez que cometo este fallo en el mismo día:
    `actor` y `to` sólo resuelven contra `AGENTES`, que sale de `roster.json`, que está
    en `.gitignore` porque lleva la flota real. Escrito sin fijarlo, este test pasaba en
    mi checkout y FALLABA en un clon limpio — exactamente lo que curé esta mañana en
    otro fichero (PR #101) y volví a hacer aquí tres horas después.
    """
    import json as _json
    import sys as _sys
    (tmp_path / "roster.json").write_text(_json.dumps({
        "agentes": [{"nombre": "cto-A", "humano": "operador", "clave": "", "rol": "cto"},
                    {"nombre": "backend", "humano": "operador", "clave": "", "rol": "be"}],
        "humanos": [{"nombre": "operador", "alias": []}], "difusion": []}))
    monkeypatch.setenv("LLMINBOX_ROSTER", str(tmp_path / "roster.json"))
    _sys.modules.pop("ledger_parse", None)
    import ledger_parse as lp_fijo
    try:
        p = tmp_path / "L.md"
        p.write_text("\n### [cto-A → backend · FYI] 9999-99-99T99:99:99Z — titular\ncuerpo\n")
        ents, _ = lp_fijo.parse(str(p))
        assert len(ents) == 1
        e = ents[0]
        assert e.ts is None and e.actor == "cto-A" and e.to == ["backend"]
        assert "titular" in e.head
    finally:
        _sys.modules.pop("ledger_parse", None)
