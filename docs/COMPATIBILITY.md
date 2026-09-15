# Protel and Altium file format compatibility with KiCad

Which legacy **Protel** and **Altium** PCB file formats can be converted to
modern **KiCad**, how, and how well. Protel shipped a new board format roughly
every product generation and all of them use the `.PCB` extension, so the
extension tells you nothing - the header does.

This page is kept current. If you have a file that is not covered, see
[Contributing a sample](#contributing-a-sample).

## Quick answer

| Your file says | Written by | Convert with | Status |
|---|---|---|---|
| `PCB FILE 9 VERSION 2.70` | Protel 99 SE | `protel-to-kicad` (this package) | **Full** |
| `PCB FILE 6 VERSION 2.80` | Protel for Windows / Protel 3 | `protel-to-kicad` (this package) | **Full** |
| `PCB 4.0 Binary File` | Protel Advanced PCB 3.00 | nothing yet | Partial research |
| `PCB FILE 5` / `PCB FILE 4` | Protel Autotrax / Easytrax | nothing yet | Publicly specified |
| `.PcbDoc` (OLE compound file) | Altium Designer | **KiCad's own importer** | Full, built into KiCad |

### How to tell which one you have

```bash
head -c 40 YOURBOARD.PCB | strings | head -1
```

Or let this package answer:

```bash
python -c "from protel99_parser import formats; from pathlib import Path; \
print(formats.identify(Path('YOURBOARD.PCB')))"
```

---

## Formats this package decodes

### `PCB FILE 9 VERSION 2.70` - Protel 99 SE

**Status: full.** Binary. Reads components with their own pad geometry, copper
on all layers, vias, arcs, fills, text, nets and the board outline. No
footprint library, no ASCII export, no ground-truth file.

The format is undocumented - it appears in no published Protel specification -
and was reverse engineered for this package. The byte-level specification is in
[`FORMAT.md`](FORMAT.md).

Verified against three independent witnesses (measured 2026-09-14):

| Witness | Result |
|---|---|
| Protel's own ASCII v2.70 export, two boards, all objects one to one | 100 % matched, median error 0.000 mil, all within 0.0009 mil |
| Gerbers Protel itself plotted, layer by layer | pads and vias within 1 mil, copper 100 %, every drill hit matched |
| KiCad's `pcbnew` loader reading the result back | positions within 0.0003 mil |
| The file's own object counters, 1178-board archive | 1170 clean, 8 salvaged from byte-level damage |

Board outline: Protel layer 29 (Mechanical 1).

### `PCB FILE 6 VERSION 2.80` - Protel for Windows

**Status: full.** Despite the name this format is **not binary** - the files are
plain ASCII, zero bytes outside 32..126 plus CR LF, measured across every
sample available. Records are a tag on its own line followed by operand lines,
and the file's second line holds the object counts.

Tags pair up by owner: `CT`/`FT` track, `CP`/`FP` pad, `CS`/`FS` text,
`CF`/`FF` fill, `CA`/`FA` arc, plus `FV` via and `NETDEF`. A `C*` object
belongs to the `COMP` most recently opened; an `F*` object stands alone.

Coordinates are in **1/1000 mil**, an order of magnitude coarser than
`PCB FILE 9`.

Verified on 11 boards (measured 2026-09-15):

| Check | Result |
|---|---|
| The file's own eight object counters | 9 of 11 agree on all eight |
| The two that do not | one off by a single text; one is byte-damaged and loses 3 records to salvage |
| KiCad's `pcbnew` loader | reads all 11, pad count matches the header on every one |

Board outline: Protel layer 28 (Keep-Out), **not** 29. Override with
`--outline-layer` if your boards differ.

**Caveat worth stating plainly.** No sample of this format has come with a
gerber set or an ASCII export, so there is no witness independent of the
reader itself. Self-consistency is not accuracy. The coordinate scale in
particular is inferred, not measured: vias read 51181 and mounting pads
137795, which are 1.3 mm and 3.5 mm to five digits, and no other power of ten
puts a via near a millimetre. If you have a `PCB FILE 6` board **with** its
gerbers, that would settle it - see below.

---

## Formats recognised but not decoded

These are identified by header, named in error messages, and skipped by batch
runs with a count rather than in silence.

### `PCB 4.0 Binary File` - Protel Advanced PCB 3.00

**Status: partial research.** Not convertible today. What is known:

- About 24 % of the file is readable ASCII in `KEY=VALUE|` form, carrying the
  layer stack, design rules and tool configuration.
- The binary remainder is divided into named sections, each introduced by the
  same `[length][ascii]` primitive the rest of the family uses: `Nets`,
  `Components`, `Polygons`, `FromTos`, `Embeddeds`, `Arcs`, `Pads`, `Vias`,
  `Tracks`, `Texts`, `Fills`, `Violations`.
- Coordinates are `int32` little-endian in **1/10000 mil**, the same scale as
  `PCB FILE 9`. On a sample board the values run to a median of 1678 mil with
  34 % landing on whole mils, which is what a hand-drawn board on a grid looks
  like.
- The record layout is **not** solved. Coordinates are interleaved with other
  fields rather than adjacent, so a scan for consecutive coordinate runs finds
  nothing and no constant record stride has been established.

### `PCB FILE 5` and `PCB FILE 4` - Protel Autotrax / Easytrax

**Status: not implemented.** Unlike the formats above, these two *are*
publicly specified by the vendor. No sample has been available to implement
and test against.

---

## Altium `.PcbDoc` - use KiCad directly

**You do not need this package.** KiCad has shipped an Altium board importer
for years. In the GUI: **File > Import > Non-KiCad Board File**.

From Python, using KiCad's own interpreter:

```python
import pcbnew
board = pcbnew.PCB_IO_MGR.Load(pcbnew.PCB_IO_MGR.ALTIUM_DESIGNER, "board.PcbDoc")
pcbnew.PCB_IO_MGR.Save(pcbnew.PCB_IO_MGR.KICAD_SEXP, "board.kicad_pcb", board)
```

KiCad's importer also covers `ALTIUM_CIRCUIT_STUDIO`, `ALTIUM_CIRCUIT_MAKER`,
`PCAD`, `EAGLE`, `CADSTAR_PCB_ARCHIVE`, `EASYEDA`, `FABMASTER` and others.

![Altium PcbDoc opened in KiCad](images/altium-pcbdoc-in-kicad.png)

*A Texas Instruments PGA309 reference design, `.PcbDoc` imported into KiCad's
PCB Editor by KiCad's own Altium importer - the route this table recommends for
that generation. Not produced by this package.*

Verified on that board (measured 2026-09-15). Pads were broken down by type in
the imported board to predict how many flashes each copper layer should carry,
then compared against the gerbers the original toolchain plotted years earlier:

| | predicted from the import | measured in the original gerbers |
|---|---|---|
| top flashes | 63 SMD + 9 through + 22 vias = **94** | **94** |
| bottom flashes | 25 SMD + 9 through + 22 vias = **56** | **56** |

**Known failure.** One `.PcbDoc` sample makes KiCad 9's importer throw an
`IO_ERROR` that is not caught across the SWIG boundary, aborting the process
(exit code 134) rather than raising a Python exception. The file is a valid OLE
compound document with the same signature as files that import cleanly. If you
hit this, try the GUI importer - it is a different code path - and report the
file to KiCad, not here.

---

## Schematics

Out of scope for this package, which converts boards. For the record, the
Protel schematic binary announces itself as
`Protel for Windows - Schematic Capture Binary File Version 1.2 - 2.0` and uses
the same `[length][ascii]` primitive. It is not decoded here.

---

## Contributing a sample

The gap between "recognised" and "decoded" is almost always a missing sample.
What helps, in order:

1. **A board plus the gerbers the original program plotted from it.** This is
   worth more than anything else: it is a witness written by the vendor's own
   software, so it can prove a decode right rather than merely self-consistent.
2. **A board plus an ASCII export** of the same board from the original tool.
3. **A board alone.** Still useful - the object counters in the header make a
   real check - but it cannot establish accuracy.

Open an issue with the header line (`head -c 40 FILE.PCB | strings | head -1`)
and say which of the three you have. Do not attach files you are not free to
publish.

---

Last verified: 2026-09-15.
