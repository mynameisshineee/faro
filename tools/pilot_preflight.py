#!/usr/bin/env python3
"""Guarda FAIL-CLOSED del arranque y de la readiness del piloto M1 (ADR-001).

Cierra DOS ausencias medidas y publicadas por separado el 2026-09-05, y que son un
`AND` — ninguna cubre a la otra:

  ⓐ  «Startup fails closed if `coordination.sqlite` is not on the configured durable
      volume» (ADR §Storage). Medido por @db-migrations sobre `coordination.py`:
      `0` implementación. Aquí se implementa comparando el DISPOSITIVO del journal
      con el del raíz: un fichero en el FS del contenedor comparte `st_dev` con `/`
      y se evapora en el `recreate` que el propio ADR exige como gate de despliegue.

  ⓑ  El testigo de identidad de volumen. ⓐ no basta: un volumen NUEVO, VACÍO y con
      el nombre correcto está «on the configured durable volume» y pasaría en verde
      sobre cero eventos. Y no es hipotético — el `04-09` nació
      `1bb10f55e868_llminbox-data` de un `compose` crudo desde el directorio de
      despliegue, con el índice a `155 KB`. El nombre del volumen lo decide el
      project name; la identidad la decide este testigo.

Todo lo que comprueba está inyectado por parámetro (`entorno`, `stat_fn`) para que
la prueba pueda montar el caso ROJO sin necesitar un contenedor. Un verificador que
sólo se puede correr donde ya funciona no se puede falsar.

  uso:  pilot_preflight.py [--init] [--readiness]
        --init       crea el testigo si el volumen está estrenándose (journal ausente)
        --readiness  modo sonda: mismas comprobaciones, nunca escribe
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import secrets
import signal
import stat as statmod
import sys
import uuid
from datetime import datetime, timezone

# El pepper corto no es pepper: HMAC admite cualquier longitud y no protesta, así
# que el suelo lo pone esto o no lo pone nadie.
# Tope del mapa de credenciales. No es una política de tamaño: es el suelo para que
# una lectura EXACTA pueda decir «esto es más grande de lo que puedo leer de una
# vez» en vez de devolver un prefijo cuyo `sha256` no casaría nunca y culpar al
# atestado de un fallo de lectura.
MAPA_MAX_BYTES = 1 << 20

PEPPER_MIN_BYTES = 32
PROJECTOR_FRAME_KEY_MIN_BYTES = 32
PROJECTOR_FRAME_KEY_MAX_BYTES = 4096

# Capacidades reconocidas. Una capacidad desconocida es ROJA y no se ignora: el
# modo de fallo de «ignoro lo que no entiendo» es que un typo abre el paso a una
# entrada que el operador creía capada.
CAPACIDADES = {
    "session", "events.write", "events.ack", "events.index", "leases",
    "commands.submit", "commands.advance", "outbox.project", "outbox.operate",
    "admission.operate",
    "runtime.observe", "runtime.recover", "runtime.read",
    "organization.read", "organization.activate",
}

TESTIGO = ".volume-id"
TESTIGO_LEDGER = ".llminbox-ledger-id"

# La SEÑAL de que un directorio es de verdad un ledger, y no cualquier carpeta que
# alguien puso en `LLMINBOX_PILOT_LEDGER_HOST`. El testigo acredita «éste es el
# directorio que se estrenó»; sin señal no acredita «éste es el ledger del carril»,
# y una ruta mal puesta LA PRIMERA VEZ se adoptaba para siempre — todo arranque
# posterior salía verde sobre el directorio equivocado.
SENAL_LEDGER = "LEDGER" + ".md"

# Formato del testigo. El `nonce` es lo que hace que dos volúmenes estrenados con
# el MISMO id declarado no puedan estar los dos verdes: cada estreno genera el
# suyo, y sólo la huella declarada por el operador casa con UNO.
TESTIGO_V = 2


def _leer_fd(fd: int, ruta: str, que: str, tope: int = 4096,
             exacto: int | None = None) -> bytes:
    """Lectura POR DESCRIPTOR con el error TIPADO, y con el final del fichero
    como caso NOMBRADO en vez de como silencio.

    🩸 NO-GO del auditor: `os.read(fd, …)` iba desnudo. Un `EIO` real —disco que
    se va, volumen que desaparece— subía `OSError` CRUDO, y como `readiness`
    sólo capturaba `Rojo`, se escapaba de la red entera: medido, `kill_fn` con
    `0` llamadas y el PID 1 VIVO con el health en rojo. Un fail-closed que sólo
    cierra ante los errores que él mismo inventa no es fail-closed.

    Se lee en BUCLE porque `read(2)` puede devolver MENOS de lo pedido sin que
    haya terminado el fichero: el corte parcial daba un JSON truncado y el Rojo
    salía por «formato», culpando al testigo de una lectura incompleta.
    """
    if exacto is not None:
        # 🩸 AUDITORÍA sobre `d3912e6`: para el TESTIGO no vale ensamblar. Yo lo
        # defendí —«`read(2)` puede devolver menos sin que el fichero termine»—
        # y para un fichero de ~100 bytes en un volumen local eso NO es la ruta
        # normal: es la señal de que algo va mal con el almacén que estamos
        # acreditando. Ensamblar convertía esa señal en verde. Conocemos el
        # tamaño por `fstat` sobre el MISMO descriptor, así que se exige la
        # lectura COMPLETA de una vez y cualquier corte es Rojo — y en la sonda,
        # un corte.
        if exacto <= 0:
            raise Rojo(f"{que} `{ruta}` está VACÍO ({exacto} bytes): cero bytes no "
                       f"acreditan nada")
        if exacto > tope:
            raise Rojo(f"{que} `{ruta}` ocupa {exacto} bytes y el tope son {tope}: "
                       f"un testigo no es un fichero grande, y leer un prefijo daría "
                       f"una huella de otra cosa")
        try:
            datos = os.read(fd, exacto)
        except OSError as e:
            raise Rojo(f"{que} `{ruta}` no se puede leer: {type(e).__name__}: "
                       f"{e.strerror or e}")
        if len(datos) != exacto:
            raise Rojo(f"{que} `{ruta}`: lectura PARCIAL — {len(datos)} bytes de "
                       f"{exacto}. No se ensambla a propósito: en un testigo de "
                       f"este tamaño un corte no es la ruta normal, es la señal de "
                       f"que el almacén que estoy acreditando va mal")
        return datos
    trozos, leidos = [], 0
    while leidos < tope:
        try:
            t = os.read(fd, tope - leidos)
        except OSError as e:
            raise Rojo(f"{que} `{ruta}` no se puede leer: {type(e).__name__}: "
                       f"{e.strerror or e}")
        if not t:                     # EOF legítimo
            break
        trozos.append(t); leidos += len(t)
    datos = b"".join(trozos)
    if not datos:
        raise Rojo(f"{que} `{ruta}` está VACÍO: cero bytes no acreditan nada, y "
                   f"leerlo como «formato inválido» culpa al contenido de una "
                   f"lectura que no trajo ninguno")
    return datos


def _leer_bytes(ruta: str, que: str) -> bytes:
    """Lectura con el error TIPADO. Un `open()` sin guardar sube un
    `PermissionError` crudo: traceback en vez del `Rojo` con nombre, que es
    exactamente lo que el docstring de `Rojo` dice que no puede pasar."""
    try:
        with open(ruta, "rb") as fh:
            return fh.read()
    except OSError as e:
        raise Rojo(f"{que} `{ruta}` no se puede leer: {type(e).__name__}: "
                   f"{e.strerror or e}")


class Rojo(Exception):
    """Fallo de precondición. El mensaje NOMBRA el motivo: un fail-closed mudo
    entrena a quien lo sufre a saltárselo."""


# Los errno con los que el ancla se niega. NO son «los del enlace», y por eso el
# nombre no lo dice: `O_NOFOLLOW` sobre un enlace da `ELOOP` en Linux, `ENOTDIR`
# en Darwin cuando además se pide `O_DIRECTORY` (medido: `errno=20`), y `EMLINK`
# en varios BSD — pero `ENOTDIR` es TAMBIÉN el de «esto es un fichero». Cablear
# uno solo deja la guarda muda en la otra plataforma; no discriminar después
# hace que un fichero corriente se anuncie como enlace.
ERRNO_ANCLA_INVALIDA = (errno.ELOOP, errno.EMLINK, errno.ENOTDIR)


def _abrir_directorio(directorio: str, que: str) -> int:
    """El descriptor de directorio, ANCLADO y sin seguir enlaces.

    🩸 Ésta era la mitad que faltaba. `49379af` puso `O_NOFOLLOW` en el testigo y
    dejó el `os.open(directorio, O_RDONLY|O_DIRECTORY)` sin él: si el DIRECTORIO
    del almacén es un enlace, el descriptor queda anclado FUERA y todo lo que
    cuelga de él —lectura, `O_EXCL`, `fstat`, `fsync`— se hace en el sitio
    equivocado con las banderas correctas. Medido antes de tocar nada sobre un
    volumen vacío y un enlace a otro directorio: `OUTSIDE_CREATED=True`,
    `INSIDE_VOL=False`, y `--init` devolviendo huella. Es el mismo defecto de
    `49379af` un nivel más arriba: allí escapaba por el fichero, aquí por la
    carpeta.

    🩸 AUDITORÍA sobre `d3912e6` — aquí decía «⚠️ LÍMITE DECLARADO» y lo dejaba
    abierto. `O_NOFOLLOW` en UN `os.open` gobierna el ÚLTIMO componente de la ruta;
    los de en medio los resuelve el kernel sin decir nada. Medido por @security
    (22:47:41Z, H1, Linux con volumen Docker REAL — su primera corrida fue en un
    `tmpdir` de macOS y él mismo la retiró por no ser la población):

        ① último componente enlace .. con `O_NOFOLLOW` -> OSError   ✅ ya cerrado
        ② componente INTERMEDIO ..... con `O_NOFOLLOW` -> ABRE      ← ESTO cierra
        ③ ¿`st_dev` lo separa? ...... dos rutas del mismo fs lo comparten ❌ no

    El activador de ② no es `/ledgers`: es una ruta configurada un nivel más abajo
    (`/journal/sub/…`), donde el enlace vive DENTRO del volumen y **lo puede poner
    cualquiera con escritura en él**. Se cierra RECORRIENDO la ruta componente a
    componente con `openat(dirfd, parte, O_DIRECTORY|O_NOFOLLOW)`: cada eslabón se
    abre relativo al descriptor del anterior, así que la bandera gobierna TODOS y
    no sólo el último, y el TOCTOU no se reabre porque entre dos eslabones no hay
    ninguna resolución por ruta.

    `..` se rechaza en vez de recorrerse: un `..` component-wise no es el `..` que
    resuelve el kernel sobre la ruta entera —depende de por dónde entraste—, así
    que aceptarlo sería recorrer una ruta distinta de la configurada.

    ⚠️ LO QUE ESTO **NO** CIERRA, y su cota:
    - ③ sigue en pie: `st_dev` no separa dos rutas del mismo sistema de ficheros.
    - El invariante de PUNTO DE MONTAJE (`fstat(fd)` frente a `stat("..",
      dir_fd=fd)`) sigue **sin implementar**. Sería un cambio de CONTRATO —pasaría
      a rechazar todo directorio que no sea un montaje— y eso le toca al dueño del
      protocolo, no a esta correctiva. Queda como gate aparte, en
      `docs/PILOT-M1-TOPOLOGY.md`.
      ⚠️ En una versión anterior este comentario afirmaba que en macOS daría falsos
      negativos por los firmlinks de APFS. **No lo he medido**, así que lo retiro:
      es una hipótesis sobre el comportamiento del gate, no un resultado, y una cota
      inventada justifica no construirlo tan bien como una medida — pero sin nada
      detrás.
    - 🔻 El recorrido es más ESTRICTO que el `os.open` de antes: una ruta cuyo
      componente intermedio sea un enlace ahora es Rojo aunque el destino fuera
      legítimo. En el contenedor las dos rutas que llegan (`/journal`,
      `/ledgers/llminbox`) no atraviesan enlaces; en un macOS de desarrollo,
      `/tmp` y `/var` SÍ lo son, así que quien monte un banco a mano tiene que
      pasar la ruta ya resuelta (`/private/tmp/…`). Es la dirección segura del
      error, y se dice para que no sorprenda.
    """
    partes = [p for p in directorio.split("/") if p not in ("", ".")]
    if ".." in partes:
        raise Rojo(f"ⓑ la ruta del {que} `{directorio}` lleva `..`: el recorrido "
                   f"anclado no lo puede seguir sin dejar de ser el de la ruta "
                   f"configurada, y una ruta de almacén no tiene por qué llevarlo")
    absoluta = directorio.startswith("/")
    try:
        desde = os.open("/" if absoluta else ".", os.O_RDONLY | os.O_DIRECTORY)
    except OSError as e:
        raise Rojo(f"ⓑ no puedo abrir la raíz del recorrido de `{directorio}` "
                   f"({que}): {type(e).__name__}: {e.strerror or e}")
    recorrido = "" if absoluta else "."
    try:
        for parte in partes:
            recorrido = f"{recorrido}/{parte}"
            try:
                siguiente = os.open(parte, os.O_RDONLY | os.O_DIRECTORY |
                                    os.O_NOFOLLOW, dir_fd=desde)
            except OSError as e:
                raise _rojo_de_componente(e, parte, recorrido, directorio, que,
                                          desde)
            os.close(desde)
            desde = siguiente
    except BaseException:
        os.close(desde)
        raise
    return desde


def _rojo_de_componente(e: OSError, parte: str, recorrido: str, directorio: str,
                        que: str, padre: int) -> Rojo:
    """El Rojo del eslabón que falló, NOMBRÁNDOLO.

    El `lstat` sólo REDACTA el mensaje —la decisión ya está tomada y no depende de
    él— y va anclado al `dirfd` del padre, así que ni siquiera para redactar se
    vuelve a resolver por ruta.
    """
    if e.errno not in ERRNO_ANCLA_INVALIDA:
        return Rojo(f"ⓑ el componente `{recorrido}` de la ruta del {que} "
                    f"`{directorio}` no se puede abrir: {type(e).__name__}: "
                    f"{e.strerror or e}")
    try:
        es_enlace = statmod.S_ISLNK(os.lstat(parte, dir_fd=padre).st_mode)
    except OSError:
        es_enlace = False
    if es_enlace:
        intermedio = recorrido.rstrip("/") != "/" + "/".join(
            x for x in directorio.split("/") if x not in ("", "."))
        return Rojo(
            f"ⓑ el componente `{recorrido}` de la ruta del {que} `{directorio}` es "
            f"un ENLACE simbólico"
            + (" — y es INTERMEDIO, que es justo el hueco que dejaba abierto un "
               "`O_NOFOLLOW` sobre la ruta entera: la bandera gobierna el ÚLTIMO "
               "componente y los de en medio los resolvía el kernel en silencio"
               if intermedio else "")
            + ": el descriptor quedaría anclado FUERA del almacén y el testigo se "
              "estrenaría en el destino del enlace, no en el almacén — con la "
              "huella devuelta como si hubiera estrenado")
    return Rojo(f"ⓑ el componente `{recorrido}` de la ruta del {que} "
                f"`{directorio}` no es un directorio anclable —o no es un "
                f"directorio, o atraviesa demasiados enlaces—: "
                f"{type(e).__name__}: {e.strerror or e}")


def _escribir_todo(fd: int, cuerpo: bytes) -> None:
    """`os.write` puede escribir MENOS de lo que se le da y devolver cuánto.

    Ignorar ese número no deja el testigo «casi bien»: la huella se calcula sobre
    el cuerpo COMPLETO y en el disco hay un prefijo, así que `--init` imprime un
    `_WITNESS` que **ningún arranque posterior puede casar jamás**. Medido con un
    `write` recortado a 10 bytes: `39e3b6ed…` declarado frente a `7ba4fe84…` en
    disco. No es un fallo transitorio, es un almacén tapiado."""
    visto = 0
    while visto < len(cuerpo):
        n = os.write(fd, cuerpo[visto:])
        if n <= 0:
            raise OSError(errno.EIO, f"write devolvió {n} con {len(cuerpo) - visto} "
                                     f"bytes pendientes")
        visto += n


def _verificar_estreno(nfd: int, dirfd: int, n_cuerpo: int) -> str:
    """Mira POR DESCRIPTOR lo que el estreno acaba de crear. Devuelve el motivo
    del rechazo, o `""` si el objeto es lo que dice ser.

    🩸 NO-GO del auditor sobre `525efff`, y tiene razón: el camino de LECTURA
    validaba `S_ISREG`, el modo y el dispositivo sobre `fstat(fd)`, y el camino de
    CREACIÓN **no miraba nada**. Devolvía la huella de `cuerpo` —lo que quisimos
    escribir— sin haber inspeccionado jamás el objeto. Es la misma clase que vengo
    curando toda la entrega: una guarda cuyo SUJETO nunca se mide.

    Y la comprobación de tamaño no es adorno ni duplica al bucle de escritura, que
    es lo primero que se piensa. Medido: un `os.write` que devuelve `len(b)` y
    escribe `10` deja el bucle SATISFECHO —no tiene forma de saberlo— y el estreno
    publica una huella de `107 B` sobre un fichero de `10`. El bucle mira el VALOR
    QUE DEVUELVE la llamada; esto mira el OBJETO. Son dos instrumentos, no uno
    repetido, y sólo el segundo cierra el caso de arriba.
    """
    st = os.fstat(nfd)
    if not statmod.S_ISREG(st.st_mode):
        return f"no es un fichero regular (st_mode={st.st_mode:#o})"
    modo = statmod.S_IMODE(st.st_mode)
    if modo != 0o444:
        return (f"quedó en modo {modo:04o} y el arranque exige `0444`: el `fchmod` "
                f"no tomó, y el rojo saldría en el arranque SIGUIENTE en vez de aquí")
    if st.st_size != n_cuerpo:
        # ⚖️ @security propone que, firmando lo releído, esta comprobación «pasa a
        # ser redundante». **No lo es, y lo corrijo hacia arriba**: el `pread` lee
        # una VENTANA de `n_cuerpo` bytes. Si el fichero es más CORTO, la huella
        # sale de lo poco que haya y casa consigo misma — verde sobre un testigo
        # truncado. Si es más LARGO, mi ventana firma un PREFIJO mientras el
        # camino de lectura hace `os.read(fd, 4096)` y firma de más: dos huellas
        # distintas del mismo fichero. Esto es lo que garantiza que la ventana del
        # `pread` sea el fichero ENTERO, así que se queda y con su falsador.
        return (f"tiene {st.st_size} bytes en disco y el cuerpo son {n_cuerpo}: la "
                f"huella describiría algo que no está escrito, y ningún arranque "
                f"posterior podría casarla")
    if st.st_dev != os.fstat(dirfd).st_dev:
        return (f"vive en el dispositivo {st.st_dev} y su directorio en "
                f"{os.fstat(dirfd).st_dev}: hay algo montado encima de lo que "
                f"acabo de crear")
    return ""


def _retirar(nombre: str, dirfd: int) -> tuple[bool, str]:
    """Quita un testigo que no se pudo garantizar. Un testigo a medias que se
    queda es peor que ninguno: el arranque siguiente lo LEE, pasa por el camino
    de lectura y devuelve su huella — tapando el fallo que acaba de ocurrir.

    Devuelve `(quitado, motivo)`. El MOTIVO viaja porque un «no he podido
    retirarlo» sin errno manda al operador a adivinar entre un permiso, un `EIO` y
    un fichero que ya no estaba.

    🩸 NO-GO del auditor sobre `a910082`: aquí faltaba el `fsync` del DIRECTORIO.
    `unlink` devuelve éxito cuando el borrado está en el caché de entradas, no
    cuando está en el disco: un corte entre el `unlink` y el `fsync` deja el
    testigo VIVO, que es exactamente el residuo que esta función existe para no
    dejar. Y por eso un `fsync` que falla se devuelve como NO retirado: una
    retirada que no es durable no es una retirada, y decir `True` ahí sería la
    misma clase de mentira que el `except OSError: pass` que ya curé arriba.
    """
    try:
        os.unlink(nombre, dir_fd=dirfd)
    except OSError as e:
        return (False, f"{type(e).__name__}: {e.strerror or e}")
    try:
        os.fsync(dirfd)
    except OSError as e:
        return (False, f"el `unlink` salió bien pero la ENTRADA del directorio no "
                       f"se pudo asegurar ({type(e).__name__}: {e.strerror or e}): "
                       f"el borrado puede no sobrevivir a un corte")
    return (True, "")


def _entorno(entorno=None) -> dict:
    return os.environ if entorno is None else entorno


def comprobar_volumen_durable(ruta_journal: str, *, raiz: str = "/", stat_fn=None) -> str:
    """ⓐ — el journal tiene que vivir en un dispositivo DISTINTO del raíz."""
    stat_fn = stat_fn or os.stat
    directorio = os.path.dirname(ruta_journal) or "/"
    try:
        st_j = stat_fn(directorio)
    except OSError as e:
        raise Rojo(f"ⓐ el directorio del journal `{directorio}` no es accesible: {e}")
    st_r = stat_fn(raiz)
    if st_j.st_dev == st_r.st_dev:
        raise Rojo(
            f"ⓐ `{ruta_journal}` está en el MISMO dispositivo que `{raiz}` "
            f"(st_dev={st_j.st_dev}): no es un volumen montado, es el sistema de "
            f"ficheros del contenedor — se destruye en el próximo `recreate`, que es "
            f"justo el gate de despliegue que pide el ADR")
    return directorio


def comprobar_testigo(directorio: str, esperado: str, *, huella_esperada: str = "",
                      init: bool = False, ruta_journal: str | None = None,
                      nombre: str = TESTIGO, que: str = "volumen",
                      var_id: str = "LLMINBOX_JOURNAL_VOLUME_ID",
                      dirfd: int | None = None,
                      creado_out: list | None = None) -> str:
    """ⓑ — el almacén montado tiene que ser EL almacén, no uno con su nombre.

    Devuelve la HUELLA (sha256 del testigo), no el id: el id lo elige el operador y
    se puede repetir; la huella nace en el estreno y no.

    🩸 TODO PASA POR UN DESCRIPTOR DE DIRECTORIO, y el motivo es un defecto que tuvo
    esta función: el estreno hacía `os.path.exists()` y luego `open(ruta, "wb")`.
    **`exists()` SIGUE los enlaces**, así que con un `.volume-id` apuntando a un
    fichero inexistente devolvía `False` —«no hay testigo, estrénalo»— y el `open`
    **seguía el enlace y creaba el objetivo FUERA del volumen**, en `0444`, dejando
    el enlace intacto. `--init` devolvía huella como si hubiera estrenado y la
    guarda ⓑ quedaba desactivada desde el minuto cero: el testigo del volumen vivía
    en otro sitio. Entre aquel `exists()` y aquel `open` cabía además otro proceso.

    Ahora: `openat` anclado al directorio, `O_NOFOLLOW` para que un enlace sea un
    error y no un desvío, `O_EXCL` para que quien llegue segundo en una carrera
    pierda en vez de pisar, y `fsync` del fichero **y del directorio** —sin lo
    segundo la entrada puede no sobrevivir a un corte, y un testigo que se evapora
    es un volumen que mañana parece sin estrenar.

    🩸 Y EL ANCLA TAMBIÉN VA SIN SEGUIR ENLACES (`_abrir_directorio`), que era la
    mitad que faltaba: anclar al descriptor no sirve de nada si el descriptor se
    obtuvo siguiendo un enlace, porque entonces todas estas banderas son correctas
    **sobre el directorio equivocado** — la peor forma del defecto, con todas las
    guardas en verde. El `fsync` del directorio, además, ya no se traga su error:
    era la única línea que sostenía la frase de arriba y vivía bajo un
    `except OSError: pass`.

    ⚠️ LÍMITE DECLARADO Y FAIL-CLOSED: esto detecta el volumen vacío, el montaje
    cruzado, el estreno duplicado, el enlace y la carrera. **No detecta una COPIA
    byte a byte del testigo**: quien puede copiarlo ya está dentro del volumen.
    Atarlo al dispositivo no sirve —dos volúmenes nombrados del mismo motor
    comparten `st_dev`— y un control que no discrimina es peor que no tenerlo.
    """
    if not esperado:
        raise Rojo(f"ⓑ falta `{var_id}`, el id esperado del {que}: sin identidad "
                   f"declarada, cualquier almacén vacío con el nombre correcto pasa "
                   f"en verde")
    # 🩸 El descriptor se REUTILIZA si el llamante ya lo abrió y validó. Reabrir
    # por ruta después de haber acreditado un `dirfd` reabre exactamente el
    # TOCTOU que ese `dirfd` existe para cerrar: entre la validación y la
    # reapertura cabe un cambio de la ruta. `estrenar` valida root y child por
    # descriptor y baja EL MISMO aquí.
    propio = dirfd is None
    if propio:
        dirfd = _abrir_directorio(directorio, que)
    try:
        ruta = os.path.join(directorio, nombre)
        try:
            fd = os.open(nombre, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=dirfd)
        except FileNotFoundError:
            fd = None
        except OSError as e:
            if e.errno in (errno.ELOOP, errno.EMLINK):
                # Incluye el enlace COLGANTE: con `O_NOFOLLOW` un enlace es un
                # error, no un desvío, así que aquí no se crea nada en su destino.
                raise Rojo(f"ⓑ el testigo `{ruta}` es un ENLACE simbólico: apunta "
                           f"fuera del almacén, así que no acredita al almacén — y "
                           f"si estuviera colgante, estrenarlo escribiría FUERA")
            raise Rojo(f"ⓑ el testigo del {que} `{ruta}` no se puede leer: "
                       f"{type(e).__name__}: {e.strerror or e}")

        if fd is None:
            # 🩸 `lexists`, NO `exists`: es el MISMO `exists()` que `49379af`
            # retiró del testigo, que seguía vivo aquí. `exists()` sigue el
            # enlace, así que un journal que es un enlace COLGANTE daba `False`
            # —«no hay datos, estrena»— y `--init` adoptaba un almacén de
            # procedencia desconocida. H5 de @security (22:47:41Z), con su cura
            # de una línea; el defecto y el remedio son suyos, no míos.
            hay_datos = bool(ruta_journal) and os.path.lexists(ruta_journal)
            if not init:
                if hay_datos:
                    raise Rojo(
                        f"ⓑ hay datos en `{ruta_journal}` y NO hay testigo: el {que} "
                        f"no se está estrenando, así que `--init` no lo puede adoptar "
                        f"a ciegas. Un `--init` aquí firmaría como propio un almacén "
                        f"de procedencia desconocida")
                raise Rojo(
                    f"ⓑ no hay testigo en `{ruta}`: el {que} está VACÍO. Uno nuevo "
                    f"con el nombre correcto pasa la comprobación ⓐ y no contiene "
                    f"nada — es exactamente el resbalón medido el 04-09. Estrénalo "
                    f"con `--init`")
            if hay_datos:
                raise Rojo(
                    f"ⓑ hay datos en `{ruta_journal}` y NO hay testigo: el {que} no "
                    f"se está estrenando, así que `--init` no lo puede adoptar a "
                    f"ciegas. Un `--init` aquí firmaría como propio un almacén de "
                    f"procedencia desconocida")
            cuerpo = json.dumps({"v": TESTIGO_V, "id": esperado.strip(),
                                 "nonce": secrets.token_hex(16),
                                 "nacido": datetime.now(timezone.utc)
                                 .isoformat(timespec="seconds")},
                                sort_keys=True).encode()
            try:
                # `O_RDWR`, no `O_WRONLY`: releer por el descriptor lo exige, y
                # su sketch de cura no lo decía — con `O_WRONLY` el `pread` da
                # `EBADF` y el estreno muere ENTERO. Falló cerrado, que es lo
                # correcto, pero habría tapiado el camino de estreno. No afloja
                # nada: `O_CREAT|O_EXCL|O_NOFOLLOW` y el modo `0600`→`0444` siguen
                # igual, y quien puede escribir un fichero ya puede releerlo.
                nfd = os.open(nombre,
                              os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                              0o600, dir_fd=dirfd)
            except FileExistsError:
                raise Rojo(
                    f"ⓑ el testigo de `{ruta}` YA EXISTE al ir a crearlo: alguien "
                    f"ganó la carrera entre comprobar y escribir. No lo piso — el "
                    f"segundo estreno creería haberlo puesto él")
            except OSError as e:
                raise Rojo(f"ⓑ no puedo estrenar el testigo en `{ruta}`: "
                           f"{type(e).__name__}: {e.strerror or e}")
            try:
                try:
                    _escribir_todo(nfd, cuerpo)
                    # `fsync` ①, ANTES de endurecer el modo. No es redundante con
                    # el ②: es lo único que sostiene la mitad de H4 que sí cierro
                    # —si el proceso muere entre medias, lo que queda es `0600` y
                    # BORRABLE, no un sólo-lectura atrapado en el volumen—. Lo
                    # vigila M10, que lo demuestra en vez de suponerlo.
                    os.fsync(nfd)
                    # `umask` RECORTA el modo de `O_CREAT`: con `umask 0044` el
                    # testigo nacía `0400` y el arranque SIGUIENTE lo rechazaba
                    # por «tiene modo 0400 y se crea 0444» — un rojo que se
                    # inflige el propio estreno. `fchmod` no pasa por la máscara,
                    # y sólo puede DEVOLVER bits que `umask` quitó: el testigo
                    # nunca queda más abierto que `0444`.
                    #
                    # Y va DESPUÉS del `fsync`, no antes: eso se lleva por delante
                    # media H4 de @security sin cambiar nada más. Si el proceso
                    # MUERE entre el `open` y el `fsync` no corre ningún `except`,
                    # así que `_retirar` no lo salva; lo que queda es el fichero
                    # a medias. Creado `0444` obliga al operador a «borrar a mano
                    # un fichero de sólo lectura dentro del volumen», que es la
                    # frase de su hallazgo. Creado `0600` queda BORRABLE.
                    # ⛔ NO cierra H4 entera: el `--init` siguiente sigue muriendo
                    # con `FileExistsError`. Eso lo cierra un `link()` atómico
                    # desde un temporal, que es un cambio del protocolo de estreno
                    # y NO se hace aquí.
                    os.fchmod(nfd, 0o444)
                    # `fsync` ②, y es del NO-GO: sin él el MODO no es durable. El
                    # modo `0444` no es cosmético, es una GUARDA que el arranque
                    # exige (`modo != 0o444` ⇒ Rojo), así que dejarla sin asegurar
                    # repite exactamente el defecto del `fsync` del directorio:
                    # una guarda cuya supervivencia a un corte no comprueba nadie.
                    # Un corte aquí dejaba el testigo en `0600` y el arranque
                    # siguiente moría acusando al volumen de algo que hizo el corte.
                    os.fsync(nfd)
                    # Y sólo AHORA se mira el objeto, que es lo que faltaba: lo
                    # verificado es exactamente lo que se acaba de hacer durable.
                    motivo = _verificar_estreno(nfd, dirfd, len(cuerpo))
                    # 🩸 P3 de @security (`01:05:00Z`), y es MI propio principio
                    # devuelto contra mí: miraba el objeto —tipo, modo, tamaño,
                    # dispositivo— y después firmaba `sha256(cuerpo)`, o sea EL
                    # BUFFER. **El tamaño es un PROXY del contenido.** Medido por
                    # mi mano: mismos bytes de longitud y contenido distinto ⇒
                    # firmada `707a9d3e…`, disco `e50601e0…`, y VERDE. El modo de
                    # fallo es el que este mismo fichero describe dos líneas más
                    # arriba: el rojo sale en el arranque SIGUIENTE, acusando al
                    # volumen de algo que pasó en el estreno. Ahora se firma lo
                    # RELEÍDO por el descriptor, que es lo mismo que hace el
                    # camino de lectura (`sha256(crudo)`).
                    releido = os.pread(nfd, len(cuerpo), 0)
                    # ⚖️ Y CORRIJO SU CURA HACIA ARRIBA: firmar lo releído es
                    # NECESARIO y no suficiente. Con sólo eso, unos bytes
                    # distintos de la MISMA longitud producen una huella que casa
                    # con el disco —medido: `e50601e0…` firmada y en disco— así
                    # que el estreno sale verde FIRMANDO BASURA, y sólo revienta
                    # en el arranque siguiente al parsear el JSON: otra vez el
                    # rojo diferido que esta entrega existe para matar. Comparar
                    # lo releído con lo que compuse es lo que hace el estreno
                    # honesto, y con el tamaño ya comprobado el fichero queda
                    # acreditado byte a byte.
                    if releido != cuerpo:
                        motivo = motivo or (
                            "lo que hay en disco NO es lo que compuse: mismo "
                            "tamaño y bytes distintos, así que la huella casaría "
                            "consigo misma y el fallo saldría en el arranque "
                            "siguiente al parsear el testigo")
                finally:
                    # El `finally` interior es lo que impide que el descriptor se
                    # escape por una excepción que no sea `OSError`; el `close`
                    # que falla cae al `except` de fuera, que también es un fallo
                    # de durabilidad y no un detalle de limpieza.
                    os.close(nfd)
            except OSError as e:
                quitado, por_que = _retirar(nombre, dirfd)
                raise Rojo(
                    f"ⓑ el testigo de `{ruta}` no se pudo escribir entero ni "
                    f"asegurar: {type(e).__name__}: {e.strerror or e}. "
                    + ("Lo he retirado: vuelve a estrenar."
                       if quitado else
                       f"🔴 Y NO he podido retirarlo ({por_que}): hay un fichero a "
                       f"medias en `{ruta}` que el arranque siguiente leería como "
                       f"testigo válido. Bórralo a mano antes de reintentar."))
            if motivo:
                quitado, por_que = _retirar(nombre, dirfd)
                raise Rojo(
                    f"ⓑ el testigo de `{ruta}` se creó pero NO es lo que dice ser: "
                    f"{motivo}. Devolver la huella aquí sería firmar un estreno que "
                    f"no he mirado. "
                    + ("Lo he retirado: vuelve a estrenar." if quitado else
                       f"🔴 Y NO he podido retirarlo ({por_que}): borra `{ruta}` a "
                       f"mano antes de reintentar."))
            # El directorio TAMBIÉN: sin esto la ENTRADA puede no sobrevivir a un
            # corte y el volumen amanece «sin estrenar» con el journal ya dentro.
            # 🩸 Y su error NO se traga. Estaba en un `except OSError: pass`, así
            # que un `EIO` aquí devolvía huella —medido: `eaf46e84…`— y el
            # operador declaraba un `_WITNESS` cuya durabilidad nadie comprobó.
            # Es la única línea que sostiene la frase de arriba; tragársela deja
            # el comentario diciendo algo que el código ya no hace.
            try:
                os.fsync(dirfd)
            except OSError as e:
                quitado, por_que = _retirar(nombre, dirfd)
                raise Rojo(
                    f"ⓑ el testigo de `{ruta}` se escribió pero la ENTRADA del "
                    f"directorio no se pudo asegurar: {type(e).__name__}: "
                    f"{e.strerror or e}. Sin ese `fsync` el testigo puede no "
                    f"sobrevivir a un corte y el volumen amanece «sin estrenar» "
                    f"con el journal dentro. "
                    + ("Lo he retirado: vuelve a estrenar." if quitado else
                       f"🔴 Y NO he podido retirarlo ({por_que}): borra `{ruta}` a "
                       f"mano antes de reintentar."))
            if creado_out is not None:
                # Marca que ESTA corrida lo acuñó. Sin el dato, revertir en un
                # fallo posterior borraría un testigo ANTERIOR y legítimo —el
                # estreno es idempotente, así que la segunda corrida no crea
                # nada y no tiene derecho a deshacer la primera.
                creado_out.append(True)
            return hashlib.sha256(releido).hexdigest()

        try:
            st_t = os.fstat(fd)
            if not statmod.S_ISREG(st_t.st_mode):
                raise Rojo(f"ⓑ `{ruta}` no es un fichero regular: un bind-mount de "
                           f"un fichero ausente Docker lo sustituye por un "
                           f"DIRECTORIO, y entonces el error sería otro")
            modo = statmod.S_IMODE(st_t.st_mode)
            if modo != 0o444:
                raise Rojo(f"ⓑ el testigo de `{ruta}` tiene modo {modo:04o} y se "
                           f"crea `0444`: uno escribible se puede cambiar entre dos "
                           f"arranques sin dejar rastro")
            if st_t.st_dev != os.fstat(dirfd).st_dev:
                raise Rojo(f"ⓑ el testigo `{ruta}` está montado ENCIMA del almacén "
                           f"(dispositivo {st_t.st_dev} frente a "
                           f"{os.fstat(dirfd).st_dev}): lo puso quien compuso el "
                           f"despliegue, no el estreno")
            crudo = _leer_fd(fd, ruta, que, exacto=st_t.st_size)
        finally:
            os.close(fd)
    finally:
        if propio:
            os.close(dirfd)

    huella = hashlib.sha256(crudo).hexdigest()
    try:
        datos = json.loads(crudo)
        visto = str(datos["id"])
    except Exception:
        raise Rojo(f"ⓑ el testigo de `{ruta}` no tiene el formato v{TESTIGO_V} "
                   f"(id + nonce): montaje cruzado, o un testigo de una versión "
                   f"anterior que no acredita el estreno. Se para")
    if datos.get("v") != TESTIGO_V:
        raise Rojo(f"ⓑ el testigo de `{ruta}` no declara `v={TESTIGO_V}`: es de una "
                   f"forma anterior o escrito a mano, y no acredita un estreno")
    nonce = str(datos.get("nonce", ""))
    if len(nonce) != 32 or any(c not in "0123456789abcdef" for c in nonce):
        raise Rojo(f"ⓑ el `nonce` del testigo de `{ruta}` no es un nonce "
                   f"({len(nonce)} caracteres): un campo relleno para pasar la "
                   f"comprobación de presencia no separa dos estrenos del mismo id")
    # `nacido` tiene que FECHAR. Comprobar sólo que no esté vacío dejaba pasar
    # «ayer por la tarde»: decoración con aspecto de dato.
    crudo_nacido = str(datos.get("nacido", ""))
    try:
        nacido = datetime.fromisoformat(crudo_nacido)
    except Exception:
        raise Rojo(f"ⓑ el `nacido` del testigo de `{ruta}` no es una marca de "
                   f"tiempo ({crudo_nacido!r}): no fecha nada")
    # …y una marca SIN ZONA tampoco fecha un instante: `2026-09-05T21:00:00` es
    # una hora de reloj, y nombra un instante distinto para cada zona de quien la
    # lea. `2026-09-05` a secas también pasa
    # por `fromisoformat` y se vuelve medianoche naive. El estreno la escribe
    # SIEMPRE en UTC (`datetime.now(timezone.utc)`), así que exigir la zona no
    # aprieta al testigo legítimo: sólo caza al escrito a mano. Medido antes de
    # curar: las dos formas pasaban esta guarda.
    if nacido.tzinfo is None or nacido.utcoffset() is None:
        raise Rojo(f"ⓑ el `nacido` del testigo de `{ruta}` no lleva zona horaria "
                   f"({crudo_nacido!r}): sin ella no fecha un instante sino una "
                   f"hora de reloj, y el estreno la escribe siempre en UTC — un "
                   f"testigo sin zona está escrito a mano")
    if visto != esperado.strip():
        raise Rojo(f"ⓑ el {que} montado es `{visto}` y se esperaba "
                   f"`{esperado.strip()}`: montaje cruzado")
    if init:
        return huella
    if not huella_esperada:
        raise Rojo(
            f"ⓑ falta la huella declarada del {que}. Sin ella, el testigo sólo "
            f"repite el id del entorno y DOS almacenes estrenados con el mismo id "
            f"salen los dos verdes. La huella de éste es `{huella}` — decláralo y "
            f"vuelve a arrancar. No se degrada a comparar sólo el id")
    if huella != huella_esperada.strip():
        raise Rojo(f"ⓑ el testigo del {que} no es el declarado: huella "
                   f"`{huella[:16]}…` frente a `{huella_esperada.strip()[:16]}…`. "
                   f"Dos estrenos del mismo id producen huellas distintas: esto es "
                   f"un almacén hermano, no el tuyo")
    return huella


def comprobar_pepper(entorno=None) -> int:
    """El pepper llega por FICHERO. Por entorno es visible en `docker inspect`.

    `O_NONBLOCK` no es adorno: sin él, un FIFO en la ruta del pepper **cuelga el
    `open()` para siempre** —lo cazó su propia prueba, que dejó la suite colgada— y
    entonces el fail-closed deja de ser cerrado y pasa a ser MUDO: el contenedor no
    arranca, el `&&` nunca dispara y nadie sabe por qué. Un fallo que no termina no
    es un fallo seguro, es un fallo sin diagnóstico.

    Se abre con `O_NOFOLLOW` y se mide con `fstat` SOBRE EL DESCRIPTOR: con
    `os.path.isfile` + `os.stat(ruta)` se seguía el enlace y se comprobaba el modo
    del DESTINO, así que un enlace a un fichero ajeno de 40 bytes en `0400` pasaba
    — y lo que acabas usando no es lo que has comprobado.
    """
    env = _entorno(entorno)
    if env.get("LLMINBOX_PEPPER"):
        raise Rojo("pepper por ENTORNO: cualquiera con acceso al demonio lo lee con "
                   "`docker inspect`. Va por fichero (`LLMINBOX_PEPPER_FILE`)")
    ruta = env.get("LLMINBOX_PEPPER_FILE", "")
    if not ruta:
        raise Rojo("falta `LLMINBOX_PEPPER_FILE`")
    try:
        fd = os.open(ruta, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.EMLINK):
            raise Rojo(f"`{ruta}` es un ENLACE simbólico: lo que se comprueba y lo "
                       f"que se lee dejarían de ser el mismo fichero")
        raise Rojo(f"el pepper `{ruta}` no se puede leer: {type(e).__name__}: "
                   f"{e.strerror or e}")
    try:
        st = os.fstat(fd)
        if not statmod.S_ISREG(st.st_mode):
            raise Rojo(f"`{ruta}` no es un fichero regular")
        datos = _leer_fd(fd, ruta, "pepper").strip()
    finally:
        os.close(fd)
    if len(datos) < PEPPER_MIN_BYTES:
        raise Rojo(f"el pepper tiene {len(datos)} bytes y el suelo son "
                   f"{PEPPER_MIN_BYTES}: HMAC acepta cualquier longitud sin protestar")
    modo = statmod.S_IMODE(st.st_mode)
    if modo & 0o077:
        raise Rojo(f"`{ruta}` tiene modo {modo:04o}: legible fuera de su dueño")
    return len(datos)


def comprobar_mapa(entorno=None) -> dict:
    """El mapa de credenciales: montado, ATESTADO y con capacidades explícitas."""
    env = _entorno(entorno)
    ruta = env.get("LLMINBOX_CREDENCIALES", "")
    if not ruta:
        raise Rojo("falta `LLMINBOX_CREDENCIALES`")
    # 🩸 Hallazgo de @security (`MARK:security-retries-no-aplica-al-corte-del-preflight`
    # §3): el mapa se abría como el pepper NO se abre — `os.path.isfile(ruta)` y
    # luego una lectura POR RUTA, que es exactamente el patrón que el docstring de
    # `comprobar_pepper` explica que está mal, doce funciones más arriba.
    #
    # ✅ Su lectura es más fina que un «agujero» y la adopto tal cual: hoy NO hay
    # sustitución posible, porque el `sha256` se calcula sobre los bytes que se
    # LEYERON, así que un enlace a otro fichero cambia el sha y cae en rojo. El
    # defecto es de DEPENDENCIA: la seguridad del mapa colgaba entera del atestado
    # y eso no estaba escrito en ninguna parte. El día que alguien haga
    # `LLMINBOX_CREDENCIALES_SHA` opcional «para desarrollo», la vía del enlace se
    # reabre sola y nadie lo relacionaría con aquel `if`.
    #
    # Se cierra por el lado bueno —el patrón ya estaba escrito— en vez de por el
    # comentario: `O_NOFOLLOW` + `fstat` SOBRE EL DESCRIPTOR + lectura por
    # descriptor. Ahora el mapa no depende del atestado para no ser otro fichero.
    try:
        fd = os.open(ruta, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.EMLINK):
            raise Rojo(f"el mapa `{ruta}` es un ENLACE simbólico: lo que se "
                       f"comprueba y lo que se lee dejarían de ser el mismo fichero")
        raise Rojo(f"el mapa `{ruta}` no se puede abrir: {type(e).__name__}: "
                   f"{e.strerror or e}. Ojo: un bind-mount de fichero ausente Docker "
                   f"lo sustituye por un DIRECTORIO vacío, y entonces el error sería "
                   f"«no es JSON» en vez de «no está»")
    try:
        st = os.fstat(fd)
        if not statmod.S_ISREG(st.st_mode):
            raise Rojo(f"el mapa `{ruta}` no es un fichero regular. Un bind-mount de "
                       f"fichero AUSENTE Docker lo sustituye por un DIRECTORIO "
                       f"vacío: eso es esto, y no «no es JSON»")
        # `exacto` por el mismo motivo que el testigo: el tamaño se conoce por
        # `fstat` del MISMO descriptor, así que un corte es Rojo con nombre en vez
        # de un JSON truncado que culpa al formato.
        crudo = _leer_fd(fd, ruta, "el mapa de credenciales",
                         tope=MAPA_MAX_BYTES, exacto=st.st_size)
    finally:
        os.close(fd)
    sha = hashlib.sha256(crudo).hexdigest()
    esperado = env.get("LLMINBOX_CREDENCIALES_SHA", "")
    if not esperado:
        raise Rojo("falta `LLMINBOX_CREDENCIALES_SHA`: sin atestado, el mapa se "
                   "sustituye en el host después del despliegue y el arranque "
                   "siguiente lo carga sin decir nada")
    if sha != esperado:
        raise Rojo(f"el mapa NO es el atestado: sha256={sha[:16]}… y se esperaba "
                   f"{esperado[:16]}…")
    try:
        mapa = json.loads(crudo)
    except Exception as e:
        raise Rojo(f"el mapa no es JSON: {e}")
    if not isinstance(mapa, dict) or not mapa:
        raise Rojo("el mapa está vacío o no es un objeto")
    # POSICIÓN, jamás un fragmento. `15dc61a` ya quitó esto de `servicio.py`
    # —cambió `credencial {cred[:8]}…` por «entrada de credencial en posición n»—
    # y aquí se había reintroducido por el otro extremo (`cred[-4:]`). El mensaje
    # sale por `stderr` y acaba en `docker logs`: el sufijo de una credencial en un
    # log es la credencial menos cuatro caracteres.
    for n, (cred, v) in enumerate(mapa.items(), start=1):
        etiqueta = f"entrada de credencial en posición {n}"
        if not isinstance(v, dict):
            raise Rojo(f"{etiqueta}: el valor no es un objeto")
        for campo in ("principal", "rol", "carril", "capacidades"):
            if not v.get(campo):
                raise Rojo(f"{etiqueta}: falta `{campo}`. Un principal derivado del "
                           f"rol es admisible en migración, pero tiene que estar "
                           f"ESCRITO — deducirlo aquí lo haría invisible")
        caps = v["capacidades"]
        if not isinstance(caps, list):
            raise Rojo(f"{etiqueta}: `capacidades` no es una lista")
        desconocidas = sorted(set(caps) - CAPACIDADES)
        if desconocidas:
            raise Rojo(f"{etiqueta}: capacidades desconocidas {desconocidas} — un "
                       f"typo no puede degradar a «sin capacidad» en silencio")
        if "session" not in caps:
            raise Rojo(f"{etiqueta}: falta `session`; una credencial sin permiso "
                       f"de abrir sesión no puede entrar en el mapa")
    return mapa


def _comprobar_frame_key_projector(ruta: str) -> int:
    """Lee el frame key por un único descriptor y exige su contrato exacto."""
    required = ("O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC")
    if any(not hasattr(os, name) for name in required):
        raise Rojo("la plataforma no ofrece apertura segura para el frame key")
    if not ruta:
        raise Rojo("projector active exige `LLMINBOX_PROJECTOR_FRAME_KEY_FILE`")
    flags = os.O_RDONLY
    for name in required:
        flags |= getattr(os, name)
    try:
        fd = os.open(ruta, flags)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.EMLINK):
            raise Rojo("el frame key es un ENLACE simbólico")
        raise Rojo(f"el frame key no se puede abrir de forma segura: "
                   f"{type(e).__name__}: {e.strerror or e}")
    try:
        before = os.fstat(fd)
        if not statmod.S_ISREG(before.st_mode):
            raise Rojo("el frame key no es un fichero regular")
        if before.st_uid != os.geteuid():
            raise Rojo("el frame key no pertenece al uid efectivo")
        mode = statmod.S_IMODE(before.st_mode)
        if mode != 0o600:
            raise Rojo(f"el frame key exige modo 0600 exacto; tiene {mode:04o}")
        if not (PROJECTOR_FRAME_KEY_MIN_BYTES <= before.st_size
                <= PROJECTOR_FRAME_KEY_MAX_BYTES):
            raise Rojo("el frame key queda fuera del tamaño permitido")
        chunks, remaining = [], before.st_size
        while remaining:
            chunk = os.read(fd, min(remaining, 64 * 1024))
            if not chunk:
                raise Rojo("el frame key se cortó durante la lectura")
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        identity = lambda st: (st.st_dev, st.st_ino, st.st_size,
                               st.st_mtime_ns, st.st_ctime_ns)
        if sum(map(len, chunks)) != before.st_size or identity(before) != identity(after):
            raise Rojo("el frame key cambió durante la lectura")
        return before.st_size
    finally:
        os.close(fd)


def comprobar_projector(entorno, mapa: dict, piloto: str) -> str:
    """Acredita los selectores y autoridades del projector activo.

    Consume el MISMO mapa ya leído y atestado por ``comprobar_mapa``. No vuelve
    a abrir la ruta, y nunca devuelve credenciales ni bytes del frame key.
    """
    env = _entorno(entorno)
    mode = env.get("LLMINBOX_PROJECTOR_MODE", "disabled")
    if type(mode) is not str or mode not in {"disabled", "active"}:
        raise Rojo("LLMINBOX_PROJECTOR_MODE debe ser exacto: active o disabled")
    if mode == "disabled":
        return "projector: disabled explícito"
    if "LLMINBOX_PROJECTOR_FRAME_KEY" in env:
        raise Rojo("el frame key no puede viajar por entorno; use `_FILE`")

    principal = env.get("LLMINBOX_PROJECTOR_PRINCIPAL_ID", "")
    ledger = env.get("LLMINBOX_PROJECTOR_LEDGER", "")
    if (type(principal) is not str or not principal
            or principal != principal.strip()):
        raise Rojo("projector active exige principal explícito y exacto")
    if type(ledger) is not str or not ledger or ledger != ledger.strip():
        raise Rojo("projector active exige ledger explícito y exacto")

    matches = [spec for spec in mapa.values()
               if spec.get("principal") == principal]
    if len(matches) != 1:
        raise Rojo("el principal del projector debe resolver una credencial exacta")
    worker = matches[0]
    worker_caps = worker.get("capacidades")
    if (type(worker_caps) is not list or len(worker_caps) != 2
            or set(worker_caps) != {"session", "outbox.project"}):
        raise Rojo("el worker exige capacidades exactas session+outbox.project")
    workers = [spec for spec in mapa.values()
               if "outbox.project" in spec.get("capacidades", ())]
    if len(workers) != 1:
        raise Rojo("el mapa exige un único worker con outbox.project")

    operators = [spec for spec in mapa.values()
                 if "admission.operate" in spec.get("capacidades", ())]
    if len(operators) != 1:
        raise Rojo("el mapa exige un único operador de admisión")
    operator = operators[0]
    operator_caps = operator.get("capacidades")
    if (type(operator_caps) is not list or len(operator_caps) != 2
            or set(operator_caps) != {"session", "admission.operate"}):
        raise Rojo("el operador exige capacidades exactas session+admission.operate")
    lane = worker.get("carril")
    if operator.get("carril") != lane:
        raise Rojo("worker y operador deben pertenecer al mismo carril")

    pilot_ledger = os.path.basename(os.path.normpath(piloto))
    if ledger != pilot_ledger:
        raise Rojo("el ledger del projector no coincide con el ledger piloto montado")
    size = _comprobar_frame_key_projector(
        env.get("LLMINBOX_PROJECTOR_FRAME_KEY_FILE", ""))
    return (f"projector: active, autoridades exactas y frame key de {size} bytes "
            "por fichero")


def comprobar_ledgers(entorno=None, *, raiz_ledgers: str = "/ledgers",
                      init: bool = False) -> str:
    """Exactamente UN ledger escribible, Y QUE SEA EL SUYO.

    Contar cuántos son escribibles no dice CUÁL: `LLMINBOX_PILOT_LEDGER_HOST` llega
    por entorno y nadie lo validaba, así que un directorio cualquiera montado en su
    sitio salía verde. El journal tenía testigo y el ledger RW no — una asimetría
    que no tenía motivo, sólo omisión.
    """
    env = _entorno(entorno)
    piloto = env.get("LLMINBOX_LEDGER_PILOTO", "")
    if not piloto:
        raise Rojo("falta `LLMINBOX_LEDGER_PILOTO`")
    if not os.path.isdir(raiz_ledgers):
        raise Rojo(f"`{raiz_ledgers}` no existe: el gateway no tiene ledgers montados")
    escribibles = []
    for nombre in sorted(os.listdir(raiz_ledgers)):
        ruta = os.path.join(raiz_ledgers, nombre)
        if os.access(ruta, os.W_OK):
            escribibles.append(ruta)
    if escribibles != [piloto]:
        raise Rojo(f"ledgers escribibles = {escribibles}, y el único que puede serlo "
                   f"es `{piloto}`. Cada ledger RW de más es radio de daño que el "
                   f"control de carril cruzado ya no puede medir")
    comprobar_testigo(piloto, env.get("LLMINBOX_LEDGER_PILOTO_ID", ""),
                      huella_esperada=env.get("LLMINBOX_LEDGER_PILOTO_WITNESS", ""),
                      init=init, nombre=TESTIGO_LEDGER, que="ledger del piloto",
                      var_id="LLMINBOX_LEDGER_PILOTO_ID")
    return piloto


def preflight(entorno=None, *, init: bool = False, stat_fn=None,
              raiz_ledgers: str = "/ledgers") -> list[str]:
    """Corre las seis comprobaciones. Devuelve evidencia o revienta."""
    env = _entorno(entorno)
    ruta_journal = env.get("LLMINBOX_JOURNAL", "")
    if not ruta_journal:
        raise Rojo("falta `LLMINBOX_JOURNAL`: una perilla de ruta sin valor deja que "
                   "el journal aterrice donde caiga")
    directorio = comprobar_volumen_durable(ruta_journal, stat_fn=stat_fn)
    testigo = comprobar_testigo(
        directorio, env.get("LLMINBOX_JOURNAL_VOLUME_ID", ""),
        huella_esperada=env.get("LLMINBOX_JOURNAL_VOLUME_WITNESS", ""),
        init=init, ruta_journal=ruta_journal)
    bytes_pepper = comprobar_pepper(env)
    mapa = comprobar_mapa(env)
    piloto = comprobar_ledgers(env, raiz_ledgers=raiz_ledgers, init=init)
    projector = comprobar_projector(env, mapa, piloto)
    return [
        f"ⓐ journal en volumen propio: {directorio}",
        f"ⓑ testigo de volumen: huella {testigo[:16]}…",
        f"pepper: {bytes_pepper} bytes por fichero, sin valor en el entorno",
        f"mapa atestado: {len(mapa)} credencial(es) con capacidades explícitas",
        f"ledger RW único y con su testigo: {piloto}",
        projector,
    ]


def _abrir_hijo_directo(rootfd: int, raiz: str, ruta_hija: str, que: str) -> tuple[int, str]:
    """Abre `ruta_hija` como HIJO DIRECTO y CANÓNICO de `raiz`, por descriptor.

    🩸 NO-GO del auditor: el estreno aceptaba cualquier `LLMINBOX_LEDGER_PILOTO`
    y acuñaba `.llminbox-ledger-id` en un directorio arbitrario. Reproducido:
    `estrenar()` devolvió las DOS huellas sobre un directorio cualquiera. La
    única cosa que lo separaba de un ledger era la SEÑAL `LEDGER.md`, y esa
    comprobación iba con `os.path.isfile`, **que sigue enlaces** — plantar un
    enlace a un fichero de fuera bastaba (`isfile -> True`, medido).

    Aquí se exige que la ruta sea `raiz/<un-solo-segmento>`: nada de `..`, ni
    `.`, ni separadores, ni rutas absolutas ajenas. Y se abre con `openat` sobre
    el descriptor del root, así que **ninguna escritura puede salir del root
    autorizado**: el nombre que se resuelve es un segmento, no una ruta.
    """
    raiz_n = os.path.normpath(raiz)
    hija_n = os.path.normpath(ruta_hija)
    padre, base = os.path.split(hija_n)
    if padre != raiz_n or base in ("", ".", "..") or os.sep in base:
        raise Rojo(
            f"{que} `{ruta_hija}` no es hijo DIRECTO de la raíz autorizada "
            f"`{raiz_n}`: el estreno sólo acuña dentro de esa raíz, y una ruta "
            f"que no lo es acuñaría un testigo en un directorio arbitrario — "
            f"reproducido antes de esta guarda")
    try:
        fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                     dir_fd=rootfd)
    except OSError as e:
        if e.errno in ERRNO_ANCLA_INVALIDA:
            raise Rojo(f"{que} `{ruta_hija}` no es un directorio anclable bajo su "
                       f"raíz (¿enlace?): {type(e).__name__}: {e.strerror or e}")
        raise Rojo(f"{que} `{ruta_hija}` no se puede abrir bajo su raíz: "
                   f"{type(e).__name__}: {e.strerror or e}")
    return fd, base


def _exigir_senal(childfd: int, piloto: str) -> None:
    """La SEÑAL del ledger, por descriptor y sin seguir enlaces.

    `os.path.isfile` SIGUE el enlace, así que un `LEDGER.md` que apunta fuera la
    satisfacía y el estreno acuñaba sobre cualquier carpeta. Se abre con
    `O_NOFOLLOW`, se exige REGULAR y se exige que viva en el MISMO dispositivo
    que su directorio: un fichero montado encima no lo puso el carril.
    """
    try:
        fd = os.open(SENAL_LEDGER, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=childfd)
    except FileNotFoundError:
        raise Rojo(
            f"`{piloto}` no parece un ledger: falta `{SENAL_LEDGER}`. ⓐ acota el "
            f"JOURNAL, no el ledger, así que sin esta señal el estreno acuñaría un "
            f"testigo para CUALQUIER directorio — y una ruta mal puesta la primera "
            f"vez queda adoptada para siempre, con todos los arranques en verde")
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.EMLINK):
            raise Rojo(
                f"la señal `{SENAL_LEDGER}` de `{piloto}` es un ENLACE simbólico: "
                f"apunta fuera del ledger, así que no acredita que ESTE directorio "
                f"sea el ledger del carril — plantar el enlace bastaba para que el "
                f"estreno acuñara aquí")
        raise Rojo(f"la señal `{SENAL_LEDGER}` de `{piloto}` no se puede leer: "
                   f"{type(e).__name__}: {e.strerror or e}")
    try:
        st = os.fstat(fd)
        if not statmod.S_ISREG(st.st_mode):
            raise Rojo(f"la señal `{SENAL_LEDGER}` de `{piloto}` no es un fichero "
                       f"regular (st_mode={st.st_mode:#o})")
        if st.st_dev != os.fstat(childfd).st_dev:
            raise Rojo(f"la señal `{SENAL_LEDGER}` de `{piloto}` vive en el "
                       f"dispositivo {st.st_dev} y su directorio en "
                       f"{os.fstat(childfd).st_dev}: está montada ENCIMA, no la "
                       f"puso el carril")
    finally:
        os.close(fd)


def estrenar(entorno=None, *, stat_fn=None, raiz_ledgers: str = "/ledgers") -> dict:
    """Camino de ESTRENO: crea los DOS testigos y devuelve sus huellas.

    Existe porque el fail-closed que entregué antes no tenía puerta: `--init`
    creaba los testigos, pero las dos huellas que el arranque exige **sólo existen
    después de un `--init` que ningún fichero invocaba** —lo midió `@sdet` (⑤)—, y
    encima el gateway pide esas huellas con `:?`, así que ni siquiera se podía
    levantar para estrenarlas. Huevo y gallina, con el operador en medio.

    Corre desde `docker-compose.pilot-estreno.yml`, que monta los mismos volúmenes
    y NO exige las huellas. Es IDEMPOTENTE: con los testigos ya puestos no escribe
    nada, valida que son los suyos y devuelve las mismas huellas.

    🩸 REESCRITO por el NO-GO del auditor, y el orden es la mitad de la cura:
    **se valida TODO —journal y ledger, root, hijo, señal— ANTES de crear NADA.**
    Antes acuñaba el testigo del journal y sólo después miraba el ledger, así que
    un ledger inválido dejaba MEDIO estreno hecho: un testigo huérfano en el
    volumen y ninguna huella en la mano del operador, que es el peor estado
    posible —el volumen ya parece estrenado y nadie tiene su huella—.
    **Un negativo deja CERO testigos.**

    Y todo cuelga de descriptores validados: el root con `O_DIRECTORY|O_NOFOLLOW`,
    el piloto como hijo DIRECTO y canónico abierto con `openat` sobre él, y la
    señal con `O_NOFOLLOW` sobre el hijo. Los mismos descriptores se BAJAN a
    `comprobar_testigo` en vez de reabrir por ruta: reabrir reabriría el TOCTOU
    que estos descriptores existen para cerrar.
    """
    env = _entorno(entorno)
    ruta_journal = env.get("LLMINBOX_JOURNAL", "")
    if not ruta_journal:
        raise Rojo("falta `LLMINBOX_JOURNAL`: no hay nada que estrenar")
    piloto = env.get("LLMINBOX_LEDGER_PILOTO", "")
    if not piloto:
        raise Rojo("falta `LLMINBOX_LEDGER_PILOTO`")

    # ── FASE 1 · VALIDAR. Aquí no se escribe una sola letra. ─────────────────
    directorio = comprobar_volumen_durable(ruta_journal, stat_fn=stat_fn)
    jfd = _abrir_directorio(directorio, "volumen")
    try:
        rootfd = _abrir_directorio(raiz_ledgers, "raíz de ledgers")
        try:
            childfd, _base = _abrir_hijo_directo(rootfd, raiz_ledgers, piloto,
                                                 "el ledger del piloto")
            try:
                _exigir_senal(childfd, piloto)
                # ── FASE 2 · ACUÑAR, con los descriptores YA acreditados ────
                acunado_j: list = []
                huella_j = comprobar_testigo(
                    directorio, env.get("LLMINBOX_JOURNAL_VOLUME_ID", ""),
                    huella_esperada="", init=True, ruta_journal=ruta_journal,
                    dirfd=jfd, creado_out=acunado_j)
                try:
                    huella_l = comprobar_testigo(
                        piloto, env.get("LLMINBOX_LEDGER_PILOTO_ID", ""),
                        huella_esperada="", init=True, nombre=TESTIGO_LEDGER,
                        que="ledger del piloto",
                        var_id="LLMINBOX_LEDGER_PILOTO_ID", dirfd=childfd)
                except BaseException as fallo_del_segundo:
                    # 🩸 AUDITORÍA: validar antes de acuñar no basta, porque el
                    # ACUÑADO mismo puede fallar en el segundo (EIO, carrera,
                    # `ENOSPC`). Si el primero lo creé YO en esta corrida, lo
                    # deshago: si no, queda un `.volume-id` huérfano y el
                    # operador se lleva CERO huellas con el volumen ya
                    # aparentando estrenado — el peor estado, y el que este
                    # camino existe para no producir.
                    #
                    # `BaseException` A PROPÓSITO, y es lo contrario de la red de
                    # `readiness`: aquí no se DECIDE nada sobre la excepción, se
                    # LIMPIA y se relanza. Un Ctrl-C entre los dos acuñados deja
                    # exactamente el residuo que hay que retirar.
                    #
                    # 🩸 NO-GO del auditor sobre `a910082`: aquí se IGNORABA el
                    # `False` de `_retirar`. La limpieza es best-effort por
                    # naturaleza —el `unlink` puede fallar por permisos, por `EIO`,
                    # por un montaje `ro` que apareció en medio— y descartar su
                    # veredicto producía EXACTAMENTE el estado que este bloque
                    # existe para no dejar: testigo huérfano, cero huellas, y ni una
                    # palabra al operador. La rama de fallo de la limpieza era el
                    # único camino sin diagnóstico de todo el estreno.
                    if acunado_j:
                        quitado, por_que = _retirar(TESTIGO, jfd)
                        if not quitado:
                            # `from` encadena la causa ORIGINAL para el traceback, y
                            # además se reproduce en el texto: quien corre el
                            # preflight ve el mensaje, no el traceback.
                            raise Rojo(
                                f"🔴 ESTRENO A MEDIAS Y NO LO HE PODIDO DESHACER. "
                                f"El testigo del ledger falló "
                                f"({type(fallo_del_segundo).__name__}: "
                                f"{fallo_del_segundo}) y al retirar el del journal "
                                f"que YO acuñé en esta corrida tampoco pude "
                                f"({por_que}). Queda un `{TESTIGO}` HUÉRFANO en "
                                f"`{os.path.join(directorio, TESTIGO)}`: el arranque "
                                f"siguiente lo leerá como un estreno bueno y "
                                f"devolverá su huella, tapando este fallo. "
                                f"RECUPERACIÓN MANUAL, en este orden: "
                                f"① borra `{os.path.join(directorio, TESTIGO)}` "
                                f"② comprueba que `{piloto}` no tiene "
                                f"`{TESTIGO_LEDGER}` ③ vuelve a estrenar. NO "
                                f"declares ningún `_WITNESS` de esta corrida."
                            ) from fallo_del_segundo
                    raise
            finally:
                os.close(childfd)
        finally:
            os.close(rootfd)
    finally:
        os.close(jfd)
    return {"LLMINBOX_JOURNAL_VOLUME_WITNESS": huella_j,
            "LLMINBOX_LEDGER_PILOTO_WITNESS": huella_l}


# Lo que el PID 1 del contenedor del piloto TIENE que ser. El gateway arranca
# `python3 … pilot_preflight.py && exec uvicorn …`, así que el proceso `1` es
# `uvicorn`. Se deja configurable porque clavarlo aquí convertiría un cambio de
# arranque en un corte que no se dispara, y eso es peor que un corte de más.
PID1_ESPERADO_POR_DEFECTO = "uvicorn"


def _pid1_acreditado(entorno=None, leer=None) -> tuple[bool, str]:
    """¿Es el PID 1 de ESTE espacio de nombres el proceso del contrato, y estamos
    de verdad en un contenedor?

    🩸 AUDITORÍA sobre `d3912e6` — la versión anterior acreditaba de más por DOS
    puertas, las dos medidas antes de tocar nada:

        LLMINBOX_PID1_ESPERADO=""      -> acreditaba a `/sbin/launchd`   (True)
        cmdline `/usr/bin/notuvicorn`  -> acreditaba                     (True)
        cmdline `/x/uvicorn-wrapper`   -> acreditaba                     (True)

    La primera porque `env.get(clave, defecto)` devuelve la CADENA VACÍA cuando la
    clave existe vacía —el defecto no entra— y `"" in cualquier_cosa` es `True`:
    **una perilla puesta a vacío desarmaba la guarda entera**. La segunda porque
    `esperado not in texto` es SUBCADENA, y `uvicorn` es subcadena de infinitos
    nombres que no son el gateway.

    Ahora: el esperado VACÍO es un fallo con nombre, la identidad se compara
    EXACTA contra el `basename` de `argv[0]`, y además se exige señal de
    contenedor — porque el contrato dice «corta el PID 1 DEL CONTENEDOR», y sin
    esa mitad la guarda acredita un `uvicorn` que sea el init de un host.
    """
    env = _entorno(entorno)
    leer = leer or (lambda ruta: open(ruta, "rb").read())
    esperado = (env.get("LLMINBOX_PID1_ESPERADO") or "").strip()
    if not esperado:
        esperado = PID1_ESPERADO_POR_DEFECTO
        if env.get("LLMINBOX_PID1_ESPERADO") is not None and \
           not (env.get("LLMINBOX_PID1_ESPERADO") or "").strip():
            return (False, "`LLMINBOX_PID1_ESPERADO` está declarada y VACÍA: una "
                           "perilla de identidad sin valor no acredita a nadie, y "
                           "antes acreditaba a CUALQUIERA porque la cadena vacía "
                           "es subcadena de todo. No disparo")
    try:
        cmdline = leer("/proc/1/cmdline")
    except OSError as e:
        return (False, f"no puedo leer `/proc/1/cmdline` ({type(e).__name__}): sin "
                       f"eso no sé a quién le mandaría la señal, y en un host el "
                       f"PID 1 es el init de la máquina")
    argv0 = cmdline.split(b"\x00", 1)[0]
    nombre = os.path.basename(argv0).decode("utf-8", "replace")
    if nombre != esperado:
        # EXACTA, no subcadena: `notuvicorn` y `uvicorn-wrapper` llevan `uvicorn`
        # dentro y no son el gateway.
        return (False, f"el PID 1 es `{nombre}` y el contrato dice `{esperado}` "
                       f"(identidad EXACTA, no subcadena). NO disparo — este "
                       f"proceso no es el gateway del piloto")
    en_contenedor, por_que = _en_contenedor(leer)
    if not en_contenedor:
        return (False, f"el PID 1 se llama `{esperado}` pero {por_que}: el contrato "
                       f"manda cortar el PID 1 DEL CONTENEDOR, y un `{esperado}` "
                       f"que fuera el init de un host se vería igual desde aquí")
    return (True, f"PID 1 acreditado: `{esperado}` exacto, {por_que}")


def _en_contenedor(leer) -> tuple[bool, str]:
    """La otra mitad del contrato: que esto SEA un contenedor.

    Sin ella, `_pid1_acreditado` sólo dice «el PID 1 se llama X», y eso también es
    cierto en un host donde alguien llamó `uvicorn` a su init. Se aceptan las dos
    señales habituales y se dice CUÁL acreditó, para que el motivo se pueda
    auditar en vez de creer.
    """
    try:
        leer("/.dockerenv")
        return (True, "hay `/.dockerenv`")
    except OSError:
        pass
    try:
        cg = leer("/proc/1/cgroup").decode("utf-8", "replace")
    except OSError:
        return (False, "no hay `/.dockerenv` ni puedo leer `/proc/1/cgroup`")
    for marca in ("docker", "containerd", "kubepods", "lxc", "podman"):
        if marca in cg:
            return (True, f"`/proc/1/cgroup` nombra `{marca}`")
    return (False, "ni `/.dockerenv` ni una marca de contenedor en `/proc/1/cgroup`")


def readiness(entorno=None, *, stat_fn=None, raiz_ledgers: str = "/ledgers",
              kill_fn=None, acreditar_fn=None) -> int:
    """Sonda periódica que, en ROJO, CORTA — no sólo avisa.

    @security lo nombró y tenía razón: **Docker no mata un contenedor
    `unhealthy`**. Con `restart: "no"` al lado, un readiness rojo dejaba el
    servicio contestando con la etiqueta puesta: fail-closed en el arranque y
    advisory a partir de ahí. Si el mapa se sustituye, el testigo deja de casar o
    el ledger cambia de identidad **después** de arrancar, seguir sirviendo
    escrituras es exactamente lo que el piloto existe para impedir.

    ⚠️ CORTA ENTERO, también las lecturas, y eso es más ancho de lo que pide el
    ADR (§23: journal-RO sirve lecturas y rechaza mutaciones). La distinción fina
    vive en el middleware del servicio y es de `P4`; desde fuera del proceso, lo
    único que se puede hacer sin mentir es parar. Se elige parar y se dice.
    """
    kill_fn = kill_fn or os.kill
    acreditar_fn = acreditar_fn or _pid1_acreditado
    try:
        preflight(entorno, init=False, stat_fn=stat_fn, raiz_ledgers=raiz_ledgers)
    except Rojo as e:
        motivo = str(e)
    except Exception as e:                    # noqa: BLE001 — ver el docstring
        # 🩸 RED FINAL. Todo lo que NO es `Rojo` se escapaba de aquí: un `EIO` en
        # la lectura del testigo salía como `OSError` crudo y la sonda moría con
        # traceback SIN cortar —medido: `kill_fn` 0 llamadas, PID 1 vivo—. El
        # health quedaba rojo y Docker no mata un contenedor `unhealthy`, así que
        # el servicio seguía aceptando escrituras. Un fail-closed que sólo cierra
        # ante los errores que él mismo define no es fail-closed.
        #
        # ⚠️ `Exception`, NUNCA `BaseException`: tragarse `KeyboardInterrupt` o
        # `SystemExit` convertiría un `docker stop` o un Ctrl-C en un corte del
        # PID 1 decidido por la sonda. La red atrapa fallos, no señales.
        motivo = (f"fallo NO tipado en el preflight ({type(e).__name__}: {e}) — "
                  f"esto es tan rojo como un `Rojo`, y antes se escapaba de la red")
    else:
        return 0
    print(f"🔴 READINESS ROJO — {motivo}", file=sys.stderr)
    puede, por_que = acreditar_fn(entorno)
    if not puede:
        print(f"· NO corto el PID 1: {por_que}", file=sys.stderr)
        print("· el veredicto ROJO se mantiene: quien orqueste esto tiene que "
              "actuar, porque desde aquí no se puede sin arriesgar a un tercero",
              file=sys.stderr)
        return 1
    print("· corto el proceso: un readiness rojo que sólo avisa deja el "
          "servicio aceptando mutaciones con la etiqueta puesta", file=sys.stderr)
    try:
        kill_fn(1, signal.SIGTERM)
    except OSError as k:
        print(f"· no pude cortar el PID 1 ({k}): el contenedor sigue vivo y "
              f"ESTO hay que mirarlo", file=sys.stderr)
    return 1


def main(argv: list[str]) -> int:
    """El cuadro de modos, y los incompatibles se rechazan TODOS en el mismo sitio.

    🩸 Hallazgo de @security (`MARK:security-retries-no-aplica-al-corte-del-preflight`
    §2): `--genera-id` vivía en el `if __name__`, ANTES de `main()` y por tanto
    antes de toda comprobación, así que
    `pilot_preflight.py --readiness --genera-id` imprimía un uuid y salía `0`:
    **healthcheck VERDE sin comprobar una sola precondición.**

    No era explotable con el compose de hoy —los argumentos están fijados ahí y
    nadie los elige desde fuera— y aun así es la única ruta que no debería existir
    en una herramienta cuyo valor entero es el fail-closed: este mismo fichero YA
    guardaba el par menos peligroso (`--init` + `--readiness` ⇒ `2`, «la sonda no
    escribe») y dejaba abierto el que devuelve VERDE sin mirar nada.

    Ahora los modos se declaran juntos y **cualquier par se rechaza**: el fallo deja
    de depender de qué combinación se le ocurrió a quien lo escribió.
    """
    init = "--init" in argv
    readiness_modo = "--readiness" in argv
    genera_id = "--genera-id" in argv
    modos = [n for n, activo in (("--init", init), ("--readiness", readiness_modo),
                                 ("--genera-id", genera_id)) if activo]
    if len(modos) > 1:
        print(f"· {' y '.join(modos)} juntos no: son modos EXCLUYENTES. "
              f"`--readiness` es una sonda que no escribe y no genera; "
              f"`--genera-id` es utilidad de despliegue y no comprueba NADA, así "
              f"que combinarlo con una sonda daría verde sin mirar nada",
              file=sys.stderr)
        return 2
    if genera_id:
        print(uuid.uuid4())
        return 0
    if readiness_modo:
        return readiness()
    if init:
        # `--init` estrena LOS DOS y escupe las dos líneas que hay que declarar.
        # Antes estrenaba de paso, dentro del preflight completo, así que el
        # operador tenía que deducir las huellas de un mensaje de error.
        try:
            salida = estrenar()
        except Rojo as e:
            print(f"🔴 ESTRENO ROJO — {e}", file=sys.stderr)
            return 1
        print("✅ estreno hecho. Declara estas dos líneas y arranca:")
        for k, v in salida.items():
            print(f"{k}={v}")
        return 0
    try:
        for linea in preflight(init=init):
            print(f"· {linea}")
    except Rojo as e:
        print(f"🔴 PREFLIGHT M1 ROJO — {e}", file=sys.stderr)
        return 1
    print("✅ preflight M1: las seis precondiciones se cumplen")
    return 0


if __name__ == "__main__":
    # UNA sola puerta. Que el `argv` no tenga ninguna vía que salte `main()`: ahí
    # es donde vivía el `--genera-id` que devolvía `0` sin comprobar nada.
    raise SystemExit(main(sys.argv[1:]))
