"""M1-8 — dos bypass de la transición a LISTO, los dos sobre el ORIGINAL.

Ambos comparten mecanismo: la transición trata como vigente algo que midió
ANTES de tener el cerrojo, y actúa sobre el fichero real antes de haber
acreditado que es suyo y que sigue siendo el que clasificó.

⚠️ Todos los controles de bytes fuerzan `journal_mode=DELETE` antes de medir. Sin
eso NO discriminan: una base que ya está en WAL no cambia un byte porque le
vuelvan a aplicar WAL, y el test pasa aunque el código toque el fichero. La
lección es del control de `test_una_base_PARCIAL_nuestra_sin_sello_tampoco_se_sella`.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import time

import pytest

import coordination as C
from ._arnes import GRAMATICA, INTENT, LANES, PEPPER, censo, journal, sesion

OTRO_PEPPER = b"pepper-de-OTRA-casa-no-es-el-que-creo-la-base"


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


def _en_delete(ruta):
    """Deja la base en `journal_mode=delete` y sin sidecars, para que el
    control pueda VER un toque."""
    con = sqlite3.connect(ruta)
    try:
        con.execute("PRAGMA journal_mode=DELETE")
    finally:
        con.close()


def _base_nuestra(tmp_path):
    """Base REAL, sellada y con datos, en `delete` y sin sidecars."""
    j = journal(tmp_path)
    s = sesion(j)
    j.accept_event(s.token, idempotency_key="k", intent=INTENT, ledger="llminbox")
    j.close()
    ruta = str(tmp_path / "coordination.sqlite")
    _en_delete(ruta)
    return ruta


# ── P0-1 · EL PEPPER SE VALIDA ANTES DE ABRIR EL ORIGINAL PARA ESCRIBIR ─────

def test_un_pepper_INCORRECTO_no_toca_ni_un_byte_ni_crea_sidecars(tmp_path):
    """La sonda RW (`journal_mode=WAL` + `BEGIN IMMEDIATE`) corría ANTES de
    validar el pepper. Sobre una base cuya FORMA es la nuestra —así que no la
    para ninguna de las clasificaciones— eso reescribe el formato y crea `-wal`
    y `-shm` en el fichero de otra casa, y sólo DESPUÉS se rechaza.

    Rechazar después de tocar no es rechazar."""
    ruta = _base_nuestra(tmp_path)
    antes = _huella(ruta)
    assert set(antes) == {"db"}, "el control no discrimina si ya hay sidecars"

    ajeno = C.Journal(ruta, pepper=OTRO_PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
    with pytest.raises(C.PepperMismatch):
        ajeno.initialize()
    ajeno.close()

    assert _huella(ruta) == antes, (
        "tocó el fichero antes de acreditar que era suyo")


def test_un_pepper_INCORRECTO_no_deja_health_writable_ni_permite_bind(tmp_path):
    """Rechazar y quedarse a medias son cosas distintas: tras el rechazo, ni la
    salud puede decir que se escribe ni la ligadura puede acuñar nada."""
    ruta = _base_nuestra(tmp_path)
    antes = _huella(ruta)

    ajeno = C.Journal(ruta, pepper=OTRO_PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
    with pytest.raises(C.PepperMismatch):
        ajeno.initialize()

    assert ajeno.health()["writable"] is False
    assert ajeno.health()["estado_ciclo"] != C.Journal.READY
    with pytest.raises(C.JournalError):
        ajeno.bind_credential("cred-intrusa", principal="x", role="be",
                              lane="llminbox")
    ajeno.close()
    assert _huella(ruta) == antes, "el rechazo escribió de todas formas"


# ── P0-2 · LA CLASIFICACIÓN SE REHACE BAJO EL CERROJO ───────────────────────

def test_una_modificacion_IN_PLACE_entre_preflight_e_initialize_se_caza(tmp_path):
    """`_verificar_identidad` compara `(dev, inode)`, y una escritura in-place
    los CONSERVA. El veredicto y la copia de antes seguían valiendo, así que la
    transición migraba con una clasificación caducada: el inode es el mismo y el
    contenido no.

    Aquí la base pasa a versión FUTURA por debajo. Con la clasificación vieja en
    la mano, la transición cree que es `conocida` y sigue."""
    ruta = _base_nuestra(tmp_path)

    j = C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
    assert j.stored_durable_v() == C.DURABLE_V      # clasifica y cachea la copia
    ino_antes = os.stat(ruta).st_ino

    con = sqlite3.connect(ruta)                     # IN PLACE: mismo inode
    con.execute("UPDATE meta SET v=? WHERE k='durable_v'", (str(C.DURABLE_V + 1),))
    con.commit()
    con.close()
    _en_delete(ruta)
    assert os.stat(ruta).st_ino == ino_antes, "el fixture reemplazó el fichero"

    with pytest.raises(C.SchemaTooNew):
        j.initialize()
    j.close()


def test_un_cambio_de_PEPPER_in_place_entre_preflight_e_initialize_se_caza(tmp_path):
    """Misma clase, distinto sujeto: lo que caduca es el `pepper_check`. Si la
    transición valida el pepper contra la copia VIEJA, acredita una base que ya
    no es la que hay en disco."""
    ruta = _base_nuestra(tmp_path)

    j = C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
    assert j.stored_durable_v() == C.DURABLE_V
    ino_antes = os.stat(ruta).st_ino

    import hmac as _hmac
    esperado_ajeno = _hmac.new(OTRO_PEPPER, C._PEPPER_CHECK,
                               hashlib.sha256).hexdigest()
    con = sqlite3.connect(ruta)                      # IN PLACE
    con.execute("UPDATE meta SET v=? WHERE k='pepper_check'", (esperado_ajeno,))
    con.commit()
    con.close()
    _en_delete(ruta)
    assert os.stat(ruta).st_ino == ino_antes
    antes = _huella(ruta)
    assert set(antes) == {"db"}

    with pytest.raises(C.PepperMismatch):
        j.initialize()
    j.close()
    # ⚠️ EL TIPO DE EXCEPCIÓN NO BASTA, y este test lo aprendió pasando por el
    #    motivo equivocado: el `PepperMismatch` salía de `_inicializar_dentro`,
    #    o sea DENTRO ya de la transacción RW sobre el original y después de
    #    haberle aplicado `journal_mode=WAL`. Levantaba la excepción correcta
    #    habiendo hecho justo lo que no se puede hacer. Lo que se exige es que
    #    el fichero no se haya tocado.
    assert _huella(ruta) == antes, (
        "rechazó por pepper, pero después de escribir en el original")


# ── V-1 · EL DUEÑO VENCIDO NO MUTA DESPUÉS DEL RELEVO ──────────────────────
#
# El falsador que ya existía (`test_el_dueno_vencido_es_RECHAZADO_al_mutar_tras_
# el_relevo`) llama a `check_fence`, que es una COMPROBACIÓN. Que el chequeo diga
# «no» no acredita que la ESCRITURA se pare: son dos bordes distintos, y el que
# importa es el de la mutación. Aquí se intenta una mutación REAL.

def test_V1_el_dueno_vencido_intenta_una_MUTACION_real_tras_el_relevo(tmp_path):
    from ._arnes import Reloj, sesiones
    reloj = Reloj()
    j = journal(tmp_path, reloj=reloj)
    a, b = sesiones(j,
        {"credential": "cred-A", "principal": "p-a"},
        {"credential": "cred-B", "principal": "p-b"})

    viejo = j.acquire_lease(a.token, "migracion", ttl_s=60)
    # ⊕ CONTROL: con la valla vigente, la MUTACIÓN entra. Sin este brazo, el
    #   rechazo de abajo no distingue «la valla para» de «esta escritura nunca
    #   funcionó».
    ok = j.accept_event(a.token, idempotency_key="v1-ok", intent=INTENT,
                        ledger="llminbox", fenced_resource="migracion",
                        fencing_token=viejo.fencing_token)
    assert ok.event_id

    reloj.avanza(61)
    nuevo = j.acquire_lease(b.token, "migracion", ttl_s=60)      # relevo
    assert nuevo.fencing_token == viejo.fencing_token + 1

    antes = _censo_eventos(j, a.token)
    with pytest.raises(C.FencingConflict):
        j.accept_event(a.token, idempotency_key="v1-zombi", intent=INTENT,
                       ledger="llminbox", fenced_resource="migracion",
                       fencing_token=viejo.fencing_token)
    # Ni con el token NUEVO: no es que su número sea viejo, es que su sesión ya
    # no es la dueña del recurso.
    with pytest.raises(C.FencingConflict):
        j.accept_event(a.token, idempotency_key="v1-zombi2", intent=INTENT,
                       ledger="llminbox", fenced_resource="migracion",
                       fencing_token=nuevo.fencing_token)
    assert _censo_eventos(j, a.token) == antes, "el zombi escribió"

    # ⊕ y el dueño NUEVO sí muta: cierra la pinza.
    assert j.accept_event(b.token, idempotency_key="v1-relevo", intent=INTENT,
                          ledger="llminbox", fenced_resource="migracion",
                          fencing_token=nuevo.fencing_token).event_id
    j.dispose()


def _censo_eventos(j, token):
    con = j._connect()
    return con.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]


def test_V1_un_rebind_IDENTICO_es_idempotente_y_no_acuna_principal_nuevo(tmp_path):
    """Control positivo del relevo: re-ligar la MISMA credencial a lo MISMO no
    puede crear un principal nuevo ni mover la generación."""
    j = journal(tmp_path)
    j.bind_credential("cred-R", principal="p-r", role="be", lane="llminbox")
    con = j._connect()
    n_antes = con.execute("SELECT COUNT(*) c FROM principals").fetchone()["c"]
    gen_antes = j.generation()

    j.bind_credential("cred-R", principal="p-r", role="be", lane="llminbox")

    assert con.execute("SELECT COUNT(*) c FROM principals").fetchone()["c"] == n_antes
    assert j.generation() == gen_antes, "un rebind idéntico movió la generación"
    j.dispose()


# ── DEGRADACIÓN RO DESPUÉS DE ACTIVIDAD: NO SE SIRVE LA FOTO DE ARRANQUE ────

def test_al_congelarse_TRAS_actividad_no_se_sirve_la_copia_del_arranque(tmp_path):
    """La copia se toma al clasificar. Si al degradar a sólo lectura se sirviera
    ESA, el lector vería el estado de cuando se abrió el journal y no el último
    commit durable — y lo vería sin ningún aviso, que es lo peor de las dos
    formas de fallar."""
    import stat as _stat
    j = journal(tmp_path)
    s = sesion(j)
    j.accept_event(s.token, idempotency_key="antes", intent=INTENT,
                   ledger="llminbox")
    # ACTIVIDAD POSTERIOR al arranque: esto NO está en la copia inicial.
    despues = j.accept_event(s.token, idempotency_key="despues", intent=INTENT,
                             ledger="llminbox")
    j.close()

    for nombre in os.listdir(tmp_path):
        os.chmod(tmp_path / nombre, _stat.S_IRUSR)
    os.chmod(tmp_path, _stat.S_IRUSR | _stat.S_IXUSR)
    try:
        ro = C.Journal(str(tmp_path / "coordination.sqlite"), pepper=PEPPER,
                       lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
        ro.initialize()
        assert ro.health()["writable"] is False
        # El evento POSTERIOR a la foto de arranque tiene que verse.
        assert ro.receipt_for_event(s.token, despues.event_id)[
            "current_state"] == "accepted"
        ro.close()
    finally:
        os.chmod(tmp_path, _stat.S_IRWXU)
        for nombre in os.listdir(tmp_path):
            os.chmod(tmp_path / nombre, _stat.S_IRUSR | _stat.S_IWUSR)


# ── LIMPIEZA DE TEMPORALES ─────────────────────────────────────────────────
#
# ⚠️ HERMÉTICO A PROPÓSITO, DOS VECES:
#   (1) no globear `tempfile.gettempdir()` — es GLOBAL AL SISTEMA, compartido
#       por cualquier proceso (otra suite, otro worker `xdist`, otro agente
#       corriendo esta misma suite a la vez), así que una foto antes/después
#       de su contenido confunde el tmpdir VIVO de un tercero con una fuga
#       propia. Medido: los 5 falsadores de esta sección, con el glob global,
#       fallaban 5/5 bajo `pytest -n 16` (y hasta en serie, con otro proceso
#       ajeno tocando el mismo `/tmp`) y 0/5 aislados.
#   (2) NO-GO detectado sobre el primer arreglo (que sí confinaba, pero
#       verificaba con `os.path.exists(<nombre recordado>)`): una limpieza
#       que RENOMBRA el directorio en vez de borrarlo hace que `exists()` del
#       nombre viejo dé `False` con la fuga todavía viva en disco bajo otro
#       nombre, y da igual el nombre porque `exists()` sigue symlinks — uno
#       colgante también se lee como «no está». Y una limpieza demasiado
#       ancha (`rmtree` del padre en vez del hijo) se llevaría por delante
#       cualquier cosa ajena sin que `exists(mi_path)` lo note.
#
# La cura: `mkdtemp` se confina a un padre PRIVADO de este test (dentro de
# `tmp_path`, nunca el `/tmp` real), con un CENTINELA ajeno sembrado al lado.
# Al cerrar el área se exige: (a) el padre no contiene NADA salvo el
# centinela — con `os.scandir`, no `exists`, así que un renombre o un symlink
# colgante siguen contando como entrada; (b) tanto el padre COMO el
# centinela siguen siendo EL MISMO objeto — igual que
# `coordination._verificar_identidad` usa `(dev, inode)`, no el nombre, para
# decidir identidad.
#
# NO-GO sobre ESE segundo arreglo: comparar sólo `st_ino` vía `Path.stat()`
# (que SIGUE symlinks) no basta.
#   · centinela: un hardlink previo al fichero original preserva su inodo Y
#     su contenido; sustituir la entrada por un symlink a ese hardlink
#     reproduce ambos enteros — `stat()` los sigue y ve el original intacto.
#     Sólo `lstat()` sobre el PROPIO path del centinela, exigiendo que su
#     `st_mode` siga siendo fichero regular, ve que ya no lo es.
#   · padre: mover el directorio entero a otro nombre y dejar un symlink en
#     su lugar apuntando a él (con el centinela intacto dentro) pasa TODOS
#     los chequeos de contenido/inodo del centinela y el `scandir` — ambos
#     siguen el symlink sin darse cuenta de que el objeto en esa ruta ya no
#     es el directorio que abrimos. Hace falta la MISMA disciplina
#     (`lstat` + `(dev, ino, st_mode)`, exigiendo directorio) también sobre
#     el padre, no sólo sobre el centinela.
# Este arnés replica `coordination._stat_seguro`: `lstat`, nunca `stat`, y
# rechazo explícito de lo que no es del tipo esperado.

import contextlib as _contextlib


@_contextlib.contextmanager
def _area_preflight_privada(tmp_path):
    """Confina TODO `tempfile.mkdtemp` de este bloque a un padre propio y
    siembra un centinela ajeno al lado. Al salir: el padre no tiene más que
    el centinela (ninguna otra entrada — renombrada, symlink colgante o lo
    que sea), y TANTO el padre COMO el centinela siguen siendo el mismo
    objeto — `lstat` y `(dev, ino, st_mode)`, nunca el nombre ni un `stat()`
    que siga un symlink de sustitución."""
    import stat as _stat

    padre = tmp_path / "area-preflight-privada"
    padre.mkdir()
    st_padre = os.lstat(padre)
    assert _stat.S_ISDIR(st_padre.st_mode), "el padre privado no nació directorio"
    identidad_padre = (st_padre.st_dev, st_padre.st_ino, st_padre.st_mode)

    centinela = padre / "centinela-ajeno"
    contenido = os.urandom(32)
    centinela.write_bytes(contenido)
    st_centinela = os.lstat(centinela)
    assert _stat.S_ISREG(st_centinela.st_mode), (
        "el centinela ajeno no nació fichero regular")
    identidad_centinela = (st_centinela.st_dev, st_centinela.st_ino,
                            st_centinela.st_mode)

    real = tempfile.mkdtemp

    def mkdtemp_confinado(*a, **k):
        k.setdefault("dir", str(padre))
        return real(*a, **k)

    tempfile.mkdtemp = mkdtemp_confinado
    try:
        yield padre
    finally:
        tempfile.mkdtemp = real
        try:
            st_padre = os.lstat(padre)
        except FileNotFoundError:
            raise AssertionError(
                "el padre privado desapareció: la limpieza tocó algo que "
                "no era suyo") from None
        assert (st_padre.st_dev, st_padre.st_ino, st_padre.st_mode) == identidad_padre, (
            "el padre privado ya no es el directorio que abrimos: lo "
            "sustituyeron (p.ej. rename + symlink puesto en su lugar)")
        try:
            st = os.lstat(centinela)
        except FileNotFoundError:
            raise AssertionError(
                "el centinela ajeno desapareció: la limpieza tocó algo que "
                "no era suyo") from None
        assert (st.st_dev, st.st_ino, st.st_mode) == identidad_centinela, (
            "el centinela ajeno cambió de identidad (dev/inodo/tipo): lo "
            "sustituyeron por otra cosa (symlink, hardlink recompuesto...) "
            "aunque el contenido coincida")
        assert centinela.read_bytes() == contenido, (
            "el centinela ajeno cambió de contenido: algo lo sobrescribió")
        quedan = sorted(e.name for e in os.scandir(padre)
                         if e.name != centinela.name)
        assert not quedan, f"quedó sin liberar en el área privada: {quedan}"


def test_area_privada_detecta_un_RENOMBRE_en_vez_de_un_borrado(tmp_path):
    """Falsador del propio arnés — NO-GO que motivó este bloque: un mutante
    que en vez de borrar RENOMBRA el directorio evade
    `os.path.exists(nombre_viejo)`, que da `False` con la fuga todavía viva
    bajo otro nombre. `scandir` sobre el padre la ve tenga el nombre que
    tenga."""
    with pytest.raises(AssertionError, match="quedó sin liberar"):
        with _area_preflight_privada(tmp_path):
            d = tempfile.mkdtemp(prefix="llminbox-preflight-")
            os.rename(d, d + "-renombrado-en-vez-de-borrado")
            assert not os.path.exists(d), (
                "control: si esto no diera falso, este falsador no probaría nada")


def test_area_privada_detecta_borrar_el_CENTINELA_ajeno(tmp_path):
    """Falsador del propio arnés: una limpieza demasiado ancha (p.ej. un
    `rmtree` del padre en vez del hijo) se llevaría por delante cualquier
    cosa ajena que viva al lado. El centinela existe para delatar
    exactamente eso."""
    with pytest.raises(AssertionError, match="centinela"):
        with _area_preflight_privada(tmp_path) as padre:
            os.unlink(padre / "centinela-ajeno")


def test_area_privada_detecta_un_symlink_colgante(tmp_path):
    """`os.path.exists` da por AUSENTE un symlink colgante — el punto ciego
    exacto del NO-GO. `scandir` lista la entrada tenga o no destino vivo."""
    with pytest.raises(AssertionError, match="quedó sin liberar"):
        with _area_preflight_privada(tmp_path) as padre:
            objetivo = padre / "objetivo-que-desaparece"
            objetivo.mkdir()
            enlace = padre / "llminbox-preflight-enlace-colgante"
            enlace.symlink_to(objetivo)
            objetivo.rmdir()          # el symlink queda COLGANDO
            assert not os.path.exists(str(enlace)), (
                "control: si esto no diera falso, este falsador no probaría nada")


def test_area_privada_detecta_un_SWAP_hardlink_mas_symlink_del_centinela(tmp_path):
    """Falsador del propio arnés — NO-GO: comparar sólo inodo+contenido vía
    `Path.stat()` (que SIGUE symlinks) no basta. Un hardlink previo preserva
    el inodo y el contenido originales del centinela; sustituir su entrada
    por un symlink a ese hardlink los reproduce enteros bajo `stat()`. El
    respaldo vive a propósito FUERA del padre (en `tmp_path`) para aislar
    este falsador del chequeo de `scandir` — si viviera dentro, ESE chequeo
    ya lo cazaría por la entrada de más y no probaría nada del `lstat` del
    centinela. Sólo `lstat()` sobre el PROPIO path del centinela ve que ya
    no es un fichero regular — ahora es un symlink — aunque inodo y bytes
    coincidan."""
    with pytest.raises(AssertionError, match="identidad"):
        with _area_preflight_privada(tmp_path) as padre:
            centinela = padre / "centinela-ajeno"
            respaldo = padre.parent / "respaldo-mismo-inodo"  # fuera del padre
            os.link(centinela, respaldo)      # mismo inodo, a salvo
            os.unlink(centinela)              # la entrada original se va
            os.symlink(respaldo, centinela)   # ahora es un symlink al respaldo


def test_area_privada_detecta_un_RENAME_del_padre_sustituido_por_symlink(tmp_path):
    """Falsador del propio arnés — NO-GO: mover el padre entero a otro
    nombre y dejar un symlink en su lugar apuntando a él (con el centinela
    intacto dentro) pasa TODOS los chequeos de contenido/inodo del
    centinela y el `scandir` de después — ambos siguen el symlink sin
    darse cuenta de que la ruta ya no es el directorio que abrimos. Sólo
    comprobar la identidad del PROPIO padre (`lstat`, sin seguir el
    enlace) lo ve."""
    with pytest.raises(AssertionError, match="padre privado"):
        with _area_preflight_privada(tmp_path) as padre:
            padre_movido = padre.with_name(padre.name + "-movido")
            os.rename(padre, padre_movido)
            os.symlink(padre_movido, padre)


def test_dispose_no_deja_copias_del_journal_en_el_temporal(tmp_path):
    """Cada `Journal` deja una copia ENTERA de la base en `/tmp`. Sin liberarla,
    abrir el journal N veces deja N réplicas del canon vivas hasta que el
    sistema limpie."""
    ruta = _base_nuestra(tmp_path)
    with _area_preflight_privada(tmp_path):
        for _ in range(3):
            j = C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
            j.initialize()
            assert j._tmpdir, "el fixture no ejerció la ruta que mide"
            j.dispose()


# ── TOPES Y BRAZOS DE LA FOTO: UN FALSADOR POR CADA UNO ────────────────────
#
# `test_14` afirma «acotado: 3 intentos o 2 s» y sólo puede probar la
# CONJUNCIÓN: como cada tope basta por sí solo para salir rápido, quitar uno deja
# el test verde. Dos topes con un solo falsador es un tope sin falsador.

def test_la_foto_no_reintenta_MAS_de_PREFLIGHT_INTENTOS_veces(tmp_path):
    """Mide el TOPE DE INTENTOS por sí solo, contando llamadas a la copia."""
    ruta = _base_nuestra(tmp_path)
    original = C._copiar_a
    veces = {"n": 0}

    def copiar_y_tocar(base, destino):
        original(base, destino)
        veces["n"] += 1
        with open(base, "r+b") as fh:      # cambia SIEMPRE: nunca hay quietud
            fh.seek(0)
            fh.write(b"S")

    C._copiar_a = copiar_y_tocar
    try:
        j = C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
        with pytest.raises(C.PreflightUnstable):
            j.initialize()
        j.dispose()
    finally:
        C._copiar_a = original
    # ⚠️ LA COTA ES LITERAL, y no por gusto: comparar contra
    # `C.PREFLIGHT_INTENTOS` hacía que el test leyera su propio listón DE LA
    # CONSTANTE QUE SE ESTÁ MUTANDO. Subirla a 10.000.000 subía también la
    # expectativa, el `assert` pasaba y el mutante sobrevivía — un falsador que
    # se mide con la regla del sospechoso. Medido: así sobrevivía M09.
    TOPE_CONGELADO = 5
    assert C.PREFLIGHT_INTENTOS <= TOPE_CONGELADO, (
        f"el tope de intentos subió a {C.PREFLIGHT_INTENTOS}: si es a propósito, "
        f"se cambia TAMBIÉN esta cota, y con eso el cambio se ve en la revisión")
    assert veces["n"] <= TOPE_CONGELADO, (
        f"reintentó {veces['n']} veces: la inestabilidad se estaba ganando a "
        f"reintentos, que es justo lo que el preflight dice no hacer")
    assert veces["n"] >= 2, "no ejerció el reintento: el test no mide el tope"


def test_una_COPIA_que_no_casa_con_el_original_hace_la_foto_inestable(tmp_path):
    """El brazo `copia[k] == antes[k]` por sí solo.

    Aquí el ORIGINAL no se mueve —`antes == despues`—, así que los otros brazos
    dan «estable». Lo único que puede cazar una copia mal hecha es compararla
    con lo copiado. Sin ese brazo se clasifica sobre bytes que no son los del
    fichero, y todo lo que venga después —pepper, versión, manifiesto— habla de
    otra cosa.
    """
    ruta = _base_nuestra(tmp_path)
    original = C._copiar_a

    def copiar_mal(base, destino):
        original(base, destino)
        destino_db = os.path.join(destino, os.path.basename(base))
        with open(destino_db, "r+b") as fh:   # SOLO la copia se corrompe
            fh.seek(20)
            b = fh.read(1)
            fh.seek(20)
            fh.write(bytes([b[0] ^ 0xFF]))

    C._copiar_a = copiar_mal
    antes = _huella(ruta)
    try:
        j = C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
        with pytest.raises(C.PreflightUnstable):
            j.initialize()
        j.dispose()
    finally:
        C._copiar_a = original
    assert _huella(ruta) == antes, "el original se movió: el test mide otra cosa"


# ── P0-A · READY→RO NO PUEDE SERVIR LA FOTO DEL ARRANQUE ───────────────────

def test_la_degradacion_a_RO_no_sirve_la_generacion_del_ARRANQUE(tmp_path):
    """LA MISMA instancia. El test anterior creaba un `Journal` NUEVO, y uno
    nuevo clasifica de cero: su copia ya traía el estado último, así que pasaba
    sin ejercer nada. El bypass sólo se ve con la instancia que YA tiene una
    copia vieja en la mano.

    Aquí `j` fotografía en `gen1`, el original avanza a `gen2` por fuera, y
    entonces se le rompe la escritura. Si al degradar sirve `self._snapshot`,
    contesta `gen1` — el estado de hace rato, presentado como el actual.
    """
    ruta = _base_nuestra(tmp_path)
    j = C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
    j.initialize()
    gen1 = j.generation()

    # El original AVANZA por fuera, con la copia de `j` ya tomada.
    otro = C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
    otro.initialize()
    otro.rotate_map_generation()
    gen2 = otro.generation()
    otro.dispose()
    assert gen2 != gen1, "el fixture no movió la generación: no mide nada"

    # Y ahora se le rompe la escritura a `j`, que sigue en READY con su copia.
    j.close()
    real = sqlite3.connect
    objetivo = os.path.realpath(ruta)

    def connect_falla_en_el_original(destino, *a, **k):
        if isinstance(destino, str) and os.path.realpath(destino) == objetivo:
            raise sqlite3.OperationalError("attempt to write a readonly database")
        return real(destino, *a, **k)

    sqlite3.connect = connect_falla_en_el_original
    try:
        with pytest.raises(C.JournalError):   # ① falla CERRADO, no degrada sola
            j.generation()
        # ② TRANSICIÓN EXPLÍCITA. Aquí es donde se decide qué se sirve, y es lo
        #    que el falsador tiene que mirar: aceptar el fail-closed y parar
        #    dejaba pasar un mutante que se salta la re-foto, porque el error
        #    llega igual por otro camino.
        j.initialize()
        visto = j.generation()
    finally:
        sqlite3.connect = real
    assert visto != gen1, (
        f"sirvió la generación del ARRANQUE ({gen1}) como si fuera la actual "
        f"({gen2}): datos rancios en silencio")
    assert visto == gen2, f"esperaba {gen2}, sirvió {visto}"
    j.dispose()


# ── P0-B · UNA CONEXIÓN CACHEADA NO SOBREVIVE AL RESPALDO QUE LA SOSTIENE ──

def test_una_conexion_de_worker_no_sigue_sirviendo_tras_dispose(tmp_path):
    """La conexión es THREAD-LOCAL, así que `dispose()` en el hilo principal no
    la tocaba: el worker seguía leyendo por un handle cuya copia ya se había
    borrado, y no tenía forma de enterarse."""
    import threading
    ruta = _base_nuestra(tmp_path)
    j = C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
    j.initialize()

    listo = threading.Event()
    sigue = threading.Event()
    resultado = {}

    # ⚠️ EL OBSERVABLE TIENE QUE DISCRIMINAR. `stored_durable_v()` NO sirve:
    #    vale 3 en la copia rancia y 3 en la realidad, así que el test pasaba
    #    con y sin cura. Se mide la GENERACIÓN, que sí cambia por fuera.
    def worker():
        j._connect()                     # cachea en ESTA hebra
        resultado["antes"] = j.generation()
        listo.set()
        sigue.wait(30)
        try:
            resultado["despues"] = j.generation()
        except C.JournalError as e:
            resultado["despues"] = type(e).__name__

    h = threading.Thread(target=worker)
    h.start()
    assert listo.wait(30)
    gen1 = resultado["antes"]

    otro = C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
    otro.initialize()
    otro.rotate_map_generation()
    gen2 = otro.generation()
    otro.dispose()
    assert gen2 != gen1, "el fixture no movió la generación: no mide nada"

    j.dispose()                          # el respaldo del worker se va
    sigue.set()
    h.join(30)

    assert resultado["despues"] != gen1, (
        f"el worker siguió contestando {gen1} por una conexión cuyo respaldo ya "
        f"no existe, con la realidad en {gen2}: la época no invalidó lo cacheado")


def test_una_REFOTO_de_otro_hilo_invalida_la_conexion_CACHEADA_del_worker(tmp_path):
    """El falsador anterior NO discriminaba y el runner lo cazó: tras `dispose()`
    el estado vuelve a `NUEVO`, así que la siguiente pregunta RECLASIFICA y
    contesta bien por un camino que no es el que se quiere probar. El mutante
    que anula la época sobrevivía.

    Aquí se mira el OBJETO conexión, que es lo único que la época puede cambiar:
    tras una re-foto de otro hilo, `_connect()` NO puede devolver el mismo
    handle que ya tenía cacheado esta hebra.
    """
    import threading
    ruta = _base_nuestra(tmp_path)
    j = C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
    j.initialize()

    cacheado, refoto_hecha, fin = threading.Event(), threading.Event(), {}

    def worker():
        fin["con1"] = j._connect()          # CACHEA de verdad en esta hebra
        cacheado.set()
        refoto_hecha.wait(30)
        try:
            fin["con2"] = j._connect()
        except C.JournalError as e:
            fin["con2"] = type(e).__name__

    h = threading.Thread(target=worker)
    h.start()
    assert cacheado.wait(30)
    assert fin["con1"] is not None

    with j._cerrojo_ciclo():               # otro hilo rehace la foto
        j._refotografiar()
    refoto_hecha.set()
    h.join(30)

    assert fin["con2"] is not fin["con1"], (
        "el worker recibió EL MISMO handle que tenía cacheado después de que "
        "otro hilo rehiciera la foto: la época no invalidó nada")
    j.dispose()


def test_V1_el_token_VIEJO_del_MISMO_dueno_no_pasa_tras_RENOVAR_la_valla(tmp_path):
    """Aísla el brazo de IGUALDAD, que es el que el mutante ataca.

    En el test de relevo el recurso cambia de dueño, así que lo que para al
    zombi es el brazo de `runtime_instance` — y con la igualdad anulada el
    mutante sobrevivía porque OTRO guard hacía el trabajo. Aquí el dueño es EL
    MISMO en los dos momentos: lo único que separa el token viejo del vigente es
    la comparación.
    """
    from ._arnes import Reloj
    reloj = Reloj()
    j = journal(tmp_path, reloj=reloj)
    s_ = sesion(j)

    uno = j.acquire_lease(s_.token, "r", ttl_s=60)
    assert j.accept_event(s_.token, idempotency_key="v1e-ok", intent=INTENT,
                          ledger="llminbox", fenced_resource="r",
                          fencing_token=uno.fencing_token).event_id

    reloj.avanza(61)                                   # vence
    dos = j.acquire_lease(s_.token, "r", ttl_s=60)     # MISMO dueño, token nuevo
    assert dos.fencing_token > uno.fencing_token
    assert dos.runtime_instance == uno.runtime_instance if hasattr(
        dos, "runtime_instance") else True

    con = j._connect()
    antes = con.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    with pytest.raises(C.FencingConflict):
        j.accept_event(s_.token, idempotency_key="v1e-viejo", intent=INTENT,
                       ledger="llminbox", fenced_resource="r",
                       fencing_token=uno.fencing_token)
    assert j._connect().execute(
        "SELECT COUNT(*) c FROM events").fetchone()["c"] == antes
    # ⊕ y el token VIGENTE del mismo dueño sí entra: cierra la pinza.
    assert j.accept_event(s_.token, idempotency_key="v1e-nuevo", intent=INTENT,
                          ledger="llminbox", fenced_resource="r",
                          fencing_token=dos.fencing_token).event_id
    j.dispose()


def test_un_SUPER_JOURNAL_de_nombre_dinamico_hace_la_foto_inestable(tmp_path):
    """`SUFIJOS` traía `-mj` LITERAL, y ese fichero no existe: SQLite lo llama
    `-mjHHHHHHHH`. El sidecar que coordina un commit multi-base era invisible,
    así que podía aparecer entre las dos lecturas sin mover el veredicto."""
    ruta = _base_nuestra(tmp_path)
    original = C._copiar_a
    estado = {"n": 0}

    def copiar_y_plantar_mj(base, destino):
        original(base, destino)
        estado["n"] += 1
        if estado["n"] <= C.PREFLIGHT_INTENTOS:
            with open(base + "-mj0A1B2C3D", "wb") as fh:
                fh.write(b"\x00" * 16)      # super-journal con nombre REAL

    C._copiar_a = copiar_y_plantar_mj
    try:
        j = C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
        with pytest.raises(C.PreflightUnstable):
            j.initialize()
        j.dispose()
    finally:
        C._copiar_a = original
        for n in os.listdir(os.path.dirname(ruta)):
            if n.startswith(os.path.basename(ruta) + "-mj"):
                os.unlink(os.path.join(os.path.dirname(ruta), n))


def test_una_foto_FALLIDA_no_deja_su_temporal_huerfano(tmp_path):
    """El camino de ÉXITO ya estaba medido; el de FALLO no, y es donde vivía el
    defecto: el finalizador se armaba antes de saber si la foto valía, así que
    una foto rota dejaba un directorio sin dueño. Medido en su día: eso puso
    flaky el falsador de 20 procesos, que en `HEAD` pasaba 3/3."""
    ruta = _base_nuestra(tmp_path)
    original = C._copiar_a

    def copiar_y_desestabilizar(base, destino):
        original(base, destino)
        with open(base, "r+b") as fh:      # nunca hay quietud: la foto falla
            fh.seek(0)
            fh.write(b"S")

    C._copiar_a = copiar_y_desestabilizar
    try:
        with _area_preflight_privada(tmp_path):
            j = C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
            with pytest.raises(C.PreflightUnstable):
                j.initialize()
            assert j._tmpdir is None, "quedó apuntando a un temporal que no vale"
            j.dispose()
    finally:
        C._copiar_a = original


# ── M1-9 · SEIS PUNTOS DE LA AUDITORÍA SOBRE eed2536 ───────────────────────

def test_una_TX_capturada_no_commitea_tras_un_dispose_ajeno(tmp_path):
    """El P0: la hebra construye la transacción, otra hace `dispose()`, y la
    primera commitea con el journal ya en `NUEVO`. La comprobación de estado se
    hacía al ENTRAR y caducaba antes del `COMMIT`."""
    import threading
    j = journal(tmp_path)
    s_ = sesion(j)
    dentro, suelta, fin = threading.Event(), threading.Event(), {}
    gen_antes = j.generation()

    def worker():
        try:
            with j._tx() as con:
                con.execute("UPDATE meta SET v=? WHERE k='generation'",
                            (str(gen_antes + 99),))
                dentro.set()
                suelta.wait(30)
            fin["r"] = "COMMITEÓ"
        except C.JournalError as e:
            fin["r"] = type(e).__name__

    h = threading.Thread(target=worker); h.start()
    assert dentro.wait(30)
    j._estado = C.Journal.NUEVO          # lo que deja un `dispose()` ajeno
    j._epoca += 1
    suelta.set(); h.join(30)

    assert fin["r"] != "COMMITEÓ", "selló una transacción con el journal muerto"


def test_el_cerrojo_de_escritura_NO_ABRIBLE_falla_cerrado(tmp_path):
    """Antes seguía sin cerrojo «porque el volumen estará congelado»: el caso
    que más exclusión necesita era el único que se la saltaba."""
    j = journal(tmp_path)
    s_ = sesion(j)
    real = os.open

    def open_falla_en_el_lockfile(ruta, *a, **k):
        if isinstance(ruta, str) and ruta.endswith(".lifecycle"):
            raise OSError(30, "Read-only file system")
        return real(ruta, *a, **k)

    os.open = open_falla_en_el_lockfile
    try:
        with pytest.raises(C.JournalError):
            j.accept_event(s_.token, idempotency_key="sin-cerrojo",
                           intent=INTENT, ledger="llminbox")
    finally:
        os.open = real
    j.dispose()


def test_un_listdir_que_falla_no_se_lee_como_SIN_sidecars(tmp_path):
    """«No he podido mirar» y «no hay» son distintos: el primero no autoriza."""
    ruta = _base_nuestra(tmp_path)
    real = os.listdir

    def listdir_roto(d):
        raise OSError(13, "Permission denied")

    os.listdir = listdir_roto
    try:
        with pytest.raises(C.JournalError):
            C._inventario(ruta)
    finally:
        os.listdir = real


def test_una_CLASIFICACION_fallida_tras_la_foto_no_fuga_el_temporal_viejo(tmp_path):
    """La foto nueva sale buena y la clasificación falla DESPUÉS. Si el
    finalizador viejo ya se había desenganchado, `dispose()` borra el nuevo y el
    anterior queda sin dueño."""
    ruta = _base_nuestra(tmp_path)

    with _area_preflight_privada(tmp_path):
        j = C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
        j.initialize()
        viejo = j._tmpdir
        assert viejo and os.path.exists(viejo)

        real = C._clasificar

        def clasificar_roto(con):
            raise RuntimeError("clasificación rota DESPUÉS de una foto buena")

        C._clasificar = clasificar_roto
        try:
            with pytest.raises(Exception):
                with j._cerrojo_ciclo():
                    j._refotografiar()
        finally:
            C._clasificar = real
        assert os.path.exists(viejo), "el temporal viejo quedó sin dueño"
        j.dispose()


def test_la_copia_INCLUYE_el_super_journal_dinamico(tmp_path):
    """El inventario lo veía y `_copiar_a` no lo copiaba: la foto era de un
    estado que no incluye la transacción multi-base a medias, y el brazo
    `copia == antes` la daba por buena porque ni la miraba."""
    ruta = _base_nuestra(tmp_path)
    mj = ruta + "-mj0A1B2C3D"
    with open(mj, "wb") as fh:
        fh.write(b"\x5a" * 24)
    try:
        j = C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
        j.initialize()                       # sidecar ESTABLE: no da inestable
        copiado = os.path.join(j._tmpdir, os.path.basename(mj))
        assert os.path.exists(copiado), "la copia no trajo el super-journal"
        assert open(copiado, "rb").read() == b"\x5a" * 24
        j.dispose()
    finally:
        if os.path.exists(mj):
            os.unlink(mj)


def test_un_fallo_de_apertura_CON_SH_EXTERIOR_no_hace_upgrade_ni_se_cuelga(tmp_path):
    """El deadlock verificado: `_connect` tomaba SH y, dentro, el `except`
    pedía EX para degradar a sólo lectura. Es un upgrade SH→EX con OTRO
    descriptor y `flock` lo resuelve bloqueándose contra uno mismo — medido:
    890 de 890 frames del `sample` dentro de `fcntl.flock`.

    Y sacarlo del `with` interno no bastaba: viniendo de un `_Tx` hay un SH
    EXTERIOR retenido. Por eso la degradación es fail-closed y la transición a
    RO es un acto posterior y explícito.

    ⏱️ Lleva watchdog: un test de deadlock que se cuelga no falla, ESPERA — y en
    un arnés sin timeout eso se lee como «lento», no como «roto».
    """
    import faulthandler
    j = journal(tmp_path)
    sesion(j)
    ruta = str(tmp_path / "coordination.sqlite")
    real = sqlite3.connect
    objetivo = os.path.realpath(ruta)

    def connect_falla(destino, *a, **k):
        if isinstance(destino, str) and os.path.realpath(destino) == objetivo:
            raise sqlite3.OperationalError("attempt to write a readonly database")
        return real(destino, *a, **k)

    j.close()
    sqlite3.connect = connect_falla
    faulthandler.dump_traceback_later(25, exit=True)
    try:
        with j._cerrojo_escritor():          # SH EXTERIOR, como haría `_Tx`
            with pytest.raises(C.JournalError):
                j._connect()
    finally:
        faulthandler.cancel_dump_traceback_later()
        sqlite3.connect = real
    j.dispose()


# ── M1-10 · CUATRO P0 DE LA REVISIÓN DEL DIFF ──────────────────────────────

def test_una_LECTURA_viva_excluye_al_ciclo_de_vida(tmp_path):
    """La garantía tiene que cubrir TODA operación SQLite, no sólo apertura y
    mutación: una lectura corre sobre una conexión CACHEADA y un `dispose()` de
    otra hebra le quita el respaldo por debajo."""
    import faulthandler, threading
    j = journal(tmp_path)
    s_ = sesion(j)
    j.accept_event(s_.token, idempotency_key="k", intent=INTENT, ledger="llminbox")
    dentro, orden = threading.Event(), []

    def lector():
        with j._lectura() as (con, _):
            dentro.set()
            orden.append("lectura-dentro")
            time.sleep(0.6)
            con.execute("SELECT COUNT(*) FROM events").fetchone()
            orden.append("lectura-fin")

    faulthandler.dump_traceback_later(30, exit=True)
    try:
        h = threading.Thread(target=lector); h.start()
        assert dentro.wait(20)
        with j._cerrojo_ciclo():          # EXCLUSIVO: tiene que ESPERAR
            orden.append("ciclo")
        h.join(20)
    finally:
        faulthandler.cancel_dump_traceback_later()
    assert orden.index("lectura-fin") < orden.index("ciclo"), (
        f"el ciclo de vida entró con una lectura viva: {orden}")
    j.dispose()


def test_dispose_con_el_lockfile_NO_ABRIBLE_falla_cerrado(tmp_path):
    """`opcional=True` hacía que soltara el respaldo SIN exclusión ninguna justo
    cuando no podía ni pedir el turno."""
    j = journal(tmp_path)
    sesion(j)
    real = os.open

    def open_falla(ruta, *a, **k):
        if isinstance(ruta, str) and ruta.endswith(".lifecycle"):
            raise OSError(30, "Read-only file system")
        return real(ruta, *a, **k)

    os.open = open_falla
    try:
        with pytest.raises(OSError):
            j.dispose()
    finally:
        os.open = real
    j.dispose()


def test_el_tmpdir_previo_no_se_deduce_del_finalizador(tmp_path):
    """`weakref.finalize` NO expone `.args` —`hasattr(f,'args') is False`—, así
    que el `getattr(..., (None,))[0]` devolvía SIEMPRE `None` y la copia vieja
    se quedaba en disco en el camino de ÉXITO. Un defecto MUDO: el `getattr`
    con defecto no falla, contesta."""
    import weakref as _wr
    assert not hasattr(_wr.finalize(lambda: None, print), "args"), (
        "si `finalize` expusiera `.args`, este test sobra")
    ruta = _base_nuestra(tmp_path)
    with _area_preflight_privada(tmp_path):
        j = C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
        j.initialize()
        primero = j._tmpdir
        with j._cerrojo_ciclo():
            j._refotografiar()                # ÉXITO: el primero debe irse
        segundo = j._tmpdir
        assert segundo != primero
        assert not os.path.exists(primero), "la copia vieja fugó en el camino bueno"
        j.dispose()


def test_la_degradacion_pendiente_SE_CONSUME_en_el_siguiente_initialize(tmp_path):
    """Escribir la bandera y no leerla nunca la convierte en un comentario con
    forma de estado. El `initialize()` posterior es el acto explícito que
    sustituye al upgrade SH→EX."""
    j = journal(tmp_path)
    sesion(j)
    j.close()
    real = sqlite3.connect
    objetivo = os.path.realpath(str(tmp_path / "coordination.sqlite"))

    def falla(destino, *a, **k):
        if isinstance(destino, str) and os.path.realpath(destino) == objetivo:
            raise sqlite3.OperationalError("attempt to write a readonly database")
        return real(destino, *a, **k)

    sqlite3.connect = falla
    try:
        with pytest.raises(C.JournalError):
            j._connect()
        assert j._degradacion_pendiente is True
    finally:
        sqlite3.connect = real
    j.initialize()                        # lo CONSUME
    assert j._degradacion_pendiente is False
    j.dispose()


def test_una_OPERACION_NORMAL_no_puede_activar_la_exencion_de_transicion(tmp_path):
    """La exención se reconoce por una conexión de inicialización DEDICADA, no
    por un estado. Un estado mutable —`READY` puesto a mano— lo hereda cualquier
    camino que lo deje puesto; una conexión que vive en un `finally` y sólo
    durante esa llamada, no."""
    j = journal(tmp_path)
    s_ = sesion(j)
    # En reposo, ninguna hebra tiene conexión de inicialización.
    assert getattr(j._local, "init_con", None) is None
    with j._tx() as con:
        assert getattr(j._local, "init_con", None) is None, (
            "una transacción normal activó la exención de la transición")
        con.execute("SELECT 1")
    j.accept_event(s_.token, idempotency_key="normal", intent=INTENT,
                   ledger="llminbox")
    assert getattr(j._local, "init_con", None) is None
    j.dispose()


def test_borrar_el_SELLO_tras_READY_no_permite_seguir_mutando(tmp_path):
    """La exención por valor leía «sin sello» como «estoy naciendo». La ausencia
    de sello no dice quién eres: dice que no hay sello."""
    j = journal(tmp_path)
    s_ = sesion(j)
    con = j._connect()
    antes = con.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    con.execute("DELETE FROM meta WHERE k='durable_v'")

    with pytest.raises(C.SchemaMismatch):
        j.accept_event(s_.token, idempotency_key="sin-sello", intent=INTENT,
                       ledger="llminbox")
    assert j._connect().execute(
        "SELECT COUNT(*) c FROM events").fetchone()["c"] == antes
    j.dispose()


def test_un_DOWNGRADE_del_sello_tras_READY_no_permite_seguir_mutando(tmp_path):
    """Bajar el sello a v2 por fuera no puede reabrir la escritura: la versión
    se exige DENTRO de la transacción que muta."""
    j = journal(tmp_path)
    s_ = sesion(j)
    con = j._connect()
    antes = con.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    con.execute("UPDATE meta SET v='2' WHERE k='durable_v'")

    with pytest.raises(C.SchemaMismatch):
        j.accept_event(s_.token, idempotency_key="v2", intent=INTENT,
                       ledger="llminbox")
    assert j._connect().execute(
        "SELECT COUNT(*) c FROM events").fetchone()["c"] == antes
    # ⊕ y una FUTURA sigue dando su propio tipo, que pide otra cosa.
    j._connect().execute(f"UPDATE meta SET v='{C.DURABLE_V + 1}' WHERE k='durable_v'")
    with pytest.raises(C.SchemaTooNew):
        j.accept_event(s_.token, idempotency_key="futura", intent=INTENT,
                       ledger="llminbox")
    j.dispose()


# ── M1-11 · SIETE FALSADORES DE CIERRE ─────────────────────────────────────

def test_dispose_CONCURRENTE_contra_initialize_no_deja_estado_a_medias(tmp_path):
    """Los dos toman el exclusivo, así que se ordenan. Lo que no puede pasar es
    que queden READY y respaldo soltado a la vez."""
    import threading
    ruta = _base_nuestra(tmp_path)
    j = C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
    j.initialize()
    fallos = []

    def ciclo(n):
        for _ in range(6):
            try:
                j.initialize()
                j.dispose()
            except C.JournalError:
                pass
            except Exception as e:
                fallos.append(f"{n}: {type(e).__name__}: {e}")

    hs = [threading.Thread(target=ciclo, args=(i,)) for i in range(4)]
    for h in hs: h.start()
    for h in hs: h.join(60)
    assert fallos == [], fallos
    # INVARIANTE: READY exige respaldo. Nunca lo uno sin lo otro.
    assert not (j._estado == C.Journal.READY and j._tmpdir is None), (
        "quedó READY con el respaldo soltado")


def test_el_solo_lectura_TRANSITORIO_es_coherente_consigo_mismo(tmp_path):
    """`health()` y el comportamiento real tienen que decir lo mismo: si dice
    que no se escribe, no se escribe; si dice que se lee, se lee."""
    import stat as _stat
    j = journal(tmp_path)
    s_ = sesion(j)
    ev = j.accept_event(s_.token, idempotency_key="k", intent=INTENT,
                        ledger="llminbox")
    j.close()
    for n in os.listdir(tmp_path):
        os.chmod(tmp_path / n, _stat.S_IRUSR)
    os.chmod(tmp_path, _stat.S_IRUSR | _stat.S_IXUSR)
    try:
        ro = C.Journal(str(tmp_path / "coordination.sqlite"), pepper=PEPPER,
                       lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
        ro.initialize()
        h = ro.health()
        assert h["writable"] is False
        assert h["estado_ciclo"] == C.Journal.READ_ONLY_READY
        # dice que lee -> lee
        assert ro.receipt_for_event(s_.token, ev.event_id)["current_state"] == "accepted"
        # dice que no escribe -> no escribe
        with pytest.raises(C.JournalError):
            ro.accept_event(s_.token, idempotency_key="no", intent=INTENT,
                            ledger="llminbox")
        ro.close()
    finally:
        os.chmod(tmp_path, _stat.S_IRWXU)
        for n in os.listdir(tmp_path):
            os.chmod(tmp_path / n, _stat.S_IRUSR | _stat.S_IWUSR)


def test_una_LECTURA_cacheada_corre_bajo_el_cerrojo_COMPARTIDO(tmp_path):
    """No basta con que la apertura lo tome: la lectura sobre la conexión ya
    cacheada tiene que estar dentro del compartido."""
    j = journal(tmp_path)
    s_ = sesion(j)
    j.accept_event(s_.token, idempotency_key="k", intent=INTENT, ledger="llminbox")
    j._connect()                                  # ya cacheada
    visto = {}
    with j._lectura() as (con, _):
        visto["escritor"] = getattr(j._local, "escritor", 0)
        con.execute("SELECT 1")
    assert visto["escritor"] >= 1, (
        "la lectura corrió sin el cerrojo compartido: un dispose ajeno podía "
        "quitarle el respaldo por debajo")
    j.dispose()


def test_pedir_el_EXCLUSIVO_con_el_COMPARTIDO_propio_no_se_cuelga(tmp_path):
    """Upgrade SH→EX en la misma hebra: `flock` se colgaría contra sí mismo.
    Error tipado e inmediato, con watchdog para que un cuelgue FALLE."""
    import faulthandler
    j = journal(tmp_path)
    faulthandler.dump_traceback_later(20, exit=True)
    try:
        with j._cerrojo_escritor():
            with pytest.raises(C.LifecycleConflict):
                with j._cerrojo_ciclo():
                    pass
    finally:
        faulthandler.cancel_dump_traceback_later()
    j.dispose()


def test_el_tmp_de_la_PRIMERA_clasificacion_siempre_se_limpia(tmp_path):
    """No sólo la re-foto: el primer preflight tiene el mismo camino, y si
    SQLite o `_clasificar` fallan tras la foto dejaba un temporal a medias."""
    ruta = _base_nuestra(tmp_path)
    real = C._clasificar

    def clasificar_roto(con):
        raise RuntimeError("rota en la PRIMERA clasificación")

    C._clasificar = clasificar_roto
    try:
        with _area_preflight_privada(tmp_path):
            j = C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
            with pytest.raises(Exception):
                j.initialize()
            j.dispose()
    finally:
        C._clasificar = real


def test_el_SHA_del_master_journal_copiado_se_VERIFICA(tmp_path):
    """Se copiaban los `-mj*` pero NO entraban en la comparación de la copia:
    tener el fichero y no tener el testigo. Fuente A / copia B / fuente A."""
    ruta = _base_nuestra(tmp_path)
    mj = ruta + "-mj0A1B2C3D"
    with open(mj, "wb") as fh:
        fh.write(b"A" * 32)
    original = C._copiar_a

    def copiar_y_corromper_solo_el_mj(base, destino):
        original(base, destino)
        d = os.path.join(destino, os.path.basename(base) + "-mj0A1B2C3D")
        if os.path.exists(d):
            with open(d, "wb") as fh:
                fh.write(b"B" * 32)          # la COPIA difiere; la fuente no

    C._copiar_a = copiar_y_corromper_solo_el_mj
    try:
        j = C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA)
        with pytest.raises(C.PreflightUnstable):
            j.initialize()
        j.dispose()
    finally:
        C._copiar_a = original
        if os.path.exists(mj):
            os.unlink(mj)


def test_un_DOBLE_fallo_en_exit_deja_ver_los_DOS(tmp_path):
    """Ni tapar el fallo de limpieza ni tapar el del cuerpo."""
    ruta = _base_nuestra(tmp_path)
    real = os.open

    def open_falla_en_el_lockfile(r, *a, **k):
        if isinstance(r, str) and r.endswith(".lifecycle"):
            raise OSError(30, "Read-only file system")
        return real(r, *a, **k)

    with pytest.raises(BaseExceptionGroup) as ei:
        with C.Journal(ruta, pepper=PEPPER, lane_ledgers=LANES, recipient_resolver=censo, grammar=GRAMATICA) as j:
            j.initialize()
            os.open = open_falla_en_el_lockfile
            raise ValueError("el error del CUERPO")
    os.open = real
    tipos = {type(e).__name__ for e in ei.value.exceptions}
    assert "ValueError" in tipos, f"se perdió el error del cuerpo: {tipos}"
    assert "OSError" in tipos, f"se perdió el fallo de limpieza: {tipos}"


def test_un_fallo_TRANSITORIO_de_escritura_no_deja_el_objeto_RO_para_siempre(tmp_path):
    """EL MISMO objeto. `_connect()` pone `_read_only=True` al fallar cerrado y
    NADIE lo bajaba: un fallo transitorio —el volumen se remonta, el NFS vuelve—
    dejaba esa instancia en sólo lectura de por vida, y `health()` lo decía como
    si fuera un hecho del disco.

    El falsador anterior no lo cazaba porque creaba OTRO `Journal` sobre
    permisos realmente RO: una instancia nueva nace limpia, así que medía el
    disco y no el estado pegado al objeto.
    """
    j = journal(tmp_path)
    s_ = sesion(j)
    ruta = str(tmp_path / "coordination.sqlite")
    real = sqlite3.connect
    objetivo = os.path.realpath(ruta)

    def falla(destino, *a, **k):
        if isinstance(destino, str) and os.path.realpath(destino) == objetivo:
            raise sqlite3.OperationalError("attempt to write a readonly database")
        return real(destino, *a, **k)

    j.close()
    sqlite3.connect = falla
    try:
        with pytest.raises(C.JournalError):
            j._connect()
        assert j._read_only is True and j._degradacion_pendiente is True
    finally:
        sqlite3.connect = real          # el fallo era TRANSITORIO

    j.initialize()                      # transición RW acreditada
    assert j._read_only is False, "quedó en sólo lectura tras un fallo pasajero"
    assert j.health()["writable"] is True
    assert j.accept_event(s_.token, idempotency_key="tras-transitorio",
                          intent=INTENT, ledger="llminbox").event_id
    j.dispose()
