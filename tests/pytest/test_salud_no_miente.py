"""`/health` no puede dar verde sobre un servicio que no puede servir a nadie.

Dos huecos medidos el 2026-08-29, los dos de la misma familia: una condición de
operación que se apaga sola y NO se ve desde fuera.
"""
from __future__ import annotations

import time

from fastapi.testclient import TestClient

from .conftest import construir

# Estos montajes afirman «esto es un despliegue SANO», y desde que `inarmable` existe eso
# exige declarar que el ack es alcanzable: sin `LLMINBOX_WATCHER_TOKEN` la vigilancia no
# puede armarse nunca y el rojo es correcto. Antes no hacía falta decirlo porque los dos
# casos —«aún nadie ha ackeado» y «nadie podrá»— compartían casilla.
_CON_WATCHER = {"LLMINBOX_WATCHER_TOKEN": "w"}

CENSO_VACIO = {"agentes": [], "humanos": [], "difusion": []}


def barrido_completado(s):
    """El arnés anula el vigilante, así que NINGÚN barrido completa y `a_tiempo` es
    False siempre: `/health` nunca da verde aquí por un motivo ajeno al que se prueba.

    Sin esta línea los dos tests de rojo PASABAN SIN EL ARREGLO — el rojo venía del
    indexador, no del censo. Lo destapó el ⊖ de abajo al no poder ponerse verde:
    dos tests que no podían fallar, y sólo el control lo enseñó.
    """
    s.SALUD["ultimo_ok"] = time.time()
    return s


def test_health_rojo_con_censo_vacio(tmp_path, monkeypatch):
    """El servicio ya aprendió esto con los ledgers y NO lo generalizó al censo.

    `/health` comprueba `bool(LEDGERS)` desde el 2026-07-27, con un comentario que
    dice «un verde impecable sobre un servicio ciego». El censo quedó fuera: con
    `roster.json` roto o vacío, `AGENTES` es 0, TODAS las bandejas salen vacías, y
    `/health` seguía diciendo `ok: true`.

    FALSADOR: si esto pasa a verde, la salud vuelve a mentir sobre el mismo eje.
    """
    s = barrido_completado(construir(tmp_path, monkeypatch, roster=CENSO_VACIO))
    import ledger_parse as lp
    assert len(lp.AGENTES) == 0, "el montaje del test no reproduce el censo vacío"
    with TestClient(s.app) as c:
        cuerpo = c.get("/health").json()
    assert cuerpo["ok"] is False, "verde con cero agentes: la salud miente"
    assert cuerpo.get("censo") == 0


def test_health_verde_con_censo_poblado(tmp_path, monkeypatch):
    """⊖ CONTROL — sin él, un `ok: False` constante también pasaría el test de
    arriba y no sabríamos si la salud discrimina o sólo está rota.
    """
    s = barrido_completado(construir(tmp_path, monkeypatch, extra_env=_CON_WATCHER))
    import ledger_parse as lp
    assert len(lp.AGENTES) > 0
    with TestClient(s.app) as c:
        cuerpo = c.get("/health").json()
    assert cuerpo["ok"] is True, "rojo con censo bueno: el chequeo es demasiado ancho"
    assert cuerpo.get("censo", 0) > 0


def test_puerta_de_carril_no_se_apaga_sola(tmp_path, monkeypatch):
    """La puerta de carril vivía detrás de una comprobación de capacidad.

    `servicio.py`: `if not x_llminbox_carril and CARRIL_LEDGER and CARRIL_OBLIGATORIO`.
    Si `_cargar_carriles()` revienta —ruta rota, TSV malformado— `CARRIL_LEDGER`
    queda vacío y la condición entera se cae: de PUERTA PUESTA a PUERTA ABIERTA,
    con un `print()` a stdout por todo rastro. Un control de acceso no puede
    degradarse por no poder cargar su propia tabla.

    FALSADOR: pedir la puerta y no poder cargar el mapa tiene que VERSE.
    """
    s = barrido_completado(construir(tmp_path, monkeypatch, extra_env={
        "LLMINBOX_CARRIL_OBLIGATORIO": "1",
        "LLMINBOX_CARRILES": str(tmp_path / "no-existe.tsv"),
    }))
    assert s.CARRIL_OBLIGATORIO is True
    assert not s.CARRIL_LEDGER, "el montaje del test no reproduce el mapa vacío"
    with TestClient(s.app) as c:
        cuerpo = c.get("/health").json()
    assert cuerpo["ok"] is False, "puerta pedida y sin mapa: verde es mentira"
    assert cuerpo.get("puerta_carril_rota") is True
    assert cuerpo["carril"] == {"estado": "broken", "obligatorio": False,
                                 "mapa_cargado": False}


def test_health_publica_la_puerta_aplicada(tmp_path, monkeypatch):
    """El CLI corre en el host y el servicio dentro del contenedor: inferir la puerta
    desde el entorno del cliente confunde dos espacios de configuración distintos.

    FALSADOR: si `/health` no publica el positivo aplicado, `llmi` puede degradar a una
    lectura global aun cuando el contenedor haya arrancado en modo obligatorio.
    """
    s = barrido_completado(construir(tmp_path, monkeypatch, extra_env={
        "LLMINBOX_CARRIL_OBLIGATORIO": "1", **_CON_WATCHER,
    }))
    assert s.CARRIL_OBLIGATORIO is True and s.CARRIL_LEDGER
    with TestClient(s.app) as c:
        cuerpo = c.get("/health").json()
    assert cuerpo["carril"] == {"estado": "on", "obligatorio": True,
                                 "mapa_cargado": True}


def test_los_avisos_se_acumulan(tmp_path, monkeypatch):
    """FALSADOR (CodeRabbit, en las cuatro PRs de la pila): con DOS fallos a la vez,
    la cadena de `if/else` tapaba el segundo — y con ledgers Y censo vacíos caía al
    `None` final: `ok:false` sin explicar NINGUNA de las dos causas.

    Un diagnóstico que sólo cuenta el primer fallo hace arreglar uno y volver a mirar.
    """
    s = barrido_completado(construir(tmp_path, monkeypatch, roster=CENSO_VACIO,
                                     extra_env={"LLMINBOX_LEDGERS": ""}))
    with TestClient(s.app) as c:
        cuerpo = c.get("/health").json()
    assert cuerpo["ok"] is False
    avisos = cuerpo["avisos"] or []
    assert len(avisos) >= 2, f"dos fallos, un solo aviso: {avisos}"
    assert any("ledgers" in a for a in avisos), "no dice lo de los ledgers"
    assert any("CENSO" in a for a in avisos), "no dice lo del censo"
    # ⊖: el escalar sigue existiendo, que siete vigías lo anclan.
    assert cuerpo["aviso"] == avisos[0]


def test_despliegue_sin_mapa_y_sin_puerta_sigue_sano(tmp_path, monkeypatch):
    """⊖ DEL ⊖ — la guarda de la puerta no puede teñir el despliegue DELIBERADO.

    Hay un despliegue legítimo sin mapa de carriles: se consume como antes y nadie
    pide puerta. Un comentario del fichero lo prometía vía una variable que NO EXISTE
    (`LLMINBOX_CARRIL_OPCIONAL`, una sola aparición en todo el repo, en prosa); el
    escape real siempre fue el término `CARRIL_LEDGER` de la condición.

    Sin este control, `puerta_carril_rota` podría haberse escrito como «no hay mapa» a
    secas y habría puesto en rojo a un despliegue correcto. Lo que se reporta es la
    CONTRADICCIÓN —pedir puerta sin poder montarla—, no la ausencia de mapa.
    """
    s = barrido_completado(construir(tmp_path, monkeypatch, extra_env={
        "LLMINBOX_CARRILES": str(tmp_path / "no-existe.tsv"),
        **_CON_WATCHER,          # el despliegue es válido, pero su vigilancia debe poder armarse
    }))
    assert not s.CARRIL_LEDGER and s.CARRIL_OBLIGATORIO is False
    with TestClient(s.app) as c:
        cuerpo = c.get("/health").json()
    assert cuerpo["ok"] is True, "sin mapa y sin puerta pedida es un despliegue válido"
    assert cuerpo["puerta_carril_rota"] is None
    assert cuerpo["carril"] == {"estado": "off", "obligatorio": False,
                                 "mapa_cargado": False}


def test_base_inalcanzable_no_puede_dar_verde(tmp_path, monkeypatch):
    """FIJA POR CONTRATO ALGO QUE HOY SE CUMPLE POR ACCIDENTE.

    `/health` consulta la base por la ruta RO acotada. La lectura del latido es la
    que convierte un fallo de apertura en `ok_meta=False`, o sea `ilegible`, fuera
    de `VIGILANCIA_SANOS`; las demás lecturas conservan sus propios desconocidos.

    Pero eso lo cubre **de rebote**: nadie escribió «prueba que la base responde»,
    sino «lee el latido», y la cobertura es un efecto colateral de por dónde pasa esa
    lectura. Un día que el latido se cachee, o que se lea de un fichero en vez de la
    base, la muerte de la base deja de tener casilla y NADA se pone rojo avisando.
    Esta prueba es lo que convierte el accidente en contrato.

    El ⊕ no es decorativo: sin la línea base en verde el ⊖ pasa por inercia —el arnés
    arranca con `vigilancia=ilegible` porque el esquema aún no existe, así que `ok` ya
    era `false` antes de matar nada y la prueba no podía fallar. Me pasó al escribirla.
    """
    import sqlite3
    s = barrido_completado(construir(tmp_path, monkeypatch, extra_env=_CON_WATCHER))
    con = s.db()
    s._preparar_indice(con)
    con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (s._META_LATIDO, str(time.time())))
    con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (s._META_QUIEN, "watcher"))
    con.commit(); con.close()
    cli = TestClient(s.app)

    verde = cli.get("/health").json()
    assert verde["ok"] is True, "sin verde previo el ⊖ pasa por inercia y no mide nada"
    assert verde["vigilancia"]["estado"] == "viva"

    def muerta(*a, **k):
        raise sqlite3.OperationalError("unable to open database file")
    monkeypatch.setattr(s, "db_ro", muerta)

    j = cli.get("/health").json()
    assert j["ok"] is False, (
        "verde con la base inalcanzable: el healthcheck del contenedor y BannerSalud "
        "darian por sano un servicio incapaz de servir una bandeja")
    assert j["vigilancia"]["estado"] == "ilegible", (
        "la base muerta ya no cae en `ilegible`: si otro camino la tumba, bien, pero "
        "revisa que siga habiendo ALGUNO — este test existe porque el que la tumba hoy "
        "no es el que parece")


def test_health_no_abre_la_ruta_rw(tmp_path, monkeypatch):
    """El healthcheck no negocia WAL ni espera detrás del escritor normal.

    El control positivo importa: primero se arma una foto verde mediante la ruta
    RW. Después esa ruta se vuelve inalcanzable; `/health` debe seguir leyendo la
    misma foto exclusivamente por `db_ro`. Si alguien reintroduce un `db()` dentro
    de health, esta prueba falla en vez de convertir el timeout en comportamiento.
    """
    s = barrido_completado(construir(tmp_path, monkeypatch, extra_env=_CON_WATCHER))
    con = s.db()
    s._preparar_indice(con)
    con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (s._META_LATIDO, str(time.time())))
    con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (s._META_QUIEN, "watcher"))
    con.commit(); con.close()

    def rw_prohibida(*_a, **_k):
        raise AssertionError("/health abrió db() y volvió a participar en la ruta RW")

    monkeypatch.setattr(s, "db", rw_prohibida)
    cuerpo = s.health()
    assert cuerpo["ok"] is True
    assert cuerpo["vigilancia"]["estado"] == "viva"
