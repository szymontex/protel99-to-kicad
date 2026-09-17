#!/usr/bin/env python3
"""Sequential parser for Protel `PCB FILE 9 VERSION 2.70` binary boards.

The file is a serialization stream of the same object model the ASCII v2.70
export writes, not a list of typed records. Three primitives:

    [len:u8][0xA1][len bytes][pad byte if len is odd]   string (pad is garbage)
    [tag:u8][0xA3][data]                                 attribute; data length
                                                         depends on the tag
    [u16 hi][u16 lo]                                     coordinate or dimension,
                                                         (hi*65536 + lo)/10000 mil

Attributes are delta-encoded against ONE global state: a tag is written only
when its value differs from the last time it was written, regardless of which
object type wrote it. Each object is therefore

    [changed attributes] [fixed positional fields] [trailing string]

Sections are announced by literal marker strings. Per component:
`M` header, designator text, comment text, `a` arcs, `f` fills, `p` pads,
`x` extra texts, `th`/`tv`/`t+`/`t-`/`tn` tracks (horizontal, vertical,
45-degree up, 45-degree down, general), `v` vias, `E` end. After the last
component: `G`, `X` texts, `TH`/`TV`/`T+`/`T-`/`TN`, `F` fills, `A` arcs,
`V` vias, `P` pads, `D` design settings (not parsed). Files are padded to a
1 KiB multiple and may carry stale bytes after `D`.

Usage:
    pcb9.py BOARD.PCB              # summary counts
    pcb9.py BOARD.PCB --dump       # every object, one per line
"""
from __future__ import annotations

import re
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

SCALE = 65536.0 / 10000.0

# Attribute data lengths. Dimensions are 4 bytes (coordinate encoding),
# enumerations and flags are 2 bytes. Meanings from matching the ASCII export.
TAG_LEN = {
    0x00: 2,   # pad/track flag, values 1 and 256 seen (archive boards)
    0x09: 4,   # pad size X, version 2.00 only (one size for every layer)
    0x0A: 4,   # pad size Y, version 2.00 only
    0x01: 2,   # layer
    0x02: 2,   # net index (varies per pad on boards that carry nets)
    0x03: 4,   # track width
    0x04: 2,   # track flag (ASCII field after layer, value 1)
    0x05: 4,   # via diameter
    0x06: 4,   # via hole
    0x07: 2,   # via flag (ASCII field after hole, value 1)
    0x0C: 4,   # pad hole
    0x0D: 2,   # pad attribute, meaning not determined; 2.70 and 2.00 write it
    0x10: 2,   # pad attribute, meaning not determined; 2.00 writes it
    0x0E: 4,   # pad X
    0x0F: 4,   # pad Y
    0x12: 4,   # text height
    0x13: 4,   # text stroke width
    0x14: 2,   # text mirror
    0x15: 2,   # text field (ASCII value 2)
    0x16: 2,   # text field, value 1 (two archive boards with polygons)
    0x17: 4,   # pad top size X
    0x18: 4,   # pad top size Y
    0x19: 4,   # pad mid size X
    0x1A: 4,   # pad mid size Y
    0x1B: 4,   # pad bottom size X
    0x1C: 4,   # pad bottom size Y
    0x1D: 2,   # text field (ASCII trailing value 1)
}

@dataclass(frozen=True)
class Layout:
    """How wide each record is in one vintage of the format.

    The stream, its markers, its attributes and its coordinate encoding are the
    same in every vintage. What changed between Advanced PCB 2.x and Protel for
    Windows 2.8 is the size of some records: 2.70 added a rotation to
    components and pads, and carries more trailing `u16` on components, pads,
    tracks, vias and nets. Everything here was measured by walking the markers
    of the demo boards that ship in both vintages - see `docs/FORMAT.md`.
    """
    version: str
    component_fields: int       # u16 after the bounding box, mirror included
    component_rotation: bool
    pad_rotation: bool
    pad_tail: int
    track_tail: int
    via_tail: int
    net_tail: int
    padstack: bool = True       # a size per copper side, or one size for all
    outline_layer: int = 29     # Protel layer the board boundary is drawn on


V270 = Layout("2.70", component_fields=5, component_rotation=True,
              pad_rotation=True, pad_tail=11, track_tail=2, via_tail=5, net_tail=7)

# Advanced PCB 2.x has no board layer: the boundary is the rectangle on the
# keep-out layer, 28. Protel for Windows added layer 29 and draws both.

# Advanced PCB 2.6 writes the same records without the rotations and with
# shorter tails.
V260 = Layout("2.60", component_fields=3, component_rotation=False,
              pad_rotation=False, pad_tail=0, track_tail=1, via_tail=1, net_tail=4,
              outline_layer=28)

# Advanced PCB 2.0 is 2.6 with one pad size for every layer, in tags 0x09 and
# 0x0A, and one shape rather than three. The padstack arrived with 2.6.
V200 = Layout("2.00", component_fields=3, component_rotation=False,
              pad_rotation=False, pad_tail=0, track_tail=1, via_tail=1, net_tail=0,
              padstack=False, outline_layer=28)

LAYOUTS = {
    "PCB FILE 9 VERSION 2.70": V270,
    "PCB FILE 9 VERSION 2.60": V260,
    "PCB FILE 9 VERSION 2.00": V200,
}

COMP_MARKERS = {b"M", b"E", b"a", b"f", b"p", b"x", b"th", b"tv", b"t+", b"t-", b"tn", b"v"}
GLOBAL_MARKERS = {b"G", b"X", b"TH", b"TV", b"T+", b"T-", b"TN", b"F", b"A", b"V", b"P", b"D"}
NET_MARKERS = {b"N", b"(", b")", b"{", b"}"}
MARKERS = COMP_MARKERS | GLOBAL_MARKERS | NET_MARKERS


class ParseError(Exception):
    pass


def dim(raw: bytes) -> float:
    hi, lo = struct.unpack("<HH", raw)
    return (hi * 65536 + lo) / 10000.0


@dataclass
class Text:
    offset: int
    x: float
    y: float
    rotation: float
    bbox: tuple
    text: str
    layer: int
    height: float
    stroke: float
    mirror: int
    f15: int
    f1d: int
    net: int = 0


@dataclass
class Pad:
    offset: int
    x: float
    y: float
    name: str
    layer: int
    hole: float
    top: tuple      # (size_x, size_y, shape)
    mid: tuple
    bot: tuple
    rotation: float
    raw_tail: tuple
    net: int = 0


@dataclass
class Track:
    offset: int
    kind: str       # h v + - n
    x1: float
    y1: float
    x2: float
    y2: float
    width: float
    layer: int
    flag: int
    tail: tuple
    net: int = 0


@dataclass
class Via:
    offset: int
    x: float
    y: float
    diameter: float
    hole: float
    flag: int
    raw_tail: tuple
    net: int = 0


@dataclass
class Arc:
    offset: int
    x: float
    y: float
    radius: float
    start: float
    end: float
    width: float
    layer: int
    tail: int
    net: int = 0


@dataclass
class Fill:
    offset: int
    x1: float
    y1: float
    x2: float
    y2: float
    layer: int
    net: int = 0


@dataclass
class Net:
    offset: int
    index: int
    name: str
    track_width: float
    via_size: float
    f1: int
    f2: int
    members: list          # (component index, pad index) pairs
    connections: list      # raw 4 x u16 groups
    tail: tuple


@dataclass
class Component:
    offset: int
    footprint: str
    x: float
    y: float
    bbox: tuple
    mirror: int
    fields: tuple
    rotation: float
    designator: Text
    comment: Text
    arcs: list = field(default_factory=list)
    fills: list = field(default_factory=list)
    pads: list = field(default_factory=list)
    texts: list = field(default_factory=list)
    tracks: list = field(default_factory=list)
    vias: list = field(default_factory=list)


@dataclass
class Polygon:
    """Polygon pour. Vertices are little-endian int32 in 1/10000 mil in a frame
    that is not yet tied to the board (two archive files only); kept raw."""
    offset: int
    layer: int
    net: int
    fields: tuple
    width: float
    grid: float
    count: int
    vertices: list


@dataclass
class Board:
    path: Path
    version: str
    counts: dict
    components: list = field(default_factory=list)
    polygons: list = field(default_factory=list)
    errors: list = field(default_factory=list)     # (offset, message) - salvage mode only
    texts: list = field(default_factory=list)
    tracks: list = field(default_factory=list)
    fills: list = field(default_factory=list)
    arcs: list = field(default_factory=list)
    vias: list = field(default_factory=list)
    pads: list = field(default_factory=list)
    nets: list = field(default_factory=list)
    end_offset: int = 0
    unknown_tags: dict = field(default_factory=dict)
    filler_skips: int = 0
    # Set by a reader that knows more about the boundary than its format entry
    # does - `PCB FILE 9` draws it on a different layer in different vintages.
    outline_layer: int | None = None


BLOCK = 4096
FILLER = b"\x00\xa0"


class Stream:
    def __init__(self, data: bytes, layout: Layout = V270):
        self.data = data
        self.layout = layout
        self.pos = 0
        self.state: dict[int, bytes] = {}
        self.unknown_tags: dict[int, int] = {}
        self.filler_skips = 0
        self.errors: list = []

    # -- block filler -------------------------------------------------------

    def skip_filler(self) -> None:
        """Skip `00 A0` padding that runs from the cursor to the next 4 KiB boundary.

        The writer flushes in 4096-byte blocks and never splits a write unit
        across a boundary. A unit is written only when strictly more bytes than
        its size remain in the block; otherwise the rest of the block is filled
        with `00 A0` pairs (measured on every boundary of both reference
        boards: a 26-byte float string is padded even when exactly 26 remain).
        Units: u16 fields, 4-byte coordinates/dimensions, whole tagged
        attributes, whole strings. Filler can therefore sit between any two
        fields, including between the two u16 halves of a track tail.
        """
        rem = BLOCK - (self.pos % BLOCK)
        if rem == BLOCK or rem % 2 or rem > 1024:
            return
        chunk = self.data[self.pos:self.pos + rem]
        if len(chunk) == rem and chunk == FILLER * (rem // 2):
            self.pos += rem
            self.filler_skips += 1

    # -- primitives ---------------------------------------------------------

    def u8(self) -> int:
        self.skip_filler()
        v = self.data[self.pos]
        self.pos += 1
        return v

    def u16(self) -> int:
        self.skip_filler()
        v = struct.unpack_from("<H", self.data, self.pos)[0]
        self.pos += 2
        return v

    def i16(self) -> int:
        self.skip_filler()
        v = struct.unpack_from("<h", self.data, self.pos)[0]
        self.pos += 2
        return v

    def u32(self) -> int:
        self.skip_filler()
        v = struct.unpack_from("<I", self.data, self.pos)[0]
        self.pos += 4
        return v

    def raw(self, n: int) -> bytes:
        self.skip_filler()
        v = self.data[self.pos:self.pos + n]
        self.pos += n
        return v

    def dim(self) -> float:
        return dim(self.raw(4))

    def string(self) -> str:
        self.skip_filler()
        n = self.data[self.pos]
        if self.data[self.pos + 1] != 0xA1:
            raise ParseError(f"@{self.pos}: expected string, got {self.data[self.pos:self.pos+4].hex(' ')}")
        s = self.data[self.pos + 2:self.pos + 2 + n]
        self.pos += 2 + n + (n & 1)
        return s.decode("latin-1")

    def float_str(self) -> float:
        """ASCII float such as ' 2.70000000000000E+0002'. Damaged archive files
        carry bit-flipped characters inside these strings; the string length is
        still right, so salvage what can be read and log the rest."""
        at = self.pos
        txt = self.string()
        try:
            return float(txt)
        except ValueError:
            pass
        cleaned = re.sub(r"[^0-9.E+\-]", "", txt.replace(",", "."))
        m = re.match(r"^(-?\d\.\d+)E([+-]\d+)$", cleaned)
        self.errors.append((at, f"unreadable float {txt!r}"))
        if m:
            try:
                return float(m.group(1)) * 10 ** int(m.group(2))
            except (ValueError, OverflowError):
                return 0.0
        return 0.0

    # -- structure ----------------------------------------------------------

    def find_marker(self, wanted: set, start: Optional[int] = None) -> Optional[int]:
        """Offset of the next `[len][A1]<marker>` for any marker in `wanted`, or None."""
        d = self.data
        i = start if start is not None else self.pos
        while True:
            i = d.find(b"\xa1", i + 1)
            if i < 0 or i == 0:
                return None
            n = d[i - 1]
            if n in (1, 2) and d[i + 1:i + 1 + n] in wanted:
                return i - 1

    def peek_marker(self) -> Optional[bytes]:
        self.skip_filler()
        d = self.data
        p = self.pos
        if p + 2 > len(d) or d[p + 1] != 0xA1:
            return None
        n = d[p]
        if n not in (1, 2):
            return None
        s = d[p + 2:p + 2 + n]
        return s if s in MARKERS else None

    def expect(self, marker: bytes) -> None:
        got = self.peek_marker()
        if got != marker:
            raise ParseError(f"@{self.pos}: expected marker {marker!r}, got {got!r} "
                             f"bytes {self.data[self.pos:self.pos+8].hex(' ')}")
        self.string()

    def tags(self) -> None:
        """Consume every attribute at the cursor into the global state."""
        d = self.data
        while True:
            self.skip_filler()
            if not (self.pos + 1 < len(d) and d[self.pos + 1] == 0xA3):
                break
            tag = d[self.pos]
            n = TAG_LEN.get(tag)
            if n is None:
                n = self._guess_tag_len(tag)
                self.unknown_tags[tag] = self.unknown_tags.get(tag, 0) + 1
            self.state[tag] = d[self.pos + 2:self.pos + 2 + n]
            self.pos += 2 + n

    def _guess_tag_len(self, tag: int) -> int:
        """Length of an attribute this reader has never seen, from what follows it.

        A tagged attribute is followed by another attribute, a string or a
        record field. Only the first two are recognisable, so the guess is:
        whichever length leaves an `A1` or `A3` marker byte at the right place.
        Block filler can sit between the attribute and whatever follows, so the
        probe looks past it.
        """
        d = self.data
        p = self.pos

        def after(n: int):
            at = p + 2 + n
            block_left = BLOCK - (at % BLOCK)
            if block_left % 2 == 0 and d[at:at + block_left] == FILLER * (block_left // 2):
                at += block_left
            return d[at + 1] if at + 1 < len(d) else None

        after2 = after(2)
        after4 = after(4)
        if after2 in (0xA1, 0xA3) and after4 not in (0xA1, 0xA3):
            return 2
        if after4 in (0xA1, 0xA3) and after2 not in (0xA1, 0xA3):
            return 4
        raise ParseError(f"@{p}: unknown tag 0x{tag:02X}, cannot infer data length; "
                         f"bytes {d[p:p+12].hex(' ')}")

    def st_u16(self, tag: int, default: int = 0) -> int:
        v = self.state.get(tag)
        return struct.unpack("<H", v)[0] if v else default

    def st_dim(self, tag: int, default: float = 0.0) -> float:
        v = self.state.get(tag)
        return dim(v) if v else default


# --------------------------------------------------------------------------
# object parsers
# --------------------------------------------------------------------------

def parse_text(s: Stream) -> Text:
    off = s.pos
    s.tags()
    x, y = s.dim(), s.dim()
    rot = s.float_str()
    bbox = (s.dim(), s.dim(), s.dim(), s.dim())
    text = s.string()
    return Text(off, x, y, rot, bbox, text,
                layer=s.st_u16(0x01), height=s.st_dim(0x12), stroke=s.st_dim(0x13),
                mirror=s.st_u16(0x14), f15=s.st_u16(0x15), f1d=s.st_u16(0x1D),
                net=s.st_u16(0x02))


def parse_pad(s: Stream) -> Pad:
    off = s.pos
    s.tags()
    if s.layout.padstack:
        shapes = (s.u16(), s.u16(), s.u16())
    else:
        shapes = (s.u16(),) * 3
    rot = s.float_str() if s.layout.pad_rotation else 0.0
    tail = tuple(s.u16() for _ in range(s.layout.pad_tail))
    name = s.string()
    if s.layout.padstack:
        sizes = ((s.st_dim(0x17), s.st_dim(0x18)), (s.st_dim(0x19), s.st_dim(0x1A)),
                 (s.st_dim(0x1B), s.st_dim(0x1C)))
    else:
        sizes = ((s.st_dim(0x09), s.st_dim(0x0A)),) * 3
    return Pad(off, s.st_dim(0x0E), s.st_dim(0x0F), name, layer=s.st_u16(0x01),
               hole=s.st_dim(0x0C),
               top=sizes[0] + (shapes[0],),
               mid=sizes[1] + (shapes[1],),
               bot=sizes[2] + (shapes[2],),
               rotation=rot, raw_tail=tail, net=s.st_u16(0x02))


def parse_track(s: Stream, kind: str) -> Track:
    off = s.pos
    s.tags()
    if kind == "h":
        x1, x2, y = s.dim(), s.dim(), s.dim()
        y1 = y2 = y
    elif kind == "v":
        x, y1, y2 = s.dim(), s.dim(), s.dim()
        x1 = x2 = x
    elif kind in "+-":
        x1, y1, length = s.dim(), s.dim(), s.dim()
        x2 = x1 + length
        y2 = y1 + length if kind == "+" else y1 - length
    else:
        x1, y1, x2, y2 = s.dim(), s.dim(), s.dim(), s.dim()
    tail = tuple(s.u16() for _ in range(s.layout.track_tail))
    return Track(off, kind, x1, y1, x2, y2, width=s.st_dim(0x03), layer=s.st_u16(0x01),
                 flag=s.st_u16(0x04), tail=tail, net=s.st_u16(0x02))


def parse_via(s: Stream) -> Via:
    off = s.pos
    s.tags()
    x, y = s.dim(), s.dim()
    tail = tuple(s.u16() for _ in range(s.layout.via_tail))
    return Via(off, x, y, diameter=s.st_dim(0x05), hole=s.st_dim(0x06),
               flag=s.st_u16(0x07), raw_tail=tail, net=s.st_u16(0x02))


def parse_arc(s: Stream) -> Arc:
    off = s.pos
    s.tags()
    x, y = s.dim(), s.dim()
    radius = s.dim()
    start = s.float_str()
    end = s.float_str()
    width = s.dim()
    tail = s.u16()
    return Arc(off, x, y, radius, start, end, width, layer=s.st_u16(0x01), tail=tail,
               net=s.st_u16(0x02))


def parse_fill(s: Stream) -> Fill:
    off = s.pos
    s.tags()
    x1, y1, x2, y2 = s.dim(), s.dim(), s.dim(), s.dim()
    return Fill(off, x1, y1, x2, y2, layer=s.st_u16(0x01), net=s.st_u16(0x02))


def parse_net(s: Stream, index: int) -> Net:
    """`N` name width via_size u16 u16 `(` (comp, pad)* `)` `{` 4xu16* `}` 7xu16."""
    off = s.pos
    s.expect(b"N")
    name = s.string()
    width, via = s.dim(), s.dim()
    f1, f2 = s.u16(), s.u16()
    s.expect(b"(")
    members = []
    while s.peek_marker() != b")":
        members.append((s.u16(), s.u16()))
    s.expect(b")")
    s.expect(b"{")
    conns = []
    while s.peek_marker() != b"}":
        conns.append(tuple(s.u16() for _ in range(4)))
    s.expect(b"}")
    tail = tuple(s.u16() for _ in range(s.layout.net_tail))
    return Net(off, index, name, width, via, f1, f2, members, conns, tail)


def parse_polygon(s: Stream) -> Polygon:
    """Polygon record inside the `G` section (seen in two archive files).

    [tags: 01 layer, 02 net] 6 x u16, width (dim), grid (dim), u16 count,
    u16, then count x (int32 x, int32 y) little-endian in 1/10000 mil. The
    vertex buffer is followed by stale bytes up to the `X` marker, so the
    caller resynchronises on `X` afterwards.
    """
    off = s.pos
    s.tags()
    fields = tuple(s.u16() for _ in range(6))
    width, grid = s.dim(), s.dim()
    count = s.u16()
    f7 = s.u16()
    verts = []
    for _ in range(min(count, 4096)):
        x, y = struct.unpack_from("<ii", s.data, s.pos)
        s.pos += 8
        verts.append((x / 10000.0, y / 10000.0))
    return Polygon(off, s.st_u16(0x01), s.st_u16(0x02), fields + (f7,), width, grid, count, verts)


def parse_section(s: Stream, marker: bytes, parser, *args) -> list:
    """Consume `marker`, then objects until the cursor sits on another marker."""
    s.expect(marker)
    out = []
    while s.peek_marker() is None:
        if s.pos >= len(s.data) - 4:
            raise ParseError(f"@{s.pos}: ran off the end inside section {marker!r}")
        out.append(parser(s, *args))
    return out


def parse_component(s: Stream) -> Component:
    off = s.pos
    s.expect(b"M")
    footprint = s.string()
    z0 = s.u16()
    x, y = s.dim(), s.dim()
    bbox = (s.dim(), s.dim(), s.dim(), s.dim())
    mirror = s.u16()
    if s.layout.component_rotation:
        fields = (s.u16(), s.i16(), s.u16(), s.u16())
        rot = s.float_str()
    else:
        # Advanced PCB 2.x places components on the 90-degree steps its own
        # user interface offered, and stores no rotation field. The footprint
        # geometry that follows is already placed, so a zero here is the truth
        # about the file rather than a missing value.
        fields = tuple(s.u16() for _ in range(s.layout.component_fields - 1))
        rot = 0.0
    desig = parse_text(s)
    comment = parse_text(s)
    c = Component(off, footprint, x, y, bbox, mirror, (z0,) + fields, rot, desig, comment)
    c.arcs = parse_section(s, b"a", parse_arc)
    c.fills = parse_section(s, b"f", parse_fill)
    c.pads = parse_section(s, b"p", parse_pad)
    c.texts = parse_section(s, b"x", parse_text)
    for marker, kind in ((b"th", "h"), (b"tv", "v"), (b"t+", "+"), (b"t-", "-"), (b"tn", "n")):
        c.tracks += parse_section(s, marker, parse_track, kind)
    c.vias = parse_section(s, b"v", parse_via)
    s.expect(b"E")
    return c


def parse(path: Path, strict: bool = False) -> Board:
    data = path.read_bytes()
    s = Stream(data)
    version = s.string()
    layout = LAYOUTS.get(version.strip())
    if layout is None:
        raise ParseError(
            f"unsupported header {version!r} - this reader decodes "
            + ", ".join(sorted(LAYOUTS)))
    s.layout = layout
    h0 = s.u16()
    raw_counts = [s.u32() for _ in range(9)]
    h_last = s.u16()
    # Same order as line 2 of the ASCII v2.70 export; meanings verified against
    # parsed object counts on boards with and without nets.
    counts = {
        "h0": h0, "connections": raw_counts[0], "tracks": raw_counts[1],
        "net_members": raw_counts[2], "nets": raw_counts[3], "arcs": raw_counts[4],
        "vias": raw_counts[5], "components": raw_counts[6], "fills": raw_counts[7],
        "pads": raw_counts[8], "last": h_last,
    }
    board = Board(path, version, counts, outline_layer=layout.outline_layer)

    def resync(err: Exception, wanted: set) -> bool:
        """Salvage mode: log the error and jump to the next wanted marker."""
        if strict:
            raise err
        at = s.find_marker(wanted, s.pos)
        s.errors.append((s.pos, f"{err}; resync to {at}"))
        if at is None:
            return False
        s.pos = at
        return True

    # components
    while s.peek_marker() == b"M":
        try:
            board.components.append(parse_component(s))
        except (ParseError, struct.error, IndexError) as e:
            if not resync(e, {b"M", b"G"}):
                break

    # global sections, in file order; each resyncs to the next one on damage
    order = [b"G", b"X", b"TH", b"TV", b"T+", b"T-", b"TN", b"F", b"A", b"V", b"P", b"D"]
    kinds = {b"TH": "h", b"TV": "v", b"T+": "+", b"T-": "-", b"TN": "n"}
    parsers = {b"X": (parse_text, "texts"), b"F": (parse_fill, "fills"), b"A": (parse_arc, "arcs"),
               b"V": (parse_via, "vias"), b"P": (parse_pad, "pads")}
    for i, marker in enumerate(order[:-1]):
        nxt = set(order[i + 1:])
        try:
            if marker == b"G":
                s.expect(b"G")
                while s.peek_marker() != b"X":
                    board.polygons.append(parse_polygon(s))
                    at = s.find_marker({b"X"}, s.pos)
                    if at is None:
                        raise ParseError(f"@{s.pos}: no X marker after polygon")
                    s.pos = at
            elif marker in kinds:
                board.tracks += parse_section(s, marker, parse_track, kinds[marker])
            else:
                fn, attr = parsers[marker]
                setattr(board, attr, getattr(board, attr) + parse_section(s, marker, fn))
            if marker == b"P":
                while s.peek_marker() == b"N":
                    board.nets.append(parse_net(s, len(board.nets)))
        except (ParseError, struct.error, IndexError, ValueError) as e:
            if not resync(e, nxt):
                break
    try:
        s.expect(b"D")
    except ParseError as e:
        if strict:
            raise
        s.errors.append((s.pos, str(e)))
    board.end_offset = s.pos
    board.unknown_tags = dict(s.unknown_tags)
    board.filler_skips = s.filler_skips
    board.errors = list(s.errors)
    return board


# --------------------------------------------------------------------------

def summary(b: Board) -> str:
    comp_tracks = sum(len(c.tracks) for c in b.components)
    comp_vias = sum(len(c.vias) for c in b.components)
    comp_arcs = sum(len(c.arcs) for c in b.components)
    comp_fills = sum(len(c.fills) for c in b.components)
    comp_pads = sum(len(c.pads) for c in b.components)
    comp_texts = sum(2 + len(c.texts) for c in b.components)
    lines = [
        f"{b.path.name}: {b.version}",
        f"header counts: {b.counts}",
        f"components {len(b.components)} (header {b.counts['components']})",
        f"tracks  free {len(b.tracks)} + comp {comp_tracks} = {len(b.tracks)+comp_tracks} (header {b.counts['tracks']})",
        f"arcs    free {len(b.arcs)} + comp {comp_arcs} = {len(b.arcs)+comp_arcs} (header {b.counts['arcs']})",
        f"vias    free {len(b.vias)} + comp {comp_vias} = {len(b.vias)+comp_vias} (header {b.counts['vias']})",
        f"fills   free {len(b.fills)} + comp {comp_fills} = {len(b.fills)+comp_fills} (header {b.counts['fills']})",
        f"texts   free {len(b.texts)} + comp {comp_texts} = {len(b.texts)+comp_texts}",
        f"pads    free {len(b.pads)} + comp {comp_pads} = {len(b.pads)+comp_pads} (header {b.counts['pads']})",
        f"nets    {len(b.nets)} (header {b.counts['nets']})   members {sum(len(n.members) for n in b.nets)}"
        f" (header {b.counts['net_members']})   connections {sum(len(n.connections) for n in b.nets)}"
        f" (header {b.counts['connections']})",
        f"parsed up to offset {b.end_offset} of {b.path.stat().st_size}",
        f"polygons {len(b.polygons)}",
        f"unknown tags: {b.unknown_tags}   block filler skips: {b.filler_skips}",
        f"errors (salvage mode): {len(b.errors)}" + (f"  first: {b.errors[0]}" if b.errors else ""),
    ]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    path = Path(argv[1])
    b = parse(path)
    print(summary(b))
    if "--dump" in argv:
        for c in b.components:
            print(f"\nCOMP @{c.offset} {c.footprint!r} ({c.x:.4f}, {c.y:.4f}) rot {c.rotation} mirror {c.mirror} "
                  f"desig {c.designator.text!r} comment {c.comment.text!r}")
            for p in c.pads:
                print(f"  PAD  {p.name!r} ({p.x:.4f}, {p.y:.4f}) L{p.layer} hole {p.hole} top {p.top} bot {p.bot} rot {p.rotation}")
            for t in c.tracks:
                print(f"  TRK{t.kind} ({t.x1:.4f}, {t.y1:.4f})-({t.x2:.4f}, {t.y2:.4f}) w {t.width} L{t.layer}")
            for v in c.vias:
                print(f"  VIA  ({v.x:.4f}, {v.y:.4f}) d {v.diameter} hole {v.hole}")
            for a in c.arcs:
                print(f"  ARC  ({a.x:.4f}, {a.y:.4f}) r {a.radius} {a.start}-{a.end} w {a.width} L{a.layer}")
            for f in c.fills:
                print(f"  FILL ({f.x1:.4f}, {f.y1:.4f})-({f.x2:.4f}, {f.y2:.4f}) L{f.layer}")
        for t in b.texts:
            print(f"TEXT ({t.x:.4f}, {t.y:.4f}) {t.text!r} h {t.height} L{t.layer} rot {t.rotation}")
        for t in b.tracks[:20]:
            print(f"TRK{t.kind} ({t.x1:.4f}, {t.y1:.4f})-({t.x2:.4f}, {t.y2:.4f}) w {t.width} L{t.layer} flag {t.flag} tail {t.tail}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
