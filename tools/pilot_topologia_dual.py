"""Verificador ESTÁTICO del cruce entre DOS LANES (V1.0, G9 — `docs/V1.0-EXECUTION.md`).

Reutiliza `pilot_topologia.verificar()` sin copiarlo: cada lane tiene que seguir
cumpliendo, por su cuenta, las mismas nueve invariantes de M1 (un ledger RW, el
agente sin montajes, el token propio, etc.), namespaced con el prefijo de la lane
(`"A:I10"`, `"B:I6"`). Lo que este módulo AÑADE son las comprobaciones de CRUCE
que M1 no necesitaba porque sólo existía una lane: que el mismo nombre lógico de
ledger («llminbox», idéntico a propósito en las dos) no se apoye en NINGÚN
recurso físico compartido.

Siete categorías, la lista exacta de G9 (identidad, sesión, ledger, recibo,
cursor de búsqueda, recuperación, volumen) más una comprobación de RED que las
sostiene a todas: sin redes separadas, cualquier cruce que viaje por HTTP queda
disponible a nivel de transporte sin que ninguna de las siete lo permita.

Lo que este fichero NO hace, dicho aquí para que nadie lo lea de más: no resuelve
NINGÚN secreto. Compara el NOMBRE de la variable de entorno que cada lane
referencia (`${LLMINBOX_DUAL_LANE_A_TOKEN:?...}` → `LLMINBOX_DUAL_LANE_A_TOKEN`,
o el valor LITERAL cuando no hay indirección), nunca el secreto que esa variable
contendría en un despliegue real — igual que `I4`/`I10` en `pilot_topologia.py`.
Y la recuperación (`X7`) se comprueba sobre el compose de ESTRENO, en
`tests/pilot/`, porque es el único fichero de este repo que declara una
escritura de identidad: este módulo no la ve.

── LÍMITES DECLARADOS (revisión estática temprana, ADR-002) ──────────────────

1. **Nombre distinto no es VALOR distinto.** `X2..X6` prueban que la lane A y
   la lane B no referencian la MISMA variable ni el mismo literal en el YAML.
   Eso es necesario y NO es suficiente: dos variables con nombres distintos
   pueden recibir, en el `.env` real de un operador, el MISMO valor a mano. Sin
   Docker desplegado no hay un secreto RESUELTO que comparar. La cura —G9,
   ADR-002— es un fingerprint atestado (HMAC sobre el witness con una clave
   por lane, nunca el secreto en claro) verificado en tiempo de arranque; NO
   está implementada aquí. Ver `docs/PILOT-V1-DUAL-LANE-TOPOLOGY.md`
   §pendiente-fingerprint.
2. **`X0_RED` prueba TRANSPORTE, no KERNEL.** Que las dos lanes no compartan
   red demuestra que un cruce por HTTP es irrealizable BAJO ESTE COMPOSE. No
   demuestra que el kernel nativo rechazaría la petición si las redes SÍ
   estuvieran unidas — esa es la responsabilidad de ADR-002's falsador
   single-kernel-adversarial (dos lanes, un proceso, intentos reales por API
   con 404/denial + cero filas observados), que vive en el workstream del
   supervisor nativo, no en éste.
3. **`X6_CURSOR` con la clave vacía en las dos lanes no prueba un cruce
   rechazado.** Prueba que Search está DESMONTADO (fail-closed) en las dos —
   ausencia, no un intento de cruce que el kernel haya denegado.
4. **`X7` no es G7 completo.** Cubre sólo la escritura de identidad del
   ESTRENO (bootstrap del volumen). La recuperación de UN WORKLOAD —
   stale/stopped/recovering con recibo por transición— es del supervisor
   nativo (otro worktree) y queda fuera de este falsador hasta que su API
   esté integrada.
"""
from __future__ import annotations

import re

import pilot_topologia as _m1

# Variables cuyo NOMBRE de indirección tiene que ser distinto entre lane A y
# lane B. Se listan por categoría para que el mensaje de fallo cite la
# categoría de G9, no sólo la clave de entorno.
_CLAVE_IDENTIDAD = ("LLMINBOX_TOKEN",)
_CLAVE_SESION = ("LLMINBOX_CREDENCIALES_SHA",)
_CLAVE_LEDGER = ("LLMINBOX_LEDGER_PILOTO_ID", "LLMINBOX_LEDGER_PILOTO_WITNESS")
_CLAVE_RECIBO = ("LLMINBOX_JOURNAL_VOLUME_ID", "LLMINBOX_JOURNAL_VOLUME_WITNESS")
_CLAVE_CURSOR = ("LLMINBOX_SEARCH_CURSOR_KEY",)

_INDIRECCION = re.compile(
    r"\A\$\{([A-Za-z_][A-Za-z0-9_]*)(?::[-?][^{}]*)?\}\Z|\A\$([A-Za-z_][A-Za-z0-9_]*)\Z")


def _var_referenciada(valor: str) -> str | None:
    """El NOMBRE de la variable dentro de `${NAME...}` o `$NAME`.

    `None` si no es una indirección reconocible en esa forma (un secreto
    literal, o `${A}${B}` compuesto) — eso ya lo caza `I4`/`I10` por lane; aquí
    sin variable que nombrar no hay nada que comparar entre lanes.
    """
    m = _INDIRECCION.fullmatch(valor.strip())
    if not m:
        return None
    return m.group(1) or m.group(2)


def _entorno(srv: dict) -> dict:
    e = (srv or {}).get("environment") or {}
    if isinstance(e, list):
        return dict(p.split("=", 1) for p in e if "=" in p)
    return e


def verificar_dual(compose: dict, *, lane_a: str = "a", lane_b: str = "b",
                    ledger_piloto: str = "/ledgers/llminbox") -> list[str]:
    """Devuelve la lista de violaciones. Lista vacía = las dos lanes están
    aisladas y ninguna reutiliza el nombre lógico compartido como recurso
    físico compartido."""
    fallos: list[str] = []
    servicios = compose.get("services") or {}
    gw_a, ag_a = f"gateway_{lane_a}", f"agente_{lane_a}"
    gw_b, ag_b = f"gateway_{lane_b}", f"agente_{lane_b}"

    # `token_var=None`: modo RELAJADO de `I10` — exige indirección OBLIGATORIA
    # y sin herencia de la flota, pero NO un nombre fijo. Exigir aquí el MISMO
    # nombre a las dos lanes sería imponer el cruce que este módulo existe
    # para prohibir; la unicidad entre lanes la exige `X2_IDENTIDAD`, abajo, y
    # nada más — así el mutante que iguala los dos nombres sólo lo caza esa
    # comprobación, y su cobertura es real y no una redundancia muda.
    for etiqueta, gw, ag in ((lane_a.upper(), gw_a, ag_a), (lane_b.upper(), gw_b, ag_b)):
        fallos += [f"{etiqueta}:{c}" for c in
                   _m1.verificar(compose, gateway=gw, agente=ag, ledger_piloto=ledger_piloto,
                                 token_var=None)]

    if gw_a not in servicios or gw_b not in servicios:
        # Sin las dos lanes no hay cruce que comprobar; los `I0` de arriba ya
        # dicen cuál falta.
        return fallos

    srv_gw_a, srv_gw_b = servicios[gw_a], servicios[gw_b]
    srv_ag_a = servicios.get(ag_a) or {}
    srv_ag_b = servicios.get(ag_b) or {}
    ea, eb = _entorno(srv_gw_a), _entorno(srv_gw_b)

    def cruce_por_variable(codigo: str, claves: tuple[str, ...], motivo: str) -> None:
        for clave in claves:
            va, vb = str(ea.get(clave, "")).strip(), str(eb.get(clave, "")).strip()
            if not va or not vb:
                # Alguna lane no declara valor: sin las DOS no hay nada que
                # comparar. Una lane muda es un hallazgo — de M1, por lane
                # (`I5`/`I11`/ausencia de `LLMINBOX_TOKEN`) — no un CRUCE.
                continue
            ra, rb = _var_referenciada(va), _var_referenciada(vb)
            # Si es indirección, la identidad es el NOMBRE de variable (nunca
            # el secreto que contiene); si es un literal, la identidad es el
            # propio valor — dos lanes con el MISMO literal a mano cruzan
            # igual que dos lanes con la misma variable.
            ia = ra if ra is not None else va
            ib = rb if rb is not None else vb
            if ia == ib:
                como = (f"la MISMA variable de entorno (`{ia}`)" if ra is not None
                        else "el MISMO valor puesto a mano")
                fallos.append(
                    f"{codigo}: `{clave}` de la lane {lane_a.upper()} y la lane "
                    f"{lane_b.upper()} usan {como} — {motivo}")

    cruce_por_variable(
        "X2_IDENTIDAD", _CLAVE_IDENTIDAD,
        "un único valor autentica en las dos lanes con la misma credencial")
    cruce_por_variable(
        "X3_SESION", _CLAVE_SESION,
        "el atestado del mapa de credenciales es el mismo: un mapa sustituido en "
        "una lane pasaría el atestado de la otra, y las sesiones que ese mapa "
        "deriva también")
    cruce_por_variable(
        "X4_LEDGER", _CLAVE_LEDGER,
        "el ledger operativo de las dos lanes se acreditaría con la misma "
        "identidad y el mismo testigo")
    cruce_por_variable(
        "X5_RECIBO", _CLAVE_RECIBO,
        "el journal — donde vive el recibo durable de cada transición — se "
        "acreditaría con la misma identidad y el mismo testigo en las dos lanes")
    cruce_por_variable(
        "X6_CURSOR", _CLAVE_CURSOR,
        "un cursor de búsqueda emitido por una lane validaría en la otra: la "
        "clave que firma el cursor tiene que ser propia de cada lane, nunca "
        "compartida ni por defecto")

    # ── X1 · VOLUMEN: ningún origen de montaje —nombrado o bind— se repite
    # entre lane A y lane B. Se mide sobre gateway Y agente de cada lane: un
    # origen compartido a través del agente sería tan cruzado como por el
    # gateway, y `I7` ya exige que el agente no monte nada — si algún día deja
    # de cumplirlo, este cruce lo sigue viendo.
    # `/app/tools` es el preflight de SOLO LECTURA que las dos lanes montan
    # desde el mismo checkout — es CÓDIGO, no estado de la lane, y M1 ya lo
    # trata así (se monta igual en gateway y en estreno). Compartirlo no es un
    # cruce de identidad/ledger/journal; excluirlo es lo que permite que este
    # cruce discrimine el caso real (dos lanes que sí comparten ESTADO) del
    # ruido (dos lanes que comparten el script que las arranca a las dos).
    montajes_a = [m for m in _m1.normaliza_montajes(srv_gw_a) + _m1.normaliza_montajes(srv_ag_a)
                  if m["destino"] != "/app/tools"]
    montajes_b = [m for m in _m1.normaliza_montajes(srv_gw_b) + _m1.normaliza_montajes(srv_ag_b)
                  if m["destino"] != "/app/tools"]

    def _identidad_origen(origen: str) -> str:
        # Un bind de HOST casi siempre llega como indirección
        # (`${LLMINBOX_DUAL_LANE_A_LEDGER_HOST:?mensaje}`), y el `:?mensaje`
        # de cada lane se redacta distinto aunque la VARIABLE se copie mal —
        # comparar la cadena entera dejaría pasar justo ese copy-paste.
        ref = _var_referenciada(origen)
        return ref if ref is not None else origen

    origenes_a = {_identidad_origen(m["origen"]) for m in montajes_a if m["origen"]}
    origenes_b = {_identidad_origen(m["origen"]) for m in montajes_b if m["origen"]}
    comunes = origenes_a & origenes_b
    if comunes:
        fallos.append(
            f"X1_VOLUMEN: origen(es) de montaje compartido(s) entre lane "
            f"{lane_a.upper()} y lane {lane_b.upper()}: {sorted(comunes)}")

    declarados = set((compose.get("volumes") or {}).keys())
    vol_a = {m["origen"] for m in montajes_a if m["tipo"] == "volumen"}
    vol_b = {m["origen"] for m in montajes_b if m["tipo"] == "volumen"}
    ajenos = (vol_a | vol_b) - declarados
    if ajenos:
        fallos.append(
            f"X1_VOLUMEN: volumen(es) referenciado(s) sin declarar en el bloque "
            f"`volumes:` de primer nivel: {sorted(ajenos)}")

    # ── X0 · RED: cada lane declara su propia red y no las comparte. Sin esto
    # compose mete a las cuatro contenedores en la red por defecto del
    # proyecto y quedan mutuamente alcanzables por DNS de servicio — la
    # superficie de transporte que necesitaría cualquiera de los seis cruces
    # de arriba si viajara por la API en vez de por un fichero mal montado.
    def redes(srv: dict) -> set[str]:
        n = (srv or {}).get("networks")
        if isinstance(n, dict):
            return set(n.keys())
        return set(n or [])

    red_a = redes(srv_gw_a) | redes(srv_ag_a)
    red_b = redes(srv_gw_b) | redes(srv_ag_b)
    if not red_a or not red_b:
        fallos.append(
            "X0_RED: alguna lane no declara `networks:` explícita en todos sus "
            "servicios — compose la pone en la red por defecto del proyecto, "
            "compartida con la otra lane")
    elif red_a & red_b:
        fallos.append(
            f"X0_RED: lane {lane_a.upper()} y lane {lane_b.upper()} comparten "
            f"red {sorted(red_a & red_b)} — alcanzables por DNS de servicio pese "
            f"a los volúmenes separados")

    return fallos
