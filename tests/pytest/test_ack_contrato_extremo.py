"""El extremo VÁLIDO del contrato ACK pasa, y el inválido no se emite ni se persiste.

Adjudicación del relevo (MARK:astra-ack-contrato-completo-cursor-inicial-unicode-
emision, 2026-09-08): el tope `4096` del CLI RECHAZABA ya contratos legítimos; se
alinea UNA cota (`ACK_CONTRATO_MAX_CHARS=32768`) en productor y ambos caminos CLI,
se conservan las longitudes de contrato (grant 128 · principal 512 · role 128 ·
lane 128 · ledger 256 · 200 llegadas) y el dominio INTEGER de SQLite para los
contadores, y el envelope se valida COMPLETO antes del INSERT.

Lo que este fichero falsa, pieza por pieza:

1. `cursor_before=-1` (bandeja sin cursor previo, `last=-1`) sigue siendo un ACK
   legítimo — un `ge=0` aquí rompería el PRIMER ack de cualquier lector nuevo.
2. 200 llegadas al máximo numérico legítimo (las 200 mayores del dominio, únicas)
   validan y no rebasan la cota compartida.
3. El estrés Unicode con controles JSON-escapados y texto astral crudo cabe en
   la cota; es una muestra de extremos, no una prueba exhaustiva de Unicode.
4. Un contrato que excede la cota NO se emite Y NO se persiste: el chequeo corre
   ANTES del INSERT, así que un fallo de longitud deja cero filas.
5. Los tres sitios de la cota (servicio, `llmi ack`, `llmi inbox`) siguen el mismo
   número — el modo de fallo real es que una mano cambie uno y no los otros.
6. Las RELACIONES del envelope son contrato, no detalle: ordenada sin duplicados,
   `watermark == max(arrivals)` y todos los arrivals > cursor_before — exactamente
   lo que los dos lectores CLI ya exigen (llmi:552-558); y el extremo completo
   atraviesa el CLI real (inbox → comando → ack → recibo), con degradación
   `ACK_GRANT_RECHAZADO` aceptada por la allowlist del CLI.

Los tests se EJECUTARÁN sólo con capacidad admisible (gate de `@cfo-guardian`):
escritos y compilados bajo NO-GO, como el resto del lote.
"""
from __future__ import annotations

import hashlib
import http.server
import json
import re
import sqlite3
import threading
from pathlib import Path

import pytest
from pydantic import ValidationError

from .conftest import construir

CRED = "cred-be-contrato-000000000"
MAXI = 2**63 - 1
RAIZ = Path(__file__).resolve().parents[2]


def _v8(tmp_path, monkeypatch):
    mapa = tmp_path / "ack-credentials.json"
    mapa.write_text(json.dumps({
        CRED: {"rol": "be", "carril": "demo", "principal_id": "workload-be"},
    }))
    s = construir(tmp_path, monkeypatch, extra_env={
        "LLMINBOX_CREDENCIALES": str(mapa),
        "LLMINBOX_CREDENCIALES_SHA": hashlib.sha256(mapa.read_bytes()).hexdigest(),
    })
    return s


def _extremo_valido(*, cursor_before: int, principal: str, role: str):
    """Las 200 llegadas MAYORES del dominio (únicas, 19 dígitos) y los campos a
    su longitud de contrato máxima. La validez de cada combinación se comprueba
    con `AckGrantV1`; no se afirma un máximo universal de bytes Unicode."""
    llegadas = [MAXI - 199 + i for i in range(200)]
    return {
        "v": 1,
        "grant": "g" * 128,
        "principal": principal,
        "role": role,
        "lane": "l" * 128,
        "ledger": "l" * 256,
        "cursor_generation": MAXI,
        "cursor_before": cursor_before,
        "allowed_arrivals": llegadas,
        "watermark": max(llegadas),
        "expires_at": 4_102_444_800,
    }


def test_primer_ack_sin_cursor_previo_es_legitimo(tmp_path, monkeypatch):
    """El sentinel -1 sobrevive al contrato: un lector nuevo ACKea."""
    s = _v8(tmp_path, monkeypatch)
    grant = _extremo_valido(cursor_before=-1, principal="p" * 512, role="r" * 128)
    model = s.AckGrantV1(**grant)          # no revienta: -1 es dominio legal
    assert model.cursor_before == -1
    assert model.allowed_arrivals[0] == MAXI - 199


def test_extremo_200_llegadas_cabe_en_la_cota(tmp_path, monkeypatch):
    s = _v8(tmp_path, monkeypatch)
    grant = _extremo_valido(cursor_before=MAXI - 201, principal="p" * 512,
                            role="r" * 128)
    s.AckGrantV1(**grant)
    contrato = s._contrato_b64url(grant)
    # Piso anti-vacuo (qa A1, MARK:qa-ack-266941a-extremos-seis-ejes-go-dos-aditivos-una-linea):
    # backend midió 7160 para este extremo; un `_contrato_b64url` degenerado (p.ej. que
    # devolviera "") pasaría la cota de arriba a secas. El test de cota no depende de
    # otro test para no ser teatro.
    assert len(contrato) >= 7_000
    assert len(contrato) <= s.ACK_CONTRATO_MAX_CHARS


def test_estres_unicode_cabe_en_la_cota(tmp_path, monkeypatch):
    """Dos expansiones de la serialización: `\x00` se escapa a 6 bytes
    (`\\u0000`) y el astral va crudo a 4 (`ensure_ascii=False`). Si la cota se
    midiera sobre caracteres y no sobre el contrato codificado, este test la
    falsaría."""
    s = _v8(tmp_path, monkeypatch)
    grant = _extremo_valido(cursor_before=-1, principal="\x00" * 512,
                            role="\U0001f600" * 128)
    crudo = json.dumps(grant, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False).encode()
    assert len(crudo) > 5_000              # el estrés SÍ engorda: el test no es vacuo
    s.AckGrantV1(**grant)
    assert len(s._contrato_b64url(grant)) <= s.ACK_CONTRATO_MAX_CHARS


def test_dominio_sqlite_de_los_contadores(tmp_path, monkeypatch):
    s = _v8(tmp_path, monkeypatch)
    grant = _extremo_valido(cursor_before=-1, principal="p", role="r")
    for campo, valor in (("cursor_before", -2), ("cursor_before", MAXI + 1),
                         ("cursor_generation", -1), ("watermark", -1),
                         ("allowed_arrivals", None)):
        roto = dict(grant)
        if campo == "allowed_arrivals":
            roto[campo] = grant[campo][:-1] + [MAXI + 1]
        else:
            roto[campo] = valor
        try:
            s.AckGrantV1(**roto)
        except ValidationError:
            pass
        else:
            raise AssertionError(f"{campo}={roto[campo]!r} fuera de dominio y el "
                                 "modelo lo admitió")


def _con_en_memoria():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript("""
    CREATE TABLE cursor_generations_v2 (
      role TEXT NOT NULL, carril TEXT NOT NULL, ledger TEXT NOT NULL,
      generation INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY (role, carril, ledger));
    CREATE TABLE ack_grants (
      nonce_hash TEXT PRIMARY KEY,
      grant_v INTEGER NOT NULL CHECK(grant_v = 1),
      principal TEXT NOT NULL, role TEXT NOT NULL, lane TEXT NOT NULL, ledger TEXT NOT NULL,
      cursor_generation INTEGER NOT NULL CHECK(cursor_generation >= 0),
      cursor_before INTEGER NOT NULL,
      allowed_arrivals TEXT NOT NULL,
      watermark INTEGER NOT NULL CHECK(watermark >= 0 AND watermark > cursor_before),
      issued_at TEXT NOT NULL, expires_at REAL NOT NULL CHECK(expires_at > 0),
      used_arrival INTEGER, used_at TEXT, receipt_id TEXT UNIQUE, receipt_json TEXT,
      CHECK((used_arrival IS NULL AND used_at IS NULL AND receipt_id IS NULL AND receipt_json IS NULL)
         OR (used_arrival IS NOT NULL AND used_at IS NOT NULL AND receipt_id IS NOT NULL
             AND receipt_json IS NOT NULL)));
    """)
    con.commit()
    return con


def test_emision_fail_closed_antes_del_insert(tmp_path, monkeypatch):
    """Un contrato que el CLI rechazaría por longitud NO se emite y NO se
    persiste: cero filas tras el ValueError. Falsaría un chequeo colocado
    después del INSERT."""
    s = _v8(tmp_path, monkeypatch)
    con = _con_en_memoria()
    con.execute("INSERT INTO cursor_generations_v2(role,carril,ledger,generation) "
                "VALUES('r','l','g',3)")
    con.commit()
    # Esta entrada rebasa presupuesto y modelo; al comprobar presupuesto primero
    # debe caer en OVERFLOW. La otra prueba acredita INVALIDO por separado.
    monkeypatch.setattr(s, "_principal_ack", lambda cred: "x" * 40_000)
    with pytest.raises(ValueError, match="ACK_CONTRATO_OVERFLOW"):
        s._emite_ack_grant(con, credencial=CRED, role="r", lane="l", ledger="g",
                           cursor_before=-1, arrivals=[1])
    filas = con.execute("SELECT COUNT(*) n FROM ack_grants").fetchone()["n"]
    assert filas == 0                      # fail-closed: nada persistido

    # El extremo VÁLIDO sí emite y persiste exactamente una fila.
    monkeypatch.setattr(s, "_principal_ack", lambda cred: "p" * 512)
    payload = s._emite_ack_grant(con, credencial=CRED, role="r" * 128, lane="l" * 128,
                                 ledger="l" * 256, cursor_before=-1,
                                 arrivals=[MAXI - 199 + i for i in range(200)])
    assert len(s._contrato_b64url(payload)) <= s.ACK_CONTRATO_MAX_CHARS
    filas = con.execute("SELECT COUNT(*) n FROM ack_grants").fetchone()["n"]
    assert filas == 1


def test_relaciones_del_envelope(tmp_path, monkeypatch):
    """Orden, unicidad y watermark/cursor son parte del contrato, no sólo los
    bytes: la cota admite un envelope desordenado y el modelo NO. FALSADOR:
    las expectativas distinguen la validación de relaciones del presupuesto."""
    s = _v8(tmp_path, monkeypatch)
    base = _extremo_valido(cursor_before=MAXI - 201, principal="p", role="r")
    rotos = (
        {**base, "allowed_arrivals": list(reversed(base["allowed_arrivals"]))},
        {**base, "allowed_arrivals": base["allowed_arrivals"][:-1] + [MAXI - 1]},
        {**base, "watermark": MAXI - 201},       # == cursor_before: CHECK exige estricto
        {**base, "watermark": MAXI - 1},         # != max(allowed_arrivals): pie mentiroso
        {**base, "cursor_before": MAXI},         # un arrival quedaría <= cursor_before
    )
    for roto in rotos:
        with pytest.raises(ValidationError):
            s.AckGrantV1(**roto)


def test_emision_rechaza_envelope_que_el_modelo_no_admite(tmp_path, monkeypatch):
    """`watermark == cursor_before` no es emitible: el modelo lo rechaza ANTES del
    INSERT. FALSADOR: quitar la validación del modelo en `_emite_ack_grant` no
    lanza ValueError — el INSERT revienta con IntegrityError (otra clase) y el
    test lo ve; con la validación, cero filas."""
    s = _v8(tmp_path, monkeypatch)
    con = _con_en_memoria()
    with pytest.raises(ValueError, match="ACK_CONTRATO_INVALIDO"):
        s._emite_ack_grant(con, credencial=CRED, role="r", lane="l", ledger="g",
                           cursor_before=5, arrivals=[5])
    filas = con.execute("SELECT COUNT(*) n FROM ack_grants").fetchone()["n"]
    assert filas == 0


def test_recorrido_funcional_cli_sobre_el_extremo_valido(tmp_path, monkeypatch):
    """El extremo válido COMPLETO atraviesa el CLI REAL. El CLI exige coherencia
    TOTAL entre lo visible y el sobre: `allowed_arrivals == filas mostradas`,
    `hasta == max(rows)`, `watermark == max(arrivals)`, `arrivals > cursor_before`
    (llmi:513-558) — así que el listing trae las 200 filas, con grant en las
    longitudes máximas legítimas (principal Unicode 512 · role 128 · lane 128 ·
    ledger 256) y texto Unicode en las filas. `llmi inbox` valida, imprime el
    comando; `llmi ack` lo revalida contra un stub que ECOA el propio grant y el
    recibo se verifica con la fórmula contractual. FALSADOR: un `32768`
    desalineado o una relación rota muere aquí en el subprocess, no en un assert
    de literales."""
    from .test_cli import _Stub, _llmi

    s = _v8(tmp_path, monkeypatch)
    con = _con_en_memoria()
    principal = "ñ" * 512                       # Unicode legítimo a su longitud máxima
    monkeypatch.setattr(s, "_principal_ack", lambda cred: principal)
    lane, ledger = "l" * 128, "L" * 256
    llegadas = [MAXI - 199 + i for i in range(200)]
    payload = s._emite_ack_grant(
        con, credencial=CRED, role="r" * 128, lane=lane, ledger=ledger,
        cursor_before=-1, arrivals=llegadas)
    sobre = {"hasta": {ledger: MAXI}, "ack": payload}
    linea = json.dumps(sobre, separators=(",", ":"), ensure_ascii=False)
    filas = "\n".join(
        f"  abc{i:03d} #{a} L10 2026-08-10T12:00:00 cto-A [REQUEST] ñ-tildé-🐬"
        for i, a in enumerate(llegadas))
    cuerpo = (
        f"── {ledger} · 200 de 200 para ti ──\n"
        f"{filas}\n"
        "\n\nconfirmación disponible — el CLI valida este sobre:\n"
        "  POST /inbox/extremo/ack\n"
        f"  {linea}\n"
    ).encode()

    class _StubExtremo(_Stub):
        def do_GET(self):
            if self.path == "/carriles":
                return self._json(200, {lane: ledger})
            if self.path.startswith("/inbox/extremo"):
                self.send_response(200)
                self.send_header("Content-Length", str(len(cuerpo)))
                self.end_headers()
                self.wfile.write(cuerpo)
                return
            return super().do_GET()

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            pedido = json.loads(self.rfile.read(n).decode())
            self.server.posts.append({"path": self.path, "headers": dict(self.headers),
                                      "body": json.dumps(pedido)})
            g = pedido["grant"]                 # el eco es del PROPIO grant, como el server
            nonce = hashlib.sha256(g["grant"].encode("ascii")).hexdigest()
            recibo = "ack_" + hashlib.sha256(
                f"ack-receipt-v1\0{nonce}\0{pedido['arrival']}".encode()).hexdigest()[:32]
            self._json(200, {"ok": True, "receipt_id": recibo, "grant_v": g["v"],
                             "principal": g["principal"], "role": g["role"],
                             "lane": g["lane"], "ledger": g["ledger"],
                             "before": g["cursor_before"], "after": pedido["arrival"],
                             "cursor_generation": g["cursor_generation"] + 1})

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _StubExtremo)
    srv.posts, srv.gets, srv.carril_estado = [], [], "off"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    api = f"http://127.0.0.1:{srv.server_port}"
    try:
        r = _llmi(["inbox", "extremo", "--carril", lane], tmp_path, api)
        assert r.returncode == 0, r.stdout + r.stderr
        assert not srv.posts, "inbox no postea"
        m = re.search(r"llmi ack extremo (\d+) --grant (\S+) --carril (\S+)", r.stdout)
        assert m, f"sin comando ack en stdout: {r.stdout[:400]!r}"
        assert int(m.group(1)) == MAXI and m.group(3) == lane
        r2 = _llmi(["ack", "extremo", m.group(1), "--grant", m.group(2),
                    "--carril", lane], tmp_path, api)
        assert r2.returncode == 0, r2.stdout + r2.stderr
        assert len(srv.posts) == 1
        pedido = json.loads(srv.posts[0]["body"])
        assert pedido["arrival"] == MAXI
        assert pedido["grant"]["watermark"] == MAXI
        assert len(pedido["grant"]["allowed_arrivals"]) == 200
        assert f"hasta: {MAXI}" in r2.stdout and "recibo: ack_" in r2.stdout
    finally:
        srv.shutdown()


def test_cli_degrada_con_ack_grant_rechazado(tmp_path):
    """El código nuevo del productor pertenece al CONTRATO del CLI: la lectura
    degrada a READ_ONLY con rc 0 en vez de morir como sobre no verificable.
    FALSADOR: quitar `ACK_GRANT_RECHAZADO` de la allowlist de `llmi` (validación
    de `ack_unavailable`) y este test ve el exit 1."""
    from .test_cli import _Stub, _llmi

    cuerpo = (
        "── demo-ledger · 1 de 1 para ti ──\n"
        "  abc123 #5 L10 2026-08-10T12:00:00 cto-A [REQUEST]\n"
        "\n\nconfirmación disponible — el CLI valida este sobre:\n"
        "  POST /inbox/rechazado/ack\n"
        '  {"hasta":{"demo-ledger":5},'
        '"ack_unavailable":{"code":"ACK_GRANT_RECHAZADO","retryable":false}}\n'
    ).encode()

    class _StubRechazo(_Stub):
        def do_GET(self):
            if self.path.startswith("/inbox/rechazado"):
                self.send_response(200)
                self.send_header("Content-Length", str(len(cuerpo)))
                self.end_headers()
                self.wfile.write(cuerpo)
                return
            return super().do_GET()

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _StubRechazo)
    srv.posts, srv.gets, srv.carril_estado = [], [], "off"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = _llmi(["inbox", "rechazado", "--carril", "demo"], tmp_path,
                  f"http://127.0.0.1:{srv.server_port}")
        assert r.returncode == 0, r.stdout + r.stderr
        assert not srv.posts
        assert "demo-ledger" in r.stdout and "#5" in r.stdout
        assert "ACK no disponible (ACK_GRANT_RECHAZADO)" in r.stderr
        assert "llmi ack" not in r.stdout
    finally:
        srv.shutdown()


def test_la_cota_es_una_sola_en_los_tres_sitios():
    """El modo de fallo real: una mano cambia un sitio y no los otros. Este test
    no duplica el contrato — lo que duplica es la EXIGENCIA de que los tres
    literales coincidan con `ACK_CONTRATO_MAX_CHARS`."""
    s_nombre = (RAIZ / "servicio.py").read_text(encoding="utf-8")
    m = re.search(r"^ACK_CONTRATO_MAX_CHARS = (\d+)$", s_nombre, re.M)
    assert m, "servicio.py perdió la constante de la cota"
    cota = int(m.group(1))
    llmi = (RAIZ / "llmi").read_text(encoding="utf-8")
    ack = re.findall(r"\$\{\#grant\} -le (\d+)", llmi)
    inbox = re.findall(r"len\(contract\)>(\d+)", llmi)
    assert ack == [str(cota)], f"llmi ack usa {ack}, la cota es {cota}"
    assert inbox == [str(cota)], f"llmi inbox usa {inbox}, la cota es {cota}"
    # La cota es un NÚMERO EXACTO del contrato (CTO 09:49), no un suelo (qa A1): un
    # cambio COORDINADO de los tres sitios a otro valor quedaba verde con `>=`.
    assert cota == 32_768
