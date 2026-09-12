// `validarOrganigrama` — la validación fail-closed del borde legado.
//   node web/src/lib/api.test.mjs
//
// Igual que `flota.test.mjs`: se transpila con el `typescript` ya instalado en
// vez de pelar tipos a mano, porque `api.ts` tiene genéricos (`Record<string,
// EntradaJerarquia>`) y una regex los persigue peor de lo que los parsea `tsc`.
// Importar el módulo entero NO ejecuta red ni DOM: `fetch`/`localStorage` sólo
// se llaman DENTRO de funciones, nunca en la carga del módulo.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import ts from "typescript";

const aqui = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(aqui, "api.ts"), "utf8");
const { outputText, diagnostics } = ts.transpileModule(src, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  reportDiagnostics: true,
});
if (diagnostics && diagnostics.length)
  throw new Error(`api.ts no transpila limpio: ${diagnostics.map((d) => d.messageText).join("; ")}`);

const {
  validarOrganigrama, validarObservacionRuntimeNativa, validarOrganizacionNativa,
  validarSesionNativa, sesionExpirada, esAusenciaOrganizacion,
  validarObservacionesRuntimeNativas, sesionUtil,
  cambiarCredencialNativa, borrarCredencialNativa, api,
  revisionIdentidadNativa, suscribirseIdentidadNativa,
} = await import("data:text/javascript," + encodeURIComponent(outputText));

// Fuente del componente, para el falsador de cableado de la credencial.
const srcCredencial = readFileSync(join(aqui, "..", "components", "CredencialNativa.tsx"), "utf8");
const srcTablaRT = readFileSync(join(aqui, "..", "components", "TablaRuntimesNativos.tsx"), "utf8");

let malos = 0;
const caso = (nombre, real, esperado) => {
  const realStr = JSON.stringify(real);
  const esperadoStr = JSON.stringify(esperado);
  if (realStr !== esperadoStr) {
    malos++;
    console.log(`  ✗ ${nombre}\n      dio:      ${realStr}\n      esperado: ${esperadoStr}`);
  } else console.log(`  ✓ ${nombre}`);
};

const VALIDO = {
  revision: 3, source_sha256: "abc", loaded_sha256: "abc", cargado_en: "2026-09-07T00:00:00",
  stale: false, jerarquia: { cto: { reporta_a: "OPERADOR", capa: "c-suite" } }, roles: 1, aviso: null,
};

caso("una forma válida se acepta tal cual", validarOrganigrama(VALIDO), VALIDO);
caso("revision null se acepta (legado sin _revision)", validarOrganigrama({ ...VALIDO, revision: null }).revision, null);
caso("revision string se acepta (diagnóstico crudo, no versión validada)", validarOrganigrama({ ...VALIDO, revision: "rev-9" }).revision, "rev-9");

// ── fail-closed: CUALQUIER campo con forma ajena invalida TODA la respuesta ──
caso("null no es un organigrama", validarOrganigrama(null), null);
caso("un array no es un organigrama", validarOrganigrama([]), null);
caso("un string no es un organigrama", validarOrganigrama("no"), null);
caso("stale no-booleano invalida todo, no sólo ese campo", validarOrganigrama({ ...VALIDO, stale: "sí" }), null);
caso("roles no-numérico invalida todo", validarOrganigrama({ ...VALIDO, roles: "1" }), null);
caso("revision con forma de objeto se rechaza (ni string ni number ni null)",
  validarOrganigrama({ ...VALIDO, revision: { n: 1 } }), null);
caso("jerarquia como array (JSON válido, forma ajena) se rechaza", validarOrganigrama({ ...VALIDO, jerarquia: [] }), null);
caso("jerarquia null se rechaza (el contrato exige objeto, aunque vacío)",
  validarOrganigrama({ ...VALIDO, jerarquia: null }), null);
caso("una fila de jerarquia que no es objeto invalida todo",
  validarOrganigrama({ ...VALIDO, jerarquia: { cto: "no-un-objeto" } }), null);
caso("source_sha256 numérico se rechaza", validarOrganigrama({ ...VALIDO, source_sha256: 123 }), null);
caso("aviso no-string-ni-null se rechaza", validarOrganigrama({ ...VALIDO, aviso: 1 }), null);

// ── validarObservacionRuntimeNativa — sin llamante hoy, pero probada YA ──────
const OBS_VALIDA = {
  workload_id: "w1", runtime_instance: "r1", lane: "llminbox", principal: "p1", role: "be",
  credential_generation: 2, status: "fresh", detector_state: null, status_seq: 3,
  status_since: "t", last_observed_at: "t", cause_id: null, transition_id: null,
  receipt_id: "rec1", organization_revision: 7,
};
caso("observación nativa válida se acepta", validarObservacionRuntimeNativa(OBS_VALIDA), OBS_VALIDA);
caso("status fuera del vocabulario cerrado se rechaza",
  validarObservacionRuntimeNativa({ ...OBS_VALIDA, status: "zombie" }), null);
caso("status_seq no-entero se rechaza (bool no cuela como int)",
  validarObservacionRuntimeNativa({ ...OBS_VALIDA, status_seq: true }), null);
caso("runtime_instance vacío se rechaza", validarObservacionRuntimeNativa({ ...OBS_VALIDA, runtime_instance: "" }), null);
// D2 [RULING cto #1252, comentario CORREGIDO tras #1348]: el CHECK real
// (`coordination.py:1838`) es UNIDIRECCIONAL — un absent NO obliga a
// runtime_instance null (puede llevar vínculo real nunca observado). La fila
// absent VACANTE (null) y la absent VINCULADA son las DOS formas legítimas.
// F3 [qa #1312 nota 2 / design #1328]: la fixture anterior viajaba
// status:"fresh"+null — forma que el servidor PROHÍBE. Verde honesto de otra
// proposición; la cura es 1 línea: status:"absent".
const OBS_ABSENT = { ...OBS_VALIDA, status: "absent", runtime_instance: null, credential_generation: null };
caso("fila absent VACANTE (runtime_instance null) se acepta, con su receipt/transition (D2+F3)",
  validarObservacionRuntimeNativa(OBS_ABSENT), OBS_ABSENT);
caso("fila absent VINCULADA (runtime_instance real, nunca observada) TAMBIÉN se acepta — el CHECK no obliga a null (cto #1348)",
  validarObservacionRuntimeNativa({ ...OBS_ABSENT, runtime_instance: "be-9", principal: "p9" })?.status, "absent");
caso("credential_generation numérico se acepta (generación puede ser entero)",
  validarObservacionRuntimeNativa({ ...OBS_VALIDA, credential_generation: 5 }).credential_generation, 5);
caso("credential_generation con forma de objeto se rechaza",
  validarObservacionRuntimeNativa({ ...OBS_VALIDA, credential_generation: {} }), null);
caso("null no es una observación", validarObservacionRuntimeNativa(null), null);

// ── validarOrganizacionNativa — forma adjudicada por RULING cto #1252 ───────
// D3/D8: `authority:true` EXIGIDA en el borde + `revision` anidada como OBJETO
// (lane/revision/source_digest/freshness/activated_at) — no número suelto.
// D4: vocabulario ES de freshness se conserva (pendiente sólo el nombre exacto
// freshness vs freshness_state, que cierran backend+frontend).
// D5: `layer` INT en el wire (0-255, como la CHECK de coordination.py).
// D6: `escalation_routes` ESTRUCTURADAS (trigger_code+target_role) — "typed
// escalation routes" (ADR-002 Dec 6); aplanar a strings perdería trigger_code.
const ORG_NATIVA_VALIDA = {
  authority: true,
  revision: {
    lane: "llminbox", revision: 4, source_digest: "sha256:abc",
    freshness: "atestiguada", activated_at: "t",
  },
  roles: [{ role: "cto", reports_to: null, reviewers: [], escalation_routes: [], layer: 2 }],
  // D7 [contrato codex #1343]: `workloads` SIEMPRE presente en el wire, aunque vacía.
  workloads: [{ workload_id: "cto-w1", role: "cto" }],
};
caso("organización nativa válida se acepta (forma RULING #1252 + workloads #1343)", validarOrganizacionNativa(ORG_NATIVA_VALIDA), ORG_NATIVA_VALIDA);
caso("authority:false se rechaza (D8: sin el positivo, nativo y legado son indistinguibles)",
  validarOrganizacionNativa({ ...ORG_NATIVA_VALIDA, authority: false }), null);
caso("authority ausente se rechaza igual que false",
  validarOrganizacionNativa((({ authority, ...resto }) => resto)(ORG_NATIVA_VALIDA)), null);
caso("revision no-objeto se rechaza (D8: objeto, no número ni null)",
  validarOrganizacionNativa({ ...ORG_NATIVA_VALIDA, revision: 4 }), null);
caso("freshness fuera del vocabulario cerrado se rechaza (D4)",
  validarOrganizacionNativa({
    ...ORG_NATIVA_VALIDA,
    revision: { ...ORG_NATIVA_VALIDA.revision, freshness: "quizas" },
  }), null);
caso("revision.revision no-entero se rechaza",
  validarOrganizacionNativa({
    ...ORG_NATIVA_VALIDA,
    revision: { ...ORG_NATIVA_VALIDA.revision, revision: 1.5 },
  }), null);
caso("roles que no es array se rechaza", validarOrganizacionNativa({ ...ORG_NATIVA_VALIDA, roles: {} }), null);
caso("una fila de roles sin `role` invalida TODA la organización",
  validarOrganizacionNativa({ ...ORG_NATIVA_VALIDA, roles: [{ reports_to: null, reviewers: [], escalation_routes: [], layer: null }] }),
  null);
caso("reviewers con un elemento no-string invalida todo",
  validarOrganizacionNativa({ ...ORG_NATIVA_VALIDA,
    roles: [{ role: "cto", reports_to: null, reviewers: [1], escalation_routes: [], layer: null }] }), null);
caso("layer string se rechaza (D5: int en el wire, la etiqueta la pinta el cliente)",
  validarOrganizacionNativa({ ...ORG_NATIVA_VALIDA,
    roles: [{ role: "cto", reports_to: null, reviewers: [], escalation_routes: [], layer: "c-suite" }] }), null);
caso("layer no-entero se rechaza",
  validarOrganizacionNativa({ ...ORG_NATIVA_VALIDA,
    roles: [{ role: "cto", reports_to: null, reviewers: [], escalation_routes: [], layer: 1.5 }] }), null);
caso("escalation_routes estructurada VÁLIDA se acepta (D6)",
  validarOrganizacionNativa({
    ...ORG_NATIVA_VALIDA,
    roles: [{ role: "be", reports_to: "cto", reviewers: [],
      escalation_routes: [{ trigger_code: "BLOCKED", target_role: "cto" }], layer: 3 }],
  }).roles[0].escalation_routes.length, 1);
caso("trigger_code fuera del vocabulario cerrado se rechaza (CHECK de coordination.py: 4 valores)",
  validarOrganizacionNativa({ ...ORG_NATIVA_VALIDA,
    roles: [{ role: "be", reports_to: "cto", reviewers: [],
      escalation_routes: [{ trigger_code: "MAS_O_MENOS", target_role: "cto" }], layer: 3 }] }), null);
caso("escalation_route con target_role vacío se rechaza",
  validarOrganizacionNativa({ ...ORG_NATIVA_VALIDA,
    roles: [{ role: "be", reports_to: "cto", reviewers: [],
      escalation_routes: [{ trigger_code: "INCIDENT", target_role: "" }], layer: 3 }] }), null);
caso("escalation_route como string plano se rechaza (D6: el string[] perdía trigger_code)",
  validarOrganizacionNativa({ ...ORG_NATIVA_VALIDA,
    roles: [{ role: "be", reports_to: "cto", reviewers: [], escalation_routes: ["cto"], layer: 3 }] }), null);

// ── D7 — workload_id en la observación y workloads[] en la organización ─────
// Ausente del wire se TOLERA mientras Dec 4 no lo congele; presente, sólo
// string no vacío o null. En la organización, si `workloads` viaja, cada carga
// nombra workload_id y role — una carga a medias apuntaría el absent al
// workload equivocado.
const OBS_COMPLETA = {
  runtime_instance: "be-1", workload_id: "be-w1", lane: "llminbox", principal: "p1", role: "be",
  credential_generation: 2, status: "fresh", detector_state: null, status_seq: 1,
  status_since: "t", last_observed_at: "t", cause_id: null, transition_id: "tr",
  receipt_id: "rec", organization_revision: 4,
};
caso("observación CON workload_id válido se acepta (D7)",
  validarObservacionRuntimeNativa(OBS_COMPLETA)?.workload_id, "be-w1");
caso("observación con workload_id STRING VACÍO se rechaza",
  validarObservacionRuntimeNativa({ ...OBS_COMPLETA, workload_id: "" }), null);
caso("observación con workload_id NO-string (5) se rechaza",
  validarObservacionRuntimeNativa({ ...OBS_COMPLETA, workload_id: 5 }), null);
caso("observación SIN workload_id se RECHAZA — obligatorio en las lecturas [contrato codex #1343]",
  validarObservacionRuntimeNativa((({ workload_id, ...resto }) => resto)(OBS_COMPLETA)), null);

caso("organización con workloads[] VÁLIDOS se acepta (D7)",
  validarOrganizacionNativa({ ...ORG_NATIVA_VALIDA,
    workloads: [{ workload_id: "be-w1", role: "be" }, { workload_id: "be-w2", role: "be" }] })
    ?.workloads.length, 2);
caso("organización SIN workloads se RECHAZA — SIEMPRE presente aunque vacía [contrato codex #1343]",
  validarOrganizacionNativa((({ workloads, ...resto }) => resto)(ORG_NATIVA_VALIDA)), null);
caso("organización con workloads VACÍA se acepta (roles sin workloads esperados ⇒ ningún absent)",
  validarOrganizacionNativa({ ...ORG_NATIVA_VALIDA, workloads: [] })?.workloads, []);
caso("organización con workloads NO-array se rechaza",
  validarOrganizacionNativa({ ...ORG_NATIVA_VALIDA, workloads: "be-w1,be-w2" }), null);
caso("organización con carga SIN workload_id se rechaza",
  validarOrganizacionNativa({ ...ORG_NATIVA_VALIDA, workloads: [{ role: "be" }] }), null);
caso("organización con carga con workload_id VACÍO se rechaza",
  validarOrganizacionNativa({ ...ORG_NATIVA_VALIDA, workloads: [{ workload_id: "", role: "be" }] }), null);
caso("organización con carga SIN role se rechaza",
  validarOrganizacionNativa({ ...ORG_NATIVA_VALIDA, workloads: [{ workload_id: "be-w1" }] }), null);

// ── CONEXIÓN NATIVA [encargo astra runtime-root, 2026-09-08] — sesión nativa
// SEPARADA del token del canal, y ausencia tipada de organización (D12) ──────
// Wire real: POST /native/v1/sessions → 201 {token, runtime_instance, principal,
// role, lane, expires_at: float, generation, principal_source, capabilities}
// [native_gateway.py _session_wire / coordination.py IssuedSession].

const SESION_VALIDA = {
  token: "st-1", runtime_instance: "be-1", principal: "p1", role: "be",
  lane: "llminbox", expires_at: 9999999999, generation: 1,
  principal_source: "config", capabilities: ["runtime.read", "organization.read"],
};
caso("sesión nativa válida se acepta", validarSesionNativa(SESION_VALIDA), SESION_VALIDA);
caso("sesión SIN token se rechaza", validarSesionNativa((({ token, ...r }) => r)(SESION_VALIDA)), null);
caso("sesión con token VACÍO se rechaza", validarSesionNativa({ ...SESION_VALIDA, token: "" }), null);
caso("sesión con expires_at no-numérico se rechaza (en la BD es REAL)",
  validarSesionNativa({ ...SESION_VALIDA, expires_at: "mañana" }), null);
caso("sesión con capabilities NO-array se rechaza",
  validarSesionNativa({ ...SESION_VALIDA, capabilities: "runtime.read" }), null);
caso("sesión con capability NO-string se rechaza",
  validarSesionNativa({ ...SESION_VALIDA, capabilities: [7] }), null);

caso("sesión vigente NO está expirada", sesionExpirada(SESION_VALIDA, 1000), false);
caso("sesión vencida SÍ está expirada", sesionExpirada(SESION_VALIDA, 10000000000), true);
caso("expiración en el MISMO instante cuenta como vencida (≤)", sesionExpirada(SESION_VALIDA, 9999999999), true);
caso("expires_at corrompido se trata como VENCIDA (fail-closed: reabrir, no confiar)",
  sesionExpirada({ ...SESION_VALIDA, expires_at: undefined }, 0), true);

// D12 — el 404 tipado del gateway distingue «no hay revisión activa» de «ruta
// que no existe» [contrato codex #1343, _ERRORS: (C.SubjectNotFound,
// "SUBJECT_NOT_FOUND", 404)].
caso("404 SUBJECT_NOT_FOUND ES ausencia tipada de organización",
  esAusenciaOrganizacion(404, { code: "SUBJECT_NOT_FOUND", message: "sujeto no encontrado" }), true);
caso("404 SIN code NO es ausencia tipada", esAusenciaOrganizacion(404, {}), false);
caso("404 con cuerpo NO-objeto NO es ausencia tipada", esAusenciaOrganizacion(404, null), false);
caso("403 POLICY_DENIED NO es ausencia", esAusenciaOrganizacion(403, { code: "POLICY_DENIED" }), false);
caso("200 NO es ausencia", esAusenciaOrganizacion(200, null), false);

caso("colección de runtimes válida se acepta",
  validarObservacionesRuntimeNativas([OBS_COMPLETA, OBS_ABSENT])?.length, 2);
caso("UNA fila mala invalida TODA la colección (fail-closed, no filas a medias)",
  validarObservacionesRuntimeNativas([OBS_COMPLETA, { status: "zombie" }]), null);
caso("un objeto suelto NO es una colección", validarObservacionesRuntimeNativas(OBS_COMPLETA), null);
caso("[] es una colección VÁLIDA (cero runtimes es un dato)",
  validarObservacionesRuntimeNativas([]), []);

// Cableado por FUENTE (mismo falsador que usa sondeo.test.mjs): las funciones
// nativas hacen la petición REAL — nada de stubs — y la sesión viaja en
// Authorization: Bearer, NUNCA en la cabecera del canal.
caso("cableado: se abre sesión nativa con POST /native/v1/sessions",
  /POST["']?,\s*\n?\s*["']\/native\/v1\/sessions["']|method:\s*"POST".*sessions|fetch\("\/native\/v1\/sessions",\s*\{\s*method:\s*"POST"/.test(src) || (src.includes('"/native/v1/sessions"') && src.includes('method: "POST"')), true);
caso("cableado: la sesión nativa viaja como Authorization: Bearer",
  src.includes("Authorization") && src.includes("Bearer"), true);
caso("cableado: pedirNativo NUNCA manda X-Llminbox-Token (separación de credenciales)",
  (() => {
    // Acotado a pedirNativo ONLY — el corte a "export const api" arrastraba a
    // pedir/pedirConCabeceras, que SÍ deben usar la cabecera del canal.
    const desde = src.indexOf("async function pedirNativo");
    const bloque = src.slice(desde, src.indexOf("async function pedir<", desde));
    return !bloque.includes("X-Llminbox-Token");
  })(), true);
caso("cableado: organizacionNativa ya NO es el stub «no existen todavía SERVIDOS»",
  !src.includes("no existen todavía SERVIDOS"), true);
caso("cableado: organizacionNativa mapea la ausencia tipada a ausente:true",
  src.includes("ausente: true"), true);
caso("cableado: GET /native/v1/organization y /native/v1/runtimes se piden de verdad",
  src.includes('"/native/v1/organization"') && src.includes('"/native/v1/runtimes"'), true);

// ── CAMBIO DE IDENTIDAD [encargo codex #1350-next, bug de 955192c] — el
// caché de sesión debe estar VINCULADO a la credencial con la que se abrió:
// sesión de A + credencial actual B ⇒ NO sirve, aunque no esté vencida. ─────
caso("sesión abierta con A y credencial actual B NO sirve (el bug de 955192c)",
  sesionUtil(SESION_VALIDA, "cred-A", "cred-B", 1000), false);
caso("sesión abierta con A y credencial A vigente SÍ sirve",
  sesionUtil(SESION_VALIDA, "cred-A", "cred-A", 1000), true);
caso("sesión de A con credencial A pero VENCIDA no sirve",
  sesionUtil(SESION_VALIDA, "cred-A", "cred-A", 10000000000), false);
caso("sin sesión no hay nada que sirva", sesionUtil(null, "cred-A", "cred-A", 1000), false);

// Cableado del ciclo de vida de la credencial — por FUENTE:
const bloqueCambiar = src.slice(src.indexOf("export function cambiarCredencialNativa"), src.indexOf("export function borrarCredencialNativa"));
const bloqueBorrar = src.slice(src.indexOf("export function borrarCredencialNativa"), src.indexOf("export function sesionUtil"));
caso("cableado: cambiarCredencialNativa suelta la sesión en memoria",
  bloqueCambiar.includes("sesionEnMemoria = null"), true);
caso("cableado: borrarCredencialNativa suelta la sesión en memoria",
  bloqueBorrar.includes("sesionEnMemoria = null"), true);
caso("cableado: borrarCredencialNativa borra SU clave, jamás la del canal",
  bloqueBorrar.includes("CLAVE_CREDENCIAL_NATIVA") && !bloqueBorrar.includes('localStorage.removeItem(CLAVE)'), true);
caso("cableado: pedirNativo descarta la respuesta si la identidad cambió en vuelo",
  src.slice(src.indexOf("async function pedirNativo"), src.indexOf("export const api")).includes("IDENTIDAD_CAMBIADA"), true);
caso("cableado: la apertura de sesión concurrente se COMPARTE (una sola en vuelo por credencial)",
  src.includes("aperturaEnVuelo") && src.includes("aperturaDeVueloCredencial === credencial"), true);
caso("cableado: CredencialNativa usa cambiarCredencialNativa, no el set crudo",
  srcCredencial.includes("cambiarCredencialNativa") && !srcCredencial.includes("setCredencialNativa("), true);
caso("cableado: CredencialNativa permite BORRAR la credencial",
  srcCredencial.includes("borrarCredencialNativa"), true);
// RULING cpo #2641 (10-sep): «La pantalla que pide la credencial dice dónde se va a
// guardar... debe decir que queda en el almacenamiento del navegador y persiste entre
// sesiones.» Falsador del ruling: un lector externo que SÓLO mira la pantalla sabe que
// persiste — antes de la cura, la letra no lo decía en ningún momento. Las frases
// asertadas viajan íntegras en UNA línea del fuente (includes no normaliza saltos).
caso("ruling: la pantalla DECLARA dónde queda la credencial (almacenamiento del navegador)",
  srcCredencial.includes("queda guardada en el almacenamiento de este navegador"), true);
caso("ruling: la pantalla declara que PERSISTE al cerrar, hasta que la quites con «Borrar»",
  srcCredencial.includes("sigue ahí aunque cierres el navegador, hasta que la quites con el botón"), true);
caso("ruling: la pantalla distingue la credencial persistente de la sesión (corta, en memoria)",
  srcCredencial.includes("es corta y vive sólo en memoria"), true);
caso("cableado: el formulario vive UNA vez, en contenedor permanente — alcanzable en carga, error, sin-org, vacio y con-datos [codex followup 17:30]",
  (srcTablaRT.match(/<CredencialNativa \/>/g) ?? []).length === 1
    && srcTablaRT.indexOf("<CredencialNativa />") > srcTablaRT.lastIndexOf("estadoPanel ==="), true);
// F5 [design #1441]: sin credencial no hubo red que fallara — la rama de error
// distingue SIN_CREDENCIAL ANTES de pintar la banda roja de fallo.
const bloqueErrorRT = srcTablaRT.slice(
  srcTablaRT.indexOf("organizacion.isError || runtimes.isError"),
  srcTablaRT.indexOf("const org = organizacion.data"),
);
caso("cableado: SIN_CREDENCIAL lleva banner punteado propio y ANTES de la banda roja de fallo de red",
  bloqueErrorRT.includes("SIN_CREDENCIAL")
    && bloqueErrorRT.indexOf("SIN_CREDENCIAL") < bloqueErrorRT.indexOf('role="alert"'), true);

// ── FETCH REAL SIMULADO [codex process-review 2026-09-08: «pruebas del fetch
// real simulado, no solo validadores»] — `pedirNativo` conducido de VERDAD con
// un `fetch` sustituido: A→B por la puerta real, respuesta tardía descartada,
// token legacy intacto y el borrado sin tocar la red. ────────────────────────
if (!globalThis.localStorage) {
  const bolsa = new Map();
  globalThis.localStorage = {
    getItem: (k) => (bolsa.has(k) ? bolsa.get(k) : null),
    setItem: (k, v) => bolsa.set(k, String(v)),
    removeItem: (k) => bolsa.delete(k),
  };
}
const rJSON = (status, cuerpo) => ({ ok: status >= 200 && status < 300, status, json: async () => cuerpo });
// Revisión de identidad PUBLICADA + SUSCRIPCIÓN [codex #1445: removeQueries
// destruye en silencio y no vacía el resultado de un observador activo — la
// capa de vista necesita SABER que la identidad cambió para cambiar las
// queryKeys]. Entero no secreto, jamás la credencial.
{
  const vistos = [];
  const quitar = suscribirseIdentidadNativa(() => vistos.push(revisionIdentidadNativa()));
  const antes = revisionIdentidadNativa();
  cambiarCredencialNativa("cred-A");
  borrarCredencialNativa();
  quitar(); // desuscrito — lo que venga ya no notifica
  cambiarCredencialNativa("cred-B");
  caso("la revisión de identidad sube con cambiar y borrar, y NOTIFICA a los suscritos (sin credencial en el valor)",
    vistos.length === 2 && revisionIdentidadNativa() === antes + 3
      && Number.isInteger(revisionIdentidadNativa()), true);
}
const SESION = (token) => rJSON(201, { ...SESION_VALIDA, token });
const ORG_OK = rJSON(200, ORG_NATIVA_VALIDA);
// Cuerpos DISTINGUIBLES por revisión [astra-ca374eb: "A y B deben devolver
// datos distintos y verificar que sólo B llega al resultado"].
const CUERPO_ORG_DE = (marca) =>
  ({ ...ORG_NATIVA_VALIDA, revision: { ...ORG_NATIVA_VALIDA.revision, source_digest: marca } });
const ORG_DE = (marca) => rJSON(200, CUERPO_ORG_DE(marca));

{
  const pedidos = [];
  const fetchReal = globalThis.fetch;
  globalThis.fetch = async (url, opts = {}) => {
    const cabeceras = opts.headers ?? {};
    pedidos.push({ url, auth: cabeceras.Authorization, canal: "X-Llminbox-Token" in cabeceras });
    if (url.endsWith("/sessions"))
      return SESION(pedidos.filter((p) => p.url.endsWith("/sessions")).length === 1 ? "S-A" : "S-B");
    return ORG_OK;
  };
  cambiarCredencialNativa("cred-A");
  const r1 = await api.organizacionNativa();
  cambiarCredencialNativa("cred-B");
  const r2 = await api.organizacionNativa();
  globalThis.fetch = fetchReal;
  const aperturas = pedidos.filter((p) => p.url.endsWith("/sessions"));
  caso("fetch: la 1ª lectura abre sesión y sirve datos", r1.disponible && r1.datos.authority === true, true);
  caso("fetch A→B: cambiar la credencial abre sesión NUEVA, no reutiliza la de A", aperturas.length, 2);
  caso("fetch A→B: la 2ª apertura viaja con la credencial B", aperturas[1]?.auth, "Bearer cred-B");
  caso("fetch A→B: la 2ª lectura sirve datos de verdad", r2.disponible && r2.datos.authority === true, true);
  caso("fetch: token legacy intacto — ninguna petición nativa lleva X-Llminbox-Token y todas son Bearer",
    pedidos.every((p) => !p.canal && typeof p.auth === "string" && p.auth.startsWith("Bearer ")), true);
}

{
  const pedidos = [];
  const fetchReal = globalThis.fetch;
  let soltar;
  const tardia = new Promise((res) => { soltar = () => res(SESION("tardia-A")); });
  let primera = true;
  globalThis.fetch = async (url, opts = {}) => {
    const cabeceras = opts.headers ?? {};
    pedidos.push({ url, auth: cabeceras.Authorization });
    if (url.endsWith("/sessions")) {
      if (primera) { primera = false; return tardia; }
      return SESION("nuevo-B");
    }
    return ORG_OK;
  };
  cambiarCredencialNativa("cred-A");
  const enVuelo = api.organizacionNativa(); // NO await: la apertura de A queda colgada
  cambiarCredencialNativa("cred-B"); // la identidad cambia MIENTRAS la petición vuela
  soltar(); // la respuesta tardía de A llega AHORA
  const r = await enVuelo;
  globalThis.fetch = fetchReal;
  caso("fetch tardía: descarta la sesión tardía de A, reintenta y sirve con B",
    r.disponible && r.datos.authority === true
      && pedidos.some((p) => p.url.endsWith("/organization") && p.auth === "Bearer nuevo-B"), true);
  caso("fetch tardía: la sesión tardía de A jamás sirvió una lectura",
    pedidos.some((p) => p.auth === "Bearer tardia-A"), false);
}

{
  cambiarCredencialNativa("cred-A");
  const fetchReal = globalThis.fetch;
  globalThis.fetch = async () => { throw new Error("NO DEBE HABER RED sin credencial"); };
  borrarCredencialNativa();
  const r = await api.organizacionNativa();
  globalThis.fetch = fetchReal;
  caso("fetch borrar: sin credencial el seam cae a no disponible SIN llamar a la red",
    r.disponible === false && globalThis.localStorage.getItem("llminbox.nativo.credencial") === null, true);
}

{
  // POST concurrente único: los dos polls despiertan sin sesión y UNA sola
  // apertura vuela [codex e101: "cambio durante apertura/GET/JSON, 401
  // atrasado, borrar y token legacy intacto"].
  borrarCredencialNativa();
  cambiarCredencialNativa("cred-A");
  const pedidos = [];
  const fetchReal = globalThis.fetch;
  globalThis.fetch = async (url, opts = {}) => {
    pedidos.push({ url, auth: opts.headers?.Authorization });
    if (url.endsWith("/sessions")) return SESION("S-unica");
    return url.endsWith("/runtimes") ? rJSON(200, [OBS_COMPLETA]) : ORG_OK;
  };
  const [rOrg, rRts] = await Promise.all([api.organizacionNativa(), api.runtimesNativos()]);
  globalThis.fetch = fetchReal;
  caso("fetch concurrente: los dos polls comparten UNA sola apertura de sesión",
    pedidos.filter((p) => p.url.endsWith("/sessions")).length, 1);
  caso("fetch concurrente: ambos polls sirven con la misma sesión",
    rOrg.disponible && rRts.disponible
      && pedidos.filter((p) => !p.url.endsWith("/sessions")).every((p) => p.auth === "Bearer S-unica"), true);
}

{
  // Cambio de identidad MIENTRAS el GET vuela — CON BARRERA [astra-ca374eb]:
  // la identidad cambia SÓLO cuando el GET de A ya está dentro de la red
  // (`await getLlego`), y A/B traen cuerpos DISTINTOS para probar cuál se
  // sirvió. La respuesta retenida de A no puede pintarse.
  cambiarCredencialNativa("cred-A");
  const pedidos = [];
  const fetchReal = globalThis.fetch;
  let fase = "calentar";
  let avisarGet, abrirGet;
  const getLlego = new Promise((res) => { avisarGet = res; });
  const puertaGet = new Promise((res) => { abrirGet = res; });
  globalThis.fetch = async (url, opts = {}) => {
    pedidos.push({ url, auth: opts.headers?.Authorization });
    if (url.endsWith("/sessions"))
      return SESION(fase === "calentar" ? "S-DE-A" : "S-DE-B");
    if (fase === "calentar") return ORG_DE("ORG-DE-A");
    if (fase === "reten") { fase = "suelto"; avisarGet(); return puertaGet; } // EL GET de A, retenido en la red
    return ORG_DE("ORG-DE-B");
  };
  await api.organizacionNativa(); // calienta: sesión S-DE-A cacheada con cred-A
  fase = "reten";
  const enVuelo = api.organizacionNativa();
  await getLlego; // BARRERA: el GET de A está DENTRO de la red
  cambiarCredencialNativa("cred-B"); // la identidad cambia CON el GET en vuelo
  abrirGet(ORG_DE("ORG-DE-A")); // la respuesta retenida de A llega AHORA — debe descartarse
  const r = await enVuelo;
  globalThis.fetch = fetchReal;
  caso("fetch GET tardío: la respuesta retenida de A se descarta — se sirve el cuerpo DISTINTO de B",
    r.disponible && r.datos.revision.source_digest === "ORG-DE-B", true);
  caso("fetch GET tardío: el reintento leyó con la sesión reabierta de B",
    pedidos.some((p) => p.url.endsWith("/organization") && p.auth === "Bearer S-DE-B"), true);
}

{
  // Cambio de identidad MIENTRAS el CUERPO se demora dentro de json() —
  // [codex e101: check DESPUÉS de await r.json()] — con barrera: la identidad
  // cambia cuando el cuerpo de A ya está dentro de json(), y A/B son cuerpos
  // distinguibles.
  cambiarCredencialNativa("cred-A");
  const pedidos = [];
  const fetchReal = globalThis.fetch;
  let fase = "calentar";
  let avisarJson, abrirJson;
  const jsonLlego = new Promise((res) => { avisarJson = res; });
  const puertaJson = new Promise((res) => { abrirJson = res; });
  globalThis.fetch = async (url, opts = {}) => {
    pedidos.push({ url, auth: opts.headers?.Authorization });
    if (url.endsWith("/sessions"))
      return SESION(fase === "calentar" ? "S-DE-A" : "S-DE-B");
    if (fase === "calentar") return ORG_DE("ORG-DE-A");
    if (fase === "reten") {
      fase = "suelto"; // la barrera se consume UNA vez; lo que venga después ya se sirve
      return { ok: true, status: 200, json: async () => { avisarJson(); return puertaJson; } }; // la red llegó; el CUERPO se demora
    }
    return ORG_DE("ORG-DE-B");
  };
  await api.organizacionNativa(); // calienta
  fase = "reten";
  const enVuelo = api.organizacionNativa();
  await jsonLlego; // BARRERA: el cuerpo de A está DENTRO de json()
  cambiarCredencialNativa("cred-B"); // la identidad cambia CON el cuerpo en mano
  // [codex followup 17:30] el CUERPO real y distinguible de A — no el objeto
  // respuesta: json() debe devolver lo que el servidor habría dado.
  abrirJson(CUERPO_ORG_DE("ORG-DE-A")); // el cuerpo de A sale de json() AHORA — debe descartarse
  const r = await enVuelo;
  globalThis.fetch = fetchReal;
  caso("fetch JSON demorado: el cuerpo de A no se valida bajo B — se sirve el DISTINTO de B",
    r.disponible && r.datos.revision.source_digest === "ORG-DE-B", true);
  caso("fetch JSON demorado: el reintento leyó con la sesión reabierta de B",
    pedidos.some((p) => p.url.endsWith("/organization") && p.auth === "Bearer S-DE-B"), true);
}

{
  // 401 ATRASADO con BARRERA: el GET vuela con la sesión de A, la identidad
  // cambia, y SÓLO ENTONCES llega el 401. El 401 viejo no puede limpiar la
  // sesión ni reabrir con la credencial vieja [codex e101].
  cambiarCredencialNativa("cred-A");
  const pedidos = [];
  const fetchReal = globalThis.fetch;
  let fase = "calentar";
  let avisarGet, soltar401;
  const getLlego = new Promise((res) => { avisarGet = res; });
  const puerta401 = new Promise((res) => { soltar401 = (v) => res(v); });
  globalThis.fetch = async (url, opts = {}) => {
    pedidos.push({ url, auth: opts.headers?.Authorization });
    if (url.endsWith("/sessions"))
      return SESION(fase === "calentar" ? "S-DE-A" : "S-DE-B");
    if (url.endsWith("/runtimes")) return rJSON(200, [OBS_COMPLETA]); // el calentamiento de B lee runtimes
    if (fase === "calentar") return ORG_DE("ORG-DE-A");
    if (fase === "reten") { fase = "suelto"; avisarGet(); return puerta401; } // EL GET con la sesión de A, retenido
    return ORG_DE("ORG-DE-B");
  };
  await api.organizacionNativa(); // calienta caché: sesión S-DE-A con cred-A
  fase = "reten";
  const enVuelo = api.organizacionNativa(); // 2ª lectura con caché → GET retenido
  await getLlego; // BARRERA: el GET con S-DE-A está en vuelo
  cambiarCredencialNativa("cred-B"); // la identidad cambia CON el GET en vuelo
  // B se abre y se CACHEA ANTES de soltar el 401 de A [astra-ca374eb]: el 401
  // atrasado no puede cargarse la sesión de B ya publicada.
  const rB = await api.runtimesNativos();
  soltar401(rJSON(401, { code: "SESSION_INVALID" })); // el 401 de A llega TARDE
  const r = await enVuelo;
  globalThis.fetch = fetchReal;
  caso("fetch 401 atrasado: B cacheada antes del 401 — el 401 viejo la respeta y se sirve B",
    r.disponible && r.datos.revision.source_digest === "ORG-DE-B" && rB.disponible, true);
  caso("fetch 401 atrasado: NINGUNA reapertura viaja con la credencial vieja (2 aperturas exactas)",
    JSON.stringify(pedidos.filter((p) => p.url.endsWith("/sessions")).map((p) => p.auth)),
    JSON.stringify(["Bearer cred-A", "Bearer cred-B"]));
}

{
  // El caché NO retrocede — con barrera de APERTURA: el POST de A está dentro
  // de la red cuando cambia la identidad; B abre, sirve y cachea; la apertura
  // de A resuelve DESPUÉS y no puede publicarse por encima [codex e101:
  // verificar el epoch antes de publicar el caché].
  cambiarCredencialNativa("cred-A");
  const fetchReal = globalThis.fetch;
  let avisarApertura, soltarApertura;
  const aperturaLlego = new Promise((res) => { avisarApertura = res; });
  const puertaApertura = new Promise((res) => { soltarApertura = (v) => res(v); });
  let aperturas = 0;
  globalThis.fetch = async (url) => {
    if (url.endsWith("/sessions")) {
      aperturas++;
      if (aperturas === 1) { avisarApertura(); return puertaApertura; } // apertura de A, retenida
      return SESION("S-DE-B");
    }
    return url.endsWith("/runtimes") ? rJSON(200, [OBS_COMPLETA]) : ORG_DE("ORG-DE-B");
  };
  const enA = api.organizacionNativa(); // apertura de A queda retenida en la red
  await aperturaLlego; // BARRERA: el POST de A está dentro
  cambiarCredencialNativa("cred-B");
  const rB = await api.runtimesNativos(); // abre B, sirve y cachea B
  soltarApertura(SESION("S-DE-A")); // A resuelve TARDE: sin guard, pisaría el caché de B
  const rA = await enA; // la lectura A se retoma bajo B y se sirve (la promesa guardada se ESPERA)
  const rC = await api.organizacionNativa(); // con B vivo en caché: cero aperturas nuevas
  globalThis.fetch = fetchReal;
  caso("fetch caché: la sesión tardía de A no retrocede el caché — B sigue vivo y no reabre",
    aperturas === 2 && rB.disponible && rA.disponible && rC.disponible, true);
  caso("fetch caché: la lectura tras la tardía sirve el cuerpo de B",
    rC.disponible && rC.datos.revision.source_digest === "ORG-DE-B", true);
}

console.log(malos ? `\n${malos} fallo(s)` : "\napi: todo verde");
process.exit(malos ? 1 : 0);
