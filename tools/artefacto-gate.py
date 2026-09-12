#!/usr/bin/env python3
"""Release-evidence gate: checksums, SBOM, provenance and a VERIFIED signature.

    python3 tools/artefacto-gate.py --precheck   # structure only. NOT the release gate.
    python3 tools/artefacto-gate.py              # release gate: verifies the signature

Both modes fail closed. A missing directory, a missing file, a missing verification
tool or a missing trust root is a RED — never a skip, never a pass with a note.

The difference between the two modes is the only one that matters:

  --precheck  answers "is the evidence shaped like release evidence?"
              A signature FILE being present is all it can say about the signature.
  (default)   answers "does the signature verify against the trust root this
              repository declares, over these checksums?"

`--precheck` green is not a release gate result and this program says so in its own
output, because "the checks passed" is exactly how a structural pass gets quoted as
if it were a verification.
"""
# Comentarios en castellano por la regla de la casa (CONTRIBUTING §Style).
#
# ⚠️ TRES FALSOS VERDES QUE ESTE FICHERO EXISTE PARA NO TENER, los tres reales y
# los tres encontrados leyendo la versión anterior:
#
# ① «hay un fichero .sig» NO es «está firmado». Un `.sig` vacío, uno de otra
#    clave, o uno de otra identidad, pasan cualquier comprobación de presencia.
#    Verificar exige HERRAMIENTA y RAÍZ DE CONFIANZA, y si falta cualquiera de las
#    dos el resultado es ROJO: «no pude verificar» no es «verificado».
#
# ② El nombre dentro de `SHA256SUMS` era libre. `/etc/passwd`, `../../x`, el mismo
#    fichero dos veces, o un enlace simbólico que apunta fuera: todos se sumaban
#    «correctamente» porque el hash se calculaba sobre lo que hubiera al final de
#    la ruta. Un nombre se confina a un BASENAME de fichero regular dentro del
#    directorio, o no se acepta.
#
# ③ La procedencia se leía hurgando recursivamente cualquier clave `sha256`. Con
#    eso, un JSON con un sha256 casual en un campo cualquiera «atestiguaba» el
#    artefacto. Se exige la forma in-toto/SLSA —`subject: [{name, digest.sha256}]`—
#    y que el `name` sea un artefacto SUMADO cuyo digest real coincida.

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SUMAS = "SHA256SUMS"
FIRMAS = (SUMAS + ".sig", SUMAS + ".asc", SUMAS + ".sigstore.json", SUMAS + ".bundle")
SBOMS = ("sbom.cdx.json", "sbom.spdx.json", "sbom.json")
PROCEDENCIAS = ("provenance.json", "provenance.intoto.jsonl", "attestation.json")
CONFIANZA = "release-trust.json"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
HEX40 = re.compile(r"^[0-9a-f]{40}$")
PLATAFORMAS = {"linux/amd64", "linux/arm64"}
TIPO_STATEMENT = "https://in-toto.io/Statement/v1"
TIPO_PROCEDENCIA = "https://slsa.dev/provenance/v1"
TIPO_BUILD = "urn:llminbox:build/oci/v1"
FROM = re.compile(
    r"^FROM[ \t]+(?P<image>[^ \t\r\n]+)(?:[ \t]+AS[ \t]+[^ \t\r\n]+)?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


def sha256(ruta):
    h = hashlib.sha256()
    with open(ruta, "rb") as fh:
        for bloque in iter(lambda: fh.read(1 << 16), b""):
            h.update(bloque)
    return h.hexdigest()


def materiales_dockerfile(ruta):
    """Materiales base esperados según el Dockerfile que se está verificando."""
    if not os.path.isfile(ruta) or os.path.islink(ruta):
        raise ValueError("Dockerfile is not a regular, non-symlink file")
    texto = open(ruta, encoding="utf-8").read()
    imagenes = [m.group("image") for m in FROM.finditer(texto)]
    if not imagenes:
        raise ValueError("Dockerfile has no FROM instructions")
    resultado = {}
    for imagen in imagenes:
        referencia, separador, digest = imagen.rpartition("@sha256:")
        if "$" in imagen or not separador or not referencia or not HEX64.fullmatch(digest):
            raise ValueError(f"Dockerfile FROM is not immutable: {imagen!r}")
        barra, dos_puntos = referencia.rfind("/"), referencia.rfind(":")
        if dos_puntos > barra:
            nombre, version = referencia[:dos_puntos], referencia[dos_puntos + 1:]
        else:
            nombre, version = referencia, None
        uri = f"pkg:docker/{nombre}" + (f"@{version}" if version else "")
        if uri in resultado:
            raise ValueError(f"Dockerfile repeats base material {uri!r}")
        resultado[uri] = digest
    return resultado


def nombre_confinado(nombre):
    """Devuelve el motivo del rechazo, o None si el nombre es aceptable.
    Un nombre de `SHA256SUMS` sólo puede ser un fichero regular en ESTE
    directorio: sin separadores, sin `..`, sin ruta absoluta, sin `~`."""
    if not nombre:
        return "empty name"
    if "/" in nombre or "\\" in nombre:
        return "contains a path separator"
    if nombre in (".", "..") or nombre.startswith("~"):
        return "is not a plain file name"
    if os.path.isabs(nombre):
        return "is an absolute path"
    if nombre != os.path.basename(nombre):
        return "is not a basename"
    return None


def clave_confinada(rel, raiz=RAIZ):
    """Resuelve la ruta de un material de confianza y la confina AL REPOSITORIO.

    Devuelve (ruta_absoluta, None) o (None, motivo).

    ⚠️ Sin esto, `release-trust.json` puede apuntar a `/Users/alguien/.gnupg/pub.asc`
    o salir del árbol con `..`, y entonces el gate verifica contra material que no
    está versionado: verde en la máquina de quien lo puso, distinto en cualquier
    otra. Un veredicto que depende del entorno no es reproducible, y lo que se está
    firmando es precisamente la reproducibilidad. Los enlaces simbólicos se
    rechazan aunque apunten dentro: lo que un enlace resuelve hoy no es lo que
    resolvía cuando alguien revisó el diff.
    """
    if not isinstance(rel, str) or not rel.strip():
        return None, "is empty"
    rel = rel.strip()
    if os.path.isabs(rel):
        return None, "is an absolute path, so it can point outside this repository"
    partes = rel.replace("\\", "/").split("/")
    if ".." in partes:
        return None, "contains `..`, so it can traverse out of this repository"
    destino = os.path.join(raiz, rel)
    # lstat en cada tramo: un enlace a mitad de la ruta también saca del árbol.
    acumulado = raiz
    for parte in partes:
        acumulado = os.path.join(acumulado, parte)
        if os.path.islink(acumulado):
            return None, f"goes through a symlink (`{os.path.relpath(acumulado, raiz)}`); " \
                         f"what a symlink resolves to today is not what it resolved to when " \
                         f"the diff was reviewed"
    real = os.path.realpath(destino)
    raiz_real = os.path.realpath(raiz)
    if not (real == raiz_real or real.startswith(raiz_real + os.sep)):
        return None, "resolves outside this repository"
    if not os.path.isfile(real):
        return None, "is not a regular file"
    return real, None


class Gate:
    def __init__(self, d, precheck, confianza=None):
        self.d, self.precheck = d, precheck
        # Ruta de la raíz de confianza. Parametrizable para que los falsadores
        # puedan correr contra una raíz sintética sin escribir en el repo.
        self.ruta_confianza = confianza or os.path.join(RAIZ, CONFIANZA)
        self.fallos = []
        self.firma_verificada = False

    def rojo(self, msg):
        self.fallos.append(msg)
        print(f"::error::{msg}")

    # ── 1 · directorio ───────────────────────────────────────────────────────
    def directorio(self):
        if not os.path.isdir(self.d):
            self.rojo(f"no artefact directory at `{os.path.relpath(self.d, RAIZ)}`: there is "
                      f"no release evidence to check. This is a RED, not a skip.")
            return None
        # El directorio de release es PLANO por contrato. La versión anterior
        # filtraba por `isfile` y con eso los directorios —y cualquier nodo que no
        # fuera fichero— desaparecían del recuento: un payload dentro de
        # `dist/extra/` no lo sumaba nadie y tampoco lo delataba el «fichero sin
        # sumar», porque nunca entraba en la lista. Se enumera TODO y se rechaza
        # lo que no sea un fichero regular.
        ficheros, no_regulares = [], []
        for nombre in sorted(os.listdir(self.d)):
            ruta = os.path.join(self.d, nombre)
            if os.path.islink(ruta):
                no_regulares.append((nombre, "a symlink: what it resolves to today is not "
                                             "what it resolved to when the checksum was taken"))
            elif os.path.isdir(ruta):
                cuantos = sum(len(fs) for _, _, fs in os.walk(ruta))
                no_regulares.append((nombre, f"a directory holding {cuantos} file(s). A release "
                                             f"directory is flat: a nested payload is covered by "
                                             f"no checksum line and shows up in no count"))
            elif not os.path.isfile(ruta):
                no_regulares.append((nombre, "not a regular file"))
            else:
                ficheros.append(nombre)
        for nombre, motivo in no_regulares:
            self.rojo(f"`{nombre}` is {motivo}")
        if not ficheros:
            self.rojo("the artefact directory holds no regular file: nothing to verify. "
                      "An empty directory is the commonest way this gate is made to pass "
                      "by accident.")
            return None
        print(f"regular files present: {len(ficheros)}")
        return ficheros

    # ── 2 · checksums, con el nombre confinado ───────────────────────────────
    def checksums(self, ficheros):
        ruta = os.path.join(self.d, SUMAS)
        sumadas = {}
        if not os.path.isfile(ruta):
            self.rojo(f"missing `{SUMAS}`: no file here can be checked at all")
            return sumadas
        vistos = set()
        for n, linea in enumerate(open(ruta, encoding="utf-8"), 1):
            linea = linea.rstrip("\n")
            if not linea.strip() or linea.lstrip().startswith("#"):
                continue
            partes = linea.split(None, 1)
            if len(partes) != 2:
                self.rojo(f"{SUMAS}:{n}: not a `<sha256>  <name>` line")
                continue
            digest, nombre = partes[0].lower(), partes[1].lstrip("*").strip()
            if not HEX64.match(digest):
                self.rojo(f"{SUMAS}:{n}: `{digest[:24]}` is not 64 hex characters")
                continue
            motivo = nombre_confinado(nombre)
            if motivo:
                self.rojo(f"{SUMAS}:{n}: name `{nombre}` {motivo} — a checksum line may only "
                          f"name a regular file inside this directory")
                continue
            if nombre in vistos:
                self.rojo(f"{SUMAS}:{n}: `{nombre}` listed more than once — with two entries "
                          f"one of them is unenforced, and nothing says which")
                continue
            vistos.add(nombre)
            sumadas[nombre] = digest
        if not sumadas:
            self.rojo(f"`{SUMAS}` lists no usable file")
        for nombre, esperado in sumadas.items():
            ruta_f = os.path.join(self.d, nombre)
            if os.path.islink(ruta_f):
                self.rojo(f"`{nombre}` is a symlink; refusing to hash through it")
                continue
            if not os.path.isfile(ruta_f):
                self.rojo(f"{SUMAS} lists `{nombre}`, which is not a regular file here")
                continue
            real = sha256(ruta_f)
            if real != esperado:
                self.rojo(f"`{nombre}`: checksum mismatch "
                          f"(listed {esperado[:16]}…, actual {real[:16]}…)")
        for f in ficheros:
            if f == SUMAS or f.startswith(SUMAS + "."):
                continue
            if f not in sumadas:
                self.rojo(f"`{f}` is in the artefact directory and not listed in {SUMAS}: "
                          f"a file nobody summed is a file nobody verified")
        return sumadas

    # ── 3 · firma: presencia (precheck) o verificación (release) ─────────────
    def unico(self, candidatos, que):
        """Varios candidatos reconocidos = ambigüedad, y elegir el primero es
        elegir en silencio. Con dos firmas presentes, verificar una y callar la
        otra deja pasar exactamente la que no se miró."""
        hallados = [f for f in candidatos if os.path.isfile(os.path.join(self.d, f))]
        if len(hallados) > 1:
            self.rojo(f"more than one {que} present ({', '.join(hallados)}). Refusing to pick "
                      f"one: whichever this gate ignored is the one nobody checked.")
            return None
        return hallados[0] if hallados else None

    def firma(self):
        fichero = self.unico(FIRMAS, "signature file")
        if fichero is None and any(os.path.isfile(os.path.join(self.d, f)) for f in FIRMAS):
            return
        if not fichero:
            self.rojo(f"no signature file for {SUMAS} (looked for: {', '.join(FIRMAS)})")
            return
        if os.path.getsize(os.path.join(self.d, fichero)) == 0:
            self.rojo(f"`{fichero}` is empty. An empty signature satisfies every check that "
                      f"only asks whether a signature exists.")
            return
        if self.precheck:
            print(f"signature file: {fichero} — PRESENT ONLY. Not verified: this is --precheck.")
            return
        self.verificar_firma(fichero)

    def verificar_firma(self, fichero):
        ruta_conf = self.ruta_confianza
        if not os.path.isfile(ruta_conf):
            self.rojo(f"no trust root: `{os.path.basename(ruta_conf)}` is missing, so there is nothing to "
                      f"verify the signature AGAINST. A signature without a declared key or "
                      f"identity proves that someone signed, not that we did.")
            return
        try:
            conf = json.load(open(ruta_conf, encoding="utf-8"))
        except ValueError as e:
            self.rojo(f"`{CONFIANZA}` is not readable JSON: {e}")
            return
        backend = conf.get("backend")
        sumas = os.path.join(self.d, SUMAS)
        firma = os.path.join(self.d, fichero)
        if not os.path.isfile(sumas):
            self.rojo(f"cannot verify `{fichero}`: there is no {SUMAS} for it to cover")
            return

        if backend == "cosign":
            self.cosign(conf, sumas, firma, fichero)
        elif backend == "minisign":
            self.minisign(conf, sumas, firma, fichero)
        elif backend == "gpg":
            self.gpg(conf, sumas, firma, fichero)
        else:
            self.rojo(f"`{CONFIANZA}`: unknown backend `{backend}` "
                      f"(expected one of: cosign, minisign, gpg)")

    def _falta_tool(self, tool):
        self.rojo(f"`{tool}` is not installed, so the signature could not be verified. "
                  f"A gate that cannot run its verifier is blind, not green.")

    def cosign(self, conf, sumas, firma, nombre):
        if not shutil.which("cosign"):
            return self._falta_tool("cosign")
        cmd = ["cosign", "verify-blob", "--signature", firma]
        if conf.get("key"):
            ruta, motivo = clave_confinada(conf["key"])
            if motivo:
                self.rojo(f"`key` {motivo}. Trust material has to be versioned inside this "
                          f"repository or the gate is not reproducible.")
                return
            cmd += ["--key", ruta]
        elif conf.get("certificate_identity") and conf.get("certificate_oidc_issuer"):
            cmd += ["--certificate-identity", conf["certificate_identity"],
                    "--certificate-oidc-issuer", conf["certificate_oidc_issuer"]]
            if conf.get("certificate"):
                motivo = nombre_confinado(conf["certificate"])
                if motivo:
                    self.rojo(f"`certificate` {motivo}: it must be a plain file name inside "
                              f"the artefact directory")
                    return
                cmd += ["--certificate", os.path.join(self.d, conf["certificate"])]
        else:
            self.rojo(f"`{CONFIANZA}`: the cosign backend needs either `key` or both "
                      f"`certificate_identity` and `certificate_oidc_issuer`. Verifying a "
                      f"signature without pinning WHO signed it accepts any valid signature "
                      f"from anyone.")
            return
        cmd.append(sumas)
        self._correr(cmd, nombre, "cosign")

    def minisign(self, conf, sumas, firma, nombre):
        if not shutil.which("minisign"):
            return self._falta_tool("minisign")
        pub = conf.get("public_key") or conf.get("public_key_file")
        if not pub:
            self.rojo(f"`{CONFIANZA}`: the minisign backend needs `public_key` "
                      f"(a key line) or `public_key_file`")
            return
        cmd = ["minisign", "-V", "-x", firma, "-m", sumas]
        if conf.get("public_key_file"):
            ruta, motivo = clave_confinada(conf["public_key_file"])
            if motivo:
                self.rojo(f"`public_key_file` {motivo}. Trust material has to be versioned "
                          f"inside this repository or the gate is not reproducible.")
                return
            cmd += ["-p", ruta]
        else:
            cmd += ["-P", pub]
        self._correr(cmd, nombre, "minisign")

    def gpg(self, conf, sumas, firma, nombre):
        """Verifica con un llavero EFÍMERO construido a partir de la clave pública
        versionada, no con lo que la máquina tenga importado.

        ⚠️ La versión anterior pedía un directorio `keyring` ya montado. Eso hace
        dos cosas malas a la vez: obliga a versionar un llavero (binario, con
        estado, imposible de revisar en un diff) y deja el veredicto a merced de lo
        que ese llavero contenga. Lo que se versiona es UNA clave pública en
        armadura ASCII —revisable línea a línea— y el gate la importa en un
        `GNUPGHOME` temporal que muere con el proceso.

        Y la huella se comprueba DOS veces, contra cosas distintas:
          · al importar, contra lo que el fichero de clave realmente contiene
            (delata una clave sustituida o alterada);
          · al verificar, contra el `VALIDSIG` de la firma (delata una firma ajena).
        Sin la primera, sustituir el fichero de clave por otro convierte al atacante
        en el firmante esperado y las dos comprobaciones cuadran entre sí.
        """
        if not shutil.which("gpg"):
            return self._falta_tool("gpg")
        huella = (conf.get("fingerprint") or "").replace(" ", "").upper()
        if not huella:
            self.rojo("the gpg backend needs `fingerprint`. Without it a signature from ANY "
                      "key in the keyring verifies, which is the foreign-signature hole this "
                      "check exists to close.")
            return
        if not re.fullmatch(r"[0-9A-F]{40}|[0-9A-F]{64}", huella):
            self.rojo(f"`fingerprint` must be a full 40- or 64-hex-character fingerprint, not "
                      f"`{huella[:16]}…` ({len(huella)} chars). A partial fingerprint matches "
                      f"more than one key by construction.")
            return
        rel = conf.get("public_key")
        if not rel:
            self.rojo("the gpg backend needs `public_key`: the path to the ASCII-armoured "
                      "public key this repository ships. Verifying against an ambient keyring "
                      "makes the verdict depend on what this machine happens to trust, which "
                      "is not a reproducible gate.")
            return
        clave, motivo = clave_confinada(rel)
        if motivo:
            self.rojo(f"`public_key` ({rel}) {motivo}. The key this gate trusts has to be a "
                      f"regular file versioned inside this repository: otherwise the verdict "
                      f"depends on material nobody reviewed.")
            return
        if os.path.getsize(clave) == 0:
            self.rojo(f"`{rel}` is empty")
            return

        hogar = tempfile.mkdtemp(prefix="llminbox-verify-")
        # ⚠️ `--no-autostart` NO ES COSMÉTICO. Sin él, cada verificación arranca un
        # `gpg-agent` contra este directorio temporal que sobrevive al proceso y
        # queda huérfano bajo PID 1 — medido: una corrida de la suite dejó agentes
        # colgados apuntando a directorios ya borrados. Verificar una firma con una
        # clave pública no necesita agente: sólo lo necesita el material privado.
        # `--no-options` evita además que un `gpg.conf` del usuario cambie el
        # comportamiento del gate, que es la misma clase de fuga que el llavero
        # ambiente: el veredicto dejaría de depender sólo de lo versionado.
        base = ["gpg", "--batch", "--no-options", "--no-autostart", "--homedir", hogar]
        try:
            os.chmod(hogar, 0o700)
            env = dict(os.environ)
            env.pop("GNUPGHOME", None)
            imp = subprocess.run(base + ["--status-fd", "1", "--import", clave],
                                 capture_output=True, text=True, env=env)
            if imp.returncode != 0:
                self.rojo(f"could not import `{rel}`: {self._resumen(imp.stdout + imp.stderr)}")
                return
            importadas = set(re.findall(r"IMPORT_OK \d+ ([0-9A-F]{40}|[0-9A-F]{64})",
                                        imp.stdout + imp.stderr))
            if not importadas:
                importadas = {f for f in re.findall(r"^fpr:{9}([0-9A-F]+):", subprocess.run(
                    base + ["--with-colons", "--list-keys"], capture_output=True,
                    text=True, env=env).stdout, re.M)}
            if huella not in importadas:
                self.rojo(f"`{rel}` does not contain the expected key: it holds "
                          f"{', '.join(sorted(importadas)) or '(nothing readable)'}, and the "
                          f"trust root declares {huella}. A substituted key file would "
                          f"otherwise make the attacker the expected signer.")
                return
            r = subprocess.run(base + ["--status-fd", "1", "--verify", firma, sumas],
                               capture_output=True, text=True, env=env)
            salida = r.stdout + r.stderr
            if r.returncode != 0:
                self.rojo(f"signature `{nombre}` did NOT verify (gpg exit {r.returncode}). "
                          f"{self._resumen(salida)}")
                return
            firmantes = set(re.findall(r"VALIDSIG ([0-9A-F]{40}|[0-9A-F]{64})", salida))
            if not firmantes:
                self.rojo(f"gpg reported success for `{nombre}` but emitted no VALIDSIG: "
                          f"nothing identifies the signer")
                return
            if huella not in firmantes:
                self.rojo(f"signature `{nombre}` is valid but was made by "
                          f"{', '.join(sorted(firmantes))}, not by the expected {huella}: "
                          f"right maths, wrong identity")
                return
            self.firma_verificada = True
            print(f"signature: `{nombre}` VERIFIED — key imported from `{rel}` into an "
                  f"ephemeral keyring, VALIDSIG {huella}")
        finally:
            # Defensa en profundidad: si alguna versión de gpg arranca el agente de
            # todos modos, se le pide morir ANTES de borrar su directorio. Borrar
            # primero deja un agente vivo apuntando a una ruta que ya no existe.
            if shutil.which("gpgconf"):
                subprocess.run(["gpgconf", "--homedir", hogar, "--kill", "gpg-agent"],
                               capture_output=True, text=True)
            shutil.rmtree(hogar, ignore_errors=True)

    def _correr(self, cmd, nombre, tool):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            self.rojo(f"signature `{nombre}` did NOT verify ({tool} exit {r.returncode}). "
                      f"{self._resumen(r.stdout + r.stderr)}")
            return
        self.firma_verificada = True
        print(f"signature: `{nombre}` VERIFIED with {tool} against the declared trust root")

    @staticmethod
    def _resumen(texto):
        lineas = [l.strip() for l in texto.splitlines() if l.strip()]
        return lineas[-1][:160] if lineas else "(no output)"

    # ── 4 · SBOM ─────────────────────────────────────────────────────────────
    def sbom(self, sumadas):
        nombre = self.unico(SBOMS, "SBOM")
        if nombre is None and any(os.path.isfile(os.path.join(self.d, f)) for f in SBOMS):
            return
        if not nombre:
            self.rojo(f"no SBOM (looked for: {', '.join(SBOMS)})")
            return
        try:
            datos = json.load(open(os.path.join(self.d, nombre), encoding="utf-8"))
        except (ValueError, OSError) as e:
            self.rojo(f"`{nombre}` is not readable JSON: {e}")
            return
        if not isinstance(datos, dict):
            self.rojo(f"`{nombre}` is not a JSON object")
            return
        if datos.get("bomFormat") == "CycloneDX":
            self._sbom_cyclonedx(nombre, datos)
        elif isinstance(datos.get("spdxVersion"), str):
            self._sbom_spdx(nombre, datos)
        else:
            self.rojo(f"`{nombre}` declares neither CycloneDX nor SPDX identity. A list named "
                      f"`components` on its own is not an SBOM contract.")
        if sumadas and nombre not in sumadas:
            self.rojo(f"`{nombre}` is not covered by {SUMAS}")

    def _sbom_cyclonedx(self, nombre, datos):
        spec = datos.get("specVersion")
        try:
            version = tuple(int(piece) for piece in str(spec).split("."))
        except ValueError:
            version = ()
        if version < (1, 5):
            self.rojo(f"`{nombre}`: CycloneDX specVersion must be at least 1.5, got {spec!r}")
        serial = datos.get("serialNumber")
        if not isinstance(serial, str) or not re.fullmatch(
                r"urn:uuid:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
                r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}", serial):
            self.rojo(f"`{nombre}`: CycloneDX has no valid `serialNumber` UUID")
        numero = datos.get("version")
        if isinstance(numero, bool) or not isinstance(numero, int) or numero < 1:
            self.rojo(f"`{nombre}`: CycloneDX document `version` must be a positive integer")
        metadata = datos.get("metadata")
        componente_raiz = metadata.get("component") if isinstance(metadata, dict) else None
        if (not isinstance(componente_raiz, dict)
                or componente_raiz.get("type") not in {"container", "application"}
                or not isinstance(componente_raiz.get("name"), str)
                or not componente_raiz["name"].strip()):
            self.rojo(f"`{nombre}`: metadata.component must identify the scanned container")
        componentes = datos.get("components")
        if not isinstance(componentes, list) or not componentes:
            self.rojo(f"`{nombre}` declares zero components. An SBOM listing nothing passes "
                      f"every check that only asks whether it exists.")
            return
        refs = set()
        identificados = 0
        tipos = {
            "application", "framework", "library", "container", "platform",
            "operating-system", "device", "device-driver", "firmware", "file",
            "machine-learning-model", "data", "cryptographic-asset",
        }
        for posicion, componente in enumerate(componentes, 1):
            if not isinstance(componente, dict):
                self.rojo(f"`{nombre}`: component {posicion} is not an object")
                continue
            ref = componente.get("bom-ref")
            if not isinstance(ref, str) or not ref.strip():
                self.rojo(f"`{nombre}`: component {posicion} lacks `bom-ref`")
            elif ref in refs:
                self.rojo(f"`{nombre}`: duplicate component `bom-ref` {ref!r}")
            else:
                refs.add(ref)
            if not isinstance(componente.get("name"), str) or not componente["name"].strip():
                self.rojo(f"`{nombre}`: component {posicion} lacks a name")
            tipo = componente.get("type")
            if tipo not in tipos:
                self.rojo(f"`{nombre}`: component {posicion} has invalid type {tipo!r}")
            # CycloneDX deliberately permits files without a version: their
            # content hash is the versioned identity. Requiring a made-up
            # version rejected Syft's real file inventory (thousands of valid
            # hashed entries). Packages, applications and the OS must still
            # carry a concrete version.
            if (tipo != "file"
                    and (not isinstance(componente.get("version"), str)
                         or not componente["version"].strip())):
                self.rojo(f"`{nombre}`: component {posicion} lacks a version")
            purl = componente.get("purl")
            hashes = componente.get("hashes")
            cpe = componente.get("cpe")
            identidad_fuerte = ((isinstance(purl, str) and purl.startswith("pkg:"))
                                 or (isinstance(hashes, list) and bool(hashes))
                                 or (isinstance(cpe, str) and cpe.startswith("cpe:")))
            # A file without a digest is just a pathname; a library/framework
            # without package identity is just a label. Applications and the
            # operating system can be valid CycloneDX components with
            # name+version+bom-ref only (this is how Syft represents PE launchers
            # embedded in pip and the Debian base).
            if tipo == "file" and not (isinstance(hashes, list) and bool(hashes)):
                self.rojo(f"`{nombre}`: file component {posicion} has no content hash")
            elif tipo in {"library", "framework"} and not identidad_fuerte:
                self.rojo(f"`{nombre}`: component {posicion} has neither purl nor hashes")
            else:
                identificados += 1
        if identificados:
            print(f"SBOM: {nombre} — CycloneDX {spec}, {len(componentes)} identified components")

    def _sbom_spdx(self, nombre, datos):
        if not re.fullmatch(r"SPDX-2\.[23]", datos.get("spdxVersion", "")):
            self.rojo(f"`{nombre}`: unsupported SPDX version {datos.get('spdxVersion')!r}")
        if datos.get("SPDXID") != "SPDXRef-DOCUMENT":
            self.rojo(f"`{nombre}`: SPDX document identity is missing")
        if not isinstance(datos.get("documentNamespace"), str):
            self.rojo(f"`{nombre}`: SPDX documentNamespace is missing")
        paquetes = datos.get("packages")
        if not isinstance(paquetes, list) or not paquetes:
            self.rojo(f"`{nombre}` declares zero packages")
            return
        ids = set()
        for posicion, paquete in enumerate(paquetes, 1):
            if not isinstance(paquete, dict):
                self.rojo(f"`{nombre}`: package {posicion} is not an object")
                continue
            spdx_id = paquete.get("SPDXID")
            if not isinstance(spdx_id, str) or not spdx_id.startswith("SPDXRef-"):
                self.rojo(f"`{nombre}`: package {posicion} lacks SPDXID")
            elif spdx_id in ids:
                self.rojo(f"`{nombre}`: duplicate package SPDXID {spdx_id!r}")
            else:
                ids.add(spdx_id)
            if not isinstance(paquete.get("name"), str) or not paquete["name"].strip():
                self.rojo(f"`{nombre}`: package {posicion} lacks a name")
            if not paquete.get("checksums") and not paquete.get("externalRefs"):
                self.rojo(f"`{nombre}`: package {posicion} has neither checksums nor externalRefs")
        print(f"SBOM: {nombre} — SPDX, {len(paquetes)} identified packages")

    # ── 5 · procedencia, atada al artefacto sumado ───────────────────────────
    def procedencia(self, sumadas):
        nombre = self.unico(PROCEDENCIAS, "provenance file")
        if nombre is None and any(os.path.isfile(os.path.join(self.d, f)) for f in PROCEDENCIAS):
            return
        if not nombre:
            self.rojo(f"no provenance attestation (looked for: {', '.join(PROCEDENCIAS)})")
            return
        try:
            doc = json.load(open(os.path.join(self.d, nombre), encoding="utf-8"))
        except (ValueError, OSError) as e:
            self.rojo(f"`{nombre}` is not readable JSON: {e}")
            return
        if not isinstance(doc, dict):
            self.rojo(f"`{nombre}` is not an in-toto Statement object")
            return
        if doc.get("_type") != TIPO_STATEMENT:
            self.rojo(f"`{nombre}`: `_type` is not in-toto Statement v1")
        if doc.get("predicateType") != TIPO_PROCEDENCIA:
            self.rojo(f"`{nombre}`: `predicateType` is not SLSA provenance v1")
        sujetos = doc.get("subject")
        if not isinstance(sujetos, list) or len(sujetos) != 1 or not isinstance(sujetos[0], dict):
            self.rojo(f"`{nombre}` must have exactly one in-toto subject")
            sujetos = []

        evidencias = {SUMAS, *FIRMAS, *SBOMS, *PROCEDENCIAS}
        artefactos = sorted(f for f in sumadas if f not in evidencias)
        if len(artefactos) != 1:
            self.rojo(f"{SUMAS} must identify exactly one release artefact; found {artefactos}")
        casados = []
        for s in sujetos:
            nom = s.get("name")
            dig = (s.get("digest") or {}).get("sha256")
            if not isinstance(nom, str) or not isinstance(dig, str):
                self.rojo(f"`{nombre}`: a subject lacks `name` or `digest.sha256`")
                continue
            nom = nom.strip()
            dig = dig.strip().lower()
            # ⚠️ NADA de `basename` aquí. Aplicarlo convertía
            # `/otro/sitio/llminbox-0.9.0.tar.gz` en `llminbox-0.9.0.tar.gz` y hacía
            # pasar por local la procedencia de un artefacto ajeno.
            motivo = nombre_confinado(nom)
            if motivo:
                self.rojo(f"`{nombre}`: subject name `{nom}` {motivo}. A subject has to name "
                          f"the artefact as this release ships it, not a path from wherever "
                          f"it was built.")
                continue
            if not HEX64.match(dig):
                self.rojo(f"`{nombre}`: subject `{nom}` has a digest that is not 64 hex chars")
                continue
            if nom not in sumadas:
                self.rojo(f"`{nombre}`: subject `{nom}` is not a file listed in {SUMAS}. "
                          f"Provenance for something this release does not ship is not "
                          f"provenance for this release.")
                continue
            if len(artefactos) == 1 and nom != artefactos[0]:
                self.rojo(f"`{nombre}`: subject names `{nom}`, but the sole release artefact "
                          f"is `{artefactos[0]}`")
                continue
            ruta = os.path.join(self.d, nom)
            real = sha256(ruta) if os.path.isfile(ruta) and not os.path.islink(ruta) else None
            if real != dig:
                self.rojo(f"`{nombre}`: subject `{nom}` claims {dig[:16]}… but the file here "
                          f"hashes to {(real or 'nothing')[:16]}…")
                continue
            casados.append(nom)
        if not casados:
            self.rojo(f"`{nombre}`: no subject names a summed artefact whose digest matches. "
                      f"The attestation is about a different artefact.")
        else:
            print(f"provenance: {nombre} — binds {len(casados)} summed artefact(s): "
                  f"{', '.join(sorted(casados))}")

        predicado = doc.get("predicate")
        if not isinstance(predicado, dict):
            self.rojo(f"`{nombre}`: missing SLSA `predicate` object")
        else:
            self._procedencia_slsa(nombre, predicado)
        if sumadas and nombre not in sumadas:
            self.rojo(f"`{nombre}` is not covered by {SUMAS}")

    def _procedencia_slsa(self, nombre, predicado):
        definicion = predicado.get("buildDefinition")
        detalles = predicado.get("runDetails")
        if not isinstance(definicion, dict):
            self.rojo(f"`{nombre}`: missing buildDefinition")
            return
        if definicion.get("buildType") != TIPO_BUILD:
            self.rojo(f"`{nombre}`: unexpected buildType {definicion.get('buildType')!r}")
        externos = definicion.get("externalParameters")
        if not isinstance(externos, dict) or set(externos) != {"platform", "source"}:
            self.rojo(f"`{nombre}`: externalParameters must contain exactly platform and source")
            externos = {}
        plataforma = externos.get("platform")
        if plataforma not in PLATAFORMAS:
            self.rojo(f"`{nombre}`: unsupported platform {plataforma!r}")
        fuente = externos.get("source")
        pareja = re.fullmatch(r"git\+(.+)@([0-9a-f]{40})", fuente or "")
        if (not pareja or any(c.isspace() for c in (fuente or ""))
                or not pareja.group(1).startswith(("https://", "ssh://", "git@"))):
            self.rojo(f"`{nombre}`: source must be a supported git URI pinned to a 40-hex commit")
            revision = None
        else:
            revision = pareja.group(2)
        internos = definicion.get("internalParameters")
        epoch = internos.get("sourceDateEpoch") if isinstance(internos, dict) else None
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch <= 0:
            self.rojo(f"`{nombre}`: sourceDateEpoch must be a positive integer")

        deps = definicion.get("resolvedDependencies")
        por_uri = {}
        if not isinstance(deps, list):
            self.rojo(f"`{nombre}`: resolvedDependencies must be an array")
            deps = []
        for posicion, dep in enumerate(deps, 1):
            uri = dep.get("uri") if isinstance(dep, dict) else None
            digest = dep.get("digest") if isinstance(dep, dict) else None
            sha = digest.get("sha256") if isinstance(digest, dict) else None
            if not isinstance(uri, str) or not uri or not isinstance(sha, str) or not HEX64.fullmatch(sha):
                self.rojo(f"`{nombre}`: resolved dependency {posicion} lacks uri/sha256")
                continue
            if set(digest) != {"sha256"}:
                self.rojo(f"`{nombre}`: dependency {uri!r} has ambiguous digest algorithms")
            if uri in por_uri:
                self.rojo(f"`{nombre}`: duplicate resolved dependency {uri!r}")
            por_uri[uri] = sha

        esperados = {}
        try:
            esperados.update(materiales_dockerfile(os.path.join(RAIZ, "Dockerfile")))
        except (OSError, ValueError) as e:
            self.rojo(f"cannot derive base-image materials from Dockerfile: {e}")
        for uri, rel in (("file:Dockerfile", "Dockerfile"),
                         ("file:requirements.lock", "requirements.lock"),
                         ("file:web/pnpm-lock.yaml", os.path.join("web", "pnpm-lock.yaml"))):
            ruta = os.path.join(RAIZ, rel)
            if not os.path.isfile(ruta) or os.path.islink(ruta):
                self.rojo(f"cannot verify provenance material `{uri}`: source file unavailable")
            else:
                esperados[uri] = sha256(ruta)
        for uri, digest in esperados.items():
            if por_uri.get(uri) != digest:
                self.rojo(f"`{nombre}`: material `{uri}` does not match the checked-out source")

        if not isinstance(detalles, dict):
            self.rojo(f"`{nombre}`: missing runDetails")
            return
        constructor = detalles.get("builder")
        constructor_id = constructor.get("id") if isinstance(constructor, dict) else None
        esperado_id = fuente.rsplit("@", 1)[0] + "/tools/build-release.sh" if pareja else None
        if constructor_id != esperado_id:
            self.rojo(f"`{nombre}`: builder.id is not the release builder for source")
        metadata = detalles.get("metadata")
        invocacion = metadata.get("invocationId") if isinstance(metadata, dict) else None
        if not HEX40.fullmatch(invocacion or "") or invocacion != revision:
            self.rojo(f"`{nombre}`: invocationId does not match the source commit")

    # ── informe ──────────────────────────────────────────────────────────────
    def informe(self):
        print()
        if self.fallos:
            print(f"{len(self.fallos)} missing or inconsistent piece(s) of release evidence.")
            print("This gate fails closed: absent evidence is a failure, never a skip.")
            return 1
        if self.precheck:
            print("STRUCTURAL PRECHECK PASSED — and that is all this means.")
            print("⚠️ The signature was NOT verified: only its presence was checked. This "
                  "result does not qualify a release. Run without --precheck for that.")
            return 0
        if not self.firma_verificada:
            print("::error::the signature was not verified, so this is not a pass")
            print("refusing to report success: reaching the end without a verified signature "
                  "is exactly the outcome this gate exists to catch.")
            return 1
        print("checksums, SBOM and provenance are consistent, and the signature VERIFIED "
              "against the declared trust root.")
        print("⚠️ This still does not mean the release is good: consistency is not "
              "correctness, and a verified signature says who signed, not what they built.")
        return 0


def main():
    ap = argparse.ArgumentParser(description="release evidence gate (fails closed)")
    ap.add_argument("--dir", default="dist", help="artefact directory (default: dist)")
    ap.add_argument("--trust", default=None,
                    help=f"trust root file (default: {CONFIANZA} at the repo root)")
    ap.add_argument("--precheck", action="store_true",
                    help="structure only; does NOT verify the signature and does not "
                         "qualify a release")
    args = ap.parse_args()

    d = args.dir if os.path.isabs(args.dir) else os.path.join(RAIZ, args.dir)
    g = Gate(d, args.precheck, args.trust)
    print(f"release evidence gate — `{os.path.relpath(d, RAIZ)}`"
          f"{' [PRECHECK: signature not verified]' if args.precheck else ''}\n")
    ficheros = g.directorio()
    if ficheros is not None:
        sumadas = g.checksums(ficheros)
        g.firma()
        g.sbom(sumadas)
        g.procedencia(sumadas)
    return g.informe()


if __name__ == "__main__":
    sys.exit(main())
