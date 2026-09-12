"""`/organigrama` no puede servir una verdad organizativa caducada en silencio.

Medido en producción el 2026-08-18: el bind-mount de FICHERO ÚNICO quedó apuntando
a un inodo borrado cuando el host reemplazó el fichero por rename. Resultado: el
endpoint servía 15 roles de hacía dos días —le faltaba `engineering-manager`— y
**el aviso no saltaba**, porque el fichero SÍ se había leído al arrancar: se leyó
el viejo. Todo agente que preguntó «¿a quién reporto?» recibió una realidad vieja.

Dos causas, las dos aquí:
  · `JERARQUIA`/`ROLES_ALIAS` se calculan al IMPORTAR y no se vuelven a mirar.
  · el aviso cubría «ilegible», no «rancio», que son estados distintos.

La invariante que estos tests fijan: **`stale=false` sólo es posible habiendo
leído la fuente en ESA petición y coincidiendo el hash.** Cualquier otro caso
—ilegible, distinta, no montada— es `stale=true`. No hay TTL: un fichero de 6 KB
se hashea por petición y eso cuesta menos que mentir.
"""
from __future__ import annotations

import hashlib
import json

from .conftest import construir
from fastapi.testclient import TestClient

ORG_INICIAL = {
    "rol_por_alias": {"backend": "be", "cto-A": "cto"},
    "jerarquia": {
        "cto": {"reporta_a": "OPERADOR", "capa": "c-suite"},
        "be": {"reporta_a": "cto", "capa": "ejecucion"},
    },
}


def _monta(tmp_path, monkeypatch, org=None):
    ruta = tmp_path / "roles-por-alias.json"
    ruta.write_text(json.dumps(org if org is not None else ORG_INICIAL))
    s = construir(tmp_path, monkeypatch, extra_env={"LLMINBOX_ROLES_ALIAS": str(ruta)})
    return s, ruta


def _sha(ruta):
    return hashlib.sha256(ruta.read_bytes()).hexdigest()


def _org(c):
    r = c.get("/organigrama")
    assert r.status_code == 200, r.text
    return r.json()


def _cliente(s):
    c = TestClient(s.app)
    c.headers.update({"X-Llminbox-Token": "test-token"})
    return c


def test_declara_de_que_bytes_esta_hablando(tmp_path, monkeypatch):
    """Una respuesta organizativa que no dice qué contenido sirve no es
    verificable. El hash servido tiene que ser el del fichero de verdad.

    FALSADOR: sin `loaded_sha256`, no hay forma de distinguir esta respuesta de
    una servida desde memoria hace dos días — que es justo lo que pasó."""
    s, ruta = _monta(tmp_path, monkeypatch)
    with _cliente(s) as c:
        d = _org(c)
    assert d["loaded_sha256"] == _sha(ruta)
    assert d["source_sha256"] == _sha(ruta)
    assert d["stale"] is False
    assert d["cargado_en"]          # nombre ya existente en el contrato


def test_cambiar_la_fuente_se_refleja_sin_reiniciar(tmp_path, monkeypatch):
    """El fallo de producción, exacto: la fuente gana un rol y el endpoint tiene
    que enterarse SIN reinicio.

    FALSADOR: con `JERARQUIA` calculada al importar, el rol nuevo no aparece
    nunca y esta aserción cae — que es el estado de hoy."""
    s, ruta = _monta(tmp_path, monkeypatch)
    with _cliente(s) as c:
        antes = _org(c)
        assert "engineering-manager" not in antes["jerarquia"]

        nuevo = json.loads(json.dumps(ORG_INICIAL))
        nuevo["jerarquia"]["engineering-manager"] = {
            "reporta_a": "cto", "capa": "ejecucion", "estado": "pendiente_arranque"}
        ruta.write_text(json.dumps(nuevo))

        despues = _org(c)
    assert "engineering-manager" in despues["jerarquia"], despues
    assert despues["loaded_sha256"] == _sha(ruta)
    assert despues["loaded_sha256"] != antes["loaded_sha256"]
    assert despues["stale"] is False


def test_reemplazo_por_rename_tambien_se_ve(tmp_path, monkeypatch):
    """La forma EXACTA del fallo: el host no edita en sitio, escribe un fichero
    nuevo y lo renombra encima. Eso cambia el inodo, que es lo que rompió el
    bind-mount y congeló la lectura.

    FALSADOR: una implementación que cachee por inodo o por descriptor abierto
    pasa el test anterior y falla éste."""
    s, ruta = _monta(tmp_path, monkeypatch)
    with _cliente(s) as c:
        _org(c)
        nuevo = json.loads(json.dumps(ORG_INICIAL))
        nuevo["jerarquia"]["sdet"] = {"reporta_a": "cto", "capa": "gate"}
        tmp = ruta.with_suffix(".nuevo")
        tmp.write_text(json.dumps(nuevo))
        tmp.replace(ruta)                      # rename encima: inodo distinto
        d = _org(c)
    assert "sdet" in d["jerarquia"], d
    assert d["stale"] is False


def test_fuente_ilegible_no_puede_decir_stale_false(tmp_path, monkeypatch):
    """La invariante del operador: **nunca** «la fuente cambió · sirvo viejo ·
    stale=false». Si no se puede leer la fuente, no se puede afirmar frescura.

    Se sigue sirviendo lo último bueno —una jerarquía vacía sería peor, el agente
    leería «no reporto a nadie»— pero marcado y con aviso.

    FALSADOR: si `stale` se calculara como «lo cargué alguna vez», aquí saldría
    False y el endpoint estaría afirmando frescura que no puede sostener."""
    s, ruta = _monta(tmp_path, monkeypatch)
    with _cliente(s) as c:
        assert _org(c)["stale"] is False
        ruta.unlink()                          # el mount roto, en su forma pura
        d = _org(c)
    assert d["stale"] is True, d
    assert d["source_sha256"] is None
    assert d["jerarquia"], "se tira la jerarquía buena: peor que servirla marcada"
    assert d["aviso"] and "rancio" in d["aviso"].lower()


def test_la_identidad_se_refresca_con_la_misma_fuente(tmp_path, monkeypatch):
    """El otro lado del mismo fichero, y el que decide de verdad: un alta firmada
    en la fuente debe dejar de dar 422 sin reiniciar el servicio.

    Es el caso `engineering-manager` medido en producción — existía en el
    organigrama y NO tenía identidad: se podía crear un rol incapaz de recibir
    trabajo.

    FALSADOR: `ROLES_ALIAS` congelado al importar mantiene el 422 para siempre."""
    s, ruta = _monta(tmp_path, monkeypatch)
    with _cliente(s) as c:
        assert c.get("/inbox/engineering-manager").status_code == 422

        nuevo = json.loads(json.dumps(ORG_INICIAL))
        nuevo["rol_por_alias"]["engineering-manager"] = "engineering-manager"
        nuevo["jerarquia"]["engineering-manager"] = {"reporta_a": "cto", "capa": "ejecucion"}
        ruta.write_text(json.dumps(nuevo))

        _org(c)                                # la petición que refresca
        assert c.get("/inbox/engineering-manager").status_code != 422


def test_sin_fuente_montada_no_se_finge_frescura(tmp_path, monkeypatch):
    """CONTROL: sin fichero montado el endpoint ya avisaba, y debe seguir
    haciéndolo. Sin este control, un `stale=True` constante pasaría los tests de
    arriba sin medir nada."""
    s = construir(tmp_path, monkeypatch)       # sin LLMINBOX_ROLES_ALIAS
    with _cliente(s) as c:
        d = _org(c)
    assert d["roles"] == 0
    assert d["jerarquia"] == {}
    assert d["aviso"] and "NO montada" in d["aviso"]
    assert d["stale"] is True


# ── tres de CodeRabbit, las tres de la misma familia ──────────────────────────

def test_una_fuente_con_forma_ajena_no_revienta_ni_pierde_lo_bueno(tmp_path, monkeypatch):
    """`json.loads` acepta `[]` y `"texto"`: son JSON válido y no son un
    organigrama. Construir los mapas FUERA del bloque protegido convertía eso en
    un 500 — y un 500 no es «rancio», es que el endpoint se cae.

    Una fuente con forma ajena es indistinguible de una ilegible: no se puede
    derivar nada de ella. Se trata igual — se conserva lo último bueno y se marca.

    FALSADOR: construir los mapas fuera del `try` da 500 y las tres aserciones
    caen a la vez."""
    s, ruta = _monta(tmp_path, monkeypatch)
    with _cliente(s) as c:
        assert _org(c)["stale"] is False
        ruta.write_text("[]")                    # JSON válido, organigrama no
        d = _org(c)
    assert d["stale"] is True, d
    # LA JERARQUÍA EXACTA, no «no vacía». Una regresión que sustituyera el estado
    # bueno por otro mapa cualquiera pasaba la versión anterior de esta aserción:
    # medía que hubiera ALGO, no que fuera lo correcto.
    assert d["jerarquia"] == ORG_INICIAL["jerarquia"], d["jerarquia"]
    assert d["aviso"] and "rancio" in d["aviso"].lower()


def test_la_respuesta_sale_de_UNA_instantanea(tmp_path, monkeypatch):
    """La misma carrera que el sello del #5, un piso más arriba: si el endpoint
    lee los globales DESPUÉS de refrescar, otra petición concurrente puede haber
    cambiado la revisión en medio — y se serviría el hash de una con la jerarquía
    de otra. Un organigrama así no es viejo: es imposible.

    Se falsa sin simular concurrencia (sería un test que a veces pasa): se ejerce
    la PROPIEDAD que la concurrencia rompería. El refresco se envuelve para que,
    justo después de devolver su instantánea, los globales cambien —que es lo que
    haría el otro hilo—. La respuesta tiene que seguir siendo coherente consigo
    misma.

    FALSADOR: que el endpoint lea `lp.JERARQUIA` en vez de la instantánea hace que
    aquí salga la jerarquía pisada."""
    s, _ruta = _monta(tmp_path, monkeypatch)
    lp = s.lp
    real = lp.refrescar_organigrama

    def refresca_y_pisa():
        foto = real()
        # SE PISAN LOS CUATRO. Pisar sólo `JERARQUIA` y `ORG_SHA` dejaba pasar un
        # endpoint que releyera `ORG_REVISION` o `ORG_CARGADO_EN` de los globales:
        # el test cubría dos campos de la instantánea y afirmaba cubrirla entera.
        lp.JERARQUIA = {"PISADO-POR-OTRO-HILO": {"reporta_a": "nadie"}}
        lp.ORG_SHA = "hash-de-otra-revision"
        lp.ORG_REVISION = "revision-de-otro-hilo"
        lp.ORG_CARGADO_EN = "1999-01-01T00:00:00+00:00"
        return foto

    monkeypatch.setattr(lp, "refrescar_organigrama", refresca_y_pisa)
    with _cliente(s) as c:
        d = _org(c)
    assert d["jerarquia"] == ORG_INICIAL["jerarquia"], d["jerarquia"]
    assert d["loaded_sha256"] != "hash-de-otra-revision", d
    assert d["revision"] != "revision-de-otro-hilo", d
    assert d["cargado_en"] != "1999-01-01T00:00:00+00:00", d


def test_sin_carga_valida_no_se_finge_una_hora(tmp_path, monkeypatch):
    """`cargado_en` significa «cuándo se cargó el ORGANIGRAMA». Si nunca hubo
    carga válida, devolver la hora de arranque del proceso es exactamente la
    mentira que esta rama vino a quitar: un sello que afirma una carga que no
    ocurrió.

    FALSADOR: el fallback a `ARRANCADO_EN` publica una hora con `jerarquia: {}`."""
    s = construir(tmp_path, monkeypatch)          # sin fuente montada
    with _cliente(s) as c:
        d = _org(c)
    assert d["jerarquia"] == {}
    assert d["cargado_en"] is None, d


def test_leida_sin_jerarquia_no_se_confunde_con_no_montada(tmp_path, monkeypatch):
    """Montada-y-leída-sin-`jerarquia` NO es lo mismo que no montada.

    La rama del aviso miraba sólo `j`, así que una fuente presente, legible en
    ESTA petición y con el hash coincidiendo respondía «jerarquía NO montada
    (LLMINBOX_ROLES_ALIAS vacío o ilegible)». Las dos causas que nombra son
    FALSAS en ese caso: el operador sale a buscar una avería de montaje que no
    existe, en vez del campo ausente en el fichero firmado. Un aviso que imputa
    una causa que su comprobación no midió es peor que no avisar.

    `est["source_sha256"]` ya separa los dos mundos: es `None` sólo cuando no
    hubo lectura válida en esta petición.

    FALSADOR: con la rama mirando sólo `j`, el aviso dice «NO montada» y la
    aserción del texto cae; `stale` además se afirmaría sin haberlo medido.
    """
    s, ruta = _monta(tmp_path, monkeypatch, org={"roles_alias": {"cto": "cto"}})
    with _cliente(s) as c:
        d = _org(c)
    # La lectura SÍ ocurrió: hay hash de fuente y coincide con el cargado.
    assert d["source_sha256"] == _sha(ruta), d
    assert d["stale"] is False, d
    assert d["jerarquia"] == {} and d["roles"] == 0, d
    # Y el aviso no puede imputar el montaje.
    aviso = (d.get("aviso") or "").lower()
    assert "no montada" not in aviso, aviso
    assert "ilegible" not in aviso, aviso
    assert "jerarquia" in aviso or "jerarquía" in aviso, aviso


def test_la_caida_a_rancio_se_anuncia_UNA_vez_y_se_rearma(tmp_path, monkeypatch, capsys):
    """Que la fuente se vuelva ilegible no imprimía NADA.

    CodeRabbit lo señaló como «registra sólo en la transición», dando por hecho
    que ya había registro que deduplicar. No lo había: el `except` devolvía la
    foto rancia en silencio. El hallazgo estaba mal en la premisa y bien en la
    forma — en una flota desatendida el único aviso era que alguien preguntara
    por `/organigrama`, y nadie pregunta hasta que algo ya salió mal.

    Una vez, porque `refrescar_organigrama()` corre en CADA petición: registrar
    sin deduplicar convierte una avería en miles de líneas idénticas y entierra
    todo lo demás. Y con rearme, porque si no, la SEGUNDA caída —la de la semana
    que viene— sería la que no se anuncia.

    FALSADOR: sin el marcador, la segunda petición vuelve a imprimir y `una vez`
    cae; sin el rearme en la rama de éxito, la segunda caída no imprime y
    `otra vez` cae.
    """
    s, ruta = _monta(tmp_path, monkeypatch)
    with _cliente(s) as c:
        assert _org(c)["stale"] is False
        capsys.readouterr()                       # descarto el ruido del arranque

        ruta.write_text("{ roto")                 # ilegible
        assert _org(c)["stale"] is True
        una_vez = capsys.readouterr().out
        assert una_vez.count("organigrama") >= 1, una_vez

        _org(c)                                   # dos peticiones más,
        _org(c)                                   # la misma avería
        assert "organigrama" not in capsys.readouterr().out.lower(), "repitió el aviso"

        ruta.write_text(json.dumps(ORG_INICIAL))  # se recupera
        assert _org(c)["stale"] is False
        capsys.readouterr()

        ruta.write_text("{ roto otra vez")        # y vuelve a caer
        assert _org(c)["stale"] is True
        otra_vez = capsys.readouterr().out
        assert "organigrama" in otra_vez.lower(), "el marcador no se rearmó: " + repr(otra_vez)
