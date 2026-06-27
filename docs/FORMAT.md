# Protel for Windows PCB Binary Format - Reverse Engineering Notes

These notes describe the `PCB FILE 9 VERSION 2.70` binary format used by Protel for
Windows (Protel 99 SE, the predecessor of Altium Designer). Everything here was
recovered by reverse engineering; the identifier is undocumented in any official spec.

## File header

```
Offset 0x00: 0x17 0xA1 - record marker (type 0x17)
Offset 0x02: "PCB FILE 9 VERSION 2.70" - null-terminated ASCII string
```

Format version string: `PCB FILE 9 VERSION 2.70`.

## Record structure

Every record starts with a 2-byte marker: `[TYPE] [0xA1]`

- `TYPE` byte: `0x01`-`0x20` (record type identifier)
- `0xA1`: constant marker byte

Records are variable-length. A record ends where the next marker begins.

## Record types (discovered via reverse engineering)

| Type | Content | Meaning |
|------|---------|---------|
| 0x01 | Binary, short (~4B avg) | Pad/pin records - hold board-range X values as LE uint16 |
| 0x02 | Binary + occasional strings | Track segments / wire connections - hold board-range coordinates |
| 0x03 | Short ASCII strings | **Designator** (R1, C1) or **value** (15k, 6k8) |
| 0x04 | Short ASCII strings | **Footprint / package** (e.g. 1206, 22nF) |
| 0x05 | Short ASCII strings | **Component value** (470pF, 680pF) |
| 0x06 | ASCII strings | **Component / footprint name** (e.g. SOT-23, BC847C) |
| 0x07 | Mixed | Unknown |
| 0x08 | ASCII string | **Pad shape / footprint style** (e.g. MINIMELF) |
| 0x09 | ASCII string | **Complex / custom footprint ref** |
| 0x0A | Binary, ~47B | **Via definitions** - hold board-range coordinates |
| 0x0B | ASCII string | **Library footprint reference** |
| 0x0C | Mixed | Unknown |
| 0x0D | Mixed | Unknown |
| 0x0E | Mixed | Unknown |
| 0x17 | ASCII float + binary tail | **Local geometry: rotation angle + pad offsets within a footprint** |
| 0x20 | Mixed, large (~152B avg) | **Complex objects (polygon fills?)** - hold board-range Y values |

About 24 record types were identified in total. Counts and exact byte sizes vary
between files.

## Float format

Floating-point values are stored as ASCII strings in scientific notation:

```
" X.XXXXXXXXXXXXXXE±XXXXX"
```

- **23 characters** (with leading space): space(1) + digit(1) + dot(1) + 14 digits(14) + `E`(1) + sign(1) + 4 digits(4) = 23
- Examples: `" 0.00000000000000E+0000"` (= 0.0), `" 2.70000000000000E+0002"` (= 270.0)
- Used for rotation angles (0, 90, 180, 270, 360 degrees).
- **Not used for coordinates** - coordinates live in the binary tail or in other record types.

## Component record sequence

Components appear as a run of sequential records:

```
REC_06: footprint name    (e.g. "SOT-23")
REC_17: float (rotation)  (e.g. 0.0 or 270.0)
REC_06: component name    (e.g. "BC847C")
...
REC_08: pad shape         (e.g. "MINIMELF")
REC_03: designator        (e.g. "R1")
REC_03/05: value          (e.g. "15k")
REC_17: float (coords?)   (binary-encoded coordinates follow the float)
```

---

## Coordinate encoding

### Two distinct coordinate layers

The format stores coordinates at two distinct levels, in different records:

1. **Local geometry** (footprint-level) - stored in **REC_17** tails.
2. **Absolute positions** (board-level) - stored in **REC_01**, **REC_02**, **REC_0A**, **REC_20**.

These must not be confused. REC_17 tails do **not** contain absolute board positions.

### Absolute position encoding (board-level) - partially resolved

Board-level **X** coordinates use **LE uint16 raw mils** (thousandths of an inch).
The **Y** coordinate encoding is **unresolved** after exhaustive binary analysis.

| Record type | Stores | X encoding | Y encoding |
|-------------|--------|-----------|-----------|
| 0x01 (pad)  | Pad/pin positions | LE uint16 mils - confirmed | **Unresolved** |
| 0x02 (wire) | Track endpoints | LE uint16 mils - confirmed | **Unresolved** |
| 0x0A (via)  | Via positions | LE uint16 mils - confirmed | **Unresolved** |
| 0x20 (poly) | Polygon vertices | Partial | Partial |

**Current heuristic** (`decode_absolute_position`): scans for adjacent LE uint16 pairs
where both values fall within the extended board range (about ±200 mils). On a typical
board this finds positions for only a small fraction of components, all clustered near
the board's left edge - X confirmed but Y not.

**Encodings exhaustively eliminated for Y coordinates:**

| Encoding | Method | Result |
|----------|--------|--------|
| LE uint16 | Scan all offsets for known Y values | 0 hits at any offset |
| BE uint16 | Scan all offsets | A handful of sporadic hits, not systematic |
| LE/BE int16 signed | Scan all offsets | Same data, no Y-range clustering |
| int32 with scale 1-99 | Scan 4-byte aligned for Y x N | 0 hits for any scale factor |
| int32/10000 (protel2kicad v3/v4) | Specific v3/v4 encoding | 0 hits - v2.70 is a different format |
| IEEE float32 (3 unit systems) | Scan all offsets | 0 real hits; many false positives from ASCII collisions |
| Turbo Pascal Real48 | Scan all offsets (6-byte) | 0 hits matching known coordinates |
| 24-bit LE integer | Scan all offsets | 0 hits for any Y value |
| Fixed-point (x1.27, x2.54, x25.4) | Scan for metric conversions | 0 meaningful hits |
| Bit-reversal of 16-bit values | Reverse all bits | 0 matches |
| Constant offset from uint16 | Correlate stored values vs known Y | No consistent offset found |

A mystery uint16 consistently follows each confirmed X value. It does not generalize to
known Y coordinates and most likely encodes per-vertex metadata (net hash, layer, or
segment attributes).

### Local geometry encoding (REC_17 tails)

REC_17 binary tails store **local / relative geometry** - pad and pin positions inside a
footprint, not absolute board coordinates. Values cluster in roughly the 5000-7000 mil
range, representing offsets within footprint definitions. See "Binary tail of REC_0x17"
below for the byte layout.

---

## Binary tail of REC_0x17

Structure of a type 0x17 record (data portion, after the 2-byte `[0x17][0xA1]` marker):

```
[0-22]   Float: " 0.00000000000000E+0000" (23 bytes, not 22)
[23-...]  Binary tail - variable length, contains geometry data
```

The binary tail starts at data byte 23 (byte 25 from record start, including the marker).
Tail length varies by record size: 17B is the most common, then 23B, then 9B, and so on.

**Tail structure for non-sub-tag records:**

- Byte 0: flag / attribute byte
- Bytes 1+: LE int16 values at stride-4 offsets (1, 5, 9, 13, ...)
- These values cluster in the 5000-7000 range.
- They represent local geometry (pad offsets within a footprint), not absolute board coordinates.
- The interleaving 2 bytes (at offsets 3, 7, 11, 15) have varied values - purpose unknown.

**Sub-tag records** (identified by `[TAG][0xA3]` at tail offset 1):

- Flag byte, then one or more `[TAG_ID][0xA3][DATA...]` groups.
- The sub-tag structure is 6 bytes total: `[TAG_ID][0xA3]` plus 4 data bytes.
- After the sub-tags, the remaining bytes contain geometry data.
- Tags observed: 0x01, 0x12, 0x13, 0x14, 0x15, 0x1C, 0x1D.

## Diff analysis notes

Comparing two revisions of the same board (different component placement and values)
surfaces the structure:

- Component records begin early in the file; a revision's first component record differs
  in its footprint name byte.
- Rotation angle changes show up as a different ASCII float at a fixed offset within the
  component's REC_17.
- Component values appear inline as readable ASCII inside REC_03/REC_05.
- 765 of the REC_02 records in a typical file are only 2 bytes long - just an ASCII net
  name prefix, with no coordinate payload. Only records of about 30 bytes or more carry
  actual wire segments.
- REC_0A (via) records begin with an ASCII net name, a null byte, then binary coordinate
  data in the same uint16 clustering pattern as the other record types.

## Example symbols seen in real files

These are standard industry designations and serve as illustrative examples only:

- Footprints: SOT-23, SOT-223, MINIMELF, 1206, DIP-8, SO-8, SO-14
- Components: BC847C, BC857B, BC807, BCP56-16, TL074, LM293
- Values: 22nF, 470pF, 680pF, 15k, 6k8, 8k2, 10k, 24k, 68k
- Designators: R1, C1, U1
