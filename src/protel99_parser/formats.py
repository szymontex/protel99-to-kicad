"""Which Protel format a file is written in, and the reader for it.

Protel shipped several generations of board format under the same `.PCB`
extension and the same vendor primitives. Telling them apart is a header read,
not a guess: each one names itself in its first bytes.

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

from . import pcb6, pcb9


class UnsupportedFormat(Exception):
    """The file is not a Protel board this package can read."""


@dataclass(frozen=True)
class Format:
    key: str
    label: str            # how the format names itself
    product: str          # the program that wrote it
    magic: bytes
    at_start: bool        # magic must be at offset 0, or anywhere in the head
    reader: object | None  # None = recognised but not decoded
    outline_layer: int = 29   # Protel layer the board outline is drawn on


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
    Format("pcb9", "PCB FILE 9 VERSION 2.70", "Protel 99 SE",
           b"PCB FILE 9", False, _read9, outline_layer=29),
    Format("pcb6", "PCB FILE 6 VERSION 2.80", "Protel for Windows / Protel 3",
           b"PCB FILE 6", True, _read6, outline_layer=28),
    Format("pcb5", "PCB FILE 5", "Protel Autotrax / Easytrax",
           b"PCB FILE 5", True, None),
    Format("pcb4", "PCB FILE 4", "Protel Autotrax / Easytrax",
           b"PCB FILE 4", True, None),
    Format("advpcb_lib", "PCB 4.0 Binary Library File", "Protel Advanced PCB 3.00",
           b"\x1bPCB 4.0 Binary Library File", True, None),
    Format("advpcb", "PCB 4.0 Binary File", "Protel Advanced PCB 3.00",
           b"\x13PCB 4.0 Binary File", True, None),
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
        head = path.open("rb").read(40)
        printable = "".join(chr(b) if 32 <= b < 127 else "." for b in head)
        raise UnsupportedFormat(
            f"{path.name}: unrecognised header {printable!r}")
    if fmt.reader is None:
        raise UnsupportedFormat(
            f"{path.name}: {fmt.label} ({fmt.product}) is recognised but not "
            f"decoded - see docs/COMPATIBILITY.md")
    return fmt.reader(path), fmt


def readable_suffixes() -> set:
    """Extensions worth opening when walking a directory."""
    return {".pcb"}
