"""El rótulo de sección de /inbox es un CONTRATO con los vigías de la flota.

`── <ledger> · N de M para ti …` no es texto decorativo: al menos 7 scripts de
`~/AGENTES/agentes_BIK/*/agent/tools/` lo parsean con el separador ` · ` PEGADO
al nombre del ledger. El 2026-08-11 metí `(carril: X)` en medio y estuvo 45
minutos desplegado cegando a todos en silencio — el vigía de backend hace
`continue` cuando no casa, o sea deja de mirar su bandeja para siempre.

Los patrones de abajo están COPIADOS de esos scripts (no inventados), con su
ruta, para que quien vuelva a tocar el rótulo vea a quién rompe antes de
mergear. Si un vigía cambia su patrón, este test se actualiza con él; lo que no
puede pasar es que el formato cambie sin que nadie lo note.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[2]
REPO_LLMI = str(REPO_DIR / "llmi")

# (etiqueta, patrón tal cual lo usa el script, fichero de origen)
PATRONES_DE_LA_FLOTA = [
    ("backend·awk-seccion", r"^── 64bis-wiki ·",
     "backend-biklabs/agent/tools/ledger-vigia-64bis.sh:48"),
    ("backend·grep-continue", r"^── 64bis-wiki ·",
     "backend-biklabs/agent/tools/ledger-vigia-64bis.sh:141"),
    ("vision-canon·extrae-secciones", r"^── [a-z0-9-]+ · [0-9]+ de [0-9]+",
     "vision-canon/agent/tools/drenar-bandeja.sh:30,81"),
    ("qa·linea-seccion", r"── 64bis-wiki ·",
     "qa-biklabs/agent/tools/vigia-ledger-cockpit.sh:58"),
    ("cfo·sed-total", r".*── 64bis-wiki · [0-9]+ de ([0-9]+) para ti.*",
     "cfo-guardian/agent/tools/ledger-vigia-cfo.sh:45"),
    ("cfo-post·grep-n", r"^── 64bis-wiki · [0-9]+ de",
     "cfo-guardian/agent/tools/cfo-post.sh:402"),
    ("security·sed-de-M", r"^── 64bis-wiki · ([0-9]*) de ([0-9]*) para ti.*",
     "security-biklabs/tools/monitor-ledger-security.sh:120"),
    ("vigia-compartido·seccion", r"^── 64bis-wiki",
     "_shared_refs/tools/ledger-vigia.sh:187"),
    ("fe·seccion-sin-punto", r"── 64bis-wiki",
     "frontend-biklabs/agent/tools/watch-inbox-fe.sh:69"),
]

# El rótulo tal y como lo emite hoy inbox(), con y sin las partes opcionales.
ROTULOS = [
    "── 64bis-wiki · 2 de 116 para ti (lo más reciente · 114 más atrás) · escuchando equipo, FLOTA · carril: 64bis ──",
    "── 64bis-wiki · 2 de 2 para ti (lo más reciente) · carril: 64bis ──",
    "── 64bis-wiki · 5 de 5 para ti (lo más reciente) ──",          # ledger sin carril mapeado
]


def test_todos_los_patrones_de_la_flota_siguen_casando():
    """FALSADOR (el bug real, reproducido): con el rótulo intermedio
    `── 64bis-wiki (carril: 64bis) · …` — el que estuvo 45 min en producción —
    7 de estos 9 patrones dejan de casar. Aquí deben casar los 9, en las tres
    formas del rótulo.
    """
    for rotulo in ROTULOS:
        for etiqueta, patron, origen in PATRONES_DE_LA_FLOTA:
            assert re.search(patron, rotulo), (
                f"ROMPES A {etiqueta} ({origen}) con el rótulo: {rotulo}")


def test_el_rotulo_roto_de_verdad_rompia():
    """Control positivo del test de arriba: si el propio test no supiera
    detectar el formato malo, sería teatro. El rótulo con el carril EN MEDIO
    debe fallar en al menos 7 de los 9 patrones.
    """
    malo = "── 64bis-wiki (carril: 64bis) · 2 de 116 para ti (lo más reciente) ──"
    rotos = [e for e, p, _ in PATRONES_DE_LA_FLOTA if not re.search(p, malo)]
    assert len(rotos) >= 7, f"el control positivo no reproduce el daño: solo {rotos}"


def test_el_servicio_emite_un_rotulo_que_la_flota_parsea(cliente):
    """Y el mismo contrato contra la salida REAL del servicio, no contra una
    cadena escrita a mano en este fichero — el falsador de que los ROTULOS de
    arriba sigan pareciéndose a lo que inbox() emite de verdad.
    """
    texto = cliente.get("/inbox/backend").text
    linea = next(l for l in texto.splitlines() if l.startswith("── demo-ledger"))
    assert re.search(r"^── demo-ledger · [0-9]+ de [0-9]+ para ti", linea), linea
    assert "carril: demo" in linea      # ⑫ sigue vivo, pero fuera del prefijo contractual


# ── Lo que declara @cto (bikeus) que parsea, contestando a mi petición del
#    2026-08-11. Cada entrada aquí viene de SU mensaje, con su ruta de uso, no de
#    lo que yo suponga que consume. Si cambia su parse, se cambia aquí con él.
CONSUMIDORES_DECLARADOS = {
    # `llmi to` / `llmi q` → JSON de /entries. De 10 claves gatea 5.
    "cto·claves-json": ["line_no", "ts", "actor", "head", "to"],
}


def test_entries_conserva_las_claves_que_cto_gatea(cliente):
    """`line_no` es la crítica: la compara contra `grep -n` del fichero para
    verificar POR LÍNEA que su entrada entró en el índice. Si cambia de nombre
    o de base (0 vs 1), él creería que el canal le pierde entradas.

    FALSADOR: renombrar o quitar cualquiera de las 5 pone esto rojo.
    """
    filas = cliente.get("/entries?limit=1").json()
    assert filas, "sin datos no se puede afirmar el contrato"
    for k in CONSUMIDORES_DECLARADOS["cto·claves-json"]:
        assert k in filas[0], f"@cto gatea '{k}' en llmi to/q — ver su mensaje del 11-08"
    assert filas[0]["line_no"] >= 1, "line_no es 1-based: él lo cruza con `grep -n`"


def test_el_banner_ocupa_exactamente_la_linea_1(cliente):
    """@cto lee el rótulo con `sed -n '2p'`, o sea ASUME que el aviso de contenido
    ajeno ocupa la línea 1 y sólo una. Añadir una línea arriba lo rompe en
    silencio: leería una línea de datos como si fuera el rótulo.

    FALSADOR: meter una segunda línea de banner (o quitarlo) desplaza el rótulo
    y esta aserción cae.
    """
    lineas = cliente.get("/inbox/backend").text.splitlines()
    assert lineas[0].startswith("⚠️"), "la línea 1 debe ser el banner de contenido ajeno"
    assert lineas[1].startswith("── "), "la línea 2 debe ser el rótulo: @cto usa sed -n '2p'"


def test_adopcion_json_no_depende_de_columnas(cliente):
    """El parse que @cto declaró como su MÁS FRÁGIL —`grep -E "^   <nombre> "`,
    tres espacios y ancho fijo— ya se rompe hoy: un nombre de 28 caracteres en una
    columna de 22 desalinea la fila, su grep devuelve vacío y él lo lee como «ese
    agente no aparece». Un falso «no existe», silencioso, sobre adopción.

    `formato=json` le da un contrato que no depende de contar espacios.
    FALSADOR: si el JSON perdiera una clave o cambiara de forma, cae aquí.
    """
    d = cliente.get("/adopcion?formato=json").json()
    assert isinstance(d, list)
    if d:
        for k in ("agente", "lecturas", "ultima_lectura", "ledgers_consumidos"):
            assert k in d[0], f"falta '{k}' en el JSON de adopción"
    # y el texto sigue existiendo para quien ya lo usa
    assert "adopción ·" in cliente.get("/adopcion").text


def test_el_titular_no_se_pierde_en_el_recorte():
    """`head[:150]` cortaba por delante, y en este corpus la cabecera lleva PRIMERO
    la lista de destinatarios: con un reparto ancho, el titular —lo único que dice
    de qué va— caía fuera y el agente veía un remite sin asunto.

    Medido sobre el índice vivo: 6.950 de 57.309 vigentes (12%) tenían su titular
    más allá del carácter 150. Lo destapó mi propio fan-out: el manual llegó a
    todas las bandejas y la flota reportó que «no había llegado» — se cortaba en
    `— 📖 M`.

    FALSADOR: volver a `head[:150]` deja fuera el titular y esto se pone rojo.
    """
    import servicio
    head = ("### [wiki-vault·llminbox → FLOTA (los 60: 64bis ∧ PM ∧ cfocockpit ∧ bikeus "
            "∧ biklabs-landing ∧ inbiku) ∧ Operador · PRODUCED] 2026-08-11T14:55:46Z — "
            "📖 MANUAL DE llminbox: cuál es TU ledger y los 13 comandos")
    v = servicio.titular_visible(head)
    assert "MANUAL DE llminbox" in v, "el titular tiene que sobrevivir al recorte"
    assert v.startswith("### ["), "y el prefijo de cabecera se conserva"
    assert len(v) <= 160


def test_una_cabecera_corta_no_se_toca():
    """Control positivo: si el head ya cabe, sale TAL CUAL — sin puntos suspensivos
    ni reordenación. La cura no puede cambiar lo que ya se veía bien."""
    import servicio
    corta = "### [cto-A → backend · FYI] 2026-08-11T00:00:00Z — titular breve"
    assert servicio.titular_visible(corta) == corta


def test_llmi_resuelve_su_repo_a_traves_de_un_symlink(tmp_path):
    """`llmi` tiene que funcionar invocado por un SYMLINK en el PATH — que es la
    única forma de que el manual («llmi post …») sea cierto para la flota.

    `dirname $BASH_SOURCE` a secas devuelve dónde está el ENLACE, así que al poner
    `~/.local/bin/llmi -> ~/llminbox/llmi` el script buscaba `publicar.py`,
    `roster.json` y `.llmi-mounts.json` en `~/.local/bin`: `llmi post` moría con
    «can't open publicar.py» y `stat` anunciaba una deriva de montaje FALSA.
    Medido en el momento de crear el enlace (2026-08-12).

    Se prueba el EFECTO, no la variable: a través del enlace tiene que llegar a la
    validación de `publicar.py` (que rechazará por censo, y bien), en vez de morir
    porque no encuentra el fichero.

    FALSADOR: volver a `DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"` hace
    que la salida traiga «can't open ... publicar.py» y esto se pone rojo.
    """
    import os
    import subprocess
    enlace = tmp_path / "llmi"
    os.symlink(REPO_LLMI, enlace)
    r = subprocess.run([str(enlace), "post", "agente-que-no-existe-zz", "cto", "FYI", "t"],
                       input="cuerpo", capture_output=True, text=True, timeout=30,
                       env=dict(os.environ, BIK_CARRIL="llminbox"))
    junto = r.stdout + r.stderr
    assert "publicar.py" not in junto or "can't open" not in junto, (
        f"no encontró publicar.py a través del symlink: {junto[:200]}")
    assert "no está en el censo" in junto, (
        f"debería haber llegado a la validación de censo: {junto[:200]}")
