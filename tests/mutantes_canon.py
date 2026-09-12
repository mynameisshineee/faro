#!/usr/bin/env python3
"""Los mutantes que TIENEN que morir, corridos en cada CI.

Un test verde no dice que discrimine. Esta noche —30/31-ago— seis falsadores míos
resultaron no medir nada, y en todos el síntoma fue el mismo: verde. La suite no lo veía
porque la suite comprueba el CÓDIGO, no comprueba a los tests.

Esto sí: para cada invariante que costó un defecto real, hay un mutante que debe MATAR a
su falsador. Si alguno sobrevive, el falsador dejó de discriminar aunque siga en verde —
y ese es exactamente el estado que produce un arnés que certifica cualquier cosa.

Se corre con `tests/mutar.py`, que rechaza el ejercicio como INSERVIBLE si no pudo medir
(base ya roja, ancla no única, fichero sin cambiar, restauración fallida).

    python3 tests/mutantes_canon.py            # todos
    python3 tests/mutantes_canon.py --lista    # qué protege cada uno

CÓMO SE COMPRUEBA QUE ESTO DISCRIMINA, porque lo hice mal dos veces seguidas: hay que
desarmar el FALSADOR —no el código— y ver que el canon se pone rojo nombrando al mutante
superviviente. Y desarmarlo ENTERO: mi primer intento debilitó una de las dos aserciones
del test y el canon siguió verde **con razón**, porque la otra seguía cazando. Firmé un
commit diciendo que el ⊖ daba rc=1 cuando daba rc=0.

    ⊖ correcto: debilitar TODAS las aserciones del falsador ⇒ rc=1, y el canon nombra
                «sobreviven: <mutante>»
    ⊕ restaurar ⇒ rc=0, verificado por diff vacío

⚠️ Y aquí se para la recursión, a propósito: este módulo protege a los falsadores, y a él
no lo protege nadie. Un guardián del guardián del guardián no termina nunca. Lo que lo
sostiene es que **su fallo es ruidoso**: si `mutar.py` no puede medir, sale INSERVIBLE; si
un mutante sobrevive, lo nombra. No hay un modo en que este fichero pase en verde sin
haber corrido — que es exactamente lo que sí les pasaba a los ⊖ escritos a mano.

CÓMO AÑADIR UNO: cuando cures un defecto y escribas su ⊖, si ese ⊖ protege algo que
volvería a doler, tráelo aquí. El criterio no es «cubre código», es «si esto se rompe,
¿alguien se entera por otra vía?». Si la respuesta es no, su mutante vive aquí.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from mutar import falsa, FalsadorInservible   # noqa: E402

RAIZ = pathlib.Path(__file__).resolve().parents[1]
PY_TEST = str(RAIZ / ".venv-test" / "bin" / "python")
if not pathlib.Path(PY_TEST).exists():
    PY_TEST = sys.executable


def _cmd(*ficheros):
    return [PY_TEST, "-m", "pytest", "-q", "-p", "no:cacheprovider",
            *[f"tests/pytest/{f}" for f in ficheros]]


def _cmd_projector(*ficheros):
    return [PY_TEST, "-m", "pytest", "-q", "-p", "no:cacheprovider",
            *[f"tests/projector/{f}" for f in ficheros]]


CANON = [
    # (invariante, fichero, [(ancla, reemplazo, nombre)], comando)
    ("C5 · /pendientes exige AMBAS credenciales",
     "servicio.py",
     [("PUERTA_WATCHER = GATE + [Depends(exige_watcher)]",
       "PUERTA_WATCHER = GATE", "cae el watcher, queda sólo el token de casa"),
      ("PUERTA_WATCHER = GATE + [Depends(exige_watcher)]",
       "PUERTA_WATCHER = [Depends(exige_watcher)]", "cae el GATE, queda sólo el watcher")],
     _cmd("test_pendientes_gate_watcher.py")),

    ("El hombre muerto: «no puede armarse» NO es sano",
     "servicio.py",
     [('VIGILANCIA_SANOS = ("viva", "sin-armar")',
       'VIGILANCIA_SANOS = ("viva", "sin-armar", "inarmable")',
       "«inarmable» se declara sano")],
     _cmd("test_vigilancia_inarmable.py")),

    ("El atajo de la wiki re-resuelve citas cuando llegan sus entradas",
     "servicio.py",
     [("                and marca_entries == _HUELLA_WIKI.get(\"entries\", 0)):",
       "                ):", "el atajo vuelve a ignorar `entries`")],
     _cmd("test_cita_rota_se_re_resuelve.py")),

    ("Config inválida NO cae al lado permisivo",
     "servicio.py",
     [("    if v <= 0:", "    if False:", "acepta un tope de cero o negativo"),
      ('    if crudo == "1":', '    if crudo in ("1", "true", "yes"):',
       "la bandera acepta sinónimos en vez de una forma canónica")],
     _cmd("test_tope_configurable.py", "test_bandera_no_se_apaga_sola.py")),

    ("El build dice CÓMO se supo, no sólo cuál",
     "servicio.py",
     [('sha, origen = r.stdout.strip(), "derivado"',
       'sha, origen = r.stdout.strip(), "declarado"',
       "un sha del disco se hace pasar por inyectado"),
      ('return {"sha": None, "origen": "desconocido", "parece_sha": False,\n'
       '                **_huella_del_fichero()}',
       'return {"sha": "unknown", "origen": "declarado", "parece_sha": False,\n'
       '                **_huella_del_fichero()}',
       "«desconocido» finge un valor")],
     _cmd("test_build_declara_su_procedencia.py")),

    ("El build trae algo que el proceso no puede afirmar de sí mismo",
     "servicio.py",
     [('with open(__file__, "rb") as f:', 'with open("/etc/hostname", "rb") as f:',
       "la huella se calcula sobre otro fichero"),
      ('return {"huella": None, "huella_de": "servicio.py"}',
       'return {"huella": sha or "", "huella_de": "servicio.py"}',
       "al no poder leer, finge una huella")],
     _cmd("test_build_huella_medida.py")),

    ("`tope_s` es contrato: tiene un consumidor fuera del repo",
     "servicio.py",
     [('"motivo": _vmotivo, "tope_s": VIGILANCIA_MUDA_S,',
       '"motivo": _vmotivo, "umbral_s": VIGILANCIA_MUDA_S,',
       "el campo se renombra y el self-heal de la flota deja de encontrarlo")],
     _cmd("test_tope_s_es_contrato.py")),

    # ── M3 · sensores ────────────────────────────────────────────────────────
    # Los seis invariantes cuyo fallo es SILENCIOSO: una etiqueta de más no da
    # error, da una factura; un secreto exportado no da error, da una fuga; y un
    # detector que confunde latido con progreso se pone verde justo cuando debía
    # avisar. Ninguno de los seis lo caza la suite por otra vía.
    ("M3 · el presupuesto de cardinalidad ACOTA de verdad",
     "observability.py",
     [("if len(self._vistas) < self.max_series:", "if True:",
       "el techo de series deja de existir"),
      ("return v if v in permitidos else OVERFLOW", "return v",
       "un valor libre crea serie en vez de desbordar")],
     _cmd("test_m3_observability.py")),

    ("M3 · el contenido sensible no sale por el exportador",
     "observability.py",
     [('        if clase == "secreto":', "        if False:",
       "un secreto baja al nivel de contenido y la captura lo desbloquea"),
      ("    if n in CLAVES_SECRETAS or n in CLAVES_CONTENIDO:", "    if False:",
       "la normalización deja de reconocer el nombre exacto de la clave"),
      ('    salida: dict[str, Any] = {f"{k}_redacted": MARCA_REDACTADO, f"{k}_len": _largo(v)}',
       '    salida: dict[str, Any] = {f"{k}_sha256": huella(v), f"{k}_len": _largo(v)}',
       "el secreto publica un digest sin clave: oráculo de confirmación"),
      ("if kl in LABELS_PROHIBIDAS or kl in CLAVES_SENSIBLES:", "if False:",
       "un identificador entra como etiqueta de métrica")],
     _cmd("test_m3_observability.py")),

    ("M3 · el agente no es una fuente",
     "observability.py",
     [("if trust not in TRUSTED:", "if False:",
       "el runtime del agente pasa a emitir señales de salud")],
     _cmd("test_m3_observability.py")),

    ("M3 · liveness y progreso son fuentes DISJUNTAS",
     "observability.py",
     [("        if not cycle_ack:", "        if False:",
       "el INTENTO refresca el latido: un watcher roto queda verde"),
      ('if self.source != "journal":', "if False:",
       "la telemetría acredita progreso sin fila durable")],
     _cmd("test_m3_observability.py")),

    ("M3 · «no puede armarse» NO es sano, tampoco aquí",
     "observability.py",
     [("SANOS = frozenset({Estado.VIVA_CON_PROGRESO, Estado.VIVA_SIN_OBLIGACION,\n"
       "                   Estado.SIN_ARMAR})",
       "SANOS = frozenset({Estado.VIVA_CON_PROGRESO, Estado.VIVA_SIN_OBLIGACION,\n"
       "                   Estado.SIN_ARMAR, Estado.INARMABLE})",
       "«inarmable» se declara sano — la misma avería que en `servicio.py`")],
     _cmd("test_m3_observability.py")),

    ("M3 · el span de materialización enlaza y trae effect_id",
     "observability.py",
     [("        if parent is not None:\n            raise SchemaError(",
       "        if False:\n            raise SchemaError(",
       "el outbox vuelve a anidar: la espera en cola se lee como latencia"),
      ("if not effect_id:", "if False:",
       "sin effect_id dos reintentos parecen dos efectos")],
     _cmd("test_m3_observability.py")),

    ("M3 · apagado por defecto y ausencia del SDK NOMBRADA",
     "observability.py",
     [('enabled = os.environ.get("LLMINBOX_OBS", "") == "1"', "enabled = True",
       "la observabilidad se enciende sola"),
      ('            exp, motivo = NullExporter(), "not_configured"',
       '            exp, motivo = NullExporter(), "ok"',
       "«no hay pipeline» se presenta como exportador sano")],
     _cmd("test_m3_observability.py")),

    ("M3 · un exportador caído no puede ser invisible",
     "observability.py",
     [("            self.stats.export_fallos += 1", "            self.stats.export_fallos += 0",
       "el fallo de exportación deja de contarse")],
     _cmd("test_m3_observability.py")),

    ("M3 · la redacción es RECURSIVA",
     "observability.py",
     [("    if isinstance(val, Mapping):\n        return _scrub_mapa(",
       "    if False:\n        return _scrub_mapa(",
       "un secreto dentro de un mapping anidado sale en claro"),
      ("    if isinstance(val, (list, tuple, set, frozenset)):\n        items = list(val)[:max_items]",
       "    if False:\n        items = list(val)[:max_items]",
       "un secreto dentro de una lista sale en claro")],
     _cmd("test_m3_observability.py")),

    ("M3 · el NOMBRE de la señal también está cerrado",
     "observability.py",
     [("        if canonico in permitidos:", "        if True:",
       "un nombre libre crea serie sin usar una etiqueta prohibida")],
     _cmd("test_m3_observability.py")),

    ("M3 · el carril es IDENTIDAD, no una dimensión del llamante",
     "observability.py",
     [('            if kl == "lane":', "            if False:",
       "un carril que MIENTE deja de contarse como violación"),
      ("        salida[self.adapter.label_key(\"lane\")] = self.identity.lane",
       "        salida.setdefault(self.adapter.label_key(\"lane\"), \"\")",
       "el carril emitido deja de ser el de la identidad"),
      ],
     _cmd("test_m3_observability.py")),

    ("M3 · la denylist no se esquiva con mayúsculas",
     "observability.py",
     [("            if kl in LABELS_PROHIBIDAS or kl in CLAVES_SENSIBLES:",
       "            if k in LABELS_PROHIBIDAS or kl in CLAVES_SENSIBLES:",
       "vuelve la comparación case-sensitive: `Event_ID` esquiva la denylist"),
      ('    "outcome", "subject_kind", "tool_class", "exit_class",',
       '    "outcome", "subject_kind", "tool_class", "exit_class", "free",',
       "la allowlist crece sin vocabulario y reabre el valor libre por esa clave")],
     _cmd("test_m3_observability.py")),

    ("M3 · el pipeline se ACREDITA, no se supone",
     "observability.py",
     # MUTANTE REPARADO (2). El anterior cerraba con `)` un `[` que abría: daba
     # `SyntaxError`, moría sin ejecutar una sola aserción, y `mutar.py` lo contaba
     # como MUERTE. Acreditaba el falsador sin haberlo corrido nunca. Éste compila y
     # es causal: la verificación del Resource se vuelve vacua, que es justo lo que el
     # falsador tiene que ver.
     [("    res_ok = all(_resource_casa(p, esperado)\n"
       "                 for p in (tracer_provider, meter_provider, logger_provider))",
       "    res_ok = True",
       "se borra la verificación de Resource: READY con identidad ajena"),
      ("    salida_ok = all(v is True for v in salidas.values())", "    salida_ok = True",
       "READY sin pipeline: proveedor sin procesador ni reader se declara listo")],
     _cmd("test_m3_observability.py")),

    ("M3 · cada señal sale por su canal y con su tipo",
     "observability.py",
     [("                enlaces.append(_t.Link(sc, attrs))", "                pass",
       "se borra el Link: la causalidad del outbox desaparece del cable"),
      ('        elif signal.tipo == "log":\n            self._log(signal)',
       '        elif signal.tipo == "log":\n            self._span(signal)',
       "un log sale como span"),
      # MUTANTE REPARADO. El anterior insertaba una clave `"_x"` en un dict que se
      # subindexa acto seguido por `[clase]` ∈ {counter,gauge,histogram}: nunca se leía,
      # así que era un NO-OP y NINGÚN falsador podía matarlo. Sobrevivía por
      # construcción, y el canon lo leía como «el falsador dejó de discriminar» —
      # acusando al test de un defecto que estaba en el mutante.
      ('                    "gauge": self.bundle.meter.create_gauge,\n'
       '                    "histogram": self.bundle.meter.create_histogram}[clase]',
       '                    "gauge": self.bundle.meter.create_counter,\n'
       '                    "histogram": self.bundle.meter.create_counter}[clase]',
       "gauge e histogram se crean como counter: la medida puntual se vuelve suma")],
     _cmd("test_m3_observability.py")),

    # ── LOS CUATRO INVARIANTES DE LA SEGUNDA AUDITORÍA ────────────────────────
    # Cada uno con un mutante COMPILABLE que desarma exactamente su guarda: si el
    # falsador correspondiente dejara de discriminar, aquí sobreviviría.
    ("M3 · una identidad rechazada no se declara sana",
     "observability.py",
     [("        if self._identidad_admitida is False:\n"
       "            return Pipeline.DEGRADED",
       "        if False:\n"
       "            return Pipeline.DEGRADED",
       "una identidad rechazada vuelve a decir READY: silencio con cara de salud")],
     _cmd("test_m3_observability.py")),

    ("M3 · el presupuesto de identidad es del proceso, no del sensor",
     "observability.py",
     [("                                or PRESUPUESTO_IDENTIDAD_PROCESO)",
       "                                or CardinalityBudget(max_series=256))",
       "un sensor sin bundle se fabrica un techo privado y evade el global")],
     _cmd("test_m3_observability.py")),

    ("M3 · un solo techo por destino",
     "observability.py",
     [("    if (bundle is not None and identity_budget is not None",
       "    if (False and identity_budget is not None",
       "`build` acepta un presupuesto distinto del que comparte el bundle")],
     _cmd("test_m3_observability.py")),

    ("M3 · el esquema del bundle es EL objeto, no uno equivalente",
     "observability.py",
     [("        if bundle is not None and adapter is not None and adapter is not bundle.adapter:",
       "        if False and adapter is not None and adapter is not bundle.adapter:",
       "otra instancia de adaptador sustituye al exacto del bundle")],
     _cmd("test_m3_observability.py")),

    ("M3 · el barrido va ANTES del digest",
     "observability.py",
     [("                if contiene_secreto(val, max_depth=max_depth):", "                if False:",
       "un contenido que envuelve un secreto publica sha256 estable")],
     _cmd("test_m3_observability.py")),

    ("M3 · a un hook sólo llega vocabulario acotado",
     "observability.py",
     [("                                 codigo_error(e))", "                                 str(e)[:80])",
       "el mensaje de la excepción —con su secreto dentro— llega a la alarma"),
      ("self.hooks._disparar(self.hooks.on_forge, self.identity, len(payload))",
       "self.hooks._disparar(self.hooks.on_forge, self.identity, sorted(payload))",
       "los nombres de campo que eligió el agente llegan al registro"),
      ('return CODIGOS_ERROR.get(type(e).__name__, "other")', "return type(e).__name__",
       "el nombre de una clase ajena reabre la cardinalidad por el error")],
     _cmd("test_m3_observability.py")),

    # ── Puente M1/M2/M3 · las señales tienen que nacer en el camino real ──
    ("Bridge · Journal no atribuye una operación al Resource de otra sesión",
     "coordination.py",
     [("        if not sensor_matches(\n"
       "                sensor, principal=view.principal_id, role=view.role, lane=view.lane,",
       "        if False and not sensor_matches(\n"
       "                sensor, principal=view.principal_id, role=view.role, lane=view.lane,",
       "se elimina la comprobación de identidad exacta de la factory")],
     _cmd("test_m123_telemetry_bridge.py")),

    ("Bridge · expiry/revoke conocidos conservan atribución durable",
     "coordination.py",
     [("                view, auth_reason = self._vista_durable_para_telemetria(token)",
       "                view, auth_reason = None, \"unknown_credential\"",
       "la reautenticación pierde la identidad justo al expirar o revocar")],
     _cmd("test_m123_telemetry_bridge.py")),

    ("Bridge · outbox productivo emite effect_id, attempt y Link",
     "coordination.py",
     [("                    if contexto is not None:\n"
       "                        sensor.outbox_span(",
       "                    if False:\n"
       "                        sensor.outbox_span(",
       "el camino real de materialización deja M3 huérfano")],
     _cmd("test_m123_telemetry_bridge.py")),

    ("Bridge · un ACK replay no cuenta otra transición",
     "coordination.py",
     [("                if detail.get(\"advanced\"):",
       "                if True:",
       "el replay de delivery vuelve a emitir y duplica el progreso")],
     _cmd("test_m123_telemetry_bridge.py")),

    ("Bridge · identidad legacy válida para M1/M2 no silencia M3",
     "telemetry_bridge.py",
     [("    if isinstance(value, str) and _CANONICAL.fullmatch(value):",
       "    if True:",
       "la identidad legacy se entrega cruda a M3 y desaparece toda su telemetría")],
     _cmd("test_m123_telemetry_bridge.py")),

    ("Bridge · errores previos a SearchStore siguen siendo observables",
     "servicio.py",
     [("        _telemetria_error_busqueda(scope, error_code=\"reserved_parameter\")",
       "        pass  # mutante: el 422 queda mudo",
       "un 422 del gateway no deja señal")],
     _cmd("test_m123_telemetry_bridge.py")),

    ("Bridge · bundle_factory sólo entrega pipelines reacreditados",
     "telemetry_bridge.py",
     [("                bundle = _acredita_bundle(candidate, identity) if candidate is not None else None",
       "                bundle = candidate",
       "se acepta pipeline_unverified de la factory"),
      ("                bundle = _acredita_bundle(candidate, identity) if candidate is not None else None",
       "                bundle = (candidate if candidate.state is obs.Pipeline.READY else None)",
       "se confía en flags viejos y no se revalida el Resource de los providers")],
     _cmd("test_m123_telemetry_bridge.py")),

    ("Bridge · effect_id del worker nunca sale crudo",
     "coordination.py",
     [("                            effect_id=self._effect_id_telemetria(\n"
       "                                argumentos[\"event_id\"], argumentos.get(\"entry_eid\")),",
       "                            effect_id=argumentos.get(\"entry_eid\") or \"\",",
       "el worker controla el effect_id exportado y puede inyectar un secreto")],
     _cmd("test_m123_telemetry_bridge.py")),

    ("Bridge · Resource se revalida en cada emisión, no sólo al construir",
     "telemetry_bridge.py",
     [("        if not _bundle_still_valid(self.bundle, self.identity, self.expected):\n"
       "            # Sensor._emit lo convierte en NOT_EXPORTED+DEGRADED; el inner no ve bytes.",
       "        if False:\n"
       "            # Sensor._emit lo convierte en NOT_EXPORTED+DEGRADED; el inner no ve bytes.",
       "una mutación posterior del tracer cruza carril con el sensor aún READY")],
     _cmd("test_m123_telemetry_bridge.py")),

    # ── C4 · endurecimiento del ActiveProjectorRunner ────────────────────────
    # Todos viven en `tests/projector/`, no en `tests/pytest/`: por eso usan
    # `_cmd_projector`. Se registran AQUÍ y no sólo en la suite porque el commit
    # anterior declaró «9 mutantes, 9 muertos» sin dejarlos en ningún gate — una
    # cifra que nadie podía volver a comprobar es una cifra que no existe.
    ("C4 · la rotación de sesión cambia el token que SE USA, no sólo el guardado",
     "projector_runner.py",
     [("""        with self._cond:
            # EL PAR, JUNTO. Publicar la sesión sin su proyector deja un ciclo
            # entero proyectando con el token de la padre revocada.
            self._session = renewed
            self._projector = renewed_projector
            self._cond.notify_all()""",
       """        with self._cond:
            self._session = renewed
            self._cond.notify_all()""",
       "el projector conserva el token de la padre YA REVOCADA"),
      ("        self._adopt_session(renewed)\n        try:\n            self._check_session(renewed)",
       "        try:\n            self._check_session(renewed)",
       "la hija rechazada queda suelta y se cierra el cadáver de la padre")],
     _cmd_projector("test_runner_activo.py")),

    ("C4 · un cierre de sesión fallido es reintentable y visible",
     "projector_runner.py",
     [("""            try:
                self._backend.close_worker_session(session)
            except BaseException as cleanup:
                with self._cond:
                    self._close_failures += 1
                    self._cond.notify_all()
                return cleanup
            with self._cond:
                self._session_closed = True
                self._cond.notify_all()
            return None""",
       """            with self._cond:
                self._session_closed = True
            try:
                self._backend.close_worker_session(session)
            except BaseException as cleanup:
                with self._cond:
                    self._close_failures += 1
                    self._cond.notify_all()
                return cleanup
            return None""",
       "la bandera se pone antes del cierre y la fuga queda muda")],
     _cmd_projector("test_runner_activo.py")),

    ("C4 · un stop durante `starting` no deja la sesión huérfana",
     "projector_runner.py",
     [("            stopping = self._stop_requested", "            stopping = False",
       "start() no re-lee la parada y lanza hebra sobre una sesión sin dueño")],
     _cmd_projector("test_runner_activo.py")),

    ("C4 · una hebra que no nace deja FATAL con snapshot LEGIBLE",
     "projector_runner.py",
     [("""                self._thread = None
                self._accepting = False
                self._in_flight = False
                self._state = RunnerState.FATAL
                self._fatal_code = FATAL_THREAD_START_FAILED
                self._cond.notify_all()
                spawn_failure = spawn""",
       "                spawn_failure = spawn",
       "el par (acepta, hebra muerta) hace ILEGIBLE al propio snapshot")],
     _cmd_projector("test_runner_activo.py")),

    ("C4 · lease_s es un entero exacto en 1..3600 y casa con stop_timeout",
     "projector_runner.py",
     [("        if type(lease_s) is not int or not MIN_LEASE_S <= lease_s <= MAX_LEASE_S:",
       "        if type(lease_s) is not int or lease_s <= 0:",
       "vuelve el arriendo sin techo que el núcleo rechaza en cada ciclo"),
      ("        if self._stop_timeout_s < float(lease_s):", "        if False:",
       "la pareja lease/stop vuelve a poder configurar una fuga garantizada")],
     _cmd_projector("test_runner_activo.py")),

    ("C4 · ninguna BaseException se traga sin código cerrado propio",
     "projector_runner.py",
     [("""            self._enter_fatal(
                FATAL_RUNNER_INTERRUPTED
                if isinstance(internal, (KeyboardInterrupt, SystemExit))
                else FATAL_RUNNER_INTERNAL_ERROR)""",
       "            self._enter_fatal(FATAL_BACKEND_CONTRACT)",
       "un fallo del runner se publica como culpa del backend")],
     _cmd_projector("test_runner_activo.py")),

    ("C4 · drain NIEGA y sólo quiesce aquieta",
     "projector_runner.py",
     [("        raise RunnerDrainNotSupported(",
       "        return self.quiesce(timeout_s=timeout_s)\n        raise RunnerDrainNotSupported(",
       "aquietar vuelve a llamarse drenar y un pending>0 sale sin excepción")],
     _cmd_projector("test_runner_activo.py")),

    ("C4 · la sesión se renueva antes del TTL y una vencida no se adopta",
     "projector_runner.py",
     [("                fatal = self._renovar_si_toca()", "                fatal = None",
       "el ciclo deja de renovar y el TTL vuelve a ser un acantilado"),
      ("        if now + self._lease_s + self._renew_margin_s < float(expires_at):",
       "        if now + self._renew_margin_s < float(expires_at):",
       "reclama con una sesión cuyo TTL no cubre el lease solicitado"),
      ("        if float(expires_at) <= self._clock():", "        if False:",
       "se adopta una credencial ya muerta al arrancar")],
     _cmd_projector("test_runner_activo.py")),
]


def main() -> int:
    if "--lista" in sys.argv:
        for inv, _, ms, _c in CANON:
            print(f"\n  {inv}")
            for m in ms:
                print(f"      ⊖ {m[2]}")
        return 0

    malos, inservibles = [], []
    for inv, fichero, mutantes, cmd in CANON:
        print(f"\n── {inv} ──")
        try:
            # REPRODUCIBLE POR SHA. Con `MUTAR_SHA` el ejercicio se hace sobre
            # una copia exportada de ese commit, no sobre el árbol de trabajo: dos
            # corridas del canon son comparables aunque alguien esté editando al
            # lado, y el resultado se puede citar contra un objeto inmutable. Sin
            # la variable se usa el árbol, que es lo que hace falta mientras se
            # desarrolla. En los dos casos se muta una COPIA y se borra al salir.
            r = falsa(fichero, mutantes, cmd, cwd=RAIZ,
                      sha=os.environ.get("MUTAR_SHA") or None)
        except FalsadorInservible as e:
            print(f"  ⛔ INSERVIBLE: {e}")
            inservibles.append(inv)
            continue
        if r["sobrevivieron"]:
            malos.append((inv, r["sobrevivieron"]))

    print("\n" + "═" * 72)
    if inservibles:
        print(f"⛔ {len(inservibles)} ejercicio(s) NO PUDIERON MEDIR — el roto es la medida, "
              f"no el código:")
        for i in inservibles:
            print(f"    · {i}")
    if malos:
        print(f"‼ {len(malos)} invariante(s) con falsador que YA NO DISCRIMINA:")
        for inv, quienes in malos:
            print(f"    · {inv}: sobreviven {', '.join(quienes)}")
    if not malos and not inservibles:
        print(f"✅ {sum(len(m) for _, _, m, _ in CANON)} mutantes, todos muertos: "
              f"los falsadores de estas {len(CANON)} invariantes siguen midiendo.")
    return 1 if (malos or inservibles) else 0


if __name__ == "__main__":
    raise SystemExit(main())
