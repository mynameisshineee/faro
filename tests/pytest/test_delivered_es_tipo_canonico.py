"""DELIVERED como tipo canónico — en `CANON_TIPOS`, NO en `TIPOS`.

@harness midió la causa raíz y acertó: `DELIVERED` no era un tipo declarado, así que @fe
tuvo que ENVOLVER el recibo dentro de una entrada `PRODUCED` citándolo con `> `, y eso
rompía su validador. Es la misma clase que este repo ya documentó en `publicar.py:253`:
«el 21-ago-2026 dos hallazgos reales se publicaron como PRODUCED porque FINDING no pasaba».

PERO EL SITIO QUE PROPUSO ERA EL EQUIVOCADO, y el propio repo lo dice a tres líneas del
suyo: «No se amplía `TIPOS`: eso sería volver a tener dos listas que hay que sincronizar a
mano». La autoridad de la puerta es `canonical_tipo`, gobernada por `CANON_TIPOS`, que es
donde se añadió `FINDING` cuando pasó exactamente esto. Ampliar `TIPOS` habría reintroducido
la segunda lista que la cicatriz anterior existe para impedir.

Y hay una tercera lista, en `llmi:687`, que NO es una puerta: alimenta las palabras vacías
de un extractor de términos. No hay que sincronizarla, y este test lo fija para que nadie
la «arregle» por parecido.
"""
from __future__ import annotations
import ledger_parse as lp


def test_delivered_es_un_tipo_aceptado():
    assert lp.canonical_tipo("DELIVERED") == "DELIVERED", (
        "`DELIVERED` no pasa la puerta: quien entregue tendrá que envolverlo en otro tipo, "
        "que es exactamente el defecto que esto cura")
    assert "DELIVERED" in lp.CANON_TIPOS


def test_NO_se_ha_ampliado_TIPOS():
    """⊖ el que protege la cicatriz: `TIPOS` es el conjunto de lexemas del parser y
    `CANON_TIPOS` el de la puerta. Meter DELIVERED en el primero devuelve las dos listas
    que hay que sincronizar a mano — el defecto que `publicar.py:253` documenta."""
    assert "DELIVERED" not in lp.TIPOS, (
        "DELIVERED se añadió a `TIPOS`: eso reintroduce la segunda lista. Va en "
        "`CANON_TIPOS`, que es la autoridad de la puerta")


def test_NUNCA_es_alias_de_PRODUCED():
    """Restricción explícita del operador: tipo nuevo, no alias. Un alias haría que un
    `DELIVERED` se guardara como `PRODUCED` y el censo de recibos volvería a mezclar
    entregas con publicaciones — las 6.635 PRODUCED contaminarían la línea base."""
    assert lp.ALIASES.get("DELIVERED") is None, "DELIVERED es alias de otro tipo"
    assert lp.canonical_tipo("DELIVERED") != "PRODUCED"


def test_una_cabecera_DELIVERED_se_parsea_como_entrada(tmp_path, monkeypatch):
    """⊕ de punta a punta sobre un fichero: la cabecera DELIVERED ES la cabecera de la
    entrada. Sin wrapper, sin `> `, sin dos entradas. Es lo que hace que el recibo tenga
    UNA representación.

    EL CENSO SE FIJA AQUÍ, NO SE HEREDA. `to` se resuelve con `RE_AGENTE`, que se
    COMPILA AL IMPORTAR a partir de `AGENTES`, y `AGENTES` sale del censo que
    `ledger_parse._censo()` lee de `roster.json` — que está en `.gitignore` (línea 4)
    porque lleva la flota real. Sin fijarlo, este test leía el roster de la máquina de
    quien lo corriera: verde en un checkout de trabajo y ROJO en un clon limpio, porque
    `qa` no está en `AGENTES`, `RE_AGENTE` no casa y `e.to` sale `[]`.

    MEDIDO el 2026-09-04 en un worktree limpio de `main`: 1 failed, 520 passed — el
    único de los 521 con esta dependencia. El job de CI hace `checkout` + `pip install`
    + `pytest` y NO fabrica `roster.json` (`.github/workflows/ci.yml:36-49`), así que
    habría salido rojo ahí... si CI hubiera corrido. Entró en `main` con la PR #90 el
    2026-09-03T09:44Z, ocho horas DESPUÉS de que Actions se bloqueara por facturación
    (01:09Z), y por eso nadie lo vio.
    """
    import json as _json
    import sys as _sys
    # Censo mínimo y PROPIO: los dos nombres que la cabecera de abajo usa, y nada más.
    (tmp_path / "roster.json").write_text(_json.dumps({
        "agentes": [{"nombre": "engineering-manager", "humano": "operador", "clave": "",
                     "rol": "em"},
                    {"nombre": "qa", "humano": "operador", "clave": "", "rol": "qa"}],
        "humanos": [{"nombre": "operador", "alias": []}],
        "difusion": [],
    }))
    monkeypatch.setenv("LLMINBOX_ROSTER", str(tmp_path / "roster.json"))
    _sys.modules.pop("ledger_parse", None)      # `CANON` se calcula AL IMPORTAR
    import ledger_parse as lp_fijo
    try:
        p = tmp_path / "L.md"
        p.write_text(
            "\n### [engineering-manager → qa · DELIVERED] 2026-09-03T01:00:00Z — recibo\n"
            "owner: engineering-manager\nrepository: r\nbranch: b\ncommit_sha: abc\n")
        ents, _ = lp_fijo.parse(str(p))
        assert len(ents) == 1, f"la cabecera DELIVERED no abre una entrada: {len(ents)}"
        e = ents[0]
        assert e.raw_tipo == "DELIVERED", e.raw_tipo
        assert lp_fijo.canonical_tipo(e.raw_tipo) == "DELIVERED"
        assert e.to == ["qa"], e.to
    finally:
        # Que NADIE herede este censo de prueba: el siguiente que importe recarga.
        _sys.modules.pop("ledger_parse", None)
    # SUPERVIVIENTE JUSTIFICADO, declarado: mutar `CANON = {}` NO pone rojo este test.
    # `CANON` sólo arregla la CAJA del nombre (`canonico()`), y «qa» ya está en la caja
    # canónica, así que la aserción no puede verlo. El ⊖ que sí mide el censo es
    # `AGENTES = DIFUSION` (comprobado: rc=1).


def test_la_lista_de_llmi_NO_es_una_puerta():
    """⊖ contra el arreglo por parecido: `llmi` tiene su propio `TIPOS`, y alguien que
    busque «dónde están los tipos» lo encontrará. No gatea nada —alimenta las palabras
    vacías de un extractor de términos— y sincronizarlo sería crear la tercera lista."""
    import pathlib
    t = pathlib.Path("llmi").read_text()
    i = t.index("TIPOS = {")
    usos = t.count("in TIPOS")
    assert usos == 1, f"`TIPOS` de llmi se usa en {usos} sitios; comprueba si alguno gatea"
    assert "ETIQUETAS or bajo in PARO" in t[i:i + 2000], (
        "el único uso de `TIPOS` en llmi ya no es la lista de palabras vacías: si ahora "
        "gatea algo, sí hay que sincronizarla")
