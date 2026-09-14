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
# credential_generation 4 = la MISMA que conservan los hijos de renovación
# (WIRE_HIJO, _whoami_de): la conservación de generación es EXIGIDA por el
# driver y el arnés la hace literal, no solo comentada.
OBSERVA = {"workload_id": "w-obs", "credential_generation": 4, "status": "running"}
OBJETIVO = {"workload_id": "w-t", "credential_generation": 7, "status": "running"}


def _consulta_de(mapa):
    def consulta(ruta, token=None):
        v = mapa.get(ruta, (404, {}))
        if isinstance(v, Exception):
            raise v
        return v
    return consulta


MAPA_BASE = {D.RUTA_RUNTIMES + "rti-obs": (200, OBSERVA),
             D.RUTA_RUNTIMES + "rti-t": (200, OBJETIVO)}


def _consulta_en_cola(cola_whoami, mapa):
    """whoami por TURNO (tuplas (status, cuerpo): original, hijos...) y mapa
    estático para el resto: la renovación pregunta whoami con el token HIJO y
    el test decide qué identidad declara el servidor en cada momento."""
    turno = {"n": 0}

    def consulta(ruta, token=None):
        if ruta == D.RUTA_WHOAMI:
            v = cola_whoami[turno["n"]] if turno["n"] < len(cola_whoami) \
                else cola_whoami[-1]
            turno["n"] += 1
            if isinstance(v, Exception):
                raise v
            return v
        return mapa.get(ruta, (404, {}))
    return consulta


def _cola(*cuerpos):
    """Atajo: cuerpos de whoami OK en cola."""
    return [(200, c) for c in cuerpos]


def _driver(monkeypatch, tmp_path, *, vueltas=2, mapa=None, srv=None, expira=None,
            muestrea=None, adopta_fallo=None, intervalo_s=5.0, **extras):
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
                      vueltas=vueltas, intervalo_s=intervalo_s,
                      reloj=reloj, espera=reloj.avanza, enviar=srv, **extras)
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


# ── operación continua (--continuo): el contrato REAL del refresh ──────────
# Medido en 2626de2, corregido por el REQUEST MARK:codex-supervisor-
# renovacion-real y endurecido por el REVIEW MARK:codex-supervisor-review-
# 85cacb2: POST /native/v1/sessions/refresh ROTA — token nuevo, rti hijo
# NUEVO, token previo revocado en la misma transacción — pero el hijo
# CONSERVA principal, role, lane, capabilities y generation: es la MISMA
# autoridad. El RECIBO real (`_session_wire`) trae esa autoridad COMPLETA y
# los fixtures la reproducen, con plazo recibo==hijo. Un fake que conservara
# el rti NO sirve como cobertura continua: el gateway real siempre rota.
# Las renovaciones de ESTE fichero son SIMULADAS (refresca y whoami
# monkeypatcheados); la aceptación contra el gateway real vive en
# tests/native_gateway/test_supervisor_driver_renovacion_real.py.
WIRE_HIJO = {"token": "TK-NUEVO-456", "runtime_instance": "rti-hijo",
             "principal": "pr-1", "role": "lane", "lane": "llminbox",
             "capabilities": ["runtime.read"],
             "expires_at": 1757000600, "generation": 4}


def _whoami_de(rti, expires, generation):
    return {"principal": WHOAMI["principal"], "role": WHOAMI["role"],
            "lane": WHOAMI["lane"], "runtime_instance": rti,
            "expires_at": expires, "generation": generation,
            "principal_source": "session", "capabilities": ["runtime.read"]}


def test_continuo_rotacion_no_validable_para_visible_y_no_observa(
        monkeypatch, tmp_path, capsys):
    """El recibo declara un hijo; el whoami (con el token hijo) sirve OTRO
    runtime: no se adopta nada y la corrida termina 3. El token previo ya lo
    revocó el gateway — la parada es la única salida honesta."""
    srv = Servidor({"rti-t": []})   # margen 120 > plazo: rota ANTES de observar
    driver, _reloj = _driver(monkeypatch, tmp_path, vueltas=99, srv=srv,
                             continuo=True)
    monkeypatch.setattr(driver, "refresca", lambda ttl: (200, dict(WIRE_HIJO)))
    # El whoami del mapa es el ORIGINAL (rti-obs): no cuadra con el recibo.

    codigo = driver.corre()

    assert codigo == D.EX_SESION_EXPIRADA
    assert srv.intentos == [], "no se observa con una identidad no validada"
    salida = capsys.readouterr().out
    assert "whoami del hijo no cuadra" in salida
    assert "TK-NUEVO-456" not in salida, "el token hijo jamás sale por stdout"
    sesion = json.loads((tmp_path / "sesion.json").read_text())
    assert sesion["estado"] == "identidad_rotada"
    assert sesion["continuo"] is True and sesion["vueltas_pedidas"] is None
    assert sesion["pendientes_al_cierre"] == []
    assert "TK-NUEVO-456" not in json.dumps(sesion)


def test_continuo_renovacion_validada_adopta_y_sigue_hasta_la_senal(
        monkeypatch, tmp_path, capsys):
    """El camino NORMAL: rotación con whoami del hijo cuadrado (misma
    autoridad) se ADOPTA — contextos y secuencias nuevos, observación que
    sigue, --vueltas NO acota el modo continuo."""
    cuerpos = []

    def srv(url, cuerpo, clave):
        cuerpos.append(dict(cuerpo))
        if len(cuerpos) >= 3:
            driver._parar = True   # señal DURANTE la tercera vuelta
        return _ok(len(cuerpos), "rti-t")

    driver, _reloj = _driver(monkeypatch, tmp_path, vueltas=2, srv=srv,
                             continuo=True, margen_s=120.0)
    hijo = _whoami_de("rti-hijo", 1757000600, 4)
    monkeypatch.setattr(driver, "refresca", lambda ttl: (200, dict(WIRE_HIJO)))
    monkeypatch.setattr(driver, "consulta",
                        _consulta_en_cola(_cola(dict(WHOAMI), hijo),
                                          dict(MAPA_BASE)))

    codigo = driver.corre()

    assert codigo == D.EX_OK, "cierre ordenado sin incidencias no es un 4"
    assert [c["supervisor_seq"] for c in cuerpos] == [1, 2, 3], \
        "secuencia nueva en frío tras adoptar: 3 vueltas pese a --vueltas 2"
    assert driver.observer_rti == "rti-hijo"
    assert driver._token == "TK-NUEVO-456"
    assert driver.deadline == 1757000600.0
    assert driver._transportes and all(
        t._token == "TK-NUEVO-456" for t in driver._transportes), \
        "los transportes nuevos nacen con el token hijo"
    salida = capsys.readouterr().out
    assert "rti-obs → rti-hijo" in salida and "misma autoridad" in salida
    assert salida.count("REFRESCO:") == 1, "adoptado el plazo, no re-refresca"
    sesion = json.loads((tmp_path / "sesion.json").read_text())
    assert sesion["estado"] == "parado_por_senal" and \
        sesion["vueltas_completadas"] == 3
    assert "TK-NUEVO-456" not in salida + json.dumps(sesion)


def test_continuo_dos_renovaciones_simuladas_seguidas_y_observa_despues(
        monkeypatch, tmp_path, capsys):
    """Dos rotaciones SIMULADAS seguidas (rti-hijo-1, rti-hijo-2), cada una
    con su whoami de validación y generación CONSERVADA (4→4); tras la
    segunda se SIGUE observando con secuencia nueva (nonce nuevo) y el sensor
    conservado. «Reales» aquí sería falso: refresca y whoami están
    monkeypatcheados — la aceptación real vive en
    tests/native_gateway/test_supervisor_driver_renovacion_real.py."""
    cuerpos, claves = [], []

    def srv(url, cuerpo, clave):
        cuerpos.append(dict(cuerpo))
        claves.append(clave)
        if len(cuerpos) >= 2:
            driver._parar = True   # señal tras la observación POSTERIOR a R2
        # El gateway ECOS el supervisor_seq del cuerpo: tras cada rotación la
        # secuencia nueva va en 1, y un eco acumulativo sería un rechazo real.
        return _ok(cuerpo["supervisor_seq"], "rti-t")

    estados_pasados = []
    ciclo_real = D.CicloSupervisor

    def ciclo_espia(*a, **kw):
        estados_pasados.append(kw.get("estado"))
        return ciclo_real(*a, **kw)
    monkeypatch.setattr(D, "CicloSupervisor", ciclo_espia)

    hijo1 = _whoami_de("rti-hijo-1", 1757000010, 4)   # expira en margen: re-renueva
    hijo2 = _whoami_de("rti-hijo-2", 1757000600, 4)   # generación CONSERVADA
    cola = _cola(dict(WHOAMI), hijo1, hijo2)

    def refresca(ttl):
        # Recibos con la autoridad completa del hijo y plazo == whoami (el
        # driver los compara semánticamente antes de adoptar).
        if len(cuerpos) == 0:
            return (200, dict(WIRE_HIJO, token="TK-NUEVO-1",
                              runtime_instance="rti-hijo-1",
                              expires_at=1757000010, generation=4))
        return (200, dict(WIRE_HIJO, token="TK-NUEVO-2",
                          runtime_instance="rti-hijo-2", generation=4,
                          expires_at=1757000600))

    driver, _reloj = _driver(monkeypatch, tmp_path, vueltas=99, srv=srv,
                             continuo=True, margen_s=120.0)
    monkeypatch.setattr(driver, "refresca", refresca)
    monkeypatch.setattr(driver, "consulta", _consulta_en_cola(
        cola, dict(MAPA_BASE)))

    codigo = driver.corre()

    assert codigo == D.EX_OK
    salida = capsys.readouterr().out
    assert salida.count("REFRESCO:") == 2, "dos renovaciones simuladas seguidas"
    assert [c["supervisor_seq"] for c in cuerpos] == [1, 1], \
        "cada identidad arranca secuencia NUEVA: jamás se reutiliza"
    assert len(set(claves)) == len(claves), "nonce nuevo por reconstrucción"
    assert driver.observer_rti == "rti-hijo-2" and driver._token == "TK-NUEVO-2"
    assert any(e is not None for e in estados_pasados), \
        "el estado del sensor viaja a la fábrica nueva (no se reinicia)"
    sesion = json.loads((tmp_path / "sesion.json").read_text())
    assert sesion["estado"] == "parado_por_senal"
    assert sesion["objetivos"][0]["arranque"] == "t0", "vínculo conservado"
    todo = salida + json.dumps(sesion)
    assert "TK-NUEVO-1" not in todo and "TK-NUEVO-2" not in todo


def test_continuo_pendiente_se_resuelve_antes_de_rotar(
        monkeypatch, tmp_path, capsys):
    """Regla de transición: con un envío en vuelo NO se rota. DOS pérdidas
    seguidas dejan el pendiente VIVO (el reintento interno del transporte se
    agota); la vuelta siguiente lo reanuda con los mismos bytes y clave bajo
    la sesión VIGENTE, y sólo entonces, con el aire limpio, se renueva y se
    observa con secuencia nueva."""
    guion = [T.RespuestaPerdida("sin respuesta"),      # intento 1: inicial
             T.RespuestaPerdida("sin respuesta"),      # intento 2: reintento interno → pendiente persiste
             _ok(1, "rti-t"),                          # intento 3: reanude en la vuelta 2
             _ok(1, "rti-t"),                          # intento 4: primera obs TRAS rotar (secuencia nueva en 1)
             _ok(2, "rti-t")]                          # intento 5: obs siguiente
    intentos = []

    def srv(url, cuerpo, clave):
        intentos.append((clave, dict(cuerpo)))
        if len(intentos) >= 5:
            driver._parar = True   # señal tras la observación POSTERIOR a la rotación
        a = guion.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    # Reloj (intervalo 2 s): v1 1756999900 (P,P → pendiente) · v2 1756999902
    # (en margen, CON pendiente → NO rota; reanude ok) · v3 1756999904 (aire
    # limpio → RENUEVA) · v4 1756999906 (observa) → señal.
    driver, _reloj = _driver(monkeypatch, tmp_path, vueltas=99, srv=srv,
                             continuo=True, margen_s=5.0, intervalo_s=2.0,
                             expira=1756999907.0)
    hijo = _whoami_de("rti-hijo", 1757000600, 4)
    monkeypatch.setattr(driver, "refresca", lambda ttl: (200, dict(WIRE_HIJO)))
    monkeypatch.setattr(driver, "consulta", _consulta_en_cola(
        # El primer whoami de la cola ES el del arranque: tiene que llevar el
        # PLAZO CORTO del escenario, no el WHOAMI genérico.
        _cola(dict(WHOAMI, expires_at=1756999907), hijo),
        dict(MAPA_BASE)))

    codigo = driver.corre()

    assert codigo == D.EX_INCIDENCIAS, "hay un fallo real acumulado: no es un 0"
    assert intentos[0][0] == intentos[1][0] == intentos[2][0], \
        "reenvío y reanude usan la MISMA clave bajo la sesión vigente"
    assert intentos[0][1] == intentos[1][1] == intentos[2][1], "mismos bytes"
    assert [c["supervisor_seq"] for _c, c in intentos] == [1, 1, 1, 1, 2], \
        "reenvío en la secuencia vigente; tras rotar, secuencia nueva en 1"
    salida = capsys.readouterr().out
    assert salida.count("REFRESCO:") == 1, "se renueva UNA vez: aire limpio"
    sesion = json.loads((tmp_path / "sesion.json").read_text())
    assert sesion["estado"] == "parado_por_senal"
    assert sesion["pendientes_al_cierre"] == [], \
        "el pendiente se resolvió antes de rotar: no queda nada en el aire"


def test_continuo_expira_con_pendiente_sin_resolver_para_visible(
        monkeypatch, tmp_path, capsys):
    """Si lo pendiente no se resuelve antes de expirar: parada VISIBLE con los
    pendientes declarados — sin revocación anticipada ni olvido. Cero
    renovaciones: jamás se rota con algo en el aire."""
    def siempre_perdida(url, cuerpo, clave):
        raise T.RespuestaPerdida("sin respuesta")

    driver, _reloj = _driver(monkeypatch, tmp_path, vueltas=99,
                             srv=siempre_perdida, continuo=True, margen_s=5.0,
                             expira=1756999910.0)
    llamadas = {"refresco": 0}

    def refresca(ttl):
        llamadas["refresco"] += 1
        return (200, dict(WIRE_HIJO))
    monkeypatch.setattr(driver, "refresca", refresca)

    codigo = driver.corre()

    assert codigo == D.EX_SESION_EXPIRADA
    assert llamadas["refresco"] == 0, "jamás se rota con peticiones en vuelo"
    salida = capsys.readouterr().out
    assert "sin resolver: 1" in salida
    sesion = json.loads((tmp_path / "sesion.json").read_text())
    assert sesion["estado"] == "sesion_terminada_sin_refresco"
    assert sesion["pendientes_al_cierre"] == ["rti-t"]


def test_continuo_autoridad_incompatible_para_visible(monkeypatch, tmp_path,
                                                      capsys):
    """El hijo conserva principal/role/lane; si el whoami del hijo sirviera
    OTROS, no es mi renovación: parada visible sin observar."""
    srv = Servidor({"rti-t": []})
    driver, _reloj = _driver(monkeypatch, tmp_path, vueltas=99, srv=srv,
                             continuo=True)
    monkeypatch.setattr(driver, "refresca", lambda ttl: (200, dict(WIRE_HIJO)))
    ajeno = dict(_whoami_de("rti-hijo", 1757000600, 4), lane="otro-carril")
    monkeypatch.setattr(driver, "consulta", _consulta_en_cola(
        _cola(dict(WHOAMI), ajeno),
        dict(MAPA_BASE)))

    codigo = driver.corre()

    assert codigo == D.EX_SESION_EXPIRADA
    assert srv.intentos == [], "una autoridad ajena no observa"
    salida = capsys.readouterr().out
    assert "autoridad" in salida
    sesion = json.loads((tmp_path / "sesion.json").read_text())
    assert sesion["estado"] == "autoridad_incompatible"


def test_continuo_recibo_con_otra_generacion_no_es_misma_autoridad(
        monkeypatch, tmp_path, capsys):
    """La generación CONSERVADA es parte de «misma autoridad» (REVIEW
    85cacb2): un recibo que declarase otra generación —aunque traiga
    principal/role/lane y plazos cuadrados— NO se adopta: parada visible."""
    srv = Servidor({"rti-t": []})
    driver, _reloj = _driver(monkeypatch, tmp_path, vueltas=99, srv=srv,
                             continuo=True)
    monkeypatch.setattr(driver, "refresca",
                        lambda ttl: (200, dict(WIRE_HIJO, generation=5)))
    codigo = driver.corre()
    assert codigo == D.EX_SESION_EXPIRADA
    assert srv.intentos == [], "una generación distinta no observa"
    assert "generación" in capsys.readouterr().out
    sesion = json.loads((tmp_path / "sesion.json").read_text())
    assert sesion["estado"] == "autoridad_incompatible"
    assert driver._token == "TK-SECRETO-123"


def test_continuo_recibo_con_otras_capacidades_para_visible(
        monkeypatch, tmp_path, capsys):
    """El conjunto EXACTO de capacidades del arranque se reproduce en el
    recibo y en el whoami: una capacidad extra (o ausente) para la parada."""
    srv = Servidor({"rti-t": []})
    driver, _reloj = _driver(monkeypatch, tmp_path, vueltas=99, srv=srv,
                             continuo=True)
    engordado = dict(WIRE_HIJO, capabilities=["runtime.read", "runtime.write"])
    monkeypatch.setattr(driver, "refresca", lambda ttl: (200, engordado))
    codigo = driver.corre()
    assert codigo == D.EX_SESION_EXPIRADA
    assert srv.intentos == []
    assert "capacidades" in capsys.readouterr().out
    sesion = json.loads((tmp_path / "sesion.json").read_text())
    assert sesion["estado"] == "autoridad_incompatible"


def test_continuo_recibo_con_plazo_vencido_para_visible(
        monkeypatch, tmp_path, capsys):
    """El plazo del recibo se PARSEA y se compara semánticamente: un
    `expires_at` en pasado no es un refresco vivo — parada antes del whoami."""
    srv = Servidor({"rti-t": []})
    driver, _reloj = _driver(monkeypatch, tmp_path, vueltas=99, srv=srv,
                             continuo=True)
    monkeypatch.setattr(driver, "refresca",
                        lambda ttl: (200, dict(WIRE_HIJO, expires_at=1756999000)))
    codigo = driver.corre()
    assert codigo == D.EX_SESION_EXPIRADA
    assert srv.intentos == []
    assert "plazo ya vencido" in capsys.readouterr().out
    sesion = json.loads((tmp_path / "sesion.json").read_text())
    assert sesion["estado"] == "identidad_rotada"


def test_continuo_plazos_recibo_y_hijo_divergentes_para_visible(
        monkeypatch, tmp_path, capsys):
    """Recibo e hijo dicen plazos DISTINTOS: la identidad no queda validada
    aunque ambos sean futuros — no se adopta un plazo que nadie confirma."""
    srv = Servidor({"rti-t": []})
    driver, _reloj = _driver(monkeypatch, tmp_path, vueltas=99, srv=srv,
                             continuo=True)
    monkeypatch.setattr(driver, "refresca", lambda ttl: (200, dict(WIRE_HIJO)))
    hijo = _whoami_de("rti-hijo", 1757000700, 4)   # ≠ recibo (1757000600)
    monkeypatch.setattr(driver, "consulta", _consulta_en_cola(
        _cola(dict(WHOAMI), hijo),
        dict(MAPA_BASE)))
    codigo = driver.corre()
    assert codigo == D.EX_SESION_EXPIRADA
    assert srv.intentos == []
    assert "no cuadra con el recibo" in capsys.readouterr().out
    sesion = json.loads((tmp_path / "sesion.json").read_text())
    assert sesion["estado"] == "identidad_rotada"


def test_continuo_hijo_sin_generation_explicita_para_visible(
        monkeypatch, tmp_path, capsys):
    """El whoami del hijo SIN `generation` no cae al fallback del recibo: lo
    que se valida es lo que el SERVIDOR confirma, explícito o parada."""
    srv = Servidor({"rti-t": []})
    driver, _reloj = _driver(monkeypatch, tmp_path, vueltas=99, srv=srv,
                             continuo=True)
    monkeypatch.setattr(driver, "refresca", lambda ttl: (200, dict(WIRE_HIJO)))
    hijo = {k: v for k, v in _whoami_de("rti-hijo", 1757000600, 4).items()
            if k != "generation"}
    monkeypatch.setattr(driver, "consulta", _consulta_en_cola(
        _cola(dict(WHOAMI), hijo),
        dict(MAPA_BASE)))
    codigo = driver.corre()
    assert codigo == D.EX_SESION_EXPIRADA
    assert srv.intentos == []
    assert "generación" in capsys.readouterr().out
    sesion = json.loads((tmp_path / "sesion.json").read_text())
    assert sesion["estado"] == "identidad_rotada"


def test_continuo_whoami_del_hijo_sin_respuesta_para_visible(
        monkeypatch, tmp_path, capsys):
    """Validación fallida del token hijo: nada se adopta (el previo ya está
    revocado) y la parada declara el estado HTTP, no el cuerpo."""
    driver, _reloj = _driver(monkeypatch, tmp_path, vueltas=99, continuo=True)
    monkeypatch.setattr(driver, "refresca", lambda ttl: (200, dict(WIRE_HIJO)))
    monkeypatch.setattr(driver, "consulta", _consulta_en_cola(
        [(200, dict(WHOAMI)), (503, {})],
        dict(MAPA_BASE)))

    codigo = driver.corre()

    assert codigo == D.EX_SESION_EXPIRADA
    assert driver._token == "TK-SECRETO-123", "sin validación no hay adopción"
    salida = capsys.readouterr().out
    assert "whoami del hijo HTTP 503" in salida
    sesion = json.loads((tmp_path / "sesion.json").read_text())
    assert sesion["estado"] == "identidad_rotada"


def test_continuo_recibo_ilegible_no_adopta_nada(monkeypatch, tmp_path):
    """Veredicto corto de `_renueva` con recibo incompleto: ni token ni plazo.
    (El recibo devuelto puede haber rotado la sesión: la parada es `rotada`,
    no `fallo` — el camino completo por `corre` está en el caso siguiente.)"""
    driver, _reloj = _driver(monkeypatch, tmp_path, vueltas=99, continuo=True)
    driver.autentica()
    monkeypatch.setattr(driver, "refresca",
                        lambda ttl: (200, {"token": "TK-NUEVO-456"}))
    veredicto = driver._renueva(_SinCiclos(), None)
    assert veredicto[0] == "parada" and veredicto[1] == "identidad_rotada"
    assert driver._token == "TK-SECRETO-123", "sin wire completo no se adopta"
    assert driver.deadline == 1757000000.0, "el plazo tampoco"


class _SinCiclos:
    def estados(self):
        return {}


def test_continuo_recibo_ilegible_para_visible_por_corre(
        monkeypatch, tmp_path, capsys):
    """El mismo recibo incompleto, por la vía real (corre): parada visible y
    ni token ni plazo adoptados."""
    driver, _reloj = _driver(monkeypatch, tmp_path, vueltas=99, continuo=True)
    monkeypatch.setattr(driver, "refresca",
                        lambda ttl: (200, {"token": "TK-NUEVO-456"}))
    assert driver.corre() == D.EX_SESION_EXPIRADA
    salida = capsys.readouterr().out
    assert "recibo de refresco ilegible" in salida
    sesion = json.loads((tmp_path / "sesion.json").read_text())
    assert sesion["estado"] == "identidad_rotada"
    assert driver._token == "TK-SECRETO-123"
    assert driver.deadline == 1757000000.0


def test_continuo_senal_con_fallo_previo_sale_4_y_declara_lo_pendiente(
        monkeypatch, tmp_path, capsys):
    def srv(url, cuerpo, clave):
        driver._parar = True
        raise T.RespuestaPerdida("sin respuesta")

    driver, _reloj = _driver(monkeypatch, tmp_path, vueltas=2, srv=srv,
                             continuo=True, margen_s=5.0)
    assert driver.corre() == D.EX_INCIDENCIAS
    assert driver.fallos_total == 1
    salida = capsys.readouterr().out
    assert "cierre ordenado" in salida and "sin resolver: 1" in salida
    sesion = json.loads((tmp_path / "sesion.json").read_text())
    assert sesion["estado"] == "parado_por_senal"


def test_continuo_restaura_los_handlers_de_senal_al_salir(
        monkeypatch, tmp_path):
    """El driver es un invitado: instala SIGINT/SIGTERM en continuo y los
    RESTAURA al salir, haya corrido una vuelta o cien."""
    import signal as signal_mod
    def srv(url, cuerpo, clave):
        driver._parar = True
        return _ok(1, "rti-t")

    driver, _reloj = _driver(monkeypatch, tmp_path, vueltas=99, srv=srv,
                             continuo=True, margen_s=5.0)
    previo_int = signal_mod.getsignal(signal_mod.SIGINT)
    previo_term = signal_mod.getsignal(signal_mod.SIGTERM)
    centinela_int = lambda s, f: None
    centinela_term = lambda s, f: None
    signal_mod.signal(signal_mod.SIGINT, centinela_int)
    signal_mod.signal(signal_mod.SIGTERM, centinela_term)
    try:
        driver.corre()
        assert signal_mod.getsignal(signal_mod.SIGINT) is centinela_int, \
            "SIGINT restaurado"
        assert signal_mod.getsignal(signal_mod.SIGTERM) is centinela_term, \
            "SIGTERM restaurado"
    finally:
        signal_mod.signal(signal_mod.SIGINT, previo_int)
        signal_mod.signal(signal_mod.SIGTERM, previo_term)


def _main_argv(tmp_path, *extra):
    tok = tmp_path / "tok"
    tok.write_text("t")
    os.chmod(tok, 0o600)
    return ["--base-url", "http://x", "--token-file", str(tok),
            "--objetivo", "rti-t:42", "--fichero-sesion",
            str(tmp_path / "s.json"), *extra]


def test_ttl_s_fuera_del_contrato_sale_2(tmp_path):
    for malo in ("29", "3601"):
        rc = D.main(_main_argv(tmp_path, "--continuo", "--ttl-s", malo))
        assert rc == D.EX_PRECONDICION, f"ttl_s={malo} viola 30..3600"


def test_margen_mayor_o_igual_que_ttl_se_rechaza(tmp_path, monkeypatch):
    """El margen vive DENTRO del plazo que cada renovación pide: margen >= ttl
    sería renovar en bucle desde el primer segundo."""
    with pytest.raises(ValueError, match="margen_s"):
        D.Driver("http://x", "tk", [("rti-t", 42)],
                 fichero_sesion="/tmp/no-hace-falta.json", vueltas=1,
                 intervalo_s=15.0, ttl_s=60, margen_s=60)
    rc = D.main(_main_argv(tmp_path, "--continuo", "--ttl-s", "60",
                           "--refresco-margen-s", "60"))
    assert rc == D.EX_PRECONDICION


def test_continuo_y_vueltas_son_excluyentes(tmp_path):
    rc = D.main(_main_argv(tmp_path, "--continuo", "--vueltas", "3"))
    assert rc == D.EX_PRECONDICION


def test_lee_token_con_techo_de_lectura(tmp_path):
    """Menor ②a de security: ni una lectura sin cota — el +1 del read es el
    que detecta el exceso, igual que en el cuerpo HTTP."""
    tok = tmp_path / "tok"
    tok.write_text("K" * (D.MAX_BYTES_TOKEN + 1))
    os.chmod(tok, 0o600)
    with pytest.raises(D.PrecondicionFallida, match="techo"):
        D.lee_token(str(tok))
    tok.write_text("K" * D.MAX_BYTES_TOKEN)
    assert D.lee_token(str(tok)) == "K" * D.MAX_BYTES_TOKEN


def test_lee_token_el_techo_cuenta_BYTES_no_caracteres(tmp_path):
    """Corrección del REQUEST MARK:codex-supervisor-renovacion-real: el techo
    se aplica sobre la lectura BINARIA. 3000 'ñ' son 3000 caracteres pero
    6000 bytes: si el techo contara caracteres, pasaría."""
    tok = tmp_path / "tok"
    tok.write_bytes("ñ".encode("utf-8") * 3000)
    tok.chmod(0o600)
    assert len("ñ" * 3000) < D.MAX_BYTES_TOKEN, "premisa del caso: cabría en chars"
    with pytest.raises(D.PrecondicionFallida, match="techo"):
        D.lee_token(str(tok))
