"""Un `@nombre` fuera del censo se ignoraba en silencio: el mensaje no llega a nadie.

El parser descarta las menciones que no están en el censo, y con razón —sin eso, un
`@media` de CSS entraría como destinatario—. Lo que faltaba es DECIRLO: quien escribe
`@wikivault` cree haber dirigido su entrada, el destinatario nunca la ve, y nadie se entera
de ninguno de los dos lados.

MEDIDO en producción el 2026-09-02, sobre las entradas desde el corte de arrobas:

    menciones @ resueltas ......................... 295.114
    menciones @ ignoradas (bruto) ..................  29.399
    de ésas, que se PARECEN a alguien del censo ....     436  en 41 handles

El bruto NO es el defecto y decirlo importa: `@me` (16.103) y `@anthropic` (6.340) son
correos y remotos de git, no destinatarios. Es mi propia lección —un censo literal mide la
redacción, no el efecto—, así que el número que cuenta es el filtrado: los que se parecen a
un nombre real. `@wikivault` → `@wiki-vault`, `@ct` → `@cto`, `@contratos` → `@contratosbik`.

LO QUE NO SE HACE ES ADIVINAR. Resolver `@ct` como `@cto` entregaría a alguien que no fue
nombrado, y un destinatario inventado es peor que uno perdido: el primero actúa. Se
publica el hallazgo con su candidato como PISTA, y decide quien escribió.
"""
from __future__ import annotations
import os
import sqlite3
from datetime import datetime, timedelta, timezone


def _planta(cliente, head, body, actor="alguien", eid="a1"):
    con = sqlite3.connect(os.environ["LLMINBOX_DB"])
    con.execute(
        "INSERT OR REPLACE INTO entries (ledger,eid,arrival,seq,line_no,byte_off,ts,"
        "actor,tipo,head,body,visto,ausente,provisional,raw_tipo) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        # ts RELATIVO (cura de sdet, adjudicada por @qa 2026-09-11T19:06Z). Un `ts` FIJO
        # caduca contra la ventana de 7 días de /doctor —conducta legítima del producto—:
        # este fichero se puso rojo solo el 2026-09-08, siete días después del 09-01.
        ("l", eid, 600, 0, 0, 0,
         (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S"),
         actor, "FYI", head, body,
         None, None, 0, "FYI"))
    con.commit(); con.close()


def test_un_arroba_que_no_resuelve_sale_en_el_informe(cliente, servicio):
    real = sorted(servicio.lp.CANON)[0]
    malo = real[:-1] + "x" if len(real) > 3 else real + "x"
    _planta(cliente, f"### [x → y · FYI] hola", f"aviso a @{malo} sobre esto")
    txt = cliente.get("/doctor").text
    assert "NOMBRA Y NO LLEGA" in txt, "el informe no tiene la sección de arrobas perdidos"
    assert malo in txt, f"@{malo} no aparece: se sigue ignorando en silencio"


def test_se_dice_QUIEN_lo_escribio_no_solo_cuantos(cliente, servicio):
    """«436 menciones perdidas» no le dice a nadie qué cambiar. «Tú, 110 veces» sí."""
    real = sorted(servicio.lp.CANON)[0]
    malo = real[:-1] + "x" if len(real) > 3 else real + "x"
    _planta(cliente, "### [x → y · FYI] hola", f"@{malo}", actor="quien-lo-escribe")
    txt = cliente.get("/doctor").text
    i = txt.index("NOMBRA Y NO LLEGA")
    assert "quien-lo-escribe" in txt[i:i + 1500], "no dice quién lo escribió"


def test_la_pista_es_PISTA_y_no_se_entrega_sola(cliente, servicio):
    """⊕ anti-adivinanza: el informe puede sugerir el candidato, pero la entrada NO puede
    acabar entregada a él. Un destinatario inventado es peor que uno perdido: actúa."""
    real = sorted(servicio.lp.CANON)[0]
    malo = real[:-1] + "x" if len(real) > 3 else real + "x"
    _planta(cliente, "### [x → y · FYI] hola", f"@{malo}", eid="pista1")
    con = sqlite3.connect(os.environ["LLMINBOX_DB"])
    n = con.execute("SELECT COUNT(*) FROM recipients WHERE eid='pista1'").fetchone()[0]
    con.close()
    assert n == 0, (
        f"la entrada se entregó a {n} destinatario(s) que nadie nombró: el informe está "
        f"adivinando en vez de sugerir")


def test_un_correo_no_cuenta_como_destinatario_perdido(cliente, servicio):
    """⊖ el que evita el ruido: `@me` y `@anthropic` son correos y remotos, no personas.
    Si entraran, la sección sería 29.399 líneas y nadie la miraría — un informe que grita
    todo el rato no informa de nada."""
    _real = sorted(servicio.lp.CANON)[0]
    _ancla = _real[:-1] + "y" if len(_real) > 3 else _real + "y"
    # ANCLA: una mención perdida de VERDAD, para que la sección se emita aunque el caso
    # de abajo sea (correctamente) filtrado. Sin ella, exigir la sección daría rojo con
    # el producto SANO; con el `if` viejo, el ⊖ pasaba con el informe vacío.
    _planta(cliente, "### [x → y · FYI] ancla", f"aviso a @{_ancla} sobre esto", eid="ancla-c1")
    _planta(cliente, "### [x → y · FYI] hola", "escribe a soporte@anthropic.com", eid="c1")
    txt = cliente.get("/doctor").text
    assert "NOMBRA Y NO LLEGA" in txt, (
        "la sección no se emite y el ⊖ de abajo pasaría con el informe VACÍO — el ancla de\n"
        "arriba la obliga a existir con el producto sano")
    if True:
        i = txt.index("NOMBRA Y NO LLEGA")
        assert "anthropic" not in txt[i:i + 1500], (
            "un dominio de correo entra como destinatario perdido: la sección se llena de "
            "ruido y deja de leerse")


def test_un_handle_que_no_se_parece_a_NADIE_no_entra(cliente, servicio):
    """⊖ del filtro de parecido — lo cazó un mutante que sobrevivió, no yo.

    Mi test del correo pasaba igual con el filtro quitado, porque a `soporte@anthropic` lo
    frena la OTRA guarda (la arroba viene pegada a texto). Así que el filtro de parecido no
    tenía quien lo probara, y quitarlo habría metido 29.399 líneas de ruido en el informe
    sin que ningún test protestara.

    `@zzzqqq` va precedido de espacio —pasa la primera guarda— y no se parece a nadie. Si
    aparece, la sección se vuelve ilegible y deja de leerse, que es la forma de matar un
    aviso sin borrarlo.
    """
    _real = sorted(servicio.lp.CANON)[0]
    _ancla = _real[:-1] + "y" if len(_real) > 3 else _real + "y"
    # ANCLA: una mención perdida de VERDAD, para que la sección se emita aunque el caso
    # de abajo sea (correctamente) filtrado. Sin ella, exigir la sección daría rojo con
    # el producto SANO; con el `if` viejo, el ⊖ pasaba con el informe vacío.
    _planta(cliente, "### [x → y · FYI] ancla", f"aviso a @{_ancla} sobre esto", eid="ancla-ruido1")
    _planta(cliente, "### [x → y · FYI] hola", "ping a @zzzqqq y ya", eid="ruido1")
    txt = cliente.get("/doctor").text
    assert "NOMBRA Y NO LLEGA" in txt, (
        "la sección no se emite y el ⊖ de abajo pasaría con el informe VACÍO — el ancla de\n"
        "arriba la obliga a existir con el producto sano")
    if True:
        i = txt.index("NOMBRA Y NO LLEGA")
        assert "zzzqqq" not in txt[i:i + 1500], (
            "un handle que no se parece a nadie entra en el informe: sin el filtro de "
            "parecido la sección son 29.399 líneas y nadie la mira")


def test_un_correo_a_un_dominio_QUE_SI_SE_PARECE_tampoco_entra(cliente, servicio):
    """⊖ que separa las dos guardas — lo cazó otro mutante superviviente.

    Mis dos casos anteriores los frenaban las DOS guardas a la vez (el parecido y la
    arroba-pegada-a-texto), así que quitar cualquiera de ellas seguía dando verde: cada una
    tapaba el hueco de la otra. Dos guardas redundantes en los tests son una guarda sin
    probar, y no se sabe cuál.

    Este caso sólo lo frena UNA: un correo cuyo dominio ES un nombre del censo. `firma@cto`
    pasa el filtro de parecido de calle —es idéntico— y sólo lo detiene que la arroba venga
    pegada a texto. Sin esa guarda, cada firma de correo del corpus se leería como un
    destinatario perdido.
    """
    real = sorted(servicio.lp.CANON)[0]
    # PARECIDO, no idéntico: un dominio que fuera EXACTAMENTE un nombre del censo resuelve
    # y sale por la primera puerta, sin llegar nunca a las guardas — mi primer intento se
    # estrelló ahí y el mutante siguió vivo. El caso que discrimina es el que llega.
    dom = real[:-1] + "x" if len(real) > 3 else real + "x"
    _real = sorted(servicio.lp.CANON)[0]
    _ancla = _real[:-1] + "y" if len(_real) > 3 else _real + "y"
    # ANCLA: una mención perdida de VERDAD, para que la sección se emita aunque el caso
    # de abajo sea (correctamente) filtrado. Sin ella, exigir la sección daría rojo con
    # el producto SANO; con el `if` viejo, el ⊖ pasaba con el informe vacío.
    _planta(cliente, "### [x → y · FYI] ancla", f"aviso a @{_ancla} sobre esto", eid="ancla-correo2")
    _planta(cliente, "### [x → y · FYI] hola", f"escríbeme a firma@{dom}.example.com",
            eid="correo2")
    txt = cliente.get("/doctor").text
    assert "NOMBRA Y NO LLEGA" in txt, (
        "la sección no se emite y el ⊖ de abajo pasaría con el informe VACÍO — el ancla de\n"
        "arriba la obliga a existir con el producto sano")
    if True:
        i = txt.index("NOMBRA Y NO LLEGA")
        assert dom not in txt[i:i + 1500], (
            "un correo a un dominio que coincide con un nombre del censo entra como "
            "destinatario perdido: sin la guarda de la arroba pegada, cada firma cuenta")
