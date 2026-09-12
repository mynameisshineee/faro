"""⑤ del doctor: el instrumento tiene que decir si la PUERTA está puesta.

Por qué existe este fichero: ⑤ nació como un medidor de PRE-VUELO —«¿se puede ya
encender `LLMINBOX_CARRIL_OBLIGATORIO`?»— y se envió sin un solo test. El 2026-08-16
la puerta se encendió; el 2026-08-18 ⑤ seguía diciendo «se puede plantear encender»
y contando 1390 «consumo(s) SIN carril» que en realidad eran 1390 **rechazos 422**.

El detalle que lo causa está en `servicio.py`: `anota_consumo()` se llama ANTES del
`raise` de la puerta, así que con el gate puesto la columna «SIN» cuenta intentos que
rebotaron. Un instrumento que llama «consumo» a un rechazo, y que aconseja encender lo
que ya lleva dos días encendido, no es un adorno roto: es la única fuente que tiene la
flota para saber si la puerta está puesta (uvicorn corre sin log de acceso).
"""
from __future__ import annotations

import pytest
from .conftest import construir

PUERTA = {"LLMINBOX_CARRIL_OBLIGATORIO": "1"}
# La ventana se FIJA aquí y no se hereda (CodeRabbit, #5): estos tests envejecen
# aciertos «9 h atrás» dando por hecho que 9 > MUDO_H. Si el entorno definiera
# `LLMINBOX_MUDO_H=12`, `be` dejaría de ser antiguo y el test pasaría sin cubrir
# nada — un arnés más permisivo que producción, otra vez.
VENTANA = {"LLMINBOX_MUDO_H": "2"}

_ABIERTOS = []


@pytest.fixture(autouse=True)
def _cierra_lifespans():
    """`TestClient.__enter__()` arranca el lifespan; sin su `__exit__()` el apagado
    NO corre y cada test deja un servicio a medio cerrar (CodeRabbit, #4). Se cierran
    en orden inverso al de apertura, que es el que espera un gestor de contexto."""
    _ABIERTOS.clear()
    yield
    for c in reversed(_ABIERTOS):
        c.__exit__(None, None, None)
    _ABIERTOS.clear()


def _cliente(tmp_path, monkeypatch, extra_env=None):
    from fastapi.testclient import TestClient
    tmp_path.mkdir(parents=True, exist_ok=True)
    if not (extra_env or {}).get("LLMINBOX_CARRIL_OBLIGATORIO"):
        # Un test construye DOS servicios (con puerta y sin ella). `monkeypatch.setenv`
        # del primero sobrevive al segundo: sin este delenv, el caso «PUERTA ABIERTA»
        # se mediría con la puerta puesta y el test pasaría por el motivo equivocado.
        monkeypatch.delenv("LLMINBOX_CARRIL_OBLIGATORIO", raising=False)
    s = construir(tmp_path, monkeypatch, extra_env={**VENTANA, **(extra_env or {})})
    c = TestClient(s.app)
    c.__enter__()
    _ABIERTOS.append(c)
    s.barrido()
    c.headers.update({"X-Llminbox-Token": "test-token"})
    return s, c


def _consumir(c, agente, carril=None):
    h = {"X-Llminbox-Carril": carril} if carril else {}
    return c.post(f"/inbox/{agente}/leido", json={"hasta": {}}, headers=h)


def test_la_puerta_puesta_se_nombra_en_el_informe(tmp_path, monkeypatch):
    """FALSADOR: si ⑤ no lee `CARRIL_OBLIGATORIO`, uno de los dos casos falla.

    No basta con que el texto sea distinto: con la puerta puesta NO puede seguir
    apareciendo el consejo de encenderla.
    """
    _, c = _cliente(tmp_path, monkeypatch, PUERTA)
    puesta = c.get("/doctor").text
    assert "PUERTA PUESTA" in puesta
    assert "se puede plantear encender" not in puesta

    _, c2 = _cliente(tmp_path / "b", monkeypatch)      # misma construcción, sin la puerta
    abierta = c2.get("/doctor").text
    assert "PUERTA ABIERTA" in abierta


def test_con_la_puerta_puesta_un_sin_carril_es_rechazo_y_no_consumo(tmp_path, monkeypatch):
    """FALSADOR: hoy ese intento sale contado como «consumo SIN carril».

    Control positivo incluido: el consumo CON carril sí es un consumo, y se cuenta
    aparte — si el arreglo se pasara de listo y dejara de contar nada, esto lo caza.
    """
    _, c = _cliente(tmp_path, monkeypatch, PUERTA)
    assert _consumir(c, "backend").status_code == 422           # rebota en la puerta
    assert _consumir(c, "backend", "demo").status_code == 200    # control positivo

    t = c.get("/doctor").text
    assert "1 consumo(s) CON carril" in t
    assert "RECHAZADO" in t
    # el rechazo NO se cuenta como consumo: la forma vieja del agregado era
    # «… CON carril · N SIN · …» y con la puerta puesta no puede reaparecer.
    assert " 1 SIN " not in t


def test_un_rol_siempre_rechazado_es_alarma_de_ahora_no_pronostico(tmp_path, monkeypatch):
    """FALSADOR: hoy el aviso dice «encender el gate HOY los dejaría mudos» —
    futuro condicional— cuando el gate ya está puesto y el rol ya está mudo.

    `cto-A` sólo consume sin carril ⇒ no drena nada. `backend` manda carril ⇒ no
    debe aparecer en la alarma (control negativo: si el arreglo marcara a todos,
    esta línea falla).
    """
    _, c = _cliente(tmp_path, monkeypatch, PUERTA)
    _consumir(c, "cto-A")                       # 422, siempre sin carril
    _consumir(c, "backend", "demo")             # 200, con carril

    t = c.get("/doctor").text
    assert "cto" in t
    assert "dejaría mudos" not in t             # ya no es un pronóstico
    linea = [x for x in t.splitlines() if "RECHAZADO SIEMPRE" in x]
    assert linea, "falta la alarma de rol 100% rechazado"
    assert "be" not in linea[0].split(":")[-1]  # el que manda carril no se denuncia


def test_con_la_puerta_abierta_el_informe_viejo_se_conserva(tmp_path, monkeypatch):
    """FALSADOR: el arreglo no puede romper el modo pre-vuelo, que sigue siendo
    el que se usa en un despliegue sin mapa de carriles."""
    _, c = _cliente(tmp_path, monkeypatch)
    _consumir(c, "cto-A")                       # 200: sin puerta, pasa sin carril
    t = c.get("/doctor").text
    assert "consumo(s) CON carril" in t and " 1 SIN " in t   # aquí SÍ es consumo
    assert "se puede plantear encender" in t or "dejaría mudos" in t


def test_rebotar_no_es_estar_parado_si_el_cursor_esta_fresco(tmp_path, monkeypatch):
    """FALSADOR del arreglo anterior, encontrado EN PRODUCCIÓN dos veces seguidas.

    1ª: ⑤ acusó a `infra` de no drenar 17 min después de que `infra` drenara.
    2ª (el mismo error, ya «arreglado» contra el arranque): acusó a `cpo`, cuyo cursor
    se había movido 11 SEGUNDOS antes de arrancar el proceso.

    La causa común: el contador vive en memoria y su ventana se reinicia con el
    servicio, así que «sin drenar desde el arranque» es cierto para TODO el mundo
    durante los primeros minutos. La pregunta que vale —¿hace mucho que no drena?—
    es una DURACIÓN, y no depende de cuándo se reinició el servicio.
    """
    from datetime import datetime, timedelta, timezone

    from .conftest import db_directa

    s, c = _cliente(tmp_path, monkeypatch, PUERTA)
    _consumir(c, "cto-A")        # 422 · le pondremos cursor FRESCO
    _consumir(c, "backend")      # 422 · le pondremos cursor RANCIO
    ahora = datetime.now(timezone.utc)
    con = db_directa(s)
    for rol, cuando in (("cto", ahora - timedelta(minutes=5)),
                        ("be",  ahora - timedelta(hours=9))):
        con.execute("INSERT OR REPLACE INTO cursors VALUES (?,?,?,?)",
                    (rol, "demo-ledger", 1, cuando.isoformat(timespec="seconds")))
    con.commit()
    con.close()

    t = c.get("/doctor").text
    roja = [x for x in t.splitlines() if "sin drenar" in x]
    assert roja, "sigue haciendo falta la alarma para quien de verdad no drena"
    assert "be" in roja[0], "lleva 9 h sin drenar y rebotando: eso sí es la alarma"
    assert "cto" not in roja[0], "drenó hace 5 min: rebota, pero NO está parado"
    aviso = [x for x in t.splitlines() if "sin migrar" in x]
    assert aviso and "cto" in aviso[0], "el que rebota y drena se nombra, pero en ⚠️"


def test_la_alarma_no_depende_de_cuando_se_reinicio_el_servicio(tmp_path, monkeypatch):
    """FALSADOR directo del bug de producción: un cursor movido JUSTO ANTES de
    arrancar (11 s, el caso real de `cpo`) no puede leerse como «parado»."""
    from datetime import datetime, timedelta

    from .conftest import db_directa

    s, c = _cliente(tmp_path, monkeypatch, PUERTA)
    _consumir(c, "cto-A")
    antes_de_arrancar = datetime.fromisoformat(s.ARRANQUE) - timedelta(seconds=11)
    con = db_directa(s)
    con.execute("INSERT OR REPLACE INTO cursors VALUES (?,?,?,?)",
                ("cto", "demo-ledger", 1, antes_de_arrancar.isoformat(timespec="seconds")))
    con.commit()
    con.close()

    t = c.get("/doctor").text
    roja = [x for x in t.splitlines() if "sin drenar" in x]
    assert not roja or "cto" not in roja[0], "drenó 11 s antes del arranque: no está parado"


def test_sin_fila_de_cursor_no_es_lo_mismo_que_cursor_parado(tmp_path, monkeypatch):
    """Tercera vez que la alarma afirma más de lo que el dato sostiene (la caza
    CodeRabbit en el #4, tras las de `infra` y `cpo` en producción).

    Un rol que rebota y NO tiene fila en `cursors` caía en `parados`, y el informe
    decía «su cursor lleva parado >2 h» sin un solo sello que lo probara: no hay
    cursor, así que no hay nada parado. Es un estado distinto —nunca ha drenado— y
    merece su propia línea, no que se le atribuya una antigüedad inventada.
    """
    from datetime import datetime, timedelta, timezone

    from .conftest import db_directa

    s, c = _cliente(tmp_path, monkeypatch, PUERTA)
    _consumir(c, "cto-A")        # rebota y NUNCA tuvo cursor
    _consumir(c, "backend")      # rebota y su cursor es rancio de verdad
    rancio = (datetime.now(timezone.utc) - timedelta(hours=9)).isoformat(timespec="seconds")
    con = db_directa(s)
    con.execute("INSERT OR REPLACE INTO cursors VALUES (?,?,?,?)",
                ("be", "demo-ledger", 1, rancio))
    con.commit()
    con.close()

    t = c.get("/doctor").text
    parado = [x for x in t.splitlines() if "sin drenar desde hace" in x]
    assert parado and "be" in parado[0], "el cursor rancio SÍ se puede fechar"
    assert "cto" not in parado[0], "sin fila de cursor no se le pone antigüedad"
    nunca = [x for x in t.splitlines() if "nunca ha drenado" in x]
    assert nunca and "cto" in nunca[0], "y aun así hay que nombrarlo: no drena nada"


def test_cursor_con_updated_nulo_tampoco_se_fecha(tmp_path, monkeypatch):
    """Misma clase: la fila existe pero sin sello. `MAX(updated)` devuelve NULL y
    NULL no es «antiguo», es «no sé»."""
    from .conftest import db_directa

    s, c = _cliente(tmp_path, monkeypatch, PUERTA)
    _consumir(c, "cto-A")
    con = db_directa(s)
    con.execute("INSERT OR REPLACE INTO cursors VALUES (?,?,?,?)",
                ("cto", "demo-ledger", 1, None))
    con.commit()
    con.close()

    t = c.get("/doctor").text
    parado = [x for x in t.splitlines() if "sin drenar desde hace" in x]
    assert not parado or "cto" not in parado[0], "updated NULL no fecha nada"
    assert any("nunca ha drenado" in x and "cto" in x for x in t.splitlines())


def test_un_carril_INVALIDO_no_cuenta_como_consumo_con_carril(tmp_path, monkeypatch):
    """FALSADOR (CodeRabbit, #4): mandar una cabecera de carril NO es declarar carril.

    `anota_consumo()` corría antes de RESOLVER el carril, así que
    `X-Llminbox-Carril: no-existe` incrementaba la columna «CON carril» y acto seguido
    devolvía 422. Con eso, ⑤ podía publicar su ✓ verde —«todo rol que consume manda
    carril»— sostenido por peticiones que fallaron TODAS. Es el mismo error que este
    fichero arregla una capa más afuera: contar el intento como si fuera el efecto.
    """
    _, c = _cliente(tmp_path, monkeypatch, PUERTA)
    r = c.post("/inbox/backend/leido", json={"hasta": {}},
               headers={"X-Llminbox-Carril": "carril-que-no-existe"})
    assert r.status_code == 422
    assert "no resuelve" in r.json()["detail"]

    t = c.get("/doctor").text
    assert "0 consumo(s) CON carril" in t, "un 422 no es un consumo"
    assert "1 RECHAZADO" in t
    assert "✓ todo rol que consume manda carril" not in t, "el verde no puede salir de un 422"


def test_un_exito_viejo_no_inmuniza_al_que_dejo_de_mandar_carril(tmp_path, monkeypatch):
    """FALSADOR: la alarma de ⑤ sólo podía moverse hacia el silencio.

    `mudos` se calculaba con `not v[0]` — «cero éxitos en TODA la ventana». Un rol que
    mandó carril UNA vez al arrancar y desde entonces sólo rebota quedaba inmunizado
    para siempre: su acierto viejo lo sacaba de la alarma y nadie volvía a mirarlo.
    Con días de uptime eso significa que **una regresión es invisible** — justo lo que
    la sesión hermana midió en ② (un agregado que no puede moverse hace publicar la
    conclusión al revés a quien se fía de él).

    Aquí `be` acierta una vez, se envejece ese acierto, y luego sólo rebota: tiene que
    volver a aparecer. Control negativo: `cto` acierta AHORA y no debe aparecer.
    """
    from datetime import datetime, timedelta, timezone

    s, c = _cliente(tmp_path, monkeypatch, PUERTA)
    _consumir(c, "backend", "demo")                     # éxito… pero viejo
    _consumir(c, "cto-A", "demo")                       # éxito reciente (control)
    _consumir(c, "cto-A")                               # …y ADEMÁS rebota: no basta con
                                                        # que no rebote para no salir
    viejo = (datetime.now(timezone.utc) - timedelta(hours=9)).isoformat(timespec="seconds")
    s.CONSUMOS["be"] = [1, 0, viejo, viejo]
    for _ in range(3):
        _consumir(c, "backend")                         # desde entonces, sólo rebota

    quinta = c.get("/doctor").text.split("⑤")[1]
    assert "be" in quinta, "dejó de mandar carril hace 9 h y la alarma no lo ve"
    nombrados = [x for x in quinta.splitlines() if "be" in x]
    assert not any("cto" in x for x in nombrados), "el que acertó hace un momento no se denuncia"


def test_el_sello_del_acierto_no_retrocede(tmp_path, monkeypatch):
    """FALSADOR de la carrera que señaló CodeRabbit en el #5.

    `ahora_iso` se calcula FUERA del lock, así que dos peticiones concurrentes del
    mismo rol pueden entrar en orden inverso al de su sello. Sin guarda, la vieja
    pisa a la nueva y ⑤ clasifica como antiguo un acierto reciente. No se simula la
    concurrencia —sería un test que a veces pasa—: se ejerce la PROPIEDAD que la
    concurrencia rompería.
    """
    s, _ = _cliente(tmp_path, monkeypatch, PUERTA)
    s.anota_consumo("be", True, "2026-08-18T10:00:06+00:00")
    s.anota_consumo("be", True, "2026-08-18T10:00:05+00:00")   # la vieja, tarde
    assert s.CONSUMOS["be"][3] == "2026-08-18T10:00:06+00:00", "el acierto retrocedió"
    s.anota_consumo("be", True, "2026-08-18T10:00:09+00:00")   # control: sí avanza
    assert s.CONSUMOS["be"][3] == "2026-08-18T10:00:09+00:00"
