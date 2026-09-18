#!/usr/bin/env python3
"""Protel `.ddb` design databases - what is inside one, and the boards we can get out.

A `.ddb` is the container almost every Protel 99 and 99 SE project is actually
delivered in. It is a Microsoft Jet (Access) database - every one begins
`\\x00\\x01\\x00\\x00Standard Jet DB` - holding the whole design: boards,
schematics, libraries and netlists as stored documents.

Jet is not decoded here and does not need to be. Every Protel document
announces itself with a header string, so the documents can be found where they
lie and cut out: a text board between its header and its `ENDPCB` terminator, a
`PCB FILE 9` binary between its header and whatever comes next, a PCB ASCII
document by its `|RECORD=` markers. The database writes its own page headers
through the middle of long documents, which the readers report as salvaged
records rather than losing the board over.

A project holds more than one document, so this module lists them and lets a
caller pick. What it cannot do is open a board stored as `PCB 4.0 Binary File`,
which is what most Protel 99 SE projects contain - see `docs/COMPATIBILITY.md`.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

from . import pcbascii
from .pcb9 import Board

MAGIC = b"\x00\x01\x00\x00Standard Jet DB"

# Documents that can sit inside a design database, by the string each one
# announces itself with. The flag marks a layout rather than a schematic or a
# library, because what the database holds decides what there is to say when
# nothing comes out of it.
CONTENTS = {
    b"\x17\xa1PCB FILE 9 VERSION": ("PCB FILE 9 binary board", True),
    b"PCB FILE 6 VERSION": ("PCB ASCII board", True),
    b"PCB FILE 5": ("Easytrax board", True),
    b"PCB FILE 4": ("Autotrax board", True),
    b"PCB 4.0 Binary Library File": ("PCB 4.0 binary footprint library", False),
    b"PCB 4.0 Binary File": ("PCB 4.0 binary board", True),
    b"PCB 3.0 Binary File": ("PCB 3.0 binary board", True),
    b"Protel for Windows - Schematic": ("schematic", False),
    b"PROTEL PCBLIB": ("footprint library", False),
}

# Markers whose documents this package has a reader for.
READABLE = {b"\x17\xa1PCB FILE 9 VERSION", b"PCB FILE 6 VERSION",
            b"PCB FILE 5", b"PCB FILE 4"}

# Documents this package reads that end with a terminator rather than running
# to whatever follows them.
TERMINATED = {b"PCB FILE 6 VERSION", b"PCB FILE 5", b"PCB FILE 4"}

# Filenames the database mentions. Protel keeps a document table in plain text,
# so the names are readable even though the table structure is not.
_NAME = re.compile(rb"[A-Za-z0-9_\-. ]{1,60}\.(?:pcb|sch|lib|net|prj|drc|ddb)",
                   re.IGNORECASE)


class ParseError(Exception):
    pass


@dataclass
class Document:
    """One Protel document found inside the database."""
    index: int
    label: str
    marker: bytes
    start: int
    end: int
    readable: bool

    @property
    def size(self) -> int:
        return self.end - self.start


def documents(data: bytes) -> list[Document]:
    """Every Protel document in the file, in the order they are stored.

    A document runs from its header string to whichever comes first: its own
    terminator, the next document's header, or the end of the file. That is
    enough for the readers, which stop when their own content stops.
    """
    hits: list[tuple[int, bytes]] = []
    for marker in CONTENTS:
        at = data.find(marker)
        while at >= 0:
            hits.append((at, marker))
            at = data.find(marker, at + 1)
    hits.sort()

    # A longer marker containing a shorter one would be found twice.
    kept: list[tuple[int, bytes]] = []
    for at, marker in hits:
        if kept and at < kept[-1][0] + len(kept[-1][1]):
            continue
        kept.append((at, marker))

    out = []
    for i, (at, marker) in enumerate(kept):
        nxt = kept[i + 1][0] if i + 1 < len(kept) else len(data)
        end = nxt
        if marker in TERMINATED:
            stop = data.find(b"ENDPCB", at, nxt)
            if stop >= 0:
                end = stop + len(b"ENDPCB")
        label, _is_board = CONTENTS[marker]
        out.append(Document(len(out), label, marker, at, end,
                            marker in READABLE))
    return out


def names(data: bytes) -> list[str]:
    """Document filenames the database mentions, deduplicated.

    These come from Protel's own document table and are shown so a caller can
    see what the project is called. They are **not** paired with the documents
    above: pairing a name to a stored blob needs the Jet table structure, which
    is not decoded here, and guessing the pairing would put a confident wrong
    name on a converted board.
    """
    seen = []
    for m in _NAME.finditer(data):
        name = m.group().decode("latin-1").strip()
        if name and name not in seen:
            seen.append(name)
    return seen


def inventory(path: Path) -> dict:
    """What the database contains, counted by document type."""
    found: dict[str, int] = {}
    for doc in documents(Path(path).read_bytes()):
        found[doc.label] = found.get(doc.label, 0) + 1
    return found


def boards(path: Path) -> list[Board]:
    """Every board in the database this package can read, in storage order."""
    path = Path(path)
    data = _jet_bytes(path)
    out = []
    for doc in documents(data):
        if not doc.readable:
            continue
        try:
            out.append(_read(path, data, doc))
        except Exception:                   # noqa: BLE001 - one bad document
            continue                        # must not lose the others
    ascii_board = _read_ascii(path, data)
    if ascii_board is not None:
        out.append(ascii_board)
    return out


def parse(path: Path, which: str | int | None = None) -> Board:
    """One board out of the database.

    `which` picks a document: an index as listed by `documents`, or a piece of
    a format name such as `pcb6`. With nothing given, the largest readable
    board wins - in a project holding one layout and a pile of libraries, that
    is the layout.

    Raises `ParseError` describing what the database does hold when there is no
    board in it this package can read, because "unrecognised file" would be
    wrong twice over: the file is recognised, and so is what is in it.
    """
    path = Path(path)
    data = _jet_bytes(path)
    docs = [d for d in documents(data) if d.readable]

    if which is not None:
        chosen = _pick(docs, which)
        return _read(path, data, chosen)

    if docs:
        best = max(docs, key=lambda d: d.size)
        return _read(path, data, best)

    board = _read_ascii(path, data)
    if board is not None:
        return board
    raise ParseError(_why_nothing(data))


def _pick(docs: list[Document], which: str | int) -> Document:
    if isinstance(which, int) or str(which).isdigit():
        index = int(which)
        for d in docs:
            if d.index == index:
                return d
        raise ParseError(f"no readable document with index {index}")
    needle = str(which).lower()
    for d in docs:
        if needle in d.label.lower():
            return d
    raise ParseError(f"no readable document matching {which!r}")


def _jet_bytes(path: Path) -> bytes:
    data = path.read_bytes()
    if not data.startswith(MAGIC):
        raise ParseError(f"{path.name}: not a Jet design database")
    return data


def _read(path: Path, data: bytes, doc: Document) -> Board:
    """Run the right reader over one document's bytes."""
    from . import pcb4, pcb6, pcb9        # local: avoids an import cycle

    readers = {
        b"\x17\xa1PCB FILE 9 VERSION": pcb9.parse,
        b"PCB FILE 6 VERSION": pcb6.parse,
        b"PCB FILE 5": pcb4.parse,
        b"PCB FILE 4": pcb4.parse,
    }
    chunk = data[doc.start:doc.end]
    if doc.marker in TERMINATED:
        chunk += b"\r\n"
    board = readers[doc.marker](_Document(path, chunk))
    board.version = f"{board.version} in .ddb"
    return board


def _read_ascii(path: Path, data: bytes) -> Board | None:
    """A board saved as PCB ASCII, which has no header of its own to find.

    Its records are scattered wherever the database put them, so unlike the
    other documents it is read from the whole file rather than from a slice.
    An export with no objects in it is not a board: saving an empty document as
    PCB ASCII still writes the option records, and returning that would look
    like a successful conversion of an empty design.
    """
    if b"|RECORD=" not in data:
        return None
    board = pcbascii.parse(path)
    objects = (len(board.components) + len(board.tracks) + len(board.pads)
               + len(board.arcs) + len(board.vias) + len(board.fills))
    if objects == 0:
        return None
    starts = len(re.findall(rb"\|RECORD=Component\|ID=0\|", data))
    if starts > 1:
        board.errors.append(
            (0, f"{starts} PCB ASCII documents in this database were read as "
                f"one board"))
    board.version = f"{board.version} in .ddb"
    return board


def _why_nothing(data: bytes) -> str:
    """The reason there is no board, which is not the same reason every time.

    The order of these four matters. A database can hold boards in a format
    that is not decoded **and** a scrap of PCB ASCII text carrying nothing but
    the board options; blaming the scrap then hides the real answer, which is
    that the boards are right there and this package cannot read them yet.
    Undecoded boards are therefore reported first, by name and by count, then
    boards a reader claimed and could not deliver, then the scrap, then the
    database that holds no board at all.
    """
    found = {}
    undecoded = {}
    refused = {}
    for doc in documents(data):
        found[doc.label] = found.get(doc.label, 0) + 1
        if not CONTENTS[doc.marker][1]:
            continue
        bucket = refused if doc.readable else undecoded
        bucket[doc.label] = bucket.get(doc.label, 0) + 1
    contents = _describe(found)

    if undecoded:
        also = (" There is PCB ASCII text in here too, but it carries only the "
                "board options." if b"|RECORD=" in data else "")
        return (f"the {_describe(undecoded)} inside cannot be read yet - that "
                f"format is not decoded. Contents: {contents}.{also} Open the "
                f"project in Protel and save the board as PCB ASCII, or see "
                f"docs/COMPATIBILITY.md")
    if refused:
        return (f"the {_describe(refused)} inside is in a format this package "
                f"reads, but no board came out of it - the document is damaged "
                f"or is stored in a way not seen before. Contents: {contents}")
    if b"|RECORD=" in data:
        return (f"the PCB ASCII text inside holds only board options, no "
                f"objects. Contents: {contents}")
    if found:
        return (f"no board document inside - this database holds {contents}. "
                f"This package converts boards; schematics and libraries are "
                f"out of scope")
    # Nothing in the file announces itself as a Protel document. Saying which
    # documents it does hold would need the Jet table structure, which is not
    # decoded here; the filenames lying in the container run together with the
    # text around them, so printing them would put a confident, mangled name in
    # front of the caller. `--list` shows them with that caveat attached.
    return ("no Protel document inside - nothing in this database announces "
            "itself as a Protel board, schematic or library. Altium project "
            "databases look like this; KiCad imports Altium .PcbDoc directly, "
            "see docs/COMPATIBILITY.md. Run --list to see the raw document "
            "names the container mentions")


class _Document:
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


def listing(path: Path) -> str:
    """What is in the database, as a caller would want to read it."""
    path = Path(path)
    data = _jet_bytes(path)
    docs = documents(data)
    lines = [f"{path.name}: {len(docs)} documents"]
    for d in docs:
        mark = "reads" if d.readable else "-"
        lines.append(f"  [{d.index:2d}] {d.label:<34} {d.size:>9} bytes  {mark}")
    if b"|RECORD=" in data:
        lines.append("  [ *] Protel PCB ASCII text                        reads")
    found = names(data)
    if found:
        lines.append("  document names in the database, order not matched to "
                     "the list above:")
        lines.append("    " + ", ".join(found[:20]))
    return "\n".join(lines)


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        p = Path(arg)
        print(listing(p))
        for b in boards(p):
            counts = pcbascii.parsed_counts(b)
            print(f"  -> {b.version}: "
                  + "  ".join(f"{k} {v}" for k, v in counts.items()))
