"""Entrada PRIVADA de operador para activar el organigrama G8 (ADR-002, Decisión 6).

La activación es una operación de OPERADOR sobre un snapshot montado: no hay
endpoint de mutación de agentes ni segundo servidor ni projector. Este CLI no
es autoridad: sólo compone lo que el kernel ya exige.

Contrato que este ejecutor HONRA (y nada más):

* Autentica una SESIÓN REAL. La identidad, el carril y los permisos salen de
  la credencial YA ligada en `credential_bindings` (debe traer
  `organization.activate`; el recibo nativo exige además
  `organization.read`). Este CLI NO llama a `bind_credential`: si la
  credencial no está ligada o no trae la capacidad, el kernel lo niega y
  aquí no se fabrica nada. Nada de esto viene del JSON.
* Controla el efecto de `Journal.initialize()` sobre esquema y datos CON UNA
  GARANTÍA ESTRUCTURAL: el journal se abre con
  `open_mode=OPEN_MODE_SOLO_EXISTENTE_V7`, y el KERNEL rechaza crear o migrar
  en la decisión que gobierna el cambio (bajo el cerrojo de ciclo de vida,
  antes de la retención v6 o de la creación crash-safe) — no en una lectura
  previa del CLI. `initialize()` sobre una base v6 SÍ migra 6→7: es la
  operación deliberada del canon `docs/V1.0-SCHEMA-V7-MIGRATION.md`, jamás un
  efecto lateral de activar. El valor por defecto de los demás llamantes del
  kernel no cambia (`OPEN_MODE_ESTANDAR`). El `stored_durable_v()` previo que
  el CLI conserva es cortesía de fallo rápido, no la promesa.
* Forma canónica y hash de fuente: `source_sha256` es el SHA-256 de la
  SERIALIZACIÓN CANÓNICA del snapshot (claves ordenadas, separadores
  compactos, UTF-8) — estable ante reordenación y espacio en blanco. El
  SHA-256 del FICHERO en bruto viaja aparte en el recibo con SU nombre; no
  son la misma cifra y no se usan indistintamente.
* Revisión monótona, validación del grafo y la revisión durables las hace el
  kernel (`Journal.activate_organization`); el recibo que se imprime es la
  LECTURA NATIVA del estado activo (`Journal.organization`), no una
  reconstrucción local.

Códigos de salida: 0 = activado (recibo JSON por stdout); 1 = el KERNEL negó
la operación (AuthError/PolicyDenied/OrganizationConflict/…); 2 = uso
incorrecto del CLI (argparse); 3 = precondición local fallida (ficheros,
pepper, versión de la base, forma del snapshot). Ninguno acredita por sí
solo el estado del servicio desplegado.

Ejemplo:
    python3 tools/activa_organizacion.py \
        --db /ruta/coordination.sqlite \
        --pepper-file /ruta/pepper.bin \
        --snapshot /ruta/org-snapshot.json \
        --credencial-file /ruta/credencial.operador
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys

_RAIZ = pathlib.Path(__file__).resolve().parents[1]
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))

import coordination as C  # noqa: E402

# El vocabulario y las claves exactas ESPEAN el contrato del kernel
# (`Journal.activate_organization` / `_records`): la prevalidación local sólo
# adelanta el fallo y lo dice en claro; la AUTORIDAD sigue siendo el kernel,
# que revalida todo dentro de su transacción.
CLAVES_SECCIONES = {
    "roles": {"role", "layer", "policy_code"},
    "reports": {"role", "reports_to"},
    "reviewers": {"role", "reviewer_role"},
    "escalations": {"role", "trigger_code", "target_role"},
    "workloads": {"workload_id", "role", "principal_id",
                  "runtime_instance", "credential_generation"},
}
CLAVES_RAIZ = {"revision", "attestation_state", *CLAVES_SECCIONES}
PEPPER_MIN_BYTES = 32  # bytes ÚTILES tras strip() (misma regla que runtime_root)


class PrecondicionFallida(Exception):
    """Fallo local de preparación (rc 3): nada tocó al kernel ni a la base."""


class KernelNegado(Exception):
    """El kernel negó la activación (rc 1): la base manda, no el CLI."""


def _lee_fichero_privado(ruta: pathlib.Path, *, que: str) -> bytes:
    if not ruta.is_file():
        raise PrecondicionFallida(f"falta el fichero de {que}: {ruta}")
    try:
        datos = ruta.read_bytes()
    except OSError as exc:
        # Permisos, E/S, carrera con un borrado: fallo LOCAL controlado (rc 3),
        # sin contenido del fichero ni traceback.
        raise PrecondicionFallida(
            f"no se pudo leer el fichero de {que} ({ruta}): "
            f"{exc.strerror or type(exc).__name__}") from exc
    if not datos.strip():
        raise PrecondicionFallida(f"el fichero de {que} está vacío: {ruta}")
    return datos


def _carga_snapshot(ruta: pathlib.Path) -> tuple[dict, str, str]:
    """Devuelve (documento, sha256_canónico, sha256_del_fichero_en_bruto)."""
    bruto = _lee_fichero_privado(ruta, que="snapshot")
    sha_fichero = hashlib.sha256(bruto).hexdigest()
    try:
        doc = json.loads(bruto.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PrecondicionFallida(f"el snapshot no es JSON UTF-8: {exc}") from exc
    if type(doc) is not dict:
        raise PrecondicionFallida("el snapshot debe ser un objeto JSON")
    desconocidas = set(doc) - CLAVES_RAIZ
    if desconocidas:
        # Fail closed: una clave que el kernel no conoce no se cuela en silencio
        # dentro del digest canónico de algo que se va a activar.
        raise PrecondicionFallida(
            f"claves desconocidas en el snapshot: {sorted(desconocidas)} — "
            f"el vocabulario es cerrado: {sorted(CLAVES_RAIZ)}")
    faltan = CLAVES_RAIZ - set(doc)
    if faltan:
        raise PrecondicionFallida(f"faltan claves en el snapshot: {sorted(faltan)}")
    if type(doc["revision"]) is not int or doc["revision"] <= 0:
        raise PrecondicionFallida("revision debe ser entero positivo")
    if type(doc["attestation_state"]) is not str:
        # Membership en el vocabulario exige hashable: un JSON con lista/dict
        # aquí sería TypeError crudo, no el rc 3 documentado.
        raise PrecondicionFallida("attestation_state debe ser texto (str)")
    if doc["attestation_state"] not in C.ORGANIZATION_ATTESTATION_STATES:
        raise PrecondicionFallida(
            "attestation_state fuera del vocabulario "
            f"{sorted(C.ORGANIZATION_ATTESTATION_STATES)}")
    for seccion, claves in CLAVES_SECCIONES.items():
        if type(doc[seccion]) is not list:
            raise PrecondicionFallida(f"{seccion} debe ser lista")
        for item in doc[seccion]:
            if type(item) is not dict or set(item) != claves:
                raise PrecondicionFallida(
                    f"cada elemento de {seccion} exige exactamente "
                    f"{sorted(claves)}")
    canonico = json.dumps(doc, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False).encode("utf-8")
    return doc, hashlib.sha256(canonico).hexdigest(), sha_fichero


def _abre_journal_v7(ruta_db: pathlib.Path, pepper: bytes) -> "C.Journal":
    """Abre una base EXISTENTE y SÓLO si ya está en v7.

    La GARANTÍA de no crear/no migrar es ESTRUCTURAL: el journal se construye
    con `open_mode=OPEN_MODE_SOLO_EXISTENTE_V7`, y el kernel rechaza
    (`OpenModeRestricted`) en la decisión que gobierna el cambio — bajo el
    cerrojo de ciclo de vida, sobre la clasificación recién fotografiada, antes
    de retener la instantánea v6 o crear nada. La lectura previa de
    `stored_durable_v()` que sigue es CORTESÍA de fallo rápido con mensaje
    operativo; no es de lo que depende la promesa.
    """
    if not ruta_db.is_file():
        raise PrecondicionFallida(
            f"la base no existe ({ruta_db}): el activador no crea bases — "
            "arranca o migra el despliegue primero")
    j = C.Journal(str(ruta_db), pepper=pepper,
                  open_mode=C.OPEN_MODE_SOLO_EXISTENTE_V7)
    try:
        v = j.stored_durable_v()
        if v is None:
            raise PrecondicionFallida(
                "la base no lleva sello durable_v: initialize() no sabe si "
                "sería creación o migración — el activador no decide eso")
        if v < C.DURABLE_V:
            raise PrecondicionFallida(
                f"la base declara durable_v={v} y activar exige "
                f"v{C.DURABLE_V}: abrir con initialize() MIGRARÍA "
                f"{v}→{C.DURABLE_V} (retiene fotografía y sella el esquema). "
                "Eso es la operación deliberada de "
                "docs/V1.0-SCHEMA-V7-MIGRATION.md, no un efecto lateral de "
                "este CLI")
        if v > C.DURABLE_V:
            raise PrecondicionFallida(
                f"la base declara durable_v={v}, futura para este código "
                f"(conoce v{C.DURABLE_V}): no se activa nada")
        try:
            j.initialize()
        except C.OpenModeRestricted as exc:
            raise PrecondicionFallida(
                "el kernel rechazó la apertura en la decisión que gobierna el "
                f"cambio (no se creó ni migró nada): {exc}") from exc
        return j
    except BaseException:
        # Ningún camino de fallo (sello ilegible, kernel negando, KeyboardInterrupt)
        # deja el journal abierto: cerrar SIEMPRE antes de propagar.
        j.close()
        raise


def activa(*, db: pathlib.Path, pepper_file: pathlib.Path,
           snapshot_file: pathlib.Path, credencial_file: pathlib.Path
           ) -> dict:
    """Ejecuta el ciclo completo y devuelve el recibo (lectura nativa)."""
    pepper = _lee_fichero_privado(pepper_file, que="pepper").strip()
    if len(pepper) < PEPPER_MIN_BYTES:
        raise PrecondicionFallida(
            f"el pepper necesita {PEPPER_MIN_BYTES} bytes útiles tras strip() "
            f"(tiene {len(pepper)}): {pepper_file}")
    credencial_bruta = _lee_fichero_privado(
        credencial_file, que="credencial").rstrip(b"\r\n")
    try:
        credencial = credencial_bruta.decode("utf-8")
    except UnicodeDecodeError as exc:
        # Fallo LOCAL de la credencial, controlado ANTES de abrir la base
        # (rc 3): no se abre journal ni se intenta sesión con bytes ajenos
        # a texto, y no viaja el contenido del fichero en el mensaje.
        raise PrecondicionFallida(
            "la credencial no es UTF-8 válido: no se abre la base") from exc
    doc, sha_canonico, sha_fichero = _carga_snapshot(snapshot_file)

    j = _abre_journal_v7(db, pepper)
    try:
        sesion = j.open_session(credencial)
        try:
            j.activate_organization(
                sesion.token,
                revision=doc["revision"],
                source_sha256=sha_canonico,
                attestation_state=doc["attestation_state"],
                roles=doc["roles"], reports=doc["reports"],
                reviewers=doc["reviewers"], escalations=doc["escalations"],
                workloads=doc["workloads"])
        except C.JournalError as exc:
            raise KernelNegado(
                f"el kernel negó la activación (revision={doc['revision']}): "
                f"{type(exc).__name__}: {exc}") from exc
        recibo = {
            "activacion": {
                "revision": doc["revision"],
                "source_sha256_canonico": sha_canonico,
                "snapshot_archivo_sha256": sha_fichero,
                "cota_digest": "source_sha256 es del CANON (claves ordenadas), "
                               "no del fichero en bruto",
            },
        }
        try:
            recibo["organizacion_activa"] = j.organization(sesion.token)
        except C.PolicyDenied:
            # La activación YA es durable; la lectura nativa exige una
            # capacidad aparte y se declara su ausencia en vez de fingirla.
            recibo["lectura_nativa"] = (
                "denegada: la credencial no trae organization.read")
        return recibo
    finally:
        j.close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Entrada PRIVADA de operador: activa una revisión del "
                    "organigrama G8 sobre una base v7 EXISTENTE (ADR-002 D6). "
                    "No crea bases, no migra, no liga credenciales, no abre "
                    "puertos.")
    p.add_argument("--db", required=True, type=pathlib.Path,
                   help="base v7 EXISTENTE del journal (debe existir; nunca "
                        "se crea ni se migra aquí)")
    p.add_argument("--pepper-file", required=True, type=pathlib.Path,
                   help="fichero con el pepper del despliegue "
                        f"(≥{PEPPER_MIN_BYTES} bytes útiles tras strip())")
    p.add_argument("--snapshot", required=True, type=pathlib.Path,
                   help="snapshot del organigrama montado en privado "
                        "(JSON; vocabulario cerrado)")
    p.add_argument("--credencial-file", required=True, type=pathlib.Path,
                   help="fichero con la credencial YA LIGADA del operador "
                        "(credential_bindings debe traer organization.activate)")
    a = p.parse_args(argv)
    try:
        recibo = activa(db=a.db, pepper_file=a.pepper_file,
                        snapshot_file=a.snapshot,
                        credencial_file=a.credencial_file)
    except PrecondicionFallida as exc:
        print(f"PRECONDICION_FALLIDA: {exc}", file=sys.stderr)
        return 3
    except KernelNegado as exc:
        print(f"KERNEL_NEGADO: {exc}", file=sys.stderr)
        return 1
    except C.AuthError as exc:
        # Sesión no emitible (credencial sin ligadura viva): negación del kernel.
        print(f"KERNEL_NEGADO: AuthError: {exc}", file=sys.stderr)
        return 1
    except C.JournalError as exc:
        # Red genérica: CUALQUIER otro rechazo del kernel (p. ej. la lectura
        # nativa del recibo) sale saneado como rc1, nunca como traceback.
        print(f"KERNEL_NEGADO: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(recibo, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
