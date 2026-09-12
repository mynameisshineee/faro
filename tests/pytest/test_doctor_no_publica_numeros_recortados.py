"""`/doctor` publicaba dos números que no eran lo que su texto decía.

Salieron del barrido por clases y los verifiqué con datos vivos antes de tocar nada.

① EL TITULAR DE LA SECCIÓN ② SUMABA SÓLO EL TOP-20. La consulta lleva `LIMIT 20` para la
   TABLA, y el titular sumaba esas veinte filas. Medido en producción:

       autores con huérfanas:        52
       titular publicaba:        32.203
       real:                     33.601      ← 1.398 fuera, el 4%

   Y la sección ① del MISMO informe ya declara su recorte («y N fila(s) más»). El patrón
   correcto estaba treinta líneas más arriba. Igual que con la carrera del claim.

② LA CABECERA PROMETE UNA VENTANA QUE LA SECCIÓN ① NO APLICA. `── doctor · ventana de N
   día(s) ──` y luego ① cuenta correo sin consumir, que es un STOCK y no un flujo de la
   ventana. No está mal contado: está mal PRESENTADO, y quien lee no puede saberlo. Un
   `llmi doctor 1` y un `llmi doctor 30` dan la misma ①, lo que se lee como «esto no se
   mueve» en vez de «esto no depende del corte».

Los dos son la misma clase: un número que afirma más de lo que mide.
"""
from __future__ import annotations
import pathlib
import re


def _doctor() -> str:
    t = pathlib.Path("servicio.py").read_text().split("\n")
    i = next(k for k, l in enumerate(t) if "① MIRA Y NO DRENA" in l)
    ini = max(0, i - 130)
    fin = next(k for k in range(i, len(t)) if "TENDENCIA" in t[k])
    return "\n".join(t[ini:fin + 20])


def test_el_titular_no_suma_una_lista_truncada():
    """⊖: volver a `sum(... for r in sin_dir)` sobre la lista con LIMIT."""
    c = _doctor()
    assert "hue = sum(r[\"huerfanas\"] for r in sin_dir)" not in c, (
        "el titular vuelve a sumar la lista truncada por LIMIT 20: publica el total del "
        "top-20 como si fuera el total (medido: 32.203 de 33.601 reales)")
    assert re.search(r"hue\s*=\s*con\.execute", c), (
        "el total de huérfanas no se mide con su propia consulta sin LIMIT")


def test_la_tabla_dice_cuantos_autores_NO_muestra():
    """Un top-20 sin nota se lee como «éstos son todos». La sección ① ya lo hace bien."""
    c = _doctor()
    assert "autor(es) más" in c or "autores más" in c, (
        "la tabla de la sección ② no declara cuántos autores quedan fuera del top-20")


def test_la_seccion_1_declara_que_NO_es_de_la_ventana(cliente):
    """⊕ sobre la SALIDA, no sobre el fuente: mi primer intento miraba el comentario del
    código y pasaba con el aviso sin emitir. Lo que lee el operador es el texto.

    Sin esto, `llmi doctor 1` y `llmi doctor 30` dan la misma ① y el lector concluye que
    el número está clavado, en vez de que no depende del corte.
    """
    txt = cliente.get("/doctor").text
    assert "① MIRA Y NO DRENA" in txt, "la sección ① no sale en el informe"
    i = txt.index("① MIRA Y NO DRENA")
    assert "STOCK" in txt[i:i + 300], (
        "la sección ① no declara que es un stock ajeno a la ventana de días que promete "
        "la cabecera")


def test_el_informe_dice_cuantos_autores_deja_fuera(cliente):
    """⊕ del otro recorte, también sobre la salida. Con pocos autores no habrá nota y eso
    es correcto: lo que no puede pasar es que HAYA recorte y no se diga."""
    txt = cliente.get("/doctor").text
    if "② PUBLICA Y NO DIRIGE" in txt:
        i = txt.index("② PUBLICA Y NO DIRIGE")
        bloque = txt[i:i + 2000]
        # UNA FILA DE DATOS NO EMPIEZA POR `· `. Ese prefijo marca una ANOTACIÓN de
        # la sección (② publica dos: lo fechable de la ventana y el stock sin sello), y
        # contarlas como autores hacía creer que había recorte donde no lo hay. Lo
        # destapó añadir esas dos líneas: el test se puso rojo con el informe correcto.
        filas = [l for l in bloque.split("\n") if l.startswith("   ") and l.strip()
                 and not l.strip().startswith("· ")
                 and "autor(es) más" not in l and "todo lo publicado" not in l]
        if len(filas) > 21:          # cabecera de tabla + 20 filas ⇒ hubo recorte
            assert "autor(es) más" in bloque, "hay recorte y no se declara"


def test_ningun_rotulo_de_seccion_se_repite(cliente):
    """⊖ del rótulo: añadí «③ NOMBRA Y NO LLEGA» sin mirar que ya existía un ③.

    Dos secciones con el mismo número en un informe que la gente cita por número («mira el
    ③») hacen la referencia ambigua justo donde sirve para algo. No es cosmético: el
    informe existe para que alguien actúe sobre una sección concreta.

    El test recorre la SALIDA, no el fuente: un rótulo bien escrito en el código y mal
    emitido seguiría colisionando.
    """
    import collections
    import os
    import re
    import sqlite3
    from datetime import datetime, timedelta, timezone
    # SE PLANTA EL CASO: la sección de claims sólo se emite si HAY claims vencidos, así que
    # sin esto las dos secciones nunca coinciden en la misma salida y el test no puede
    # fallar. Mi primera versión pasaba con la colisión puesta, por eso.
    con = sqlite3.connect(os.environ["LLMINBOX_DB"])
    viejo = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
    con.execute("INSERT INTO claims(tema,rol,agent,agent_bruto,abierto,bruto) "
                "VALUES(?,?,?,?,?,?)", ("t-viejo", "ejecuta", "x", "x", viejo, "t"))
    con.execute(
        "INSERT OR REPLACE INTO entries (ledger,eid,arrival,seq,line_no,byte_off,ts,"
        "actor,tipo,head,body,visto,ausente,provisional,raw_tipo) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        # ts RELATIVO: con el FIJO, desde el 2026-09-08 la sección ⑦ dejó de emitirse y
        # este test pasaba con la colisión ③/③ PUESTA — medido con mutante por sdet el
        # 2026-09-11; misma bomba que test_arrobas_que_no_llegan.
        ("l", "rot1", 950, 0, 0, 0,
         (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S"),
         "alguien", "FYI", "h",
         "aviso a @equipoo sobre esto", None, None, 0, "FYI"))
    con.commit(); con.close()
    txt = cliente.get("/doctor").text
    assert "NOMBRA Y NO LLEGA" in txt, (
        "la sección de arrobas no se emite: sin ella este test no puede ver la colisión")
    rot = re.findall(r"^([①②③④⑤⑥⑦⑧⑨])\s", txt, re.M)
    dup = [k for k, n in collections.Counter(rot).items() if n > 1]
    assert not dup, f"rótulos repetidos en el informe: {dup} (de {rot})"
