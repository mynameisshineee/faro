"""El build trae, además de lo que DECLARA, algo que el proceso no puede fingir.

@cto lo señaló auditando: `build.origen == "declarado"` es **el productor afirmando de sí
mismo**, y `parece_sha` sólo comprueba la FORMA. Un proceso puede decir que corre
`abc1234` y estar corriendo cualquier otra cosa; el receptor no tiene con qué desmentirlo.
Su cura —resolver el SHA contra el repo con `git cat-file -e`— prueba que ese objeto
EXISTE, no que sea el que se está ejecutando.

⇒ `build.huella` es el `sha256` del `servicio.py` que el proceso tiene abierto, leído de
su propio `__file__`. Nadie lo declara: sale del disco que sirve. Un tercero coge el SHA
declarado, saca ese fichero de git, lo hashea y compara. Si no cuadra, el proceso corre
código distinto del que dice — la avería que costó congelar la flota, ahora detectable
desde fuera y sin acceso a la máquina.

⚠️ LA COTA VA EN EL PROPIO CAMPO, porque prometer más de lo que mide sería el defecto de
al lado: cubre EL FICHERO QUE SIRVE, no la imagen ni el árbol. Un cambio en `publicar.py`,
en el `Dockerfile` o en una dependencia no la mueve.
"""
from __future__ import annotations
import hashlib
import pathlib
from fastapi.testclient import TestClient
from .conftest import construir


def test_la_huella_es_del_fichero_que_el_proceso_ejecuta(tmp_path, monkeypatch):
    monkeypatch.setenv("LLMINBOX_WATCHER_TOKEN", "w")
    s = construir(tmp_path, monkeypatch)
    b = TestClient(s.app).get("/health", headers={"X-Llminbox-Token": s.TOKEN}).json()["build"]
    esperado = hashlib.sha256(pathlib.Path(s.__file__).read_bytes()).hexdigest()
    assert b["huella"] == esperado, (
        "la huella no corresponde al fichero que el proceso tiene abierto: entonces no "
        "mide nada y es otra afirmación más")
    assert b["huella_de"] == "servicio.py", "el campo debe declarar QUÉ cubre"


def test_la_huella_CAMBIA_si_cambia_el_fichero(tmp_path, monkeypatch):
    """⊖ EL QUE IMPORTA: si la huella fuera constante —un literal, un hash del nombre—
    pasaría el test de arriba y no detectaría nada. Tiene que MOVERSE con el contenido."""
    monkeypatch.setenv("LLMINBOX_WATCHER_TOKEN", "w")
    s = construir(tmp_path, monkeypatch)
    original = pathlib.Path(s.__file__).read_bytes()
    antes = TestClient(s.app).get("/health", headers={"X-Llminbox-Token": s.TOKEN}).json()["build"]["huella"]
    try:
        pathlib.Path(s.__file__).write_bytes(original + b"\n# mutacion de prueba\n")
        despues = TestClient(s.app).get("/health",
                                        headers={"X-Llminbox-Token": s.TOKEN}).json()["build"]["huella"]
    finally:
        pathlib.Path(s.__file__).write_bytes(original)
    assert despues != antes, (
        "la huella no se movió al cambiar el fichero: es un valor fijo disfrazado de medida")


def test_declarado_y_medido_son_campos_DISTINTOS(tmp_path, monkeypatch):
    """⊖ de alcance: la huella NO sustituye al sha declarado ni lo valida. Son dos
    afirmaciones distintas —«esto es lo que se construyó» y «esto es lo que corre»— y
    fundirlas devolvería el problema que el `origen` vino a separar."""
    monkeypatch.setenv("LLMINBOX_BUILD", "abc1234def5678")
    monkeypatch.setenv("LLMINBOX_WATCHER_TOKEN", "w")
    s = construir(tmp_path, monkeypatch)
    b = TestClient(s.app).get("/health", headers={"X-Llminbox-Token": s.TOKEN}).json()["build"]
    assert b["sha"] == "abc1234def5678" and b["origen"] == "declarado"
    assert b["huella"] != b["sha"], "la huella se está copiando del sha declarado"
    assert len(b["huella"]) == 64


def test_si_no_se_puede_leer_el_fichero_lo_DICE(tmp_path, monkeypatch):
    """Un `except` que devolviera cadena vacía o el sha declarado sería el estado sin
    casilla cayendo al lado bueno, otra vez y en el campo que existe para no fiarse."""
    monkeypatch.setenv("LLMINBOX_WATCHER_TOKEN", "w")
    s = construir(tmp_path, monkeypatch)
    monkeypatch.setattr(s, "__file__", str(tmp_path / "no-existe.py"))
    b = TestClient(s.app).get("/health", headers={"X-Llminbox-Token": s.TOKEN}).json()["build"]
    assert b["huella"] is None, f"inventó una huella: {b['huella']!r}"
