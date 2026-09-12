#!/usr/bin/env python3
"""Arnés de mutantes de M2. Comprueba a los TESTS, no al código.

Cada mutante es un defecto CONCRETO que alguien podría introducir. Si sobrevive, el roto
es el falsador: un test que sigue verde con el defecto dentro certifica cualquier cosa.

## Por qué está endurecido así — cada mecanismo viene de una corrida que NO fue evidencia

**Corrida 1, descartada**: los hijos terminaron, la shell que los lanzó quedó huérfana y el
vigía esperaba un fichero efímero ya borrado. No era ni verde ni rojo: era un observador
mirando donde no había nada.

**Corrida 2, descartada (17 resultados)**: estampaba `HEAD` UNA vez pero cada mutante hacía
`copytree(RAIZ)`. Con el árbol vivo cambiando durante horas, **cada resultado probaba un
sujeto distinto** y ninguno el que decía el encabezado. Además no hasheaba CONTENIDO —los
tests sin trackear no entraban en `HEAD` ni en `git diff`— y un `SIGTERM` se llevó el
proceso sin ejecutar `finally`, dejando el JSON sin resumen: indistinguible de una corrida
en curso.

De ahí sale todo lo de abajo:

· **UNA instantánea CONGELADA** del sujeto al principio. Cada mutante nace de esa copia
  inmutable; `RAIZ` no se vuelve a leer nunca. Todos los resultados prueban el MISMO árbol.
· **Manifiesto canónico** `path + sha256` de todos los fuentes y tests relevantes,
  **incluidos los untracked**, más `HEAD` y el sha256 del `git diff`. El digest del
  manifiesto es lo que identifica al sujeto: `HEAD` solo no lo hace.
· **Pre-vuelo de las agujas**: cada aguja tiene que aparecer **exactamente una vez** en la
  instantánea. Cero es una aguja obsoleta; dos es peor, porque `replace(..., 1)` mutaría
  una arbitraria y el resultado se firmaría como sustitución exacta.
· **Manifiesto verificado al ENTRAR y al SALIR**, con la distinción escrita: al salir se
  rehashea la INSTANTÁNEA —que prueba que la copia no se movió— y **también el árbol
  VIVO**, que es otra cosa. Que el vivo derive durante la corrida NO invalida la
  evidencia congelada; se registra (`vivo_cambio_durante_la_corrida`) para que el lector
  sepa que el `git status` de este JSON puede no describir ya el directorio de trabajo.
  Y comprobación de que cada mutante cambia **exactamente el fichero previsto y ninguno
  más**.
· **`rc` de pytest CLASIFICADO**: sólo `1` es «un test falló», que es lo único que mata.
  `0` es `VIVO`; `2`/`3`/`4`/`5` son `ARNES_ERROR` (nadie aseveró nada: recolección rota,
  error interno, uso, cero tests) y un `rc` negativo es `SENAL`. Contarlos como `MUERTO`
  era el falso verde más caro de todos.
· **Fallo del arnés = `ERROR` terminal, fail-closed.** Cualquier excepción (copytree,
  lectura, disco) da veredicto propio y estado terminal; nunca se sale con `EN_CURSO`.
· **Una corrida PARCIAL no es evidencia**: va a otro fichero, se marca
  `PARTIAL_DIAGNOSTIC` y devuelve no-cero. El GO canónico exige que los ids seleccionados
  sean TODO el censo y que haya un resultado por cada uno.
· **`COMPLETA` significa MEDIDO, no «el bucle acabó».** Con un solo `ERROR`,
  `ARNES_ERROR`, `TIMEOUT`, `SENAL` o aguja rota dentro, el estado pasa a
  `ARNES_DEGRADADO`: un instrumento averiado no firma su propia corrida.
· **Cerrojo `flock` exclusivo** sobre la evidencia: una segunda corrida se NIEGA en vez de
  entrelazar resultados. Ya pasó —un full obsoleto corriendo en paralelo—. Y el temporal
  de cada escritura lleva pid + uuid: un `.tmp` de nombre fijo es esa misma carrera.
· **La instantánea prístina queda en SÓLO LECTURA y nada se ejecuta dentro de ella.** La
  suite limpia y cada mutante corren en un CLON DESECHABLE. Correr dentro la ensuciaba
  (`__pycache__`, `.pytest_cache`, ficheros de los tests) y el árbol del que nacían los
  mutantes ya no era el que se hasheó.
· **El manifiesto incluye configuración y dependencias** (`requirements-test.txt`,
  `pytest.ini`/`pyproject.toml`/`setup.cfg`/`tox.ini`, `conftest.py` de raíz) y el sujeto
  declara intérprete, `pytest` y `sqlite`: la misma suite contra otro pin no es el mismo
  sujeto.
· **El hijo se cierra y se COMPRUEBA**: `Popen` dentro del `try` (había ventana entre
  crearlo y protegerlo), `BaseException` y no sólo `Interrumpido`, y `poll()` verificado
  tras el `SIGKILL` — si sigue vivo, se dice.
· **`SIGINT`/`SIGTERM` manejados**: veredicto `INTERRUMPIDO`, distinto de todo lo demás. Se
  mata el grupo del hijo, se limpia el temporal y se escribe el JSON terminal. Nunca se
  cuenta como `MUERTO` lo que no se midió.
· **Grupo de procesos propio** por mutante y **`killpg` sólo de ese grupo**.
· **JSON progresivo atómico Y DURABLE** (`tmp` + `fsync` del fichero + `os.replace` +
  **`fsync` del directorio**) a ruta ESTABLE. Es salida de
  ejecución: va al `.gitignore`, no al commit. El runner y los tests sí se commitean.
· **La suite limpia corre PRIMERO** sobre la instantánea. Si el sujeto no está verde, los
  mutantes no significan nada y no se corren.

⛔ No mide tiempos y no es un benchmark.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import importlib.metadata
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

RAIZ = pathlib.Path(__file__).resolve().parents[2]
SALIDA = pathlib.Path(__file__).resolve().parent / ".mutantes_m2.json"
# Ruta SEPARADA para las corridas parciales (`M2_MUTANTES=…`). No es una comodidad de
# nombres: si compartieran fichero, un diagnóstico de dos mutantes SOBREESCRIBIRÍA la
# única corrida canónica y el operador leería `COMPLETA` sobre dos resultados.
SALIDA_PARCIAL = pathlib.Path(__file__).resolve().parent / ".mutantes_m2.parcial.json"
TIMEOUT_S = float(os.environ.get("M2_MUTANTE_TIMEOUT_S", "300"))
S, C, U = "search_store.py", "search_contract.py", "search_cursor.py"

# Lo que ENTRA en el manifiesto. Sin comodines abiertos: un manifiesto que cambia porque
# alguien dejó un fichero al lado no identifica al sujeto, sólo hace ruido.
# `servicio.py` ENTRA: registra la UDF en sus 8 conexiones, así que es parte del sujeto —
# un cambio ahí puede romper el índice sin tocar una línea de `search_*`. `bench/` entra
# por lo mismo que la configuración: define lo que se ejecuta alrededor del sujeto.
FUENTES = (S, C, U, "servicio.py")
TESTS_DIR = "tests/search"
BENCH_DIR = "bench"
# CONFIG y DEPS entran al manifiesto: la suite no la define sólo el código. Un
# `pytest.ini` que cambie `addopts`, un `conftest.py` de raíz o un pin distinto en
# `requirements-test.txt` cambian LO QUE SE EJECUTA sin tocar una línea de fuente — y un
# manifiesto que no los ve firma como «mismo sujeto» dos corridas que no lo son.
CONFIG = ("requirements-test.txt", "pytest.ini", "pyproject.toml", "setup.cfg",
          "tox.ini", "conftest.py")


MUTANTES: list[tuple[str, str, str, str]] = [
    ("M01 frontera: sin AND e.ledger", S, "   AND e.ledger = ?\n", ""),
    ("M02 frontera: sin AND d.ledger", S, "   AND d.ledger = ?\n", ""),
    ("M03 orden por ts", S, " ORDER BY e.arrival DESC, e.eid DESC",
     " ORDER BY e.ts DESC, e.eid DESC"),
    ("M54 orden sin eid", S, " ORDER BY e.arrival DESC, e.eid DESC",
     " ORDER BY e.arrival DESC"),
    ("M05 ausentes se sirven", S, "   AND e.ausente IS NULL\n", ""),
    # M29/M30 reconstruidos: el censo tenía un HUECO en la numeración (M01–M70 con 68
    # entradas) y ninguna traza en historial ni en docs. En vez de rebajar la promesa a
    # 68, se cubren dos invariantes que NINGÚN otro mutante discriminaba.
    ("M29 sin posicion se sirven", S, "   AND e.arrival IS NOT NULL\n", ""),
    ("M30 ventana con hasta inclusivo", S,
     "   AND (? IS NULL OR e.ts    < ?)\n", "   AND (? IS NULL OR e.ts   <= ?)\n"),
    ("M06 integrity-check sin rank=1", S,
     "            self.con.execute(\n"
     "                \"INSERT INTO search_fts(search_fts, rank) VALUES('integrity-check', 1)\")",
     "            self.con.execute(\n"
     "                \"INSERT INTO search_fts(search_fts) VALUES('integrity-check')\")"),
    ("M07 rid sin AUTOINCREMENT", S, "rid    INTEGER PRIMARY KEY AUTOINCREMENT",
     "rid    INTEGER PRIMARY KEY"),
    ("M27 borrar el mapeo al borrar", S,
     "     WHERE ledger = old.ledger AND eid = old.eid;\nEND;\n\nCREATE TRIGGER IF NOT EXISTS search_au_body",
     "     WHERE ledger = old.ledger AND eid = old.eid;\n  DELETE FROM search_documents"
     " WHERE ledger = old.ledger AND eid = old.eid;\nEND;\n\nCREATE TRIGGER IF NOT EXISTS search_au_body"),
    ("M40 vista sin la UDF", S, "SELECT d.rid AS rowid, llminbox_proyecta(e.body) AS body",
     "SELECT d.rid AS rowid, e.body AS body"),
    ("M44 readiness sin huellas", S,
     "                    if distintos:\n"
     "                        problemas.append(\"huella distinta en: \" + \", \".join(distintos))",
     "                    if False:\n"
     "                        problemas.append(\"huella distinta en: \" + \", \".join(distintos))"),
    ("M46 readiness sin normalizador", S,
     "        if norma is not None and norma != sc.normalizer_fingerprint():",
     "        if False:"),
    ("M47 sin instantanea unica", S, '        self.con.execute("BEGIN DEFERRED")\n        try:',
     "        try:"),
    ("M48 rebuild sin frontera de eid", S, "            largos = self._eids_largos()",
     "            largos = 0"),
    ("M51 fusible sin validar", S, "        if not math.isfinite(fusible_s) or fusible_s < 0:",
     "        if False:"),
    ("M55 recortadas cuenta la sonda", S,
     '"recortadas": max(0, len(candidatas) - len(page)),',
     '"recortadas": max(0, len(filas) - len(page)),'),
    ("M56 head/body sin rtrim", S,
     '"   AND (body IS NULL OR instr(rtrim(body), rtrim(head)) = 0)"',
     '"   AND (body IS NULL OR instr(body, head) = 0)"'),
    ("M57 marcas dentro del texto", S, "            elif ch == sc.CENTINELA_FIN:",
     "            elif False:"),
    ("M31 primera fila siempre admitida", S,
     "            if acumulado + coste > presupuesto:\n                if not page:",
     "            if page and acumulado + coste > presupuesto:\n                if False:"),
    ("M32 sin reserva del sobre", S, "        presupuesto = MAX_RESPONSE_BYTES - sobre",
     "        presupuesto = MAX_RESPONSE_BYTES"),
    ("M33 snippet sin reservar la marca", S,
     '        hueco = tope - len(MARCA_RECORTE.encode("utf-8"))', "        hueco = tope"),
    ("M41 strip_framing no-op", C,
     'return "\\n".join(l for l in texto.splitlines() if not _MARCADOR.match(l))',
     "return texto"),
    ("M42 normalize sin casefold", C,
     'return unicodedata.normalize("NFKC", limpio).casefold()',
     'return unicodedata.normalize("NFKC", limpio)'),
    ("M43 normalize sin NFKC", C, 'return unicodedata.normalize("NFKC", limpio).casefold()',
     "return limpio.casefold()"),
    ("M15 compilador trunca termino", C, "        if b > MAX_TERM_BYTES:", "        if False:"),
    ("M16 compilador N terminos", C, "    if len(terminos) > MAX_TERMS:", "    if False:"),
    ("M17 filter_sha256 truncado", C,
     'return hashlib.sha256(crudo.encode("utf-8")).hexdigest()',
     'return hashlib.sha256(crudo.encode("utf-8")).hexdigest()[:16].ljust(64, "0")'),
    ("M18 filtros sin hasta", C, '        "hasta": hasta,\n', ""),
    ("M19 filtros sin lane", C, '        "lane": lane,\n', ""),
    ("M20 compilador reenvia cruda", C,
     '    citados = [\'"\' + t.replace(\'"\', \'""\') + \'"\' for t in terminos]\n'
     '    return " AND ".join(citados), terminos', "    return normal, terminos"),
    ("M23 limite maximo 1000", C, "MAX_LIMIT = 100", "MAX_LIMIT = 1000"),
    ("M52 sin cotas de filtros", C, "        if n > MAX_FILTER_BYTES:", "        if False:"),
    ("M53 sin cota cruda de consulta", C, "    if bruto > MAX_QUERY_BYTES * COTA_CRUDA:",
     "    if False:"),
    ("M11 HMAC truncado", U, "firma = hmac.new(k, cuerpo, hashlib.sha256).hexdigest()",
     "firma = hmac.new(k, cuerpo, hashlib.sha256).hexdigest()[:16]"),
    ("M12 cursor sin filtros", U,
     'if not hmac.compare_digest(str(payload["filter_sha256"]), filter_sha256):', "if False:"),
    ("M13 cursor sin generacion", U,
     'if not hmac.compare_digest(str(payload["generation"]), generation):', "if False:"),
    ("M14 clave minima 1", U, "MIN_KEY_BYTES = 32", "MIN_KEY_BYTES = 1"),
    ("M24 cursor sin cota de tamano", U, "    if n > MAX_CURSOR_BYTES:", "    if False:"),
    ("M25 hex solo por longitud", U,
     '    try:\n        int(valor, 16)\n    except ValueError:\n        raise CursorMalformed(f"`{nombre}` no es hexadecimal") from None\n    return valor',
     "    return valor"),
    ("M49 base64 sin canonicidad", U, "    if _b64u(crudo) != txt:", "    if False:"),
    ("M50 base64 sin alfabeto", U, "    if any(c not in _ALFABETO_B64U for c in txt):",
     "    if False:"),
    ("M58 eid sin cota en el cursor", U, "    if n_eid > MAX_EID_BYTES:", "    if False:"),
    ("M04 keyset solo arrival", S, "(e.arrival, e.eid) < (?, ?)",
     "e.arrival < ? AND ? IS NOT NULL"),
    ("M08 fallo de rebuild deja ready", S,
     'self._set("state", ESTADO_CONSTRUYENDO)\n            self.con.execute("COMMIT")',
     'self.con.execute("COMMIT")'),
    ("M09 readiness ignora objetos", S, "if faltan:\n            problemas.append",
     "if False:\n            problemas.append"),
    ("M10 readiness ignora rebuild viejo", S,
     'problemas.append(f"schema_v={v} < {SEARCH_SCHEMA_V} (rebuild viejo)")', "pass"),
    ("M45 readiness sin generacion", S,
     '        if gen is None:\n            return "sin generación sellada"',
     '        if False:\n            return "sin generación sellada"'),
    ("M21 fusible desactivado", S,
     '                estado["disparado"] = True\n                return 1',
     "                return 0"),
    ("M22 sin head en body", S, "            fuera = self._head_fuera_de_body()",
     "            fuera = 0"),
    ("M26 lane opcional en filtros", C,
     '    if not isinstance(lane, str) or not lane:\n        raise InvalidFilter("`lane` es obligatorio: identifica al carril que pregunta")\n',
     ""),
    ("M28 rebuild regenera los mapeos", S,
     '"INSERT OR IGNORE INTO search_documents(ledger, eid)"',
     '"DELETE FROM search_documents"); self.con.execute(\n                "INSERT INTO search_documents(ledger, eid)"'),
    ("M34 sin cap de head", S,
     '(("head", MAX_HEAD_BYTES), ("actor", MAX_ACTOR_BYTES),\n                                ("tipo", MAX_TIPO_BYTES))',
     '(("actor", MAX_ACTOR_BYTES), ("tipo", MAX_TIPO_BYTES))'),
    ("M35 sin cap de actor/tipo", S,
     '(("head", MAX_HEAD_BYTES), ("actor", MAX_ACTOR_BYTES),\n                                ("tipo", MAX_TIPO_BYTES))',
     '(("head", MAX_HEAD_BYTES),)'),
    ("M36 sin medida final", S,
     "            if total <= MAX_RESPONSE_BYTES:\n                break\n",
     "            if True:\n                break\n"),
    ("M37 medida con separadores compactos", S,
     'return json.dumps(obj, ensure_ascii=False).encode("utf-8")',
     'return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")'),
    ("M38 recorte sin marca", S,
     "return retenido + MARCA_RECORTE, True, len(retenido)",
     "return retenido, True, len(retenido)"),
    ("M39 reserva acoplada al techo", S, '"bytes_presupuestados": 10 ** 12}',
     '"bytes_presupuestados": MAX_RESPONSE_BYTES}'),
    ("M59 register no mira la transaccion", S, "        if con.in_transaction:",
     "        if False:"),
    # ── M2-5 ──────────────────────────────────────────────────────────────────────
    ("M60 DDL auto-bendecido (foto de lo vivo)", S,
     "            desajustes = self._desajustes_ddl()", "            desajustes = []"),
    ("M61 readiness sin comparar DDL", S,
     "        problemas.extend(self._desajustes_ddl())", "        problemas.extend([])"),
    ("M62 DDL solo en una direccion", S,
     '        for identidad in sorted(set(esperado) - set(vivo)):\n'
     '            problemas.append(f"falta {_etiqueta_objeto(identidad)}")',
     "        pass"),
    ("M63 huellas: parseo no fail-closed", S,
     "            if not isinstance(esperadas, dict) or not esperadas:", "            if False:"),
    ("M64 huellas: sin keyset exacto", S,
     "                if set(esperadas) != set(vivas):", "                if False:"),
    ("M65 framing parte por \\n", C,
     'return "\\n".join(l for l in texto.splitlines() if not _MARCADOR.match(l))',
     'return "\\n".join(l for l in texto.split("\\n") if not _MARCADOR.match(l))'),
    ("M66 marcas contra el fragmento con elipsis", S,
     "return [m for m in marcas if m[1] <= retenido]",
     "return [m for m in marcas if m[1] <= retenido + 1]"),
    ("M67 sin separador por fila", S,
     "            coste = self._bytes_fila(fila) + SEPARADOR_FILA_BYTES",
     "            coste = self._bytes_fila(fila)"),
    ("M68 sobre con eid de 64", S,
     'return scur.encode(arrival=2 ** 63 - 1, eid="f" * scur.MAX_EID_BYTES,',
     'return scur.encode(arrival=2 ** 63 - 1, eid="f" * 64,'),
    ("M69 sin defensa final de retirada", S,
     "            if len(respuesta[\"filas\"]) <= 1:", "            if True:"),
    ("M70 sin unidad de marcas", S, '            "unidad_marcas": cls.UNIDAD_MARCAS,\n', ""),
    # ── M2-6: autorización del carril y clave de orden ────────────────────────────
    ("M71 sin comprobar la ACL en python", S,
     "        self._autoriza(lane, ledger)\n        if self.con.in_transaction:",
     "        if self.con.in_transaction:"),
    ("M72 sin la ACL en el SQL", S,
     "   AND EXISTS (SELECT 1 FROM search_acl a\n"
     "                WHERE a.lane = ? AND a.ledger = e.ledger)\n", ""),
    ("M73 la clave de orden se puede mover", S,
     "WHEN new.arrival IS NOT old.arrival BEGIN", "WHEN 0 BEGIN"),
    ("M74 el alcance no acota el ledger", S,
     "            if not scope.permite(ledger):", "            if False:"),
    ("M75 principal inventado desde el rol", S,
     "    if not isinstance(principal_id, str) or not principal_id:", "    if False:"),
    ("M76 set_acl no es idempotente", S,
     "            cambia = self.acl_persistida() != canon", "            cambia = True"),
    ("M77 register_udf toca el isolation_level", S,
     "        con.create_function(UDF_PROYECTA, 1, sc.project_body, deterministic=True)\n"
     "\n    @staticmethod",
     "        con.create_function(UDF_PROYECTA, 1, sc.project_body, deterministic=True)\n"
     "        con.isolation_level = None\n"
     "\n    @staticmethod"),
    ("M78 el motivo del base64 se pierde", U,
     "    except CursorMalformed:\n        # YA trae el motivo exacto",
     "    except CursorTooLarge:\n        # YA trae el motivo exacto"),
    # ── M2-8: schema_v total y TODA la clave de orden inmutable ──────────────────
    ("M79 schema_v acepta forma no canonica", S,
     '        if (not crudo or not crudo.isdecimal()\n'
     '                or (len(crudo) > 1 and crudo[0] == "0")):',
     "        if False:"),
    ("M80 schema_v acepta entero fuera de rango", S,
     '        if len(crudo) > len(maximo) or (len(crudo) == len(maximo) and crudo > maximo):',
     "        if False:"),
    ("M81 readiness propaga schema_v corrupto", S,
     "        except SearchSchemaCorrupt as e:", "        except TypeError as e:"),
    ("M82 cambiar eid no invalida", S,
     "WHEN new.ledger IS NOT old.ledger OR new.eid IS NOT old.eid BEGIN",
     "WHEN new.ledger IS NOT old.ledger BEGIN"),
    ("M83 cambiar ledger no invalida", S,
     "WHEN new.ledger IS NOT old.ledger OR new.eid IS NOT old.eid BEGIN",
     "WHEN new.eid IS NOT old.eid BEGIN"),
    ("M84 cambiar clave no ensucia estado", S,
     "     WHERE ledger = new.ledger AND eid = new.eid;\n"
     "  UPDATE search_state SET v = 'building' WHERE k = 'state';\n"
     "  UPDATE search_state SET v = lower(hex(randomblob(16))) WHERE k = 'generation';\n"
     "END;\n\"\"\"",
     "     WHERE ledger = new.ledger AND eid = new.eid;\n"
     "  UPDATE search_state SET v = lower(hex(randomblob(16))) WHERE k = 'generation';\n"
     "END;\n\"\"\""),
    ("M85 cambiar clave no rota generacion", S,
     "  UPDATE search_state SET v = 'building' WHERE k = 'state';\n"
     "  UPDATE search_state SET v = lower(hex(randomblob(16))) WHERE k = 'generation';\n"
     "END;\n\"\"\"",
     "  UPDATE search_state SET v = 'building' WHERE k = 'state';\n"
     "END;\n\"\"\""),
    ("M86 update noop de clave tambien invalida", S,
     "WHEN new.ledger IS NOT old.ledger OR new.eid IS NOT old.eid BEGIN",
     "WHEN 1 BEGIN"),
    # ── M2-9: storage durable, migración cerrada y FTS antes del rebuild ─────────
    ("M87 schema_v acepta BLOB con bytes numericos", S,
     '        if storage != "text":\n'
     '            raise clase(\n'
     '                f"{k} corrupto: storage class {storage!r}, se esperaba \'text\'")',
     '        if False:\n'
     '            raise clase(\n'
     '                f"{k} corrupto: storage class {storage!r}, se esperaba \'text\'")'),
    ("M88 cualquier OperationalError parece tabla ausente", S,
     '        return str(e) == "no such table: search_state"',
     "        return True"),
    ("M89 readiness propaga generation con storage corrupto", S,
     "        except SearchStateCorrupt as e:\n"
     "            gen = None\n"
     "            problema_gen = e.detail",
     "        except TypeError as e:\n"
     "            gen = None\n"
     "            problema_gen = str(e)"),
    ("M90 migracion acepta cualquier version de origen", S,
     "            if version != SEARCH_SCHEMA_V1:", "            if False:"),
    ("M91 migracion no compara cuerpos DDL v1", S,
     "            if distintos:\n                raise SearchMigrationRejected(\n"
     "                    \"DDL v1 desconocido; cuerpo distinto en: \"\n"
     "                    + \", \".join(_etiqueta_objeto(i) for i in distintos))",
     "            if False:\n                raise SearchMigrationRejected(\n"
     "                    \"DDL v1 desconocido; cuerpo distinto en: \"\n"
     "                    + \", \".join(_etiqueta_objeto(i) for i in distintos))"),
    ("M92 migracion no exige huellas v1", S,
     "            if huellas != huellas_v1:", "            if False:"),
    ("M93 migracion no rota generacion", S,
     "            nueva = _nueva_generacion()", "            nueva = generacion"),
    ("M94 migracion no sella schema_v2", S,
     '            self._set("generation", nueva)\n'
     '            self._set("schema_v", str(SEARCH_SCHEMA_V))\n'
     '            self._set("huellas", json.dumps(self._huellas_objetos(), sort_keys=True))',
     '            self._set("generation", nueva)\n'
     '            self._set("huellas", json.dumps(self._huellas_objetos(), sort_keys=True))'),
    ("M95 trigger key deja tokens de la clave vieja", S,
     "WHEN new.ledger IS NOT old.ledger OR new.eid IS NOT old.eid BEGIN\n"
     "  INSERT INTO search_fts(search_fts, rowid, body)\n"
     "    SELECT 'delete', rid, llminbox_proyecta(old.body) FROM search_documents\n"
     "     WHERE ledger = old.ledger AND eid = old.eid;\n"
     "  INSERT OR IGNORE INTO search_documents(ledger, eid) VALUES (new.ledger, new.eid);",
     "WHEN new.ledger IS NOT old.ledger OR new.eid IS NOT old.eid BEGIN\n"
     "  INSERT OR IGNORE INTO search_documents(ledger, eid) VALUES (new.ledger, new.eid);"),
    ("M96 trigger key no indexa la clave nueva", S,
     "  INSERT OR IGNORE INTO search_documents(ledger, eid) VALUES (new.ledger, new.eid);\n"
     "  INSERT INTO search_fts(rowid, body)\n"
     "    SELECT rid, llminbox_proyecta(new.body) FROM search_documents\n"
     "     WHERE ledger = new.ledger AND eid = new.eid;\n"
     "  UPDATE search_state SET v = 'building' WHERE k = 'state';",
     "  INSERT OR IGNORE INTO search_documents(ledger, eid) VALUES (new.ledger, new.eid);\n"
     "  UPDATE search_state SET v = 'building' WHERE k = 'state';"),
    ("M97 update de body borra pero no reindexa", S,
     "WHEN new.ledger = old.ledger AND new.eid = old.eid BEGIN\n"
     "  INSERT INTO search_fts(search_fts, rowid, body)\n"
     "    SELECT 'delete', rid, llminbox_proyecta(old.body) FROM search_documents\n"
     "     WHERE ledger = old.ledger AND eid = old.eid;\n"
     "  INSERT INTO search_fts(rowid, body)\n"
     "    SELECT rid, llminbox_proyecta(new.body) FROM search_documents\n"
     "     WHERE ledger = new.ledger AND eid = new.eid;\n"
     "END;",
     "WHEN new.ledger = old.ledger AND new.eid = old.eid BEGIN\n"
     "  INSERT INTO search_fts(search_fts, rowid, body)\n"
     "    SELECT 'delete', rid, llminbox_proyecta(old.body) FROM search_documents\n"
     "     WHERE ledger = old.ledger AND eid = old.eid;\n"
     "END;"),
    ("M98 migracion no reemplaza el trigger v1", S,
     '            self.con.execute("DROP TRIGGER search_au_key")\n'
     '            self.con.execute(SQL_TRIGGER_AU_KEY_V2.strip().rstrip(";"))\n', ""),
    # ── M2-10: inventario real y sellos SQL NULL/duplicados ─────────────────────
    ("M99 inventario ignora triggers globales", S,
     '        if tipo == "trigger":\n            return True',
     '        if False:\n            return True'),
    ("M100 inventario ignora indices sobre Search", S,
     '        if tipo == "index" and tabla in TABLAS_SEARCH_OWNED:\n            return True',
     '        if False:\n            return True'),
    ("M101 inventario tolera namespace search intruso", S,
     '        if nombre.startswith("search_"):\n            sombra = (tipo, nombre, tabla)',
     '        if False:\n            sombra = (tipo, nombre, tabla)'),
    ("M102 inventario ignora triggers TEMP", S,
     '            return _temp_afecta_search(fila)',
     '            return False'),
    ("M103 raw_state vuelve a ejecutar bytes de NULL", S,
     '        if storage != "text":\n'
     '            raise clase(\n'
     '                f"{k} corrupto: storage class {storage!r}, se esperaba \'text\'")\n'
     '        if not isinstance(raw, (bytes, bytearray, memoryview)):\n'
     '            raise clase(f"{k} corrupto: representación durable no binaria")',
     '        if storage != "text" and raw is not None:\n'
     '            raise clase(\n'
     '                f"{k} corrupto: storage class {storage!r}, se esperaba \'text\'")'),
    ("M104 raw_state acepta filas duplicadas", S,
     '        if len(filas) != 1:\n'
     '            raise clase(f"{k} corrupto: hay más de una fila durable para la misma clave")',
     '        if False:\n'
     '            raise clase(f"{k} corrupto: hay más de una fila durable para la misma clave")'),
    # ── M2-11: identidad sqlite_master y frontera TEMP por conexión ─────────────
    ("M105 inventario vuelve a colapsar por name", S,
     '    return (esquema, fila["type"], fila["name"], fila["tbl_name"])',
     '    return ("main", "object", fila["name"], fila["name"])'),
    ("M106 guard acepta TEMP peligroso preexistente", S,
     "    if peligrosos:\n", "    if False:\n"),
    ("M107 authorizer permite CREATE TEMP TRIGGER", S,
     "    sqlite3.SQLITE_CREATE_TEMP_TRIGGER,\n", ""),
    ("M108 authorizer permite DROP TEMP TABLE", S,
     "    sqlite3.SQLITE_DROP_TEMP_TABLE,\n", ""),
    ("M109 authorizer permite ATTACH", S,
     "_ESQUEMA_ADJUNTO_DENEGADO = frozenset({sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH})",
     "_ESQUEMA_ADJUNTO_DENEGADO = frozenset({sqlite3.SQLITE_DETACH})"),
    ("M110 SearchStore no instala el guard", S,
     "        guard_search_connection(con)\n", ""),
    ("M111 TEMP entries deja de considerarse relevante", S,
     '    return (tipo == "trigger" or nombre_sqlite == "entries"\n'
     '            or nombre_sqlite.startswith("search_")',
     '    return (tipo == "trigger" or False\n'
     '            or nombre_sqlite.startswith("search_")'),
    ("M112 identidad olvida tbl_name", S,
     '    return (esquema, fila["type"], fila["name"], fila["tbl_name"])',
     '    return (esquema, fila["type"], fila["name"], fila["name"])'),
    ("M113 TEMP vuelve a comparar case-sensitive", S,
     "    return nombre.translate(_SQLITE_IDENT_TRANSLATE)",
     "    return nombre"),
]


# ── QUIÉN DEBE MATAR A CADA MUTANTE ──────────────────────────────────────────────
# Una EXPECTATIVA, no una descripción. La versión anterior descubría los nodeids después
# del fallo y los usaba como si fueran la predicción: eso no predice nada, sólo cuenta lo
# que pasó. Aquí se declara ANTES, y el runner exige que el declarado esté entre los que
# fallaron y que vuelva a fallar en un clon FRESCO con la misma mutación.
#
# La declaración es la HIPÓTESIS falsable previa; la corrida causal comprueba después que
# realmente cae y lo reproduce. Un mutante sin declaración sale `MUERTO_SIN_DECLARAR`,
# que NO es una medida y bloquea el GO. `M2_DESCUBRIR=1` sólo ayuda a diagnosticar una
# futura ampliación del censo y sigue siendo no-GO: observar un fallo no lo retroconvierte
# en predicción.
_T3 = "tests/search/test_m2_3_correctivas.py"
_T5 = "tests/search/test_m2_5_correctivas.py"
_T6 = "tests/search/test_m2_6_acl_y_orden.py"
_T7 = "tests/search/test_m2_7_supervivientes.py"
_TF = "tests/search/test_frontera_y_aislamiento.py"
_TC = "tests/search/test_cursor_y_keyset.py"
_TI = "tests/search/test_indice_estado_y_recuperacion.py"
_T8 = "tests/search/test_m2_8_schema_keyset.py"


def _n(fichero: str, prueba: str) -> str:
    return f"{fichero}::{prueba}"


# Contrato estático, escrito antes de la corrida. Los nodeids sin sufijo parametrizado
# designan la prueba completa; pytest añade ``[...]`` al informar qué caso concreto cayó.
MATADORES: dict[str, tuple] = {
    "M01": (_n(_TF, "test_carriles_hermanos_no_se_cuelan"),),
    "M02": (_n(_TF, "test_carriles_hermanos_no_se_cuelan"),),
    "M03": (_n(_TC, "test_orden_total_con_ts_repetido"),),
    "M04": (_n(_T3, "test_cuatro_filas_con_el_MISMO_arrival_paginan_sin_perdida"),),
    "M05": (_n(_TI, "test_las_ausentes_NO_se_sirven_y_no_hay_modo_de_incluirlas"),),
    "M06": (_n(_TI, "test_el_rebuild_NO_sella_un_indice_desacoplado"),),
    "M07": (_n(_TI, "test_una_clave_DISTINTA_no_hereda_el_rid_de_una_muerta"),),
    "M08": (_n(_TI, "test_un_rebuild_que_falla_deja_building_EN_DISCO_y_no_mueve_la_generacion"),),
    "M09": (_n(_T7, "test_readiness_NOMBRA_el_objeto_que_falta_y_no_solo_que_el_DDL_no_cuadra"),),
    "M10": (_n(_TI, "test_rebuild_viejo_es_503_equivalente"),),
    "M11": (_n(_TC, "test_la_firma_es_hmac_sha256_completo"),),
    "M12": (_n(_TC, "test_cursor_atado_a_cada_filtro"),),
    "M13": (_n(_TC, "test_cursor_de_otra_generacion_caduca"),),
    "M14": (_n(_TC, "test_clave_corta_se_rechaza_al_construir"),),
    "M15": (_n(_TI, "test_nunca_trunca_rechaza"),),
    "M16": (_n(_TI, "test_nunca_trunca_rechaza"),),
    "M17": (_n(_TI, "test_filter_sha256_es_el_digest_COMPLETO"),),
    "M18": (_n(_TC, "test_cursor_atado_a_cada_filtro"),),
    "M19": (_n(_TC, "test_el_carril_cambia_la_firma_aunque_el_ledger_sea_el_mismo"),),
    "M20": (_n(_TF, "test_la_entrada_cruda_no_llega_al_motor"),),
    "M21": (_n(_TI, "test_el_fusible_corta_por_tiempo"),),
    "M22": (_n(_TI, "test_el_rebuild_no_sella_si_head_no_esta_en_body"),),
    "M23": (_n(_TI, "test_el_limite_maximo_es_100"),),
    "M24": (_n(_TC, "test_cursor_enorme_se_rechaza_ANTES_de_decodificar"),),
    "M25": (_n(_TC, "test_encode_exige_hex_REAL_no_solo_longitud"),),
    "M26": (_n(_TC, "test_los_filtros_canonicos_exigen_carril_Y_ledger"),),
    "M27": (_n(_TI, "test_borrar_y_reinsertar_LA_MISMA_clave_conserva_su_rid"),),
    "M28": (_n(_TI, "test_el_rebuild_conserva_los_rid_y_las_lapidas"),),
    "M29": (_n(_TF, "test_una_entrada_sin_posicion_no_se_sirve"),),
    "M30": (_n(_TF, "test_la_ventana_de_fechas_es_el_intervalo_semiabierto"),),
    "M31": (_n(_TI, "test_si_NI_UNA_fila_cabe_se_rechaza_tipado_y_SIN_cursor"),),
    "M32": (_n(_TI, "test_si_NI_UNA_fila_cabe_se_rechaza_tipado_y_SIN_cursor"),),
    "M33": (_n(_TI, "test_el_snippet_cabe_en_el_techo_MARCA_INCLUIDA"),),
    "M34": (_n(_TI, "test_los_caps_por_campo_son_contractuales_y_se_declaran"),),
    "M35": (_n(_TI, "test_los_caps_por_campo_son_contractuales_y_se_declaran"),),
    "M36": (_n(_TI, "test_la_medida_FINAL_RETIRA_FILAS_cuando_la_reserva_se_queda_corta"),),
    "M37": (_n(_T5, "test_el_presupuesto_cuenta_los_SEPARADORES_del_array"),),
    "M38": (_n(_T5, "test_una_marca_en_la_zona_CORTADA_no_apunta_a_la_elipsis"),),
    "M39": (_n(_T7, "test_la_reserva_del_sobre_NO_encoge_al_bajar_el_techo"),),
    "M40": (_n(_T3, "test_sin_el_normalizador_la_conexion_FALLA_CERRADA"),),
    "M41": (_n(_T3, "test_el_cuerpo_indexado_no_contiene_NADA_del_framing"),),
    "M42": (_n(_T3, "test_sin_asimetria_entre_lo_indexado_y_lo_consultado"),),
    "M43": (_n(_T3, "test_la_normalizacion_pliega_COMPATIBILIDAD_y_no_solo_el_CASO"),),
    "M44": (_n(_T5, "test_una_huella_DISTINTA_con_el_mismo_juego_de_nombres_pone_not_ready"),),
    "M45": (_n(_T3, "test_ready_exige_generacion_de_32_hex"),),
    "M46": (_n(_T3, "test_si_cambia_el_normalizador_el_indice_deja_de_estar_listo"),),
    "M47": (_n(_T3, "test_un_building_concurrente_NUNCA_sirve_una_pagina"),),
    "M48": (_n(_T3, "test_un_eid_largo_NO_sella_el_indice"),),
    "M49": (_n(_T7, "test_dos_escrituras_del_MISMO_cursor_no_pueden_valer_las_dos"),),
    "M50": (_n(_T7, "test_un_caracter_fuera_del_alfabeto_se_rechaza_POR_EL_ALFABETO"),),
    "M51": (_n(_T3, "test_el_fusible_rechaza_configuraciones_invalidas"),),
    "M52": (_n(_T3, "test_cotas_baratas_de_los_filtros"),),
    "M53": (_n(_T3, "test_la_consulta_cruda_enorme_se_rechaza_SIN_normalizar"),),
    "M54": (_n(_T5, "test_el_keyset_canonico_sigue_siendo_arrival_eid"),),
    "M55": (_n(_TC, "test_truncado_se_declara_siempre"),),
    "M56": (_n(_T3, "test_el_espacio_final_del_titular_no_bloquea_el_rebuild"),),
    "M57": (_n(_T3, "test_las_marcas_son_desplazamientos_y_no_corchetes_dentro_del_texto"),),
    "M58": (_n(_T7, "test_un_eid_por_encima_de_la_frontera_no_llega_a_firmarse"),),
    "M59": (_n(_T3, "test_register_RECHAZA_una_conexion_con_transaccion_abierta"),),
    "M60": (_n(_T5, "test_un_trigger_NO_OP_PREEXISTENTE_no_se_deja_bendecir_por_el_rebuild"),),
    "M61": (_n(_T5, "test_con_las_huellas_RESELLADAS_sobre_el_DDL_roto_sigue_not_ready"),),
    "M62": (_n(_T5, "test_un_objeto_QUE_FALTA_se_detecta_aunque_el_sello_cuadre"),),
    "M63": (_n(_T5, "test_huellas_degeneradas_NUNCA_dejan_ready"),),
    "M64": (_n(_T5, "test_el_juego_de_huellas_se_compara_en_LAS_DOS_direcciones"),),
    "M65": (_n(_T5, "test_el_framing_se_retira_con_cualquier_terminador"),),
    "M66": (_n(_T5, "test_la_frontera_de_las_marcas_es_lo_RETENIDO_no_el_fragmento"),),
    "M67": (_n(_T5, "test_el_presupuesto_cuenta_los_SEPARADORES_del_array"),),
    "M68": (_n(_T5, "test_el_sobre_reserva_el_eid_MAS_LARGO_QUE_SE_PERMITE"),),
    "M69": (_n(_TI, "test_la_medida_FINAL_RETIRA_FILAS_cuando_la_reserva_se_queda_corta"),),
    "M70": (_n(_T5, "test_las_marcas_declaran_su_unidad_y_apuntan_a_texto"),),
    "M71": (_n(_T6, "test_un_carril_NO_AUTORIZADO_no_ve_las_filas"),),
    "M72": (_n(_T6, "test_la_ACL_VIVE_EN_LA_SENTENCIA_no_solo_en_python"),),
    "M73": (_n(_T6, "test_mover_el_arrival_INVALIDA_en_vez_de_perder_filas"),),
    "M74": (_n(_T6, "test_un_alcance_atado_no_deja_pedir_otro_ledger"),),
    "M75": (_n(_T6, "test_todos_los_caminos_del_resolutor_fallan_CERRADOS"),),
    "M76": (_n(_T6, "test_una_ACL_IDENTICA_no_cambia_readiness_ni_generacion"),),
    "M77": (_n(_T3, "test_register_udf_NO_toca_isolation_level_ni_la_transaccion_abierta"),),
    "M78": (_n(_T7, "test_un_caracter_fuera_del_alfabeto_se_rechaza_POR_EL_ALFABETO"),),
    "M79": (_n(_T8, "test_schema_v_corrupto_cierra_readiness_y_no_se_autocura"),),
    "M80": (_n(_T8, "test_schema_v_corrupto_cierra_readiness_y_no_se_autocura"),),
    "M81": (_n(_T8, "test_schema_v_corrupto_cierra_readiness_y_no_se_autocura"),),
    "M82": (_n(_T8, "test_mover_cualquier_componente_del_keyset_invalida_cursor_y_recorre_completo"),),
    "M83": (_n(_T8, "test_mover_cualquier_componente_del_keyset_invalida_cursor_y_recorre_completo"),),
    "M84": (_n(_T8, "test_mover_cualquier_componente_del_keyset_invalida_cursor_y_recorre_completo"),),
    "M85": (_n(_T8, "test_mover_cualquier_componente_del_keyset_invalida_cursor_y_recorre_completo"),),
    "M86": (_n(_T8, "test_update_noop_de_cada_clave_no_rota_generacion_ni_estado"),),
    "M87": (_n(_T8, "test_schema_v_corrupto_durable_conserva_bytes_y_sale_tipado"),),
    "M88": (_n(_T8, "test_solo_no_such_table_equivale_a_ausencia"),),
    "M89": (_n(_T8, "test_readiness_es_total_ante_storage_no_textual_en_sellos_criticos"),),
    "M90": (_n(_T8, "test_migracion_rechaza_origen_no_exacto_sin_mutar"),),
    "M91": (_n(_T8, "test_migracion_rechaza_origen_no_exacto_sin_mutar"),),
    "M92": (_n(_T8, "test_migracion_rechaza_origen_no_exacto_sin_mutar"),),
    "M93": (_n(_T8, "test_migracion_v1_a_v2_es_explicita_exacta_y_no_reconstruye"),),
    "M94": (_n(_T8, "test_migracion_v1_a_v2_es_explicita_exacta_y_no_reconstruye"),),
    "M95": (_n(_T8, "test_trigger_de_keyset_conserva_fts_exacto_antes_del_rebuild"),),
    "M96": (_n(_T8, "test_trigger_de_keyset_conserva_fts_exacto_antes_del_rebuild"),),
    "M97": (_n(_T8, "test_noop_de_clave_y_update_de_body_reindexa_sin_invalidar"),),
    "M98": (_n(_T8, "test_migracion_v1_a_v2_es_explicita_exacta_y_no_reconstruye"),),
    "M99": (_n(_T8, "test_trigger_global_con_target_ajeno_y_cuerpo_search_tambien_se_rechaza"),),
    "M100": (_n(_T8, "test_objetos_extra_que_ocupan_search_se_rechazan"),),
    "M101": (_n(_T8, "test_objetos_extra_que_ocupan_search_se_rechazan"),),
    "M102": (_n(_T8, "test_trigger_TEMP_sobre_entries_no_escapa_del_inventario"),),
    "M103": (_n(_T8, "test_readiness_es_total_ante_NULL_en_cada_sello_y_no_escribe"),),
    "M104": (_n(_T8, "test_dos_filas_para_un_sello_son_corrupcion_tipificada"),),
    "M105": (_n(_T8, "test_tabla_y_trigger_homonimos_no_colapsan_y_el_cursor_no_pierde_e4"),),
    "M106": (_n(_T8, "test_guard_rechaza_TEMP_preexistente_y_no_colapsa_homonimos"),),
    "M107": (_n(_T8, "test_guard_niega_nuevo_DDL_TEMP_en_writer_B"),),
    "M108": (_n(_T8, "test_guard_niega_DROP_TEMP_y_ATTACH"),),
    "M109": (_n(_T8, "test_guard_niega_DROP_TEMP_y_ATTACH"),),
    "M110": (_n(_T8, "test_SearchStore_aplica_guard_y_rechaza_TEMP_preexistente_relevante"),),
    "M111": (_n(_T8, "test_SearchStore_aplica_guard_y_rechaza_TEMP_preexistente_relevante"),),
    "M112": (_n(_T5, "test_el_manifiesto_esperado_cubre_tablas_indices_vista_fts_y_triggers"),),
    "M113": (_n(_T8, "test_TEMP_SEARCH_STATE_con_ocho_sellos_se_rechaza_antes_de_readiness"),),
}

DESCUBRIR = os.environ.get("M2_DESCUBRIR", "").strip() not in ("", "0", "no")


class Interrumpido(Exception):
    """`SIGINT`/`SIGTERM`. Se propaga para que el `finally` cierre bien."""


# ── Clasificación del `rc` de pytest ──────────────────────────────────────────────
# SÓLO `1` significa «un test falló», que es lo único que mata a un mutante. Los demás
# códigos son del ARNÉS, no del sujeto, y contarlos como `MUERTO` es exactamente el falso
# verde que este fichero existe para no producir: `5` (no se recogió ningún test) y `2`
# (error de recolección — el mutante rompió el import) salen no-cero sin que NADIE haya
# aseverado nada. Un `rc` negativo es una señal en el hijo.
PYTEST_OK, PYTEST_FALLOS = 0, 1
_RC_ARNES = {
    2: "pytest interrumpido / error de recolección (nadie aseveró nada)",
    3: "error interno de pytest",
    4: "error de uso de pytest",
    5: "pytest no recogió NINGÚN test",
}


def veredicto_por_rc(rc: int, detalle: str) -> tuple[str, str]:
    """`rc` → (veredicto, detalle). Pura y sin efectos: es lo que prueban sus tests."""
    if rc == PYTEST_FALLOS:
        return "MUERTO", detalle
    if rc == PYTEST_OK:
        return "VIVO", detalle
    if rc < 0:
        return "SENAL", f"el hijo murió por señal {-rc}: {detalle}"
    motivo = _RC_ARNES.get(rc, f"rc={rc} no documentado")
    return "ARNES_ERROR", f"{motivo} · {detalle}"


def valida_aguja(fuente: str, viejo: str, nuevo: str, fichero: str):
    """`None` si la aguja es EXACTA; si no, `(veredicto, detalle)`.

    Exige **exactamente una** ocurrencia. Con dos, `str.replace(..., 1)` muta la PRIMERA
    —que no tiene por qué ser la que el mutante nombra— y el arnés certificaría como
    «sustitución exacta» un cambio en otro sitio. Y exige que la sustitución CAMBIE algo:
    una aguja idéntica a su reemplazo produce un mutante que es el sujeto limpio, o sea
    un `MUERTO` imposible y un `VIVO` mentiroso.
    """
    n = fuente.count(viejo)
    if n == 0:
        return "VACUO", f"aguja ausente en {fichero}"
    if n > 1:
        return "AGUJA_AMBIGUA", (
            f"la aguja aparece {n} veces en {fichero}: `replace(...,1)` mutaría una "
            f"arbitraria y el resultado no probaría lo que el mutante dice probar")
    if viejo == nuevo or fuente.replace(viejo, nuevo, 1) == fuente:
        return "MUTACION_NULA", f"la sustitución no cambia {fichero}"
    return None


def _sha_fichero(ruta: pathlib.Path) -> str:
    return hashlib.sha256(ruta.read_bytes()).hexdigest()


def _manifiesto(raiz: pathlib.Path) -> dict:
    """`path -> sha256`, ORDENADO, de fuentes y tests. Incluye los untracked.

    El contenido es lo que identifica al sujeto. `HEAD` no basta —los tests de esta tanda
    no están commiteados— y `git diff` tampoco los ve. Por eso se hashea el fichero.
    """
    entradas = {}
    for rel in (*FUENTES, *CONFIG):
        f = raiz / rel
        if f.exists():
            entradas[rel] = _sha_fichero(f)
    for rel_dir in (TESTS_DIR, BENCH_DIR):
        d = raiz / rel_dir
        if not d.is_dir():
            continue
        for f in sorted(d.rglob("*.py")):
            if "__pycache__" in f.parts:
                continue
            entradas[str(f.relative_to(raiz))] = _sha_fichero(f)
    return dict(sorted(entradas.items()))


def _digest(manifiesto: dict) -> str:
    return hashlib.sha256(json.dumps(manifiesto, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _git(raiz: pathlib.Path, *args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=raiz, capture_output=True,
                              text=True, timeout=30).stdout.strip()
    except Exception as e:                                  # pragma: no cover
        return f"<git falló: {e}>"


def _sujeto(raiz: pathlib.Path, manifiesto: dict) -> dict:
    diff = _git(raiz, "diff", "HEAD")
    return {
        "head": _git(raiz, "rev-parse", "HEAD"),
        "rama": _git(raiz, "rev-parse", "--abbrev-ref", "HEAD"),
        "status_porcelain": _git(raiz, "status", "--porcelain"),
        "diff_sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
        "diff_bytes": len(diff.encode("utf-8")),
        "manifiesto_digest": _digest(manifiesto),
        "manifiesto": manifiesto,
        # El sujeto no es sólo el árbol: la MISMA suite contra otro intérprete o contra
        # otro pytest no prueba lo mismo. Va escrito en la evidencia.
        "python": sys.version.split()[0],
        "python_ejecutable": sys.executable,
        "pytest": _version_pytest(),
        "sqlite": _version_sqlite(),
    }


def _version_pytest() -> str:
    try:
        return importlib.metadata.version("pytest")
    except Exception as e:                                   # pragma: no cover
        return f"<no medible: {type(e).__name__}>"


def _version_sqlite() -> str:
    try:
        import sqlite3
        return sqlite3.sqlite_version
    except Exception as e:                                   # pragma: no cover
        return f"<no medible: {type(e).__name__}>"


def _vuelca(estado: dict, salida: pathlib.Path) -> None:
    """Atómico Y DURABLE: `fsync` del fichero **y del directorio**.

    `os.replace` es atómico, pero la entrada de directorio que lo publica también vive en
    caché: sin `fsync` del directorio, un corte de corriente puede dejar el fichero viejo
    (o ninguno) aunque el nuevo se hubiera escrito y sincronizado. Se promete durabilidad
    ante caída, así que se paga entera.
    """
    # TEMPORAL ÚNICO. Un nombre fijo es una condición de carrera con nombre propio: dos
    # procesos escribiendo a la vez se pisan el `.tmp` y `os.replace` publica un fichero
    # medio escrito por el otro. El `flock` de `main()` impide la carrera entre corridas;
    # esto la impide también entre cualquier otro escritor.
    tmp = salida.with_name(f"{salida.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(estado, fh, ensure_ascii=False, indent=1, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, salida)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    # El `fsync` del DIRECTORIO **no se traga**. Era un `except OSError: pass`: si fallaba,
    # la promesa de durabilidad quedaba incumplida y el JSON decía lo mismo que si se
    # hubiera cumplido. Una promesa que no se puede comprobar no es una promesa.
    fd = os.open(str(salida.parent), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _nodeids_fallidos(salida: str) -> list[str]:
    """Los `FAILED`/`ERROR` con NOMBRE. Es lo que convierte un `rc` en una CAUSA."""
    fuera = []
    for l in (salida or "").splitlines():
        for pre in ("FAILED ", "ERROR "):
            if l.startswith(pre):
                nodeid = l[len(pre):].split(" - ")[0].strip().split(" ")[0]
                if nodeid and nodeid not in fuera:
                    fuera.append(nodeid)
    return fuera


def _corre_pytest(cwd: str, nodeids: list | None = None) -> tuple[int, str, list]:
    """Suite en su PROPIO grupo de procesos, con timeout y `killpg` acotado.

    Con `nodeids`, corre EXACTAMENTE esos y nada más: es la corrida DIRIGIDA con la que se
    comprueba que los tests que dicen matar a un mutante lo matan de verdad y no fue una
    intermitencia de otro sitio.
    """
    # El `Popen` va DENTRO del `try`. Fuera, había una ventana real: una señal entregada
    # entre que `Popen` devuelve el hijo y que se entra al bloque dejaba un pytest
    # huérfano corriendo contra un árbol temporal que este arnés iba a borrar.
    proc = None
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "pytest", *(nodeids or [TESTS_DIR]),
             "-q", "--no-header"],
            cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True)
        salida, _ = proc.communicate(timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        _mata(proc)
        return -9, "TIMEOUT", []
    except BaseException:
        # `BaseException` y no `Interrumpido`: `KeyboardInterrupt` y `SystemExit` también
        # dejaban al hijo suelto, y son exactamente los dos que más se dan al abortar.
        _mata(proc)
        raise
    cola = [l for l in (salida or "").strip().splitlines()
            if "passed" in l or "failed" in l or "error" in l]
    return (proc.returncode, (cola[-1] if cola else f"rc={proc.returncode}"),
            _nodeids_fallidos(salida))


class ArnesError(Exception):
    """Fallo del INSTRUMENTO, no del sujeto. Nunca se cuenta como veredicto."""


def _mata(proc) -> None:
    """SÓLO el grupo del hijo, y se COMPRUEBA que murió.

    `killpg` + `communicate` sin verificar el `poll()` era un cierre que se creía a sí
    mismo: si el hijo sobrevivía (grupo cambiado, zombi con hijos propios), el arnés
    seguía adelante con un pytest vivo escribiendo en un árbol que estaba a punto de
    borrar. Ahora, si tras el intento sigue vivo, se dice y falla cerrado.
    """
    if proc is None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    with contextlib.suppress(Exception):
        proc.communicate(timeout=30)
    if proc.poll() is None:
        raise ArnesError(f"el hijo {proc.pid} sigue vivo tras SIGKILL a su grupo")


def _instala_senales() -> None:
    def manejador(num, _frame):
        raise Interrumpido(f"señal {num}")
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, manejador)


def ids_mutantes() -> list[str]:
    return [m[0].split()[0] for m in MUTANTES]


def _seleccion() -> list:
    """`M2_MUTANTES=M01,M60` corre sólo ésos. Existe para poder DIAGNOSTICAR el arnés sin
    pagar la batería entera: una corrida de dos mutantes prueba el mecanismo (instantánea,
    manifiesto, grupo de procesos, JSON terminal) en un minuto.

    ⚠️ Una corrida así **no es evidencia y no puede pasar el gate**: va a otro fichero y
    devuelve no-cero. Ver `es_canonica`.
    """
    filtro = os.environ.get("M2_MUTANTES", "").strip()
    if not filtro:
        return list(MUTANTES)
    quiero = {x.strip() for x in filtro.split(",") if x.strip()}
    elegidos = [m for m in MUTANTES if m[0].split()[0] in quiero]
    faltan = quiero - {m[0].split()[0] for m in elegidos}
    if faltan:
        raise SystemExit(f"M2_MUTANTES nombra mutantes que no existen: {sorted(faltan)}")
    return elegidos


def es_canonica(seleccion: list, resultados: dict) -> tuple[bool, str]:
    """El GO exige el CENSO ENTERO, no «los que corrí».

    Dos condiciones distintas y las dos necesarias: que la SELECCIÓN sean todos los ids
    únicos del censo, y que haya un RESULTADO por cada uno. La primera sin la segunda deja
    pasar una corrida abortada a la mitad; la segunda sin la primera deja pasar una
    selección parcial que casualmente tenga tantos resultados como eligió.
    """
    todos = set(ids_mutantes())
    elegidos = {m[0].split()[0] for m in seleccion}
    if elegidos != todos:
        return False, (f"selección parcial: {len(elegidos)} de {len(todos)} mutantes "
                       f"(faltan {sorted(todos - elegidos)[:8]}…)")
    if len(resultados) != len(MUTANTES):
        return False, (f"{len(resultados)} resultados para {len(MUTANTES)} mutantes: "
                       f"la corrida no llegó al final")
    return True, "censo completo"


# Veredictos que NO son una medida. Un `VIVO` sí lo es —el mutante sobrevivió, y eso es
# un hallazgo—; éstos son el instrumento fallando, y con cualquiera de ellos dentro la
# corrida no puede llamarse `COMPLETA`.
NO_MEDIDOS = frozenset({"ERROR", "ARNES_ERROR", "TIMEOUT", "SENAL", "INTERRUMPIDO",
                        "VACUO", "AGUJA_AMBIGUA", "MUTACION_NULA", "MUTACION_IMPRECISA",
                        "MUERTO_NO_REPRODUCIBLE", "MUERTO_SIN_CAUSA_DECLARADA",
                        "MUERTO_SIN_DECLARAR"})


def _permisos(raiz: str, *, escribible: bool) -> None:
    """Congela (o descongela) un árbol entero. La instantánea PRÍSTINA se deja de SÓLO
    LECTURA: correr la suite dentro de ella la contamina (`__pycache__`, `.pytest_cache`,
    ficheros temporales de los tests) y a partir de ahí cada mutante nace de un sujeto
    que ya no es el que se hasheó. Read-only convierte esa contaminación en un error
    ruidoso en vez de en una deriva silenciosa."""
    dmode, fmode = (0o755, 0o644) if escribible else (0o555, 0o444)
    for base_dir, dirs, ficheros in os.walk(raiz, topdown=not escribible):
        for f in ficheros:
            with contextlib.suppress(OSError):
                os.chmod(os.path.join(base_dir, f), fmode)
        for d in dirs:
            with contextlib.suppress(OSError):
                os.chmod(os.path.join(base_dir, d), dmode)
    with contextlib.suppress(OSError):
        os.chmod(raiz, dmode)


def _clon(origen: str, destino: str) -> str:
    """Copia DESECHABLE y escribible de la instantánea prístina."""
    shutil.copytree(origen, destino)
    _permisos(destino, escribible=True)
    return destino


@contextlib.contextmanager
def _cerrojo(salida: pathlib.Path):
    """`flock` EXCLUSIVO y no bloqueante sobre la evidencia.

    Dos corridas a la vez sobre el mismo fichero producen un JSON que no describe a
    ninguna de las dos: los resultados se entrelazan y el resumen final es del último que
    escriba. Es exactamente el modo de fallo que ya se dio (un full obsoleto corriendo en
    paralelo). Se falla cerrado: la segunda no espera, se niega.
    """
    lock = salida.with_name(salida.name + ".lock")
    fh = open(lock, "a+")
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise ArnesError(
                f"hay otra corrida con el cerrojo {lock}: no se escribe evidencia "
                f"concurrente ({e})") from None
        fh.seek(0), fh.truncate(), fh.write(f"pid={os.getpid()}\n"), fh.flush()
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def _causa(base: str, snapshot: str, mutante: tuple, fallidos: list,
           res: dict) -> tuple[str, str]:
    """`MUERTO` exige CAUSA DECLARADA, NOMBRADA y REPRODUCIDA EN UN ÁRBOL LIMPIO.

    Un `rc=1` dice «algo falló», no «este mutante fue cazado». Con la suite entera
    corriendo, cualquier intermitencia ajena produce el mismo `rc` y el arnés lo firmaba
    como mutante muerto.

    Tres defectos de la primera versión, los tres reales:
      ① descubría los nodeids DESPUÉS del fallo y los usaba como si fueran la expectativa:
         eso no es una predicción, es una descripción. Ahora cada mutante DECLARA a quién
         espera que le mate (`MATADORES`), y el declarado tiene que estar entre los que
         fallaron.
      ② repetía la corrida dirigida sobre el MISMO `dst`, ya contaminado por la pasada
         anterior (`.pytest_cache`, `__pycache__`, ficheros que escriban los tests). Una
         reproducción sobre el árbol sucio no descarta que el estado dejado por la primera
         pasada sea parte de la causa. Ahora se hace un CLON FRESCO de la instantánea y se
         le aplica la MISMA mutación.
      ③ leía `matador_declarado` de un sitio donde nadie lo escribía: código muerto que
         parecía un control.
    """
    nombre, fichero, viejo_txt, nuevo_txt = mutante[0], mutante[1], mutante[2], mutante[3]
    clave = nombre.split()[0]
    if not fallidos:
        return "ARNES_ERROR", "rc=1 sin un solo `FAILED` con nombre: no hay causa"
    ajenos = [n for n in fallidos if not n.startswith(TESTS_DIR)]
    if ajenos:
        return "ARNES_ERROR", f"falló algo fuera de {TESTS_DIR}: {ajenos[:4]}"

    declarados = MATADORES.get(clave)
    res["matadores_declarados"] = declarados
    if not declarados:
        if DESCUBRIR:
            # MODO DESCUBRIMIENTO: registra lo observado para poder DECLARARLO luego con
            # evidencia. Sigue sin ser una medida y sigue bloqueando el GO.
            res["descubierto"] = fallidos
            return ("MUERTO_SIN_DECLARAR",
                    f"descubierto (no declarado): {fallidos[:6]}")
        return ("MUERTO_SIN_DECLARAR",
                f"este mutante no declara quién debe matarlo; fallaron {fallidos[:4]}. "
                f"Añádelo a `MATADORES` con lo medido")
    def cayo(declarado: str, observados: list[str]) -> bool:
        return any(n == declarado or n.startswith(declarado + "[")
                   for n in observados)

    faltan = [n for n in declarados if not cayo(n, fallidos)]
    if faltan:
        return ("MUERTO_SIN_CAUSA_DECLARADA",
                f"se esperaba que lo matasen {faltan}; fallaron {fallidos[:6]}")

    # ── reproducción en un árbol LIMPIO, con la misma mutación ──
    fresco = os.path.join(base, f"{clave}-repro")
    try:
        _clon(snapshot, fresco)
        p = pathlib.Path(fresco, fichero)
        p.write_text(p.read_text(encoding="utf-8").replace(viejo_txt, nuevo_txt, 1),
                     encoding="utf-8")
        rc2, det2, fall2 = _corre_pytest(fresco, nodeids=list(declarados))
    finally:
        _permisos(fresco, escribible=True)
        shutil.rmtree(fresco, ignore_errors=True)
    res["dirigida"] = {"rc": rc2, "detalle": det2, "fallidos": fall2,
                       "arbol": "clon fresco de la instantánea"}
    if rc2 != PYTEST_FALLOS or any(not cayo(n, fall2) for n in declarados):
        return ("MUERTO_NO_REPRODUCIBLE",
                f"en un clon FRESCO con la misma mutación, los declarados dieron rc={rc2} "
                f"({det2}) y fallaron {fall2}: la muerte no se reproduce")
    return "MUERTO", f"{det2} · causa declarada: {', '.join(n.split('::')[-1] for n in declarados)}"


def main() -> int:
    _instala_senales()
    seleccion = _seleccion()
    parcial = {m[0].split()[0] for m in seleccion} != set(ids_mutantes())
    # Una corrida parcial NUNCA escribe sobre la evidencia canónica: si compartieran ruta,
    # un diagnóstico de dos mutantes borraría la única corrida que sí valía.
    salida = SALIDA_PARCIAL if parcial else SALIDA
    manifiesto_vivo = _manifiesto(RAIZ)
    sujeto = _sujeto(RAIZ, manifiesto_vivo)
    base = tempfile.mkdtemp(prefix="m2-mutantes-")
    snapshot = os.path.join(base, "_sujeto_congelado")
    estado: dict = {
        "sujeto": sujeto, "timeout_s": TIMEOUT_S, "total": len(seleccion),
        "censo_total": len(MUTANTES), "seleccion_parcial": parcial,
        "seleccion": sorted(m[0].split()[0] for m in seleccion),
        "comenzado": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "resultados": {}, "estado_corrida": "EN_CURSO", "salida": str(salida),
    }
    # El CERROJO se toma ANTES de escribir nada: si hay otra corrida, esta ni siquiera
    # toca el fichero de evidencia. Escribir primero y bloquear después dejaría el JSON
    # de la corrida buena pisado por un `EN_CURSO` de la que se va a negar.
    try:
        cerrojo = _cerrojo(salida)
        cerrojo.__enter__()
    except ArnesError as e:
        print(f"MUTANTES [OTRA_CORRIDA_EN_CURSO]: {e}")
        shutil.rmtree(base, ignore_errors=True)
        return 1
    try:
        _vuelca(estado, salida)
        return _corrida(estado, seleccion, sujeto, base, snapshot, salida)
    finally:
        cerrojo.__exit__(None, None, None)


def _corrida(estado, seleccion, sujeto, base, snapshot, salida) -> int:
    try:
        # ── ⓪ ids únicos: dos mutantes con la misma clave se pisan en `resultados` ──
        dups = sorted({i for i in ids_mutantes() if ids_mutantes().count(i) > 1})
        if dups:
            estado["estado_corrida"] = f"IDS_DUPLICADOS {dups}"
            return 1

        # ── ① instantánea CONGELADA, una sola vez ────────────────────────────────
        shutil.copytree(RAIZ, snapshot, ignore=shutil.ignore_patterns(
            ".git", "__pycache__", ".venv*", "*.sqlite*", "node_modules", "web",
            ".mutantes_m2.json*", ".mutantes_m2.parcial.json*"))
        man_snap = _manifiesto(pathlib.Path(snapshot))
        if _digest(man_snap) != sujeto["manifiesto_digest"]:
            estado["estado_corrida"] = "SNAPSHOT_INFIEL"
            return 1
        estado["snapshot_digest"] = _digest(man_snap)
        # PRÍSTINA e INTOCABLE a partir de aquí. Nada se ejecuta DENTRO de ella.
        _permisos(snapshot, escribible=False)

        # ── ② PRE-VUELO DE LAS AGUJAS sobre la instantánea, ANTES de correr nada ──
        # Una aguja obsoleta o ambigua se descubre en segundos aquí, no tras una hora de
        # corrida. Y aborta: con el instrumento roto, los veredictos no valen.
        agujas = {}
        for nombre, fichero, viejo, nuevo in seleccion:
            fuente = pathlib.Path(snapshot, fichero).read_text(encoding="utf-8")
            malo = valida_aguja(fuente, viejo, nuevo, fichero)
            if malo:
                agujas[nombre.split()[0]] = {"veredicto": malo[0], "detalle": malo[1],
                                             "nombre": nombre}
        if agujas:
            estado["agujas_invalidas"] = agujas
            estado["resultados"].update(agujas)
            estado["estado_corrida"] = "AGUJAS_INVALIDAS"
            for k, v in sorted(agujas.items()):
                print(f"  {v['veredicto']:<20}{v['nombre']} — {v['detalle']}")
            return 1

        # ── ⑥ la suite LIMPIA primero, en un CLON DESECHABLE ─────────────────────
        # Correrla dentro de la prístina la ensuciaba: pytest deja `__pycache__`,
        # `.pytest_cache` y lo que escriban los tests, así que el árbol del que nacían
        # los 98 mutantes no era ya el que se hasheó. El clon se borra y la prístina no
        # se toca (además de estar en sólo lectura, que convierte el intento en error).
        limpio = _clon(snapshot, os.path.join(base, "_suite_limpia"))
        try:
            rc, detalle, fallidos_limpia = _corre_pytest(limpio)
        finally:
            _permisos(limpio, escribible=True)
            shutil.rmtree(limpio, ignore_errors=True)
        estado["suite_limpia"] = {"rc": rc, "detalle": detalle,
                                  "fallidos": fallidos_limpia}
        _vuelca(estado, salida)
        if rc != 0:
            estado["estado_corrida"] = "SUJETO_EN_ROJO"
            print(f"  SUJETO EN ROJO: rc={rc} {detalle} — no corro mutantes")
            return 1

        for nombre, fichero, viejo, nuevo in seleccion:
            clave = nombre.split()[0]
            dst = os.path.join(base, clave)
            try:
                # ① nace de la INSTANTÁNEA prístina, jamás de RAIZ
                _clon(snapshot, dst)
                p = pathlib.Path(dst, fichero)
                fuente = p.read_text(encoding="utf-8")
                malo = valida_aguja(fuente, viejo, nuevo, fichero)
                if malo:
                    res = {"veredicto": malo[0], "detalle": malo[1]}
                else:
                    p.write_text(fuente.replace(viejo, nuevo, 1), encoding="utf-8")
                    # ③ la mutación toca EXACTAMENTE el fichero previsto
                    man_mut = _manifiesto(pathlib.Path(dst))
                    difs = sorted(k for k in man_snap
                                  if man_mut.get(k) != man_snap[k])
                    nuevos = sorted(set(man_mut) - set(man_snap))
                    if difs != [fichero] or nuevos:
                        res = {"veredicto": "MUTACION_IMPRECISA",
                               "detalle": f"cambió {difs + nuevos}, se esperaba [{fichero}]"}
                    else:
                        rc, detalle, fallidos = _corre_pytest(dst)
                        if detalle == "TIMEOUT":
                            res = {"veredicto": "TIMEOUT",
                                   "detalle": f"sin medir tras {TIMEOUT_S:g}s", "rc": rc}
                        else:
                            v, d = veredicto_por_rc(rc, detalle)
                            res = {"veredicto": v, "detalle": d, "rc": rc,
                                   "nodeids_matadores": fallidos}
                            if v == "MUERTO":
                                v, d = _causa(base, snapshot,
                                              (nombre, fichero, viejo, nuevo),
                                              fallidos, res)
                                res["veredicto"], res["detalle"] = v, d
            except Interrumpido:
                raise
            except Exception as e:
                # FAIL-CLOSED: cualquier fallo del arnés (copytree, permisos, lectura,
                # disco lleno) es un NO-MEDIDO con nombre propio. Antes esta rama dejaba
                # `res` sin asignar y el `finally` de fuera nunca llegaba a escribir un
                # estado terminal: el JSON se quedaba en `EN_CURSO`, indistinguible de
                # una corrida viva, y el operador leía «aún corriendo» sobre un cadáver.
                res = {"veredicto": "ERROR",
                       "detalle": f"{type(e).__name__}: {e}"}
            finally:
                _permisos(dst, escribible=True)
                shutil.rmtree(dst, ignore_errors=True)
            res["nombre"] = nombre
            estado["resultados"][clave] = res
            print(f"  {res['veredicto']:<20}{nombre}", flush=True)
            _vuelca(estado, salida)

        # ③ el sujeto congelado NO se movió durante la corrida
        if _digest(_manifiesto(pathlib.Path(snapshot))) != estado["snapshot_digest"]:
            estado["estado_corrida"] = "SNAPSHOT_ALTERADO"
            return 1
        estado["estado_corrida"] = "COMPLETA"
    except Interrumpido as e:
        estado["estado_corrida"] = f"INTERRUMPIDO ({e})"
        for nombre, *_ in seleccion:
            clave = nombre.split()[0]
            estado["resultados"].setdefault(
                clave, {"nombre": nombre, "veredicto": "INTERRUMPIDO",
                        "detalle": "no se llegó a medir"})
    except Exception as e:
        # Igual que arriba pero para lo que pase FUERA del bucle (instantánea, git,
        # manifiesto). Sin esto, `estado_corrida` se quedaba en `EN_CURSO`.
        estado["estado_corrida"] = f"ERROR ({type(e).__name__}: {e})"
    finally:
        _permisos(base, escribible=True)
        shutil.rmtree(base, ignore_errors=True)
        estado["terminado"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        # ③ (bis) EL VIVO, al final. Rehashear sólo la instantánea prueba que la COPIA no
        # se movió — que es inmutabilidad de la copia, no deriva del árbol vivo. Son dos
        # cosas distintas y sólo la primera invalida la evidencia. Que el vivo cambie
        # durante la corrida NO la invalida: todos los resultados prueban el árbol congelado
        # cuyo digest está escrito arriba. Se registra para que el lector sepa que el
        # `git status` de este JSON puede no describir ya el directorio de trabajo.
        try:
            vivo_final = _digest(_manifiesto(RAIZ))
        except Exception as e:                               # pragma: no cover
            vivo_final = f"<no medible: {type(e).__name__}: {e}>"
        estado["manifiesto_vivo_final_digest"] = vivo_final
        estado["vivo_cambio_durante_la_corrida"] = (
            vivo_final != sujeto["manifiesto_digest"])
        cuenta: dict = {}
        for r in estado["resultados"].values():
            cuenta[r["veredicto"]] = cuenta.get(r["veredicto"], 0) + 1
        estado["resumen"] = cuenta
        if estado.get("estado_corrida") in (None, "EN_CURSO"):
            # No se puede salir de `main` con el estado inicial: si se sale, es que algo
            # se llevó el flujo por delante sin dejar nombre. Terminal y no-cero.
            estado["estado_corrida"] = "ERROR_SIN_CERRAR"
        canonica, motivo = es_canonica(seleccion, estado["resultados"])
        # `COMPLETA` significa «todo el censo se MIDIO», no «el bucle llegó al final». Con un
        # solo `ERROR`, `ARNES_ERROR`, `TIMEOUT`, `SENAL` o aguja rota dentro, el bucle
        # también llega al final y el estado decía `COMPLETA`: un instrumento averiado
        # firmando su propia corrida como buena.
        degradado = sorted(set(cuenta) & NO_MEDIDOS)
        estado["degradado"] = degradado
        estado["canonica"] = canonica
        estado["canonica_motivo"] = motivo
        if estado["estado_corrida"] == "COMPLETA":
            if degradado:
                estado["estado_corrida"] = "ARNES_DEGRADADO"
            elif not canonica:
                estado["estado_corrida"] = "PARTIAL_DIAGNOSTIC"
        _vuelca(estado, salida)
        sin_declarar = {k: v.get("descubierto") for k, v in estado["resultados"].items()
                        if v.get("veredicto") == "MUERTO_SIN_DECLARAR" and v.get("descubierto")}
        if sin_declarar:
            estado["declaraciones_propuestas"] = sin_declarar
            print("\n# Pega esto en MATADORES (medido en esta corrida):")
            for k in sorted(sin_declarar):
                print(f'    "{k}": ({", ".join(repr(n) for n in sin_declarar[k])},),')
            print()
        print(f"MUTANTES [{estado['estado_corrida']}]: " +
              " · ".join(f"{v} {k.lower()}" for k, v in sorted(cuenta.items())) +
              f" · total {len(seleccion)}/{len(MUTANTES)} · canónica={canonica}"
              f" ({motivo}) · no-medidos={degradado or 'ninguno'}"
              f" · evidencia: {salida}")

    # Sólo MUERTO cuenta como cazado. VIVO, VACUO, AGUJA_AMBIGUA, MUTACION_NULA,
    # MUTACION_IMPRECISA, TIMEOUT, INTERRUMPIDO, ERROR, SENAL y ARNES_ERROR son fallos:
    # los que no son VIVO lo son porque NO SE MIDIÓ, y un no-medido contado como muerto
    # es el falso verde que esto existe para no producir. Y una corrida NO CANÓNICA no
    # pasa el gate ni aunque todo lo que corrió muriera.
    malos = sum(v for k, v in cuenta.items() if k != "MUERTO")
    return 0 if (malos == 0 and estado["estado_corrida"] == "COMPLETA"
                 and estado["canonica"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
