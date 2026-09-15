"""Falsadores de la demo de migración/rollback v6→v7 (extremo operativo 0.9).

El ciclo completo — con el ancla 0.9 en subprocess — ES el falsador del
objetivo «migración y rollback DEMOSTRADOS»: corre en segundos porque todo es
SQLite local y el kernel 0.9 es stdlib-only.

Estructura: 2 guardas baratas de precondición; el ciclo entero con sus tres
postcondiciones (IDA · falsador ⊖ · VUELTA operativa); el clavo de que el
insumo del ⊖ era v7 de verdad; y los del gancho de fase 2 (hallazgo de @infra
20:31Z): el modo del ancla se DECLARA y no se infiere, la identidad se MIDE
superficie a superficie — el modo oci contra los blobs del SHA, el modo arbol
contra los hashes fijados en el código (el manifest vecino no es autoridad) —
un directorio que no sea el ancla muere antes de tocar al sujeto, y la muerte
del extremo operativo rotula FALSADA, no no-ejecutable (taxonomía rc de @qa).

El modo arbol corre sobre el fixture bench/ancla-v09: los mismos bytes v0.9
certificados, fletados por contenido para un checkout público limpio sin la
historia git del ancla. Los modos git y oci exigen esa historia y sus límites
declarados quedan intactos.
"""
from __future__ import annotations

import pathlib
import shutil
import sqlite3
import subprocess

import pytest

from bench import demo_migracion_rollback as D

_RAIZ_REPO = pathlib.Path(D.__file__).resolve().parents[1]


def _copia_fixture_en(destino: pathlib.Path) -> None:
    """Copia el fixture del ancla (bench/ancla-v09) con su layout: raíz +
    tests/journal. Es la fuente de bytes del ancla en un checkout público."""
    for superficie in D.SUPERFICIES_ANCLA:
        ruta = destino / superficie
        ruta.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_RAIZ_REPO / D.ANCLA_V09_RUTA / superficie, ruta)


def test_worktree_ancla_rechaza_destino_existente(tmp_path):
    """El ancla certificado no se recicla: destino ocupado ⇒ rechazo previo."""
    ocupado = tmp_path / "ancla"
    ocupado.mkdir()
    with pytest.raises(D.DemoNoEjecutable, match="ya existe"):
        D._worktree_ancla(ocupado)


def test_la_demo_no_reusa_base_de_evidencia(tmp_path):
    """Misma disciplina que el banco: base de evidencia preexistente ⇒ rc de
    guarda, y ni un byte del sujeto se toca (esto muere ANTES del worktree)."""
    db = tmp_path / "coordination.sqlite"
    db.write_bytes(b"no es una base")
    with pytest.raises(D.DemoNoEjecutable, match="ya existe"):
        D.ejecuta_demo(db, worktree_ancla=tmp_path / "ancla")


def test_ciclo_completo_ida_falsador_y_vuelta_operativa(tmp_path):
    db = tmp_path / "coordination.sqlite"
    reporte = D.ejecuta_demo(db, modo_ancla="arbol", eventos_v6=2)
    assert reporte["veredicto"] == "DEMO_OK"
    # el recibo identifica CADA superficie ejecutada, con el hash medido igual
    # al fijado en el código
    assert reporte["ancla_modo"] == "arbol"
    assert reporte["ancla_ejecucion"] == "fixture_ancla_v09_en_host"
    assert reporte["ancla_identidad"] == D.ANCLA_V09_SUPERFICIES_SHA256
    # IDA: el kernel 1.0 dejó v7 con la fotografía v6 acreditada y conteos
    assert reporte["ida"]["durable_v"] == 7
    assert reporte["ida"]["fotografia"]["source_durable_v"] == 6
    assert reporte["ida"]["conteos_preservados"] is True
    assert reporte["fase_0_v6_del_ancla"]["conteos"]["durable_v"] == 6
    # ⊖: el falsador del instrumento discriminó — el ancla NO acepta v7
    assert reporte["falsador_instrumento"]["excepcion"] == "SchemaTooNew"
    assert "durable_v=7" in reporte["falsador_instrumento"]["mensaje"]
    # VUELTA: bytes == fotografía Y un proceso 0.9 re-abrió y ESCRIBIÓ
    assert reporte["vuelta"]["sha256_base"] == reporte["ida"]["fotografia"]["sha256"]
    assert reporte["vuelta"]["durable_v"] == 6
    assert reporte["vuelta"]["ancla_reabrio_y_escribio"] is True
    assert reporte["vuelta"]["eventos_tras_reapertura"] == (
        reporte["fase_0_v6_del_ancla"]["conteos"]["events"] + 1)
    # la pérdida post-snapshot es por diseño, declarada y verificada
    assert reporte["perdido_post_snapshot"]["presente_en_restaurada"] is False


def test_los_bytes_que_ofrece_el_falsador_son_v7_de_verdad(tmp_path):
    db = tmp_path / "coordination.sqlite"
    D.ejecuta_demo(db, modo_ancla="arbol")
    falsador = db.with_name(db.name + ".v7-para-falsador")
    con = sqlite3.connect(f"file:{falsador}?mode=ro", uri=True)
    try:
        assert con.execute(
            "SELECT v FROM meta WHERE k='durable_v'").fetchone()[0] == "7"
    finally:
        con.close()


# ── El gancho de fase 2: dos modos EXPLÍCITOS, identidad MEDIDA ──────────────

def test_el_modo_del_ancla_se_declara_y_no_se_infiere(tmp_path):
    """modo_ancla inválido ⇒ rechazo; oci sin ruta ⇒ exige (no recurre al modo
    git ni crea nada); oci con ruta inexistente ⇒ muere y NO deja nada creado;
    arbol con --worktree-ancla ⇒ rechazado (corre sobre el fixture de ESTE
    árbol, nada se redirige en silencio). Un typo en la ruta no puede cambiar
    de modo en silencio."""
    db = tmp_path / "c.sqlite"
    with pytest.raises(D.DemoNoEjecutable, match="desconocido"):
        D.ejecuta_demo(db, modo_ancla="tar")
    with pytest.raises(D.DemoNoEjecutable, match="exige"):
        D.ejecuta_demo(db, modo_ancla="oci")
    extraccion = tmp_path / "extraccion"
    with pytest.raises(D.DemoNoEjecutable, match="no existe"):
        D.ejecuta_demo(db, worktree_ancla=extraccion, modo_ancla="oci")
    assert not extraccion.exists()
    ancla = tmp_path / "ancla"
    with pytest.raises(D.DemoNoEjecutable, match="no acepta"):
        D.ejecuta_demo(db, worktree_ancla=ancla, modo_ancla="arbol")
    assert not ancla.exists() and not db.exists()


@pytest.mark.parametrize("superficie", D.SUPERFICIES_ANCLA)
def test_identidad_arbol_rechaza_un_fixture_impostor(tmp_path, superficie):
    """EL falsador del gancho (hallazgo de @infra), forma arbol: un directorio
    que NO sea el ancla debe MORIR, no correr. Vacío ⇒ falta de superficie;
    bytes impostores ⇒ IDENTIDAD DEL ANCLA NO VERIFICADA nombrando la
    superficie — contra los hashes FIJADOS en el código, no contra el
    manifest vecino."""
    vacio = tmp_path / "vacio"
    vacio.mkdir()
    with pytest.raises(D.DemoNoEjecutable, match="no trae"):
        D._identidad_ancla_arbol(vacio)
    impostor = tmp_path / "impostor"
    _copia_fixture_en(impostor)
    (impostor / superficie).write_text("# no es el archivo del ancla\n")
    with pytest.raises(D.DemoNoEjecutable,
                       match="IDENTIDAD DEL ANCLA NO VERIFICADA"):
        D._identidad_ancla_arbol(impostor)


def test_el_manifest_del_fixture_no_puede_desviarse_del_codigo(tmp_path):
    """El manifest vecino NO es la autoridad de la identidad — un fichero
    modificable por sí solo no prueba procedencia: los hashes FIJADOS en el
    código mandan. El manifest del fixture debe ser su copia exacta, y un
    manifest que discrepe (aunque los bytes sean fieles) es un fallo de
    preparación, no una identidad aceptada."""
    fixture = _RAIZ_REPO / D.ANCLA_V09_RUTA
    assert D._lee_manifest_ancla_v09(fixture) == D.ANCLA_V09_SUPERFICIES_SHA256
    mentiroso = tmp_path / "fixture"
    shutil.copytree(fixture, mentiroso)
    (mentiroso / "MANIFEST.sha256").write_text(
        "0" * 64 + "  coordination.py\n", encoding="utf-8")
    with pytest.raises(D.DemoNoEjecutable, match="MANIFEST"):
        D._identidad_ancla_arbol(mentiroso)


def test_limpieza_solo_pide_retirar_el_worktree_temporal_propio(tmp_path, monkeypatch):
    temporal = tmp_path / "temporal-propio"
    ancla = temporal / "ancla"
    ancla.mkdir(parents=True)
    ajeno = tmp_path / "ajeno"
    ajeno.mkdir()
    (ajeno / "evidencia").write_bytes(b"conservar")
    llamadas = []

    def git_remove(argv, **kwargs):
        llamadas.append(argv)
        assert argv == ["git", "-C", str(D._RAIZ), "worktree", "remove",
                        "--", str(ancla)]
        ancla.rmdir()
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(D.subprocess, "run", git_remove)
    resultado = D._retira_ancla_temporal(temporal, ancla, creada=True)
    assert resultado == {"intentada": True, "completada": True}
    assert len(llamadas) == 1
    assert not temporal.exists()
    assert (ajeno / "evidencia").read_bytes() == b"conservar"


def test_limpieza_rechazada_conserva_el_directorio_y_no_fuerza(tmp_path, monkeypatch):
    temporal = tmp_path / "temporal-propio"
    ancla = temporal / "ancla"
    ancla.mkdir(parents=True)
    evidencia = ancla / "cambio-no-previsto"
    evidencia.write_bytes(b"conservar")
    llamadas = []

    def rechazo(argv, **kwargs):
        llamadas.append(argv)
        return subprocess.CompletedProcess(argv, 128)

    monkeypatch.setattr(D.subprocess, "run", rechazo)
    resultado = D._retira_ancla_temporal(temporal, ancla, creada=True)
    assert resultado["completada"] is False
    assert resultado["error"] == "git_worktree_remove_rechazado"
    assert len(llamadas) == 1
    assert "--force" not in llamadas[0] and "prune" not in llamadas[0]
    assert evidencia.read_bytes() == b"conservar"


def test_la_muerte_del_extremo_operativo_es_falsada_no_arranque(tmp_path):
    """Taxonomía rc (nota de @qa): un rollback que dejó una base inoperable
    EMPEZÓ y FALLÓ ⇒ rc 1 (DemoFalsada), no rc 2. La muerte del corredor en el
    extremo operativo es evidencia contra el objetivo; la misma muerte en las
    fases de preparación sigue siendo precondición (rc 2)."""
    roto = tmp_path / "roto"  # sin código: el corredor no puede importar nada
    roto.mkdir()
    with pytest.raises(D.DemoFalsada, match="RESTAURADA"):
        D._corre_ancla("reabre_v6", tmp_path / "c.sqlite", roto, 1,
                       si_muere="falsada")
    with pytest.raises(D.DemoNoEjecutable, match="murió"):
        D._corre_ancla("escribe_v6", tmp_path / "c.sqlite", roto, 1)
