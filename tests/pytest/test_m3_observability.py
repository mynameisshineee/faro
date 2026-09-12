"""M3 · falsadores del kernel de sensores.

Cada propiedad del contrato con su control, y —donde el verde podría no significar
nada— con su ⊕ al lado. El caso paradigmático es el de datos sensibles: un barrido
que nunca encuentra un secreto es indistinguible de un barrido roto, así que aquí
hay siempre una corrida con la captura ENCENDIDA que demuestra que el barrido SÍ
ve el secreto cuando lo hay.
"""
from __future__ import annotations

import re
import subprocess
import sys

import pytest

import observability as obs


def _ident(principal="backend", lane="llminbox", runtime="rt-0001"):
    return obs.Identity.server_derived(principal=principal, role="be", lane=lane,
                                       runtime_instance=runtime)


def _sensor(**kw):
    exp = kw.pop("exporter", None) or obs.MemoryExporter()
    s = obs.build(_ident(), obs.Trust.GATEWAY, enabled=kw.pop("enabled", True),
                  exporter=exp, **kw)
    return s, exp


# ─────────────────────────────────────────────────────────────────────────────
# Apagado por defecto · el SDK ausente no rompe nada
# ─────────────────────────────────────────────────────────────────────────────
def test_apagado_por_defecto_no_exporta_nada(monkeypatch):
    monkeypatch.delenv("LLMINBOX_OBS", raising=False)
    exp = obs.MemoryExporter()
    s = obs.build(_ident(), obs.Trust.GATEWAY, exporter=exp)
    assert s.enabled is False
    s.count("events.accepted")
    s.span("coordination.event.accept")
    s.log("policy.denied")
    assert exp.signals == [], "apagado y aun así exportó"


def test_encendido_SI_exporta(monkeypatch):
    """⊕ del anterior: sin esto, «no exportó» no distingue apagado de roto."""
    monkeypatch.delenv("LLMINBOX_OBS", raising=False)
    s, exp = _sensor()
    assert s.count("events.accepted") is obs.ExportResult.ACCEPTED_BY_SDK
    assert len(exp.metrics()) == 1


def test_la_variable_de_entorno_enciende(monkeypatch):
    monkeypatch.setenv("LLMINBOX_OBS", "1")
    s = obs.build(_ident(), obs.Trust.GATEWAY, exporter=obs.MemoryExporter())
    assert s.enabled is True


def test_sin_bundle_el_estado_es_NOT_CONFIGURED_y_el_kernel_no_se_rompe():
    """«No hay salida» es un ESTADO, ni excepción ni silencio — y ya no se busca el
    SDK por los globals: el pipeline se construye y se pasa, o no lo hay."""
    s = obs.build(_ident(), obs.Trust.GATEWAY, enabled=True)
    assert s.state is obs.Pipeline.NOT_CONFIGURED
    assert s.stats.motivo_exportador == "not_configured"
    assert s.count("events.accepted") is obs.ExportResult.NOT_EXPORTED   # no lanza
    assert s.stats.export_ok == 0 and s.stats.ultimo_export_ok is None


def test_apagado_es_su_PROPIO_estado_distinto_de_no_configurado():
    """⊖: si los dos cayeran en la misma casilla, «lo apagué yo» sería
    indistinguible de «nadie montó la salida»."""
    assert obs.disabled().state is obs.Pipeline.DISABLED
    assert obs.build(_ident(), obs.Trust.GATEWAY, enabled=True).state is \
        obs.Pipeline.NOT_CONFIGURED


def test_el_kernel_no_arrastra_fastapi():
    """M1/M2 lo importan; no puede traerse el framework del servicio detrás."""
    r = subprocess.run(
        [sys.executable, "-c",
         "import sys, observability; "
         "print('fastapi' in sys.modules, 'servicio' in sys.modules, "
         "'opentelemetry' in sys.modules)"],
        cwd=str(__import__("pathlib").Path(__file__).resolve().parents[2]),
        capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "False False False", r.stdout


# ─────────────────────────────────────────────────────────────────────────────
# Confianza · el agente no es una fuente (F-FORGE)
# ─────────────────────────────────────────────────────────────────────────────
def test_el_runtime_del_agente_no_puede_ser_sensor():
    with pytest.raises(obs.UntrustedSource):
        obs.Sensor(_ident(), obs.Trust.AGENT)


def test_gateway_y_supervisor_SI_son_sensores():
    """⊖ que acota el anterior: si nadie pudiera emitir, el test de arriba pasaría igual."""
    for t in (obs.Trust.GATEWAY, obs.Trust.SUPERVISOR):
        assert obs.Sensor(_ident(), t).trust is t


def test_un_runtime_instance_OPACO_es_identidad_valida():
    """ADR-001 emite `runtime_instance` opaco. Una regex demasiado estrecha aquí
    no seria «mas seguro»: seria M1 sin poder construir su propia identidad."""
    i = obs.Identity.server_derived(principal="backend", role="be", lane="llminbox",
                                    runtime_instance="rt:9F3A-DEAD_beef.1")
    assert i.runtime_instance == "rt:9F3A-DEAD_beef.1"


def test_una_identidad_malformada_SI_se_rechaza():
    """⊖ del anterior: si todo pasara, el validador no estaria midiendo nada."""
    with pytest.raises(obs.SchemaError):
        obs.Identity.server_derived(principal="back end", role="be", lane="l",
                                    runtime_instance="rt-1")


def test_la_identidad_no_se_acepta_del_cliente():
    with pytest.raises(obs.UntrustedSource):
        obs.Identity.from_untrusted({"principal": "cto", "runtime_instance": "rt-9"})


def test_el_self_report_del_agente_se_RECHAZA_y_se_cuenta():
    visto = []
    h = obs.Hooks(on_forge=lambda i, campos: visto.append(campos))
    s, _ = _sensor(hooks=h)
    with pytest.raises(obs.UntrustedSource):
        s.reject_agent_report({"status": "healthy", "principal": "otro"})
    assert s.stats.untrusted_rechazos == 1
    assert visto, "el hook F-FORGE no se disparó: el rechazo no es observable"
    assert visto == [2], "el hook recibió algo que no es el nº de campos"


def test_el_hook_de_forja_no_reenvia_NOMBRES_elegidos_por_el_agente():
    """Los nombres de campo los elige la parte NO confiable, y un nombre puede
    llevar el valor dentro (`token=sk-...`). Reenviarlos a la alarma es dejar que
    el sujeto rechazado escriba en el registro que lo rechaza."""
    visto = []
    s, _ = _sensor(hooks=obs.Hooks(on_forge=lambda i, n: visto.append(n)))
    with pytest.raises(obs.UntrustedSource):
        s.reject_agent_report({"token=sk-live-DEADBEEF": 1, "x": 2})
    assert visto == [2]
    assert "sk-live-DEADBEEF" not in "".join(str(x) for x in visto)


# ─────────────────────────────────────────────────────────────────────────────
# Identidad server-derived en el recurso, JAMÁS en las etiquetas
# ─────────────────────────────────────────────────────────────────────────────
def test_la_identidad_va_en_el_RECURSO():
    s, exp = _sensor()
    s.count("events.accepted")
    res = exp.metrics()[0].resource
    assert res["llminbox.principal"] == "backend"
    assert res["service.instance.id"] == "rt-0001"
    assert res["llminbox.lane"] == "llminbox"


@pytest.mark.parametrize("clave", [
    "event_id", "receipt_id", "runtime_instance", "principal", "task", "uri",
    "credential_ref", "entry_eid", "body", "path",
])
def test_un_identificador_NUNCA_es_etiqueta_de_metrica(clave):
    s, exp = _sensor()
    s.count("events.accepted", **{clave: "valor-unico-12345"})
    m = exp.metrics()[0]
    assert clave not in m.labels, f"{clave} entró como etiqueta"
    assert "valor-unico-12345" not in "".join(
        f"{k}={x}" for k, x in m.labels.items()), "el valor entró por otra clave"
    assert s.stats.label_violaciones == 1
    assert m.labels == {"lane": "llminbox"}, "se perdió la etiqueta legítima"


def test_en_estricto_la_etiqueta_prohibida_LANZA():
    """⊕: sin esto, «se descartó» no se distingue de «nunca se intentó»."""
    s, _ = _sensor(strict=True)
    with pytest.raises(obs.ForbiddenLabel):
        s.count("events.accepted", event_id="evt-1")


def test_un_valor_libre_DESBORDA_en_vez_de_crear_serie():
    s, exp = _sensor()
    s.count("denials", reason="motivo-inventado-por-el-llamante")
    assert exp.metrics()[0].labels["reason"] == obs.OVERFLOW


def test_un_valor_del_vocabulario_pasa_TAL_CUAL():
    """⊖ del anterior: si todo desbordara, la métrica no mediría nada."""
    s, exp = _sensor()
    s.count("denials", reason="lane_mismatch")
    assert exp.metrics()[0].labels["reason"] == "lane_mismatch"


# ─────────────────────────────────────────────────────────────────────────────
# F-NOMBRE · el nombre de la señal también es cardinalidad
# ─────────────────────────────────────────────────────────────────────────────
def test_un_NOMBRE_de_metrica_libre_no_se_emite():
    """Con las etiquetas cerradas y el nombre abierto, `count(f"job.{id}")` crea
    una serie por id sin tocar una sola etiqueta prohibida: cerrar la puerta y
    dejar la ventana."""
    s, exp = _sensor()
    for i in range(50):
        s.count(f"job.{i}.done")
    assert exp.metrics() == []
    assert s.stats.nombre_violaciones == 50


def test_un_NOMBRE_del_vocabulario_SI_se_emite():
    """⊖: si nada se emitiera, el test de arriba pasaría por vacío."""
    s, exp = _sensor()
    assert s.count("events.accepted") is obs.ExportResult.ACCEPTED_BY_SDK
    assert len(exp.metrics()) == 1


@pytest.mark.parametrize("emisor,nombre", [
    ("count", "inventada.por.el.llamante"),
    ("span", "runtime.job.de-la-tarea-42"),
    ("log", "algo.que.paso"),
])
def test_ningun_emisor_acepta_un_nombre_fuera_del_vocabulario(emisor, nombre):
    s, exp = _sensor()
    getattr(s, emisor)(nombre) if emisor != "count" else s.count(nombre)
    assert exp.signals == []
    assert s.stats.nombre_violaciones == 1


def test_en_estricto_el_nombre_libre_LANZA():
    s, _ = _sensor(strict=True)
    with pytest.raises(obs.SchemaError):
        s.count("inventada")


# ─────────────────────────────────────────────────────────────────────────────
# F-CARD · presupuesto de cardinalidad
# ─────────────────────────────────────────────────────────────────────────────
def _sensores(n, budget, exp, hooks=None):
    """N sensores con N identidades, compartiendo presupuesto y exportador.

    Es el vector REAL de explosión de cardinalidad, y por eso el test cambió: la
    versión anterior la generaba pasando `lane=f"carril-{i}"`, o sea explotando el
    agujero que este mismo commit cierra. Medía el presupuesto A TRAVÉS de un
    defecto; cerrado el defecto, habría medido su propia cura.
    """
    return [obs.build(_ident(principal=f"agente{i}", runtime=f"rt-{i:04d}"),
                      obs.Trust.GATEWAY, enabled=True, exporter=exp,
                      budget=budget, hooks=hooks) for i in range(n)]


def test_el_presupuesto_de_series_ACOTA_aunque_el_llamante_no_colabore():
    visto = []
    exp = obs.MemoryExporter()
    b = obs.CardinalityBudget(max_series=8)
    for s in _sensores(200, b, exp, obs.Hooks(on_cardinality=lambda i, n: visto.append(n))):
        s.count("events.accepted")
    assert b.activas <= 8
    assert len(exp.series()) <= 9, f"series activas: {len(exp.series())}"
    assert b.desbordes > 0
    assert visto, "el hook F-CARD no se disparó"


def test_sin_presupuesto_estrecho_las_series_SI_crecen():
    """⊕ imprescindible: mide el presupuesto, no una constante del emisor.

    Con `lane` derivado de la identidad, dos sensores de identidades distintas
    siguen siendo series distintas —la clave incluye `identity.series_key`—, así
    que el ⊕ sigue teniendo por dónde crecer.
    """
    exp = obs.MemoryExporter()
    b = obs.CardinalityBudget(max_series=1000)
    for s in _sensores(200, b, exp):
        s.count("events.accepted")
    assert b.activas == 200


def test_en_estricto_el_desborde_LANZA():
    exp = obs.MemoryExporter()
    b = obs.CardinalityBudget(max_series=2)
    a, c, d = _sensores(3, b, exp)
    a.strict = c.strict = d.strict = True
    a.count("events.accepted")
    c.count("events.accepted")
    with pytest.raises(obs.CardinalityBudgetExceeded):
        d.count("events.accepted")


# ─────────────────────────────────────────────────────────────────────────────
# F-CARRIL · el valor de una etiqueta de vocabulario ABIERTO era una puerta
# ─────────────────────────────────────────────────────────────────────────────
def test_un_secreto_no_puede_salir_como_VALOR_de_la_etiqueta_de_carril():
    """`lane` no tenía lista de valores —«acotado por el mapa de credenciales»— y
    devolvía la cadena del llamante tal cual. Cerrar las claves y dejar abierto un
    valor es la misma avería que cerrar las etiquetas y dejar abierto el nombre."""
    s, exp = _sensor()
    s.count("events.accepted", lane="sk-live-DEADBEEF")
    assert exp.metrics()[0].labels["lane"] == "llminbox"
    assert "sk-live-DEADBEEF" not in exp.volcado()
    assert s.stats.label_violaciones == 1


def test_el_carril_emitido_es_el_de_la_IDENTIDAD_y_no_el_del_llamante():
    """Peor que la fuga: la etiqueta podía CONTRADECIR al recurso server-derived,
    y `lane` es la clave de correlación de todo el sistema."""
    s, exp = _sensor()
    s.count("events.accepted", lane="otro-carril")
    m = exp.metrics()[0]
    assert m.labels["lane"] == m.resource["llminbox.lane"] == "llminbox"


def test_un_carril_que_COINCIDE_TAMBIEN_es_violacion():
    """Perdonar el carril que coincide deja viva la ruta por la que llega —y con
    ella el sitio donde mañana alguien pasa uno que no coincide—, además de premiar
    la coincidencia accidental: dos sesiones del mismo carril nunca verían el aviso.
    El carril no se propone en ningún caso."""
    s, exp = _sensor()
    s.count("events.accepted", lane="llminbox")
    assert s.stats.label_violaciones == 1
    assert exp.metrics()[0].labels["lane"] == "llminbox"   # derivado, no aceptado


def test_sin_carril_en_la_llamada_NO_hay_violacion():
    """⊖ del anterior: si toda métrica contase violación, el contador dejaría de
    señalar al llamante que aporta carril y pasaría a señalar a todo el mundo."""
    s, _ = _sensor()
    s.count("events.accepted")
    assert s.stats.label_violaciones == 0


def test_toda_etiqueta_PERMITIDA_tiene_vocabulario_CERRADO():
    """La allowlist y el vocabulario pueden separarse en silencio: basta añadir una
    clave a `LABELS_PERMITIDAS` y olvidar su lista de valores para reabrir el valor
    libre por esa clave. Antes eso lo tapaba una rama «vocabulario abierto»; hoy la
    rama no existe, así que la deriva se caza aquí en vez de en producción.

    `lane` es la excepción declarada: no lo propone el llamante, se deriva.
    """
    sin_vocabulario = sorted(
        k for k in obs.LABELS_PERMITIDAS
        if k != "lane" and not obs.VOCABULARIO.get(k))
    assert sin_vocabulario == [], (
        f"etiquetas permitidas sin vocabulario cerrado: {sin_vocabulario}")


def test_la_denylist_de_etiquetas_no_se_esquiva_con_MAYUSCULAS():
    """La denylist comparaba `k`, la de contenido `k.lower()`. `Event_ID` esquivaba
    la primera; lo paraba la allowlist después, así que no filtraba — pero un
    control que no discrimina acredita un instrumento muerto sintiéndose riguroso."""
    s, _ = _sensor(strict=True)
    for clave in ("Event_ID", "RECEIPT_ID", "Principal"):
        with pytest.raises(obs.ForbiddenLabel):
            s.count("events.accepted", **{clave: "x"})


# ─────────────────────────────────────────────────────────────────────────────
# Privacidad · el secreto no aparece en el exportador FALSO
# ─────────────────────────────────────────────────────────────────────────────
# CONTENIDO (prompt, body, tool args, resultados) y SECRETOS (credenciales) NO se
# tratan igual, y por eso van en dos diccionarios: del segundo no puede salir un
# digest estable ni aunque se encienda la captura.
CONTENIDO = {
    "prompt": "eres un agente y esto es una instruccion larga",
    "body": "cuerpo del mensaje con datos del cliente",
    "tool_args": "--destino=/ledger/64bis.md --lineas=40",
    "tool_result": "salida de la herramienta con PII: 12345678Z",
}
SECRETOS = {
    "password": "p4ssw0rd-de-la-flota",
    "token": "sk-live-DEADBEEF",
    "api_key": "AKIA" + "0" * 16,
    "authorization": "Bearer sk-live-DEADBEEF",
    "cookie": "session=abc123",
    "credential": "c-000000-aaaa",
}
ANIDADO = {
    "job": {"tool": {"tool_args": "--token=sk-ANIDADO-1"},
            "auth": {"authorization": "Bearer sk-ANIDADO-2"}},
    "msgs": [{"prompt": "sk-ANIDADO-3"}, {"password": "sk-ANIDADO-4"},
             [{"cookie": "sk-ANIDADO-5"}]],
}


def test_ningun_contenido_sensible_llega_al_exportador():
    s, exp = _sensor()
    s.span("runtime.job", attributes=dict(CONTENIDO))
    s.log("policy.denied", **CONTENIDO)
    volcado = exp.volcado()
    for k, val in CONTENIDO.items():
        assert val not in volcado, f"{k} salió en claro"
        assert f"{k}_redacted" in volcado, f"{k} desapareció sin dejar marca"
        assert f"{k}_len" in volcado


def test_con_captura_ENCENDIDA_el_barrido_SI_ve_el_contenido():
    """⊕ MADRE de la privacidad.

    Un barrido que nunca encuentra nada es indistinguible de un barrido roto. Con
    la captura encendida el MISMO barrido tiene que encontrar el contenido; si no
    lo encuentra, el test de arriba no estaba midiendo, estaba decorando.
    """
    s, exp = _sensor(capture_content=True)
    s.span("runtime.job", attributes=dict(CONTENIDO))
    volcado = exp.volcado()
    for val in CONTENIDO.values():
        assert val in volcado


def test_un_SECRETO_no_sale_ni_con_la_captura_ENCENDIDA():
    """La perilla de depuración no puede ser una perilla de exfiltración."""
    assert re.fullmatch(r"AKIA[0-9A-Z]{16}", SECRETOS["api_key"])
    s, exp = _sensor(capture_content=True, content_digest=True)
    s.span("runtime.job", attributes=dict(SECRETOS))
    volcado = exp.volcado()
    for k, val in SECRETOS.items():
        assert val not in volcado, f"{k} salió en claro con captura encendida"
        assert f"{k}_redacted" in volcado


def test_de_un_SECRETO_no_sale_un_SHA_estable():
    """Un digest sin clave de una credencial es un oráculo de confirmación: con el
    espacio de valores adivinable —prefijo conocido, longitud fija— se prueba
    offline hasta acertar. Y aunque no lo sea, correlaciona al sujeto para siempre."""
    import hashlib
    assert re.fullmatch(r"AKIA[0-9A-Z]{16}", SECRETOS["api_key"])
    s, exp = _sensor(content_digest=True)
    s.span("runtime.job", attributes=dict(SECRETOS))
    volcado = exp.volcado()
    for k, val in SECRETOS.items():
        assert f"{k}_sha256" not in volcado, f"{k} publicó un digest sin clave"
        assert hashlib.sha256(val.encode()).hexdigest()[:12] not in volcado


def test_el_digest_de_CONTENIDO_solo_si_se_pide():
    s_no, exp_no = _sensor()
    s_no.span("runtime.job", attributes={"prompt": "x"})
    assert "prompt_sha256" not in exp_no.volcado()

    s_si, exp_si = _sensor(content_digest=True)
    s_si.span("runtime.job", attributes={"prompt": "x"})
    assert "prompt_sha256" in exp_si.volcado(), "⊕: si nunca saliera, el ⊖ no mediría"


def test_con_clave_separada_el_secreto_sale_como_HMAC_y_no_como_valor():
    s, exp = _sensor(hmac_key=b"clave-de-correlacion-aparte")
    s.span("runtime.job", attributes={"token": "sk-live-DEADBEEF"})
    volcado = exp.volcado()
    assert "sk-live-DEADBEEF" not in volcado
    assert "token_hmac" in volcado
    assert "token_sha256" not in volcado


# ── ANIDADO: el caso REAL, no el de juguete ─────────────────────────────────
def test_un_secreto_ANIDADO_en_mapping_o_sequence_no_se_filtra():
    """El primer redactor sólo miraba el nivel de arriba, y un atributo de span es
    casi siempre contexto anidado. Un barrido de superficie da verde justo sobre
    los payloads que importan."""
    s, exp = _sensor()
    s.span("runtime.job", attributes=ANIDADO)
    volcado = exp.volcado()
    for i in range(1, 6):
        assert f"sk-ANIDADO-{i}" not in volcado, f"se filtró el anidado nº{i}"


def test_el_barrido_ANIDADO_SI_ve_el_contenido_cuando_se_permite():
    """⊕ del anterior, y sólo sobre CONTENIDO: los secretos siguen fuera."""
    s, exp = _sensor(capture_content=True)
    s.span("runtime.job", attributes=ANIDADO)
    volcado = exp.volcado()
    assert "sk-ANIDADO-1" in volcado and "sk-ANIDADO-3" in volcado   # tool_args, prompt
    assert "sk-ANIDADO-2" not in volcado                             # authorization
    assert "sk-ANIDADO-4" not in volcado                             # password
    assert "sk-ANIDADO-5" not in volcado                             # cookie anidado en lista


def test_la_recursion_tiene_cota_de_profundidad_y_de_items():
    hondo: dict = {"x": "fondo"}
    for _ in range(40):
        hondo = {"n": hondo}
    r = obs.redacta(hondo, max_depth=4)
    assert obs.MARCA_PROFUNDIDAD in str(r)
    assert "fondo" not in str(r), "más allá de la cota se emitió el crudo"
    r2 = obs.redacta({"l": list(range(500))}, max_items=8)
    assert obs.MARCA_TRUNCADO in str(r2)


def test_una_cadena_no_se_recorre_letra_a_letra():
    """`str` es Sequence: sin excluirla, el redactor devolvería el secreto
    despiezado en letras — irreconocible para un grep y perfectamente legible."""
    r = obs.redacta({"nota": "abc"})
    assert r["nota"] == "abc"


def test_el_baggage_solo_lleva_carril_y_esquema():
    s, _ = _sensor()
    assert set(s.baggage()) == {"lane", "schema"}


def test_el_baggage_rechaza_identidad():
    s, _ = _sensor(strict=True)
    with pytest.raises(obs.SchemaError):
        s.baggage(principal="backend")


# ─────────────────────────────────────────────────────────────────────────────
# Hechos durables mandan sobre telemetría
# ─────────────────────────────────────────────────────────────────────────────
def test_el_progreso_solo_lo_acredita_un_hecho_del_JOURNAL():
    with pytest.raises(obs.UntrustedSource):
        obs.Fact(kind="event", seq=1, at=0.0, source="telemetry")


def test_un_hecho_durable_SI_construye():
    """⊖: si ninguna fuente valiera, el test de arriba pasaría por vacío."""
    assert obs.Fact(kind="event", seq=1, at=0.0).source == "journal"


def test_el_latido_NO_mueve_el_progreso():
    reloj = [1000.0]
    lp = obs.LivenessProgress(_ident(), clock=lambda: reloj[0])
    lp.heartbeat(trust=obs.Trust.SUPERVISOR, cycle_ack=True)
    assert lp.evaluate(open_obligations=1).estado is obs.Estado.ATASCADA, (
        "el latido acreditó progreso: fuentes no disjuntas")


# ─────────────────────────────────────────────────────────────────────────────
# Liveness · quién puede latir y con qué
# ─────────────────────────────────────────────────────────────────────────────
def test_el_gateway_no_puede_latir_por_el_agente():
    lp = obs.LivenessProgress(_ident())
    with pytest.raises(obs.UntrustedSource):
        lp.heartbeat(trust=obs.Trust.GATEWAY, cycle_ack=True)


def test_el_INTENTO_no_refresca_el_latido():
    """B2, literal: refrescar con el intento deja verde a un watcher roto."""
    lp = obs.LivenessProgress(_ident())
    assert lp.heartbeat(trust=obs.Trust.SUPERVISOR, cycle_ack=False) is False
    assert lp.evaluate().estado is obs.Estado.SIN_ARMAR


def test_el_ACUSE_si_refresca():
    """⊕ del anterior."""
    lp = obs.LivenessProgress(_ident())
    assert lp.heartbeat(trust=obs.Trust.SUPERVISOR, cycle_ack=True) is True
    assert lp.evaluate().estado is not obs.Estado.SIN_ARMAR


# ─────────────────────────────────────────────────────────────────────────────
# Detector · un estado por avería, sin rama por defecto benigna
# ─────────────────────────────────────────────────────────────────────────────
def _lp(reloj, **kw):
    return obs.LivenessProgress(_ident(), clock=lambda: reloj[0], **kw)


def test_F_KILL_sin_latido_dentro_del_plazo_es_MUDA():
    reloj = [1000.0]
    visto = []
    lp = _lp(reloj, hooks=obs.Hooks(on_kill=lambda i: visto.append(1)))
    lp.heartbeat(trust=obs.Trust.SUPERVISOR, cycle_ack=True)
    reloj[0] += 181
    v = lp.evaluate()
    assert v.estado is obs.Estado.MUDA and not v.sano
    assert visto, "el hook F-KILL no se disparó"


def test_F_SIGSTOP_vivo_sin_avance_y_CON_obligacion_es_ATASCADA():
    reloj = [1000.0]
    visto = []
    lp = _lp(reloj, hooks=obs.Hooks(on_sigstop=lambda i: visto.append(1)))
    lp.heartbeat(trust=obs.Trust.SUPERVISOR, cycle_ack=True)
    lp.progress(obs.Fact(kind="event", seq=1, at=reloj[0]))
    reloj[0] += 400            # el latido sigue, el progreso caducó
    lp.heartbeat(trust=obs.Trust.SUPERVISOR, cycle_ack=True)
    v = lp.evaluate(open_obligations=1)
    assert v.estado is obs.Estado.ATASCADA and not v.sano
    assert visto


def test_F_SIGSTOP_control_negativo_sin_obligacion_es_OCIO_y_es_SANO():
    """⊖ que impide curar de más: llamar «atascado» al ocio mata la alarma."""
    reloj = [1000.0]
    lp = _lp(reloj)
    lp.heartbeat(trust=obs.Trust.SUPERVISOR, cycle_ack=True)
    v = lp.evaluate(open_obligations=0)
    assert v.estado is obs.Estado.VIVA_SIN_OBLIGACION and v.sano


def test_F_LOOP_reintento_del_mismo_item_es_EN_BUCLE():
    reloj = [1000.0]
    visto = []
    lp = _lp(reloj, hooks=obs.Hooks(on_loop=lambda i: visto.append(1)))
    lp.heartbeat(trust=obs.Trust.SUPERVISOR, cycle_ack=True)
    v = lp.evaluate(loop=obs.LoopEvidence(outbox_attempts=9, outbox_pending=True))
    assert v.estado is obs.Estado.EN_BUCLE and v.motivo == "outbox_retry"
    assert visto


def test_F_LOOP_control_negativo_misma_tasa_con_sujetos_DISTINTOS_no_dispara():
    """⊖: un agente rápido y sano no puede ser indistinguible de uno en bucle."""
    reloj = [1000.0]
    lp = _lp(reloj)
    lp.heartbeat(trust=obs.Trust.SUPERVISOR, cycle_ack=True)
    lp.progress(obs.Fact(kind="event", seq=1, at=reloj[0]))
    v = lp.evaluate(loop=obs.LoopEvidence(denials_same_reason=400,
                                          distinct_subjects=400))
    assert v.estado is obs.Estado.VIVA_CON_PROGRESO


def test_F_MUTE_exportador_que_no_entrega_es_SENSOR_MUDO():
    reloj = [1000.0]
    visto = []
    lp = _lp(reloj, hooks=obs.Hooks(on_mute=lambda i: visto.append(1)))
    lp.heartbeat(trust=obs.Trust.SUPERVISOR, cycle_ack=True)
    v = lp.evaluate(exporter_stale_s=999)
    assert v.estado is obs.Estado.SENSOR_MUDO and not v.sano
    assert visto


def test_F_CLOCK_un_latido_en_el_futuro_es_INDETERMINADO():
    reloj = [1000.0]
    lp = _lp(reloj)
    lp.heartbeat(trust=obs.Trust.SUPERVISOR, at=reloj[0] + 500, cycle_ack=True)
    v = lp.evaluate()
    assert v.estado is obs.Estado.INDETERMINADO and not v.sano


def test_estado_durable_ilegible_no_cae_al_lado_sano():
    lp = obs.LivenessProgress(_ident())
    lp.readable = False
    v = lp.evaluate()
    assert v.estado is obs.Estado.ILEGIBLE and not v.sano


def test_INARMABLE_no_es_SANO():
    """La avería exacta que ya costó un `ok:true` sobre un hombre muerto."""
    lp = obs.LivenessProgress(_ident(), armable=False)
    v = lp.evaluate()
    assert v.estado is obs.Estado.INARMABLE
    assert obs.Estado.INARMABLE not in obs.SANOS
    assert not v.sano


def test_TODO_estado_declarado_tiene_un_productor():
    """La partición se prueba PRODUCIENDO los diez estados, no enumerándolos.

    Un `Estado` nuevo sin escenario que lo genere es una casilla que nadie alcanza
    —o, peor, que se alcanza por el `else` de otro—. Este test se pone rojo el día
    que alguien añada un estado y no diga cómo se llega a él.
    """
    producidos = set()

    def corre(**kw):
        reloj = [1000.0]
        pre = kw.pop("pre", None)
        lp = obs.LivenessProgress(_ident(), clock=lambda: reloj[0],
                                  armable=kw.pop("armable", True))
        lp.readable = kw.pop("readable", True)
        if pre:
            pre(lp, reloj)
        producidos.add(lp.evaluate(**kw).estado)

    corre(readable=False)                                        # ILEGIBLE
    corre(armable=False)                                         # INARMABLE
    corre()                                                      # SIN_ARMAR
    corre(pre=lambda lp, r: lp.heartbeat(trust=obs.Trust.SUPERVISOR, cycle_ack=True),
          exporter_stale_s=999)                                  # SENSOR_MUDO
    corre(pre=lambda lp, r: lp.heartbeat(trust=obs.Trust.SUPERVISOR,
                                         at=r[0] + 500, cycle_ack=True))  # INDETERMINADO

    def _muerto(lp, r):
        lp.heartbeat(trust=obs.Trust.SUPERVISOR, at=r[0] - 500, cycle_ack=True)
    corre(pre=_muerto)                                           # MUDA

    def _vivo(lp, r):
        lp.heartbeat(trust=obs.Trust.SUPERVISOR, cycle_ack=True)
    corre(pre=_vivo)                                             # VIVA_SIN_OBLIGACION
    corre(pre=_vivo, open_obligations=1)                         # ATASCADA
    corre(pre=_vivo, loop=obs.LoopEvidence(outbox_attempts=9,
                                           outbox_pending=True))  # EN_BUCLE

    def _vivo_con_avance(lp, r):
        lp.heartbeat(trust=obs.Trust.SUPERVISOR, cycle_ack=True)
        lp.progress(obs.Fact(kind="event", seq=1, at=r[0]))
    corre(pre=_vivo_con_avance)                                  # VIVA_CON_PROGRESO

    faltan = set(obs.Estado) - producidos
    assert not faltan, f"estados declarados que nadie produce: {sorted(e.value for e in faltan)}"
    assert obs.SANOS == {obs.Estado.VIVA_CON_PROGRESO,
                         obs.Estado.VIVA_SIN_OBLIGACION,
                         obs.Estado.SIN_ARMAR}, (
        "cambió el conjunto sano: cada añadido aquí declara sano un estado nuevo")


# ─────────────────────────────────────────────────────────────────────────────
# Exportador caído · nunca tumba al llamante, nunca es invisible
# ─────────────────────────────────────────────────────────────────────────────
def test_el_exportador_caido_no_rompe_el_camino_que_lo_llamo():
    visto = []
    fall = obs.FailingExporter()
    s = obs.build(_ident(), obs.Trust.GATEWAY, enabled=True, exporter=fall,
                  hooks=obs.Hooks(on_exporter_down=lambda i, m: visto.append(m)))
    assert s.count("events.accepted") is obs.ExportResult.NOT_EXPORTED   # no lanza
    assert s.stats.export_fallos == 1
    assert fall.intentos == 1
    assert visto, "un exportador caído en silencio es ausencia leída como salud"
    assert visto == ["exporter_unavailable"], "el hook recibió algo que no es un código"


class _ExportadorQueFiltra:
    """Falla con un mensaje que lleva un secreto dentro — el caso realista: la
    librería de red mete la URL con el token en el texto de la excepción."""
    nombre = "filtra"

    def export(self, signal):
        raise ConnectionError(
            "POST https://otlp.example/v1/traces?api_key=sk-live-FUGA falló")


def test_una_EXCEPCION_con_un_secreto_dentro_no_llega_ni_al_hook_ni_al_exportador():
    """El mensaje de error es el sitio donde nadie busca un secreto, porque el
    campo por el que sale no se llama como un secreto."""
    visto = []
    s = obs.build(_ident(), obs.Trust.GATEWAY, enabled=True,
                  exporter=_ExportadorQueFiltra(),
                  hooks=obs.Hooks(on_exporter_down=lambda i, c: visto.append(c)))
    assert s.count("events.accepted") is obs.ExportResult.NOT_EXPORTED
    assert visto == ["connection"], f"llegó algo que no es un código: {visto}"
    assert "sk-live-FUGA" not in "".join(visto)
    assert "otlp.example" not in "".join(visto)


def test_el_codigo_de_error_es_de_un_conjunto_CERRADO():
    """⊖: si `codigo_error` devolviera el nombre de la clase, una excepción ajena
    de nombre arbitrario reabriría la cardinalidad por la puerta del error."""
    class _RarisimaExcepcionDeUnaLibreriaAjena(Exception):
        pass
    assert obs.codigo_error(_RarisimaExcepcionDeUnaLibreriaAjena("x")) == "other"
    assert obs.codigo_error(TimeoutError()) == "timeout"


def test_el_exportador_sano_devuelve_ACCEPTED_BY_SDK():
    """⊖: sin esto, `False` no distingue «falló» de «siempre devuelve False»."""
    s, _ = _sensor()
    assert s.count("events.accepted") is obs.ExportResult.ACCEPTED_BY_SDK


# ─────────────────────────────────────────────────────────────────────────────
# outbox.materialize · Link y effect_id
# ─────────────────────────────────────────────────────────────────────────────
# HEX DE VERDAD. La primera versión usaba `"t"*32`/`"s"*16`, que no son
# hexadecimales: contra el exportador de mentira daba igual —guardaba la cadena—
# pero el SDK real no puede construir un `SpanContext` con eso. Un fixture que sólo
# vale contra el doble hace que el doble mida al doble.
CTX = {"trace_id": "a1b2c3d4" * 4, "span_id": "0fedcba9" * 2}


def test_el_span_de_outbox_ENLAZA_y_no_anida():
    s, exp = _sensor()
    s.outbox_span(event_id="evt-1", effect_id="eff-1", accept_context=CTX,
                  attempt=3, outcome="materialized")
    sp = exp.spans()[0]
    assert sp.links and sp.links[0]["rel"] == "accepted_by"
    assert sp.links[0]["span_id"] == CTX["span_id"]
    assert "parent_span_id" not in sp.attributes
    assert sp.attributes["effect_id"] == "eff-1"
    assert sp.attributes["attempt"] == 3


def test_anidar_el_span_de_outbox_es_un_error_DURO():
    s, _ = _sensor()
    with pytest.raises(obs.SchemaError):
        s.outbox_span(event_id="e", effect_id="f", accept_context=CTX,
                      attempt=1, outcome="ok", parent=CTX)


def test_sin_effect_id_no_hay_span_de_outbox():
    s, _ = _sensor()
    with pytest.raises(obs.SchemaError):
        s.outbox_span(event_id="e", effect_id="", accept_context=CTX,
                      attempt=1, outcome="ok")


def test_un_span_normal_SI_puede_anidar():
    """⊖ que da valor al anterior: la prohibición es del outbox, no del anidado."""
    s, exp = _sensor()
    s.span("coordination.event.accept", parent=CTX)
    assert exp.spans()[0].attributes["parent_span_id"] == CTX["span_id"]


# ─────────────────────────────────────────────────────────────────────────────
# Adaptador · la convención ajena va pinneada y detrás de una junta
# ─────────────────────────────────────────────────────────────────────────────
def test_el_sitio_de_llamada_no_escribe_nombres_de_otel():
    s, exp = _sensor(adapter=obs.OtelSemconvAdapter())
    s.count("events.accepted")
    m = exp.metrics()[0]
    assert m.resource["otel.semconv.version"] == obs.OTEL_SEMCONV_PIN
    assert m.resource["llminbox.schema"].startswith("otel.semconv/")


def test_cambiar_de_adaptador_no_cambia_el_sitio_de_llamada():
    a, ea = _sensor(adapter=obs.LlminboxV1Adapter())
    b, eb = _sensor(adapter=obs.OtelSemconvAdapter())
    a.count("events.accepted", lane="llminbox")
    b.count("events.accepted", lane="llminbox")
    assert ea.metrics()[0].nombre == eb.metrics()[0].nombre == "llminbox.events.accepted"
    assert ea.metrics()[0].resource["llminbox.schema"] != \
        eb.metrics()[0].resource["llminbox.schema"]


# ─────────────────────────────────────────────────────────────────────────────
# No-op determinista
# ─────────────────────────────────────────────────────────────────────────────
def test_el_no_op_es_DETERMINISTA_y_no_crece():
    a, b = obs.disabled(), obs.disabled()
    for _ in range(100):
        a.count("events.accepted", lane="llminbox")
        b.count("events.accepted", lane="llminbox")
    assert a.stats.emitidas == b.stats.emitidas == 0
    assert isinstance(a.exporter, obs.NullExporter)
    assert obs.NullExporter().export(obs.Signal("metric", "x", 1)) is \
        obs.ExportResult.NOT_EXPORTED


# ─────────────────────────────────────────────────────────────────────────────
# F-SDK · integración con el SDK REAL, en memoria, con force_flush
# ─────────────────────────────────────────────────────────────────────────────
# Todo lo de arriba mide contra un exportador de mentira, y eso deja una clase
# entera sin cubrir: que lo que le entregamos al SDK sea algo que el SDK ACEPTA y
# rinde con la forma que prometemos. Un doble acepta cualquier cosa; el SDK no.
# ⛔ AQUÍ NO HAY `importorskip`, Y ES LA CORRECCIÓN QUE MOTIVA ESTE BLOQUE.
# Con él, un CI que instalara sólo `requirements-test.txt` se saltaba la
# integración ENTERA y salía VERDE: el gate acreditaba «M3 pasa» sin ejecutar un
# solo caso contra el SDK real. Un salto silencioso es la forma más barata de que
# un gate certifique cualquier cosa. Sin SDK, este fichero revienta al importar,
# que es lo que tiene que pasar. El extra es OBLIGATORIO en el job de M3.
import opentelemetry.sdk as _otel_sdk           # noqa: F401  (import DURO a propósito)
from opentelemetry.sdk._logs.export import SimpleLogRecordProcessor

# `InMemoryLogRecordExporter` es el nombre vigente en 1.44; el viejo
# `InMemoryLogExporter` sigue existiendo pero emite `DeprecationWarning`, y esta
# suite corre con `-W error::DeprecationWarning`: usar el viejo la pondría roja por
# el arnés y no por el sujeto.
try:
    from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter as _LogExp
except ImportError:                              # SDK anterior a 1.44
    from opentelemetry.sdk._logs.export import InMemoryLogExporter as _LogExp

SDK_FIJADO = "1.44.0"
CASOS_SDK_ESPERADOS = 34


def _pipeline_real(ident, *, con_procesador=True, con_reader=True, con_log_proc=True,
                   span_exporter=None, registro=None, identity_budget=None):
    """Monta los tres proveedores con exportadores EN MEMORIA y el Resource nuestro."""
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry.sdk._logs import LoggerProvider

    res = Resource.create(obs.LlminboxV1Adapter().resource_attributes(ident))
    sp_exp = span_exporter if span_exporter is not None else InMemorySpanExporter()
    tp = TracerProvider(resource=res)
    if con_procesador:
        env = obs.instrumenta_exportador(sp_exp, registro) if registro else sp_exp
        tp.add_span_processor(SimpleSpanProcessor(env))
    reader = InMemoryMetricReader()
    mp = MeterProvider(resource=res, metric_readers=[reader] if con_reader else [])
    lg_exp = _LogExp()
    lp = LoggerProvider(resource=res)
    if con_log_proc:
        # El registro envuelve TAMBIÉN el exportador de logs: si sólo instrumentara
        # spans, un test que emite sólo un log tendría el registro a cero y el
        # veredicto honesto sería `ACCEPTED_BY_SDK` — o sea que el canal de logs
        # quedaría sin acreditar mientras el de spans dice `EXPORTED`.
        env_log = obs.instrumenta_exportador(lg_exp, registro) if registro else lg_exp
        lp.add_log_record_processor(SimpleLogRecordProcessor(env_log))
    b = obs.build_bundle(ident, tracer_provider=tp, meter_provider=mp,
                         logger_provider=lp, registro=registro,
                         identity_budget=identity_budget)
    return b, sp_exp, reader, lg_exp


def _recursos_vistos(reader, spans, logs) -> set:
    """Los Resource REALES que salieron por los tres exportadores.

    Se leen del dato, no del bundle: el Resource lo fija el provider al construirse y
    es lo que de verdad viaja. Preguntárselo al objeto que lo compone sería preguntarle
    al acusado.
    """
    vistos = set()
    datos = reader.get_metrics_data()
    if datos is not None:
        for rm in datos.resource_metrics:
            vistos.add(rm.resource.attributes.get("service.instance.id"))
    for sp in spans.get_finished_spans():
        vistos.add(sp.resource.attributes.get("service.instance.id"))
    for lr in logs.get_finished_logs():
        # 1.44 devuelve `ReadableLogRecord`, con el Resource ARRIBA. En 1.38 colgaba de
        # `.log_record`; leerlo del sitio viejo da `AttributeError`, no un valor malo —
        # que al menos es ruidoso.
        vistos.add(lr.resource.attributes.get("service.instance.id"))
    vistos.discard(None)
    return vistos


def _emite_las_seis(sensor):
    """Las SEIS señales del contrato, por sus seis puertas públicas."""
    # Nombres del VOCABULARIO CERRADO, no inventados: con nombres libres el rechazo
    # llegaría por `SchemaError` y el test mediría el gate de nombres, no el de
    # identidad — verde o rojo por el motivo equivocado.
    return [sensor.count("events.accepted"),
            sensor.gauge("outbox.pending", 1.0),
            sensor.observe("events.accept_duration_s", 0.5),
            sensor.log("policy.denied"),
            sensor.span("coordination.event.accept"),
            sensor.outbox_span(event_id="e1", effect_id="f1",
                               accept_context={}, attempt=1, outcome="ok")]


@pytest.mark.parametrize("estricto", [False, True])
def test_TECHO_1_una_segunda_identidad_no_emite_NINGUNA_de_las_seis(estricto):
    """M3-7 · el falsificador que da nombre a la corrección, contra el SDK REAL.

    El P0 era que el presupuesto de identidad sólo gobernaba métricas: logs, spans y
    `outbox_span` salían por debajo con SU Resource, así que con techo 1 el cable veía
    N identidades. Un techo que mira un tercio de las señales no acota: reparte.

    Aquí la segunda identidad intenta LAS SEIS y no puede quedar nada suyo en ningún
    exportador — ni un dato ni un Resource. Y la primera tiene que seguir viva: una
    puerta que cierra a todos no es una puerta, es un apagón.
    """
    bud = obs.CardinalityBudget(max_series=1)
    id1 = _ident(runtime="rt-0001")
    id2 = _ident(runtime="rt-0002")
    b1, sp1, rd1, lg1 = _pipeline_real(id1, identity_budget=bud)
    b2, sp2, rd2, lg2 = _pipeline_real(id2, identity_budget=bud)

    s1 = obs.build(id1, obs.Trust.GATEWAY, enabled=True, bundle=b1,
                   identity_budget=bud, strict=estricto)
    assert all(r is not obs.ExportResult.NOT_EXPORTED for r in _emite_las_seis(s1)), (
        "la PRIMERA identidad, la admitida, no llegó a emitir: el techo cerró a todos")

    if estricto:
        # ESTRICTO: el error TIPADO, y en construcción — antes de que exista un sensor
        # capaz de emitir. Fallar al primer `count()` dejaría vivo un objeto que ya
        # tiene providers y Resource propios.
        with pytest.raises(obs.CardinalityBudgetExceeded):
            obs.build(id2, obs.Trust.GATEWAY, enabled=True, bundle=b2,
                      identity_budget=bud, strict=True)
    else:
        s2 = obs.build(id2, obs.Trust.GATEWAY, enabled=True, bundle=b2,
                       identity_budget=bud, strict=False)
        assert all(r is obs.ExportResult.NOT_EXPORTED for r in _emite_las_seis(s2)), (
            "alguna de las seis señales salió por debajo de la puerta de identidad")
        assert s2.stats.descartadas == 6, (
            f"descartes contabilizados: {s2.stats.descartadas}, esperados 6 — un drop "
            "que no se cuenta es indistinguible de no haber intentado nada")
        assert s2.stats.identidad_desbordes == 1

    # NI UN DATO NI UN RESOURCE NUEVO, en ninguno de los seis exportadores.
    assert rd2.get_metrics_data() is None or not rd2.get_metrics_data().resource_metrics
    assert sp2.get_finished_spans() == ()
    assert lg2.get_finished_logs() == ()
    vistos = _recursos_vistos(rd1, sp1, lg1) | _recursos_vistos(rd2, sp2, lg2)
    esperado = obs.LlminboxV1Adapter().resource_attributes(id1)["service.instance.id"]
    assert vistos == {esperado}, (
        f"Resources en el cable: {vistos}. Con techo 1 sólo puede viajar uno; si "
        f"aparece el de `rt-0002`, el diseño sigue permitiendo Resources nuevos")

    # Y LA PRIMERA SIGUE FUNCIONANDO DESPUÉS del rechazo de la segunda.
    assert s1.count("events.accepted") is not obs.ExportResult.NOT_EXPORTED


def test_la_MISMA_identidad_reutiliza_cupo_y_no_lo_gasta():
    """CONTROL POSITIVO del techo: si reconectar consumiera cupo, un servicio que se
    reinicia se apagaría solo a la segunda, y el falsificador de arriba pasaría por el
    motivo equivocado."""
    bud = obs.CardinalityBudget(max_series=1)
    ident = _ident(runtime="rt-0001")
    for _ in range(3):
        b, *_ = _pipeline_real(ident, identity_budget=bud)
        s = obs.build(ident, obs.Trust.GATEWAY, enabled=True, bundle=b,
                      identity_budget=bud)
        assert s.count("events.accepted") is not obs.ExportResult.NOT_EXPORTED
    assert bud.activas == 1


@pytest.mark.parametrize("estricto", [False, True])
def test_nacer_APAGADO_y_encenderse_por_atributo_no_esquiva_el_techo(estricto):
    """`enabled` es un atributo PÚBLICO y mutable.

    Con la admisión decidida sólo en construcción y `_identidad_admitida = True` cuando
    el sensor nacía apagado, esto bastaba para saltarse el presupuesto ENTERO sin
    tocarlo: construir con `enabled=False` y hacer `s.enabled = True` a continuación.
    El techo se esquivaba cambiando una bandera, no gastando cupo.

    La admisión es PEREZOSA y fail-closed: si no se decidió al construir, se decide en
    la primera señal.
    """
    bud = obs.CardinalityBudget(max_series=1)
    id1, id2 = _ident(runtime="rt-0001"), _ident(runtime="rt-0002")
    b1, sp1, rd1, lg1 = _pipeline_real(id1, identity_budget=bud)
    b2, sp2, rd2, lg2 = _pipeline_real(id2, identity_budget=bud)

    s1 = obs.build(id1, obs.Trust.GATEWAY, enabled=True, bundle=b1, identity_budget=bud)
    assert s1.count("events.accepted") is not obs.ExportResult.NOT_EXPORTED

    # Nace APAGADO: no se decide nada, y por eso no se gasta ni se regala cupo.
    # EXPORTADOR REAL Y EXPLÍCITO. Con `enabled=False` y sin él, `build` inyecta un
    # `NullExporter` y este test saldría verde porque no hay por dónde emitir — no
    # porque la puerta de identidad funcione. El falso verde lo cazó su propio control
    # positivo, no yo.
    s2 = obs.build(id2, obs.Trust.GATEWAY, enabled=False, bundle=b2,
                   exporter=obs.OtelExporter(b2), identity_budget=bud, strict=estricto)
    assert s2._identidad_admitida is None, (
        "decidió con el sensor apagado: `True` aquí es la puerta trasera entera")

    s2.enabled = True                      # el atributo público, sin pasar por `build`
    if estricto:
        with pytest.raises(obs.CardinalityBudgetExceeded):
            s2.count("events.accepted")
    else:
        assert all(r is obs.ExportResult.NOT_EXPORTED for r in _emite_las_seis(s2)), (
            "encender por atributo esquivó el presupuesto de identidad")
        assert s2.stats.descartadas == 6

    assert rd2.get_metrics_data() is None or not rd2.get_metrics_data().resource_metrics
    assert sp2.get_finished_spans() == () and lg2.get_finished_logs() == ()
    esperado = obs.LlminboxV1Adapter().resource_attributes(id1)["service.instance.id"]
    assert (_recursos_vistos(rd1, sp1, lg1) | _recursos_vistos(rd2, sp2, lg2)) == {esperado}


def test_nacer_apagado_y_encenderse_SI_admite_cuando_hay_cupo():
    """CONTROL POSITIVO. Si la admisión perezosa negara siempre, el test de arriba
    pasaría por el motivo equivocado y un sensor legítimo quedaría mudo."""
    bud = obs.CardinalityBudget(max_series=2)
    ident = _ident(runtime="rt-0009")
    b, sp, rd, lg = _pipeline_real(ident, identity_budget=bud)
    s = obs.build(ident, obs.Trust.GATEWAY, enabled=False, bundle=b,
                  exporter=obs.OtelExporter(b), identity_budget=bud)
    s.enabled = True
    assert s.count("events.accepted") is not obs.ExportResult.NOT_EXPORTED
    assert s.stats.descartadas == 0


def test_build_RECHAZA_una_identidad_distinta_de_la_del_bundle():
    """El Resource lo fijan los providers del bundle: con identidades cruzadas la señal
    sale atribuida a la otra, y las dos mitades del dato se contradicen."""
    b, *_ = _pipeline_real(_ident(runtime="rt-0001"))
    with pytest.raises(obs.IdentityMismatch):
        obs.build(_ident(runtime="rt-0002"), obs.Trust.GATEWAY, enabled=True, bundle=b)


def test_con_pipeline_REAL_el_estado_es_READY():
    b, *_ = _pipeline_real(_ident())
    assert b.resource_verificado is True
    assert b.salida_verificada is True
    assert b.state is obs.Pipeline.READY


@pytest.mark.parametrize("falta", ["procesador", "reader", "log_proc"])
def test_un_proveedor_SIN_salida_nunca_es_READY(falta):
    """⊖ que da sentido al ⊕ de arriba: un provider bien construido y sin nada
    colgado se traga las señales en silencio. Es el `sdk_present_no_exporter` de
    antes, pero medido por ESTRUCTURA y no por el nombre de la clase."""
    kw = {"con_procesador": falta != "procesador",
          "con_reader": falta != "reader",
          "con_log_proc": falta != "log_proc"}
    b, *_ = _pipeline_real(_ident(), **kw)
    assert b.salida_verificada is False
    assert b.state is obs.Pipeline.PIPELINE_UNVERIFIED


def test_un_Resource_que_NO_es_el_nuestro_nunca_es_READY():
    """Si el Resource no declara nuestra identidad, las señales salen atribuidas a
    otro workload — la avería que la identidad server-derived cierra arriba,
    reaparecida abajo, donde nadie mira."""
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry.sdk._logs import LoggerProvider
    ajeno = Resource.create({"service.name": "otro", "llminbox.principal": "otro"})
    tp = TracerProvider(resource=ajeno)
    tp.add_span_processor(SimpleSpanProcessor(InMemorySpanExporter()))
    mp = MeterProvider(resource=ajeno, metric_readers=[InMemoryMetricReader()])
    lp = LoggerProvider(resource=ajeno)
    lp.add_log_record_processor(SimpleLogRecordProcessor(_LogExp()))
    b = obs.build_bundle(_ident(), tracer_provider=tp, meter_provider=mp,
                         logger_provider=lp)
    assert b.resource_verificado is False
    assert b.state is obs.Pipeline.PIPELINE_UNVERIFIED


def test_el_SDK_real_recibe_span_con_RESOURCE_LINK_y_PADRE():
    ident = _ident()
    reg = obs.RegistroDeEntrega()
    b, sp_exp, _, _ = _pipeline_real(ident, registro=reg)
    s = obs.build(ident, obs.Trust.GATEWAY, enabled=True, bundle=b)
    assert s.state is obs.Pipeline.READY
    r = s.outbox_span(event_id="evt-1", effect_id="eff-1", accept_context=CTX,
                      attempt=2, outcome="materialized")
    assert r is obs.ExportResult.ACCEPTED_BY_SDK
    assert s.exporter.flush() is obs.ExportResult.EXPORTED

    spans = sp_exp.get_finished_spans()
    assert len(spans) == 1, spans
    sp = spans[0]
    assert sp.name == "outbox.materialize"
    assert sp.resource.attributes["llminbox.principal"] == ident.principal
    assert len(sp.links) == 1, "el Link no llegó al SDK"
    assert f"{sp.links[0].context.span_id:016x}" == CTX["span_id"]
    assert sp.attributes["effect_id"] == "eff-1"
    assert sp.parent is None, "outbox.materialize no puede tener padre"


def test_el_SDK_real_distingue_COUNTER_de_GAUGE_y_de_HISTOGRAM():
    """Emitir los tres con `add` daría números plausibles y equivocados: una medida
    puntual presentada como suma es el error más caro de un panel."""
    ident = _ident()
    b, _, reader, _ = _pipeline_real(ident)
    s = obs.build(ident, obs.Trust.GATEWAY, enabled=True, bundle=b)
    s.count("events.accepted", verb="inform")
    s.gauge("outbox.pending", 7)
    s.observe("events.accept_duration_s", 0.25, verb="inform")
    datos = reader.get_metrics_data()
    tipos = {}
    for rm in datos.resource_metrics:
        assert rm.resource.attributes["llminbox.lane"] == ident.lane
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                tipos[m.name] = type(m.data).__name__
    assert tipos["llminbox.events.accepted"] == "Sum"
    assert tipos["llminbox.outbox.pending"] == "Gauge"
    assert tipos["llminbox.events.accept_duration_s"] == "Histogram"


def test_el_SDK_real_recibe_un_LOG_de_verdad_no_un_span():
    ident = _ident()
    reg = obs.RegistroDeEntrega()
    b, sp_exp, _, lg_exp = _pipeline_real(ident, registro=reg)
    s = obs.build(ident, obs.Trust.GATEWAY, enabled=True, bundle=b)
    s.log("policy.denied", reason="lane_mismatch")
    assert s.exporter.flush() is obs.ExportResult.EXPORTED
    logs = lg_exp.get_finished_logs()
    assert len(logs) == 1, "el log no llegó por el canal de logs"
    assert logs[0].log_record.body == "policy.denied"
    assert sp_exp.get_finished_spans() == (), "un log salió como span"


def test_ni_el_SDK_real_deja_salir_un_secreto_anidado():
    """El mismo barrido, contra el exportador de verdad: si la redacción se
    apoyara en el doble, aquí se vería."""
    ident = _ident()
    b, sp_exp, _, _ = _pipeline_real(ident)
    s = obs.build(ident, obs.Trust.GATEWAY, enabled=True, bundle=b)
    s.span("runtime.job", attributes=ANIDADO)
    s.exporter.flush()
    texto = "".join(f"{k}={v}" for k, v in sp_exp.get_finished_spans()[0].attributes.items())
    for i in range(1, 6):
        assert f"sk-ANIDADO-{i}" not in texto


def test_un_contexto_MALFORMADO_tira_el_enlace_y_no_la_senal():
    """⊖ de la cura: el span correcto tiene que salir igual, sin enlace y sin
    acusar al exportador de una avería que puso el llamante."""
    ident = _ident()
    b, sp_exp, _, _ = _pipeline_real(ident)
    s = obs.build(ident, obs.Trust.GATEWAY, enabled=True, bundle=b)
    r = s.outbox_span(event_id="e", effect_id="f",
                      accept_context={"trace_id": "no-es-hex", "span_id": "zz"},
                      attempt=1, outcome="ok")
    assert r is obs.ExportResult.ACCEPTED_BY_SDK
    s.exporter.flush()
    spans = sp_exp.get_finished_spans()
    assert len(spans) == 1 and spans[0].links == ()
    assert s.state is obs.Pipeline.READY, "se marcó degradado por culpa del llamante"


def test_un_CONTENIDO_que_envuelve_un_secreto_no_publica_SHA_estable():
    """El digest se pide (`content_digest`) y aun así NO sale, porque el barrido
    previo encuentra la credencial dentro. Un sha256 estable de algo que contiene
    un secreto es el mismo oráculo de confirmación con envoltorio inocente."""
    s, exp = _sensor(content_digest=True)
    s.span("runtime.job", attributes={
        "body": {"headers": {"authorization": "Bearer sk-ENVUELTO"}}})
    volcado = exp.volcado()
    assert "body_sha256" not in volcado, "publicó digest estable sobre un secreto"
    assert "body_digest_omitido=secreto_anidado" in volcado
    assert "sk-ENVUELTO" not in volcado


def test_un_CONTENIDO_limpio_SI_publica_su_digest():
    """⊖: si el digest no saliera nunca, el test de arriba no mediría el barrido."""
    s, exp = _sensor(content_digest=True)
    s.span("runtime.job", attributes={"body": {"texto": "sin nada dentro"}})
    assert "body_sha256" in exp.volcado()


# ─────────────────────────────────────────────────────────────────────────────
# F-RESOURCE · el presupuesto de identidad cuenta lo que VIAJA
# ─────────────────────────────────────────────────────────────────────────────
def test_el_presupuesto_de_identidad_cuenta_el_RESOURCE_no_los_cuatro_campos():
    """Dos sensores con la MISMA tupla (principal, role, lane, runtime) y distinta
    `credential_generation` son DOS Resource en el cable y dos series en el
    backend. Contando sólo la tupla, el presupuesto decía «1 identidad» mientras el
    receptor veía N: acotaba una cosa distinta de la que se factura."""
    exp = obs.MemoryExporter()
    b = obs.CardinalityBudget(max_series=100)
    for gen in range(10):
        ident = obs.Identity.server_derived(
            principal="backend", role="be", lane="llminbox",
            runtime_instance="rt-0001", credential_generation=gen)
        obs.build(ident, obs.Trust.GATEWAY, enabled=True, exporter=exp,
                  identity_budget=b).count("events.accepted")
    assert b.activas == 10, (
        f"el presupuesto vio {b.activas} identidades y en el cable van 10 Resource")


def test_el_MISMO_resource_no_gasta_presupuesto_dos_veces():
    """⊖ que acota: si cada sensor gastara una ranura, el presupuesto contaría
    OBJETOS y no identidades, y se agotaría por reconexiones del mismo agente."""
    exp = obs.MemoryExporter()
    b = obs.CardinalityBudget(max_series=100)
    for _ in range(10):
        obs.build(_ident(), obs.Trust.GATEWAY, enabled=True, exporter=exp,
                  identity_budget=b).count("events.accepted")
    assert b.activas == 1


def test_el_resource_del_presupuesto_es_el_que_EMITE_el_adaptador():
    """Si el adaptador añade un atributo, entra en la cuenta sin tocar nada más:
    la clave se deriva de `resource_attributes`, no de una lista paralela que
    alguien tendría que acordarse de actualizar."""
    s, _ = _sensor()
    clave = dict(s._clave_resource())
    emitido = s.adapter.resource_attributes(s.identity)
    assert set(clave) == {str(k) for k in emitido}


def test_el_camino_de_LOG_no_usa_la_clase_que_se_va():
    """El log se emite por KWARGS, no construyendo `LogRecord`.

    `LogRecord` desaparece en 1.39.0. Si el camino dependiera de él, la subida no
    daría un error visible: `_log` reventaría, `_emit` se tragaría la excepción y
    la volvería `NOT_EXPORTED`, o sea que los logs se apagarían EN SILENCIO
    mientras spans y métricas siguen saliendo. Este test comprueba que el módulo
    no menciona la clase y que el log llega igual.
    """
    import pathlib
    fuente = pathlib.Path(obs.__file__).read_text(encoding="utf-8")
    assert "LogRecord(" not in fuente, (
        "el camino de logs volvió a construir `LogRecord`, que desaparece en 1.39")
    ident = _ident()
    b, _, _, lg_exp = _pipeline_real(ident)
    s = obs.build(ident, obs.Trust.GATEWAY, enabled=True, bundle=b)
    assert s.log("runtime.start") is obs.ExportResult.ACCEPTED_BY_SDK
    s.exporter.flush()
    assert len(lg_exp.get_finished_logs()) == 1


# ─────────────────────────────────────────────────────────────────────────────
# P0 · `force_flush` es una BARRERA, no un acuse
# ─────────────────────────────────────────────────────────────────────────────
class _ExportadorQueFALLA:
    """Exportador de spans que contesta FAILURE siempre. Es el caso real: un
    colector caído, un 5xx, un TLS roto."""
    def __init__(self):
        self.intentos = 0

    def export(self, spans):
        from opentelemetry.sdk.trace.export import SpanExportResult
        self.intentos += 1
        return SpanExportResult.FAILURE

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis=30000):
        return True


def test_un_exporter_que_FALLA_no_puede_acabar_en_EXPORTED():
    """El P0 del auditor, reproducido y cerrado.

    `SimpleSpanProcessor` IGNORA el `SpanExportResult.FAILURE` —no lo propaga, no
    lo cuenta— y `provider.force_flush()` devuelve `True` igualmente, porque su
    contrato es «vacié la cola», no «el otro lado lo aceptó». Antes esto daba
    `state=ready`, `emit=accepted_by_sdk` y `flush=exported`: tres verdes sobre
    cero entregas.
    """
    ident = _ident()
    reg = obs.RegistroDeEntrega()
    falla = _ExportadorQueFALLA()
    b, _, _, _ = _pipeline_real(ident, span_exporter=falla, registro=reg)
    s = obs.build(ident, obs.Trust.GATEWAY, enabled=True, bundle=b)
    assert s.span("runtime.job") is obs.ExportResult.ACCEPTED_BY_SDK
    assert falla.intentos >= 1, "el arnés no ejercitó el exportador"
    assert reg.fallos >= 1, "el registro no conservó el FAILURE"
    assert s.exporter.flush() is obs.ExportResult.NOT_EXPORTED
    assert s.state is obs.Pipeline.DEGRADED


def test_sin_REGISTRO_el_techo_es_ACCEPTED_BY_SDK_no_EXPORTED():
    """⊖ que fija el límite: `force_flush` sin nadie que conserve el resultado del
    otro extremo no puede acreditar entrega. «No se sabe» no cae del lado bueno."""
    ident = _ident()
    b, _, _, _ = _pipeline_real(ident)          # sin registro
    s = obs.build(ident, obs.Trust.GATEWAY, enabled=True, bundle=b)
    s.span("runtime.job")
    assert s.exporter.flush() is obs.ExportResult.ACCEPTED_BY_SDK


def test_con_REGISTRO_y_entrega_BUENA_si_hay_EXPORTED():
    """⊕ sin el cual los dos de arriba pasarían con un `flush` que nunca acredita."""
    ident = _ident()
    reg = obs.RegistroDeEntrega()
    b, _, _, _ = _pipeline_real(ident, registro=reg)
    s = obs.build(ident, obs.Trust.GATEWAY, enabled=True, bundle=b)
    s.span("runtime.job")
    assert reg.ok >= 1 and reg.fallos == 0
    assert s.exporter.flush() is obs.ExportResult.EXPORTED
    assert s.state is obs.Pipeline.READY


# ─────────────────────────────────────────────────────────────────────────────
# F-WIRE · el presupuesto acota lo que VIAJA, no un contador nuestro
# ─────────────────────────────────────────────────────────────────────────────
def test_al_desbordar_se_DESCARTA_antes_del_sink_si_no_hay_agregado():
    """Mi cura anterior emitía un `Signal.resource` colapsado, y eso sólo lo miraba
    el exportador de mentira: por el camino real el Resource lo FIJAN los providers
    al construirse y no cambia por señal, así que salía el ORIGINAL. La «cura»
    funcionaba exactamente donde no importaba.

    Sin sink agregado explícito, lo honesto es DROP antes del sink: el hecho se
    pierde y se cuenta, en vez de emitirse con una atribución que miente.
    """
    exp = obs.MemoryExporter()
    bud = obs.CardinalityBudget(max_series=2)
    sensores = []
    for g in range(5):
        i = obs.Identity.server_derived(principal="backend", role="be",
                                        lane="llminbox", runtime_instance=f"rt-{g}",
                                        credential_generation=g)
        s = obs.build(i, obs.Trust.GATEWAY, enabled=True, exporter=exp,
                      identity_budget=bud)
        sensores.append(s)
        s.count("events.accepted")
    wire = {tuple(sorted(m.resource.items())) for m in exp.metrics()}
    assert len(exp.metrics()) == 2, "salió al cable algo por encima del techo"
    assert len(wire) == 2, f"Resource en el cable: {len(wire)}, techo 2"
    assert sum(s.stats.descartadas for s in sensores) == 3
    assert all("identity_overflow" not in dict(w) for w in wire)


def test_overflow_sink_YA_NO_EXISTE_y_el_desborde_es_DROP():
    """El parámetro se retiró, y no por simplificar: era un `Exporter` genérico y sin
    acreditar, así que el llamante podía pasarle el MISMO exportador de la identidad
    admitida y reabrir el bypass entero por la puerta que se abrió para cerrarlo.

    Para v0.9 la conducta segura es DROP contabilizado. Un destino agregado de verdad
    necesita su propio bundle tipado con Resource propio VERIFICADO, y queda diferido.
    """
    exp = obs.MemoryExporter()
    bud = obs.CardinalityBudget(max_series=1)
    with pytest.raises(TypeError):
        obs.build(_ident(), obs.Trust.GATEWAY, enabled=True, exporter=exp,
                  identity_budget=bud, overflow_sink=obs.MemoryExporter())

    # Y el DROP se cuenta, que es lo que distingue «no emitió» de «no pasó nada».
    for g in range(3):
        i = obs.Identity.server_derived(principal="backend", role="be",
                                        lane="llminbox", runtime_instance=f"rt-{g}",
                                        credential_generation=g)
        s = obs.build(i, obs.Trust.GATEWAY, enabled=True, exporter=exp,
                      identity_budget=bud)
        s.count("events.accepted")
    assert len(exp.metrics()) == 1, "una identidad por encima del techo emitió"


def test_el_presupuesto_de_identidad_se_COMPARTE_en_el_pipeline():
    """Un cupo por sensor no acota nada: el coste lo paga el receptor, que ve la
    suma de todos. El eje se comparte donde se comparte el destino."""
    ident = _ident()
    bud = obs.CardinalityBudget(max_series=3)
    b, _, _, _ = _pipeline_real(ident, identity_budget=bud)
    s1 = obs.build(ident, obs.Trust.GATEWAY, enabled=True, bundle=b)
    s2 = obs.build(ident, obs.Trust.SUPERVISOR, enabled=True, bundle=b)
    assert s1.identity_budget is bud and s2.identity_budget is bud


# ─────────────────────────────────────────────────────────────────────────────
# GATE DE WARNINGS · específico de NUESTRO camino, sin tapar los ajenos
# ─────────────────────────────────────────────────────────────────────────────
def test_cero_warnings_de_OTel_o_del_modulo_en_el_camino_completo(recwarn):
    """`-W error::DeprecationWarning` a secas no sirve de gate aquí y hay dos
    motivos medidos, en direcciones opuestas:

      · SE PASA DE LARGO: el aviso que teníamos (`LogDeprecatedInitWarning`)
        hereda de `UserWarning`, no de `DeprecationWarning` — el flag no lo habría
        cazado nunca.
      · SE PASA DE FRENADA: `starlette`/`anyio` emiten su propia
        `DeprecationWarning` al importar el TestClient, ajena a este módulo, y
        poner el flag global tumbaba la suite por una dependencia de terceros.

    Así que el gate es por ORIGEN: cero avisos que salgan de `observability.py` o
    de `opentelemetry`. Los ajenos NO se ocultan —siguen apareciendo en el
    reporte de pytest— pero no deciden este veredicto, porque no son nuestros y
    cambiarlos a ciegas sería tocar dependencias que no estamos auditando.
    """
    import pathlib
    ident = _ident()
    reg = obs.RegistroDeEntrega()
    b, _, _, _ = _pipeline_real(ident, registro=reg)
    s = obs.build(ident, obs.Trust.GATEWAY, enabled=True, bundle=b)
    s.count("events.accepted", verb="inform")
    s.gauge("outbox.pending", 3)
    s.observe("events.accept_duration_s", 0.1)
    s.log("policy.denied", reason="expired")
    s.outbox_span(event_id="e", effect_id="f", accept_context=CTX, attempt=1,
                  outcome="materialized")
    s.exporter.flush()

    nuestros = [w for w in recwarn.list
                if "opentelemetry" in str(w.filename)
                or pathlib.Path(obs.__file__).name in str(w.filename)]
    assert nuestros == [], (
        "avisos en nuestro camino: "
        + "; ".join(f"{w.category.__name__} en {w.filename}:{w.lineno}" for w in nuestros))


# ─────────────────────────────────────────────────────────────────────────────
# M3-6 · lo que el SDK REAL recibe, inspeccionado
# ─────────────────────────────────────────────────────────────────────────────
def _prov(ident, **over):
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter)
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry.sdk._logs import LoggerProvider
    a = obs.LlminboxV1Adapter().resource_attributes(ident)
    a.update(over)
    res = Resource.create(a)
    tp = TracerProvider(resource=res)
    tp.add_span_processor(SimpleSpanProcessor(InMemorySpanExporter()))
    rd = InMemoryMetricReader()
    mp = MeterProvider(resource=res, metric_readers=[rd])
    lp = LoggerProvider(resource=res)
    lp.add_log_record_processor(SimpleLogRecordProcessor(_LogExp()))
    return tp, mp, lp, rd


def test_un_bundle_de_OTRA_identidad_se_RECHAZA():
    """Medido antes de curar: bundle `lane-a` + sensor `lane-b` emitía
    `resource.llminbox.lane=lane-a` con `datapoint.lane=lane-b`. El aislamiento por
    carril roto, y las dos mitades del mismo dato contradiciéndose."""
    a = _ident(lane="lane-a")
    b_ident = obs.Identity.server_derived(principal="backend", role="be",
                                          lane="lane-b", runtime_instance="rt-0001")
    tp, mp, lp, _ = _prov(a)
    bundle = obs.build_bundle(a, tracer_provider=tp, meter_provider=mp,
                              logger_provider=lp)
    with pytest.raises(obs.IdentityMismatch):
        obs.build(b_ident, obs.Trust.GATEWAY, enabled=True, bundle=bundle)


def test_el_bundle_de_SU_identidad_SI_construye():
    """⊖: si ninguna combinación pasara, el test de arriba no mediría nada."""
    a = _ident(lane="lane-a")
    tp, mp, lp, _ = _prov(a)
    bundle = obs.build_bundle(a, tracer_provider=tp, meter_provider=mp,
                              logger_provider=lp)
    assert obs.build(a, obs.Trust.GATEWAY, enabled=True, bundle=bundle).state \
        is obs.Pipeline.READY


@pytest.mark.parametrize("clave,valor", [
    ("llminbox.role", "cto"),
    ("llminbox.credential_generation", 999),
    ("llminbox.schema", "falso"),
    ("llminbox.lane", "otro"),
    ("llminbox.principal", "otro"),
    ("service.instance.id", "rt-9999"),
])
def test_un_Resource_con_UN_campo_forjado_nunca_es_READY(clave, valor):
    """Comparar sólo instancia, principal y carril dejaba pasar `role` forjado,
    `credential_generation=999` y `schema` falso — medido: READY con los tres
    mentidos. El Resource es la atribución de TODA señal que sale."""
    ident = _ident()
    tp, mp, lp, _ = _prov(ident, **{clave: valor})
    b = obs.build_bundle(ident, tracer_provider=tp, meter_provider=mp,
                         logger_provider=lp)
    assert b.resource_verificado is False
    assert b.state is obs.Pipeline.PIPELINE_UNVERIFIED


def test_un_atributo_autoritativo_de_MAS_tampoco_es_READY():
    """Un `llminbox.*` que nosotros no emitimos es alguien añadiendo atribución por
    su cuenta, y el consumidor no puede saber cuál de las dos manos la puso."""
    ident = _ident()
    tp, mp, lp, _ = _prov(ident, **{"llminbox.inventado": "x"})
    assert obs.build_bundle(ident, tracer_provider=tp, meter_provider=mp,
                            logger_provider=lp).state is obs.Pipeline.PIPELINE_UNVERIFIED


def test_el_Resource_RECIBIDO_por_el_SDK_casa_con_las_etiquetas():
    """Se inspecciona lo que LLEGA, no lo que creemos mandar: Resource del
    datapoint y etiqueta de carril tienen que decir lo mismo."""
    ident = _ident()
    tp, mp, lp, rd = _prov(ident)
    b = obs.build_bundle(ident, tracer_provider=tp, meter_provider=mp,
                         logger_provider=lp)
    s = obs.build(ident, obs.Trust.GATEWAY, enabled=True, bundle=b)
    s.count("events.accepted", verb="inform")
    vistos = 0
    for rm in rd.get_metrics_data().resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                for dp in m.data.data_points:
                    vistos += 1
                    assert dict(dp.attributes)["lane"] == \
                        rm.resource.attributes["llminbox.lane"] == ident.lane
                    assert rm.resource.attributes["llminbox.role"] == ident.role
    assert vistos == 1


def test_un_exportador_de_METRICAS_que_falla_con_timeout_kw_degrada():
    """La firma del instrumento no puede ser la suya: el exportador de métricas de
    1.44 se llama `export(data, timeout_millis=...)`, y con `export(datos)` el
    proxy reventaba con `TypeError`, el SDK se lo tragaba y el registro quedaba en
    0/0 — veredicto `ACCEPTED_BY_SDK` sobre un camino que no exportó nada."""
    from opentelemetry.sdk.metrics.export import MetricExportResult
    reg = obs.RegistroDeEntrega()

    class _MetricasQueFallan:
        def __init__(self):
            self.kw = None

        def export(self, data, timeout_millis=10_000, **kwargs):
            self.kw = timeout_millis
            return MetricExportResult.FAILURE

    inner = _MetricasQueFallan()
    env = obs.instrumenta_exportador(inner, reg)
    r = env.export({"datos": 1}, timeout_millis=1234)
    assert r is MetricExportResult.FAILURE
    assert inner.kw == 1234, "el proxy se comió el kwarg del SDK"
    assert (reg.ok, reg.fallos) == (0, 1)
    assert reg.veredicto() is obs.ExportResult.NOT_EXPORTED


def test_una_EXCEPCION_del_exportador_tambien_cuenta_como_fallo():
    """⊖ del anterior: dejarla pasar sin anotar repetiría el 0/0 por otra puerta."""
    reg = obs.RegistroDeEntrega()

    class _Revienta:
        def export(self, *a, **k):
            raise ConnectionError("colector caído")

    with pytest.raises(ConnectionError):
        obs.instrumenta_exportador(_Revienta(), reg).export([], timeout_millis=1)
    assert (reg.ok, reg.fallos) == (0, 1)


# ─────────────────────────────────────────────────────────────────────────────
# GATE DEL GATE · si los casos del SDK no se ejecutaron, esto tiene que ser ROJO
# ─────────────────────────────────────────────────────────────────────────────
def test_la_version_del_SDK_es_la_FIJADA():
    """No es celo de versión: la acreditación de salida mira atributos internos, y
    ésos se han movido entre menores (1.38 los tenía en `_sdk_config.metric_readers`,
    1.44 en `_metric_readers`). Correr contra otra versión mediría otro objeto."""
    import importlib.metadata as md
    assert md.version("opentelemetry-sdk") == SDK_FIJADO
    assert md.version("opentelemetry-api") == SDK_FIJADO


def test_los_casos_del_SDK_REAL_se_ejecutaron_todos():
    """CENSO, no confianza. Un `skip`, un `xfail`, un rename o un borrado dejarían
    la integración fuera sin poner nada rojo: el fichero seguiría «pasando» con
    menos casos, que es el estado que produce un gate capaz de certificar cualquier
    cosa.

    El número está ESCRITO A MANO a propósito: añadir un caso obliga a subirlo, y
    quitar uno tira este test.
    """
    import inspect
    import sys as _sys
    mod = _sys.modules[__name__]
    reales = [n for n, f in vars(mod).items()
              if n.startswith("test_") and callable(f)
              and n != "test_los_casos_del_SDK_REAL_se_ejecutaron_todos"
              and any(h in (inspect.getsource(f) or "")
                      for h in ("_pipeline_real(", "_prov("))]
    assert len(reales) == CASOS_SDK_ESPERADOS, (
        f"casos contra el SDK real: {len(reales)}, esperados {CASOS_SDK_ESPERADOS}. "
        f"{sorted(reales)}")


def test_force_flush_acredita_los_PROVIDERS_no_la_entrega_remota():
    """LÍMITE DEL `EXPORTED`, medido y no prometido.

    Acredita que los proveedores de ESTE proceso vaciaron su cola y que el
    exportador instrumentado confirmó. NO dice que saliera por la red, ni que un
    colector lo aceptara, ni que un backend lo indexara: para eso haría falta un
    acuse del otro extremo, que aquí no existe.
    """
    ident = _ident()
    reg = obs.RegistroDeEntrega()
    b, sp_exp, _, _ = _pipeline_real(ident, registro=reg)
    s = obs.build(ident, obs.Trust.GATEWAY, enabled=True, bundle=b)
    s.span("runtime.job")
    assert sp_exp.get_finished_spans() != (), "el span ni llegó al exportador local"
    assert s.exporter.flush() is obs.ExportResult.EXPORTED

    # ⊖ que fija el límite: un proveedor que no puede vaciar no puede dar EXPORTED.
    class _SinFlush:
        def force_flush(self, *a, **k):
            raise RuntimeError("este proveedor no puede vaciar")

    b.detalle["tracer_provider"] = _SinFlush()
    assert s.exporter.flush() is obs.ExportResult.NOT_EXPORTED


# ══════════════════════════════════════════════════════════════════════════════════
# Auditoría independiente de `ab719eb` · los cinco defectos, cada uno con su falsador
# ══════════════════════════════════════════════════════════════════════════════════

def test_outbox_no_gasta_cupo_APAGADO_y_si_lo_gasta_al_ENCENDER():
    """① `outbox_span` era la única puerta sin comprobar `enabled` antes de admitir.

    Con la admisión perezosa, un sensor APAGADO que llamara a `outbox_span` gastaba
    cupo sin emitir nada: el techo se consumía con señales que no existen y la
    identidad legítima siguiente se encontraba la puerta cerrada por un fantasma.

    Se mide sobre UN SOLO sensor y su TRANSICIÓN, que es donde vive el defecto. Mi
    primera versión usaba dos sensores parametrizados y tomaba el suelo DESPUÉS del
    `build`; con `enabled=True` la identidad ya se admite en construcción, así que el
    control positivo exigía un segundo consumo que no debe existir — medía la ventana
    equivocada y salía rojo contra el código correcto.
    """
    bud = obs.CardinalityBudget(max_series=8)
    ident = _ident(runtime="rt-outbox")
    b, *_ = _pipeline_real(ident, identity_budget=bud)
    s = obs.build(ident, obs.Trust.GATEWAY, enabled=False, bundle=b,
                  identity_budget=bud)
    assert bud.activas == 0, "el sensor apagado ya había gastado cupo al construirse"

    # ⊖ APAGADO: ni emite ni decide.
    assert s.outbox_span(event_id="e", effect_id="f", accept_context={},
                         attempt=1, outcome="ok") is obs.ExportResult.NOT_EXPORTED
    assert bud.activas == 0, "`outbox_span` apagado consumió presupuesto de identidad"
    assert s._identidad_admitida is None, "decidió la admisión estando apagado"

    # ⊕ ENCENDIDO por el atributo: ahora sí admite, UNA vez, y emite.
    s.enabled = True
    assert s.outbox_span(event_id="e", effect_id="f", accept_context={},
                         attempt=2, outcome="ok") is not obs.ExportResult.NOT_EXPORTED
    assert bud.activas == 1, f"admisiones tras encender: {bud.activas}, esperada 1"
    assert s._identidad_admitida is True


def test_nacer_apagado_CONSERVA_el_exportador_del_bundle():
    """② Con `enabled=False` se inyectaba un `NullExporter` aunque hubiera bundle.

    Encender después por el atributo dejaba un sensor `enabled=True` y MUDO. Y peor:
    hacía pasar en verde cualquier falsador de la puerta de identidad montado sobre
    un sensor nacido apagado, porque no había emisión que bloquear — el falso verde
    que ya me cazó su propio control positivo.
    """
    ident = _ident(runtime="rt-cable")
    b, sp, rd, lg = _pipeline_real(ident)
    s = obs.build(ident, obs.Trust.GATEWAY, enabled=False, bundle=b)
    assert isinstance(s.exporter, obs.OtelExporter), (
        f"el cable se perdió al nacer apagado: {type(s.exporter).__name__}")
    s.enabled = True
    assert s.count("events.accepted") is not obs.ExportResult.NOT_EXPORTED
    assert rd.get_metrics_data().resource_metrics, "encendido y mudo"


def test_un_bundle_acreditado_con_cable_NULO_no_es_READY():
    """②-bis. `state` derivaba del bundle, que sólo sabe de sus providers: con un
    `NullExporter` delante todo estaba bien construido y no salía una sola señal."""
    ident = _ident(runtime="rt-nulo")
    b, *_ = _pipeline_real(ident)
    assert b.state is obs.Pipeline.READY          # el bundle SÍ está acreditado
    s = obs.build(ident, obs.Trust.GATEWAY, enabled=True, bundle=b,
                  exporter=obs.NullExporter())
    assert s.state is not obs.Pipeline.READY, "dijo READY sin por dónde emitir"
    assert s.state is obs.Pipeline.NOT_CONFIGURED


def test_el_presupuesto_de_identidad_NO_nace_uno_por_bundle():
    """③ `build_bundle` creaba un `CardinalityBudget(256)` propio si no le pasaban uno.

    Un techo por bundle se multiplica con los bundles: dos pipelines se creían cada
    uno dentro de su cupo mientras el receptor veía la suma. El defecto tiene que ser
    un presupuesto de PROCESO compartido.
    """
    b1, *_ = _pipeline_real(_ident(runtime="rt-comp-1"))
    b2, *_ = _pipeline_real(_ident(runtime="rt-comp-2"))
    assert b1.identity_budget is b2.identity_budget, (
        "cada bundle trae su propio techo: no acota, se multiplica")
    assert b1.identity_budget is obs.PRESUPUESTO_IDENTIDAD_PROCESO
    # CONTROL POSITIVO: quien pasa el suyo NO comparte, que es lo que hace testeable
    # el aislamiento sin volver global todo lo demás.
    propio = obs.CardinalityBudget(max_series=4)
    b3, *_ = _pipeline_real(_ident(runtime="rt-comp-3"), identity_budget=propio)
    assert b3.identity_budget is propio
    assert b3.identity_budget is not obs.PRESUPUESTO_IDENTIDAD_PROCESO


def test_el_bundle_conserva_su_ESQUEMA_y_el_sensor_lo_hereda():
    """④ El Resource de los providers se verificó CONTRA el adaptador del bundle."""
    ad = obs.LlminboxV1Adapter()
    ident = _ident(runtime="rt-schema")
    b, *_ = _pipeline_real(ident)
    assert b.adapter is not None and b.adapter.version == ad.version
    s = obs.build(ident, obs.Trust.GATEWAY, enabled=True, bundle=b)
    assert s.adapter is b.adapter, "el sensor no heredó el esquema del bundle"


def test_un_esquema_que_CONTRADICE_al_del_bundle_se_rechaza():
    """④-bis. Etiquetas de un vocabulario y Resource de otro: las dos mitades del
    dato contradiciéndose sin que nada lo diga. O se hereda, o se rechaza."""
    class _OtroAdaptador(obs.LlminboxV1Adapter):
        version = "9.9.9-inventada"

    ident = _ident(runtime="rt-schema-2")
    b, *_ = _pipeline_real(ident)
    with pytest.raises(obs.SchemaError):
        obs.build(ident, obs.Trust.GATEWAY, enabled=True, bundle=b,
                  adapter=_OtroAdaptador())


@pytest.mark.parametrize("modo", ["false", "excepcion"])
def test_un_force_flush_que_NO_vacia_degrada_el_estado(modo):
    """⑤ El fallo de la barrera se devolvía como `NOT_EXPORTED` y se callaba.

    `state` deriva `DEGRADED` de los fallos registrados, así que un flush que llevaba
    rato sin poder vaciar dejaba el sensor en `ready`: el veredicto bajaba y el estado
    no se movía. Ahora entra por el mismo canal que el fallo del exportador.
    """
    ident = _ident(runtime="rt-flush-%s" % modo)
    b, *_ = _pipeline_real(ident)
    s = obs.build(ident, obs.Trust.GATEWAY, enabled=True, bundle=b)
    assert s.state is obs.Pipeline.READY, "no partía de READY: el ⊖ sería vacuo"

    class _BarreraRota:
        def force_flush(self, *_a, **_k):
            if modo == "excepcion":
                raise RuntimeError("la cola no vacía")
            return False

    for clave in ("tracer_provider", "meter_provider", "logger_provider"):
        b.detalle[clave] = _BarreraRota()
    assert s.exporter.flush() is obs.ExportResult.NOT_EXPORTED
    assert s.state is obs.Pipeline.DEGRADED, (
        "la barrera no pudo vaciar y el estado siguió diciendo que todo va bien")


# ══════════════════════════════════════════════════════════════════════════════════
# Segunda auditoría independiente · cuatro defectos más, cada uno con su falsador
# ══════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("estricto", [False, True])
def test_una_identidad_RECHAZADA_no_puede_seguir_diciendo_READY(estricto):
    """① `_identidad_admitida is False` significa que este sensor no emite NADA —ni
    métrica, ni log, ni span, ni outbox— y `state` seguía devolviendo `READY`.

    El panel veía un sensor sano que llevaba rato sin mandar una sola señal. Es la
    avería más cara de las que este módulo existe para impedir: el silencio con cara
    de salud.
    """
    bud = obs.CardinalityBudget(max_series=1)
    id1, id2 = _ident(runtime="rt-deg-1"), _ident(runtime="rt-deg-2")
    b1, *_ = _pipeline_real(id1, identity_budget=bud)
    b2, *_ = _pipeline_real(id2, identity_budget=bud)
    s1 = obs.build(id1, obs.Trust.GATEWAY, enabled=True, bundle=b1, identity_budget=bud)
    assert s1.state is obs.Pipeline.READY, "el admitido no partía de READY: ⊖ vacuo"

    if estricto:
        with pytest.raises(obs.CardinalityBudgetExceeded):
            obs.build(id2, obs.Trust.GATEWAY, enabled=True, bundle=b2,
                      identity_budget=bud, strict=True)
    else:
        s2 = obs.build(id2, obs.Trust.GATEWAY, enabled=True, bundle=b2,
                       identity_budget=bud, strict=False)
        assert s2._identidad_admitida is False
        assert s2.state is obs.Pipeline.DEGRADED, (
            f"rechazada y aun así `{s2.state.value}`: silencio con cara de salud")
    # Y EL ADMITIDO NO SE CONTAGIA: si el rechazo degradara a todos, el falsador
    # pasaría por el motivo equivocado.
    assert s1.state is obs.Pipeline.READY


def test_un_sensor_SIN_bundle_no_se_fabrica_un_techo_privado():
    """② Se creaba un `CardinalityBudget(256)` propio y se evadía del global entero:
    N sensores sueltos = N techos, cada uno creyéndose dentro del suyo mientras el
    receptor ve la suma."""
    a = obs.build(_ident(runtime="rt-suelto-1"), obs.Trust.GATEWAY, enabled=True)
    b = obs.build(_ident(runtime="rt-suelto-2"), obs.Trust.GATEWAY, enabled=True)
    assert a.identity_budget is obs.PRESUPUESTO_IDENTIDAD_PROCESO, (
        "el sensor suelto se fabricó un techo privado")
    assert a.identity_budget is b.identity_budget, (
        "dos sensores sueltos con techos distintos: el eje no acota, se multiplica")
    # ⊕ el eje SE MUEVE entre identidades distintas, que es lo que lo hace un eje.
    antes = obs.PRESUPUESTO_IDENTIDAD_PROCESO.activas
    obs.build(_ident(runtime="rt-suelto-3"), obs.Trust.GATEWAY, enabled=True)
    assert obs.PRESUPUESTO_IDENTIDAD_PROCESO.activas > antes
    # ⊕ y quien pasa el suyo NO comparte.
    propio = obs.CardinalityBudget(max_series=2)
    c = obs.build(_ident(runtime="rt-suelto-4"), obs.Trust.GATEWAY, enabled=True,
                  identity_budget=propio)
    assert c.identity_budget is propio


def test_build_RECHAZA_un_presupuesto_distinto_del_que_comparte_el_bundle():
    """③ Dos techos sobre el mismo destino no acotan: se suman, y los dos se creen
    correctos."""
    ident = _ident(runtime="rt-dos-techos")
    compartido = obs.CardinalityBudget(max_series=4)
    b, *_ = _pipeline_real(ident, identity_budget=compartido)
    with pytest.raises(obs.CardinalityBudgetExceeded):
        obs.build(ident, obs.Trust.GATEWAY, enabled=True, bundle=b,
                  identity_budget=obs.CardinalityBudget(max_series=99))
    # ⊕ CONTROL: pasar EL MISMO objeto sí vale, y no pasar ninguno también.
    assert obs.build(ident, obs.Trust.GATEWAY, enabled=True, bundle=b,
                     identity_budget=compartido).identity_budget is compartido
    assert obs.build(ident, obs.Trust.GATEWAY, enabled=True,
                     bundle=b).identity_budget is compartido


def test_otro_adaptador_con_MISMO_tipo_y_version_tambien_se_rechaza():
    """④ Mi primera versión aceptaba «otro adaptador con el mismo tipo y versión»: una
    equivalencia que me inventé.

    El Resource se verificó contra EL objeto del bundle, y otra instancia puede nombrar
    los instrumentos de otra forma —`metric_name` es suyo— aunque comparta tipo y
    `version`. Los nombres son la mitad del dato.
    """
    ident = _ident(runtime="rt-mismo-tipo")
    b, *_ = _pipeline_real(ident)
    gemelo = obs.LlminboxV1Adapter()
    assert type(gemelo) is type(b.adapter) and gemelo.version == b.adapter.version
    assert gemelo is not b.adapter
    with pytest.raises(obs.SchemaError):
        obs.build(ident, obs.Trust.GATEWAY, enabled=True, bundle=b, adapter=gemelo)
    # ⊕ CONTROL: EL MISMO objeto pasa, y no pasar ninguno hereda.
    assert obs.build(ident, obs.Trust.GATEWAY, enabled=True, bundle=b,
                     adapter=b.adapter).adapter is b.adapter
    assert obs.build(ident, obs.Trust.GATEWAY, enabled=True, bundle=b).adapter is b.adapter
