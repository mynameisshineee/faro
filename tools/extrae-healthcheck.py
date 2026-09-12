#!/usr/bin/env python3
"""Imprime el comando del HEALTHCHECK del Dockerfile, tal cual se envía.

Existe para que el test del healthcheck pruebe LO QUE SE ENVÍA y no una copia
suya: una copia sigue verde el día que alguien cambia el original, que es
exactamente cómo la imagen acabó comprobando algo distinto de lo que comprobaba
`docker-compose.yml`.
"""
import re
import os
import sys

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
texto = open(os.path.join(RAIZ, "Dockerfile"), encoding="utf-8").read()
# Une las continuaciones de línea antes de buscar: el HEALTHCHECK ocupa dos.
unido = re.sub(r"\\\s*\n\s*", " ", texto)
m = re.search(r'^HEALTHCHECK\b.*?CMD\s+python3\s+-c\s+"(.+?)"\s*$', unido, re.M | re.S)
if not m:
    sys.exit("no encuentro un `HEALTHCHECK ... CMD python3 -c \"…\"` en el Dockerfile")
print(m.group(1))
