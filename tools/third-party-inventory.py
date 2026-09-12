#!/usr/bin/env python3
"""Build the third-party dependency inventory for THIRD_PARTY_NOTICES.md.

    python3 tools/third-party-inventory.py            # print the inventory
    python3 tools/third-party-inventory.py --write    # rewrite THIRD_PARTY_NOTICES.md

What it does NOT do, on purpose: it never guesses a licence. A package whose
licence text is not on disk is listed as `not collected`, never as "probably MIT".
An inventory that fills its own gaps is worse than one with holes, because the
holes are the part a lawyer needs to see.
"""
# Comentarios en castellano por la regla de la casa (CONTRIBUTING §Style).
#
# ⚠️ EL PROBLEMA QUE ESTO EXISTE PARA HACER VISIBLE:
# la imagen se construye en dos etapas y la segunda copia el bundle compilado de
# la primera. Eso es REDISTRIBUCIÓN de código de terceros, no consumo — y MIT,
# ISC y Apache-2.0 §4(c) exigen que el aviso viaje con la copia. El `NOTICE` de
# hoy sólo habla de las dependencias de Python, que ni siquiera se redistribuyen.
#
# Este script sólo produce el INVENTARIO (qué viaja, en qué versión). La
# recolección de los textos de licencia se hace donde están: dentro de la etapa
# de build, con `node_modules` resuelto. Sin él, la columna sale `not collected`
# y así se publica.

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK = os.path.join(RAIZ, "web", "pnpm-lock.yaml")
NM = os.path.join(RAIZ, "web", "node_modules")
SALIDA = os.path.join(RAIZ, "THIRD_PARTY_NOTICES.md")
PY_INPUT = os.path.join(RAIZ, "requirements.in")
PY_LOCK = os.path.join(RAIZ, "requirements.lock")

NOMBRES_LICENCIA = ("LICENSE", "LICENSE.md", "LICENSE.txt", "license", "license.md", "LICENCE")


def sha256_fichero(ruta):
    """Identifica la entrada externa sin hacer que el inventario se referencie a sí mismo."""
    if not os.path.isfile(ruta):
        return "missing"
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        for bloque in iter(lambda: f.read(1024 * 1024), b""):
            h.update(bloque)
    return h.hexdigest()


def deps_de_produccion():
    """Dependencias directas de ejecución, con la versión RESUELTA del lockfile.
    Son las que acaban dentro del bundle que la imagen redistribuye."""
    if not os.path.exists(LOCK):
        return [], 0
    texto = open(LOCK, encoding="utf-8").read()

    # `importers:` → el paquete raíz `.:` → su bloque `dependencies:`.
    m = re.search(r"^importers:\n(.*?)^packages:", texto, re.S | re.M)
    if not m:
        return [], 0
    bloque = m.group(1)
    m2 = re.search(r"^  \.:\n(.*?)(?=^  \S|\Z)", bloque, re.S | re.M)
    if not m2:
        return [], 0
    raiz = m2.group(1)
    m3 = re.search(r"^    dependencies:\n(.*?)(?=^    \S|\Z)", raiz, re.S | re.M)
    prod = m3.group(1) if m3 else ""

    out = []
    nombre = None
    for linea in prod.splitlines():
        n = re.match(r"^      '?([^':]+)'?:\s*$", linea)
        if n:
            nombre = n.group(1)
            continue
        v = re.match(r"^        version:\s*(\S+)", linea)
        if v and nombre:
            # Las versiones traen el árbol de peers entre paréntesis; sobra.
            out.append((nombre, v.group(1).split("(", 1)[0]))
            nombre = None
    total = len(re.findall(r"^  [^ ].*:\n", texto[texto.index("packages:"):], re.M))
    return out, total


def clausura_produccion():
    """La clausura REAL de producción, preguntándosela a pnpm.

    ⚠️ Las dependencias DIRECTAS no son la obligación. El bundle incluye lo que
    ellas arrastran, y son otro conjunto: aquí, 14 directas contra 41 en la
    clausura. Atribuir sólo las directas deja fuera dos tercios de los avisos que
    la copia tiene que llevar encima.

    Devuelve (paquetes, metodo). Sin `pnpm` o sin `node_modules` resuelto no se
    inventa nada: se cae a las directas del lockfile y el fichero DICE cuál usó.
    """
    web = os.path.join(RAIZ, "web")
    if not (shutil.which("pnpm") and os.path.isdir(os.path.join(web, "node_modules"))):
        return None, "lockfile (direct dependencies only)"
    r = subprocess.run(["pnpm", "list", "--prod", "--depth", "Infinity", "--json"],
                       cwd=web, capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        return None, "lockfile (direct dependencies only)"
    try:
        datos = json.loads(r.stdout)
    except ValueError:
        return None, "lockfile (direct dependencies only)"
    acc = {}

    def anda(nodo):
        for nombre, v in (nodo.get("dependencies") or {}).items():
            clave = (nombre, v.get("version"))
            if clave in acc:
                continue
            acc[clave] = v.get("path")
            anda(v)

    for raiz in (datos if isinstance(datos, list) else [datos]):
        anda(raiz)
    return sorted(acc.items()), "pnpm production closure"


def licencia_en_ruta(ruta, nombre):
    """Sólo lo que se puede LEER. Si no hay `package.json` en disco, no se
    deduce la licencia del nombre ni de la costumbre: se escribe `not collected`."""
    declarada = None
    pj = os.path.join(ruta, "package.json") if ruta else None
    if pj and os.path.isfile(pj):
        try:
            d = json.load(open(pj, encoding="utf-8"))
            lic = d.get("license") or d.get("licenses")
            if isinstance(lic, list):
                lic = " OR ".join(x.get("type", str(x)) if isinstance(x, dict) else str(x) for x in lic)
            declarada = lic if isinstance(lic, str) else None
        except (ValueError, OSError):
            declarada = None
    fichero = None
    if ruta:
        for n in NOMBRES_LICENCIA:
            if os.path.isfile(os.path.join(ruta, n)):
                fichero = n
                break
    return declarada, fichero


def licencia_en_disco(nombre):
    """Sólo lo que se puede leer. Si `node_modules` no está resuelto en esta
    máquina, esto devuelve (None, None) y la tabla lo dice."""
    ruta = os.path.join(NM, *nombre.split("/"))
    pj = os.path.join(ruta, "package.json")
    declarada = None
    if os.path.exists(pj):
        try:
            d = json.load(open(pj, encoding="utf-8"))
            lic = d.get("license") or d.get("licenses")
            if isinstance(lic, list):
                lic = " OR ".join(x.get("type", str(x)) if isinstance(x, dict) else str(x) for x in lic)
            declarada = lic if isinstance(lic, str) else None
        except (ValueError, OSError):
            declarada = None
    fichero = None
    for n in NOMBRES_LICENCIA:
        if os.path.exists(os.path.join(ruta, n)):
            fichero = f"web/node_modules/{nombre}/{n}"
            break
    return declarada, fichero


def deps_python():
    """Lee la clausura sólo del lock generado desde requirements.in."""
    if os.path.exists(PY_LOCK):
        pines = []
        for linea in open(PY_LOCK, encoding="utf-8"):
            linea = linea.split("#", 1)[0].strip()
            # Los locks con hashes continúan cada requisito con `\\`; el parser
            # anterior exigía fin de línea justo tras la versión y, por tanto,
            # declaraba una clausura de CERO paquetes sobre un lock válido.
            m = re.match(
                r"^([A-Za-z0-9._-]+(?:\[[^\]]+\])?)==([^\s;\\]+)(?:\s*\\)?(?:\s*;.*)?$",
                linea,
            )
            if m:
                pines.append((m.group(1), m.group(2)))
        return pines, "requirements.lock"
    return [], "missing requirements.lock"


def autoridad_python(pines):
    """Comprueba que requirements.in es exacto y que el lock lo materializa."""
    if not os.path.isfile(PY_INPUT):
        return ["`requirements.in` is missing; there is no Python dependency authority."]
    directos = []
    problemas = []
    for bruto in open(PY_INPUT, encoding="utf-8"):
        linea = bruto.split("#", 1)[0].strip()
        if not linea:
            continue
        m = re.fullmatch(r"([A-Za-z0-9._-]+)(?:\[[^\]]+\])?==([^\s;\\]+)", linea)
        if not m or "*" in (m.group(2) if m else ""):
            problemas.append(
                f"`requirements.in` contains a non-exact or unsupported requirement: `{linea}`.")
            continue
        directos.append((m.group(1).lower().replace("_", "-"), m.group(2)))
    cierre = {n.split("[", 1)[0].lower().replace("_", "-"): v for n, v in pines}
    for nombre, version in directos:
        if cierre.get(nombre) != version:
            problemas.append(
                f"`requirements.lock` does not contain authoritative pin `{nombre}=={version}`.")
    return problemas


def main():
    escribir = "--write" in sys.argv
    npm, total_paquetes = deps_de_produccion()
    clausura, metodo_npm = clausura_produccion()
    py, fuente_py = deps_python()
    problemas_autoridad = autoridad_python(py)
    resuelto = os.path.isdir(NM)

    # El inventario identifica únicamente sus entradas externas. Incluir aquí el
    # commit o el estado del árbol lo haría autorreferencial: al confirmar el
    # propio fichero cambiaría el commit que dice describir.
    hash_py = sha256_fichero(PY_LOCK)
    hash_py_input = sha256_fichero(PY_INPUT)
    hash_js = sha256_fichero(LOCK)
    bloqueos = []
    if not resuelto:
        bloqueos.append("`web/node_modules` was not resolved, so no licence could be read.")
    if clausura is None:
        bloqueos.append("The npm list below is DIRECT dependencies only; the production "
                        "closure that actually reaches the bundle was not resolved.")
    if fuente_py != "requirements.lock":
        bloqueos.append("Python pins came from `" + fuente_py + "`, not from a "
                        "`requirements.lock`, so the transitive closure is unrecorded.")
    bloqueos.extend(problemas_autoridad)
    if clausura is None:
        bloqueos.append("The set of packages actually reaching the shipped bundle was not "
                        "read from the build output. Only direct dependencies are listed; "
                        "transitive redistribution obligations are unknown.")
    else:
        bloqueos.append("The set of packages actually reaching the shipped bundle was not read "
                        "from the build output. What is listed is the production closure, a safe "
                        "SUPERSET: tree-shaking may drop part of it, and over-attributing is the "
                        "conservative error.")
    L = []
    L.append("# Third-party notices")
    L.append("")
    L.append("This file is the **inventory** of third-party software that this project")
    L.append("redistributes or depends on at runtime. It is generated by")
    L.append("`tools/third-party-inventory.py`; do not edit it by hand.")
    L.append("")
    L.append("## Provenance of this file")
    L.append("")
    L.append("| | |")
    L.append("|---|---|")
    L.append(f"| Python authority: `requirements.in` SHA-256 | `{hash_py_input}` |")
    L.append(f"| `requirements.lock` SHA-256 | `{hash_py}` |")
    L.append(f"| `web/pnpm-lock.yaml` SHA-256 | `{hash_js}` |")
    L.append(f"| Python pins read from | `{fuente_py}` |")
    L.append(f"| `web/node_modules` resolved | {'yes' if resuelto else 'no'} |")
    L.append(f"| npm set resolved by | `{metodo_npm}` |")
    L.append("")
    L.append("> **Status: INCOMPLETE — this is an inventory, not the notices themselves.**")
    L.append("> Nothing below is a guess: a licence that could not be read on disk is written")
    L.append("> as `not collected`, never inferred from a package name or a habit.")
    L.append("")
    L.append("### ⛔ This file does not qualify a release")
    L.append("")
    L.append("A release may only ship an inventory regenerated from the exact dependency")
    L.append("inputs of that release, with `web/node_modules` resolved from the committed lockfile, with")
    L.append("Python pins read from the `requirements.lock` generated from the sole authority")
    L.append("`requirements.in`, and with the closure that actually")
    L.append("reaches the bundle computed from the build output rather than from the list of")
    L.append("direct dependencies — licence texts collected and shipped. **Until all of that")
    L.append("holds, the release is blocked.** As generated, the following are open:")
    L.append("")
    for b in bloqueos:
        L.append(f"- {b}")
    L.append("")
    L.append("## Why this file exists")
    L.append("")
    L.append("The container image is built in two stages, and the second copies the compiled")
    L.append("web bundle out of the first. That makes the image a **redistribution** of the")
    L.append("JavaScript packages in that bundle, not merely a consumer of them — and the")
    L.append("permissive licences involved (MIT, ISC, Apache-2.0 §4(c)) require the copyright")
    L.append("and attribution notices to travel with the copy.")
    L.append("")
    L.append("## Runtime JavaScript — redistributed inside the image")
    L.append("")
    directas = {n for n, _ in npm}
    if clausura is not None:
        L.append(f"**{len(clausura)} packages** in the production closure "
                 f"({len(directas)} of them direct), resolved with `{metodo_npm}` from the "
                 f"committed lockfile, which itself resolves {total_paquetes} packages in "
                 f"total including build-time only ones.")
        L.append("")
        L.append("| Package | Version | | Declared licence | Licence text on disk |")
        L.append("|---|---|---|---|---|")
        for (nombre, version), ruta in clausura:
            declarada, fichero = licencia_en_ruta(ruta, nombre)
            marca = "direct" if nombre in directas else ""
            L.append(f"| `{nombre}` | `{version}` | {marca} | {declarada or '_not collected_'} | "
                     f"{'`' + fichero + '`' if fichero else '_not collected_'} |")
        sin_lic = [n for (n, _), r in clausura if not licencia_en_ruta(r, n)[0]]
        sin_txt = [n for (n, _), r in clausura if not licencia_en_ruta(r, n)[1]]
        L.append("")
        L.append(f"Declared licence missing for {len(sin_lic)} package(s); licence TEXT not "
                 f"found on disk for {len(sin_txt)}. Nothing is filled in by inference.")
        if sin_txt:
            L.append("")
            L.append("Without a licence text on disk there is nothing to ship alongside the "
                     "bundle for: " + ", ".join(f"`{n}`" for n in sorted(sin_txt)[:12]) +
                     ("…" if len(sin_txt) > 12 else ""))
    elif npm:
        L.append(f"{len(npm)} DIRECT runtime dependencies (the production closure could not be "
                 f"resolved on this machine), at the versions pinned in `web/pnpm-lock.yaml`, "
                 f"which resolves {total_paquetes} packages in total.")
        L.append("")
        L.append("| Package | Version | Declared licence | Licence text on disk |")
        L.append("|---|---|---|---|")
        for nombre, version in sorted(npm):
            declarada, fichero = licencia_en_disco(nombre)
            L.append(f"| `{nombre}` | `{version}` | {declarada or '_not collected_'} | "
                     f"{'`' + fichero + '`' if fichero else '_not collected_'} |")
    else:
        L.append("_Could not read `web/pnpm-lock.yaml`._")
    L.append("")
    if not resuelto:
        L.append("⚠️ `web/node_modules` is not resolved in the tree this was generated from, so")
        L.append("no licence could be read. To fill both licence columns with measured values:")
        L.append("")
        L.append("```bash")
        L.append("cd web && pnpm install --frozen-lockfile")
        L.append("cd .. && python3 tools/third-party-inventory.py --write")
        L.append("```")
        L.append("")
    L.append("## Runtime Python — redistributed inside the image")
    L.append("")
    L.append(f"Pins read from `{fuente_py}`.")
    L.append("")
    if py:
        L.append("| Package | Pin | Declared licence |")
        L.append("|---|---|---|")
        for nombre, version in py:
            L.append(f"| `{nombre}` | `{version}` | _not collected_ |")
        L.append("")
        L.append("These exact pins are the resolved runtime closure installed into the image.")
        L.append("Declared licences and their texts are deliberately still marked uncollected")
        L.append("until they are measured from the built artefact.")
    else:
        L.append("_Could not read exact pins from `requirements.lock`._")
    L.append("")
    L.append("## What is still missing")
    L.append("")
    L.append("1. Licence **texts** collected and shipped in the image next to the bundle, and")
    L.append("   committed here so a reader of the repository sees them too.")
    L.append("2. Declared licences measured for the exact Python closure in the image.")
    L.append("3. A check that fails the build when a runtime dependency arrives without a")
    L.append("   readable licence. Falsifier: add a dependency with the licence file removed —")
    L.append("   the build must go red.")
    L.append("")
    L.append("Until (1) is done, this file documents the gap rather than closing it. Saying so")
    L.append("is the point: an inventory that looks complete and is not would be worse than")
    L.append("none, because nobody would go looking.")
    texto = "\n".join(L) + "\n"

    if escribir:
        open(SALIDA, "w", encoding="utf-8").write(texto)
        print(f"written: THIRD_PARTY_NOTICES.md ({len(npm)} npm, {len(py)} python, "
              f"licences {'collected' if resuelto else 'NOT collected'})")
    else:
        print(texto)
    return 0


if __name__ == "__main__":
    sys.exit(main())
