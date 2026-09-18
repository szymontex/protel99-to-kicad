"""Convert legacy Protel board files to KiCad.

One entry point, `protel-to-kicad`, and one model. `formats` identifies a file
from its header and hands it to the reader that decodes it; every reader
returns the same `Board`, so the KiCad writer never asks which generation of
Protel wrote the file it is given.

    from pathlib import Path
    from protel_to_kicad import formats, pcb9_kicad

    board, fmt = formats.parse(Path("board.PCB"))
    text, stats = pcb9_kicad.generate(board, board.outline_layer or fmt.outline_layer)

Readers: `pcb9` (binary, versions 2.00 / 2.60 / 2.70), `pcb4` (Autotrax and
Easytrax text), `pcb6` (Protel PCB ASCII), `pcbascii` (the later `|RECORD=|`
PCB ASCII), `ddb` (Protel design databases, which hold a whole project).
"""

from protel_to_kicad.pcb9 import Board

__all__ = ["Board"]
