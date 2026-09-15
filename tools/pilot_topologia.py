"""Verificador ESTÁTICO de la topología del piloto M1 (ADR-001).

Por qué existe como FUNCIÓN PURA sobre un dict y no como un script que lee el
fichero: un verificador que sólo sabe leer el compose bueno no se puede falsar.
Tomando el dict ya parseado, la prueba puede MUTARLO —quitar el volumen del
journal, montar un ledger de más, colar el socket de Docker— y exigir que el
verificador se ponga rojo. Sin esa forma, el `⊕` del compose real es un verde que
no discrimina: pasaría igual con el verificador desconectado.

Lo que este fichero NO hace, dicho aquí para que nadie lo lea de más: NO prueba
que el despliegue cumpla el compose. Mide el CONTRATO escrito. La guarda de la
CORRIDA vive en `tools/pilot_preflight.py`, que corre dentro del contenedor.
"""
from __future__ import annotations

import re

# Prefijo bajo el que el piloto monta los ledgers. La clasificación va por DESTINO
# dentro del contenedor y no por ruta del host: el host cambia de máquina a máquina
# y el destino lo fija este repo.
PREFIJO_LEDGERS = "/ledgers/"

# Rutas del host que el contenedor del agente no puede montar NUNCA. El socket de
# Docker es el peor: montarlo equivale a dar root en el host y convierte cualquier
# aislamiento por contenedor en decorado (quien alcanza el demonio lee el token de
# cualquier otro contenedor con `docker inspect`).
PROHIBIDAS_AGENTE = ("/var/run/docker.sock", "/run/docker.sock")

# Claves de entorno que NO pueden llevar valor literal en el compose: el compose se
# commitea. El pepper viaja por FICHERO y su variable termina en _FILE.
# Una indirección PURA: la sustitución entera y nada pegado a los lados. Lo que
# esto rechaza y el chequeo anterior aceptaba: `p$ssw0rd`, `pre${VAR}`, `${A}${B}`.
INDIRECCION_PURA = re.compile(
    r"\$\{[A-Za-z_][A-Za-z0-9_]*(?::[-?][^}]*)?\}"     # ${VAR} ${VAR:-x} ${VAR:?x}
    r"|\$[A-Za-z_][A-Za-z0-9_]*")                        # $VAR

# 🩸 AUDITORÍA sobre `d3912e6`: «indirección PURA» cerraba la FORMA y dejaba
# abierto el CONTENIDO. `${TOKEN:-hardcoded-secret}` es una sustitución entera,
# nada pegado a los lados, y `fullmatch` la aprobaba — con el secreto escrito en
# el YAML que se commitea. Medido antes de tocar nada, con `${OTRO:-…}` en las
# cuatro claves: `LLMINBOX_PEPPER`, `LLMINBOX_JOURNAL_PEPPER` y
# `LLMINBOX_WATCHER_TOKEN` salían VERDE ENTERO; sólo `LLMINBOX_TOKEN` caía, y por
# `I10`, no por aquí. Tres de cuatro sin guarda.
#
# `:-` da VALOR y por eso es rojo. `:?` NO: lo de dentro es el mensaje de error
# con el que la sustitución ABORTA, así que `${VAR:?falta el token}` sigue verde
# —y ése es el ⊕ que impide que endurecer esto rompa la forma legítima—.
DEFECTO_CON_VALOR = re.compile(
    r"\A\$\{[A-Za-z_][A-Za-z0-9_]*:-(?P<defecto>[^}]+)\}\Z")

# 🩸 AUDITORÍA EN DOS PASADAS, y la segunda me la hizo @security
# (`MARK:security-el-doble-pipe-se-cierra-por-lista-no-por-forma`).
#
# ① Primero esto era una lista de LITERALES —`("|| true", "|| exit 0", …)`— que
#    enumeraba CÓMO se escribió el tragante, no QUÉ hace, y se le escapaban
#    `--readiness ||true` (sin espacio) y `--readiness | true` (una barra).
# ② La cambié por una gramática con `_NOOP = (true|:|/bin/true|exit 0)` … que
#    ENUMERA IGUAL, sólo que un nivel más arriba: una lista de PROGRAMAS. Medido:
#    `|| echo ok`, `|| printf ''`, `; sleep 0`, `|| test 1`, `|| cat /dev/null` y
#    `|| builtin true` pasaban los dos guards. **Cualquier orden que salga `0` se
#    traga el `rc`**, y la lista de órdenes que salen `0` no se puede enumerar.
#
# 🔑 Y el falsador de ② estaba escrito por mí dos líneas más arriba, justificando
# ①: «una política se aplica ENUMERANDO sus casos, y una lista de ejemplos siempre
# deja fuera el que nadie escribió». Lo apliqué a los literales y no a los
# programas. Para `|` sí aserté la PROPIEDAD —rechazar la barra entera «en vez de
# intentar adivinar la etapa final»— y para `||`, `;` y `&` volví a enumerar: dos
# separadores, dos criterios, y el bueno en el que menos importa.
#
# Ahora la propiedad es una y vale para los cinco separadores, sin mirar qué
# programa hay detrás: **el healthcheck es UN solo eslabón, o todos sus
# separadores son `&&`.** Se reutiliza `_cadena_de_arranque`, que ya existía para
# `I8-ter`. `&&` no se traga nada (`cmd && x` propaga el fallo de `cmd`), y por eso
# es el único que pasa.

# El servidor que el contrato del piloto acredita. Su nombre vive aquí y no
# suelto en la comprobación porque el preflight lo mira también
# (`PID1_ESPERADO_POR_DEFECTO`): son el mismo invariante en dos ficheros.
SERVIDOR_ESPERADO = "uvicorn"

# 🩸 AUDITORÍA sobre `d3912e6`: `I8-ter` decidía con `str.index` sobre SUBCADENAS,
# y las dos mitades fallaban por lo mismo. Medido verde antes de tocar nada:
#
#   `preflight && true ; exec uvicorn`  ← `&&` está DENTRO de `entre`, y cuelga de
#                                          `true`; el `;` suelta uvicorn igual
#   `preflight && exec not-uvicorn-wrapper` ← `"uvicorn" in arranque` es True
#
# Preguntar «¿aparece `&&` en algún sitio entre estas dos posiciones?» no es
# preguntar «¿está la cadena unida?», y `in` no es identidad. Se parte la cadena
# por sus separadores y se miran los ESLABONES.
SEPARADOR_SHELL = re.compile(r"(\|\||&&|[;|&])")
_ENVOLTORIOS = ("exec", "nohup", "setsid", "eval", "time")
_ASIGNACION = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")


def _cadena_de_arranque(texto: str) -> tuple[list[str], list[str]]:
    """Parte la línea de arranque en (eslabones, separadores).

    `separadores[i]` es el que une `segmentos[i]` con `segmentos[i+1]`, así que la
    cadena entre dos eslabones es exactamente `separadores[a:b]` — sin rodajas de
    texto ni índices de subcadena.
    """
    piezas = SEPARADOR_SHELL.split(texto)
    return [t.strip() for t in piezas[0::2]], [t.strip() for t in piezas[1::2]]


def _programa(segmento: str) -> str:
    """El `basename` del ejecutable de un eslabón, sin envoltorios ni `VAR=val`.

    Devuelve `""` si el eslabón está vacío; el llamante lo trata como «no arranca
    nada», que es rojo igual.
    """
    toks = segmento.split()
    while toks and (toks[0] in _ENVOLTORIOS or _ASIGNACION.fullmatch(toks[0])):
        toks.pop(0)
    return toks[0].rsplit("/", 1)[-1] if toks else ""


# 🩸 AUDITORÍA: `I10` imprimía el token en su propio mensaje de fallo
# (`f"vale \`{token}\`"`), y ese mensaje acaba en el log del gate, en el CI y en el
# ledger. Un rojo que publica lo que protege es peor que no tenerlo: convierte una
# comprobación de seguridad en el canal de fuga.
#
# Lo que sitúa el fallo es la FORMA y la LONGITUD —el valor el operador ya lo tiene
# delante, en su propio YAML—, así que se describe la forma y no se reproduce nada.
# Vive en una función y no en cada mensaje porque una política se aplica en un sitio
# o se olvida en el siguiente que alguien escriba.
_FORMA = re.compile(r"\A\$\{([A-Za-z_][A-Za-z0-9_]*)(:?[-?])([^{}]*)\}\Z")


def _forma_de(valor: str) -> str:
    """Describe un valor sensible SIN reproducirlo. Nunca devuelve `valor`."""
    v = valor.strip()
    if "$" not in v:
        return f"un literal en claro de {len(valor)} caracteres"
    m = _FORMA.fullmatch(v)
    if not m:
        return f"una composición de {len(valor)} caracteres con `$` dentro"
    var, op, resto = m.groups()
    if op in ("-", ":-"):
        return (f"`${{{var}{op}…}}` con un DEFECTO de {len(resto)} caracteres"
                if resto else f"`${{{var}{op}}}`, con el default VACÍO")
    return f"`${{{var}{op}…}}`"


SECRETOS_PROHIBIDOS_EN_YAML = (
    "LLMINBOX_PEPPER",
    "LLMINBOX_JOURNAL_PEPPER",
    "LLMINBOX_TOKEN",
    "LLMINBOX_WATCHER_TOKEN",
)


def _partir_montaje(cadena: str) -> list[str]:
    """Parte `origen:destino[:modo]` SIN romper dentro de `${...}`.

    Cazado por el propio control positivo de `tests/pilot/`: un `split(":")` pelado
    parte `${VAR:?mensaje}` por su `:?` y devuelve un destino inventado
    (`?mensaje}`). El verificador entonces NO VE el montaje y da verde por no
    encontrarlo — el peor de los verdes, porque se parece a la conformidad.
    """
    partes, actual, prof = [], [], 0
    i = 0
    while i < len(cadena):
        c = cadena[i]
        if c == "$" and i + 1 < len(cadena) and cadena[i + 1] == "{":
            prof += 1
            actual.append(cadena[i:i + 2])
            i += 2
            continue
        if c == "}" and prof:
            prof -= 1
        elif c == ":" and not prof:
            partes.append("".join(actual))
            actual = []
            i += 1
            continue
        actual.append(c)
        i += 1
    partes.append("".join(actual))
    return partes


def _normaliza_montajes(servicio: dict) -> list[dict]:
    """Devuelve [{origen, destino, ro, tipo}] tanto de la sintaxis corta como larga.

    Soportar LAS DOS no es cortesía: si el verificador sólo entendiera la corta,
    reescribir un montaje en sintaxis larga lo esquivaría en silencio — un hueco
    que se abre con una reindentación y no deja rastro.
    """
    salida: list[dict] = []
    for v in servicio.get("volumes") or []:
        if isinstance(v, str):
            partes = _partir_montaje(v)
            if len(partes) < 2:
                # Volumen anónimo (`- /dentro`): sin origen declarado.
                salida.append({"origen": None, "destino": partes[0], "ro": False,
                               "tipo": "anonimo"})
                continue
            origen, destino = partes[0], partes[1]
            modo = partes[2] if len(partes) > 2 else ""
            tipo = "bind" if origen.startswith((".", "/", "$", "~")) else "volumen"
            salida.append({"origen": origen, "destino": destino,
                           "ro": "ro" in modo.split(","), "tipo": tipo})
        elif isinstance(v, dict):
            salida.append({"origen": v.get("source"), "destino": v.get("target"),
                           "ro": bool(v.get("read_only")),
                           "tipo": v.get("type") or "bind"})
    return salida


def _es_ledger(destino: str | None) -> bool:
    return bool(destino) and destino.startswith(PREFIJO_LEDGERS)


# ── I13 · ENDURECIMIENTO, y se asevera de TODOS los servicios ────────────────
#
# 🩸 Hallazgo de @security (`MARK:security-endurecimiento-invertido-y-secretos-por-entorno`),
# medido y confirmado por mi mano:
#
#     servicio  monta                                     user   rootfs
#     gateway   journal RW · index RW · ledger RW ·        ROOT   ESCRIBIBLE
#               mapa ro · pepper ro · ./tools ro
#     estreno   journal RW · ledger RW · ./tools ro        ROOT   ESCRIBIBLE
#     agente    NADA                                       1000   read_only ✅
#
# **El único endurecido era el que no toca nada.** `cap_drop: ALL` y
# `no-new-privileges` sí estaban en los tres —eso es real y cuenta— pero no
# sustituyen a `read_only` ni a `user`: con rootfs escribible y `uid 0`, una
# ejecución de código dentro del gateway escribe en cualquier parte de la imagen y
# escribe el ledger RW **como root**, con el residuo de propiedad en el host.
#
# Y compone con `./tools:/app/tools:ro`: el `entrypoint` y el `healthcheck`
# ejecutan `pilot_preflight.py` desde el ÁRBOL DE TRABAJO del host. El montaje es
# `ro` dentro, pero fuera es el checkout — quien escriba el árbol elige qué corre
# el gateway como root, al arrancar y cada `interval`. Con `user` y `read_only`
# puestos ese montaje pesa mucho menos.
#
# Se asevera de TODOS los servicios y no de una lista de nombres: una política que
# enumera sujetos deja fuera el servicio que alguien añada mañana — que es la misma
# clase que me comí en `I8` dos veces.
_HARDENING = ("read_only", "user", "tmpfs", "cap_drop", "security_opt")


def verificar_endurecimiento(compose: dict) -> list[str]:
    """`I13` sobre CADA servicio del compose. Público: el estreno vive en otro
    fichero y no tiene `gateway`, así que `verificar()` no puede alcanzarlo."""
    fallos: list[str] = []
    for nombre, srv in (compose.get("services") or {}).items():
        srv = srv or {}
        if srv.get("read_only") is not True:
            fallos.append(f"I13: `{nombre}` no declara `read_only: true` — el rootfs "
                          f"de la imagen es escribible. Los VOLÚMENES siguen "
                          f"escribiéndose: `read_only` sólo congela la imagen")
        fallos += _usuario_no_root(nombre, srv.get("user"))
        tmpfs = srv.get("tmpfs")
        tmpfs = [tmpfs] if isinstance(tmpfs, str) else (tmpfs or [])
        if not any(str(t).split(":", 1)[0] == "/tmp" for t in tmpfs):
            fallos.append(f"I13: `{nombre}` no declara `tmpfs: [\"/tmp\"]` — con "
                          f"`read_only` y sin `/tmp` escribible, cualquier fichero "
                          f"temporal revienta el proceso en vez de fallar claro")
        if "ALL" not in [str(c).upper() for c in (srv.get("cap_drop") or [])]:
            fallos.append(f"I13: `{nombre}` no hace `cap_drop: [\"ALL\"]`")
        opts = [str(o) for o in (srv.get("security_opt") or [])]
        if not any(o.replace(" ", "").lower() == "no-new-privileges:true" for o in opts):
            fallos.append(f"I13: `{nombre}` no declara "
                          f"`security_opt: [\"no-new-privileges:true\"]` — sin eso un "
                          f"binario `setuid` dentro de la imagen recupera lo que "
                          f"`cap_drop` quitó")
    return fallos


def _usuario_no_root(nombre: str, user) -> list[str]:
    """`user:` explícito y NO root. Un nombre se rechaza a propósito: desde el YAML
    no se puede saber a qué uid resuelve dentro de la imagen, y una guarda que no
    puede medir su sujeto no es una guarda. Se pide uid numérico."""
    if user is None or str(user).strip() == "":
        return [f"I13: `{nombre}` corre como ROOT (no declara `user:`) y monta el "
                f"estado del piloto: lo que escriba en un volumen queda root en el "
                f"host. Declara `user: \"<uid>:<gid>\"` no-root"]
    uid = str(user).split(":", 1)[0].strip()
    if not uid.isdigit():
        return [f"I13: `{nombre}` declara `user: \"{user}\"` por NOMBRE: desde el "
                f"compose no se puede saber a qué uid resuelve dentro de la imagen. "
                f"Usa uid numérico, que es lo que esta guarda puede medir"]
    if int(uid) == 0:
        return [f"I13: `{nombre}` declara `user: \"{user}\"`, que es ROOT explícito"]
    return []


def verificar(compose: dict, *, gateway: str = "gateway", agente: str = "agente",
              ledger_piloto: str | None = None,
              token_var: str | None = "LLMINBOX_PILOT_TOKEN") -> list[str]:
    """Devuelve la lista de violaciones. Lista vacía = topología conforme.

    Se devuelven TODAS, no la primera: un verificador que corta al primer rojo
    obliga a N corridas para ver N problemas, y quien las arregla de una en una
    cree cada vez que ya está.

    `token_var` es la variable de indirección que `LLMINBOX_TOKEN` tiene que
    referenciar (`I10`). El defecto es la de M1, de una sola lane; el
    verificador de dos lanes (`pilot_topologia_dual.py`) pasa aquí la variable
    PROPIA de cada lane, porque exigir la misma en las dos sería exigir el
    cruce que esa entrega existe para prohibir.
    """
    fallos: list[str] = []
    servicios = compose.get("services") or {}

    if gateway not in servicios:
        return [f"I0: no existe el servicio `{gateway}`"]
    if agente not in servicios:
        fallos.append(f"I0: no existe el servicio `{agente}` — sin él no hay nada "
                      f"que aislar y el piloto no prueba su propiedad central")
        srv_agente = {}
    else:
        srv_agente = servicios[agente]
    srv_gw = servicios[gateway]

    mg = _normaliza_montajes(srv_gw)
    ma = _normaliza_montajes(srv_agente)
    declarados = set((compose.get("volumes") or {}).keys())

    # ── I1 · el journal vive en un volumen NOMBRADO propio, distinto del índice.
    vol_journal = [m for m in mg if m["destino"] == "/journal"]
    vol_indice = [m for m in mg if m["destino"] == "/data"]
    if not vol_journal:
        fallos.append("I1: el gateway no monta nada en `/journal`")
    elif vol_journal[0]["tipo"] != "volumen":
        fallos.append("I1: `/journal` no es un volumen nombrado "
                      f"(tipo={vol_journal[0]['tipo']}) — un bind hereda el ciclo de "
                      "vida del host y el ADR pide almacén durable propio")
    elif vol_journal[0]["origen"] not in declarados:
        fallos.append(f"I1: el volumen `{vol_journal[0]['origen']}` no está declarado "
                      "en el bloque `volumes:` de primer nivel")
    if vol_journal and vol_indice and vol_journal[0]["origen"] == vol_indice[0]["origen"]:
        fallos.append("I2: journal e índice comparten volumen — el índice es "
                      "reconstruible y su doctrina escrita dice que se puede borrar; "
                      "el journal no tiene camino de reconstrucción")

    # ── I3 · el journal se monta RW en el gateway y NO EXISTE en el agente.
    if vol_journal and vol_journal[0]["ro"]:
        fallos.append("I3: el journal está montado `ro` en el gateway — no podría "
                      "aceptar un solo evento nativo")
    # Centinela imposible cuando no hay journal: con `None`, un volumen anónimo del
    # agente (origen `None`) dispararía este rojo por coincidencia y no por montaje.
    origen_journal = vol_journal[0]["origen"] if vol_journal else "\0sin-journal"
    if any(m["destino"] == "/journal" or m["origen"] == origen_journal for m in ma):
        fallos.append("I3: el agente monta el journal — la autoridad se pide por la "
                      "API, nunca se escribe a mano")

    # ── I4 · ningún secreto por VALOR en el YAML; el pepper por fichero.
    entorno_gw = srv_gw.get("environment") or {}
    if isinstance(entorno_gw, list):
        entorno_gw = dict(p.split("=", 1) for p in entorno_gw if "=" in p)
    for clave in SECRETOS_PROHIBIDOS_EN_YAML:
        valor = str(entorno_gw.get(clave, ""))
        # 🩸 CIERRE del auditor: antes bastaba con que el valor LLEVARA un `$`.
        # «Contiene `$`» no es «es una indirección»: `p$ssw0rd`, `${VAR}-cola` y
        # `pre${VAR}` llevan `$` y son SECRETOS LITERALES que se commitean. La
        # regla buena es que el valor sea ENTERO una sustitución y nada más.
        if valor and not INDIRECCION_PURA.fullmatch(valor):
            fallos.append(f"I4: `{clave}` no es una indirección PURA en el compose "
                          f"(`${{VAR}}`, `${{VAR:-}}`, `${{VAR:?}}` o `$VAR`): un "
                          f"valor con `$` dentro sigue siendo un secreto literal, y "
                          f"se commitea igual")
        elif valor:
            defecto = DEFECTO_CON_VALOR.match(valor)
            if defecto:
                # El valor NO se reproduce: es el secreto, y este mensaje acaba en
                # el log del gate y en el ledger. Se da su longitud, que basta para
                # localizarlo en el YAML y no lo publica.
                fallos.append(
                    f"I4: `{clave}` es {_forma_de(valor)}: la forma es una "
                    f"indirección pero el contenido es un SECRETO LITERAL que se "
                    f"commitea, y además convierte el fail-closed en fail-open "
                    f"—quien no ponga la variable arranca con el valor conocido—. "
                    f"Usa `${{VAR:?…}}`, que aborta en vez de rellenar")
    if not entorno_gw.get("LLMINBOX_PEPPER_FILE"):
        fallos.append("I4: falta `LLMINBOX_PEPPER_FILE` — sin pepper por fichero, "
                      "`credential_ref` deja de ser opaco o el pepper acaba en el YAML")

    # ── I5 · el mapa de credenciales, `ro` y CON su atestado.
    mapa = [m for m in mg if m["destino"].endswith("/mapa.json")]
    if not mapa:
        fallos.append("I5: el gateway no monta el mapa de credenciales")
    elif not mapa[0]["ro"]:
        fallos.append("I5: el mapa de credenciales no está montado `:ro`")
    if not entorno_gw.get("LLMINBOX_CREDENCIALES_SHA"):
        fallos.append("I5: falta `LLMINBOX_CREDENCIALES_SHA` — sin atestado, el mapa "
                      "se puede sustituir en el host después de desplegarlo y nadie "
                      "lo nota en el arranque siguiente")
    roster_path = entorno_gw.get("LLMINBOX_ROSTER")
    roster = [m for m in mg if m["destino"] == roster_path]
    if not roster_path:
        fallos.append("I5: falta `LLMINBOX_ROSTER` — un mapa con censo vacío no "
                      "puede resolver identidades y el runtime no arranca")
    elif len(roster) != 1:
        fallos.append("I5: el roster declarado no tiene un único montaje")
    elif not roster[0]["ro"]:
        fallos.append("I5: el roster no está montado `:ro`")

    # ── I6 · UN ledger RW (el del carril piloto) y el resto `ro`.
    ledgers_gw = [m for m in mg if _es_ledger(m["destino"])]
    rw = [m for m in ledgers_gw if not m["ro"]]
    if len(rw) != 1:
        fallos.append(f"I6: el gateway tiene {len(rw)} ledgers RW y debe tener "
                      f"exactamente 1 (el del carril piloto): {[m['destino'] for m in rw]}")
    elif ledger_piloto and rw[0]["destino"] != ledger_piloto:
        fallos.append(f"I6: el ledger RW es `{rw[0]['destino']}` y el del carril "
                      f"piloto es `{ledger_piloto}`")
    if len(ledgers_gw) < 2:
        fallos.append("I6: hay menos de 2 ledgers montados — sin un segundo ledger "
                      "`ro` el control negativo de carril cruzado no puede correr, y "
                      "su verde lo firmaría la ausencia, no la autorización")

    # ── I7 · el agente: CERO montajes y CERO secretos. No una lista de prohibidos.
    #
    # Antes esto era una enumeración —ledger, socket, HOME, privileged— y por eso
    # un agente que montaba el pepper y el mapa PASABA (A1 de @security), igual que
    # uno que montaba el volumen del índice (A2). El comentario del compose ya
    # prometía «no es que estén todos en `ro`: es que NO HAY»; una política en
    # prosa se aplica ENUMERANDO sus casos o no se aplica, y esa enumeración
    # siempre deja fuera lo que nadie pensó. Se asevera la propiedad, no la lista.
    if ma:
        fallos.append(f"I7: el agente declara {len(ma)} montaje(s) "
                      f"{[m['destino'] for m in ma]} y tiene que declarar CERO — "
                      f"no «todos en ro», CERO: un agente que puede leer no "
                      f"necesita pedir autoridad, y entonces el piloto no prueba "
                      f"que la frontera sea el montaje")
    # `volumes` no era la única puerta: `configs`, `secrets`, `env_file`,
    # `volumes_from` y `devices` entregan material con el mismo efecto. Se
    # ENUMERAN LAS PUERTAS, que son finitas y están en el esquema de compose, en
    # vez de enumerar los materiales, que no lo están.
    for puerta in ("secrets", "configs", "env_file", "volumes_from", "devices"):
        if srv_agente.get(puerta):
            fallos.append(
                f"I7: el agente declara `{puerta}:` — es otra puerta para el mismo "
                f"material (mapa, pepper, entorno del gateway) y el piloto existe "
                f"para probar que la frontera es el montaje, no el buen juicio")
    # Los prohibidos concretos se siguen nombrando: la regla de arriba ya los cubre
    # a todos, pero un mensaje que dice CUÁL se arregla más rápido que uno que dice
    # CUÁNTOS. Son diagnóstico, no la guarda.
    for m in ma:
        if _es_ledger(m["destino"]):
            fallos.append(f"I7: el agente monta el ledger `{m['destino']}`")
        origen = str(m["origen"] or "")
        if origen in PROHIBIDAS_AGENTE or m["destino"] in PROHIBIDAS_AGENTE:
            fallos.append(f"I7: el agente monta el socket de Docker (`{origen}`) — "
                          "eso es root en el host y anula todo el aislamiento")
        if origen.startswith("${HOME") or origen.startswith("~") or origen == "$HOME":
            fallos.append(f"I7: el agente monta el HOME del operador (`{origen}`)")
    if srv_agente.get("privileged"):
        fallos.append("I7: el agente corre `privileged`")

    # ── I8 · healthcheck presente y ATADO al preflight, no a un 200 pelado.
    hc = srv_gw.get("healthcheck") or {}
    prueba = " ".join(hc.get("test") or []) if isinstance(hc.get("test"), list) else str(hc.get("test") or "")
    if not prueba:
        fallos.append("I8: el gateway no declara healthcheck")
    elif "pilot_preflight" not in prueba:
        fallos.append("I8: el healthcheck no invoca `pilot_preflight --readiness`: un "
                      "chequeo que sólo mira el HTTP marca `healthy` sobre un journal "
                      "ausente o un pepper equivocado")
    elif "--readiness" not in prueba:
        # Sin la bandera corre el preflight NORMAL: comprueba y NO corta. La
        # diferencia entre las dos rutas es justo lo que promete el apartado de
        # `unhealthy`, y un healthcheck que sólo comprueba lo deja en advisory.
        fallos.append("I8: el healthcheck llama al preflight SIN `--readiness`: ésa "
                      "es la ruta que comprueba pero no corta")
    # ── I8-bis · un healthcheck INERTE es peor que no tenerlo: pinta `healthy`.
    if hc.get("disable"):
        fallos.append("I8: el healthcheck está `disable: true` — declarado y MUERTO, "
                      "que es la forma de tenerlo sin que mida nada")
    if prueba.strip().upper().startswith("NONE"):
        fallos.append("I8: el healthcheck es `NONE`: anula el de la imagen y no pone "
                      "ninguno")
    if prueba:
        _, seps_hc = _cadena_de_arranque(prueba)
        sueltos_hc = [x for x in seps_hc if x != "&&"]
        if sueltos_hc:
            fallos.append(
                f"I8: el healthcheck encadena el preflight con {sueltos_hc} y su "
                f"código de salida deja de ser el del preflight ⇒ `healthy` pase lo "
                f"que pase. Tiene que ser UN solo eslabón, o todos sus separadores "
                f"`&&`: con `||`/`;`/`&` el rc es el del ÚLTIMO que corra y con `|` "
                f"el de la última etapa de la tubería")
    # ── I8-ter · el ORDEN. El compose lo dice en un COMENTARIO y no lo hacía
    # cumplir nadie: el preflight tiene que correr ANTES de `uvicorn` y unido por
    # `&&`. Detrás, es un aviso; con `;` o `||`, el servidor arranca igual.
    arranque = srv_gw.get("entrypoint") or srv_gw.get("command") or []
    arranque = " ".join(arranque) if isinstance(arranque, list) else str(arranque)
    if arranque.strip():
        segmentos, separadores = _cadena_de_arranque(arranque)
        i_srv = len(segmentos) - 1
        servidor = _programa(segmentos[i_srv])
        i_pre = next((i for i, seg in enumerate(segmentos)
                      if "pilot_preflight" in seg), None)
        if servidor != SERVIDOR_ESPERADO:
            fallos.append(
                f"I8: lo último que arranca el gateway es `{servidor or '(nada)'}` y "
                f"el contrato dice `{SERVIDOR_ESPERADO}` (identidad EXACTA, no "
                f"subcadena): `not-{SERVIDOR_ESPERADO}-wrapper` lleva "
                f"`{SERVIDOR_ESPERADO}` dentro y pasaba por él, con el preflight "
                f"encadenado a un servidor que nadie ha acreditado")
        if i_pre is None:
            fallos.append("I8: el arranque del gateway no pasa por "
                          "`pilot_preflight`: el fail-closed no está en la ruta")
        elif i_pre >= i_srv:
            fallos.append("I8: el preflight no va ANTES del servidor en la cadena de "
                          "arranque: para cuando comprueba, el servidor ya sirve")
        else:
            sueltos = [x for x in separadores[i_pre:i_srv] if x != "&&"]
            if sueltos:
                fallos.append(
                    f"I8: entre el preflight y el servidor hay {sueltos} y la cadena "
                    f"entera tiene que ir unida por `&&`: basta UN `;` o `||` en "
                    f"medio para que el servidor arranque con el preflight en rojo. "
                    f"Mirar sólo si `&&` APARECE no bastaba — en "
                    f"`preflight && true ; exec {SERVIDOR_ESPERADO}` aparece, y "
                    f"cuelga de `true`")

    # ── I13 · endurecimiento de TODOS los servicios (detalle arriba).
    fallos += verificar_endurecimiento(compose)

    # ── I9 · puertos sólo en loopback.
    for nombre in (gateway, agente):
        for p in (servicios.get(nombre, {}).get("ports") or []):
            if not str(p).startswith("127.0.0.1:"):
                fallos.append(f"I9: `{nombre}` publica `{p}` fuera de loopback")
    if servicios.get(agente, {}).get("ports"):
        fallos.append("I9: el agente publica puertos — no tiene nada que servir")

    # ── I12 · NADA se monta DENTRO del journal ni del ledger del piloto.
    #
    # `I1` mira el montaje de `/journal`; nadie miraba lo que se monta ENCIMA. Un
    # bind de un fichero forjado sobre `/journal/.volume-id` deja el testigo en
    # manos de quien monta, y la guarda ⓑ lee exactamente lo que le pusieron:
    # la identidad del volumen la decidiría el compose, no el volumen.
    # Los protegidos se DERIVAN de los montajes, no del parámetro: pedirle al
    # llamante que nombre el ledger dejaba la guarda apagada cuando no lo pasaba,
    # que es como la mitad de las llamadas.
    protegidos = [m["destino"] for m in mg
                  if m["destino"] in ("/journal", "/data") or _es_ledger(m["destino"])]
    if ledger_piloto and ledger_piloto not in protegidos:
        protegidos.append(ledger_piloto)
    for m in mg:
        for raiz in protegidos:
            if m["destino"] != raiz and m["destino"].startswith(raiz.rstrip("/") + "/"):
                fallos.append(
                    f"I12: `{m['destino']}` se monta DENTRO de `{raiz}`: un fichero "
                    f"forjado ahí encima suplanta al testigo, y el `:ro` no protege "
                    f"—protege de escribirlo, no de que sea otro")

    # ── I10 · el token del piloto es SUYO, no el de la flota.
    #
    # `LLMINBOX_TOKEN` es obligatorio (el servicio arranca mudo sin él) y su
    # portador dispara el fail-open de `exige_ser` (`servicio.py:3494-3496`): sin
    # identidad, anota y DEJA PASAR. El piloto existe para probar que no hay
    # escrituras operativas anónimas; heredar el token de la flota mete dentro
    # justo la credencial que las hace anónimas — y `SECURITY.md` ya publica que
    # loopback NO es aislamiento en Docker Desktop macOS.
    token = str(entorno_gw.get("LLMINBOX_TOKEN", ""))
    if not token:
        fallos.append("I10: el gateway no recibe `LLMINBOX_TOKEN` y el servicio "
                      "arranca mudo sin él")
    elif token_var is None:
        # Modo RELAJADO: para el verificador de dos lanes, que exige nombre
        # ÚNICO por lane, no un nombre FIJO — exigir aquí el mismo nombre en
        # las dos lanes sería exigir el cruce que esa entrega existe para
        # prohibir. La unicidad entre lanes la comprueba
        # `pilot_topologia_dual.verificar_dual` (`X2_IDENTIDAD`), no aquí.
        m = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*):\?[^{}]*\}", token.strip())
        if not m:
            fallos.append(
                f"I10: `LLMINBOX_TOKEN` es {_forma_de(token)} y tiene que ser una "
                f"indirección OBLIGATORIA de la forma `${{VAR:?<motivo>}}` — sin "
                f"default y sin componer con nada. El valor NO se reproduce a "
                f"propósito: este mensaje acaba en el log del gate y en el ledger")
        elif "LLMINBOX_TOKEN" in token.replace(m.group(1), ""):
            fallos.append(f"I10: el valor compone con `LLMINBOX_TOKEN` "
                          f"({_forma_de(token)}): hereda el token de la flota por la "
                          f"puerta del default. El valor NO se reproduce")
    elif not re.fullmatch(rf"\$\{{{re.escape(token_var)}:\?[^{{}}]*\}}", token.strip()):
        # La forma se exige ENTERA, no por prefijo. `${LLMINBOX_PILOT_TOKEN:-x}`
        # casaba con la aguja vieja y convierte el fail-closed en fail-open: quien
        # no ponga la variable arranca con un token conocido. Y
        # `${LLMINBOX_PILOT_TOKEN:-${LLMINBOX_TOKEN}}` casaba TAMBIÉN, reabriendo
        # por la puerta del default justo la herencia que P1-4 cerró.
        fallos.append(
            f"I10: `LLMINBOX_TOKEN` es {_forma_de(token)} y la única forma admitida "
            f"es `${{{token_var}:?<motivo>}}` — obligatoria, sin default y "
            f"sin componer con nada. El valor NO se reproduce a propósito: este "
            f"mensaje acaba en el log del gate y en el ledger")
    elif "LLMINBOX_TOKEN" in token.replace(token_var, ""):
        fallos.append(f"I10: el valor compone con `LLMINBOX_TOKEN` "
                      f"({_forma_de(token)}): hereda el token de la flota por la "
                      f"puerta del default. El valor NO se reproduce")

    # ── I11 · las cuatro perillas de testigo VIAJAN.
    #
    # Una perilla que el preflight lee y el compose no pasa es INERTE: se puede
    # poner, el arranque no protesta y no hace nada. Este repo ya tiene un guarda
    # para esa clase en su otro compose (`test_ninguna_perilla_es_inerte.py`).
    for clave in ("LLMINBOX_JOURNAL_VOLUME_ID", "LLMINBOX_JOURNAL_VOLUME_WITNESS",
                  "LLMINBOX_LEDGER_PILOTO_ID", "LLMINBOX_LEDGER_PILOTO_WITNESS"):
        if not entorno_gw.get(clave):
            fallos.append(f"I11: `{clave}` no viaja al contenedor: el preflight la "
                          f"lee y sin ella se para, o peor, no discrimina")

    return fallos


# ── API PÚBLICA para otros verificadores (p.ej. `pilot_topologia_dual.py`) ──
#
# Hallazgo de revisión estática (CFO/ADR-002, temprana): un módulo externo
# llamando a `_normaliza_montajes`/`_partir_montaje` con el guion bajo se
# apoya en una función que este fichero puede renombrar o mover sin previo
# aviso — el guion bajo es justo la señal de «no es contrato». Se exponen
# alias PÚBLICOS y nada más: la implementación sigue siendo una sola, así que
# no hay lógica que pueda divergir entre la copia interna y la externa.
normaliza_montajes = _normaliza_montajes
partir_montaje = _partir_montaje
