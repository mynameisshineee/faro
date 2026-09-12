"""`campos_exigidos` sale de la política, no de una lista mía. Y cuando no está, se dice.

@harness lo cazó y era defecto mío: yo inventé nueve nombres en castellano y la política
—que es la SoT versionada— usa otros nueve. Sólo casaba `owner`. Un `DELIVERED` escrito
conforme a la política habría dado 1/9 en mi censo PARA SIEMPRE, y el piloto habría
medido «0 completos» por un desacuerdo de nombres y no por conducta.

    censo mío:   owner  repo        rama    commit      artefacto  falsador  revisor  gate         integration
    policy:      owner  repository  branch  commit_sha  artifact   falsifier reviewer gate_result  integration_state

DOS POBLACIONES, NO UNA. El histórico (6.635 PRODUCED) está escrito con los nombres
castellanos y seguirá estándolo: cambiarlos ahí no alinea nada, borra el censo. Así que
los alias castellanos quedan SÓLO para `tipo=PRODUCED`, y cualquier otro tipo lee de la
política. Dos poblaciones distintas medidas con su propia regla, que es lo contrario de
un sinónimo.

EL LÍMITE QUE NO ME TOCA CURAR, y va declarado en la respuesta: la política vive hoy en
`/private/tmp/...`. Un contrato que se evapora al reiniciar no es una fuente de verdad, y
mi contenedor tampoco la monta. Si falta, este endpoint NO se inventa la lista y NO cae a
la mía en silencio: lo dice en `campos_origen` y deja `campos_exigidos` vacío. Un censo
que finge conocer el contrato es peor que uno que confiesa no tenerlo.
"""
from __future__ import annotations
import json
import pathlib

import pytest

CANONICOS = ["owner", "repository", "branch", "commit_sha", "artifact",
             "falsifier", "reviewer", "gate_result", "integration_state"]


@pytest.fixture
def policy(tmp_path, monkeypatch):
    p = tmp_path / "fleet-operating-policy.json"
    p.write_text(json.dumps({"delivery": {"required_fields": CANONICOS}}))
    monkeypatch.setenv("LLMINBOX_POLICY", str(p))
    return p


def test_delivered_lee_los_campos_de_la_politica(cliente, policy):
    d = cliente.get("/recibos/censo?tipo=DELIVERED").json()
    assert d["campos_exigidos"] == CANONICOS, (
        "el censo de DELIVERED usa una lista propia en vez de la de la política: es "
        "exactamente la divergencia que hace inmedible el piloto")
    assert d["campos_origen"] == "politica"


def test_producido_sigue_con_los_alias_del_historico(cliente, policy):
    """⊖ el que protege al censo viejo: si los canónicos se aplicaran al histórico, las
    6.635 entradas en castellano pasarían a 0 en todos los campos y parecería una caída
    de conducta donde sólo hubo un cambio de vocabulario."""
    d = cliente.get("/recibos/censo?tipo=PRODUCED").json()
    assert "rama" in d["campos_exigidos"], "el histórico perdió sus alias"
    assert "branch" not in d["campos_exigidos"]
    assert d["campos_origen"] == "alias-historico"


def test_sin_politica_no_se_inventa_la_lista(cliente, monkeypatch, tmp_path):
    """⊕ anti-invención. Caer a mi lista en silencio devolvería el defecto entero con
    cara de estar arreglado — y el validador de @harness lo daría por alineado."""
    monkeypatch.setenv("LLMINBOX_POLICY", str(tmp_path / "no-existe.json"))
    d = cliente.get("/recibos/censo?tipo=DELIVERED").json()
    assert d["campos_exigidos"] == [], (
        "sin política, el censo se inventa los campos exigidos")
    assert d["campos_origen"] == "ausente"
    # el método tiene que EXPLICAR el origen, no sólo etiquetarlo: quien lea «ausente»
    # sin la explicación puede tomarlo por «cero campos exigidos», que es lo contrario
    assert "campos_origen" in d["metodo"]
    assert "no se sustituye" in d["metodo"]["campos_origen"].lower()


def test_una_politica_ilegible_no_pasa_por_ausente(cliente, monkeypatch, tmp_path):
    """Ausente y roto no son lo mismo: lo primero es que aún no está, lo segundo es que
    alguien la rompió. Confundirlos esconde el segundo caso dentro del primero."""
    mala = tmp_path / "rota.json"
    mala.write_text("{esto no es json")
    monkeypatch.setenv("LLMINBOX_POLICY", str(mala))
    d = cliente.get("/recibos/censo?tipo=DELIVERED").json()
    assert d["campos_origen"] == "ilegible", d["campos_origen"]
    assert d["campos_exigidos"] == []


def test_sin_contrato_NADIE_sale_conforme(cliente, monkeypatch, tmp_path):
    """Lo cazó un ⊖ que sobrevivió, no yo: sin política, `exigidos` queda vacío y
    `n_campo == len(exigidos)` es `0 == 0` para TODA entrada. O sea que quitar el
    contrato haría conforme a la flota entera, en vez de dejarla sin medir.

    Es la avería más peligrosa de este endpoint porque va en la dirección cómoda: el
    número sube solo, nadie mira dos veces un 100%, y el piloto declararía «100% con
    recibo» el día que la política se evapore de `/tmp` — que es un día que va a llegar.
    """
    import os, sqlite3
    con = sqlite3.connect(os.environ["LLMINBOX_DB"])
    con.execute(
        "INSERT OR REPLACE INTO entries (ledger,eid,arrival,seq,line_no,byte_off,ts,"
        "actor,tipo,head,body,visto,ausente,provisional,raw_tipo) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("l", "sin-contrato", 950, 0, 0, 0, "2026-09-01T00:00:00Z", "alguien",
         "DELIVERED", "cabecera", "cuerpo sin un solo campo", None, None, 0, "DELIVERED"))
    con.commit(); con.close()

    monkeypatch.setenv("LLMINBOX_POLICY", str(tmp_path / "no-existe.json"))
    d = cliente.get("/recibos/censo?tipo=DELIVERED").json()
    assert d["total"] >= 1, "la entrada plantada no se ve; el test no puede fallar"
    assert d["completos"] == 0, (
        f"{d['completos']} recibos «completos» sin contrato que exigir: cero campos "
        f"exigidos hace conforme a todo el mundo")


def test_el_censo_publica_la_HUELLA_de_la_politica_que_leyo(cliente, policy):
    """Una COPIA durable de la política resuelve la fragilidad de `/tmp` y crea a cambio
    una segunda fuente. Hoy es idéntica —verificado byte a byte, sha f70422e1…— y nada
    detecta la divergencia de mañana.

    Es literalmente el defecto que acabo de curar: yo tenía una lista propia que había
    divergido del contrato y nadie lo veía. La cura no puede ser «confiar en que la copia
    siga igual»; la cura es que la divergencia sea VISIBLE. Publicando el sha256 de lo que
    de verdad leí, el validador de enfrente compara tres huellas en vez de creerse dos.
    """
    d = cliente.get("/recibos/censo?tipo=DELIVERED").json()
    import hashlib
    esperado = hashlib.sha256(policy.read_bytes()).hexdigest()
    assert d["policy_sha"] == esperado, (
        "el censo no publica la huella del fichero que leyó: una copia que divergiera "
        "pasaría por buena")
    assert d["policy_ruta"] == str(policy), "no dice de dónde leyó"


def test_sin_politica_la_huella_es_NULA_y_no_vacia(cliente, monkeypatch, tmp_path):
    """⊖ de forma: un `""` se compararía igual que un sha y dos ausencias parecerían
    coincidir. `None` no se compara con nada por accidente."""
    monkeypatch.setenv("LLMINBOX_POLICY", str(tmp_path / "no-existe.json"))
    d = cliente.get("/recibos/censo?tipo=DELIVERED").json()
    assert d["policy_sha"] is None, f"huella {d['policy_sha']!r} sin política"
