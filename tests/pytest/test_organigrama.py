"""Organigrama ejecutable — la jerarquía deja de ser prosa y pasa a ser dato.

Orden del operador 2026-08-13. Tres piezas, cada una con su falsador, porque las
tres son de la clase «parece que funciona si no lo miras»:

  ① `escucha_autor` — QA lee lo que el CPO **escribe**, no lo que le **llega**.
     `escucha` (el campo que ya existía) filtra por `recipients.who`
     (servicio.py, la subconsulta EXISTS del /inbox): entrega lo dirigido A
     alguien. Para «QA valida contra los criterios que puso el CPO, no contra
     lo que Backend entendió» hace falta lo contrario: suscripción por AUTOR.
     Se comprobó ANTES de escribir esto — el campo viejo no servía, y montarlo
     igual habría dado una bandeja plausible y equivocada.

  ② tope de claims `ejecuta` VIVOS por rol — medido el 2026-08-13 sobre el
     despliegue real: 69 claims abiertos, **los 69 vencidos**, `contratos`
     acaparando 21. El reparto era un candado que no cerraba.

  ③ `jerarquia` servida por el propio servicio — para que un agente pregunte
     a quién reporta en vez de leer 18 KB de prosa que no va a abrir.

⛔ Lo que estos tests NO prueban, dicho aquí para que nadie lo dé por cubierto:
   que el coto se deduzca del TEXTO del tema. No se intenta a propósito — el
   propio `tablero_abierto()` rechaza un detector de parecidos con medición
   (Jaccard 0,111 sobre el par real que casi chocó). Un tope por rol no lee el
   tema; por eso no se puede equivocar al leerlo.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from .conftest import construir

CAB = {"X-Llminbox-Token": "test-token"}

ROSTER_ORG = {
    "agentes": [
        {"nombre": "cpo", "humano": "operador", "clave": "", "rol": "cpo"},
        {"nombre": "cto-A", "humano": "operador", "clave": "", "rol": "cto"},
        # El único con suscripción por autor. Los demás son el falsador vivo:
        # si el correo del CPO les llega a ellos también, no lo entregó
        # `escucha_autor`, lo entregó un colador.
        {"nombre": "qa", "humano": "operador", "clave": "", "rol": "qa",
         "escucha_autor": ["cpo"]},
        {"nombre": "backend", "humano": "operador", "clave": "", "rol": "be"},
    ],
    "humanos": [{"nombre": "operador", "alias": ["Operador"]}],
    "difusion": ["equipo"],
}

# `cpo → cto-A` NO nombra a qa por ningún lado. Ese es el caso entero: hoy QA
# sólo se entera de un criterio si el CPO se acuerda de ponerle en copia, y
# ponerle en copia es justo el fan-out que estamos intentando bajar.
ENTRADAS = (
    "### [cpo → cto-A · PRODUCED] 2026-08-13T10:00:00Z — criterio de aceptacion: Esc cierra el modal\n"
    "El modal se cierra con Escape y devuelve el foco al disparador.\n"
    "\n"
    "### [cto-A → backend · REQUEST] 2026-08-13T10:05:00Z — implementa el modal\n"
    "cuerpo sin nada para qa\n"
)


def _montar(tmp_path, monkeypatch, roster=None, extra_env=None):
    """`construir` siembra su propio ledger; lo sobreescribimos ANTES del barrido."""
    s = construir(tmp_path, monkeypatch, extra_env=extra_env,
                  roster=roster if roster is not None else ROSTER_ORG)
    (tmp_path / "DEMO-LEDGER.md").write_text(ENTRADAS)
    (tmp_path / "OTRO-LEDGER.md").write_text("")
    return s


def _cliente(s):
    c = TestClient(s.app)
    c.__enter__()
    s.barrido()
    c.headers.update(CAB)
    return c


# ── ① escucha_autor ───────────────────────────────────────────────────────────

def test_qa_recibe_lo_que_escribe_el_cpo_aunque_no_le_nombre(tmp_path, monkeypatch):
    """El caso que pidió el operador: el criterio llega a QA sin que el CPO le copie."""
    s = _montar(tmp_path, monkeypatch)
    c = _cliente(s)
    cuerpo = c.get("/inbox/qa").text
    assert "criterio de aceptacion" in cuerpo, (
        "QA no recibió lo que escribió el CPO — la suscripción por autor no entrega")


def test_solo_el_suscrito_recibe_al_cpo(tmp_path, monkeypatch):
    """FALSADOR de ①. Si esto también pasa, no hay suscripción: hay colador.

    `backend` no tiene `escucha_autor`. Si el criterio del CPO aparece en SU
    bandeja, el test de arriba estaría verde por la razón equivocada.
    """
    s = _montar(tmp_path, monkeypatch)
    c = _cliente(s)
    cuerpo = c.get("/inbox/backend").text
    assert "criterio de aceptacion" not in cuerpo, (
        "el correo del CPO llegó a quien NO lo escucha — la entrega no discrimina")


def test_suscribirse_a_un_autor_no_abre_toda_la_bandeja(tmp_path, monkeypatch):
    """FALSADOR 2 de ①: se escucha a UN autor, no «todo lo que pase».

    La entrada `cto-A → backend` no la escribe el CPO y no nombra a QA: no tiene
    por qué estar en su bandeja. Si está, `escucha_autor` se implementó como un
    OR que se traga el filtro entero.
    """
    s = _montar(tmp_path, monkeypatch)
    c = _cliente(s)
    cuerpo = c.get("/inbox/qa").text
    assert "implementa el modal" not in cuerpo, (
        "QA recibió correo de un autor al que no está suscrito")


def test_suscribirse_a_un_rol_cubre_TODAS_sus_firmas(tmp_path, monkeypatch):
    """El fallo que casi se me cuela, cazado midiendo antes de firmar el censo.

    El CPO no firma con un nombre: firma con TRES (medido 2026-08-13 sobre los
    ledgers vivos — `cpo` 778, `cpo-biklabs` 265, `cpo-cfo-cockpit` 84). Una
    suscripción al nombre literal `cpo` habría entregado el 69 % del criterio de
    producto y perdido el carril cfocockpit ENTERO, sin un solo error: bandeja
    verde, plausible, y con un tercio menos. Por eso se escucha al ROL.
    """
    roster = json.loads(json.dumps(ROSTER_ORG))
    roster["agentes"].append(
        {"nombre": "cpo-cfo-cockpit", "humano": "operador", "clave": "", "rol": "cpo"})
    s = _montar(tmp_path, monkeypatch, roster=roster)
    (tmp_path / "DEMO-LEDGER.md").write_text(
        "### [cpo-cfo-cockpit → cto-A · PRODUCED] 2026-08-13T11:00:00Z — criterio del otro carril\n"
        "el mismo rol, otra firma\n")
    c = _cliente(s)
    assert "criterio del otro carril" in c.get("/inbox/qa").text, (
        "suscrito al rol 'cpo' pero sólo entrega la firma literal — el resto del "
        "rol se pierde en silencio")


def test_escuchar_al_cpo_no_le_consume_su_bandeja(tmp_path, monkeypatch):
    """El cursor es de quien escucha, nunca del escuchado (misma garantía que
    `escucha`, PROTOCOL §6.1). Si QA leyendo vaciara al CPO, la suscripción
    costaría el correo de un C-suite."""
    s = _montar(tmp_path, monkeypatch)
    c = _cliente(s)
    c.get("/inbox/qa")
    antes = c.get("/cursor/cpo").json()
    c.post("/inbox/qa/leido", json={})
    assert c.get("/cursor/cpo").json() == antes, (
        "marcar leído en QA movió el cursor del CPO")


# ── ② tope de claims `ejecuta` vivos por rol ─────────────────────────────────

def test_un_rol_no_puede_acaparar_mas_de_su_tope(tmp_path, monkeypatch):
    """`contratos` tenía 21 abiertos el día que se midió. Con tope 2, el 3º cae."""
    s = _montar(tmp_path, monkeypatch, extra_env={"LLMINBOX_TOPE_EJECUTA": "2"})
    c = _cliente(s)
    for t in ("tema_uno", "tema_dos"):
        assert c.post("/claim", json={"tema": t, "agent": "backend"}).json()["ok"]
    r = c.post("/claim", json={"tema": "tema_tres", "agent": "backend"}).json()
    assert r["ok"] is False, "el tercer claim entró con el tope en 2"
    assert r.get("tope") == 2
    assert r.get("abiertos"), "el rechazo no dice QUÉ tienes abierto — es un no mudo"


def test_el_tope_es_por_rol_y_no_frena_a_otro(tmp_path, monkeypatch):
    """FALSADOR de ②: que el tope no sea un contador global disfrazado."""
    s = _montar(tmp_path, monkeypatch, extra_env={"LLMINBOX_TOPE_EJECUTA": "1"})
    c = _cliente(s)
    assert c.post("/claim", json={"tema": "suyo", "agent": "backend"}).json()["ok"]
    otro = c.post("/claim", json={"tema": "del_otro", "agent": "cpo"}).json()
    assert otro["ok"] is True, "el tope de un rol bloqueó a otro rol distinto"


def test_los_vencidos_no_cuentan_para_el_tope(tmp_path, monkeypatch):
    """FALSADOR 2 de ②, y es el que evita el daño: si los vencidos contaran, el
    despliegue real —69 abiertos, 69 vencidos— arrancaría con TODOS los roles
    bloqueados el primer día. Un tope que se cuenta sobre trabajo muerto no
    reparte: ladrillea."""
    s = _montar(tmp_path, monkeypatch,
                extra_env={"LLMINBOX_TOPE_EJECUTA": "1", "LLMINBOX_CLAIM_TTL_H": "0"})
    c = _cliente(s)
    assert c.post("/claim", json={"tema": "viejo", "agent": "backend"}).json()["ok"]
    r = c.post("/claim", json={"tema": "nuevo", "agent": "backend"}).json()
    assert r["ok"] is True, "un claim VENCIDO consumió plaza del tope"


def test_el_tope_de_ejecuta_no_toca_las_plazas_de_revision(tmp_path, monkeypatch):
    """FALSADOR 3 de ②: revisar es lo que caza los datos falsos y NO se toca
    (ORGANIGRAMA §5ter). El tope nuevo es de ejecución; si recorta revisión,
    hemos apagado el mecanismo horizontal para arreglar el vertical."""
    s = _montar(tmp_path, monkeypatch, extra_env={"LLMINBOX_TOPE_EJECUTA": "1"})
    c = _cliente(s)
    c.post("/claim", json={"tema": "algo", "agent": "backend"})
    r = c.post("/claim", json={"tema": "otra_cosa", "agent": "backend", "rol": "revisa"})
    assert r.json()["ok"] is True, "el tope de ejecuta bloqueó una plaza de REVISIÓN"


# ── ④ la copia (fan-out), medida ─────────────────────────────────────────────
#
# La regla YA existe y lleva 5 días sin instrumento: ORGANIGRAMA §5ter dice «al
# nombrar, nombra a UNO», medido el 2026-08-08 en «52 % de los titulares nombra a
# 5-6». El 2026-08-13 la media de destinatarios por entrada dirigida era 6,38 —
# o sea, la regla no se cumple y NADIE lo sabía, porque nada la contaba. No se
# inventa regla nueva: se le pone el contador que le faltaba.
#
# ⛔ Y NO se bloquea al escribir. El camino canónico de append es `>>` (PROTOCOL
# §8) — un gate en el servicio lo esquiva cualquiera con un `printf`. Poner un
# candado en la puerta que nadie usa es teatro; contar y publicar, no.

FANOUT = (
    "### [cto-A → backend ∧ qa ∧ cpo ∧ sdet · PRODUCED] 2026-08-13T10:00:00Z — a cuatro\n"
    "cuerpo\n"
    "### [backend → cto-A · PRODUCED] 2026-08-13T10:01:00Z — a uno\n"
    "cuerpo\n"
)


def test_lint_mide_la_copia_y_senala_a_quien_mas_reparte(tmp_path, monkeypatch):
    roster = json.loads(json.dumps(ROSTER_ORG))
    roster["agentes"].append(
        {"nombre": "sdet", "humano": "operador", "clave": "", "rol": "sdet"})
    s = construir(tmp_path, monkeypatch, roster=roster)
    (tmp_path / "DEMO-LEDGER.md").write_text(FANOUT)
    (tmp_path / "OTRO-LEDGER.md").write_text("")
    c = _cliente(s)
    txt = c.get("/lint").text
    assert "COPIA" in txt, "/lint no mide la copia — la regla sigue sin contador"
    # 5 destinatarios / 2 entradas dirigidas = 2.50 de media global.
    assert "2.50" in txt or "2,50" in txt, f"media de copia mal calculada:\n{txt}"
    # Y NOMBRA al que reparte de más: cto pone 4, be pone 1.
    assert "cto" in txt and "4.00" in txt, "no señala quién pone más nombres"


def test_lint_no_inventa_alarma_cuando_todos_nombran_a_uno(tmp_path, monkeypatch):
    """FALSADOR de ④, y este carril tiene cicatriz: la versión anterior de un
    detector de este mismo endpoint fabricó TRES falsos positivos y se publicaron.
    Con todo el mundo cumpliendo la regla, la media es 1.00 y no hay a quién
    señalar."""
    s = construir(tmp_path, monkeypatch, roster=ROSTER_ORG)
    (tmp_path / "DEMO-LEDGER.md").write_text(
        "### [cto-A → backend · PRODUCED] 2026-08-13T10:00:00Z — a uno\ncuerpo\n"
        "### [backend → cto-A · PRODUCED] 2026-08-13T10:01:00Z — a uno tambien\ncuerpo\n")
    (tmp_path / "OTRO-LEDGER.md").write_text("")
    c = _cliente(s)
    txt = c.get("/lint").text
    assert "1.00" in txt or "1,00" in txt, "con todos a UNO, la media no es 1"
    assert "🔴" not in txt.split("COPIA")[-1][:400], (
        "marca en rojo a gente que cumple la regla — alarma fabricada")


# ── ⑤ «¿qué tengo cogido yo?» ────────────────────────────────────────────────
#
# Propuse un endpoint compuesto `/mi-sitio` (quién eres · a quién reportas · qué
# tienes cogido · a qué te suscribes) y el CPO lo tumbó con la escalera en la mano,
# paso 2 (¿ya está en el repo?): TRES de esas cuatro piezas YA se imprimen solas en
# cada arranque, y un GET que hay que acordarse de teclear pierde contra lo que ya
# está delante de los ojos — que es literalmente el argumento del propio
# `carril-banner.sh`. Sólo faltaba UNA pieza, y no necesitaba endpoint: un filtro.
#
# ⚠️ Lo que esto NO arregla, y hay que decirlo porque es la mitad grande: un GET es
# PULL. Que `contratos` PUEDA ver sus 23 abiertos no es que nada le OBLIGUE a mirar.
# Una consulta que nadie hace por costumbre no cura un cierre que nadie hace por
# costumbre.

def test_claims_filtra_por_agente(tmp_path, monkeypatch):
    s = _montar(tmp_path, monkeypatch)
    c = _cliente(s)
    c.post("/claim", json={"tema": "mio_uno", "agent": "backend"})
    c.post("/claim", json={"tema": "de_otro", "agent": "cpo"})
    d = c.get("/claims?agent=backend").json()
    temas = {x["tema"] for x in d["claims"]}
    assert temas == {"mio_uno"}, f"el filtro no acota: {temas}"
    assert d["abiertos"] == 1


def test_claims_acepta_el_ROL_ademas_del_nombre(tmp_path, monkeypatch):
    """Cazado por mi propio falsador en producción, 5 minutos después de desplegar:
    `?agent=contratosbik` devolvía sus 23 y `?agent=contratos` daba 422, aunque el
    docstring prometía que cualquier alias del rol da lo mismo. La causa es la misma
    dualidad nombre/rol de todo este trabajo: el censo guarda por ROL (`contratos`)
    pero el nombre censado es `contratosbik`, y el guard miraba sólo la lista de
    NOMBRES. Un agente escribe el suyo como le sale; si acierta, ve su trabajo, y si
    no, se lleva un 422 que parece que él no existe."""
    s = _montar(tmp_path, monkeypatch)
    c = _cliente(s)
    c.post("/claim", json={"tema": "algo_mio", "agent": "backend"})
    por_nombre = c.get("/claims?agent=backend")
    por_rol = c.get("/claims?agent=be")
    assert por_rol.status_code == 200, "pedirlo por el ROL da error"
    assert por_rol.json()["abiertos"] == por_nombre.json()["abiertos"] == 1


def test_claims_separa_propiedad_de_plazas_de_revision(tmp_path, monkeypatch):
    """`abiertos` sumaba dos cosas distintas y eso hizo tropezar a TRES agentes en
    un solo día (2026-08-13): yo conté 21, el cpo contó 23, y el cto comparó mi 21
    con su 23 y dedujo un crecimiento que no había ocurrido — los 23 claims de
    `contratos` son del 8, 9 y 10 de agosto, cero abiertos hoy.

    Tener 21 temas EN PROPIEDAD y ocupar 2 plazas de revisión no son la misma
    situación ni de lejos: la primera es acaparar trabajo, la segunda es hacer de
    revisor, que es exactamente lo que la casa quiere que pase. Un número que las
    suma invita al error, y lo invitó tres veces."""
    s = _montar(tmp_path, monkeypatch)
    c = _cliente(s)
    c.post("/claim", json={"tema": "propio", "agent": "backend"})
    c.post("/claim", json={"tema": "de_otro", "agent": "cpo"})
    c.post("/claim", json={"tema": "de_otro", "agent": "backend", "rol": "revisa"})
    d = c.get("/claims?agent=backend").json()
    assert d["ejecuta"] == 1, "no separa lo que tienes EN PROPIEDAD"
    assert d["revisa"] == 1, "no separa las plazas de REVISIÓN que ocupas"
    assert d["abiertos"] == 2, "el total deja de cuadrar con la suma"


def test_organigrama_dice_cuando_se_cargo(tmp_path, monkeypatch):
    """La jerarquía se lee UNA VEZ al arrancar el proceso. Sin esta marca, un agente
    que sospecha que el organigrama está desactualizado no tiene NINGÚN dato con que
    decidirlo — y la cicatriz de la plaza 15 es exactamente esa: firmar el censo no
    da de alta a nadie, porque el consumidor sigue con lo que cargó al arrancar."""
    s = _con_jerarquia(tmp_path, monkeypatch)
    c = _cliente(s)
    d = c.get("/organigrama").json()
    assert d.get("cargado_en"), "no dice cuándo se cargó — 'está al día' es indecidible"
    assert "T" in d["cargado_en"], "el sello no parece ISO"


def test_el_barrido_cierra_los_vencidos_sin_perder_historial(tmp_path, monkeypatch):
    """Un claim vencido que nadie reclama se quedaba «abierto» PARA SIEMPRE: hoy el
    flag `vencido` sólo se resuelve si OTRO agente pelea el mismo tema. Medido el
    2026-08-13: 69 abiertos, los 69 vencidos, entre 3,2 y 4,8 días, ninguno tocado.

    Daño real y medible, que es lo que justifica tocarlo: `tablero_abierto()` enseña
    los 12 últimos temas cogidos cuando vas a coger algo, y es la ÚNICA guarda que
    esta casa midió que funciona (el casi-choque del 08-11). Con 69 muertos creciendo,
    esos 12 son ruido muerto en vez de señal de choque.

    Se cierra en el BARRIDO, no en el GET. El CTO propuso hacerlo al leer; este
    código aprendió caro que un GET no escribe (una fila de telemetría en un GET
    tumbó 212 lecturas el 08-08). Mismo patrón que `vuelca_lecturas`.
    `abiertos` tiene que significar «alguien lo está trabajando AHORA», no «alguien
    lo tocó alguna vez» — criterio de producto del cpo, 2026-08-13.
    """
    s = _montar(tmp_path, monkeypatch, extra_env={"LLMINBOX_CLAIM_TTL_H": "0"})
    c = _cliente(s)
    c.post("/claim", json={"tema": "zombi", "agent": "backend"})
    assert c.get("/claims").json()["abiertos"] == 1
    s.barrido()
    d = c.get("/claims").json()
    assert d["abiertos"] == 0, "el vencido sigue contando como abierto tras el barrido"
    # El historial NO se pierde: se cierra con motivo, como el relevo.
    con = s.db()
    fila = con.execute("SELECT motivo, cerrado FROM claims WHERE tema='zombi'").fetchone()
    con.close()
    assert fila["cerrado"], "no lo cerró, lo borró o lo dejó"
    assert fila["motivo"] == "ttl_expirado", f"motivo sin declarar: {fila['motivo']}"


def test_lint_delata_si_la_siega_se_llevo_trabajo_VIVO(tmp_path, monkeypatch):
    """La señal que le faltaba a mi propia siega, y es el riesgo peor del día.

    `LLMINBOX_CLAIM_TTL_H=4` era un parámetro DORMIDO —sólo pintaba un flag— y el
    2026-08-13 lo hice PORTANTE: ahora cierra claims de verdad. Toda la evidencia
    de que 4 h basta son 5 claims cerrados por su dueño, el más largo de 0,44 h.
    N=5 no es una muestra, y no dejé forma de enterarme si empiezo a matar trabajo
    de alguien que está trabajando.

    La pista es barata y no necesita telemetría nueva: si el DUEÑO de un claim
    publicó en el ledger DESPUÉS de abrirlo y aun así se lo segué, ese claim no
    estaba muerto — estaba trabajando. `entries(actor, ts)` y `claims(agent,
    abierto, cerrado, motivo)` viven en la misma base.

    ⚠️ El cruce va por ROL, no por nombre: `claims.agent` guarda el rol (`be`) y
    `entries.actor` guarda la firma (`backend`). Comparar en crudo daría CERO
    siempre — un cero tranquilizador, que es la peor clase de fallo y hoy ya me
    ha mordido cuatro veces.
    """
    from datetime import datetime, timedelta, timezone
    s = _montar(tmp_path, monkeypatch)          # TTL por defecto: 4 h
    c = _cliente(s)
    # `backend` (rol `be`) coge un tema... y sigue publicando: está trabajando.
    c.post("/claim", json={"tema": "trabajo_que_sigue_vivo", "agent": "backend"})
    # Se ENVEJECE el claim en vez de trucar el reloj con TTL=0. Con TTL=0 la ventana
    # de detección —las TTL horas previas a la siega— tiene anchura CERO y no puede
    # contener nada: el test pasaría o fallaría por el arnés, no por la conducta.
    # Así se reproduce el caso real: un claim abierto hace 5 h con su dueño publicando
    # hace 1 h, o sea DENTRO de las 4 h previas a que se lo seguemos.
    ahora = datetime.now(timezone.utc)
    con = s.db()
    con.execute("UPDATE claims SET abierto=? WHERE tema='trabajo_que_sigue_vivo'",
                ((ahora - timedelta(hours=5)).isoformat(timespec="seconds"),))
    con.commit(); con.close()
    hace_una_hora = (ahora - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    (tmp_path / "DEMO-LEDGER.md").write_text(
        ENTRADAS + f"\n### [backend → cto-A · PRODUCED] {hace_una_hora}Z — sigo en ello\n"
        "publicado mientras el claim seguia abierto\n")
    s.barrido()          # indexa la entrada nueva y siega el claim ya vencido
    txt = c.get("/lint").text
    assert "SEGADO EN CALIENTE" in txt, (
        "/lint no delata que la siega se llevó un claim cuyo dueño seguía publicando")
    # Sobre TODA la cola, no sobre una ventana de N caracteres: la sección lleva
    # varias líneas declarando el límite del proxy ANTES de la lista de roles, y una
    # aserción recortada falla por la longitud de un aviso, no por la conducta.
    cola = txt.split("SEGADO EN CALIENTE")[-1]
    assert "be" in cola, "no dice a QUIÉN se lo hizo"


def test_lint_no_acusa_a_la_siega_cuando_sego_basura(tmp_path, monkeypatch):
    """FALSADOR: un claim cuyo dueño NO publicó nada después estaba muerto de
    verdad, y segarlo es el trabajo bien hecho. Si la señal salta también aquí,
    es una alarma que grita siempre — y una alarma que grita siempre se apaga."""
    s = _montar(tmp_path, monkeypatch, extra_env={"LLMINBOX_CLAIM_TTL_H": "0"})
    c = _cliente(s)
    c.post("/claim", json={"tema": "abandonado_de_verdad", "agent": "cpo"})
    s.barrido()
    txt = c.get("/lint").text
    cola = txt.split("SEGADO EN CALIENTE")[-1][:300] if "SEGADO EN CALIENTE" in txt else ""
    assert "cpo" not in cola, "acusa a la siega de matar trabajo que estaba muerto"


def test_el_barrido_no_toca_el_trabajo_vivo(tmp_path, monkeypatch):
    """FALSADOR: con el TTL por defecto, un claim recién cogido es trabajo VIVO y
    el barrido no puede tocarlo. Un reaper que se lleva por delante lo que alguien
    está haciendo es peor que los 69 zombis."""
    s = _montar(tmp_path, monkeypatch)
    c = _cliente(s)
    c.post("/claim", json={"tema": "trabajo_vivo", "agent": "backend"})
    s.barrido()
    assert c.get("/claims").json()["abiertos"] == 1, "el barrido mató trabajo vivo"


def test_claims_sin_filtro_sigue_devolviendo_todo(tmp_path, monkeypatch):
    """FALSADOR de ⑤: el parámetro es OPCIONAL. Si al añadirlo se colara un WHERE
    por defecto, el tablero global —que es lo que evita duplicar trabajo— se
    quedaría vacío para todos y nadie lo notaría hasta chocar."""
    s = _montar(tmp_path, monkeypatch)
    c = _cliente(s)
    c.post("/claim", json={"tema": "mio_uno", "agent": "backend"})
    c.post("/claim", json={"tema": "de_otro", "agent": "cpo"})
    assert c.get("/claims").json()["abiertos"] == 2, "sin filtro ya no se ve todo"


def test_claims_con_agente_desconocido_no_finge_bandeja_vacia(tmp_path, monkeypatch):
    """Un nombre que el censo no conoce tiene que DECIRLO, no devolver 0 abiertos.
    «no tienes nada cogido» y «te escribiste mal el nombre» son indistinguibles en
    una lista vacía, y el segundo caso te deja creyendo que estás libre. Misma
    doctrina que `/inbox` con un ledger inexistente: 422, no vacío mudo."""
    s = _montar(tmp_path, monkeypatch)
    c = _cliente(s)
    assert c.get("/claims?agent=securty").status_code == 422


# ── ③ la jerarquía, servida ──────────────────────────────────────────────────

JERARQUIA_FIXTURE = {
    "rol_por_alias": {"cpo": "cpo", "cto-a": "cto", "qa": "qa", "backend": "be"},
    "jerarquia": {
        "vision": {"reporta_a": "OPERADOR", "capa": "c-suite"},
        "cto": {"reporta_a": "vision", "capa": "c-suite"},
        "cpo": {"reporta_a": "vision", "capa": "c-suite"},
        "be": {"reporta_a": "cto", "capa": "ejecucion"},
        "qa": {"reporta_a": "cto", "capa": "gate", "gatea": ["*"],
               "criterios_de": "cpo"},
    },
}


def _con_jerarquia(tmp_path, monkeypatch):
    fich = tmp_path / "roles-por-alias.json"
    fich.write_text(json.dumps(JERARQUIA_FIXTURE))
    return _montar(tmp_path, monkeypatch,
                   extra_env={"LLMINBOX_ROLES_ALIAS": str(fich)})


def test_organigrama_dice_a_quien_reportas(tmp_path, monkeypatch):
    s = _con_jerarquia(tmp_path, monkeypatch)
    c = _cliente(s)
    d = c.get("/organigrama").json()
    assert d["jerarquia"]["be"]["reporta_a"] == "cto"
    assert d["jerarquia"]["cto"]["reporta_a"] == "vision", (
        "vision no está por encima del CTO — orden del operador 2026-08-13")


def test_organigrama_sin_fichero_montado_no_revienta(tmp_path, monkeypatch):
    """FALSADOR de ③: sin el fichero firmado montado, degrada — no 500, y lo
    DICE. Un organigrama vacío servido como si fuera el bueno es peor que no
    tenerlo: el agente concluiría que no reporta a nadie."""
    s = _montar(tmp_path, monkeypatch)
    c = _cliente(s)
    r = c.get("/organigrama")
    assert r.status_code == 200
    d = r.json()
    assert d["jerarquia"] == {}
    assert d.get("aviso"), "degradó en silencio: sin jerarquía y sin decirlo"
