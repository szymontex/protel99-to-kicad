#!/usr/bin/env python3
"""Protel `.ddb` design databases - what is inside one, and the boards we can get out.

A `.ddb` is the container almost every Protel 99 and 99 SE project is actually
delivered in. It is a Microsoft Jet (Access) database - every one begins
`\\x00\\x01\\x00\\x00Standard Jet DB` - holding the whole design: boards,
schematics, libraries and netlists as stored documents.

Most of those documents are `PCB 4.0 Binary File`, which this package does not
decode. But a board saved as **PCB ASCII** keeps its `|RECORD=...|` text in the
database, and that text is readable without understanding Jet at all: the
records are found where they lie and the database's own page headers, which cut
across them, are tolerated by the ASCII reader.

Measured across 91 public `.ddb` files carrying any `|RECORD=` text, 1 held a
board with objects in it and 90 held only the options records that accompany a
binary document. So this is a narrow door, not a general `.ddb` reader - which
is exactly what it reports when it finds nothing to read.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from . import pcbascii
from .pcb9 import Board

MAGIC = b"\x00\x01\x00\x00Standard Jet DB"

# Documents that can sit inside a design database, by the string each one
# announces itself with.
CONTENTS = {
    b"PCB 4.0 Binary File": "PCB 4.0 binary board",
    b"PCB 4.0 Binary Library File": "PCB 4.0 binary footprint library",
    b"PCB 3.0 Binary File": "PCB 3.0 binary board",
    b"PCB FILE 9 VERSION": "PCB FILE 9 binary board",
    b"PCB FILE 6 VERSION": "PCB ASCII board",
    b"PCB FILE 4": "Autotrax board",
    b"PCB FILE 5": "Easytrax board",
    b"Protel for Windows - Schematic": "schematic",
    b"PROTEL PCBLIB": "footprint library",
}


class ParseError(Exception):
    pass


def inventory(path: Path) -> dict:
    """What the database appears to contain, counted by announcing string.

    These are occurrences of a marker, not a document count: a document can be
    stored more than once, and Jet keeps superseded pages. Treat it as a guide
    to what is in there, not a census.
    """
    data = Path(path).read_bytes()
    found = {}
    for marker, name in CONTENTS.items():
        n = data.count(marker)
        if n:
            found[name] = found.get(name, 0) + n
    return found


def parse(path: Path) -> Board:
    """The board stored as PCB ASCII inside the database.

    Raises `ParseError` naming the contents when there is no such board, which
    is the common case: the design is there, in a binary this package cannot
    read yet.
    """
    path = Path(path)
    data = path.read_bytes()
    if not data.startswith(MAGIC):
        raise ParseError(f"{path.name}: not a Jet design database")

    carved = _carve_text_board(path, data)
    if carved is not None:
        return carved

    if b"|RECORD=" not in data:
        raise ParseError(
            f"{path.name}: no PCB ASCII document inside. Contents: "
            f"{_describe(inventory(path))}. Open the project in Protel and save "
            f"the board as PCB ASCII, or see docs/COMPATIBILITY.md")

    board = pcbascii.parse(path)
    objects = (len(board.components) + len(board.tracks) + len(board.pads)
               + len(board.arcs) + len(board.vias) + len(board.fills))
    if objects == 0:
        raise ParseError(
            f"{path.name}: the PCB ASCII text inside holds only board options, "
            f"no objects - the design itself is in another format. Contents: "
            f"{_describe(inventory(path))}")

    # Several boards saved as ASCII into one database would concatenate into
    # one nonsense board. Each document restarts its component numbering, so
    # more than one `ID=0` component is the signal that this has happened.
    starts = len(re.findall(rb"\|RECORD=Component\|ID=0\|", data))
    if starts > 1:
        board.errors.append(
            (0, f"{starts} PCB ASCII documents in this database were read as "
                f"one board"))
    board.version = f"{board.version} in .ddb"
    return board


def _carve_text_board(path: Path, data: bytes) -> Board | None:
    """Cut out a line-based board document stored whole inside the database.

    `PCB FILE 4`, `5` and `6` documents are text and end with `ENDPCB`, so both
    ends of one are findable without understanding Jet: the header string
    begins it and the terminator ends it. The database may still have written
    its own page headers through the middle, which the readers report as
    salvaged records rather than losing the board over.

    Returns None when no such document is present, leaving the caller to try
    the `|RECORD=` route.
    """
    from . import pcb4, pcb6            # local: avoids an import cycle

    readers = ((b"PCB FILE 6 VERSION", pcb6.parse),
               (b"PCB FILE 5", pcb4.parse),
               (b"PCB FILE 4", pcb4.parse))
    for marker, reader in readers:
        start = data.find(marker)
        if start < 0:
            continue
        end = data.find(b"ENDPCB", start)
        if end < 0:
            continue
        chunk = data[start:end + len(b"ENDPCB")] + b"\r\n"

        # The reader takes a path, and handing it a temporary file would put a
        # meaningless name on the board. Write beside nothing, parse in memory.
        scratch = _TextDocument(path, chunk)
        board = reader(scratch)
        board.version = f"{board.version} in .ddb"
        return board
    return None


class _TextDocument:
    """A path-shaped object whose bytes are a document carved out of a database."""

    def __init__(self, path: Path, data: bytes):
        self._path = Path(path)
        self._data = data
        self.name = self._path.name

    def read_bytes(self) -> bytes:
        return self._data

    def __fspath__(self) -> str:
        return str(self._path)

    def __str__(self) -> str:
        return str(self._path)


def _describe(found: dict) -> str:
    if not found:
        return "nothing recognised"
    return ", ".join(f"{n} x {name}" for name, n in sorted(found.items()))


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        p = Path(arg)
        print(f"{p.name}: {_describe(inventory(p))}")
        try:
            b = parse(p)
        except ParseError as e:
            print(f"  no board: {e}")
            continue
        print("  " + "  ".join(f"{k} {v}" for k, v in pcbascii.parsed_counts(b).items()))
