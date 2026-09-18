#!/usr/bin/env python3
"""Reader for Protel PCB ASCII, the `|RECORD=...|` interchange format.

This is what Protel 98, Protel 99 and Protel 99 SE write for **File > Save As
> PCB ASCII**, and the vendor documented it in full - `Protel 99 SE PCB ASCII
File Format Reference`, 2000. It matters because the binary those products
write, `PCB 4.0 Binary File`, is not decoded here: a board nothing else in this
package can open becomes readable the moment its owner re-saves it as text.

Every record is one line of `KEY=VALUE` pairs separated by `|`:

    |RECORD=Track|NET=26|SELECTION=FALSE|LAYER=TOP|LOCKED=FALSE|X1=9846.77mil
    |Y1=7508.22mil|X2=9950.11mil|Y2=7508.22mil|WIDTH=10mil|SUBPOLYINDEX=0

so the file names its own fields and nothing here depends on field order. Two
value spellings occur and both are read: `10mil` in files written by the
editor, and ` 1.00000000000000E+0001mil` in the vendor's own examples.

Objects say which component and which net they belong to with `COMPONENT=` and
`NET=`, both indices into the `Component` and `Net` records. Layers are named
rather than numbered, which removes the guesswork the binary formats need.

Three vintages share this grammar, and the header tells them apart:
`KIND=Protel_Advanced_PCB|VERSION=3.00` for ASCII 3.0 and 4.0. This reader does
not branch on it - every field it uses is present in all of them.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from .pcb9 import (
    Arc, Board, Component, Fill, Net, Pad, Text, Track, Via,
)

MM_TO_MIL = 1000.0 / 25.4


class ParseError(Exception):
    pass


# Protel layer names -> the numbering this package uses everywhere else. The
# numbering is Protel 99 SE's own, so most of this is identity with the names
# spelled out; the value of doing it by name is that a file which uses a layer
# outside the set says so instead of landing on a plausible wrong number.
LAYERS = {
    "TOP": 1, "BOTTOM": 16,
    "TOPOVERLAY": 17, "BOTTOMOVERLAY": 18,
    "TOPPASTE": 19, "BOTTOMPASTE": 20,
    "TOPSOLDER": 21, "BOTTOMSOLDER": 22,
    "DRILLGUIDE": 27, "KEEPOUT": 28,
    "DRILLDRAWING": 33, "MULTILAYER": 34,
}
for _i in range(1, 15):
    LAYERS[f"MID{_i}"] = LAYERS[f"MIDLAYER{_i}"] = 1 + _i
for _i in range(1, 5):
    LAYERS[f"PLANE{_i}"] = LAYERS[f"INTERNALPLANE{_i}"] = 22 + _i
    LAYERS[f"MECHANICAL{_i}"] = 28 + _i
UNKNOWN_LAYER = 0        # the writer maps anything it does not know to a note layer

SHAPES = {"ROUND": 1, "RECTANGLE": 2, "OCTAGONAL": 3, "ROUNDRECT": 2,
          "ROUNDEDRECTANGLE": 2}

_VALUE = re.compile(r"^\s*(-?[\d.]+(?:[eE][+-]?\d+)?)\s*(mil|mm)?\s*$")


def dim(value: str | None, default: float = 0.0) -> float:
    """A length in mils, from either `10mil` or ` 1.0000E+0001mil`."""
    if value is None:
        return default
    m = _VALUE.match(value)
    if not m:
        return default
    v = float(m.group(1))
    return v * MM_TO_MIL if m.group(2) == "mm" else v


def num(value: str | None, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def flag(value: str | None) -> bool:
    return (value or "").upper() == "TRUE"


def layer_of(record: dict) -> int:
    name = (record.get("LAYER") or "").upper().replace(" ", "")
    return LAYERS.get(name, UNKNOWN_LAYER)


def records(text: str):
    """Yield one dict per record.

    A record occupies one line, but the same text also turns up inside a `.ddb`
    where the database interleaves its own page headers, so a record can be cut
    in half. Splitting on the record marker rather than on newlines keeps those
    readable: whatever fragment of a header landed in the middle becomes one
    unparsable `KEY=VALUE` pair and the rest of the fields survive.
    """
    for chunk in text.split("|RECORD=")[1:]:
        chunk = chunk.split("\n")[0]
        fields = {}
        kind, _, rest = chunk.partition("|")
        fields["RECORD"] = kind.strip()
        for pair in rest.split("|"):
            key, sep, value = pair.partition("=")
            if sep:
                fields[key.strip().upper()] = value
        yield fields


def parse(path: Path) -> Board:
    text = path.read_bytes().decode("latin-1")
    if "|RECORD=" not in text:
        raise ParseError("no |RECORD= markers - not a Protel PCB ASCII file")

    version = "Protel PCB ASCII"
    head = text[:4096]
    m = re.search(r"KIND=([^|]+)\|VERSION=([^|\r\n]+)", head)
    if m:
        version = f"Protel PCB ASCII {m.group(1).strip()} {m.group(2).strip()}"
    board = Board(path, version, {})

    comps: dict[int, Component] = {}
    free = {"Arc": board.arcs, "Pad": board.pads, "Via": board.vias,
            "Track": board.tracks, "Text": board.texts, "Fill": board.fills}
    owned = {"Arc": "arcs", "Pad": "pads", "Via": "vias",
             "Track": "tracks", "Text": "texts", "Fill": "fills"}

    def place(kind: str, rec: dict, obj) -> None:
        """Into its component if it names one, otherwise onto the board."""
        ref = rec.get("COMPONENT")
        if ref is not None and ref.strip().isdigit():
            comp = comps.get(int(ref))
            if comp is not None:
                getattr(comp, owned[kind]).append(obj)
                return
        free[kind].append(obj)

    for i, rec in enumerate(records(text)):
        kind = rec["RECORD"]
        try:
            if kind == "Net":
                board.nets.append(Net(
                    offset=i, index=len(board.nets), name=rec.get("NAME", ""),
                    track_width=0.0, via_size=0.0, f1=0, f2=0,
                    members=[], connections=[], tail=()))

            elif kind == "Component":
                ident = int(rec.get("ID", len(comps)))
                c = Component(
                    offset=i, footprint=rec.get("PATTERN", ""),
                    x=dim(rec.get("X")), y=dim(rec.get("Y")),
                    bbox=(0.0, 0.0, 0.0, 0.0),
                    mirror=1 if (rec.get("LAYER") or "").upper() == "BOTTOM" else 0,
                    fields=(), rotation=num(rec.get("ROTATION")),
                    designator=None, comment=None)
                comps[ident] = c
                board.components.append(c)

            elif kind == "Track":
                place(kind, rec, Track(
                    offset=i, kind="n",
                    x1=dim(rec.get("X1")), y1=dim(rec.get("Y1")),
                    x2=dim(rec.get("X2")), y2=dim(rec.get("Y2")),
                    width=dim(rec.get("WIDTH")), layer=layer_of(rec),
                    flag=0, tail=(), net=net_of(rec)))

            elif kind == "Arc":
                place(kind, rec, Arc(
                    offset=i, x=dim(rec.get("LOCATION.X")),
                    y=dim(rec.get("LOCATION.Y")), radius=dim(rec.get("RADIUS")),
                    start=num(rec.get("STARTANGLE")), end=num(rec.get("ENDANGLE"), 360.0),
                    width=dim(rec.get("WIDTH")), layer=layer_of(rec), tail=0,
                    net=net_of(rec)))

            elif kind == "Pad":
                size = (dim(rec.get("XSIZE")), dim(rec.get("YSIZE")),
                        SHAPES.get((rec.get("SHAPE") or "").upper(), 2))
                place(kind, rec, Pad(
                    offset=i, x=dim(rec.get("X")), y=dim(rec.get("Y")),
                    name=rec.get("NAME", ""), layer=layer_of(rec),
                    hole=dim(rec.get("HOLESIZE")),
                    top=size, mid=size, bot=size,
                    rotation=num(rec.get("ROTATION")), raw_tail=(),
                    net=net_of(rec)))

            elif kind == "Via":
                place(kind, rec, Via(
                    offset=i, x=dim(rec.get("X")), y=dim(rec.get("Y")),
                    diameter=dim(rec.get("DIAMETER")), hole=dim(rec.get("HOLESIZE")),
                    flag=0, raw_tail=(), net=net_of(rec)))

            elif kind == "Fill":
                place(kind, rec, Fill(
                    offset=i, x1=dim(rec.get("X1")), y1=dim(rec.get("Y1")),
                    x2=dim(rec.get("X2")), y2=dim(rec.get("Y2")),
                    layer=layer_of(rec), net=net_of(rec)))

            elif kind == "Text":
                place(kind, rec, text_object(i, rec))
        except (ValueError, KeyError) as exc:
            board.errors.append((i, f"{type(exc).__name__}: {exc}"))

    _lift_component_names(board)
    return board


def net_of(rec: dict) -> int:
    """`NET=` is a zero-based index into the net records; objects count from 1."""
    ref = rec.get("NET")
    if ref is None or not ref.strip().lstrip("-").isdigit():
        return 0
    value = int(ref)
    return value + 1 if value >= 0 else 0


def text_object(i: int, rec: dict) -> Text:
    body = rec.get("TEXT", "")
    tx, ty = dim(rec.get("X")), dim(rec.get("Y"))
    th = dim(rec.get("HEIGHT"), 60.0)
    rot = num(rec.get("ROTATION")) % 360.0
    # No bounding box in this format either. An all-zero one would be read as a
    # real corner at the origin, so it is estimated from the stroke font.
    tw = 0.6 * th * len(body)
    if 45.0 <= rot < 135.0 or 225.0 <= rot < 315.0:
        box = (tx - th, ty, tx, ty + tw)
    else:
        box = (tx, ty, tx + tw, ty + th)
    return Text(offset=i, x=tx, y=ty, rotation=rot, bbox=box, text=body,
                layer=layer_of(rec), height=th, stroke=dim(rec.get("WIDTH")),
                mirror=1 if flag(rec.get("MIRROR")) else 0, f15=0, f1d=0)


def _lift_component_names(board: Board) -> None:
    """Give every component a designator and a comment.

    The `Component` record carries no name of its own - only `NAMEON` and
    `COMMENTON` switches - so the two strings arrive as ordinary text objects
    pointing back at the component. The first belongs to the designator and the
    second to the comment, which is the order Protel writes them in; a
    component whose strings are missing gets empty ones rather than `None`, so
    the KiCad writer has something to place.
    """
    for c in board.components:
        if c.texts:
            c.designator = c.texts.pop(0)
        if c.texts:
            c.comment = c.texts.pop(0)
        for attr in ("designator", "comment"):
            if getattr(c, attr) is None:
                setattr(c, attr, Text(
                    offset=c.offset, x=c.x, y=c.y, rotation=0.0,
                    bbox=(c.x, c.y, c.x + 1.0, c.y + 1.0), text="",
                    layer=18 if c.mirror else 17, height=60.0, stroke=10.0,
                    mirror=c.mirror, f15=0, f1d=0))


from .pcb9 import parsed_counts        # noqa: E402,F401


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        board = parse(Path(arg))
        print(Path(arg).name, board.version)
        print("  " + "  ".join(f"{k} {v}" for k, v in parsed_counts(board).items()))
        if board.errors:
            print(f"  {len(board.errors)} records unreadable")
