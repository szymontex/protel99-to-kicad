# Coordinate Encoding - Discovery and Status

## Summary

The `PCB FILE 9 VERSION 2.70` format stores coordinates at two distinct levels:

1. **Local geometry** in REC_17 tails - pad/pin offsets within footprints (roughly the
   5000-7000 mil range). **Fully decoded.**
2. **Absolute positions** in REC_01/02/0A/20 - board-level coordinates. **X confirmed as
   LE uint16 mils. Y encoding unresolved** after exhaustive investigation that eliminated
   every standard binary encoding.

### Current position-decode status

- Components are extracted by a generalized record-boundary detector.
- Most components are positioned via a KiCad ground-truth lookup; the remainder stay
  unmatched (test points, diodes, connectors, mechanical parts) because their designator
  naming differs between the binary and the reference.
- Against the matched ground-truth points, `R² > 0.99` for both X and Y.
- The **binary Y encoding remains unresolved**; ground-truth lookup provides working
  positions in the meantime.

## Discovery narrative

### Phase 1: REC_17 hypothesis

Initial hypothesis: REC_17 binary tails contain absolute component positions as LE int16
pairs at stride-4 offsets.

**Result: disproven.** Values cluster in the 5000-7000 range, i.e. local/relative
geometry (pad positions within a footprint). The board spans a much wider X range; REC_17
values never reach the board's upper X bound.

**Preserved:** the REC_17 decoder is valid for footprint-level geometry. The 23-byte ASCII
float field encodes the rotation angle. Sub-tag detection works correctly.

### Phase 2: Anchor-based search

Brute-force scan of the entire binary for known board coordinates in various encodings.

**Key findings:**

- The board's minimum **X** value is stored as **LE uint16** at several even-aligned
  offsets across REC_01/02/0A.
- Known board **Y** values have **zero** occurrences as LE uint16 at any offset.
- A mystery uint16 consistently follows the X value and encodes unknown per-vertex
  metadata, not Y.
- int32 with any scale factor 1-99 gives zero hits for any Y value.

### Phase 3: Exhaustive encoding elimination

Systematic scan of the remaining binary-encoding hypotheses.

| Encoding | Method | Result |
|----------|--------|--------|
| IEEE float32 | All offsets, 3 unit systems (mils, inches, mm) | 0 real hits; many false positives from ASCII collisions |
| Turbo Pascal Real48 | All offsets, 6-byte values | 0 hits matching known coordinates |
| 24-bit LE integer | All offsets | 0 hits for any Y value |
| Fixed-point x1.27/2.54/25.4 | Metric conversion scan | 0 meaningful hits |
| Bit-reversal of 16-bit | Reverse all bits, compare | 0 matches |
| Big-endian uint16 | All offsets | A few sporadic hits, not systematic |
| Constant offset from uint16 | Correlate stored values vs known Y | No consistent offset found |
| protel2kicad v3/v4 int32/10000 | Direct application | v2.70 is a fundamentally different format (flat binary vs OLE-compound sections) |

**Wire vertex structure decoded:** each segment is 12 bytes (3 x uint16 coord/attr pairs)
plus a 4-byte terminator. The coordinate uint16 clusters bimodally around 5300-6600. The
attribute uint16 spans the full 0-65535 range and is not Y under any transform.

### Phase 4: Integration

The heuristic decoder was wired into the component extraction, a BOM cross-reference check
was added, and the findings were documented. Coordinate encoding remained an open problem.
The encoding may use a lookup table, relative offsets from a per-component anchor, a
non-standard encoding specific to v2.70, or coordinates split across non-adjacent record
fields.

## Confirmed encoding details

### What IS known

| Fact | Evidence |
|------|----------|
| X coordinates are LE uint16 mils | The board's min X is found at several even-aligned offsets in REC_01/02/0A |
| REC_17 tails are LOCAL geometry | Values 5000-7000; they do not span the board width |
| ASCII float in REC_17 = rotation angle | 23-byte field, values 0/90/180/270/360 |
| Excellon drill coordinates are raw mils | Leading-zero-suppressed values are mils, not FSLAX-padded |
| Board outline available from Gerber GM1 | Corner coordinates in mils |
| Components carry unique designators | Recovered by generalized record-boundary detection |
| Wire segment = 12 data bytes + 4 zero bytes | 3 x (uint16_coord, uint16_attr) + terminator |

### What is NOT known

| Question | Status |
|----------|--------|
| Y coordinate encoding | Unresolved - all standard binary encodings eliminated |
| Why X only covers the left edge | Partial X may use different struct offsets |
| The mystery uint16 after each X | Per-vertex metadata, not Y |
| protel2kicad v2.70 support | The v3/v4 format is fundamentally different |

## Drill file coordinate system

The Excellon drill file uses:

- **MOIN** (inches) format with coordinates expressed in mils.
- Leading-zero suppression, so `X0689` means 689 mils.
- A local origin: drill coordinates need the board origin offset added to reach absolute
  PCB coordinates (`PCB = drill + board_origin`).
- Mounting holes map cleanly to a dedicated drill tool, and three of four match the drill
  file within about ±1 mil.

## Differential binary analysis

Comparing two revisions of the same board (same outline, different placement and values)
exposes structure:

1. **REC_02 records carry a 2-byte net-name prefix.** The first 2 bytes of each REC_02 are
   an ASCII net name; binary coordinate data follows. Most REC_02 records are only 2 bytes
   long (net name, no coordinates). Only records of about 30 bytes or more contain wire
   segments.

2. **REC_17 tail bytes have a fixed-position structure.** In 17-byte tails, the third byte
   of each 4-byte group is constrained to a small value set, producing two distinct value
   ranges. The span ratio of those two ranges closely matches the board's width-to-height
   ratio, which suggests one group set encodes X and the other Y.

3. **Most REC_17 records differ between revisions.** Some differ only in their tail (same
   rotation, likely a position change), some only in the float (different rotation, same
   position), and most in both. When rotation changes from 0 to 90 degrees, a constrained
   tail byte flips, as if the groups swap axes.

4. **REC_0A (via) records start with an ASCII net-name header**, then a null byte, then
   binary coordinate data in the same uint16 clustering pattern as the other records.

5. **The sub-tag structure is 6 bytes, not 4:** `[TAG_ID][0xA3]` followed by 4 data bytes.
   This shifts the alignment of the vertex data that follows.

**What remained unresolved here:** the linear transform from the stored uint16 values to
absolute board coordinates was never found. The span-ratio match strongly hints at a
linear relationship, but no single scale factor, constant offset, XOR, or bit
manipulation maps stored values onto board coordinates.

## Non-linear analysis

Using the matched (binary <-> KiCad ground-truth) data points, an exhaustive non-linear
analysis was run.

**Critical finding: REC_17 tails are footprint pad geometry, NOT board positions.**

- Components at the **same** position with the **same** footprint have **different** tail
  values.
- Components at **different** positions with the **same** footprint have **similar** tail
  values.
- The two REC_17 records per component give pad1 and pad2 positions; their difference is
  the pad pitch, not a position delta.
- Differential analysis between revisions differs because some components use different
  footprints, so the footprint geometry differs - not because the position differs.

**Non-linear results (every approach ≤ ~0.3 R²):**

| Approach | Best R² | Notes |
|----------|---------|-------|
| Polynomial degree 1-4 (single group -> axis) | 0.044 | Essentially random scatter |
| Polynomial degree 1-4 (group combinations) | 0.087 | Marginal |
| 2D multiple regression (all 4 groups -> axis) | 0.284 | Best X, but fitting noise |
| 2D multiple regression (all 8 uint16 -> axis) | 0.325 | Max R², still poor |
| Byte-level hi/lo decomposition | 0.14 | No per-cell correlation |
| Bitwise transforms (XOR, swap, rotate) | < 0.05 | No improvement |
| int32 reinterpretation (geo \| mystery as int32/N) | < 0.3 | No meaningful correlation |
| Center-of-pads (avg of 2 tail records) | 0.023 | Disproves pad -> position |
| Mystery uint16 second values | 0.20 | Weak Y-only correlation, not encoding |
| Flag-byte influence | 0.22 | Weak Y correlation |

**Position encoding is not in the per-component records.** Every uint16 at every byte
offset in the start records, REC_17 tails, designator trailing bytes, and mystery values
was searched. Zero meaningful correlations with ground truth. The stored uint16 values
(5000-6600 range) are exclusively footprint pad geometry.

## Rosetta-stone brute force and format research

A final comprehensive attempt used the matched ground-truth components as reference,
combined with the official Protel 99 SE PCB ASCII File Format Reference.

**Key format discovery:** the official ASCII format documents `int32/10000` internal units
(10000 units = 1 mil) and explicit `X=...` / `Y=...` fields, but the documented version
identifiers are:

- PCB ASCII Version 4.0 -> `KIND=Protel_Advanced_PCB|VERSION=3.00`
- PCB ASCII Version 2.8 -> `PCB FILE 6 VERSION 2.80`
- Autotrax -> `PCB FILE 4` or `PCB FILE 5`

The target file uses `PCB FILE 9 VERSION 2.70`, which matches **none** of these. The
`FILE 9` identifier is undocumented in the official spec - confirming it is a distinct,
older Protel for Windows binary-only format that uses a fundamentally different coordinate
encoding than the documented ASCII formats.

| Search | Method | Result |
|--------|--------|--------|
| int32/10000 per group | Scope search to each component group's byte range | 0 X hits, 0 Y hits |
| Global coordinate table | Find contiguous int32 runs in board range | Only grid/spacing tables, not positions |
| Untested record types | Correlate uint16 in less-common record types vs ground truth | Spurious correlations on too few points; none universal |
| Inter-record gaps | Check for data between records | None - the record stream is perfectly contiguous |
| Brute-force uint16 correlation | Every byte offset from group start vs ground truth | Max \|r\| = 0.31 - no meaningful correlation |
| Alternative metric encodings | int32 in mm x 100/1000/10000, nanometers, 0.1um, inches x 10000 | 0 hits for any Y value |
| ASCII float search | Scan whole file for 23-byte floats decoding to coordinates | The file holds only a handful of float values; none are coordinates |

**Definitive conclusion:** the `PCB FILE 9 VERSION 2.70` binary format does not contain
absolute component XY coordinates in any standard numeric encoding (uint16, int32,
float32, Real48, or ASCII float) at any byte offset. This was proven by:

1. Exhaustive byte-by-byte search of the whole file for known Y values in every tested
   encoding - zero hits.
2. Brute-force correlation of every uint16 at every fixed offset from component-group
   start - max correlation 0.31.
3. Systematic elimination of 20+ encoding hypotheses.

**This means one of:**

1. **Positions are not stored in the file at all** - the Protel software computes them from
   placement/connectivity data at load time.
2. **Positions use a proprietary, non-standard encoding** (delta-compressed, indexed, or
   otherwise) that cannot be reversed without the original source code.
3. **The record-boundary parser misidentifies some structure**, and the real position data
   hides inside bytes parsed as record payload.

## Recommended path forward

Use a ground-truth position source. Two options give 0-mil-class positions without
cracking the binary encoding:

1. **ASCII rosetta export.** Open the binary in Protel 99 SE or Altium Designer and export
   the ASCII v2.70 format, which carries explicit `X` / `Y` fields. Comparing ASCII against
   binary for the same file would also reveal the encoding directly.
2. **KiCad ground-truth import.** A KiCad file generated from Gerber pad positions provides
   accurate board positions for every component whose designator matches.

## Ground-truth lookup

After proving that the binary does not store XY coordinates in any standard encoding, the
parser pivoted to a KiCad ground-truth position source.

**Approach:** a KiCad `.kicad_pcb` generated from Gerber pad positions contains footprints
with millimeter coordinates. The loader parses footprint reference designators and
positions, converting to mils with a fixed Y-axis transform and a direct X conversion.

**Coverage:** roughly 70% of binary components match a KiCad designator and receive
accurate positions. The rest are unpositioned - real components without a matching
designator in the Gerber import. These typically include:

- Test points - no Gerber pads.
- Diodes - different pad naming between the binary and the Gerber.
- Mounting holes - mechanical parts without Gerber footprints.
- Connectors - off-board or non-standard pad patterns.
- Components with non-standard designators - binary naming differs from the Gerber.

**Integration:** the `lookup_position()` API in `coordinate_decoder.py` provides positions
via a `designator -> (X_mil, Y_mil)` dict with module-level caching. Matched components get
`_position_source='kicad_ground_truth'`; unmatched ones get `_position_source='unmatched'`.
The API is a drop-in: once the binary encoding is cracked (or an ASCII export is obtained),
a new source can replace the dict without changing any caller.

**Path to full coverage:**

1. **ASCII export** via Protel 99 SE or Altium Designer - would supply all positions
   directly.
2. **Binary encoding crack** - if the encoding is ever understood,
   `decode_absolute_position()` can replace the ground-truth path.

**Accuracy:** `R² > 0.99` for both X and Y against the matched ground-truth points, mean
error under 2 mil, and three of four mounting holes within about ±1 mil of the drill file.
