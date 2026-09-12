"""M4 · preparación de piloto: build anclado, contrato de salud separado y gates.

Este fichero prueba INFRAESTRUCTURA, no el journal nativo: M1 no está integrado. Donde
algo depende de M1 hay un **adaptador fail-closed** y un **cable-trampa**, no un `skip`.
La diferencia importa: un `skip` declara cubierto lo que no existe, y el runbook de M4
ya nombra ese patrón como la forma habitual de que un gate ausente se lea como verde.

Los gates de shell se ejercitan con dobles de `docker` y `curl` en el PATH — el mismo
idioma que el resto de la suite— porque lo que hay que probar es que **saben decir que
no**, y eso no se puede probar contra un despliegue real sin desplegar.
"""
from __future__ import annotations

import json
import copy
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
LIB = RAIZ / "scripts" / "m4-evidencia.sh"


# ══ 1 · el destino de una reversión no puede ser móvil ═══════════════════════════
@pytest.mark.parametrize("destino,acepta,porque", [
    ("img@sha256:" + "6f" * 32, True, "digest completo: inmutable por construcción"),
    ("img:rollback-80e919900c18", True, "etiqueta de anclaje que nombra un sha"),
    ("img:v1.4.0", True, "versión fijada"),
    ("img:latest", False, "MÓVIL: no nombra un estado, nombra lo que haya al ejecutarla"),
    ("img", False, "sin etiqueta resuelve a latest"),
    ("img:qa-15dc61a", False, "etiqueta de trabajo: nadie garantiza que no se reasigne"),
    ("img@sha256:zz", False, "digest malformado"),
])
def test_destino_de_reversion(destino, acepta, porque):
    r = subprocess.run(["bash", "-c", f'. "{LIB}"; destino_inmutable "{destino}"'],
                       capture_output=True, text=True, timeout=30)
    assert (r.returncode == 0) is acepta, f"{destino}: {porque}\n{r.stderr}"


# ══ 2 · outbox: fail-closed y con nombres ════════════════════════════════════════
def _api_falsa(tmp_path, salud: dict, nombre="curl"):
    """Un `curl` de mentira que sirve un `/health` concreto. Devuelve el dir del PATH."""
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    (b / nombre).write_text("#!/bin/sh\ncat <<'JSON'\n" + json.dumps(salud) + "\nJSON\n")
    (b / nombre).chmod(0o755)
    return b


def _corre(tmp_path, fn, salud, docker=None):
    b = _api_falsa(tmp_path, salud)
    if docker is not None:
        (b / "docker").write_text(docker)
        (b / "docker").chmod(0o755)
    for real in ("jq", "mktemp", "date"):
        p = shutil.which(real)
        if p and not (b / real).exists():
            (b / real).symlink_to(p)
    return subprocess.run(["bash", "-c", f'. "{LIB}"; {fn}'], capture_output=True,
                          text=True, timeout=60,
                          env={"PATH": f"{b}:/usr/bin:/bin:/usr/sbin:/sbin"})


OUTBOX_OK = {"politica": {"outbox": {"disponible": True, "pendientes": 0,
                                     "fallidos": 0, "pares": []}}}
OUTBOX_SUCIO = {"politica": {"outbox": {
    "disponible": True, "pendientes": 3, "fallidos": 1,
    "pares": [{"lane": "64bis", "verb": "post", "pendientes": 2, "fallidos": 0},
              {"lane": "llminbox", "verb": "claim", "pendientes": 1, "fallidos": 1}]}}}
OUTBOX_AUSENTE = {"politica": {"outbox": {"disponible": False, "pendientes": None,
                                          "fallidos": None, "pares": None,
                                          "motivo": "sin outbox en el producto"}}}


def test_outbox_drenado_deja_pasar(tmp_path):
    """CONTROL POSITIVO. Sin esto, una guarda que se niega SIEMPRE parecería correcta."""
    r = _corre(tmp_path, 'gate_outbox "http://x"', OUTBOX_OK)
    assert r.returncode == 0, r.stderr


def test_outbox_pendiente_se_niega_y_NOMBRA_los_pares(tmp_path):
    r = _corre(tmp_path, 'gate_outbox "http://x"', OUTBOX_SUCIO)
    assert r.returncode != 0, "revirtió con el outbox sucio"
    for lane, verb in (("64bis", "post"), ("llminbox", "claim")):
        assert lane in r.stderr and verb in r.stderr, (
            f"no nombra ({lane}, {verb}). Con un solo par, una implementación que "
            f"imprima el primero y calle el resto saldría verde:\n{r.stderr}")


def test_outbox_NO_CERTIFICABLE_se_niega_igual(tmp_path):
    """El caso de HOY. «No se puede consultar» no es «está vacío»."""
    r = _corre(tmp_path, 'gate_outbox "http://x"', OUTBOX_AUSENTE)
    assert r.returncode != 0, (
        "autorizó la reversión sin poder consultar el outbox: eso es fail-OPEN")
    assert "NO CERTIFICABLE" in r.stderr


# ══ 3 · los campos de integridad se comprueban por PRESENCIA ═════════════════════
V8_OK = {"v8": {"integridad": "verificada", "mapa_alterado": False, "credenciales": 1}}
V8_ALTERADO = {"v8": {"integridad": "alterada", "mapa_alterado": True, "credenciales": 0}}
# Exactamente lo que devuelve una imagen anterior al gate: SIN las claves, y el resto
# del bloque se ve sano. Medido en M0 al retroceder a `15dc61a`.
V8_VIEJO = {"v8": {"configurado": True, "credenciales": 1, "roles_cubiertos": 1}}

_RUNNER_FILES = [
    "coordination.py", "ledger_parse.py", "native_gateway.py", "observability.py",
    "projector.py", "projector_runner.py", "runtime_root.py", "search_contract.py",
    "search_cursor.py", "search_store.py", "servicio.py", "telemetry_bridge.py",
]
ARTEFACTO_GATE_OK = {
    "artefacto": {
        "disponible": True,
        "esquema": 1,
        "requirements_lock_sha256": "a" * 64,
        "fuentes_sha256": {name: "b" * 64 for name in _RUNNER_FILES},
        "runner_empaquetado": {
            "entrypoints": ["runtime_root", "projector", "projector_runner"],
            "exigidos": _RUNNER_FILES,
            "faltan": [],
            "completo": True,
        },
    }
}


@pytest.mark.parametrize("salud,ok,porque", [
    (V8_OK, True, "claves presentes y verificada"),
    (V8_ALTERADO, False, "claves presentes, valor malo"),
    (V8_VIEJO, False, "SIN claves: su ausencia es roja, no verde"),
])
def test_gate_integridad(tmp_path, salud, ok, porque):
    r = _corre(tmp_path, 'gate_integridad "http://x"', salud)
    assert (r.returncode == 0) is ok, f"{porque}\n{r.stderr}"


def test_la_imagen_vieja_no_puede_leerse_como_verde(tmp_path):
    """El falsador que da nombre al P0-B5 del runbook."""
    r = _corre(tmp_path, 'gate_integridad "http://x"', V8_VIEJO)
    assert "AUSENCIA es rojo" in r.stderr


@pytest.mark.parametrize("artefacto,porque", [
    ({"disponible": True}, "el booleano solo no es evidencia"),
    (ARTEFACTO_GATE_OK["artefacto"] | {"requirements_lock_sha256": "a" * 63},
     "un lock sin SHA-256 exacto no ata la imagen"),
    (ARTEFACTO_GATE_OK["artefacto"] | {"requirements_lock_sha256": "a" * 64 + "\n"},
     "el ancla `$` de jq casa antes de newline: la longitud debe ser exacta"),
    (ARTEFACTO_GATE_OK["artefacto"] | {"runner_empaquetado": {
        **ARTEFACTO_GATE_OK["artefacto"]["runner_empaquetado"], "completo": False}},
     "un runner incompleto no es activable"),
])
def test_gate_artefacto_rechaza_autodescripcion_debil(tmp_path, artefacto, porque):
    r = _corre(tmp_path, 'gate_artefacto "http://x"', {"artefacto": artefacto})
    assert r.returncode != 0, porque


def test_gate_artefacto_acepta_el_contrato_completo(tmp_path):
    r = _corre(tmp_path, 'gate_artefacto "http://x"', ARTEFACTO_GATE_OK)
    assert r.returncode == 0, r.stderr


# ══ 4 · gate post-recreate ═══════════════════════════════════════════════════════
def _docker_falso(estado="running", desde="2026-09-05T05:00:00Z", reinicios=0,
                  cid="b" * 64):
    payload = json.dumps([{
        "Id": cid, "Created": "2026-09-05T04:59:00Z", "Image": "sha256:" + "c" * 64,
        "State": {"Status": estado, "StartedAt": desde, "Health": {"Status": "healthy"}},
        "Config": {"Image": "img:latest"}, "RestartCount": reinicios, "Mounts": []}])
    return "#!/bin/sh\n[ \"$1\" = inspect ] && { cat <<'JSON'\n" + payload + "\nJSON\nexit 0; }\nexit 0\n"


@pytest.mark.parametrize("kw,ok,porque", [
    ({}, True, "id nuevo, running, fecha real, 0 reinicios"),
    ({"cid": "a" * 64}, False, "MISMO id que antes ⇒ fue un restart, no un recreate"),
    ({"estado": "created", "desde": "0001-01-01T00:00:00Z"}, False,
     "nunca arrancó: RestartCount=0 también es cierto ahí"),
    ({"reinicios": 2}, False, "RestartCount != 0"),
])
def test_gate_post_recreate(tmp_path, kw, ok, porque):
    r = _corre(tmp_path, 'gate_post_recreate inst ' + "a" * 64 + ' "http://x"',
               V8_OK | ARTEFACTO_GATE_OK, docker=_docker_falso(**kw))
    assert (r.returncode == 0) is ok, f"{porque}\nstdout={r.stdout}\nstderr={r.stderr}"


def test_recreate_falso_se_nombra_como_restart(tmp_path):
    r = _corre(tmp_path, 'gate_post_recreate inst ' + "a" * 64 + ' "http://x"',
               V8_OK | ARTEFACTO_GATE_OK,
               docker=_docker_falso(cid="a" * 64))
    assert "RECREATE FALSO" in r.stderr and "restart" in r.stderr


# ══ 5 · build anclado (estático, no construye) ═══════════════════════════════════
def test_las_bases_van_por_digest_no_por_tag():
    froms = [l for l in _dockerfile_efectivo().splitlines() if l.startswith("FROM ")]
    assert froms, "sin líneas FROM: el falsador no tiene sujeto"
    for l in froms:
        assert re.search(r"@sha256:[0-9a-f]{64}", l), f"base por etiqueta móvil: {l}"


def _dockerfile_efectivo() -> str:
    """Las INSTRUCCIONES, sin comentarios.

    Mi primera versión escaneaba el fichero entero y se ponía roja por su propio
    comentario: la cura explica qué pines había antes (`==0.121.*`) y el localizador
    contaba esa prosa como si fuera un pin vivo. El contaminante lo escribe quien cura,
    y por eso se mide la estructura y no el texto.
    """
    return "\n".join(l for l in (RAIZ / "Dockerfile").read_text().splitlines()
                      if not l.lstrip().startswith("#"))


def test_ninguna_dependencia_flota():
    d = _dockerfile_efectivo()
    assert "--require-hashes" in d, "pip resolvería por su cuenta en cada build"
    assert not re.search(r'==\d+\.\d+\.\*', d), "quedan rangos abiertos en el Dockerfile"
    # CONTROL POSITIVO del propio localizador: si el filtro de comentarios se comiera
    # todo, este test pasaría por vacío. El fichero efectivo tiene que traer FROM y RUN.
    assert "FROM " in d and "RUN " in d, "el filtro dejó el Dockerfile vacío"


def test_el_lock_trae_hash_para_TODO_paquete():
    txt = (RAIZ / "requirements.lock").read_text()
    paquetes = re.findall(r"^([A-Za-z0-9._-]+)==", txt, re.M)
    assert len(paquetes) >= 15, f"lock sospechosamente corto: {paquetes}"
    bloques = re.split(r"^(?=[A-Za-z0-9._-]+==)", txt, flags=re.M)
    sin_hash = [b.split("==")[0] for b in bloques
                if re.match(r"^[A-Za-z0-9._-]+==", b) and "--hash=sha256:" not in b]
    assert not sin_hash, f"sin hash: {sin_hash} — `--require-hashes` los rechazaría"


def test_el_artefacto_se_atestigua_en_el_build():
    d = (RAIZ / "Dockerfile").read_text()
    assert "atestigua_artefacto.py" in d and "ARTEFACTO.json" in d
    src = (RAIZ / "atestigua_artefacto.py").read_text()
    for pieza in ("sqlite_version", "_vfs", "fts5", "compile_options",
                  "requirements_lock_sha256"):
        assert pieza in src, f"el atestado no cubre {pieza}"
    assert "return {\"nombre\": None" in src or '"medido": False' in src, (
        "el VFS tiene que poder declararse NO MEDIBLE; afirmar 'unix' porque es lo "
        "normal en Linux sería convertir una expectativa en una medida")
    for fuente_autoritativa in (
            "coordination.py", "native_gateway.py", "runtime_root.py",
            "projector_runner.py"):
        assert fuente_autoritativa in src, (
            f"el artefacto empaqueta {fuente_autoritativa} pero no liga su digest")


# ══ 6 · contrato de salud separado, y el adaptador M1 ════════════════════════════
def test_health_separa_indice_journal_y_politica(servicio):
    for f in ("_indice", "_journal", "_politica", "_artefacto"):
        assert hasattr(servicio, f), f"falta {f}: el contrato no está separado"
    ind = servicio._indice()
    assert "reconstruible" in ind and ind["reconstruible"] is True
    assert ind.get("medido_por") == "os.access(W_OK)", (
        "hay que declarar CÓMO se midió: `os.access` no es una escritura probada")


def test_journal_ausente_NO_se_declara_sano(servicio):
    j = servicio._journal()
    assert j["disponible"] is False and j["motivo"] == "M1_NO_INTEGRADO"
    assert j["reconstruible"] is False, "el journal es dato de usuario, no caché"


def test_artefacto_sin_fichero_es_fail_closed(servicio, tmp_path, monkeypatch):
    monkeypatch.setattr(servicio, "_ARTEFACTO_DIRECTORIO", str(tmp_path))
    monkeypatch.setattr(servicio, "_ARTEFACTO_NOMBRE", "no-existe.json")
    a = servicio._artefacto()
    assert a["disponible"] is False and "motivo" in a


def _artefacto_valido(servicio):
    digest = "a" * 64
    return {
        "esquema": 1,
        "python": {"implementacion": "CPython", "version": "3.13.14"},
        "sqlite": {
            "version_biblioteca": "3.46.1", "threadsafety": 3,
            "compile_options": ["ENABLE_FTS5"],
            "fts5": {"disponible": True,
                     "medido_por": "CREATE VIRTUAL TABLE"},
            "vfs": {"medido": True, "nombre": "unix", "via": "libsqlite3"},
        },
        "paquetes": {name: "1.0" for name in servicio._ARTEFACTO_PAQUETES},
        "requirements_lock_sha256": digest,
        "fuentes_sha256": {
            name: digest for name in servicio._ARTEFACTO_FUENTES
        },
        "runner_empaquetado": {
            "completo": True,
            "entrypoints": list(servicio._ARTEFACTO_ENTRYPOINTS),
            "exigidos": list(servicio._ARTEFACTO_RUNNER_FILES),
            "faltan": [],
        },
    }


def _instala_artefacto(servicio, tmp_path, monkeypatch, data=None):
    ruta = tmp_path / "ARTEFACTO.json"
    raw = json.dumps(data or _artefacto_valido(servicio),
                     sort_keys=True, separators=(",", ":")).encode()
    ruta.write_bytes(raw)
    ruta.chmod(0o444)
    monkeypatch.setattr(servicio, "_ARTEFACTO_DIRECTORIO", str(tmp_path))
    monkeypatch.setattr(servicio, "_ARTEFACTO_NOMBRE", ruta.name)
    # El fixture vive bajo el UID de pytest; simula el usuario runtime distinto
    # sin abrir una perilla equivalente en producción.
    monkeypatch.setattr(servicio.os, "geteuid", lambda: os.getuid() + 1)
    monkeypatch.setattr(servicio.os, "access", lambda *_a, **_kw: False)
    return ruta, raw


def test_el_artefacto_publicado_no_lleva_secretos(servicio):
    validado = servicio._json_artefacto(
        json.dumps(_artefacto_valido(servicio)).encode())
    plano = json.dumps(validado).lower()
    for prohibido in ("token", "credencial", "secret", "password", "llminbox_token"):
        assert prohibido not in plano, f"`/health` responde sin token y filtra {prohibido}"


def test_artefacto_valido_se_lee_por_descriptor_estable(
        servicio, tmp_path, monkeypatch):
    _instala_artefacto(servicio, tmp_path, monkeypatch)
    result = servicio._artefacto()
    assert result["disponible"] is True
    assert set(result) == {
        "disponible", "esquema", "python", "sqlite", "paquetes",
        "requirements_lock_sha256", "fuentes_sha256", "runner_empaquetado",
    }


def test_override_de_entorno_no_redirige_el_atestado(
        servicio, tmp_path, monkeypatch):
    forged = tmp_path / "forged.json"
    forged.write_text('{"token":"EXFIL"}')
    monkeypatch.setenv("LLMINBOX_ARTEFACTO", str(forged))
    assert servicio._ARTEFACTO_DIRECTORIO == str(Path(servicio.__file__).parent)
    result = servicio._artefacto()
    assert result["disponible"] is False
    assert "EXFIL" not in json.dumps(result)


def test_symlink_y_fifo_no_son_atestados(
        servicio, tmp_path, monkeypatch):
    real = tmp_path / "real.json"
    real.write_text(json.dumps(_artefacto_valido(servicio)))
    real.chmod(0o444)
    link = tmp_path / "link.json"
    link.symlink_to(real)
    monkeypatch.setattr(servicio, "_ARTEFACTO_DIRECTORIO", str(tmp_path))
    monkeypatch.setattr(servicio.os, "geteuid", lambda: os.getuid() + 1)
    monkeypatch.setattr(servicio.os, "access", lambda *_a, **_kw: False)
    monkeypatch.setattr(servicio, "_ARTEFACTO_NOMBRE", link.name)
    assert servicio._artefacto() == {
        "disponible": False, "motivo": "ARTEFACTO_NO_CONFIABLE"}
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, 0o444)
    monkeypatch.setattr(servicio, "_ARTEFACTO_NOMBRE", fifo.name)
    assert servicio._artefacto() == {
        "disponible": False, "motivo": "ARTEFACTO_NO_CONFIABLE"}


def test_modos_y_tamano_del_atestado_son_fail_closed(
        servicio, tmp_path, monkeypatch):
    ruta, _ = _instala_artefacto(servicio, tmp_path, monkeypatch)
    ruta.chmod(0o644)
    assert servicio._artefacto()["disponible"] is False
    ruta.chmod(0o444)
    ruta.unlink()
    ruta.write_bytes(b"x" * (servicio._ARTEFACTO_MAX_BYTES + 1))
    ruta.chmod(0o444)
    assert servicio._artefacto()["disponible"] is False


def test_mutacion_durante_lectura_se_detecta(
        servicio, tmp_path, monkeypatch):
    ruta, _ = _instala_artefacto(servicio, tmp_path, monkeypatch)
    original_read = servicio.os.read
    touched = False

    def read_and_touch(fd, size):
        nonlocal touched
        chunk = original_read(fd, size)
        if chunk and not touched:
            touched = True
            os.utime(ruta, None)
        return chunk

    monkeypatch.setattr(servicio.os, "read", read_and_touch)
    assert servicio._artefacto() == {
        "disponible": False, "motivo": "ARTEFACTO_NO_CONFIABLE"}


@pytest.mark.parametrize("mutation", [
    "schema_bool", "schema_wrong", "extra_secret", "bad_digest",
    "runner_incomplete",
])
def test_formas_no_canonicas_no_se_publican(servicio, mutation):
    data = copy.deepcopy(_artefacto_valido(servicio))
    if mutation == "schema_bool":
        data["esquema"] = True
    elif mutation == "schema_wrong":
        data["esquema"] = 2
    elif mutation == "extra_secret":
        data["token"] = "EXFIL"
    elif mutation == "bad_digest":
        data["fuentes_sha256"]["projector.py"] = "a" * 63
    else:
        data["runner_empaquetado"]["completo"] = False
        data["runner_empaquetado"]["faltan"] = ["projector.py"]
    with pytest.raises(ValueError):
        servicio._json_artefacto(json.dumps(data).encode())


def test_json_duplicado_no_puede_esconder_el_esquema(servicio):
    with pytest.raises(ValueError, match="duplicada"):
        servicio._json_artefacto(b'{"esquema":1,"esquema":true}')


def test_medidas_sqlite_negativas_siguen_siendo_atestado_valido(servicio):
    """No poder medir una capacidad es un dato cerrado, no JSON de otra versión."""
    data = _artefacto_valido(servicio)
    data["sqlite"]["fts5"] = {
        "disponible": False,
        "medido_por": "CREATE VIRTUAL TABLE",
        "error": "OperationalError",
    }
    data["sqlite"]["vfs"] = {
        "medido": False,
        "nombre": None,
        "via": None,
        "motivo": "sqlite3_vfs_find no alcanzable desde este artefacto",
    }
    assert servicio._json_artefacto(json.dumps(data).encode())["sqlite"] == data["sqlite"]


@pytest.mark.parametrize("bloque,valor", [
    ("fts5", {"disponible": False, "medido_por": "CREATE VIRTUAL TABLE"}),
    ("vfs", {"medido": False, "nombre": None, "via": None}),
])
def test_medida_sqlite_negativa_exige_su_motivo_cerrado(servicio, bloque, valor):
    data = _artefacto_valido(servicio)
    data["sqlite"][bloque] = valor
    with pytest.raises(ValueError):
        servicio._json_artefacto(json.dumps(data).encode())


# ── CABLE-TRAMPA de M1, no un `skip` ────────────────────────────────────────────
# Hoy el outbox no existe (medido: 0 apariciones en el producto). Este test fija el
# estado PENDIENTE. El día que M1 aterrice y alguien encienda `LLMINBOX_M1_INTEGRADO`,
# este test SE PONE ROJO — y esa rotura es la señal de que toca escribir el falsador de
# verdad del invariante 18. Un `skip` no lo haría: seguiría verde para siempre.
def test_PENDIENTE_M1_la_politica_es_fail_closed_mientras_no_haya_outbox(servicio):
    assert servicio.M1_INTEGRADO is False, (
        "M1 se declara integrado: este cable-trampa ha cumplido su función. Escribe "
        "ahora el falsador real de «la reversión se niega con outbox pendiente y "
        "nombra cada (lane, verb)» contra la implementación, y retira este test.")
    p = servicio._politica()
    assert p["disponible"] is False and p["motivo"] == "M1_NO_INTEGRADO"
    ob = p["outbox"]
    assert ob["disponible"] is False
    # LA ASERCIÓN QUE LO SOSTIENE TODO: `None`, jamás `0`. Un cero se lee «drenado» y
    # autoriza una reversión que nadie ha comprobado.
    assert ob["pendientes"] is None and ob["fallidos"] is None, (
        "un 0 sobre un outbox inexistente se lee como «drenado»")
    assert ob["pares"] is None


def test_PENDIENTE_M1_encender_la_bandera_sin_implementar_ROMPE(servicio, monkeypatch):
    """Fail-closed también hacia delante: encender la bandera no puede publicar una
    política vacía como si fuera sana."""
    monkeypatch.setattr(servicio, "M1_INTEGRADO", True)
    with pytest.raises(NotImplementedError):
        servicio._politica()


# ══════════════════════════════════════════════════════════════════════════════════
# M4-2 · falsadores de ausencia, supervivencia, concurrencia y longitud de digest
# ══════════════════════════════════════════════════════════════════════════════════

# ── longitud del digest: 64 EXACTOS ─────────────────────────────────────────────
# Mi primera versión pedía «8 o más» con un glob, así que aceptaba las tres longitudes
# malas. Un digest corto no identifica nada y Docker lo rechazaría más tarde — ya con el
# `tag` hecho y el contenedor recreado.
@pytest.mark.parametrize("n,acepta", [(8, False), (63, False), (64, True), (65, False)])
def test_digest_longitud(n, acepta):
    ref = "img@sha256:" + "a" * n
    r = subprocess.run(["bash", "-c", f'. "{LIB}"; destino_inmutable "{ref}"'],
                       capture_output=True, text=True, timeout=30)
    assert (r.returncode == 0) is acepta, f"{n} hex\n{r.stderr}"


def test_digest_de_64_no_hex_se_rechaza():
    """CONTROL: la longitud sola no basta — 64 zetas mide 64 y no es un sha256."""
    r = subprocess.run(["bash", "-c", f'. "{LIB}"; destino_inmutable "img@sha256:{"z" * 64}"'],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode != 0 and "no hex" in r.stderr


# ── ausencia de inspect ─────────────────────────────────────────────────────────
_INSPECT_AUSENTE = '#!/bin/sh\n[ "$1" = inspect ] && { echo "Error: No such object: x" >&2; exit 1; }\nexit 0\n'
_INSPECT_AMBIGUO = '#!/bin/sh\n[ "$1" = inspect ] && { echo "Cannot connect to the Docker daemon" >&2; exit 1; }\nexit 0\n'
# El peor de los tres: sale 0 y no imprime nada. Docker no produce eso jamás —un
# contenedor que existe SIEMPRE tiene `Config.Env`—, y por eso el doble tiene que poder
# mentir así: es la forma en que un gate se vuelve verde sin haber medido.
_INSPECT_MUDO = '#!/bin/sh\n[ "$1" = inspect ] && exit 0\nexit 0\n'


@pytest.mark.parametrize("doble,etiqueta", [
    (_INSPECT_AUSENTE, "no existe el contenedor"),
    (_INSPECT_AMBIGUO, "el daemon no contesta"),
    (_INSPECT_MUDO, "inspect sale 0 y calla"),
])
def test_sin_inspect_el_gate_NO_acredita(tmp_path, doble, etiqueta):
    r = _corre(tmp_path, 'gate_post_recreate inst ' + "a" * 64 + ' "http://x"',
               V8_OK | ARTEFACTO_GATE_OK, docker=doble)
    assert r.returncode != 0, (
        f"acreditó un recreate sin poder medir el contenedor ({etiqueta}): «no lo sé» "
        f"se convirtió en «salió bien»\nstdout={r.stdout}")


def test_evidencia_distingue_ausente_de_no_medible(tmp_path):
    """Las dos son fallo, pero NO son lo mismo, y el motivo tiene que decirlo: un daemon
    que tarda se recupera solo; un contenedor que no existe, no."""
    a = _corre(tmp_path, 'evidencia_contenedor inst', {}, docker=_INSPECT_AUSENTE)
    b = _corre(tmp_path, 'evidencia_contenedor inst', {}, docker=_INSPECT_AMBIGUO)
    assert '"ausente"' in a.stdout, a.stdout
    assert "no medible" in b.stdout, b.stdout
    assert a.stdout != b.stdout, "colapsa dos estados distintos en el mismo motivo"


# ── el snapshot sobrevive a la pérdida del volumen ──────────────────────────────
def test_un_snapshot_DENTRO_del_volumen_se_rechaza(tmp_path):
    vol = tmp_path / "data"
    vol.mkdir()
    dentro = vol / "llminbox.sqlite.snapshot-x"
    dentro.write_text("x")
    r = subprocess.run(
        ["bash", "-c", f'. "{LIB}"; snapshot_fuera_del_volumen "{dentro}" "{vol}"'],
        capture_output=True, text=True, timeout=30)
    assert r.returncode != 0, "aceptó un respaldo que muere con lo que respalda"
    assert "DENTRO del volumen" in r.stderr


def test_el_snapshot_de_verdad_SOBREVIVE_a_perder_el_volumen(tmp_path):
    """No basta con comprobar la ruta: se destruye el volumen y se mira quién queda.

    Es la diferencia entre probar la regla y probar el efecto que la regla promete.
    """
    vol, host = tmp_path / "data", tmp_path / "evidencia"
    vol.mkdir(); host.mkdir()
    dentro, fuera = vol / "snap.sqlite", host / "snap.sqlite"
    dentro.write_text("respaldo"); fuera.write_text("respaldo")
    shutil.rmtree(vol)                                   # se pierde el volumen
    assert not dentro.exists(), "el montaje de prueba no simuló la pérdida"
    assert fuera.exists(), "el snapshot del host tampoco sobrevivió: la cura no cura"
    r = subprocess.run(
        ["bash", "-c", f'. "{LIB}"; snapshot_fuera_del_volumen "{fuera}" "{vol}"'],
        capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"rechazó el único respaldo que sí sobrevivió:\n{r.stderr}"


def test_snapshot_vacio_no_cuenta_como_respaldo(tmp_path):
    """CONTROL POSITIVO del control: existir no es contener."""
    host = tmp_path / "e"; host.mkdir()
    vacio = host / "snap.sqlite"; vacio.touch()
    r = subprocess.run(
        ["bash", "-c", f'. "{LIB}"; snapshot_fuera_del_volumen "{vacio}" "/data"'],
        capture_output=True, text=True, timeout=30)
    assert r.returncode != 0 and "vacío" in r.stderr


def test_el_pilotaje_saca_el_snapshot_del_volumen():
    """Estático: el guion no puede volver a dejarlo en `/data`, que es el volumen."""
    s = (RAIZ / "scripts" / "m4-pilot.sh").read_text()
    assert "docker cp" in s or '"$DOCKER" cp' in s, "no lo saca al host"
    assert 'SNAP="$EVID/' in s, "el snapshot final no vive en el directorio de evidencia"
    assert "snapshot_fuera_del_volumen" in s, "no comprueba dónde acabó"


# ── restauración concurrente prohibida ──────────────────────────────────────────
def test_dos_restauraciones_a_la_vez_se_rechazan(tmp_path):
    guion = f'. "{LIB}"; LLMI_LOCK_DIR="{tmp_path}" cerrojo_restauracion inst'
    a = subprocess.run(["bash", "-c", guion + "; sleep 0"], capture_output=True,
                       text=True, timeout=30, env={"PATH": "/usr/bin:/bin",
                                                   "LLMI_LOCK_DIR": str(tmp_path)})
    assert a.returncode == 0, f"no pudo tomar el cerrojo la primera vez:\n{a.stderr}"
    # El `trap EXIT` lo suelta al salir, así que para el segundo hay que dejarlo tomado.
    (tmp_path / ".m4-restore-inst.lock").mkdir(exist_ok=True)
    b = subprocess.run(["bash", "-c", guion], capture_output=True, text=True, timeout=30,
                       env={"PATH": "/usr/bin:/bin", "LLMI_LOCK_DIR": str(tmp_path)})
    assert b.returncode != 0, (
        "dos restauraciones concurrentes sobre el mismo almacén: se pisan justo sobre "
        "el fichero que existe para poder volver atrás")
    assert "otra restauración en marcha" in b.stderr


def test_el_cerrojo_se_suelta_y_no_deja_bloqueado(tmp_path):
    """CONTROL POSITIVO. Un cerrojo que no se suelta bloquea para siempre y se lee igual
    que uno que funciona — hasta que hace falta restaurar."""
    guion = f'. "{LIB}"; LLMI_LOCK_DIR="{tmp_path}" cerrojo_restauracion inst'
    for intento in (1, 2):
        r = subprocess.run(["bash", "-c", guion], capture_output=True, text=True,
                           timeout=30, env={"PATH": "/usr/bin:/bin",
                                            "LLMI_LOCK_DIR": str(tmp_path)})
        assert r.returncode == 0, f"intento {intento}: el cerrojo quedó huérfano\n{r.stderr}"


# ── veredicto estructurado, fail-closed ─────────────────────────────────────────
def _veredicto(tmp_path, anotaciones, esperadas):
    f = tmp_path / "v.json"
    # `"".join` con cero anotaciones dejaba `. LIB; ; veredicto_escribe …` —error de
    # sintaxis de bash— y el test leía «no hay fichero» como si el código no lo escribiera.
    # El caso de CERO anotaciones es justo el que más importa aquí, así que el arnés no
    # puede ser quien lo rompa.
    cuerpo = "".join(f'veredicto_anota {a}; ' for a in anotaciones)
    r = subprocess.run(
        ["bash", "-c", f'. "{LIB}"; {cuerpo}veredicto_escribe "{f}" op inst {esperadas}'],
        capture_output=True, text=True, timeout=60,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin"})
    return r, (json.loads(f.read_text()) if f.exists() else None)


def test_veredicto_todas_las_puertas_ok(tmp_path):
    r, v = _veredicto(tmp_path, ["a ok", "b ok"], "a b")
    assert v["ok"] is True and v["fallidas"] == [] and r.returncode == 0


def test_una_puerta_QUE_NO_SE_CORRIO_es_fallo_no_ausencia(tmp_path):
    """El corazón del fail-closed: `b` no se anotó nunca."""
    r, v = _veredicto(tmp_path, ["a ok"], "a b")
    assert v["ok"] is False, "una puerta sin correr salió aprobada"
    assert v["puertas"]["b"]["estado"] == "no_medido"
    assert "b" in v["fallidas"], "la puerta desapareció del veredicto en vez de contar"
    assert r.returncode != 0


def test_una_puerta_en_fallo_hunde_el_veredicto(tmp_path):
    r, v = _veredicto(tmp_path, ["a ok", "b fallo"], "a b")
    assert v["ok"] is False and v["fallidas"] == ["b"] and r.returncode != 0


def test_el_veredicto_nombra_TODAS_las_puertas_esperadas(tmp_path):
    """Sin esto, una implementación que sólo escriba las que corrió saldría verde: lo que
    falta es justo lo que hay que ver."""
    _, v = _veredicto(tmp_path, [], "a b c")
    assert set(v["puertas"]) == {"a", "b", "c"} and v["ok"] is False


@pytest.mark.parametrize("guion", ["m4-pilot.sh", "m4-rollback.sh"])
def test_los_guiones_escriben_veredicto_en_TODA_salida(guion):
    """Estático: cada `exit 1` de los caminos con puertas tiene que dejar veredicto. Un
    guion que se va sin escribirlo deja al lector con «no hay fichero», que es
    indistinguible de «no llegué a correr»."""
    s = (RAIZ / "scripts" / guion).read_text()
    assert s.count("veredicto_escribe") >= 3, (
        f"{guion} escribe veredicto en menos caminos de los que corta")
    assert "veredicto_anota gate" in s
