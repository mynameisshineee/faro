"""M0 · los cuatro NO-GO de @security sobre `2f743eca`, con falsador por cada uno.

El NO-GO, literal: ① el meta-falsador de `guardas.sh` es SINTÁCTICO y deja pasar una
relectura real vía `shasum`; ② `llmi credenciales` no propaga el SHA del mapa candidato;
③ SHA ausente no se distingue y falla ABIERTO; ④ el snapshot no queda 0600.

Lo que separa este fichero del que ya existe (`test_v8_mapa_snapshot.py`) es el
INSTRUMENTO. Aquel mide la relectura ENUMERANDO cadenas en el fuente (`_huella_mapa "`,
`open(sys.argv`), y por eso `shasum -a 256 "$_SNAP"` lo atraviesa entero: la clase no se
cierra listando las formas de escribirla. Aquí se mide la CONDUCTA, y con dos
instrumentos independientes que no comparten modo de fallo:

  · SUSTITUCIÓN — el snapshot cambia de contenido dentro de la ventana. Cualquier
    relectura, se llame como se llame, devuelve otros bytes y mueve una decisión.
  · DENEGACIÓN — el snapshot pasa a `chmod 000` dentro de la ventana. Cualquier
    apertura real FALLA, incluidos los builtins del shell (`$(<f)`), que una lista de
    nombres de programa no puede ver.

⚠️ QUÉ NO SON, dicho antes de que alguien lo cite de más: esto **no cuenta aperturas**.
No hay contador de syscalls ni instrumentación del kernel; lo que hay es observación de
CONDUCTA. La denegación se acerca —convierte cualquier apertura real en un fallo
observable— pero sigue midiendo el efecto, no el número. La primera versión de esta
cabecera decía «cuenta aperturas reales» y era una promesa mayor que el instrumento.

⚠️ Y DÓNDE EMPIEZA LA VENTANA, que es lo que decide si miden algo. Mi primera versión
saboteaba cuando el doble de `docker` llegaba a `compose up`, y eso deja fuera un tramo
entero: una relectura metida entre `export LLMINBOX_CREDENCIALES_SHA` y `compose`
seguiría viendo el mapa original y el falsador la daría por buena. La ventana se abre
INMEDIATAMENTE DESPUÉS de la única lectura legítima —la que produce el digest— mediante
un envoltorio de `sed` que, al extraer la segunda línea de ESE buffer, devuelve la salida
real y sabotea a continuación. Deja MARCA obligatoria: sin marca el escenario no ocurrió y
un verde no significa nada. Y la marca sólo acredita que el sabotaje CORRIÓ, así que cada
falsador comprueba además su EFECTO sobre el snapshot (contenido sustituido · modo 000).
"""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from .conftest import construir
from .test_v8_mapa_snapshot import CID12, _corre, _sandbox

RAIZ = Path(__file__).resolve().parents[2]

MAPA_A = '{"tok-a": {"rol": "be", "carril": "demo"}}'
MAPA_B = '{"tok-b": {"rol": "qa", "carril": "demo"}, "tok-c": {"rol": "cto", "carril": "demo"}}'
TOKEN = "token-de-prueba"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class _SaludCongelada:
    """`/health` que atestigua un digest FIJO, no el fichero que haya en disco.

    Es la diferencia que hace medible la relectura, y no es una comodidad del arnés:
    reproduce lo que pasa de verdad. `servicio.py` lee el mapa UNA vez, al importar, o
    sea ANTES de que nada pueda sustituirlo; su atestado describe para siempre los bytes
    que cargó. Si el doble releyera el disco como hace `_Salud`, el arnés se movería con
    la mutación y taparía justo lo que se quiere ver: quién MÁS la vuelve a leer.
    """

    def __init__(self, digest: str, token: str = TOKEN, instancia: str = CID12):
        import hmac
        import http.server
        import json
        import threading

        self.valor = hmac.new(token.encode(),
                              instancia.encode() + b"\0" + digest.encode(),
                              hashlib.sha256).hexdigest()
        salud = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                cuerpo = json.dumps({"ok": True, "v8": {
                    "configurado": True, "mapa_atestado": salud.valor}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(cuerpo)))
                self.end_headers()
                self.wfile.write(cuerpo)

            def log_message(self, *a):
                pass

        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def para(self):
        self.srv.shutdown()


def _escenario(tmp_path, sabotaje: str, *, mutante: bool = False):
    """Corre un `llmi up` completo con el snapshot saboteado DENTRO de la ventana.

    `sabotaje` se ejecuta justo después de extraer la segunda línea del único buffer
    legítimo: en ese punto la lectura inicial ya terminó y todavía no se ha exportado
    el digest. Devuelve (resultado, sha de lo congelado, ruta del sello).
    """
    mapa = tmp_path / "mapa.json"
    mapa.write_text(MAPA_A)
    congelado = _sha(MAPA_A.encode())
    reg = tmp_path / "docker.log"
    repo, casa, binario = _sandbox(tmp_path / "x", f'''
if [ "$1" = "inspect" ]; then
  echo "Error: No such object: $2" >&2; exit 1
fi
printf 'args=%s\\n' "$*" >> "{reg}"
exit 0
''')
    marca = tmp_path / "sabotaje-ejecutado"
    sed_real = shutil.which("sed")
    assert sed_real, "el arnés necesita un sed real detrás del envoltorio"
    (binario / "sed").write_text(f'''#!/bin/sh
if [ "$*" = "-n 2p" ] && [ ! -e "$M0_MARCA" ]; then
  "{sed_real}" "$@"
  _rc=$?
  # A stderr A PROPOSITO: esto corre DENTRO de `$(printf ... | sed -n 2p)`, asi que
  # cualquier byte que el sabotaje mande a stdout se pega al `_SNAP_HMAC` que `llmi`
  # captura. Un sabotaje que hablase corromperia el sujeto en vez de moverlo, y el
  # falsador mediria su propia contaminacion.
  /bin/sh -c "$M0_SABOTAJE" >&2
  : > "$M0_MARCA"
  exit "$_rc"
fi
exec "{sed_real}" "$@"
''')
    (binario / "sed").chmod(0o755)
    if mutante:
        # LA MUTACIÓN QUE @security DESCRIBIÓ, literal: una relectura del snapshot con
        # `shasum` alimentando el sello. Atraviesa la guarda sintáctica sin despeinarse
        # —no dice `_huella_mapa` ni `open(sys.argv`— así que si estos falsadores no la
        # ven, no valen para lo que existen.
        fuente = (repo / "llmi").read_text()
        aguja = '            export LLMINBOX_CREDENCIALES_SHA="$_SNAP_SHA"\n'
        assert aguja in fuente, (
            "el ancla del mutante no existe en `llmi`: sin ella el ⊕ no inyecta nada y "
            "este control quedaría VERDE por vacío, que es peor que no tenerlo")
        (repo / "llmi").write_text(fuente.replace(
            aguja,
            aguja + '            _SNAP_SHA="$( { shasum -a 256 "$_SNAP" 2>/dev/null || '
            'sha256sum "$_SNAP" 2>/dev/null; } | cut -d" " -f1)"\n'))
    salud = _SaludCongelada(congelado)
    try:
        r = _corre(repo, casa, binario, "up",
                   extra_env={"LLMINBOX_CREDENCIALES": str(mapa),
                              "LLMINBOX_API": salud.url,
                              "LLMI_ATESTADO_ESPERA": "5",
                              "M0_SABOTAJE": sabotaje,
                              "M0_MARCA": str(marca)})
    finally:
        salud.para()
    assert marca.exists(), (
        "el sabotaje no se ejecutó tras la lectura inicial: un verde sería vacío")
    return r, congelado, casa / ".llmi-v8-mapa-llminbox.sha256"


# ─────────────────────────────────────────────────────────────────────────────
# ① NINGUNA DECISIÓN DEL `up` SE APOYA EN RELEER EL SNAPSHOT — conductual
# ─────────────────────────────────────────────────────────────────────────────

def test_una_SUSTITUCION_del_snapshot_tras_congelarlo_no_mueve_ninguna_decision(tmp_path):
    """Si algo relee, sella el mapa que apareció en la ventana en vez del validado."""
    otro = tmp_path / "otro.json"
    otro.write_text(MAPA_B)
    snap = tmp_path / "x" / "home" / ".llmi-v8-mapa-llminbox.json"
    r, congelado, sello = _escenario(tmp_path, f'cp "{otro}" "{snap}"')
    assert r.returncode == 0, f"el `up` no terminó:\n{r.stdout}\n{r.stderr}"
    assert snap.read_text() == MAPA_B, (
        "el arnés no reprodujo la ventana: sin sustitución esto no mide nada")
    assert sello.read_text() == congelado, (
        "el sello describe los bytes que aparecieron DESPUÉS de congelar: alguna "
        "decisión volvió a abrir el snapshot")


def test_el_mutante_de_relectura_con_shasum_pone_ROJO_el_falsador_de_SUSTITUCION(tmp_path):
    """⊕ del anterior. Sin este control, un verde no distingue «no hay relectura» de
    «el instrumento no la vería»."""
    otro = tmp_path / "otro.json"
    otro.write_text(MAPA_B)
    snap = tmp_path / "x" / "home" / ".llmi-v8-mapa-llminbox.json"
    r, congelado, sello = _escenario(tmp_path, f'cp "{otro}" "{snap}"', mutante=True)
    assert snap.read_text() == MAPA_B, (
        "el arnes no reprodujo la ventana: sin sustitucion el mutante no tiene que releer nada")
    sellado = sello.read_text() if sello.exists() else ""
    assert not (r.returncode == 0 and sellado == congelado), (
        "el `llmi` MUTADO relee el snapshot con `shasum` y aun así el falsador lo da "
        "por bueno: mide la forma de escribir la relectura, no la relectura")


def test_una_DENEGACION_de_lectura_tras_congelar_no_impide_terminar_el_up(tmp_path):
    """Segundo instrumento, sin modo de fallo común con el primero: el snapshot pasa a
    `chmod 000` en la ventana. Cualquier apertura REAL falla — también `$(<f)` y demás
    builtins, que una lista de nombres de programa nunca podría ver."""
    if os.geteuid() == 0:
        pytest.skip("como root los bits de permiso no deniegan: el instrumento no mide")
    snap = tmp_path / "x" / "home" / ".llmi-v8-mapa-llminbox.json"
    r, congelado, sello = _escenario(tmp_path, f'chmod 000 "{snap}"')
    try:
        # LA MARCA PRUEBA QUE EL SABOTAJE CORRIO; ESTO, QUE LE PASO AL SUJETO. Sin
        # esta linea un `chmod` disparado antes de que el snapshot existiera fallaria
        # en silencio y el verde no distinguiria «nadie relee» de «no se denego nada».
        assert stat.S_IMODE(snap.stat().st_mode) == 0o000, (
            "el arnes no dejo el snapshot ilegible: no hay denegacion que medir")
        assert r.returncode == 0, (
            "algo volvió a ABRIR el snapshot después de congelarlo: con el fichero "
            f"ilegible el `up` debería seguir igual.\n{r.stdout}\n{r.stderr}")
        assert sello.read_text() == congelado
    finally:
        snap.chmod(0o600)


def test_el_mutante_de_relectura_con_shasum_pone_ROJO_el_falsador_de_DENEGACION(tmp_path):
    """⊕ del anterior."""
    if os.geteuid() == 0:
        pytest.skip("como root los bits de permiso no deniegan: el instrumento no mide")
    snap = tmp_path / "x" / "home" / ".llmi-v8-mapa-llminbox.json"
    r, congelado, sello = _escenario(tmp_path, f'chmod 000 "{snap}"', mutante=True)
    try:
        assert stat.S_IMODE(snap.stat().st_mode) == 0o000, (
            "el arnes no dejo el snapshot ilegible: el mutante no tenia que atravesar nada")
        sellado = sello.read_text() if sello.exists() else ""
        assert not (r.returncode == 0 and sellado == congelado), (
            "el `llmi` MUTADO abre un fichero ilegible y el falsador no se entera")
    finally:
        if snap.exists():
            snap.chmod(0o600)


# ─────────────────────────────────────────────────────────────────────────────
# ④ EL SNAPSHOT ES UN MAPA DE CREDENCIALES Y QUEDA 0600
# ─────────────────────────────────────────────────────────────────────────────

def test_el_snapshot_del_mapa_queda_0600(tmp_path):
    """`cp -p` heredaba el modo del origen: un mapa 0644 dejaba una segunda copia
    legible por todos en `$HOME`, y encima la que de verdad se monta."""
    mapa = tmp_path / "mapa.json"
    mapa.write_text(MAPA_A)
    mapa.chmod(0o644)          # el caso que importa: origen permisivo
    congelado = _sha(MAPA_A.encode())
    repo, casa, binario = _sandbox(tmp_path / "x", '''
if [ "$1" = "inspect" ]; then echo "Error: No such object: $2" >&2; exit 1; fi
exit 0
''')
    salud = _SaludCongelada(congelado)
    try:
        r = _corre(repo, casa, binario, "up",
                   extra_env={"LLMINBOX_CREDENCIALES": str(mapa),
                              "LLMINBOX_API": salud.url, "LLMI_ATESTADO_ESPERA": "5"})
    finally:
        salud.para()
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    snap = casa / ".llmi-v8-mapa-llminbox.json"
    modo = stat.S_IMODE(snap.stat().st_mode)
    assert modo == 0o600, (
        f"el snapshot quedó {modo:04o}: es un mapa de credenciales y hereda el modo "
        "del origen, así que un origen permisivo deja el secreto abierto en $HOME")


# ─────────────────────────────────────────────────────────────────────────────
# ② `llmi credenciales` VALIDA EL CANDIDATO CON SU PROPIO SHA
# ─────────────────────────────────────────────────────────────────────────────

def _sandbox_credenciales(tmp_path, cuerpo_exec: str):
    reg = tmp_path / "exec.log"
    repo, casa, binario = _sandbox(tmp_path / "x", f'''
if [ "$1" = "inspect" ]; then printf '%s\\n' "{CID12}"; exit 0; fi
if [ "$1" = "exec" ]; then
  printf 'exec args=%s\\n' "$*" >> "{reg}"
  {cuerpo_exec}
fi
exit 0
''')
    return repo, casa, binario, reg


def test_credenciales_propaga_el_SHA_DEL_CANDIDATO_y_no_el_del_desplegado(tmp_path):
    """Sin esto el validador miente en la dirección tranquilizadora. El contenedor trae
    `LLMINBOX_CREDENCIALES_SHA` del mapa DESPLEGADO; `docker exec` sólo sobreescribía
    `LLMINBOX_CREDENCIALES`, así que el servicio comparaba el candidato contra el sha
    del otro, se declaraba ALTERADO y devolvía `{}` — y el validador lo leía como
    «0 credenciales, el servicio arrancaría», con rc=0, sobre un mapa perfecto."""
    cand = tmp_path / "candidato.json"
    cand.write_text(MAPA_B)
    repo, casa, binario, reg = _sandbox_credenciales(tmp_path, 'cat >/dev/null 2>&1; exit 0')
    r = _corre(repo, casa, binario, "credenciales", str(cand))
    registro = reg.read_text() if reg.exists() else ""
    esperado = _sha(MAPA_B.encode())
    assert f"LLMINBOX_CREDENCIALES_SHA={esperado}" in registro, (
        "no se propagó el sha DEL CANDIDATO al validador; el servicio compara contra "
        f"el del mapa desplegado y todo candidato sale alterado.\nregistro={registro!r}"
        f"\n{r.stdout}\n{r.stderr}")


def test_credenciales_con_candidato_ALTERADO_falla_y_no_lo_da_por_bueno(tmp_path):
    """⊖: si el servicio dice que el candidato no casa con su sha, `llmi` no puede
    imprimir un veredicto tranquilizador ni salir 0."""
    cand = tmp_path / "candidato.json"
    cand.write_text(MAPA_B)
    # El doble hace de servicio con el mapa alterado: es el rc que produce el import
    # cuando el sha declarado no casa con los bytes recibidos.
    repo, casa, binario, _ = _sandbox_credenciales(
        tmp_path,
        'cat >/dev/null 2>&1; '
        'echo "INTEGRIDAD: el candidato no casa con su sha" >&2; exit 4')
    r = _corre(repo, casa, binario, "credenciales", str(cand))
    assert r.returncode != 0, (
        f"candidato alterado y rc=0:\n{r.stdout}\n{r.stderr}")
    assert "arrancaria con este mapa" not in r.stdout, (
        f"veredicto tranquilizador sobre un candidato alterado:\n{r.stdout}")


# ─────────────────────────────────────────────────────────────────────────────
# ③ INTEGRIDAD AUSENTE ES UN ESTADO EXPLÍCITO Y NO DEJA MUTAR
# ─────────────────────────────────────────────────────────────────────────────

def _con_mapa(tmp_path, monkeypatch, *, sha: str | None, contenido: str = MAPA_A):
    mapa = tmp_path / "mapa-v8.json"
    mapa.write_text(contenido)
    extra = {"LLMINBOX_CREDENCIALES": str(mapa)}
    if sha is not None:
        extra["LLMINBOX_CREDENCIALES_SHA"] = sha
    else:
        monkeypatch.delenv("LLMINBOX_CREDENCIALES_SHA", raising=False)
    return construir(tmp_path, monkeypatch, extra_env=extra)


def test_sin_SHA_declarado_la_integridad_es_NO_VERIFICADA_y_se_ve_en_health(tmp_path, monkeypatch):
    """El agujero exacto del NO-GO: la comprobación colgaba de `if _esperado_sha:`, así
    que ausente ⇒ no se comprueba nada ⇒ `/health` decía `mapa_alterado: false`,
    indistinguible de «verificado y correcto». Un gate no puede depender de que alguien
    haya puesto una variable: eso es un flag, no una dependencia."""
    s = _con_mapa(tmp_path, monkeypatch, sha=None)
    v8 = TestClient(s.app).get("/health", headers={"X-Llminbox-Token": s.TOKEN}).json()["v8"]
    assert v8.get("integridad") == "no_verificada", (
        "sin sha declarado el estado tiene que DECIRSE; si sale como verificada o no "
        f"sale, nadie puede distinguir comprobado de no comprobado. v8={v8!r}")


def test_health_DISTINGUE_verificada_de_no_verificada_y_de_alterada(tmp_path, monkeypatch):
    """⊕/⊖ del anterior: tres estados, tres valores. Sin el ⊕ «verificada» un campo
    constante pasaría el test de arriba sin medir nada."""
    ok = _con_mapa(tmp_path, monkeypatch, sha=_sha(MAPA_A.encode()))
    v_ok = TestClient(ok.app).get("/health", headers={"X-Llminbox-Token": ok.TOKEN}).json()["v8"]
    assert v_ok.get("integridad") == "verificada", v_ok
    assert v_ok.get("mapa_alterado") is False, "compatibilidad: el campo viejo sigue"

    mal = _con_mapa(tmp_path, monkeypatch, sha=_sha(b"otra cosa"))
    v_mal = TestClient(mal.app).get("/health", headers={"X-Llminbox-Token": mal.TOKEN}).json()["v8"]
    assert v_mal.get("integridad") == "alterada", v_mal
    assert v_mal.get("mapa_alterado") is True, "compatibilidad: el campo viejo sigue"


def test_sin_SHA_declarado_NINGUNA_mutacion_pasa(tmp_path, monkeypatch):
    """Lo que de verdad cierra el fail-open. Con el mapa sin verificar, `CREDENCIALES`
    no se carga ⇒ `IDENTIDAD` cae a `_SinIdentidad` ⇒ `exige_ser` sólo anota ⇒ el token
    compartido autorizaba las mutaciones igual. No comprobar es peor que comprobar y
    fallar, porque se siente idéntico a estar bien."""
    s = _con_mapa(tmp_path, monkeypatch, sha=None)
    c = TestClient(s.app)
    r = c.post("/append", headers={"X-Llminbox-Token": s.TOKEN},
               json={"ledger": "demo-ledger", "actor": "backend", "texto": "x"})
    assert r.status_code == 503, (
        f"una mutación pasó con la integridad SIN VERIFICAR: {r.status_code} {r.text[:200]}")
    assert "INTEGRIDAD" in r.text, r.text[:200]


def test_sin_SHA_declarado_LEER_sigue_libre(tmp_path, monkeypatch):
    """⊖ que acota el anterior: tumbar el bus ya costó 11 reinicios y ~71 sesiones sin
    coordinación. Cortar las escrituras es la cura; cortar las lecturas es la avería."""
    s = _con_mapa(tmp_path, monkeypatch, sha=None)
    r = TestClient(s.app).get("/health", headers={"X-Llminbox-Token": s.TOKEN})
    assert r.status_code == 200, f"la lectura se cortó: {r.status_code}"


def test_sin_mapa_configurado_la_fase_1_sigue_MUTANDO(tmp_path, monkeypatch):
    """⊖ IMPRESCINDIBLE y el que más caro sale si falta: «no hay mapa» NO es «hay mapa
    sin verificar». Confundirlos convierte esta cura en un apagón para toda la flota que
    todavía no tiene credencial emitida."""
    monkeypatch.delenv("LLMINBOX_CREDENCIALES", raising=False)
    monkeypatch.delenv("LLMINBOX_CREDENCIALES_SHA", raising=False)
    s = construir(tmp_path, monkeypatch)
    v8 = TestClient(s.app).get("/health", headers={"X-Llminbox-Token": s.TOKEN}).json()["v8"]
    assert v8.get("integridad") == "sin_mapa", v8
    r = TestClient(s.app).post("/append", headers={"X-Llminbox-Token": s.TOKEN},
                               json={"ledger": "demo-ledger", "actor": "backend", "texto": "x"})
    assert r.status_code != 503 or "INTEGRIDAD" not in r.text, (
        "sin mapa configurado no hay integridad que verificar y la fase 1 tiene que "
        f"poder escribir: {r.status_code} {r.text[:200]}")


# ─────────────────────────────────────────────────────────────────────────────
# ⑤ `build` PUBLICA HUELLA AUNQUE NO HAYA SHA (fuera de git y sin LLMINBOX_BUILD)
# ─────────────────────────────────────────────────────────────────────────────

def test_build_publica_huella_fuera_de_git_y_sin_LLMINBOX_BUILD(tmp_path, monkeypatch):
    """`_build()` retornaba `{sha, origen, parece_sha}` por la rama de «desconocido»,
    ANTES de mezclar `_huella_del_fichero()`. O sea que el único campo que el proceso
    NO puede fingir desaparecía justo cuando los dos declarados ya no valen nada — y el
    verde de la suite dependía de correr dentro de un git."""
    monkeypatch.delenv("LLMINBOX_BUILD", raising=False)
    s = construir(tmp_path, monkeypatch)
    fuera = tmp_path / "sin-git"
    fuera.mkdir()
    monkeypatch.setattr(s, "RAIZ_GIT", str(fuera))
    b = s._build()
    assert b["sha"] is None and b["origen"] == "desconocido", (
        f"el arnés no reprodujo el caso: {b!r}")
    assert "huella" in b and "huella_de" in b, (
        f"sin sha declarado ni derivado desaparece la única medida no fingible: {b!r}")
    assert b["huella"] == hashlib.sha256(Path(s.__file__).read_bytes()).hexdigest()
    assert b["huella_de"] == "servicio.py"


def test_health_trae_huella_aunque_el_build_sea_desconocido(tmp_path, monkeypatch):
    """El mismo hecho por la puerta por la que se consume de verdad."""
    monkeypatch.delenv("LLMINBOX_BUILD", raising=False)
    s = construir(tmp_path, monkeypatch)
    fuera = tmp_path / "sin-git"
    fuera.mkdir()
    monkeypatch.setattr(s, "RAIZ_GIT", str(fuera))
    b = TestClient(s.app).get("/health", headers={"X-Llminbox-Token": s.TOKEN}).json()["build"]
    assert b.get("huella_de") == "servicio.py", f"build={b!r}"
    assert b.get("huella"), f"build={b!r}"
