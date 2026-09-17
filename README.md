# protel99-to-kicad - convert legacy Protel PCB files to KiCad

Open old **Protel** `.PCB` board files in **KiCad**. No Protel, no Altium, no
licence, no Windows. Pure Python, zero runtime dependencies.

Protel shipped a new board format roughly every product generation and gave
them all the same `.PCB` extension, so the extension tells you nothing. This
package reads the header, picks the right decoder, and writes a KiCad 9
`.kicad_pcb` with components, copper tracks, vias, arcs, fills, text, nets and
the board outline - **from the board file alone**, with no footprint library
and no ASCII export. Six generations of the format go in and one KiCad board
comes out; the writer is never told which one it was handed.

| Your file says | Written by | Status |
|---|---|---|
| `PCB FILE 4` | Protel Autotrax | **converts** |
| `PCB FILE 5` | Protel Easytrax | **converts** |
| `PCB FILE 9 VERSION 2.70` | Protel for Windows / Advanced PCB | **converts** |
| `PCB FILE 6 VERSION 1.10` / `2.70` / `2.80` | Protel PCB ASCII export | **converts** |
| `\|RECORD=Board\|...` | Protel 98 / 99 / 99 SE, saved as PCB ASCII | **converts** |
| `.ddb` design database | Protel 99 / 99 SE | **converts** the boards inside it this package can read |
| `PCB FILE 9 VERSION 2.00` / `2.60` | Protel for Windows 2.x | recognised, not decoded |
| `PCB 4.0 Binary File` | Protel 98 / 99 / 99 SE | recognised, not decoded - [save it as PCB ASCII](docs/COMPATIBILITY.md#protel-pcb-ascii---the-way-out-of-the-binary) |
| `PCB 3.0 Binary File`, `DOS 3 PCB` | Protel Advanced PCB 3, Protel for DOS | recognised, not decoded |
| `.PcbDoc` | Altium Designer | use KiCad's own importer - [how](docs/COMPATIBILITY.md#altium-pcbdoc---use-kicad-directly) |

Full detail, accuracy figures and caveats: **[docs/COMPATIBILITY.md](docs/COMPATIBILITY.md)**.

## Install

```bash
pip install -e .
```

Python 3.10 or newer. Standard library only - `matplotlib` is pulled in solely
by the optional `[plot]` extra.

## Use

```bash
protel-to-kicad board.PCB -o board.kicad_pcb       # one board, format auto-detected
protel-to-kicad --batch archive/ converted/        # every readable board under archive/
```

A mixed directory needs no sorting first. Boards in a format that is
recognised but not decoded are reported by name and counted, not skipped in
silence:

```
ok    analog-front.PCB  [pcb9]  components 108 segments 1402 vias 138 nets 106
ok    z80-cpu.PCB       [pcb4]  components  52 segments  594 vias  29 nets   0
ok    filter-rev2.pcb   [pcb6]  components  97 segments  585 vias  11 nets  65
ok    psu-board.pcb     [pcb6]  components 136 segments  799 vias  17 nets  89  SALVAGED (3 errors)
ok    tag104.ddb        [ddb ]  components  60 segments 3282 vias  60 nets  32
batch: 18 converted, 0 failed
       1 skipped: PCB 4.0 Binary File not decoded yet
```

### Which format is this file?

```bash
head -c 40 board.PCB | strings | head -1
```

### A whole project in a `.ddb`

A design database holds the project, not a board, so look inside it first:

```bash
protel-to-kicad project.ddb --list                     # what is in there
protel-to-kicad project.ddb --document 0 -o out.kicad_pcb
protel-to-kicad project.ddb -o out.kicad_pcb           # the largest board in it
```

Most Protel 99 SE projects store their boards as `PCB 4.0 Binary File`, which
is not decoded. When that is what is inside, the message says so and says what
to do about it. Detail: [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md#ddb-design-databases---partial).

### Flags

| Flag | Description |
|------|-------------|
| `-o`, `--output PATH` | Output `.kicad_pcb` path |
| `--batch IN_DIR OUT_DIR` | Convert every readable board under `IN_DIR`, mirroring the tree |
| `--outline-layer N` | Protel layer holding the board outline. Default follows the format: 29 (Mechanical 1) for `PCB FILE 9` and PCB ASCII, 28 (Keep Out) for `PCB FILE 6` and Autotrax |
| `--list` | List the documents inside a `.ddb` design database and stop |
| `--document N` | Which board to take out of a `.ddb`, by index from `--list` or by format name. Default is the largest board in it |
| `--hide-designators` | Write designators as hidden fields, as Protel's silkscreen plots show them |
| `--quiet` | Suppress the per-board summary |

## Why this exists

`PCB FILE 9 VERSION 2.70` is a binary board format from the Protel for Windows
and Advanced PCB generation - the line that became Altium Designer. It was
abandoned around 2003 and **no current tool reads it** - not Altium Designer,
not `protel2kicad`, not KiCad. The identifier appears in no published Protel
specification: the vendor documented the ASCII formats and stopped there.

So boards drawn in the 1990s are readable only by software nobody can license
or run any more. This package decodes the bytes instead. The specification
recovered from them is in [`docs/FORMAT.md`](docs/FORMAT.md).

The text formats are a different problem and get a different answer. `PCB FILE
4` and `PCB FILE 5` were specified by the vendor and are implemented here from
that specification. `PCB FILE 6` and the later `|RECORD=|` PCB ASCII are what
Protel writes when asked for text, and reading them is what lets a board from a
generation whose **binary** is undecoded reach KiCad anyway: open it in Protel,
save as PCB ASCII, convert that.

## Accuracy

`PCB FILE 9` is checked against three witnesses, none written by the code being
checked. All measured 2026-09-14; commands in [`docs/FORMAT.md`](docs/FORMAT.md).

**Protel's own ASCII v2.70 export** of two boards, all objects compared one to
one, in mils: components, pads (position and the size on each copper side),
tracks, vias, arcs and fills 100 % matched, median error 0.000 mil, every
coordinate within 0.0009 mil.

**The gerbers Protel itself plotted** for those boards, layer by layer: pads
and vias within 1 mil, copper 100 %, every drill hole matched with the tool
diameter Protel wrote.

**KiCad's own `pcbnew` loader**, reading the result back: pad, footprint-anchor
and via positions within 0.0003 mil of the binary.

**The file's own object counters**, across a 1178-board archive with no export
of any kind: 1170 parse clean with zero count mismatches and zero unknown tags;
8 are damaged at byte level and are read in salvage mode, which reports every
resynchronisation with its offset.

**Boards published by other people.** 140 boards collected from vendor
installers on archive.org and from 35 unrelated GitHub repositories, none of
them written by anyone involved here, were converted and then drawn by
`kicad-cli` from the converted file (measured 2026-09-17):

| Format | Boards | Clean | Salvaged | Failed |
|---|---|---|---|---|
| `PCB FILE 4` / `PCB FILE 5` | 95 | 95 | 0 | 0 |
| `PCB FILE 6` (1.10 and 2.80) | 21 | 21 | 0 | 0 |
| `PCB FILE 9 VERSION 2.70` | 7 | 6 | 1 | 0 |
| `.ddb` design databases | 16 | 6 | 10 | 0 |
| Protel PCB ASCII | 1 | 1 | 0 | 0 |

Salvage means the reader lost records to damage and said so with a line number,
not that it guessed. The `.ddb` figure is high for a reason worth knowing: the
database writes its own page headers straight through the document stored
inside it.

Per-format figures and their caveats - including where a format has **no**
independent witness - are in [`docs/COMPATIBILITY.md`](docs/COMPATIBILITY.md).

## Not carried over

- **Polygon pours** are written as unfilled zone outlines. The vertex axis
  order is inferred from two files only.
- **Design rules** are not decoded.
- **Net connection records** are not decoded. Net membership comes from the
  per-object net tag, which is consistent across pads, tracks, vias, fills,
  arcs and texts; the net's own member list disagrees with those tags on part
  of its entries and looks like stale bookkeeping.
- **The designator-visibility flag** is not identified in the binary, which is
  why `--hide-designators` is a switch rather than a decode.

## Format internals

[`docs/FORMAT.md`](docs/FORMAT.md) is the decoded specification. In short, for
`PCB FILE 9`:

- **Not a record taxonomy.** The file is a serialisation stream. `[len][A1]` is
  a string of that length; `[tag][A3]` is an attribute written only when its
  value changes, in one global state shared by every object type.
- **Coordinates** are `[u16 hi][u16 lo]`, value `(hi * 65536 + lo) / 10000` mil,
  both words little-endian, high word first. Absolute, Y up.
- **Block filler.** The file is written in 4 KiB blocks padded with `00 A0`. A
  write unit is emitted only when more bytes remain in the block than it needs,
  so filler can land between any two fields - including between the two halves
  of what looks like a 32-bit value.

An earlier search for the coordinate encoding failed and concluded it was
unsolvable. It was not: the mistake was comparing stored values against
coordinates in mils without the 6.5536 scale, so the right numbers looked like
the wrong range. That search is kept in
[`docs/_archive/`](docs/_archive/COORDINATES-search-history-2026-06.md) as a
record of what was eliminated and why the eliminations missed.

## Adding a format

`src/protel99_parser/formats.py` is the dispatcher and the machine-readable
half of the compatibility table. A new format is a reader that returns the
shared `Board` model plus one row in `FORMATS` - not a branch at every call
site. The KiCad writer never asks which generation it was handed.

The gap between "recognised" and "decoded" is almost always a missing sample.
If you have boards in an undecoded format - especially **with the gerbers the
original program plotted from them**, which is a witness the decoder cannot
fake - see [Contributing a sample](docs/COMPATIBILITY.md#contributing-a-sample).

## Legacy pipeline (`protel99-parse`)

**Superseded. Use `protel-to-kicad`.** This entry point predates the decoded
format and is kept only so existing scripts keep running.

It cannot place anything from the binary alone: it needs footprint libraries
for geometry and an ASCII v2.70 export for positions and copper. Without a
rosetta export it resolves about 70 % of positions through a pad-centroid
fallback with a mean error around 126 mil, and emits **zero copper tracks**.
Those limits are properties of this pipeline, not of the format.

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

The suite covers the stream primitives, format detection and every ASCII
reader against synthetic data - the Autotrax and PCB ASCII fixtures are the
record examples printed in the vendors' own specifications. Reference-board tests read their directory from
`PCB9_REFERENCE_DIR` and skip cleanly when the boards are not present; real
boards are intentionally not bundled.

## License

MIT. See [`LICENSE`](LICENSE).

---

<sub>Keywords: Protel to KiCad converter, Protel 99 SE to KiCad, Autotrax to
KiCad, Easytrax to KiCad, open Protel PCB file, convert `.ddb` to KiCad,
Protel PCB ASCII, `PCB FILE 4`, `PCB FILE 5`, `PCB FILE 6 VERSION 2.80`,
`PCB FILE 9 VERSION 2.70`, `PCB 4.0 Binary File`, Protel Advanced PCB, Protel
for Windows, legacy PCB file conversion, EDA file format reverse engineering,
Altium PcbDoc to KiCad, convert old PCB files, `.PCB` file format.</sub>
