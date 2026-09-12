"""El servicio expone SU build — y CÓMO lo sabe, que es la mitad que decide si vale.

Pedido por @sdet vía harness: G0 medía que C5 respondía (CONDUCTA) y de ahí se leía que
el proceso era `cd30e94` (IDENTIDAD). No se sigue: cualquier build que contenga C5 da la
misma evidencia. «Conducta presente» no prueba procedencia.

Y LA PROCEDENCIA DEL DATO ES PARTE DEL DATO. Un `build` que a veces viene inyectado en la
imagen y a veces se deriva del `.git` del disco NO son la misma afirmación: la segunda
puede describir un árbol de trabajo que ya no es lo que corre. Por eso el campo dice
también su ORIGEN — y cuando no se sabe, lo dice, en vez de caer a un valor que se lee
como si se supiera. Es la clase entera de esta semana: el estado sin casilla no puede
aterrizar en el lado tranquilizador.
"""
from __future__ import annotations
import subprocess
from fastapi.testclient import TestClient
from .conftest import construir

SHA = "cd30e94abd22314b8f6cb8caf20e04d3ebe6e9f5"


def test_declarado_gana_y_se_dice_que_es_declarado(tmp_path, monkeypatch):
    monkeypatch.setenv("LLMINBOX_BUILD", SHA)
    s = construir(tmp_path, monkeypatch)
    c = TestClient(s.app)
    b = c.get("/health", headers={"X-Llminbox-Token": s.TOKEN}).json()["build"]
    assert b["sha"] == SHA
    assert b["origen"] == "declarado"


def test_sin_nada_dice_DESCONOCIDO_y_no_finge(tmp_path, monkeypatch):
    """⊖ EL QUE IMPORTA. Sin build inyectado y sin `.git`, el campo NO puede traer una
    cadena que se lea como un SHA: quien la consuma creería tener procedencia."""
    monkeypatch.setenv("LLMINBOX_BUILD", "")
    s = construir(tmp_path, monkeypatch)
    # DESPUÉS de construir: `construir` recarga el módulo, así que un `setattr` previo
    # parchea el objeto que se tira. Mi primera versión lo hacía antes y el test leía el
    # `.git` del repo real — daba el SHA del árbol del operador y yo lo habría firmado
    # como «no inventa nada».
    monkeypatch.setattr(s, "RAIZ_GIT", str(tmp_path / "no-hay-git"))
    c = TestClient(s.app)
    b = c.get("/health", headers={"X-Llminbox-Token": s.TOKEN}).json()["build"]
    assert b["sha"] is None, f"inventó un sha: {b['sha']!r}"
    assert b["origen"] == "desconocido"


def test_version_responde_lo_mismo_y_esta_gateado(tmp_path, monkeypatch):
    monkeypatch.setenv("LLMINBOX_BUILD", SHA)
    s = construir(tmp_path, monkeypatch)
    c = TestClient(s.app)
    assert c.get("/version").status_code == 401, "el build sale sin credencial"
    v = c.get("/version", headers={"X-Llminbox-Token": s.TOKEN})
    assert v.status_code == 200
    assert v.json()["sha"] == SHA
    salud = c.get("/health", headers={"X-Llminbox-Token": s.TOKEN}).json()["build"]
    assert v.json() == salud, "dos superficies dicen builds distintos: peor que una callada"


def test_un_sha_que_no_lo_parece_se_marca_pero_no_se_tira(tmp_path, monkeypatch):
    """No valido la forma para RECHAZAR —un despliegue puede etiquetarse `v2.1` y es
    legítimo—, sino para que el consumidor sepa si puede cruzarlo con `git`."""
    monkeypatch.setenv("LLMINBOX_BUILD", "v2.1-rc3")
    s = construir(tmp_path, monkeypatch)
    b = TestClient(s.app).get("/health", headers={"X-Llminbox-Token": s.TOKEN}).json()["build"]
    assert b["sha"] == "v2.1-rc3"
    assert b["origen"] == "declarado"
    assert b["parece_sha"] is False


def test_derivado_NO_puede_hacerse_pasar_por_declarado(tmp_path, monkeypatch):
    """Añadido porque un mutante SOBREVIVIÓ: cambiar `derivado` por `declarado` en la
    rama del `git rev-parse` dejaba los cuatro tests en verde. O sea que la distinción
    que este campo existe para hacer no la vigilaba nadie, y un refactor podía fundir
    los dos orígenes sin que nada se pusiera rojo.

    La diferencia es material, no cosmética: `declarado` afirma «esto es lo que se
    construyó»; `derivado` sólo «esto es lo que hay en ese directorio ahora». Confundirlos
    es la avería que nos costó congelar la flota — un servicio corriendo el árbol de
    trabajo mientras todos leíamos el grafo de `main`.
    """
    monkeypatch.setenv("LLMINBOX_BUILD", "")
    s = construir(tmp_path, monkeypatch)
    # El paquete de release NO lleva `.git`. Depender del checkout hacía que este
    # falsador se saltase justo en el artefacto y su mutante sobreviviera. Construimos
    # un origen Git mínimo y controlado para ejercer siempre la rama `derivado`.
    origen = tmp_path / "origen-con-git"
    origen.mkdir()
    subprocess.run(["git", "init", "-q", str(origen)], check=True)
    (origen / "testigo").write_text("build derivable\n")
    subprocess.run(["git", "-C", str(origen), "add", "testigo"], check=True)
    subprocess.run([
        "git", "-C", str(origen), "-c", "user.name=llminbox-tests",
        "-c", "user.email=tests@llminbox.invalid", "commit", "-qm", "testigo",
    ], check=True)
    monkeypatch.setattr(s, "RAIZ_GIT", str(origen))
    b = TestClient(s.app).get("/health", headers={"X-Llminbox-Token": s.TOKEN}).json()["build"]
    assert b["origen"] == "derivado", (
        f"un sha sacado del disco se declara como {b['origen']!r}: el consumidor creería "
        f"que viene inyectado en la imagen")
    assert b["sha"] is not None
