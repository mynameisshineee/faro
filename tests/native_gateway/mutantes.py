#!/usr/bin/env python3
"""Mutantes DISCRIMINANTES del techo de transporte.

Una suite verde no prueba que sus aserciones muerdan. Esto rompe el techo de
diez maneras concretas y exige que la suite se ponga ROJA en cada una; si un
mutante sobrevive, el falsador que deberia cazarlo es decorativo.

Trae su propio META-FALSADOR: el arbol SIN mutar tiene que salir VERDE. Sin el,
un error de montaje (ruta mala, import roto) pintaria los seis mutantes como
«muertos» sin haber probado nada.

    python3 tests/native_gateway/mutantes.py

Sale `0` si los diez mueren y el control vive; `1` si sobrevive alguno.

Una muerte solo cuenta si es VALIDA: `rc == 1` (pytest: fallo de TEST), sin
errores de montaje/coleccion y —cuando el mutante declara juez— con ese nodeid
entre los `FAILED`. El meta-control se autotesta con `mutantes.py --meta`.

⚠️ Corre la suite entera OCHO veces mas el control: NO es un falsador focal.
Para un mutante suelto contra el fichero focal:

    python3 tests/native_gateway/mutantes.py M7-retiene-los-mensajes-en-vez-de-fundirlos
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


RAIZ = Path(__file__).resolve().parents[2]
PY = sys.executable

# (nombre, original, mutado) — cada uno rompe UNA propiedad, no varias, para que
# el mutante que sobreviva senale un falsador concreto y no una zona.
MUTANTES: list[tuple[str, str, str]] = [
    ("M1-inclusivo-a-exclusivo",
     "            if leidos + len(trozo) > self.max_bytes:\n"
     "                return [], 413",
     "            if leidos + len(trozo) >= self.max_bytes:\n"
     "                return [], 413"),
    # 🩸 M2 y M7 mutaban DENTRO de `_pre_leer(self, receive)` metiendo una
    # referencia a `scope`, que ahi NO EXISTE: morian por `NameError` antes de
    # ejercitar nada. Un mutante que revienta al montarse no prueba que su juez
    # muerda — prueba que el interprete sabe leer. Rediseñados para que EJECUTEN
    # el sujeto entero y mueran por el juez semantico que les toca.
    # M2 se muda a `__call__`, que es donde `scope` vive: la decision pasa a
    # salir de `Content-Length` en vez de los bytes contados. Muere en
    # `test_content_length_no_decide`, que manda 32 bytes REALES con techo 16 y
    # `declarada` en `[b"32", b"1", b"999999999", None]`: con `b"1"` el mutante
    # no corta y devuelve 200 donde el juez exige 413.
    ("M2-cuenta-la-cabecera-en-vez-de-los-bytes",
     "            if self.read_timeout_s is None:\n"
     "                reproducidos, status = await self._pre_leer(receive)\n"
     "            else:\n"
     "                async with asyncio.timeout(self.read_timeout_s):\n"
     "                    reproducidos, status = await self._pre_leer(receive)",
     "            _declarada = int(dict(scope.get(\"headers\") or []).get("
     "b\"content-length\", b\"0\") or 0)\n"
     "            if self.read_timeout_s is None:\n"
     "                reproducidos, status = await self._pre_leer(receive)\n"
     "            else:\n"
     "                async with asyncio.timeout(self.read_timeout_s):\n"
     "                    reproducidos, status = await self._pre_leer(receive)\n"
     "            status = 413 if _declarada > self.max_bytes else None"),
    ("M3-entrega-el-chunk-que-se-pasa",
     "            if leidos + len(trozo) > self.max_bytes:\n"
     "                return [], 413",
     "            if leidos + len(trozo) > self.max_bytes:\n"
     "                pass"),
    ("M4-el-413-lleva-cuerpo",
     "        await send({\"type\": \"http.response.body\", \"body\": b\"\"})",
     "        await send({\"type\": \"http.response.body\", "
     "\"body\": b'{\"code\":\"TOO_LARGE\"}'})"),
    ("M5-la-desconexion-cuenta-como-exceso",
     "                cola.append(mensaje)\n                break",
     "                return [], 413"),
    ("M6-sin-techo",
     "            if leidos + len(trozo) > self.max_bytes:\n"
     "                return [], 413",
     "            if False and leidos + len(trozo) > self.max_bytes:\n"
     "                return [], 413"),
    # M7 y M8 nacen de dos medidas EN ROJO sobre la version que guardaba los
    # mensajes tal cual: `×192,7` de amplificacion con chunks de `1 B`, y
    # `200.000` chunks vacios saliendo `200` con techo `16`.
    # M7 se queda donde estaba, pero retiene sobre `self` —que SI esta en el
    # scope de `_pre_leer`— en vez de sobre `scope`. Guarda cada `dict` de
    # mensaje uno a uno, que es justo lo que la version vieja hacia y lo que
    # medimos en `×192,7`. Su juez declarado es
    # `test_muchos_chunks_no_amplifican_la_memoria`, que corre 64Ki mensajes y
    # exige un pico por debajo de `8×` el techo (medido con el mutante:
    # `12.748.198 B` para un cuerpo de `65.536 B`).
    ("M7-retiene-los-mensajes-en-vez-de-fundirlos",
     "            leidos += len(trozo)",
     "            leidos += len(trozo)\n"
     "            self.__dict__.setdefault(\"_retenidos\", []).append(mensaje)"),
    ("M8-sin-tope-de-chunks-vacios",
     "                if vacios > self.max_empty_chunks:",
     "                if False and vacios > self.max_empty_chunks:"),
    ("M9-sin-tope-de-mensajes",
     "            if mensajes > self.max_chunks:",
     "            if False and mensajes > self.max_chunks:"),
    ("M10-sin-plazo-de-lectura",
     "                async with asyncio.timeout(self.read_timeout_s):",
     "                async with asyncio.timeout(None):"),
]


# ── JUEZ CAUSAL DECLARADO, POR MUTANTE ──────────────────────────────────────────
#
# 🩸 P2 de la reauditoria de `bf5e0f5`: el arnes corria el fichero focal ENTERO con
# `-x` y declaraba que M2 moria en `test_content_length_no_decide`. Medido: bajo
# `-x` moria en `test_limite_inclusivo[17-True]` y NUNCA llegaba al juez que el
# commit le atribuia. El mutante era bueno; la ATRIBUCION era falsa.
# 🔑 «Muerto» y «muerto POR ESTO» son afirmaciones distintas, y sale la misma linea
# verde para las dos. Un arnes que solo mira el `rc` no puede separarlas, asi que
# el nodeid causal se DECLARA y se EXIGE en la salida.
JUEZ_CAUSAL: dict[str, str] = {
    # Con el CASO, no solo la funcion: el encargo fija `[1]` (`Content-Length: 1`
    # con 32 bytes reales y techo 16), que es el par exacto donde la cabecera y
    # los bytes discrepan hacia el lado peligroso — el mutante NO corta y devuelve
    # 200 donde el juez exige 413. `[None]` tambien cae, pero `[1]` es el que
    # nombra la propiedad.
    "M2-cuenta-la-cabecera-en-vez-de-los-bytes":
        "tests/native_gateway/test_raw_body_limit.py::test_content_length_no_decide[1]",
    "M7-retiene-los-mensajes-en-vez-de-fundirlos":
        "tests/native_gateway/test_raw_body_limit.py"
        "::test_muchos_chunks_no_amplifican_la_memoria",
}


def _monta(destino: Path) -> None:
    # La suite del gateway reutiliza las autoridades estrictas del arnes del
    # Journal. El montaje temporal tiene que copiar esa dependencia completa:
    # si no, el meta-falsador muere en collection por ``ModuleNotFoundError`` y
    # ningun mutante llega a ejercitar el techo de transporte.
    for nombre in ("native_gateway.py", "coordination.py", "ledger_parse.py"):
        shutil.copy2(RAIZ / nombre, destino / nombre)
    shutil.copytree(RAIZ / "tests" / "native_gateway",
                    destino / "tests" / "native_gateway")
    (destino / "tests" / "journal").mkdir(parents=True)
    shutil.copy2(RAIZ / "tests" / "journal" / "_arnes.py",
                 destino / "tests" / "journal" / "_arnes.py")


FOCAL = "tests/native_gateway/test_raw_body_limit.py"


TOPE_S = 180


def _corre(destino: Path, objetivo: str = "tests/native_gateway") -> tuple[int, str]:
    """Corre la suite. `rc = -1` significa CUELGUE, que no es lo mismo que rojo.

    Sin este `timeout` un mutante que rompa un plazo cuelga el ARNES entero:
    medido con `M10-sin-plazo-de-lectura`, que dejo la corrida parada hasta
    matarla a mano. Un mutante que cuelga la suite esta muerto —la suite no
    pasa— pero se etiqueta aparte, porque un cuelgue dice ademas que ALGUN
    falsador no tiene plazo propio, y eso es un defecto del test, no del mutante.
    """
    try:
        r = subprocess.run(
            [PY, "-m", "pytest", objetivo, "-q", "-x", "-p", "no:cacheprovider"],
            cwd=destino, capture_output=True, text=True, timeout=TOPE_S)
    except subprocess.TimeoutExpired:
        return -1, f"CUELGUE: la suite no termino en {TOPE_S}s"
    # ENTERA, no recortada: el recorte a 400 caracteres era lo que dejaba fuera
    # de la vista el `NameError` que este arnes tiene ahora que cazar. Se recorta
    # AL IMPRIMIR, que es donde el recorte no cuesta nada.
    return r.returncode, r.stdout + r.stderr


# ── META-CONTROL DE LA MUERTE ───────────────────────────────────────────────────
#
# 🩸 EL DEFECTO QUE ESTO CIERRA (auditoria de `dd2bc36`): `M2` y `M7` morian por
# `NameError` —metian `scope` donde no existe— y el arnes los contaba como
# muertos. Un `rc != 0` dice que la suite se puso ROJA; NO dice por que. Un
# mutante que revienta al MONTARSE no ha ejercitado al sujeto, asi que su muerte
# no acredita a ningun juez: acredita al interprete.
#
# ⚠️ Y el modo de fallo es de los caros, porque apunta al lado que TRANQUILIZA:
# un mutante roto sale «muerto ✅» y sube el marcador. El error se lee como salud.
MUERTE_INVALIDA = (
    "NameError",            # el mutante nombra algo que no esta en su scope
    "SyntaxError",          # el remiendo no compila
    "IndentationError",     # el remiendo entro con la sangria de otro bloque
    "ImportError",
    "ModuleNotFoundError",  # el montaje del arbol temporal quedo incompleto
    "ERROR collecting",     # pytest ni llego a ejecutar
    "errors during collection",
    "INTERNALERROR",
)


def _muerte_valida(rc: int, salida: str, juez: str = "") -> tuple[bool, str]:
    """Una muerte vale si la suite falla EJECUTANDO, no montandose.

    🩸 P1 de la reauditoria de `bf5e0f5`: esta funcion solo buscaba nombres de
    errores de infraestructura, y una suite VERDE no contiene ninguno — asi que
    devolvia `True` para un mutante que SOBREVIVE. El meta-control que existe
    para evitar falsos verdes producia uno. Reproducido por el auditor con un
    mutante que solo cambia un comentario: «SURVIVOR-control muerte VALIDA (rc=0)».
    🔑 La ausencia de una causa mala no es la presencia de la causa buena. Ahora
    la validez es una CONJUNCION: `rc == 1` (pytest: fallo de TEST) **y** ninguna
    aguja de montaje **y**, si el mutante declara juez, ese nodeid entre los
    `FAILED`.
    `rc == 0` es SUPERVIVENCIA. `rc == -1` es CUELGUE. `rc in (2,3,4,5)` son
    errores de uso/coleccion/interrupcion de pytest, no muertes.
    """
    if rc == 0:
        return False, "rc=0: el mutante SOBREVIVE, no ha muerto"
    if rc == -1:
        return False, "CUELGUE: la suite no termino"
    if rc != 1:
        return False, f"rc={rc}: pytest no reporta fallo de test (uso/coleccion/interrupcion)"
    for aguja in MUERTE_INVALIDA:
        if aguja in salida:
            return False, aguja
    if juez and f"FAILED {juez}" not in salida:
        return False, f"murio, pero NO por su juez declarado ({juez})"
    return True, ""


# ⚠️ COTA DE ESTA COMPROBACION, medida y no deducida. El nodeid acredita que EL
# JUEZ DECLARADO FALLO; NO acredita que sea la unica causa ni la verdadera.
# Falsado por mi mano: apuntando el juez de M2 a `test_limite_inclusivo` —un test
# que M2 TAMBIEN mata— la comprobacion pasa igual. O sea que atrapa el P2 real
# (atribuir a un juez que ni se ejecuto, porque ahora se ejecuta siempre) pero no
# atrapa una atribucion a un juez que el mutante mata de paso.
# 🔑 Cerrar eso pide un mutante de RADIO UNO, y eso es rediseñar los mutantes, no
# el arnes. Queda declarado, no tapado.


# Las DOS mutaciones rotas tal y como estaban en `74a730d`/`dd2bc36`. No son
# historia: son el CONTROL POSITIVO del meta-control. Sin ellas, `_muerte_valida`
# podria devolver `True` siempre y nadie lo notaria — que es exactamente la forma
# del defecto que este bloque existe para impedir.
# El SUPERVIVIENTE deliberado: cambia UN comentario y nada mas, asi que la focal
# entera sigue verde (`rc=0`). El meta-control tiene que RECHAZARLO. Es el negativo
# que la reauditoria exige, y el que separa «detecta errores de montaje» de
# «acredita que hubo muerte»: la version anterior lo daba por muerte valida.
SUPERVIVIENTE: list[tuple[str, str, str]] = [
    ("SURVIVOR-solo-toca-un-comentario",
     "                # reenviarlo intacto: no es un exceso y no lo convertimos en uno.",
     "                # reenviarlo intacto: no es un exceso (mutante inocuo)."),
]


ROTOS_HISTORICOS: list[tuple[str, str, str]] = [
    ("M2-viejo-rompia-por-NameError",
     "            leidos += len(trozo)",
     "            leidos = int(dict(scope.get(\"headers\") or {}).get("
     "b\"content-length\", b\"0\") or 0)"),
    ("M7-viejo-rompia-por-NameError",
     "            leidos += len(trozo)",
     "            leidos += len(trozo)\n"
     "            scope.setdefault(\"_retenidos\", []).append(mensaje)"),
]


def _meta(mutantes: list[tuple[str, str, str]], espera_valida: bool) -> int:
    """Corre mutaciones y exige un veredicto CONCRETO del meta-control."""
    malos = 0
    for nombre, viejo, mutado in mutantes:
        juez = JUEZ_CAUSAL.get(nombre, "")
        with tempfile.TemporaryDirectory() as tmp:
            destino = Path(tmp) / "mut"
            destino.mkdir()
            _monta(destino)
            objetivo = destino / "native_gateway.py"
            fuente = objetivo.read_text(encoding="utf-8")
            if fuente.count(viejo) != 1:
                print(f"{nombre:44} ANCLA NO UNICA  🔴")
                malos += 1
                continue
            objetivo.write_text(fuente.replace(viejo, mutado, 1), encoding="utf-8")
            rc, salida = _corre(destino, juez or FOCAL)
            valida, motivo = _muerte_valida(rc, salida, juez)
            if valida == espera_valida:
                detalle = "muerte VALIDA" if valida else f"RECHAZADA por `{motivo}`"
                print(f"{nombre:44} {detalle}  ✅  (rc={rc})")
            else:
                print(f"{nombre:44} el meta-control dijo "
                      f"valida={valida} ({motivo or 'sin motivo'}), "
                      f"se esperaba {espera_valida}  🔴  (rc={rc})")
                malos += 1
    return malos


def _autotest_meta() -> int:
    """⊕/⊖ del meta-control, y sin el ⊕ no mide nada.

    ⊖a las dos mutaciones rotas de la auditoria (mueren por `NameError`) → RECHAZADAS.
    ⊖b el SUPERVIVIENTE deliberado (`rc=0`) → RECHAZADO. Es el negativo que faltaba:
       sin el, `_muerte_valida` daba por buena una suite VERDE, que es un falso
       verde producido por el control que existe para evitarlos.
    ⊕  las dos rediseñadas tienen que pasar por VALIDAS — si `_muerte_valida`
       devolviera `False` siempre, los dos ⊖ saldrian verdes igual y el arnes no
       distinguiria «rechaza lo roto» de «rechaza todo».
    """
    print("meta-control ⊖a · las dos mutaciones ROTAS deben ser rechazadas")
    malos = _meta(ROTOS_HISTORICOS, espera_valida=False)
    print("meta-control ⊖b · el SUPERVIVIENTE (rc=0) debe ser rechazado")
    malos += _meta(SUPERVIVIENTE, espera_valida=False)
    print("meta-control ⊕ · las dos REDISEÑADAS deben pasar por validas")
    vivos = [m for m in MUTANTES
             if m[0] in ("M2-cuenta-la-cabecera-en-vez-de-los-bytes",
                         "M7-retiene-los-mensajes-en-vez-de-fundirlos")]
    malos += _meta(vivos, espera_valida=True)
    print("\nmeta-control: " + ("OK ✅" if not malos else f"{malos} desviacion(es) 🔴"))
    return 1 if malos else 0


def main(argv: list[str] | None = None) -> int:
    """Sin argumentos corre los ocho; con nombres, SOLO esos.

    El filtro existe para poder falsar UN mutante sin pagar la suite completa
    nueve veces, que es lo que pide un host cargado.
    """
    pedidos = list(argv or [])
    if "--meta" in pedidos:
        return _autotest_meta()
    seleccion = [m for m in MUTANTES if not pedidos or m[0] in pedidos]
    if pedidos and len(seleccion) != len(pedidos):
        conocidos = ", ".join(m[0] for m in MUTANTES)
        print(f"mutante desconocido. Los que hay: {conocidos}")
        return 64
    fallos: list[str] = []
    colgados: list[str] = []
    invalidos: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "control"
        base.mkdir()
        _monta(base)
        rc, salida = _corre(base, FOCAL if pedidos else "tests/native_gateway")
        if rc != 0:
            print("META-FALSADOR EN ROJO: el arbol SIN mutar no pasa.\n"
                  + salida[-400:])
            return 1
        print("meta-falsador ....... control sin mutar VERDE  ✅")

    for nombre, viejo, mutado in seleccion:
        with tempfile.TemporaryDirectory() as tmp:
            destino = Path(tmp) / "mut"
            destino.mkdir()
            _monta(destino)
            objetivo = destino / "native_gateway.py"
            fuente = objetivo.read_text(encoding="utf-8")
            if fuente.count(viejo) != 1:
                print(f"{nombre:44} ANCLA NO UNICA ({fuente.count(viejo)})  🔴")
                fallos.append(nombre)
                continue
            objetivo.write_text(fuente.replace(viejo, mutado, 1), encoding="utf-8")
            # Si el mutante DECLARA juez causal, se corre CONTRA EL. Correr el
            # fichero focal con `-x` y atribuir la muerte al juez declarado fue
            # justo el P2 de la reauditoria: `-x` corta en el primer rojo, que
            # para M2 era `test_limite_inclusivo[17-True]`, y el juez declarado
            # ni se ejecutaba. Dirigirlo cuesta menos y ademas lo acredita.
            # Sin juez declarado se mantiene lo de antes: focal con seleccion,
            # suite entera sin ella, y escalada si sobrevive en el subconjunto.
            juez = JUEZ_CAUSAL.get(nombre, "")
            if juez:
                objetivo_run, alcance = juez, "juez"
            else:
                objetivo_run = FOCAL if pedidos else "tests/native_gateway"
                alcance = "focal" if pedidos else "suite"
            rc, salida = _corre(destino, objetivo_run)
            if rc == 0 and pedidos and not juez:
                rc, salida = _corre(destino)
                alcance = "focal->suite"
            if rc == 0:
                print(f"{nombre:44} SOBREVIVE  🔴  ({alcance})")
                fallos.append(nombre)
            elif rc == -1:
                print(f"{nombre:44} muerto POR CUELGUE  🟡  ({alcance})")
                colgados.append(nombre)
            else:
                valida, motivo = _muerte_valida(rc, salida, JUEZ_CAUSAL.get(nombre, ""))
                if not valida:
                    # NO cuenta como muerto: el mutante no llego a ejercitar al
                    # sujeto, asi que no acredita a su juez.
                    print(f"{nombre:44} MUERTE INVALIDA (`{motivo}`)  🔴  ({alcance})")
                    invalidos.append(nombre)
                    fallos.append(nombre)
                else:
                    print(f"{nombre:44} muerto     ✅  ({alcance})")

    print(f"\n{len(seleccion) - len(fallos)}/{len(seleccion)} mutantes muertos")
    if colgados:
        print(f"🟡 {len(colgados)} colgaron la suite en vez de ponerla roja: "
              f"{', '.join(colgados)} — hay un falsador sin plazo propio")
    if invalidos:
        print(f"🔴 {len(invalidos)} murieron SIN ejercitar al sujeto: "
              f"{', '.join(invalidos)} — su rojo no acredita a ningun juez")
    return 1 if fallos else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
