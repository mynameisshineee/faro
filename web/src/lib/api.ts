/** Cliente del API de llminbox.
 *
 *  El token lo teclea la persona y vive en el localStorage de su navegador; cada
 *  petición lo manda en la cabecera. Nunca se incrusta en el HTML servido: el
 *  puerto lo alcanza cualquier contenedor de la máquina (medido en Docker Desktop
 *  para macOS), así que regalar el token en la página sería regalarlo a todos.
 */
const CLAVE = "llminbox.token";
export const getToken = () => localStorage.getItem(CLAVE) ?? "";
export const setToken = (t: string) => localStorage.setItem(CLAVE, t);
export const clearToken = () => localStorage.removeItem(CLAVE);

/** Credencial BOOTSTRAP del plano nativo — en SU PROPIA clave de localStorage,
 *  jamás en `CLAVE` [encargo astra runtime-root 2026-09-08: "Mantén token nativo
 *  separado del legacy"]. El canal (/entries, /inbox…) sigue autenticando con
 *  `X-Llminbox-Token`; el nativo (/native/v1/*) abre una SESIÓN con esta
 *  credencial (`POST /native/v1/sessions` → Bearer de sesión). Dos credenciales,
 *  dos cabeceras, dos ciclos de vida: un 401 nativo NUNCA toca `CLAVE`. */
const CLAVE_CREDENCIAL_NATIVA = "llminbox.nativo.credencial";
export const getCredencialNativa = () => localStorage.getItem(CLAVE_CREDENCIAL_NATIVA) ?? "";
export const setCredencialNativa = (t: string) => localStorage.setItem(CLAVE_CREDENCIAL_NATIVA, t);

export type Entrada = {
  ledger: string; eid: string; arrival: number; seq: number;
  ts: string | null; actor: string | null; tipo: string | null;
  line_no: number; head: string; body?: string; to?: string[];
  // EL DESMENTIDO DE ATRIBUCIÓN, que el servicio manda en CADA entrada y esta
  // interfaz no leía. Su comentario en `servicio.py` explica por qué existe: «un
  // campo estructurado de un índice consultable se lee como hecho del sistema mucho
  // más que una firma al pie. Sin desmentido, se lee como afirmado».
  //
  // Son DOS preguntas distintas y la interfaz sólo contestaba la primera:
  //     el ACTO ......... autenticado por el canal (token)   → sí
  //     la ATRIBUCIÓN ... verificada individualmente         → NO
  //
  // `/inbox` lo dice en su cabecera de texto desde hace tiempo; aquí no se decía.
  actor_identity_verified?: boolean; actor_provenance?: string;
};
export type Estado = {
  ledger: string; bytes: number | null; entradas: number; desaparecidas: number;
  con_tipo_pct: number; con_fecha_pct: number; con_destinatario_pct: number; ultima: string | null;
};
export type Salud = {
  ok: boolean; auth: boolean; ledgers: number; rotos: Record<string, string> | null;
  // `null` cuando el índice se puede escribir; el MOTIVO cuando no. Está aquí porque
  // `ok:false` no significa «el indexador va tarde»: significa una de tres cosas, y el
  // banner las atribuía todas a la primera.
  solo_lectura: string | null;
  aviso: string | null; reconstrucciones: number;
  // El build del PROCESO y CÓMO se supo. `origen` no es adorno: `declarado` viene
  // inyectado en la imagen y afirma qué se construyó; `derivado` sale del .git del disco
  // y sólo afirma qué hay en ese directorio ahora. `sha` es null cuando no se sabe —
  // nunca "" ni "unknown", que se leen como un valor.
  // `huella` es el sha256 del fichero que el proceso ejecuta AHORA, leído en cada
  // petición. No convierte `sha` en verdad: hace que una mentira tenga que ser
  // consistente con git —mentir en `sha` Y en `huella`, y que las dos casen contra un
  // fichero real de ese commit—. `huella_de` dice qué cubre: el fichero, no la imagen.
  build: { sha: string | null; origen: "declarado" | "derivado" | "desconocido";
           parece_sha: boolean; huella: string | null; huella_de: string };
  // `cadencia_s` es el par ya resuelto: si el barrido tarda más que `poll_s`, la cadencia
  // real ES el barrido. Sin las dos, nadie ve que el intervalo configurado sea inalcanzable.
  indexador: { error: string | null; fallos_seguidos: number; hace_s: number | null;
               poll_s: number; cadencia_s: number };
};
/** El censo crudo: quién es agente (y de qué humano responde), quién es humano
 *  escribiendo directo, y qué nombres son destinos de difusión. Lo sirve `/roster`
 *  sin reinterpretar — la clasificación (agente/humano/difusión) vive en el cliente. */
export type Roster = {
  agentes: { nombre: string | null; humano: string | null }[];
  humanos: { nombre: string | null; alias: string[] }[];
  difusion: string[];
};
/** `{ledger: última_llegada_leída}` del agente en `YO`. -1 = nunca leído. Solo
 *  lectura: la avanza la CLI u otro agente, nunca esta interfaz. */
export type Cursores = Record<string, number>;
export type AckGrant = {
  v: 1; grant: string; principal: string; role: string; lane: string;
  ledger: string; cursor_generation: number; cursor_before: number;
  allowed_arrivals: number[]; watermark: number; expires_at: number;
};

/** Una fila de `jerarquia`: rol → de quién depende, en qué capa vive, a quién
 *  gatea y de quién toma criterio [ledger_parse.py::_cargar_jerarquia]. Sólo
 *  `reporta_a`/`capa` están confirmados contra los fixtures reales
 *  (`tests/pytest/test_doctor_principals.py`); `gatea`/`criterios_de` son
 *  opcionales porque ningún fixture visto los trae poblados todavía. */
export type EntradaJerarquia = {
  reporta_a?: string | null;
  capa?: string | null;
  gatea?: string | string[] | null;
  criterios_de?: string | null;
};
/** `GET /organigrama` — LEGADO, `authority:false` por decisión explícita
 *  [`docs/ADR-002-FLEET-CONTROL-PLANE.md` Decisión 6: *"Legacy `GET /organigrama`
 *  remains available for compatibility... It is advisory and must be labelled
 *  `authority:false`. Native clients and the v1 console use `GET
 *  /native/v1/organization`; they do not merge the legacy endpoint into an
 *  authoritative view."*]. Este tipo NO es el grafo de responsabilidad G8 — es
 *  compatibilidad, y `flota.ts`/los componentes no lo mezclan con
 *  `OrganizacionNativa`. `revision` se conserva como diagnóstico crudo, nunca
 *  como versión validada (ADR-002 Decisión 8: *"`unknown` is not accepted as a
 *  version or revision"* — esa regla es para la fuente NATIVA; aquí se respeta
 *  no dándole a este campo ningún peso de autoridad). Con `jerarquia` vacío hay
 *  DOS causas distintas y el propio servicio las distingue en `aviso`: sin
 *  montar (`source_sha256 === null`) o montada sin el campo. */
export type Organigrama = {
  revision: string | number | null;
  source_sha256: string | null;
  loaded_sha256: string | null;
  cargado_en: string | null;
  stale: boolean;
  jerarquia: Record<string, EntradaJerarquia>;
  roles: number;
  aviso: string | null;
};

/** Valida la forma de `/organigrama` en el borde, fail-closed: un campo con tipo
 *  ajeno hace que TODA la respuesta se trate como inválida (null), nunca que se
 *  sirvan a medias los campos que sí casaron. `/organigrama` es legado y hand-
 *  edited (`PROTOCOL.md`); confiar en su forma sin comprobarla es exactamente lo
 *  que ADR-002 Decisión 8 prohíbe para la fuente nativa, y no hay motivo para
 *  tratarla peor aquí sólo porque el endpoint es de compatibilidad. */
export function validarOrganigrama(x: unknown): Organigrama | null {
  if (!x || typeof x !== "object") return null;
  const o = x as Record<string, unknown>;
  if (typeof o.stale !== "boolean") return null;
  if (typeof o.roles !== "number") return null;
  if (o.source_sha256 !== null && typeof o.source_sha256 !== "string") return null;
  if (o.loaded_sha256 !== null && typeof o.loaded_sha256 !== "string") return null;
  if (o.cargado_en !== null && typeof o.cargado_en !== "string") return null;
  if (o.aviso !== null && typeof o.aviso !== "string") return null;
  if (o.revision !== null && typeof o.revision !== "string" && typeof o.revision !== "number") return null;
  const j = o.jerarquia;
  if (j === null || typeof j !== "object" || Array.isArray(j)) return null;
  for (const v of Object.values(j as Record<string, unknown>)) {
    if (!v || typeof v !== "object" || Array.isArray(v)) return null;
  }
  return {
    revision: (o.revision as string | number | null) ?? null,
    source_sha256: o.source_sha256 as string | null,
    loaded_sha256: o.loaded_sha256 as string | null,
    cargado_en: o.cargado_en as string | null,
    stale: o.stale,
    jerarquia: j as Record<string, EntradaJerarquia>,
    roles: o.roles,
    aviso: o.aviso as string | null,
  };
}

/** `RuntimeStatus` — vocabulario CERRADO de 6 valores, `ADR-002-FLEET-CONTROL-
 *  PLANE.md` Decisión 1. Deliberadamente NO es lo mismo que el `Estado` de 10
 *  valores del detector M3 (`observability.py`) — esa decisión existe
 *  precisamente para que nadie traduzca `muda` a `stopped` por su cuenta. Cada
 *  valor tiene una prueba de autoridad exigida por la ADR (nunca inferido de un
 *  timeout ni de prosa de agente):
 *   · absent      — sin binding de runtime activo en la revisión de organización
 *                    vigente, o nunca observado. NUNCA se infiere de un id desconocido.
 *   · fresh       — un supervisor externo autenticado confirmó la instancia exacta
 *                    dentro del plazo.
 *   · stale       — el plazo se superó. Un timeout NUNCA implica `stopped` por sí solo.
 *   · degraded    — evidencia autenticada de detector/recurso, sin salida conocida.
 *   · stopped     — observación externa autenticada de salida/ausencia del proceso.
 *   · recovering  — comando de recuperación aceptado, aún sin resolver.
 */
export type RuntimeStatus = "absent" | "fresh" | "stale" | "degraded" | "stopped" | "recovering";

/** Fila de `GET /native/v1/runtimes` [ADR-002 Decisión 4/5]. `receipt_id` y
 *  `organization_revision` viajan en CADA fila a propósito — Decisión 8 exige
 *  mostrar "the status receipt and organisation revision used for every joined
 *  row", no un recibo global para toda la tabla. Campos completos contra el
 *  texto de la Decisión 4 (*"lane · target principal/role/runtime_instance/
 *  credential_generation · status · detector_state · status_seq · status_since
 *  · last_observed_at · cause_id · transition_id · receipt_id ·
 *  organization_revision"*) — `credential_generation` y `status_seq` faltaban
 *  en una transcripción anterior [finding directo de `codex-llminbox`,
 *  independiente de si el endpoint existe: es un error contra el TEXTO, no
 *  contra un servidor vivo].
 *
 *  `runtime_instance` es `string | null` [diferencia D2, RULING cto #1252].
 *  CORREGIDO tras RULING cto #1348 — la forma real del CHECK
 *  (`coordination.py:1838`) es UNIDIRECCIONAL:
 *  `(status='absent' OR (runtime_instance IS NOT NULL AND principal_id IS NOT
 *  NULL))` — un runtime NO-absent exige vínculo, pero un `absent` NO obliga a
 *  NULL: puede llevar un vínculo real nunca observado. Por eso el cliente NO
 *  fija el cruzado `absent ⇒ null`: rechazar `absent` con `runtime_instance`
 *  presente tumbaría un caso legítimo que el gateway sirve y testea. El string
 *  VACÍO sí se rechaza: no hay ninguna fila legítima con "". */
export type ObservacionRuntimeNativa = {
  runtime_instance: string | null;
  /** D7: la unidad de la ausencia es el WORKLOAD — el servidor keys
   *  `runtime_status` por PK (lane, workload_id). OBLIGATORIO y no vacío en
   *  las lecturas [contrato codex #1343, verificado cto #1348: "workload_id
   *  OBLIGATORIO" en GET /runtimes]. */
  workload_id: string;
  lane: string;
  principal: string | null;
  role: string | null;
  /** Generación de la credencial del runtime observado — distinta de
   *  `organization_revision`: una rota el certificado de rollback (ver el
   *  contrato C4 de admisión), la otra versiona el grafo de responsabilidad.
   *  No se funden aunque ambas sean enteros de generación. */
  credential_generation: string | number | null;
  status: RuntimeStatus;
  /** Vocabulario M3, expuesto POR SEPARADO — nunca se deriva `status` de esto en
   *  el cliente (esa es la regla que Decisión 1 protege). */
  detector_state: string | null;
  /** Contador de TRANSICIONES DE ESTADO del runtime observado (el TARGET).
   *  ATENCIÓN [infra #1346, medido]: NO es `supervisor_seq` — ese es el
   *  contador propio del OBSERVADOR por (observer, target, generación), vive en
   *  `runtime_observations` y NO viaja en las lecturas. Un consumidor que
   *  rehidrate su secuencia con `status_seq` usa un número plausible del
   *  tamaño equivocado. Decisión 4 ("supervisor_seq is monotonic per observer")
   *  habla del OTRO contador — este campo sólo se MUESTRA, nunca se reutiliza. */
  status_seq: number;
  status_since: string | null;
  last_observed_at: string | null;
  cause_id: string | null;
  transition_id: string | null;
  receipt_id: string | null;
  organization_revision: number;
};

/** Trigger de escalado — vocabulario CERRADO de 4 valores, igual que la CHECK de
 *  `organization_escalations` (`coordination.py:1594`). El cliente NO traduce:
 *  la etiqueta legible la pinta la tabla. */
export type TriggerEscaladoNativa = "BLOCKED" | "REVIEW_REQUIRED" | "INCIDENT" | "RECOVERY_FAILED";
/** Ruta de escalado ESTRUCTURADA [diferencia D6, RULING cto #1252 — ADR-002
 *  Decisión 6: "typed escalation routes"]. El `string[]` plano anterior PERDÍA
 *  `trigger_code`: saber A QUIÉN escala sin saber POR QUÉ no es el grafo que la
 *  ADR describe. */
export type RutaEscaladoNativa = { trigger_code: TriggerEscaladoNativa; target_role: string };

/** Rol de `GET /native/v1/organization` [ADR-002 Decisión 6] — el grafo de
 *  responsabilidad AUTORITATIVO, cuando exista. Nótese la ausencia deliberada de
 *  cualquier campo de "agente/nombre": la ADR es explícita — "Organisation data
 *  never modifies principals, credential bindings, capabilities, sessions or
 *  leases" y el beta no tiene mutación agente-facing. El vínculo agente↔rol no
 *  es dato de este tipo, y no se inventa uno client-side (ver `flota.ts`).
 *
 *  `layer` es `number | null` [diferencia D5, RULING cto #1252]: en el wire
 *  viaja el ENTERO de `organization_roles` (CHECK 0-255 en BD); la etiqueta
 *  ("c-suite", "ejecución") es presentación y la pinta el cliente. */
export type RolOrganizacionNativa = {
  role: string;
  reports_to: string | null;
  reviewers: string[];
  escalation_routes: RutaEscaladoNativa[];
  layer: number | null;
};
/** Vocabulario CERRADO para "freshness/attestation state" [Decisión 6: "source
 *  digest, activation time and freshness/attestation state"]. `no-atestiguada`
 *  es DISTINTA de `stale`: una organización puede ser reciente en el reloj y
 *  seguir sin atestación de integridad — son dos preguntas, igual que
 *  `RuntimeStatus.stale` (plazo) nunca implica `stopped` (evidencia externa) por
 *  sí solo. Nombres ES del adapter sobre la CHECK EN de BD
 *  (`attestation_state: attested/unattested/stale`, biyección 1:1) — la
 *  proyección del gateway emite estos nombres [RULING cto #1252]. */
export type FrescuraOrganizacionNativa = "atestiguada" | "no-atestiguada" | "rancia";
/** La fila de revisión de `organization_revisions` (`coordination.py:1538`),
 *  proyectada: `lane`+número viajan DENTRO de `revision` porque la respuesta
 *  también lleva `roles` — `revision` suelto sería ambiguo. */
export type RevisionOrganizacionNativa = {
  lane: string;
  revision: number;
  source_digest: string;
  freshness: FrescuraOrganizacionNativa;
  activated_at: string;
};
/** `GET /native/v1/organization` — forma adjudicada [diferencias D3/D8, RULING
 *  cto #1252: el gateway PROYECTA su forma relacional de 5 tablas a esta; el
 *  cliente no reconstruye]. `authority:true` se EXIGE en el borde (Decisión 6/8:
 *  sin el positivo, la respuesta nativa y la legada `authority:false` son
 *  indistinguibles — y "unknown is not accepted"). */
/** Un workload ESPERADO por la organización activa [D7 — `expected_workloads`,
 *  `coordination.py:1609`, PK (lane, organization_revision, workload_id)]. Es la
 *  unidad de la ausencia: un rol con 2 workloads esperados produce 2 filas
 *  `absent`, no 1. Sólo los campos que el cruce necesita viajan al tipo — el
 *  resto (principal_id, credential_generation…) queda en el wire sin contrato
 *  cliente hasta que se consuma. */
export type CargaEsperadaNativa = { workload_id: string; role: string };
export type OrganizacionNativa = {
  authority: true;
  revision: RevisionOrganizacionNativa;
  roles: RolOrganizacionNativa[];
  /** SIEMPRE presente en el wire, aunque vacía [contrato codex #1343,
   *  verificado cto #1348: "workloads SIEMPRE presente aunque vacío"] — la
   *  unidad de la ausencia (D7). Vacía con roles presentes ⇒ ningún absent: un
   *  rol sin workloads esperados no se espera en ejecución. */
  workloads: CargaEsperadaNativa[];
};

/** Validación fail-closed para cuando `runtimesNativos()`/`organizacionNativa()`
 *  dejen de ser stubs y empiecen a parsear JSON de verdad — sin esperar a ese
 *  día para escribirla y probarla: Decisión 8 es explícita, "Runtime schemas are
 *  validated at the boundary; `unknown` is not accepted as a version or
 *  revision", y eso se cumple mejor teniendo la función lista ANTES del primer
 *  fetch real que después, con prisa y sin test. Hoy no la llama nadie (el seam
 *  nunca toca la red) — el día que la llame, este es el único punto que cambia. */
export function validarObservacionRuntimeNativa(x: unknown): ObservacionRuntimeNativa | null {
  if (!x || typeof x !== "object") return null;
  const o = x as Record<string, unknown>;
  const RUNTIME_STATUS: readonly string[] = ["absent", "fresh", "stale", "degraded", "stopped", "recovering"];
  // D2 [corregido tras RULING cto #1348]: el CHECK real es UNIDIRECCIONAL —
  // no-absent exige vínculo, absent NO obliga a null (puede llevar vínculo
  // real nunca observado). El cliente no fija el cruzado; el string vacío no
  // existe en ninguna fila legítima y se rechaza.
  if (o.runtime_instance !== null && (typeof o.runtime_instance !== "string" || !o.runtime_instance)) return null;
  // D7: OBLIGATORIO y no vacío en las lecturas [contrato codex #1343].
  if (typeof o.workload_id !== "string" || !o.workload_id) return null;
  if (typeof o.lane !== "string" || !o.lane) return null;
  if (typeof o.status !== "string" || !RUNTIME_STATUS.includes(o.status)) return null;
  if (typeof o.status_seq !== "number" || !Number.isInteger(o.status_seq)) return null;
  if (typeof o.organization_revision !== "number" || !Number.isInteger(o.organization_revision)) return null;
  const opcionalTexto = (v: unknown) => v === null || typeof v === "string";
  if (!opcionalTexto(o.principal) || !opcionalTexto(o.role) || !opcionalTexto(o.detector_state)) return null;
  if (!opcionalTexto(o.status_since) || !opcionalTexto(o.last_observed_at)) return null;
  if (!opcionalTexto(o.cause_id) || !opcionalTexto(o.transition_id) || !opcionalTexto(o.receipt_id)) return null;
  if (o.credential_generation !== null && typeof o.credential_generation !== "string" && typeof o.credential_generation !== "number")
    return null;
  return o as unknown as ObservacionRuntimeNativa;
}

/** Misma doctrina que `validarObservacionRuntimeNativa`, para el otro seam. Una
 *  fila de `roles` con forma ajena invalida TODA la organización — un grafo de
 *  responsabilidad a medias no es un grafo más pequeño, es un grafo que miente
 *  sobre a quién gobierna. */
export function validarOrganizacionNativa(x: unknown): OrganizacionNativa | null {
  if (!x || typeof x !== "object") return null;
  const o = x as Record<string, unknown>;
  const FRESCURA: readonly string[] = ["atestiguada", "no-atestiguada", "rancia"];
  const TRIGGERS: readonly string[] = ["BLOCKED", "REVIEW_REQUIRED", "INCIDENT", "RECOVERY_FAILED"];
  // D8: el positivo EXIGIDO — false y ausente caen igual. Decisión 8: "unknown
  // is not accepted".
  if (o.authority !== true) return null;
  if (!o.revision || typeof o.revision !== "object" || Array.isArray(o.revision)) return null;
  const rev = o.revision as Record<string, unknown>;
  if (typeof rev.lane !== "string" || !rev.lane) return null;
  if (typeof rev.revision !== "number" || !Number.isInteger(rev.revision)) return null;
  if (typeof rev.source_digest !== "string" || !rev.source_digest) return null;
  if (typeof rev.activated_at !== "string" || !rev.activated_at) return null;
  if (typeof rev.freshness !== "string" || !FRESCURA.includes(rev.freshness)) return null;
  if (!Array.isArray(o.roles)) return null;
  for (const r of o.roles) {
    if (!r || typeof r !== "object") return null;
    const rol = r as Record<string, unknown>;
    if (typeof rol.role !== "string" || !rol.role) return null;
    if (rol.reports_to !== null && typeof rol.reports_to !== "string") return null;
    if (!Array.isArray(rol.reviewers) || rol.reviewers.some((v) => typeof v !== "string")) return null;
    // D6: estructurada — un string plano ("cto") se rechaza: perdía trigger_code.
    if (!Array.isArray(rol.escalation_routes)) return null;
    for (const e of rol.escalation_routes) {
      if (!e || typeof e !== "object" || Array.isArray(e)) return null;
      const ruta = e as Record<string, unknown>;
      if (typeof ruta.trigger_code !== "string" || !TRIGGERS.includes(ruta.trigger_code)) return null;
      if (typeof ruta.target_role !== "string" || !ruta.target_role) return null;
    }
    // D5: ENTERO en el wire (CHECK 0-255 es del servidor); la etiqueta la pinta el cliente.
    if (rol.layer !== null && (typeof rol.layer !== "number" || !Number.isInteger(rol.layer))) return null;
  }
  // D7: OBLIGATORIA [contrato codex #1343 — "workloads SIEMPRE presente aunque
  // vacío"], y cada carga nombra su workload_id y su rol — una carga a medias
  // no es una carga menos, es un absent que apuntaría al workload equivocado.
  if (!Array.isArray(o.workloads)) return null;
  for (const w of o.workloads) {
    if (!w || typeof w !== "object" || Array.isArray(w)) return null;
    const carga = w as Record<string, unknown>;
    if (typeof carga.workload_id !== "string" || !carga.workload_id) return null;
    if (typeof carga.role !== "string" || !carga.role) return null;
  }
  return o as unknown as OrganizacionNativa;
}

/** El "seam" honesto: `disponible:false` es un ESTADO DE PRIMERA CLASE, no un
 *  array vacío disfrazado de "cero fallos" [ADR-002 Decisión 8: "Returning an
 *  empty array is not evidence that the fleet has zero failures"]. Ningún
 *  consumidor puede confundir "no lo sé" con "no hay nada que saber" — son la
 *  MISMA distinción que ya obliga `Organigrama.aviso` para el legado, aplicada
 *  ahora a la fuente nativa.
 *
 *  `ausente:true` es la TERCERA vía que trae el 404 tipado del gateway
 *  [contrato codex #1343]: `SUBJECT_NOT_FOUND` = "el servidor está sano y
 *  responde que NO hay revisión de organización activa". Ni "no lo sé" (fallo)
 *  ni "cero fallos" (vacío) — un dato propio que el panel pinta con su estado. */
export type SeamNativo<T> =
  | { disponible: true; datos: T }
  | { disponible: false; motivo: string; ausente?: true };

// ─── SESIÓN NATIVA — POST /native/v1/sessions, Bearer de sesión ─────────────
// Wire real [native_gateway.py `_session_wire`, coordination.py `IssuedSession`]:
// {token, runtime_instance, principal, role, lane, expires_at: float, generation,
// principal_source, capabilities}. El token de sesión vive SÓLO en memoria de
// módulo — no en localStorage: es de vida corta y se reabre sola.

export type SesionNativa = {
  token: string;
  runtime_instance: string;
  principal: string;
  role: string;
  lane: string;
  expires_at: number;
  generation: number;
  principal_source: string;
  capabilities: string[];
};

export function validarSesionNativa(x: unknown): SesionNativa | null {
  if (!x || typeof x !== "object") return null;
  const o = x as Record<string, unknown>;
  if (typeof o.token !== "string" || !o.token) return null;
  if (typeof o.runtime_instance !== "string" || !o.runtime_instance) return null;
  if (typeof o.principal !== "string" || !o.principal) return null;
  if (typeof o.role !== "string" || !o.role) return null;
  if (typeof o.lane !== "string" || !o.lane) return null;
  if (typeof o.expires_at !== "number" || !Number.isFinite(o.expires_at)) return null;
  if (typeof o.generation !== "number" || !Number.isInteger(o.generation)) return null;
  if (typeof o.principal_source !== "string") return null;
  if (!Array.isArray(o.capabilities) || o.capabilities.some((c) => typeof c !== "string")) return null;
  return o as unknown as SesionNativa;
}

/** VENCIDA también en el MISMO instante (≤) y ante `expires_at` corrompido —
 *  fail-closed: ante la duda se REABRE la sesión, nunca se confía en un token
 *  cuya caducidad no se sabe leer. */
export function sesionExpirada(s: SesionNativa, ahora: number): boolean {
  if (typeof s.expires_at !== "number" || !Number.isFinite(s.expires_at)) return true;
  return s.expires_at <= ahora;
}

// ── Ciclo de vida de la credencial [bug de 955192c corregido, encargo codex
// #1350-next]: el caché de sesión está VINCULADO a la credencial con la que se
// abrió. `setCredencialNativa` sólo escribía storage y `sesionEnMemoria`
// seguía siendo la del principal/carril ANTERIOR hasta caducar — pedirNativo
// reutilizaba la identidad vieja con la credencial nueva. Ahora:
//
//   · `sesionUtil` — el predicado puro: sesión sirve ⇔ no vencida ∧ abierta
//     CON la credencial que se pide AHORA.
//   · cambiar/borrar credencial sueltan la sesión Y SUBEN el epoch: toda
//     respuesta en vuelo abierta con la identidad anterior se DESCARTA al
//     volver (IDENTIDAD_CAMBIADA) y pedirNativo reintenta una vez con la nueva.
//   · la apertura concurrente se COMPARTE por credencial: los dos polls
//     (organización y runtimes) sanean al mismo intervalo y sólo UNA
//     `POST /native/v1/sessions` vuela a la vez.

let sesionEnMemoria: SesionNativa | null = null;
let credencialDeSesion = "";
let epochNativa = 0;
let aperturaEnVuelo: Promise<SesionNativa> | null = null;
let aperturaDeVueloCredencial = "";
const oyentesIdentidad = new Set<() => void>();

/** Lector público de la revisión de identidad (el epoch) y su SUSCRIPCIÓN —
 *  un entero no secreto, jamás la credencial. La capa de vista lo lee con
 *  `useSyncExternalStore` para que el cambio de credencial re-renderee y las
 *  queryKeys cambien CON la identidad [codex #1445: `removeQueries` destruye
 *  en silencio y no vacía el resultado de un observador activo — la pantalla
 *  necesita que la CLAVE cambie, no sólo que la caché se vacíe]. */
export const revisionIdentidadNativa = (): number => epochNativa;
export const suscribirseIdentidadNativa = (oyente: () => void): (() => void) => {
  oyentesIdentidad.add(oyente);
  return () => {
    oyentesIdentidad.delete(oyente);
  };
};

/** Cambia la credencial bootstrap: suelta la sesión vieja y sube el epoch
 *  ANTES de escribir, para que ninguna respuesta de la identidad anterior
 *  llegue a pintarse. La UI llama a ésta (nunca al set crudo). */
export function cambiarCredencialNativa(t: string): void {
  epochNativa++;
  sesionEnMemoria = null;
  aperturaEnVuelo = null;
  setCredencialNativa(t);
  for (const oyente of oyentesIdentidad) oyente();
}

/** Borra la credencial: igual que cambiar, y la sesión siguiente no puede
 *  abrirse — el seam cae a `SIN_CREDENCIAL` con su motivo, estado de primera
 *  clase. Jamás toca la clave del token del canal. */
export function borrarCredencialNativa(): void {
  epochNativa++;
  sesionEnMemoria = null;
  aperturaEnVuelo = null;
  localStorage.removeItem(CLAVE_CREDENCIAL_NATIVA);
  for (const oyente of oyentesIdentidad) oyente();
}

/** El predicado puro de reutilización: la sesión sirve ⇔ existe ∧ no vencida
 *  ∧ fue abierta CON la credencial que se pide ahora. */
export function sesionUtil(
  s: SesionNativa | null,
  credencialAbierta: string,
  credencialActual: string,
  ahora: number,
): boolean {
  return s !== null && credencialAbierta === credencialActual && !sesionExpirada(s, ahora);
}

/** D12 — el predicado que distingue «no hay revisión activa» de cualquier otro
 *  fallo. Sólo el 404 cuyo cuerpo trae `code:"SUBJECT_NOT_FOUND"` [_ERRORS en
 *  native_gateway.py] es ausencia; un 404 desnudo es una ruta que no existe y
 *  NO se le deja pasar por ausencia. */
export function esAusenciaOrganizacion(status: number, cuerpo: unknown): boolean {
  return (
    status === 404 &&
    typeof cuerpo === "object" &&
    cuerpo !== null &&
    (cuerpo as Record<string, unknown>).code === "SUBJECT_NOT_FOUND"
  );
}

/** Colección de runtimes — fail-closed entero: UNA fila con forma ajena
 *  invalida TODA la respuesta (una colección a medias no es una colección más
 *  pequeña, es una tabla que miente por omisión). `[]` es un dato válido. */
export function validarObservacionesRuntimeNativas(x: unknown): ObservacionRuntimeNativa[] | null {
  if (!Array.isArray(x)) return null;
  const filas: ObservacionRuntimeNativa[] = [];
  for (const fila of x) {
    const v = validarObservacionRuntimeNativa(fila);
    if (!v) return null;
    filas.push(v);
  }
  return filas;
}

/** Error del plano nativo con su código tipado del gateway — el motivo del seam
 *  lo cita, y `esAusenciaOrganizacion` decide sobre `status`+`code`. */
export class ErrorNativo extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    mensaje: string,
  ) {
    super(mensaje);
  }
}

function codigoNativo(cuerpo: unknown): string {
  if (cuerpo && typeof cuerpo === "object" && typeof (cuerpo as Record<string, unknown>).code === "string")
    return (cuerpo as Record<string, unknown>).code as string;
  return "SIN_CODIGO";
}

async function abrirSesionNativa(credencial: string): Promise<SesionNativa> {
  const r = await fetch("/native/v1/sessions", {
    method: "POST",
    headers: { Authorization: `Bearer ${credencial}` },
  });
  if (!r.ok) {
    const cuerpo = await r.json().catch(() => null);
    const code = codigoNativo(cuerpo);
    throw new ErrorNativo(r.status, code, `POST /native/v1/sessions → HTTP ${r.status} (${code})`);
  }
  const sesion = validarSesionNativa(await r.json());
  if (!sesion)
    throw new ErrorNativo(0, "SESION_MALFORMADA", "POST /native/v1/sessions: forma de sesión no reconocida (fail-closed)");
  return sesion;
}

/** Sesión vigente PARA ESTA CREDENCIAL — el único punto que decide reutilizar
 *  o abrir. La apertura en vuelo se comparte entre los dos polls; el `finally`
 *  sólo limpia si la promesa en vuelo sigue siendo la SUYA (un epoch que sube
 *  mientras vuela no debe borrar la apertura nueva que lo sustituyó). Y la
 *  publicación en caché también está vigilada por el epoch [codex e101]:
 *  una apertura de A que resuelve TARDE no puede escribir el caché bajo B. */
async function sesionNativaVigente(credencial: string): Promise<SesionNativa> {
  const enCache = sesionEnMemoria;
  // `sesionUtil` devuelve bool — no es type guard — así que el no-null se
  // comprueba AQUÍ y el return usa la constante estrechada [astra-ca374eb].
  if (enCache !== null && sesionUtil(enCache, credencialDeSesion, credencial, Date.now() / 1000))
    return enCache;
  if (aperturaEnVuelo && aperturaDeVueloCredencial === credencial) return aperturaEnVuelo;
  aperturaDeVueloCredencial = credencial;
  const miEpoch = epochNativa;
  const promesa = abrirSesionNativa(credencial)
    .then((s) => {
      if (epochNativa === miEpoch) {
        sesionEnMemoria = s;
        credencialDeSesion = credencial;
      }
      return s;
    })
    .finally(() => {
      if (aperturaEnVuelo === promesa) aperturaEnVuelo = null;
    });
  aperturaEnVuelo = promesa;
  return promesa;
}

/** Lectura nativa con sesión. Invariantes de identidad [encargo codex
 *  #1350-next + carreras de codex e101]: ① la sesión se reutiliza SÓLO si fue
 *  abierta con la credencial actual (`sesionUtil`); ② el epoch se captura antes
 *  de cada await y se comprueba DESPUÉS DE CADA UNO — apertura, GET, cuerpo del
 *  error y JSON de datos: una respuesta que voló bajo otra identidad no se
 *  pinta, se reintenta una vez con la nueva; ③ el 401 comprueba el epoch ANTES
 *  de limpiar o reabrir — el 401 atrasado de A no puede cargarse la sesión de
 *  B; ④ caducar no es un fallo que enseñar. Jamás toca `CLAVE` ni recarga. */
async function pedirNativo<T>(ruta: string, validador: (x: unknown) => T | null, etiqueta: string): Promise<T> {
  for (let intento = 0; intento < 2; intento++) {
    const credencial = getCredencialNativa();
    if (!credencial)
      throw new ErrorNativo(0, "SIN_CREDENCIAL", "Sin credencial nativa: pégala en el panel — se guarda aparte del token del canal.");
    const miEpoch = epochNativa;
    let sesion = await sesionNativaVigente(credencial);
    if (epochNativa !== miEpoch) continue; // identidad cambió mientras abría — reintento con la nueva
    let r = await fetch(ruta, { headers: { Authorization: `Bearer ${sesion.token}` } });
    if (epochNativa !== miEpoch) continue; // respuesta atrasada de la identidad anterior
    if (r.status === 401) {
      if (epochNativa !== miEpoch) continue; // el 401 atrasado no limpia ni reabre bajo identidad vieja
      sesionEnMemoria = null;
      sesion = await sesionNativaVigente(credencial);
      if (epochNativa !== miEpoch) continue; // la reapertura también puede quedar atrasada
      r = await fetch(ruta, { headers: { Authorization: `Bearer ${sesion.token}` } });
      if (epochNativa !== miEpoch) continue;
    }
    if (!r.ok) {
      const cuerpo = await r.json().catch(() => null);
      if (epochNativa !== miEpoch) continue; // el cuerpo del error también vuela bajo la identidad vieja
      const code = codigoNativo(cuerpo);
      throw new ErrorNativo(r.status, code, `${etiqueta} → HTTP ${r.status} (${code})`);
    }
    const cuerpoDatos = await r.json();
    if (epochNativa !== miEpoch) continue; // JSON demorado de A no se pinta bajo B [codex e101]
    const validado = validador(cuerpoDatos);
    if (!validado) throw new ErrorNativo(0, "FORMA_INVALIDA", `${etiqueta}: forma no reconocida (fail-closed)`);
    return validado;
  }
  throw new ErrorNativo(0, "IDENTIDAD_CAMBIADA", `${etiqueta}: la identidad cambió dos veces seguidas — el próximo sondeo trae datos de la nueva`);
}

async function pedir<T>(ruta: string, params: Record<string, unknown> = {}): Promise<T> {
  const u = new URL(ruta, location.origin);
  for (const [k, v] of Object.entries(params)) {
    if (v !== "" && v != null && v !== false) u.searchParams.set(k, String(v));
  }
  const r = await fetch(u, { headers: { "X-Llminbox-Token": getToken() } });
  if (r.status === 401) { clearToken(); location.reload(); }
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.headers.get("content-type")?.includes("json") ? r.json() : (r.text() as T);
}

/** Como `pedir`, pero DEVUELVE TAMBIÉN LAS CABECERAS.
 *
 *  Existe porque el backend declara sus recortes en cabeceras —`x-filas-capadas` para la
 *  lista, `x-cuerpos-recortados` para los cuerpos, `x-cursor-tapiado` para un cursor por
 *  encima del último real— y este front no leía NINGUNA. El servicio se molestaba en
 *  distinguir «esto es todo lo que hay» de «esto es todo lo que te doy», y la distinción
 *  moría en el transporte. */
async function pedirConCabeceras<T>(
  ruta: string,
  params: Record<string, unknown> = {},
): Promise<{ datos: T; capadas: number | null }> {
  const u = new URL(ruta, location.origin);
  for (const [k, v] of Object.entries(params)) {
    if (v !== "" && v != null && v !== false) u.searchParams.set(k, String(v));
  }
  const r = await fetch(u, { headers: { "X-Llminbox-Token": getToken() } });
  if (r.status === 401) { clearToken(); location.reload(); }
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  const crudo = r.headers.get("x-filas-capadas");
  // `null` y no 0 cuando no viene: un 0 se compararía como un recorte de cero filas.
  const capadas = crudo == null ? null : Number(crudo);
  return { datos: (await r.json()) as T, capadas: Number.isFinite(capadas!) ? capadas : null };
}

export const api = {
  salud: () => pedir<Salud>("/health"),
  estado: () => pedir<Estado[]>("/stat"),
  entradasConCorte: (p: Record<string, unknown>) =>
    pedirConCabeceras<Entrada[]>("/entries", p),
  roster: () => pedir<Roster>("/roster"),
  /** Legado, `authority:false` — ver el comentario de `Organigrama`. La validación
   *  es fail-closed: una forma que no casa lanza en vez de servir campos a medias
   *  con pinta de dato bueno. */
  organigrama: async (): Promise<Organigrama> => {
    const crudo = await pedir<unknown>("/organigrama");
    const validado = validarOrganigrama(crudo);
    if (!validado) throw new Error("/organigrama: forma inesperada, tratada como inválida (fail-closed)");
    return validado;
  },
  /** Los dos endpoints nativos de `ADR-002-FLEET-CONTROL-PLANE.md` Decisión 5 —
   *  CONECTADOS [encargo astra runtime-root 2026-09-08: las cinco rutas YA sirven
   *  vía `runtime_root.build_app`, 155/155 en 5eefaa4; contrato
   *  MARK:astra-gateway-e1f0e2a-review-20260908]. Sesión nativa con credencial
   *  PROPIA (ver `CLAVE_CREDENCIAL_NATIVA`), validación fail-closed al borde y
   *  errores capturados en el seam — `queryFn` nunca lanza, así el panel decide
   *  entre disponible/ausente/no-disponible con el motivo en la mano.
   *
   *  El stub anterior (`NATIVO_NO_INTEGRADO`, "0 rutas servidas") quedó FALSO el
   *  día que codex sirvió el router: una premisa muerta en el código es la que
   *  cuesta más caro — nadie la re-mide porque "ya estaba así". */
  organizacionNativa: async (): Promise<SeamNativo<OrganizacionNativa>> => {
    try {
      return {
        disponible: true,
        datos: await pedirNativo("/native/v1/organization", validarOrganizacionNativa, "GET /native/v1/organization"),
      };
    } catch (e) {
      const motivo = e instanceof Error ? e.message : "error desconocido";
      if (e instanceof ErrorNativo && esAusenciaOrganizacion(e.status, { code: e.code }))
        return { disponible: false, motivo, ausente: true }; // D12 — ausencia tipada
      return { disponible: false, motivo };
    }
  },
  runtimesNativos: async (): Promise<SeamNativo<ObservacionRuntimeNativa[]>> => {
    try {
      return {
        disponible: true,
        datos: await pedirNativo("/native/v1/runtimes", validarObservacionesRuntimeNativas, "GET /native/v1/runtimes"),
      };
    } catch (e) {
      // Colección: aquí NO hay `ausente` — un 200 con [] es un dato (cero
      // runtimes), y cualquier otro código es "no disponible" a secas.
      return { disponible: false, motivo: e instanceof Error ? e.message : "error desconocido" };
    }
  },
  cursor: (agente: string) => pedir<Cursores>(`/cursor/${encodeURIComponent(agente)}`),
  entradas: (p: { ledger?: string; to?: string; actor?: string; tipo?: string;
                  q?: string; limit?: number; cuerpo?: boolean }) =>
    pedir<Entrada[]>("/entries", { ...p, cuerpo: p.cuerpo ?? true }),
  /** OJO: es de LECTURA. No avanza el cursor — eso es un POST aparte, a propósito:
   *  un GET que muta se lo dispara cualquiera, incluido un <img src> en otra página. */
  bandeja: (agente: string, limit = 30) =>
    pedir<string>(`/inbox/${encodeURIComponent(agente)}`, { limit }),
  /** Sólo canjea el grant servido por `/inbox`; la UI no bordea el camino causal
   *  llamando al endpoint legacy con un `hasta` fabricado por el navegador. */
  confirmarAck: async (agente: string, ack: AckGrant, arrival: number) => {
    const r = await fetch(`/inbox/${encodeURIComponent(agente)}/ack`, {
      method: "POST",
      headers: { "X-Llminbox-Token": getToken(),
                 "X-Llminbox-Carril": ack.lane,
                 "Content-Type": "application/json" },
      body: JSON.stringify({ grant: ack, arrival }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  },
};
