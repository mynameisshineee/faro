"""`/health.vigilancia.tope_s` tiene un consumidor QUE NO VIVE EN ESTE REPO.

Declarado por @harness el 2026-08-31: su `vigia-buzon-local` DERIVA su umbral leyendo
este campo al arrancar, para que el self-heal de cada sesión quede acoplado por
construcción a nuestra detección — una sola fuente en vez de dos relojes que se
desincronizan. Si `/health` es ilegible, aborta ruidoso en vez de caer a un default
local silencioso.

POR QUÉ ESTO NECESITA UN TEST PROPIO Y NO BASTA LA COBERTURA QUE YA HABÍA: el campo
estaba tocado sólo por una aserción INCIDENTAL dentro de un test que mide el hombre
muerto (`test_pendientes_y_hombre_muerto`). Eso es cobertura DE REBOTE: protege mientras
ese test exista con esa forma, y desaparece el día que se reescriba — sin que nadie lo
note, porque nada dice que ahí colgaba un contrato.

Y el consumidor es EXTERNO, que es lo que sube la apuesta: ni el grep ni esta suite
pueden verlo. Un renombrado de `tope_s` compila, pasa los 360 tests, y rompe el self-heal
de toda la flota EN SILENCIO. La regla de la casa —«el formato de salida es una API:
grepea quién lo parsea antes de tocarlo»— no alcanza a un parser que vive en otro árbol.
Por eso el guardián tiene que estar aquí dentro.
"""
from __future__ import annotations
from fastapi.testclient import TestClient
from .conftest import construir


def test_tope_s_existe_y_es_un_numero_de_segundos(tmp_path, monkeypatch):
    monkeypatch.setenv("LLMINBOX_VIGILANCIA_MUDA_S", "45")
    s = construir(tmp_path, monkeypatch)
    v = TestClient(s.app).get("/health", headers={"X-Llminbox-Token": s.TOKEN}).json()["vigilancia"]
    assert "tope_s" in v, (
        "`tope_s` ha desaparecido de /health: el `vigia-buzon-local` de la flota lo lee "
        "al arrancar para derivar su umbral. Sin él aborta — ruidoso, pero abortado.")
    assert v["tope_s"] == 45, (
        f"`tope_s` no refleja LLMINBOX_VIGILANCIA_MUDA_S ({v['tope_s']!r} != 45): el "
        f"consumidor derivaría un umbral distinto del nuestro y volveríamos a tener dos "
        f"relojes, que es justo lo que este acoplamiento vino a cerrar")
    assert isinstance(v["tope_s"], (int, float)) and not isinstance(v["tope_s"], bool)


def test_el_campo_esta_donde_el_consumidor_lo_busca(tmp_path, monkeypatch):
    """La RUTA es parte del contrato, no sólo el nombre. Mover `tope_s` a la raíz de
    `/health` —o a `/version`, que sería 'más ordenado'— rompe igual que borrarlo."""
    s = construir(tmp_path, monkeypatch)
    cuerpo = TestClient(s.app).get("/health", headers={"X-Llminbox-Token": s.TOKEN}).json()
    assert "vigilancia" in cuerpo and "tope_s" in cuerpo["vigilancia"], (
        "el consumidor lee `/health` → `vigilancia` → `tope_s`; cualquier otro sitio, "
        "por más limpio que quede, es una ruta que él no mira")
