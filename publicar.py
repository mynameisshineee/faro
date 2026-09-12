#!/usr/bin/env python3
"""`llmi post` — publicar una entrada VALIDADA, sin depender del servicio.

## Por qué existe, con la medida delante

`POST /append` ya rechaza una entrada que no nombra a nadie —«'to' vacío: una entrada
sin destinatario no la lee nadie»— desde hace semanas. Y el 2026-08-11, sobre la red
viva:

    POST /append ......................    47 llamadas
    entradas indexadas ................ 103.257
    entradas que no nombran a nadie ...     34 %

**El 0,05 % pasa por la puerta.** El resto escribe con `cat >>`, que es lo que
documenta el protocolo y no valida nada. O sea que el 34 % no era conducta que
corregir con avisos: era una puerta puesta donde no está el camino.

## Las cinco cosas que comprueba, y por qué cada una

1. **Destinatario que RESUELVE en el censo.** Sin él la entrada no cae en ninguna
   bandeja: se publica en un canal que ya nadie lee entero. Un nombre mal tecleado
   (`securty`) es peor que ninguno — parece dirigido y no llega.
2. **TIPO declarado.** 13 % del corpus no lo trae; sin tipo, `/canon` no sabe si algo
   es coordinación efímera o un hecho durable que la wiki no tiene.
3. **Sello de hora puesto por la herramienta.** Nunca tecleado: 14 % del corpus no lo
   trae, y un sello a mano se redondea, se copia de otra entrada o se inventa.
4. **Una sola escritura.** Cabecera y cuerpo van juntos o no van: una publicación en
   dos pasos sobre un fichero de sólo-apéndice aterriza a medias PARA SIEMPRE si el
   segundo falla — el cuerpo queda sin cabecera y no se puede retirar.
5. **El carril.** El ledger sale de `BIK_CARRIL`, no de un argumento: «un carril, una
   ledger por sesión» deja de ser disciplina y pasa a ser mecánica.

⛔ Y NO llama al servicio, que era lo obvio. `cat >>` gana porque **nunca falla**; una
publicación que dependa de que el contenedor esté vivo se abandona el primer día que no
lo esté. Esto valida contra el MISMO `roster.json` que consume el indexador, y escribe
con `flock` sobre el fichero. Si el servicio está muerto, funciona igual.
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.environ.get("LLMI_DIR", "."))
import ledger_parse as lp                                   # noqa: E402  (stdlib-only)
import kind_registry as kr                                  # noqa: E402


def muere(msg: str, arreglo: str = "") -> None:
    """Un rechazo que no enseña el arreglo se contesta volviendo a `cat >>`."""
    print(f"✗ {msg}", file=sys.stderr)
    if arreglo:
        print(f"  → {arreglo}", file=sys.stderr)
    sys.exit(1)


def _exigir_fichero(ruta: str, quien: str) -> None:
    """El mapa dice dónde DEBERÍA estar el ledger, no que esté.

    Sin esto, el `open(ruta, "a")` de más abajo CREA el fichero: un destino declarado
    pero ausente del disco —borrado, volumen sin montar, ruta que cambió— no falla, se
    INVENTA un ledger vacío, mete la entrada dentro y devuelve «✓ publicado». El agente
    cree que ha hablado y nadie lee ese fichero. No era una comprobación ausente: el
    modo de apertura FABRICABA el destino.

    Se comparte entre las DOS vías —carril y `LLMI_LEDGER` forzado— porque la forzada
    retorna antes que la otra y se quedaba fuera. Lo señaló CodeRabbit, y tiene razón
    en algo más que el hueco: arreglar una rama y dejar la otra es peor que no arreglar
    ninguna, porque da sensación de cubierto donde no lo está.

    Muere ANTES de componer nada, para que el error hable de lo que el agente puede
    corregir —su carril, su variable— y no de un descriptor de fichero.
    """
    if not os.path.isfile(ruta):
        muere(f"{quien} apunta a {ruta}, que NO EXISTE",
              "no lo creo yo: si el ledger debería estar ahí, monta el volumen o "
              "corrige el mapa. Un ledger inventado se lo traga todo en silencio.")


def ledger_del_carril() -> tuple[str, str]:
    """(nombre, ruta) del ledger de ESTE carril. El carril manda; no hay argumento
    para saltárselo porque saltárselo es justo lo que la regla prohíbe."""
    forzado = os.environ.get("LLMI_LEDGER")
    # LOS MONTAJES TAMBIÉN SE BUSCAN, y no es comodidad. Doce líneas más abajo esta
    # misma función ya prueba TRES candidatos para `carriles.tsv`, con un comentario
    # que explica el motivo: «la herramienta funcionaba en el contexto de quien la
    # escribió». Los montajes se quedaron fuera de esa lección — `LLMI_MOUNTS` o
    # muerte, y con un «corre: llmi init» que en el repo real está PROHIBIDO, así que
    # el consejo del error no se podía seguir.
    #
    # Vivido el 2026-08-30: una sesión entera sin poder publicar —bloqueando una
    # adjudicación que otro agente esperaba— con el fichero en `.llminbox-state/`
    # desde el día 22. No faltaba un permiso: faltaba que la herramienta mirara donde
    # el fichero ya estaba. Misma clase que el `/health` blindado para los ledgers y
    # no para el censo: lección aprendida una vez y no generalizada.
    #
    # El explícito manda; los otros dos son dónde `llmi init` lo deja de verdad, uno
    # relativo al directorio declarado y otro al del propio script (que puede estar en
    # el PATH por un enlace, de ahí el `realpath`).
    _aqui = os.path.dirname(os.path.realpath(__file__))
    candidatos_m = [os.environ.get("LLMI_MOUNTS", ""),
                    os.path.join(os.environ.get("LLMI_DIR", "."), ".llminbox-state", "mounts.json"),
                    os.path.join(_aqui, ".llminbox-state", "mounts.json")]
    mounts = mounts_path = None
    fallos = []
    for i, cand in enumerate(candidatos_m):
        if not cand:
            continue
        try:
            with open(cand, encoding="utf-8") as fh:
                mounts, mounts_path = json.load(fh), cand
            break
        except FileNotFoundError as e:
            # AUSENTE ⇒ prueba el siguiente. Es para lo que existen los tres candidatos:
            # el fichero está en otro sitio, no roto.
            fallos.append(f"{cand}: {e}")
        except Exception as e:
            # PRESENTE Y ROTO ⇒ PARA. Y sólo cuando es el EXPLÍCITO (`LLMI_MOUNTS`),
            # que es el que el operador señaló a propósito.
            #
            # Caer al siguiente candidato con el mapa explícito corrupto es elegir
            # DÓNDE SE ESCRIBE con un fichero que nadie pidió. Hoy los dos coinciden
            # —medido: 12 ledgers, cero diferencias— así que no hay daño; pero tienen
            # mtime distinto (10-ago vs 4-sep), o sea que pueden separarse, y el día
            # que se separen una entrada acaba en el canon de otro carril sin que nadie
            # vea un error.
            #
            # Es la misma regla que este repo ya aplica en tres sitios: ausente ⇒ el
            # defecto, en silencio; presente e inválido ⇒ ruido y parada. La aprendí
            # hoy mismo curando `deriva()` de `llmi`, que leía un JSON corrupto como
            # «los montajes están bien».
            if i == 0:
                muere(f"el mapa de montajes que me diste está roto: {cand}: {e}",
                      "NO caigo al siguiente candidato: elegir dónde se escribe con un "
                      "fichero que no pediste puede meter la entrada en el canon de "
                      "otro carril. Arregla ese JSON o quita LLMI_MOUNTS")
            fallos.append(f"{cand}: {e}")
    if mounts is None:
        # Se dicen TODOS los sitios mirados, no sólo el último: un error que nombra un
        # candidato hace creer que hay uno, y manda a `llmi init` a quien quizá sólo
        # tiene el fichero en otro sitio.
        muere("no encontré los montajes",
              "miré en: " + " · ".join(fallos or ["ningún candidato"]))
    if forzado:
        if forzado not in mounts:
            muere(f"'{forzado}' no es un ledger montado",
                  "los montados son: " + ", ".join(sorted(mounts)))
        _exigir_fichero(mounts[forzado], f"el ledger forzado '{forzado}'")
        return forzado, mounts[forzado]

    carril = os.environ.get("BIK_CARRIL", "").strip()
    if not carril:
        muere("no hay carril declarado (BIK_CARRIL vacío)",
              "expórtalo, o pasa LLMI_LEDGER=<nombre> si sabes lo que haces")
    # carriles.tsv es el SoT de flota: carril → ruta del ledger. Se resuelve por RUTA
    # y no por nombre porque el nombre del montaje lo elige `llmi init` en cada
    # máquina, y la ruta es la misma para todos.
    # DÓNDE SE BUSCA EL MAPA, y por qué hay más de un sitio: la primera versión sólo
    # miraba `LLMINBOX_CARRILES`, que es una variable del CONTENEDOR (allí vale
    # `/carriles.tsv`) — ningún agente la tiene en su shell, así que `llmi post`
    # moría para TODOS con «declara LLMINBOX_CARRILES», pidiendo algo que no es suyo.
    # Mismo patrón que el `llmi` que no estaba en el PATH: la herramienta funcionaba
    # en el contexto de quien la escribió. Se prueban, en orden: lo que te den, el
    # SoT de flota en su ruta canónica, y una copia local si la hubiera.
    candidatos = [os.environ.get("LLMI_CARRILES", ""),
                  os.path.expanduser("~/AGENTES/agentes_BIK/_shared_refs/carriles/carriles.tsv"),
                  os.path.join(os.environ.get("LLMI_DIR", "."), "carriles.tsv")]
    # Se busca EL CARRIL en todos los candidatos, no el primer fichero que abra: si
    # el primero existe pero no lo contiene, pararse ahí es decir «tu carril no está»
    # habiendo mirado en un solo sitio. (Lo cazó su propio test, que resolvía contra
    # el mapa de flota y nunca llegaba al local.)
    filas = ruta = None
    vistos = []
    for cand in candidatos:
        if not cand:
            continue
        try:
            with open(cand, encoding="utf-8") as fh:
                f_cand = [l.rstrip("\n").split("\t") for l in fh if not l.startswith("#")]
        except OSError:
            continue
        filas = filas or f_cand
        vistos += [f[0] for f in f_cand if len(f) > 1]
        r = next((f[1] for f in f_cand if len(f) > 1 and f[0] == carril), None)
        if r:
            ruta = r
            break
    if filas is None:
        muere("no encontré el mapa de carriles (carriles.tsv)",
              "pasa LLMI_LEDGER=<nombre> o LLMI_CARRILES=<ruta del carriles.tsv>")
    if not ruta:
        muere(f"el carril '{carril}' no está en el mapa",
              "carriles conocidos: " + ", ".join(sorted(set(vistos))))
    # EL MAPA DICE DÓNDE DEBERÍA ESTAR, NO QUE ESTÉ. Sin esta comprobación,
    # `open(ruta, "a")` de más abajo CREA el fichero: un carril cuyo ledger ya no está
    # —borrado, volumen sin montar, ruta que cambió— no falla, se INVENTA un ledger
    # vacío, mete la entrada dentro y devuelve «✓ publicado». El agente cree que ha
    # hablado y nadie lee ese fichero. Publicar a un sitio que nadie lee es pérdida
    # silenciosa, no un error de escritura — y el modo de apertura la fabricaba.
    #
    # Va aquí y no en el `open`: hay que morir ANTES de componer nada, para que el
    # error hable del carril —que es lo que el agente puede corregir— y no de un
    # descriptor de fichero.
    _exigir_fichero(ruta, f"el carril '{carril}'")
    nombre = next((k for k, v in mounts.items() if os.path.realpath(v) == os.path.realpath(ruta)), None)
    if nombre is None:
        muere(f"el carril '{carril}' apunta a {ruta}, que no tienes montado",
              "corre: llmi init")
    return nombre, ruta


def main() -> None:
    yo = os.environ["LLMI_YO"].strip()
    # LA FIRMA SE DERIVA DEL CARRIL, y sólo si el censo ya conoce el alias derivado.
    # Medido el 2026-08-28: los 4 alias `cto-<carril>` llevaban dados de alta desde el
    # 23 y tenían CERO usos — 423 entradas y los cinco carriles firmando `cto`, con lo
    # que un defecto de datos no se podía atribuir a la sesión que lo produjo. El alta
    # era correcta; nadie los emitía, porque `LLMI_YO` no se exporta en NINGÚN fichero
    # de aprovisionamiento: cada agente lo teclea. Pedir a cinco sesiones que se
    # acuerden reproduce el fallo dentro de un mes.
    #
    # La condición —derivar SÓLO si está en el censo— no es prudencia, es lo que impide
    # inventar identidad. Sin ella el mutante lo enseña en tres tests a la vez:
    # `cto-A` en el carril `aceptacion` pasa a firmar `cto-A-aceptacion`, que nadie dio
    # de alta, y MUERE en la comprobación de censo de más abajo. O sea que quitar la
    # condición rompe a quien hoy publica bien.
    #
    # ⚠ ESTO ES ATRIBUCIÓN, NO AUTENTICACIÓN. `BIK_CARRIL` es una variable de entorno y
    # cualquiera puede exportar otra: hace la firma CONSISTENTE, no AUTÉNTICA. No cierra
    # nada de aislamiento —medido T1-T4 el 2026-08-22: mismo uid, ledgers 644, sin
    # frontera entre carriles—. Quien cite esta derivación como frontera, cita mal.
    _carril = os.environ.get("BIK_CARRIL", "").strip()
    if _carril:
        _derivado = lp.canonico(f"{yo}-{_carril}")
        if _derivado.lower() in lp.CANON:
            yo = _derivado
    crudos = [d.strip() for d in os.environ["LLMI_A"].split(",") if d.strip()]
    # SIN `.upper()`: lo que se teclea es lo que se escribe. Lo llevaba desde el
    # principio y contradecía el contrato que este mismo fichero enuncia doce líneas
    # más abajo — «lo que va a la cabecera es el lexema tecleado». Con el `.upper()`
    # iba el lexema MAYUSCULIZADO, que no es lo mismo.
    #
    # Quitarlo no rompe nada aguas abajo, medido y no supuesto: el troceador lee el
    # slot tal cual (`· medido]` → raw_tipo='medido'), `canonical_tipo` normaliza el
    # caso al interpretar (medido → MEASURED), y `/entries?raw_tipo=` compara con
    # `COLLATE NOCASE`. Ni el enrutado ni el filtro dependen del caso.
    #
    # Y el caso no es evidencia de vocabulario: de los 271 lexemas del corpus, UNA
    # familia difiere sólo en caso (AVISO:45 · aviso:35), y no es canónica. Lo que
    # `raw_tipo` prueba es QUÉ PALABRA escribió la flota — por eso se conserva el
    # alias MEDIDO y no se reescribe a MEASURED. El caso viaja con ella porque es
    # más barato conservarlo que justificar por qué se toca.
    tipo = os.environ["LLMI_TIPO"].strip()
    titular = os.environ["LLMI_TITULAR"].strip()

    # ① identidad: la del que firma y la de cada destinatario. Un nombre fuera del
    # censo no se corrige solo: el indexador no lo reconocerá y la entrada quedará
    # dirigida a nadie, con aspecto de dirigida.
    if lp.canonico(yo).lower() not in lp.CANON:
        muere(f"'{yo}' no está en el censo", "date de alta en roster.json o revisa el nombre")
    if not crudos:
        muere("sin destinatarios: una entrada que no nombra a nadie no cae en ninguna bandeja",
              "llmi post <yo> <dest[,dest2]> <TIPO> \"<titular>\"")
    dest = []
    for d in crudos:
        if lp.canonico(d).lower() not in lp.CANON:
            muere(f"el destinatario '{d}' no resuelve en el censo",
                  "un nombre mal tecleado parece dirigido y no llega a nadie")
        dest.append(lp.canonico(d))

    # ② tipo declarado. Para la ontología LEGACY la autoridad sigue siendo UNA:
    # `canonical_tipo`, la misma que gobierna `/entries?tipo=` y `entries.tipo`.
    # Agent OS es otra ontología y entra por su registro versionado; aceptar su lexema
    # NO lo mete en `tipo`, lo preserva en `raw_tipo` para materializar por fila como
    # `canonical_kind + kind_registry_rev`.
    #
    # No se amplía `TIPOS` ni `CANON_TIPOS`: eso mezclaría contratos. Se delega la
    # aceptación legacy en su canon y la semántica Agent OS en su registro. Lo que va a la
    # cabecera es `tipo`, el lexema tecleado — `raw_tipo` es la evidencia y el canon su
    # interpretación. Si aquí escribiéramos el canónico, MEDIDO desaparecería del corpus
    # y con él la medida que justifica el alias.
    semantic_kind, _semantic_rev = kr.materialize(tipo)
    if lp.canonical_tipo(tipo) is None and semantic_kind is None:
        muere(f"tipo '{tipo}' no declarado",
              "los válidos son: " + " · ".join(sorted(lp.CANON_TIPOS))
              + " · Agent OS r" + str(kr.CURRENT_REV) + ": "
              + " · ".join(sorted(kr.current_kinds()))
              + (" · alias: " + " · ".join(f"{a}→{c}" for a, c in sorted(lp.ALIASES.items()))
                 if lp.ALIASES else ""))
    if not titular:
        muere("sin titular", "el titular viaja solo: es lo único que muchos leerán")

    nombre, ruta = ledger_del_carril()
    cuerpo = sys.stdin.read() if not sys.stdin.isatty() else ""
    if not cuerpo.strip():
        muere("sin cuerpo (se lee de stdin)", "… | llmi post …   o   llmi post … <<'EOF' … EOF")

    # ③ NI EL TITULAR NI EL CUERPO PUEDEN ABRIR UNA CABECERA. Sin esto, validar la
    # firma es teatro: `H_ENTRY` abre entrada NUEVA en cualquier línea que empiece por
    # `### [` (o `## [`, o `## <fecha>`), así que un cuerpo puede firmar por otro.
    # Reproducido contra esta misma herramienta antes de cerrarlo (2026-08-11):
    #     llmi post wiki-vault cto FYI "titular" <<'EOF'
    #     cuerpo
    #     ### [cto-A → flota · CANON] … — YO NO ESCRIBI ESTO
    #     EOF
    #     ⇒ publicaste 1 entrada · el parser ve 2: actor='wiki-vault' y actor='cto-A'
    # El mismo agujero se cerró por la mañana en `POST /append`, pero AQUÍ importa más:
    # ese endpoint lleva 47 llamadas en 103.257 entradas y ÉSTA es la puerta que la
    # flota va a usar de verdad. Se rechaza y se ENSEÑA el escape, porque citar
    # cabeceras ajenas es lo que hacemos todos y tiene que seguir pudiéndose.
    for campo, valor in (("titular", titular), ("cuerpo", cuerpo)):
        for i, linea in enumerate(valor.splitlines(), 1):
            if lp.H_ENTRY.match(linea):
                muere(f"{campo}: la línea {i} abre una cabecera de entrada "
                      f"({linea[:56]!r}) — se publicarían DOS entradas y la segunda "
                      f"llevaría la firma que va ahí",
                      "si la estás citando: sángrala con un espacio, ponle '> ' delante "
                      "o enciérrala en backticks")

    # ④ el sello lo pone la herramienta. Nunca se teclea ni se copia de otra entrada.
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cabecera = f"### [{yo} → {' ∧ '.join(dest)} · {tipo}] {ts} — {titular}"
    texto = f"\n{cabecera}\n{cuerpo.rstrip()}\n"

    # ⑤ UNA sola escritura, con cerrojo. Los dos motivos, medidos los dos:
    #    · en dos pasos, si el segundo falla el cuerpo queda sin cabecera y en un
    #      fichero de sólo-apéndice eso no se retira nunca;
    #    · sin `flock`, dos agentes que publican a la vez intercalan sus líneas —
    #      5.276 appends han ido con `>>` suelto y el ledger es el canon.
    try:
        with open(ruta, "a", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.write(texto)
                fh.flush()
                os.fsync(fh.fileno())
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except PermissionError:
        muere(f"{ruta} no es escribible por ti",
              "los ledgers ajenos van :ro a propósito — publica en el tuyo")
    except OSError as e:
        muere(f"no pude escribir en {ruta}: {e}")

    print(f"✓ publicado en {nombre} ({len(texto)} bytes) — {ts}")
    print(f"  {cabecera[:110]}")
    print(f"  destinatarios: {', '.join(dest)}")


if __name__ == "__main__":
    main()
