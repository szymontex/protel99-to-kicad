#!/usr/bin/env python3
"""Reader for `PCB FILE 4` (Autotrax) and `PCB FILE 5` (Easytrax) boards.

These two are the only Protel board formats the vendor published a
specification for, and they are the oldest. The grammar is a tag on its own
line followed by one or two operand lines, which is the same shape the later
`PCB FILE 6` ASCII export uses - `PCB FILE 6` is this format grown extra
fields. Records:

    PCB FILE 4
    COMP                                 designator, pattern, comment,
    SW2                                  then comment data, designator data,
    RADO.2                               then x y and three status flags
    PB RESET
     327 1507 60 3 10 7
     407 1507 60 3 10 7
    225 1425 1 1 2
    CP                                   pad: x y xsize ysize shape hole
    225 1425 62 70 1 30 2 13                  pwr/gnd layer, then its name
    1
    ENDCOMP
    FT                                   track: x1 y1 x2 y2 width layer
    925 1475 1175 1225 12 1 1                 user-routed
    FV                                   via: x y diameter hole
    175 4150 50 28
    ENDPCB

`C*` objects belong to the `COMP` most recently opened, `F*` stand alone, and
the six types are `A` arc, `F` fill, `P` pad, `S` string, `T` track, `V` via.

Two differences from the specification show up in real files and both are
handled: tracks are sometimes written without the trailing `user-routed` flag
(6 fields rather than 7, in 13406 of 101142 tracks across the sample set), and
vias sometimes carry one field more than the four documented.

Easytrax differs from Autotrax only in not assigning hole sizes to pads, which
it writes as a zero in the same field, so one reader serves both.

Units are whole mils. Arcs carry a quadrant bitmask rather than angles.

Layer numbers are translated to the later Protel numbering the rest of this
package uses, so the KiCad writer needs no idea which generation it was handed.
"""
from __future__ import annotations

import sys
from pathlib import Path

from .pcb9 import (
    Arc, Board, Component, Fill, Net, Pad, Text, Track, Via,
)

SCALE = 1.0             # stored units per mil - this format counts whole mils


class ParseError(Exception):
    pass


# Autotrax layer -> the numbering used by `PCB FILE 9` and by the KiCad writer.
#
# The two plane layers are the awkward pair. Autotrax puts them after the mid
# layers in the stack, so they land on the inner copper that follows: a board
# using a ground plane comes out with the plane between its mids and its
# bottom, which is where Autotrax put it. Four objects in four files across the
# sample set use them, so the choice decides very little.
LAYER_MAP = {
    1: 1,       # top
    2: 2, 3: 3, 4: 4, 5: 5,     # mid 1..4
    9: 6,       # ground plane -> inner copper
    10: 7,      # power plane  -> inner copper
    6: 16,      # bottom
    7: 17,      # top overlay (silkscreen)
    8: 18,      # bottom overlay
    11: 29,     # board layer     -> Edge.Cuts when --outline-layer says so
    12: 28,     # keep out        -> the outline in practice, see OUTLINE_LAYER
    13: 34,     # multi layer
}

# Where the board outline actually is. The specification names layer 11 "Board
# Layer", but measured across 95 sample boards layer 11 appears in 3 files and
# encloses all copper in 2 of them, while layer 12 (Keep Out) appears in 10 and
# encloses all copper in 7. Keep Out is what Autotrax boards are drawn with,
# the same as the later `PCB FILE 6` boards. `--outline-layer 29` selects the
# Board layer instead.
OUTLINE_LAYER = 28

# Autotrax pad shape -> the shape codes the KiCad writer understands.
# 1 circular and 2 rectangular map straight across; 3 octagonal is the writer's
# chamfered rectangle. Rounded rectangle, cross hair and moire targets have no
# equivalent there: the first becomes a rectangle, the two targets become
# circles, which is what they plot as.
SHAPE_MAP = {1: 1, 2: 2, 3: 3, 4: 2, 5: 1, 6: 1}


def _layer(v: str) -> int:
    """Translate a layer number, passing anything unmapped through unchanged."""
    return LAYER_MAP.get(int(v), int(v))


def _n(tok: str) -> float:
    return float(tok) / SCALE


def quadrant_angles(mask: int) -> tuple[float, float]:
    """Autotrax arc quadrant bitmask -> (start, end) in degrees, Y up.

    Bit 0 is the upper right quadrant, then counter-clockwise: upper left,
    lower left, lower right. A complete circle is 15. The mask can describe a
    shape that is not one arc at all - opposite quadrants only - and there is
    no way to draw that as a single arc, so the widest run wins and the rest is
    lost. That case does not occur in any sample measured.
    """
    if mask & 15 == 15 or mask & 15 == 0:
        return 0.0, 360.0
    quads = [bool(mask & (1 << i)) for i in range(4)]
    # Find the longest contiguous run, allowing it to wrap past quadrant 3.
    best_start, best_len = 0, 0
    for start in range(4):
        if not quads[start] or quads[(start - 1) % 4]:
            continue                     # not the first quadrant of a run
        length = 0
        while quads[(start + length) % 4] and length < 4:
            length += 1
        if length > best_len:
            best_start, best_len = start, length
    return best_start * 90.0, (best_start + best_len) * 90.0


class Reader:
    def __init__(self, text: str):
        self.lines = text.replace("\r\n", "\n").split("\n")
        self.i = 0

    def line(self) -> str:
        if self.i >= len(self.lines):
            raise ParseError(f"file ends at line {self.i}")
        v = self.lines[self.i]
        self.i += 1
        return v

    def nums(self) -> list:
        return self.line().split()


TAGS = {"COMP", "ENDCOMP", "CT", "FT", "CP", "FP", "CS", "FS",
        "CF", "FF", "CA", "FA", "CV", "FV", "NETDEF", "ENDPCB"}


def parse(path: Path) -> Board:
    raw = path.read_bytes()
    r = Reader(raw.decode("latin-1"))

    version = r.line().strip()
    if not (version.startswith("PCB FILE 4") or version.startswith("PCB FILE 5")):
        raise ParseError(f"unsupported header {version!r}")
    board = Board(path, version, {})

    current = None

    def resync(at: int, exc: Exception) -> None:
        board.errors.append((at, f"{type(exc).__name__}: {exc}"))
        while r.i < len(r.lines) and r.lines[r.i].strip() not in TAGS:
            r.i += 1

    def sink(kind: str):
        if kind[0] == "C" and current is not None:
            return getattr(current, {"CT": "tracks", "CP": "pads", "CS": "texts",
                                     "CF": "fills", "CA": "arcs", "CV": "vias"}[kind])
        return {"FT": board.tracks, "FP": board.pads, "FS": board.texts,
                "FF": board.fills, "FA": board.arcs, "FV": board.vias,
                "CT": board.tracks, "CP": board.pads, "CS": board.texts,
                "CF": board.fills, "CA": board.arcs, "CV": board.vias}[kind]

    def string(at: int, f: list, body: str) -> Text:
        """x y height rotation linewidth layer, then the text on its own line.

        Rotation is a code, not degrees: 0..3 are quarter turns and 16..19 the
        same turns mirrored on X. The bounding box is estimated from the stroke
        font because this format does not store one, and an all-zero box would
        be read as a real corner at the origin.
        """
        tx, ty, th = _n(f[0]), _n(f[1]), _n(f[2])
        code = int(f[3])
        rot = (code % 16) * 90.0
        mirror = 1 if code >= 16 else 0
        tw = 0.6 * th * len(body)
        if rot in (90.0, 270.0):
            box = (tx - th, ty, tx, ty + tw)
        else:
            box = (tx, ty, tx + tw, ty + th)
        return Text(offset=at, x=tx, y=ty, rotation=rot, bbox=box, text=body,
                    layer=_layer(f[5]), height=th, stroke=_n(f[4]),
                    mirror=mirror, f15=0, f1d=0)

    while r.i < len(r.lines):
        at = r.i
        tag = r.line().strip()
        if not tag:
            continue
        if tag == "ENDPCB":
            board.end_offset = at
            break

        try:
            if tag == "ENDCOMP":
                current = None

            elif tag == "COMP":
                designator = r.line().strip()
                pattern = r.line().strip()
                comment = r.line().strip()
                cdata = r.nums()          # comment text placement
                ddata = r.nums()          # designator text placement
                f = r.nums()              # xref yref and three status flags
                current = Component(
                    offset=at, footprint=pattern, x=_n(f[0]), y=_n(f[1]),
                    bbox=(0.0, 0.0, 0.0, 0.0), mirror=0, fields=tuple(f),
                    rotation=0.0,
                    designator=string(at, ddata, designator),
                    comment=string(at, cdata, comment))
                board.components.append(current)

            elif tag in ("CT", "FT"):
                f = r.nums()
                # The trailing user-routed flag is absent in older files.
                sink(tag).append(Track(
                    offset=at, kind="n", x1=_n(f[0]), y1=_n(f[1]),
                    x2=_n(f[2]), y2=_n(f[3]), width=_n(f[4]), layer=_layer(f[5]),
                    flag=int(f[6]) if len(f) > 6 else 0, tail=tuple(f[6:])))

            elif tag in ("CP", "FP"):
                f = r.nums()
                name = r.line().strip()
                size = (_n(f[2]), _n(f[3]), SHAPE_MAP.get(int(f[4]), 2))
                # One size serves every copper layer the pad reaches - this
                # format has no per-side padstack.
                sink(tag).append(Pad(
                    offset=at, x=_n(f[0]), y=_n(f[1]), name=name,
                    layer=_layer(f[7]), hole=_n(f[5]),
                    top=size, mid=size, bot=size,
                    rotation=0.0, raw_tail=tuple(f[6:7])))

            elif tag in ("CS", "FS"):
                f = r.nums()
                body = r.line()
                sink(tag).append(string(at, f, body))

            elif tag in ("CF", "FF"):
                f = r.nums()
                sink(tag).append(Fill(
                    offset=at, x1=_n(f[0]), y1=_n(f[1]),
                    x2=_n(f[2]), y2=_n(f[3]), layer=_layer(f[4])))

            elif tag in ("CA", "FA"):
                f = r.nums()
                start, end = quadrant_angles(int(f[3]))
                sink(tag).append(Arc(
                    offset=at, x=_n(f[0]), y=_n(f[1]), radius=_n(f[2]),
                    start=start, end=end, width=_n(f[4]), layer=_layer(f[5]),
                    tail=int(f[3])))

            elif tag in ("CV", "FV"):
                f = r.nums()
                sink(tag).append(Via(
                    offset=at, x=_n(f[0]), y=_n(f[1]), diameter=_n(f[2]),
                    hole=_n(f[3]), flag=0, raw_tail=tuple(f[4:])))

            elif tag == "NETDEF":
                name = r.line().strip()
                show = r.line().strip()
                members, connections = [], []
                for opener, closer, into in (("(", ")", members),
                                             ("{", "}", connections)):
                    if r.i >= len(r.lines) or r.lines[r.i].strip() != opener:
                        continue
                    r.line()
                    while r.i < len(r.lines) and r.lines[r.i].strip() != closer:
                        into.append(r.line().strip())
                    r.line()
                board.nets.append(Net(
                    offset=at, index=len(board.nets), name=name,
                    track_width=0.0, via_size=0.0,
                    f1=int(show) if show.isdigit() else 0, f2=0,
                    members=members, connections=connections, tail=()))

            else:
                board.unknown_tags[tag] = board.unknown_tags.get(tag, 0) + 1
        except (ValueError, IndexError, ParseError) as exc:
            resync(at, exc)

    # Net membership. Unlike the later formats there is no net index on each
    # object; the net lists its nodes as `DESIGNATOR-PADNAME` instead. Resolving
    # those to the objects is what gives KiCad a netlist, and it is done here
    # rather than in the writer so that every reader hands over the same thing.
    _resolve_nets(board)
    return board


def _resolve_nets(board: Board) -> None:
    """Tag pads with their net index, from the `DESIGNATOR-PADNAME` node lists.

    Tracks and vias carry no net of their own in this format. Leaving them at
    net 0 is honest: KiCad then shows the copper as unconnected rather than
    claiming a connectivity that was inferred from geometry.
    """
    by_designator: dict[str, Component] = {}
    for c in board.components:
        if c.designator is not None and c.designator.text:
            by_designator[c.designator.text.upper()] = c

    for net in board.nets:
        index = net.index + 1                  # net 0 means no net
        for node in net.members:
            designator, _, pad_name = node.rpartition("-")
            comp = by_designator.get(designator.upper())
            if comp is None:
                continue
            for pad in comp.pads:
                if pad.name == pad_name:
                    pad.net = index


def parsed_counts(b: Board) -> dict:
    """What came out of the stream. This format carries no header counts."""
    return {
        "components": len(b.components),
        "tracks": len(b.tracks) + sum(len(c.tracks) for c in b.components),
        "pads": len(b.pads) + sum(len(c.pads) for c in b.components),
        "texts": (len(b.texts) + sum(len(c.texts) for c in b.components)
                  + sum(bool(c.designator) + bool(c.comment) for c in b.components)),
        "fills": len(b.fills) + sum(len(c.fills) for c in b.components),
        "arcs": len(b.arcs) + sum(len(c.arcs) for c in b.components),
        "vias": len(b.vias) + sum(len(c.vias) for c in b.components),
        "nets": len(b.nets),
    }


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        board = parse(Path(arg))
        print(Path(arg).name, board.version)
        print("  " + "  ".join(f"{k} {v}" for k, v in parsed_counts(board).items()))
        if board.errors:
            print(f"  SALVAGE: {len(board.errors)} records unreadable, "
                  f"first at line {board.errors[0][0]}")
