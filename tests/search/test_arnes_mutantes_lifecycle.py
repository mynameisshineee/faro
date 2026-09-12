"""Guardas baratas del arnés lifecycle; la corrida causal vive en su script."""

from tests.search import mutantes_lifecycle as M


def test_censo_lifecycle_es_cerrado_y_sus_agujas_son_unicas():
    assert len(M.MUTANTES) == 6
    assert len({m.nombre for m in M.MUTANTES}) == len(M.MUTANTES)
    for mutante in M.MUTANTES:
        assert (M.ROOT / mutante.fichero).read_text().count(mutante.viejo) == 1
