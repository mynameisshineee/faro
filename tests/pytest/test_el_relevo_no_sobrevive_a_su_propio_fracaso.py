"""Relevar un claim vencido y NO poder cogerlo dejaba el tema huérfano.

`coger()` cierra el claim vencido de otro con `motivo='relevo'` y después intenta insertar
el suyo bajo el coto del tope. Las dos sentencias van en la MISMA transacción, pero el
`commit()` era INCONDICIONAL: si el INSERT no dispara —porque quien releva ya está en su
tope— el relevo se consolida igual.

Resultado: el tema queda sin dueño. El anterior lo pierde de su lista sin cerrarlo él y sin
que nadie se lo lleve, y `/claim/{tema}` dice `puedes_cogerlo: true` sobre un trabajo que
alguien tenía. Nadie se entera: el que releva recibe un «no, estás en tu tope» y se va
creyendo que no pasó nada.

Es la clase de la sesión por su cara silenciosa: no es que falle ruidosamente, es que el
efecto colateral SOBREVIVE al fracaso de la operación que lo justificaba.

Lo encontró un barrido adversarial contra mi propio código, y lo verifiqué por mi mano
antes de curarlo.
"""
from __future__ import annotations
import os
import sqlite3
from datetime import datetime, timedelta, timezone


def test_si_el_INSERT_no_entra_el_relevo_se_deshace(cliente, servicio):
    s = servicio
    roles = {}
    for n in sorted(s.lp.CANON):
        roles.setdefault(s.lp.rol_de(n), n)
    quienes = list(roles.items())
    assert len(quienes) >= 2, "hacen falta dos roles distintos"
    (rol_a, nom_a), (rol_b, nom_b) = quienes[0], quienes[1]

    con = sqlite3.connect(os.environ["LLMINBOX_DB"])
    con.execute("DELETE FROM claims")
    # `rol_b` tiene su tope lleno
    ahora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for i in range(s.TOPE_EJECUTA):
        con.execute("INSERT INTO claims(tema,rol,agent,agent_bruto,abierto,bruto) "
                    "VALUES(?,?,?,?,?,?)",
                    (f"suyo-{i}", "ejecuta", rol_b, nom_b, ahora, f"suyo-{i}"))
    # y `rol_a` tiene un claim VENCIDO sobre el tema en disputa
    viejo = (datetime.now(timezone.utc)
             - timedelta(hours=s.CLAIM_TTL_H + 6)).isoformat(timespec="seconds")
    con.execute("INSERT INTO claims(tema,rol,agent,agent_bruto,abierto,bruto) "
                "VALUES(?,?,?,?,?,?)",
                ("disputado", "ejecuta", rol_a, nom_a, viejo, "disputado"))
    con.commit(); con.close()

    r = cliente.post("/claim", json={"tema": "disputado", "agent": nom_b}).json()
    assert r["ok"] is False, f"esperaba rechazo por tope, dio {r}"

    con = sqlite3.connect(os.environ["LLMINBOX_DB"])
    con.row_factory = sqlite3.Row
    f = con.execute("SELECT agent, cerrado, motivo FROM claims WHERE tema='disputado'"
                    ).fetchall()
    con.close()
    vivos = [x for x in f if x["cerrado"] is None]
    assert vivos, (
        "el claim de quien lo tenía quedó CERRADO como 'relevo' aunque el relevador no "
        "pudo cogerlo: el tema queda sin dueño y él lo pierde sin cerrarlo ni que nadie "
        f"se lo lleve. Filas: {[dict(x) for x in f]}")
    assert vivos[0]["agent"] == rol_a, "el claim vivo no es del dueño original"


def test_un_relevo_que_SI_entra_sigue_relevando(cliente, servicio):
    """⊕ obligatorio: la cura no puede impedir el relevo legítimo. Con hueco en el tope,
    relevar un vencido tiene que seguir funcionando — si no, se arregla el efecto
    colateral rompiendo la función."""
    s = servicio
    roles = {}
    for n in sorted(s.lp.CANON):
        roles.setdefault(s.lp.rol_de(n), n)
    (rol_a, nom_a), (rol_b, nom_b) = list(roles.items())[0], list(roles.items())[1]

    con = sqlite3.connect(os.environ["LLMINBOX_DB"])
    con.execute("DELETE FROM claims")
    viejo = (datetime.now(timezone.utc)
             - timedelta(hours=s.CLAIM_TTL_H + 6)).isoformat(timespec="seconds")
    con.execute("INSERT INTO claims(tema,rol,agent,agent_bruto,abierto,bruto) "
                "VALUES(?,?,?,?,?,?)",
                ("disputado", "ejecuta", rol_a, nom_a, viejo, "disputado"))
    con.commit(); con.close()

    r = cliente.post("/claim", json={"tema": "disputado", "agent": nom_b}).json()
    assert r["ok"] is True, f"el relevo legítimo dejó de funcionar: {r}"
    assert r.get("relevaste_a") == rol_a, r
