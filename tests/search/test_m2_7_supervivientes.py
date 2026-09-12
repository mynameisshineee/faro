"""Los mutantes que SOBREVIVIERON al censo rojo, y el falsador que faltaba para cada uno.

Un mutante vivo no dice que el código esté mal: dice que **ningún test discrimina** ese
defecto. Cada test de aquí nombra al mutante que lo dejó al descubierto, y se comprobó que
muere con la cura y sólo por este test.
"""

import json
import sqlite3

import pytest

from .conftest import ACL, CARRIL, CLAVE, LANE, mete, nueva_con
import search_contract as sc
import search_cursor as scur
import search_store as ss


def _store(tmp_path, filas=None, nombre="m2.sqlite"):
    con = nueva_con(tmp_path, nombre)
    st = ss.SearchStore(con, cursor_key=CLAVE, acl=ACL)
    st.ensure_schema()
    st.set_acl(ACL)
    for kw in (filas or []):
        mete(con, LANE, **kw)
    con.commit()
    if filas:
        st.rebuild()
    return st


# ── M09 · readiness ignora objetos ────────────────────────────────────────────────
def test_readiness_NOMBRA_el_objeto_que_falta_y_no_solo_que_el_DDL_no_cuadra(tmp_path):
    """`M09` (`if faltan:` → `if False:`) sobrevivía porque la comparación de DDL cazaba
    también la ausencia: dos guardas para lo mismo y sólo una probada.

    Se conservan las dos a propósito —una mira el CENSO de objetos y la otra su CUERPO—,
    pero entonces las dos tienen que estar probadas. Lo que se fija aquí es el CONTRATO de
    `readiness`: si falta un objeto, tiene que salir en `missing` **y** decirlo en
    `problems` con esas palabras. Quien lee el diagnóstico necesita distinguir «no está»
    de «está pero es otro».
    """
    st = _store(tmp_path, [dict(eid="e0", arrival=1, cuerpo="born red")])
    assert st.readiness()["ready"] is True                      # ⊕ antes de romper
    st.con.execute("DROP TRIGGER search_ai")

    r = st.readiness()
    assert r["ready"] is False
    assert "search_ai" in r["missing"], "`missing` no lista el objeto ausente"
    problemas = " · ".join(r["problems"])
    assert "faltan objetos" in problemas, (
        f"`readiness` no dice que FALTE un objeto, sólo que el DDL no cuadra: {problemas}")
    assert "search_ai" in problemas


# ── M39 · la reserva del sobre no puede depender del techo ───────────────────────
def test_la_reserva_del_sobre_NO_encoge_al_bajar_el_techo(tmp_path, monkeypatch):
    """`M39` (`10 ** 12` → `MAX_RESPONSE_BYTES`) sobrevivía: nadie medía la reserva con
    DOS techos distintos.

    Si el coste del sobre dependiera del techo, bajar el techo encogería la reserva y el
    presupuesto no bajaría lo mismo — el error se compensaría solo en el caso de prueba y
    reaparecería en producción, que es donde el techo es otro.
    """
    st = _store(tmp_path, [dict(eid="e0", arrival=1, cuerpo="born red")])
    gen, fsha = st.generation(), "a" * 64
    alto = st._envelope_bytes(gen, fsha, sc.ORDER_ARRIVAL_DESC)

    monkeypatch.setattr(ss, "MAX_RESPONSE_BYTES", 4096)
    bajo = st._envelope_bytes(gen, fsha, sc.ORDER_ARRIVAL_DESC)

    assert bajo == alto, (
        f"la reserva del sobre cambió de {alto} a {bajo} al bajar el techo: está acoplada "
        f"a lo que acota, así que no es una cota independiente")


# ── M49 · base64url CANÓNICA ──────────────────────────────────────────────────────
def _cursor_no_canonico(st):
    """Un cursor cuyo cuerpo decodifica IGUAL pero se escribe DISTINTO.

    ⚠️ La equivalencia se busca con el decodificador PERMISIVO de la librería estándar, no
    con `_unb64u`: `_unb64u` es justo la guarda que se está probando, y usarla para
    fabricar el sujeto haría que el barrido descartase precisamente los casos que
    interesan — el instrumento se filtraría a sí mismo y el test diría «no hay ninguno».

    Hace falta que el base64 no sea múltiplo de 4: sólo entonces el último carácter tiene
    bits sin usar y admite variantes equivalentes. Se barre sobre `eid` y `arrival` hasta
    dar con uno, así el sujeto es un cursor REAL emitido por este servidor.
    """
    import base64

    def permisivo(txt):
        return base64.urlsafe_b64decode(txt + "=" * (-len(txt) % 4))

    for largo in range(1, 30):
        for arrival in range(1, 40):
            cursor = scur.encode(arrival=arrival, eid="e" * largo,
                                 generation=st.generation(),
                                 filter_sha256="b" * 64, key=CLAVE)
            cuerpo, firma = cursor.split(".", 1)
            if len(cuerpo) % 4 == 0:
                continue
            base = permisivo(cuerpo)
            for c in scur._ALFABETO_B64U:
                variante = cuerpo[:-1] + c
                if variante == cuerpo:
                    continue
                try:
                    if permisivo(variante) == base:
                        return f"{variante}.{firma}", cursor
                except Exception:
                    continue
    raise AssertionError("no se encontró una variante no canónica: el barrido no sirve")


def test_dos_escrituras_del_MISMO_cursor_no_pueden_valer_las_dos(tmp_path):
    """`M49` (`if _b64u(crudo) != txt:` → `if False:`).

    `urlsafe_b64decode` es permisiva: cadenas DISTINTAS decodifican al MISMO cuerpo. Y la
    firma se calcula sobre los BYTES, no sobre el texto — así que sin este control el
    cursor falsificado pasa la firma y se acepta. Dos formas del mismo estado son dos
    claves distintas para cualquier capa de arriba que use la cadena como identidad
    (caché, deduplicación, registro): dos peticiones idénticas se verían como distintas.
    """
    st = _store(tmp_path, [dict(eid="e0", arrival=1, cuerpo="born red")])
    torcido, canonico = _cursor_no_canonico(st)
    assert torcido != canonico
    # ⊕ CONTROL: los dos cuerpos son EL MISMO —medido con el decodificador PERMISIVO,
    # que es el que tendría el atacante—, así que la firma del torcido es válida y sin la
    # guarda el cursor se ACEPTA. Sin este arm, el rechazo de abajo podría ser por
    # cualquier otra cosa.
    import base64 as _b64

    def _permisivo(txt):
        return _b64.urlsafe_b64decode(txt + "=" * (-len(txt) % 4))

    assert _permisivo(torcido.split(".")[0]) == _permisivo(canonico.split(".")[0])
    assert torcido.split(".")[1] == canonico.split(".")[1], "la firma tiene que ser la misma"

    with pytest.raises(scur.CursorMalformed, match="canónica"):
        scur.decode(torcido, key=CLAVE, filter_sha256="b" * 64,
                    generation=st.generation())


# ── M50 · alfabeto base64url, con su propio mensaje ──────────────────────────────
@pytest.mark.parametrize("intruso", ["+", "/", "=", " ", "\n"])
def test_un_caracter_fuera_del_alfabeto_se_rechaza_POR_EL_ALFABETO(tmp_path, intruso):
    """`M50` (`if any(c not in _ALFABETO_B64U ...)` → `if False:`) sobrevivía porque el
    control de canonicidad cazaba también estos casos… con OTRO mensaje.

    Que dos guardas tapen el mismo agujero no es motivo para dejar una sin probar: el
    mensaje es parte del contrato —dice QUÉ regla se rompió— y es lo único que separa
    «esto no es base64url» de «esto es otro base64url del mismo cuerpo».
    """
    st = _store(tmp_path, [dict(eid="e0", arrival=1, cuerpo="born red")])
    cursor = scur.encode(arrival=1, eid="e0", generation=st.generation(),
                         filter_sha256="b" * 64, key=CLAVE)
    cuerpo, firma = cursor.split(".", 1)
    with pytest.raises(scur.CursorMalformed, match="alfabeto"):
        scur.decode(f"{cuerpo}{intruso}.{firma}", key=CLAVE,
                    filter_sha256="b" * 64, generation=st.generation())


# ── M58 · el `eid` tiene cota al EMITIR el cursor ────────────────────────────────
def test_un_eid_por_encima_de_la_frontera_no_llega_a_firmarse():
    """`M58` (`if n_eid > MAX_EID_BYTES:` → `if False:`).

    La cota estaba probada en el borde exacto (`MAX_EID_BYTES` justos, que SÍ entra) y no
    por encima: el `⊕` sin su `⊖`. Un `eid` más largo produce un cursor que este mismo
    decodificador rechaza por tamaño — o sea, una página que nadie puede continuar.
    """
    comun = dict(generation="a" * 32, filter_sha256="b" * 64, key=CLAVE)
    # ⊕ el borde JUSTO sí entra
    cursor = scur.encode(arrival=1, eid="f" * scur.MAX_EID_BYTES, **comun)
    assert len(cursor.encode("utf-8")) <= scur.MAX_CURSOR_BYTES

    # ⊖ uno solo por encima, NO
    with pytest.raises(scur.CursorMalformed, match="frontera"):
        scur.encode(arrival=1, eid="f" * (scur.MAX_EID_BYTES + 1), **comun)
    with pytest.raises(scur.CursorMalformed, match="frontera"):
        scur.encode(arrival=1, eid="ñ" * scur.MAX_EID_BYTES, **comun)   # 2 bytes/carácter
