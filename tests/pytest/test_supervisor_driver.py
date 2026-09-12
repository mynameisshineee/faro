"""El driver compone los módulos reales: aquí se sustituye el socket y el reloj.

Caso guía que atraviesa TODO el fichero: la identidad se CONSULTA (whoami +
runtimes/{rti}, guionizados como servidor), no se declara; el contexto va ATADO
a la secuencia y al transporte por la subclase de composición; y el exit code
refleja fallos/pendientes, no que se acabaran las vueltas.
"""
from __future__ import annotations

import json
import os
import stat

import pytest

import process_sampler as P
import supervisor_driver as D
import supervisor_transport as T
from supervisor_periodico import ResultadoDeVuelta


class Servidor:
    """Guion de observaciones por objetivo (misma forma que composicion)."""

    def __init__(self, guion_por_rti):
        self.guion = {k: list(v) for k, v in guion_por_rti.items()}
        self.intentos = []

    def __call__(self, url, cuerpo, clave):
        rti = url.split("/runtimes/", 1)[1].rsplit("/observations", 1)[0]
        self.intentos.append((rti, clave, dict(cuerpo)))
        a = self.guion[rti].pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    def seqs(self, rti):
        return [cu["supervisor_seq"] for r, _c, cu in self.intentos if r == rti]


def _ok(seq, rti):
    return (202, {"observation_id": f"o{seq}", "workload_id": "w-t",
                  "runtime_instance": rti, "supervisor_seq": seq, "replayed": False})


WHOAMI = {"principal": "pr-1", "role": "lane", "lane": "llminbox",
          "runtime_instance": "rti-obs", "expires_at": 1757000000,
          "principal_source": "session", "capabilities": ["runtime.read"]}
OBSERVA = {"workload_id": "w-obs", "credential_generation": 3, "status": "running"}
OBJETIVO = {"workload_id": "w-t", "credential_generation": 7, "status": "running"}


def _consulta_de(mapa):
    def consulta(ruta):
        v = mapa.get(ruta, (404, {}))
        if isinstance(v, Exception):
            raise v
        return v
    return consulta


def _driver(monkeypatch, tmp_path, *, vueltas=2, mapa=None, srv=None, expira=None,
            muestrea=None, adopta_fallo=None):
    whoami = dict(WHOAMI)
    if expira is not None:
        whoami["expires_at"] = expira
    completo = {D.RUTA_WHOAMI: (200, whoami),
                D.RUTA_RUNTIMES + "rti-obs": (200, OBSERVA),
                D.RUTA_RUNTIMES + "rti-t": (200, OBJETIVO)}
    completo.update(mapa or {})
    reloj = _Reloj(1756999900.0)
    driver = D.Driver("http://x", "TK-SECRETO-123", [("rti-t", 42)],
                      fichero_sesion=str(tmp_path / "sesion.json"),
                      vueltas=vueltas, intervalo_s=5.0,
                      reloj=reloj, espera=reloj.avanza, enviar=srv)
    monkeypatch.setattr(driver, "consulta", _consulta_de(completo))
    monkeypatch.setattr(P, "adopta",
                        lambda pid: P.Handle(pid=pid, arranque="t0", backend="ps"))
    if adopta_fallo is not None:
        monkeypatch.setattr(P, "adopta", adopta_fallo)
    monkeypatch.setattr(P, "muestrea",
                        muestrea or (lambda _h: P.Muestra(viva=True, rss_bytes=10,
                                                          cpu_millis=5)))
    return driver, reloj


class _Reloj:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def avanza(self, s):
        self.t += s


# ── la corrida feliz: composición con contexto ATADO ───────────────────────
def test_corrida_completa_envia_con_secuencia_y_deja_sesion_operativa(
        monkeypatch, tmp_path, capsys):
    """Si la secuencia no llevara el mismo contexto que el transporte, el envío
    moriría en `exige_contexto` ANTES del socket: que haya intentos con seq 1,2
    ES la prueba de que la subclde ató ambas identidades."""
    srv = Servidor({"rti-t": [_ok(1, "rti-t"), _ok(2, "rti-t")]})
    driver, _reloj = _driver(monkeypatch, tmp_path, vueltas=2, srv=srv)

    codigo = driver.corre()

    assert codigo == D.EX_OK
    assert srv.seqs("rti-t") == [1, 2], "en frío y avanzando, POR objetivo"
    salida = capsys.readouterr().out
    assert "rti-obs" in salida, "el observador consultado se declara al arrancar"
    fichero = tmp_path / "sesion.json"
    sesion = json.loads(fichero.read_text())
    assert sesion["estado"] == "completada" and sesion["vueltas_completadas"] == 2
    assert sesion["objetivos"][0]["arranque"] == "t0"
    # Libro operativo, no autoridad: ni seq ni nonce ni token jamás.
    assert not any(k in sesion for k in ("token", "seq", "nonce", "supervisor_seq"))
    assert stat.S_IMODE(fichero.stat().st_mode) == 0o600
    assert "TK-SECRETO-123" not in salida + fichero.read_text()


# ── objetivos: 404 = falla cerrado nombrando el prerrequisito ──────────────
def test_objetivo_404_nombra_activate_organization_y_no_inventa(monkeypatch, tmp_path):
    mapa = {D.RUTA_RUNTIMES + "rti-t": (404, {})}
    driver, _reloj = _driver(monkeypatch, tmp_path, mapa=mapa)
    with pytest.raises(D.PrecondicionFallida, match="activate_organization"):
        driver.corre()


def test_whoami_bootstrap_sin_runtime_instance_no_arranca(monkeypatch, tmp_path,
                                                          capsys):
    mapa = {D.RUTA_WHOAMI: (200, {k: v for k, v in WHOAMI.items()
                                  if k != "runtime_instance"})}
    driver, _reloj = _driver(monkeypatch, tmp_path, mapa=mapa, vueltas=1)
    with pytest.raises(D.PrecondicionFallida, match="bootstrap"):
        driver.autentica()


def test_main_con_bootstrap_sale_2(monkeypatch, tmp_path):
    """CERO red: `main` construye su propio Driver, así que el fake va a nivel
    de CLASE — ningún socket real puede abrirse en este caso."""
    token = tmp_path / "tok"
    token.write_text("TK-SECRETO-123")
    os.chmod(token, 0o600)
    bootstrap = {k: v for k, v in WHOAMI.items() if k != "runtime_instance"}
    sockets = []
    monkeypatch.setattr(D.Driver, "consulta",
                        lambda self, ruta: sockets.append(ruta) or (200, bootstrap))
    rc = D.main(["--base-url", "http://x", "--token-file", str(token),
                 "--objetivo", "rti-t:42", "--vueltas", "1",
                 "--fichero-sesion", str(tmp_path / "s.json")])
    assert rc == D.EX_PRECONDICION, "sin sesión no hay corrida: falla cerrado"
    assert sockets == [D.RUTA_WHOAMI], "se pregunta whoami y se para: cero red"


# ── expires_at: epoch NÚMERO (lo que whoami sirve) e ISO de refresco ───────
def test_parse_expira_epoch_e_iso():
    assert D._parse_expira(1757000000) == 1757000000.0
    assert D._parse_expira(1757000000.5) == 1757000000.5
    assert D._parse_expira("2026-09-12T00:00:00Z") == D._parse_expira(
        "2026-09-12T00:00:00+00:00")
    with pytest.raises(D.PrecondicionFallida):
        D._parse_expira("no-es-fecha")
    with pytest.raises(D.PrecondicionFallida):
        D._parse_expira(True), "un bool NO es un epoch"


# ── sesión expirada: termina 3, declara la rehidratación por 409 ───────────
def test_sesion_expirada_termina_con_codigo_3_y_avisa_resincronizacion(
        monkeypatch, tmp_path, capsys):
    srv = Servidor({"rti-t": [_ok(i, "rti-t") for i in range(1, 7)]})
    # expira en +30s del reloj inicial; intervalo 5 · cada vuelta avanza 5.
    driver, _reloj = _driver(monkeypatch, tmp_path, vueltas=10, srv=srv,
                             expira=1756999930.0)
    codigo = driver.corre()
    assert codigo == D.EX_SESION_EXPIRADA
    salida = capsys.readouterr().out
    assert "409" in salida, "la guía de rearranque es la resincronización"
    assert json.loads((tmp_path / "sesion.json").read_text())[
        "estado"] == "sesion_expirada"


# ── fallos/pendientes: acabar vueltas NO es éxito ──────────────────────────
def test_perdida_persistente_sale_4_con_pendiente_declarado(monkeypatch, tmp_path):
    def siempre_perdida(url, cuerpo, clave):
        raise T.RespuestaPerdida("sin respuesta")
    driver, _reloj = _driver(monkeypatch, tmp_path, vueltas=2,
                             srv=siempre_perdida)
    assert driver.corre() == D.EX_INCIDENCIAS
    assert driver.fallos_total == 2, "una por vuelta: TYPE-only, sin texto de red"


# ── retirado NO se re-adopta, y su ciclo se suelta ─────────────────────────
def test_pid_reusado_retira_y_no_mira_otra_vez(monkeypatch, tmp_path):
    srv = Servidor({"rti-t": [_ok(1, "rti-t")]})
    llamadas = {"n": 0}

    def muestrea(_h):
        llamadas["n"] += 1
        if llamadas["n"] >= 2:   # el pid fue reciclado: OTRO proceso
            raise P.IdentidadDeProcesoCambiada("arranque distinto")
        return P.Muestra(viva=True, rss_bytes=10, cpu_millis=5)

    driver, _reloj = _driver(monkeypatch, tmp_path, vueltas=3, srv=srv,
                             muestrea=muestrea)
    codigo = driver.corre()
    assert codigo == D.EX_OK, "retirar un muerto no es incidencia pendiente"
    assert len(srv.intentos) == 1, "NO se re-adopta el pid: el nuevo es otro"
    assert driver.retirados_total == 1


# ── el texto de una excepción ajena no arrastra secretos ───────────────────
def test_ningun_secreto_en_salida_ni_sesion_aun_con_fallo_hostil(monkeypatch,
                                                                 tmp_path, capsys):
    def hostil(url, cuerpo, clave):
        raise RuntimeError(f"fallo con TK-SECRETO-123 dentro en {url}")

    driver, _reloj = _driver(monkeypatch, tmp_path, vueltas=1, srv=hostil)
    assert driver.corre() == D.EX_INCIDENCIAS
    salida = capsys.readouterr().out
    todo = salida + (tmp_path / "sesion.json").read_text()
    assert "TK-SECRETO-123" not in todo
    assert "fallo con" not in todo, "del fallo ajeno viaja SÓLO el TYPE"
    assert "rti-t:RuntimeError" in salida, "el TYPE se declara, el texto no"


# ── _linea: acotada, TYPE-only en fallos ───────────────────────────────────
def test_linea_de_vuelta_acota_motivos_y_no_trae_texto_libre():
    r = ResultadoDeVuelta(observados=["rti-t"],
                          sensor_caido=[("rti-t", "SensorNoDisponible: " + "x" * 300)],
                          retirados=[], fallos=[("rti-t", "ValueError")])
    linea = D.Driver._linea(2, r)
    assert "ValueError" in linea and "rti-t" in linea
    assert len(linea) < 600, "una línea de vuelta no crece sin tope"
    assert "x" * 100 not in linea


# ── contrato del fichero privado de token ──────────────────────────────────
def test_lee_token_exige_600_y_no_vacio(tmp_path):
    tok = tmp_path / "tok"
    tok.write_text("  TK-SECRETO-123  ")
    os.chmod(tok, 0o644)
    with pytest.raises(D.PrecondicionFallida, match="600"):
        D.lee_token(str(tok))
    os.chmod(tok, 0o600)
    assert D.lee_token(str(tok)) == "TK-SECRETO-123"
    tok.write_text("")
    with pytest.raises(D.PrecondicionFallida, match="vacío"):
        D.lee_token(str(tok))
    with pytest.raises(D.PrecondicionFallida):
        D.lee_token(str(tmp_path / "no-existe"))


def test_main_rechaza_objetivo_ilegible(tmp_path):
    tok = tmp_path / "tok"
    tok.write_text("t")
    os.chmod(tok, 0o600)
    rc = D.main(["--base-url", "http://x", "--token-file", str(tok),
                 "--objetivo", "sin-pid", "--vueltas", "1",
                 "--fichero-sesion", str(tmp_path / "s.json")])
    assert rc == D.EX_PRECONDICION


# ── sensor caído sostenido: se mira sin mirar — consta como incidencia ─────
def test_sensor_caido_sostenido_sale_4_y_lo_dice_el_libro(monkeypatch, tmp_path):
    srv = Servidor({"rti-t": []})

    def sin_sensor(_h):
        raise P.SensorNoDisponible("sin backend de arranque")

    driver, _reloj = _driver(monkeypatch, tmp_path, vueltas=2, srv=srv,
                             muestrea=sin_sensor)
    assert driver.corre() == D.EX_INCIDENCIAS, \
        "sin instrumento tampoco SE MIRA: no es un 0"
    assert driver.sensor_total == 2 and driver.fallos_total == 0
    assert len(srv.intentos) == 0, "sin muestra no hay observación que enviar"
    assert json.loads((tmp_path / "sesion.json").read_text())[
        "estado"] == "completada_con_incidencias"


# ── contrato del token: fichero REGULAR y 600 EXACTO ───────────────────────
def test_lee_token_exige_regular_y_600_exacto(tmp_path):
    tok = tmp_path / "tok"
    tok.write_text("TK-SECRETO-123")
    os.chmod(tok, 0o400)
    with pytest.raises(D.PrecondicionFallida, match="600 exacto"):
        D.lee_token(str(tok))
    os.chmod(tok, 0o640)
    with pytest.raises(D.PrecondicionFallida, match="600 exacto"):
        D.lee_token(str(tok))
    with pytest.raises(D.PrecondicionFallida, match="regular"):
        D.lee_token(str(tmp_path))


# ── parámetros no finitos: precondición, no traceback ──────────────────────
def test_intervalo_nan_muere_en_precondicion_sin_red(tmp_path):
    tok = tmp_path / "tok"
    tok.write_text("t")
    os.chmod(tok, 0o600)
    rc = D.main(["--base-url", "http://x", "--token-file", str(tok),
                 "--objetivo", "rti-t:42", "--vueltas", "1",
                 "--intervalo-s", "nan",
                 "--fichero-sesion", str(tmp_path / "s.json")])
    assert rc == D.EX_PRECONDICION


# ── cuerpo ilegible del gateway: tipado, sin reflejar texto remoto ─────────
def test_cuerpo_ilegible_es_precondicion_y_no_objeto():
    with pytest.raises(D.PrecondicionFallida):
        D.Driver._cuerpo_de(b"esto no es json")
    with pytest.raises(D.PrecondicionFallida):
        D.Driver._cuerpo_de(b"[1,2]")
    assert D.Driver._cuerpo_de(b"") == {}
    assert D.Driver._cuerpo_de(b'{"k":1}') == {"k": 1}


def test_cuerpo_json_valido_respeta_el_limite_de_lectura():
    # JSON válido a ambos lados del límite: un rechazo de parseo no lo probaría.
    prefijo, sufijo = b'{"k":"', b'"}'
    valor = b'a' * (T.MAX_CUERPO_RESPUESTA - len(prefijo) - len(sufijo))
    assert D.Driver._cuerpo_de(prefijo + valor + sufijo) == {"k": valor.decode()}
    with pytest.raises(D.PrecondicionFallida, match="límite de lectura"):
        D.Driver._cuerpo_de(prefijo + valor + b'a' + sufijo)


def test_token_no_utf8_es_precondicion_sin_reflejar_su_contenido(tmp_path):
    tok = tmp_path / 'token'
    tok.write_bytes(b'private-token-\xff')
    tok.chmod(0o600)
    with pytest.raises(D.PrecondicionFallida) as caught:
        D.lee_token(str(tok))
    assert 'private-token' not in str(caught.value)
