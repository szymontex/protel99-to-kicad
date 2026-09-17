"""Which Protel format a file is written in, and the reader for it.

Protel shipped several generations of board format under the same `.PCB`
extension and the same vendor primitives. Telling them apart is a header read,
not a guess: each one names itself in its first bytes.

Readers translate their own layer numbering into the one `PCB FILE 9` uses -
1 top, 16 bottom, 17 and 18 silkscreen, 28 keep-out, 29 mechanical 1, 34
multi-layer - so `--outline-layer` and everything downstream speak one
language whatever the file was written in.

Every reader here returns the same `Board` model, so callers - the KiCad
writer, a viewer, a batch job - have one shape to handle and no idea which
generation produced it. Adding a format means adding a reader and a row in
`FORMATS`, not a branch at every call site.

Compatibility is documented in `docs/COMPATIBILITY.md`; this module is the
machine-readable half of the same table.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import ddb, pcb4, pcb6, pcb9, pcbascii


class UnsupportedFormat(Exception):
    """The file is not a Protel board this package can read."""


READER_ERRORS = (ddb.ParseError, pcb4.ParseError, pcb6.ParseError,
                 pcb9.ParseError, pcbascii.ParseError)


@dataclass(frozen=True)
class Format:
    key: str
    label: str            # how the format names itself
    product: str          # the program that wrote it
    magic: bytes
    at_start: bool        # magic must be at offset 0, or anywhere in the head
    reader: object | None  # None = recognised but not decoded
    outline_layer: int = 29   # Protel layer the board outline is drawn on


def _read4(path: Path):
    return pcb4.parse(path)


def _read_ascii(path: Path):
    return pcbascii.parse(path)


def _read_ddb(path: Path):
    return ddb.parse(path)


def _read6(path: Path):
    return pcb6.parse(path)


def _read9(path: Path):
    return pcb9.parse(path)


# Ordered most specific first. `PCB 4.0 Binary Library File` has to be tested
# before `PCB 4.0 Binary File` or the library matches the board's prefix.
FORMATS: tuple = (
    # Outline layer is a property of the generation, not of the board: every
    # PCB FILE 9 file in the archives measured draws it on 29 (Mechanical 1)
    # and every PCB FILE 6 file on 28 (Keep-Out). `--outline-layer` overrides.
    # Product attributions were corrected on 2026-09-17 against material the
    # vendors themselves shipped: `PCB FILE 9 VERSION 2.70` is what the Protel
    # for Windows 2.8 installer of 1995 carries its demo boards in, four years
    # before Protel 99 SE, and no `.ddb` written by 99 SE contains the string
    # at all. `PCB FILE 6` is not a product's native format but Protel's text
    # export, which the vendor reference calls PCB ASCII 2.8.
    Format("pcb9", "PCB FILE 9 VERSION 2.70", "Protel for Windows / Advanced PCB",
           b"PCB FILE 9", False, _read9, outline_layer=29),
    Format("pcb6", "PCB FILE 6 VERSION 2.80", "Protel PCB ASCII 2.8 export",
           b"PCB FILE 6", True, _read6, outline_layer=28),
    Format("pcb5", "PCB FILE 5", "Protel Easytrax",
           b"PCB FILE 5", True, _read4, outline_layer=pcb4.OUTLINE_LAYER),
    Format("pcb4", "PCB FILE 4", "Protel Autotrax",
           b"PCB FILE 4", True, _read4, outline_layer=pcb4.OUTLINE_LAYER),
    Format("pcbascii", "Protel PCB ASCII (|RECORD=)", "Protel 98 / 99 / 99 SE",
           b"|RECORD=", True, _read_ascii, outline_layer=29),
    Format("advpcb_lib", "PCB 4.0 Binary Library File", "Protel 98 / 99 / 99 SE",
           b"\x1bPCB 4.0 Binary Library File", True, None),
    Format("advpcb", "PCB 4.0 Binary File", "Protel 98 / 99 / 99 SE",
           b"\x13PCB 4.0 Binary File", True, None),
    Format("advpcb3_lib", "PCB 3.0 Binary Library File", "Protel Advanced PCB 3",
           b"\x1bPCB 3.0 Binary Library File", True, None),
    Format("advpcb3", "PCB 3.0 Binary File", "Protel Advanced PCB 3",
           b"\x13PCB 3.0 Binary File", True, None),
    Format("dos3", "DOS 3 PCB", "Protel PCB for DOS 3",
           b"DOS 3 PCB", True, None),
    # The container nearly every Protel 99 SE project arrives in. A board
    # saved inside it as PCB ASCII is readable; a board saved as PCB 4.0
    # binary is not, and the reader says which it found.
    Format("ddb", "Protel design database (.ddb)", "Protel 99 / 99 SE",
           b"\x00\x01\x00\x00Standard Jet DB", True, _read_ddb,
           outline_layer=29),
)


@dataclass(frozen=True)
class Foreign:
    """A `.pcb` file written by something that is not Protel."""
    label: str
    magic: bytes
    advice: str
    tail: bytes = b""          # a second byte pattern, at `tail_at`
    tail_at: int = 0

    def matches(self, head: bytes) -> bool:
        if not head.startswith(self.magic):
            return False
        if not self.tail:
            return True
        return head[self.tail_at:self.tail_at + len(self.tail)] == self.tail


# `.pcb` is not a Protel extension - it is four vendors' extension, and a board
# downloaded from a chip maker's evaluation page is more likely to be one of
# these than a Protel file. Saying which one it is turns a dead end into a next
# step. The advice names KiCad 9's own importers, checked against an installed
# 9.0.8: it has P-CAD, gEDA and Altium, and no PADS.
FOREIGN = (
    # PADS binary: 00 FF <version> 20. Analog Devices, Linear Technology and
    # many others ship their evaluation boards in this.
    Foreign("PADS (binary)", b"\x00\xff",
            "KiCad 9 has no PADS importer; the development branch does. PADS "
            "itself exports ASCII, which more tools read",
            tail=b" ", tail_at=3),
    Foreign("PADS (ASCII)", b"!PADS-POWERPCB",
            "KiCad 9 has no PADS importer; the development branch does"),
    Foreign("P-CAD / ACCEL ASCII", b"ACCEL_ASCII",
            "KiCad reads this: File > Import > Non-KiCad Board File"),
    Foreign("gEDA pcb", b"# release: pcb",
            "KiCad reads this: File > Import > Non-KiCad Board File"),
    # pcb-rnd's own format. It writes the gEDA one too, so both turn up.
    Foreign("pcb-rnd (lihata)", b"ha:pcb-rnd-board",
            "pcb-rnd exports the gEDA PCB format, which KiCad reads"),
    Foreign("CIRCAD", b"CIRCAD",
            "no importer known; CIRCAD plots gerbers, which KiCad reads"),
    Foreign("KiCad", b"(kicad_pcb",
            "this is already a KiCad board"),
    # OLE compound document. Altium `.PcbDoc` and OrCAD `.dsn` share it, and
    # both turn up under a `.pcb` name in vendor download packages.
    Foreign("OLE compound document - Altium .PcbDoc or OrCAD .dsn",
            b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",
            "if it is Altium, KiCad reads it: File > Import > Non-KiCad Board "
            "File. See docs/COMPATIBILITY.md"),
)


def identify(path: Path) -> Format | None:
    """Name the format from the file header, or None if nothing matches."""
    head = Path(path).open("rb").read(64)
    for fmt in FORMATS:
        if fmt.at_start:
            if head.startswith(fmt.magic):
                return fmt
        elif fmt.magic in head:
            return fmt
    return None


def identify_foreign(path: Path) -> Foreign | None:
    """Name the tool that wrote a `.pcb` this package has no business reading."""
    head = Path(path).open("rb").read(64)
    for f in FOREIGN:
        if f.matches(head):
            return f
    return None


def parse(path: Path):
    """Read a board in any supported format.

    Returns `(Board, Format)`. Raises `UnsupportedFormat` when the header is
    unknown, or when it is known but the format is not decoded yet - the
    message says which of the two it is, because "we do not read this" and
    "we do not recognise this" call for different next steps.
    """
    path = Path(path)
    fmt = identify(path)
    if fmt is None:
        other = identify_foreign(path)
        if other is not None:
            raise UnsupportedFormat(
                f"{path.name}: this is a {other.label} file, not a Protel "
                f"board - {other.advice}")
        head = path.open("rb").read(40)
        printable = "".join(chr(b) if 32 <= b < 127 else "." for b in head)
        raise UnsupportedFormat(
            f"{path.name}: unrecognised header {printable!r}")
    if fmt.reader is None:
        raise UnsupportedFormat(
            f"{path.name}: {fmt.label} ({fmt.product}) is recognised but not "
            f"decoded - see docs/COMPATIBILITY.md")
    try:
        return fmt.reader(path), fmt
    except READER_ERRORS as exc:
        # A reader raises this only from its header check - everything after it
        # is salvaged rather than thrown. So this is a file of the right family
        # and a vintage the reader will not claim to understand, which is the
        # same answer for the caller as a format with no reader at all.
        raise UnsupportedFormat(f"{path.name}: {exc}") from exc


def readable_suffixes() -> set:
    """Extensions worth opening when walking a directory.

    `.ddb` is here although nothing reads one yet: a batch run over a project
    folder should say what it found and skipped rather than walk past the file
    every Protel 99 SE project is actually delivered in.
    """
    return {".pcb", ".ddb"}
