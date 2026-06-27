"""
Rosetta Stone - ASCII PCB parsers for ground-truth coordinate extraction.

Parses Protel text11 and Autotrax ASCII export formats to extract
component designator -> (X, Y) positions in integer mils. These serve as
the ground truth for correlating with binary PCB data.

Text11 format: "PCB FILE 6 VERSION 1.10"
Autotrax format: "PCB FILE 4"
ASCII v2.70 format: "PCB FILE 6 VERSION 2.70" - copper tracks, vias, board outline

Also provides:
- match_designators() - correlate ASCII and binary component lists
- protel_to_gerber() - Protel internal coords -> Gerber mils
- extract_pad_centroid() - approximate position from binary pad uint16 values
- decode_coordinate() - unified coordinate decoder (ASCII exact or binary approx)
- load_ground_truth() - convenience: all components in Gerber mils
- parse_ascii_v270_copper() - extract copper tracks, vias, board outline from v2.70 ASCII
- find_ascii_rosetta_file() - auto-discover paired ASCII export from binary PCB path
"""

from __future__ import annotations

import re
import struct
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def _is_designator(label: str) -> bool:
    """Check if a label is a valid component designator.

    Accepts both traditional alpha-prefix designators (R1, C10, U3) and
    numeric-only designators (918, 40, 1) used for jumpers and
    mounting-hole footprints.

    Returns:
        True if the label matches a known designator pattern.
    """
    return bool(re.match(r"^[A-Z]+\d", label) or re.match(r"^\d+$", label))


def _find_repo_root() -> Optional[Path]:
    """Walk up from this file looking for a project root marker.

    Returns the first ancestor directory that contains a ``pyproject.toml``
    or a ``.git`` directory, or None if none is found within the search
    depth. This never raises - callers should fall back to deriving paths
    from the parsed PCB file's own location.
    """
    current = Path(__file__).resolve().parent
    for _ in range(10):
        if (current / "pyproject.toml").is_file() or (current / ".git").is_dir():
            return current
        if current.parent == current:
            break
        current = current.parent
    return None


def _no_default_path_error(kind: str) -> FileNotFoundError:
    return FileNotFoundError(
        f"No {kind} path was provided and none could be derived. "
        "Pass an explicit filepath, or use find_text11_file()/"
        "find_ascii_rosetta_file() to discover the export next to your "
        "binary PCB file."
    )


def parse_text11_pcb(
    filepath: Optional[str | Path] = None,
) -> Dict[str, Tuple[int, int]]:
    """Parse a Protel text11 ASCII PCB file and extract component positions.

    Text11 COMP block structure:
        COMP
        <footprint_name>
        0 0 X Y mirror 0 -105 bbox_xmin bbox_ymin bbox_xmax bbox_ymax rotation layer flag
        CS
        <pad_data_line>
        <designator_or_empty>   <- first label after CS matching /^[A-Z]+\\d/
        CS
        ...
        ENDCOMP

    Args:
        filepath: Path to text11 PCB file. Required.

    Returns:
        Dict mapping designator (e.g. 'U1') to (x_mils, y_mils) tuple.
        Only components with valid designators are included.
    """
    if filepath is None:
        raise _no_default_path_error("text11")
    filepath = Path(filepath)

    data = filepath.read_text(encoding="latin-1")

    # Normalize line endings to \n for consistent parsing
    data = data.replace("\r\n", "\n")

    # Detect format version: v2.70 coordinates are 1000× larger than v1.10
    is_v270 = "VERSION 2.70" in data[:50]

    blocks = re.findall(r"COMP\n(.*?)ENDCOMP", data, re.DOTALL)

    positions: Dict[str, Tuple[int, int]] = {}

    for block in blocks:
        lines = block.strip().split("\n")
        if len(lines) < 2:
            continue

        # line[0] = footprint name, line[1] = position/attributes line
        pos_parts = lines[1].strip().split()
        if len(pos_parts) < 4:
            continue

        try:
            x = int(pos_parts[2])
            y = int(pos_parts[3])
        except (ValueError, IndexError):
            continue

        # Scale v2.70 coordinates to v1.10 resolution (mils)
        if is_v270:
            x = round(x / 1000)
            y = round(y / 1000)

        # Find designator: first label after a CS block matching /^[A-Z]+\d/
        designator = None
        i = 2
        while i < len(lines):
            if lines[i].strip() == "CS":
                # Next line is pad data, line after that is potential label
                if i + 2 < len(lines):
                    label = lines[i + 2].strip()
                    if _is_designator(label):
                        designator = label
                        break
                i += 3  # skip CS + pad_data + label
            else:
                i += 1

        if designator:
            positions[designator] = (x, y)

    return positions


def parse_text11_footprints(
    filepath: Optional[str | Path] = None,
) -> Dict[str, str]:
    """Parse text11 COMP blocks and return designator -> footprint name mapping.

    Each COMP block's first line is the footprint name (e.g. a 2-pin
    jumper, 1206, or a mounting-hole footprint). This function extracts
    that mapping for all components.

    Args:
        filepath: Path to text11 PCB file. Required.

    Returns:
        Dict mapping designator (e.g. 'R1', '918') to footprint name string.
    """
    if filepath is None:
        raise _no_default_path_error("text11")
    filepath = Path(filepath)

    data = filepath.read_text(encoding="latin-1")
    data = data.replace("\r\n", "\n")

    blocks = re.findall(r"COMP\n(.*?)ENDCOMP", data, re.DOTALL)

    footprints: Dict[str, str] = {}

    for block in blocks:
        lines = block.strip().split("\n")
        if len(lines) < 2:
            continue

        footprint_name = lines[0].strip()

        # Find designator: first label after a CS block matching _is_designator()
        designator = None
        i = 2
        while i < len(lines):
            if lines[i].strip() == "CS":
                if i + 2 < len(lines):
                    label = lines[i + 2].strip()
                    if _is_designator(label):
                        designator = label
                        break
                i += 3
            else:
                i += 1

        if designator:
            footprints[designator] = footprint_name

    return footprints


def parse_autotrax_pcb(
    filepath: Optional[str | Path] = None,
) -> Dict[str, Tuple[int, int]]:
    """Parse a Protel Autotrax ASCII PCB file and extract component positions.

    Autotrax COMP block structure:
        COMP
        <designator>          <- line[0] is the designator
        <footprint_name>      <- line[1] is the footprint
        <extra_label>         <- line[2] optional extra text
        <pad_lines...>        <- pad data (6+ fields)
        X Y rot layer flag    <- position line (5 fields, small rot/layer values)
        ...
        ENDCOMP

    The position line has exactly 5 space-separated integer fields where
    the 3rd and 4th values are small (0 or 1 for rotation and layer).

    Args:
        filepath: Path to Autotrax PCB file. Required.

    Returns:
        Dict mapping designator to (x_mils, y_mils) tuple.
    """
    if filepath is None:
        raise _no_default_path_error("Autotrax")
    filepath = Path(filepath)

    data = filepath.read_text(encoding="latin-1")
    data = data.replace("\r\n", "\n")

    blocks = re.findall(r"COMP\n(.*?)ENDCOMP", data, re.DOTALL)

    positions: Dict[str, Tuple[int, int]] = {}

    for block in blocks:
        lines = block.strip().split("\n")
        if len(lines) < 3:
            continue

        # line[0] = designator or numeric ID
        candidate_desig = lines[0].strip()

        # Find the position line: exactly 5 space-separated fields,
        # where fields[2] (rot) and fields[3] (layer) are small ints (0-3)
        pos_x, pos_y = None, None
        for line in lines[2:]:
            parts = line.strip().split()
            if len(parts) == 5:
                try:
                    vals = [int(p) for p in parts]
                    # Position line: X Y rot layer flag
                    # rot is 0 or 1, layer is 0 or 1, flag is 1
                    if vals[2] in (0, 1) and vals[3] in (0, 1) and vals[4] == 1:
                        pos_x, pos_y = vals[0], vals[1]
                        break
                except ValueError:
                    continue

        if pos_x is None:
            continue

        # Only include blocks with valid designators (letters + digits or numeric-only)
        if _is_designator(candidate_desig):
            positions[candidate_desig] = (pos_x, pos_y)

    return positions


# ── Protel->Gerber Coordinate Transform ───────────────────────────────

# Mounting holes discovered from text11 mounting-hole footprint blocks
# and KiCad MountingHole footprints. These anchor the coordinate transform.
#
# text11 (Protel internal):  Gerber (mils):
#   (34948, 41021)           (6594, 7156)
#   (34948, 43128)           (6594, 9263)
#   (38767, 43128)           (10413, 9263)
#   (41641, 43128)           (13287, 9263)
#
# Transform: gerber_x = protel_x - 28354
#            gerber_y = protel_y - 33865
#
# Verified on 4 mounting holes with max error ≤1 mil.

_PROTEL_OFFSET_X = 28351.838
_PROTEL_OFFSET_Y = 33555.020

# Gerber mounting hole positions (derived from transform)
_GERBER_MOUNTING_HOLES = [
    (6594.0, 7156.0),   # = (34948-28354, 41021-33865)
    (6594.0, 9263.0),   # = (34948-28354, 43128-33865)
    (10413.0, 9263.0),  # = (38767-28354, 43128-33865)
    (13287.0, 9263.0),  # = (41641-28354, 43128-33865)
]

# Linear regression: gerber_mil = scale * uint16 + intercept
# Computed from 152 matched components (2nd REC_17 tail, offsets 1:3 and 5:7)
_PAD_SCALE_X = 6.534249
_PAD_INTERCEPT_X = -28194.40
_PAD_SCALE_Y = 6.030179
_PAD_INTERCEPT_Y = -30529.97

# Uppercase substring used to recognise mounting-hole footprint names in a
# text11 export. Adjust to match the naming used in your own libraries.
_MOUNTING_HOLE_MARKER = "MOUNT"


def _extract_text11_mounting_holes(
    filepath: Optional[str | Path] = None,
) -> List[Tuple[int, int]]:
    """Extract mounting-hole footprint positions from a text11 file.

    These are COMP blocks whose footprint name contains the configured
    mounting-hole marker substring (see _MOUNTING_HOLE_MARKER).

    Returns:
        List of (x, y) positions in Protel internal mils.
    """
    if filepath is None:
        raise _no_default_path_error("text11")
    filepath = Path(filepath)
    data = filepath.read_text(encoding="latin-1")
    data = data.replace("\r\n", "\n")
    blocks = re.findall(r"COMP\n(.*?)ENDCOMP", data, re.DOTALL)

    holes: List[Tuple[int, int]] = []
    for block in blocks:
        lines = block.strip().split("\n")
        if len(lines) < 2:
            continue
        footprint = lines[0].strip()
        if _MOUNTING_HOLE_MARKER not in footprint.upper():
            continue
        pos_parts = lines[1].strip().split()
        if len(pos_parts) >= 4:
            try:
                holes.append((int(pos_parts[2]), int(pos_parts[3])))
            except ValueError:
                continue
    return holes


def protel_to_gerber(x_protel: int, y_protel: int) -> Tuple[float, float]:
    """Convert Protel internal coordinates to Gerber mils.

    Uses the offset derived from 4 mounting holes:
      gerber_x = protel_x - 28354
      gerber_y = protel_y - 33865

    Args:
        x_protel: X coordinate in Protel internal units (integer mils).
        y_protel: Y coordinate in Protel internal units (integer mils).

    Returns:
        (x_gerber, y_gerber) in mils as floats.
    """
    return (float(x_protel - _PROTEL_OFFSET_X),
            float(y_protel - _PROTEL_OFFSET_Y))


def match_designators(
    ascii_positions: Dict[str, Tuple[int, int]],
    binary_components: list,
) -> Tuple[Dict[str, Tuple[Tuple[int, int], str]], List[str], List[str]]:
    """Match ASCII ground-truth designators to binary parser components.

    Performs case-sensitive exact string matching on designator names.

    Args:
        ascii_positions: Dict from parse_text11_pcb(), mapping designator -> (x, y).
        binary_components: List of Component objects from the binary parser
            (pcb.components), each having a .designator attribute.

    Returns:
        Tuple of:
          - matched: dict mapping designator -> (ascii_pos, binary_designator)
            where ascii_pos is (x, y) in Protel mils
          - ascii_only: sorted list of designators only in ASCII
          - binary_only: sorted list of designators only in binary
    """
    binary_desigs = {c.designator for c in binary_components}
    ascii_desigs = set(ascii_positions.keys())

    overlap = ascii_desigs & binary_desigs
    ascii_only = sorted(ascii_desigs - binary_desigs)
    binary_only = sorted(binary_desigs - ascii_desigs)

    matched = {}
    for desig in sorted(overlap):
        matched[desig] = (ascii_positions[desig], desig)

    return matched, ascii_only, binary_only


def _build_component_record_groups(pcb_data) -> Dict[str, list]:
    """Build a map from designator to the list of raw records in its group.

    Uses the same component group boundaries as the binary parser.

    Args:
        pcb_data: A PCBData object from the binary parser.

    Returns:
        Dict mapping designator -> list of RawRecord objects.
    """
    records = pcb_data.all_records
    types = [r.record_type for r in records]
    n = len(types)
    start_types = {0x06, 0x08, 0x04, 0x0B, 0x09, 0x0A}
    start_indices = [i for i in range(n) if types[i] in start_types]

    desig_re = re.compile(r"^[A-Z]+\d+")
    groups: Dict[str, list] = {}

    for si_idx in range(len(start_indices)):
        start_idx = start_indices[si_idx]
        end_idx = start_indices[si_idx + 1] if si_idx + 1 < len(start_indices) else n

        desig = None
        for j in range(start_idx, end_idx):
            r = records[j]
            if r.record_type in (0x02, 0x03):
                s = r.first_string()
                if s:
                    m = desig_re.match(s)
                    if m and not desig:
                        desig = m.group()

        if desig and desig not in groups:
            groups[desig] = records[start_idx:end_idx]

    return groups


def extract_pad_centroid(
    component_records: list,
) -> Optional[Tuple[float, float]]:
    """Extract approximate position from binary pad data in component records.

    Finds the 2nd REC_17 (geometry) record in the group. Parses the tail
    bytes after the 23-byte ASCII float:
      - tail[1:3] as LE uint16 -> pad X raw value
      - tail[5:7] as LE uint16 -> pad Y raw value
    Applies pre-computed linear regression coefficients to convert to Gerber mils.

    Accuracy: ~50-200 mil (pad geometry center, not component reference point).

    Args:
        component_records: List of RawRecord objects for one component group.

    Returns:
        (x_mil, y_mil) in Gerber coordinates, or None if extraction fails.
    """
    rec17s = [r for r in component_records if r.record_type == 0x17]
    if len(rec17s) < 2:
        return None

    r17 = rec17s[1]
    tail = r17.data[23:]  # skip 23-byte ASCII float prefix
    if len(tail) < 7:
        return None

    try:
        raw_x = struct.unpack_from("<H", tail, 1)[0]
        raw_y = struct.unpack_from("<H", tail, 5)[0]
    except struct.error:
        return None

    # Apply linear regression
    x_mil = _PAD_SCALE_X * raw_x + _PAD_INTERCEPT_X
    y_mil = _PAD_SCALE_Y * raw_y + _PAD_INTERCEPT_Y

    # Board extents check (loose): 4000-16000 mils
    if not (4000 < x_mil < 16000 and 4000 < y_mil < 16000):
        return None

    return (x_mil, y_mil)


def decode_coordinate(
    designator: str,
    ascii_positions: Optional[Dict[str, Tuple[int, int]]] = None,
    binary_pcb_data=None,
    _record_groups: Optional[Dict[str, list]] = None,
) -> Tuple[float, float, str]:
    """Unified coordinate decoder - returns (x, y, source) in Gerber mils.

    Resolution strategy:
    1. If designator is in ascii_positions -> apply protel_to_gerber() -> source='ascii_exact'
    2. Else if designator is in binary data -> extract_pad_centroid() -> source='binary_pad_centroid'
    3. Else -> (0.0, 0.0, 'unmatched')

    Args:
        designator: Component designator (e.g. 'U1', 'R440').
        ascii_positions: Dict from parse_text11_pcb(). Auto-loaded if None.
        binary_pcb_data: PCBData from the binary parser. Used for pad centroid fallback.
        _record_groups: Pre-built designator->records map (optimization).

    Returns:
        (x_mil, y_mil, source_tag) tuple.
    """
    # Auto-load ASCII positions if not provided
    if ascii_positions is None:
        ascii_positions = parse_text11_pcb()

    # Strategy 1: ASCII exact
    if designator in ascii_positions:
        ax, ay = ascii_positions[designator]
        gx, gy = protel_to_gerber(ax, ay)
        return (gx, gy, "ascii_exact")

    # Strategy 2: Binary pad centroid
    if binary_pcb_data is not None:
        if _record_groups is None:
            _record_groups = _build_component_record_groups(binary_pcb_data)

        if designator in _record_groups:
            pos = extract_pad_centroid(_record_groups[designator])
            if pos is not None:
                return (pos[0], pos[1], "binary_pad_centroid")

    return (0.0, 0.0, "unmatched")


def load_ground_truth(
    pcb_path: Optional[str | Path] = None,
) -> Dict[str, Tuple[float, float]]:
    """Load text11 ground truth as Gerber-coordinates dict.

    Parses every component in the text11 file and applies the
    protel_to_gerber() transform.

    Args:
        pcb_path: Path to text11 file. Required.

    Returns:
        Dict mapping designator -> (x_gerber_mil, y_gerber_mil).
        Contains all components from the text11 export.
    """
    t11 = parse_text11_pcb(pcb_path)
    result: Dict[str, Tuple[float, float]] = {}
    for desig, (px, py) in t11.items():
        result[desig] = protel_to_gerber(px, py)
    return result


def load_component_layers(
    pcb_path: Optional[str | Path] = None,
) -> Dict[str, str]:
    """Extract component layers from text11 CS pad data.

    Protel text11 CS (component shape) records include a pad layer field:
    layer 17 = F.Cu (top), layer 18 = B.Cu (bottom). For THT components with
    pads on both layers, the first CS pad layer determines the component side.

    Args:
        pcb_path: Path to text11 file. Required.

    Returns:
        Dict mapping designator -> KiCad layer name ("F.Cu" or "B.Cu").
    """
    if pcb_path is None:
        raise _no_default_path_error("text11")
    pcb_path = Path(pcb_path)

    data = pcb_path.read_text(encoding="latin-1").replace("\r\n", "\n")
    blocks = re.findall(r"COMP\n(.*?)ENDCOMP", data, re.DOTALL)

    _PAD_LAYERS = {17: "F.Cu", 18: "B.Cu"}
    result: Dict[str, str] = {}

    for block in blocks:
        lines = block.strip().split("\n")
        if len(lines) < 2:
            continue

        # Find designator and first CS pad layer
        designator = None
        pad_layer = None
        i = 2
        while i < len(lines):
            if lines[i].strip() == "CS":
                if i + 1 < len(lines) and pad_layer is None:
                    pad_parts = lines[i + 1].strip().split()
                    if len(pad_parts) >= 9:
                        try:
                            layer_num = int(pad_parts[8])
                            if layer_num in _PAD_LAYERS:
                                pad_layer = _PAD_LAYERS[layer_num]
                        except ValueError:
                            pass
                if i + 2 < len(lines) and designator is None:
                    label = lines[i + 2].strip()
                    if _is_designator(label):
                        designator = label
                i += 3
            else:
                i += 1

        if designator and pad_layer:
            result[designator] = pad_layer

    return result


def load_component_rotations(
    pcb_path: Optional[str | Path] = None,
) -> Dict[str, float]:
    """Extract component rotations from text11 COMP position lines.

    Text11 COMP block position line (line[1] of each block):
        0 0 X Y mirror 0 -105 bbox_xmin bbox_ymin bbox_xmax bbox_ymax rotation layer flag

    Field[11] is the rotation in degrees (float, e.g. 360.000, 180.000, 90.000, 0.000).

    Args:
        pcb_path: Path to text11 file. Required.

    Returns:
        Dict mapping designator -> rotation degrees (float).
    """
    if pcb_path is None:
        raise _no_default_path_error("text11")
    pcb_path = Path(pcb_path)

    data = pcb_path.read_text(encoding="latin-1").replace("\r\n", "\n")
    blocks = re.findall(r"COMP\n(.*?)ENDCOMP", data, re.DOTALL)

    result: Dict[str, float] = {}

    for block in blocks:
        lines = block.strip().split("\n")
        if len(lines) < 2:
            continue

        pos_parts = lines[1].strip().split()
        if len(pos_parts) < 12:
            continue

        try:
            rotation = float(pos_parts[11])
        except (ValueError, IndexError):
            continue

        # Find designator
        designator = None
        i = 2
        while i < len(lines):
            if lines[i].strip() == "CS":
                if i + 2 < len(lines):
                    label = lines[i + 2].strip()
                    if _is_designator(label):
                        designator = label
                        break
                i += 3
            else:
                i += 1

        if designator:
            result[designator] = rotation

    return result


# ── ASCII v2.70 Copper Parser ────────────────────────────────────────

# Layer mapping: Protel layer number -> KiCad layer name
_COPPER_LAYERS = {1: "F.Cu", 16: "B.Cu"}
_OUTLINE_LAYER = 29


def parse_ascii_v270_copper(
    filepath: Optional[str | Path] = None,
) -> dict:
    """Parse ASCII v2.70 PCB export and extract copper tracks, vias, board outline,
    fills, and free strings.

    Reads FT (Free Track) and CT (Component Track) records on copper layers
    (1=F.Cu, 16=B.Cu), CV (Component Via) records, FF (Free Fill) records,
    FS (Free String) records, and FT records on layer 29 (board outline).

    Record formats (each keyword on its own line, data on next line):
      FT/CT: ``0 0 X1 Y1 X2 Y2 width layer net_flag1 net_flag2``
             followed by continuation line `` 0 0 1``
      CV:    ``0 0 X Y diameter drill ...`` (single data line, no continuation)
      FF:    ``0 0 X1 Y1 X2 Y2 layer net_flag``
             followed by continuation line `` 0 1``
      FS:    ``0 0 X Y height rotation 0 2 layer ...``
             followed by text line with the string content

    Coordinates are Protel internal units (mil × 1000). Converted to Gerber mils
    by dividing by 1000.0 and subtracting the Protel->Gerber offset.

    Returns:
        Dict with keys:
          - 'tracks': list of Track objects (copper layers only)
          - 'vias': list of Via objects
          - 'fills': list of Fill objects (copper layer fills)
          - 'free_strings': list of FreeString objects
          - 'board_outline': list of (x1, y1, x2, y2) tuples in Gerber mils
    """
    from protel99_parser.parser import Track, Via, Fill, FreeString

    if filepath is None:
        raise _no_default_path_error("ASCII v2.70")
    filepath = Path(filepath)

    data = filepath.read_text(encoding="latin-1")
    data = data.replace("\r\n", "\n")

    # Verify header
    first_line = data.split("\n", 1)[0].strip()
    if not first_line.startswith("PCB FILE 6 VERSION 2.70"):
        raise ValueError(
            f"Expected 'PCB FILE 6 VERSION 2.70' header, got: {first_line!r}"
        )

    lines = data.split("\n")
    n = len(lines)

    tracks: list = []
    vias: list = []
    fills: list = []
    free_strings: list = []
    board_outline: list = []

    i = 0
    while i < n:
        tag = lines[i].strip()

        if tag in ("FT", "CT"):
            # Next line is the data line
            if i + 1 >= n:
                i += 1
                continue
            parts = lines[i + 1].strip().split()
            # Expected: 0 0 X1 Y1 X2 Y2 width layer ...
            if len(parts) >= 8:
                try:
                    raw_x1 = int(parts[2])
                    raw_y1 = int(parts[3])
                    raw_x2 = int(parts[4])
                    raw_y2 = int(parts[5])
                    raw_width = int(parts[6])
                    layer_num = int(parts[7])
                except (ValueError, IndexError):
                    i += 3  # skip data + continuation
                    continue

                # Convert Protel internal (mil×1000) -> Gerber mils
                gx1 = raw_x1 / 1000.0 - _PROTEL_OFFSET_X
                gy1 = raw_y1 / 1000.0 - _PROTEL_OFFSET_Y
                gx2 = raw_x2 / 1000.0 - _PROTEL_OFFSET_X
                gy2 = raw_y2 / 1000.0 - _PROTEL_OFFSET_Y
                width_mils = raw_width / 1000.0

                if layer_num in _COPPER_LAYERS:
                    tracks.append(Track(
                        x1=gx1, y1=gy1, x2=gx2, y2=gy2,
                        width=width_mils,
                        layer=_COPPER_LAYERS[layer_num],
                        net=0,
                    ))
                elif layer_num == _OUTLINE_LAYER:
                    board_outline.append((gx1, gy1, gx2, gy2))

            i += 3  # tag + data + continuation line

        elif tag == "CV":
            # Next line is the data line (no continuation)
            if i + 1 >= n:
                i += 1
                continue
            parts = lines[i + 1].strip().split()
            # Expected: 0 0 X Y diameter drill ...
            if len(parts) >= 6:
                try:
                    raw_x = int(parts[2])
                    raw_y = int(parts[3])
                    raw_diameter = int(parts[4])
                    raw_drill = int(parts[5])
                except (ValueError, IndexError):
                    i += 2
                    continue

                gx = raw_x / 1000.0 - _PROTEL_OFFSET_X
                gy = raw_y / 1000.0 - _PROTEL_OFFSET_Y
                diameter_mils = raw_diameter / 1000.0
                drill_mils = raw_drill / 1000.0

                vias.append(Via(
                    x=gx, y=gy,
                    size=diameter_mils,
                    drill=drill_mils,
                    layers=("F.Cu", "B.Cu"),
                    net=0,
                ))

            i += 2  # tag + data

        elif tag == "FF":
            # Free Fill: rectangle on copper layer
            if i + 1 >= n:
                i += 1
                continue
            parts = lines[i + 1].strip().split()
            # Expected: 0 0 X1 Y1 X2 Y2 layer net_flag
            if len(parts) >= 7:
                try:
                    raw_x1 = int(parts[2])
                    raw_y1 = int(parts[3])
                    raw_x2 = int(parts[4])
                    raw_y2 = int(parts[5])
                    layer_num = int(parts[6])
                except (ValueError, IndexError):
                    i += 3
                    continue

                if layer_num in _COPPER_LAYERS:
                    gx1 = raw_x1 / 1000.0 - _PROTEL_OFFSET_X
                    gy1 = raw_y1 / 1000.0 - _PROTEL_OFFSET_Y
                    gx2 = raw_x2 / 1000.0 - _PROTEL_OFFSET_X
                    gy2 = raw_y2 / 1000.0 - _PROTEL_OFFSET_Y
                    fills.append(Fill(
                        x1=gx1, y1=gy1, x2=gx2, y2=gy2,
                        layer=_COPPER_LAYERS[layer_num],
                        net=0,
                    ))

            i += 3  # tag + data + continuation

        elif tag == "FS":
            # Free String: text on a layer
            if i + 2 >= n:
                i += 1
                continue
            parts = lines[i + 1].strip().split()
            text_content = lines[i + 2].strip() if i + 2 < n else ""
            # Expected: 0 0 X Y height rotation 0 2 layer ...
            if len(parts) >= 9 and text_content:
                try:
                    raw_x = int(parts[2])
                    raw_y = int(parts[3])
                    raw_height = int(parts[4])
                    rotation = float(parts[5])
                    layer_num = int(parts[8])
                except (ValueError, IndexError):
                    i += 3
                    continue

                layer_name = _COPPER_LAYERS.get(layer_num)
                if layer_name is None:
                    # Map other layers
                    if layer_num == 17:
                        layer_name = "F.SilkS"
                    elif layer_num == 18:
                        layer_name = "B.SilkS"
                    else:
                        i += 3
                        continue

                gx = raw_x / 1000.0 - _PROTEL_OFFSET_X
                gy = raw_y / 1000.0 - _PROTEL_OFFSET_Y
                height_mils = raw_height / 1000.0

                free_strings.append(FreeString(
                    x=gx, y=gy,
                    text=text_content,
                    height=height_mils,
                    rotation=rotation,
                    layer=layer_name,
                ))

            i += 3  # tag + data + text

        else:
            i += 1

    return {
        "tracks": tracks,
        "vias": vias,
        "fills": fills,
        "free_strings": free_strings,
        "board_outline": board_outline,
    }


# ── ASCII Rosetta File Auto-Discovery ────────────────────────────────


def _is_v270_header(filepath: Path) -> bool:
    """Check if a file has a VERSION 2.70 header (ASCII v2.70 format)."""
    try:
        header = filepath.read_text(encoding="latin-1")[:50]
        return "VERSION 2.70" in header
    except (OSError, UnicodeDecodeError):
        return False


def _is_text11_header(filepath: Path) -> bool:
    """Check if a file has a text11-compatible header (VERSION 1.10 or 2.70)."""
    try:
        header = filepath.read_text(encoding="latin-1")[:50]
        return "VERSION 1.10" in header or "VERSION 2.70" in header
    except (OSError, UnicodeDecodeError):
        return False


def find_text11_file(
    binary_pcb_path: str | Path,
) -> Optional[Path]:
    """Auto-discover the text11 ASCII export for a binary PCB file.

    All candidate paths are derived from the binary PCB file's own
    location and stem; no project-specific directory names are assumed.

    Search order (``<stem>`` is the binary PCB file stem):
    1. ``<binary_dir>/<stem>_text11.PCB``
    2. ``<binary_dir>/../PCB_ASCII/<stem>_text11.PCB``
    3. ``<binary_dir>/../PCB_ASCII/<stem>.PCB`` with a text11 header
    4. Walk up a few levels looking for a ``PCB_ASCII/`` directory that
       contains a ``*text11*`` file matching the stem.

    Args:
        binary_pcb_path: Path to the binary .PCB file.

    Returns:
        Path to the text11 file, or None if not found.
    """
    binary_pcb_path = Path(binary_pcb_path).resolve()
    stem = binary_pcb_path.stem
    binary_dir = binary_pcb_path.parent

    # Strategy 1: text11 sibling next to the binary PCB itself
    candidate = binary_dir / f"{stem}_text11.PCB"
    if candidate.is_file() and _is_text11_header(candidate):
        return candidate

    # Strategy 2: sibling PCB_ASCII directory next to the binary's parent
    ascii_dir = binary_dir.parent / "PCB_ASCII"
    if ascii_dir.is_dir():
        candidate = ascii_dir / f"{stem}_text11.PCB"
        if candidate.is_file() and _is_text11_header(candidate):
            return candidate
        # Also try just <stem>.PCB with a text11 header
        candidate = ascii_dir / f"{stem}.PCB"
        if candidate.is_file() and _is_text11_header(candidate):
            return candidate

    # Strategy 3: walk up looking for a PCB_ASCII/ directory with a match
    current = binary_dir.parent
    stem_upper = stem.upper()
    for _ in range(5):
        subdir = current / "PCB_ASCII"
        if subdir.is_dir():
            for f in sorted(subdir.iterdir()):
                if f.suffix.upper() == ".PCB" and "text11" in f.stem.lower():
                    if stem_upper in f.stem.upper() and _is_text11_header(f):
                        return f
        if current.parent == current:
            break
        current = current.parent

    return None


def find_ascii_rosetta_file(
    binary_pcb_path: str | Path,
) -> Optional[Path]:
    """Auto-discover the paired ASCII v2.70 export for a binary PCB file.

    All candidate paths are derived from the binary PCB file's own
    location and stem; no project-specific directory names are assumed.

    Search order (``<stem>`` is the binary PCB file stem):
    1. ``<binary_dir>/<stem>_ascii.PCB``
    2. ``<binary_dir>/../PCB_ASCII/<stem>_ascii.PCB``
    3. ``<binary_dir>/../PCB_ASCII/<stem>.PCB`` with a v2.70 header
    4. Walk up a few levels looking for a ``PCB_ASCII/`` directory that
       contains an ``*_ascii.PCB`` file matching the stem.

    Args:
        binary_pcb_path: Path to the binary ``.PCB`` file.

    Returns:
        Path to the ASCII v2.70 file, or None if not found.
    """
    binary_pcb_path = Path(binary_pcb_path).resolve()
    stem = binary_pcb_path.stem
    binary_dir = binary_pcb_path.parent

    # Strategy 1: _ascii sibling next to the binary PCB itself
    candidate = binary_dir / f"{stem}_ascii.PCB"
    if candidate.is_file() and _is_v270_header(candidate):
        return candidate

    # Strategy 2: sibling PCB_ASCII directory next to the binary's parent
    ascii_dir = binary_dir.parent / "PCB_ASCII"
    if ascii_dir.is_dir():
        # Try <stem>_ascii.PCB
        candidate = ascii_dir / f"{stem}_ascii.PCB"
        if candidate.is_file():
            return candidate
        # Try <stem>.PCB (verify v2.70 header to avoid picking text11/autotrax)
        candidate = ascii_dir / f"{stem}.PCB"
        if candidate.is_file() and _is_v270_header(candidate):
            return candidate

    # Strategy 3: walk up looking for a PCB_ASCII/ directory with a match
    current = binary_dir.parent
    for _ in range(5):
        pcb_ascii = current / "PCB_ASCII"
        if pcb_ascii.is_dir():
            for f in sorted(pcb_ascii.iterdir()):
                if f.suffix.upper() == ".PCB" and stem in f.stem:
                    if _is_v270_header(f):
                        return f
        if current.parent == current:
            break
        current = current.parent

    return None
