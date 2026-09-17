#!/usr/bin/env python3
"""Reader for `PCB FILE 6` boards - Protel's ASCII interchange format.

Twelve boards in the archive were filed as unreadable. Eleven of them are this
format and it is not a binary at all - the files are plain ASCII, zero bytes
outside the 32..126 range plus CR LF, measured across all eleven.

Protel's own reference calls this "PCB ASCII", and version numbers track the
product generation rather than a redesign: 1.10, 2.70 and 2.80 all appear in
the wild and all share one grammar. Two header lines, then records until
`ENDPCB`:

    PCB FILE 6 VERSION 2.80
    0 12 0 3 0 1 2 0 8 4                 object counts
    COMP                                 record tag on its own line
    DIP14                                operand lines, tag decides how many
    0 0 1000000 2000000 1 0 0 ...
    ...
    ENDPCB

Tags come in a component pair and a free pair: `CT`/`FT` track, `CP`/`FP` pad,
`CS`/`FS` text, `CF`/`FF` fill, `CA`/`FA` arc, `FV` via. Objects tagged `C*`
belong to the `COMP` most recently opened; `F*` stand on their own.

Three things change between versions, and all three are read from the file
rather than assumed:

- **Scale.** 2.70 and 2.80 count 1/1000 mil. The scale is not a guess: vias
  measure 51181 and pads 137795, which are 1.3 mm and 3.5 mm to five digits,
  and no other power of ten puts a via near a millimetre. Version 1.10 counts
  whole mils - its boards run 135 to 7900 across, with 25 mil tracks and 65 mil
  pads, which at 1/1000 mil would be a board eight thousandths of an inch wide.
- **Continuation lines.** Tracks, arcs and fills are followed by a second
  operand line in 2.70 and 2.80 and by nothing in 1.10. Rather than key that
  to the version, the reader consumes a continuation only when the next line is
  not itself a record tag.
- **Pad layout.** 2.80 writes a 33-field padstack with a separate size for
  top, middle and bottom. 1.10 writes 12 fields and one size for the whole
  pad. The field count decides which is which.

The output is the `Board` model of the `PCB FILE 9` reader, so the KiCad writer
takes it unchanged.
"""
from __future__ import annotations

import sys
from pathlib import Path

from .pcb9 import (
    Arc, Board, Component, Fill, Net, Pad, Text, Track, Via,
)

SCALE = 1000.0          # stored units per mil, from version 2.70 onwards
SCALE_V1 = 1.0          # version 1.10 counts whole mils


class ParseError(Exception):
    pass


def scale_for(version: str) -> float:
    """Stored units per mil, from the version in the header."""
    return SCALE_V1 if version.startswith("PCB FILE 6 VERSION 1.") else SCALE


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


def parse(path: Path) -> Board:
    raw = path.read_bytes()
    text = raw.decode("latin-1")
    r = Reader(text)

    version = r.line().strip()
    if not version.startswith("PCB FILE 6"):
        raise ParseError(f"unsupported header {version!r}")
    scale = scale_for(version)
    header = [int(v) for v in r.nums()]
    board = Board(path, version, {"header": header})

    def n(tok: str) -> float:
        return float(tok) / scale

    current = None          # open COMP, or None
    comp_texts = 0          # strings seen inside the open component
    tags: dict[str, int] = {}
    # Known tags double as resynchronisation points. Some files in this archive
    # carry bit-flipped bytes inside otherwise sound records - `10000` arrives
    # as `100p0` - which is the same damage the `PCB FILE 9` boards show. A
    # record that will not parse is recorded with its line number and skipped
    # to the next tag, rather than dropped in silence or allowed to
    # desynchronise everything after it.
    TAGS = {"COMP", "ENDCOMP", "CT", "FT", "CP", "FP", "CS", "FS",
            "CF", "FF", "CA", "FA", "FV", "NETDEF", "ENDPCB"}

    def resync(at: int, exc: Exception) -> None:
        board.errors.append((at, f"{type(exc).__name__}: {exc}"))
        while r.i < len(r.lines) and r.lines[r.i].strip() not in TAGS:
            r.i += 1

    def cont() -> None:
        """Swallow the continuation line a record may or may not have.

        Version 1.10 writes none. Keying this to the version would break on the
        first file that splits the difference, so the next line is examined
        instead: a record tag on its own line is the start of the next record
        and belongs to nobody.
        """
        if r.i < len(r.lines) and r.lines[r.i].strip() not in TAGS:
            r.line()

    def sink(kind):
        """Where an object goes: into the open component, or onto the board."""
        if kind[0] == "C" and current is not None:
            return getattr(current, {"CT": "tracks", "CP": "pads", "CS": "texts",
                                     "CF": "fills", "CA": "arcs"}[kind])
        return {"FT": board.tracks, "FP": board.pads, "FS": board.texts,
                "FF": board.fills, "FA": board.arcs, "FV": board.vias,
                "CT": board.tracks, "CP": board.pads, "CS": board.texts,
                "CF": board.fills, "CA": board.arcs}[kind]

    while r.i < len(r.lines):
        at = r.i
        tag = r.line().strip()
        if not tag:
            continue
        if tag == "ENDPCB":
            board.end_offset = at
            break
        tags[tag] = tags.get(tag, 0) + 1

        try:
          if tag == "ENDCOMP":
              current = None

          elif tag == "COMP":
              name = r.line()
              f = r.nums()
              comp_texts = 0
              current = Component(
                  offset=at, footprint=name, x=n(f[2]), y=n(f[3]),
                  bbox=(0.0, 0.0, 0.0, 0.0), mirror=int(f[4]) if len(f) > 4 else 0,
                  fields=tuple(f), rotation=float(f[11]) if len(f) > 11 else 0.0,
                  designator=None, comment=None)
              board.components.append(current)

          elif tag in ("CT", "FT"):
              f = r.nums()
              cont()
              sink(tag).append(Track(
                  offset=at, kind="n", x1=n(f[2]), y1=n(f[3]),
                  x2=n(f[4]), y2=n(f[5]), width=n(f[6]), layer=int(f[7]),
                  flag=0, tail=tuple(f[8:]),
                  net=int(f[9]) if len(f) > 9 else 0))

          elif tag in ("CP", "FP"):
              f = r.nums()
              name = r.line()
              if len(f) >= 19:
                  # Padstack form: a separate size and shape for the top, the
                  # middle and the bottom of the pad.
                  pad = Pad(
                      offset=at, x=n(f[2]), y=n(f[3]), name=name,
                      layer=int(f[15]), hole=n(f[13]),
                      top=(n(f[4]), n(f[5]), int(f[6])),
                      mid=(n(f[7]), n(f[8]), int(f[9])),
                      bot=(n(f[10]), n(f[11]), int(f[12])),
                      rotation=float(f[18]), raw_tail=tuple(f[19:]),
                      net=int(f[16]))
              else:
                  # Short form, version 1.10: one size for the whole pad. The
                  # two trailing fields are zero on every pad measured, so
                  # nothing is read from them - including the net, which this
                  # vintage carries only in the node lists if at all.
                  size = (n(f[4]), n(f[5]), int(f[6]))
                  pad = Pad(
                      offset=at, x=n(f[2]), y=n(f[3]), name=name,
                      layer=int(f[9]), hole=n(f[7]),
                      top=size, mid=size, bot=size,
                      rotation=0.0, raw_tail=tuple(f[10:]), net=0)
              sink(tag).append(pad)

          elif tag in ("CS", "FS"):
              f = r.nums()
              body = r.line()
              # `PCB FILE 9` stores a text's bounding box; this format does
              # not, and the box is what the catalogue measures a board with.
              # An all-zero box is worse than none: it is truthy, so every
              # string would pull the board extent back to the origin and every
              # drawing would come out with a quadrant of empty space. Estimate
              # it from the stroke font instead - glyphs run about 0.6 of their
              # height - and let rotation decide which way it runs.
              tx, ty, th = n(f[2]), n(f[3]), n(f[4])
              tw = 0.6 * th * len(body)
              rot = float(f[5]) % 360.0
              if 45.0 <= rot < 135.0 or 225.0 <= rot < 315.0:
                  box = (tx - th, ty, tx, ty + tw)
              else:
                  box = (tx, ty, tx + tw, ty + th)
              text = Text(
                  offset=at, x=tx, y=ty, rotation=float(f[5]),
                  bbox=box, text=body, layer=int(f[8]),
                  height=th, stroke=n(f[13]) if len(f) > 13 else 0.0,
                  mirror=int(f[6]), f15=int(f[7]), f1d=0)
              # The first two strings inside a component are its designator and
              # its comment, in that order - the same two that `PCB FILE 9`
              # stores as named fields. Everything after them is ordinary
              # silkscreen belonging to the footprint.
              if tag == "CS" and current is not None and comp_texts < 2:
                  # A component string with no position of its own is written
                  # as 0 0 - the comment usually is. Left alone it lands at the
                  # board origin, which draws every comment on the archive in
                  # one heap outside the outline. Anchor it to the component.
                  if text.x == 0.0 and text.y == 0.0:
                      dx, dy = current.x, current.y
                      text.x, text.y = dx, dy
                      text.bbox = (text.bbox[0] + dx, text.bbox[1] + dy,
                                   text.bbox[2] + dx, text.bbox[3] + dy)
                  if comp_texts == 0:
                      current.designator = text
                  else:
                      current.comment = text
                  comp_texts += 1
              else:
                  sink(tag).append(text)

          elif tag in ("CF", "FF"):
              f = r.nums()
              cont()
              sink(tag).append(Fill(
                  offset=at, x1=n(f[2]), y1=n(f[3]),
                  x2=n(f[4]), y2=n(f[5]), layer=int(f[6])))

          elif tag in ("CA", "FA"):
              f = r.nums()
              cont()
              sink(tag).append(Arc(
                  offset=at, x=n(f[2]), y=n(f[3]), radius=n(f[4]),
                  start=float(f[5]), end=float(f[6]), width=n(f[7]),
                  layer=int(f[8]), tail=0))

          elif tag == "FV":
              f = r.nums()
              board.vias.append(Via(
                  offset=at, x=n(f[2]), y=n(f[3]), diameter=n(f[4]),
                  hole=n(f[5]), flag=0, raw_tail=tuple(f[6:]),
                  net=int(f[8]) if len(f) > 8 else 0))

          elif tag == "NETDEF":
              name = r.line()
              f = r.nums()
              # Two bracketed lists follow, then two number lines. Both lists are
              # empty on every board here - membership is carried the other way
              # round, by the net index on each pad and track, exactly as in
              # `PCB FILE 9`. The brackets are read generically anyway, because
              # an empty list is a fact about this archive, not about the format.
              members, connections = [], []
              for opener, closer, into in (("(", ")", members),
                                           ("{", "}", connections)):
                  if r.lines[r.i].strip() != opener:
                      continue
                  r.line()
                  while r.lines[r.i].strip() != closer:
                      into.append(r.line())
                  r.line()
              tail = tuple(r.nums()) + tuple(r.nums())
              board.nets.append(Net(
                  offset=at, index=len(board.nets), name=name,
                  track_width=n(f[0]), via_size=n(f[1]),
                  f1=int(f[2]), f2=int(f[3]),
                  members=members, connections=connections, tail=tail))

        # Anything else is an operand line that follows a tag this reader does
        # not know. Counting it as an unknown tag is honest; guessing its
        # length would desynchronise everything after it.
          else:
              board.unknown_tags[tag] = board.unknown_tags.get(tag, 0) + 1
        except (ValueError, IndexError, ParseError) as exc:
            resync(at, exc)

    # `Board.counts` is what the file claims, never what this reader produced.
    # The catalogue checks one against the other, and a reader that fills both
    # sides agrees with itself on every board - that mistake has already been
    # made once here and it made a broken check look green.
    #
    # Positions in the header line, read off eleven boards: every one of these
    # eight matched the parsed total on nine files, and on the other two the
    # difference was one text and three records lost to byte damage. Positions
    # 0 and 2 stay unnamed because nothing measured explains them.
    board.counts = dict(zip(
        ("_0", "tracks", "_2", "nets", "arcs", "vias",
         "components", "fills", "pads", "texts"), header))
    board.counts["header"] = header
    return board


def parsed_counts(b: Board) -> dict:
    """Count what came out of the stream, in the header's own categories.

    The header states eight totals. Comparing them against what was parsed is
    the only check available for this format: there is no ASCII export and no
    gerber set for these boards in any archive seen so far.

    One subtlety decides whether the check is meaningful. The header counts
    every string in the file, and two strings per component are its designator
    and comment, which this reader lifts into named fields rather than leaving
    on the text list. Counting only the list makes every board disagree with
    its own header.
    """
    return {
        "components": len(b.components),
        "tracks": len(b.tracks) + sum(len(c.tracks) for c in b.components),
        "pads": len(b.pads) + sum(len(c.pads) for c in b.components),
        "texts": (len(b.texts) + sum(len(c.texts) for c in b.components)
                  + sum(bool(c.designator) + bool(c.comment)
                        for c in b.components)),
        "fills": len(b.fills) + sum(len(c.fills) for c in b.components),
        "arcs": len(b.arcs) + sum(len(c.arcs) for c in b.components),
        "vias": len(b.vias),
        "nets": len(b.nets),
    }


def header_disagreements(b: Board) -> dict:
    """Counters where the header and the stream differ: {key: (header, parsed)}."""
    got = parsed_counts(b)
    return {k: (b.counts.get(k), v) for k, v in got.items()
            if b.counts.get(k) != v}


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        board = parse(Path(arg))
        print(Path(arg).name)
        print("  " + "  ".join(f"{k} {v}" for k, v in parsed_counts(board).items()))
        bad = header_disagreements(board)
        print("  header agrees" if not bad else
              "  MISMATCH: " + ", ".join(
                  f"{k} header {h} parsed {g}" for k, (h, g) in bad.items()))
        if board.errors:
            print(f"  SALVAGE: {len(board.errors)} records unreadable, "
                  f"first at line {board.errors[0][0]}")
