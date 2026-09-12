"""Arnés del piloto M1 — DELIBERADAMENTE VACÍO de dependencias del servicio.

Vive fuera de `tests/pytest/` porque aquel `conftest.py` importa `fastapi` al
cargarse: estas pruebas son ESTÁTICAS (leen el compose y llaman al verificador y
al preflight) y no deben necesitar el stack HTTP para correr. Un arnés que exige
media aplicación para comprobar un YAML es un arnés que un día no se corre.
"""
