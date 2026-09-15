# Falsadores de `d83ae04` — mapeo invariante → cuerpo de test

Encargo codex-llminbox 2026-09-08 («completa los falsadores faltantes de v1-v5
y contradicciones recovery/workload»). Base: `a8a7126`. **Reescrito** tras el
FINDING de codex (`MARK:astra-schema-vlegacy-forma-valida-no-solo-sello`) y el
FINDING de qa (`MARK:qa-f-vlegacy-parcial-cadena-falsada-f-assoc-confundida`).
Estado: **ESCRITOS, NO EJECUTADOS** — capacidad medida NO-GO

🔻 **ACTUALIZADO 2026-09-11 (sdet, adjudicado por @db-mig `MARK:dbmig-adjudico-generalizar-los-ocho-al-patron-de-retirada`):**
los `8` de `test_migracion_v1_a_v2` que seguían aseverando la migración **ya están escritos
en este patrón y CORRIDOS** (arnés en proceso, sobre `1d7bb19`): `14/14` verdes. El `k` viaja
en el pre-aserto y el motivo del rechazo es el MISMO para los ocho — medido:

| pieza | valor medido |
|---|---|
| mensaje del rechazo | `el contrato beta sólo admite creación nueva, v6→v7 o v7; durable_v={k} exige un migrador offline anterior` |
| sustratos cubiertos | v1 (A y B) · v2 · v2 con fila histórica de hash · v2 con idempotency tocada |
| ⊖ levantar la guarda del contrato | los `8` caen por **otro** error, y la aserción del mensaje lo distingue |
| ⊖ sustrato corrupto (sello `v3` sobre forma v1) | caen en el **pre-aserto** («dejó de ser una v1 reconocible») |
| hermanos pendientes | preservación 1→actual · escritura nueva `verified` · ruta 1→2→3 · replay con `req_hash_v=1` · `ReplayUnverifiable` con `req_hash_v=99` |

Lo que sigue **ESCRITO Y NO EJECUTADO** es el resto de la tabla de abajo (`k=1..5` con
`_fixture_historica`), que necesita el migrador offline estacionado.
(gate-capacidad-v3.sh 09:04-09:05Z, veredicto NO-GO; Pytest/Docker/benchmark
NOT_RUN). La ejecución serial focal es de `@sdet` según Phase B; el primer run
autorizado cita el SHA exacto de este commit. No se acepta el antiguo «5/5»
(`MARK:qa-item4-no-es-cinco-de-cinco-…`).

## v1-v5 rejection

**Población REQUERIDA — las cinco formas históricas VÁLIDAS** (criterio codex):
`test_base_historica_valida_rechazada_por_limite_de_version` (param. k=1..5).
Fixture `_fixture_historica(tmp_path, k)`: base v7 REAL con datos (principal +
command), rebajada por DELTA-INVERSA de los migradores (v7→v6 es el mismo
patrón de `_fixture_v6`; v5..v1 son las inversas de `_m5_a_6`…`_m1_a_2`). El
test PRE-ASERTE que `_clasificar` la reconoce — retorno real de 3 elementos:
`("conocida", k, "")` — y SÓLO entonces exige el rechazo: `MigrationFailed`
con «durable_v={k} … migrador offline» (`_transicion_a_listo`: el contrato
beta «sólo admite creación nueva, v6→v7 o v7»; `_migrar` sólo 6→7). Pins
post-estado: 0 tablas `V7_ONLY`, sello intacto, DATOS originales preservados.

| Complementaria | Test | Qué falsa |
|---|---|---|
| sello SIN forma (criterio codex: «conserva como cobertura complementaria») | `test_sello_v1_a_v5_sin_forma_rechazado_antes_de_cualquier_byte_v7` (param. k=1..5) | `SchemaIndeterminate` («dice v{k} y …») + 0 bytes v7 + sello intacto |
| ⊕ del instrumento | `test_crear_path_si_materializa_bytes_v7` | el predicado «¿hay bytes v7?» da POSITIVO en creación — la ausencia de los falsadores no es ceguera |

Corrección del commit anterior (`0b2689b`): su README y el docstring afirmaban
que la shape-backed v1/v2 «MIGRA por el opener actual». FALSADO por qa sobre
`0b2689b`: `_m1_a_2`…`_m5_a_6` no tienen call-site fuera de `tests/` — la
cadena histórica no es camino alcanzable desde `initialize()`; la forma válida
v1..v5 cae en `MigrationFailed` por límite de versión. Los tests de
`test_migracion_v1_a_v2.py` son legado NO alcanzable en v7, no cobertura.

## association contradictions (tupla de 5 + estado)

`test_observacion_cruzada_rechazada_la_tupla_no_presta_su_recibo`, flota
`_fleet_asociacion` (be-01/target-a y be-02/target-b, cada uno con SU tupla —
NO hay tupla compartida: `u_expected_runtime` UNIQUE (lane, revision,
runtime_instance), coordination.py:1629, la hace inexistente).

| Campo de la tupla | Muerte | Sonda |
|---|---|---|
| `target_runtime_instance` | **BARRERA PREVIA** | be-01 observada con la runtime de be-02 ⇒ `SubjectNotFound` antes del JOIN, por desigualdad con el workload activo. La sonda acredita esa barrera y atomicidad; no distingue eliminar sólo ese predicado del JOIN |
| `organization_revision` | **COTA DECLARADA** | toda activación resetea `runtime_status` a `absent` y el JOIN exige `recovering`; la sonda no distingue eliminar sólo el predicado de revisión. Sí exige el comportamiento R2 huérfana tras bump ⇒ `RecoveryConflict` atómico; no se ha ejecutado un mutante |
| `workload_id` | **COTA DECLARADA · BARRERA ACREDITADA** | la muerte aislada exigiría dos workloads compartiendo (lane, revisión, runtime, generation) — población que `u_expected_runtime` hace INEXISTENTE; la barrera se demuestra con SU prueba `test_dos_workloads_no_comparten_runtime_en_la_misma_revision` (`sqlite3.IntegrityError` + atomicidad: la revisión activa sigue siendo 1). La restricción productiva NO se toca para que un fixture pase |
| `target_generation` | **COTA DECLARADA** | viaja en la MISMA fila de expected_workloads que la revisión y sólo cambia con bump → no diverge sola jamás |
| `lane` | **COTA DECLARADA** | el lane del JOIN es el de la SESIÓN observadora y la validación de workloads exige el (runtime, generation) declarado como `runtime_session` EN ESE lane → toda variación de lane arrastra runtime ajena |

No se atribuye muerte de mutantes combinados ni equivalencia universal por
lectura de estas sondas. ⊕ dentro del test: la cita desde SU tupla cierra
(`fresh` + command `succeeded`) — el rechazo discrimina por tupla, no es veto
general.

## cached connection tras restore por OTRA instancia (qa 10:00:44Z)

`test_conexion_cacheada_tras_restore_por_otra_instancia_exige_initialize` —
la otra mitad del término two-Journal/inode, escrita y pendiente de ejecución
y aceptación independiente; no eleva ningún ✓ de QA. A escribe (thread-local
caliente); B, SEGUNDA instancia sobre la MISMA ruta, sella con el motivo
admitido `DRAIN_FOR_ROLLBACK` y corre `restore_pre_v7_snapshot`. Se comprueba
que A conserva el mismo handle cacheado antes de su siguiente operación;
la siguiente escritura operacional de A ⇒ `IdentityChanged` ESTRICTO (la
identidad corre en `_Tx.__enter__` antes de admisión/auth — un
`AdmissionConflict` sería verde por la razón equivocada), evento
`_identidad_invalidada` puesto, el REINTENTO también falla (exige
`initialize()` explícito), y pins del fichero: `durable_v='6'`, tablas
disjuntas de V7_ONLY, sha == snapshot. Mutante P0 que mata: sin el check
(coordination.py:4146-4152) la fd cacheada de A escribe en el inode huérfano
— pérdida silenciosa.

## (los 3 ya verificados por cuerpos — qa 08-sep)

`test_lock_sh_del_writer_…` · `test_v6_con_forma_v7_parcial_…` ·
`test_dos_journals_no_asignan_…` — two-Journal/inode · exact v6 · sequence
scope.
