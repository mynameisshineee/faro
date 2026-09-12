# bench/ancla-v09 — el ancla 0.9 por contenido

Las tres superficies del corredor de `bench/demo_migracion_rollback.py`
(`SUPERFICIES_ANCLA`: `coordination.py`, `ledger_parse.py`,
`tests/journal/_arnes.py`) con sus bytes EXACTOS del ancla certificado
`767702559817759a36b0be4715af6e1a8ccf9f7b`.

Existe para el modo `--modo-ancla arbol`: un checkout público limpio no tiene
la historia git donde vive ese SHA, así que el ancla viaja como fixture
ordinario, verificado POR CONTENIDO. La identidad esperada está fijada en el
código (`ANCLA_V09_SUPERFICIES_SHA256` en la demo); `MANIFEST.sha256` es su
copia trazable y debe coincidir. Los modos `git` y `oci` quedan intactos para
instalaciones con la historia completa.

Higiene de publicación: los tres blobs son versiones v0.9 de ficheros YA
publicados en este árbol (v7); cada identificador que contienen (vocabulario
llminbox, citas MARK, nombres de la flota) existe también en las versiones v7
publicadas — cero contenido nuevo no público. Verificado al fletar el fixture.
