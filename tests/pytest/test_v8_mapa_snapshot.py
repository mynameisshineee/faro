"""Los dos P0 de `7413b963`: el mapa es MUTABLE y el sello de aplicado es CIEGO.

`MARK:finding-llminbox-7413-dos-p0-mapa-mutable-y-falso-aplicado-20260905`.

P0-A · TOCTOU DEL MAPA. El wrapper calculaba la huella de un fichero que sigue siendo
del mundo, y DESPUÉS Compose consumía esa misma ruta. Entre las dos cosas cabe una
sustitución: se valida A, se monta B, y se sella SHA(B) sin que nada note que lo
medido y lo aplicado son ficheros distintos. La huella de algo mutable sólo describe
el instante en que se calculó.

P0-B · FALSO APLICADO. El mapa entra por un bind-mount cuya RUTA no cambia, así que
Compose no ve diferencia y reutiliza el contenedor: sin `--force-recreate`, el proceso
sigue con el mapa viejo —`servicio.py` carga `CREDENCIALES` UNA vez al importar— y aun
así se sellaba el nuevo. Disco y sello dicen B mientras el proceso autentica con A.
Es la clase «construido no es alcanzable»: el sello afirma más que el dato.
"""
from __future__ import annotations
import hashlib
import shutil
import subprocess
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
# La identidad REAL del contenedor. El atestado se ata a ella —no al nombre lógico—, así
# que el doble tiene que saber contestarla: un `docker inspect --format {{.Id}}` sobre un
# contenedor que existe SIEMPRE la devuelve.
CID = "abcdef0123456789" * 4
CID12 = CID[:12]
_RESPONDE_ID = 'case "$*" in *"{{.Id}}"*) printf \'%s\\n\' "' + CID + '"; exit 0 ;; esac\n'


class _Salud:
    """Un `/health` mínimo que atestigua el mapa que se le diga.

    Hace falta de verdad: desde el P0-B, `llmi up` no sella hasta que el PROCESO VIVO
    declara el mapa que se montó, y sin alguien que conteste eso el `up` no puede
    terminar bien. Además da el ⊖ que hace valer al ⊕: si el atestado NO coincide, el
    `up` tiene que fallar y no sellar.
    """
    def __init__(self, mapa: Path | None, token: str, instancia: str = CID12):
        # `instancia` es ahora la IDENTIDAD REAL del contenedor (su id corto), no la
        # etiqueta: es lo que el servicio usa, y atar el doble a otra cosa mediría de
        # menos — aceptaría un atestado que en producción no valdría.
        self.instancia = instancia
        import http.server, json, threading
        self.mapa, self.token = mapa, token
        salud = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                cuerpo = json.dumps({"ok": True, "v8": {
                    "configurado": True, "mapa_atestado": salud.atestado()}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(cuerpo)))
                self.end_headers()
                self.wfile.write(cuerpo)
            def log_message(self, *a):  # silencio
                pass

        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def atestado(self) -> str:
        import hashlib, hmac
        if self.mapa is None or not self.mapa.exists():
            return ""
        # ATADO A LA INSTANCIA, como el servicio: sin ella el doble aceptaría un
        # atestado que en producción no valdría, y el ⊕ mediría de menos.
        digest = hashlib.sha256(self.mapa.read_bytes()).hexdigest().encode()
        msg = self.instancia.encode() + b"\0" + digest
        return hmac.new(self.token.encode(), msg, hashlib.sha256).hexdigest()

    def para(self):
        self.srv.shutdown()
A = ('{"agentes": [{"nombre": "backend", "humano": "a", "clave": "", "rol": "be"}],'
     ' "humanos": [], "difusion": []}')


def _sha(texto: str) -> str:
    return hashlib.sha256(texto.encode()).hexdigest()


def _sandbox(root: Path, docker: str):
    repo, casa, binario = root / "repo", root / "home", root / "bin"
    for d in (repo, casa, binario):
        d.mkdir(parents=True, exist_ok=True)
    for f in ("llmi", "docker-compose.yml", "roster.example.json"):
        shutil.copy(RAIZ / f, repo / f)
    (repo / "roster.json").write_text(A)
    (repo / ".llmi-mounts.json").write_text("{}")
    (casa / ".llminbox.token").write_text("token-de-prueba")
    (repo / ".llminbox-state").mkdir(exist_ok=True)
    (repo / ".llminbox-state" / "roster.json").write_text(A)
    (repo / ".llminbox-state" / "mounts.json").write_text("{}")
    d = binario / "docker"
    d.write_text("#!/bin/sh\n" + _RESPONDE_ID + docker)
    d.chmod(0o755)
    return repo, casa, binario


# Este es un FUSIBLE del proceso de prueba, no el reloj del producto. Los casos de
# atestado fijan LLMI_ATESTADO_ESPERA a 2..5 intentos; ese es el límite semántico que
# detecta un bucle roto. En el host real de flotas (load >100 medido) arrancar bash,
# curl y varios intérpretes puede superar 30 s sin que el sujeto exceda ese límite.
_FUSIBLE_PROCESO_S = 300


def _corre(repo, casa, binario, *args, extra_env=None, timeout=_FUSIBLE_PROCESO_S):
    env = {"HOME": str(casa), "PATH": f"{binario}:/usr/bin:/bin:/usr/sbin:/sbin"}
    env.update(extra_env or {})
    return subprocess.run([str(repo / "llmi"), *args], cwd=repo, env=env,
                          capture_output=True, text=True, timeout=timeout, check=False)


def test_el_mapa_que_se_monta_es_un_SNAPSHOT_no_el_fichero_mutable(tmp_path):
    """P0-A. Se sustituye el mapa DESPUÉS del precheck; lo montado tiene que seguir
    siendo lo VALIDADO, y el sello tiene que describir eso mismo.

    La ventana se abre de forma determinista: el doble de `docker` reescribe el origen
    la primera vez que se le llama (el `inspect` del precheck), o sea exactamente entre
    medir y montar.
    """
    mapa = tmp_path / "mapa.json"
    CONTENIDO_A = '{"tok-a": {"rol": "be", "carril": "64bis"}}'
    CONTENIDO_B = '{"tok-b": {"rol": "qa", "carril": "otro"}}'
    mapa.write_text(CONTENIDO_A)
    # El sustituto se escribe DESDE PYTHON y el doble sólo lo copia: interpolar JSON
    # con comillas dentro de un `printf` de `sh` lo destroza, y entonces el ⊖ falla por
    # la avería del arnés y no por la del código —que fue justo lo que pasó aquí—.
    sustituto = tmp_path / "sustituto.json"
    sustituto.write_text(CONTENIDO_B)
    reg = tmp_path / "docker.log"
    repo, casa, binario = _sandbox(tmp_path / "x", f'''
if [ "$1" = "inspect" ]; then
  cp "{sustituto}" "{mapa}"                    # la sustitución, en la ventana exacta
  echo "Error: No such object: $2" >&2; exit 1
fi
printf 'cred=%s args=%s\\n' "$LLMINBOX_CREDENCIALES" "$*" >> "{reg}"
exit 0
''')
    salud = _Salud(casa / ".llmi-v8-mapa-llminbox.json", "token-de-prueba")
    try:
        r = _corre(repo, casa, binario, "up",
                   extra_env={"LLMINBOX_CREDENCIALES": str(mapa),
                              "LLMINBOX_API": salud.url, "LLMI_ATESTADO_ESPERA": "5"})
    finally:
        salud.para()
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    montado = [l.split("cred=")[1].split(" args=")[0]
               for l in reg.read_text().splitlines() if "compose up" in l]
    assert montado, f"no llegó a compose up: {reg.read_text()!r}"
    ruta = Path(montado[-1])
    assert ruta != mapa, (
        "se montó el fichero MUTABLE del usuario: entre validar y montar cabe una "
        "sustitución y nadie la vería")
    assert ruta.exists(), f"la ruta montada no existe: {ruta}"
    assert ruta.read_text() == CONTENIDO_A, (
        "el snapshot no es lo que se validó: se montó lo que apareció en la ventana")
    assert mapa.read_text() == CONTENIDO_B, "el test no reprodujo la ventana"


def test_cambiar_el_mapa_OBLIGA_a_recrear_el_contenedor(tmp_path):
    """P0-B. La ruta del bind-mount no cambia, así que Compose reutiliza el contenedor
    y el proceso se queda con el mapa viejo — `servicio.py` lee `CREDENCIALES` una sola
    vez, al importar. Sellar sin recrear declara aplicado algo que nadie cargó.
    """
    reg = tmp_path / "docker.log"
    doble = f'''
if [ "$1" = "inspect" ]; then
  printf '%s\\n' 'LLMINBOX_CREDENCIALES=/credenciales/mapa.json'; exit 0
fi
printf 'args=%s\\n' "$*" >> "{reg}"
exit 0
'''
    mapa = tmp_path / "mapa.json"
    mapa.write_text('{"tok-a": {"rol": "be", "carril": "64bis"}}')
    repo, casa, binario = _sandbox(tmp_path / "x", doble)
    salud = _Salud(casa / ".llmi-v8-mapa-llminbox.json", "token-de-prueba")
    entorno = {"LLMINBOX_CREDENCIALES": str(mapa), "LLMINBOX_API": salud.url,
               "LLMI_ATESTADO_ESPERA": "5"}
    # ① primer `up`: deja el mapa A sellado. Lleva `--cobertura-verificada` porque el
    # contenedor ya tiene V8 y no hay sello previo contra el que comparar — el gate de
    # cobertura hace bien en pedirlo, y aquí el sujeto de la prueba es otro.
    r1 = _corre(repo, casa, binario, "up", "--cobertura-verificada", extra_env=entorno)
    assert r1.returncode == 0, f"{r1.stdout}\n{r1.stderr}"
    reg.write_text("")
    # ② mapa DISTINTO, con la cobertura declarada, y roster/mounts SIN tocar
    mapa.write_text('{"tok-b": {"rol": "be", "carril": "64bis"}, '
                    '"tok-c": {"rol": "qa", "carril": "64bis"}}')
    r2 = _corre(repo, casa, binario, "up", "--cobertura-verificada", extra_env=entorno)
    salud.para()
    assert r2.returncode == 0, f"{r2.stdout}\n{r2.stderr}"
    args = reg.read_text()
    assert "compose up" in args, f"no llegó a compose: {args!r}"
    assert "--force-recreate" in args, (
        "cambió el mapa y NO se recreó: el proceso sigue autenticando con el anterior "
        f"mientras el sello declara el nuevo. args={args!r}")


def test_el_sello_del_mapa_NO_avanza_si_el_arranque_falla(tmp_path):
    """⊖ del sello: si `compose up` falla, lo aplicado sigue siendo lo de antes.
    Sin esto, el sello se convierte en una declaración de intenciones y el siguiente
    `up` compara contra una línea base que nunca existió.
    """
    reg = tmp_path / "docker.log"
    mapa = tmp_path / "mapa.json"
    mapa.write_text('{"tok-a": {"rol": "be", "carril": "64bis"}}')
    repo, casa, binario = _sandbox(tmp_path / "x", f'''
if [ "$1" = "inspect" ]; then echo "Error: No such object: $2" >&2; exit 1; fi
printf 'args=%s\\n' "$*" >> "{reg}"
case "$*" in *"compose up"*) exit 1 ;; esac
exit 0
''')
    r = _corre(repo, casa, binario, "up",
               extra_env={"LLMINBOX_CREDENCIALES": str(mapa),
                          "LLMI_ATESTADO_ESPERA": "2"})
    assert r.returncode != 0, "un `compose up` fallido salió con 0"
    sellos = list((casa).glob(".llmi-v8-mapa-*.sha256"))
    assert not sellos, f"selló un mapa que nunca llegó a arrancar: {sellos}"


def test_si_el_proceso_NO_atestigua_el_mapa_el_sello_no_avanza(tmp_path):
    """⊖ QUE HACE VALER LA ATESTACIÓN. Sin esto, el `⊕` de arriba pasaría igual con la
    comprobación desactivada: un servicio que contesta cualquier cosa la satisfaría.

    Aquí el servicio atestigua OTRO mapa —el caso real del P0-B: contenedor reutilizado
    que sigue con el anterior—. El `up` tiene que fallar y NO sellar, para que el
    siguiente vuelva a recrear.
    """
    reg = tmp_path / "docker.log"
    mapa = tmp_path / "mapa.json"
    mapa.write_text('{"tok-a": {"rol": "be", "carril": "64bis"}}')
    otro = tmp_path / "otro.json"
    otro.write_text('{"tok-viejo": {"rol": "be", "carril": "64bis"}}')
    repo, casa, binario = _sandbox(tmp_path / "x", f'''
if [ "$1" = "inspect" ]; then echo "Error: No such object: $2" >&2; exit 1; fi
printf 'args=%s\\n' "$*" >> "{reg}"
exit 0
''')
    salud = _Salud(otro, "token-de-prueba")        # atestigua un mapa que NO es el suyo
    try:
        r = _corre(repo, casa, binario, "up",
                   extra_env={"LLMINBOX_CREDENCIALES": str(mapa),
                              "LLMINBOX_API": salud.url, "LLMI_ATESTADO_ESPERA": "2"})
    finally:
        salud.para()
    assert r.returncode != 0, (
        f"selló sobre un proceso que declara OTRO mapa:\n{r.stdout}\n{r.stderr}")
    assert not list(casa.glob(".llmi-v8-mapa-*.sha256")), (
        "avanzó el sello sin que el proceso confirmara nada")
    assert "atestigua" in (r.stdout + r.stderr), "no dice por qué se para"


def test_el_atestado_es_HMAC_y_no_un_sha_expuesto(tmp_path, monkeypatch):
    """`/health` responde **200 SIN token** —está fuera del gate a propósito, y @qa lo
    dejó medido—. Publicar ahí el `sha256` del mapa sería un oráculo de confirmación:
    cualquiera que alcance el puerto y tenga un mapa candidato comprobaría si acertó.
    Con la clave del servicio de por medio sólo puede comparar quien ya la tiene, que es
    justo quien despliega.
    """
    import hashlib, hmac, importlib, json, sys
    # EL CENSO SE FIJA, NO SE HEREDA: `roster.json` está en `.gitignore` (lleva la flota
    # real), así que un test que importe `servicio` sin fijarlo pasa en un checkout de
    # trabajo y revienta en un clon limpio. Ya ocurrió dos veces en este repo.
    (tmp_path / "roster.json").write_text(json.dumps({
        "agentes": [{"nombre": "backend", "humano": "a", "clave": "", "rol": "be"}],
        "humanos": [{"nombre": "a", "alias": []}], "difusion": []}))
    monkeypatch.setenv("LLMINBOX_ROSTER", str(tmp_path / "roster.json"))
    monkeypatch.setenv("LLMINBOX_DB", str(tmp_path / "prueba.sqlite"))
    mapa = tmp_path / "mapa.json"
    mapa.write_bytes(b'{"tok": {"rol": "be", "carril": "64bis"}}')
    monkeypatch.setenv("LLMINBOX_CREDENCIALES", str(mapa))
    monkeypatch.setenv("LLMINBOX_CREDENCIALES_SHA",
                       hashlib.sha256(mapa.read_bytes()).hexdigest())
    monkeypatch.setenv("LLMINBOX_TOKEN", "clave-del-servicio")
    monkeypatch.setenv("HOSTNAME", CID12)
    sys.modules.pop("servicio", None)
    try:
        srv = importlib.import_module("servicio")
        esperado = hmac.new(b"clave-del-servicio",
                            CID12.encode() + b"\0" + hashlib.sha256(mapa.read_bytes()).hexdigest().encode(),
                            hashlib.sha256).hexdigest()
        assert srv.ATESTADO_MAPA == esperado
        # ⊖ NO es el sha pelado: si lo fuera, el oráculo estaría publicado.
        assert srv.ATESTADO_MAPA != hashlib.sha256(mapa.read_bytes()).hexdigest()
        # ⊖ sin clave no se inventa un valor: se dice que no hay atestado.
        monkeypatch.setenv("LLMINBOX_TOKEN", "")
        assert srv._atestado_mapa() == ""
        # ⊖ SIN MAPA CARGADO tampoco hay atestado de relleno. Y va por los BYTES que se
        # parsearon, no por releer el disco: el fichero puede desaparecer y el atestado
        # sigue describiendo lo que este proceso tiene cargado, que es lo que acredita.
        monkeypatch.setenv("LLMINBOX_TOKEN", "clave-del-servicio")
        mapa.unlink()
        assert srv._atestado_mapa() == esperado, (
            "el atestado cambió al borrar el fichero: está releyendo el disco en vez de "
            "firmar los bytes que el proceso cargó")
        srv._BYTES_MAPA.clear()
        assert srv._atestado_mapa() == ""
    finally:
        sys.modules.pop("servicio", None)


def test_un_atestado_fallido_no_deja_avanzar_NINGUN_sello(tmp_path):
    """El sello de estado iba por delante del atestado, y eso rompe la promesa escrita.

    `.llmi-applied` se escribía justo después de `compose up` y ANTES de comprobar que
    el proceso hubiera cargado el mapa. Con el atestado en rojo el `up` salía 1 y no
    escribía el `.sha256` — pero `.llmi-applied` YA había avanzado, así que el siguiente
    `up` veía `aplicado == deseado`, NO añadía `--force-recreate`, y el contenedor se
    quedaba con el mapa viejo para siempre. El mensaje de error decía literalmente «el
    próximo `up` volverá a recrear»: el código lo desmentía.

    Es la misma clase que este fichero ya protege un piso más abajo (`si el BUILD falla
    el sello no avanza`), reintroducida encima. Los dos sellos son UNA sola verdad: o
    avanzan los dos, o ninguno.
    """
    reg = tmp_path / "docker.log"
    mapa = tmp_path / "mapa.json"
    mapa.write_text('{"tok-a": {"rol": "be", "carril": "64bis"}}')
    otro = tmp_path / "otro.json"
    otro.write_text('{"tok-viejo": {"rol": "be", "carril": "64bis"}}')
    repo, casa, binario = _sandbox(tmp_path / "x", f'''
if [ "$1" = "inspect" ]; then echo "Error: No such object: $2" >&2; exit 1; fi
printf 'args=%s\\n' "$*" >> "{reg}"
exit 0
''')
    aplicado = repo / ".llmi-applied"
    # ① atestado ERRÓNEO: el proceso declara otro mapa
    malo = _Salud(otro, "token-de-prueba")
    try:
        r1 = _corre(repo, casa, binario, "up",
                    extra_env={"LLMINBOX_CREDENCIALES": str(mapa),
                               "LLMINBOX_API": malo.url, "LLMI_ATESTADO_ESPERA": "2"})
    finally:
        malo.para()
    assert r1.returncode != 0, "el atestado erróneo salió con 0"
    assert not aplicado.exists(), (
        "`.llmi-applied` avanzó con el atestado en rojo: el siguiente `up` creerá que no "
        "hay nada que hacer y el contenedor se queda con el mapa viejo para siempre")

    # ② segundo `up`, ahora con el atestado BUENO: TIENE que recrear.
    reg.write_text("")
    bueno = _Salud(casa / ".llmi-v8-mapa-llminbox.json", "token-de-prueba")
    try:
        r2 = _corre(repo, casa, binario, "up",
                    extra_env={"LLMINBOX_CREDENCIALES": str(mapa),
                               "LLMINBOX_API": bueno.url, "LLMI_ATESTADO_ESPERA": "5"})
    finally:
        bueno.para()
    assert r2.returncode == 0, f"{r2.stdout}\n{r2.stderr}"
    assert "--force-recreate" in reg.read_text(), (
        f"el segundo `up` no recreó: {reg.read_text()!r} — justo lo que el mensaje del "
        "primero prometía que pasaría")
    assert aplicado.exists(), "tras un `up` bueno el sello de estado tiene que avanzar"


def test_la_API_sale_del_envfile_y_no_de_una_instancia_ajena(tmp_path):
    """P0. `API` se calculaba en la línea 18 y `~/.llminbox-env` se leía en la 44: la
    configuración canónica del operador llegaba TARDE. Con una segunda instancia
    declarada allí, el atestado se le pedía a `127.0.0.1:8077` — otra instancia — y el
    despliegue se sellaba (o se bloqueaba) por lo que dijera un servicio que no es el
    suyo. Es la misma clase que ya curé para `COMPOSE_PROJECT_NAME` y dejé aquí.
    """
    reg = tmp_path / "docker.log"
    mapa = tmp_path / "mapa.json"
    mapa.write_text('{"tok-a": {"rol": "be", "carril": "64bis"}}')
    repo, casa, binario = _sandbox(tmp_path / "x", f'''
if [ "$1" = "inspect" ]; then echo "Error: No such object: $2" >&2; exit 1; fi
printf 'args=%s\\n' "$*" >> "{reg}"
exit 0
''')
    salud = _Salud(casa / ".llmi-v8-mapa-llminbox.json", "token-de-prueba")
    # LA API SÓLO EXISTE EN EL ENVFILE. Si `llmi` la calcula antes de leerlo, apunta al
    # 8077 por defecto y el atestado nunca llega.
    (casa / ".llminbox-env").write_text(f"LLMINBOX_API={salud.url}\n")
    try:
        r = _corre(repo, casa, binario, "up",
                   extra_env={"LLMINBOX_CREDENCIALES": str(mapa),
                              "LLMI_ATESTADO_ESPERA": "3"})
    finally:
        salud.para()
    assert r.returncode == 0, (
        "no encontró el servicio de SU instancia: la API se resolvió antes de leer "
        f"`~/.llminbox-env`\n{r.stdout}\n{r.stderr}")
    assert (repo / ".llmi-applied").exists(), "no llegó a sellar"


def test_la_ruta_de_error_no_EJECUTA_nada(tmp_path):
    """P1, y es de los que dan vergüenza: en el mensaje de error escribí

        echo "   `compose up` salió bien, pero…"

    con acentos graves DENTRO de comillas dobles. En `sh` eso no es tipografía: es
    sustitución de comandos. La ruta de error —que corre justo cuando algo ya ha ido
    mal— ejecutaba `compose up`.
    """
    reg = tmp_path / "docker.log"
    ejecutado = tmp_path / "compose-fue-ejecutado"
    mapa = tmp_path / "mapa.json"
    mapa.write_text('{"tok-a": {"rol": "be", "carril": "64bis"}}')
    otro = tmp_path / "otro.json"
    otro.write_text('{"tok-viejo": {"rol": "be", "carril": "64bis"}}')
    repo, casa, binario = _sandbox(tmp_path / "x", f'''
if [ "$1" = "inspect" ]; then echo "Error: No such object: $2" >&2; exit 1; fi
printf 'args=%s\\n' "$*" >> "{reg}"
exit 0
''')
    # un `compose` en el PATH que deja marca si alguien lo invoca
    (binario / "compose").write_text(f'#!/bin/sh\n: > "{ejecutado}"\nexit 0\n')
    (binario / "compose").chmod(0o755)
    salud = _Salud(otro, "token-de-prueba")        # fuerza la RUTA DE ERROR
    try:
        r = _corre(repo, casa, binario, "up",
                   extra_env={"LLMINBOX_CREDENCIALES": str(mapa),
                              "LLMINBOX_API": salud.url, "LLMI_ATESTADO_ESPERA": "2"})
    finally:
        salud.para()
    assert r.returncode != 0, "la ruta de error no se alcanzó: el test no prueba nada"
    assert not ejecutado.exists(), (
        "el mensaje de error EJECUTÓ `compose`: acentos graves sin escapar dentro de "
        "comillas dobles")


def test_el_servicio_firma_LOS_MISMOS_BYTES_que_parsea(tmp_path, monkeypatch):
    """P0. `_credenciales()` abría el mapa y `_atestado_mapa()` lo abría OTRA VEZ: dos
    lecturas, y entre ellas cabe una sustitución. El servicio podía **parsear A y firmar
    B**, y entonces el atestado —que existe justo para acreditar qué cargó el proceso—
    acreditaba un fichero que el proceso nunca usó. Un testigo que mira otra cosa.
    """
    import builtins, hashlib, hmac, importlib, json, sys
    A_BYTES = b'{"tok-a": {"rol": "be", "carril": "64bis"}}'
    B_BYTES = b'{"tok-b": {"rol": "qa", "carril": "otro"}}'
    mapa = tmp_path / "mapa.json"
    mapa.write_bytes(A_BYTES)
    (tmp_path / "roster.json").write_text(json.dumps({
        "agentes": [{"nombre": "backend", "humano": "a", "clave": "", "rol": "be"},
                    {"nombre": "qa", "humano": "a", "clave": "", "rol": "qa"}],
        "humanos": [{"nombre": "a", "alias": []}], "difusion": []}))
    monkeypatch.setenv("LLMINBOX_ROSTER", str(tmp_path / "roster.json"))
    monkeypatch.setenv("LLMINBOX_DB", str(tmp_path / "p.sqlite"))
    monkeypatch.setenv("LLMINBOX_CREDENCIALES", str(mapa))
    monkeypatch.setenv("LLMINBOX_CREDENCIALES_SHA",
                       hashlib.sha256(A_BYTES).hexdigest())
    monkeypatch.setenv("LLMINBOX_TOKEN", "clave")
    monkeypatch.setenv("HOSTNAME", CID12)

    # LA VENTANA, DETERMINISTA: la SEGUNDA apertura del mapa ve ya el fichero cambiado.
    # (No hay reloj de por medio: se cuenta la apertura.)
    real_open, veces = builtins.open, {"n": 0}
    def open_con_ventana(fichero, *a, **k):
        if str(fichero) == str(mapa):
            veces["n"] += 1
            if veces["n"] == 2:
                mapa.write_bytes(B_BYTES)
        return real_open(fichero, *a, **k)
    monkeypatch.setattr(builtins, "open", open_con_ventana)

    sys.modules.pop("servicio", None)
    try:
        srv = importlib.import_module("servicio")
        parseado = set(srv.CREDENCIALES)
        assert parseado == {"tok-a"}, f"no parseó A: {parseado}"
        assert srv.ATESTADO_MAPA == hmac.new(
            b"clave", CID12.encode() + b"\0" + hashlib.sha256(A_BYTES).hexdigest().encode(), hashlib.sha256).hexdigest(), (
            "el servicio firmó unos bytes que NO son los que parseó: el atestado "
            "acredita un fichero que el proceso nunca cargó")
    finally:
        sys.modules.pop("servicio", None)


def test_el_envfile_no_puede_EJECUTAR_por_el_nombre_de_la_clave(tmp_path):
    """`~/.llminbox-env` se parseaba con `eval` y la clave sólo se comprobaba con
    «empieza por LLMINBOX_». Una línea como

        LLMINBOX_A$(touch /tmp/x)=1

    pasa ese filtro y la sustitución de comandos acaba DENTRO del `eval`. MEDIDO: se
    ejecuta. El valor, en cambio, NO se re-evalúa —la expansión de parámetro no se
    vuelve a escanear— y lo comprobé antes de tocar nada, para no curar lo que no es.
    """
    marca = tmp_path / "EJECUTADO"
    casa, binario = tmp_path / "home", tmp_path / "bin"
    for d in (casa, binario):
        d.mkdir(parents=True, exist_ok=True)
    (casa / ".llminbox.token").write_text("t")
    (binario / "docker").write_text("#!/bin/sh\nexit 0\n")
    (binario / "docker").chmod(0o755)
    (casa / ".llminbox-env").write_text(f'LLMINBOX_A$(touch {marca})=1\n')
    subprocess.run([str(RAIZ / "llmi"), "stat"], cwd=RAIZ,
                   env={"HOME": str(casa), "PATH": f"{binario}:/usr/bin:/bin"},
                   capture_output=True, text=True, timeout=30, check=False)
    assert not marca.exists(), (
        "el nombre de una clave del envfile EJECUTÓ un comando: `eval` sobre una clave "
        "que sólo se filtra por prefijo")


def test_el_sello_describe_LOS_MISMOS_BYTES_que_se_atestiguan(tmp_path):
    """TOCTOU POST-SNAPSHOT. El snapshot se leía DOS veces: una para el sha del sello y
    otra, más tarde, para el HMAC esperado. Si cambia en medio, el HMAC coincide con lo
    que el servicio cargó (B) y el sello guarda el sha de lo anterior (A): el `up` sale
    0 declarando aplicado un mapa que no es el que corre. La segunda lectura de algo
    mutable es una ventana, aunque el fichero sea mío.
    """
    reg = tmp_path / "docker.log"
    mapa = tmp_path / "mapa.json"
    A_ = '{"tok-a": {"rol": "be", "carril": "64bis"}}'
    B_ = '{"tok-b": {"rol": "qa", "carril": "64bis"}}'
    mapa.write_text(A_)
    sustituto = tmp_path / "b.json"
    sustituto.write_text(B_)
    # La ruta del snapshot se conoce ANTES de crear el sandbox: el doble la necesita
    # dentro de su propio texto, y `casa` todavía no existe cuando se escribe.
    snap = tmp_path / "x" / "home" / ".llmi-v8-mapa-llminbox.json"
    repo, casa, binario = _sandbox(tmp_path / "x", f'''
if [ "$1" = "inspect" ]; then echo "Error: No such object: $2" >&2; exit 1; fi
printf 'args=%s\\n' "$*" >> "{reg}"
case "$*" in *"compose up"*) cp "{sustituto}" "{snap}" ;; esac
exit 0
''')
    salud = _Salud(casa / ".llmi-v8-mapa-llminbox.json", "token-de-prueba")
    try:
        r = _corre(repo, casa, binario, "up",
                   extra_env={"LLMINBOX_CREDENCIALES": str(mapa),
                              "LLMINBOX_API": salud.url, "LLMI_ATESTADO_ESPERA": "3"})
    finally:
        salud.para()
    sello = casa / ".llmi-v8-mapa-llminbox.sha256"
    if r.returncode == 0:
        assert sello.exists()
        real = hashlib.sha256((casa / ".llmi-v8-mapa-llminbox.json").read_bytes()).hexdigest()
        assert sello.read_text() == real, (
            "selló el sha de lo que midió al principio, no el de lo que quedó montado y "
            "atestiguado: el sello describe un mapa que no es el que corre")


def test_el_atestado_esta_ATADO_a_la_instancia(tmp_path, monkeypatch):
    """El HMAC era `clave + bytes del mapa`, sin nada que dijera QUÉ instancia lo emite.
    Dos instancias con la misma clave y el mismo mapa producen el MISMO atestado, así
    que el despliegue de B podía darse por bueno con el `/health` de A — que es justo el
    fallo que el atestado existe para impedir. Se ata al nombre de instancia.
    """
    import hashlib, hmac, importlib, json, sys
    mapa = tmp_path / "mapa.json"
    mapa.write_bytes(b'{"tok": {"rol": "be", "carril": "64bis"}}')
    (tmp_path / "roster.json").write_text(json.dumps({
        "agentes": [{"nombre": "backend", "humano": "a", "clave": "", "rol": "be"}],
        "humanos": [{"nombre": "a", "alias": []}], "difusion": []}))
    monkeypatch.setenv("LLMINBOX_ROSTER", str(tmp_path / "roster.json"))
    monkeypatch.setenv("LLMINBOX_DB", str(tmp_path / "p.sqlite"))
    monkeypatch.setenv("LLMINBOX_CREDENCIALES", str(mapa))
    monkeypatch.setenv("LLMINBOX_CREDENCIALES_SHA",
                       hashlib.sha256(mapa.read_bytes()).hexdigest())
    monkeypatch.setenv("LLMINBOX_TOKEN", "clave")

    def atestado_de(instancia: str) -> str:
        monkeypatch.setenv("HOSTNAME", instancia)
        sys.modules.pop("servicio", None)
        return importlib.import_module("servicio").ATESTADO_MAPA

    try:
        a, b = atestado_de(CID12), atestado_de("0123456789ab")
        assert a and b, "sin atestado no hay nada que comparar"
        assert a != b, (
            "dos instancias con la MISMA clave y el MISMO mapa emiten el mismo atestado: "
            "el despliegue de una puede sellarse con el `/health` de la otra")
        # ⊖ y no es aleatorio: la misma instancia da siempre lo mismo
        assert atestado_de(CID12) == a
        assert a != hmac.new(b"clave", mapa.read_bytes(), hashlib.sha256).hexdigest(), (
            "sigue siendo el HMAC del mapa a secas, sin la instancia dentro")
    finally:
        sys.modules.pop("servicio", None)


def test_el_envfile_no_ejecuta_NI_por_clave_NI_por_valor(tmp_path):
    """La clase entera, no sólo el caso. Validar la clave bloqueaba la inyección que
    medí, pero el `eval` seguía ahí: cualquier retoque futuro de ese filtro la reabre.
    El shebang es `bash`, así que hay expansión indirecta y `printf -v` —las dos en
    bash 3.2, el de macOS— y no hace falta `eval` para nada.

    Se prueban los DOS vectores, aunque sólo uno estuviera vivo: el valor no se
    re-evaluaba y lo medí antes de curar. Un falsador que sólo cubre el caso observado
    deja de avisar en cuanto la implementación cambia de forma.
    """
    for etiqueta, linea in (
        ("clave", 'LLMINBOX_A$(touch {m})=1\n'),
        ("clave-backtick", 'LLMINBOX_A`touch {m}`=1\n'),
        ("valor", 'LLMINBOX_API=$(touch {m})\n'),
        ("valor-backtick", 'LLMINBOX_API=`touch {m}`\n'),
    ):
        marca = tmp_path / f"EJECUTADO-{etiqueta}"
        casa, binario = tmp_path / etiqueta / "home", tmp_path / etiqueta / "bin"
        for d in (casa, binario):
            d.mkdir(parents=True, exist_ok=True)
        (casa / ".llminbox.token").write_text("t")
        (binario / "docker").write_text("#!/bin/sh\nexit 0\n")
        (binario / "docker").chmod(0o755)
        (casa / ".llminbox-env").write_text(linea.format(m=marca))
        subprocess.run([str(RAIZ / "llmi"), "stat"], cwd=RAIZ,
                       env={"HOME": str(casa), "PATH": f"{binario}:/usr/bin:/bin"},
                       capture_output=True, text=True, timeout=30, check=False)
        assert not marca.exists(), f"el envfile EJECUTÓ por el vector `{etiqueta}`"


def _salud_con_atestado(valor: str):
    """Un `/health` que devuelve EXACTAMENTE el atestado que se le diga."""
    import http.server, json, threading
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            cuerpo = json.dumps({"ok": True, "v8": {
                "configurado": True, "mapa_atestado": valor}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(cuerpo)))
            self.end_headers()
            self.wfile.write(cuerpo)
        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_una_API_ajena_HOMONIMA_no_puede_acreditar_este_despliegue(tmp_path):
    """El atestado se ataba a `LLMINBOX_NAME`, que es un nombre LÓGICO: cualquiera puede
    llamarse igual. Un servicio ajeno con el mismo mapa, la misma clave y el mismo nombre
    devuelve el HMAC esperado, y `llmi` daba el despliegue por bueno — sellando un
    proceso que no es el que acaba de arrancar. El testigo tiene que estar atado a ALGO
    QUE SÓLO ESTE CONTENEDOR PUEDE DECIR: su identidad real, no su etiqueta.
    """
    import hashlib, hmac
    reg = tmp_path / "docker.log"
    mapa = tmp_path / "mapa.json"
    mapa.write_bytes(b'{"tok": {"rol": "be", "carril": "64bis"}}')
    snap = tmp_path / "x" / "home" / ".llmi-v8-mapa-llminbox.json"
    ID_LOCAL = "aaaabbbbcccc0000111122223333444455556666777788889999000011112222"
    repo, casa, binario = _sandbox(tmp_path / "x", f'''
if [ "$1" = "inspect" ]; then
  case "$*" in
    *"{{{{.Id}}}}"*) printf '%s\\n' "{ID_LOCAL}"; exit 0 ;;
  esac
  echo "Error: No such object: $2" >&2; exit 1
fi
printf 'args=%s\\n' "$*" >> "{reg}"
exit 0
''')
    # LA API AJENA: firma con el NOMBRE LÓGICO, que es lo que cualquiera puede copiar.
    ajeno = hmac.new(b"token-de-prueba",
                     b"llminbox\0" + hashlib.sha256(mapa.read_bytes()).hexdigest().encode(), hashlib.sha256).hexdigest()
    srv, url = _salud_con_atestado(ajeno)
    try:
        r = _corre(repo, casa, binario, "up",
                   extra_env={"LLMINBOX_CREDENCIALES": str(mapa),
                              "LLMINBOX_API": url, "LLMI_ATESTADO_ESPERA": "2"})
    finally:
        srv.shutdown()
    assert r.returncode != 0, (
        "una API AJENA con el mismo nombre lógico acreditó este despliegue:\n"
        f"{r.stdout}\n{r.stderr}")
    assert not (casa / ".llmi-v8-mapa-llminbox.sha256").exists(), "selló con un testigo ajeno"
    assert not (repo / ".llmi-applied").exists(), "avanzó el estado con un testigo ajeno"


def test_si_el_sello_no_se_puede_escribir_el_up_NO_sale_con_cero(tmp_path):
    """El `mv` del sello iba dentro de un `{ …; chmod … || true; }` cuyo resultado nadie
    miraba: si la escritura fallaba, `llmi` seguía, avanzaba `.llmi-applied` y salía 0
    **sin sello**. El siguiente `up` compararía contra una línea base que no existe.

    Se reproduce dejando la ruta del sello ocupada por un DIRECTORIO: el `mv` no falla
    ruidosamente, mete el temporal dentro, y el sello queda ausente — el modo de fallo
    más silencioso de los posibles.
    """
    import hashlib, hmac
    reg = tmp_path / "docker.log"
    mapa = tmp_path / "mapa.json"
    mapa.write_bytes(b'{"tok": {"rol": "be", "carril": "64bis"}}')
    ID_LOCAL = "ffff0000ffff0000ffff0000ffff0000ffff0000ffff0000ffff0000ffff0000"
    repo, casa, binario = _sandbox(tmp_path / "x", f'''
if [ "$1" = "inspect" ]; then
  case "$*" in
    *"{{{{.Id}}}}"*) printf '%s\\n' "{ID_LOCAL}"; exit 0 ;;
  esac
  echo "Error: No such object: $2" >&2; exit 1
fi
printf 'args=%s\\n' "$*" >> "{reg}"
exit 0
''')
    (casa / ".llmi-v8-mapa-llminbox.sha256").mkdir()      # la ruta del sello, OCUPADA
    esperado = hmac.new(b"token-de-prueba",
                        ID_LOCAL[:12].encode() + b"\0" + hashlib.sha256(mapa.read_bytes()).hexdigest().encode(),
                        hashlib.sha256).hexdigest()
    srv, url = _salud_con_atestado(esperado)
    try:
        r = _corre(repo, casa, binario, "up",
                   extra_env={"LLMINBOX_CREDENCIALES": str(mapa),
                              "LLMINBOX_API": url, "LLMI_ATESTADO_ESPERA": "5"})
    finally:
        srv.shutdown()
    assert r.returncode != 0, (
        f"salió 0 sin haber podido escribir el sello:\n{r.stdout}\n{r.stderr}")
    assert not (repo / ".llmi-applied").exists(), (
        "avanzó `.llmi-applied` sin sello: el siguiente `up` compara contra una línea "
        "base que no existe")


def test_un_mapa_alterado_tras_el_atestado_no_sobrevive_a_un_REINICIO(tmp_path, monkeypatch):
    """El agujero que `llmi` no puede tapar por sí solo. Tras el atestado, la ruta
    bind-montada puede mutar A→B; y con `restart: unless-stopped` un reinicio de Docker
    carga B **sin pasar por `llmi`** ni por atestado ninguno. Un envoltorio no puede
    prometer nada sobre reinicios que no origina.

    Quien sí puede es el proceso: el digest del mapa desplegado viaja DENTRO del
    contenedor y se comprueba EN CADA ARRANQUE. Si no cuadra, no se cargan credenciales
    —arrancar con un mapa alterado es peor que quedarse sin V8— y se grita por `/health`.
    NO se tumba el bus: negarse a arrancar ya lo tiró 11 veces en un día.
    """
    import hashlib, importlib, json, sys
    mapa = tmp_path / "mapa.json"
    mapa.write_bytes(b'{"tok": {"rol": "be", "carril": "64bis"}}')
    (tmp_path / "roster.json").write_text(json.dumps({
        "agentes": [{"nombre": "backend", "humano": "a", "clave": "", "rol": "be"}],
        "humanos": [{"nombre": "a", "alias": []}], "difusion": []}))
    monkeypatch.setenv("LLMINBOX_ROSTER", str(tmp_path / "roster.json"))
    monkeypatch.setenv("LLMINBOX_DB", str(tmp_path / "p.sqlite"))
    monkeypatch.setenv("LLMINBOX_CREDENCIALES", str(mapa))
    monkeypatch.setenv("LLMINBOX_TOKEN", "clave")
    monkeypatch.setenv("HOSTNAME", CID12)
    bueno = hashlib.sha256(mapa.read_bytes()).hexdigest()

    def arranca() -> object:
        sys.modules.pop("servicio", None)
        return importlib.import_module("servicio")

    try:
        # ⊕ el mapa desplegado: se carga y se atestigua
        monkeypatch.setenv("LLMINBOX_CREDENCIALES_SHA", bueno)
        srv = arranca()
        assert srv.CREDENCIALES, "no cargó el mapa correcto"
        assert srv.ATESTADO_MAPA, "no atestiguó el mapa correcto"
        assert not srv._MAPA_ALTERADO

        # ⊖ EL REINICIO con el mapa cambiado por debajo
        mapa.write_bytes(b'{"otro": {"rol": "qa", "carril": "otro"}}')
        srv = arranca()
        assert srv.CREDENCIALES == {}, (
            "cargó un mapa que NO es el que se desplegó: un reinicio de Docker se salta "
            "a `llmi` entero")
        assert srv.ATESTADO_MAPA == "", "atestiguó un mapa alterado"
        assert srv._MAPA_ALTERADO, "no dejó constancia de la alteración"
    finally:
        sys.modules.pop("servicio", None)


def test_el_nombre_de_instancia_es_un_proyecto_de_compose_VALIDO(tmp_path):
    """El validador admitía mayúsculas y puntos; Compose sólo acepta `[a-z0-9_-]`
    empezando por letra o número minúscula. Admitir aquí lo que el orquestador rechaza
    después mueve el fallo al último paso y con su vocabulario — que es justo lo que
    este validador existe para evitar.
    """
    casa, binario = tmp_path / "home", tmp_path / "bin"
    for d in (casa, binario):
        d.mkdir(parents=True, exist_ok=True)
    (casa / ".llminbox.token").write_text("t")
    marca = tmp_path / "docker-invocado"
    (binario / "docker").write_text(f'#!/bin/sh\n: > "{marca}"\nexit 0\n')
    (binario / "docker").chmod(0o755)
    # El vacío NO va en esta lista: `${LLMINBOX_NAME:-llminbox}` lo sustituye por el
    # defecto, que es la conducta correcta y no un nombre inválido.
    for malo in ("Mayuscula", "con.punto", "-empieza-guion", "espacio malo", "MAYUS"):
        if marca.exists():
            marca.unlink()
        r = subprocess.run([str(RAIZ / "llmi"), "down"], cwd=RAIZ,
                           env={"HOME": str(casa), "PATH": f"{binario}:/usr/bin:/bin",
                                "LLMINBOX_NAME": malo},
                           capture_output=True, text=True, timeout=30, check=False)
        assert r.returncode != 0, f"aceptó el nombre inválido {malo!r}"
        assert not marca.exists(), f"el nombre {malo!r} llegó hasta Docker"
    # ⊕ un nombre válido SÍ pasa: un validador que dice que no a todo no sirve
    if marca.exists():
        marca.unlink()
    r = subprocess.run([str(RAIZ / "llmi"), "down"], cwd=RAIZ,
                       env={"HOME": str(casa), "PATH": f"{binario}:/usr/bin:/bin",
                            "LLMINBOX_NAME": "segunda-instancia_2"},
                       capture_output=True, text=True, timeout=30, check=False)
    assert r.returncode == 0, f"rechazó un nombre válido: {r.stderr}"
    assert marca.exists(), "un nombre válido no llegó a Docker"


def test_con_el_mapa_ALTERADO_se_lee_pero_no_se_MUTA(tmp_path, monkeypatch):
    """E2E de endpoint, con su control. Mi primera cura del reinicio hacía lo contrario
    de lo que decía: al detectar el mapa alterado vaciaba `CREDENCIALES`, con lo que
    `IDENTIDAD` cae a `_SinIdentidad()`, `exige_ser()` sólo anota, y el token compartido
    autorizaba las mutaciones igual. Manipular el fichero DESACTIVABA el gate: fail-OPEN,
    peor que el agujero que creía tapar.

    Va en middleware y no por ruta porque hay verbos que sólo pasan por `auth` y nunca
    llaman a `exige_ser`: una protección que dependa de que cada ruta se acuerde nace
    con agujeros.
    """
    import hashlib, importlib, json, sys
    from fastapi.testclient import TestClient
    mapa = tmp_path / "mapa.json"
    mapa.write_bytes(b'{"tok": {"rol": "be", "carril": "64bis"}}')
    (tmp_path / "roster.json").write_text(json.dumps({
        "agentes": [{"nombre": "backend", "humano": "a", "clave": "", "rol": "be"}],
        "humanos": [{"nombre": "a", "alias": []}], "difusion": []}))
    monkeypatch.setenv("LLMINBOX_ROSTER", str(tmp_path / "roster.json"))
    monkeypatch.setenv("LLMINBOX_DB", str(tmp_path / "p.sqlite"))
    monkeypatch.setenv("LLMINBOX_CREDENCIALES", str(mapa))
    monkeypatch.setenv("LLMINBOX_TOKEN", "clave")
    monkeypatch.setenv("HOSTNAME", CID12)
    monkeypatch.setenv("LLMINBOX_CREDENCIALES_SHA",
                       hashlib.sha256(mapa.read_bytes()).hexdigest())
    cab = {"X-Llminbox-Token": "clave"}

    def cliente():
        sys.modules.pop("servicio", None)
        srv = importlib.import_module("servicio")
        return srv, TestClient(srv.app)

    try:
        # ⊕ CONTROL: con el mapa BUENO, el POST no lo rechaza la integridad.
        srv, c = cliente()
        assert not srv._MAPA_ALTERADO
        r_ok = c.post("/inbox/backend/leido", json={"hasta": {}}, headers=cab)
        assert r_ok.status_code != 503, (
            f"el control ya sale 503 sin mapa alterado: el ⊖ no probaría nada ({r_ok.text[:120]})")

        # ⊖ EL MAPA CAMBIA POR DEBAJO Y EL PROCESO REARRANCA
        mapa.write_bytes(b'{"otro": {"rol": "qa", "carril": "x"}}')
        srv, c = cliente()
        assert srv._MAPA_ALTERADO, "no detectó la alteración"
        salud = c.get("/health")
        assert salud.status_code == 200, "tumbó el bus: leer tiene que seguir"
        assert salud.json()["v8"]["mapa_alterado"] is True, "no lo grita en /health"
        assert c.get("/inbox/backend", headers=cab).status_code == 200, "leer dejó de funcionar"
        r = c.post("/inbox/backend/leido", json={"hasta": {}}, headers=cab)
        assert r.status_code == 503, (
            f"MUTÓ con el mapa alterado y el token compartido: {r.status_code} {r.text[:160]}")
        assert "INTEGRIDAD" in r.text
        # y un verbo que NO pasa por `exige_ser` tampoco pasa
        r2 = c.post("/append", json={"ledger": "x", "cuerpo": "y"}, headers=cab)
        assert r2.status_code == 503, f"un verbo sin exige_ser sí mutó: {r2.status_code}"
    finally:
        sys.modules.pop("servicio", None)
