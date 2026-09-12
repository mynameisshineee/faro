"""Lo que una reconstrucción de índice NO puede llevarse por delante.

El 2026-08-15T22:47:55 el índice se corrompió (`quick_check: wrong # of entries in
index i_who`), el servicio se curó como debía —esa cura es una propiedad del producto,
con su prueba en el humo— y en el viaje se llevó **96 claims, 70 de ellos abiertos**:
el estado de reparto de trabajo de los 15 agentes. `claims` nació DESPUÉS de
`_rescatar()` y nadie la añadió a su lista.

Y el remate: `/doctor ③` publicó la pérdida como **«0 sin cerrar ni relevar»**, que es
la mejor nota posible. Una pérdida de datos con cara de disciplina perfecta.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3

from fastapi.testclient import TestClient

from .conftest import construir


def test_rescate_cubre_lo_no_derivable(servicio):
    """EL GUARDA ESTRUCTURAL, y es el que de verdad arregla esto: la lista de rescate
    se compara contra el ESQUEMA. Toda tabla que no se re-derive tiene que estar
    rescatada; si alguien añade una tabla de estado nueva y no decide qué pasa con
    ella, esto se pone rojo con su nombre.

    FALSADOR: quitar `claims` de `TABLAS_RESCATADAS` tiene que romper este test —es
    exactamente el estado en que el repo estuvo desde que se creó la tabla hasta el
    incidente.
    """
    del_esquema = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", servicio.SCHEMA))
    rescatadas = {t for t, _ in servicio.TABLAS_RESCATADAS}
    huerfanas = del_esquema - set(servicio.DERIVADAS) - rescatadas
    assert not huerfanas, (
        f"tablas que ni se re-derivan ni se rescatan: {sorted(huerfanas)} — "
        "decide: ¿sale del markdown, o se pierde en la próxima corrupción?")


def test_las_columnas_declaradas_existen_de_verdad(servicio):
    """La lista nombra columnas, y una columna mal escrita convierte el rescate en un
    `OperationalError` que se traga el `except` de al lado: el rescate 'funciona' y no
    trae nada. Se comprueba contra el esquema real, no contra la memoria de quien lo
    escribió."""
    # Contra el esquema COMPLETO (SCHEMA + ALTERs), no contra `SCHEMA` a secas: dos de
    # las columnas rescatadas se añaden por ALTER y comparar sólo con el CREATE TABLE
    # las daba por inexistentes — un rojo que acusaba a la lista teniendo razón ella.
    cols = _esquema_completo(servicio)
    for tabla, declaradas in servicio.TABLAS_RESCATADAS:
        pedidas = set(declaradas.split(","))
        reales = cols.get(tabla, set())
        assert pedidas <= reales, f"{tabla}: no existen {sorted(pedidas - reales)}"


def test_los_claims_sobreviven_a_una_reconstruccion(cliente, servicio):
    """El caso REAL, extremo a extremo: se coge un claim, se reconstruye el índice como
    lo hace la cura de corrupción, y el claim tiene que seguir ahí.

    FALSADOR: sin `claims` en la lista de rescate, la base nueva sale con la tabla
    vacía y esto da 0 — que es literalmente lo que pasó en producción.
    """
    cliente.post("/claim", json={"tema": "no_me_pierdas", "rol": "ejecuta",
                                 "agent": "backend"})
    con = servicio.db()
    antes = con.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
    con.close()
    assert antes == 1, "precondición: el claim tiene que existir antes de reconstruir"

    assert servicio.reconstruir_indice("prueba de rescate") is True

    con = servicio.db()
    fila = con.execute("SELECT tema, rol, agent FROM claims").fetchall()
    con.close()
    assert [tuple(r) for r in fila] == [("no_me_pierdas", "ejecuta", "be")], fila


def test_ack_grant_y_cursor_generation_sobreviven_a_una_reconstruccion(tmp_path, monkeypatch):
    """El mismo incidente que `claims`, para el ACK causal (P1, @security,
    MARK:security-fleet-lane-cursor-hardening-tres-p1-confirmados-static-only,
    2026-09-07T15:58:18Z; cerrado por MARK:security-p1-1-no-cierra-sin-clave-de-
    reconciliacion-mismo-defecto). `ack_grants`/`cursor_generations` estaban en
    `TABLAS_RESCATADAS` sin clave en `CLAVE_RECONCILIACION`: `_reconciliar` caía en
    `tarde or pronto`, y como ambas tablas escriben en CADA ack con éxito (caso
    MODAL, no ventana de carrera), la foto temprana se descartaba entera —
    exactamente igual que `claims` antes de este arreglo.

    FALSADOR: sin las dos entradas nuevas en `CLAVE_RECONCILIACION`, un grant SIN
    USAR y una `cursor_generations` recién escrita, emitidos antes de
    `reconstruir_indice`, desaparecen — es el caso extremo a extremo que
    `test_rescate_cubre_lo_no_derivable` (sólo mira la LISTA, no la supervivencia)
    no puede cazar.

    El estado previo se levanta por el camino PÚBLICO autorizado GET→ACK bajo V8,
    comprobando cada respuesta: el primer run de sdet
    (MARK:sdet-ack-266941a-medido-un-fallo-noventaynueve-pasan, 2026-09-08) midió
    que el legacy `/leido` con `test-token` NO acredita generación bajo V8 — la
    precondición nacía vacía; no era el rescate quien la perdía. La semilla trae
    2 entradas, así que el primer ACK es PARCIAL (arrival=min, el camino que
    `test_ack_grants.py` ya acredita): deja la segunda pendiente y permite emitir
    el grant nuevo SIN USAR. Tras el rescate, el grant USADO se re-verifica por
    replay: mismo recibo — la fila con su receipt_json sobrevive a la foto.

    MEDIDO (sdet, primer run): 99 passed, 1 failed EN LA PRECONDICIÓN del setup
    legacy, antes de llegar al rescate. Esta versión vuelve a la cola de sdet:
    quien la corra cita comando, rc y SHA.
    """
    mapa = tmp_path / "ack-credentials.json"
    mapa.write_text(json.dumps({
        "cred-be-rescate-0000000": {"rol": "be", "carril": "demo",
                                     "principal_id": "workload-be-rescate"},
    }))
    s = construir(tmp_path, monkeypatch, extra_env={
        "LLMINBOX_CREDENCIALES": str(mapa),
        "LLMINBOX_CREDENCIALES_SHA": hashlib.sha256(mapa.read_bytes()).hexdigest(),
    })
    h = {"X-Llminbox-Token": "cred-be-rescate-0000000", "X-Llminbox-Carril": "demo"}

    def _sobre_grant(c) -> dict:
        r = c.get("/inbox/backend", params={"only": "demo-ledger"}, headers=h)
        assert r.status_code == 200, r.text
        g = json.loads([x.strip() for x in r.text.splitlines()
                        if x.strip().startswith('{"hasta"')][-1])["ack"]
        assert g, f"sin grant en el sobre: {r.text[-300:]}"
        return g

    def _ack(c, grant: dict, arrival: int) -> dict:
        r = c.post("/inbox/backend/ack", json={"grant": grant, "arrival": arrival},
                   headers=h)
        assert r.status_code == 200, r.text
        return r.json()

    with TestClient(s.app) as c:
        s.barrido()

        # ① cursor + generación por el flujo público: ACK PARCIAL sobre la primera
        # de las 2 entradas de la semilla, para que quede PENDIENTE una segunda.
        g1 = _sobre_grant(c)
        recibo = _ack(c, g1, min(g1["allowed_arrivals"]))
        assert recibo["cursor_generation"] == g1["cursor_generation"] + 1
        assert recibo["before"] == g1["cursor_before"]
        assert recibo["after"] == min(g1["allowed_arrivals"])

        # ② el grant nuevo SIN USAR: cubre la entrada que quedó pendiente, con la
        # generación sellada por el ACK de ①.
        g2 = _sobre_grant(c)
        assert g2["cursor_generation"] == recibo["cursor_generation"]
        assert g2["cursor_before"] == recibo["after"]

        con = sqlite3.connect(s.DB)
        antes_sin_usar = con.execute(
            "SELECT COUNT(*) FROM ack_grants WHERE used_arrival IS NULL").fetchone()[0]
        antes_gen = con.execute(
            "SELECT generation FROM cursor_generations_v2 "
            "WHERE role=? AND carril='demo' AND ledger=?",
            ("be", "demo-ledger")).fetchone()
        con.close()
        assert antes_sin_usar == 1, "precondición: el grant sin usar tiene que existir antes de reconstruir"
        assert antes_gen is not None, "precondición: la generación tiene que existir antes de reconstruir"
        assert antes_gen[0] == recibo["cursor_generation"], \
            "precondición: la fila de generación es la que selló el ACK"

        assert s.reconstruir_indice("prueba de rescate ack") is True

        con = sqlite3.connect(s.DB)
        despues_sin_usar = con.execute(
            "SELECT COUNT(*) FROM ack_grants WHERE nonce_hash=? AND used_arrival IS NULL",
            (hashlib.sha256(g2["grant"].encode("ascii")).hexdigest(),)
        ).fetchone()[0]
        despues_gen = con.execute(
            "SELECT generation FROM cursor_generations_v2 "
            "WHERE role=? AND carril='demo' AND ledger=?",
            ("be", "demo-ledger")).fetchone()
        con.close()
        assert despues_sin_usar == 1, "el grant sin usar desaparece en la reconstrucción"
        assert despues_gen == antes_gen, "la generación de fencing se pierde en la reconstrucción"

        # ③ replay del grant USADO tras el rescate: el MISMO recibo, sin mutar nada.
        # El camino idempotente va ANTES del chequeo de staleness, pero exige que la
        # fila del grant con su receipt_json sobreviva a la reconstrucción.
        assert _ack(c, g1, min(g1["allowed_arrivals"])) == recibo


# ── el mismo guarda, una capa más abajo: COLUMNAS ────────────────────────────

# Columnas que a propósito NO se rescatan, cada una con su motivo. Es una lista de
# EXCEPCIONES declaradas, no un cajón: quien añada una columna nueva tiene que
# rescatarla o justificarla aquí, y eso es justo lo que se quiere que cueste.
NO_SE_RESCATAN = {
    ("incidencias", "id"),   # AUTOINCREMENT: lo pone la base nueva, copiarlo no aporta
    # `entries` es DERIVADA (ver `servicio.DERIVADAS`): se re-deriva entera del
    # markdown, así que `raw_tipo` se vuelve a calcular y rescatarlo no aporta.
    ("entries", "raw_tipo"),
    # Estas dos se re-derivan de `raw_tipo` mediante el registro APPEND-ONLY: cada
    # lexema conserva su primera revisión para siempre y una revisión posterior no
    # puede redefinirlo. La pareja es interpretación versionada, pero determinista;
    # el falsador completo vive en `test_message_policy.py`.
    ("entries", "canonical_kind"),
    ("entries", "kind_registry_rev"),
}


def _esquema_completo(servicio):
    """El esquema tal y como existe EN PRODUCCIÓN: `SCHEMA` más los ALTERs del
    arranque. Comparar sólo contra `SCHEMA` mira a un esquema que no existe en
    ninguna máquina — `claims.motivo` y `claims.cerrado_por` se añaden por ALTER, así
    que una comprobación ingenua no las vería y daría verde justo sobre el hueco."""
    import sqlite3
    con = sqlite3.connect(":memory:")
    con.executescript(servicio.SCHEMA)
    for tabla, col, tipo in servicio.COLUMNAS_ANADIDAS:
        # Con su `except`, igual que producción: una columna puede estar en el CREATE
        # TABLE **y** en la lista de ALTERs a la vez (`coste.maximo` lo está: en el
        # esquema para las bases nuevas, en el ALTER para las que se crearon antes).
        # Un ayudante de test que no tolere lo que el servicio tolera mide un esquema
        # que no existe, y falla por su propia rigidez.
        try:
            con.execute(f"ALTER TABLE {tabla} ADD COLUMN {col} {tipo}")
        except sqlite3.OperationalError:
            pass
    cols = {t: {r[1] for r in con.execute(f"PRAGMA table_info({t})")}
            for t in re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", servicio.SCHEMA)}
    con.close()
    return cols


def test_rescate_no_se_deja_columnas(servicio):
    """El guarda de tablas comparaba TABLAS y por eso esto le pasaba por debajo:
    `coste` estaba rescatada y su columna `maximo` volvía a 0 en cada cura; `claims`
    perdía `motivo` y `cerrado_por` — las dos que existen para distinguir «lo cerró su
    dueño» de «se lo relevaron». El dato que mide la disciplina, borrado por la cura.

    FALSADOR: quitar `maximo` de la lista de `coste` tiene que poner esto rojo con su
    nombre. Es el estado exacto en que estuvo el repo hasta hoy.
    """
    cols = _esquema_completo(servicio)
    faltan = {}
    for tabla, declaradas in servicio.TABLAS_RESCATADAS:
        reales = cols.get(tabla, set())
        sueltas = reales - set(declaradas.split(",")) - {c for t, c in NO_SE_RESCATAN if t == tabla}
        if sueltas:
            faltan[tabla] = sorted(sueltas)
    assert not faltan, (
        f"columnas que existen y no se rescatan: {faltan} — se pierden en la próxima "
        "reconstrucción. Añádelas a TABLAS_RESCATADAS o decláralas en NO_SE_RESCATAN.")


def test_las_columnas_del_alter_tambien_se_rescatan(servicio):
    """CONTROL DIRIGIDO al caso que se escapó: las columnas que NO están en el
    `CREATE TABLE` sino en un `ALTER` posterior son las más fáciles de olvidar —no se
    ven leyendo el esquema— y son, por definición, las más nuevas."""
    for tabla, col, _ in servicio.COLUMNAS_ANADIDAS:
        if tabla in servicio.DERIVADAS:
            # Se re-deriva del markdown; no hay estado que rescatar. La excepción
            # se ancla a `DERIVADAS`, que ya declara qué se reconstruye, en vez de
            # a una lista nueva que se quedaría vieja por su cuenta.
            assert (tabla, col) in NO_SE_RESCATAN, (
                f"{tabla}.{col} es de una tabla derivada pero nadie lo ha declarado: "
                "decláralo en NO_SE_RESCATAN con el motivo, o rescátalo")
            continue
        declaradas = dict((t, c) for t, c in servicio.TABLAS_RESCATADAS).get(tabla)
        assert declaradas is not None, f"{tabla} no se rescata en absoluto"
        assert col in declaradas.split(","), f"{tabla}.{col} se añade por ALTER y no se rescata"


# ── la ventana entre las dos fotos ───────────────────────────────────────────

def test_reconciliar_une_y_gana_la_foto_tardia(servicio):
    """`_reconciliar` no es un `or` ni un reemplazo, y cada mitad importa:
    · lo que sólo está en la PRIMERA se conserva (si la lectura tardía falla o vuelve
      corta porque la corrupción avanzó, quedarse sólo con ella pierde filas);
    · lo que sólo está en la SEGUNDA entra (es el correo de la ventana);
    · y en el empate gana la tardía, que es la más nueva por construcción.
    """
    r = servicio._reconciliar
    pronto = [("a", "ejecuta", "qa", None, "T1", None, "a", None, None)]
    tarde = [("a", "ejecuta", "qa", None, "T1", "CERRADO", "a", "cierro", "qa"),
             ("b", "ejecuta", "be", None, "T2", None, "b", None, None)]
    out = {f[0]: f for f in r("claims", pronto, tarde)}
    assert out["a"][5] == "CERRADO", "el empate lo gana la foto tardía"
    assert "b" in out, "lo que sólo está en la tardía tiene que entrar"

    solo_pronto = r("claims", pronto, [])
    assert solo_pronto == pronto, "si la lectura tardía no trae nada, no se pierde lo que había"

    # Y una tabla sin clave declarada no puede reventar: degrada a «la que haya».
    assert r("desconocida", [("x",)], []) == [("x",)]


def test_un_claim_de_la_VENTANA_no_lo_pisa_la_foto_vieja(cliente, servicio, monkeypatch):
    """El caso que reportó CodeRabbit, montado: entre la primera foto y el cambio de
    base pasan los segundos de re-derivar el markdown, con el servicio VIVO. Un
    `/claim` que aterrice ahí lo pisaba la foto vieja y desaparecía.

    Se simula haciendo que la SEGUNDA llamada a `_rescatar` traiga un claim que la
    primera no tenía — que es exactamente lo que pasa cuando alguien escribe en medio.

    FALSADOR: con el volcado en su sitio viejo (antes de la segunda foto) este claim
    no aparece, porque sólo se escribía `rescatado`.
    """
    cliente.post("/claim", json={"tema": "el_de_antes", "rol": "ejecuta", "agent": "backend"})
    real = servicio._rescatar
    llamadas = {"n": 0}

    def espia(ruta):
        out = real(ruta)
        llamadas["n"] += 1
        if llamadas["n"] == 2:          # la foto TARDÍA: alguien escribió en la ventana
            out["claims"] = list(out["claims"]) + [
                ("en_la_ventana", "ejecuta", "cto", None, "2026-08-18T10:00:00+00:00",
                 None, "en_la_ventana", None, None)]
        return out

    monkeypatch.setattr(servicio, "_rescatar", espia)
    assert servicio.reconstruir_indice("prueba de ventana") is True
    assert llamadas["n"] >= 2, "la reconstrucción tiene que tomar DOS fotos"

    con = servicio.db()
    temas = {r[0] for r in con.execute("SELECT tema FROM claims")}
    con.close()
    assert "en_la_ventana" in temas, "el claim de la ventana se ha perdido"
    assert "el_de_antes" in temas, "y el anterior no puede desaparecer por rescatar el nuevo"
