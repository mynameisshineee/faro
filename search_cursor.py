"""M2 · Cursor opaco, firmado y atado a su recorte.

El cursor es dato del SERVIDOR que viaja por el CLIENTE. Sin firma es un parámetro de
consulta editable — y lleva dentro el carril, así que editarlo sería moverse de carril
paginando. Con firma, un cursor manipulado es un rechazo tipado y no una respuesta con
filas de otro sitio.

Tres ataduras, y las tres existen por una avería distinta:

· `filter_sha256` — un cursor presentado con OTROS filtros se rechaza. Sin esto, cambiar
  el carril, el ledger o `hasta` y seguir paginando devuelve filas de una población
  distinta sin un solo error.
· `generation`    — un `rebuild` cambia la generación, así que los cursores anteriores
  CADUCAN en vez de saltarse filas en silencio sobre un índice reconstruido.
· `HMAC-SHA256 COMPLETO` — 64 hex, no un prefijo. El prefijo se propone por estética de
  URL; lo que compra es un espacio de falsificación más pequeño a cambio de nada.

La clave es SEPARADA del resto de secretos del servicio y de ≥32 bytes. Reutilizar la
credencial del servicio para firmar cursores acopla dos rotaciones que no tienen por qué
coincidir: rotar la del servicio invalidaría todos los cursores vivos, y a la inversa.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

CURSOR_VERSION = 1
MIN_KEY_BYTES = 32
# Cota DURA del cursor, en bytes, comprobada ANTES de decodificar nada. Un base64 de
# entrada arbitraria es trabajo que el llamante elige: sin cota, una cadena de megabytes
# se decodifica entera y se le calcula el HMAC antes de descubrir que no vale. La cota va
# primero para que rechazar sea barato.
MAX_CURSOR_BYTES = 2 * 1024
GENERATION_HEX = 32          # 128 bits
FILTER_SHA_HEX = 64          # sha256 completo

# FRONTERA DURABLE DEL `eid`. El desempate del orden es `(arrival, eid)` y el `eid` viaja
# DENTRO del cursor, así que un `eid` sin cota puede producir un cursor que este mismo
# decodificador rechaza por tamaño: el servidor emitiría una página que no se puede
# continuar. En el corpus el `eid` es un sha256 (64 hex, `ledger_parse.Entrada.sha`), así
# que 128 es holgado — pero la cota se APLICA, no se supone: `search_store` se niega a
# sellar un índice que contenga uno más largo.
MAX_EID_BYTES = 128


class CursorError(Exception):
    code = "CURSOR"

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class CursorKeyTooShort(CursorError):
    code = "CURSOR_KEY_TOO_SHORT"


class CursorMalformed(CursorError):
    code = "CURSOR_MALFORMED"


class CursorTooLarge(CursorError):
    code = "CURSOR_TOO_LARGE"


class CursorSignatureInvalid(CursorError):
    code = "CURSOR_SIGNATURE_INVALID"


class CursorFilterMismatch(CursorError):
    code = "CURSOR_FILTER_MISMATCH"


class CursorGenerationStale(CursorError):
    code = "CURSOR_GENERATION_STALE"


def _check_key(key: bytes) -> bytes:
    if isinstance(key, str):
        key = key.encode("utf-8")
    if not isinstance(key, (bytes, bytearray)):
        raise CursorKeyTooShort("la clave del cursor tiene que ser bytes")
    if len(key) < MIN_KEY_BYTES:
        raise CursorKeyTooShort(
            f"la clave del cursor mide {len(key)} bytes y el mínimo son "
            f"{MIN_KEY_BYTES}: una clave corta hace la firma decorativa, y una firma "
            f"decorativa es peor que ninguna porque se cita como si protegiera")
    return bytes(key)


_ALFABETO_B64U = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64u(txt: str) -> bytes:
    """base64url ESTRICTA Y CANÓNICA: decodifica, y exige que RE-CODIFIQUE idéntico.

    `base64.urlsafe_b64decode` es permisiva de tres formas, y las tres producen cadenas
    DISTINTAS que decodifican al MISMO cuerpo: acepta `+`/`/` del alfabeto estándar,
    ignora saltos de línea, y admite relleno de más. Un cursor con dos formas es un cursor
    con dos firmas válidas para el mismo estado — y basta con que alguna capa de arriba
    use la cadena como clave (caché, dedup, registro) para que dos peticiones idénticas se
    vean como distintas. Aquí sólo hay UNA forma: la que este módulo emite.
    """
    if any(c not in _ALFABETO_B64U for c in txt):
        raise CursorMalformed(
            "el cursor trae caracteres fuera del alfabeto base64url sin relleno "
            "(`+`, `/`, `=`, espacios o saltos de línea no valen)")
    relleno = "=" * (-len(txt) % 4)
    crudo = base64.urlsafe_b64decode(txt + relleno)
    if _b64u(crudo) != txt:
        raise CursorMalformed(
            "el cursor no está en forma canónica: re-codificarlo da otra cadena")
    return crudo


def _exigir_hex(valor, largo: int, nombre: str) -> str:
    """Hex REAL de la longitud exacta, no «una cadena de ese tamaño».

    Comprobar sólo la longitud deja pasar relleno: un digest de 16 hex rellenado a 64 con
    ceros mide 64 y no es un sha256. Aquí se exige que TODO sea hexadecimal, que es lo que
    distingue un digest de una cadena con la forma de un digest.
    """
    if not isinstance(valor, str) or len(valor) != largo:
        raise CursorMalformed(f"`{nombre}` tiene que ser {largo} caracteres hex")
    try:
        int(valor, 16)
    except ValueError:
        raise CursorMalformed(f"`{nombre}` no es hexadecimal") from None
    return valor


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def encode(*, arrival: int, eid: str, generation: str, filter_sha256: str,
           key: bytes) -> str:
    """`base64url(payload canónico) . hex(HMAC-SHA256 completo)`.

    El payload va canonicalizado (claves ordenadas, sin espacios) para que la MISMA
    posición produzca SIEMPRE los mismos bytes: si el orden de claves dependiera del
    dict de Python, dos servidores firmarían distinto sobre el mismo estado.
    """
    k = _check_key(key)
    if not isinstance(arrival, int) or isinstance(arrival, bool):
        raise CursorMalformed("`arrival` tiene que ser entero")
    if not isinstance(eid, str) or not eid:
        raise CursorMalformed("`eid` tiene que ser texto no vacío")
    n_eid = len(eid.encode("utf-8"))
    if n_eid > MAX_EID_BYTES:
        raise CursorMalformed(
            f"el `eid` ocupa {n_eid} bytes y la frontera son {MAX_EID_BYTES}: un cursor "
            f"con él dentro no cabría en {MAX_CURSOR_BYTES} y yo mismo lo rechazaría "
            f"después — sería una página imposible de continuar")
    _exigir_hex(generation, GENERATION_HEX, "generation")
    _exigir_hex(filter_sha256, FILTER_SHA_HEX, "filter_sha256")
    cuerpo = _canonical({
        "v": CURSOR_VERSION,
        "arrival": arrival,
        "eid": eid,
        "generation": generation,
        "filter_sha256": filter_sha256,
    })
    firma = hmac.new(k, cuerpo, hashlib.sha256).hexdigest()
    return f"{_b64u(cuerpo)}.{firma}"


def decode(cursor: str, *, key: bytes, filter_sha256: str, generation: str) -> tuple[int, str]:
    """Verifica firma, recorte y generación, en ESE orden, y devuelve `(arrival, eid)`.

    El orden importa y no es estético: si se comparase el recorte ANTES que la firma, un
    cursor falsificado con el recorte correcto llegaría a distinguirse de otro por el
    mensaje de error. Primero se acredita que el cursor lo emitió este servidor; sólo
    después se mira qué dice.
    """
    k = _check_key(key)
    if not isinstance(cursor, str):
        raise CursorMalformed("el cursor tiene que ser texto")
    # LA COTA VA PRIMERO, antes de partir, decodificar o firmar: es lo único que se puede
    # comprobar sin hacer trabajo que el llamante elige.
    n = len(cursor.encode("utf-8"))
    if n > MAX_CURSOR_BYTES:
        raise CursorTooLarge(
            f"el cursor ocupa {n} bytes y el máximo son {MAX_CURSOR_BYTES}: no lo "
            f"decodifico ni le calculo la firma")
    if cursor.count(".") != 1:
        raise CursorMalformed("cursor con forma inválida: falta la firma")
    cuerpo_b64, firma = cursor.split(".", 1)
    try:
        cuerpo = _unb64u(cuerpo_b64)
    except CursorMalformed:
        # YA trae el motivo exacto —alfabeto o canonicidad— y es parte del contrato:
        # dice QUÉ regla se rompió. Envolverlo en un mensaje genérico borraba esa
        # distinción y dejaba dos guardas distintas indistinguibles desde fuera.
        raise
    except Exception:
        raise CursorMalformed("cursor con base64url inválido") from None
    esperada = hmac.new(k, cuerpo, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(esperada, firma):
        raise CursorSignatureInvalid(
            "la firma del cursor no es de este servidor: no lo interpreto")
    try:
        payload = json.loads(cuerpo.decode("utf-8"))
    except Exception:
        raise CursorMalformed("cursor firmado pero con payload ilegible") from None
    if not isinstance(payload, dict) or payload.get("v") != CURSOR_VERSION:
        raise CursorMalformed(
            f"versión de cursor no soportada: {payload.get('v') if isinstance(payload, dict) else '?'}")
    for campo in ("arrival", "eid", "generation", "filter_sha256"):
        if campo not in payload:
            raise CursorMalformed(f"al cursor le falta `{campo}`")
    _exigir_hex(payload["generation"], GENERATION_HEX, "generation")
    _exigir_hex(payload["filter_sha256"], FILTER_SHA_HEX, "filter_sha256")
    # Comparaciones en tiempo constante también aquí: el `filter_sha256` y la
    # `generation` son valores del servidor, y una comparación que corta en el primer
    # byte distinto es un oráculo barato para ir ajustándolos.
    if not hmac.compare_digest(str(payload["filter_sha256"]), filter_sha256):
        raise CursorFilterMismatch(
            "este cursor se emitió para OTRO recorte (carril, actor, tipo, desde, "
            "hasta, ausentes, consulta u orden): no te devuelvo filas de otra pregunta")
    if not hmac.compare_digest(str(payload["generation"]), generation):
        raise CursorGenerationStale(
            "el índice se reconstruyó desde que te di este cursor: empieza de nuevo, "
            "porque seguir por él se saltaría filas sin avisar")
    arrival = payload["arrival"]
    eid = payload["eid"]
    if not isinstance(arrival, int) or isinstance(arrival, bool) or not isinstance(eid, str):
        raise CursorMalformed("cursor con tipos inválidos en la posición")
    return arrival, eid
