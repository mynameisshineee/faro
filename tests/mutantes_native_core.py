#!/usr/bin/env python3
"""Mutantes de A1/A6/A7 — el ⊖ que `mutantes_m1.py` NO da.

`mutantes_m1.py` corre 9/9 en verde y **no toca ninguna de estas tres curas**:
sus anclas viven en preflight, pepper y WAL. Presentar aquel verde como
evidencia de A1/A6/A7 sería acreditar el sujeto con un instrumento que no lo
mira — la clase «un ⊕ valida el instrumento, jamás su sujeto».

Reglas heredadas del runner oficial, y por los mismos motivos:
  · se muta un CLON desechable; el árbol vivo nunca se escribe.
  · el ancla es texto exacto: si no casa, ABORTA. Un mutante saltado se lee en
    el informe igual que uno muerto.
  · el veredicto exige que caigan los tests NOMBRADOS. Que la suite se ponga
    roja «por algo» no discrimina: un mutante que rompe la recolección también
    lo pone todo rojo.

Uso:  python3 tests/mutantes_native_core.py
"""
from __future__ import annotations

import ast
import pathlib
import shutil
import subprocess
import sys
import tempfile

RAIZ = pathlib.Path(__file__).resolve().parent.parent
SUITE = "tests/journal/test_native_core_a1_a6_a7.py"
SUITE_P1 = "tests/journal/test_native_core_p1.py"
SUITE_S = "tests/journal/test_native_core_sesion_y_scope.py"
SUITE_N = "tests/journal/test_native_core_p1_normalizacion.py"
SUITE_T = "tests/journal/test_native_core_lease_toctou.py"
SUITE_SE = "tests/journal/test_native_core_sesion_toctou.py"
SUITE_L = "tests/journal/test_leases_y_comandos.py"
SUITE_O = "tests/journal/test_native_core_orden_fail_closed.py"
SUITE_G = "tests/journal/test_native_core_detalle_global.py"
SUITE_C = "tests/journal/test_native_core_cota_intent.py"
SUITE_D = "tests/journal/test_native_core_detail_trunca.py"
SUITE_B = "tests/journal/test_native_core_presupuesto_compartido.py"
SUITE_AG = "tests/journal/test_native_core_agregado_canonico.py"
SUITE_X = "tests/journal/test_native_core_contador_unico.py"
SUITE_OC = "tests/journal/test_outbox_counts.py"

# (id, fichero, ancla exacta, reemplazo, tests que DEBEN caer)
MUTANTES = [
    ("MA1-roles", "coordination.py",
     "                 _canonical(_dest_lit), _canonical(_dest_roles),",
     "                 _canonical(_dest_lit), _canonical(_dest_lit),",
     ["test_a1_acusa_por_ROL_canonico_aunque_el_literal_sea_otro_nombre",
      "test_a1_duplicados_por_alias_cuentan_UNA_vez"]),

    ("MA1-failopen", "coordination.py",
     '        if self._recipient_resolver is None:\n            raise RecipientUnresolved(',
     '        if False:\n            raise RecipientUnresolved(',
     ["test_a1_SIN_censo_inyectado_falla_CERRADO"]),

    ("MA1-difusion-en-denominador", "coordination.py",
     '            if es_difusion:\n                difusion.append(lit)\n                continue',
     '            if es_difusion:\n                difusion.append(lit)\n                roles.append(lit)\n                continue',
     ["test_a1_difusion_sola_alcanza_estado_terminal_explicito",
      "test_a1_OMEGA_difusion_no_se_queda_en_delivery_progress",
      "test_a1_difusion_NO_cuenta_en_el_denominador_del_acuse",
      "test_a1_acuse_sobre_evento_de_pura_difusion_se_rechaza"]),

    ("MA1-sin-terminal", "coordination.py",
     "                self._cerrar_si_no_hay_acusables(con, event_id, rc[\"receipt_id\"],\n                                                 seq + 1, at)",
     "                pass",
     ["test_a1_difusion_sola_alcanza_estado_terminal_explicito",
      "test_a1_OMEGA_difusion_no_se_queda_en_delivery_progress"]),

    ("MA1-motivo-unico", "coordination.py",
     '        motivo = ("sin destinatarios acusables: todos los destinos son de difusión"\n'
     '                  if self._hubo_difusion(con, event_id) else\n'
     '                  "sin destinatarios: la entrada no va dirigida a nadie")',
     '        motivo = "sin destinatarios acusables: todos los destinos son de difusión"',
     ["test_a1_sin_destinatarios_y_solo_difusion_NO_dan_el_mismo_motivo"]),

    ("MA6-renew-delega", "coordination.py",
     '            if row["expires_at"] <= now:\n                raise LeaseConflict(',
     '            if False:\n                raise LeaseConflict(',
     ["test_a6_renew_de_lease_VENCIDO_da_conflicto_y_NO_toca_el_fencing"]),

    # ⚠️ EL ANCLA SE EXTIENDE HASTA LA LÍNEA QUE DISTINGUE. Desnuda
    # (`if row["runtime_instance"] != view.runtime_instance:`) casa TRES veces
    # —`renew_lease`, `release_lease` y `_fence_locked`— y el `replace(...,1)`
    # acertaba por ORDEN. Es el P2-1 que midió `@qa`
    # (`MARK:qa-go-a-f23e7aa-y-49379af-dos-p2-y-una-divergencia-de-contrato`);
    # el `assert count == 1` de abajo lo convierte en abort en vez de en un
    # acierto por suerte.
    ("MA6-renew-ajeno", "coordination.py",
     '            if row["runtime_instance"] != view.runtime_instance:\n'
     '                # Sin nombrar al dueño: quién tiene el recurso es información del',
     '            if False:\n'
     '                # Sin nombrar al dueño: quién tiene el recurso es información del',
     ["test_a6_renew_AJENO_da_conflicto_y_NO_toca_el_fencing"]),

    # El GEMELO que la desambiguación deja al descubierto: al fijar `renew`, la
    # guarda de `release_lease` se queda sin mutante. `@qa` ya midió que mutando
    # ESA cae `test_soltar_el_lease_de_OTRO_falla_y_no_lo_suelta`; aquí queda
    # declarado y ejecutado, no citado.
    ("MA6-release-ajeno", "coordination.py",
     '            if row["runtime_instance"] != view.runtime_instance:\n'
     '                raise LeaseConflict(\n'
     '                    f"`{resource}` es de otro runtime: soltarlo no es tuyo")',
     '            if False:\n'
     '                raise LeaseConflict(\n'
     '                    f"`{resource}` es de otro runtime: soltarlo no es tuyo")',
     ["test_soltar_el_lease_de_OTRO_falla_y_no_lo_suelta"]),

    ("MA6-renew-liberado", "coordination.py",
     '            if row["released_at"] is not None:\n                raise LeaseConflict(',
     '            if False:\n                raise LeaseConflict(',
     ["test_a6_renew_de_lease_LIBERADO_da_conflicto"]),

    ("MA7-reservados", "coordination.py",
     '        self._sin_atribucion(payload, "payload")',
     '        pass',
     ["test_a7_payload_con_campo_RESERVADO_se_rechaza"]),

    ("MA7-sin-command-id", "coordination.py",
     '                e.command_id = ya["command_id"]',
     '                e.command_id = None',
     ["test_a7_el_conflicto_de_revision_LLEVA_el_command_id_original"]),

    ("MA7-conflicto-cross-lane", "coordination.py",
     '            ya = con.execute("SELECT command_id FROM commands WHERE lane=?"\n'
     '                             "  AND workstream_id=? AND revision=?",\n'
     '                             (view.lane, workstream_id, int(revision))).fetchone()',
     '            ya = con.execute("SELECT command_id FROM commands WHERE"\n'
     '                             "  workstream_id=? AND revision=?",\n'
     '                             (workstream_id, int(revision))).fetchone()',
     ["test_a7_el_conflicto_NO_filtra_el_command_id_de_OTRO_carril",
      "test_a7_OMEGA_el_mensaje_del_conflicto_no_contiene_ids_ajenos"]),
]


def censo_jueces():
    """PRE-FLIGHT: todo mutante nombra jueces que EXISTEN **EN SU SUITE**.

    Devuelve la lista de `(mid, suite, jueces_que_faltan)`; vacia = sano.

    🩸 POR QUE ESTA PREGUNTA Y NO LA FACIL. «¿Existe el test?» da `0` huerfanos
    sobre `1.257` tests declarados en `132` ficheros — y aun asi habia `3`
    mutantes cuyo `-k` NO casaba nada, porque el arnes corre `-k` contra UNA
    suite y el juez vivia en otra. **El control estaba mal PUESTO: medi el
    nombre en el universo entero cuando la pregunta era por su suite.**
    Un `-k` que no casa da `rc=5`, y `rc=5` se pinta en el informe igual que un
    mutante vivo. Por eso esto ABORTA en vez de avisar: un arnes que mide mal
    unos cuantos es peor que uno que no corre, porque el informe sale igual.
    """
    import ast as _ast
    grupos = [v for k, v in globals().items()
              if (k == "MUTANTES" or k.startswith("MUTANTES_")) and isinstance(v, list)]
    cache = {}
    def tests_de(rel):
        if rel not in cache:
            f = RAIZ / rel
            cache[rel] = None if not f.exists() else {
                n.name for n in _ast.walk(_ast.parse(f.read_text()))
                if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))
                and n.name.startswith("test_")}
        return cache[rel]
    malos = []
    for grupo in grupos:
        for mid, _f, _a, _n, jueces in grupo:
            suite = suite_de(mid)
            hay = tests_de(suite)
            faltan = [j for j in jueces if hay is None or j not in hay]
            if faltan or not jueces:
                malos.append((mid, suite, faltan or ["<SIN JUECES>"]))
    return malos


def grupos_despues_del_guard(fuente: str | None = None) -> list:
    """Nombres `MUTANTES*` asignados DESPUES del `if __name__ == "__main__"`.

    🩸 NACE DE UN DEFECTO MEDIDO, y de uno que mi propio instrumento no podia
    ver: `MUTANTES_OC` quedo DEBAJO del guard. El modulo IMPORTA sin ruido —y
    por eso el banco focal, que importa el modulo y nunca llama a `main()`, lo
    dio por bueno— pero la EJECUCION DIRECTA resuelve el nombre dentro de
    `main()`, antes de llegar a la asignacion: `NameError` y el arnes no corre
    NADA. Y un arnes que no corre se lee igual que uno que no encontro nada.

    ⇒ Es la clase «medi con el call-site que YO elijo». La cura no es acordarse
      de donde va el bloque: es que el arnes se lo pregunte a si mismo antes de
      clonar nada, igual que ya hace `censo_jueces`.
    """
    src = fuente if fuente is not None else pathlib.Path(__file__).read_text()
    arbol = ast.parse(src)
    guard = None
    for n in arbol.body:
        if isinstance(n, ast.If) and "__main__" in ast.dump(n.test):
            guard = n.lineno
    if guard is None:
        return ['<SIN guard `if __name__ == "__main__"`>']
    malos = []
    for n in arbol.body:
        if isinstance(n, ast.Assign) and n.lineno > guard:
            malos += [t.id for t in n.targets
                      if isinstance(t, ast.Name) and t.id.startswith("MUTANTES")]
    return malos


def suite_de(mid):
    """El enrutado mutante -> suite. UNA SOLA VERDAD, y por eso es una funcion.

    🩸 NACE DE UN DEFECTO MIO, MEDIDO: la cadena vivia INLINE dentro de `main()`,
    asi que nadie fuera podia preguntarle a que suite va un mutante. Yo medi mis
    tres `MB7-*` con el banco focal **pasandole la suite A MANO** y salieron
    verdes — pero la rama era `startswith("MB1")`, o sea los `MB7` caian al
    `else SUITE` (`a1_a6_a7`), donde sus jueces NO EXISTEN. En el arnes real:
    `-k` sin seleccion, `rc=5`, y **se leen como VIVOS**.
    ⇒ Es la misma clase que ya me habia mordido dos veces esta jornada: **medir
    con el argumento que YO elijo en vez de con el que usa el call-site.** Con la
    cadena fuera, el censo de `SUITE_B` la interroga en vez de replicarla — una
    replica se queda vieja justo cuando alguien toca el original.
    """
    return (SUITE_X if mid.startswith("MX")
            else SUITE_AG if mid.startswith("MAG")
            # 🔻 `MB` y NO `MB1`: la rama vieja dejaba fuera a toda familia `MBn`
            #    nueva. Un prefijo mas estrecho que su familia no da error: da
            #    enrutado silencioso al cajon de sastre.
            # 🔻 `MQA`: familia nombrada por el `RULING @cto` sobre `d132f207`
            #    (`F-RUTA-4`). Va ANTES que cualquier prefijo mas corto — la
            #    leccion del `MB1`: un prefijo mas estrecho que su familia no da
            #    error, da enrutado silencioso al cajon de sastre.
            else SUITE_P1 if mid.startswith("MQA")
            else SUITE_B if mid.startswith("MB")
            else SUITE_D if mid.startswith("MD")
            else SUITE_C if mid.startswith("MC")
            # 🩸 EXCEPCION POR ID, como ya la tiene `MA6-release-ajeno`, y
            # por un defecto MEDIDO con el runner oficial: los dos
            # `MVE-sin-congelar-*` nombran `test_FBM_record_*`, que viven
            # en `SUITE_C`, no en `SUITE_G`. El enrutado POR PREFIJO no
            # puede separarlos —comparten `MVE` con tres que si son de
            # `SUITE_G`— y su `-k` no seleccionaba NADA: `rc=5`.
            else SUITE_C if mid.startswith("MVE-sin-congelar")
            else SUITE_G if mid.startswith("MVE") or mid.startswith("MG")
            # 🔻 `MOC` ANTES que `MO`: `"MOC".startswith("MO")` es True, así que
            # con la rama corta delante los `MOC` caerían en `SUITE_O`, donde su
            # `-k` no selecciona nada (rc=5) y se leerían VIVOS. Es la misma
            # trampa que ya documenta `MB1` doce líneas más arriba.
            else SUITE_OC if mid.startswith("MOC")
            else SUITE_O if mid.startswith("MO")
            else SUITE_L if mid == "MA6-release-ajeno"
            else SUITE_T if mid.startswith("MT")
            else SUITE_N if mid.startswith("MN")
            else SUITE_SE if mid.startswith("MSE")
            else SUITE_S if mid.startswith("MS")
            else SUITE_P1 if mid.startswith("MP1") else SUITE)


def corre(cwd, nombres=None, suite=None):
    sel = " or ".join(nombres) if nombres else None
    cmd = [sys.executable, "-m", "pytest", suite or SUITE, "-q", "--no-header",
           "-p", "no:cacheprovider"]
    if sel:
        cmd += ["-k", sel]
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


# Mutantes de los TRES P1 de la auditoría del gateway. Las anclas se
# extrajeron del código vivo y se verificó que cada una casa EXACTAMENTE una
# vez: un ancla ambigua muta un sitio que no es el que se cree medir.
MUTANTES_P1 = [
    ('MP1-1-kind', "coordination.py",
     '        canonico = g.canonical_kind(intent.get("kind"))',
     '        canonico = intent.get("kind") or "FYI"',
     ['test_p1_1_los_SIETE_cuerpos_de_su_sonda_dejan_de_aceptarse', 'test_p1_1_el_kind_se_guarda_CANONICO_no_como_lo_mando_el_cliente']),

    ('MP1-1-head-largo', "coordination.py",
     '        if len(head) > g.head_max:',
     '        if False:',
     ['test_p1_1_los_SIETE_cuerpos_de_su_sonda_dejan_de_aceptarse']),

    ('MP1-1-head-salto', "coordination.py",
     '        if "\\n" in head or "\\r" in head:',
     '        if False:',
     ['test_p1_1_los_SIETE_cuerpos_de_su_sonda_dejan_de_aceptarse', 'test_p1_1_head_con_CR_tambien_parte_la_cabecera']),

    ('MP1-1-solo-head-no-body', "coordination.py",
     '        for campo in ("head", "body"):',
     '        for campo in ("head",):',
     ['test_p1_1_los_SIETE_cuerpos_de_su_sonda_dejan_de_aceptarse',
      'test_p1_1_los_BACKTICKS_no_protegen_y_eso_es_fiel_a_append',
      'test_security_un_body_con_cabecera_ajena_NO_produce_dos_entradas']),

    ('MP1-1-to-vacio', "coordination.py",
     '        if not literales:\n            raise GrammarRejected(',
     '        if False:\n            raise GrammarRejected(',
     ['test_p1_1_los_SIETE_cuerpos_de_su_sonda_dejan_de_aceptarse']),

    ('MP1-1-gramatica-failopen', "coordination.py",
     '        if self._grammar is None:\n            raise GrammarUnavailable(',
     '        if False:\n            raise GrammarUnavailable(',
     ['test_p1_1_SIN_gramatica_inyectada_falla_CERRADO']),

    ('MP1-2-sin-recursion', "coordination.py",
     '                cls._sin_atribucion(v, f"{donde}.{k}", prof + 1)',
     '                pass',
     ['test_p1_2_atribucion_a_profundidad_2_se_rechaza']),

    # 🔻 JUEZ REANCLADO. Nombraba `test_p1_2_las_listas_tambien_se_recorren`, que
    # NO EXISTE en ninguna suite ⇒ `-k` vacio ⇒ `rc=5` ⇒ el informe lo leia como
    # VIVO. **Un mutante sin juez no es un superviviente: es un mutante SIN
    # MEDIR**, y las dos cosas se pintan igual. Huerfano declarado desde
    # `bb43f588` y cerrado aqui.
    # Los dos jueces existen y discriminan (medido: `DID NOT RAISE
    # AttributionRejected` en ambos). El segundo cubre el caso que el DOCSTRING
    # del sujeto nombra literalmente —`{"meta": [{"role": "cto"}]}`, la lista
    # ANIDADA— y que ningun test ejercia: el que habia pone la lista en la RAIZ.
    ('MP1-2-sin-listas', "coordination.py",
     '        elif isinstance(valor, (list, tuple)):\n            for i, v in enumerate(valor):',
     '        elif False:\n            for i, v in enumerate(valor):',
     ['test_p1_2_atribucion_dentro_de_una_LISTA_tambien',
      'test_p1_2_atribucion_dentro_de_una_lista_ANIDADA_el_caso_del_docstring']),

    # 🔻 JUEZ CAMBIADO. Este mutante SOBREVIVIO a
    # `test_p1_2_una_estructura_DEMASIADO_PROFUNDA_se_rechaza_no_se_recorre` y el
    # motivo esta medido: por la ruta publica corta antes el CONGELADOR
    # (`_congela_valor`, `prof_max` de la MISMA constante) con la MISMA clase y
    # `dimension="depth"`. Aquel test pide la CLASE, o sea acredita «alguien lo
    # paro», no «lo paro ESTA guarda». Sondeado con el mutante puesto,
    # `accept_event` da IDENTICO en sano y mutante incluso con atribucion al
    # fondo: la ruta publica NO PUEDE discriminarlo. Los dos jueces nuevos son
    # UNITARIOS sobre `_sin_atribucion`, que es donde la guarda vive.
    # El test viejo SE QUEDA en la suite —mide otra cosa, que el cuerpo hondo se
    # rechaza— pero deja de ser el juez de este mutante: nombrarlo aqui haria
    # leer «muerto» como si el hubiera discriminado.
    ('MP1-2-profundidad-fail-open', "coordination.py",
     '        if prof > cls._PROFUNDIDAD_MAX:\n            # Por el MISMO embudo',
     '        if prof > 10 ** 9:\n            # Por el MISMO embudo',
     ['test_p1_2_el_tope_de_profundidad_de_SIN_ATRIBUCION_dispara_EL_SOLO',
      'test_p1_2_una_estructura_DEMASIADO_PROFUNDA_NO_SE_RECORRE_de_verdad',
      # 🔻 el juez de ALCANZABILIDAD entra aqui: con la PRIMERA guarda
      #    neutralizada por el test, borrar la SEGUNDA deja la peticion sin
      #    ningun rechazo. Es lo que convierte «defensa en profundidad» de
      #    declaracion en MEDIDA.
      'test_ALCANZABILIDAD_si_cae_la_PRIMERA_guarda_la_SEGUNDA_rechaza_por_RUTA_PUBLICA']),

    # ⊖ DE `F-RUTA-5` (NO-GO de `@qa` sobre `b02549b2`): reinyecta en el fuente la
    # afirmacion de que la ruta sobrevive, EN LA FORMA QUE LA VERSION ANTERIOR DEL
    # FALSADOR NO CAZABA — mayusculas y el otro verbo. Si `F-RUTA-5` no se pone
    # rojo, la ampliacion (case-insensitive + `(viaja|va)`) no sirve de nada.
    # 🩸 El defecto que lo motiva es mio y de libro: mi aguja casaba SOLO la
    # variante que yo habia escrito, asi que medi mi memoria y no el fichero. El
    # parrafo superviviente estaba a CINCO lineas del bloque que lo contradice.
    ('MQA-RUTA5-la-ruta-vuelve-a-viajar', "coordination.py",
     '            # ⚖️ Un ATRIBUTO del contrato no puede llevar un valor que el\n',
     '            # EL CAMINO NO SE PIERDE, CAMBIA DE CANAL: va en la COLA.\n            # ⚖️ Un ATRIBUTO del contrato no puede llevar un valor que el\n',
     ['test_F_RUTA_5_el_fichero_no_afirma_que_la_ruta_VIAJA']),

    # ⊖ `F-RUTA-4` del `RULING @cto` sobre `d132f207`: la atribucion tiene que
    # seguir viva SIN `field`. Al retirarse la ruta, el UNICO discriminador del
    # productor es la marca de prosa; si se borra, la 2a capa deja de ser
    # identificable y el ⊕ de alcanzabilidad acredita a cualquiera.
    # 🩸 El RULING lo cita como si existiera y NO existia — lo cree aqui. Un
    # falsador nombrado en una adjudicacion no esta escrito por estar nombrado.
    ('MQA-2a-capa-sin-marca-de-prosa', "coordination.py",
     '                       "entra; lo para la 2a capa, la de atribucion")\n',
     '                       "entra")\n',
     # ⚠️ SOLO el juez que DISCRIMINA. La linea base tambien lo nombraba y NO
     # cae: afirma que «2a capa» NO sale en sano, y con la marca borrada sigue
     # sin salir ⇒ pasa. Nombrar un juez que no discrimina hace leer el `✅
     # muerto` como si el hubiera medido — la clase que este arnes ya curo dos
     # veces (`MP1-2-profundidad-fail-open`, `MB7-*`).
     ['test_ALCANZABILIDAD_si_cae_la_PRIMERA_guarda_la_SEGUNDA_rechaza_por_RUTA_PUBLICA']),

    # ⊖ DEL CONTROL NEGATIVO, que sin esto seria un test que nunca falla.
    # El ⊖ promete detectar «con las dos guardas apagadas, algo AJENO sigue
    # rechazando y el ⊕ estaria acreditando a un tercero». Este mutante crea
    # justo ese tercero: baja `NODOS_MAX` para que el cuerpo hondo muera por
    # NODOS aunque la profundidad este desactivada.
    ('MP1-2-un-TERCERO-rechaza-el-cuerpo-hondo', "coordination.py",
     'NODOS_MAX = 8192                 # nodos POR PETICION (1 nodo = 1 VALOR)\n',
     'NODOS_MAX = 100                 # nodos POR PETICION (1 nodo = 1 VALOR)\n',
     ['test_CONTROL_NEGATIVO_con_las_DOS_guardas_neutralizadas_la_peticion_ENTRA']),

    # ⊖ LA PRIMERA GUARDA, la del CONGELADOR — el falsador que pidio el operador.
    # Neutralizarla NO debe dejar pasar nada: lo que cambia es QUIEN rechaza, y
    # se ve en el `field` (PLANO la primera, CON CAMINO la segunda). Por eso su
    # juez es el CONTROL de linea base y no el de alcanzabilidad: ese ya
    # neutraliza esta guarda por su cuenta, asi que el mutante no le cambia nada.
    ('MP1-2-congelador-sin-profundidad', "coordination.py",
     '    prof_max = Journal._PROFUNDIDAD_MAX if prof_max is None else prof_max\n',
     '    prof_max = 10 ** 9 if prof_max is None else prof_max\n',
     ['test_CONTROL_el_rechazo_SANO_lo_pone_la_PRIMERA_guarda_y_se_ve_en_la_PROSA']),

    ('MP1-2-trace-sin-mirar', "coordination.py",
     '        self._sin_atribucion(trace, "trace")',
     '        pass',
     ['test_p1_2_atribucion_anidada_en_TRACE']),

    ('MP1-3-sin-normalizar', "coordination.py",
     '        norm = self._gramatica().normalize_resource(str(resource))',
     '        norm = str(resource)',
     ['test_p1_3_Deploy_y_deploy_son_UN_recurso_no_dos', 'test_p1_3_las_variantes_colisionan_con_la_forma_canonica']),

    ('MP1-3-vacio-pasa', "coordination.py",
     '        if not norm:\n            raise GrammarRejected(',
     '        if False:\n            raise GrammarRejected(',
     ['test_p1_3_un_recurso_que_se_normaliza_a_VACIO_se_rechaza']),

    ('MP1-3-fence-sin-normalizar', "coordination.py",
     '        resource = self._recurso(resource)\n        row = con.execute("SELECT * FROM leases WHERE lane=? AND resource=?",',
     '        row = con.execute("SELECT * FROM leases WHERE lane=? AND resource=?",',
     ['test_p1_3_renew_release_y_fence_hablan_del_MISMO_recurso']),

    ('MP1-3-literal-perdido', "coordination.py",
     '        literal, resource = str(resource), self._recurso(resource)',
     '        literal, resource = None, self._recurso(resource)',
     ['test_p1_3_el_literal_se_conserva_como_METADATO']),

]



# Los primitives que no tenían sujeto: revocación, profundidad de outbox y
# `may_execute`. Anclas extraídas del código vivo, cada una única.
MUTANTES_S = [
    ('MS1-revoke-current-fuera-de-tx', "coordination.py",
     '            view = _authenticate_locked(con, token, self._clock())\n            if view is None:\n                raise AuthError("se requiere sesión de runtime válida")\n            con.execute("UPDATE runtime_sessions SET revoked_at=?, revoke_reason=?"\n                        " WHERE runtime_instance=? AND revoked_at IS NULL",\n                        (self._clock(), reason, view.runtime_instance))\n            return view.runtime_instance',
     '            view = self.authenticate(token)\n            if view is None:\n                raise AuthError("se requiere sesión de runtime válida")\n            con.execute("UPDATE runtime_sessions SET revoked_at=?, revoke_reason=?"\n                        " WHERE runtime_instance=? AND revoked_at IS NULL",\n                        (self._clock(), reason, view.runtime_instance))\n            return view.runtime_instance',
     ['test_revoke_current_con_token_desconocido_da_AuthError', 'test_revoke_current_es_IDEMPOTENTE_y_no_resucita']),

    ('MS1-revoke-ajena-pasa', "coordination.py",
     '            if not propia:',
     '            if False:',
     ['test_revoke_session_AJENA_de_otro_principal_se_RECHAZA', 'test_revoke_session_de_OTRO_CARRIL_se_rechaza']),

    # ('MS1-revoke-sin-carril') RETIRADO: la rama es INALCANZABLE — `principal_id`
    # ya deriva del carril (medido), así que ese mutante NO PUEDE morir y un
    # mutante inmortal se lee igual que uno vivo por defecto. La propiedad de la
    # que depende se falsa en `test_el_principal_id_YA_deriva_del_carril`.

    ('MS2-pending-global', "coordination.py",
     '    def pending_outbox(self, token: str) -> int:\n        """Profundidad de la cola DEL CARRIL de la sesión."""\n        return self._outbox_del_carril(token, ("pending",))',
     '    def pending_outbox(self, token: str) -> int:\n        """Profundidad de la cola DEL CARRIL de la sesión."""\n        return self._outbox_global(("pending",))',
     ['test_pending_outbox_cuenta_SOLO_el_carril_de_la_sesion', 'test_pending_outbox_SIN_sesion_valida_se_rechaza']),

    ('MS2-unresolved-global', "coordination.py",
     '        return self._outbox_del_carril(token, ("pending", "failed"))',
     '        return self._outbox_global(("pending", "failed"))',
     ['test_unresolved_outbox_tambien_es_por_carril']),

    ('MS3-may-execute-oraculo', "coordination.py",
     '            if row is None or row["lane"] != view.lane:\n                # MISMO mensaje para «no existe» y «no es de tu carril».\n                raise SubjectNotFound(f"no existe el comando {command_id}")',
     '            if row is None:\n                raise SubjectNotFound(f"no existe el comando {command_id}")\n            if row["lane"] != view.lane:\n                return False',
     ['test_may_execute_un_comando_de_OTRO_CARRIL_es_indistinguible_de_inexistente']),

    ('MS3-may-execute-sin-sesion', "coordination.py",
     '            view = _authenticate_locked(con, token, self._clock())\n            if view is None:\n                raise AuthError("se requiere sesión de runtime válida")\n            row = con.execute("SELECT lane, workstream_id, revision, state FROM commands"',
     '            view = _authenticate_locked(con, token, self._clock())\n            if False:\n                raise AuthError("se requiere sesión de runtime válida")\n            row = con.execute("SELECT lane, workstream_id, revision, state FROM commands"',
     ['test_may_execute_exige_sesion']),

]



# El NO-GO de @contratosbik: la atribución recursiva no NORMALIZA la clave.
# Cada eje de la normalización tiene su mutante propio — quitar los cuatro de
# golpe sólo probaría que "algo" filtra; separados, cada uno acredita SU eje.
MUTANTES_N = [
    ("MN1-nfkd-a-nfc-pierde-homoglifos", "coordination.py",
     '        t = unicodedata.normalize("NFKD", str(k)).lower()',
     '        t = unicodedata.normalize("NFD", str(k)).lower()',
     ["test_los_HOMOGLIFOS_de_security_no_cuelan"]),

    # ⚠️ El ancla se re-extrajo del código vivo al partir el mapa en dos líneas.
    # Antes casaba `0` veces y el arnés abortaba — que es lo que tiene que hacer:
    # un mutante saltado se lee en el informe igual que uno muerto.
    ("MN1-alias-mal-apuntado", "coordination.py",
     '    _ALIAS_RESERVADAS = {"carril": "lane", "rol": "role", "actorid": "principalid",',
     '    _ALIAS_RESERVADAS = {"carril": "lane", "rol": "role", "actorid": "principal_id",',
     ["test_los_alias_apuntan_a_una_reservada_REAL",
      "test_el_alias_de_actor_id_ya_no_cuela"]),

    ("MN1-nfkd-despues-del-regex", "coordination.py",
     '        t = unicodedata.normalize("NFKD", str(k)).lower()\n'
     '        t = "".join(c for c in t if not unicodedata.combining(c))\n'
     '        t = re.sub(r"[^a-z0-9]+", "", t)',
     '        t = re.sub(r"[^a-zA-Z0-9]+", "", str(k)).lower()',
     ["test_los_DIACRITICOS_se_pliegan_y_NO_se_borran_como_separador"]),

    ('MN1-sin-normalizar', "coordination.py",
     '        t = unicodedata.normalize("NFKD", str(k)).lower()\n        t = "".join(c for c in t if not unicodedata.combining(c))\n        t = re.sub(r"[^a-z0-9]+", "", t)\n        return cls._ALIAS_RESERVADAS.get(t, t)',
     '        return str(k)',
     ['test_una_clave_reservada_DISFRAZADA_se_rechaza']),

    ('MN1-sin-minusculas', "coordination.py",
     '        t = unicodedata.normalize("NFKD", str(k)).lower()',
     '        t = unicodedata.normalize("NFKD", str(k))',
     ['test_una_clave_reservada_DISFRAZADA_se_rechaza']),

    ('MN1-sin-separadores', "coordination.py",
     '        t = re.sub(r"[^a-z0-9]+", "", t)',
     '        t = t.strip()',
     ['test_una_clave_reservada_DISFRAZADA_se_rechaza']),

    ('MN1-sin-alias', "coordination.py",
     '        return cls._ALIAS_RESERVADAS.get(t, t)',
     '        return t',
     ['test_una_clave_reservada_DISFRAZADA_se_rechaza']),

    ('MN1-por-subcadena', "coordination.py",
     '        return cls._clave_norm(k) in cls._reservadas_norm()',
     '        return any(r in cls._clave_norm(k) for r in cls._reservadas_norm())',
     ['test_OMEGA_una_clave_que_solo_SE_PARECE_sigue_pasando']),

    ('MN1-conjunto-vacio', "coordination.py",
     '        return frozenset(cls._clave_norm(k) for k in cls._RESERVADAS)',
     '        return frozenset()',
     ['test_el_conjunto_normalizado_se_DERIVA_y_no_puede_nacer_vacio', 'test_una_clave_reservada_DISFRAZADA_se_rechaza']),

    # Las SEIS RAÍCES: se quitan las cuatro inglesas del conjunto (las dos
    # castellanas son alias y caen solas) y luego, aparte, sólo los alias — así
    # cada mitad acredita la suya. Quitar todo de golpe sólo probaría que «algo»
    # filtra.
    ('MN1-sin-las-cuatro-raices', "coordination.py",
     '        "agent", "attribution", "runtime", "credential"))',
     '        ))',
     ['test_las_SEIS_RAICES_son_reservadas',
      'test_las_seis_raices_se_rechazan_ANIDADAS_y_dentro_de_LISTAS',
      'test_las_seis_raices_tampoco_entran_por_PAYLOAD_ni_por_TRACE']),

    ('MN1-sin-los-alias-de-castellano', "coordination.py",
     '    _ALIAS_RESERVADAS = {"carril": "lane", "rol": "role", "actorid": "principalid",\n'
     '                         "agente": "agent", "credencial": "credential"}',
     '    _ALIAS_RESERVADAS = {"carril": "lane", "rol": "role", "actorid": "principalid"}',
     ['test_las_SEIS_RAICES_son_reservadas',
      'test_los_alias_de_castellano_de_las_raices_apuntan_a_su_INGLESA']),

    # La lista blanca de alfabeto: apagada, MAL COLOCADA (después de normalizar,
    # que es donde no ve nada) y DEMASIADO ANCHA (sin `isalnum`, se come los
    # separadores). Las tres por separado: una sola no distingue «existe» de
    # «está bien puesta» ni de «no cierra de más».
    ('MN1-sin-guarda-de-alfabeto', "coordination.py",
     '                ofensor = cls.clave_fuera_del_alfabeto(k)\n                if ofensor is not None:\n',
     '                ofensor = None\n                if ofensor is not None:\n',
     ['test_los_homoglifos_cross_script_NO_LLEGAN_A_PERSISTIRSE']),

    ('MN1-alfabeto-mirado-DESPUES-de-normalizar', "coordination.py",
     '                ofensor = cls.clave_fuera_del_alfabeto(k)\n                if ofensor is not None:\n',
     '                ofensor = cls.clave_fuera_del_alfabeto(cls._clave_norm(k))\n                if ofensor is not None:\n',
     ['test_los_homoglifos_cross_script_NO_LLEGAN_A_PERSISTIRSE']),

    # ── `D19` · el alfabeto de claves, cerrado por adjudicación ────────────
    # Los mutantes de `_SEPARADORES` / `_PUNTUACION_EN_DISCUSION` / `_PERMITIDOS`
    # SE RETIRAN con sus constantes: medían una lista blanca de `4` separadores
    # que `@cto` retiró en el cierre (`00:38:45Z`). No se dejan «por si acaso»:
    # un mutante cuya ancla ya no existe ABORTA el arnés entero.

    # ⊖ LA TRAMPA QUE `@cto` MARCÓ COMO FALSADOR OBLIGATORIO. `'a\tb'.isascii()`
    # es `True`, así que con `isascii()` el tabulador, el salto de línea y el
    # `NUL` ENTRAN — y todo lo demás de la suite sigue verde. Es el único brazo
    # que separa «imprimible» de «ASCII».
    ('MN1-isascii-en-vez-del-rango-deja-entrar-los-de-control', "coordination.py",
     '            if c not in cls._ASCII_IMPRIMIBLE:\n',
     '            if not c.isascii():\n',
     ['test_D19_FALSADOR_los_ASCII_de_CONTROL_se_RECHAZAN',
      'test_D19_los_de_CONTROL_no_llegan_a_PERSISTIRSE',
      'test_D19_el_rango_se_escribe_como_RANGO_y_NO_como_isascii']),

    # ⊖ el rango MÁS ANCHO: hasta Latin-1 deja pasar `ß` y `·`.
    ('MN1-alfabeto-hasta-latin1-cierra-de-menos', "coordination.py",
     '    _ASCII_IMPRIMIBLE = frozenset(chr(c) for c in range(0x20, 0x7F))   # 95\n',
     '    _ASCII_IMPRIMIBLE = frozenset(chr(c) for c in range(0x20, 0x100))   # 95\n',
     ['test_LIMITE_DECLARADO_lo_que_la_lista_blanca_deja_FUERA',
      'test_BARRIDO_por_categoria_ningun_codepoint_de_fuera_entra',
      'test_la_lista_blanca_es_CERRADA_y_no_pregunta_ninguna_propiedad_de_unicode']),

    # ⊕ el rango MÁS ESTRECHO: sin el tramo de puntuación baja, `$schema` y
    # `a+b` vuelven a caerse. Es el lado que acredita que los ⊕ discriminan.
    ('MN1-alfabeto-sin-la-puntuacion-baja-cierra-de-mas', "coordination.py",
     '    _ASCII_IMPRIMIBLE = frozenset(chr(c) for c in range(0x20, 0x7F))   # 95\n',
     '    _ASCII_IMPRIMIBLE = frozenset(chr(c) for c in range(0x30, 0x7F))   # 95\n',
     ['test_D19_el_ASCII_IMPRIMIBLE_se_ACEPTA',
      'test_D19_y_ADEMAS_ENTRAN_de_verdad_y_PERSISTEN',
      'test_la_lista_blanca_es_CERRADA_y_no_pregunta_ninguna_propiedad_de_unicode']),

    # ⊖ VOLVER a interpolar la clave CRUDA: es el estado exacto de `59ad745` y
    # el NO-GO del auditor. Con `k = 'a\nFAKE-LOG…'` el mensaje sale en DOS
    # líneas y la segunda la escribe el cliente.
    ('MN1-el-mensaje-vuelve-a-llevar-la-clave-CRUDA', "coordination.py",
     '                        f"la clave {cls.clave_para_mensaje(k)} lleva "\n',
     '                        f"la clave `{k}` lleva "\n',
     ['test_INVARIANTE_un_rechazo_por_CHARSET_sigue_diciendo_POR_QUE']),

    # ⊖ el saneado a medias: `repr()` escapa los controles pero NO pasa a ASCII,
    # así que el mensaje que describe un homóglifo lo PINTA. Un mutante que sólo
    # quitara el escape no separaría las dos mitades de `ascii()`.
    # ⊖ sin tope: la cardinalidad del mensaje la elige quien ataca.
    ('MN1-la-clave-sin-su-tope-propio', "coordination.py",
     '        return cls.detalle_seguro(k, tope=cls._CLAVE_EN_MENSAJE_MAX)\n',
     '        return cls.detalle_seguro(k)\n',
     ['test_NOGO_la_clave_en_el_mensaje_esta_ACOTADA',
      'test_el_MARCADOR_de_truncado_es_ASCII']),

    # ⊖ el texto que MIENTE tras `D19`.
    ('MN1-el-mensaje-vuelve-a-citar-el-alfabeto-viejo', "coordination.py",
     '                        f"alfabeto admitido [0x20-0x7E] (ASCII imprimible) tras "\n',
     '                        f"alfabeto admitido [a-z0-9] tras "\n',
     ['test_NOGO_el_TEXTO_del_mensaje_dice_LA_VERDAD_tras_D19']),

    # ── NO-GO 2 · el EMBUDO del detalle ────────────────────────────────────
    # Cuatro mutantes, uno por cada pieza de la cura. Separados a propósito: uno
    # solo probaría que «algo» sanea, y el defecto de la ronda anterior fue
    # justamente que UNA pieza saneaba y las de al lado no.

    # ⊖ quitar el embudo del mensaje de ATRIBUCION (el F1 del auditor).
    ('MN1-atribucion-sin-embudo', "coordination.py",
     '                    raise AttributionRejected(cls.detalle_seguro(\n'
     '                        f"la clave {cls.clave_para_mensaje(k)} la pone el "\n',
     '                    raise AttributionRejected((\n'
     '                        f"la clave `{k}` la pone el "\n',
     ['test_INVARIANTE_un_rechazo_por_ATRIBUCION_sigue_diciendo_POR_QUE']),

    # ⚖️ AQUÍ VIVÍA `MN1-contexto-crudo-en-charset`, RETIRADO por EQUIVALENTE.
    # Medido con un directorio limpio por mutante: da resultados IDÉNTICOS al
    # sano en las tres entradas que probé (contexto de 80000, anidado, y base),
    # porque en ESE mensaje la razón (`U+XXXX`) va ANTES que el contexto, así que
    # truncar `donde` no cambia nada observable. Un mutante que no puede
    # discriminar se retira: dejarlo «vivo» ensucia el informe y entrena a
    # ignorar supervivientes. El tope interno SE QUEDA en el código por
    # consistencia, y `test_INVARIANTE_y_el_de_CHARSET_tambien_aunque_ahi_no_
    # discrimine` fija la equivalencia Y avisa si alguien reordena el mensaje.

    # ⊖ NO congelar el valor al borde: vuelve la ventana del `__format__` (F3).
    ('MN1-sin-congelar-el-valor-vuelve-el-TOCTOU', "coordination.py",
     '                k = str(k_vivo)\n',
     '                k = k_vivo\n',
     ['test_F3_el_TOCTOU_de_str_y_format_esta_MUERTO']),






    # ⊖ y el TERCER mensaje, el de profundidad, que se me quedo fuera del embudo
    # y cazo el BARRIDO — no yo.
    # 🔻 JUEZ CAMBIADO, por SUPERVIVENCIA medida. `test_INVARIANTE_un_rechazo_
    # por_PROFUNDIDAD_sigue_diciendo_POR_QUE` pide que la RAZON sobreviva y que
    # el mensaje quepa en `260`; el mutante sustituye el contexto por un literal
    # de `6` signos, o sea lo hace MAS CORTO, y la razon sobra de sitio. Un tope
    # no se falsa encogiendo el dato: se falsa por lo que el tope PROTEGE, que
    # aqui son DOS cosas y por eso hay DOS jueces — FIDELIDAD (el `field` nombra
    # el campo real; el mutante dice `intent` venga de donde venga, o sea MIENTE,
    # la clase que `D19` cerro) y EMBUDO (con camino real enorme el `field` sale
    # acotado a `_CONTEXTO_EN_MENSAJE_MAX` y con `MARCA_TRUNCADO`: medido `96`
    # con marca en sano contra `6` sin ella en el mutante).
    # 🔻 TRASLADADO, NO BORRADO. Su ancla (`campo = detalle_seguro(donde, …)`)
    # DEJO DE EXISTIR con la cura del `field`: ya no hay un trozo externo que
    # meter en el atributo, hay una RAIZ CANONICA. El pre-flight lo cazo como
    # huerfano (ancla `0` casos) en la misma corrida en que lo cure — que es
    # exactamente para lo que se escribio.
    # La INTENCION del mutante se conserva en el diseño nuevo: que lo EXTERNO no
    # entre en `field`. Ahora eso se rompe devolviendo el `donde` completo en vez
    # de su raiz; `_rle` lo caza con su `if`/`raise` del enum.
    # ⛔ Y NO se le deja el juez viejo: `..._pasa_por_el_EMBUDO` se reescribio
    # como `..._esta_en_el_ENUM_CERRADO` porque el embudo protegia un valor que
    # jamas debio estar en un atributo del contrato.
    ('MN1-el-camino-entero-en-el-field', 'coordination.py',
     '            raise _rle("depth", _campo_raiz(donde), cls._PROFUNDIDAD_MAX, prof,\n',
     '            raise _rle("depth", donde, cls._PROFUNDIDAD_MAX, prof,\n',
     ['test_el_CAMPO_del_rechazo_por_PROFUNDIDAD_esta_en_el_ENUM_CERRADO',
      'test_el_CAMPO_del_rechazo_por_PROFUNDIDAD_nombra_el_campo_REAL']),

    # ⊖ el código que MIENTE, que es la mitad de `D19` que arregla algo que yo
    # había roto: acusar de atribución a quien escribió `$schema`.
    ('MN1-el-charset-devuelve-el-codigo-de-ATRIBUCION', "coordination.py",
     '                    raise KeyCharsetRejected(cls.detalle_seguro(\n',
     '                    raise AttributionRejected(cls.detalle_seguro(\n',
     ['test_D19_el_CODIGO_del_charset_es_PROPIO_y_no_MIENTE',
      'test_los_homoglifos_cross_script_NO_LLEGAN_A_PERSISTIRSE',
      'test_D19_los_de_CONTROL_no_llegan_a_PERSISTIRSE']),

    # ⊖ el código nuevo FUERA del vocabulario cerrado: la pasarela lo derivaría
    # de una tabla que no lo tiene.
    ('MN1-el-codigo-nuevo-no-esta-en-el-vocabulario', "coordination.py",
     '    "KEY_CHARSET_REJECTED",   # clave con codepoint fuera de ASCII imprimible\n',
     '',
     ['test_D19_el_CODIGO_del_charset_es_PROPIO_y_no_MIENTE']),

    ('MN1-no-persiste-el-rechazo', "coordination.py",
     '        self._sin_atribucion(intent, "intent")',
     '        pass',
     ['test_ninguna_variante_llega_a_PERSISTIRSE']),

]



# El TOCTOU del reloj en las dos puertas del lease. El mutante REVIERTE la
# cura —lee el reloj antes del seam y lo reusa dentro—, que es exactamente el
# estado del freeze 32c3646. Las anclas son largas a propósito: el fragmento
# es idéntico en `acquire` y `renew`, así que se extienden hasta la línea que
# distingue a cada una. Un ancla ambigua muta la puerta que no crees medir.
MUTANTES_T = [
    ('MT1-acquire-reloj-fuera', 'coordination.py',
     '        literal, resource = str(resource), self._recurso(resource)\n        if self.authenticate(token) is None:      # barato, NO decide\n            raise AuthError("se requiere sesión de runtime válida")\n        self._tras_precheck()\n        with self._tx() as con:\n            view = _authenticate_locked(con, token, self._clock())\n            if view is None:\n                raise AuthError(\n                    "sesión invalidada entre la comprobación y la escritura")\n            # ⏱️ EL RELOJ SE LEE AQUÍ, DENTRO DE LA TRANSACCIÓN Y TRAS EL LOCK.\n            # Estaba antes de `_tras_precheck()` y del `BEGIN IMMEDIATE`, y entre\n            # esa lectura y el `UPDATE` cabía el vencimiento ENTERO. Medido con\n            # el seam (`ttl=100`, el hook avanza `+101`):\n            #   renew   -> ACEPTABA, fencing 1->1, y devolvía expires_at=1000100\n            #              con now=1000101: prometía continuidad Y entregaba una\n            #              valla caducada al nacer.\n            #   acquire -> el gemelo: veía el lease VIVO con el reloj viejo y\n            #              NEGABA un relevo legítimo.\n            # Es la misma clase que este fichero ya persigue en todas partes:\n            # ninguna decisión se apoya en una segunda lectura de algo mutable —\n            # aquí lo mutable era el TIEMPO, que es el que nadie vigila.\n            now = self._clock()\n',
     '        literal, resource = str(resource), self._recurso(resource)\n        if self.authenticate(token) is None:      # barato, NO decide\n            raise AuthError("se requiere sesión de runtime válida")\n        _now_viejo = self._clock()\n        self._tras_precheck()\n        with self._tx() as con:\n            view = _authenticate_locked(con, token, self._clock())\n            if view is None:\n                raise AuthError(\n                    "sesión invalidada entre la comprobación y la escritura")\n            # ⏱️ EL RELOJ SE LEE AQUÍ, DENTRO DE LA TRANSACCIÓN Y TRAS EL LOCK.\n            # Estaba antes de `_tras_precheck()` y del `BEGIN IMMEDIATE`, y entre\n            # esa lectura y el `UPDATE` cabía el vencimiento ENTERO. Medido con\n            # el seam (`ttl=100`, el hook avanza `+101`):\n            #   renew   -> ACEPTABA, fencing 1->1, y devolvía expires_at=1000100\n            #              con now=1000101: prometía continuidad Y entregaba una\n            #              valla caducada al nacer.\n            #   acquire -> el gemelo: veía el lease VIVO con el reloj viejo y\n            #              NEGABA un relevo legítimo.\n            # Es la misma clase que este fichero ya persigue en todas partes:\n            # ninguna decisión se apoya en una segunda lectura de algo mutable —\n            # aquí lo mutable era el TIEMPO, que es el que nadie vigila.\n            now = _now_viejo\n',
     ['test_acquire_SI_releva_cuando_vence_en_la_ventana']),

    ('MT1-renew-reloj-fuera', 'coordination.py',
     '        resource = self._recurso(resource)\n        if self.authenticate(token) is None:      # barato, NO decide\n            raise AuthError("se requiere sesión de runtime válida")\n        self._tras_precheck()\n        with self._tx() as con:\n            view = _authenticate_locked(con, token, self._clock())\n            if view is None:\n                raise AuthError(\n                    "sesión invalidada entre la comprobación y la escritura")\n            # ⏱️ EL RELOJ SE LEE AQUÍ, DENTRO DE LA TRANSACCIÓN Y TRAS EL LOCK.\n            # Estaba antes de `_tras_precheck()` y del `BEGIN IMMEDIATE`, y entre\n            # esa lectura y el `UPDATE` cabía el vencimiento ENTERO. Medido con\n            # el seam (`ttl=100`, el hook avanza `+101`):\n            #   renew   -> ACEPTABA, fencing 1->1, y devolvía expires_at=1000100\n            #              con now=1000101: prometía continuidad Y entregaba una\n            #              valla caducada al nacer.\n            #   acquire -> el gemelo: veía el lease VIVO con el reloj viejo y\n            #              NEGABA un relevo legítimo.\n            # Es la misma clase que este fichero ya persigue en todas partes:\n            # ninguna decisión se apoya en una segunda lectura de algo mutable —\n            # aquí lo mutable era el TIEMPO, que es el que nadie vigila.\n            now = self._clock()\n',
     '        resource = self._recurso(resource)\n        if self.authenticate(token) is None:      # barato, NO decide\n            raise AuthError("se requiere sesión de runtime válida")\n        _now_viejo = self._clock()\n        self._tras_precheck()\n        with self._tx() as con:\n            view = _authenticate_locked(con, token, self._clock())\n            if view is None:\n                raise AuthError(\n                    "sesión invalidada entre la comprobación y la escritura")\n            # ⏱️ EL RELOJ SE LEE AQUÍ, DENTRO DE LA TRANSACCIÓN Y TRAS EL LOCK.\n            # Estaba antes de `_tras_precheck()` y del `BEGIN IMMEDIATE`, y entre\n            # esa lectura y el `UPDATE` cabía el vencimiento ENTERO. Medido con\n            # el seam (`ttl=100`, el hook avanza `+101`):\n            #   renew   -> ACEPTABA, fencing 1->1, y devolvía expires_at=1000100\n            #              con now=1000101: prometía continuidad Y entregaba una\n            #              valla caducada al nacer.\n            #   acquire -> el gemelo: veía el lease VIVO con el reloj viejo y\n            #              NEGABA un relevo legítimo.\n            # Es la misma clase que este fichero ya persigue en todas partes:\n            # ninguna decisión se apoya en una segunda lectura de algo mutable —\n            # aquí lo mutable era el TIEMPO, que es el que nadie vigila.\n            now = _now_viejo\n',
     ['test_renew_NO_acepta_un_lease_que_vence_en_la_ventana', 'test_renew_NUNCA_devuelve_un_expires_at_ya_pasado', 'test_el_expires_at_nuevo_se_calcula_con_el_reloj_de_DENTRO']),

]


# El MISMO TOCTOU del reloj, en las DOS puertas de la SESIÓN. El mutante
# REVIERTE la cura —lee el reloj antes del seam y lo reusa dentro—, que es
# exactamente el estado del freeze `9793f8a`. Y hay un tercer eje que NO es el
# reloj: el `ttl_s` del ARGUMENTO produce el mismo estado sin ninguna carrera,
# así que su guarda tiene su propio mutante o no está acreditada.
MUTANTES_SE = [
    # ⚠️ EL ANCLA LLEGA HASTA EL `now = self._clock()` DE DENTRO, y no se corta
    # antes. Mi primera versión sólo INSERTABA `_now_viejo` arriba y no tocaba la
    # lectura de dentro: el mutante era un NO-OP y SOBREVIVIÓ — se leyó en el
    # informe como «el test no discrimina» cuando lo que no discriminaba era el
    # mutante. Un mutante que no cambia el comportamiento acredita cero, y se
    # siente igual de riguroso que uno que sí.
    ('MSE-open-reloj-fuera', 'coordination.py',
     '        token = secrets.token_urlsafe(32)\n        rti = _new_id("rti")\n        ref = self.credential_ref(credential)\n        self._tras_precheck()\n        with self._tx() as con:\n            binding = _resolver_ligadura(con, ref)\n            if binding is None:\n                raise AuthError(\n                    "credencial retirada mientras se emitía la sesión: el mapa "\n                    "cambió entre la comprobación y la escritura")\n            # ⏱️ EL RELOJ SE LEE AQUÍ, DENTRO DE LA TRANSACCIÓN Y TRAS EL LOCK.\n            # Estaba antes de `_tras_precheck()` y del `BEGIN IMMEDIATE`, y el\n            # `expires_at` que se INSERTA salía de esa lectura vieja. Medido con\n            # el seam (`ttl=100`, el hook avanza `+500`): la sesión se escribía\n            # con `expires_at=1000100` y `now=1000500`, o sea NACÍA VENCIDA, y\n            # `authenticate()` sobre el token recién entregado daba `None`.\n            # Falla cerrado, sí — y aun así es la misma clase que la cura del\n            # lease: ninguna decisión se apoya en una segunda lectura de algo\n            # mutable, y el tiempo es lo mutable que nadie vigila.\n            now = self._clock()\n',
     '        token = secrets.token_urlsafe(32)\n        rti = _new_id("rti")\n        ref = self.credential_ref(credential)\n        _now_viejo = self._clock()\n        self._tras_precheck()\n        with self._tx() as con:\n            binding = _resolver_ligadura(con, ref)\n            if binding is None:\n                raise AuthError(\n                    "credencial retirada mientras se emitía la sesión: el mapa "\n                    "cambió entre la comprobación y la escritura")\n            # ⏱️ EL RELOJ SE LEE AQUÍ, DENTRO DE LA TRANSACCIÓN Y TRAS EL LOCK.\n            # Estaba antes de `_tras_precheck()` y del `BEGIN IMMEDIATE`, y el\n            # `expires_at` que se INSERTA salía de esa lectura vieja. Medido con\n            # el seam (`ttl=100`, el hook avanza `+500`): la sesión se escribía\n            # con `expires_at=1000100` y `now=1000500`, o sea NACÍA VENCIDA, y\n            # `authenticate()` sobre el token recién entregado daba `None`.\n            # Falla cerrado, sí — y aun así es la misma clase que la cura del\n            # lease: ninguna decisión se apoya en una segunda lectura de algo\n            # mutable, y el tiempo es lo mutable que nadie vigila.\n            now = _now_viejo\n',
     ['test_open_session_NO_nace_vencida_cuando_el_reloj_corre_en_la_ventana',
      'test_ninguna_fila_de_sesion_nace_con_expires_at_ya_pasado']),

    ('MSE-refresh-reloj-fuera', 'coordination.py',
     '        nuevo = secrets.token_urlsafe(32)\n        rti = _new_id("rti")\n        self._tras_precheck()\n        with self._tx() as con:\n            # ⏱️ EL RELOJ SE LEE AQUÍ, DENTRO DE LA TRANSACCIÓN Y TRAS EL LOCK.\n            # Estaba antes de `_tras_precheck()` y del `BEGIN IMMEDIATE`, y con\n            # ese valor viejo se comparaba `s.expires_at > now`: entre la lectura\n            # y el `SELECT` cabía el VENCIMIENTO ENTERO del padre. Medido con el\n            # seam (`ttl` del padre `100`, el hook avanza `+101`):\n            #   · el padre YA estaba vencido y aun así casaba,\n            #   · se le revocaba con motivo `rotated` —un padre muerto no se\n            #     rota, se deja morir—,\n            #   · y el hijo nacía con `expires_at = now_viejo + ttl`, o sea VIVO:\n            #     `authenticate(hijo)` daba `True` con el padre muerto.\n            # Eso no es rotar: es RESUCITAR. Quien conserve un token caducado\n            # recupera sesión válida con sólo refrescarlo, y el vencimiento deja\n            # de ser exigible. Con el reloj dentro, el padre vencido no casa y la\n            # llamada muere en `AuthError`.\n            now = self._clock()\n',
     '        nuevo = secrets.token_urlsafe(32)\n        rti = _new_id("rti")\n        _now_viejo = self._clock()\n        self._tras_precheck()\n        with self._tx() as con:\n            # ⏱️ EL RELOJ SE LEE AQUÍ, DENTRO DE LA TRANSACCIÓN Y TRAS EL LOCK.\n            # Estaba antes de `_tras_precheck()` y del `BEGIN IMMEDIATE`, y con\n            # ese valor viejo se comparaba `s.expires_at > now`: entre la lectura\n            # y el `SELECT` cabía el VENCIMIENTO ENTERO del padre. Medido con el\n            # seam (`ttl` del padre `100`, el hook avanza `+101`):\n            #   · el padre YA estaba vencido y aun así casaba,\n            #   · se le revocaba con motivo `rotated` —un padre muerto no se\n            #     rota, se deja morir—,\n            #   · y el hijo nacía con `expires_at = now_viejo + ttl`, o sea VIVO:\n            #     `authenticate(hijo)` daba `True` con el padre muerto.\n            # Eso no es rotar: es RESUCITAR. Quien conserve un token caducado\n            # recupera sesión válida con sólo refrescarlo, y el vencimiento deja\n            # de ser exigible. Con el reloj dentro, el padre vencido no casa y la\n            # llamada muere en `AuthError`.\n            now = _now_viejo\n',
     ['test_refresh_NO_acepta_un_padre_que_vence_en_la_ventana',
      'test_refresh_de_un_padre_VENCIDO_no_deja_hijo_que_AUTENTIQUE',
      'test_al_padre_vencido_NO_se_le_pone_el_motivo_rotated',
      'test_el_expires_at_del_hijo_se_calcula_con_el_reloj_de_DENTRO']),

    # El otro medio del refresh: aunque el reloj esté DENTRO, si el predicado de
    # vencimiento desaparece del `SELECT` el padre muerto vuelve a casar. Son
    # dos piezas y hacen falta las dos; un solo mutante no separa cuál sostiene.
    ('MSE-refresh-sin-predicado-de-vencimiento', 'coordination.py',
     '                " WHERE s.token_hash=? AND s.revoked_at IS NULL"\n'
     '                "   AND s.expires_at>? AND s.generation=?",\n'
     '                (_sha256(token), now, gen)).fetchone()',
     '                " WHERE s.token_hash=? AND s.revoked_at IS NULL"\n'
     '                "   AND s.expires_at>-1e18 AND s.generation=?",\n'
     '                (_sha256(token), gen)).fetchone()',
     ['test_refresh_NO_acepta_un_padre_que_vence_en_la_ventana',
      'test_refresh_de_un_padre_VENCIDO_no_deja_hijo_que_AUTENTIQUE']),

    ('MSE-open-sin-guarda-de-ttl', 'coordination.py',
     '        if ttl_s <= 0:\n'
     '            raise OperationInvalid(\n'
     '                f"ttl_s={ttl_s} no es positivo: la sesión nacería ya vencida y el "',
     '        if False:\n'
     '            raise OperationInvalid(\n'
     '                f"ttl_s={ttl_s} no es positivo: la sesión nacería ya vencida y el "',
     ['test_open_session_con_ttl_no_positivo_se_RECHAZA']),

    ('MSE-refresh-sin-guarda-de-ttl', 'coordination.py',
     '        if ttl_s <= 0:                     # argumento, no credencial — ver `open_session`\n'
     '            raise OperationInvalid(\n'
     '                f"ttl_s={ttl_s} no es positivo: la rotación revocaría la sesión "',
     '        if False:\n'
     '            raise OperationInvalid(\n'
     '                f"ttl_s={ttl_s} no es positivo: la rotación revocaría la sesión "',
     ['test_refresh_con_ttl_no_positivo_se_RECHAZA_y_NO_mata_la_viva']),

    # ⊖ de la guarda al revés: `< 0` en vez de `<= 0` deja pasar el `0`, que es
    # justo el valor que produce la sesión muerta. Un mutante que sólo apaga la
    # guarda no distingue «existe» de «está bien puesta».
    ('MSE-guarda-de-ttl-mal-puesta', 'coordination.py',
     '        if ttl_s <= 0:\n'
     '            raise OperationInvalid(\n'
     '                f"ttl_s={ttl_s} no es positivo: la sesión nacería ya vencida y el "',
     '        if ttl_s < 0:\n'
     '            raise OperationInvalid(\n'
     '                f"ttl_s={ttl_s} no es positivo: la sesión nacería ya vencida y el "',
     ['test_open_session_con_ttl_no_positivo_se_RECHAZA']),

]


# El NO-GO del auditor: la FORMA se validaba ANTES que la SESIÓN, y eso filtra un
# ORÁCULO del contrato a quien no ha demostrado ser nadie. El mutante DEVUELVE el
# `_sin_atribucion` arriba, que es el estado exacto de `811f635`. Las anclas
# llegan hasta la llamada movida — un ancla que sólo cubriera el `if` de
# autenticar produciría una inserción muerta, que es el no-op que ya me costó una
# corrida entera de 50/52.
MUTANTES_O = [
    ('MO1-accept-forma-antes-de-auth', 'coordination.py',
     '        # gratis sobre el contrato para quien no ha demostrado ser nadie.\n        if self.authenticate(token) is None:      # barato, NO decide\n',
     '        # gratis sobre el contrato para quien no ha demostrado ser nadie.\n        if False:      # barato, NO decide\n',
     ['test_con_token_INVALIDO_un_cuerpo_sucio_da_AuthError_y_NO_delata_la_reservada',
      'test_el_cuerpo_SUCIO_y_el_LIMPIO_dan_LA_MISMA_clase_de_error',
      'test_CENSO_ninguna_puerta_publica_valida_la_FORMA_antes_de_AUTENTICAR']),

    ('MO1-submit-forma-antes-de-auth', 'coordination.py',
     '        self._guard_mutable()\n        if self.authenticate(token) is None:      # barato, NO decide\n            raise AuthError("se requiere sesión de runtime válida")\n        # La MISMA inversión que',
     '        self._guard_mutable()\n        if False:      # barato, NO decide\n            raise AuthError("se requiere sesión de runtime válida")\n        # La MISMA inversión que',
     ['test_con_token_INVALIDO_un_cuerpo_sucio_da_AuthError_y_NO_delata_la_reservada',
      'test_el_cuerpo_SUCIO_y_el_LIMPIO_dan_LA_MISMA_clase_de_error',
      'test_CENSO_ninguna_puerta_publica_valida_la_FORMA_antes_de_AUTENTICAR']),

]


# La GARANTIA GLOBAL en la base de `JournalError`. Cada mutante ataca UNA de las
# cuatro propiedades por separado: uno solo probaria que «algo» sanea, y el
# defecto de las tres rondas anteriores fue justamente que una pieza saneaba y
# las de al lado no. Mas dos ANTI-VACUIDAD, porque un barrido que deja de
# levantar sale verde por no llegar a ninguna puerta.
MUTANTES_G = [
    ('MG1-la-base-no-sanea', 'coordination.py',
     '        super().__init__(_saneado(" \u00b7 ".join(congelados)) if congelados else "")\n',
     '        super().__init__(" \u00b7 ".join(congelados) if congelados else "")\n',
     ['test_GLOBAL_ninguna_familia_refleja_el_veneno',
      'test_CENSO_todas_las_subclases_de_JournalError_heredan_el_saneado']),

    # ⚠️ Este apuntaba a un test de la suite de NORMALIZACION mientras los `MG`
    # corren contra la GLOBAL: el `-k` no seleccionaba nada (rc=5) y el arnes lo
    # canto como «no mide». El test vive ahora en la suite correcta.
    ('MG1-sin-plegar-diacriticos-el-acento-se-escapa', 'coordination.py',
     '    t = "".join(c for c in t if not unicodedata.combining(c))\n    return ascii(t)[1:-1]\n',
     '    t = "".join(c for c in t if True)\n    return ascii(t)[1:-1]\n',
     ['test_ALTO_el_acento_se_PLIEGA_y_no_se_ESCAPA']),

    ('MG1-sin-escapar-solo-recorta', 'coordination.py',
     '    return ascii(t)[1:-1]\n',
     '    return t\n',
     ['test_GLOBAL_ninguna_familia_refleja_el_veneno',
      'test_CENSO_todas_las_subclases_de_JournalError_heredan_el_saneado']),

    # ⊖ la ARIDAD: volver a sanear solo `args[0]` y solo si ya es `str`.
    ('MG1-solo-sanea-el-primer-arg-si-ya-es-str', 'coordination.py',
     '        congelados = [str(a) for a in args]\n'
     '        super().__init__(_saneado(" \u00b7 ".join(congelados)) if congelados else "")\n',
     '        if args and isinstance(args[0], str):\n'
     '            args = (_saneado(args[0]),) + tuple(args[1:])\n'
     '        super().__init__(*args)\n',
     ['test_ALTO_la_garantia_NO_depende_de_la_ARIDAD_ni_del_TIPO']),

    # ⊖ el tope NEGATIVO sin clampar: `t[:-5]` recorta desde el final.
    ('MG1-tope-negativo-sin-clampar', 'coordination.py',
     '    limite = max(0, DETALLE_MAX if tope is None else tope)\n    if limite == 0:\n',
     '    limite = DETALLE_MAX if tope is None else tope\n    if limite == 0:\n',
     ['test_ALTO_un_tope_NEGATIVO_o_MINUSCULO_sigue_acotando']),

    # ⊖ DOS verdades otra vez: que `Journal` deje de derivar del modulo.
    ('MG1-dos-constantes-otra-vez', 'coordination.py',
     '    _DETALLE_MAX = DETALLE_MAX      # DERIVADA: una sola verdad, la del modulo\n',
     '    _DETALLE_MAX = 320\n',
     ['test_ALTO_hay_UNA_sola_funcion_de_saneado_y_UNAS_solas_constantes']),

    ('MG1-sin-tope-global', 'coordination.py',
     '    limite = max(0, DETALLE_MAX if tope is None else tope)\n',
     '    limite = max(0, 10 ** 9 if tope is None else tope)\n',
     ['test_GLOBAL_ninguna_familia_refleja_el_veneno']),

    # ⊕ ANTI-VACUIDAD: si el barrido deja de levantar, su verde no vale nada.
    ('MG1-antivacuidad-el-barrido-deja-de-levantar', 'coordination.py',
     '        canonico = g.canonical_kind(intent.get("kind"))\n',
     '        canonico = g.canonical_kind(intent.get("kind")) or "FYI"\n',
     ['test_GLOBAL_el_barrido_no_es_VACUO_todas_las_familias_LEVANTAN']),

    # ⊕ ANTI-VACUIDAD del censo de SUBCLASES: sacar una clase de la jerarquia le
    # quita el saneado heredado. Antes apuntaba al censo de `raise` FUERA de la
    # jerarquia y SOBREVIVIO — porque esos `raise` pasan un `Call`, no un
    # f-string, asi que el filtro `JoinedStr` no los ve y el censo no se movia.
    # Estaba MAL APUNTADO: el mutante era bueno, el test nombrado no lo miraba.
    ('MG1-antivacuidad-el-censo-mira-poblacion-vacia', 'coordination.py',
     'class KeyCharsetRejected(JournalError):\n',
     'class KeyCharsetRejected(Exception):\n',
     ['test_CENSO_todas_las_subclases_de_JournalError_heredan_el_saneado']),

]


    # ⚖️ AQUI VIVIAN 5 mutantes `MN1-*` del sanitizador: `detalle-sin-tope-global`,
    # `el-marcador-se-suma-al-tope`, `tope-menor-que-el-marcador-desborda`,
    # `marcador-de-truncado-no-ascii` y `detalle-con-repr-en-vez-de-ascii`.
    # Apuntaban al CUERPO de `Journal.detalle_seguro`, que ya no existe: ahora
    # delega en `_saneado`. Al fundir DOS implementaciones en UNA, sus mutantes
    # se funden tambien — los `MG1-*` de abajo miden lo mismo sobre la unica que
    # queda. Se retiran en vez de dejarlos ABORTANDO el arnes, y queda escrito
    # para que nadie los eche de menos y los reponga apuntando a la nada.

# Los DOS `ValueError` publicos. Cada mutante quita UNA pieza: el saneado
# exterior, el tope del componente, y el congelado. Separados porque el defecto
# de las rondas anteriores fue exactamente que una pieza cubria y las de al lado
# no — y un mutante por las tres a la vez no distingue cual sostiene.
MUTANTES_VE = [
    ('MVE-record-rejection-sin-embudo', 'coordination.py',
     '            raise ValueError(_saneado(\n'
     '                f"motivo {_saneado(motivo, DETALLE_MAX // 4)} fuera del "\n'
     '                f"vocabulario cerrado"))\n',
     '            raise ValueError(\n'
     '                f"motivo `{motivo}` fuera del "\n'
     '                f"vocabulario cerrado")\n',
     ['test_VE_record_rejection_no_refleja_el_crudo',
      'test_CENSO_ningun_raise_del_fichero_queda_FUERA_de_la_jerarquia']),

    ('MVE-record-denial-sin-embudo', 'coordination.py',
     '            raise ValueError(_saneado(\n'
     '                f"motivo {_saneado(motivo, DETALLE_MAX // 4)} fuera del "\n'
     '                f"vocabulario cerrado: un motivo libre lleva el dato que lo "\n'
     '                f"provoco y hace la agregacion inutil"))\n',
     '            raise ValueError(\n'
     '                f"motivo `{motivo}` fuera del "\n'
     '                f"vocabulario cerrado: un motivo libre lleva el dato que lo "\n'
     '                f"provoco y hace la agregacion inutil")\n',
     ['test_VE_record_denial_no_refleja_el_crudo',
      'test_CENSO_ningun_raise_del_fichero_queda_FUERA_de_la_jerarquia']),

    # 🩸 RESTAURADOS. Los retire como EQUIVALENTES tras cuatro sondas que
    # apuntaban TODAS al mismo lado (`__str__` NO canonico). El auditor probo la
    # direccion CONTRARIA —valor subyacente NO canonico, `__str__` que devuelve
    # `POLICY_DENIED`— y SI discriminan:
    #     sano     acepta · 1 llamada a str() · persiste POLICY_DENIED
    #     mutante  ValueError · denials = 0
    # La propiedad que prueban no es el mensaje: es que el valor CONGELADO
    # gobierne la comprobacion de PERTENENCIA. Mi `79/79` estaba SOBREDECLARADO.
    ('MVE-sin-congelar-record-rejection', 'coordination.py',
     '        motivo = str(reason)            # 🧊 idem: una lectura, la misma para todo\n',
     '        motivo = reason\n',
     ['test_FBM_record_rejection_congela_ANTES_de_comprobar_pertenencia']),

    ('MVE-sin-congelar-record-denial', 'coordination.py',
     '        motivo = str(reason)\n        if motivo not in REASON_CODES:\n            # `ValueError` SE MANTIENE',
     '        motivo = reason\n        if motivo not in REASON_CODES:\n            # `ValueError` SE MANTIENE',
     ['test_FBM_record_denial_congela_ANTES_de_comprobar_pertenencia']),

    # ⚖️ (aqui vivia la nota de equivalencia, RETIRADA: era falsa)
    # Cuatro sondas con directorio limpio por variante —mensaje, contador de
    # lecturas, `reason` persistido en `denials`, y `subject_id` del camino
    # AGREGADO con la cuota forzada a 2— dieron IDENTICO al sano. La razon:
    # `_saneado()` ya llama a `str()` por dentro, asi que el objeto vivo nunca
    # llega a un f-string. El congelado SE QUEDA en el codigo (consistencia, y
    # una refactorizacion lo volveria discriminante), y
    # `test_EQUIVALENCIA_DECLARADA_el_congelado_del_motivo_no_tiene_falsador_hoy`
    # avisa si eso pasa. Un mutante que no puede distinguir se retira.

    # ⊕ ANTI-VACUIDAD: si la guarda no levantara, TODOS los ⊖ de arriba pasarian
    # por no llegar nunca al `raise`, y el ⊕ del camino bueno lo caza.
    ('MVE-antivacuidad-la-guarda-no-levanta', 'coordination.py',
     '        if motivo not in REASON_CODES:\n'
     '            raise ValueError(_saneado(\n'
     '                f"motivo {_saneado(motivo, DETALLE_MAX // 4)} fuera del "\n'
     '                f"vocabulario cerrado"))\n',
     '        if False:\n'
     '            raise ValueError(_saneado(\n'
     '                f"motivo {_saneado(motivo, DETALLE_MAX // 4)} fuera del "\n'
     '                f"vocabulario cerrado"))\n',
     ['test_VE_ANTIVACUIDAD_la_guarda_levanta_ANTES_de_resolver_la_identidad']),

]


# La COTA del `intent` y las dos propiedades nuevas del saneado. Cada mutante
# ataca UNA pieza: el operador de la frontera, la unidad, la posicion (antes o
# despues de escribir), el recorte crudo y la politica de cola.
MUTANTES_C = [
    # 🔻 REANCLADO POR FORMA al normalizador unico (`_congelar`). El grupo
    # anterior apuntaba a `_pesa_mas_de` / `_validar_estructura` /
    # `_validar_cardinalidad`, que el bloque A retiro: sus anclas casaban `0`
    # veces y el runner ABORTABA la corrida entera (`n != 1` -> `return 2`).
    # No pasaba verde —eso lo dije mal en un informe y lo corrijo—, pero dejaba
    # el arnes inutilizable. ✅ **Eso YA NO ES ASI**: el grupo esta reanclado y
    # corre con los demas; esto se conserva como historia, no como estado.
    #
    # ⚠️ COMO SE MIDIO EL `9/9`: **no con este runner**. En el entorno donde se
    # corrio no hay `pytest` en NINGUN interprete. El juez fue un focal
    # autonomo con los mismos asertos y la misma disciplina (ancla unica · el
    # reemplazo cambia el fuente · el mutante muere solo si cae su PROPIA
    # familia, no si el juez se pone rojo «por algo»). Los nombres de test de
    # abajo son el objetivo para quien SI tenga `pytest`: **esa correspondencia
    # NO esta verificada aqui** y no la presento como medida.
    #
    # 🩸 Y un defecto del juez, que casi me cuesta un falso «equivalente»:
    # `MC1-frontera-exclusiva` salio rojo SIN una sola linea de fallo porque el
    # ⊕ levantaba y MATABA el script. Un juez que muere no distingue «fallo» de
    # «revento». El ⊕ se endurecio para que una excepcion cuente como fallo.

    # ⊖ FRONTERA de bytes: `>` por `>=` mueve el inclusivo. (focal F-C1)
    ('MC1-frontera-exclusiva', 'coordination.py',
     '        if n > tope_bytes:\n',
     '        if n >= tope_bytes:\n',
     ['test_N_exacto_se_ACEPTA', 'test_la_frontera_es_INCLUSIVA_y_se_prueba_con_el_PAR']),

    # ⊖ UNIDAD: caracteres en vez de bytes UTF-8. (focal F-C2)
    ('MC1-unidad-caracteres-no-bytes', 'coordination.py',
     '        n = len(_canonical(snap).encode("utf-8"))\n',
     '        n = len(_canonical(snap))\n',
     ['test_la_UNIDAD_es_el_byte_UTF8_no_el_caracter']),

    # ⊖ el chequeo EXACTO apagado. 🔑 Lo que lo delata NO es el ASCII —ahi la
    #   cota incremental ya casi coincide— sino el MULTIBYTE, donde la cota
    #   inferior subcuenta y solo el exacto puede cazarlo. (focal F-C2)
    ('MC1-eje-bytes-exacto-apagado', 'coordination.py',
     '        if n > tope_bytes:\n',
     '        if False:\n',
     ['test_la_UNIDAD_es_el_byte_UTF8_no_el_caracter', 'test_N_mas_1_se_RECHAZA']),

    # ⊖ la COTA INCREMENTAL apagada: vuelve a materializarse el cuerpo para
    #   medirlo. Medido: pico `4.650 B` -> `11.250.944 B`. (focal F-C3)
    ('MC1-cota-incremental-apagada', 'coordination.py',
     '        if c[1] > tope_bytes:\n',
     '        if False:\n',
     ['test_congelar_NO_materializa_el_string_gigante']),

    # ⊖ el eje de PROFUNDIDAD apagado. Sin el vuelve el `RecursionError`, ahora
    #   en el propio recorrido: medido, `30.000` niveles lo levantan. (F-C4)
    ('MC1-eje-profundidad-apagado', 'coordination.py',
     '    if prof > prof_max:\n',
     '    if False:\n',
     ['test_un_intent_DEMASIADO_PROFUNDO_da_error_TIPADO']),

    # ⊖ el eje de NODOS apagado: un documento pequeño en bytes pasa. (F-C5)
    ('MC1-eje-nodos-apagado', 'coordination.py',
     '    c[0] += 1\n    if c[0] > nodos_max:\n',
     '    c[0] += 0\n    if c[0] > nodos_max:\n',
     ['test_el_eje_de_NODOS_acota_un_documento_PEQUENO']),

    # ⊖ el eje de CARDINALIDAD apagado: cada elemento es una FILA durable. (F-C6)
    ('MC1-eje-cardinalidad-apagado', 'coordination.py',
     '        if len(corte) > tope:\n',
     '        if False:\n',
     ['test_el_eje_de_CARDINALIDAD_acota_las_FILAS']),

    # ⊖ DOS VERDADES para la profundidad. Sustituye a
    #   `MC1-validador-recursivo-otra-vez`, que quedo SUBSUMIDO: el recorrido ya
    #   es recursivo y lo que lo hace seguro es el guard DELANTE, o sea
    #   exactamente lo que muta `MC1-eje-profundidad-apagado`. En su lugar, la
    #   propiedad que el bloque A introdujo y nadie vigilaba. (focal F-C7)
    ('MC1-profundidad-dos-verdades', 'coordination.py',
     '    prof_max = Journal._PROFUNDIDAD_MAX if prof_max is None else prof_max\n',
     '    prof_max = 12 if prof_max is None else prof_max\n',
     ['test_la_profundidad_tiene_UNA_sola_verdad_la_de_Journal']),

    # ⊖ el codigo FUERA del vocabulario cerrado. (focal F-C8)
    ('MC1-codigo-fuera-del-vocabulario', 'coordination.py',
     '    "RESOURCE_LIMIT_EXCEEDED",   # LIMITE_RECURSO -> 413\n',
     '',
     ['test_el_codigo_esta_en_el_VOCABULARIO_y_no_mueve_la_taxonomia']),

    # ⛔ RETIRADO CON ARGUMENTO, no por «equivalente»:
    #    `MC1-bytes-antes-que-profundidad`. Su propiedad —medir bytes antes de
    #    acotar la profundidad— ya no es expresable: el unico sitio donde se
    #    miden bytes exactos es `len(_canonical(snap)...)`, y `snap` ES el
    #    retorno del recorrido guardado. La DEPENDENCIA DE DATOS fuerza el
    #    orden; no hay texto que lo invierta sin reescribir la funcion.
    #    ⚠️ Es un argumento ESTATICO, no una medida. Si alguien construye el
    #    mutante, cae.
]


def main() -> int:
    # ⛔ PRE-FLIGHT · antes de clonar nada: si un `-k` no casa en su suite, el
    # informe saldria con `rc=5` disfrazados de vivos. Abortar es mas barato.
    fuera = grupos_despues_del_guard()
    if fuera:
        print("🛑 ABORTA — grupos definidos DESPUES del `if __name__`: "
              f"{fuera}. `main()` resuelve el nombre antes de la asignacion "
              "(NameError) y el arnes no corre NADA.")
        return 2
    malos = censo_jueces()
    if malos:
        print("🛑 ABORTA — mutantes cuyo `-k` NO casa en SU suite (se leerian VIVOS):")
        for mid, suite, faltan in malos:
            print(f"   · {mid} -> {suite}\n     falta: {faltan}")
        return 2
    rc, out = corre(RAIZ)
    if rc != 0:
        print("⊖ ABORTA: la base no está verde; un mutante sobre rojo no mide nada")
        print(out[-2000:])
        return 2
    print(f"⊕ base verde sobre {SUITE}\n")

    rc, out = corre(RAIZ, suite=SUITE_T)
    if rc != 0:
        print("⊖ ABORTA: la base del TOCTOU no está verde")
        print(out[-2000:]); return 2
    print(f"⊕ base verde sobre {SUITE_T}")

    rc, out = corre(RAIZ, suite=SUITE_C)
    if rc != 0:
        print("⊖ ABORTA: la base de la COTA no esta verde")
        print(out[-2000:]); return 2
    print(f"⊕ base verde sobre {SUITE_C}")

    # 🩸 FALTABA, y era el unico grupo sin base: los `MD*` se enrutan a
    # `SUITE_D` y hasta aqui NADIE habia comprobado que esa suite estuviera
    # verde. Un mutante sobre una base ROJA no mide —el rojo lo pone la base—,
    # asi que un `MD` se leia como «muerto» sin haber discriminado nada. Es el
    # mismo `⊖ ABORTA` que los otros doce grupos ya tenian.
    rc, out = corre(RAIZ, suite=SUITE_D)
    if rc != 0:
        print("⊖ ABORTA: la base del `detail` que TRUNCA no esta verde")
        print(out[-2000:]); return 2
    print(f"⊕ base verde sobre {SUITE_D}")

    rc, out = corre(RAIZ, suite=SUITE_B)
    if rc != 0:
        print("⊖ ABORTA: la base del PRESUPUESTO COMPARTIDO no esta verde")
        print(out[-2000:]); return 2
    print(f"⊕ base verde sobre {SUITE_B}")

    rc, out = corre(RAIZ, suite=SUITE_X)
    if rc != 0:
        print("⊖ ABORTA: la base del CONTADOR UNICO no esta verde")
        print(out[-2000:]); return 2
    print(f"⊕ base verde sobre {SUITE_X}")

    rc, out = corre(RAIZ, suite=SUITE_AG)
    if rc != 0:
        print("⊖ ABORTA: la base del AGREGADO CANONICO no esta verde")
        print(out[-2000:]); return 2
    print(f"⊕ base verde sobre {SUITE_AG}")

    rc, out = corre(RAIZ, suite=SUITE_G)
    if rc != 0:
        print("⊖ ABORTA: la base de la garantia GLOBAL no esta verde")
        print(out[-2000:]); return 2
    print(f"⊕ base verde sobre {SUITE_G}")

    rc, out = corre(RAIZ, suite=SUITE_OC)
    if rc != 0:
        print("⊖ ABORTA: la base de `outbox_counts` no está verde")
        print(out[-2000:]); return 2
    print(f"⊕ base verde sobre {SUITE_OC}")

    rc, out = corre(RAIZ, suite=SUITE_O)
    if rc != 0:
        print("⊖ ABORTA: la base del ORDEN fail-closed no está verde")
        print(out[-2000:]); return 2
    print(f"⊕ base verde sobre {SUITE_O}")

    rc, out = corre(RAIZ, suite=SUITE_L)
    if rc != 0:
        print("⊖ ABORTA: la base de leases/comandos no está verde")
        print(out[-2000:]); return 2
    print(f"⊕ base verde sobre {SUITE_L}")

    rc, out = corre(RAIZ, suite=SUITE_SE)
    if rc != 0:
        print("⊖ ABORTA: la base del TOCTOU de SESIÓN no está verde")
        print(out[-2000:]); return 2
    print(f"⊕ base verde sobre {SUITE_SE}")

    rc, out = corre(RAIZ, suite=SUITE_N)
    if rc != 0:
        print("⊖ ABORTA: la base de normalización no está verde")
        print(out[-2000:]); return 2
    print(f"⊕ base verde sobre {SUITE_N}")

    rc, out = corre(RAIZ, suite=SUITE_S)
    if rc != 0:
        print("⊖ ABORTA: la base de sesión/scope no está verde")
        print(out[-2000:]); return 2
    print(f"⊕ base verde sobre {SUITE_S}")

    rc, out = corre(RAIZ, suite=SUITE_P1)
    if rc != 0:
        print("⊖ ABORTA: la base de P1 no está verde")
        print(out[-2000:])
        return 2
    print(f"⊕ base verde sobre {SUITE_P1}\n")

    vivos = []
    for mid, fich, ancla, nuevo, deben_caer in (
            MUTANTES + MUTANTES_P1 + MUTANTES_S + MUTANTES_N + MUTANTES_T
            + MUTANTES_SE + MUTANTES_O + MUTANTES_G + MUTANTES_VE
            + MUTANTES_C + MUTANTES_B + MUTANTES_AG + MUTANTES_D
            + MUTANTES_X + MUTANTES_OC):
        # ⚠️ `MSE` ANTES que `MS`: `startswith("MS")` casa los dos y los MSE se
        # irían a la suite de sesión/scope, donde su `-k` no selecciona nada
        # (rc=5). El orden de estas ramas ES la desambiguación.
        # 🩸 PISE LA TRAMPA QUE ESTE BLOQUE DOCUMENTA. Llame `MN1` a un mutante
        # mio y enrute `startswith("MN1")` a mi suite — pero `MN1` ya existia en
        # `MUTANTES_N` (`MN1-profundidad-fuera-del-embudo`), asi que el AJENO se
        # iba a MI suite y su `-k` no seleccionaba nada. La cura no es otra rama:
        # es que el prefijo IDENTIFIQUE al grupo. El mio pasa a `MAG5`.
        suite = suite_de(mid)
        with tempfile.TemporaryDirectory(prefix="mut-native-core-") as tmp:
            clon = pathlib.Path(tmp) / "repo"
            shutil.copytree(RAIZ, clon, ignore=shutil.ignore_patterns(
                ".git", "__pycache__", "*.pyc", ".pytest_cache", ".venv*"))
            objetivo = clon / fich
            s = objetivo.read_text()
            # ⊖ el ancla tiene que casar UNA vez, no «al menos una». P2-1 de
            # `@qa` sobre este mismo arnés (`MARK:qa-go-a-f23e7aa-y-49379af-dos-
            # p2-y-una-divergencia-de-contrato`): comprobaba PRESENCIA y hacía
            # `replace(...,1)`, así que con un ancla repetida mutaba la primera
            # ocurrencia — acertaba por ORDEN, no por construcción, y el informe
            # no distingue «muté la puerta que creía» de «muté la de al lado».
            n = s.count(ancla)
            if n != 1:
                print(f"🛑 ABORTA — {mid}: el ancla casa {n} veces (se exige 1). "
                      f"Con 0 el mutante se salta y se lee igual que uno muerto; "
                      f"con >1 muta la ocurrencia que toque por orden.")
                return 2
            mutado = s.replace(ancla, nuevo, 1)
            # ⊕ EL MUTANTE TIENE QUE CAMBIAR EL FUENTE. Sin esto, un reemplazo
            # que no altera el comportamiento —el mío insertaba una variable y no
            # la usaba— corre entero, sale VIVO y se lee en el informe como «el
            # test no discrimina». Lo que no discriminaba era el mutante. Es la
            # otra mitad del `count == 1`: aquélla dice DÓNDE muta, ésta si MUTA.
            if mutado == s:
                print(f"🛑 ABORTA — {mid}: el reemplazo deja el fichero IDÉNTICO. "
                      f"Un mutante NO-OP sobrevive siempre y acusa al test.")
                return 2
            # ⊕ TERCERA GUARDA, y nace de un defecto MEDIDO en `MOC1`: su
            # ancla dejaba fuera el `return`, el dedent lo dejaba colgando y el
            # mutante mataba por `IndentationError` — o sea rompia la
            # IMPORTACION y ponia rojo todo, que es exactamente lo que este
            # arnes dice en su cabecera que no discrimina. `count==1` dice
            # DONDE muta y `!= s` dice SI muta; ninguna de las dos ve que el
            # resultado ya no compila.
            try:
                compile(mutado, str(objetivo), "exec")
            except SyntaxError as e:
                print(f"🛑 ABORTA — {mid}: el mutante NO COMPILA ({e}). "
                      f"Mataria por error de importacion, no por la propiedad.")
                return 2
            objetivo.write_text(mutado)
            rc_m, out_m = corre(clon, deben_caer, suite=suite)
            # No basta con rojo: tienen que caer los NOMBRADOS, y la selección
            # tiene que haber encontrado tests (un `-k` que no casa da rc=5
            # «no tests ran», que también es distinto de 0).
            selecciono = " no tests ran" not in out_m and "no tests ran" not in out_m.split("\n")[-3:][0]
            if rc_m == 0:
                vivos.append((mid, "SOBREVIVE: los tests nombrados siguen verdes"))
                print(f"❌ {mid} VIVO — {deben_caer}")
            elif rc_m == 5:
                vivos.append((mid, "el `-k` no seleccionó ningún test"))
                print(f"🛑 {mid} — el `-k` no casó ningún test: no mide")
            else:
                print(f"✅ {mid} muerto — cayeron los nombrados")

    total = (len(MUTANTES) + len(MUTANTES_P1) + len(MUTANTES_S)
             + len(MUTANTES_N) + len(MUTANTES_T) + len(MUTANTES_SE)
             + len(MUTANTES_O) + len(MUTANTES_G) + len(MUTANTES_VE)
             + len(MUTANTES_C) + len(MUTANTES_B) + len(MUTANTES_AG)
             + len(MUTANTES_D) + len(MUTANTES_X) + len(MUTANTES_OC))
    print(f"\n{total - len(vivos)}/{total} muertos · {len(vivos)} vivos")
    for mid, por in vivos:
        print(f"   · {mid}: {por}")
    return 1 if vivos else 0



# ── MUTANTES_D · `detail` TRUNCA, NO RECHAZA ──────────────────────────────
# Reanclados a la semantica del CORRECTIVO del operador sobre
# `MARK:cto-detail-trunca-y-dimension-no-es-funcion-del-canonico`.
#
# ⚠️ ESTADO MEDIDO: `4/4` muertos, pero **NO con este runner** — en el entorno
# donde se corrieron no hay `pytest` en ningun interprete. El juez fue el
# falsador autonomo con la MISMA disciplina (ancla unica · el reemplazo cambia
# el fuente · el mutante muere solo si el juez se pone rojo). Quien tenga
# `pytest` debe re-correrlos por aqui: no presento aquel `4/4` como si hubiera
# salido de este arnes.
#
# 🩸 Y el defecto del propio instrumento, que casi me lo trago: la primera
# corrida dio `0/4` con la base VERDE. No eran mutantes equivalentes — el juez
# llevaba la ruta del repo CABLEADA e importaba el sujeto SANO. «Base verde +
# todos vivos» es la firma de un instrumento MUDO, no de un test que no
# discrimina.
MUTANTES_D = [
    # ⊖ EL DEFECTO EXACTO que el operador mando retirar: el tope de METADATO
    # vuelve y la operacion muere por el TAMAÑO DE SU COMENTARIO.
    # 🔻 REANCLADO: la cura del techo global partio la llamada en dos lineas y el
    #    ancla vieja (`tope_bytes=None`) paso a casar CERO veces ⇒ el arnes
    #    ABORTABA. Un ancla que no casa no deja el mutante «igual»: lo saca de la
    #    medida, y en el informe eso se lee como muerto.
    ('MD1-detail-vuelve-a-rechazar', 'coordination.py',
     '            detail = _congelar(detail, "detail",\n                               tope_bytes=CANONICAL_REQUEST_MAX_BYTES)\n',
     '            detail = _congelar(detail, "detail",\n                               tope_bytes=METADATO_MAX_BYTES)\n',
     ['test_un_detail_de_5KiB_AUTENTICADO_PROSPERA_truncado_y_no_da_413',
      'test_el_detail_grande_NO_levanta_ResourceLimitExceeded',
      'test_CONTROL_un_detail_GRANDE_pero_bajo_el_techo_global_sigue_pasando']),

    # ⊖ LA CURA SE DESHACE: vuelve el `None`, que no es «sin politica» sino el
    # contador APAGADO — `suma()` abre con `if tope_bytes is None: return`.
    # 🔴 Este mutante existe por un fallo MIO: el test que fijaba el hallazgo le
    # pasaba `tope_bytes=None` A MANO al helper, asi que medi el HELPER con mi
    # argumento y no el CALL-SITE. Prometia caer al curarse y NO cayo. Los jueces
    # de abajo miden `advance_command` (ruta publica) y el simbolo del call-site
    # (AST) — que es lo unico que ve una reversion.
    ('MD4-detail-sin-techo-otra-vez', 'coordination.py',
     '            detail = _congelar(detail, "detail",\n                               tope_bytes=CANONICAL_REQUEST_MAX_BYTES)\n',
     '            detail = _congelar(detail, "detail",\n                               tope_bytes=None)\n',
     ['test_el_detail_tiene_el_techo_GLOBAL_y_el_corte_es_INCREMENTAL',
      'test_el_CALL_SITE_de_detail_lleva_el_techo_GLOBAL_por_SIMBOLO']),

    # ⊖ EL LITERAL EN VEZ DEL SIMBOLO: hoy vale lo mismo, y el dia que el techo
    # global cambie `detail` se queda con el numero viejo, en silencio. El
    # funcional NO lo ve; el juez por AST si. Esa es su razon de existir.
    ('MD5-detail-techo-cableado-a-mano', 'coordination.py',
     '                               tope_bytes=CANONICAL_REQUEST_MAX_BYTES)\n',
     '                               tope_bytes=1048576)\n',
     ['test_el_CALL_SITE_de_detail_lleva_el_techo_GLOBAL_por_SIMBOLO']),

    # ⊖ TRUNCADO MUDO: corta, pero la clave no lo delata.
    ('MD2-truncado-mudo', 'coordination.py',
     '    return {"payload_truncado": _saneado(crudo, DETALLE_MAX)}\n',
     '    return {"payload": _saneado(crudo, DETALLE_MAX)}\n',
     ['test_un_detail_de_5KiB_AUTENTICADO_PROSPERA_truncado_y_no_da_413']),

    # ⊖ TRUNCA SIEMPRE: mata el control positivo del detail pequeño.
    ('MD3-trunca-siempre', 'coordination.py',
     '    if len(crudo) <= DETALLE_MAX:\n',
     '    if False:\n',
     ['test_CONTROL_un_detail_pequeno_llega_INTACTO_y_SIN_marca']),

    # ⊖ NO TRUNCA NUNCA: el cuerpo entero entra en la fila durable.
    ('MD4-no-trunca-nunca', 'coordination.py',
     '    if len(crudo) <= DETALLE_MAX:\n',
     '    if True:\n',
     ['test_un_detail_de_5KiB_AUTENTICADO_PROSPERA_truncado_y_no_da_413']),
]

# ✅ `MUTANTES_C` REANCLADO por forma al normalizador unico, y el grupo corre
# en el runner con los demas. La nota que decia «el arnes esta INSERVIBLE
# hasta reanclar ese grupo» era cierta cuando se escribio y ya NO lo es:
# dejarla habria hecho que el siguiente lector no corriera el arnes.



# ── MUTANTES_B · el PRESUPUESTO COMPARTIDO (B1) ──────────────────────────
# `@cpo` (`08:52`): «el techo compuesto se cuenta UNA VEZ por peticion — esa es
# la CONDICION, no el numero». El acumulador nacia FRESCO en cada `_congelar` y
# `_congelar_secuencia` llama una vez POR ELEMENTO ⇒ el techo real era
# `cardinalidad x nodos_max` = `256 x 8.192 = 2.097.152`, no `8.192`.
# ⚠️ Medidos con juez focal, NO con este runner: no hay `pytest` en el entorno.
MUTANTES_B = [
    # ── B7 · los TRES ⊖ de los censos nuevos ────────────────────────────────
    # Un censo verde no dice que la aguja lea: dice que no encontro nada. Estos
    # tres son lo unico que separa «no hay defecto» de «el instrumento esta mudo».

    # ⊖ un campo MULTI-peticion pierde el contador compartido.
    # 🩸 EL PRIMER INTENTO NO MEDIA: insertaba `nodos=None` en una llamada que YA
    #    llevaba `nodos=presupuesto` ⇒ argumento duplicado ⇒ `SyntaxError` ⇒ el
    #    banco daba `1 error during collection` y lo leia como MUERTO. Es la
    #    trampa que la cabecera de este arnes nombra: «un mutante que rompe la
    #    recoleccion tambien lo pone todo rojo». Un mutante que no COMPILA no
    #    acredita nada — y su rojo se pinta igual que el bueno.
    ('MB7-censo-multicampo-suelto', 'coordination.py',
     '        trace = _congelar(trace, "trace", tope_bytes=METADATO_MAX_BYTES,\n                          nodos=presupuesto, agregado=agregado)\n',
     '        trace = _congelar(trace, "trace", tope_bytes=METADATO_MAX_BYTES,\n                          agregado=agregado)\n',
     ['test_B7_CENSO_toda_peticion_MULTICAMPO_comparte_su_contador']),

    # ⊖ aparece un congelado AISLADO nuevo, en un metodo que no esta declarado.
    ('MB7-censo-aislado-nuevo', 'coordination.py',
     '        self._denial_quota = int(denial_quota)\n',
     '        self._denial_quota = int(denial_quota)\n        _congelar(None, "trace", tope_bytes=METADATO_MAX_BYTES)\n',
     ['test_B7_CENSO_los_congelados_AISLADOS_son_exactamente_los_declarados']),

    # ⊖ el agregado deja de ser INALCANZABLE en SESSION: la conclusion caduca.
    ('MB7-agregado-alcanzable-en-session', 'coordination.py',
     'METADATO_MAX_BYTES = 4096        # annotations · attestation · trace\n',
     'METADATO_MAX_BYTES = 2097152        # annotations · attestation · trace\n',
     ['test_B7_ninguna_peticion_de_UN_SOLO_campo_necesita_exigir_el_agregado']),

    # ⊖ EL DEFECTO EXACTO que B1 cierra.
    ('MB1-contador-fresco-otra-vez', 'coordination.py',
     '    c = [nodos[0] if nodos is not None else 0, 0]\n',
     '    c = [0, 0]\n',
     ['test_el_techo_de_nodos_se_cuenta_UNA_VEZ_entre_elementos',
      'test_las_DOS_secuencias_comparten_el_presupuesto']),

    # ⊖ la secuencia deja de hilar el presupuesto a sus elementos.
    # 🔻 REANCLADO: la comprension paso a BUCLE al meter el envoltorio que
    #    normaliza `field` al enum cerrado. La propiedad no cambia; el ancla si.
    ('MB1-secuencia-no-hila', 'coordination.py',
     '            salida.append(_congelar(x, campo, tope_bytes=tope_bytes,\n                                    nodos=nodos, n_out=uno,\n                                    nulo_es_valor=True))\n',
     '            salida.append(_congelar(x, campo, tope_bytes=tope_bytes,\n                                    n_out=uno,\n                                    nulo_es_valor=True))\n',
     ['test_el_techo_de_nodos_se_cuenta_UNA_VEZ_entre_elementos']),

    # ⊖ comparte de IDA pero no de VUELTA: lo gastado no se devuelve.
    ('MB1-no-devuelve-lo-gastado', 'coordination.py',
     '    if nodos is not None:\n        nodos[0] = c[0]              # lo gastado vuelve al presupuesto comun\n',
     '    if False:\n        nodos[0] = c[0]\n',
     ['test_el_techo_de_nodos_se_cuenta_UNA_VEZ_entre_elementos']),

    # ⊖ los BYTES tambien se comparten: convierte «4 KiB POR ELEMENTO» en
    #   «4 KiB en total», que es otro contrato y mas apretado que el adjudicado.
    ('MB1-bytes-tambien-compartidos', 'coordination.py',
     '    c = [nodos[0] if nodos is not None else 0, 0]\n',
     '    c = [nodos[0] if nodos is not None else 0, nodos[0] if nodos is not None else 0]\n',
     ['test_los_bytes_siguen_siendo_POR_ELEMENTO']),
]



# ── MUTANTES_AG · AGREGADO CANONICO · enum de `field` · `None` como valor ──
# Arbitraje: RECHAZA bajar `ELEMENTO_MAX_BYTES` a `1.024` (se queda en `4.096`) y
# manda `CANONICAL_REQUEST_MAX_BYTES = 1.048.576`, simbolo DISTINTO del
# `RAW_BODY_MAX_BYTES` del Gateway — que NO se define en Core.
# Precedencia: ① forma · ② limites locales · ③ agregado canonico AL FINAL.
#
# 🩸 DOS de estos cinco nacieron VIVOS y NINGUNO era equivalente:
#   · `MAG3` mutaba el ARGUMENTO y el envoltorio normalizaba el `field` de
#     vuelta — hay DOS capas y mutar una sola es invisible. La propiedad vive en
#     el ENVOLTORIO, y ahi es donde muta ahora.
#   · `MAG4` sobrevivia porque mi frontera del agregado tenia `3.644 B` de
#     margen y los separadores son `251`. El falsador `⑨` la aprieta: con
#     separadores `1.048.577`, sin ellos `1.048.325`.
MUTANTES_AG = [
    # ⊖ el nulo de ELEMENTO vuelve a salir gratis (retornaba ANTES del contador).
    ('MAG5-nulo-de-elemento-gratis', 'coordination.py',
     '        if not nulo_es_valor:\n            return None\n',
     '        if True:\n            return None\n',
     ['test_un_None_de_ELEMENTO_cuenta_como_nodo']),

    # ⊖ el agregado se calcula pero NO decide.
    ('MAG1-agregado-no-decide', 'coordination.py',
     '    if agregado is not None and agregado.total > tope:\n',
     '    if False:\n',
     ['test_el_agregado_canonico_rechaza_cruzando_campos']),

    # ⊖ el agregado no acumula: cada campo pasa solo y el total nunca sube.
    ('MAG2-agregado-no-suma', 'coordination.py',
     '            agregado.campo(campo, n)\n',
     '            agregado.campo(campo, 0)\n',
     ['test_el_agregado_canonico_rechaza_cruzando_campos']),

    # ⊖ el ENVOLTORIO devuelve el indice dentro de `field` -> enum ABIERTO.
    ('MAG3-el-envoltorio-devuelve-el-indice', 'coordination.py',
     '            raise _rle(e.dimension, "request" if cruzado else campo,\n',
     '            raise _rle(e.dimension, "request" if cruzado else campo + "[0]",\n',
     ['test_el_field_de_exceso_de_elemento_es_un_enum_cerrado']),

    # ⊖ los separadores del canonico de la lista dejan de contar.
    ('MAG4-secuencia-sin-separadores', 'coordination.py',
     '        agregado.campo(campo, propio[0] + 2 + max(0, len(salida) - 1))\n',
     '        agregado.campo(campo, propio[0])\n',
     ['test_los_separadores_de_la_lista_cuentan_en_el_agregado']),
    # ⊖ `seq=None` vuelve a normalizarse en silencio a `[]`.
    ('MAG6-seq-None-normalizada-en-silencio', 'coordination.py',
     '        raise OperationInvalid(\n            f"el campo {campo} llego nulo: se espera una secuencia (`[]` para "\n',
     '        return []\n        raise OperationInvalid(\n            f"el campo {campo} llego nulo: se espera una secuencia (`[]` para "\n',
     ['test_seq_None_es_FORMA_INVALIDA_y_NO_se_normaliza_a_lista_vacia',
      'test_seq_None_es_invalida_en_LAS_DOS_puertas']),
]



# ── MUTANTES_X · contador UNICO por operacion · raiz · framing · true ─────
# NO-GO independiente a `05bb1330`: el contador de nodos era POR SUPERFICIE
# (`24.576` por peticion) y el agregado media la SUMA DE VALORES, no el objeto
# exterior. `@qa` (`09:53:23Z`) lo separo: *«el agregado nuevo cruza superficies
# en BYTES y yo bloquee el eje de NODOS — `24.576` nulos son `98 KB`»*.
#
# 🩸 `MX7` nacio VIVO y NO era equivalente: mi falsador de precedencia usaba
# `body` ASCII, y ahi el tope local salta por la COTA INCREMENTAL, asi que
# apagar el pase EXACTO no se nota. El unico regimen donde solo el exacto puede
# cazarlo es el MULTIBYTE (`40.000` caracteres `ñ` = `40.000` de cota inferior
# bajo el tope y `~80.000` bytes reales por encima). El falsador va ahi ahora.
MUTANTES_X = [
    # ⊖ EL DEFECTO EXACTO que `@qa` cazo (§4 sobre `bb5fad94`) y que esta tanda
    # cura: la RLE de la segunda guarda vuelve a construirse A MANO, eludiendo
    # `_rle`, y publica el CAMINO en `field` — fuera del enum CERRADO.
    # 🔴 Es el ⊖ que `@qa` declaro OBLIGATORIO: sin el, el censo por ruta publica
    # sale verde midiendo el vacio (en sano esta rama no se alcanza nunca).
    ('MX-rle-eludida-field-fuera-del-enum', 'coordination.py',
     '            raise _rle("depth", _campo_raiz(donde), cls._PROFUNDIDAD_MAX, prof,\n',
     '            raise ResourceLimitExceeded("depth", dimension="depth",\n                                        field=cls.detalle_seguro(donde, tope=cls._CONTEXTO_EN_MENSAJE_MAX),\n                                        limit=cls._PROFUNDIDAD_MAX, seen_at_least=prof) or _rle("depth", _campo_raiz(donde), cls._PROFUNDIDAD_MAX, prof,\n',
     ['test_CENSO_solo_rle_construye_ResourceLimitExceeded',
      'test_CENSO_por_RUTA_PUBLICA_el_field_de_toda_RLE_esta_en_el_ENUM']),

    ('MX1-contador-por-superficie', 'coordination.py',
     '        intent = _congelar(intent, "intent", tope_bytes=PAYLOAD_MAX_BYTES,\n'
     '                           nodos=presupuesto, agregado=agregado,\n',
     '        intent = _congelar(intent, "intent", tope_bytes=PAYLOAD_MAX_BYTES,\n'
     '                           agregado=agregado,\n',
     ['test_el_contador_de_nodos_cruza_los_campos_de_la_operacion']),

    ('MX2-el-cruzado-pierde-request', 'coordination.py',
     '            cruzado = e.field == "request"\n', '            cruzado = False\n',
     ['test_el_exceso_cruzado_se_reporta_como_field_request']),

    ('MX3-raiz-de-lista-no-cuenta', 'coordination.py',
     '    nodos[0] += 1\n    if nodos[0] > NODOS_MAX:\n',
     '    nodos[0] += 0\n    if nodos[0] > NODOS_MAX:\n',
     ['test_la_raiz_de_una_lista_cuenta_un_nodo']),

    ('MX4-framing-sin-nombre-de-campo', 'coordination.py',
     '        self._bytes += len(nombre) + 3 + n\n', '        self._bytes += n\n',
     ['test_el_agregado_es_el_canonico_del_objeto_exterior']),

    ('MX5-framing-sin-llaves-ni-comas', 'coordination.py',
     '        return 2 + self._bytes + max(0, self._campos - 1)\n',
     '        return self._bytes\n',
     ['test_el_agregado_es_el_canonico_del_objeto_exterior']),

    ('MX6-true-cuesta-5', 'coordination.py',
     '        suma(4 if v is True else 5)\n', '        suma(5)\n',
     ['test_true_cuesta_4_y_false_5']),

    ('MX7-agregado-antes-que-el-local', 'coordination.py',
     '        if n > tope_bytes:\n            raise _rle("bytes", campo, tope_bytes, n,\n',
     '        if False:\n            raise _rle("bytes", campo, tope_bytes, n,\n',
     ['test_el_limite_local_decide_ANTES_que_el_agregado']),

    # ⛔ RETIRADO CON ARGUMENTO, no como «equivalente»: `MX8-enum-sin-guarda`
    #    mutaba `assert campo in ...`, y esa FORMA ya no existe — el `assert`
    #    se sustituyo por un `if`/`raise` porque `python -O` borra los
    #    `assert` y la guarda desaparecia justo en el modo de despliegue.
    #    `MX10` cubre la MISMA propiedad en la forma nueva y ademas la
    #    ejerce bajo `-O`.

    ('MX9-nulo-obligatorio-gratis', 'coordination.py',
     '        if obligatorio:\n', '        if False:\n',
     ['test_intent_nulo_es_rechazo_de_forma']),
    # ⊖ la guarda del enum vuelve a estar apagada (y con `assert` moriria con -O).
    # 🔻 REANCLADO: el ancla llevaba PEGADO el `raise AssertionError(` de la
    #    linea siguiente, y al meter el `_saneado` —con su parrafo de porque—
    #    entre el `if` y el `raise` dejo de casar (`0` veces ⇒ el arnes ABORTA,
    #    que es lo correcto: un mutante saltado se lee igual que uno muerto).
    #    La propiedad no cambia; el ancla es ahora SOLO la condicion, que casa
    #    exactamente una vez y es lo unico que este mutante tiene que apagar.
    ('MX10-enum-sin-check-de-runtime', 'coordination.py',
     '    if campo not in CAMPOS_CONTRATO_CORE:\n',
     '    if False:\n',
     ['test_el_enum_de_field_vive_en_dato']),
]



# ── MUTANTES_OC · `outbox_counts` ─────────────────────────────────────────────
# Cuatro propiedades, un mutante cada una: retirar las cuatro de golpe sólo
# probaría que «algo» falla; separadas, cada una acredita SU eje.
#
# `MOC1` es EL mutante de este bloque: la MISMA ruta con una sola regla retirada
# —que las dos cifras salgan de UNA transacción—, no un gemelo escrito para
# caer. Conserva la autenticación, la capacidad y el carril; lo único que cambia
# es dónde cierra la transacción. Medido sobre la carga del test de 20 procesos:
# el conteo en dos transacciones desgarra `46` de `900` muestras, así que el
# juez probabilístico lo mata con margen — y el DETERMINISTA
# (`test_las_dos_cifras_salen_de_LA_MISMA_foto`) lo mata siempre.
MUTANTES_OC = [
    ('MOC1-dos-transacciones', "coordination.py",
     '            self._tras_precheck()\n'
     '            fila = con.execute(\n'
     '                "SELECT"\n'
     '                "  SUM(CASE WHEN o.state=\'pending\' THEN 1 ELSE 0 END) AS p,"\n'
     '                "  SUM(CASE WHEN o.state=\'failed\'  THEN 1 ELSE 0 END) AS f"\n'
     '                "  FROM outbox o JOIN events e USING (event_id)"\n'
     '                " WHERE e.lane=? AND o.state IN (\'pending\',\'failed\')",\n'
     '                (view.lane,)).fetchone()\n'
     '            # `SUM` sobre cero filas da NULL, no 0: sin esto, un carril vac\u00edo\n'
     '            # devolv\u00eda `None` y el primer `+` del llamante reventaba.\n'
     '            return OutboxCounts(lane=view.lane,\n'
     '                                pending=fila["p"] or 0, failed=fila["f"] or 0)',
     '        self._tras_precheck()\n'
     '        pending = self._outbox_del_carril(token, ("pending",))\n'
     '        failed = self._outbox_del_carril(token, ("failed",))\n'
     '        return OutboxCounts(lane=view.lane, pending=pending, failed=failed)',
     ['test_las_dos_cifras_salen_de_LA_MISMA_foto',
      'test_veinte_procesos_nunca_ven_un_total_roto_mientras_la_cola_se_mueve']),

    ('MOC2-sin-carril-en-el-where', "coordination.py",
     '                " WHERE e.lane=? AND o.state IN (\'pending\',\'failed\')",\n'
     '                (view.lane,)).fetchone()',
     '                " WHERE o.state IN (\'pending\',\'failed\')",\n'
     '                ()).fetchone()',
     ['test_outbox_counts_cuenta_pending_y_failed_SOLO_del_carril_de_la_sesion',
      'test_outbox_counts_de_un_carril_VACIO_da_CEROS_enteros_y_no_None']),

    ('MOC3-sin-la-capacidad', "coordination.py",
     '            self._exigir_capacidad_locked(con, view, CAP_OUTBOX_WORKER,\n'
     '                                          "outbox_counts")',
     '            pass',
     ['test_outbox_counts_exige_la_capacidad_outbox_worker']),

    # Los tres de la FORMA CANONICA del DTO. Separados porque cada uno cierra
    # una puerta distinta: el `bool` que se cuela por ser subclase de `int`, el
    # negativo que no es un dato viejo sino imposible, y el `lane` vacio que se
    # renderiza como si fuera un nombre.
    ('MOC5-bool-pasa-por-int', "coordination.py",
     '            if type(v) is not int:',
     '            if not isinstance(v, int):',
     ['test_OutboxCounts_rechaza_lo_que_no_es_un_int_EXACTO_empezando_por_bool']),

    ('MOC6-negativo-pasa', "coordination.py",
     '            if v < 0:',
     '            if False:',
     ['test_OutboxCounts_rechaza_contadores_NEGATIVOS']),

    ('MOC7-lane-vacio-pasa', "coordination.py",
     '        if type(self.lane) is not str or not self.lane:',
     '        if type(self.lane) is not str:',
     ['test_OutboxCounts_rechaza_un_lane_que_no_sea_str_no_vacio']),

    ('MOC8-lane-por-isinstance', "coordination.py",
     '        if type(self.lane) is not str or not self.lane:',
     '        if not isinstance(self.lane, str) or not self.lane:',
     ['test_OutboxCounts_rechaza_un_lane_que_no_sea_str_no_vacio']),

    # El mensaje CERRADO tambien es una propiedad, no una preferencia de estilo:
    # el texto del rechazo es lo unico que sube cuando la fila viene corrupta.
    ('MOC9-mensaje-abierto', "coordination.py",
     '                    f"`OutboxCounts.{campo}` no puede ser negativo: una cuenta "\n'
     '                    f"bajo cero no es un dato viejo, es un dato imposible")',
     '                    f"`OutboxCounts.{campo}` no puede ser negativo: llego {v!r}")',
     ['test_el_rechazo_NO_repite_el_valor_sospechoso_en_el_mensaje']),

    ('MOC4-sum-nulo-sin-cero', "coordination.py",
     '                                pending=fila["p"] or 0, failed=fila["f"] or 0)',
     '                                pending=fila["p"], failed=fila["f"])',
     ['test_outbox_counts_de_un_carril_VACIO_da_CEROS_enteros_y_no_None']),
]


if __name__ == "__main__":
    raise SystemExit(main())
