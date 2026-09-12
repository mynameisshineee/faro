#!/usr/bin/env python3
"""Arnés de MUTANTES del piloto M1 — versionado, porque un arnés que vive en el
`/tmp` de una sesión no es evidencia de nadie.

🩸 Existe versionado por el cierre del auditor sobre `2d9e785`: los `23` mutantes
se declaraban en el mensaje del commit y el runner que los produjo **no estaba en
el repo**, así que el número no era reproducible por un tercero. Un recuento que
sólo su autor puede volver a sacar es una afirmación, no una medida.

QUÉ ACREDITA Y QUÉ NO
---------------------
Acredita que, apagando UNA guarda, MUERE al menos un test — y CUÁL. No acredita
cobertura: un mutante que mata a muchos no dice de cuál es el test, y por eso el
informe imprime el conjunto entero en vez de un número.

TRES ASERCIONES SOBRE EL PROPIO ARNÉS, y las tres nacieron de fallos medidos:

  ① `count(ancla) == n`, con `n` DECLARADO por mutante. Un ancla que casa de
     menos no muta nada; una que casa de más muta líneas que nadie eligió. Dos
     anclas mías casaron `2` veces sin querer (la misma línea existía en el
     camino de lectura y en el de creación).
  ② `mutado != base`. Un mutante NO-OP sobrevive siempre y la culpa cae en el
     TEST, que es lo que se lee como rigor.
  ③ ⚠️ ② NO BASTA, y esto es la cicatriz: un `if False: pass` insertado al lado
     CAMBIA el texto y NO desactiva la guarda. La regla es semántica y no la
     puede comprobar el arnés — **si puedes borrar tu mutación y el programa se
     comporta igual, no has mutado nada**. Queda escrita aquí porque me pasó.

MUTANTES EQUIVALENTES
---------------------
Un superviviente es un HUECO o una EQUIVALENCIA. Las equivalencias van
DECLARADAS con su motivo, y **la declaración lleva su propio falsador**: si un
mutante declarado equivalente MUERE, el arnés lo imprime como «la declaración era
FALSA». Una equivalencia que no se puede desmentir es una excusa, no un análisis.

  uso:  python3 tools/pilot_mutantes.py [--python EJECUTABLE]
"""
from __future__ import annotations

import io
import os
import re
import subprocess
import sys

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREFLIGHT = os.path.join(RAIZ, "tools", "pilot_preflight.py")
TOPOLOGIA = os.path.join(RAIZ, "tools", "pilot_topologia.py")

# (id, fichero, ancla, reemplazo, n_esperado)
MUTANTES = [
    # ── el ancla del descriptor de directorio ────────────────────────────────
    ("M1  dirfd sin O_NOFOLLOW", PREFLIGHT,
     "return os.open(directorio, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)",
     "return os.open(directorio, os.O_RDONLY | os.O_DIRECTORY)", 1),
    ("M2  el mensaje no discrimina enlace/fichero", PREFLIGHT,
     "                es_enlace = statmod.S_ISLNK(os.lstat(directorio).st_mode)",
     "                es_enlace = True", 1),
    # ── durabilidad ──────────────────────────────────────────────────────────
    ("M3  fsync(dirfd) se traga el error", PREFLIGHT,
     '            except OSError as e:\n                quitado = _retirar(nombre, dirfd)\n'
     '                raise Rojo(\n                    f"ⓑ el testigo de `{ruta}` se escribió pero la ENTRADA del "',
     '            except OSError as e:\n                pass\n'
     '            if False:\n                raise Rojo(\n                    f"ⓑ el testigo de `{ruta}` se escribió pero la ENTRADA del "', 1),
    ("M4  fsync(fichero) se traga el error", PREFLIGHT,
     "os.fsync(nfd)", "_fsync_tragado(nfd)", 2),
    # ⊜ Declarado EQUIVALENTE, y por eso EXISTE: una equivalencia que no se
    # corre es una declaración que nadie puede desmentir. Se ejecuta para que
    # el arnés pueda decir «murió ⇒ tu declaración era FALSA».
    ("M4b SOLO el fsync ① se traga", PREFLIGHT,
     "                    os.fsync(nfd)\n                    # `umask` RECORTA",
     "                    _fsync_tragado(nfd)\n                    # `umask` RECORTA", 1),
    ("M5  sin fchmod (vuelve la umask)", PREFLIGHT,
     "                    os.fchmod(nfd, 0o444)", "                    pass", 1),
    ("M6  write de una sola pasada", PREFLIGHT,
     "        n = os.write(fd, cuerpo[visto:])",
     "        n = len(cuerpo); os.write(fd, cuerpo[visto:])", 1),
    ("M7  sin exigir zona en `nacido`", PREFLIGHT,
     "    if nacido.tzinfo is None or nacido.utcoffset() is None:", "    if False:", 1),
    ("M8  _retirar miente", PREFLIGHT,
     "        os.unlink(nombre, dir_fd=dirfd)\n        return True", "        return True", 1),
    ("M9  H5: vuelve `exists` (sigue enlaces)", PREFLIGHT,
     "hay_datos = bool(ruta_journal) and os.path.lexists(ruta_journal)",
     "hay_datos = bool(ruta_journal) and os.path.exists(ruta_journal)", 1),
    ("M10 H4: el fchmod vuelve ANTES del fsync", PREFLIGHT,
     "                    _escribir_todo(nfd, cuerpo)\n                    # `fsync` ①",
     "                    os.fchmod(nfd, 0o444)\n                    _escribir_todo(nfd, cuerpo)\n                    # `fsync` ①", 1),
    ("M11 H4: el testigo nace 0444 (no borrable)", PREFLIGHT,
     "                              0o600, dir_fd=dirfd)", "                              0o444, dir_fd=dirfd)", 1),
    # ── la verificación del objeto recién creado ─────────────────────────────
    ("M12 sin verificar el objeto creado", PREFLIGHT,
     "                    motivo = _verificar_estreno(nfd, dirfd, len(cuerpo))",
     '                    motivo = ""', 1),
    ("M13 sin el fsync ② (modo no durable)", PREFLIGHT,
     "                    os.fsync(nfd)\n                    # Y sólo AHORA se mira el objeto",
     "                    pass\n                    # Y sólo AHORA se mira el objeto", 1),
    # 🩸 El ancla de M14 llevaba pegado el comentario `# ⚖️ @security propone`,
    # que vive bajo la guarda del TAMAÑO (M15), no bajo la del MODO: casaba `0`.
    # El arnés lo dijo en vez de mutar nada, que es justo para lo que está ①.
    ("M14 la verificación no mira el MODO", PREFLIGHT,
     '    if modo != 0o444:\n        return (f"quedó en modo',
     '    if False:\n        return (f"quedó en modo', 1),
    ("M15 la verificación no mira el TAMAÑO", PREFLIGHT,
     "    if st.st_size != n_cuerpo:\n        # ⚖️ @security propone",
     "    if False:\n        # ⚖️ @security propone", 1),
    ("M16 la verificación no mira el DISPOSITIVO", PREFLIGHT,
     "    if st.st_dev != os.fstat(dirfd).st_dev:", "    if False:", 1),
    ("M17 la verificación no mira S_ISREG", PREFLIGHT,
     '    if not statmod.S_ISREG(st.st_mode):\n        return f"no es un fichero regular',
     '    if False:\n        return f"no es un fichero regular', 1),
    # ── la huella: buffer contra disco ───────────────────────────────────────
    ("M18 vuelve a firmar el BUFFER", PREFLIGHT,
     "            return hashlib.sha256(releido).hexdigest()",
     "            return hashlib.sha256(cuerpo).hexdigest()", 1),
    ("M19 relee pero NO compara con lo compuesto", PREFLIGHT,
     "                    if releido != cuerpo:", "                    if False:", 1),
    ("M20 el descriptor vuelve a O_WRONLY", PREFLIGHT,
     "                              os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,",
     "                              os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,", 1),
    # ── la sonda: red final y corte acreditado ───────────────────────────────
    ("M21 la sonda sólo captura Rojo (sin red final)", PREFLIGHT,
     "    except Exception as e:                    # noqa: BLE001 — ver el docstring",
     "    except _NuncaOcurre as e:", 1),
    ("M22 corta SIN acreditar el PID 1", PREFLIGHT,
     "    puede, por_que = acreditar_fn(entorno)",
     '    puede, por_que = True, "sin acreditar"', 1),
    ("M23 el acreditador dice que sí a cualquiera", PREFLIGHT,
     '    if esperado not in texto:', "    if False:", 1),
    ("M24 la lectura no ensambla (una sola read)", PREFLIGHT,
     "    while leidos < tope:", "    while leidos < 0:", 1),
    ("M25 el fichero VACÍO deja de ser un caso", PREFLIGHT,
     "    if not datos:", "    if False:", 1),
    # ── el estreno anclado a su raíz ─────────────────────────────────────────
    ("M26 la señal vuelve a comprobarse por RUTA", PREFLIGHT,
     "        fd = os.open(SENAL_LEDGER, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,",
     "        fd = os.open(SENAL_LEDGER, os.O_RDONLY | os.O_NONBLOCK,", 1),
    ("M27 el piloto no tiene que ser hijo directo", PREFLIGHT,
     "    if padre != raiz_n or base in (\"\", \".\", \"..\") or os.sep in base:",
     "    if False:", 1),
    ("M28 la raíz de ledgers sin O_NOFOLLOW", PREFLIGHT,
     '        rootfd = _abrir_directorio(raiz_ledgers, "raíz de ledgers")',
     '        rootfd = os.open(raiz_ledgers, os.O_RDONLY | os.O_DIRECTORY)', 1),
    ("M29 la señal no exige fichero REGULAR", PREFLIGHT,
     '        if not statmod.S_ISREG(st.st_mode):\n            raise Rojo(f"la señal',
     '        if False:\n            raise Rojo(f"la señal', 1),
    # ── los gates estáticos ──────────────────────────────────────────────────
    ("M30 I4 vuelve a «lleva un $»", TOPOLOGIA,
     "        if valor and not INDIRECCION_PURA.fullmatch(valor):",
     '        if valor and "$" not in valor:', 1),
    ("M31 I8 no mira el ORDEN preflight/uvicorn", TOPOLOGIA,
     '    if "uvicorn" in arranque:', "    if False:", 1),
    ("M32 I8 no mira el healthcheck INERTE", TOPOLOGIA,
     '    if hc.get("disable"):', "    if False:", 1),
]

# Supervivientes DECLARADOS. Si uno MUERE, la declaración era falsa y se dice.
EQUIVALENTES = {
    "M18 vuelve a firmar el BUFFER":
        "lo subsume la comparación `releido == cuerpo` de dos líneas antes: firmar "
        "uno u otro son LOS MISMOS BYTES por construcción. La guarda del eje es "
        "M19; firmar lo releído hace el código literal, no discrimina.",
    "M4b SOLO el fsync ① se traga":
        "el `fsync` ② es un punto de control POSTERIOR sobre el MISMO descriptor y "
        "levanta el mismo EIO. La diferencia sólo vive en la ventana de un corte, "
        "que ningún test puede ejercitar.",
}

# `_fsync_tragado` y `_NuncaOcurre` los inyecta el arnés: son andamios del
# mutante, no del producto, y por eso no viven en `pilot_preflight.py`.
ANDAMIOS = {
    "_fsync_tragado": "def _fsync_tragado(fd):\n    try:\n        os.fsync(fd)\n"
                      "    except OSError:\n        pass\n\n\n",
    "_NuncaOcurre": "class _NuncaOcurre(Exception):\n    pass\n\n\n",
}


def corre(py: str) -> tuple[list[str], int, str]:
    r = subprocess.run([py, "-m", "pytest", os.path.join(RAIZ, "tests", "pilot"),
                        "-q", "--no-header", "-p", "no:cacheprovider", "--tb=no"],
                       capture_output=True, text=True, cwd=RAIZ)
    muertos = sorted(set(re.findall(r"^FAILED (\S+)", r.stdout, re.M)))
    m = re.search(r"(\d+) passed", r.stdout)
    return muertos, (int(m.group(1)) if m else -1), r.stdout


def main(argv: list[str]) -> int:
    py = sys.executable
    if "--python" in argv:
        py = argv[argv.index("--python") + 1]

    base = {f: io.open(f, encoding="utf-8").read() for f in {m[1] for m in MUTANTES}}
    muertos, verdes, salida = corre(py)
    print(f"⊕ CONTROL sin mutar: {verdes} passed · {len(muertos)} muertos")
    if muertos:
        print(salida[-2000:]); return 1

    fallos = []
    for mid, fichero, ancla, nuevo, n_esperado in MUTANTES:
        s = base[fichero]
        if s.count(ancla) != n_esperado:
            fallos.append(f"{mid}: el ancla casa {s.count(ancla)} y se declararon {n_esperado}")
            print(f"⚠️  {mid:46} ANCLA ROTA ({s.count(ancla)}, declaradas {n_esperado})")
            continue
        mutado = s.replace(ancla, nuevo)
        if mutado == s:
            fallos.append(f"{mid}: MUTANTE NO-OP — no cambia el fuente")
            continue
        for simbolo, cuerpo in ANDAMIOS.items():
            if simbolo in mutado and f"def {simbolo}" not in mutado \
               and f"class {simbolo}" not in mutado:
                mutado = mutado.replace("def _retirar(", cuerpo + "def _retirar(", 1)
        io.open(fichero, "w", encoding="utf-8").write(mutado)
        try:
            caen, verdes_m, _ = corre(py)
        finally:
            io.open(fichero, "w", encoding="utf-8").write(s)
        equivalente = mid in EQUIVALENTES
        if caen:
            estado = "MUERE"
            if equivalente:
                fallos.append(f"{mid}: MURIÓ y lo declaré EQUIVALENTE — la "
                              f"declaración era FALSA: {EQUIVALENTES[mid]}")
        else:
            estado = "⊜ SOBREVIVE" if equivalente else "🔴 SOBREVIVE"
            if not equivalente:
                fallos.append(f"{mid}: SOBREVIVE — guarda sin vigilante")
        print(f"{estado:12} {mid:46} {verdes_m} passed · mata {len(caen)}")
        for t in caen:
            print(f"                 └─ {t.split('::')[-1]}")
        if equivalente and not caen:
            print(f"             ↑ EQUIVALENCIA DECLARADA: {EQUIVALENTES[mid]}")

    print()
    if fallos:
        print("🔴 ARNÉS INCOMPLETO:")
        for f in fallos:
            print("  ·", f)
        return 1
    print(f"✅ los {len(MUTANTES)} mutantes resueltos · restaurado")
    muertos, verdes, _ = corre(py)
    print(f"✅ tras restaurar: {verdes} passed · {len(muertos)} muertos")
    return 0 if not muertos else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
