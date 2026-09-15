"""M1-5: generación del mapa, base ajena, lecturas acotadas y oráculos.

Varios de estos miden BYTES y sidecars, no sólo excepciones: «no la toqué» es
una afirmación sobre el fichero, y se comprueba mirando el fichero.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys

import pytest

import coordination as C
from ._arnes import GRAMATICA, INTENT, LANES, OPERADOR, PEPPER, Reloj, censo, journal, sesion, sesiones


def _huella(ruta):
    """Conjunto, tamaño y sha256 de la base y sus sidecars."""
    out = {}
    for suf in ("", "-wal", "-shm"):
        p = ruta + suf
        if os.path.exists(p):
            with open(p, "rb") as fh:
                b = fh.read()
            out[suf or "db"] = (len(b), hashlib.sha256(b).hexdigest())
    return out


# ── ① GENERACIÓN DEL MAPA ───────────────────────────────────────────────────

def test_una_recarga_IDENTICA_es_un_no_op_exacto(tmp_path):
    """Recargar el mismo mapa es lo que hace un supervisor al reiniciar. Subir la
    generación de oficio tumbaba todas las sesiones vivas por comprobar."""
    j = journal(tmp_path)
    mapa = {"cred-A": {"principal": "backend", "role": "be", "lane": "llminbox",
                       "capabilities": ["outbox_operator"]}}
    j.reload_credential_map(mapa)
    s = j.open_session("cred-A")
    gen = j.generation()
    assert j.reload_credential_map(dict(mapa)) == gen      # ni un escalón
    assert j.generation() == gen
    assert j.authenticate(s.token) is not None, "la recarga idéntica mató la sesión"
    # …y el orden de las capacidades tampoco cuenta como cambio.
    mapa2 = {"cred-A": {"principal": "backend", "role": "be", "lane": "llminbox",
                        "capabilities": ["outbox_operator", "outbox_operator"]}}
    assert j.reload_credential_map(mapa2) == gen
    assert j.authenticate(s.token) is not None
    j.close()


@pytest.mark.parametrize("cambio", [
    {"principal": "otro", "role": "be", "lane": "llminbox",
     "capabilities": ["outbox_operator"]},
    {"principal": "backend", "role": "cto", "lane": "llminbox",
     "capabilities": ["outbox_operator"]},
    {"principal": "backend", "role": "be", "lane": "carril-uno",
     "capabilities": ["outbox_operator"]},
    {"principal": "backend", "role": "be", "lane": "llminbox",
     "capabilities": []},                       # SÓLO cambian las capacidades
])
def test_un_cambio_efectivo_sube_la_generacion_EXACTAMENTE_una_vez(tmp_path, cambio):
    """Incluido el cambio que sólo toca capacidades: sin compararlas, quitarle a
    alguien el permiso de operador se leía como «no ha cambiado nada» — la
    retirada más importante era justo la invisible."""
    j = journal(tmp_path)
    j.reload_credential_map({"cred-A": {"principal": "backend", "role": "be",
                                        "lane": "llminbox",
                                        "capabilities": ["outbox_operator"]}})
    s = j.open_session("cred-A")
    gen = j.generation()
    assert j.reload_credential_map({"cred-A": cambio}) == gen + 1
    assert j.generation() == gen + 1, "subió más de un escalón"
    assert j.authenticate(s.token) is None
    fila = j._connect().execute(
        "SELECT revoke_reason FROM runtime_sessions WHERE runtime_instance=?",
        (s.runtime_instance,)).fetchone()
    assert fila["revoke_reason"] == "binding_retired"
    j.close()


def test_bind_credential_efectivo_tambien_sube_la_generacion_una_vez(tmp_path):
    """Dos puertas al mismo sitio no pueden tener reglas distintas: por esta vía
    quedaban sesiones vivas contra un mapa que ya no era el vigente."""
    j = journal(tmp_path)
    j.bind_credential("cred-A", principal="uno", role="be", lane="llminbox")
    gen = j.generation()
    j.bind_credential("cred-A", principal="uno", role="be", lane="llminbox")
    assert j.generation() == gen, "una ligadura IDÉNTICA subió la generación"
    j.bind_credential("cred-A", principal="dos", role="be", lane="llminbox")
    assert j.generation() == gen + 1
    j.close()


# ── ② BASE AJENA O PARCIAL: NI UN BYTE ─────────────────────────────────────

def test_una_base_AJENA_no_se_toca_ni_en_formato_ni_en_bytes(tmp_path):
    """`SchemaIndeterminate` llegaba DESPUÉS de que `journal_mode=WAL` hubiera
    reescrito el fichero de otro. Rechazar después de tocar no es rechazar."""
    ruta = str(tmp_path / "ajena.sqlite")
    con = sqlite3.connect(ruta)
    con.execute("CREATE TABLE cosas_de_otro (x INTEGER)")
    con.execute("INSERT INTO cosas_de_otro VALUES (1)")
    con.commit(); con.close()
    antes = _huella(ruta)
    assert set(antes) == {"db"}                     # ⊕ sin sidecars al empezar

    j = C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
    with pytest.raises(C.SchemaIndeterminate):
        j.initialize()
    j.close()
    assert _huella(ruta) == antes, "le cambió el fichero a una base que no es suya"


def test_una_base_PARCIAL_nuestra_sin_sello_tampoco_se_sella(tmp_path):
    ruta = str(tmp_path / "coordination.sqlite")
    j = journal(tmp_path)
    s = sesion(j)
    j.accept_event(s.token, idempotency_key="k", intent=INTENT, ledger="llminbox")
    j.close()
    con = sqlite3.connect(ruta)
    con.execute("DELETE FROM meta WHERE k='durable_v'")
    con.commit()
    # ⚠️ SIN esto el control NO discrimina: la base ya está en WAL, así que
    # volver a aplicarlo no cambia un byte y el test pasaba aunque el código
    # tocara el fichero. Dejarla en `delete` es lo que hace visible el toque.
    con.execute("PRAGMA journal_mode=DELETE")
    con.close()
    antes = _huella(ruta)
    assert set(antes) == {"db"}

    otro = C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
    with pytest.raises(C.SchemaIndeterminate):
        otro.initialize()
    otro.close()
    assert _huella(ruta) == antes


def test_una_base_FUTURA_conserva_bytes_y_conjunto_de_sidecars(tmp_path):
    ruta = str(tmp_path / "coordination.sqlite")
    j = journal(tmp_path)
    j.close()
    con = sqlite3.connect(ruta)
    con.execute("UPDATE meta SET v=? WHERE k='durable_v'", (str(C.DURABLE_V + 1),))
    con.commit()
    con.execute("PRAGMA journal_mode=DELETE")
    con.close()
    antes = _huella(ruta)
    assert set(antes) == {"db"}

    futura = C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
    with pytest.raises(C.SchemaTooNew):
        futura.initialize()
    for i in range(10):
        futura.record_unknown_credential(f"x-{i}")
    futura.close()
    assert _huella(ruta) == antes


def test_un_MUTADOR_decorado_con_token_desconocido_no_escribe_en_base_futura(tmp_path):
    """El decorador `@_audita` intenta registrar el rechazo. Sobre una base
    futura, ESE registro sería la escritura que decimos no hacer."""
    ruta = str(tmp_path / "coordination.sqlite")
    j = journal(tmp_path)
    j.close()
    con = sqlite3.connect(ruta)
    con.execute("UPDATE meta SET v=? WHERE k='durable_v'", (str(C.DURABLE_V + 1),))
    con.commit()
    con.execute("PRAGMA journal_mode=DELETE")
    con.close()
    antes = _huella(ruta)

    futura = C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
    for verbo in (lambda: futura.accept_event("tok-desconocido", idempotency_key="k",
                                              intent=INTENT, ledger="llminbox"),
                  lambda: futura.acquire_lease("tok-desconocido", "r"),
                  lambda: futura.submit_command("tok-desconocido", workstream_id="w",
                                                revision=1, payload={})):
        with pytest.raises(C.JournalError):
            verbo()
    # `record_rejection` con token desconocido NO lanza: su trabajo es registrar,
    # y sin sujeto no hay nada que registrar. Devuelve `None` — lo que se le
    # exige aquí no es el error, es que tampoco escriba.
    assert futura.record_rejection("tok-desconocido", "POLICY_DENIED") is None
    futura.close()
    assert _huella(ruta) == antes, "el auditor escribió en una base que no entiende"


# ── ② CAPACIDAD ≠ ROL ──────────────────────────────────────────────────────

def test_el_rol_operador_SIN_capacidad_no_opera(tmp_path):
    """La capacidad no se deduce del rol: un rol es una etiqueta que se repite
    entre credenciales, y deducirla haría operador a todo el que la lleve."""
    reloj = Reloj()
    j = journal(tmp_path, reloj=reloj, max_attempts=1)
    j.bind_credential("cred-op", principal="op", role="operator", lane="llminbox")
    s = j.open_session("cred-op", ttl_s=10 ** 6)
    a = j.accept_event(s.token, idempotency_key="k", intent=INTENT, ledger="llminbox")
    reloj.avanza(1000)
    with pytest.raises(C.PolicyDenied):
        j.claim_outbox(s.token, lease_s=60)
    fila = j._connect().execute(
        "SELECT state, claim_token FROM outbox WHERE event_id=?", (a.event_id,)
    ).fetchone()
    assert tuple(fila) == ("pending", None), (
        "el rol sin capacidad reclamó o alteró el trabajo")
    j.close()


# ── ③ LECTURAS: SQL ACOTADO Y SIN ORÁCULO ──────────────────────────────────

def test_el_carril_va_en_el_SQL_no_en_un_if_de_python(tmp_path):
    """Se mide por EFECTO: con el filtro en el `WHERE`, la consulta que sirve al
    ajeno no devuelve fila; con el filtro en Python, la fila ya se leyó."""
    j = journal(tmp_path)
    a, b = sesiones(j,
        {"credential": "cred-A", "principal": "p-a", "lane": "carril-uno"},
        {"credential": "cred-B", "principal": "p-b", "lane": "carril-dos"})
    ev = j.accept_event(a.token, idempotency_key="k", intent=INTENT,
                        ledger="ledger-uno")
    rid = j.receipt_for_event(a.token, ev.event_id)["receipt_id"]
    # Mismo error y mismo tipo para «de otro carril» y «no existe».
    err_ajeno = err_falso = None
    try:
        j.receipt(b.token, rid)
    except C.JournalError as e:
        err_ajeno = (type(e), str(e))
    try:
        j.receipt(b.token, "rcp_no_existe")
    except C.JournalError as e:
        err_falso = (type(e), str(e))
    assert err_ajeno == err_falso, "el mensaje distingue lo ajeno de lo inexistente"
    j.close()


def test_una_revocacion_a_mitad_no_deja_leer_el_recibo(tmp_path):
    """Auth y lectura en la MISMA transacción: entre un `SELECT` de sesión y otro
    de recibo cabe una revocación entera."""
    j = journal(tmp_path)
    s = sesion(j)
    ev = j.accept_event(s.token, idempotency_key="k", intent=INTENT,
                        ledger="llminbox")
    j.revoke_current(s.token, "prueba")
    with pytest.raises(C.AuthError):
        j.receipt_for_event(s.token, ev.event_id)
    j.close()


# ── ④ SIN ORÁCULO PARA EL DESCONOCIDO ──────────────────────────────────────

def test_un_token_desconocido_no_aprende_si_su_causa_estaba_mal(tmp_path):
    """Con la forma validada antes, el desconocido distinguía «mi causa está mal»
    de «mi token no vale»: un oráculo del contrato gratis."""
    j = journal(tmp_path)
    sesion(j)
    errores = set()
    for causas in ([{"ledger": "l", "entry_eid": "e"}], [{}], ["no-es-dict"]):
        try:
            j.accept_event("desconocido", idempotency_key="k", intent=INTENT,
                           ledger="llminbox", external_causes=causas)
        except C.JournalError as e:
            errores.add((type(e).__name__, str(e)))
    assert len(errores) == 1, f"tres respuestas distintas: {errores}"
    assert errores.pop()[0] == "AuthError"
    j.close()


# ── ④ bis: EL PAR DE FENCING NO ADMITE UN BOOL ────────────────────────────

def test_un_fencing_token_booleano_no_se_cuela_por_ser_un_int(tmp_path):
    """`True` es un `int` en Python, así que `fencing_token=True` pasaba la
    comprobación del par y se comparaba como `1` contra un token real."""
    j = journal(tmp_path)
    s = sesion(j)
    lease = j.acquire_lease(s.token, "r", ttl_s=600)
    assert lease.fencing_token == 1                 # ⊕ el escenario es el peor
    with pytest.raises(C.JournalError):
        j.accept_event(s.token, idempotency_key="k", intent=INTENT,
                       ledger="llminbox", fenced_resource="r", fencing_token=True)
    assert j._connect().execute("SELECT COUNT(*) c FROM events").fetchone()["c"] == 0
    j.close()


# ── ④ ter: MIGRACIÓN, LOS TRES DESAJUSTES POR SEPARADO ────────────────────

@pytest.mark.parametrize("campo", ["principal", "lane"])
def test_la_migracion_para_ante_CADA_desajuste_del_runtime(tmp_path, campo):
    from .test_migracion_v1_a_v2 import _fixture_v1
    ruta, b, s1, s2, ev, _ = _fixture_v1(tmp_path, "B")
    con = sqlite3.connect(ruta)
    con.execute("PRAGMA foreign_keys=OFF")
    if campo == "principal":
        con.execute("INSERT INTO principals(principal_id,principal,role,lane,"
                    "created_at) VALUES('prn_otro','otro','be','llminbox','t')")
        con.execute("UPDATE commands SET principal_id='prn_otro'")
    else:
        con.execute("UPDATE commands SET lane='carril-dos'")
    con.commit(); con.close()

    j = C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
    with pytest.raises(C.MigrationFailed):
        j.initialize()
    assert j.stored_durable_v() == 1
    j.close()


def test_un_desajuste_de_ROL_TAMBIEN_para_la_migracion(tmp_path):
    """🔻 SUPERA mi posición anterior, que era una declaración sin medir.

    En M1-5 escribí que un rol distinto no paraba la migración «porque el rol se
    deriva del principal y no hay dos fuentes que comparar». La auditoría de M1-6
    pidió rechazar por `principal`, `role` y `lane` POR SEPARADO, y tiene razón:
    en el esquema c51 `commands.role` es una columna propia, así que sí hay dos
    fuentes —la columna y la sesión— y pueden contradecirse. Que yo no supiera
    construir el caso no era lo mismo que el caso no existir.
    """
    from .test_migracion_v1_a_v2 import _fixture_v1
    ruta, b, s1, s2, ev, _ = _fixture_v1(tmp_path, "B")
    con = sqlite3.connect(ruta)
    con.execute("UPDATE commands SET role='cto'")     # rol distinto del de la sesión
    con.commit(); con.close()
    j = C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
    with pytest.raises(C.MigrationFailed) as exc:
        j.initialize()
    # El contrato beta rechaza cualquier v1 antes de inspeccionar sus campos;
    # no debe prometer un diagnóstico de `role` que el migrador retirado ya no
    # puede alcanzar.
    assert "durable_v=1" in str(exc.value)
    assert "migrador offline" in str(exc.value)
    assert j.stored_durable_v() == 1
    j.close()


# ── DB v2 GENERADA POR EL BINARIO REAL ─────────────────────────────────────

def test_una_v2_parida_por_bcd05c5_rechaza_sin_tocar(tmp_path):
    """La única prueba que NO depende de mi reconstrucción del esquema v2.

    Se pide el `coordination.py` de aquel commit y se le hace CREAR la base. Si
    el árbol no tiene `.git` —tarball, export— se salta con motivo: el fixture
    escrito a mano sigue cubriendo el caso, y lo que aquí se gana es que la forma
    v2 la fije el binario y no yo.
    """
    try:
        fuente = subprocess.run(["git", "show", "bcd05c5:coordination.py"],
                                cwd=os.path.dirname(os.path.dirname(
                                    os.path.dirname(os.path.abspath(__file__)))),
                                capture_output=True, text=True, timeout=30)
        if fuente.returncode != 0 or not fuente.stdout:
            pytest.skip("sin `.git`: el fixture a mano cubre este caso")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pytest.skip("sin `git` disponible")

    mod = tmp_path / "bcd_coordination.py"
    mod.write_text(fuente.stdout)
    sys.path.insert(0, str(tmp_path))
    try:
        import importlib
        viejo = importlib.import_module("bcd_coordination")
        assert viejo.DURABLE_V == 2, f"bcd05c5 no era v2 sino v{viejo.DURABLE_V}"
        ruta = str(tmp_path / "coordination.sqlite")
        vj = viejo.Journal(ruta, pepper=PEPPER,
                           lane_ledgers={"llminbox": ["llminbox"]})
        vj.initialize()
        vj.bind_credential("cred-A", principal="backend", role="be", lane="llminbox")
        s = vj.open_session("cred-A", ttl_s=10 ** 6)
        a = vj.accept_event(s.token, idempotency_key="k",
                            intent={"type": "message", "verb": "inform", "kind": "FYI",
                                    "to": ["be"], "head": "h", "body": "cuerpo"})
        cid, _ = vj.submit_command(s.token, workstream_id="ws", revision=1,
                                   payload={"x": 1})
        vj.close()
    finally:
        sys.path.remove(str(tmp_path))

    nueva = C.Journal(ruta, pepper=PEPPER, lane_ledgers={"llminbox": ["llminbox"]}, recipient_resolver=censo, grammar=GRAMATICA)
    assert nueva.stored_durable_v() == 2
    antes = _huella(ruta)
    with pytest.raises(C.MigrationFailed) as exc:
        nueva.initialize()
    assert "durable_v=2" in str(exc.value)
    assert "migrador offline" in str(exc.value)
    assert _huella(ruta) == antes
    nueva.close()


def test_una_revocacion_ENTRE_la_auth_y_la_consulta_no_parte_la_lectura(tmp_path):
    """El TOCTOU de lectura, con el seam disparando en medio.

    Con auth y consulta en la MISMA transacción, la lectura sirve la instantánea
    coherente con la sesión que autorizó. Sin ella, se autentica contra un estado
    y se leen datos de otro: una vista partida que nadie puede reproducir después
    para comprobar qué se sirvió.
    """
    j = journal(tmp_path)
    s = sesion(j)
    ev = j.accept_event(s.token, idempotency_key="k", intent=INTENT,
                        ledger="llminbox")
    rid = j.receipt_for_event(s.token, ev.event_id)["receipt_id"]
    n_antes = len(j.transitions(s.token, rid))

    # La segunda instancia se acredita ANTES de que la lectora tome el lock
    # compartido de lifecycle. Inicializarla dentro del hook pediría un lock
    # exclusivo mientras esta misma hebra conserva el compartido: mediría un
    # auto-deadlock del arnés, no el TOCTOU de auth/lectura.
    otro = C.Journal(j.path, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
    otro.initialize()

    def sabotaje():
        try:
            otro.revoke_current(s.token, "carrera")
            otro._append_transition(rid, "materialization_failed",
                                    {"quien": "el saboteador"})
        except BaseException:
            otro.close()
            raise

    j._gancho_carrera = sabotaje
    leidas = j.transitions(s.token, rid)
    j._gancho_carrera = None
    assert len(leidas) == n_antes, (
        "la lectura mezcló la sesión de antes con los datos de después")
    # ⊕ y la revocación SÍ ocurrió: el control no pasa por no haber pasado nada.
    assert j.authenticate(s.token) is None
    assert len(j._transitions(rid)) == n_antes + 1
    otro.close()
    j.close()


# ── AJUSTES: lane estricto, cero bytes, invalidación global ────────────────

def test_un_recibo_SIN_carril_no_lo_lee_nadie_por_la_via_de_runtime(tmp_path):
    """`lane=?` estricto. Con `OR lane IS NULL`, una fila con el campo a nulo
    —por un camino nuevo, por una migración— se volvía legible para toda la
    flota sin que nadie tocara esta consulta."""
    j = journal(tmp_path)
    a, b = sesiones(j,
        {"credential": "cred-A", "principal": "p-a", "lane": "carril-uno"},
        {"credential": "cred-B", "principal": "p-b", "lane": "carril-dos"})
    ev = j.accept_event(a.token, idempotency_key="k", intent=INTENT,
                        ledger="ledger-uno")
    rid = j.receipt_for_event(a.token, ev.event_id)["receipt_id"]
    j._connect().execute("UPDATE receipts SET lane=NULL WHERE receipt_id=?", (rid,))
    for tok in (a.token, b.token):
        with pytest.raises(C.SubjectNotFound):
            j.receipt(tok, rid)
        with pytest.raises(C.SubjectNotFound):
            j.transitions(tok, rid)
        # …y POR EVENTO, que es la otra puerta a la misma fila. Sin esta línea,
        # relajar `receipt_for_event` a `OR lane IS NULL` sobrevivía al test: el
        # camino que dejé sin cubrir es exactamente el que un mutante encuentra.
        with pytest.raises(C.SubjectNotFound):
            j.receipt_for_event(tok, ev.event_id)
    j.close()


def test_transitions_filtra_por_carril_EN_SU_PROPIA_consulta(tmp_path):
    """No vale validar en una sentencia y leer en otra: el filtro se reparte
    entre dos sitios que hay que acordarse de mantener juntos."""
    j = journal(tmp_path)
    a, b = sesiones(j,
        {"credential": "cred-A", "principal": "p-a", "lane": "carril-uno"},
        {"credential": "cred-B", "principal": "p-b", "lane": "carril-dos"})
    ev = j.accept_event(a.token, idempotency_key="k", intent=INTENT,
                        ledger="ledger-uno")
    rid = j.receipt_for_event(a.token, ev.event_id)["receipt_id"]
    assert j.transitions(a.token, rid)                      # ⊕ el dueño sí
    with pytest.raises(C.SubjectNotFound):
        j.transitions(b.token, rid)
    j.close()


def test_una_lectura_fallida_no_escribe_y_funciona_en_SOLO_LECTURA(tmp_path):
    """Decorar las lecturas con el auditor hacía que un `SubjectNotFound` —el
    caso más común— intentara ESCRIBIR su recibo de rechazo. Sobre un journal
    congelado eso convertía «no encontrado» en `JournalReadOnly`, o sea rompía
    justo lo que el modo degradado existe para conservar."""
    import os as _os, stat as _stat
    j = journal(tmp_path)
    s = sesion(j)
    ev = j.accept_event(s.token, idempotency_key="k", intent=INTENT,
                        ledger="llminbox")
    n_denials = j._connect().execute("SELECT COUNT(*) c FROM denials").fetchone()["c"]
    with pytest.raises(C.SubjectNotFound):
        j.receipt(s.token, "rcp_no_existe")
    assert j._connect().execute("SELECT COUNT(*) c FROM denials"
                                ).fetchone()["c"] == n_denials, "la lectura escribió"
    j.close()

    for nombre in _os.listdir(tmp_path):
        _os.chmod(tmp_path / nombre, _stat.S_IRUSR)
    _os.chmod(tmp_path, _stat.S_IRUSR | _stat.S_IXUSR)
    try:
        ro = C.Journal(str(tmp_path / "coordination.sqlite"), pepper=PEPPER,
                       lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
        ro.initialize()          # clasifica sin cerrojo (volumen congelado)
        assert ro.receipt_for_event(s.token, ev.event_id)["current_state"] == "accepted"
        with pytest.raises(C.SubjectNotFound):      # NO `JournalReadOnly`
            ro.receipt(s.token, "rcp_no_existe")
        ro.close()
    finally:
        _os.chmod(tmp_path, _stat.S_IRWXU)
        for nombre in _os.listdir(tmp_path):
            _os.chmod(tmp_path / nombre, _stat.S_IRUSR | _stat.S_IWUSR)


def test_un_fichero_PREEXISTENTE_de_cero_bytes_no_se_adopta(tmp_path):
    """`sqlite3.connect` CREA el fichero, así que después ya no se distingue
    «venía vacío» de «lo acabo de crear yo». Un cero bytes que ya estaba puede
    ser de cualquiera: un `touch` de un despliegue, un volumen a medio montar,
    un truncado. Adoptarlo es sellar como propio algo que no lo es."""
    ruta = str(tmp_path / "coordination.sqlite")
    open(ruta, "wb").close()
    assert os.path.getsize(ruta) == 0                       # ⊕ el escenario
    antes = _huella(ruta)

    j = C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
    # Desde M1-6 el rechazo llega ANTES, en la inspección: el preflight ni
    # siquiera lo clasifica, porque un fichero de cero bytes no admite
    # inspección con garantías. Es más estricto que `SchemaIndeterminate`, no
    # menos — los dos son `JournalError` y ninguno abre el original.
    with pytest.raises(C.PreflightRejected):
        j.initialize()
    j.close()
    assert _huella(ruta) == antes, "escribió en un fichero que no era suyo"

    # ⊕ y una ruta que NO existía sí se crea: el rechazo distingue los dos casos.
    nueva = C.Journal(str(tmp_path / "otra.sqlite"), pepper=PEPPER,
                      lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
    assert nueva.initialize() == C.DURABLE_V
    nueva.close()


def test_atar_una_credencial_INVALIDA_las_sesiones_de_las_demas(tmp_path):
    """Es SEMÁNTICA, no efecto colateral: la generación del mapa es global, así
    que un cambio efectivo invalida toda sesión anterior — también las de otros
    principales. Queda escrito como falsador para que nadie lo 'arregle' bajando
    la regla; lo que se adapta es el ORDEN de los tests (ligar todo y luego
    emitir, o reabrir), no la semántica."""
    j = journal(tmp_path)
    a = sesion(j, "cred-A", principal="p-a")
    assert j.authenticate(a.token) is not None                  # ⊕ viva
    j.bind_credential("cred-B", principal="p-b", role="be", lane="llminbox")
    assert j.authenticate(a.token) is None, (
        "la generación global dejó viva una sesión anterior al cambio de mapa")
    # Reabrir es la vía: la credencial sigue siendo válida, la SESIÓN no.
    a2 = j.open_session("cred-A")
    assert j.authenticate(a2.token) is not None
    j.close()


def test_una_recarga_de_LOTE_sube_la_generacion_una_sola_vez(tmp_path):
    """Diez credenciales nuevas en una recarga son UN cambio de mapa, no diez.
    Subir por cada una dejaría la generación contando ligaduras en vez de
    versiones, y nadie podría razonar sobre qué invalidó qué."""
    j = journal(tmp_path, admision=False)  # `admision=False`: este test CUENTA filas de identidad, y las
    # credenciales/sesiones que el arnés usa para abrir la barrera son
    # filas como cualquier otra. No acepta eventos, así que no necesita
    # la puerta abierta.
    gen = j.generation()
    mapa = {f"cred-{i}": {"principal": f"p{i}", "role": "be", "lane": "llminbox"}
            for i in range(10)}
    assert j.reload_credential_map(mapa) == gen + 1
    assert j.generation() == gen + 1
    assert j._connect().execute("SELECT COUNT(*) c FROM credential_bindings"
                                ).fetchone()["c"] == 10
    # Y una segunda recarga idéntica del lote entero sigue siendo no-op.
    assert j.reload_credential_map(dict(mapa)) == gen + 1
    j.close()
