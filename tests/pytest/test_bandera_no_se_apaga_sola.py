"""Una bandera mal escrita NO puede apagarse sola: eso abre la puerta en silencio.

`CARRIL_OBLIGATORIO` era `os.environ.get(...) == "1"`, así que quien escribiera `true`,
`TRUE`, `yes` o `on` —todas formas razonables de decir «sí»— obtenía **False**: la puerta
de carril desactivada, creyendo haberla activado. Medido antes de tocar nada:

    '1' -> True     'true' -> False   'TRUE' -> False
    'yes' -> False  'on' -> False     'sí' -> False

Es la clase de la semana en una puerta de SEGURIDAD, y por el lado peor: el valor que no
encaja no cae a «no sé», cae a «desactivado». Un operador que enciende la puerta y no la
enciende no tiene forma de saberlo — `/health` sólo avisa si la puerta está PEDIDA y rota,
y con la bandera apagada no está pedida.

La misma disciplina que `_entero_env`: `no configurado` y `configurado mal` son casos
distintos y sólo el primero tiene un destino silencioso.
"""
from __future__ import annotations
import pytest
from .conftest import construir


def _bandera(tmp_path, monkeypatch, valor):
    if valor is None:
        monkeypatch.delenv("LLMINBOX_CARRIL_OBLIGATORIO", raising=False)
    else:
        monkeypatch.setenv("LLMINBOX_CARRIL_OBLIGATORIO", valor)
    monkeypatch.setenv("LLMINBOX_WATCHER_TOKEN", "w")
    return construir(tmp_path, monkeypatch).CARRIL_OBLIGATORIO


def test_el_uno_enciende(tmp_path, monkeypatch):
    assert _bandera(tmp_path, monkeypatch, "1") is True


@pytest.mark.parametrize("v", ["0", ""])
def test_el_cero_y_el_vacio_apagan(tmp_path, monkeypatch, v):
    """⊕ obligatorio: si todo encendiera, el ⊖ de abajo pasaría y habríamos convertido una
    puerta opcional en obligatoria para toda la flota."""
    assert _bandera(tmp_path, monkeypatch, v) is False


def test_ausente_es_apagada(tmp_path, monkeypatch):
    assert _bandera(tmp_path, monkeypatch, None) is False


@pytest.mark.parametrize("v", ["true", "TRUE", "yes", "on", "sí", "vale", "2", "activar"])
def test_un_valor_QUE_NO_SE_ENTIENDE_aborta_en_vez_de_apagar(tmp_path, monkeypatch, v):
    """⊖ EL QUE IMPORTA, y ahora cubre también los sinónimos.

    Mi primera cura los ACEPTABA (`true`, `yes`, `on`…) y era peor arreglo aunque lo
    pareciera: la seguridad no viene del vocabulario, viene de que lo no reconocido
    ABORTE. Con el error ruidoso puesto, cada sinónimo sólo añade otra forma de escribir
    lo mismo y deja al siguiente preguntándose si `enabled` también vale.

    UNA SOLA FORMA CANÓNICA: `1` o `0`. `true` aborta igual que `activar`, y el mensaje
    dice exactamente qué escribir — que es lo que el operador necesita, no una lista de
    equivalencias que tiene que adivinar."""
    with pytest.raises(SystemExit) as e:
        _bandera(tmp_path, monkeypatch, v)
    assert "CARRIL_OBLIGATORIO" in str(e.value)
