"""La guarda fail-closed del arranque, con su control positivo y su mutante.

Cada comprobación se prueba en LAS DOS direcciones. El motivo no es simetría: una
guarda sólo probada por su camino bueno acredita que el proceso arranca cuando
todo está bien —que es lo que ya hacía sin guarda—. Lo que hay que acreditar es
que se pone ROJA, y por el motivo correcto.

`stat_fn` se inyecta porque la comprobación ⓐ compara DISPOSITIVOS, y montar un
volumen de verdad dentro de una prueba unitaria no es posible. **Eso es un hueco
declarado**: lo que aquí se acredita es la LÓGICA de la comparación, no que Docker
le dé al journal un `st_dev` distinto en esta plataforma. Esa mitad sólo la cierra
una instancia desplegada, y esta entrega no despliega.
"""
from __future__ import annotations

import hashlib
import json
import errno
import os
import pathlib
import signal
import sys

import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ / "tools"))

import pilot_preflight as pf  # noqa: E402

VOL_ID = "3f2a9c10-0000-4000-8000-000000000001"
LEDGER_ID = "3f2a9c10-0000-4000-8000-0000000000ff"


class _Stat:
    def __init__(self, dev): self.st_dev = dev


def stat_distinto(ruta):
    """⊕ el journal en su propio dispositivo (lo que hace un volumen montado)."""
    return _Stat(2 if ruta == "/" else 1)


def stat_igual(ruta):
    """⊖ journal y raíz en el mismo dispositivo (FS del contenedor)."""
    return _Stat(7)


@pytest.fixture()
def banco(tmp_path):
    """Monta un piloto entero y VÁLIDO en disco; cada prueba rompe UNA cosa."""
    journal_dir = tmp_path / "journal"; journal_dir.mkdir()
    huella_j = pf.comprobar_testigo(str(journal_dir), VOL_ID, init=True,
                                    ruta_journal=str(journal_dir / "coordination.sqlite"))

    pepper = tmp_path / "journal.pepper"
    pepper.write_bytes(b"x" * 48)
    pepper.chmod(0o600)

    mapa = {
        "cred-agente-piloto": {"principal": "agente-piloto", "rol": "infra",
                               "carril": "llminbox",
                               "capacidades": ["session", "events.write"]},
        "cred-lector": {"principal": "lector", "rol": "qa", "carril": "llminbox",
                        "capacidades": ["session"]},
    }
    ruta_mapa = tmp_path / "mapa.json"
    crudo = json.dumps(mapa, ensure_ascii=False).encode()
    ruta_mapa.write_bytes(crudo)

    ledgers = tmp_path / "ledgers"; ledgers.mkdir()
    piloto = ledgers / "llminbox"; piloto.mkdir()
    with open(piloto / pf.SENAL_LEDGER, "a", encoding="utf-8") as _fh:
        _fh.write("# fixture del carril piloto\n")
    huella_l = pf.comprobar_testigo(str(piloto), LEDGER_ID, init=True,
                                    nombre=pf.TESTIGO_LEDGER, que="ledger del piloto")
    testigo = ledgers / "testigo"; testigo.mkdir()
    testigo.chmod(0o555)

    entorno = {
        "LLMINBOX_JOURNAL": str(journal_dir / "coordination.sqlite"),
        "LLMINBOX_JOURNAL_VOLUME_ID": VOL_ID,
        "LLMINBOX_JOURNAL_VOLUME_WITNESS": huella_j,
        "LLMINBOX_LEDGER_PILOTO_ID": LEDGER_ID,
        "LLMINBOX_LEDGER_PILOTO_WITNESS": huella_l,
        "LLMINBOX_PEPPER_FILE": str(pepper),
        "LLMINBOX_CREDENCIALES": str(ruta_mapa),
        "LLMINBOX_CREDENCIALES_SHA": hashlib.sha256(crudo).hexdigest(),
        "LLMINBOX_LEDGER_PILOTO": str(piloto),
    }
    yield {"env": entorno, "tmp": tmp_path, "journal_dir": journal_dir,
           "pepper": pepper, "mapa": ruta_mapa, "ledgers": ledgers,
           "piloto": piloto, "testigo": testigo,
           "huella_j": huella_j, "huella_l": huella_l}
    testigo.chmod(0o755)          # que el tmp se pueda limpiar


def raiz_ledgers(banco):
    return str(banco["ledgers"])


def activar_projector(banco, *, worker_caps=None, operator_caps=None,
                      operator_lane="llminbox"):
    """Completa el fixture con las dos autoridades estrechas del modo active."""
    mapa = json.loads(banco["mapa"].read_text(encoding="utf-8"))
    mapa["cred-projector"] = {
        "principal": "pilot-projector", "rol": "projector",
        "carril": "llminbox",
        "capacidades": worker_caps or ["session", "outbox.project"],
    }
    mapa["cred-operator"] = {
        "principal": "pilot-operator", "rol": "infra",
        "carril": operator_lane,
        "capacidades": operator_caps or ["session", "admission.operate"],
    }
    raw = json.dumps(mapa, ensure_ascii=False).encode()
    banco["mapa"].write_bytes(raw)
    key = banco["tmp"] / "frame.key"
    key.write_bytes(b"f" * 48)
    key.chmod(0o600)
    banco["env"].update({
        "LLMINBOX_CREDENCIALES_SHA": hashlib.sha256(raw).hexdigest(),
        "LLMINBOX_PROJECTOR_MODE": "active",
        "LLMINBOX_PROJECTOR_PRINCIPAL_ID": "pilot-projector",
        "LLMINBOX_PROJECTOR_LEDGER": "llminbox",
        "LLMINBOX_PROJECTOR_FRAME_KEY_FILE": str(key),
    })
    return key


# ── ⊕ EL PILOTO COMPLETO Y SANO PASA ───────────────────────────────────────────

def test_preflight_completo_en_verde(banco):
    lineas = pf.preflight(banco["env"], stat_fn=stat_distinto,
                          raiz_ledgers=raiz_ledgers(banco))
    assert len(lineas) == 6
    assert "2 credencial(es)" in lineas[3]
    assert "disabled" in lineas[5]


def test_projector_active_completo_pasa_con_el_mismo_mapa_atestado(banco):
    activar_projector(banco)
    lineas = pf.preflight(banco["env"], stat_fn=stat_distinto,
                          raiz_ledgers=raiz_ledgers(banco))
    assert "active" in lineas[5]
    assert "48 bytes" in lineas[5]


@pytest.mark.parametrize("mode", ["ACTIVE", " active", "active ", ""])
def test_projector_mode_no_admite_normalizacion_ni_vocabulario_abierto(banco, mode):
    activar_projector(banco)
    banco["env"]["LLMINBOX_PROJECTOR_MODE"] = mode
    with pytest.raises(pf.Rojo, match="exacto"):
        pf.preflight(banco["env"], stat_fn=stat_distinto,
                     raiz_ledgers=raiz_ledgers(banco))


def test_projector_principal_debe_existir_una_vez(banco):
    activar_projector(banco)
    banco["env"]["LLMINBOX_PROJECTOR_PRINCIPAL_ID"] = "ausente"
    with pytest.raises(pf.Rojo, match="credencial exacta"):
        pf.preflight(banco["env"], stat_fn=stat_distinto,
                     raiz_ledgers=raiz_ledgers(banco))


def test_projector_worker_no_puede_acumular_capacidades(banco):
    activar_projector(
        banco, worker_caps=["session", "outbox.project", "events.write"])
    with pytest.raises(pf.Rojo, match="capacidades exactas"):
        pf.preflight(banco["env"], stat_fn=stat_distinto,
                     raiz_ledgers=raiz_ledgers(banco))


def test_projector_exige_un_operador_estrecho_del_mismo_carril(banco):
    activar_projector(banco, operator_lane="otro")
    with pytest.raises(pf.Rojo, match="mismo carril"):
        pf.preflight(banco["env"], stat_fn=stat_distinto,
                     raiz_ledgers=raiz_ledgers(banco))


def test_projector_ledger_debe_ser_el_piloto_montado(banco):
    activar_projector(banco)
    banco["env"]["LLMINBOX_PROJECTOR_LEDGER"] = "otro"
    with pytest.raises(pf.Rojo, match="ledger piloto"):
        pf.preflight(banco["env"], stat_fn=stat_distinto,
                     raiz_ledgers=raiz_ledgers(banco))


def test_projector_frame_key_es_fichero_0600_y_no_env(banco):
    key = activar_projector(banco)
    key.chmod(0o644)
    with pytest.raises(pf.Rojo, match="0600"):
        pf.preflight(banco["env"], stat_fn=stat_distinto,
                     raiz_ledgers=raiz_ledgers(banco))
    key.chmod(0o600)
    banco["env"]["LLMINBOX_PROJECTOR_FRAME_KEY"] = "no-se-loguea"
    with pytest.raises(pf.Rojo, match="por entorno") as caught:
        pf.preflight(banco["env"], stat_fn=stat_distinto,
                     raiz_ledgers=raiz_ledgers(banco))
    assert "no-se-loguea" not in str(caught.value)


def test_projector_frame_key_no_sigue_symlink(banco):
    key = activar_projector(banco)
    alias = banco["tmp"] / "frame-link"
    alias.symlink_to(key)
    banco["env"]["LLMINBOX_PROJECTOR_FRAME_KEY_FILE"] = str(alias)
    with pytest.raises(pf.Rojo, match="ENLACE"):
        pf.preflight(banco["env"], stat_fn=stat_distinto,
                     raiz_ledgers=raiz_ledgers(banco))


# ── ⓐ VOLUMEN DURABLE ──────────────────────────────────────────────────────────

def test_a_journal_en_el_fs_del_contenedor_es_rojo(banco):
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_volumen_durable(banco["env"]["LLMINBOX_JOURNAL"],
                                     stat_fn=stat_igual)
    assert "ⓐ" in str(e.value) and "recreate" in str(e.value)


def test_a_journal_en_volumen_propio_pasa(banco):
    assert pf.comprobar_volumen_durable(banco["env"]["LLMINBOX_JOURNAL"],
                                        stat_fn=stat_distinto)


# ── ⓑ TESTIGO DE IDENTIDAD DE VOLUMEN ──────────────────────────────────────────

def test_b_volumen_vacio_con_el_nombre_correcto_es_rojo(banco):
    """El resbalón del 04-09: volumen NUEVO, nombre correcto, cero eventos."""
    (banco["journal_dir"] / pf.TESTIGO).unlink()
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_testigo(str(banco["journal_dir"]), VOL_ID,
                             ruta_journal=banco["env"]["LLMINBOX_JOURNAL"])
    assert "VACÍO" in str(e.value)


def test_b_volumen_cruzado_es_rojo(banco, tmp_path):
    otro = tmp_path / "journal-otro"; otro.mkdir()
    pf.comprobar_testigo(str(otro), "id-de-otro-volumen", init=True,
                         ruta_journal=str(otro / "coordination.sqlite"))
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_testigo(str(otro), VOL_ID, huella_esperada=banco["huella_j"])
    assert "montaje cruzado" in str(e.value)


def test_b_sin_identidad_esperada_es_rojo(banco):
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_testigo(str(banco["journal_dir"]), "")
    assert "LLMINBOX_JOURNAL_VOLUME_ID" in str(e.value)


def test_b_init_estrena_un_volumen_vacio(banco, tmp_path):
    nuevo = tmp_path / "journal-nuevo"; nuevo.mkdir()
    huella = pf.comprobar_testigo(str(nuevo), VOL_ID, init=True,
                                  ruta_journal=str(nuevo / "coordination.sqlite"))
    assert (nuevo / pf.TESTIGO).exists()
    # y la segunda corrida, ya sin `--init`, tiene que seguir verde con SU huella
    assert pf.comprobar_testigo(str(nuevo), VOL_ID, huella_esperada=huella) == huella


def test_b_init_NO_adopta_un_journal_de_procedencia_desconocida(banco):
    """Un journal con datos y sin testigo no es un volumen estrenándose."""
    (banco["journal_dir"] / pf.TESTIGO).unlink()
    pathlib.Path(banco["env"]["LLMINBOX_JOURNAL"]).write_bytes(b"SQLite format 3\x00")
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_testigo(str(banco["journal_dir"]), VOL_ID, init=True,
                             ruta_journal=banco["env"]["LLMINBOX_JOURNAL"])
    assert "procedencia" in str(e.value)


# ── PEPPER ─────────────────────────────────────────────────────────────────────

def test_pepper_por_entorno_es_rojo(banco):
    env = dict(banco["env"], LLMINBOX_PEPPER="x" * 48)
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_pepper(env)
    assert "docker inspect" in str(e.value)


def test_pepper_corto_es_rojo(banco):
    banco["pepper"].write_bytes(b"corto")
    with pytest.raises(pf.Rojo):
        pf.comprobar_pepper(banco["env"])


def test_pepper_legible_por_otros_es_rojo(banco):
    banco["pepper"].chmod(0o644)
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_pepper(banco["env"])
    assert "legible fuera de su dueño" in str(e.value)


def test_pepper_ausente_es_rojo(banco):
    banco["pepper"].unlink()
    with pytest.raises(pf.Rojo):
        pf.comprobar_pepper(banco["env"])


def test_pepper_bueno_pasa(banco):
    assert pf.comprobar_pepper(banco["env"]) == 48


# ── MAPA DE CREDENCIALES: ATESTADO Y CAPACIDADES ───────────────────────────────

def test_mapa_sustituido_despues_del_atestado_es_rojo(banco):
    """El modo de fallo real: el fichero del host cambia y el arranque lo carga."""
    banco["mapa"].write_bytes(json.dumps({"otra": {"principal": "x", "rol": "y",
                                                   "carril": "z",
                                                   "capacidades": ["session"]}}).encode())
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_mapa(banco["env"])
    assert "NO es el atestado" in str(e.value)


def test_mapa_sin_sha_declarado_es_rojo(banco):
    env = dict(banco["env"]); env["LLMINBOX_CREDENCIALES_SHA"] = ""
    with pytest.raises(pf.Rojo):
        pf.comprobar_mapa(env)


def test_entrada_sin_capacidades_es_roja(banco):
    mapa = {"cred": {"principal": "p", "rol": "r", "carril": "llminbox"}}
    crudo = json.dumps(mapa).encode()
    banco["mapa"].write_bytes(crudo)
    env = dict(banco["env"], LLMINBOX_CREDENCIALES_SHA=hashlib.sha256(crudo).hexdigest())
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_mapa(env)
    assert "capacidades" in str(e.value)


def test_entrada_sin_session_es_roja(banco):
    mapa = {"cred": {"principal": "p", "rol": "r", "carril": "llminbox",
                     "capacidades": ["events.write"]}}
    crudo = json.dumps(mapa).encode()
    banco["mapa"].write_bytes(crudo)
    env = dict(banco["env"], LLMINBOX_CREDENCIALES_SHA=hashlib.sha256(crudo).hexdigest())
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_mapa(env)
    assert "session" in str(e.value)


def test_capacidad_desconocida_es_roja(banco):
    """Un typo no puede degradar a «sin capacidad» sin que nadie lo vea."""
    mapa = {"cred": {"principal": "p", "rol": "r", "carril": "llminbox",
                     "capacidades": ["events.writ"]}}
    crudo = json.dumps(mapa).encode()
    banco["mapa"].write_bytes(crudo)
    env = dict(banco["env"], LLMINBOX_CREDENCIALES_SHA=hashlib.sha256(crudo).hexdigest())
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_mapa(env)
    assert "desconocidas" in str(e.value)


def test_mapa_bueno_pasa(banco):
    assert len(pf.comprobar_mapa(banco["env"])) == 2


def test_el_mensaje_del_mapa_no_imprime_la_credencial(banco):
    """El mapa se indexa POR LA CREDENCIAL EN CLARO. Un mensaje de error que la
    escupa la filtra al log del contenedor, que es donde nadie la busca."""
    mapa = {"credencial-secretisima-12345": {"principal": "p", "rol": "r",
                                             "carril": "llminbox"}}
    crudo = json.dumps(mapa).encode()
    banco["mapa"].write_bytes(crudo)
    env = dict(banco["env"], LLMINBOX_CREDENCIALES_SHA=hashlib.sha256(crudo).hexdigest())
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_mapa(env)
    assert "credencial-secretisima" not in str(e.value)


# ── LEDGERS: UNO SOLO ESCRIBIBLE ───────────────────────────────────────────────

def test_dos_ledgers_escribibles_es_rojo(banco):
    banco["testigo"].chmod(0o755)
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_ledgers(banco["env"], raiz_ledgers=raiz_ledgers(banco))
    assert "radio de daño" in str(e.value)


def test_un_solo_ledger_escribible_pasa(banco):
    assert pf.comprobar_ledgers(banco["env"],
                                raiz_ledgers=raiz_ledgers(banco)) == str(banco["piloto"])


def test_journal_sin_ruta_es_rojo(banco):
    env = dict(banco["env"]); env["LLMINBOX_JOURNAL"] = ""
    with pytest.raises(pf.Rojo) as e:
        pf.preflight(env, stat_fn=stat_distinto, raiz_ledgers=raiz_ledgers(banco))
    assert "LLMINBOX_JOURNAL" in str(e.value)


# ══════════════════════════════════════════════════════════════════════════════
# CORRECTIVA sobre @security (`19:56:36Z`) — P1-1 · P1-2 · P2-1 · P2-2 · P2-3
# y el apunte de diseño: un readiness rojo tiene que CORTAR, no sólo avisar.
# ══════════════════════════════════════════════════════════════════════════════

def test_P1_1_ledger_arbitrario_montado_como_el_del_piloto_es_ROJO(banco):
    """A3 de @security: `comprobar_ledgers` contaba CUÁNTOS eran escribibles y
    nunca CUÁL. `LLMINBOX_PILOT_LEDGER_HOST` llega por entorno y nadie lo validaba:
    cualquier directorio montado en su sitio pasaba en verde. Asimetría con el
    journal, que sí tenía testigo."""
    (banco["piloto"] / pf.TESTIGO_LEDGER).unlink()      # un directorio cualquiera
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_ledgers(banco["env"], raiz_ledgers=raiz_ledgers(banco))
    assert "testigo" in str(e.value).lower()


def test_P1_1_ledger_con_el_testigo_de_OTRO_es_ROJO(banco):
    (banco["piloto"] / pf.TESTIGO_LEDGER).chmod(0o644)
    (banco["piloto"] / pf.TESTIGO_LEDGER).write_text('{"id": "otro-ledger"}',
                                                     encoding="utf-8")
    with pytest.raises(pf.Rojo):
        pf.comprobar_ledgers(banco["env"], raiz_ledgers=raiz_ledgers(banco))


def test_P1_1_el_ledger_del_piloto_con_SU_testigo_pasa(banco):
    assert pf.comprobar_ledgers(banco["env"], raiz_ledgers=raiz_ledgers(banco)) \
        == str(banco["piloto"])


def test_P1_2_el_error_NO_lleva_ningun_fragmento_de_la_credencial(banco):
    """REGRESIÓN de `15dc61a`, que ya quitó esto de `servicio.py` cambiando
    `credencial {cred[:8]}…` por «entrada de credencial en posición n». El fichero
    nuevo lo reintrodujo por el otro extremo (`cred[-4:]`), y el mensaje sale por
    `stderr` ⇒ acaba en `docker logs`.

    El falsador mira TODAS las ventanas de 4 caracteres de la credencial, no sólo
    la cola: un detector que sólo buscara el sufijo aprobaría un cambio a prefijo.
    """
    # Opaca A PROPÓSITO: mi primera versión usaba «credencial-secretisima-…», que
    # comparte la palabra «credencial» con el mensaje LEGÍTIMO («entrada de
    # credencial en posición 1») y hacía saltar el detector sobre un mensaje
    # limpio. El defecto era del falsador, no del código: una aguja que es
    # subcadena de algo también presente no da cero, da un positivo plausible.
    secreta = "Zq7xK2p9W4m1B8t6R3v5Yh0jL"
    mapa = {secreta: {"principal": "p", "rol": "r", "carril": "llminbox"}}
    crudo = json.dumps(mapa).encode()
    banco["mapa"].write_bytes(crudo)
    env = dict(banco["env"], LLMINBOX_CREDENCIALES_SHA=hashlib.sha256(crudo).hexdigest())
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_mapa(env)
    msg = str(e.value)
    ventanas = [secreta[i:i + 4] for i in range(len(secreta) - 3)]
    filtradas = [v for v in ventanas if v in msg]
    assert not filtradas, f"el mensaje filtra {filtradas} de la credencial: {msg}"
    assert "posición" in msg, "hay que numerar la entrada, como hizo 15dc61a"


def test_P2_1_dos_volumenes_estrenados_con_el_MISMO_id_no_pueden_estar_los_dos_verdes(banco, tmp_path):
    """A4 de @security: el testigo repetía el valor del entorno, así que dos
    volúmenes `--init` con el mismo `VOLUME_ID` salían los dos verdes. Acreditaba
    «alguien escribió aquí el id esperado», no «éste es el volumen que siempre lo
    fue» — y rompía la detección de montaje cruzado justo en el split-brain."""
    a = banco["journal_dir"]
    (a / pf.TESTIGO).unlink()
    b = tmp_path / "journal-B"; b.mkdir()

    huella_a = pf.comprobar_testigo(str(a), VOL_ID, init=True,
                                    ruta_journal=str(a / "coordination.sqlite"))
    huella_b = pf.comprobar_testigo(str(b), VOL_ID, init=True,
                                    ruta_journal=str(b / "coordination.sqlite"))
    assert huella_a != huella_b, "dos estrenos con el mismo id dan la misma huella"

    # Con la huella de A declarada, A pasa y B es ROJO. No los dos.
    assert pf.comprobar_testigo(str(a), VOL_ID, huella_esperada=huella_a) == huella_a
    with pytest.raises(pf.Rojo):
        pf.comprobar_testigo(str(b), VOL_ID, huella_esperada=huella_a)


def test_P2_1_sin_huella_declarada_es_ROJO_no_permisivo(banco):
    """El límite se declara FAIL-CLOSED: si el operador no fija la huella, no se
    degrada a «comparo sólo el id» — se para."""
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_testigo(str(banco["journal_dir"]), VOL_ID, huella_esperada="")
    assert "huella" in str(e.value).lower()


def test_P2_2_pepper_por_enlace_simbolico_es_ROJO(banco, tmp_path):
    """A5 de @security: `os.path.isfile` SIGUE enlaces y el modo comprobado era el
    del DESTINO. Un enlace a un fichero de 40 bytes en 0400 pasaba."""
    real = tmp_path / "otro.pepper"
    real.write_bytes(b"y" * 40)
    real.chmod(0o400)
    enlace = tmp_path / "enlace.pepper"
    enlace.symlink_to(real)
    env = dict(banco["env"], LLMINBOX_PEPPER_FILE=str(enlace))
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_pepper(env)
    assert "enlace" in str(e.value).lower()


def test_P2_3_pepper_ilegible_da_Rojo_con_nombre_y_no_un_traceback(banco):
    """A7 de @security: los `open()` iban sin guardar ⇒ `PermissionError` crudo.
    Contradice el docstring de `Rojo`: «un fail-closed mudo entrena a quien lo
    sufre a saltárselo»."""
    banco["pepper"].chmod(0o000)
    try:
        with pytest.raises(pf.Rojo) as e:
            pf.comprobar_pepper(banco["env"])
        assert "no se puede leer" in str(e.value).lower()
    finally:
        banco["pepper"].chmod(0o600)


def test_P2_3_mapa_ilegible_da_Rojo(banco):
    banco["mapa"].chmod(0o000)
    try:
        with pytest.raises(pf.Rojo):
            pf.comprobar_mapa(banco["env"])
    finally:
        banco["mapa"].chmod(0o644)


def test_P2_3_testigo_ilegible_da_Rojo(banco):
    banco["journal_dir"].joinpath(pf.TESTIGO).chmod(0o000)
    try:
        with pytest.raises(pf.Rojo):
            pf.comprobar_testigo(str(banco["journal_dir"]), VOL_ID,
                                 huella_esperada="da-igual")
    finally:
        banco["journal_dir"].joinpath(pf.TESTIGO).chmod(0o444)


# ── El readiness deja de ser advisory ──────────────────────────────────────────

def test_readiness_ROJO_corta_el_proceso(banco):
    """@security, apunte de diseño: Docker NO mata un contenedor `unhealthy`. Con
    `restart: "no"`, un readiness rojo dejaba el servicio contestando con la
    etiqueta puesta — fail-closed en el arranque y advisory después."""
    matados = []
    env = dict(banco["env"]); env["LLMINBOX_JOURNAL"] = ""
    rc = pf.readiness(env, stat_fn=stat_distinto, raiz_ledgers=raiz_ledgers(banco),
                      kill_fn=lambda pid, sig: matados.append((pid, sig)),
                      acreditar_fn=lambda e: (True, "contenedor simulado"))
    assert rc == 1
    assert matados and matados[0][0] == 1, "un readiness rojo tiene que cortar el PID 1"


def test_readiness_VERDE_no_corta_nada(banco):
    """⊕ del mismo instrumento: si cortara siempre, el de arriba no probaría nada."""
    matados = []
    rc = pf.readiness(banco["env"], stat_fn=stat_distinto,
                      raiz_ledgers=raiz_ledgers(banco),
                      kill_fn=lambda pid, sig: matados.append((pid, sig)),
                      acreditar_fn=lambda e: (True, "contenedor simulado"))
    assert rc == 0 and matados == []


# ══════════════════════════════════════════════════════════════════════════════
# 2ª CORRECTIVA · ⑤ de @sdet: «son DOS valores que sólo existen tras un `--init`
# que NINGÚN fichero invoca. El fail-closed es más fuerte y el camino para
# estrenarlo sigue sin estar escrito.»
# ══════════════════════════════════════════════════════════════════════════════

def _sin_testigos(banco):
    (banco["journal_dir"] / pf.TESTIGO).unlink()
    (banco["piloto"] / pf.TESTIGO_LEDGER).chmod(0o644)
    (banco["piloto"] / pf.TESTIGO_LEDGER).unlink()


def test_SDET_5_el_estreno_crea_LOS_DOS_testigos(banco):
    _sin_testigos(banco)
    salida = pf.estrenar(banco["env"], raiz_ledgers=raiz_ledgers(banco),
                         stat_fn=stat_distinto)
    assert set(salida) == {"LLMINBOX_JOURNAL_VOLUME_WITNESS",
                           "LLMINBOX_LEDGER_PILOTO_WITNESS"}
    assert (banco["journal_dir"] / pf.TESTIGO).exists()
    assert (banco["piloto"] / pf.TESTIGO_LEDGER).exists()
    assert all(len(v) == 64 for v in salida.values()), "las huellas son sha256"


def test_SDET_5_el_estreno_es_IDEMPOTENTE(banco):
    """Correrlo dos veces no cambia nada y devuelve LO MISMO. Un camino de estreno
    que sólo vale la primera vez obliga a saber si ya se corrió, y eso es
    exactamente el dato que el operador no tiene delante."""
    _sin_testigos(banco)
    a = pf.estrenar(banco["env"], raiz_ledgers=raiz_ledgers(banco), stat_fn=stat_distinto)
    b = pf.estrenar(banco["env"], raiz_ledgers=raiz_ledgers(banco), stat_fn=stat_distinto)
    assert a == b


def test_SDET_5_tras_el_estreno_el_arranque_pasa_con_esas_huellas(banco):
    """⊕ que cierra el lazo: lo que el estreno IMPRIME es exactamente lo que el
    arranque EXIGE. Sin este control, el estreno podría emitir un valor que el
    preflight no acepta y los dos estarían «bien» por separado."""
    _sin_testigos(banco)
    salida = pf.estrenar(banco["env"], raiz_ledgers=raiz_ledgers(banco), stat_fn=stat_distinto)
    env = dict(banco["env"], **salida)
    assert len(pf.preflight(env, stat_fn=stat_distinto,
                            raiz_ledgers=raiz_ledgers(banco))) == 6


def test_SDET_5_SIN_estreno_el_arranque_falla_CERRADO_por_el_journal(banco):
    _sin_testigos(banco)
    with pytest.raises(pf.Rojo) as e:
        pf.preflight(banco["env"], stat_fn=stat_distinto,
                     raiz_ledgers=raiz_ledgers(banco))
    assert "ⓑ" in str(e.value) and "VACÍO" in str(e.value)


def test_SDET_5_SIN_estreno_del_LEDGER_tambien_falla_CERRADO(banco):
    """El journal estrenado NO tapa al ledger sin estrenar: son dos, y el segundo
    llegó con la correctiva anterior sin camino propio."""
    (banco["piloto"] / pf.TESTIGO_LEDGER).chmod(0o644)
    (banco["piloto"] / pf.TESTIGO_LEDGER).unlink()
    with pytest.raises(pf.Rojo) as e:
        pf.preflight(banco["env"], stat_fn=stat_distinto,
                     raiz_ledgers=raiz_ledgers(banco))
    assert "ledger del piloto" in str(e.value)


def test_SDET_5_el_estreno_NO_adopta_un_journal_con_datos(banco):
    """Mismo criterio que `--init`: estrenar no es adoptar."""
    _sin_testigos(banco)
    pathlib.Path(banco["env"]["LLMINBOX_JOURNAL"]).write_bytes(b"SQLite format 3\x00")
    with pytest.raises(pf.Rojo) as e:
        pf.estrenar(banco["env"], raiz_ledgers=raiz_ledgers(banco), stat_fn=stat_distinto)
    assert "procedencia" in str(e.value)


def test_SDET_5_el_estreno_exige_el_volumen_durable(banco):
    """⊖ del propio estreno: si ⓐ no se comprueba antes de escribir el testigo, se
    estrena el FS del contenedor y el testigo se evapora en el primer recreate —
    dejando un `_WITNESS` en el entorno que ya no casa con nada."""
    _sin_testigos(banco)
    with pytest.raises(pf.Rojo) as e:
        pf.estrenar(banco["env"], raiz_ledgers=raiz_ledgers(banco), stat_fn=stat_igual)
    assert "ⓐ" in str(e.value)


# ── Falsadores propios para guardas que sólo tenían diagnóstico compartido ─────

def test_pepper_que_no_es_fichero_regular_es_ROJO(banco, tmp_path):
    """`S_ISREG` llegó en la correctiva anterior sin falsador propio: sobrevivía a
    su mutante porque el camino lo cubría otra guarda."""
    fifo = tmp_path / "pepper.fifo"
    os.mkfifo(fifo)
    env = dict(banco["env"], LLMINBOX_PEPPER_FILE=str(fifo))
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_pepper(env)
    assert "regular" in str(e.value) or "no se puede leer" in str(e.value)


def test_mapa_que_es_una_LISTA_es_ROJO(banco):
    crudo = json.dumps([{"principal": "p"}]).encode()
    banco["mapa"].write_bytes(crudo)
    env = dict(banco["env"], LLMINBOX_CREDENCIALES_SHA=hashlib.sha256(crudo).hexdigest())
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_mapa(env)
    assert "objeto" in str(e.value)


def test_capacidades_que_no_son_una_lista_es_ROJO(banco):
    crudo = json.dumps({"c": {"principal": "p", "rol": "r", "carril": "l",
                              "capacidades": "session"}}).encode()
    banco["mapa"].write_bytes(crudo)
    env = dict(banco["env"], LLMINBOX_CREDENCIALES_SHA=hashlib.sha256(crudo).hexdigest())
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_mapa(env)
    assert "no es una lista" in str(e.value)


def test_SEC_el_estreno_NO_acuna_un_directorio_que_no_es_el_ledger(banco, tmp_path):
    """P2 de @security (falsador `B1`): `estrenar()` corría sólo ⓐ, que acota el
    JOURNAL, y acuñaba las dos huellas para CUALQUIER directorio. El testigo del
    ledger acreditaba «éste es el directorio que se estrenó», no «éste es el ledger
    del carril piloto» ⇒ un `LLMINBOX_PILOT_LEDGER_HOST` mal puesto **la primera
    vez** se adopta para siempre y todo arranque posterior sale verde."""
    _sin_testigos(banco)
    otro = tmp_path / "no-soy-un-ledger"; otro.mkdir()
    env = dict(banco["env"], LLMINBOX_LEDGER_PILOTO=str(otro))
    with pytest.raises(pf.Rojo) as e:
        pf.estrenar(env, raiz_ledgers=raiz_ledgers(banco), stat_fn=stat_distinto)
    # 🔻 RE-ANCLADO por el cierre del auditor: este directorio vive FUERA de la
    # raíz autorizada, así que ahora muere ANTES y por un motivo más fuerte —«no
    # es hijo DIRECTO de la raíz»— sin llegar siquiera a mirar la señal. El caso
    # de la señal, con un hijo VÁLIDO, tiene ahora sus propios tests abajo.
    assert "hijo DIRECTO" in str(e.value)
    assert not (otro / pf.TESTIGO_LEDGER).exists(), "acuñó el testigo igualmente"


def test_SEC_el_estreno_SI_acuna_el_ledger_de_verdad(banco):
    """⊕ del mismo detector: con su señal, el estreno sigue funcionando. Sin este
    control, exigirla podría haber roto el camino bueno sin que nadie lo viera."""
    _sin_testigos(banco)
    salida = pf.estrenar(banco["env"], raiz_ledgers=raiz_ledgers(banco),
                         stat_fn=stat_distinto)
    assert len(salida) == 2


# ── AUDITORÍA INDEPENDIENTE · supervivientes ④ y ⑤, y el ① por dentro ─────────

def test_S4_un_testigo_que_es_ENLACE_a_fuera_es_ROJO(banco, tmp_path):
    """Superviviente ④: el testigo se abría por RUTA. Un enlace simbólico a un
    fichero de fuera con el contenido correcto da la huella exacta y pasa — y
    entonces la identidad del volumen la decide quien puso el enlace, no el
    volumen. Es el mismo agujero que ya se cerró en el pepper, en el otro fichero."""
    real = tmp_path / "testigo-de-otro"
    real.write_bytes((banco["journal_dir"] / pf.TESTIGO).read_bytes())
    (banco["journal_dir"] / pf.TESTIGO).unlink()
    (banco["journal_dir"] / pf.TESTIGO).symlink_to(real)
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_testigo(str(banco["journal_dir"]), VOL_ID,
                             huella_esperada=banco["huella_j"])
    assert "enlace" in str(e.value).lower()


def test_S5_un_testigo_LEGACY_sin_nonce_es_ROJO(banco):
    """Superviviente ⑤: `--init` (y el arranque) sólo leían `datos["id"]`, así que
    un `{"id": "<vol>"}` a mano pasaba — sin `v`, sin `nonce`, sin `nacido`. El
    `nonce` es LO ÚNICO que impide que dos volúmenes estrenados con el mismo id
    estén los dos verdes; un testigo sin él no acredita ningún estreno."""
    ruta = banco["journal_dir"] / pf.TESTIGO
    ruta.chmod(0o644)
    ruta.write_text(json.dumps({"id": VOL_ID}), encoding="utf-8")
    ruta.chmod(0o444)          # el testigo real es 0444 y eso se comprueba antes
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_testigo(str(banco["journal_dir"]), VOL_ID, init=True,
                             ruta_journal=banco["env"]["LLMINBOX_JOURNAL"])
    # Muere en el PRIMER campo que falta (`v`), y da igual cuál sea: lo que se
    # exige es que un testigo sin esquema no acredite un estreno.
    assert any(x in str(e.value) for x in ("v=2", "nonce", "formato"))


def test_S5_un_testigo_con_nonce_FALSO_es_ROJO(banco):
    """Y el nonce tiene que PARECER un nonce: `"nonce": "x"` no es un estreno, es
    un campo rellenado para pasar la comprobación de presencia."""
    ruta = banco["journal_dir"] / pf.TESTIGO
    ruta.chmod(0o644)
    ruta.write_text(json.dumps({"v": pf.TESTIGO_V, "id": VOL_ID, "nonce": "x",
                                "nacido": "2026-01-01T00:00:00+00:00"}), encoding="utf-8")
    ruta.chmod(0o444)
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_testigo(str(banco["journal_dir"]), VOL_ID, huella_esperada="x" * 64)
    assert "nonce" in str(e.value)


def test_S5_el_testigo_BUENO_sigue_pasando(banco):
    """⊕: el que escribe el propio estreno cumple el esquema completo."""
    assert pf.comprobar_testigo(str(banco["journal_dir"]), VOL_ID,
                                huella_esperada=banco["huella_j"]) == banco["huella_j"]


def test_S1_un_testigo_que_es_su_PROPIO_montaje_es_ROJO(banco, monkeypatch):
    """Superviviente ① por dentro: el overmount se ve desde el proceso porque el
    fichero montado encima vive en OTRO dispositivo que su directorio. La guarda
    estática (`I12`) mira el compose; ésta mira la corrida, y hacen falta las dos —
    un `docker run` a mano no pasa por el compose.

    Se falsea el PRIMER `fstat`, que es el del testigo: desde que la lectura va
    anclada al descriptor de directorio, no hay ninguna ruta que interceptar, y eso
    es justo la propiedad que cierra el TOCTOU.
    """
    real = os.fstat
    n = {"i": 0}

    class _St:
        def __init__(self, st, dev): self._st = st; self.st_dev = dev
        def __getattr__(self, k): return getattr(self._st, k)

    def falso(fd):
        st = real(fd)
        n["i"] += 1
        return _St(st, 999) if n["i"] == 1 else st

    monkeypatch.setattr(os, "fstat", falso)
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_testigo(str(banco["journal_dir"]), VOL_ID,
                             huella_esperada=banco["huella_j"])
    assert "montado ENCIMA" in str(e.value)

def test_S5_un_testigo_con_OTRA_version_es_ROJO(banco):
    """Falsador propio del campo `v`, separado del `nonce`.

    🩸 Nació de un mutante que SOBREVIVIÓ: al desactivar la comprobación de `v`, el
    testigo legacy seguía muriendo **por el nonce**, así que la guarda de versión no
    tenía quien la vigilara. Un testigo con nonce y fecha válidos pero otra versión
    sólo lo caza ella."""
    ruta = banco["journal_dir"] / pf.TESTIGO
    ruta.chmod(0o644)
    ruta.write_text(json.dumps({"v": 1, "id": VOL_ID, "nonce": "a" * 32,
                                "nacido": "2026-01-01T00:00:00+00:00"}),
                    encoding="utf-8")
    ruta.chmod(0o444)
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_testigo(str(banco["journal_dir"]), VOL_ID, huella_esperada="z" * 64)
    assert "v=2" in str(e.value)


# ══════════════════════════════════════════════════════════════════════════════
# NO-GO independiente sobre `e21ed00`/`4b98811` — el estreno escapaba del volumen.
# ══════════════════════════════════════════════════════════════════════════════

def test_NOGO_init_con_enlace_COLGANTE_no_crea_nada_FUERA(banco, tmp_path):
    """El defecto, reproducido antes de curarlo: `--init` hacía `os.path.exists()`
    y luego `open(ruta, "wb")`. **`exists()` SIGUE el enlace**, así que sobre un
    `.volume-id` que apunta a un fichero inexistente devolvía `False` —«no hay
    testigo, estrénalo»— y el `open` **seguía el enlace y creaba el objetivo FUERA
    del volumen**, en `0444`, con el enlace intacto. `--init` devolvía huella como
    si hubiera estrenado, y toda la guarda ⓑ quedaba desactivada desde el minuto
    cero: el testigo del volumen vivía en otro sitio.

    🟡 CAVEAT que hay que leer antes de contar este test como el falsador de
    `O_NOFOLLOW`, y que NO es mío: lo midió **@qa** en su auditoría de `49379af`
    («sigue verde sin `O_NOFOLLOW`: lo sostiene `O_EXCL`, que sobre un enlace
    colgante falla con `EEXIST`»). Tiene razón y lo dejo escrito aquí, que es
    donde se lee: **este test acredita el EFECTO —no se crea nada fuera— y no la
    BANDERA.** Quien vigila `O_NOFOLLOW` es
    `test_P2_2_pepper_por_enlace_simbolico_es_ROJO`, y desde esta correctiva
    también `test_CORR_P1a_el_directorio_del_volumen_que_es_ENLACE_es_ROJO`, que
    muere si se le quita al `openat` del DIRECTORIO."""
    vol = tmp_path / "journal-colgante"; vol.mkdir()
    fuera = tmp_path / "FUERA-del-volumen"
    (vol / pf.TESTIGO).symlink_to(fuera)
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_testigo(str(vol), "vol-A", init=True,
                             ruta_journal=str(vol / "coordination.sqlite"))
    assert "enlace" in str(e.value).lower()
    assert not fuera.exists(), "creó el testigo FUERA del volumen"
    assert (vol / pf.TESTIGO).is_symlink(), "el enlace debería seguir donde estaba"


def test_NOGO_carrera_entre_comprobar_y_crear_es_ROJA(banco, tmp_path, monkeypatch):
    """TOCTOU. Entre «no hay testigo» y «lo creo» cabe otro proceso —o alguien—
    poniendo uno. Sin `O_EXCL`, el segundo estreno pisaría al primero y los dos
    creerían haberlo puesto. Se simula de forma determinista: el impostor escribe
    justo antes de nuestra llamada de creación, y la creación tiene que fallar."""
    vol = tmp_path / "journal-carrera"; vol.mkdir()
    real_open = os.open
    disparado = {"n": 0}

    def con_impostor(path, flags, *a, **k):
        if flags & os.O_CREAT and disparado["n"] == 0:
            disparado["n"] = 1
            # El impostor gana la carrera por un microsegundo.
            with open(vol / pf.TESTIGO, "w", encoding="utf-8") as fh:
                fh.write('{"v": 2, "id": "del-impostor"}')
        return real_open(path, flags, *a, **k)

    monkeypatch.setattr(os, "open", con_impostor)
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_testigo(str(vol), "vol-A", init=True,
                             ruta_journal=str(vol / "coordination.sqlite"))
    assert disparado["n"] == 1, "el impostor no llegó a correr: la prueba no mide nada"
    assert "ya existe" in str(e.value) or "carrera" in str(e.value).lower()


def test_NOGO_nacido_que_no_es_un_TIMESTAMP_es_ROJO(banco):
    """P2: `nacido` sólo se comprobaba como no vacío. `"ayer"` pasaba, y entonces
    el campo no fecha nada — es decoración con aspecto de dato."""
    ruta = banco["journal_dir"] / pf.TESTIGO
    ruta.chmod(0o644)
    ruta.write_text(json.dumps({"v": pf.TESTIGO_V, "id": VOL_ID, "nonce": "a" * 32,
                                "nacido": "ayer por la tarde"}), encoding="utf-8")
    ruta.chmod(0o444)
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_testigo(str(banco["journal_dir"]), VOL_ID, huella_esperada="z" * 64)
    assert "nacido" in str(e.value)


def test_NOGO_el_testigo_escribible_es_ROJO(banco):
    """P2: el testigo se crea `0444` y nadie comprobaba que siguiera así. Uno
    escribible es uno que se puede cambiar sin dejar rastro entre dos arranques."""
    ruta = banco["journal_dir"] / pf.TESTIGO
    ruta.chmod(0o644)
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_testigo(str(banco["journal_dir"]), VOL_ID,
                             huella_esperada=banco["huella_j"])
    assert "0444" in str(e.value) or "escribible" in str(e.value)


def test_NOGO_un_DIRECTORIO_en_lugar_del_testigo_es_ROJO(banco, tmp_path):
    """Un bind-mount de un fichero ausente Docker lo sustituye por un DIRECTORIO —
    cicatriz escrita en el compose de la flota. Aquí eso tiene que ser Rojo con
    nombre, no un `EISDIR` crudo."""
    vol = tmp_path / "journal-dir"; vol.mkdir()
    (vol / pf.TESTIGO).mkdir()
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_testigo(str(vol), "vol-A", huella_esperada="z" * 64)
    assert "regular" in str(e.value) or "directorio" in str(e.value)


def test_NOGO_el_estreno_bueno_SIGUE_funcionando(banco, tmp_path):
    """⊕ imprescindible: endurecer el estreno no puede romperlo. Crea, es `0444`,
    y la segunda corrida devuelve la misma huella."""
    vol = tmp_path / "journal-sano"; vol.mkdir()
    h1 = pf.comprobar_testigo(str(vol), "vol-A", init=True,
                              ruta_journal=str(vol / "coordination.sqlite"))
    assert len(h1) == 64
    assert oct(os.stat(vol / pf.TESTIGO).st_mode & 0o777) == "0o444"
    assert pf.comprobar_testigo(str(vol), "vol-A", init=True,
                                ruta_journal=str(vol / "coordination.sqlite")) == h1


# ══════════════════════════════════════════════════════════════════════════════
# 3ª CORRECTIVA — los cinco de la auditoría sobre `49379af`. `49379af` puso
# `O_NOFOLLOW` en el TESTIGO y dejó sin él el descriptor del DIRECTORIO; puso el
# `fsync` del directorio y le colgó un `except OSError: pass`, que es la línea
# que lo desactiva. Los cinco se reprodujeron ANTES de tocar nada.
# ══════════════════════════════════════════════════════════════════════════════

# ── P1-a · el descriptor de directorio se anclaba FUERA ────────────────────────

def test_CORR_P1a_el_directorio_del_volumen_que_es_ENLACE_es_ROJO(tmp_path):
    """El mismo defecto de `49379af` un nivel más arriba: allí el estreno escapaba
    por el FICHERO, aquí por la CARPETA. `os.open(directorio, O_RDONLY|O_DIRECTORY)`
    sin `O_NOFOLLOW` sigue el enlace, el descriptor queda anclado fuera del
    almacén, y todo lo que cuelga de él —`O_EXCL`, `fstat`, `fsync`, la lectura—
    se hace en el sitio equivocado CON LAS BANDERAS CORRECTAS. Es la peor forma:
    todas las guardas verdes, sobre otro directorio.

    Medido antes de curar: `OUTSIDE_CREATED=True`, `INSIDE_VOL=False`, y `--init`
    devolviendo huella. **Este test SÍ discrimina la bandera**: quítale
    `O_NOFOLLOW` a `_abrir_directorio` y se pone rojo.
    """
    volumen = tmp_path / "volumen-real"; volumen.mkdir()
    fuera = tmp_path / "FUERA-del-volumen"; fuera.mkdir()
    punto = tmp_path / "punto-de-montaje"; punto.symlink_to(fuera)
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_testigo(str(punto), "vol-A", init=True,
                             ruta_journal=str(punto / "coordination.sqlite"))
    assert "enlace" in str(e.value).lower()
    assert not (fuera / pf.TESTIGO).exists(), "estrenó FUERA del volumen"
    assert not (volumen / pf.TESTIGO).exists()
    assert punto.is_symlink(), "el enlace debería seguir donde estaba"


def test_CORR_P1e_un_componente_INTERMEDIO_enlace_es_ROJO(tmp_path):
    """🩸 La mitad que el test de arriba NO cubría, y estaba DECLARADA como límite
    abierto en el propio docstring de `_abrir_directorio`.

    `O_NOFOLLOW` en UN `os.open` gobierna el ÚLTIMO componente; los de en medio los
    resuelve el kernel en silencio. Medido por @security (H1, Linux, volumen Docker
    real): último componente enlace ⇒ `OSError`; componente INTERMEDIO ⇒ **ABRE**.
    El activador real es una ruta configurada un nivel más abajo (`/journal/sub/…`),
    donde el enlace vive DENTRO del volumen y lo puede poner cualquiera con
    escritura en él.

    Se cierra recorriendo la ruta con `openat(dirfd, parte, O_DIRECTORY|O_NOFOLLOW)`
    componente a componente. **Este test discrimina**: devuélvele a
    `_abrir_directorio` el `os.open` de la ruta entera y se pone verde.
    """
    fuera = tmp_path / "FUERA-del-volumen"; fuera.mkdir()
    (fuera / "dentro").mkdir()
    medio = tmp_path / "medio"; medio.symlink_to(fuera)
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_testigo(str(medio / "dentro"), "vol-A", init=True,
                             ruta_journal=str(medio / "dentro" / "coordination.sqlite"))
    assert "ENLACE" in str(e.value), str(e.value)
    assert "INTERMEDIO" in str(e.value), "no dice CUÁL de los eslabones falló"
    assert not (fuera / "dentro" / pf.TESTIGO).exists(), "estrenó FUERA del volumen"
    assert medio.is_symlink(), "el enlace debería seguir donde estaba"


def test_CORR_P1e_un_dosPuntos_en_la_ruta_es_ROJO(tmp_path):
    """`..` no se recorre: componente a componente no es el `..` que resuelve el
    kernel sobre la ruta entera —depende de por dónde entraste—, así que aceptarlo
    sería recorrer una ruta distinta de la configurada."""
    vol = tmp_path / "vol"; vol.mkdir()
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_testigo(f"{vol}/../vol", "vol-A", init=True,
                             ruta_journal=str(vol / "coordination.sqlite"))
    assert "`..`" in str(e.value)


def test_CORR_P1a_un_FICHERO_en_lugar_del_directorio_da_el_mensaje_CORRECTO(tmp_path):
    """⊕ del discriminador del mensaje, no del rojo.

    En Darwin `O_NOFOLLOW|O_DIRECTORY` sobre un enlace da `ENOTDIR`, no `ELOOP`
    (medido: `errno=20`). Como `ENOTDIR` es TAMBIÉN el errno de «esto es un
    fichero», tratar el errno a secas haría que un fichero corriente se anunciara
    como «es un ENLACE simbólico» — un Rojo con el nombre equivocado, que manda al
    operador a buscar un enlace que no existe. Sin este control, esa confusión
    pasa desapercibida porque el rojo sale igual.
    """
    fichero = tmp_path / "no-soy-un-directorio"
    fichero.write_text("x", encoding="utf-8")
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_testigo(str(fichero), "vol-A", init=True,
                             ruta_journal=str(fichero / "coordination.sqlite"))
    assert "no es un directorio" in str(e.value)
    assert "ENLACE" not in str(e.value), "acusa de enlace a un fichero corriente"


def test_CORR_P1a_el_directorio_de_verdad_SIGUE_estrenando(tmp_path):
    """⊕ imprescindible: `O_NOFOLLOW` en el directorio no puede romper el camino
    bueno. Sin este control, `_abrir_directorio` podría estar rechazándolo todo y
    los dos tests de arriba seguirían verdes."""
    vol = tmp_path / "vol-sano"; vol.mkdir()
    h = pf.comprobar_testigo(str(vol), "vol-A", init=True,
                             ruta_journal=str(vol / "coordination.sqlite"))
    assert len(h) == 64
    assert pf.comprobar_testigo(str(vol), "vol-A", huella_esperada=h) == h


# ── P1-b · el `fsync` cuyo error se tragaba un `except OSError: pass` ─────────

def _fsync_que_falla_en(tipo, monkeypatch, errno_=5):
    """Falla el `fsync` SÓLO sobre directorios (`dir`) o sólo sobre ficheros
    (`fich`). Discriminar por el `fd` es lo único que separa las dos guardas: un
    stub que reventara en los dos haría que cada test pasara por el motivo del
    otro."""
    import stat as sm
    real = os.fsync

    def falso(fd):
        es_dir = sm.S_ISDIR(os.fstat(fd).st_mode)
        if (tipo == "dir" and es_dir) or (tipo == "fich" and not es_dir):
            raise OSError(errno_, "Input/output error")
        return real(fd)

    monkeypatch.setattr(os, "fsync", falso)


def test_CORR_P1b_EIO_en_el_fsync_del_DIRECTORIO_es_ROJO(tmp_path, monkeypatch):
    """`49379af` escribió, en su propio docstring, que sin el `fsync` del
    directorio «la ENTRADA puede no sobrevivir a un corte, y un testigo que se
    evapora es un volumen que mañana parece sin estrenar» — y acto seguido le
    colgó un `except OSError: pass`. El comentario decía una cosa y el código
    hacía otra: con `EIO` el estreno devolvía huella (medido: `eaf46e84…`) y el
    operador declaraba un `_WITNESS` cuya durabilidad no había comprobado nadie.
    """
    vol = tmp_path / "vol-eio-dir"; vol.mkdir()
    _fsync_que_falla_en("dir", monkeypatch)
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_testigo(str(vol), "vol-B", init=True,
                             ruta_journal=str(vol / "coordination.sqlite"))
    assert "ENTRADA del directorio" in str(e.value)
    assert not (vol / pf.TESTIGO).exists(), (
        "dejó el testigo puesto: el arranque siguiente lo LEE por el camino de "
        "lectura y devuelve su huella, tapando el fallo que acaba de ocurrir")


def test_CORR_P1b_EIO_en_el_fsync_del_FICHERO_es_ROJO(tmp_path, monkeypatch):
    """Falsador PROPIO de la otra mitad. Son dos guardas para dos fallos: el del
    fichero pierde el CONTENIDO, el del directorio pierde la ENTRADA. Sin este
    test, quitar el `fsync` del fichero sobreviviría — lo cazaría el del
    directorio y nadie vería que la mitad de la durabilidad ya no se comprueba."""
    vol = tmp_path / "vol-eio-fich"; vol.mkdir()
    _fsync_que_falla_en("fich", monkeypatch)
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_testigo(str(vol), "vol-B", init=True,
                             ruta_journal=str(vol / "coordination.sqlite"))
    assert "no se pudo escribir entero ni asegurar" in str(e.value)
    assert not (vol / pf.TESTIGO).exists()


def test_CORR_P1b_si_no_puede_RETIRAR_el_testigo_lo_DICE(tmp_path, monkeypatch):
    """El mensaje tiene DOS ramas y sólo una se ejercitaba. Si el `unlink` también
    falla queda un fichero a medias en el volumen, y eso el operador tiene que
    leerlo: es el único caso en que hay trabajo manual pendiente."""
    vol = tmp_path / "vol-sin-retirar"; vol.mkdir()
    _fsync_que_falla_en("dir", monkeypatch)
    monkeypatch.setattr(os, "unlink",
                        lambda *a, **k: (_ for _ in ()).throw(OSError(1, "EPERM")))
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_testigo(str(vol), "vol-B", init=True,
                             ruta_journal=str(vol / "coordination.sqlite"))
    assert "NO he podido retirarlo" in str(e.value)


def test_CORR_P1b_el_fsync_bueno_no_estorba(tmp_path):
    """⊕: con los dos `fsync` funcionando, el estreno pasa. Si esta guarda diera
    rojo siempre, los tres de arriba pasarían por el motivo equivocado."""
    vol = tmp_path / "vol-fsync-ok"; vol.mkdir()
    assert len(pf.comprobar_testigo(str(vol), "vol-B", init=True,
                                    ruta_journal=str(vol / "x.sqlite"))) == 64


# ── P2-a · `umask` recortaba el modo y el arranque SIGUIENTE lo rechazaba ─────

@pytest.mark.parametrize("mascara", [0o000, 0o022, 0o044, 0o077])
def test_CORR_P2a_ninguna_umask_cambia_el_modo_del_testigo(tmp_path, mascara):
    """`O_CREAT` pasa el modo por la `umask` del proceso. Con `umask 0044` el
    testigo nacía `0400` (medido) y el arranque siguiente moría en su propia
    guarda `modo != 0o444` — un ROJO que se infligía el estreno a sí mismo, sin
    que nada estuviera mal en el volumen. `fchmod` no pasa por la máscara.

    El falsador NO es «el modo es 0444»: es que **el arranque siguiente pasa**.
    Comprobar sólo el modo dejaría verde una cura que arreglase el número y
    rompiese la cadena.
    """
    vol = tmp_path / f"vol-umask-{mascara:04o}"; vol.mkdir()
    previa = os.umask(mascara)
    try:
        huella = pf.comprobar_testigo(str(vol), "vol-C", init=True,
                                      ruta_journal=str(vol / "x.sqlite"))
    finally:
        os.umask(previa)
    assert oct(os.stat(vol / pf.TESTIGO).st_mode & 0o777) == "0o444"
    assert pf.comprobar_testigo(str(vol), "vol-C", huella_esperada=huella) == huella


def test_CORR_P2a_el_fchmod_no_puede_ABRIR_mas_de_0444(tmp_path):
    """La dirección que nadie mira: una cura del modo que lo dejara escribible
    reabriría justo lo que `test_NOGO_el_testigo_escribible_es_ROJO` cierra."""
    vol = tmp_path / "vol-modo"; vol.mkdir()
    pf.comprobar_testigo(str(vol), "vol-C", init=True, ruta_journal=str(vol / "x.sqlite"))
    modo = os.stat(vol / pf.TESTIGO).st_mode & 0o777
    assert modo & 0o222 == 0, f"el testigo nació escribible ({modo:04o})"


# ── P2-b · `os.write` puede escribir MENOS, y la huella no lo sabía ───────────

def test_CORR_P2b_un_write_TROCEADO_deja_el_testigo_COMPLETO(tmp_path, monkeypatch):
    """La huella se calcula sobre el cuerpo COMPLETO. Si el `write` escribe un
    prefijo y nadie mira el número devuelto, `--init` imprime un `_WITNESS` que
    **ningún arranque posterior puede casar jamás**: no es un fallo transitorio,
    es un almacén tapiado. Medido contra `49379af`: `10 B` en disco y huella
    declarada `39e3b6ed…` frente a `7ba4fe84…` real.

    🔑 La aserción NO es «salta Rojo» —el bucle debe COMPLETAR la escritura—, sino
    que **la huella devuelta describe los bytes que hay en el disco**. Mi primer
    falsador afirmaba lo primero y salía verde contra el código roto y contra el
    curado por motivos distintos: no discriminaba.
    """
    vol = tmp_path / "vol-troceado"; vol.mkdir()
    real = os.write
    monkeypatch.setattr(os, "write", lambda fd, b: real(fd, b[:10]))
    huella = pf.comprobar_testigo(str(vol), "vol-D", init=True,
                                  ruta_journal=str(vol / "x.sqlite"))
    monkeypatch.undo()
    disco = (vol / pf.TESTIGO).read_bytes()
    assert hashlib.sha256(disco).hexdigest() == huella, (
        f"la huella declarada no describe el fichero: {len(disco)} bytes en disco")
    assert pf.comprobar_testigo(str(vol), "vol-D", huella_esperada=huella) == huella


def test_CORR_P2b_un_write_que_NO_AVANZA_es_ROJO_y_no_deja_testigo(tmp_path, monkeypatch):
    """El otro extremo: un `write` que devuelve `0` haría girar el bucle para
    siempre. Tiene que ser un Rojo con nombre, y no dejar el fichero vacío detrás
    —que el arranque siguiente leería como testigo y rechazaría por formato, con
    el mensaje equivocado."""
    vol = tmp_path / "vol-cero"; vol.mkdir()
    monkeypatch.setattr(os, "write", lambda fd, b: 0)
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_testigo(str(vol), "vol-D", init=True,
                             ruta_journal=str(vol / "x.sqlite"))
    monkeypatch.undo()
    assert "escribir entero" in str(e.value)
    assert not (vol / pf.TESTIGO).exists()


# ── P2-c · `nacido` sin zona no fecha un instante ────────────────────────────

@pytest.mark.parametrize("nacido,motivo", [
    ("2026-09-05T21:00:00", "naive: son 26 horas distintas según quién la lea"),
    ("2026-09-05", "sólo fecha: `fromisoformat` la vuelve medianoche naive"),
    ("2026-09-05T21:00:00.123456", "naive con microsegundos"),
])
def test_CORR_P2c_nacido_SIN_ZONA_es_ROJO(banco, tmp_path, nacido, motivo):
    """`datetime.fromisoformat` acepta una marca SIN zona, así que la guarda que
    `49379af` puso contra «ayer por la tarde» dejaba pasar `2026-09-05T21:00:00`
    —y `2026-09-05` a secas—. Medido antes de curar: las dos formas pasaban.

    Un campo que no fecha un instante no acredita cuándo se estrenó el volumen, y
    ése es todo su trabajo."""
    vol = tmp_path / f"vol-tz-{abs(hash(nacido))}"; vol.mkdir()
    cuerpo = json.dumps({"v": pf.TESTIGO_V, "id": "vol-E", "nonce": "a" * 32,
                         "nacido": nacido}).encode()
    (vol / pf.TESTIGO).write_bytes(cuerpo)
    (vol / pf.TESTIGO).chmod(0o444)
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_testigo(str(vol), "vol-E",
                             huella_esperada=hashlib.sha256(cuerpo).hexdigest())
    assert "zona horaria" in str(e.value), motivo


@pytest.mark.parametrize("nacido", [
    "2026-09-05T21:00:00+00:00",
    "2026-09-05T21:00:00Z",
    "2026-09-05T23:00:00+02:00",
])
def test_CORR_P2c_nacido_CON_zona_pasa(tmp_path, nacido):
    """⊕ del mismo detector, y no es decorativo: exigir la zona podía haber
    rechazado al testigo LEGÍTIMO, que es lo único que esta guarda no puede hacer.
    Se prueban las tres formas que `fromisoformat` acepta con desplazamiento."""
    vol = tmp_path / f"vol-tz-ok-{abs(hash(nacido))}"; vol.mkdir()
    cuerpo = json.dumps({"v": pf.TESTIGO_V, "id": "vol-E", "nonce": "a" * 32,
                         "nacido": nacido}).encode()
    (vol / pf.TESTIGO).write_bytes(cuerpo)
    (vol / pf.TESTIGO).chmod(0o444)
    huella = hashlib.sha256(cuerpo).hexdigest()
    assert pf.comprobar_testigo(str(vol), "vol-E", huella_esperada=huella) == huella


def test_CORR_P2c_lo_que_ESCRIBE_el_estreno_lleva_zona(tmp_path):
    """⊕ que cierra el lazo, y es el que impide que la guarda y el escritor
    diverjan: si el estreno dejara de escribir en UTC, todos los tests de arriba
    seguirían verdes y el camino bueno se rompería en silencio."""
    vol = tmp_path / "vol-nacido"; vol.mkdir()
    pf.comprobar_testigo(str(vol), "vol-E", init=True, ruta_journal=str(vol / "x.sqlite"))
    datos = json.loads((vol / pf.TESTIGO).read_bytes())
    from datetime import datetime as _dt
    assert _dt.fromisoformat(datos["nacido"]).utcoffset() is not None


# ── La cadena entera, que es lo que el operador corre de verdad ───────────────

def test_CORR_el_estreno_COMPLETO_sigue_verde_bajo_umask_hostil(banco, tmp_path):
    """⊕ de integración de las cinco curas juntas: `estrenar()` acuña LOS DOS
    testigos y el `preflight` los acepta, con una `umask` que antes rompía la
    cadena. Los tests unitarios de arriba no lo cubren: cada uno mira una guarda,
    y lo que el operador corre es la cadena."""
    _sin_testigos(banco)
    previa = os.umask(0o044)
    try:
        salida = pf.estrenar(banco["env"], raiz_ledgers=raiz_ledgers(banco),
                             stat_fn=stat_distinto)
    finally:
        os.umask(previa)
    env = dict(banco["env"], **salida)
    assert len(pf.preflight(env, stat_fn=stat_distinto,
                            raiz_ledgers=raiz_ledgers(banco))) == 6


def test_CORR_el_LEDGER_tambien_esta_protegido_por_el_enlace_del_directorio(banco, tmp_path):
    """`comprobar_ledgers` y `estrenar` llegan al mismo `comprobar_testigo`, así
    que la cura del directorio los cubre a los dos. Se comprueba en vez de
    suponerse: el ledger es el ÚNICO almacén RW del piloto."""
    fuera = tmp_path / "ledger-FUERA"; fuera.mkdir()
    (fuera / pf.SENAL_LEDGER).write_text("# no soy el ledger\n", encoding="utf-8")
    enlace = tmp_path / "ledger-enlazado"; enlace.symlink_to(fuera)
    env = dict(banco["env"], LLMINBOX_LEDGER_PILOTO=str(enlace))
    with pytest.raises(pf.Rojo) as e:
        pf.estrenar(env, raiz_ledgers=raiz_ledgers(banco), stat_fn=stat_distinto)
    # 🔻 RE-ANCLADO: el enlace vive fuera de la raíz autorizada, así que lo caza
    # la guarda del hijo directo antes que la del `O_NOFOLLOW`. El eje del test
    # —no se acuña NADA fuera— es el mismo y es lo que sigue aserto.
    assert "hijo DIRECTO" in str(e.value) or "enlace" in str(e.value).lower()
    assert not (fuera / pf.TESTIGO_LEDGER).exists(), "acuñó el testigo FUERA"


# ── H5 y media H4 de @security (22:47:41Z) — NO estaban en el encargo ─────────
# Se adoptan y se dice: el hallazgo y la cura de H5 son suyos, y la mitad de H4
# que se cierra aquí sale gratis de mover una línea. La otra mitad NO se cierra.

def test_SEC_H5_un_journal_que_es_ENLACE_COLGANTE_no_se_adopta(tmp_path):
    """H5: `hay_datos` usaba `os.path.exists`, que SIGUE enlaces — el mismo
    `exists()` que `49379af` acababa de retirar del testigo, vivo en la
    comprobación de datos previos. Un journal que es un enlace colgante daba
    `False` («no hay datos, estrena») y `--init` adoptaba como propio un almacén
    de procedencia desconocida. Cura de una línea, suya: `os.path.lexists`.

    ⊖ del instrumento: el enlace es COLGANTE a propósito (`lexists=True`,
    `exists=False`). Con un enlace a un fichero que existe, `exists()` también
    diría `True` y el test pasaría contra el código roto sin medir nada."""
    vol = tmp_path / "vol-h5"; vol.mkdir()
    journal = vol / "coordination.sqlite"
    journal.symlink_to(tmp_path / "no-existe-este-destino")
    assert os.path.lexists(journal) and not os.path.exists(journal), (
        "el enlace no es colgante: el test no discrimina `exists` de `lexists`")
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_testigo(str(vol), "vol-H5", init=True, ruta_journal=str(journal))
    assert "procedencia" in str(e.value)
    assert not (vol / pf.TESTIGO).exists(), "estrenó sobre un almacén con datos"


def test_SEC_H5_sin_journal_el_estreno_SIGUE_funcionando(tmp_path):
    """⊕: `lexists` no puede volver rojo el estreno legítimo, que es un volumen
    donde el journal todavía NO existe. Sin este control, cambiar a `lexists`
    podría estar bloqueando todo estreno y el test de arriba seguiría verde."""
    vol = tmp_path / "vol-h5-ok"; vol.mkdir()
    assert len(pf.comprobar_testigo(str(vol), "vol-H5", init=True,
                                    ruta_journal=str(vol / "coordination.sqlite"))) == 64


def test_SEC_H4_si_el_proceso_MUERE_a_medias_el_resto_es_BORRABLE(tmp_path, monkeypatch):
    """Media H4. Si el proceso muere entre el `open` y el `fsync` no corre ningún
    `except`, así que `_retirar` no lo salva y queda un fichero a medias. Creado
    `0444` obligaba a «borrar a mano un fichero de sólo lectura dentro del
    volumen» —la frase del hallazgo— sin que ningún mensaje lo dijera. Creado
    `0600` y endurecido a `0444` DESPUÉS del `fsync`, lo que queda es borrable.

    La muerte se simula con una excepción que NO es `OSError`, que es justo lo que
    esquiva el camino de retirada.

    ⛔ Esto NO cierra H4 entera y el test no pretende decir que sí: el `--init`
    siguiente sigue muriendo con `FileExistsError`. Eso lo cierra un `link()`
    atómico desde un temporal, que es un cambio del protocolo de estreno."""
    vol = tmp_path / "vol-h4"; vol.mkdir()

    def muere(fd, b):
        raise KeyboardInterrupt("el proceso muere a mitad de escribir")

    monkeypatch.setattr(os, "write", muere)
    with pytest.raises(KeyboardInterrupt):
        pf.comprobar_testigo(str(vol), "vol-H4", init=True,
                             ruta_journal=str(vol / "x.sqlite"))
    monkeypatch.undo()
    resto = vol / pf.TESTIGO
    assert resto.exists(), "el test no mide nada: no quedó ningún resto"
    modo = os.stat(resto).st_mode & 0o777
    assert modo & 0o200, (
        f"el resto quedó en {modo:04o}: sólo-lectura dentro del volumen, que es "
        f"exactamente lo que H4 nombra")
    assert resto.stat().st_size == 0


# ══════════════════════════════════════════════════════════════════════════════
# 4ª CORRECTIVA — NO-GO del auditor sobre `525efff`. Dos huecos del camino de
# CREACIÓN, y los dos son la clase que esta entrega lleva curando: una guarda
# cuyo SUJETO nunca se mide, y una durabilidad que no cubre lo que el arranque
# exige. Traza medida antes de tocar nada:
#     write → fsync(fich) → fchmod(0444) → fsync(dir)
#              ↑ sin fstat del fd nuevo · sin fsync del fichero tras el fchmod
# ══════════════════════════════════════════════════════════════════════════════

def _trazar(monkeypatch):
    """Registra la secuencia REAL de syscalls sobre el estreno. Un test de ORDEN
    necesita la secuencia, no un contador: «hay dos fsync» no dice si el segundo
    cae antes o después del `fchmod`, que es justo lo que el NO-GO señala."""
    import stat as sm
    traza = []
    rw, rf, rc, rs = os.write, os.fsync, os.fchmod, os.fstat
    monkeypatch.setattr(os, "write", lambda fd, b: (traza.append("write"), rw(fd, b))[1])
    monkeypatch.setattr(os, "fsync", lambda fd: (
        traza.append("fsync(dir)" if sm.S_ISDIR(rs(fd).st_mode) else "fsync(fich)"),
        rf(fd))[1])
    monkeypatch.setattr(os, "fchmod", lambda fd, m: (traza.append("fchmod"), rc(fd, m))[1])
    return traza


def test_NOGO2_el_fsync_del_FICHERO_cae_DESPUES_del_fchmod(tmp_path, monkeypatch):
    """El modo `0444` NO es cosmético: es una guarda que el arranque EXIGE
    (`modo != 0o444` ⇒ Rojo). Endurecerlo y no asegurarlo repite exactamente el
    defecto del `fsync` del directorio — una guarda cuya supervivencia a un corte
    no comprueba nadie. Un corte entre el `fchmod` y el flush dejaba el testigo en
    `0600`, y el arranque siguiente moría acusando al volumen de algo que hizo el
    corte.

    🔑 La aserción es de ORDEN, no de conteo: «hay dos `fsync`» no distingue el
    caso bueno del malo. Y el `fsync` ① tiene que SEGUIR estando antes del
    `fchmod` —lo vigila M10— porque es lo que deja el resto BORRABLE si el proceso
    muere; los dos existen por motivos distintos y el test los ancla a los dos."""
    vol = tmp_path / "vol-traza"; vol.mkdir()
    traza = _trazar(monkeypatch)
    pf.comprobar_testigo(str(vol), "vol-T", init=True, ruta_journal=str(vol / "x.sqlite"))
    monkeypatch.undo()
    fich = [i for i, x in enumerate(traza) if x == "fsync(fich)"]
    chmod = traza.index("fchmod")
    assert any(i > chmod for i in fich), (
        f"ningún `fsync` del FICHERO tras el `fchmod`: el modo no es durable. {traza}")
    assert any(i < chmod for i in fich), (
        f"el `fsync` ① desapareció de delante del `fchmod`: un corte a medias "
        f"dejaría un sólo-lectura atrapado en el volumen. {traza}")
    assert traza.index("write") < chmod
    assert traza[-1] == "fsync(dir)", f"el `fsync` del directorio ya no cierra: {traza}"


def test_NOGO2_un_fchmod_que_NO_TOMA_es_ROJO_EN_EL_ESTRENO(tmp_path, monkeypatch):
    """`fchmod` puede no tomar (montaje restringido, FS que no lo soporta) y no lo
    dice: no levanta error. Sin mirar el descriptor, el estreno devolvía huella y
    el rojo salía en el ARRANQUE SIGUIENTE, acusando al volumen de un defecto del
    estreno — con el operador ya mirando a otro lado. Medido: huella devuelta y
    modo real `0600`.

    El falsador exige las dos mitades: que sea Rojo, y que sea **aquí**."""
    vol = tmp_path / "vol-fchmod"; vol.mkdir()
    monkeypatch.setattr(os, "fchmod", lambda fd, m: None)
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_testigo(str(vol), "vol-F", init=True,
                             ruta_journal=str(vol / "x.sqlite"))
    monkeypatch.undo()
    assert "el `fchmod` no tomó" in str(e.value)
    assert "0444" in str(e.value)
    assert not (vol / pf.TESTIGO).exists(), "dejó puesto un testigo que no verificó"


def test_NOGO2_un_write_que_MIENTE_en_su_retorno_es_ROJO(tmp_path, monkeypatch):
    """🔑 El caso que demuestra que el `st_size` NO duplica al bucle de escritura,
    que es la objeción evidente. Un `os.write` que devuelve `len(b)` y escribe
    `10` deja el bucle SATISFECHO —no tiene forma de saberlo— y el estreno
    publicaba una huella de `107 B` sobre un fichero de `10`: el mismo «almacén
    tapiado» que creí cerrado, por una puerta que el bucle no puede ver.

    El bucle mira el VALOR QUE DEVUELVE la llamada. Esto mira el OBJETO. Dos
    instrumentos, y sólo el segundo cierra este caso."""
    vol = tmp_path / "vol-miente"; vol.mkdir()
    real = os.write
    monkeypatch.setattr(os, "write", lambda fd, b: (real(fd, b[:10]), len(b))[1])
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_testigo(str(vol), "vol-M", init=True,
                             ruta_journal=str(vol / "x.sqlite"))
    monkeypatch.undo()
    assert "bytes en disco" in str(e.value) and "huella describiría" in str(e.value)
    assert not (vol / pf.TESTIGO).exists()


def _fstat_falso(monkeypatch, transforma):
    """Falsea el PRIMER `fstat`, que en el camino de creación es el del testigo
    recién creado. Mismo patrón que `test_S1`, que ya lo usa para el overmount en
    el camino de lectura."""
    real = os.fstat
    n = {"i": 0}

    class _St:
        def __init__(self, st): self._st = st
        def __getattr__(self, k): return getattr(self._st, k)

    def falso(fd):
        st = real(fd)
        n["i"] += 1
        return transforma(_St(st)) if n["i"] == 1 else st

    monkeypatch.setattr(os, "fstat", falso)


def test_NOGO2_un_testigo_montado_ENCIMA_al_CREARLO_es_ROJO(tmp_path, monkeypatch):
    """El camino de LECTURA detecta el overmount desde `f6a…`; el de CREACIÓN no
    lo miraba. Son dos caminos y hacen falta las dos guardas."""
    vol = tmp_path / "vol-overmount"; vol.mkdir()

    def encima(st): st.st_dev = 999; return st
    _fstat_falso(monkeypatch, encima)
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_testigo(str(vol), "vol-O", init=True,
                             ruta_journal=str(vol / "x.sqlite"))
    monkeypatch.undo()
    assert "montado encima" in str(e.value)


def test_NOGO2_un_objeto_NO_REGULAR_al_CREARLO_es_ROJO(tmp_path, monkeypatch):
    """⚠️ COTA DEL PROPIO FALSADOR, y la digo yo: con `O_CREAT|O_EXCL` no conozco
    un escenario real que produzca aquí algo que no sea un fichero regular, así
    que su único falsador es un `fstat` falseado. Eso acredita que **el código se
    ramifica sobre ese valor**, NO que el caso ocurra. Se deja porque el camino de
    lectura lo comprueba y una asimetría entre los dos caminos es la clase de
    hueco que este NO-GO acaba de encontrar — pero no se vende como más."""
    vol = tmp_path / "vol-noreg"; vol.mkdir()

    import stat as sm

    def no_regular(st):
        st.st_mode = (st.st_mode & ~0o170000) | sm.S_IFIFO
        return st

    _fstat_falso(monkeypatch, no_regular)
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_testigo(str(vol), "vol-N", init=True,
                             ruta_journal=str(vol / "x.sqlite"))
    monkeypatch.undo()
    assert "no es un fichero regular" in str(e.value)



def test_NOGO2_el_estreno_BUENO_pasa_las_cuatro_comprobaciones(tmp_path):
    """⊕ imprescindible: cuatro rechazos nuevos podrían ser «rechaza siempre», y
    los cinco tests de arriba seguirían verdes. Cierra el lazo hasta el arranque:
    lo que el estreno verifica es lo que el arranque acepta."""
    vol = tmp_path / "vol-4ok"; vol.mkdir()
    huella = pf.comprobar_testigo(str(vol), "vol-B", init=True,
                                  ruta_journal=str(vol / "x.sqlite"))
    assert len(huella) == 64
    assert oct(os.stat(vol / pf.TESTIGO).st_mode & 0o777) == "0o444"
    assert hashlib.sha256((vol / pf.TESTIGO).read_bytes()).hexdigest() == huella
    assert pf.comprobar_testigo(str(vol), "vol-B", huella_esperada=huella) == huella


# ══════════════════════════════════════════════════════════════════════════════
# 5ª CORRECTIVA — P3 de @security (`01:05:00Z`) sobre `34ed4e2`. El hallazgo es
# suyo y es mi propio principio devuelto contra mí: `_verificar_estreno` mira el
# objeto (tipo · modo · tamaño · dispositivo) y después se firmaba
# `sha256(cuerpo)` — EL BUFFER. **El tamaño es un PROXY del contenido.**
# ══════════════════════════════════════════════════════════════════════════════

def test_P3_la_huella_del_estreno_describe_EL_DISCO_y_no_el_BUFFER(tmp_path, monkeypatch):
    """El camino de LECTURA firma `sha256(crudo)`, lo que hay en disco; el de
    CREACIÓN firmaba el buffer. Medido antes de curar: mismos bytes de longitud y
    contenido distinto ⇒ firmada `707a9d3e…` y disco `e50601e0…`, **en VERDE**, con
    el rojo saliendo en el arranque SIGUIENTE — el fallo diferido que esta entrega
    entera existe para matar.

    ⚖️ Y la cura de @security era NECESARIA y NO SUFICIENTE, lo cual se ve en este
    mismo test: firmando lo releído, este caso pasaba a VERDE con la huella honesta
    —el estreno firmaba BASURA y reventaba al parsear en el arranque siguiente—.
    Lo que lo cierra es COMPARAR lo releído con lo que compuse."""
    vol = tmp_path / "vol-p3"; vol.mkdir()
    real = os.write
    monkeypatch.setattr(os, "write", lambda fd, b: real(fd, b"X" * len(b)))
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_testigo(str(vol), "vol-P", init=True,
                             ruta_journal=str(vol / "x.sqlite"))
    monkeypatch.undo()
    assert "NO es lo que compuse" in str(e.value)
    assert not (vol / pf.TESTIGO).exists()


def test_P3_la_huella_del_estreno_ES_la_del_fichero(tmp_path):
    """⊕ del mismo eje, y es el que ata los dos caminos: lo que el estreno DEVUELVE
    tiene que ser exactamente `sha256` de los bytes del fichero, que es lo que el
    arranque va a calcular. Sin este control, firmar el buffer y firmar el disco se
    ven idénticos mientras nadie los separe."""
    vol = tmp_path / "vol-p3-ok"; vol.mkdir()
    huella = pf.comprobar_testigo(str(vol), "vol-P", init=True,
                                  ruta_journal=str(vol / "x.sqlite"))
    assert hashlib.sha256((vol / pf.TESTIGO).read_bytes()).hexdigest() == huella


def test_P3_un_fichero_MAS_LARGO_que_el_cuerpo_es_ROJO(tmp_path, monkeypatch):
    """⚖️ Éste es el que CORRIGE HACIA ARRIBA el «bonus» del hallazgo. @security
    propone que, firmando lo releído, la comprobación de tamaño «pasa a ser
    redundante». **No lo es**: el `pread` lee una VENTANA de `len(cuerpo)` bytes.
    Con un fichero MÁS LARGO mi ventana firma un PREFIJO mientras el camino de
    lectura hace `os.read(fd, 4096)` y firma de más ⇒ dos huellas distintas del
    mismo fichero, y el arranque siguiente no casaría nunca. El tamaño es lo que
    garantiza que la ventana del `pread` sea el fichero ENTERO."""
    vol = tmp_path / "vol-p3-largo"; vol.mkdir()
    real = os.write
    monkeypatch.setattr(os, "write", lambda fd, b: (real(fd, b + b"XX"), len(b))[1])
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_testigo(str(vol), "vol-P", init=True,
                             ruta_journal=str(vol / "x.sqlite"))
    monkeypatch.undo()
    assert "bytes en disco" in str(e.value)
    assert not (vol / pf.TESTIGO).exists()


# ══════════════════════════════════════════════════════════════════════════════
# 6ª CORRECTIVA — CIERRE del auditor sobre `2d9e785`. Dos P1: la sonda sólo
# capturaba `Rojo` (un `EIO` real se le escapaba SIN cortar) y el estreno acuñaba
# en un directorio arbitrario con un `LEDGER.md` que era ENLACE a fuera.
# ══════════════════════════════════════════════════════════════════════════════

# ── P1-a · la red final, y el corte que se GANA acreditando el objetivo ───────

def _acredita(ok=True, motivo="contenedor simulado"):
    return lambda entorno: (ok, motivo)


@pytest.mark.parametrize("nombre", ["EIO", "EOF/vacío"])
def test_NOGO4_un_fallo_NO_TIPADO_en_la_lectura_CORTA_igual(banco, monkeypatch,
                                                            capsys, nombre):
    """🩸 El defecto: `readiness` tenía UN solo `except Rojo`, y el
    `crudo = os.read(fd, 4096)` iba desnudo. Un `EIO` real —disco que se va,
    volumen que desaparece— subía `OSError` CRUDO, se escapaba de la red entera y
    la sonda moría con traceback **sin cortar**. Medido: `kill_fn` con `0`
    llamadas y el PID 1 VIVO, con el health en rojo y Docker sin matar contenedores
    `unhealthy`. Un fail-closed que sólo cierra ante los errores que él mismo
    define no es fail-closed.

    Los dos casos son el mismo eje por dos puertas distintas: el error de
    dispositivo y el fichero VACÍO (cero bytes no acreditan nada).

    🩸 La lectura PARCIAL iba aquí como tercer caso y la SAQUÉ: mi bucle la
    ensambla correctamente, así que sale VERDE. Afirmar «corta» sobre ella era un
    ⊖ que no discrimina —el mismo defecto de falsador que ya me pasó con el short
    write—. Su aserción correcta es un ⊕ y vive en el test de abajo."""
    import stat as sm
    ruta = banco["journal_dir"] / pf.TESTIGO
    real = os.read
    if nombre == "EIO":
        def falso(fd, n):
            st = os.fstat(fd)
            if sm.S_ISREG(st.st_mode) and 0 < st.st_size < 4096:
                raise OSError(5, "Input/output error")
            return real(fd, n)
        monkeypatch.setattr(os, "read", falso)
    elif nombre == "EOF/vacío":
        ruta.chmod(0o644); ruta.write_bytes(b""); ruta.chmod(0o444)
    else:
        ruta.chmod(0o644); ruta.write_bytes(b""); ruta.chmod(0o444)

    matados = []
    rc = pf.readiness(banco["env"], stat_fn=stat_distinto,
                      raiz_ledgers=raiz_ledgers(banco),
                      kill_fn=lambda pid, sig: matados.append((pid, sig)),
                      acreditar_fn=_acredita())
    monkeypatch.undo()
    capsys_texto = lambda: capsys.readouterr().err
    assert rc == 1, f"[{nombre}] la sonda no dio ROJO"
    assert matados == [(1, signal.SIGTERM)], (
        f"[{nombre}] no cortó el PID 1: kill_fn recibió {len(matados)} llamadas")
    # 🩸 Y el MOTIVO, no sólo el rojo. Sin esto, el caso VACÍO lo satisfacía el
    # `json.loads` fallando por «formato» — la sonda salía roja por el camino
    # equivocado y el mutante que borra la guarda del vacío SOBREVIVÍA. Un rojo
    # correcto por el motivo equivocado no acredita la guarda que dice acreditar.
    if nombre == "EOF/vacío":
        assert "VACÍO" in capsys_texto(), (
            "murió por «formato» en vez de por VACÍO: cero bytes no acreditan nada, "
            "y culpar al contenido de una lectura que no trajo ninguno es otro fallo")


def test_NOGO4_CERO_PID_AJENO_si_el_PID1_no_esta_acreditado(banco):
    """🔑 La mitad peligrosa de la cura, y por eso tiene falsador propio: mandar
    `SIGTERM` al PID `1` es correcto DENTRO del contenedor y es un disparo a
    ciegas fuera. En un host el PID 1 es `launchd`/`systemd`. **Cero llamadas a
    `kill_fn` cuando el objetivo no está acreditado**, y el ROJO se mantiene: un
    corte que no se dispara deja el health en rojo, que se ve; un corte al proceso
    equivocado no se deshace."""
    matados = []
    env = dict(banco["env"]); env["LLMINBOX_JOURNAL"] = ""
    rc = pf.readiness(env, stat_fn=stat_distinto, raiz_ledgers=raiz_ledgers(banco),
                      kill_fn=lambda pid, sig: matados.append((pid, sig)),
                      acreditar_fn=_acredita(False, "el PID 1 no es `uvicorn`"))
    assert rc == 1, "sin poder cortar, el veredicto sigue siendo ROJO"
    assert matados == [], f"disparó a un PID ajeno: {matados}"


def test_NOGO4_la_red_final_NO_atrapa_señales(banco, monkeypatch):
    """⚠️ `except Exception`, NUNCA `BaseException`. Tragarse `KeyboardInterrupt`
    o `SystemExit` convertiría un `docker stop` o un Ctrl-C en un corte del PID 1
    decidido por la sonda. La red atrapa FALLOS, no señales — y sin este control
    un `except BaseException` pasaría los otros tests igual de verde."""
    matados = []

    def interrumpe(*a, **k):
        raise KeyboardInterrupt("docker stop")

    monkeypatch.setattr(pf, "comprobar_volumen_durable", interrumpe)
    with pytest.raises(KeyboardInterrupt):
        pf.readiness(banco["env"], stat_fn=stat_distinto,
                     raiz_ledgers=raiz_ledgers(banco),
                     kill_fn=lambda pid, sig: matados.append((pid, sig)),
                     acreditar_fn=_acredita())
    monkeypatch.undo()
    assert matados == [], "una señal no puede acabar en un SIGTERM al PID 1"


# ── el acreditador, por su cuenta ─────────────────────────────────────────────

@pytest.mark.parametrize("cmdline,espera_corte", [
    (b"uvicorn\x00servicio:app\x00", True),
    (b"/sbin/launchd\x00", False),
    (b"/lib/systemd/systemd\x00--switched-root\x00", False),
    (b"", False),
])
def test_NOGO4_el_acreditador_solo_da_permiso_al_gateway(cmdline, espera_corte):
    """⊕/⊖ del INSTRUMENTO, tabulado antes de fiarme de él: acredita a `uvicorn` y
    a nadie más. Sin el caso VERDE, un acreditador que dijera «no» siempre pasaría
    el test de arriba y dejaría la sonda sin corte para siempre."""
    puede, motivo = pf._pid1_acreditado({}, leer=lambda ruta: cmdline)
    assert puede is espera_corte, f"{cmdline!r} -> {motivo}"


@pytest.mark.parametrize("cmdline", [
    b"/usr/bin/not-uvicorn-wrapper\x00",
    b"/x/uvicorn-wrapper\x00",
    b"/usr/bin/notuvicorn\x00",
    b"uvicorn-mitm\x00servicio:app\x00",
])
def test_NOGO4_el_acreditador_compara_identidad_EXACTA_no_SUBCADENA(cmdline):
    """🩸 `esperado not in texto` es SUBCADENA, y `uvicorn` es subcadena de
    infinitos nombres que no son el gateway. Los cuatro acreditaban, y acreditar
    es dar permiso para mandarle `SIGTERM` al PID 1.

    Ahora se compara el `basename` de `argv[0]` con `==`. **Discrimina**: vuelve a
    `esperado not in texto` y los cuatro se ponen verdes."""
    puede, motivo = pf._pid1_acreditado({}, leer=lambda ruta: cmdline)
    assert puede is False, motivo
    assert "EXACTA" in motivo


@pytest.mark.parametrize("perilla", ["", "   "])
def test_NOGO4_una_perilla_de_identidad_VACIA_no_acredita_a_nadie(perilla):
    """🩸 `env.get(clave, defecto)` devuelve la CADENA VACÍA cuando la clave existe
    vacía —el defecto no entra— y `"" in cualquier_cosa` es `True`: una perilla
    puesta a vacío desarmaba la guarda ENTERA y acreditaba a `/sbin/launchd`.

    Fail-closed HACIA EL NO DISPARO y con el motivo escrito: una perilla declarada
    sin valor es un fallo con nombre, no un silencio que hereda el defecto."""
    puede, motivo = pf._pid1_acreditado(
        {"LLMINBOX_PID1_ESPERADO": perilla}, leer=lambda ruta: b"uvicorn\x00")
    assert puede is False
    assert "VAC" in motivo.upper() and "LLMINBOX_PID1_ESPERADO" in motivo


def test_NOGO4_un_uvicorn_que_NO_esta_en_un_contenedor_no_se_corta():
    """La otra mitad del contrato: dice «corta el PID 1 DEL CONTENEDOR». Sin ella
    el acreditador sólo afirma «el PID 1 se llama uvicorn», y eso también es cierto
    en un host donde alguien llamó `uvicorn` a su init —o en el portátil de quien
    corra la sonda a mano—. Un corte al proceso equivocado no se deshace."""
    def leer(ruta):
        if ruta == "/proc/1/cmdline":
            return b"uvicorn\x00servicio:app\x00"
        if ruta == "/proc/1/cgroup":
            return b"0::/user.slice/user-501.slice"
        raise FileNotFoundError(2, "no", ruta)

    puede, motivo = pf._pid1_acreditado({}, leer=leer)
    assert puede is False
    assert "contenedor" in motivo


@pytest.mark.parametrize("señal,marca", [
    (b"", "/.dockerenv"),
    (None, "cgroup"),
])
def test_NOGO4_el_contexto_de_contenedor_se_acredita_y_DICE_CUAL(señal, marca):
    """⊕ imprescindible del par: sin él, exigir contexto podría estar rechazándolo
    TODO y el ⊖ de arriba seguiría verde. Y el motivo nombra la señal que acreditó,
    para que se pueda auditar en vez de creer."""
    def leer(ruta):
        if ruta == "/proc/1/cmdline":
            return b"uvicorn\x00servicio:app\x00"
        if ruta == "/.dockerenv":
            if señal is None:
                raise FileNotFoundError(2, "no", ruta)
            return señal
        if ruta == "/proc/1/cgroup":
            return b"0::/docker/8a54fda53a77"
        raise FileNotFoundError(2, "no", ruta)

    puede, motivo = pf._pid1_acreditado({}, leer=leer)
    assert puede is True, motivo
    assert marca in motivo


def test_NOGO4_sin_proc_no_se_corta_y_lo_DICE(tmp_path):
    """En macOS —donde corre este arnés— no hay `/proc`. Fail-closed HACIA EL NO
    DISPARO, y con el motivo escrito: un corte mudo que no ocurre es
    indistinguible de un corte que sí."""
    def no_hay(ruta):
        raise FileNotFoundError(2, "No such file or directory", ruta)

    puede, motivo = pf._pid1_acreditado({}, leer=no_hay)
    assert puede is False
    assert "/proc/1/cmdline" in motivo and "PID 1" in motivo


def _trocear_solo(monkeypatch, ruta, veces):
    """Hace que `read(2)` devuelva UN byte, y sólo sobre el fichero pedido.

    Trocear TODO no discrimina: los dos caminos de `_leer_fd` —el que ensambla y
    el que exige la lectura completa— se ejercitarían a la vez y el veredicto no
    diría cuál habló. Se ancla por `st_ino`, no por ruta, porque quien lee ya
    tiene el descriptor y la ruta no vuelve a resolverse.
    """
    real = os.read
    ino = os.stat(ruta).st_ino

    def de_uno_en_uno(fd, n):
        if os.fstat(fd).st_ino == ino and n > 1:
            veces["n"] += 1
            return real(fd, 1)
        return real(fd, n)

    monkeypatch.setattr(os, "read", de_uno_en_uno)


def test_NOGO4_una_lectura_PARCIAL_del_TESTIGO_es_ROJA_y_CORTA(banco, monkeypatch):
    """🩸 AUDITORÍA: este test pedía lo CONTRARIO —«se ensambla y sigue verde»— y
    su docstring lo defendía: «`read(2)` puede devolver menos sin que el fichero
    haya terminado». Es cierto en general y NO es la ruta normal de un testigo de
    ~138 bytes en un volumen local: ahí un corte es la señal de que el almacén que
    estamos acreditando va mal, y ensamblar convertía esa señal en verde.

    El tamaño se conoce por `fstat` SOBRE EL MISMO descriptor, así que se exige la
    lectura completa de una vez. Cualquier corte es Rojo, y en la sonda, un corte.
    """
    veces = {"n": 0}
    _trocear_solo(monkeypatch, banco["journal_dir"] / pf.TESTIGO, veces)
    matados = []
    rc = pf.readiness(banco["env"], stat_fn=stat_distinto,
                      raiz_ledgers=raiz_ledgers(banco),
                      kill_fn=lambda pid, sig: matados.append((pid, sig)),
                      acreditar_fn=_acredita())
    monkeypatch.undo()
    assert veces["n"] == 1, ("el troceo no llegó a ocurrir, o se reintentó: el "
                             "camino exacto lee UNA vez y no ensambla")
    assert rc == 1, "una lectura parcial del testigo no puede salir verde"
    assert matados == [(1, signal.SIGTERM)], "el rojo del testigo tiene que cortar"


def test_NOGO4_una_lectura_PARCIAL_del_PEPPER_se_ENSAMBLA_y_sigue_verde(banco, monkeypatch):
    """⊕ que separa la cura de un rechazo CIEGO, y es la mitad que sobrevive del
    test anterior. Endurecer el testigo no puede convertir la sonda en una que se
    pone roja ante un kernel que se comporta como el manual dice: el pepper se
    lee por el camino que ENSAMBLA (`_leer_fd` sin `exacto=`) y un troceo suyo
    sigue siendo verde.

    Sin este control, el ⊖ de arriba pasaría igual con `_leer_fd` rechazando
    TODA lectura corta, y nadie lo vería."""
    veces = {"n": 0}
    _trocear_solo(monkeypatch, banco["pepper"], veces)
    matados = []
    rc = pf.readiness(banco["env"], stat_fn=stat_distinto,
                      raiz_ledgers=raiz_ledgers(banco),
                      kill_fn=lambda pid, sig: matados.append((pid, sig)),
                      acreditar_fn=_acredita())
    monkeypatch.undo()
    assert veces["n"] > 1, "el troceo no llegó a ocurrir: el test no mide nada"
    assert rc == 0 and matados == [], "una lectura troceada del pepper no es un fallo"


# ── P1/P2-b · el estreno no puede escribir fuera de su raíz autorizada ───────

def _hijo(banco, nombre, con_senal=True):
    d = pathlib.Path(raiz_ledgers(banco)) / nombre
    d.mkdir()
    if con_senal:
        (d / pf.SENAL_LEDGER).write_text("# ledger\n", encoding="utf-8")
    return d


def test_NOGO4_la_SEÑAL_que_es_ENLACE_a_fuera_es_ROJA(banco, tmp_path):
    """🩸 El defecto, reproducido antes de curarlo: `os.path.isfile` SIGUE
    enlaces, así que plantar un `LEDGER.md` que apunta a un fichero externo hacía
    `isfile -> True` y **`estrenar()` devolvía las DOS huellas acuñando
    `.llminbox-ledger-id` en un directorio arbitrario** (medido). La señal es lo
    ÚNICO que separa «el ledger del carril» de «una carpeta cualquiera», y se
    comprobaba por la ruta.

    Es la TERCERA vez que esta familia aparece en este fichero: `exists` en
    `hay_datos` (H5), el testigo abierto por ruta (`49379af`), y ahora la señal."""
    _sin_testigos(banco)
    externo = tmp_path / "FUERA-no-soy-un-ledger"
    externo.write_text("cualquier cosa\n", encoding="utf-8")
    hijo = _hijo(banco, "impostor", con_senal=False)
    (hijo / pf.SENAL_LEDGER).symlink_to(externo)
    env = dict(banco["env"], LLMINBOX_LEDGER_PILOTO=str(hijo))
    with pytest.raises(pf.Rojo) as e:
        pf.estrenar(env, raiz_ledgers=raiz_ledgers(banco), stat_fn=stat_distinto)
    assert "ENLACE" in str(e.value) and pf.SENAL_LEDGER in str(e.value)
    assert not (hijo / pf.TESTIGO_LEDGER).exists()


def test_NOGO4_un_hijo_SIN_señal_sigue_siendo_ROJO(banco):
    """⊕ del discriminador: con un hijo VÁLIDO de la raíz y sin señal, la guarda
    que muerde es la de la señal y no la del hijo. Sin este control, la guarda
    nueva podría estar tapando a la vieja y nadie lo vería."""
    _sin_testigos(banco)
    hijo = _hijo(banco, "sin-senal", con_senal=False)
    env = dict(banco["env"], LLMINBOX_LEDGER_PILOTO=str(hijo))
    with pytest.raises(pf.Rojo) as e:
        pf.estrenar(env, raiz_ledgers=raiz_ledgers(banco), stat_fn=stat_distinto)
    assert f"falta `{pf.SENAL_LEDGER}`" in str(e.value)
    assert "hijo DIRECTO" not in str(e.value), "murió por la guarda equivocada"


@pytest.mark.parametrize("forma", ["padre", "nieto", "escape-con-puntos", "absoluta-ajena"])
def test_NOGO4_el_piloto_tiene_que_ser_hijo_DIRECTO_y_canonico(banco, tmp_path, forma):
    """`raiz/<un-solo-segmento>`: nada de `..`, ni nietos, ni rutas ajenas. El
    `openat` resuelve un SEGMENTO, no una ruta, así que ninguna escritura puede
    salir de la raíz autorizada — que es lo que el cierre pide DEMOSTRAR, no
    afirmar."""
    _sin_testigos(banco)
    raiz = raiz_ledgers(banco)
    ruta = {
        "padre": raiz,
        "nieto": str(pathlib.Path(raiz) / "llminbox" / "dentro"),
        "escape-con-puntos": str(pathlib.Path(raiz) / "llminbox" / ".." / ".." / "fuga"),
        "absoluta-ajena": str(tmp_path / "otra-parte"),
    }[forma]
    env = dict(banco["env"], LLMINBOX_LEDGER_PILOTO=ruta)
    with pytest.raises(pf.Rojo) as e:
        pf.estrenar(env, raiz_ledgers=raiz, stat_fn=stat_distinto)
    assert "hijo DIRECTO" in str(e.value), f"[{forma}] murió por otra guarda: {e.value}"


def test_NOGO4_un_NEGATIVO_deja_CERO_testigos(banco, tmp_path):
    """🔑 EL ORDEN ES LA MITAD DE LA CURA, y esto es lo que lo acredita.

    Antes se acuñaba el testigo del JOURNAL y sólo DESPUÉS se miraba el ledger,
    así que un ledger inválido dejaba MEDIO estreno: un testigo huérfano en el
    volumen y ninguna huella en la mano del operador — el peor estado posible,
    porque el volumen ya parece estrenado y su huella no la tiene nadie. Ahora se
    valida TODO antes de escribir NADA.

    ⊖ del propio test: se comprueba el testigo del JOURNAL, que es el que se
    acuñaba primero; mirar sólo el del ledger no distinguiría las dos versiones."""
    _sin_testigos(banco)
    externo = tmp_path / "FUERA"; externo.write_text("x\n", encoding="utf-8")
    hijo = _hijo(banco, "medio-estreno", con_senal=False)
    (hijo / pf.SENAL_LEDGER).symlink_to(externo)
    env = dict(banco["env"], LLMINBOX_LEDGER_PILOTO=str(hijo))
    with pytest.raises(pf.Rojo):
        pf.estrenar(env, raiz_ledgers=raiz_ledgers(banco), stat_fn=stat_distinto)
    assert not (banco["journal_dir"] / pf.TESTIGO).exists(), (
        "acuñó el testigo del JOURNAL antes de validar el ledger: medio estreno")
    assert not (hijo / pf.TESTIGO_LEDGER).exists()


def test_NOGO4_la_RAIZ_de_ledgers_que_es_ENLACE_es_ROJA(banco, tmp_path):
    """El root también va con `O_DIRECTORY|O_NOFOLLOW`: si la raíz autorizada es
    un enlace, todo lo que cuelga de ella queda anclado fuera y las guardas del
    hijo serían correctas sobre el árbol equivocado."""
    _sin_testigos(banco)
    fuera = tmp_path / "raiz-de-fuera"; fuera.mkdir()
    enlace = tmp_path / "raiz-enlazada"; enlace.symlink_to(fuera)
    with pytest.raises(pf.Rojo) as e:
        pf.estrenar(banco["env"], raiz_ledgers=str(enlace), stat_fn=stat_distinto)
    assert "ENLACE" in str(e.value) or "enlace" in str(e.value)
    assert not (fuera / pf.TESTIGO_LEDGER).exists()


def test_NOGO4_el_estreno_BUENO_sigue_pasando_entero(banco):
    """⊕ imprescindible: seis rechazos nuevos podrían ser «rechaza siempre». Cierra
    el lazo hasta el arranque, que es lo único que acredita que la cadena entera
    sigue viva."""
    _sin_testigos(banco)
    salida = pf.estrenar(banco["env"], raiz_ledgers=raiz_ledgers(banco),
                         stat_fn=stat_distinto)
    assert len(salida) == 2
    env = dict(banco["env"], **salida)
    assert len(pf.preflight(env, stat_fn=stat_distinto,
                            raiz_ledgers=raiz_ledgers(banco))) == 6


def test_NOGO4_la_RED_FINAL_atrapa_lo_que_NO_es_Rojo_y_CORTA(banco, monkeypatch):
    """🩸 HUECO PROPIO que cazó el arnés de mutantes, y lo cuento porque es la
    clase: apagar la red final (`except Exception`) NO mataba a nadie. Motivo —
    `_leer_fd` convierte el `EIO` en `Rojo`, así que mi test del EIO entraba por
    el PRIMER `except Rojo` y **la red final no se ejercitaba jamás**. Tenía una
    guarda nueva sin un solo caso que pasara por ella, y los otros tests la
    tapaban dándome verde.

    Aquí se levanta algo que NO es `Rojo` ni `OSError` desde dentro del preflight:
    la sonda tiene que darlo por rojo y CORTAR igual, porque un fallo no tipado es
    tan rojo como uno tipado — y antes se escapaba entero."""
    def revienta(*a, **k):
        raise ValueError("un fallo que nadie tipó")

    monkeypatch.setattr(pf, "comprobar_pepper", revienta)
    matados = []
    rc = pf.readiness(banco["env"], stat_fn=stat_distinto,
                      raiz_ledgers=raiz_ledgers(banco),
                      kill_fn=lambda pid, sig: matados.append((pid, sig)),
                      acreditar_fn=_acredita())
    monkeypatch.undo()
    assert rc == 1
    assert matados == [(1, signal.SIGTERM)], "la red final no cortó"


def test_NOGO4_una_SEÑAL_que_no_es_fichero_REGULAR_es_ROJA(banco):
    """Otro hueco que cazó el arnés: `S_ISREG` sobre la señal no tenía falsador.
    Un bind-mount de fichero ausente Docker lo sustituye por un DIRECTORIO —la
    cicatriz que este fichero ya nombra para el testigo—, así que un `LEDGER.md`
    que es directorio tiene que ser Rojo con nombre y no un `EISDIR` crudo."""
    _sin_testigos(banco)
    hijo = _hijo(banco, "senal-directorio", con_senal=False)
    (hijo / pf.SENAL_LEDGER).mkdir()
    env = dict(banco["env"], LLMINBOX_LEDGER_PILOTO=str(hijo))
    with pytest.raises(pf.Rojo) as e:
        pf.estrenar(env, raiz_ledgers=raiz_ledgers(banco), stat_fn=stat_distinto)
    assert "regular" in str(e.value)
    assert not (hijo / pf.TESTIGO_LEDGER).exists()


# ── NO-GO del auditor sobre `a910082` · la limpieza del estreno tenía un camino
#    sin diagnóstico ────────────────────────────────────────────────────────────

def _virgen(tmp_path, banco):
    """Un par journal+ledger SIN estrenar, para que `estrenar` acuñe de verdad.

    El `banco` llega con los dos testigos ya puestos, así que sobre él `estrenar`
    es idempotente y `acunado_j` sale vacío — justo la rama que NO quiero probar.
    """
    jd = tmp_path / "j-virgen"; jd.mkdir()
    raiz = tmp_path / "ledgers-virgen"; raiz.mkdir()
    pil = raiz / "llminbox"; pil.mkdir()
    (pil / pf.SENAL_LEDGER).write_text("# carril piloto\n", encoding="utf-8")
    env = dict(banco["env"])
    env["LLMINBOX_JOURNAL"] = str(jd / "coordination.sqlite")
    env["LLMINBOX_LEDGER_PILOTO"] = str(pil)
    return env, jd, pil, raiz


def test_NOGO5_segundo_acunado_FALLA_y_la_limpieza_SALE_BIEN(banco, tmp_path):
    """⊖ ①: el testigo del ledger falla y el del journal —que acuñé YO en esta
    corrida— se retira sin residuo.

    Lo que se exige aquí es que el error que sube sea el ORIGINAL: si la limpieza
    salió bien no hay nada que denunciar, y sustituirlo por un mensaje de «estreno
    a medias» le escondería al operador la causa de verdad."""
    env, jd, pil, raiz = _virgen(tmp_path, banco)
    pil.chmod(0o555)                      # el segundo acuñado no puede crear
    try:
        with pytest.raises(pf.Rojo) as e:
            pf.estrenar(env, stat_fn=stat_distinto, raiz_ledgers=str(raiz))
    finally:
        pil.chmod(0o755)
    assert "HUÉRFANO" not in str(e.value), (
        "la limpieza salió bien: no hay huérfano que denunciar")
    assert "ledger del piloto" in str(e.value) or "estrenar el testigo" in str(e.value)
    assert not (jd / pf.TESTIGO).exists(), "quedó residuo con la limpieza en verde"
    assert not (pil / pf.TESTIGO_LEDGER).exists()


def test_NOGO5_segundo_acunado_FALLA_y_la_limpieza_TAMBIEN_denuncia_el_huerfano(
        banco, tmp_path, monkeypatch):
    """⊖ ②, y es el camino que `a910082` dejaba MUDO: se ignoraba el `False` de
    `_retirar`, así que un `unlink` fallido —permisos, `EIO`, un `ro` que aparece
    en medio— dejaba el `.volume-id` huérfano, cero huellas devueltas y ni una
    palabra. El arranque siguiente lo lee como un estreno bueno y devuelve su
    huella, TAPANDO este fallo.

    Se exige lo que hace falta para recuperar a mano: que lo llame huérfano, que dé
    la RUTA exacta, que numere los pasos, que diga que no se declare ningún
    `_WITNESS`, y que la causa original ni se pierda (`__cause__`) ni se quede sólo
    en el traceback (el operador ve el mensaje, no el traceback)."""
    real_unlink = os.unlink

    def unlink_que_falla(nombre, *a, **k):
        if nombre == pf.TESTIGO:
            raise OSError(errno.EACCES, "Permission denied")
        return real_unlink(nombre, *a, **k)

    env, jd, pil, raiz = _virgen(tmp_path, banco)
    pil.chmod(0o555)
    monkeypatch.setattr(os, "unlink", unlink_que_falla)
    try:
        with pytest.raises(pf.Rojo) as e:
            pf.estrenar(env, stat_fn=stat_distinto, raiz_ledgers=str(raiz))
    finally:
        monkeypatch.undo()
        pil.chmod(0o755)

    texto = str(e.value)
    assert "HUÉRFANO" in texto, texto
    assert str(jd / pf.TESTIGO) in texto, "no da la ruta que hay que borrar"
    assert "RECUPERACIÓN MANUAL" in texto and "①" in texto
    assert "_WITNESS" in texto, "no dice que no se declare la huella"
    assert "Permission denied" in texto or "EACCES" in texto, (
        "no dice POR QUÉ no pudo retirarlo")
    assert e.value.__cause__ is not None, "se perdió la causa original"
    assert (jd / pf.TESTIGO).exists(), (
        "el huérfano tiene que seguir ahí: es lo que se está denunciando")


def test_NOGO5_retirar_con_el_fsync_ROTO_no_dice_que_retiro(tmp_path, monkeypatch):
    """`unlink` devuelve éxito cuando el borrado está en el caché de entradas, no
    cuando está en el disco. Sin el `fsync` del directorio, un corte entre los dos
    deja el testigo VIVO — el residuo que `_retirar` existe para no dejar.

    Por eso un `fsync` que falla se devuelve como NO retirado: una retirada que no
    es durable no es una retirada. ⊕ al lado: con el `fsync` bueno, sí lo dice."""
    d = tmp_path / "d"; d.mkdir()
    (d / pf.TESTIGO).write_text("x", encoding="utf-8")
    dirfd = os.open(str(d), os.O_RDONLY | os.O_DIRECTORY)
    try:
        monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(
            OSError(errno.EIO, "Input/output error")))
        quitado, por_que = pf._retirar(pf.TESTIGO, dirfd)
        monkeypatch.undo()
        assert quitado is False, "un borrado no durable no es un borrado"
        assert "ENTRADA del directorio" in por_que and "EIO" in por_que.upper() or \
               "Input/output" in por_que, por_que

        # ⊕ del par: sin romper el `fsync`, la MISMA función sí retira. Sin esto,
        # un `_retirar` que devolviera `False` siempre pasaría el ⊖ de arriba.
        (d / pf.TESTIGO).write_text("x", encoding="utf-8")
        quitado, por_que = pf._retirar(pf.TESTIGO, dirfd)
        assert quitado is True and por_que == ""
        assert not (d / pf.TESTIGO).exists()
    finally:
        os.close(dirfd)


# ── correctiva sobre `acea43ce` · modos excluyentes y mapa por descriptor ────

@pytest.mark.parametrize("argv", [
    ["--readiness", "--genera-id"],
    ["--genera-id", "--readiness"],
    ["--init", "--genera-id"],
    ["--init", "--readiness"],
])
def test_SEC_los_modos_son_EXCLUYENTES_y_ninguno_sale_verde(argv, capsys):
    """🩸 @security (`MARK:security-retries-no-aplica-al-corte-del-preflight` §2):
    `--genera-id` vivía en el `if __name__`, ANTES de `main()` y por tanto antes de
    toda comprobación ⇒ `--readiness --genera-id` imprimía un uuid y **salía `0`:
    healthcheck VERDE sin comprobar una sola precondición.**

    No era explotable con el compose de hoy —los argumentos están fijados ahí— y es
    la única ruta que no debería existir en una herramienta cuyo valor entero es el
    fail-closed: el fichero YA guardaba el par menos peligroso (`--init` +
    `--readiness`) y dejaba abierto el que devuelve verde sin mirar nada.

    Se parametrizan LOS DOS ÓRDENES porque una guarda que mire `argv[0]` pasaría uno
    de los dos, y el par viejo para que la cura no lo pierda por el camino."""
    rc = pf.main(argv)
    assert rc == 2, f"{argv} -> rc={rc}"
    assert "EXCLUYENTES" in capsys.readouterr().err


def test_SEC_genera_id_SOLO_sigue_funcionando(capsys):
    """⊕ imprescindible: la utilidad de despliegue sigue viva. Sin este control,
    romper `--genera-id` del todo pasaría los cuatro ⊖ de arriba — y el compose del
    estreno lo cita en su propio mensaje de `:?`."""
    import uuid as _uuid
    assert pf.main(["--genera-id"]) == 0
    _uuid.UUID(capsys.readouterr().out.strip())      # es un uuid de verdad


def test_SEC_ninguna_ruta_de_argv_salta_main():
    """La guarda de la guarda: el `if __name__` no puede volver a tener una salida
    propia. Ahí es donde vivía la que devolvía `0` sin comprobar nada."""
    fuente = (RAIZ / "tools" / "pilot_preflight.py").read_text(encoding="utf-8")
    cola = fuente[fuente.index('if __name__ == "__main__":'):]
    assert "SystemExit" in cola
    assert cola.count("SystemExit") == 1, "hay más de una salida en el `if __name__`"
    assert "sys.argv" in cola and "main(" in cola


def test_SEC_el_mapa_por_ENLACE_es_ROJO_aunque_el_atestado_case(banco, tmp_path):
    """🩸 @security §3: el mapa se abría como el pepper NO se abre —`os.path.isfile`
    + lectura por RUTA— que es el patrón que el docstring de `comprobar_pepper`
    explica que está mal, doce funciones más arriba.

    ✅ Su lectura es más fina que «hay un agujero», y la adopto: hoy la sustitución
    NO pasa, porque el `sha256` se calcula sobre los bytes LEÍDOS. El defecto es de
    DEPENDENCIA — la seguridad del mapa colgaba entera del atestado y eso no estaba
    escrito—. El día que alguien haga `LLMINBOX_CREDENCIALES_SHA` opcional «para
    desarrollo», la vía del enlace se reabre sola.

    🔑 Por eso este falsador pone el atestado del DESTINO: con el sha casando, lo
    ÚNICO que puede dar rojo es la apertura. Si el rojo viniera del atestado, el
    test pasaría con la cura desconectada y no mediría nada."""
    real = tmp_path / "mapa-de-otro.json"
    real.write_bytes((banco["mapa"]).read_bytes())
    enlace = tmp_path / "mapa-enlazado.json"
    enlace.symlink_to(real)
    env = dict(banco["env"])
    env["LLMINBOX_CREDENCIALES"] = str(enlace)
    env["LLMINBOX_CREDENCIALES_SHA"] = hashlib.sha256(real.read_bytes()).hexdigest()
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_mapa(env)
    assert "ENLACE" in str(e.value), str(e.value)


def test_SEC_el_mapa_que_es_un_DIRECTORIO_lo_dice_por_su_nombre(banco, tmp_path):
    """Un bind-mount de fichero AUSENTE Docker lo sustituye por un DIRECTORIO vacío.
    Con la apertura por descriptor eso ya no llega como «no es JSON»: `fstat` sobre
    el fd dice que no es regular, que es el diagnóstico correcto."""
    d = tmp_path / "mapa-directorio.json"; d.mkdir()
    env = dict(banco["env"]); env["LLMINBOX_CREDENCIALES"] = str(d)
    with pytest.raises(pf.Rojo) as e:
        pf.comprobar_mapa(env)
    assert "no es un fichero regular" in str(e.value)
    assert "JSON" not in str(e.value).split("DIRECTORIO")[0]


def test_SEC_el_mapa_BUENO_sigue_cargando(banco):
    """⊕ del par: endurecer la apertura no puede romper el camino bueno. Sin esto,
    un `comprobar_mapa` que rechazara todo dejaría los tres ⊖ de arriba verdes."""
    mapa = pf.comprobar_mapa(banco["env"])
    assert isinstance(mapa, dict) and mapa
