"""
Load component positions, copper, and board outline from a KiCad .kicad_pcb file.

Parses S-expression format using regex (pure Python, no deps).
Returns designator->(X_mil, Y_mil) dict using the Gerber coordinate transform.

Coordinate transform (Gerber Y-axis transform):
  X_mil = X_kicad_mm / 0.0254
  Y_mil = 9420.0 - (Y_kicad_mm - 173.2788) / 0.0254

This module is the ground truth source for component positions, copper tracks/vias,
and board outline until the binary encoding in Protel 99 SE v2.70 is cracked.
"""

import re
import sys
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Y-axis transform constants
Y_GERBER_TOP = 9730.0
Y_KICAD_ORIGIN_MM = 173.2788
MM_TO_MIL = 1.0 / 0.0254


def _kicad_mm_to_mils(x_mm: float, y_mm: float) -> tuple[float, float]:
    """Convert KiCad mm coordinates to Gerber mils using the Y-axis transform."""
    x_mil = x_mm * MM_TO_MIL
    y_mil = Y_GERBER_TOP - (y_mm - Y_KICAD_ORIGIN_MM) * MM_TO_MIL
    return (x_mil, y_mil)


def load_kicad_ground_truth(path: str | Path) -> dict[str, tuple[float, float]]:
    """Parse a .kicad_pcb file and extract footprint positions.

    Args:
        path: Path to the .kicad_pcb file.

    Returns:
        Dict mapping designator (e.g. 'R1', 'U22') to (X_mil, Y_mil).
        Only includes footprints that have a valid reference designator
        matching the pattern [A-Z]+[0-9]+.

    Raises:
        FileNotFoundError: If the file doesn't exist.
        ValueError: If the file can't be parsed.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"KiCad file not found: {path}")

    content = path.read_text(encoding='utf-8')

    # Split on top-level footprint blocks
    # Each footprint starts with '  (footprint "...' at indent level 1
    fp_blocks = content.split('(footprint ')[1:]  # skip everything before first footprint

    if not fp_blocks:
        raise ValueError(f"No footprints found in {path}")

    result: dict[str, tuple[float, float]] = {}
    duplicates: dict[str, int] = {}
    total_fps = 0
    desig_pattern = re.compile(r'^[A-Z]+\d+$')

    for block in fp_blocks:
        total_fps += 1

        # Extract footprint-level (at X Y [rot]) - first occurrence before any (pad
        # The footprint position is at indent level ~4 spaces, before pad definitions
        at_match = re.search(r'\(at ([\d.+-]+) ([\d.+-]+)', block)
        if not at_match:
            continue

        # Extract reference designator from fp_text reference "REF"
        ref_match = re.search(r'fp_text reference "([^"]+)"', block)
        if not ref_match:
            continue

        ref = ref_match.group(1)

        # Only keep standard designators (R1, C1, U22, T1, etc.)
        if not desig_pattern.match(ref):
            continue

        x_mm = float(at_match.group(1))
        y_mm = float(at_match.group(2))
        x_mil, y_mil = _kicad_mm_to_mils(x_mm, y_mm)

        if ref in result:
            duplicates[ref] = duplicates.get(ref, 1) + 1
            # Keep first occurrence (matches Protel's component grouping behavior)
            continue

        result[ref] = (x_mil, y_mil)

    # Log diagnostics
    logger.info(
        "KiCad ground truth: %d footprints parsed, %d with valid designators, %d duplicates skipped",
        total_fps, len(result), sum(v - 1 for v in duplicates.values()) if duplicates else 0
    )
    if duplicates:
        logger.debug("Duplicate designators (first kept): %s", list(duplicates.keys()))

    # Log to stderr for observability
    print(
        f"[ground_truth] Loaded {len(result)} positions from {path.name} "
        f"({total_fps} total footprints)",
        file=sys.stderr
    )

    return result


def load_kicad_copper(path: str | Path) -> tuple[list, list]:
    """Parse track segments and vias from a KiCad .kicad_pcb file.

    Returns:
        (tracks, vias) where tracks is a list of Track objects and vias
        is a list of Via objects, both with coordinates in mils.
        Returns ([], []) for non-KiCad files or on parse failure.
    """
    from protel99_parser import Track, Via

    path = Path(path)
    if not path.exists():
        logger.warning("KiCad file not found for copper loading: %s", path)
        return ([], [])

    content = path.read_text(encoding='utf-8')

    # Segment regex - matches: (segment (start X Y) (end X Y) (width W) (layer "L") ...)
    seg_re = re.compile(
        r'\(segment\s+\(start\s+([\d.+-]+)\s+([\d.+-]+)\)\s*'
        r'\(end\s+([\d.+-]+)\s+([\d.+-]+)\)\s*'
        r'\(width\s+([\d.]+)\)\s*'
        r'\(layer\s+"([^"]+)"\)'
    )

    tracks = []
    for m in seg_re.finditer(content):
        x1_mm, y1_mm = float(m.group(1)), float(m.group(2))
        x2_mm, y2_mm = float(m.group(3)), float(m.group(4))
        width_mm = float(m.group(5))
        layer = m.group(6)

        x1, y1 = _kicad_mm_to_mils(x1_mm, y1_mm)
        x2, y2 = _kicad_mm_to_mils(x2_mm, y2_mm)
        width_mil = width_mm * MM_TO_MIL

        tracks.append(Track(x1=x1, y1=y1, x2=x2, y2=y2,
                            width=width_mil, layer=layer))

    # Via regex - matches: (via (at X Y) (size S) (drill D) (layers "L1" "L2") ...)
    via_re = re.compile(
        r'\(via\s+\(at\s+([\d.+-]+)\s+([\d.+-]+)\)\s*'
        r'\(size\s+([\d.]+)\)\s*'
        r'\(drill\s+([\d.]+)\)\s*'
        r'\(layers\s+"([^"]+)"\s+"([^"]+)"\)'
    )

    vias = []
    for m in via_re.finditer(content):
        x_mm, y_mm = float(m.group(1)), float(m.group(2))
        size_mm = float(m.group(3))
        drill_mm = float(m.group(4))
        layer1, layer2 = m.group(5), m.group(6)

        x, y = _kicad_mm_to_mils(x_mm, y_mm)
        size_mil = size_mm * MM_TO_MIL
        drill_mil = drill_mm * MM_TO_MIL

        vias.append(Via(x=x, y=y, drill=drill_mil, size=size_mil,
                        layers=(layer1, layer2)))

    logger.info("KiCad copper: %d segments, %d vias", len(tracks), len(vias))
    return (tracks, vias)


def load_kicad_board_outline(path: str | Path) -> list[tuple[float, float, float, float]]:
    """Parse board outline (Edge.Cuts gr_line segments) from a KiCad file.

    Returns:
        List of (x1_mil, y1_mil, x2_mil, y2_mil) tuples for each Edge.Cuts line.
        Returns [] for non-KiCad files or if no Edge.Cuts lines found.
    """
    path = Path(path)
    if not path.exists():
        logger.warning("KiCad file not found for outline loading: %s", path)
        return []

    content = path.read_text(encoding='utf-8')

    # gr_line regex - uses (stroke (width W) (type T)) format, filtered to Edge.Cuts
    outline_re = re.compile(
        r'\(gr_line\s+\(start\s+([\d.+-]+)\s+([\d.+-]+)\)\s*'
        r'\(end\s+([\d.+-]+)\s+([\d.+-]+)\)\s*'
        r'\(stroke\s+\(width\s+[^)]+\)\s*\(type\s+[^)]+\)\)\s*'
        r'\(layer\s+"Edge\.Cuts"\)'
    )

    edges = []
    for m in outline_re.finditer(content):
        x1_mm, y1_mm = float(m.group(1)), float(m.group(2))
        x2_mm, y2_mm = float(m.group(3)), float(m.group(4))

        x1, y1 = _kicad_mm_to_mils(x1_mm, y1_mm)
        x2, y2 = _kicad_mm_to_mils(x2_mm, y2_mm)

        edges.append((x1, y1, x2, y2))

    logger.info("KiCad board outline: %d Edge.Cuts lines", len(edges))
    return edges


def find_ground_truth_file(pcb_path: str | Path) -> Optional[Path]:
    """Auto-detect the KiCad ground truth file relative to a PCB file.

    Derives the expected KiCad filename from the input PCB stem:
      e.g. BOARD.PCB -> BOARD_smart.kicad_pcb

    Search order:
      1. ../kicad/<stem>_smart.kicad_pcb (relative to input PCB)
      2. ../<stem>_smart.kicad_pcb
      3. <stem>_smart.kicad_pcb (same dir as PCB)
      4. ../../*/kicad/<stem>_smart.kicad_pcb (project layout - sibling dirs)

    Returns:
        Path to the KiCad file, or None if not found.
    """
    def _safe_exists(p: Path) -> bool:
        try:
            return p.exists()
        except OSError:
            return False

    pcb_path = Path(pcb_path).resolve()
    pcb_dir = pcb_path.parent
    stem = pcb_path.stem
    kicad_name = f'{stem}_smart.kicad_pcb'

    candidates = [
        pcb_dir.parent / 'kicad' / kicad_name,
        pcb_dir.parent / kicad_name,
        pcb_dir / kicad_name,
    ]

    for candidate in candidates:
        if _safe_exists(candidate):
            return candidate

    # Project layout: check sibling dirs of parent.parent for kicad/
    # e.g. <project>/samples/*.PCB -> ../<board>/kicad/
    # Skip this scan at (or near) the filesystem root to avoid walking '/'.
    grandparent = pcb_dir.parent.parent
    if grandparent != grandparent.parent and _safe_exists(grandparent):
        try:
            for subdir in grandparent.iterdir():
                if subdir.is_dir():
                    candidate = subdir / 'kicad' / kicad_name
                    if _safe_exists(candidate):
                        return candidate
        except OSError:
            pass

    return None
