# protel99-to-kicad

Convert legacy Protel 99 SE PCB files (`PCB FILE 9 VERSION 2.70`) to KiCad - no Protel or Altium needed.

The format was abandoned around 2003 and no current tool reads it. This is a
reverse-engineered, dependency-free Python parser that turns the binary into a
KiCad `.kicad_pcb`: components, copper, vias, and board outline.

## Why this exists

- `PCB FILE 9 VERSION 2.70` is the binary PCB format of **Protel for Windows** (Protel 99 SE,
  the predecessor of Altium Designer). The format was abandoned around 2003.
- No modern tool reads this specific binary. `protel2kicad` does not import it, and
  Altium Designer does not import it either.
- The identifier `PCB FILE 9 VERSION 2.70` does not appear in any official Protel
  specification. The published specs only document `PCB FILE 4` / `PCB FILE 5`
  (Autotrax / Easytrax), `PCB FILE 6 VERSION 2.80`, and `Advanced_PCB 3.00`. The
  `FILE 9` identifier is undocumented.

## What it does

- Pure-Python parser using only the standard library. **Zero runtime dependencies.**
  `matplotlib` is pulled in only through the optional `[plot]` extra.
- Extracts components, designators, footprints, values, and pad shapes from the binary.
- Maps extracted footprints onto KiCad footprints.
- Generates a `.kicad_pcb` file containing:
  - components with their pads,
  - copper tracks on `F.Cu` and `B.Cu`,
  - through-hole vias,
  - the board outline on `Edge.Cuts`.

## Installation

```bash
pip install -e .
```

- Requires Python **3.10+**.
- Installs the `protel99-parse` CLI entry point.
- Optional plotting support: `pip install -e .[plot]`.
- Licensed under the MIT License.

## Usage

Basic conversion:

```bash
protel99-parse board.PCB -o board.kicad_pcb
```

### CLI flags

| Flag | Description |
|------|-------------|
| `-o`, `--output PATH` | Output `.kicad_pcb` file path |
| `--ascii-rosetta PATH` | ASCII v2.70 export used for component positions and copper (best quality) |
| `--pcblib PATH` | Footprint library to load. Repeatable - pass once per library |
| `--position-stats` | Print position-source breakdown and copper track counts |
| `--stats` | Print file-structure statistics |
| `--components` | Print the component list |
| `--json` | Emit component data as JSON |
| `--raw` | Dump raw record data |
| `--plot` | Plot the board layout |
| `--plot-copper` | Plot the copper layers |
| `--save-plot PATH` | Save the plot to a file instead of displaying it |

### Examples

```bash
# Best quality: pair the binary with its ASCII v2.70 export (positions + copper)
protel99-parse board.PCB --ascii-rosetta board.ascii.PCB -o board.kicad_pcb

# Binary only: components and outline, no copper tracks
protel99-parse sample.PCB -o sample.kicad_pcb

# Inspect where each component position came from
protel99-parse board.PCB --ascii-rosetta board.ascii.PCB --position-stats
```

## Position priority chain

Component positions are resolved through a five-level chain, trying each source in
order until one succeeds:

| Level | Source | Accuracy |
|-------|--------|----------|
| 1 | Explicit ASCII rosetta export (`--ascii-rosetta`) | 0 mil error, sub-mil precision |
| 2 | Auto-discovered ASCII rosetta export | 0 mil error, sub-mil precision |
| 3 | Explicit KiCad ground-truth reference | ground-truth quality |
| 4 | Auto-discovered KiCad ground-truth reference | ground-truth quality |
| 5 | Binary-only pad-centroid fallback | ~126 mil mean error |

Each component carries a `_position_source` attribute recording which level was used:

- `ascii_exact` - from an ASCII rosetta export (levels 1-2).
- `kicad_ground_truth` - from a KiCad reference file (levels 3-4).
- `binary_pad_centroid` - computed from pad geometry in the binary (level 5).
- `unmatched` - no source matched; position defaults to `(0, 0)`.

## Format internals

A short summary follows. Full byte-level notes are in [`docs/FORMAT.md`](docs/FORMAT.md)
and the coordinate analysis is in [`docs/COORDINATES.md`](docs/COORDINATES.md).

- **Header.** Offset `0x00` holds the marker `0x17 0xA1`. Offset `0x02` holds the
  null-terminated ASCII string `PCB FILE 9 VERSION 2.70`.
- **Record format.** Every record starts with `[TYPE][0xA1]`, where `TYPE` runs from
  `0x01` to `0x20`. Records are variable-length; a record ends where the next marker
  begins.
- **Record types.** About 24 types were identified by reverse engineering, including:
  `0x01` pin/pad, `0x02` track, `0x03` designator/value, `0x06` component name,
  `0x08` pad shape, `0x0A` via, `0x17` local geometry (carries an ASCII float rotation
  angle), and `0x20` polygon.
- **Floats.** Stored as 23-byte ASCII scientific notation. Used for rotation angles.

## Coordinates (key limitation)

The format stores geometry on two layers:

1. **Local footprint geometry** lives in the tails of `REC_17` records - the offsets of
   pads inside a footprint definition. This layer is **fully decoded**.
2. **Absolute, board-level positions.** Here the **X** coordinate is confirmed to be a
   little-endian `uint16` in mils. The **Y** coordinate is **unsolved**.

The Y encoding resisted exhaustive analysis. More than 20 candidate encodings were
eliminated, including LE/BE `int16`/`uint16`, `int32` with scale factors, `int32/10000`,
IEEE `float32`, Turbo Pascal `Real48`, 24-bit integers, and fixed-point variants. A
brute-force correlation of every `uint16` at every offset against known coordinates
peaked at `|r| = 0.31`.

The conclusion: absolute XY positions are most likely not stored in any standard
encoding. They are either computed by the Protel software at load time or held in a
proprietary encoding. This is why the ASCII rosetta export (levels 1-2 above) is the
recommended position source.

## Limitations

- **Copper tracks and vias require the ASCII rosetta export.** Binary-only conversion
  yields components and the board outline but **zero copper tracks**.
- **Position coverage is about 70% without a rosetta export**, using the pad-centroid
  fallback (~126 mil mean error).
- **Some designators stay unmatched** - typically test points, diodes, and mounting
  holes whose naming differs between sources.
- **The Y-coordinate binary encoding is unsolved.** See the coordinates section above.

## Testing

```bash
pip install -e .
pytest
```

The suite consists of unit tests run against synthetic data. Integration tests that
would require real boards are intentionally not bundled with the repository.

## License

MIT. See [`LICENSE`](LICENSE).
