#!/usr/bin/env python3
"""Corre un ⊖ imponiendo lo que se olvida — porque yo lo olvidé seis veces en una noche.

Un falsador negativo sólo vale si TRES cosas son ciertas a la vez, y las tres se pierden
en silencio:

  ① la línea base está VERDE — si ya estaba roja, el mutante no mide nada
  ② la mutación se APLICÓ de verdad — un ancla que no encaja muta cero y deja el verde
    intacto, y ese verde se lee como «el arnés no discrimina» cuando el arnés está bien
  ③ el resultado se mide por el CÓDIGO DE SALIDA, no por la última línea impresa

Los seis fallos de una sola noche (2026-08-30/31), todos míos y todos con el mismo
síntoma —un verde que no significa nada—:

    leer la salida en vez del rc
      · `$?` tras un pipeline midió `tail` en vez de python  -> «el gate no bloquea»
      · PIPESTATUS perdido al cruzar un `cd`                 -> publiqué un rc vacío
      · leí «todo verde» y no el rc                          -> «el test es teatro»
    un ⊖ que no muta nada
      · `sed` sin encaje                                     -> «el arnés no discrimina»
      · parcheé un módulo que el arnés recarga después       -> midió el .git real
      · ancla rota por el escape del shell                   -> 0 mutaciones

Ninguno se cazó por diseño: se cazaron por insistencia. Esto los convierte en imposibles.

USO (módulo):

    from mutar import falsa
    falsa("servicio.py", [("if hace > TOPE:", "if False:", "centinela desarmado")],
          ["pytest", "-q", "tests/pytest/test_x.py"])

USO (CLI, para los ⊖ que viven en bash):

    python3 tests/mutar.py --fichero servicio.py \
        --comando 'pytest -q tests/pytest/test_x.py' \
        --mutante 'if hace > TOPE:=>if False:=>centinela desarmado'

SUPERVIVIENTES JUSTIFICADOS: un mutante puede sobrevivir con razón —`hmac.compare_digest`
-> `==` sobrevive porque un test funcional no ve un canal lateral de tiempo—. Se declaran
con `sobrevive=True` y salen listados. Un mutation score sin la lista de supervivientes
justificados no es una medida, es una nota.
"""
from __future__ import annotations

import argparse
import hashlib
import pathlib
import shlex
import shutil
import subprocess
import tempfile
import time
import sys


class FalsadorInservible(AssertionError):
    """El ⊖ no pudo medir. NO es «el código está mal»: es «la medida no vale»."""


def _huella(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _rc(comando: list[str], cwd: pathlib.Path) -> int:
    """El código de salida, y NADA MÁS. Sin pipes, sin `tail`, sin leer la última línea:
    los tres fallos de esa familia venían de mirar texto en vez de mirar esto."""
    return subprocess.run(comando, cwd=cwd, capture_output=True, text=True).returncode


def _diff_id(repo: pathlib.Path) -> str:
    """Huella del estado del árbol de trabajo, para poder afirmar que no se movió."""
    r = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                       capture_output=True, text=True)
    return hashlib.sha256((r.stdout or "").encode()).hexdigest()


def _copia_aislada(repo: pathlib.Path, destino: pathlib.Path, sha: str | None) -> None:
    """Deja en `destino` una copia sobre la que mutar. NUNCA se toca `repo`.

    Con `sha`, la copia sale de un SHA CONGELADO (`git archive`): el ejercicio se
    hace contra un objeto inmutable y dos corridas del mismo canon son comparables
    aunque el árbol de trabajo haya cambiado en medio. Sin `sha`, se copia el árbol
    tal cual, que es lo que hace falta mientras se desarrolla.
    """
    destino.mkdir(parents=True, exist_ok=True)
    if sha:
        tar = subprocess.run(["git", "-C", str(repo), "archive", sha],
                             capture_output=True)
        if tar.returncode != 0:
            raise FalsadorInservible(f"no pude exportar el SHA {sha}")
        x = subprocess.run(["tar", "-x", "-C", str(destino)], input=tar.stdout,
                           capture_output=True)
        if x.returncode != 0:
            raise FalsadorInservible(f"no pude desempaquetar el SHA {sha}")
        return
    shutil.copytree(repo, destino, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc",
                                                  ".venv*", "node_modules"))


def falsa(fichero, mutantes, comando, cwd=".", verbose=True, sha=None):
    """mutantes: lista de (ancla, reemplazo, nombre[, sobrevive_con_razon]).

    ⚠️ MUTA UNA COPIA, NUNCA EL REPO. La versión anterior escribía sobre el fichero
    de verdad y lo restauraba al final, y eso tiene dos averías que ya se cobraron
    su precio en esta máquina:

      · si el proceso MUERE mutado —y aquí los mata el segador de memoria— el repo
        se queda con el mutante dentro, y el siguiente que mire ve código que nadie
        escribió;
      · si alguien EDITA el fichero mientras corre, la restauración final escribe
        los bytes viejos ENCIMA de la edición y se la come en silencio.

    Con la copia, las dos desaparecen por construcción, y además dos ejercicios
    pueden correr a la vez sin pisarse: cada uno tiene su directorio. El `finally`
    borra la copia pase lo que pase, y al terminar se AFIRMA que el original sigue
    con la misma huella y el árbol con el mismo `git status`.
    """
    repo = pathlib.Path(cwd).resolve()
    original = (repo / fichero).resolve()
    h_original = _huella(original)
    diff_original = _diff_id(repo)

    if isinstance(comando, str):
        comando = shlex.split(comando)

    # PADRE DETERMINISTA + BARRIDO DE HUÉRFANOS. El `finally` limpia la salida
    # ordenada, pero aquí a los procesos los mata el segador de memoria con SIGKILL
    # y ese camino NO ejecuta `finally`: medido, dos copias quedaron colgadas tras
    # un corte. Un temporal huérfano de un repo entero no es basura inocente —lleva
    # dentro una copia del árbol— así que se barren los del arranque anterior antes
    # de empezar. «Limpia al terminar» no basta cuando terminar no está garantizado.
    padre = pathlib.Path(tempfile.gettempdir()) / "mutar-copias"
    padre.mkdir(parents=True, exist_ok=True)
    ahora = time.time()
    for viejo_dir in padre.glob("m-*"):
        try:
            if ahora - viejo_dir.stat().st_mtime > 3600:
                shutil.rmtree(viejo_dir, ignore_errors=True)
        except OSError:
            pass
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="m-", dir=str(padre)))
    try:
        _copia_aislada(repo, tmp, sha)
        p = tmp / fichero
        if not p.exists():
            raise FalsadorInservible(f"{fichero} no está en la copia aislada")
        contenido = p.read_bytes()
        h0 = _huella(p)

        base = _rc(comando, tmp)
        if base != 0:
            raise FalsadorInservible(
                f"la LÍNEA BASE ya falla (rc={base}) EN LA COPIA. Un mutante sobre un "
                f"rojo previo no mide nada: arregla la base o el ⊖ es decorativo.")
        if verbose:
            print("  ⊕ línea base (copia aislada): rc=0")

        filas, murieron, sobrevivieron = [], 0, []
        # EN SERIE, y dicho a propósito: dos mutaciones a la vez sobre la misma
        # copia se contaminan, y el rc dejaría de poder atribuirse a una sola.
        for m in mutantes:
            ancla, reemplazo, nombre = m[0], m[1], m[2]
            justificado = m[3] if len(m) > 3 else False
            texto = contenido.decode()
            n = texto.count(ancla)
            if n != 1:
                raise FalsadorInservible(
                    f"«{nombre}»: el ancla aparece {n} veces, no 1. Con 0 la mutación NO "
                    f"SE APLICA y el verde se lee como «no detecta»; con 2+ mutas de más "
                    f"y no sabes cuál pesó. Ancla: {ancla!r}")
            p.write_text(texto.replace(ancla, reemplazo))
            if _huella(p) == h0:
                raise FalsadorInservible(
                    f"«{nombre}»: el fichero NO CAMBIÓ tras mutar (¿reemplazo idéntico "
                    f"al ancla?). Un ⊖ que no muta nada no es un ⊖ que no detecta: es un "
                    f"⊖ que no se corrió.")
            # SE PRECOMPILA ANTES DE CORRER. Un mutante que no compila muere por
            # `SyntaxError` sin ejecutar una sola aserción, y con `murio = rc != 0`
            # eso se contabilizaba como MUERTE: el canon acreditaba un falsador que
            # nunca llegó a correr. Medido en el mutante de `res_ok`, que cerraba con
            # `)` un `[` — «✓ muere» durante toda su vida sin haber probado nada.
            #
            # Un mutante que no compila no es un falsador que detecta: es un ⊖ roto,
            # y eso es INSERVIBLE, no un aprobado.
            try:
                compile(p.read_text(), str(p), "exec")
            except SyntaxError as e:
                p.write_bytes(contenido)
                raise FalsadorInservible(
                    f"«{nombre}»: el mutante NO COMPILA ({e.msg}, línea {e.lineno}). "
                    f"Moriría por SyntaxError sin ejercitar el falsador, y eso se "
                    f"contaría como muerte. Reemplazo: {reemplazo!r}") from None
            for pyc in tmp.rglob("__pycache__"):
                for f in pyc.glob("*.pyc"):
                    f.unlink(missing_ok=True)
            rc = _rc(comando, tmp)
            p.write_bytes(contenido)
            if _huella(p) != h0:
                raise FalsadorInservible(f"«{nombre}»: la copia no volvió a su estado.")
            # SÓLO `rc=1` MATA. Los demás códigos de pytest no son un falsador que
            # detecta:
            #   2 interrumpido · 3 error interno · 4 mal uso de la línea de órdenes ·
            #   5 no recogió NINGÚN test
            # El 5 es el peor de los cuatro, porque describe exactamente el estado que
            # este arnés existe para impedir: cero tests corridos, contabilizados como
            # aprobado. Un fallo de recogida o de import entra por ahí.
            if rc not in (0, 1):
                raise FalsadorInservible(
                    f"«{nombre}»: pytest salió con rc={rc}, que no es «falló un test» "
                    f"(1) ni «pasaron todos» (0). "
                    f"{ {2: 'interrumpido', 3: 'error interno', 4: 'mal uso', 5: 'no recogió NINGÚN test'}.get(rc, 'código desconocido') }"
                    ": el falsador no llegó a discriminar, así que esto no acredita nada.")
            murio = rc == 1
            if murio:
                murieron += 1
            elif not justificado:
                sobrevivieron.append(nombre)
            filas.append((nombre, rc, murio, justificado))

        for pyc in tmp.rglob("__pycache__"):
            for f in pyc.glob("*.pyc"):
                f.unlink(missing_ok=True)
        if _rc(comando, tmp) != 0:
            raise FalsadorInservible("tras restaurar, la base ya no pasa en la copia.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # EL REPO NO SE HA MOVIDO, Y SE AFIRMA. Sin esto, «muto una copia» sería una
    # promesa del docstring; con esto es una medida.
    if _huella(original) != h_original:
        raise FalsadorInservible(
            f"{fichero} CAMBIÓ durante el ejercicio: la copia no estaba aislada.")
    if _diff_id(repo) != diff_original:
        raise FalsadorInservible(
            "el `git status` del repo cambió durante el ejercicio: algo tocó el árbol.")

    if verbose:
        for nombre, rc, murio, just in filas:
            marca = "✓ muere" if murio else ("· sobrevive CON RAZÓN" if just else "‼ SOBREVIVE")
            print(f"  ⊖ {nombre:<44} rc={rc}  {marca}")
        print("  ⊕ original intacto (huella y `git status` sin cambios)")
        vivos_just = [n for n, _, mu, ju in filas if not mu and ju]
        if vivos_just:
            print(f"  supervivientes justificados ({len(vivos_just)}): {', '.join(vivos_just)}")
    return {"base_ok": True, "murieron": murieron, "sobrevivieron": sobrevivieron,
            "filas": filas}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fichero", required=True)
    ap.add_argument("--comando", required=True)
    ap.add_argument("--cwd", default=".")
    ap.add_argument("--mutante", action="append", required=True,
                    help="ancla=>reemplazo=>nombre[=>sobrevive]")
    a = ap.parse_args()
    ms = []
    for crudo in a.mutante:
        partes = crudo.split("=>")
        if len(partes) < 3:
            print(f"· mutante mal formado: {crudo!r} (ancla=>reemplazo=>nombre)", file=sys.stderr)
            return 2
        ms.append((partes[0], partes[1], partes[2],
                   len(partes) > 3 and partes[3].strip().lower() in ("1", "si", "sí", "true")))
    try:
        r = falsa(a.fichero, ms, a.comando, a.cwd)
    except FalsadorInservible as e:
        print(f"· ⊖ INSERVIBLE: {e}", file=sys.stderr)
        return 2
    if r["sobrevivieron"]:
        print(f"· sobreviven sin justificar: {', '.join(r['sobrevivieron'])}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
