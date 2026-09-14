# Protel `PCB FILE 9 VERSION 2.70` - binary format specification

The native board file of Protel for Windows (Protel 98 / Protel 99 SE, the
predecessor of Altium Designer). The identifier appears in no published
specification; everything below comes from reverse engineering against
Protel's own ASCII v2.70 export of the same boards.

**Status (verified 2026-09-13).** The decoder in `src/protel99_parser/pcb9.py`
reproduces the ASCII v2.70 export of two reference boards to the last digit:
every component, pad, track, via, arc, fill and text matches (board A: 244
components, 516 pads, 3342 tracks, 122 vias, 18 arcs, 394 fills, 494 texts;
board B: 91 / 159 / 872 / 42 / 8 / 4 / 183). On an archive of 1178 boards the
parsed object counts equal the counts stored in each file header for 1170
boards; the remaining 8 carry byte-level damage and are read in salvage
mode. The binary holds 1/10000 mil, the ASCII export only 1/1000 mil, so the
binary is the more precise source.

Verification commands are at the end.

---

## 1. Serialization primitives

The file is a serialization stream of Protel's object model, written with
four primitives. It is not a list of typed records.

| Primitive | Bytes | Notes |
|---|---|---|
| **String** | `[len:u8] [0xA1] [len bytes] [pad]` | `pad` is one byte, present only when `len` is odd. Its value is uninitialised memory, not data. `len` may be 0 (`00 A1` = empty string). |
| **Attribute** | `[tag:u8] [0xA3] [data]` | Data length depends on the tag (section 6): 2 bytes for enumerations and flags, 4 bytes for dimensions. |
| **Dimension / coordinate** | `[u16 hi] [u16 lo]` | Value = `(hi * 65536 + lo) / 10000` mil, both words little-endian, high word first. Equivalent: `hi * 6.5536 + lo / 10000`. |
| **Field** | `u16`, `i16` | Little-endian. |

Earlier notes read `[len][0xA1]` as a record type marker. The "type" byte is
the string length: `0x17 0xA1` precedes every 23-character ASCII float and
the 23-character header string, `0x01 0xA1` precedes one-letter pin names.

### Floats

Angles are ASCII strings of exactly 23 characters in scientific notation,
` 2.70000000000000E+0002`, stored as a String primitive (so `17 A1` + 23
bytes + one pad byte). Damaged archive files show bit-flipped characters
inside these strings; the length prefix keeps the stream in sync.

### Delta-encoded attributes

Attributes are written only when their value differs from the last value
written for that tag - regardless of object type. One global state, keyed by
tag, persists across the whole file. A reader must keep that state and give
every object the current value of every tag it uses. Example: PRZELOT-2
components re-emit `layer = 17` for their designator text after the previous
component's tracks set `layer = 1`.

### Object shape

Every object is written as

```
[changed attributes]  [fixed positional fields]  [trailing string]
```

The trailing string is the pad name, the text content, or absent.

### 4 KiB blocks and filler

The writer flushes in 4096-byte blocks and never splits a write unit across
a block boundary. A unit is written only when strictly more bytes than its
size remain in the block; otherwise the rest of the block is filled with the
byte pair `00 A0` up to the boundary. Units are: a `u16`, a 4-byte
dimension, a whole attribute (`tag A3 data`), a whole string (with pad).
Consequences:

- filler can appear between any two fields, including between the two `u16`
  halves of a track tail;
- every block boundary in a file carries at least 2 filler bytes (a 2-byte
  unit is padded when exactly 2 bytes remain);
- a 26-byte float string is padded even when exactly 26 bytes remain.

Files are a multiple of 4096 bytes. Bytes after the end of the last section
are stale: board B carries a second, identical copy of its last sections
there. Stop reading at the `D` marker.

---

## 2. File layout

```
header
component * N                     "M" ... "E"
"G"  polygon *                    usually empty
"X"  text *                       free strings
"TH" "TV" "T+" "T-" "TN"          free tracks by orientation
"F"  fill *
"A"  arc *
"V"  via *
"P"  pad *                        free pads
"N"  net *                        absent when the board has no nets
"D"  design settings              not decoded (origin, grids, colours, printer)
```

Markers are ordinary String primitives with literal content: `01 A1 47` is
`"G"`, `02 A1 54 48` is `"TH"`. Lowercase markers are used inside
components, uppercase at board level.

### Header

```
0x00  String  "PCB FILE 9 VERSION 2.70"      (17 A1 + 23 bytes + pad)
0x1A  u16     0                              unknown
0x1C  u32     connections    total entries in the nets' connection lists
0x20  u32     tracks         free + component
0x24  u32     net members    total entries in the nets' member lists
0x28  u32     nets
0x2C  u32     arcs
0x30  u32     vias
0x34  u32     components
0x38  u32     fills
0x3C  u32     pads           free + component
0x40  u16     unknown        6, 1, 15, 74 seen
0x42  first component
```

The nine `u32` values are, in the same order, line 2 of the ASCII v2.70
export (`0 3342 0 0 18 122 244 394 516`). They give every reader a built-in
structural self-check: after parsing, the object counts must equal them.

---

## 3. Component

```
"M"
String   footprint name                       "SOT-23"
u16      0
dim x 6  X, Y, bbox X1, bbox Y1, bbox X2, bbox Y2
u16      mirror (1 = bottom side)
u16      unknown (0 or 1)
i16      unknown (-105, -82 seen)
u16      1
u16      1
String   rotation, ASCII float
text     designator          (section 4.1)
text     comment             (section 4.1)
"a"  arc *
"f"  fill *
"p"  pad *
"x"  text *                  extra strings, usually none
"th" "tv" "t+" "t-" "tn"     track * each
"v"  via *
"E"
```

Component tracks, arcs, fills, pads and vias are stored in **absolute board
coordinates**, already placed and rotated. No footprint library is needed to
reproduce the board; every pad carries its own geometry.

The ASCII `COMP` header writes the same values in a different order:
`0 0 X Y mirror u16 i16 BBX1 BBY1 BBX2 BBY2 rotation 1 1`.

---

## 4. Objects

Every object begins by consuming attributes into the global state
(section 6). Fields below are the fixed part that follows.

### 4.1 Text (`CS` / `FS` in ASCII)

```
dim x 2   X, Y                    anchor = lower-left corner of the text
String    rotation (float)
dim x 4   bounding box X1 Y1 X2 Y2
String    text                    may be empty (00 A1)
```
Attributes used: `01` layer, `12` height, `13` stroke width, `14` mirror,
`15` (value 2), `16` (value 1, rare), `1D` (value 1), `02` net.

### 4.2 Pad (`CP` / `FP`)

```
u16 x 3   shape top, mid, bottom    1 round, 2 rectangle, 3 octagon
String    rotation (float)
u16 x 11  0, 20000, 0, 0, 1, 34464, 1, 34464, 1, 34464, 4
          (read as 11 u16 - filler may fall between any two)
String    name
```
Attributes: `17`/`18` top size X/Y, `19`/`1A` mid size X/Y, `1B`/`1C`
bottom size X/Y, `0C` hole, `01` layer (1 top, 16 bottom, 34 multilayer),
`0E` X, `0F` Y, `02` net, `00` flag. Sizes default to 0; a bottom-only SMD
pad has only `1B`/`1C` set, matching the ASCII `0 0 2 0 0 2 35000 35000 2`.
The 11 `u16` correspond to the ASCII tail `2000 0 10000 10000 10000 4`
(2 mil, 0, 10 mil x 3, 4) - solder-mask/paste expansions and a flag, taken
as constants so far. Protel's own mask gerbers agree with the first value:
the mask opening is the pad plus 2 mil a side (board C, GTS aperture 44 for
a 40 mil pad; `compare_gerbers.py --layers all` reports a size delta of
0.0 mil with `pad_to_mask_clearance 0.0508`).

All three sizes are real: on board D six pads are 39.37 mil on top and
275.591 mil on the bottom, and Protel's gerbers show exactly that. The KiCad
writer carries them as a KiCad 9 padstack (`front_inner_back`).

### 4.3 Track (`CT` / `FT`)

Tracks are grouped by orientation; the group marker fixes the layout.

| Marker | Fields | Meaning |
|---|---|---|
| `th` / `TH` | dim X1, dim X2, dim Y | horizontal: (X1, Y) - (X2, Y) |
| `tv` / `TV` | dim X, dim Y1, dim Y2 | vertical: (X, Y1) - (X, Y2) |
| `t+` / `T+` | dim X, dim Y, dim L | 45 degrees up: (X, Y) - (X + L, Y + L) |
| `t-` / `T-` | dim X, dim Y, dim L | 45 degrees down: (X, Y) - (X + L, Y - L) |
| `tn` / `TN` | dim X1, dim Y1, dim X2, dim Y2 | general |

Each block ends with `u16 u16` (0 0 so far). Attributes: `03` width,
`01` layer, `04` flag (value 1), `02` net, `00` flag. A block is followed
directly by the next block; attribute changes sit between blocks.

### 4.4 Via (`CV` / `FV`)

```
dim x 2   X, Y
u16 x 5   0, 20000, 1, 34464, 0      (2 mil, 10 mil, 0 - as in the pad tail)
```
Attributes: `05` diameter, `06` hole, `07` flag (value 1), `02` net.

### 4.5 Arc (`CA` / `FA`)

```
dim x 2   centre X, Y
dim       radius
String    start angle (float)
String    end angle (float)        counter-clockwise from start, Y up
dim       width
u16       0
```
Attributes: `01` layer, `02` net. Start = end (0 / 360) is a full circle.
Width is a fixed field here, not attribute `03`.

### 4.6 Fill (`CF` / `FF`)

```
dim x 4   X1, Y1, X2, Y2
```
Attributes: `01` layer, `02` net. No trailing field.

### 4.7 Polygon (section `G`)

Seen in two archive files only (board E, board F).

```
u16 x 6   0 1 1 1 0 1 seen
dim       track width (20 mil)
dim       grid (20 mil)
u16       vertex count
u16       unknown (918)
i32 x 2 * count   vertices, little-endian int32, 1/10000 mil
                  (plain LE, not the hi/lo dimension encoding)
stale bytes up to the "X" marker
```
Attributes: `01` layer, `02` net. In both files the vertices fit the board
only when read as `(Y, X)`; track endpoints land on the outline under that
reading (19 and 7 endpoints) and never under `(X, Y)`. Treat the axis order
as inferred, not proven.

### 4.8 Net (records after `P`)

```
"N"
String    name
dim       track width for the net
dim       via diameter for the net
u16 x 2   (0 or 2), 4
"("  (u16, u16) *   member list, until ")"
")"
"{"  (u16 x 4) *    connection list, until "}"
"}"
u16 x 7   0 0 0 0 128 32768 0 seen
```
Net index on objects is attribute `02`: **1-based position in the `N`
list, 0 = no net.** Pads, tracks, vias, fills, arcs and texts all carry it,
consistently. The member pairs look like `(component + 1, pad + 1)` and
agree with the object tags for part of the entries only (60 of 149 on
board H); the rest point at other components. They appear to be stale
bookkeeping - use the tags. Connections (4 x `u16`) are not decoded.

---

## 5. Coordinates

- Absolute, Y up, in mils, resolution 1/10000 mil. Boards in the archive
  sit at 5000-65000 mil.
- The ASCII v2.70 export **truncates** to 1/1000 mil: every matched value
  satisfies `0 <= binary - ascii < 0.001` (measured on 6000+ objects).
  A byte search for an exactly encoded ASCII value therefore misses whenever
  the fourth decimal is non-zero, which is why the first search for a track
  X failed while Y (fourth decimal 0) hit.
- The board outline is drawn on layer 29 (Mechanical 1) in this archive;
  the `D` section begins with the board origin, which equals the lower-left
  corner of the outline on both reference boards.

---

## 6. Attribute tags

| Tag | Bytes | Meaning | Objects |
|---|---|---|---|
| `00` | 2 | flag, 1 or 256 seen | pad, track |
| `01` | 2 | layer | all |
| `02` | 2 | net index (1-based, 0 = none) | all |
| `03` | 4 | track width | track |
| `04` | 2 | track flag, 1 | track |
| `05` | 4 | via diameter | via |
| `06` | 4 | via hole | via |
| `07` | 2 | via flag, 1 | via |
| `0C` | 4 | pad hole | pad |
| `0E` | 4 | pad X | pad |
| `0F` | 4 | pad Y | pad |
| `12` | 4 | text height | text |
| `13` | 4 | text stroke width | text |
| `14` | 2 | text mirror | text |
| `15` | 2 | text, value 2 | text |
| `16` | 2 | text, value 1 (rare) | text |
| `17` `18` | 4 | pad top size X, Y | pad |
| `19` `1A` | 4 | pad mid size X, Y | pad |
| `1B` `1C` | 4 | pad bottom size X, Y | pad |
| `1D` | 2 | text, value 1 | text |

An unknown tag stops the decoder with its offset unless its length can be
inferred from the bytes that follow. Across 1178 archive boards no tag
outside this table occurs.

---

## 7. Layers

Protel numbering as it appears in the file and in the ASCII export:

| Number | Layer | KiCad |
|---|---|---|
| 1 | Top copper | F.Cu |
| 2-15 | Mid 1-14 | In1.Cu - In14.Cu |
| 16 | Bottom copper | B.Cu |
| 17 / 18 | Top / Bottom overlay | F.SilkS / B.SilkS |
| 19 / 20 | Top / Bottom paste | F.Paste / B.Paste |
| 21 / 22 | Top / Bottom solder | F.Mask / B.Mask |
| 27 | Drill guide | Dwgs.User |
| 28 | Keep-out | Margin |
| 29-32 | Mechanical 1-4 | Edge.Cuts (29, outline by convention), Dwgs/Cmts/Eco1.User |
| 33 | Drill drawing | Eco2.User |
| 34 | Multilayer | through-hole pads |

---

## 8. Not decoded

- Header `u16` at 0x1A and 0x40 (0x40 is not the polygon count: 14 on a
  board with none, 15 on one with one).
- Component header fields: the first `u16` after the mirror flag is the
  ASCII `COMP` column 5 (0 or 1; 13 of 244 components on board A), the
  `i16` is constant per board (-105, 87 or 0), the two trailing `1` never
  vary. None of them is the text hide flag: on boards whose silkscreen
  gerber shows some designators and not others (board E, board G) the
  fields are identical for both groups.
- The 11-`u16` pad tail and 5-`u16` via tail beyond their constant values.
- Text attributes `15`, `16`, `1D`; text visibility. Protel's silkscreen
  plots in this archive show the comments and, on most boards, no
  designators at all, and draw some texts of components about one text
  width away from the stored box; neither the flag nor the offset rule is
  identified.
- Net member pairs (stale: they agree with the per-object net index on
  21-38 % of pads, and the index is the one that matches copper
  connectivity), connection lists, the 7-`u16` net tail (colour?).
- Polygon fields, vertex axis order (inferred), stale-buffer length.
- The `D` section: 16 bytes, 11 flag bytes, then grids as float strings in
  1/10000 mil (board C: 3937007.87 = 10 mm, 984251.97 = 2.5 mm, 200000 =
  20 mil, 492125.98 = 1.25 mm; board B and board A: 400, 100, 20, 25 mil),
  the ECO file name, defaults (30/50 mil), printer, layer scales and
  colours. Workspace settings, not board geometry.

None of these affect geometry, layers or connectivity.

---

## 9. Verification

The format was not taken on trust. Four witnesses, none of which owes anything
to this parser, were used; the tooling that drives them is kept outside this
repository because it carries the boards it was run against.

**Protel's own ASCII v2.70 export.** Two boards in the source archive have one.
Every object class was matched one to one - components, texts, pads, tracks,
vias, arcs, fills. Result: nothing on either side without a counterpart, and
every coordinate within `[0, 0.0009]` mil. The export quantises to 1/1000 mil
while the binary holds 1/10000, so the binary is the more precise of the two.

**Protel's own gerbers and NC drill files.** 106 boards in the archive carry
the plots Protel sent to the fab, in the same directory as the binary they came
from. Copper is compared as distance to the other side's copper *body* - stroke
width, pad shape and filled region included - because the two tools lay the same
metal down differently. Result on the boards measured: pads and vias one to one
under 1 mil, copper 100 % once two of Protel's own habits are accounted for
(below). Drill files match hole for hole with agreeing diameters.

Two things in Protel's plots are not conversion faults and any comparison has to
know about them: a pad or track with no matching aperture is *painted* with a
brush - a round 80 mil one, or a square of `floor(shorter side)` for fills - and
the Mechanical 1 outline is appended to every plot, copper included.

**KiCad's own loader.** The generated board is read back through `pcbnew` and pad,
footprint-anchor and via positions are compared with the binary. Result: at most
0.0003 mil.

**The file's own header.** It states the number of tracks, arcs, vias,
components, fills, pads, nets, members and connections. After parsing, those
counts must match what was read. This validates an archive of 1178 boards with
no export of any kind: 1170 parse clean with zero count mismatches and zero
unknown tags, 8 are damaged at byte level and are read in salvage mode, which
reports every resynchronisation with its offset.

### Unit tests

`tests/test_pcb9.py` covers the stream primitives and, when reference boards are
available, their header counts. Point `PCB9_REFERENCE_DIR` at a directory
holding them; without it those tests skip.

```bash
pip install -e .
pytest
```

Last verified: 2026-09-14.
