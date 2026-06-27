"""
Generate KiCad 9 .kicad_pcb files from parsed Protel 99 SE PCBData.

Converts mils->mm with Y-axis inversion, resolves footprints from parsed
PCBLIB libraries, and emits a complete S-expression PCB file with real
pad geometry and silk outlines.

Coordinate transform (inverse of the Gerber Y-axis transform):
  X_mm = X_mil * 0.0254
  Y_mm = 173.2788 + (9420.0 - Y_mil) * 0.0254

Pad positions within footprints are relative to the footprint origin -
they need mil->mm conversion with Y negation (to compensate for the
board-level Y inversion). Component rotation is also negated for the
same reason.
"""

import sys
import uuid
import logging
from pathlib import Path

from protel99_parser import PCBData, Component, Track, Via
from protel99_parser.pcblib_parser import parse_pcblib, FootprintData, PadInfo, LineInfo
from protel99_parser.footprint_generator import resolve_footprint
from protel99_parser.footprint_corrections import FOOTPRINT_ORIGIN_CORRECTIONS

logger = logging.getLogger(__name__)

# ============================================================
# Constants
# ============================================================

# Default board-extents fallback (used when the board outline is missing)
_DEFAULT_Y_GERBER_TOP = 9730.0
_DEFAULT_Y_KICAD_ORIGIN_MM = 173.2788
MIL_TO_MM = 0.0254

# Public aliases of the default values (stable API for tests and callers)
Y_GERBER_TOP = _DEFAULT_Y_GERBER_TOP
Y_KICAD_ORIGIN_MM = _DEFAULT_Y_KICAD_ORIGIN_MM

# Module-level state - set dynamically per board in generate_kicad_pcb()
_Y_GERBER_TOP = _DEFAULT_Y_GERBER_TOP
_Y_KICAD_ORIGIN_MM = _DEFAULT_Y_KICAD_ORIGIN_MM

# KiCad 9 file format version
KICAD_VERSION = 20241229

# Footprint names that are not real placed components (via/jumper pads and
# mounting holes) and should be skipped during PCB generation. These match
# the conventions in the source library; adjust for your own library.
_SKIP_FOOTPRINT_PREFIXES = ()  # footprint-name prefixes to skip (extend as needed)
_SKIP_FOOTPRINT_NAMES = frozenset({'PAD-RUBKI'})


def _configure_board_origin(pcb_data: 'PCBData'):
    """Compute Y-axis transform constants from board outline.

    Sets module-level _Y_GERBER_TOP and _Y_KICAD_ORIGIN_MM based on the
    board's actual extents. Falls back to the default extents if no outline.
    """
    global _Y_GERBER_TOP, _Y_KICAD_ORIGIN_MM

    if pcb_data.board_outline:
        ys = [y1 for x1, y1, x2, y2 in pcb_data.board_outline] + \
             [y2 for x1, y1, x2, y2 in pcb_data.board_outline]
        y_top = max(ys)
        # Add small margin above board top
        _Y_GERBER_TOP = y_top + 100
        _Y_KICAD_ORIGIN_MM = 0.0  # Place board near KiCad origin
        logger.info(
            "[generator] Board Y range: %.0f-%.0f, Y_GERBER_TOP=%.0f",
            min(ys), max(ys), _Y_GERBER_TOP,
        )
    else:
        _Y_GERBER_TOP = _DEFAULT_Y_GERBER_TOP
        _Y_KICAD_ORIGIN_MM = _DEFAULT_Y_KICAD_ORIGIN_MM
        logger.warning("[generator] No board outline - using default Y transform")


def _mils_to_kicad_mm(x_mil: float, y_mil: float) -> tuple[float, float]:
    """Convert Protel mils to KiCad mm with Y-axis inversion.

    Used for board-level positions (components, tracks, vias, board outline).
    NOT for pad positions within footprints - those use _pad_mil_to_mm().
    """
    x_mm = x_mil * MIL_TO_MM
    y_mm = _Y_KICAD_ORIGIN_MM + (_Y_GERBER_TOP - y_mil) * MIL_TO_MM
    return (x_mm, y_mm)


def _pad_mil_to_mm(val: float) -> float:
    """Convert a pad-local dimension from mils to mm (no Y inversion)."""
    return round(val * MIL_TO_MM, 4)


def _fmt(val: float) -> str:
    """Format a coordinate/dimension to 4 decimal places (KiCad convention)."""
    return f"{val:.4f}"


def _uuid() -> str:
    return str(uuid.uuid4())


# ============================================================
# Library auto-discovery
# ============================================================

def find_pcblib_libraries(pcb_path: Path) -> dict[str, dict[str, FootprintData]]:
    """Discover and load PCBLIB libraries from a protel-libs/ directory.

    Search order: same dir as pcb_path, parent, grandparent, sibling of parent.
    Loads all *.LIB files found. Earlier libraries (alphabetical) win on name conflicts.
    Returns {lib_name: {footprint_name: FootprintData}} or empty dict.
    """
    pcb_path = Path(pcb_path).resolve()
    search_dirs = [
        pcb_path.parent / "protel-libs",
        pcb_path.parent.parent / "protel-libs",
        pcb_path.parent.parent.parent / "protel-libs",
    ]
    # Sibling of parent: e.g. project_root/protel-libs when PCB is in project_root/samples/
    if pcb_path.parent.parent.exists():
        for sibling in pcb_path.parent.parent.iterdir():
            if sibling.is_dir() and sibling.name == "protel-libs":
                if sibling not in search_dirs:
                    search_dirs.append(sibling)

    lib_dir = None
    for candidate in search_dirs:
        if candidate.is_dir():
            lib_dir = candidate
            break

    if lib_dir is None:
        searched = [str(d) for d in search_dirs]
        logger.warning("[generator] No protel-libs/ directory found. Searched: %s", searched)
        return {}

    lib_files = sorted(lib_dir.glob("*.LIB"))
    if not lib_files:
        logger.warning("[generator] protel-libs/ found at %s but contains no *.LIB files", lib_dir)
        return {}

    # Sort alphabetically so conflict priority is deterministic
    # Entries loaded first win on merge
    libraries: dict[str, dict[str, FootprintData]] = {}
    total_fps = 0
    for lib_file in lib_files:
        try:
            fps = parse_pcblib(lib_file)
            libraries[lib_file.stem] = fps
            total_fps += len(fps)
        except Exception as e:
            logger.warning("[generator] Failed to parse library %s: %s", lib_file, e)

    logger.info(
        "[generator] Discovered %d libraries (%d footprints) in %s",
        len(libraries), total_fps, lib_dir,
    )
    return libraries


# ============================================================
# Footprint origin correction from Pick and Place data
# ============================================================

def _parse_pnp_origin_offsets(
    pnp_path: Path,
    libraries: dict[str, dict[str, FootprintData]],
) -> dict[str, tuple[float, float]]:
    """Compute per-footprint origin correction from Protel Pick and Place CSV.

    Protel's Pick and Place export gives Ref (reference point) and Mid (centroid)
    per component. The Protel reference point is where the component position
    (from text11) places the footprint. But PCBLIB footprints may have their
    origin at a different point than Protel's ref.

    This function computes, for each footprint pattern:
      correction = PCBLIB_centroid - PnP_ref_to_mid_local

    where ref_to_mid_local is the Ref->Mid vector de-rotated into the footprint's
    local coordinate frame using Protel's CW rotation convention.

    Returns: {footprint_name: (ox_mil, oy_mil)} to subtract from PCBLIB pad coords.
    """
    import csv
    import re
    import math
    from collections import defaultdict

    # Merge all library footprints
    all_fps: dict[str, FootprintData] = {}
    for lib_name, fps in libraries.items():
        for name, fp in fps.items():
            if name not in all_fps:
                all_fps[name] = fp

    # Parse PnP CSV
    ref_to_mid_local: dict[str, list[tuple[float, float]]] = defaultdict(list)
    try:
        with open(pnp_path, encoding='latin-1') as f:
            reader = csv.DictReader(f)
            for row in reader:
                des = row.get("Designator", "").strip()
                if not des:
                    continue
                pattern = row.get("Pattern", "").strip()
                if not pattern or pattern not in all_fps:
                    continue

                def _parse_mil(s: str) -> float | None:
                    m = re.match(r'([-\d.]+)mil', s.strip())
                    return float(m.group(1)) if m else None

                ref_x = _parse_mil(row.get("Ref X", ""))
                ref_y = _parse_mil(row.get("Ref Y", ""))
                mid_x = _parse_mil(row.get("Mid X", ""))
                mid_y = _parse_mil(row.get("Mid Y", ""))
                rot_str = row.get("Rotation", "0").strip()

                if any(v is None for v in (ref_x, ref_y, mid_x, mid_y)):
                    continue

                rot = float(rot_str)
                dx = mid_x - ref_x
                dy = mid_y - ref_y

                # Protel uses CW rotation. De-rotate global->local with CW convention:
                # local = rotate(global, +rot)
                rad = math.radians(rot)
                lx = dx * math.cos(rad) - dy * math.sin(rad)
                ly = dx * math.sin(rad) + dy * math.cos(rad)
                ref_to_mid_local[pattern].append((lx, ly))
    except Exception as e:
        logger.warning("[generator] Failed to parse PnP file %s: %s", pnp_path, e)
        return {}

    # Compute per-footprint correction
    from statistics import median
    corrections: dict[str, tuple[float, float]] = {}
    for pattern, items in ref_to_mid_local.items():
        if pattern not in all_fps or len(items) < 1:
            continue
        fp = all_fps[pattern]
        if not fp.pads:
            continue
        cx = sum(p.x for p in fp.pads) / len(fp.pads)
        cy = sum(p.y for p in fp.pads) / len(fp.pads)

        lx = median([i[0] for i in items])
        ly = median([i[1] for i in items])

        ref_x = cx - lx
        ref_y = cy - ly

        # Only apply correction if it's meaningful (> 1 mil)
        if abs(ref_x) <= 1.0 and abs(ref_y) <= 1.0:
            continue

        # Validate: correction should be within the footprint extent.
        # Reject if ref point is too far from centroid (garbage PnP data).
        pad_xs = [p.x for p in fp.pads]
        pad_ys = [p.y for p in fp.pads]
        extent = max(max(pad_xs) - min(pad_xs), max(pad_ys) - min(pad_ys), 100)
        dist = math.sqrt((ref_x - cx)**2 + (ref_y - cy)**2)
        if dist > extent * 3:
            logger.debug(
                "[generator] Rejected PnP correction for %s: dist=%.0f > 3×extent=%.0f",
                pattern, dist, extent,
            )
            continue

        corrections[pattern] = (ref_x, ref_y)

    logger.info(
        "[generator] PnP origin corrections: %d footprints from %s",
        len(corrections), pnp_path,
    )
    return corrections


def _find_pnp_file(pcb_path: Path) -> Path | None:
    """Auto-discover Protel Pick and Place CSV near the PCB file.

    Searches for PnP CSV in common export directory structures.
    """
    pcb_path = Path(pcb_path).resolve()
    candidates = []

    # Search nearby directories whose name suggests a Pick and Place export.
    for ancestor in [pcb_path.parent, pcb_path.parent.parent,
                     pcb_path.parent.parent.parent]:
        if ancestor.is_dir():
            for child in ancestor.iterdir():
                if child.is_dir() and 'picknplace' in child.name.lower():
                    for f in child.glob("*.PIK"):
                        candidates.append(f)
                    for f in child.glob("*.csv"):
                        candidates.append(f)

    if candidates:
        # Prefer the first match (most specific path)
        return candidates[0]
    return None


# ============================================================
# Emitters
# ============================================================

def _emit_header(f, pcb_data: PCBData):
    """Write KiCad PCB file header: version, general, layers, setup, nets."""
    f.write('(kicad_pcb\n')
    f.write(f'  (version {KICAD_VERSION})\n')
    f.write('  (generator "protel99-to-kicad")\n')
    f.write('  (generator_version "1.0")\n')
    f.write('  (general\n')
    f.write('    (thickness 1.6)\n')
    f.write('    (legacy_teardrops no)\n')
    f.write('  )\n')
    f.write('  (paper "A3")\n')

    # Layers - match reference file exactly
    f.write('  (layers\n')
    f.write('    (0 "F.Cu" signal)\n')
    f.write('    (31 "B.Cu" signal)\n')
    f.write('    (32 "B.Adhes" user "B.Adhesive")\n')
    f.write('    (33 "F.Adhes" user "F.Adhesive")\n')
    f.write('    (34 "B.Paste" user)\n')
    f.write('    (35 "F.Paste" user)\n')
    f.write('    (36 "B.SilkS" user "B.Silkscreen")\n')
    f.write('    (37 "F.SilkS" user "F.Silkscreen")\n')
    f.write('    (38 "B.Mask" user)\n')
    f.write('    (39 "F.Mask" user)\n')
    f.write('    (40 "Dwgs.User" user "User.Drawings")\n')
    f.write('    (41 "Cmts.User" user "User.Comments")\n')
    f.write('    (42 "Eco1.User" user "User.Eco1")\n')
    f.write('    (43 "Eco2.User" user "User.Eco2")\n')
    f.write('    (44 "Edge.Cuts" user)\n')
    f.write('    (45 "Margin" user)\n')
    f.write('    (46 "B.CrtYd" user "B.Courtyard")\n')
    f.write('    (47 "F.CrtYd" user "F.Courtyard")\n')
    f.write('    (48 "B.Fab" user)\n')
    f.write('    (49 "F.Fab" user)\n')
    f.write('  )\n')

    # Setup - minimal
    f.write('  (setup\n')
    f.write('    (pad_to_mask_clearance 0.05)\n')
    f.write('    (allow_soldermask_bridges_in_footprints no)\n')
    f.write('    (pcbplotparams\n')
    f.write('      (layerselection 0x00010fc_ffffffff)\n')
    f.write('      (plot_on_all_layers_selection 0x0000000_00000000)\n')
    f.write('    )\n')
    f.write('  )\n')

    # Nets
    for net_id in sorted(pcb_data.nets.keys()):
        net_name = pcb_data.nets[net_id]
        f.write(f'  (net {net_id} "{net_name}")\n')
    f.write('\n')


def _emit_board_outline(f, pcb_data: PCBData) -> int:
    """Write board outline as gr_line segments on Edge.Cuts layer."""
    count = 0
    for x1, y1, x2, y2 in pcb_data.board_outline:
        x1_mm, y1_mm = _mils_to_kicad_mm(x1, y1)
        x2_mm, y2_mm = _mils_to_kicad_mm(x2, y2)
        f.write(
            f'  (gr_line (start {_fmt(x1_mm)} {_fmt(y1_mm)}) '
            f'(end {_fmt(x2_mm)} {_fmt(y2_mm)}) '
            f'(stroke (width 0.1) (type solid)) '
            f'(layer "Edge.Cuts") (uuid "{_uuid()}"))\n'
        )
        count += 1
    f.write('\n')
    return count


def _emit_tracks(f, pcb_data: PCBData) -> int:
    """Write copper track segments."""
    count = 0
    for t in pcb_data.tracks:
        x1_mm, y1_mm = _mils_to_kicad_mm(t.x1, t.y1)
        x2_mm, y2_mm = _mils_to_kicad_mm(t.x2, t.y2)
        width_mm = t.width * MIL_TO_MM
        f.write(
            f'  (segment (start {_fmt(x1_mm)} {_fmt(y1_mm)}) '
            f'(end {_fmt(x2_mm)} {_fmt(y2_mm)}) '
            f'(width {_fmt(width_mm)}) '
            f'(layer "{t.layer}") '
            f'(net {t.net}) '
            f'(uuid "{_uuid()}"))\n'
        )
        count += 1
    return count


def _emit_vias(f, pcb_data: PCBData) -> int:
    """Write vias."""
    count = 0
    for v in pcb_data.vias:
        x_mm, y_mm = _mils_to_kicad_mm(v.x, v.y)
        size_mm = v.size * MIL_TO_MM
        drill_mm = v.drill * MIL_TO_MM
        layers = v.layers if v.layers else ('F.Cu', 'B.Cu')
        f.write(
            f'  (via (at {_fmt(x_mm)} {_fmt(y_mm)}) '
            f'(size {_fmt(size_mm)}) '
            f'(drill {_fmt(drill_mm)}) '
            f'(layers "{layers[0]}" "{layers[1]}") '
            f'(net {v.net}) '
            f'(uuid "{_uuid()}"))\n'
        )
        count += 1
    f.write('\n')
    return count


def _emit_fills(f, pcb_data: PCBData) -> int:
    """Write copper fill rectangles as KiCad zones with filled polygons."""
    count = 0
    for fill in pcb_data.fills:
        x1_mm, y1_mm = _mils_to_kicad_mm(fill.x1, fill.y1)
        x2_mm, y2_mm = _mils_to_kicad_mm(fill.x2, fill.y2)
        uid = _uuid()
        f.write(
            f'  (zone (net {fill.net}) (net_name "") '
            f'(layer "{fill.layer}") (uuid "{uid}")\n'
            f'    (hatch edge 0.5)\n'
            f'    (fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5))\n'
            f'    (polygon\n'
            f'      (pts\n'
            f'        (xy {_fmt(x1_mm)} {_fmt(y1_mm)})\n'
            f'        (xy {_fmt(x2_mm)} {_fmt(y1_mm)})\n'
            f'        (xy {_fmt(x2_mm)} {_fmt(y2_mm)})\n'
            f'        (xy {_fmt(x1_mm)} {_fmt(y2_mm)})\n'
            f'      )\n'
            f'    )\n'
            f'    (filled_polygon (layer "{fill.layer}")\n'
            f'      (pts\n'
            f'        (xy {_fmt(x1_mm)} {_fmt(y1_mm)})\n'
            f'        (xy {_fmt(x2_mm)} {_fmt(y1_mm)})\n'
            f'        (xy {_fmt(x2_mm)} {_fmt(y2_mm)})\n'
            f'        (xy {_fmt(x1_mm)} {_fmt(y2_mm)})\n'
            f'      )\n'
            f'    )\n'
            f'  )\n'
        )
        count += 1
    return count


def _emit_free_strings(f, pcb_data: PCBData) -> int:
    """Write free text strings on copper/silk layers."""
    count = 0
    for s in pcb_data.free_strings:
        x_mm, y_mm = _mils_to_kicad_mm(s.x, s.y)
        height_mm = s.height * MIL_TO_MM
        width_mm = s.width * MIL_TO_MM
        text = s.text.replace('"', '\\"')
        f.write(
            f'  (gr_text "{text}"\n'
            f'    (at {_fmt(x_mm)} {_fmt(y_mm)} {_fmt(s.rotation)})\n'
            f'    (layer "{s.layer}")\n'
            f'    (uuid "{_uuid()}")\n'
            f'    (effects\n'
            f'      (font (size {_fmt(height_mm)} {_fmt(height_mm)}) '
            f'(thickness {_fmt(width_mm)}))\n'
            f'    )\n'
            f'  )\n'
        )
        count += 1
    return count


def _emit_library_pads(f, fp_data: FootprintData, indent: str = "    ",
                       comp_layer: str = "F.Cu",
                       origin_offset: tuple[float, float] = (0.0, 0.0),
                       comp_rotation: int = 0):
    """Emit real pad definitions from parsed FootprintData.

    Pad positions are footprint-local (relative to origin). Convert mils->mm
    with Y negation.

    origin_offset: (ox_mil, oy_mil) subtracted from each pad position to shift
    the footprint origin to the Protel reference point. Computed from PnP data.
    comp_rotation: component rotation in degrees. For oval pads at 90°/270°,
    swap width/height so the Gerber aperture matches the rotated orientation.
    """
    ox, oy = origin_offset
    rot_swaps_wh = (comp_rotation % 180) == 90  # 90° or 270° swaps W/H
    for pad in fp_data.pads:
        x_mm = _pad_mil_to_mm(pad.x - ox)
        y_mm = _pad_mil_to_mm(-(pad.y - oy))
        w_mm = _pad_mil_to_mm(pad.width)
        h_mm = _pad_mil_to_mm(pad.height)

        # Ensure minimum pad size (avoid 0×0 pads from parse failures)
        if w_mm < 0.01:
            w_mm = 1.0
        if h_mm < 0.01:
            h_mm = 1.0

        pin = pad.pin if pad.pin else "1"

        if pad.layers == 'tht':
            pad_type = 'thru_hole'
            layers_str = '"*.Cu" "*.Mask"'
            drill_mm = _pad_mil_to_mm(pad.drill)
            drill_part = f' (drill {_fmt(drill_mm)})' if drill_mm > 0 else ''
        else:
            pad_type = 'smd'
            if comp_layer == "B.Cu":
                layers_str = '"B.Cu" "B.Paste" "B.Mask"'
            else:
                layers_str = '"F.Cu" "F.Paste" "F.Mask"'
            drill_part = ''

        shape = pad.shape
        if shape not in ('rect', 'circle', 'oval'):
            shape = 'rect'

        # For oval/rect pads: if footprint is rotated 90°/270°, swap W/H
        # so the Gerber aperture reflects the rotated orientation.
        # KiCad Gerber export doesn't rotate apertures - it uses the pad
        # dimensions as-is for flash commands.
        if rot_swaps_wh and shape in ('oval', 'rect') and abs(w_mm - h_mm) > 0.01:
            w_mm, h_mm = h_mm, w_mm

        # Per-pad rotation (from text11 CP records)
        pad_rot = pad.rotation if hasattr(pad, 'rotation') and pad.rotation else 0.0
        # Y-negation flips rotation sign
        if pad_rot:
            pad_rot = -pad_rot
        at_rot = f' {_fmt(pad_rot)}' if pad_rot else ''

        f.write(
            f'{indent}(pad "{pin}" {pad_type} {shape}\n'
            f'{indent}  (at {_fmt(x_mm)} {_fmt(y_mm)}{at_rot})\n'
            f'{indent}  (size {_fmt(w_mm)} {_fmt(h_mm)}){drill_part}\n'
            f'{indent}  (layers {layers_str})\n'
            f'{indent}  (uuid "{_uuid()}")\n'
            f'{indent})\n'
        )


def _emit_library_silk(f, fp_data: FootprintData, indent: str = "    ",
                       origin_offset: tuple[float, float] = (0.0, 0.0),
                       comp_layer: str = "F.Cu"):
    """Emit silk screen lines from parsed FootprintData."""
    ox, oy = origin_offset
    silk_layer = "B.SilkS" if comp_layer == "B.Cu" else "F.SilkS"
    for line in fp_data.lines:
        x1 = _fmt(_pad_mil_to_mm(line.x1 - ox))
        y1 = _fmt(_pad_mil_to_mm(-(line.y1 - oy)))
        x2 = _fmt(_pad_mil_to_mm(line.x2 - ox))
        y2 = _fmt(_pad_mil_to_mm(-(line.y2 - oy)))
        w = _fmt(_pad_mil_to_mm(line.width))
        f.write(
            f'{indent}(fp_line (start {x1} {y1}) (end {x2} {y2})'
            f' (stroke (width {w}) (type solid))'
            f' (layer "{silk_layer}") (uuid "{_uuid()}"))\n'
        )


def _emit_generic_pads(f, comp: Component, indent: str = "    "):
    """Fallback: generate generic pads when no library data available."""
    pin_count = max(comp.pin_count, 2)
    pad_type = "smd"
    layers_str = '"F.Cu" "F.Paste" "F.Mask"'
    pad_size = "1.0 1.0"
    drill = ''

    pitch_mm = 1.27  # 50 mil pitch

    if pin_count == 2:
        positions = [(0.0, 0.635), (0.0, -0.635)]
    else:
        total_span = (pin_count - 1) * pitch_mm
        start_y = -total_span / 2.0
        positions = [(0.0, start_y + i * pitch_mm) for i in range(pin_count)]

    for i, (px, py) in enumerate(positions, start=1):
        f.write(
            f'{indent}(pad "{i}" {pad_type} circle\n'
            f'{indent}  (at {_fmt(px)} {_fmt(py)})\n'
            f'{indent}  (size {pad_size}){drill}\n'
            f'{indent}  (layers {layers_str})\n'
            f'{indent}  (uuid "{_uuid()}")\n'
            f'{indent})\n'
        )


def _emit_footprints(
    f,
    pcb_data: PCBData,
    libraries: dict[str, dict[str, FootprintData]],
    origin_corrections: dict[str, tuple[float, float]] | None = None,
    text11_lib: dict[str, FootprintData] | None = None,
) -> tuple[int, int, int, int]:
    """Write component footprints with real library pad geometry.

    Returns (placed_count, skipped_count, resolved_count, fallback_count).
    """
    placed = 0
    skipped = 0
    resolved = 0
    fallback = 0

    for comp in pcb_data.components:
        # Skip unmatched components (x=0, y=0, invalid position).
        if not comp._position_source or comp._position_source in ('unmatched', 'none', ''):
            skipped += 1
            continue

        # Skip non-component footprints (via/jumper pseudo-pads and
        # mounting holes). These appear as copper pours/vias or board
        # features in Protel rather than as placed components. The names
        # below match the conventions seen in the source library; extend
        # _SKIP_FOOTPRINT_PREFIXES / _SKIP_FOOTPRINT_NAMES for your own.
        fp_raw = comp.footprint.strip() or comp.library_ref.strip()
        if (any(fp_raw.startswith(p) for p in _SKIP_FOOTPRINT_PREFIXES)
                or fp_raw in _SKIP_FOOTPRINT_NAMES):
            skipped += 1
            continue

        x_mm, y_mm = _mils_to_kicad_mm(comp.x, comp.y)
        rotation = int(comp.rotation) % 360

        # Resolve footprint: try text11 library first (correct origin, shapes, sizes),
        # then fall back to PCBLIB
        fp_data = None
        match_type = None
        fp_source = None  # 'text11' or 'pcblib'
        for name_candidate in (comp.footprint.strip(), comp.library_ref.strip()):
            if not name_candidate:
                continue
            # Try text11 library first (exact match)
            if text11_lib and name_candidate in text11_lib:
                fp_data = text11_lib[name_candidate]
                match_type = 'text11_exact'
                fp_source = 'text11'
                break
            # Try text11 with suffix stripping
            if text11_lib:
                import re as _re
                # Try stripping trailing digits/symbols, then trailing single char
                for pattern in [r'[0-9|]+$', r'[0-9A-Z|]+$', r'.$']:
                    base = _re.sub(pattern, '', name_candidate)
                    if base and base != name_candidate and base in text11_lib:
                        fp_data = text11_lib[base]
                        match_type = 'text11_suffix'
                        fp_source = 'text11'
                        break
                if fp_source == 'text11':
                    break
            # Fall back to PCBLIB
            fp_data, match_type = resolve_footprint(name_candidate, libraries)
            if fp_data is not None:
                fp_source = 'pcblib'
                break

        # Compensate for footprint origin offset.
        # Protel component position = pad 1 location (pin 1 or reference point).
        # Our parsed footprints have pad 1 at (0,0). KiCad places (at X Y) as
        # the footprint origin. So pad 1 will land exactly at the component position.
        # But all other pads will be offset by their footprint-local coordinates.
        # To center the component properly, we DON'T offset - the Protel position
        # IS the footprint origin (pad 1). This is correct if our footprint has
        # pad 1 at (0,0), which it does.
        #
        # However, if pad positions in the footprint aren't centroid-based but
        # origin-at-pad1, the component reference point must match. The rosetta
        # stone ASCII position represents the component's reference point in Protel,
        # which is typically the first pad. No centroid offset needed.

        if fp_data is not None:
            resolved += 1
            fp_name = f"Protel_Import:{comp.footprint.strip() or comp.library_ref.strip()}"
            is_tht = any(p.drill > 0 for p in fp_data.pads)
        else:
            fallback += 1
            fp_name = f"Protel_Import:{comp.footprint.strip() or comp.library_ref.strip() or comp.name or 'unknown'}"
            is_tht = False
            logger.warning(
                "[generator] Footprint fallback for %s (footprint=%r, library_ref=%r)",
                comp.designator, comp.footprint, comp.library_ref,
            )

        attr = "through_hole" if is_tht else "smd"

        f.write(f'  (footprint "{fp_name}"\n')
        f.write(f'    (layer "{comp.layer}")\n')
        f.write(f'    (uuid "{_uuid()}")\n')
        f.write(f'    (at {_fmt(x_mm)} {_fmt(y_mm)} {int(rotation)})\n')
        f.write(f'    (attr {attr})\n')

        # Reference text
        designator = comp.designator or f"?{comp.index}"
        f.write(
            f'    (fp_text reference "{designator}" (at 0 -2) '
            f'(layer "F.SilkS") (uuid "{_uuid()}")\n'
            f'      (effects (font (size 0.8 0.8) (thickness 0.12)))\n'
            f'    )\n'
        )

        # Value text
        value = comp.value or comp.name or ""
        f.write(
            f'    (fp_text value "{value}" (at 0 2) '
            f'(layer "F.Fab") (uuid "{_uuid()}")\n'
            f'      (effects (font (size 0.8 0.8) (thickness 0.12)))\n'
            f'    )\n'
        )

        # Pads and silk
        if fp_data is not None:
            # text11 footprints already have correct origin (pin 1 at 0,0)
            # PCBLIB footprints need origin correction
            if fp_source == 'text11':
                offset = (0.0, 0.0)
            else:
                fp_key = comp.footprint.strip() or comp.library_ref.strip()
                offset = (origin_corrections or {}).get(fp_key, None)
                if offset is None:
                    import re as _re
                    base_name = _re.sub(r'[0-9|]+$', '', fp_key)
                    if base_name != fp_key:
                        offset = (origin_corrections or {}).get(base_name, (0.0, 0.0))
                    else:
                        offset = (0.0, 0.0)
            _emit_library_pads(f, fp_data, comp_layer=comp.layer,
                               origin_offset=offset, comp_rotation=rotation)
            _emit_library_silk(f, fp_data, origin_offset=offset,
                               comp_layer=comp.layer)
        else:
            _emit_generic_pads(f, comp)

        f.write('  )\n\n')
        placed += 1

    return (placed, skipped, resolved, fallback)


def generate_kicad_pcb(
    pcb_data: PCBData,
    output_path: Path,
    libraries: dict[str, dict[str, FootprintData]] | None = None,
    pnp_path: Path | str | None = None,
    pcb_source_path: Path | str | None = None,
) -> dict:
    """Generate a KiCad 9 .kicad_pcb file from parsed PCBData.

    Args:
        pcb_data: Populated PCBData with components, tracks, vias, board_outline, nets.
                  All coordinates in mils.
        output_path: Path to write the .kicad_pcb file.
        libraries: Pre-loaded PCBLIB libraries as {lib_name: {fp_name: FootprintData}}.
                   If None, auto-discovers protel-libs/ near output_path.
        pnp_path: Path to Protel Pick and Place CSV. If None, auto-discovers.
                  Used to compute per-footprint origin corrections.
        pcb_source_path: Path to the source .PCB file. Used for PnP auto-discovery
                         when output_path is in a different directory tree.

    Returns:
        Dict with generation stats: component_count, track_count, via_count,
        edge_count, skipped_count, footprints_resolved, footprints_fallback.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Configure board-specific Y transform from board outline
    _configure_board_origin(pcb_data)

    # Auto-discover libraries if not provided
    if libraries is None:
        libraries = find_pcblib_libraries(output_path)

    # Per-footprint origin corrections: static table + optional per-board PnP
    # Static table (from Place Whole Library exports) covers all known footprints.
    # Per-board PnP overrides for board-specific footprints not in the table.
    origin_corrections: dict[str, tuple[float, float]] = dict(FOOTPRINT_ORIGIN_CORRECTIONS)
    if pnp_path is not None:
        pnp_path = Path(pnp_path)
    else:
        # Try auto-discovery from output path, then from source PCB path
        pnp_path = _find_pnp_file(output_path)
        if pnp_path is None and pcb_source_path:
            pnp_path = _find_pnp_file(Path(pcb_source_path))
    if pnp_path is not None and pnp_path.exists():
        board_corrections = _parse_pnp_origin_offsets(pnp_path, libraries)
        if board_corrections:
            # Board-specific corrections override static table
            origin_corrections.update(board_corrections)
            print(
                f"[generator] Applied {len(board_corrections)} board-specific footprint "
                f"origin corrections from {pnp_path}",
                file=sys.stderr,
            )
    logger.info(
        "[generator] Total footprint origin corrections: %d (%d from static table)",
        len(origin_corrections), len(FOOTPRINT_ORIGIN_CORRECTIONS),
    )

    # Load text11 footprint library (from Place Whole Library exports)
    text11_lib: dict[str, FootprintData] | None = None
    try:
        from protel99_parser.text11_footprint_lib import load_text11_footprints
        text11_lib = load_text11_footprints()
        if text11_lib:
            print(
                f"[generator] Loaded {len(text11_lib)} text11 footprints "
                f"(priority over PCBLIB)",
                file=sys.stderr,
            )
    except Exception as e:
        logger.warning("[generator] Failed to load text11 footprints: %s", e)

    with open(output_path, 'w', encoding='utf-8') as f:
        _emit_header(f, pcb_data)
        edge_count = _emit_board_outline(f, pcb_data)
        track_count = _emit_tracks(f, pcb_data)
        via_count = _emit_vias(f, pcb_data)
        fill_count = _emit_fills(f, pcb_data)
        string_count = _emit_free_strings(f, pcb_data)
        component_count, skipped_count, resolved, fallback = _emit_footprints(
            f, pcb_data, libraries, origin_corrections, text11_lib,
        )
        f.write(')\n')  # close (kicad_pcb

    stats = {
        'component_count': component_count,
        'track_count': track_count,
        'via_count': via_count,
        'edge_count': edge_count,
        'fill_count': fill_count,
        'string_count': string_count,
        'skipped_count': skipped_count,
        'footprints_resolved': resolved,
        'footprints_fallback': fallback,
    }

    # Observability - log to stderr
    lib_count = sum(len(v) for v in libraries.values())
    print(
        f"[generator] Writing {component_count} components, {track_count} tracks, "
        f"{via_count} vias, {edge_count} board edges, {fill_count} fills, "
        f"{string_count} strings to {output_path} "
        f"(resolved={resolved}, fallback={fallback}, libraries={len(libraries)} "
        f"with {lib_count} footprints)",
        file=sys.stderr,
    )
    if skipped_count > 0:
        print(
            f"[generator] Skipped {skipped_count} unmatched components",
            file=sys.stderr,
        )
    if fallback > 0:
        print(
            f"[generator] WARNING: {fallback} components used generic pad fallback",
            file=sys.stderr,
        )

    return stats
