"""Falsificadores del techo de TRANSPORTE en bytes crudos.

Se conducen dos planos a proposito:

* **Plano ASGI crudo** (`_correr`): la unica forma honesta de falsar FRAMING.
  `TestClient`/`httpx` normalizan el troceado y la `Content-Length`, asi que un
  test que solo pase por ahi no puede distinguir «cuento bytes» de «leo la
  cabecera» — que es justo la propiedad que este techo promete.
* **Plano de la app real** (`TestClient`): comprueba que el veredicto de
  transporte convive con `D8`-`D14` y que lo que NO se pasa sigue llegando al
  router con su envelope de siempre.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import coordination as C
import native_gateway as G

from test_native_gateway import CREDS, GRANTS          # mismo banco, sin duplicar


MIB = 1 << 20


# ── plano ASGI crudo ───────────────────────────────────────────────────────

def _scope(*, cabeceras: list[tuple[bytes, bytes]] | None = None,
           ruta: str = "/native/v1/sessions") -> dict:
    return {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": ruta, "raw_path": ruta.encode(), "root_path": "",
            "query_string": b"", "server": ("test", 80), "client": ("test", 1),
            "headers": cabeceras or []}


async def _correr(app, scope, mensajes):
    """Corre `app` con una cola EXACTA de mensajes de `receive`.

    Devuelve `(enviados, vistos)`: lo que salio por `send` y lo que la
    aplicacion de dentro llego a recibir. Sin `vistos` no se puede distinguir
    «rechazado» de «rechazado despues de entregarle el cuerpo».
    """
    cola = list(mensajes)
    enviados: list[dict] = []

    async def receive():
        if cola:
            return cola.pop(0)
        return {"type": "http.disconnect"}

    async def send(mensaje):
        enviados.append(mensaje)

    await app(scope, receive, send)
    return enviados


def _espia(vistos: list[bytes], *, status: int = 200):
    """App ASGI que APUNTA cada byte que le llega y responde con contenido."""
    async def app(scope, receive, send):
        while True:
            mensaje = await receive()
            if mensaje["type"] == "http.disconnect":
                raise RuntimeError("cliente desconectado")
            vistos.append(mensaje.get("body", b""))
            if not mensaje.get("more_body"):
                break
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body",
                    "body": b'{"secreto":"esto no debe salir"}'})
    return app


def _correr_sync(app, scope, mensajes):
    import asyncio
    return asyncio.run(_correr(app, scope, mensajes))


def _status(enviados):
    for m in enviados:
        if m["type"] == "http.response.start":
            return m["status"]
    return None


def _cuerpo(enviados):
    return b"".join(m.get("body", b"") for m in enviados
                    if m["type"] == "http.response.body")


def _trozos(total: int, tam: int) -> list[dict]:
    """`total` bytes troceados en chunks de `tam`, con `more_body` correcto."""
    restante, mensajes = total, []
    while restante > 0:
        n = min(tam, restante)
        restante -= n
        mensajes.append({"type": "http.request", "body": b"x" * n,
                         "more_body": restante > 0})
    if not mensajes:
        mensajes.append({"type": "http.request", "body": b"", "more_body": False})
    return mensajes


# F-1 · el limite es INCLUSIVO y el veredicto es de los bytes, no de la cabecera
@pytest.mark.parametrize("enviados_bytes, espera_413", [
    (0, False), (1, False), (16, False), (16 + 1, True), (16 + 4096, True),
])
def test_limite_inclusivo(enviados_bytes, espera_413):
    vistos: list[bytes] = []
    app = G.RawBodyLimitMiddleware(_espia(vistos), max_bytes=16)
    salida = _correr_sync(app, _scope(), _trozos(enviados_bytes, 8))
    if espera_413:
        assert _status(salida) == 413
        assert _cuerpo(salida) == b""
    else:
        assert _status(salida) == 200
        assert b"".join(vistos) == b"x" * enviados_bytes


# F-2 · el techo POR DEFECTO es `256 KiB` y su frontera es `N`/`N+1`
@pytest.mark.parametrize("delta, espera", [(0, 200), (1, 413)])
def test_frontera_del_techo_por_defecto(delta, espera):
    vistos: list[bytes] = []
    app = G.RawBodyLimitMiddleware(_espia(vistos))          # techo por defecto
    salida = _correr_sync(app, _scope(),
                          _trozos(G.RAW_BODY_MAX_BYTES + delta, 64 * 1024))
    assert _status(salida) == espera
    # El numero se fija aqui a proposito: si alguien lo mueve, este test lo dice
    # y obliga a rehacer las tres piernas de la derivacion del docstring.
    assert G.RAW_BODY_MAX_BYTES == 1_048_576 == 1 << 20


# F-3 · `Content-Length` correcta, MENTIROSA (por lo bajo y por lo alto) y
#       AUSENTE dan el MISMO veredicto: el de los bytes que llegan
@pytest.mark.parametrize("declarada", [b"32", b"1", b"999999999", None])
def test_content_length_no_decide(declarada):
    cabeceras = [] if declarada is None else [(b"content-length", declarada)]
    vistos: list[bytes] = []
    app = G.RawBodyLimitMiddleware(_espia(vistos), max_bytes=16)
    salida = _correr_sync(app, _scope(cabeceras=cabeceras), _trozos(32, 8))
    assert _status(salida) == 413, f"32 bytes reales con CL={declarada!r}"
    assert _cuerpo(salida) == b""


def test_content_length_enorme_con_cuerpo_pequeno_pasa():
    """La cabecera NO es el documento: mentir por lo alto no rechaza nada."""
    vistos: list[bytes] = []
    app = G.RawBodyLimitMiddleware(_espia(vistos), max_bytes=16)
    salida = _correr_sync(
        app, _scope(cabeceras=[(b"content-length", b"99999999")]),
        _trozos(8, 8))
    assert _status(salida) == 200
    assert b"".join(vistos) == b"x" * 8


# F-4 · troceado: el chunk que se pasa NO se entrega, y nada de lo que la
#       aplicacion queria responder sale a la red
@pytest.mark.parametrize("tam_chunk", [1, 3, 8, 64])
def test_chunked_no_filtra_ni_entrada_ni_salida(tam_chunk):
    vistos: list[bytes] = []
    app = G.RawBodyLimitMiddleware(_espia(vistos), max_bytes=16)
    salida = _correr_sync(app, _scope(), _trozos(64, tam_chunk))
    assert _status(salida) == 413
    assert _cuerpo(salida) == b""
    assert b"secreto" not in _cuerpo(salida)
    assert sum(len(v) for v in vistos) <= 16, "se entrego el chunk que se paso"


# F-5 · una DESCONEXION de verdad no es un exceso
def test_desconexion_real_no_produce_413():
    async def app_interna(scope, receive, send):
        mensaje = await receive()
        assert mensaje["type"] == "http.request"
        segundo = await receive()
        assert segundo["type"] == "http.disconnect"
        await send({"type": "http.response.start", "status": 499,
                    "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = G.RawBodyLimitMiddleware(app_interna, max_bytes=16)
    salida = _correr_sync(app, _scope(), [
        {"type": "http.request", "body": b"x" * 4, "more_body": True},
        {"type": "http.disconnect"},
    ])
    assert _status(salida) == 499, "un disconnect del cliente no es un 413"


# F-6 · stream PARCIAL: el cuerpo acaba antes de lo declarado -> se cuenta lo
#       que llego, no lo prometido
def test_stream_parcial_cuenta_lo_que_llego():
    vistos: list[bytes] = []
    app = G.RawBodyLimitMiddleware(_espia(vistos), max_bytes=16)
    salida = _correr_sync(
        app, _scope(cabeceras=[(b"content-length", b"1024")]),
        [{"type": "http.request", "body": b"x" * 4, "more_body": False}])
    assert _status(salida) == 200
    assert b"".join(vistos) == b"x" * 4


# F-7 · la excepcion de la aplicacion sube intacta: el middleware no tiene
#       `except` que la pueda tapar
def test_error_de_la_app_no_se_traga():
    async def app_rota(scope, receive, send):
        await receive()
        raise ZeroDivisionError("defecto real de la aplicacion")

    app = G.RawBodyLimitMiddleware(app_rota, max_bytes=16)
    with pytest.raises(ZeroDivisionError):
        _correr_sync(app, _scope(), _trozos(4, 4))


# F-8 · websocket y otros scopes pasan de largo sin tocarse
def test_scope_no_http_pasa_intacto():
    tocado: list[str] = []

    async def app_interna(scope, receive, send):
        tocado.append(scope["type"])

    app = G.RawBodyLimitMiddleware(app_interna, max_bytes=0)
    _correr_sync(app, {"type": "lifespan"}, [])
    assert tocado == ["lifespan"]


# ── plano de la app real: convivencia con D8-D14 ───────────────────────────

@pytest.fixture()
def cliente(tmp_path):
    journal = C.Journal(str(tmp_path / "coordination.sqlite"), pepper="pepper-de-test")
    journal.initialize()
    G.configure_journal_from_v8(journal, CREDS, GRANTS)
    app = G.create_native_app(journal, raw_body_max_bytes=16)
    with TestClient(app) as cliente:
        yield cliente


def test_app_real_corta_antes_de_llegar_al_router(cliente):
    """Se pasa el techo -> `413` vacio, y NO el `401` que daria el router."""
    r = cliente.post("/native/v1/sessions", content=b"x" * 64)
    assert r.status_code == 413
    assert r.content == b""
    assert "code" not in r.text
    assert r.headers.get("content-type") is None


def test_app_real_por_debajo_del_techo_sigue_dando_error_plano_d11(cliente):
    """`D8`-`D11` intactos: sin `Authorization` sale el error del CONTRATO."""
    r = cliente.post("/native/v1/sessions", content=b"{}")
    assert r.status_code == 401
    cuerpo = r.json()
    assert cuerpo["code"] == "RUNTIME_CREDENTIAL_REQUIRED"
    assert "error" not in cuerpo


# F-9 · si se pasa, la aplicacion NO SE INVOCA: no hay contenido que filtrar
def test_al_pasarse_la_app_no_se_invoca():
    invocada: list[int] = []

    async def app_interna(scope, receive, send):
        invocada.append(1)

    app = G.RawBodyLimitMiddleware(app_interna, max_bytes=16)
    salida = _correr_sync(app, _scope(), _trozos(64, 8))
    assert _status(salida) == 413
    assert invocada == [], "la aplicacion vio una peticion que el techo corto"


# F-10 · la lectura por delante esta ACOTADA por el propio techo: en cuanto se
#        pasa deja de pedir, aunque queden megas por venir
def test_deja_de_leer_en_cuanto_se_pasa():
    pedidos = {"n": 0, "bytes": 0}
    chunks = _trozos(MIB, 1024)          # 1 MiB por delante, techo de 16

    async def receive():
        pedidos["n"] += 1
        if chunks:
            m = chunks.pop(0)
            pedidos["bytes"] += len(m["body"])
            return m
        return {"type": "http.disconnect"}

    enviados: list[dict] = []

    async def send(mensaje):
        enviados.append(mensaje)

    async def app_interna(scope, receive_, send_):
        raise AssertionError("no debe llegar")

    import asyncio
    asyncio.run(G.RawBodyLimitMiddleware(app_interna, max_bytes=16)(
        _scope(), receive, send))
    assert _status(enviados) == 413
    assert pedidos["bytes"] <= 16 + 1024, (
        f"leyo {pedidos['bytes']} B para un techo de 16: no esta acotado")


# F-11 · end-to-end: un cuerpo VALIDO pero pasado de techo no deja estado
def test_cuerpo_valido_pero_pasado_no_toca_el_journal(tmp_path):
    journal = C.Journal(str(tmp_path / "coordination.sqlite"),
                        pepper="pepper-de-test")
    journal.initialize()
    G.configure_journal_from_v8(journal, CREDS, GRANTS)
    app = G.create_native_app(journal, raw_body_max_bytes=64)
    with TestClient(app) as cliente:
        cabeceras = {"Authorization": "Bearer cred-author-high-entropy"}
        antes = cliente.post("/native/v1/sessions", json={"ttl_s": 60},
                             headers=cabeceras)
        assert antes.status_code in (200, 201), antes.text
        # el MISMO cuerpo valido, rellenado por encima del techo de transporte
        r = cliente.post("/native/v1/sessions",
                         content=b'{"ttl_s":60}' + b" " * 128,
                         headers={**cabeceras, "Content-Type": "application/json"})
        assert r.status_code == 413
        assert r.content == b""


# ── amplificacion, streams que no acaban, y la cota HONESTA de memoria ─────
#
# Los tres de abajo nacen de tres medidas EN ROJO sobre la version anterior de
# este middleware, que guardaba los mensajes tal cual en una lista:
#   A) `64 KiB` en chunks de `1 B` retenian `12,6 MB` — amplificacion `×192,7`.
#   B) `200.000` chunks VACIOS con techo `16` salian `200` tras retener `38 MB`.
#   C) UN chunk de `8 MiB` con techo `16` leia los `8 MiB`, contra un docstring
#      que prometia `max_bytes + 1`.

def _pico(fn):
    """Pico de memoria trazada por `tracemalloc` durante `fn()`."""
    import tracemalloc
    tracemalloc.start()
    try:
        fn()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


# F-12 · el troceado del CLIENTE no manda en la memoria del servidor
def test_muchos_chunks_no_amplifican_la_memoria():
    techo = 64 * 1024

    async def app_nula(scope, receive, send):
        while True:
            if not (await receive()).get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    # Los mensajes se generan DENTRO de la region trazada, uno a uno, para que
    # el pico mida quien los RETIENE. Con una lista pre-construida el harness se
    # come la medida: los `dict` los habria alojado el test, no el middleware.
    def corre():
        import asyncio

        async def receive():
            i = pedidos["n"]
            pedidos["n"] += 1
            if i < techo:
                return {"type": "http.request", "body": b"x",
                        "more_body": i < techo - 1}
            return {"type": "http.disconnect"}

        async def send(mensaje):
            enviados.append(mensaje)

        # `max_chunks` desactivado a proposito: aqui se mide la MEMORIA, no el
        # tope de mensajes, y con el puesto la peticion se cortaria antes.
        asyncio.run(G.RawBodyLimitMiddleware(
            app_nula, max_bytes=techo, max_chunks=10 ** 9)(
            _scope(), receive, send))

    pedidos = {"n": 0}
    enviados: list[dict] = []
    pico = _pico(corre)
    assert _status(enviados) == 200
    # El cuerpo cabe justo en el techo; el camino troceado paga ademas UNA copia
    # final (`bytes(bytearray)`), asi que la cota honesta es `2·techo` + holgura.
    # `×8` deja sitio a esa holgura y sigue estando 24 veces por debajo del
    # `×192,7` que se midio antes de fundir los chunks: si alguien vuelve a
    # guardar los mensajes uno a uno, esto se pone rojo.
    assert pico < techo * 8, f"pico {pico} B para un cuerpo de {techo} B"


# F-13 · el tope de chunks VACIOS es inclusivo: `N` entra, `N+1` no
@pytest.mark.parametrize("vacios, espera", [(4, 200), (5, 413)])
def test_chunks_vacios_tope_inclusivo(vacios, espera):
    vistos: list[bytes] = []
    app = G.RawBodyLimitMiddleware(_espia(vistos), max_bytes=16,
                                   max_empty_chunks=4)
    mensajes = [{"type": "http.request", "body": b"", "more_body": True}
                for _ in range(vacios)]
    mensajes.append({"type": "http.request", "body": b"ok", "more_body": False})
    salida = _correr_sync(app, _scope(), mensajes)
    assert _status(salida) == espera
    if espera == 200:
        assert b"".join(vistos) == b"ok", "el cuerpo real se perdio por el camino"
    else:
        assert _cuerpo(salida) == b"", "el 413 por stream infinito lleva cuerpo"


# F-14 · un stream de vacios SIN FIN se corta, y sin retener nada
def test_stream_infinito_de_vacios_no_cuelga_ni_crece():
    import asyncio

    pedidos = {"n": 0}

    async def receive():
        pedidos["n"] += 1
        if pedidos["n"] > 100_000:
            raise AssertionError("el middleware no corto: leeria para siempre")
        return {"type": "http.request", "body": b"", "more_body": True}

    enviados: list[dict] = []

    async def send(mensaje):
        enviados.append(mensaje)

    async def app_interna(scope, receive_, send_):
        raise AssertionError("la aplicacion no debe ver un stream que no acaba")

    def corre():
        asyncio.run(G.RawBodyLimitMiddleware(app_interna, max_bytes=16)(
            _scope(), receive, send))

    pico = _pico(corre)
    assert _status(enviados) == 413
    assert pedidos["n"] == G.RAW_BODY_MAX_EMPTY_CHUNKS + 1, (
        f"leyo {pedidos['n']} chunks vacios para un tope de "
        f"{G.RAW_BODY_MAX_EMPTY_CHUNKS}")
    assert pico < 1 << 20, f"retuvo {pico} B de chunks que no traian ni un byte"


# F-15 · la cota de memoria es `max_bytes + mayor chunk`, NO `max_bytes + 1`
def test_un_solo_chunk_gigante_desmiente_el_max_mas_uno():
    """El tamano del chunk lo elige el servidor ASGI: no se puede acotar aqui.

    Este test NO pide que el pico sea pequeno —seria pedir lo imposible—: fija
    que la cota verdadera es la que dice el docstring, para que nadie vuelva a
    escribir `max_bytes + 1` creyendo que este codigo la cumple.
    """
    grande = 8 << 20

    async def app_interna(scope, receive, send):
        raise AssertionError("no debe llegar")

    app = G.RawBodyLimitMiddleware(app_interna, max_bytes=16)
    pico = _pico(lambda: _correr_sync(app, _scope(), [
        {"type": "http.request", "body": b"x" * grande, "more_body": False}]))
    assert pico > 16 + 1, (
        "si esto pasa a ser cierto, el middleware ya acota el chunk del "
        "servidor y el docstring se puede reescribir")
    assert pico >= grande, f"pico {pico} B para un chunk de {grande} B"

    doc = G.RawBodyLimitMiddleware.__doc__ or ""
    assert "max_bytes + len(mayor chunk" in doc, (
        "el docstring ya no declara la cota honesta de memoria")


# F-16 · el veredicto de transporte NO depende de la credencial (pre-read
#        antes de auth): mismo `413` vacio con credencial valida y sin ninguna
def test_el_413_es_identico_con_y_sin_credencial(tmp_path):
    journal = C.Journal(str(tmp_path / "coordination.sqlite"),
                        pepper="pepper-de-test")
    journal.initialize()
    G.configure_journal_from_v8(journal, CREDS, GRANTS)
    app = G.create_native_app(journal, raw_body_max_bytes=16)
    with TestClient(app) as cliente:
        pasado = b'{"ttl_s":60}' + b" " * 64
        anonimo = cliente.post("/native/v1/sessions", content=pasado)
        autorizado = cliente.post(
            "/native/v1/sessions", content=pasado,
            headers={"Authorization": "Bearer cred-author-high-entropy"})
        invalido = cliente.post("/native/v1/sessions", content=pasado,
                                headers={"Authorization": "Bearer no-existe"})
    for r in (anonimo, autorizado, invalido):
        assert r.status_code == 413, r.text
        assert r.content == b""
    doc = G.RawBodyLimitMiddleware.__doc__ or ""
    assert "ANTES DE CUALQUIER AUTENTICACION" in doc, (
        "el pre-read anterior a auth dejo de estar documentado")


# F-17 · si solo llega un `http.disconnect`, no se fabrica un `http.request`
def test_disconnect_solitario_no_inventa_una_peticion():
    recibidos: list[str] = []

    async def app_interna(scope, receive, send):
        recibidos.append((await receive())["type"])
        await send({"type": "http.response.start", "status": 499, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = G.RawBodyLimitMiddleware(app_interna, max_bytes=16)
    salida = _correr_sync(app, _scope(), [{"type": "http.disconnect"}])
    assert recibidos == ["http.disconnect"], (
        "se invento un cuerpo vacio que el cliente nunca envio")
    assert _status(salida) == 499


# F-18 · el techo de transporte es ESTRICTAMENTE MAYOR que la cota del `intent`
def test_el_techo_de_transporte_supera_la_cota_semantica():
    """Si cortara antes que el nucleo, mataria el `413` con causa del contrato.

    `32.768 B` es la cota del `intent` fijada en el carril; aqui solo se fija el
    ORDEN entre las dos, que es lo unico que ata a este modulo. El numero del
    techo es decision de despliegue y no lo decide este test.
    """
    COTA_INTENT = 65_536          # reglado 2026-09-06T08:35Z (era 32 KiB)
    assert G.RAW_BODY_MAX_BYTES > COTA_INTENT, (
        f"techo {G.RAW_BODY_MAX_BYTES} B <= cota del intent {COTA_INTENT} B: "
        "el transporte cortaria antes que el nucleo")


# ── los otros tres ejes: N / N+1 sobre los topes POR DEFECTO ───────────────

# F-19 · mensajes: `max_chunks` entra, `max_chunks + 1` no
@pytest.mark.parametrize("delta, espera", [(0, 200), (1, 413)])
def test_frontera_de_mensajes(delta, espera):
    n = G.RAW_BODY_MAX_CHUNKS + delta
    vistos: list[bytes] = []
    # techo de bytes holgado: aqui el unico eje que puede cortar es el CONTEO.
    app = G.RawBodyLimitMiddleware(_espia(vistos), max_bytes=10 * n)
    mensajes = [{"type": "http.request", "body": b"x", "more_body": i < n - 1}
                for i in range(n)]
    salida = _correr_sync(app, _scope(), mensajes)
    assert _status(salida) == espera
    if espera == 200:
        assert b"".join(vistos) == b"x" * n
    else:
        assert _cuerpo(salida) == b""
    assert G.RAW_BODY_MAX_CHUNKS == 4096


# F-20 · vacios: el tope POR DEFECTO, no el parametrizado de F-13
@pytest.mark.parametrize("delta, espera", [(0, 200), (1, 413)])
def test_frontera_de_vacios_por_defecto(delta, espera):
    n = G.RAW_BODY_MAX_EMPTY_CHUNKS + delta
    vistos: list[bytes] = []
    app = G.RawBodyLimitMiddleware(_espia(vistos), max_bytes=16)
    mensajes = [{"type": "http.request", "body": b"", "more_body": True}
                for _ in range(n)]
    mensajes.append({"type": "http.request", "body": b"ok", "more_body": False})
    salida = _correr_sync(app, _scope(), mensajes)
    assert _status(salida) == espera
    assert G.RAW_BODY_MAX_EMPTY_CHUNKS == 64


# F-21 · tiempo: un cuerpo que NO acaba a tiempo da `408`, no `413`
#        (un cuerpo que tarda no es un cuerpo grande)
def test_el_plazo_del_pre_read_da_408_y_no_413():
    import asyncio

    async def receive():
        # Primer chunk legal y luego silencio: ni desconexion ni fin de cuerpo.
        # Es el cliente que ocupa el worker sin pasarse de bytes ni de mensajes.
        if not receive.dado:
            receive.dado = True
            return {"type": "http.request", "body": b"x", "more_body": True}
        await asyncio.Event().wait()
    receive.dado = False

    enviados: list[dict] = []

    async def send(mensaje):
        enviados.append(mensaje)

    async def app_interna(scope, receive_, send_):
        raise AssertionError("la app no se invoca: el plazo vence en el pre-read")

    # 🔴 GUARDA PROPIA, y no es ceremonia: sin ella este test NO FALLA cuando el
    # plazo se rompe — se CUELGA. Medido: el mutante que sustituye el plazo por
    # `None` dejo la corrida colgada hasta que la mate a los 120 s. Un falsador
    # que cuelga en vez de ponerse rojo no es un falsador. El plazo de aqui es
    # `40x` el del middleware, asi que solo puede vencer si el suyo no vencio.
    async def corre():
        await asyncio.wait_for(
            G.RawBodyLimitMiddleware(
                app_interna, max_bytes=1024, read_timeout_s=0.05)(
                _scope(), receive, send),
            timeout=2.0)

    try:
        asyncio.run(corre())
    except TimeoutError:
        raise AssertionError(
            "el plazo del middleware NO vencio: colgado 2 s con un cliente que "
            "ni acaba el cuerpo ni se desconecta") from None
    assert _status(enviados) == 408, "un cuerpo que tarda no es un cuerpo grande"
    assert _cuerpo(enviados) == b"", "el 408 lleva cuerpo"
    assert G.RAW_BODY_READ_TIMEOUT_S == 60.0


# F-22 · el plazo NO castiga al que llega a tiempo, y `None` lo desactiva
def test_dentro_del_plazo_pasa_y_none_lo_desactiva():
    vistos: list[bytes] = []
    app = G.RawBodyLimitMiddleware(_espia(vistos), max_bytes=16,
                                   read_timeout_s=5.0)
    salida = _correr_sync(app, _scope(), _trozos(8, 4))
    assert _status(salida) == 200
    assert b"".join(vistos) == b"x" * 8

    vistos2: list[bytes] = []
    sin_plazo = G.RawBodyLimitMiddleware(_espia(vistos2), max_bytes=16,
                                         read_timeout_s=None)
    salida2 = _correr_sync(sin_plazo, _scope(), _trozos(8, 4))
    assert _status(salida2) == 200


# F-23 · la pierna ⓐ de la derivacion: el cuerpo LEGITIMO MAXIMO cabe
def test_el_cuerpo_legitimo_maximo_cabe_en_el_techo():
    """`intent` en su cota adjudicada + `causes`/`external_causes` y todos los
    escalares en su tope. Si alguien baja el techo por debajo de esto, rompe uso
    real y este falsador lo dice antes que un cliente.
    """
    import json, uuid

    # Las TRES cotas de abajo son las REGLADAS (`08:35Z`), no las que yo asumi:
    # `intent 64 KiB` (era `32`) y `causes`/`external_causes` `256` elementos
    # cada una (eran `64`). Si la politica las vuelve a mover, este test es el
    # que tiene que ponerse rojo ANTES que un cliente.
    COTA_INTENT = 65_536
    intent = {
        "type": "message", "verb": "inform", "to": ["security"],
        "kind": "DELIVERED", "head": "h", "body": "",
    }
    hueco = COTA_INTENT - len(json.dumps(
        intent, separators=(",", ":"), sort_keys=True).encode())
    intent["body"] = "a" * hueco
    cuerpo = json.dumps({
        **intent,
        "ledger": "l" * 512,
        "causes": ["evt_" + uuid.uuid4().hex for _ in range(256)],
        "external_causes": [{"ledger": "l" * 512, "entry_eid": "e" * 256}
                            for _ in range(256)],
        "fenced_resource": "r" * 512,
        "fencing_token": 2 ** 63 - 1,
        "trace": {"trace_id": "0" * 32, "span_id": "0" * 16},
    }, ensure_ascii=False).encode()

    assert len(cuerpo) <= G.RAW_BODY_MAX_BYTES, (
        f"el cuerpo legitimo maximo mide {len(cuerpo)} B y el techo es "
        f"{G.RAW_BODY_MAX_BYTES} B: el techo rompe uso real")
    vistos: list[bytes] = []
    app = G.RawBodyLimitMiddleware(_espia(vistos))
    salida = _correr_sync(app, _scope(), _trozos(len(cuerpo), 64 * 1024))
    assert _status(salida) == 200
    # Y el orden con la cota semantica, que es lo unico no negociable (F-18).
    assert G.RAW_BODY_MAX_BYTES > COTA_INTENT


# F-24 · OFF-BY-ONE DE VERDAD, sobre los TRES ejes que cuentan
#
# Un `N+1` que solo mira el status no prueba que el corte sea en `N+1`: un techo
# roto en `N` da el MISMO `413`. La forma que lo distingue —y que no era mia,
# me la trae una auditoria— es TOCAR el elemento `N+1` y poner un CENTINELA en
# `N+2` que no debe tocarse nunca. Dos aserciones, no una:
#   ⓐ se pidio el elemento `N+1`  (el corte no es prematuro, en `N`)
#   ⓑ NO se pidio el `N+2`        (el corte no es tardio, y no lee de mas)
@pytest.mark.parametrize("eje, limite, kwargs, hacer", [
    ("bytes", 16, {"max_bytes": 16, "max_chunks": 10 ** 6},
     lambda i: {"type": "http.request", "body": b"x", "more_body": True}),
    ("mensajes", 8, {"max_bytes": 10 ** 6, "max_chunks": 8},
     lambda i: {"type": "http.request", "body": b"x", "more_body": True}),
    ("vacios", 4, {"max_bytes": 16, "max_empty_chunks": 4},
     lambda i: {"type": "http.request", "body": b"", "more_body": True}),
])
def test_el_corte_toca_el_limite_mas_uno_y_no_el_mas_dos(eje, limite, kwargs, hacer):
    import asyncio

    pedidos = {"n": 0}
    CENTINELA = limite + 2          # 1-indexado: el que NO se debe pedir jamas

    async def receive():
        pedidos["n"] += 1
        if pedidos["n"] >= CENTINELA:
            raise AssertionError(
                f"[{eje}] pidio el elemento {pedidos['n']} (centinela en "
                f"{CENTINELA}): lee mas alla del limite+1")
        return hacer(pedidos["n"])

    enviados: list[dict] = []

    async def send(mensaje):
        enviados.append(mensaje)

    async def app_interna(scope, receive_, send_):
        raise AssertionError(f"[{eje}] la app no debe invocarse")

    asyncio.run(G.RawBodyLimitMiddleware(app_interna, **kwargs)(
        _scope(), receive, send))

    assert _status(enviados) == 413, f"[{eje}] no corto"
    assert _cuerpo(enviados) == b""
    # ⓐ el corte se produjo TOCANDO el limite+1, no antes
    assert pedidos["n"] == limite + 1, (
        f"[{eje}] corto tras {pedidos['n']} elementos y el limite es {limite}: "
        f"{'prematuro' if pedidos['n'] <= limite else 'tardio'}")
