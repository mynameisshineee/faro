"""`GET /pendientes` y el interruptor de hombre muerto de la vigilancia compartida.

Diseño acordado con `harness` y ratificado en el ledger de 64bis: 60 vigías lógicos
sondean el servicio cada minuto cuando el abanico ya está hecho una vez en su
`vigilante()`. Un watcher compartido llama a `/pendientes` 1×/min y despierta sólo a
quien tiene correo — 60 peticiones/min pasan a 1.

LA CONDICIÓN, no una mejora: un watcher compartido es UN punto de fallo silencioso
donde hoy hay 60 redundantes. Los 60 son derroche RESILIENTE. Si el compartido muere,
la flota entera queda sorda a la vez y nadie se entera. Por eso el servicio —que es el
lado que sobrevive— tiene que notar la AUSENCIA de llamadas. Un muerto no avisa.
"""
from __future__ import annotations

import time

from fastapi.testclient import TestClient

from .conftest import construir, db_directa

H = {"X-Llminbox-Token": "test-token"}
# C5 · `/pendientes` exige AMBAS credenciales desde que agrega todos los inboxes de una.
# Va en una constante propia y NO tocando `H`: `H` la usan también el ACK y otros
# endpoints, y meterle el watcher ahí les regalaría una credencial que no piden — con
# eso, el ⊖ «los demás endpoints NO aceptan el token de watcher» dejaría de medir.
HW = {**H, "X-Llminbox-Watcher": "w"}
WENV = {"LLMINBOX_WATCHER_TOKEN": "w"}


def barrido_ok(s):
    s.SALUD["ultimo_ok"] = time.time()
    return s


def test_pendientes_responde_el_agregado(tmp_path, monkeypatch):
    """⊕ Una sola consulta contra la base contesta por TODOS, que es lo que permite
    cambiar 60 sondeos por uno."""
    s = construir(tmp_path, monkeypatch, extra_env=WENV)
    with TestClient(s.app) as c:
        con = db_directa(s)
        for n, r in s.LEDGERS.items():
            s.reindex(n, r, con)
        con.commit(); con.close()
        cuerpo = c.get("/pendientes", headers=HW).json()
    assert isinstance(cuerpo["pendientes"], list)
    assert cuerpo["pendientes"], "nadie tiene pendientes: el montaje no prueba nada"
    # EL LEDGER ES OBLIGATORIO EN CADA FILA: sin él, 55 de 62 destinatarios salen
    # ambiguos —un rol con una sesión por carril no dice a cuál despertar— y el
    # consumidor no puede resolver a qué sesión va el aviso.
    assert all({"quien", "ledger", "n"} <= set(f) for f in cuerpo["pendientes"]), (
        f"falta algún campo del contrato: {cuerpo['pendientes'][:2]}")
    assert cuerpo["destinatarios"] >= 1


def test_el_health_no_se_arma_antes_de_que_exista_el_watcher(tmp_path, monkeypatch):
    """⊖ EL MÁS IMPORTANTE DE LOS TRES. Si el hombre muerto se armara sin que nadie
    haya llamado nunca, `/health` se pondría ROJO en toda la flota el día que se
    despliegue esto y ANTES de que el watcher exista.

    Un gate que grita antes de tener nada que vigilar enseña a ignorar el rojo — que
    es la misma avería que un verde permanente, por el otro lado.

    ⚠️ EL MONTAJE DECLARA LA CREDENCIAL, y no es un detalle del arnés: lo que este test
    afirma es «esperar el primer ack no es una avería», y eso sólo vale cuando el ack
    ES ALCANZABLE. Sin `LLMINBOX_WATCHER_TOKEN` el estado no es `sin-armar` sino
    `inarmable` —nadie podrá ackear nunca— y ahí el rojo SÍ es correcto. Antes el test
    no lo declaraba porque los dos casos compartían casilla.
    """
    s = barrido_ok(construir(tmp_path, monkeypatch, extra_env={"LLMINBOX_WATCHER_TOKEN": "w"}))
    with TestClient(s.app) as c:
        cuerpo = c.get("/health").json()
    assert cuerpo["vigilancia_muda"] is None, "se armó sin que nadie hubiera llamado"
    assert cuerpo["ok"] is True


def test_si_el_watcher_calla_el_health_lo_dice(tmp_path, monkeypatch):
    """⊕ FALSADOR VINCULANTE del diseño: matar el watcher tiene que ponerse rojo.
    Sin esto, el cambio sustituye 60 fallos ruidosos por uno silencioso."""
    s = barrido_ok(construir(tmp_path, monkeypatch, extra_env={
        "LLMINBOX_VIGILANCIA_MUDA_S": "1", "LLMINBOX_WATCHER_TOKEN": "w"}))
    with TestClient(s.app) as c:
        # Late CON SU CREDENCIAL: tras C1, una llamada normal ya no arma — que es
        # justo lo que impide que los 60 vigías heredados lo mantengan vivo.
        c.post(ACK, headers={**H, "X-Llminbox-Watcher": "w"})
        assert c.get("/health").json()["vigilancia_muda"] is None
        time.sleep(1.2)                            # …y deja de llamar
        cuerpo = c.get("/health").json()
    assert cuerpo["vigilancia_muda"] is True, "el watcher calló y la salud no lo dijo"
    assert cuerpo["ok"] is False
    assert any("vigilancia" in a.lower() for a in (cuerpo["avisos"] or []))


def test_con_el_watcher_vivo_nada_se_pone_rojo(tmp_path, monkeypatch):
    """⊖ del falsador anterior — sin él, un `vigilancia_muda: True` constante también
    lo pasaría, y habríamos puesto la flota en rojo permanente."""
    s = barrido_ok(construir(tmp_path, monkeypatch, extra_env={
        "LLMINBOX_VIGILANCIA_MUDA_S": "60", "LLMINBOX_WATCHER_TOKEN": "w"}))
    with TestClient(s.app) as c:
        c.post(ACK, headers={**H, "X-Llminbox-Watcher": "w"})
        cuerpo = c.get("/health").json()
    assert cuerpo["vigilancia_muda"] is None
    assert cuerpo["ok"] is True


# ── C1 · SÓLO EL WATCHER LATE ────────────────────────────────────────────────
# Corrección de security, y es un fallo GRAVE de mi primera versión: cualquier
# llamada a `/pendientes` actualizaba el latido. Durante el rollout ADITIVO los 60
# vigías heredados siguen sondeando, así que habrían mantenido vivo el hombre muerto
# y NUNCA habría detectado la muerte del watcher compartido. Un interruptor que
# cualquiera puede alimentar no vigila a nadie: es fail-open silencioso, que es
# exactamente lo que este interruptor existe para impedir.

def test_una_llamada_sin_credencial_de_watcher_NO_late(tmp_path, monkeypatch):
    """FALSADOR C1: sondear `/pendientes` con el token normal NO puede armar ni
    refrescar el latido. Si lo hiciera, los 60 vigías heredados lo mantendrían vivo
    durante toda la adopción y el interruptor no saltaría jamás."""
    s = barrido_ok(construir(tmp_path, monkeypatch, extra_env={
        "LLMINBOX_WATCHER_TOKEN": "secreto-del-watcher",
        "LLMINBOX_VIGILANCIA_MUDA_S": "1"}))
    with TestClient(s.app) as c:
        c.get("/pendientes", headers=HW)                     # llamada NORMAL
        cuerpo = c.get("/health").json()
    assert cuerpo["vigilancia_muda"] is None, "una llamada normal armó el interruptor"
    assert cuerpo["vigilancia_ultimo_latido"] is None, "una llamada normal dejó latido"


def test_el_watcher_con_su_credencial_SI_late_y_se_identifica(tmp_path, monkeypatch):
    """⊖ del anterior — sin esto, «no latir nunca» pasaría el test de arriba y el
    interruptor no podría armarse jamás. Y `/health` expone QUIÉN latió: un latido
    anónimo no distingue al watcher de cualquiera que hubiera robado la ruta."""
    s = barrido_ok(construir(tmp_path, monkeypatch, extra_env={
        "LLMINBOX_WATCHER_TOKEN": "secreto-del-watcher",
        "LLMINBOX_VIGILANCIA_MUDA_S": "60"}))
    with TestClient(s.app) as c:
        c.post(ACK, headers={**H, "X-Llminbox-Watcher": "secreto-del-watcher"})
        cuerpo = c.get("/health").json()
    assert cuerpo["vigilancia_ultimo_latido"] is not None, "el watcher latió y no consta"
    assert cuerpo["vigilancia_muda"] is None
    assert cuerpo["ok"] is True


# ── C2 · EL ARMADO SOBREVIVE AL REINICIO ─────────────────────────────────────
def test_el_armado_persiste_a_un_reinicio(tmp_path, monkeypatch):
    """FALSADOR C2: mi primera versión guardaba el latido EN MEMORIA, así que un
    reinicio del servicio desarmaba el interruptor solo — y desarmarse solo es
    fail-open. Si el watcher murió y el servicio se reinició, tiene que seguir rojo.

    Se simula el reinicio construyendo un servicio NUEVO sobre la MISMA base.
    """
    env = {"LLMINBOX_WATCHER_TOKEN": "secreto-del-watcher",
           "LLMINBOX_VIGILANCIA_MUDA_S": "1"}
    s = barrido_ok(construir(tmp_path, monkeypatch, extra_env=env))
    with TestClient(s.app) as c:
        c.post(ACK, headers={**H, "X-Llminbox-Watcher": "secreto-del-watcher"})
    time.sleep(1.2)
    s2 = barrido_ok(construir(tmp_path, monkeypatch, extra_env=env))   # «reinicio»
    with TestClient(s2.app) as c:
        cuerpo = c.get("/health").json()
    assert cuerpo["vigilancia_muda"] is True, (
        "el reinicio desarmó el interruptor: fail-open, el defecto que viene a impedir")
    assert cuerpo["ok"] is False


# ── C6 · EL TIPO VIAJA EN LA FILA, NO EN UN COMENTARIO ───────────────────────
# Un comentario describe la forma de un contrato pero NO obliga a ningún consumidor;
# un campo sí. Si `tipo` no viaja, el watcher tiene que INFERIRLO de un nombre — y una
# inferencia sobre un nombre es exactamente donde esto se rompe: tratar `FLOTA` como
# destinatario normal despierta a los 60 por cada publicación a la flota.

def test_cada_fila_declara_su_tipo(tmp_path, monkeypatch):
    """FALSADOR C6: difusión, humano y agente salen MARCADOS, no adivinables.

    Lo que este falsador NO cubre, y hay que decirlo: comprobar que el campo está
    puesto pasa en verde mientras la amplificación ocurre aguas abajo. Contar
    despertares —un post a FLOTA despierta N sesiones UNA vez cada una— es del lado
    del watcher, y es un falsador CONJUNTO que ninguno de los dos puede correr solo.
    """
    censo = {
        "agentes": [{"nombre": "backend", "humano": "operador", "clave": "", "rol": "be"},
                    {"nombre": "cto-A", "humano": "operador", "clave": "", "rol": "cto"}],
        "humanos": [{"nombre": "operador", "alias": ["OPERADOR"]}],
        "difusion": ["FLOTA", "equipo"],
    }
    s = construir(tmp_path, monkeypatch, roster=censo, extra_env=WENV)
    # LOS TRES CASOS TIENEN QUE EXISTIR EN LA RESPUESTA, y por eso se siembran a
    # propósito. Mi primera versión ponía las aserciones dentro de un bucle sobre las
    # filas devueltas: si `FLOTA` no aparecía, el cuerpo del bucle no se ejecutaba y el
    # test pasaba en verde con la clasificación BORRADA. Lo destapó el mutante — quitar
    # la rama de difusión no rompía nada. Es el universo vacío otra vez, en mi control.
    ruta = tmp_path / "DEMO-LEDGER.md"
    ruta.write_text(ruta.read_text()
                    + "### [cto-A → FLOTA · FYI] a la flota\ncuerpo\n"
                    + "### [cto-A → OPERADOR · FYI] al humano\ncuerpo\n")
    with TestClient(s.app) as c:
        con = db_directa(s)
        for n, r in s.LEDGERS.items():
            s.reindex(n, r, con)
        con.commit(); con.close()
        filas = c.get("/pendientes", headers=HW).json()["pendientes"]
    tipos = {f["quien"].lower(): f["tipo"] for f in filas}
    assert all(f.get("tipo") for f in filas), f"alguna fila sin tipo: {filas[:2]}"
    assert set(tipos.values()) <= {"agente", "difusion", "humano", "desconocido"}
    # ⊕ los tres casos PRESENTES, comprobado antes de mirar su tipo — si no está, el
    # montaje no prueba nada y hay que saberlo, no pasar en verde.
    assert "flota" in tipos, f"el montaje no sembró difusión: {sorted(tipos)}"
    assert "operador" in tipos, f"el montaje no sembró humano: {sorted(tipos)}"
    assert "backend" in tipos, f"el montaje no sembró agente: {sorted(tipos)}"
    assert tipos["flota"] == "difusion", f"FLOTA salió {tipos['flota']}: amplificación ×N"
    assert tipos["operador"] == "humano", f"OPERADOR salió {tipos['operador']}: no es sesión"
    assert tipos["backend"] == "agente", f"backend salió {tipos['backend']}"


# ── CONTRATO VERSIONADO ──────────────────────────────────────────────────────
# Sin versión, mi fixture y el consumidor divergen sin que nadie lo vea — que es
# exactamente la avería que este endpoint existe para no repetir. Con versión, harness
# fija un número y SU selftest falla cuando yo cambie el contrato, en producción no.

CONTRATO = "pendientes/2"
CAMPOS_FILA = {"quien", "ledger", "n", "tipo", "tope", "eid_tope"}
CAMPOS_RAIZ = {"contrato", "pendientes", "filas", "destinatarios"}
ACK = "/vigilancia/ack"
TIPOS = {"agente", "difusion", "humano", "desconocido"}


def test_el_contrato_va_versionado_y_su_forma_esta_fijada(tmp_path, monkeypatch):
    """FALSADOR DEL CONTRATO: la versión viaja, y la FORMA está clavada aquí.

    Este test es el instrumento que hace verificable la costura: si alguien cambia la
    forma sin subir la versión, esto enrojece. Si sube la versión a propósito, tiene
    que venir aquí a cambiarlo — y eso es una decisión visible en un diff, no una
    divergencia silenciosa descubierta por el consumidor en caliente.

    SEMÁNTICA DE COMPATIBILIDAD, y va escrita para que no se interprete:
      · añadir un campo OPCIONAL a la fila o a la raíz  → NO sube la versión
      · quitar o renombrar un campo                      → SUBE
      · cambiar el significado de un campo existente     → SUBE (aunque el nombre siga)
      · añadir un valor nuevo a `tipo`                   → SUBE: el consumidor enruta
        por ese campo, y un valor que no sabe manejar es peor que un campo que falta
    """
    s = construir(tmp_path, monkeypatch, extra_env=WENV)
    with TestClient(s.app) as c:
        con = db_directa(s)
        for n, r in s.LEDGERS.items():
            s.reindex(n, r, con)
        con.commit(); con.close()
        d = c.get("/pendientes", headers=HW).json()
    assert d["contrato"] == CONTRATO, (
        f"el contrato cambió de versión: {d.get('contrato')} ≠ {CONTRATO}. Si es "
        f"deliberado, actualiza este test Y avisa al consumidor; si no lo es, acabas "
        f"de romper a harness sin enterarte.")
    assert CAMPOS_RAIZ <= set(d), f"faltan campos de raíz: {CAMPOS_RAIZ - set(d)}"
    assert d["pendientes"], "sin filas no se prueba la forma de la fila"
    for f in d["pendientes"]:
        assert CAMPOS_FILA <= set(f), f"fila incompleta: {f}"
        assert f["tipo"] in TIPOS, f"tipo fuera del contrato: {f['tipo']}"
        assert isinstance(f["n"], int) and f["n"] > 0


# ── B1 · WATERMARK, y su estabilidad declarada ───────────────────────────────
def test_la_fila_trae_watermark_y_su_identidad_estable(tmp_path, monkeypatch):
    """FALSADOR B1: sin `tope`+`eid_tope` el consumidor NO puede deduplicar.

    Con sólo `n`: si entra un mensaje y se consume otro, `n` no cambia y el nuevo se
    pierde; y si reescribe cada ciclo, redespierta a los 62 cada minuto por el mismo
    atraso. Y `tope` SOLO tampoco basta —es `MAX(arrival)`, monótono por instancia
    pero NO estable por entrada—: un re-parseo renumera y un replay se vuelve
    indistinguible de correo nuevo. Por eso viaja también `eid_tope`, que es identidad
    por contenido y sobrevive a la renumeración.
    """
    s = construir(tmp_path, monkeypatch, extra_env=WENV)
    with TestClient(s.app) as c:
        con = db_directa(s)
        for n, r in s.LEDGERS.items():
            s.reindex(n, r, con)
        con.commit(); con.close()
        filas = c.get("/pendientes", headers=HW).json()["pendientes"]
    assert filas
    for f in filas:
        assert isinstance(f["tope"], int), f"sin watermark no hay dedup posible: {f}"
        assert f["eid_tope"], f"sin identidad estable el replay parece correo nuevo: {f}"
        assert len(f["eid_tope"]) >= 32


# ── B2 · EL LATIDO ACREDITA EL CICLO, NO EL INTENTO ──────────────────────────
def test_el_GET_ya_no_late_ni_con_la_credencial_del_watcher(tmp_path, monkeypatch):
    """FALSADOR B2: traer los pendientes NO acredita que el ciclo se completara.

    Antes el GET latía —y encima ANTES de ejecutar la consulta—, así que un watcher
    vivo pero roto (que no parsea, no rutea o no escribe) mantenía la salud verde para
    siempre. La señal de vida tiene que acreditar el CICLO COMPLETADO por el sujeto
    vigilado, no que alguien llamó.
    """
    s = barrido_ok(construir(tmp_path, monkeypatch, extra_env={
        "LLMINBOX_WATCHER_TOKEN": "w", "LLMINBOX_VIGILANCIA_MUDA_S": "60"}))
    with TestClient(s.app) as c:
        c.get("/pendientes", headers={**H, "X-Llminbox-Watcher": "w"})
        cuerpo = c.get("/health").json()
    assert cuerpo["vigilancia"]["estado"] == "sin-armar", (
        "el GET latió: acredita el intento, no el ciclo")


def test_un_ciclo_declarado_fallido_no_late(tmp_path, monkeypatch):
    """⊖ del ACK: un watcher que detecta su propio fallo lo declara y NO late.
    Un ciclo fallido declarado vale más que un latido que miente."""
    s = barrido_ok(construir(tmp_path, monkeypatch, extra_env={
        "LLMINBOX_WATCHER_TOKEN": "w", "LLMINBOX_VIGILANCIA_MUDA_S": "60"}))
    with TestClient(s.app) as c:
        r = c.post(ACK + "?ciclo_ok=false&motivo=ruteo", headers={**H, "X-Llminbox-Watcher": "w"})
        assert r.json()["latido"] is False
        # R2 · el fallo CONOCIDO se ve YA, no dentro de N. Esperar a que el plazo lo
        # dedujera por ausencia era tirar evidencia que el watcher tenía en la mano.
        v = c.get("/health").json()["vigilancia"]
        assert v["estado"] == "fallida", f"un fallo declarado sigue invisible: {v}"
        assert v["motivo"] == "ruteo"
        assert c.get("/health").json()["ok"] is False


# ── I1 · LA FIXTURE ATRAVIESA EL GATE REAL ───────────────────────────────────
def test_sin_el_token_compartido_da_401_aunque_traiga_el_del_watcher(tmp_path, monkeypatch):
    """⊖ OBLIGATORIO: hacen falta LAS DOS credenciales, no una.

    El consumidor mandaba sólo `X-Llminbox-Watcher` y en producción se habría comido
    un 401 en la primera llamada — oculto porque las fixtures no atravesaban el GATE.
    Es la misma forma que el falsador de C6 que no discriminaba: verde en cada
    aserción, falsa la conclusión.
    """
    s = construir(tmp_path, monkeypatch, extra_env={"LLMINBOX_WATCHER_TOKEN": "w"})
    with TestClient(s.app) as c:
        r1 = c.get("/pendientes", headers={"X-Llminbox-Watcher": "w"})
        r2 = c.post(ACK, headers={"X-Llminbox-Watcher": "w"})
    assert r1.status_code == 401, f"pasó sin el token compartido: {r1.status_code}"
    assert r2.status_code == 401, f"el ack pasó sin el token compartido: {r2.status_code}"


# ── I2 · PRE-ARM TIENE NOMBRE PROPIO ─────────────────────────────────────────
def test_los_tres_estados_de_vigilancia_son_explicitos(tmp_path, monkeypatch):
    """FALSADOR I2: `ok:true` + `muda:null` se leía como «vivo» cuando podía ser
    PRE-ARM. Un estado sin nombre obliga al consumidor a inferirlo, y ahí se rompe —
    es mi propio argumento de C6 aplicado a mi propia salud."""
    s = barrido_ok(construir(tmp_path, monkeypatch, extra_env={
        "LLMINBOX_WATCHER_TOKEN": "w", "LLMINBOX_VIGILANCIA_MUDA_S": "1"}))
    with TestClient(s.app) as c:
        assert c.get("/health").json()["vigilancia"]["estado"] == "sin-armar"
        c.post(ACK, headers={**H, "X-Llminbox-Watcher": "w"})
        assert c.get("/health").json()["vigilancia"]["estado"] == "viva"
        time.sleep(1.2)
        v = c.get("/health").json()["vigilancia"]
    assert v["estado"] == "muda", f"tras callar sigue en {v['estado']}"
    assert v["tope_s"] == 1 and v["hace_s"] is not None


def test_el_ack_exige_la_credencial_del_watcher_no_solo_el_gate(tmp_path, monkeypatch):
    """⊖ QUE FALTABA, y lo destapó un mutante: quitar la comprobación de
    `X-Llminbox-Watcher` en el ACK no rompía NINGÚN test.

    Mi test de I1 comprobaba el 401 del GATE —el token compartido— y daba por cubierta
    también la credencial del watcher. No lo estaba: con el token compartido puesto y
    SIN el del watcher, cualquiera de los 69 agentes podría haber latido por él, y el
    hombre muerto habría vuelto a medir tráfico en vez de al vigía. Es C1 otra vez,
    entrando por la puerta nueva.
    """
    s = barrido_ok(construir(tmp_path, monkeypatch, extra_env={
        "LLMINBOX_WATCHER_TOKEN": "w", "LLMINBOX_VIGILANCIA_MUDA_S": "60"}))
    with TestClient(s.app) as c:
        sin = c.post(ACK, headers=H)                                   # sólo el GATE
        malo = c.post(ACK, headers={**H, "X-Llminbox-Watcher": "otro"})  # credencial mala
        assert sin.status_code == 403, f"latió sin credencial de watcher: {sin.status_code}"
        assert malo.status_code == 403, f"latió con credencial ajena: {malo.status_code}"
        assert c.get("/health").json()["vigilancia"]["estado"] == "sin-armar"


# ── LA PARTICIÓN · ningún estado es el `else` ────────────────────────────────
# Van SIETE instancias de la MISMA avería —C1, B2, I2, I3, el endpoint ausente, R1 y
# R2— y todas son la misma frase: EL ESTADO QUE NO TIENE CASILLA CAE AL LADO BUENO.
# Por eso el falsador que cierra esto no es uno por bug: es FORZAR CADA ESTADO y
# comprobar que se distingue de todos los demás. La octava instancia llegaría por el
# estado que hoy no se nos ocurre, que es justo el que un `else` se traga.

def _estado(c):
    return c.get("/health").json()["vigilancia"]["estado"]


def test_cada_estado_de_vigilancia_se_distingue_de_los_demas(tmp_path, monkeypatch):
    """FALSADOR DE LA PARTICIÓN — y la lista de casos SALE DEL ENUM, no de mi memoria.

    Mi versión anterior era la OCTAVA instancia de la clase que este test existe para
    impedir: declaraba seis estados, ejercía cinco, y afirmaba «cinco» en su propio
    assert. La prueba de exhaustividad no era exhaustiva, y encima lo decía.

    Y la causa no era despiste: enumeraba desde una lista escrita A MANO. Mientras sea
    así, cada estado nuevo nace sin caso y el verde sigue siendo verde. Iterando sobre
    `VIGILANCIA_ESTADOS`, el día que alguien añada un séptimo sin su forzador esto se
    pone rojo SOLO — la exhaustividad deja de ser una afirmación y pasa a ser una
    propiedad, que es la única forma en que se sostiene sin que nadie se acuerde.
    """
    env = {"LLMINBOX_WATCHER_TOKEN": "w", "LLMINBOX_VIGILANCIA_MUDA_S": "1"}

    def fuerza_sin_armar(c, s):
        pass                                   # recién construido: nadie ha ackeado

    def fuerza_inarmable(c, s):
        # No es «nadie ha ackeado todavía» sino «nadie PUEDE ackear»: sin credencial el
        # ack contesta 403 y la vigilancia no tiene salida. Se fuerza vaciando el token
        # EN EL MÓDULO ya construido, porque el estado no depende del entorno del
        # arranque sino de si el mecanismo puede funcionar ahora.
        # Y se limpia el rastro del forzador anterior: los del bucle COMPARTEN `tmp_path`,
        # así que el `fallida` que persistió el de antes seguía en `meta` y ganaba la
        # partición (se evalúa primero, y con razón: un fallo declarado es información).
        # Sin esto, «forcé inarmable y salió fallida» — el arnés midiendo su propia
        # herencia, no el producto.
        con = s.db()
        con.execute("DELETE FROM meta WHERE k IN (?,?)", (s._META_FALLIDA, s._META_LATIDO))
        con.commit(); con.close()
        s.WATCHER_TOKEN = ""

    def fuerza_viva(c, s):
        c.post(ACK, headers={**H, "X-Llminbox-Watcher": "w"})

    def fuerza_muda(c, s):
        c.post(ACK, headers={**H, "X-Llminbox-Watcher": "w"}); time.sleep(1.2)

    def fuerza_fallida(c, s):
        c.post(ACK + "?ciclo_ok=false&motivo=escritura",
               headers={**H, "X-Llminbox-Watcher": "w"})

    def fuerza_ilegible(c, s):
        s._latido_vigilancia = lambda: (None, None, None, False)

    def fuerza_indeterminado(c, s):
        # Reloj movido: el latido queda en el FUTURO y la resta da negativo. No es
        # `viva` —el instrumento no puede decir cuánto hace— y no puede caer al lado
        # bueno por no tener casilla.
        s._latido_vigilancia = lambda: (time.time() + 3600, "watcher-e2e", None, True)

    FORZADORES = {"sin-armar": fuerza_sin_armar, "viva": fuerza_viva,
                  "muda": fuerza_muda, "fallida": fuerza_fallida,
                  "inarmable": fuerza_inarmable,
                  "ilegible": fuerza_ilegible, "indeterminado": fuerza_indeterminado}

    import servicio as _s0
    declarados = set(_s0.VIGILANCIA_ESTADOS)
    assert declarados == set(FORZADORES), (
        f"hay estados declarados sin forzador (o al revés): "
        f"{declarados ^ set(FORZADORES)}. Si has añadido un estado, añade su caso: "
        f"un estado sin forzar es exactamente el hueco que este test existe para cerrar.")

    vistos = {}
    for nombre, forzar in FORZADORES.items():
        s = barrido_ok(construir(tmp_path, monkeypatch, extra_env=env))
        with TestClient(s.app) as c:
            forzar(c, s)
            d = c.get("/health").json()
        vistos[nombre] = (d["vigilancia"]["estado"], d["ok"])

    for esperado, (real, _) in vistos.items():
        assert real == esperado, f"forcé «{esperado}» y salió «{real}»"
    assert len({v[0] for v in vistos.values()}) == len(FORZADORES), (
        f"dos estados distintos dan el mismo valor: {vistos}")
    # Y lo que de verdad importa: SÓLO los declarados sanos dejan `ok:true`.
    for nombre, (_, ok) in vistos.items():
        esperado_ok = nombre in _s0.VIGILANCIA_SANOS
        assert ok is esperado_ok, (
            f"«{nombre}» dio ok={ok} y VIGILANCIA_SANOS dice {esperado_ok}")


def test_la_enumeracion_viaja_al_lector(tmp_path, monkeypatch):
    """⊖ del anterior: el lector no tiene que adivinar qué valores existen ni cuáles
    son sanos. Si mañana añado un estado, lo ve en la respuesta en vez de tratarlo
    como desconocido-luego-benigno."""
    s = barrido_ok(construir(tmp_path, monkeypatch))
    with TestClient(s.app) as c:
        v = c.get("/health").json()["vigilancia"]
    assert set(v["sanos"]) == {"viva", "sin-armar"}
    assert {"muda", "fallida", "ilegible", "indeterminado"} <= set(v["estados"])


def test_un_fallo_declarado_sobrevive_al_reinicio_y_solo_lo_limpia_un_ack_bueno(
        tmp_path, monkeypatch):
    """⊖⊖ de R2: persiste el reinicio, y NO se limpia con el tiempo — sólo con un ack
    bueno. Que se limpiara solo sería volver a perder la evidencia que R2 conserva."""
    env = {"LLMINBOX_WATCHER_TOKEN": "w", "LLMINBOX_VIGILANCIA_MUDA_S": "60"}
    s = barrido_ok(construir(tmp_path, monkeypatch, extra_env=env))
    with TestClient(s.app) as c:
        c.post(ACK + "?ciclo_ok=false&motivo=fetch", headers={**H, "X-Llminbox-Watcher": "w"})
    s2 = barrido_ok(construir(tmp_path, monkeypatch, extra_env=env))   # «reinicio»
    with TestClient(s2.app) as c:
        assert _estado(c) == "fallida", "el reinicio borró un fallo conocido"
        c.post(ACK, headers={**H, "X-Llminbox-Watcher": "w"})           # ack BUENO
        assert _estado(c) == "viva", "un ack bueno no limpió el fallo"


def test_un_motivo_fuera_de_la_lista_no_se_guarda_como_prosa(tmp_path, monkeypatch):
    """⊖ del código cerrado: un motivo inventado por el watcher cae en `otro`, que es
    visible y clasificable. Guardar prosa remota sería reintroducir la interpretación
    que el campo viene a quitar — mismo principio que el `tipo` de las filas."""
    s = barrido_ok(construir(tmp_path, monkeypatch, extra_env={
        "LLMINBOX_WATCHER_TOKEN": "w", "LLMINBOX_VIGILANCIA_MUDA_S": "60"}))
    with TestClient(s.app) as c:
        r = c.post(ACK + "?ciclo_ok=false&motivo=se-me-cayo-el-disco",
                   headers={**H, "X-Llminbox-Watcher": "w"})
        assert r.json()["motivo"] == "otro"
        assert c.get("/health").json()["vigilancia"]["motivo"] == "otro"


# ── P9 · LA CAPA DE COMPAT MENTÍA ────────────────────────────────────────────
def test_el_campo_legado_es_seguro_para_los_seis_estados(tmp_path, monkeypatch):
    """DÉCIMA instancia de la clase, y en el sitio más difícil de mirar.

    Arreglé el campo NUEVO con una partición exhaustiva y el VIEJO seguía mintiendo:
    `vigilancia_muda = (_vest == "muda")` dejaba `null` para `fallida`, `ilegible` e
    `indeterminado`, así que cualquier lector sin migrar los veía BENIGNOS.

    La causa merece nombre porque volverá: una capa de compatibilidad se escribe para
    PRESERVAR LA SEMÁNTICA VIEJA — y aquí la semántica vieja ERA EL DEFECTO. Un shim
    fiel reproduce fielmente la avería. La regla correcta no es «mantén el campo como
    estaba» sino «mantén el NOMBRE y dale la semántica SEGURA».

    Y muerde justo durante la CONVIVENCIA, que es la fase larga y la que el diseño
    aditivo hace inevitable: no es un residuo, es el modo por defecto durante semanas.

    Tabular sobre el enum, no sobre una lista a mano: mismo remedio que el falsador de
    la partición.
    """
    import servicio as _s0
    env = {"LLMINBOX_WATCHER_TOKEN": "w", "LLMINBOX_VIGILANCIA_MUDA_S": "1"}
    def _borra_latido(s):
        con = s.db()
        con.execute("DELETE FROM meta WHERE k IN (?,?)", (s._META_LATIDO, s._META_FALLIDA))
        con.commit(); con.close()

    forz = {
        "sin-armar": lambda c, s: None,
        "viva": lambda c, s: c.post(ACK, headers={**H, "X-Llminbox-Watcher": "w"}),
        # `inarmable`: nunca hubo latido Y NO PUEDE HABERLO. Para un lector sin migrar
        # es tan no-benigno como `muda` — más, porque no tiene salida.
        # Limpia el latido que dejó el forzador anterior: los del bucle COMPARTEN
        # `tmp_path`, y con latido heredado esto sale `viva`, no `inarmable`.
        "inarmable": lambda c, s: (_borra_latido(s), setattr(s, "WATCHER_TOKEN", "")),
        "muda": lambda c, s: (c.post(ACK, headers={**H, "X-Llminbox-Watcher": "w"}),
                              time.sleep(1.2)),
        "fallida": lambda c, s: c.post(ACK + "?ciclo_ok=false&motivo=fetch",
                                       headers={**H, "X-Llminbox-Watcher": "w"}),
        "ilegible": lambda c, s: setattr(s, "_latido_vigilancia",
                                         lambda: (None, None, None, False)),
        "indeterminado": lambda c, s: setattr(s, "_latido_vigilancia",
                                              lambda: (time.time() + 3600, "w", None, True)),
    }
    assert set(_s0.VIGILANCIA_ESTADOS) == set(forz), "el enum y la tabla divergieron"
    for nombre, forzar in forz.items():
        s = barrido_ok(construir(tmp_path, monkeypatch, extra_env=env))
        with TestClient(s.app) as c:
            forzar(c, s)
            d = c.get("/health").json()
        sano = nombre in _s0.VIGILANCIA_SANOS
        assert d["vigilancia_muda"] is (None if sano else True), (
            f"«{nombre}»: el campo legado dice {d['vigilancia_muda']!r} y el estado "
            f"{'es sano' if sano else 'NO es sano'} — un lector sin migrar lo lee mal")


# ── P10 · `quien` NO PUEDE SER TEXTO LIBRE ───────────────────────────────────
def test_la_identidad_del_latido_se_canonicaliza(tmp_path, monkeypatch):
    """El token autentica QUE PUEDE ESCRIBIR, no QUÉ escribe. Son dos controles y sólo
    tenía el primero.

    `quien` viene de fuera, se persiste y luego se muestra en `/health` — misma
    procedencia y mismo riesgo que el `who` remoto que acordamos que jamás toque una
    ruta de fichero. Forma cerrada, o `no-canonico`: nunca el valor crudo.
    """
    s = barrido_ok(construir(tmp_path, monkeypatch, extra_env={
        "LLMINBOX_WATCHER_TOKEN": "w", "LLMINBOX_VIGILANCIA_MUDA_S": "60"}))
    with TestClient(s.app) as c:
        c.post(ACK + "?quien=Watcher-Compartido_1", headers={**H, "X-Llminbox-Watcher": "w"})
        v = c.get("/health").json()["vigilancia"]
        assert v["quien"] == "watcher-compartido_1" and v["quien_canonico"] is True
        for ajeno in ("../../etc/passwd", "<script>x</script>", "a" * 200, "con espacios"):
            c.post(ACK + f"?quien={ajeno}", headers={**H, "X-Llminbox-Watcher": "w"})
            v = c.get("/health").json()["vigilancia"]
            # A1 · la marca es ESTRUCTURAL. Mi primera cura usaba el centinela
            # `no-canonico`, que casa la propia regex: un watcher llamado así de verdad
            # era indistinguible de un rechazo. Un campo aparte no colisiona jamás.
            assert v["quien"] is None and v["quien_canonico"] is False, (
                f"«{ajeno[:20]}» dejó {v['quien']!r}")


# ── A1 · EL CENTINELA VIVÍA DENTRO DEL DOMINIO QUE MARCABA ───────────────────
def test_una_identidad_literal_no_canonico_se_distingue_del_rechazo(tmp_path, monkeypatch):
    """Mi cura de P10 metió un centinela DENTRO del dominio que pretendía marcar: la
    cadena `no-canonico` casa mi propia regex, así que un watcher que se llamara así de
    verdad era indistinguible de uno rechazado.

    Es mi propio argumento de C6 sin aplicar al sitio nuevo — decidí marcar el tipo con
    un CAMPO y no con prosa porque «un comentario no obliga a ningún consumidor; un
    campo sí», y aquí volví al valor especial. Y la diferencia no es de elegancia:
    cualquier cadena elegida fuera del dominio es sólo IMPROBABLE; un booleano aparte
    es imposible de colisionar POR CONSTRUCCIÓN. Reducir el riesgo no es eliminarlo.
    """
    s = barrido_ok(construir(tmp_path, monkeypatch, extra_env={
        "LLMINBOX_WATCHER_TOKEN": "w", "LLMINBOX_VIGILANCIA_MUDA_S": "60"}))
    with TestClient(s.app) as c:
        c.post(ACK + "?quien=no-canonico", headers={**H, "X-Llminbox-Watcher": "w"})
        legitimo = c.get("/health").json()["vigilancia"]
        c.post(ACK + "?quien=../../etc/passwd", headers={**H, "X-Llminbox-Watcher": "w"})
        rechazado = c.get("/health").json()["vigilancia"]
    assert legitimo["quien"] == "no-canonico" and legitimo["quien_canonico"] is True
    assert rechazado["quien"] is None and rechazado["quien_canonico"] is False, (
        f"el rechazo no se distingue estructuralmente: {rechazado}")


# ── A2 · EL FALLO HEREDABA EL NOMBRE DEL ÚLTIMO QUE ACERTÓ ───────────────────
def test_un_ciclo_fallido_se_atribuye_a_quien_lo_declaro(tmp_path, monkeypatch):
    """PEOR QUE UNA AUSENCIA: `/health` no se quedaba sin decir quién falló — decía un
    nombre EQUIVOCADO, el del último que lo hizo bien.

    Una ausencia empuja a investigar; un nombre falso CIERRA la investigación. Y en
    cuanto haya más de un watcher, ese nombre es sobre el que alguien actúa.
    """
    s = barrido_ok(construir(tmp_path, monkeypatch, extra_env={
        "LLMINBOX_WATCHER_TOKEN": "w", "LLMINBOX_VIGILANCIA_MUDA_S": "60"}))
    with TestClient(s.app) as c:
        c.post(ACK + "?quien=watcher-a", headers={**H, "X-Llminbox-Watcher": "w"})
        c.post(ACK + "?quien=watcher-b&ciclo_ok=false&motivo=ruteo",
               headers={**H, "X-Llminbox-Watcher": "w"})
        v = c.get("/health").json()["vigilancia"]
    assert v["estado"] == "fallida"
    assert v["quien"] == "watcher-b", f"el fallo se atribuyó a {v['quien']!r}, no a quien lo declaró"
    assert v["quien"] != "watcher-a"


def test_un_fallo_con_identidad_invalida_queda_explicito(tmp_path, monkeypatch):
    """⊖ del anterior: si quien declara el fallo manda una identidad no canónica, el
    fallo NO puede heredar la anterior — se declara sin atribuir."""
    s = barrido_ok(construir(tmp_path, monkeypatch, extra_env={
        "LLMINBOX_WATCHER_TOKEN": "w", "LLMINBOX_VIGILANCIA_MUDA_S": "60"}))
    with TestClient(s.app) as c:
        c.post(ACK + "?quien=watcher-a", headers={**H, "X-Llminbox-Watcher": "w"})
        c.post(ACK + "?quien=<script>&ciclo_ok=false&motivo=schema",
               headers={**H, "X-Llminbox-Watcher": "w"})
        v = c.get("/health").json()["vigilancia"]
    assert v["estado"] == "fallida"
    assert v["quien"] is None and v["quien_canonico"] is False, (
        f"heredó un culpable: {v}")
