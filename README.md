# protel99-to-kicad

Convert legacy Protel 99 SE PCB files (`PCB FILE 9 VERSION 2.70`) to KiCad - no
Protel or Altium needed.

The format was abandoned around 2003 and no current tool reads it. This is a
reverse-engineered, dependency-free Python decoder that turns the binary into a
KiCad 9 `.kicad_pcb`: components with their pads, copper, vias, arcs, fills,
text, nets and the board outline, from the binary alone.

## Why this exists

- `PCB FILE 9 VERSION 2.70` is the binary PCB format of **Protel for Windows**
  (Protel 99 SE, the predecessor of Altium Designer). The format was abandoned
  around 2003.
- No modern tool reads this specific binary. `protel2kicad` does not import it,
  and Altium Designer does not import it either.
- The identifier `PCB FILE 9 VERSION 2.70` does not appear in any official
  Protel specification. The published specs only document `PCB FILE 4` /
  `PCB FILE 5` (Autotrax / Easytrax), `PCB FILE 6 VERSION 2.80`, and
  `Advanced_PCB 3.00`. The `FILE 9` identifier is undocumented.

## Quick start

```bash
pip install -e .

pcb9-to-kicad board.PCB -o board.kicad_pcb          # one board
pcb9-to-kicad --batch archive/ converted/           # every board under archive/
```

Requires Python **3.10+**. Zero runtime dependencies - standard library only.

### Flags

| Flag | Description |
|------|-------------|
| `-o`, `--output PATH` | Output `.kicad_pcb` path |
| `--batch IN_DIR OUT_DIR` | Convert every `PCB FILE 9` board under `IN_DIR`, mirroring the tree |
| `--outline-layer N` | Protel layer holding the board outline (default 29, Mechanical 1) |
| `--hide-designators` | Write designators as hidden fields, as Protel's silkscreen plots show them |
| `--quiet` | Suppress the per-board summary |

## What it produces

Everything below comes out of the binary. **No footprint library, no ASCII
export, no ground-truth file.**

- components with their own pad geometry, placed and rotated,
- copper tracks on `F.Cu`, `B.Cu` and the mid layers,
- through-hole vias,
- arcs, both copper and graphic,
- fills as zones,
- text,
- nets, where the file carries them,
- the board outline on `Edge.Cuts`.

Through pads keep Protel's three sizes (top, inner, bottom) as a KiCad 9
padstack. The solder mask opening is the pad plus 2 mil a side, matching
Protel's own mask plots.

## Accuracy

Three independent witnesses, none of them written by the code being checked.
All figures measured 2026-09-14; the commands that produce them are in
[`docs/FORMAT.md`](docs/FORMAT.md).

**Protel's own ASCII v2.70 export** of two boards, all objects compared one to
one, in mils: components, pads (position and the size on each copper side),
tracks, vias, arcs and fills 100 % matched, median error 0.000 mil, every
coordinate within 0.0009 mil. The export truncates to 1/1000 mil, which
accounts for the remainder.

**The gerbers Protel itself plotted** for those boards, compared layer by layer
- pads and vias one to one, copper as distance to the other side's copper body,
NC drill files hit by hit: pads and vias within 1 mil, copper 100 %, every
drill hole matched with the tool diameter Protel wrote. Two habits of Protel's
plotter are accounted for in the comparison rather than reproduced in the
output: a pad or track with no matching aperture is painted with a brush, and
the Mechanical 1 outline is appended to every plot, copper included.

**KiCad's own `pcbnew` loader**, reading the generated board back: pad,
footprint-anchor and via positions differ from the binary by at most
0.0003 mil.

**The file's own header**, which states the number of tracks, arcs, vias,
components, fills, pads, nets, members and connections. Across an archive of
1178 boards with no export of any kind: 1170 parse clean with zero count
mismatches and zero unknown tags; 8 are damaged at byte level and are read in
salvage mode, which reports every resynchronisation with its offset.

## Not carried over

- **Polygon pours** are written as unfilled zone outlines. The vertex axis
  order is inferred from two files only.
- **Design rules** are not decoded.
- **Net connection records** (4 x `u16`) are not decoded. Net membership comes
  from the per-object net tag, which is consistent across pads, tracks, vias,
  fills, arcs and texts; the net's own member list disagrees with those tags on
  part of its entries and looks like stale bookkeeping.
- **The designator-visibility flag** is not identified in the binary, which is
  why `--hide-designators` exists as a switch rather than being read per board.

## Format

[`docs/FORMAT.md`](docs/FORMAT.md) is the decoded specification: primitives,
block filler, attribute state, section grammar, record layouts, and the
verification behind each claim. In short:

- **Not a record taxonomy.** The file is a serialisation stream. `[len][A1]` is
  a string of that length, and `[tag][A3]` is an attribute written only when its
  value changes, in one global state shared by every object type.
- **Coordinates** are `[u16 hi][u16 lo]`, value `(hi * 65536 + lo) / 10000` mil,
  both words little-endian, high word first. Absolute, Y up.
- **Block filler.** The file is written in 4 KiB blocks padded with `00 A0`. A
  write unit is emitted only when more bytes remain in the block than it needs,
  so filler can land between any two fields - including between the two halves
  of what looks like a 32-bit value.
- **Floats** are ASCII scientific notation, used for rotation angles.

An earlier search for the coordinate encoding failed and concluded it was
unsolvable. It was not: the mistake was comparing stored values against
coordinates in mils without the 6.5536 scale, so the right numbers looked like
the wrong range. That search is kept in
[`docs/_archive/`](docs/_archive/COORDINATES-search-history-2026-06.md) as a
record of what was eliminated and why the eliminations missed.

## Legacy pipeline (`protel99-parse`)

**Superseded. Use `pcb9-to-kicad`.** This entry point predates the decoded
format and is kept only so existing scripts keep running.

It cannot place anything from the binary alone: it needs footprint libraries for
geometry and an ASCII v2.70 export for positions and copper. Without a rosetta
export it resolves about 70 % of positions through a pad-centroid fallback with
a mean error around 126 mil, and emits **zero copper tracks**. Those limits are
properties of this pipeline, not of the format.

```bash
protel99-parse board.PCB --ascii-rosetta board.ascii.PCB -o board.kicad_pcb
```

| Flag | Description |
|------|-------------|
| `-o`, `--output PATH` | Output `.kicad_pcb` file path |
| `--ascii-rosetta PATH` | ASCII v2.70 export used for component positions and copper |
| `--ground-truth PATH` | KiCad reference board used for positions when no rosetta export is available |
| `--pcblib PATH` | Footprint library to load. Repeatable |
| `--position-stats` | Print position-source breakdown and copper track counts |
| `--stats` | Print file-structure statistics |
| `--components` | Print the component list |
| `--json` | Emit component data as JSON |
| `--raw` | Dump raw record data |
| `--plot` | Plot the board layout |
| `--plot-copper` | Plot the copper layers |
| `--save-plot PATH` | Save the plot to a file instead of displaying it |

Optional plotting support: `pip install -e .[plot]`.

## Testing

```bash
pip install -e .
pytest
```

The suite covers the stream primitives against synthetic data. Reference-board
tests read their directory from `PCB9_REFERENCE_DIR` and skip cleanly when the
boards are not present; real boards are intentionally not bundled.

## License

MIT. See [`LICENSE`](LICENSE).
