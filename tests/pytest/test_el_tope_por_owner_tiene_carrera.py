"""El tope por owner se comprueba en dos pasos, y el patrón correcto está 15 líneas abajo.

Salió persiguiendo la pregunta de @harness sobre el WIP global («¿hay carrera entre el
count y el insert?»). La respuesta correcta no era la que le di —`BEGIN IMMEDIATE`— sino
que ESTE REPO YA RESUELVE ESTO BIEN, en la misma función, para el otro rol:

    # rol == 'revisa': el tope se comprueba DENTRO de la sentencia, no antes.
    # Comprobar y luego insertar en dos pasos deja pasar al 4º y al 5º cuando
    # llegan a la vez — probado con 20 procesos: así entran exactamente 3.
    INSERT INTO claims(...) SELECT ... WHERE (SELECT COUNT(*) ...) < ?

El camino `ejecuta` hace justo lo que ese comentario dice que NO se haga: un SELECT que
cuenta `mios`, y luego un INSERT aparte. Dos claims simultáneos del mismo agente pueden
pasar los dos el chequeo y entrar los dos.

EL FALSADOR SE MIDE POR EL ESTADO DE LA TABLA, no por lo que responda el endpoint —lo
pidió @harness con esas palabras y tiene razón: un 200 no prueba que la fila entró, y
aquí el fallo es justamente que entran filas que no debían.
"""
from __future__ import annotations
import os
import sqlite3
import threading

import pytest


def _claims_vivos(agente: str) -> int:
    con = sqlite3.connect(os.environ["LLMINBOX_DB"])
    try:
        return con.execute(
            "SELECT COUNT(*) FROM claims WHERE agent=? AND rol='ejecuta' "
            "AND cerrado IS NULL", (agente,)).fetchone()[0]
    finally:
        con.close()


def test_el_tope_por_owner_aguanta_la_simultaneidad(cliente, servicio):
    """⊖ del EFECTO, medido en la TABLA y no en la respuesta.

    LA RÁFAGA SE REPITE A PROPÓSITO. Una sola tanda de 9 peticiones simultáneas contra el
    código con la carrera dio 5 vivos con tope 3 en un intento y 3 en los dos siguientes:
    caza el defecto 1 de cada 3 veces. Un detector así, en CI, dice «verde» dos de cada
    tres veces sobre código roto — que es exactamente la clase de verde que llevamos todo
    el día persiguiendo, sólo que disfrazada de test de concurrencia.

    Con 12 tandas la probabilidad de no verlo cae por debajo del 1%. Y en el código curado
    el tope aguanta en TODAS, así que repetir no lo vuelve intermitente en verde: sólo deja
    de serlo en rojo.
    """
    tope = servicio.TOPE_EJECUTA
    quien = sorted(servicio.lp.CANON)[0]
    rol = servicio.lp.rol_de(quien)
    n = tope + 6
    peor = 0

    for tanda in range(12):
        con = sqlite3.connect(os.environ["LLMINBOX_DB"])
        con.execute("DELETE FROM claims WHERE agent=?", (rol,))
        con.commit(); con.close()

        barrera = threading.Barrier(n)
        errores: list = []

        def coge(i, _t=tanda):
            try:
                barrera.wait(timeout=10)
                cliente.post("/claim", json={"tema": f"carrera-{_t}-{i}",
                                             "agent": quien, "rol": "ejecuta"})
            except Exception as e:                  # pragma: no cover
                errores.append(e)

        hilos = [threading.Thread(target=coge, args=(i,)) for i in range(n)]
        for h in hilos: h.start()
        for h in hilos: h.join(timeout=30)
        assert not errores, errores
        peor = max(peor, _claims_vivos(rol))

    assert peor <= tope, (
        f"{peor} claims vivos con el tope en {tope}: el chequeo y el INSERT van en dos "
        f"pasos y las peticiones simultáneas se cuelan entre medias. El patrón correcto "
        f"—INSERT ... SELECT ... WHERE (SELECT COUNT(*)) < ?— ya está en esta misma "
        f"función para el rol 'revisa', con su comentario explicando por qué")


def test_el_tope_se_comprueba_DENTRO_de_la_sentencia(cliente):
    """⊕ del mecanismo: aunque la carrera no se reproduzca en una máquina concreta, la
    FORMA sí es comprobable. Sin esto, el test de arriba pasa por suerte de scheduler y
    la regresión vuelve sin que nadie la vea."""
    import pathlib
    t = pathlib.Path("servicio.py").read_text().split("\n")
    i = next(k for k, l in enumerate(t) if l.startswith("def coger("))
    fin = next(k for k in range(i + 1, len(t)) if t[k].startswith(("def ", "@")))
    cuerpo = "\n".join(t[i:fin])
    ej = cuerpo[:cuerpo.index("# rol == 'revisa'")]
    assert "INSERT INTO claims" in ej, "no encuentro el INSERT del camino 'ejecuta'"
    ins = ej[ej.index("INSERT INTO claims"):]
    assert "SELECT COUNT(*)" in ins, (
        "el INSERT de 'ejecuta' no lleva el tope dentro de la sentencia: se comprueba "
        "antes, en un SELECT aparte, y eso es la carrera")
