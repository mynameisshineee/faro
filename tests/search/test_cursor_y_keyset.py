"""Cursor firmado y atado, y paginación que ni pierde ni repite."""

import pytest

from .conftest import ACL, CARRIL, CARRIL_HERMANO, CLAVE, LANE, mete
import search_contract as sc
import search_cursor as scur
import search_store as ss


def test_clave_corta_se_rechaza_al_construir(store):
    with pytest.raises(scur.CursorKeyTooShort):
        ss.SearchStore(store.con, cursor_key=b"corta", acl=ACL)
    with pytest.raises(scur.CursorKeyTooShort):
        ss.SearchStore(store.con, cursor_key=b"x" * 31, acl=ACL)
    ss.SearchStore(store.con, cursor_key=b"x" * 32, acl=ACL)      # ⊕ 32 justos SÍ


def test_la_firma_es_hmac_sha256_completo():
    c = scur.encode(arrival=1, eid="e", generation="a" * 32, filter_sha256="a" * 64,
                    key=CLAVE)
    firma = c.split(".", 1)[1]
    assert len(firma) == 64, f"firma truncada a {len(firma)} hex"


def test_cursor_manipulado_se_rechaza(poblado):
    r = poblado.search(lane=CARRIL, ledger=LANE, query="born red", limit=2)
    cur = r["cursor"]
    cuerpo, firma = cur.split(".", 1)
    # ⊖ un byte de la FIRMA
    otra = ("0" if firma[0] != "0" else "1") + firma[1:]
    with pytest.raises(scur.CursorSignatureInvalid):
        poblado.search(lane=CARRIL, ledger=LANE, query="born red", limit=2, cursor=f"{cuerpo}.{otra}")
    # ⊖ un byte del CUERPO (la firma deja de casar)
    roto = ("A" if cuerpo[0] != "A" else "B") + cuerpo[1:]
    with pytest.raises(scur.CursorSignatureInvalid):
        poblado.search(lane=CARRIL, ledger=LANE, query="born red", limit=2, cursor=f"{roto}.{firma}")


@pytest.mark.parametrize("cambio", [
    {"ledger": "64bis-wiki-archivo"},
    {"actor": "cto"},
    {"tipo": "DELIVERED"},
    {"desde": "2020-01-01"},
    {"hasta": "2030-01-01"},
    # Carril AUTORIZADO y distinto: así lo que rechaza es la FIRMA del cursor y no
    # la ACL. Con un carril no autorizado, este caso medía la ACL y se leía como si
    # midiera el atado del cursor.
    {"lane": CARRIL_HERMANO},
    {"query": "born"},
])
def test_cursor_atado_a_cada_filtro(poblado, cambio):
    """`hasta` y `ausente` entran en la lista a propósito: son los que casi nadie manda
    y por eso son los que se olvidan al firmar."""
    base = dict(lane=CARRIL, ledger=LANE, query="born red", limit=2)
    r = poblado.search(**base)
    with pytest.raises(scur.CursorFilterMismatch):
        poblado.search(**{**base, **cambio, "cursor": r["cursor"]})


def test_cursor_de_otra_generacion_caduca(poblado):
    r = poblado.search(lane=CARRIL, ledger=LANE, query="born red", limit=2)
    poblado.rebuild()                                  # generación nueva
    with pytest.raises(scur.CursorGenerationStale):
        poblado.search(lane=CARRIL, ledger=LANE, query="born red", limit=2, cursor=r["cursor"])


def test_generacion_es_aleatoria_de_128_bits(poblado):
    vistas = {poblado.generation()}
    for _ in range(5):
        vistas.add(poblado.rebuild())
    assert len(vistas) == 6, "la generación se repite: no es aleatoria"
    assert all(len(g) == 32 and int(g, 16) >= 0 for g in vistas), "no son 128 bits hex"


def _paginar(store, **kw):
    vistos, cur, paginas = [], None, 0
    while True:
        r = store.search(cursor=cur, **kw)
        vistos += [f["eid"] for f in r["filas"]]
        paginas += 1
        cur = r["cursor"]
        if not r["hay_mas"]:
            return vistos, paginas
        assert paginas < 50, "paginación que no termina"


def test_keyset_no_pierde_ni_repite_con_insercion_a_mitad(poblado):
    """Población > 3 × page_size, con `n/N` impreso, y una entrada NUEVA entre páginas."""
    page = 2
    r1 = poblado.search(lane=CARRIL, ledger=LANE, query="born red", limit=page)
    preexistentes = {f["eid"] for f in poblado.search(
        lane=CARRIL, ledger=LANE, query="born red", limit=100)["filas"]}
    assert len(preexistentes) > 3 * page, f"n/N = {len(preexistentes)}/{3 * page + 1}"

    mete(poblado.con, LANE, "INTRUSO", 9999, actor="cto", cuerpo="born red comun")
    poblado.con.commit()

    vistos = [f["eid"] for f in r1["filas"]]
    cur = r1["cursor"]
    while cur:
        r = poblado.search(lane=CARRIL, ledger=LANE, query="born red", limit=page, cursor=cur)
        vistos += [f["eid"] for f in r["filas"]]
        cur = r["cursor"] if r["hay_mas"] else None
    assert len(vistos) == len(set(vistos)), "la paginación REPITE"
    assert preexistentes <= set(vistos), (
        f"la paginación PIERDE: faltan {preexistentes - set(vistos)}")


def test_falsador_born_red_offset_si_pierde_con_insercion(poblado):
    """⊖ del ⊖: la misma prueba con OFFSET tiene que ponerse ROJA.

    Sin esto, el test de arriba podría estar verde porque la inserción no altera el
    recorte, no porque el keyset lo resista.
    """
    page = 2
    expr, _ = sc.compile_query("born red")
    sql = ss.SQL_BUSQUEDA.replace(
        "   AND (? = 0 OR (e.arrival, e.eid) < (?, ?))\n", "").replace(
        " LIMIT ?", " LIMIT ? OFFSET ?")

    def pagina(off):
        return [f["eid"] for f in poblado.con.execute(sql, (
            expr, CARRIL, LANE, LANE, None, None, None, None, None, None, None, None,
            page, off)).fetchall()]

    p1 = pagina(0)
    mete(poblado.con, LANE, "INTRUSO", 9999, actor="cto", cuerpo="born red comun")
    poblado.con.commit()
    vistos = list(p1)
    off = page
    while True:
        p = pagina(off)
        if not p:
            break
        vistos += p
        off += page
    assert len(vistos) != len(set(vistos)), (
        "OFFSET NO repitió: el corpus no discrimina y el test del keyset no mide nada")


def test_orden_total_con_ts_repetido(poblado):
    """`ts` colisiona hasta el 70,7 % del corpus: el desempate lo hace `eid`."""
    for i in range(4):
        mete(poblado.con, LANE, f"tie-{i}", 5000 + i, actor="cto",
             ts="2026-09-05T00:00:00Z", cuerpo="born red comun")
    poblado.con.commit()
    poblado.rebuild()
    vistos, _ = _paginar(poblado, lane=CARRIL, ledger=LANE, query="born red", limit=2)
    assert len(vistos) == len(set(vistos))
    de_una = [f["eid"] for f in poblado.search(
        lane=CARRIL, ledger=LANE, query="born red", limit=100)["filas"]]
    assert vistos == de_una, "el orden entre páginas no coincide con el de una sola página"


def test_truncado_se_declara_siempre(poblado):
    r = poblado.search(lane=CARRIL, ledger=LANE, query="born red", limit=2)
    tr = r["truncado"]
    # `recortadas == 0`: la página está COMPLETA. La fila que sobra es la SONDA del
    # `LIMIT n+1`, que existe para saber si hay más — contarla como recortada decía «te
    # recorté una» sobre una página entera.
    assert tr["por"] == "filas" and tr["servidas"] == 2 and tr["recortadas"] == 0
    assert r["hay_mas"] is True
    ultima = poblado.search(lane=CARRIL, ledger=LANE, query="born red", limit=100)
    assert ultima["hay_mas"] is False
    assert ultima["truncado"]["por"] is None and ultima["truncado"]["recortadas"] == 0, (
        "«no hay más» y «hay más y te di menos» tienen que ser distinguibles")


# ── correctivas: cota de tamaño, hex real y atadura del carril ────────────────────
def test_cursor_enorme_se_rechaza_ANTES_de_decodificar(monkeypatch):
    """La cota va primero. Se comprueba que no se llega a `base64` ni al HMAC: si se
    llegara, el llamante elegiría cuánto trabajo hace el servidor."""
    llamadas = []
    monkeypatch.setattr(scur, "_unb64u",
                        lambda t: llamadas.append(t) or b"{}")
    grande = "A" * (scur.MAX_CURSOR_BYTES + 1) + ".ff"
    with pytest.raises(scur.CursorTooLarge):
        scur.decode(grande, key=CLAVE, filter_sha256="a" * 64, generation="b" * 32)
    assert llamadas == [], "decodificó antes de mirar el tamaño"
    # ⊕ control: uno del tamaño justo SÍ pasa de la cota (y muere en la firma, más tarde)
    with pytest.raises(scur.CursorSignatureInvalid):
        scur.decode("A" * 40 + "." + "f" * 64, key=CLAVE,
                    filter_sha256="a" * 64, generation="b" * 32)


@pytest.mark.parametrize("gen,fsha", [
    ("z" * 32, "a" * 64),                       # generation no hexadecimal
    ("a" * 31, "a" * 64),                       # generation corta
    ("a" * 33, "a" * 64),                       # generation larga
    ("a" * 32, "z" * 64),                       # filter_sha no hexadecimal
    ("a" * 32, "a" * 63),                       # filter_sha corto
])
def test_encode_exige_hex_REAL_no_solo_longitud(gen, fsha):
    """Sin caso saltado: un `skip` dentro de un parametrizado es un caso que no mide.

    El relleno («16 hex + ceros») NO se prueba aquí porque SÍ es hexadecimal válido —
    contra eso protege `test_filter_sha256_es_el_digest_COMPLETO`, que compara con el
    digest real. Poner aquí un caso que esta función no puede cazar sería teatro.
    """
    with pytest.raises(scur.CursorMalformed):
        scur.encode(arrival=1, eid="e", generation=gen, filter_sha256=fsha, key=CLAVE)


def test_un_payload_firmado_con_generation_no_hex_se_rechaza():
    """Firmado por ESTE servidor y aun así inválido: la validación de forma corre después
    de la firma, no en su lugar."""
    import base64
    import hmac as _h
    import hashlib as _hl
    import json as _j
    cuerpo = _j.dumps({"v": 1, "arrival": 1, "eid": "e", "generation": "no-es-hex" * 3,
                       "filter_sha256": "a" * 64},
                      sort_keys=True, separators=(",", ":")).encode()
    firma = _h.new(CLAVE, cuerpo, _hl.sha256).hexdigest()
    cur = base64.urlsafe_b64encode(cuerpo).decode().rstrip("=") + "." + firma
    with pytest.raises(scur.CursorMalformed):
        scur.decode(cur, key=CLAVE, filter_sha256="a" * 64, generation="b" * 32)


def test_los_filtros_canonicos_exigen_carril_Y_ledger():
    with pytest.raises(sc.InvalidFilter):
        sc.canonical_filters(lane="", ledger=LANE, query="x")
    with pytest.raises(sc.InvalidFilter):
        sc.canonical_filters(lane=CARRIL, ledger="", query="x")
    f = sc.canonical_filters(lane=CARRIL, ledger=LANE, query="x")
    assert f["lane"] == CARRIL and f["ledger"] == LANE


def test_el_carril_cambia_la_firma_aunque_el_ledger_sea_el_mismo():
    a = sc.filter_sha256(sc.canonical_filters(lane="carril-A", ledger=LANE, query="x"))
    b = sc.filter_sha256(sc.canonical_filters(lane="carril-B", ledger=LANE, query="x"))
    assert a != b, (
        "dos carriles distintos sobre el MISMO ledger comparten firma: un cursor emitido "
        "para uno serviría al otro")
