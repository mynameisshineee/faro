"""Cobertura del empaquetado, usando Git real y documentos de prueba neutros.

    python3 -m unittest discover -s tests -p 'test_higiene_superficie.py' -v
"""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    "higiene", Path(__file__).resolve().parents[1] / "tools" / "higiene.py")
higiene = importlib.util.module_from_spec(spec)
spec.loader.exec_module(higiene)


class SuperficiePublicadaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporal = tempfile.TemporaryDirectory(prefix="higiene-superficie-")
        cls.addClassCleanup(cls.temporal.cleanup)
        cls.raiz = Path(cls.temporal.name)
        # No usar firmas, hooks ni la identidad del operador en el repo de prueba.
        cls.entorno = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        cls.entorno.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
        cls.git("init", "-q")
        for i in range(50):
            (cls.raiz / f"producto-{i}.txt").write_text("fixture\n", encoding="utf-8")
        (cls.raiz / "docs").mkdir()
        (cls.raiz / "docs" / "sin declarar.md").write_text("fixture\n", encoding="utf-8")
        (cls.raiz / "internal").mkdir()
        for nombre in ["nota.md", "declarado.md"]:
            (cls.raiz / "internal" / nombre).write_text("fixture\n", encoding="utf-8")
        (cls.raiz / ".gitattributes").write_text(
            "internal export-ignore\ninternal/** export-ignore\nrecibos export-ignore\n",
            encoding="utf-8")
        (cls.raiz / ".gitignore").write_text("local-only/\n", encoding="utf-8")
        (cls.raiz / "OSS-MANIFEST.txt").write_text(
            "\n".join([f"producto-{i}.txt" for i in range(10)]
                      + ["internal/declarado.md", "solo-manifiesto/"]) + "\n",
            encoding="utf-8")
        cls.git("add", ".")
        cls.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
                "commit", "-qm", "Neutral publication fixture")

    @classmethod
    def git(cls, *args):
        return subprocess.run(["git", *args], cwd=cls.raiz, env=cls.entorno,
                              check=True, capture_output=True, text=True).stdout

    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.object(higiene, "RAIZ", str(self.raiz)).start()
        patch.dict(os.environ, self.entorno, clear=True).start()

    def test_documento_archivado_sin_declarar_esta_en_la_superficie(self):
        publicado = higiene.superficie_publicada()
        self.assertTrue(publicado("docs/sin declarar.md"))
        self.assertTrue(publicado("producto-0.txt"))
        self.assertFalse(publicado("inexistente.md"))

    def test_export_ignore_se_respeta_y_el_manifiesto_sigue_sumando(self):
        self.assertIn("internal/nota.md", self.git("ls-tree", "-r", "--name-only", "HEAD"))
        publicado = higiene.superficie_publicada()
        self.assertFalse(publicado("internal/nota.md"))
        self.assertTrue(publicado("internal/declarado.md"))
        self.assertTrue(publicado("solo-manifiesto/nuevo.md"))
        self.assertFalse(publicado("solo-manifiesto-extra/nuevo.md"))

    def escribir_nuevo(self, nombre):
        ruta = self.raiz / nombre
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text("fixture\n", encoding="utf-8")
        self.addCleanup(ruta.unlink)

    def test_nuevos_sin_commit_respetan_exclusiones_de_archivo_y_directorio(self):
        nombres = ["docs/nuevo sin declarar.md", "docs/nota-é.md",
                   "internal/nuevo.md", "recibos/nuevo.md", "local-only/nuevo.md"]
        for nombre in nombres:
            self.escribir_nuevo(nombre)
        publicado = higiene.superficie_publicada()
        for nombre in nombres[:2]:
            with self.subTest(nombre=nombre):
                self.assertTrue(publicado(nombre))
        for nombre in nombres[2:]:
            with self.subTest(nombre=nombre):
                self.assertFalse(publicado(nombre))

    def test_anadir_al_indice_no_cambia_la_cobertura(self):
        nombre = "docs/añadido.md"
        self.escribir_nuevo(nombre)
        self.assertTrue(higiene.superficie_publicada()(nombre))
        self.git("add", "--", nombre)
        self.addCleanup(self.git, "reset", "-q", "HEAD", "--", nombre)
        self.assertTrue(higiene.superficie_publicada()(nombre))

    def test_retirar_export_ignore_incluye_el_archivo_antes_del_commit(self):
        atributos = self.raiz / ".gitattributes"
        original = atributos.read_bytes()
        self.addCleanup(atributos.write_bytes, original)
        atributos.write_text("", encoding="utf-8")
        self.assertTrue(higiene.superficie_publicada()("internal/nota.md"))

    def test_atributos_ilegibles_no_reducen_la_cobertura(self):
        ejecutar = subprocess.run

        def con_fallo(cmd, *args, **kwargs):
            if cmd[:2] == ["git", "check-attr"]:
                raise subprocess.CalledProcessError(1, cmd)
            return ejecutar(cmd, *args, **kwargs)

        with patch.object(subprocess, "run", side_effect=con_fallo), \
                contextlib.redirect_stderr(io.StringIO()) as salida:
            with self.assertRaises(SystemExit) as fallo:
                higiene.superficie_publicada()
        self.assertEqual(fallo.exception.code, 2)
        self.assertIn("FATAL: cannot determine pre-commit", salida.getvalue())

    def test_no_hay_cobertura_si_git_archive_falla(self):
        with tempfile.TemporaryDirectory(prefix="higiene-sin-git-") as vacio:
            with patch.object(higiene, "RAIZ", vacio), contextlib.redirect_stderr(io.StringIO()) as salida:
                with self.assertRaises(SystemExit) as fallo:
                    higiene.ficheros_del_tarball()
        self.assertEqual(fallo.exception.code, 2)
        self.assertIn("FATAL: cannot read", salida.getvalue())

    def test_un_tarball_vacio_no_acredita_una_superficie_limpia(self):
        with tempfile.TemporaryDirectory(prefix="higiene-vacio-") as vacio:
            subprocess.run(["git", "init", "-q", vacio], env=self.entorno, check=True)
            subprocess.run(
                ["git", "-C", vacio, "-c", "user.name=Fixture", "-c",
                 "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
                 "-c", "core.hooksPath=/dev/null", "commit", "--allow-empty", "-qm", "Empty fixture"],
                env=self.entorno, check=True, capture_output=True)
            with patch.object(higiene, "RAIZ", vacio), contextlib.redirect_stderr(io.StringIO()) as salida:
                with self.assertRaises(SystemExit) as fallo:
                    higiene.ficheros_del_tarball()
        self.assertEqual(fallo.exception.code, 2)
        # tarfile puede rechazar un archive vacío al leer su cabecera o dejar
        # que la guarda de cardinalidad lo rechace. Ambos deben fallar cerrado.
        self.assertRegex(salida.getvalue(), r"^FATAL: .*tarball")


if __name__ == "__main__":
    unittest.main()
