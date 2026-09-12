"""Falsadores del crash que dejó sellos verdes con `recipients` vacío.

La propiedad no es «el segundo arranque suele reindexar»: es que la intención
sobrevive al proceso, `files` no puede saltársela y cada ledger cambia de snapshot
en una sola transacción sin mover los cursores.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time

from .conftest import construir


ROSTER_VIEJO = {
    "agentes": [
        {"nombre": "backend", "humano": "operador", "clave": "", "rol": "be"},
        {"nombre": "cto-A", "humano": "operador", "clave": "", "rol": "cto"},
    ],
    "humanos": [{"nombre": "operador", "alias": ["Operador"]}],
    "difusion": ["FLOTA"],
}
ROSTER_NUEVO = {
    **ROSTER_VIEJO,
    "agentes": ROSTER_VIEJO["agentes"] + [
        {"nombre": "new-agent", "humano": "operador", "clave": "", "rol": "new"},
    ],
}


def _ledgers(tmp_path):
    demo = tmp_path / "DEMO-LEDGER.md"
    otro = tmp_path / "OTRO-LEDGER.md"
    demo.write_text(
        "### [new-agent → new-agent · REQUEST] actor y directo antes desconocidos\nuno\n"
        "### [cto-A → FLOTA · FYI] difusion demo\ndos\n"
    )
    otro.write_text(
        "### [cto-A → backend · REQUEST] directo conocido\ntres\n"
        "### [cto-A → FLOTA · FYI] difusion otro\ncuatro\n"
    )
    return demo, otro


def _recargar(monkeypatch):
    for mod in ("servicio", "ledger_parse"):
        sys.modules.pop(mod, None)
    import servicio as s

    async def _vigilante_noop():
        return

    monkeypatch.setattr(s, "vigilante", _vigilante_noop)
    return s


def _foto(db_path):
    con = sqlite3.connect(db_path)
    try:
        recipients = con.execute(
            "SELECT ledger,eid,who FROM recipients ORDER BY ledger,eid,who"
        ).fetchall()
        sellos = dict(con.execute(
            "SELECT k,v FROM meta WHERE k IN ('roster_v','parser_v')"
        ).fetchall())
        pending = con.execute(
            "SELECT ledger,roster_v,parser_v FROM rederive_pending ORDER BY ledger"
        ).fetchall()
        cursores = con.execute(
            "SELECT agent,ledger,last_arrival,updated FROM cursors ORDER BY agent,ledger"
        ).fetchall()
        return recipients, sellos, pending, cursores
    finally:
        con.close()


def _preparar(s):
    con = s.db()
    try:
        s._preparar_indice(con)
    finally:
        con.close()


def test_rederivacion_sobrevive_crash_dos_reinicios_y_files_iguales(
        tmp_path, monkeypatch):
    """Dos ledgers, directo + FLOTA, y un SIGKILL lógico entre intención y trabajo.

    Mata a los dos mutantes caros:
      * adelantar el sello: tras el crash tiene que seguir en la revisión vieja;
      * borrar pending antes de acreditar el ledger: tras un fallo sigue en 2.
    """
    s0 = construir(tmp_path, monkeypatch, roster=ROSTER_VIEJO)
    demo, otro = _ledgers(tmp_path)
    _preparar(s0)
    s0.barrido()

    con = sqlite3.connect(s0.DB)
    con.executemany(
        "INSERT OR REPLACE INTO cursors(agent,ledger,last_arrival,updated) "
        "VALUES ('backend',?,?, 'antes')",
        [(ledger, 0) for ledger in ("demo-ledger", "otro-ledger")],
    )
    con.commit()
    con.close()
    inicial = _foto(s0.DB)
    n = len(inicial[0])
    assert n >= 3                         # directo conocido + las dos difusiones
    assert inicial[2] == []

    # Cambia el censo sin tocar los ledgers ni su firma registrada. El proceso cae
    # con os._exit justo después del commit de la intención y antes del primer parse.
    (tmp_path / "roster.json").write_text(json.dumps(ROSTER_NUEVO))
    crash = subprocess.run(
        [sys.executable, "-c",
         "import os, servicio; c=servicio.db(); "
         "servicio._preparar_indice(c); os._exit(91)"],
        cwd=os.path.dirname(s0.__file__), env=os.environ.copy(), check=False,
    )
    assert crash.returncode == 91
    tras_crash = _foto(s0.DB)
    assert len(tras_crash[2]) == 2
    assert tras_crash[0] == inicial[0], "la intención no puede vaciar recipients"
    assert tras_crash[1] == inicial[1], "el sello no acredita trabajo todavía inexistente"
    assert tras_crash[3] == inicial[3]

    # Reinicio 1: aunque files coincide, el pending obliga a entrar. Simulamos un
    # parser roto: barrido hace rollback y las dos intenciones siguen durables.
    s1 = _recargar(monkeypatch)
    _preparar(s1)
    original_parse = s1.lp.parse

    def _parser_roto(_path):
        raise RuntimeError("crash antes de reconstruir")

    monkeypatch.setattr(s1.lp, "parse", _parser_roto)
    s1.barrido()
    tras_fallo = _foto(s1.DB)
    assert len(tras_fallo[2]) == 2, "un intento fallido no consume pending"
    assert tras_fallo[0] == inicial[0]
    assert tras_fallo[1] == inicial[1]
    assert tras_fallo[3] == inicial[3]
    monkeypatch.setattr(s1.lp, "parse", original_parse)

    # Reinicio 2: termina sólo uno. El lote no se reinicia, el sello no se adelanta
    # y el cursor no se mueve. El nuevo destinatario hace M>N de forma observable.
    s2 = _recargar(monkeypatch)
    _preparar(s2)
    con = s2.db()
    s2.reindex("demo-ledger", str(demo), con)
    con.close()
    parcial = _foto(s2.DB)
    assert [r[0] for r in parcial[2]] == ["otro-ledger"]
    assert parcial[1] == inicial[1]
    assert parcial[3] == inicial[3]
    assert len(parcial[0]) > n

    # Otro arranque no vuelve a encolar el ledger ya acreditado. `otro-ledger`
    # conserva la MISMA firma de files: sólo la fila durable hace que barrido lo lea.
    s3 = _recargar(monkeypatch)
    _preparar(s3)
    assert [r[0] for r in _foto(s3.DB)[2]] == ["otro-ledger"]
    s3.barrido()
    final = _foto(s3.DB)
    assert final[2] == []
    assert len(final[0]) > n
    assert final[1] == {"parser_v": str(s3.lp.PARSER_V),
                        "roster_v": s3.huella_censo()}
    assert final[3] == inicial[3]
    assert any(r[2] == "new-agent" for r in final[0])
    assert sum(r[2] == "FLOTA" for r in final[0]) == 2
    con = sqlite3.connect(s3.DB)
    actor = con.execute(
        "SELECT actor FROM entries WHERE head LIKE '%actor y directo antes desconocidos%'"
    ).fetchone()[0]
    con.close()
    assert actor == "new-agent", "actor no se actualizó en el commit de su ledger"


def test_cero_ledgers_preserva_snapshot_y_sello_y_degrada_health(tmp_path, monkeypatch):
    s0 = construir(tmp_path, monkeypatch, roster=ROSTER_VIEJO)
    _ledgers(tmp_path)
    _preparar(s0)
    s0.barrido()
    antes = _foto(s0.DB)
    assert antes[0] and antes[1] and antes[2] == []

    (tmp_path / "roster.json").write_text(json.dumps(ROSTER_NUEVO))
    monkeypatch.setenv("LLMINBOX_LEDGERS", "")
    s1 = _recargar(monkeypatch)
    _preparar(s1)
    despues = _foto(s1.DB)

    assert despues[0] == antes[0]
    assert despues[1] == antes[1]
    assert despues[2] == []
    salud = s1.health()
    assert salud["ok"] is False
    assert salud["ledgers"] == 0
    assert any("CERO ledgers" in aviso for aviso in salud["avisos"])


def test_migracion_cura_sello_verde_legacy_con_recipients_vacio(tmp_path, monkeypatch):
    """Una base ya envenenada por la versión anterior no puede quedar fosilizada.

    `roster_v` y `parser_v` coinciden a propósito; la señal de migración es que la
    tabla durable aún no existía. `files` también coincide, así que sin pending el
    barrido saltaría ambos ledgers y el vacío sería permanente.
    """
    s0 = construir(tmp_path, monkeypatch, roster=ROSTER_VIEJO)
    _ledgers(tmp_path)
    _preparar(s0)
    s0.barrido()
    con = sqlite3.connect(s0.DB)
    con.execute(
        "INSERT OR REPLACE INTO cursors(agent,ledger,last_arrival,updated) "
        "VALUES ('backend','demo-ledger',0,'antes')"
    )
    con.execute("DELETE FROM recipients")
    con.execute("DROP TABLE rederive_pending")
    con.commit()
    sellos = dict(con.execute(
        "SELECT k,v FROM meta WHERE k IN ('roster_v','parser_v')"
    ).fetchall())
    files = con.execute("SELECT ledger,bytes,mtime FROM files ORDER BY ledger").fetchall()
    cursor = con.execute("SELECT * FROM cursors").fetchall()
    con.close()

    s1 = _recargar(monkeypatch)
    _preparar(s1)
    en_migracion = _foto(s1.DB)
    assert len(en_migracion[2]) == 2
    assert en_migracion[1] == sellos, "programar la cura no adelanta ni reescribe sellos"
    assert en_migracion[3] == cursor
    con = sqlite3.connect(s1.DB)
    assert con.execute("SELECT ledger,bytes,mtime FROM files ORDER BY ledger").fetchall() == files
    con.close()

    s1.barrido()
    curada = _foto(s1.DB)
    assert curada[2] == []
    assert curada[0], "el sello verde legacy con recipients vacío quedó fosilizado"
    assert curada[1] == sellos
    assert curada[3] == cursor


def test_health_cierra_readiness_mientras_hay_rederivacion(tmp_path, monkeypatch):
    """No basta con avisar: el snapshot legacy preservado puede ser el envenenado.

    El watcher está configurado y el reloj del barrido se fuerza fresco para que el
    único motivo del rojo sea `rederive_pending`; sin ese control el test pasaría por
    inercia con cualquier implementación.
    """
    s0 = construir(tmp_path, monkeypatch, roster=ROSTER_VIEJO,
                   extra_env={"LLMINBOX_WATCHER_TOKEN": "w"})
    _ledgers(tmp_path)
    _preparar(s0)
    s0.barrido()
    (tmp_path / "roster.json").write_text(json.dumps(ROSTER_NUEVO))

    s1 = _recargar(monkeypatch)
    _preparar(s1)
    s1.SALUD["ultimo_ok"] = time.time()
    salud = s1.health()

    assert salud["vigilancia"]["estado"] == "sin-armar"
    assert salud["vigilancia"]["estado"] in s1.VIGILANCIA_SANOS
    # Subconjunto y no igualdad exacta: `rederivacion` creció con `objetivos` y
    # `revision` (defensa en profundidad), y una igualdad de dict entero convierte
    # cualquier campo aditivo en un fallo. Lo que este test afirma sigue intacto.
    assert salud["rederivacion"]["pendientes"] == 2
    assert salud["rederivacion"]["completa"] is False
    assert salud["ok"] is False
    assert any("readiness permanece cerrada" in a for a in salud["avisos"])
