"""El umbral del hombre muerto se puede configurar por despliegue.

`LLMINBOX_VIGILANCIA_MUDA_S` la LEE el servicio (default 180) y `docker-compose.yml` NO
LA PASABA: exportarla no servía de nada porque no había camino hasta el proceso. O sea
que el número que gobierna cuándo se declara sorda a la flota estaba clavado en el código.

② TIENE CONSUMIDOR, y por eso se arregla en vez de documentarse — es el filtro que dejó
fuera a las otras nueve variables huérfanas del mismo grep:

  · `/health.vigilancia.tope_s` lo publica, y el `vigia-buzon-local` de la flota DERIVA
    de ahí su propio umbral (declarado por @harness el 2026-08-31), para que las dos
    mitades no se desincronicen. Si el número está clavado, lo están las dos.
  · y el ciclo real del watcher se mide en segundos: si un día tarda más que el tope, la
    flota entera queda `muda` de forma permanente y NO HAY FORMA de ajustarlo sin
    reconstruir la imagen.
"""
from __future__ import annotations
import re
import pathlib
from fastapi.testclient import TestClient
from .conftest import construir


def test_el_compose_pasa_la_variable():
    """El camino ENTERO, no sólo la mitad que vive en Python: una variable que el código
    lee y el compose no pasa es inconfigurable en el único sitio donde se despliega."""
    comp = pathlib.Path("docker-compose.yml").read_text()
    assert re.search(r'^\s+LLMINBOX_VIGILANCIA_MUDA_S:', comp, re.M), (
        "el compose no pasa el umbral: exportarlo no llega al contenedor")


def test_el_valor_configurado_llega_a_health(tmp_path, monkeypatch):
    monkeypatch.setenv("LLMINBOX_VIGILANCIA_MUDA_S", "45")
    monkeypatch.setenv("LLMINBOX_WATCHER_TOKEN", "w")
    s = construir(tmp_path, monkeypatch)
    v = TestClient(s.app).get("/health", headers={"X-Llminbox-Token": s.TOKEN}).json()["vigilancia"]
    assert v["tope_s"] == 45


def test_sin_configurar_mantiene_el_default(tmp_path, monkeypatch):
    """⊕ obligatorio: la pasarela va VACÍA por defecto, así que un despliegue que no la
    exporte tiene que seguir con 180 — si la pasarela pisara el default con `""`, el
    umbral se iría a cero y la flota entera saldría `muda` al instante."""
    monkeypatch.delenv("LLMINBOX_VIGILANCIA_MUDA_S", raising=False)
    monkeypatch.setenv("LLMINBOX_WATCHER_TOKEN", "w")
    s = construir(tmp_path, monkeypatch)
    v = TestClient(s.app).get("/health", headers={"X-Llminbox-Token": s.TOKEN}).json()["vigilancia"]
    assert v["tope_s"] == 180


def test_una_cadena_vacia_NO_se_toma_como_cero(tmp_path, monkeypatch):
    """⊖ EL QUE IMPORTA, y el motivo de que la pasarela sea peligrosa: `"${VAR:-}"` inyecta
    la variable con valor VACÍO cuando nadie la exporta. Si el servicio hiciera
    `float("" or 180)` bien, pero `float("")` revienta y `int("" )` también — y un umbral
    que se lee como 0 declara sorda a la flota entera en el primer barrido."""
    monkeypatch.setenv("LLMINBOX_VIGILANCIA_MUDA_S", "")
    monkeypatch.setenv("LLMINBOX_WATCHER_TOKEN", "w")
    s = construir(tmp_path, monkeypatch)
    v = TestClient(s.app).get("/health", headers={"X-Llminbox-Token": s.TOKEN}).json()["vigilancia"]
    assert v["tope_s"] == 180, (
        f"una cadena vacía se tomó como {v['tope_s']}: la pasarela del compose inyecta "
        f"exactamente eso cuando nadie exporta la variable")


def test_un_valor_BASURA_no_cae_al_default_en_silencio(tmp_path, monkeypatch):
    """⊖ DE LA CURA, y la distinción que la hace correcta: «no configurado» (vacío) y
    «configurado MAL» (`"abc"`, `"30s"`, `"-5"`) no son el mismo caso.

    El primero cae al default, que es lo que significa no configurar nada. El segundo
    tiene que FALLAR RUIDOSO: alguien escribió un valor a propósito y no está en vigor.
    Tragarlo sería la clase de toda esta semana —config inválida cayendo al lado bueno—
    y encima en el número que decide cuándo se declara sorda a la flota.
    """
    import pytest as _p
    for basura in ("abc", "30s", "-5", "0"):
        monkeypatch.setenv("LLMINBOX_VIGILANCIA_MUDA_S", basura)
        monkeypatch.setenv("LLMINBOX_WATCHER_TOKEN", "w")
        # `SystemExit` hereda de BaseException, NO de Exception: un `raises(Exception)`
        # lo deja pasar y el test parece fallar cuando el producto acierta. Me pasó aquí.
        with _p.raises(SystemExit) as e:
            construir(tmp_path, monkeypatch)
        assert "VIGILANCIA_MUDA_S" in str(e.value), (
            f"«{basura}» no menciona la variable en el error: quien despliegue no sabrá qué "
            f"corregir")


def test_un_plazo_ABSURDO_no_desarma_el_hombre_muerto(tmp_path, monkeypatch):
    """El hallazgo es de Codex (PR #48). Su PR quedó en borrador y su autor ya no está, así
    que la adopto — verificada por mi mano, no aplicada a ciegas.

    `LLMINBOX_VIGILANCIA_MUDA_S=180000` era configuración VÁLIDA para Python e INVÁLIDA
    para la función de seguridad: el umbral quedaba en 50 horas, o sea el hombre muerto
    formalmente armado y sin saltar nunca a tiempo. Medido antes de curar.

    Es la clase de siempre por su cara más peligrosa: nadie lo desarma, y por eso nadie
    ve que está desarmado. Un `0` sí lo cazaba el guarda de abajo; un número enorme no.
    """
    import pytest
    from .conftest import construir
    monkeypatch.setenv("LLMINBOX_VIGILANCIA_MUDA_S", "180000")
    with pytest.raises(SystemExit) as e:
        construir(tmp_path, monkeypatch)
    assert "techo" in str(e.value).lower(), str(e.value)


def test_el_techo_no_estorba_a_un_plazo_RAZONABLE(tmp_path, monkeypatch):
    """⊕ obligatorio: un techo que rechace lo legítimo desarma la vigilancia igual, sólo
    que ruidosamente. 1800 s (30 min) es holgado y tiene que pasar."""
    from .conftest import construir
    monkeypatch.setenv("LLMINBOX_VIGILANCIA_MUDA_S", "1800")
    s = construir(tmp_path, monkeypatch)
    assert s.VIGILANCIA_MUDA_S == 1800
