"""Arnés del journal de coordinación — SIN dependencias fuera de la stdlib.

Vive en su propio directorio y NO reutiliza `tests/pytest/conftest.py` a
propósito: aquél importa `fastapi` al cargarse, así que colgar de él haría que
estas pruebas —de un módulo que sólo usa la stdlib— dejaran de poder correr en
un entorno pelado. La separación es el punto, no una comodidad.
"""
from __future__ import annotations

import os
import sys

RAIZ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if RAIZ not in sys.path:
    sys.path.insert(0, RAIZ)
