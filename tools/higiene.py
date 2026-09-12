#!/usr/bin/env python3
"""Publication hygiene gate — runs identically on a laptop and in CI.

    python3 tools/higiene.py            # scan, exit 1 on a blocking finding
    python3 tools/higiene.py --strict   # warnings block too
    python3 tools/higiene.py --links    # only check that relative links resolve

Run from a Git checkout with a committed HEAD. An unpacked archive without Git
metadata cannot establish publication coverage and exits 2 (not measured).
It scans tracked files and non-ignored new files. Blocking coverage includes the
manifest, the actual HEAD archive and files that current export rules would ship
after commit. It never edits or deletes anything.
"""
# Comentarios en castellano por la regla de la casa (CONTRIBUTING §Style).
#
# ⚠️ POR QUÉ ESTE FICHERO EXISTE Y NO UN HEREDOC EN EL WORKFLOW:
# el detector vivía dentro de `.github/workflows/ci.yml`, así que sólo corría en
# CI y sólo en `main`/PR. Un hallazgo real (`docs/V0.9-EXECUTION.md`, ruta personal)
# estuvo ROJO e INVISIBLE en una rama de trabajo durante días: el gate existía y
# nadie podía correrlo donde se trabaja. Un gate que sólo corre donde no miras
# tiene el mismo valor que no tenerlo.
#
# ⚠️ Y POR QUÉ HAY DOS SEVERIDADES:
# bloquear por «nombre interno» dejaría este repo en rojo permanente (hay cientos
# de menciones, y la mayoría son el mejor material del repo: cicatrices medidas).
# Un rojo permanente enseña a ignorar el rojo, que es el mismo defecto que un
# verde permanente enseña a confiar en él. Lo que bloquea es lo que NO admite
# discusión (secretos, rutas de una máquina concreta, correos); lo que exige
# criterio se REPORTA con su cuenta, y `--strict` lo convierte en bloqueo el día
# que alguien decida cerrar esa lista.

import argparse
import os
import re
import subprocess
import sys
import tarfile
import tempfile

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── detectores ────────────────────────────────────────────────────────────────
# Cada uno trae su caso PLANTADO (⊕ debe casar) y su caso LIMPIO (⊖ NO debe
# casar). El ⊖ es la mitad que falta en casi todos los gates: un detector
# demasiado ancho también da un resultado inservible, sólo que en la otra
# dirección — y se descubre tarde, cuando ya nadie lee su salida.
BLOQUEANTES = {
    "secret token": (
        re.compile(r"\b(sv_|wk_|ghp_|gho_|github_pat_|sk-|xoxb-)[A-Za-z0-9_\-]{16,}"),
        "sv_abcdefghij1234567890",
        "the token is required but never printed",
    ),
    "AWS credential": (
        re.compile(r"AKIA[0-9A-Z]{16}|\barn:aws:"),
        "arn:aws:iam",
        "aws is not a dependency here",
    ),
    "private key": (
        re.compile(r"BEGIN [A-Z ]*PRIVATE KEY"),
        "-----BEGIN RSA PRIVATE KEY-----",
        "no keys are shipped",
    ),
    "absolute home path": (
        # `/home/runner` es la ruta de GitHub Actions y no identifica a nadie.
        re.compile(r"/Users/[A-Za-z0-9._-]+|/home/(?!runner\b)[A-Za-z0-9._-]+|[A-Z]:\\Users\\"),
        "/Users/someone/project",
        "paths are relative or /home/runner in CI",
    ),
    "private IP": (
        re.compile(r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b|\b192\.168\.\d{1,3}\.\d{1,3}\b"),
        "10.1.0.1",
        "the service binds to 127.0.0.1",
    ),
    "email address": (
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        "someone@example.org",
        "report through the repository advisory form",
    ),
    "unfilled placeholder": (
        # 🩸 El primer patrón casaba sólo la forma ESCUETA del marcador y dejaba pasar la
        # forma que un humano escribe de verdad en un documento serio —la que lleva DENTRO del
        # delimitador la explicación de qué falta. Cuanto más informativo el hueco, más invisible
        # para la aguja. Medido contra este mismo gate: la escueta daba rc=1 y la explicada rc=0,
        # y la explicada era justo la cláusula que el gate existe para no publicar en blanco.
        # Un hueco sin rellenar en algo que se publica no es un descuido menor: es una
        # promesa a medias con la firma de la casa debajo. Nació de un caso concreto —un
        # texto legal entregado con el domicilio social sin rellenar—, y por eso la lista
        # es de marcadores de HUECO, no de TODO: `TODO` aparece 99 veces en el código
        # publicado y marcarlo haría el gate inservible, que es el modo de fallo que este
        # fichero existe para no cometer.
        re.compile(r"\[[Pp]endiente[^]\n]*\]|\[PENDIENTE[^]\n]*\]|<rellenar[^>\n]*>"
                   r"|\bTBD\b|\bFIXME\b|\bXXX\b"),
        "courts of [pendiente: ciudad del domicilio social]",
        "the registered office is stated in full",
    ),
    "person name": (
        # Regla de producto (2026-09-11): el nombre de una persona se anonimiza
        # ANTES de publicar. Lista CORTA Y DECLARADA a propósito, como `private
        # domain`: un detector genérico de nombres de persona produce ruido, y un
        # gate ruidoso se desactiva solo. Crece cuando alguien nombre un caso, y lo
        # legítimo —un NOTICE de tercero, una licencia— sale por la allowlist.
        # NO incluye handles públicos (el de GitHub del mantenedor está en
        # MAINTAINERS.md a propósito: es cómo se le escribe).
        # ⚠️ la forma LARGA de un nombre no la casa la corta: la frontera de palabra corta
        # antes de la letra que las separa, así que las dos van en la lista.
        # La lista sale de la SSoT del grupo, no de la memoria de quien la escribe: la forma
        # LEGAL es la que aparece en un documento fiscal, y es la que faltaba.
        re.compile(r"\b(?:Alberto|ALBERTO|Albert|ALBERT|Alonso|ALONSO|Andr[eé]s|ANDR[EÉ]S|Patxi|PATXI)\b"),
        "decided by Albert on the call",
        "decided by the operator on the call",
    ),
    "private domain": (
        # Dominios de la organización. NO se listan todos los dominios del mundo
        # a propósito: `docs/` cita fuentes oficiales de terceros por URL, y un
        # detector que las marcase haría inservible el gate.
        re.compile(r"\b(?:biklabs\.ai|bik\.eus|64bis\.eus|ayudas\.ai|bikain[a-z]*\.[a-z]{2,})\b"),
        "www.biklabs.ai",
        "no first-party domain is required to run this",
    ),
}

AVISOS = {
    # Nombres de proyectos, carriles y roles de la organización que los escribió.
    # No son secretos; son contexto que no significa nada fuera de casa, y en los
    # valores por defecto llegan a ser un defecto de producto (un usuario hereda
    # una exclusión que no entiende).
    "internal name": (
        re.compile(
            r"\b(?:64bis|biklabs|bikain|crm-pm|cfocockpit|cfo-cockpit|contratosbik"
            r"|harness-biklabs|wiki-vault|bik-labs)\b",
            re.IGNORECASE,
        ),
        "the 64bis lane",
        "a lane called team",
    ),
}

# Documentos que describen la organización, no el producto. Se REPORTAN para que
# publicarlos sea una decisión y no un olvido; este gate no borra nada.
DOCS_INTERNAS = re.compile(
    r"(?:^|/)(?:AGENT-OS[^/]*\.md|ORGANIGRAMA[^/]*\.md|LEDGER[^/]*\.md"
    r"|COORDINACION-[^/]*\.md|HANDOFF-[^/]*\.md)$"
)

# ⚠️ UNA SOLA EXCLUSIÓN, Y ES LA ÚNICA QUE NO ADMITE OTRA FORMA.
# La versión anterior saltaba `.github/workflows/*` entero y todo `*.lock`/`*.yaml`
# POR EXTENSIÓN. Ahí es justo donde caben secretos y URLs: un workflow lleva
# credenciales de despliegue y un lockfile lleva registries, tarballs y a veces
# tokens embebidos en una URL. Una exclusión en bloque es un punto ciego POR
# CONSTRUCCIÓN — y encima invisible, porque no aparece en ningún recuento.
# La regla que la sustituye: se escanea el texto y se allowlistea el fixture
# CONCRETO, línea a línea y con su motivo.
#
# El registro de excepciones es lo único que queda fuera, y no por comodidad:
# contiene los textos casados COMO DATO, así que casarse a sí mismo no significa
# nada. Todo lo demás —este propio fichero incluido— se escanea.
SE_EXCLUYEN = {"tools/higiene-allowlist.txt"}

# Sólo binarios de verdad. Nada se salta por ser "configuración" o "generado".
BINARIOS = (".png", ".jpg", ".jpeg", ".ico", ".gif", ".pdf", ".woff", ".woff2")


def cargar_allowlist(ruta):
    """`fichero:texto  # motivo` — una excepción sin motivo escrito no es una
    excepción, es un agujero, así que el motivo es obligatorio."""
    permitido = {}
    if not os.path.exists(ruta):
        return permitido
    for n, linea in enumerate(open(ruta, encoding="utf-8"), 1):
        linea = linea.strip()
        if not linea or linea.startswith("#"):
            continue
        cuerpo = linea.split("#", 1)
        if len(cuerpo) != 2 or not cuerpo[1].strip():
            print(f"allowlist:{n}: entry without a written reason — ignored", file=sys.stderr)
            continue
        if ":" not in cuerpo[0]:
            print(f"allowlist:{n}: expected `path:text  # reason`", file=sys.stderr)
            continue
        fichero, texto = cuerpo[0].split(":", 1)
        permitido[(fichero.strip(), texto.strip())] = cuerpo[1].strip()
    return permitido


def ficheros_del_tarball():
    """Miembros de `git archive HEAD`, sin extraerlos ni cargar el tar en memoria.

    `ls-tree` no basta: incluye los ficheros marcados `export-ignore`. Una lectura
    fallida tampoco equivale a un tarball vacío ni permite estrechar el escaneo.
    """
    with tempfile.TemporaryFile() as errores:
        try:
            with subprocess.Popen(
                ["git", "archive", "--format=tar", "HEAD"], cwd=RAIZ,
                stdout=subprocess.PIPE, stderr=errores,
            ) as proceso:
                try:
                    with tarfile.open(fileobj=proceso.stdout, mode="r|") as archivo:
                        ficheros = {m.name for m in archivo if not m.isdir()}
                    # Consumir también el relleno final antes de cerrar la tubería.
                    while proceso.stdout.read(65536):
                        pass
                except (tarfile.TarError, OSError):
                    proceso.kill()
                    raise
                finally:
                    proceso.stdout.close()
                if proceso.wait() != 0:
                    raise RuntimeError("git archive failed")
        except (OSError, tarfile.TarError, RuntimeError) as exc:
            errores.seek(0)
            detalle = errores.read(2000).decode("utf-8", errors="replace").strip()
            print(f"FATAL: cannot read the published tarball: {exc}. {detalle}",
                  file=sys.stderr)
            raise SystemExit(2) from exc
    if len(ficheros) < 50:
        print(f"FATAL: the tarball lists only {len(ficheros)} file(s). "
              "Refusing to decide publication coverage from a list that short.",
              file=sys.stderr)
        raise SystemExit(2)
    return ficheros


def ficheros_por_publicar():
    """Cobertura previa al commit según los atributos del árbol de trabajo.

    Incluye archivos nuevos, añadidos al índice y exclusiones retiradas. Un
    directorio con `export-ignore` excluye su contenido aunque el atributo del
    archivo hijo sea `unspecified`; por eso se consultan también sus padres.
    """
    try:
        candidatos = ficheros_versionados()
        rutas = set(candidatos)
        for fichero in candidatos:
            padre = os.path.dirname(fichero)
            while padre:
                rutas.add(padre)
                padre = os.path.dirname(padre)
        resultado = subprocess.run(
            ["git", "check-attr", "-z", "--stdin", "export-ignore"], cwd=RAIZ,
            input=b"".join(os.fsencode(r) + b"\0" for r in sorted(rutas)),
            capture_output=True, check=True,
        )
        campos = resultado.stdout.split(b"\0")
        if campos[-1] != b"" or (len(campos) - 1) != 3 * len(rutas):
            raise ValueError("incomplete export-ignore attributes")
        atributos = {}
        for i in range(0, len(campos) - 1, 3):
            ruta, atributo, valor = campos[i:i + 3]
            nombre = os.fsdecode(ruta)
            if atributo != b"export-ignore" or nombre not in rutas or nombre in atributos:
                raise ValueError("unexpected export-ignore attributes")
            atributos[nombre] = valor
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"FATAL: cannot determine pre-commit publication coverage: {exc}",
              file=sys.stderr)
        raise SystemExit(2) from exc

    publicados = set()
    for fichero in candidatos:
        ruta = fichero
        while ruta and atributos[ruta] != b"set":
            ruta = os.path.dirname(ruta)
        if not ruta:
            publicados.add(fichero)
    return publicados


def superficie_publicada():
    """Los ficheros que SE PUBLICAN: lo que el manifiesto declara ∪ lo que el tarball
    lleva ∪ lo que las reglas actuales publicarían tras el commit. Es el UNIVERSO
    en el que un hallazgo BLOQUEA; hacer `git add` no cambia su severidad.

    Por qué el gate no puede bloquear sobre el árbol entero, medido el 2026-09-11: este
    repositorio tiene `666` ficheros versionados y publica `467`. Los `199` de diferencia
    —recibos de `docs/evidence/`, el censo de la flota, documentos de despliegue— no salen
    de aquí, y sobre ellos el gate daba `102` bloqueantes. Un gate que bloquea por lo que
    no se publica enseña a ignorarlo, y el día que marque algo real ya nadie lo lee.

    Lo de fuera NO desaparece: se REPORTA con su recuento. Una exclusión en bloque es un
    punto ciego por construcción — y encima invisible, porque no aparece en ningún número.
    """
    manifiesto = os.path.join(RAIZ, "OSS-MANIFEST.txt")
    entradas = []
    if os.path.exists(manifiesto):
        with open(manifiesto, encoding="utf-8") as archivo_manifiesto:
            for linea in archivo_manifiesto:
                linea = linea.split("#", 1)[0].strip()
                if linea:
                    entradas.append(linea)
    # Suelo, con la misma doctrina que el resto del fichero: si una de las dos fuentes no
    # se leyó, «todo queda fuera del universo» y el gate saldría VERDE por ceguera.
    if len(entradas) < 10:
        print(f"FATAL: the manifest lists only {len(entradas)} entr(y/ies). Refusing to decide "
              f"what is published from a list that short.", file=sys.stderr)
        raise SystemExit(2)

    tarball = ficheros_del_tarball() | ficheros_por_publicar()

    def publicado(f):
        if f in tarball:
            return True
        for e in entradas:
            if f == e:
                return True
            pref = e if e.endswith("/") else e + "/"
            if f.startswith(pref):
                return True
        return False

    return publicado


def controles(grupo):
    """⊕ y ⊖ ANTES de mirar el árbol. Un detector muerto y un árbol limpio
    producen exactamente el mismo cero."""
    for nombre, (rx, plantado, limpio) in grupo.items():
        if not rx.search(plantado):
            print(f"FATAL: detector `{nombre}` did not match its planted case", file=sys.stderr)
            return False
        if rx.search(limpio):
            print(f"FATAL: detector `{nombre}` matched its clean case (too broad)", file=sys.stderr)
            return False
    return True


def ficheros_versionados():
    """Versionados **y** los no versionados que git no ignora.

    ⚠️ FALSO VERDE MEDIDO: con sólo `git ls-files`, un fichero nuevo es invisible
    para este gate hasta que alguien lo añade al índice. Un colaborador lo corre
    antes de `git add`, lo ve en verde, y lo que el gate no miró es exactamente lo
    único que había cambiado. En CI todo está commiteado y el hueco no aparece —
    que es lo que lo hace durar.

    Lo ignorado por `.gitignore` se queda fuera a propósito: no se publica.
    """
    def git(*args):
        return subprocess.run(["git", *args], cwd=RAIZ, capture_output=True,
                              check=True).stdout.split(b"\0")
    seguidos = [os.fsdecode(f) for f in git("ls-files", "-z") if f]
    nuevos = [os.fsdecode(f) for f in git("ls-files", "-z", "--others", "--exclude-standard") if f]
    # Suelo: si esto sale casi vacío, no es que el árbol esté limpio — es que no se
    # leyó. Un cero de una lista vacía es indistinguible de un cero de un árbol sano.
    if len(seguidos) < 20:
        print(f"FATAL: git listed only {len(seguidos)} tracked file(s). Refusing to report "
              f"a clean tree from a list that short: it is not a measurement.", file=sys.stderr)
        raise SystemExit(2)
    return sorted(set(seguidos) | set(nuevos))


def barrer(grupo, permitido):
    """Devuelve (hallazgos, exenciones). Una excepción concedida se IMPRIME en cada
    corrida: si vive sólo dentro del fichero de allowlist, deja de existir para
    quien lee la salida, y una excepción invisible es indistinguible de un agujero."""
    hallazgos, exenciones = [], []
    for f in ficheros_versionados():
        if f in SE_EXCLUYEN:
            continue
        ruta = os.path.join(RAIZ, f)
        if not os.path.isfile(ruta) or f.endswith(BINARIOS):
            continue
        with open(ruta, encoding="utf-8", errors="replace") as fh:
            for i, linea in enumerate(fh, 1):
                for nombre, (rx, _, _) in grupo.items():
                    m = rx.search(linea)
                    if not m:
                        continue
                    clave = (f, m.group(0))
                    if clave in permitido:
                        exenciones.append((nombre, f, i, m.group(0)[:60], permitido[clave]))
                        continue
                    hallazgos.append((nombre, f, i, m.group(0)[:60]))
    return hallazgos, exenciones


def docs_internas():
    return [f for f in ficheros_versionados() if DOCS_INTERNAS.search(f)]


def enlaces_rotos():
    """Enlaces relativos de markdown que no resuelven. Sin red: sólo el disco,
    que es lo que se puede comprobar de forma reproducible y offline."""
    patron = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
    # ⚠️ El código se quita ANTES de buscar. Un documento que EXPLICA la sintaxis
    # markdown escribe `[texto](url)` dentro de comillas invertidas, y un
    # comprobador que no lo distingue publica un enlace roto que no existe. Cazado
    # en la primera corrida, sobre un fichero que estaba bien.
    vallas = re.compile(r"^\s*(?:```|~~~)")
    codigo = re.compile(r"`[^`]*`")
    rotos = []
    for f in ficheros_versionados():
        if not f.endswith(".md"):
            continue
        base = os.path.dirname(os.path.join(RAIZ, f))
        dentro_de_valla = False
        with open(os.path.join(RAIZ, f), encoding="utf-8", errors="replace") as fh:
            for i, linea in enumerate(fh, 1):
                if vallas.match(linea):
                    dentro_de_valla = not dentro_de_valla
                    continue
                if dentro_de_valla:
                    continue
                linea = codigo.sub("", linea)
                for destino in patron.findall(linea):
                    destino = destino.split(" ", 1)[0].strip()
                    if destino.startswith(("http://", "https://", "mailto:", "#")):
                        continue
                    objetivo = os.path.normpath(os.path.join(base, destino.split("#", 1)[0]))
                    if not os.path.exists(objetivo):
                        rotos.append((f, i, destino))
    return rotos


def main():
    ap = argparse.ArgumentParser(description="publication hygiene gate")
    ap.add_argument("--strict", action="store_true", help="warnings block too")
    ap.add_argument("--links", action="store_true", help="only check relative links")
    args = ap.parse_args()

    if args.links:
        rotos = enlaces_rotos()
        for f, i, d in rotos:
            print(f"::error file={f},line={i}::[broken link] {d}")
        print(f"\nlinks: {len(rotos)} broken relative link(s)")
        return 1 if rotos else 0

    if not (controles(BLOQUEANTES) and controles(AVISOS)):
        return 2
    print(
        f"positive+negative controls: {len(BLOQUEANTES) + len(AVISOS)} detectors "
        f"match their planted case and reject their clean case OK"
    )

    permitido = cargar_allowlist(os.path.join(RAIZ, "tools", "higiene-allowlist.txt"))
    print(f"allowlist: {len(permitido)} reviewed exception(s)\n")

    publicado = superficie_publicada()
    todos, exentos_b = barrer(BLOQUEANTES, permitido)
    bloqueos = [h for h in todos if publicado(h[1])]
    fuera = [h for h in todos if not publicado(h[1])]
    for nombre, f, i, txt in bloqueos:
        print(f"::error file={f},line={i}::[{nombre}] {txt}")
    # Fuera de la superficie publicada NO bloquea, y NO desaparece: se cuenta y se nombra.
    for nombre, f, i, txt in fuera:
        print(f"::warning file={f},line={i}::[{nombre} · not published] {txt}")

    avisos, exentos_a = barrer(AVISOS, permitido)
    for nombre, f, i, txt in avisos:
        print(f"::warning file={f},line={i}::[{nombre}] {txt}")

    for nombre, f, i, txt, motivo in exentos_b + exentos_a:
        print(f"::notice file={f},line={i}::[allowlisted {nombre}] {txt} — {motivo}")

    # Una excepción que ya no casa nada es una excepción muerta: sigue autorizando
    # un patrón que quizá vuelva mañana, y nadie la retira porque nada la nombra.
    usadas = {(f, txt) for _, f, _, txt, _ in exentos_b + exentos_a}
    muertas = [k for k in permitido if k not in usadas]
    for f, txt in sorted(muertas):
        print(f"::warning file=tools/higiene-allowlist.txt,line=1::[dead allowlist entry] "
              f"{f}:{txt} no longer matches anything — remove it")

    internas = docs_internas()
    for f in internas:
        print(f"::warning file={f},line=1::[internal document] publishing this is a decision, not a default")

    print(
        f"\nblocking: {len(bloqueos)} (in the published surface) · "
        f"not published, reported only: {len(fuera)} · "
        f"warnings: {len(avisos) + len(internas)} "
        f"({len(avisos)} internal names, {len(internas)} internal documents) "
        f"· allowlisted: {len(exentos_b) + len(exentos_a)} "
        f"({len(muertas)} dead)"
    )
    if avisos or internas:
        print("warnings do not fail this run; re-run with --strict once the list is closed.")
    return 1 if bloqueos or (args.strict and (avisos or internas)) else 0


if __name__ == "__main__":
    sys.exit(main())
