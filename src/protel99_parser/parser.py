"""
protel99-to-kicad: Parse Protel 99 SE binary PCB files.

Reverse-engineered from "PCB FILE 9 VERSION 2.70" format.
Extracts components, tracks, pads, vias, board outline.

Author: Szymon Gwóźdź
License: MIT
"""

import struct
import sys
import re
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from collections import Counter

logger = logging.getLogger(__name__)


# ============================================================
# Constants
# ============================================================

MARKER_BYTE = 0xA1
SUPPORTED_VERSIONS = ["PCB FILE 9 VERSION 2.70"]

RECORD_TYPES = {
    0x01: "pin",
    0x02: "wire",
    0x03: "text",        # designator, value, or net name
    0x04: "footprint",   # footprint type or value string
    0x05: "value",       # component value
    0x06: "component",   # component/footprint name (starts component group)
    0x07: "unknown_07",
    0x08: "pad_shape",   # pad shape (MINIMELF, etc.)
    0x09: "custom_fp",   # custom (library-defined) footprint
    0x0A: "via",
    0x0B: "lib_ref",     # library footprint reference
    0x0C: "unknown_0c",
    0x0D: "unknown_0d",
    0x0E: "unknown_0e",
    0x15: "unknown_15",
    0x16: "unknown_16",
    0x17: "geometry",    # rotation float + binary coordinates
    0x20: "polygon",
}


# ============================================================
# Data classes
# ============================================================

@dataclass
class RawRecord:
    offset: int
    record_type: int
    data: bytes

    @property
    def type_name(self) -> str:
        return RECORD_TYPES.get(self.record_type, f"unknown_{self.record_type:02x}")

    def strings(self) -> list[str]:
        result = []
        cur = bytearray()
        for b in self.data:
            if 0x20 <= b <= 0x7e:
                cur.append(b)
            else:
                if len(cur) >= 2:
                    result.append(cur.decode('ascii'))
                cur = bytearray()
        if len(cur) >= 2:
            result.append(cur.decode('ascii'))
        return result

    def first_string(self) -> str:
        s = self.strings()
        return s[0] if s else ""

    def ascii_float(self) -> Optional[float]:
        # Protel 99 SE v2.70 ASCII float is 23 bytes, not 22.
        # Format: ' X.XXXXXXXXXXXXXXE±XXXX' = 23 chars
        for bi in range(min(len(self.data), 5)):
            if self.data[bi] == 0x20 and bi + 23 <= len(self.data):
                try:
                    txt = self.data[bi:bi+23].decode('ascii')
                    if re.match(r' [\-\d]\.\d{14}E[\+\-]\d{4}', txt):
                        return float(txt)
                except:
                    pass
        return None


@dataclass
class Component:
    """A PCB component with all its properties."""
    index: int = 0
    footprint: str = ""        # SOT-23, DIL8-1, 1206, MINIMELF
    name: str = ""             # BC847C, TL074, PIC10F202-I/P
    designator: str = ""       # T1, R1, C1, U1
    value: str = ""            # 10k, 100nF, BC847C
    pad_shape: str = ""        # MINIMELF, 1206
    library_ref: str = ""      # library footprint reference name
    rotation: float = 0.0      # degrees
    pin_count: int = 0
    wire_count: int = 0
    x: float = 0.0             # absolute X position in mils (0.0 = not decoded)
    y: float = 0.0             # absolute Y position in mils (0.0 = not decoded)
    layer: str = "F.Cu"        # component layer: "F.Cu" (top) or "B.Cu" (bottom)
    _position_source: str = "" # diagnostic: which record/method provided x,y
    pad_records: list = field(default_factory=list)


@dataclass
class Track:
    """A copper track segment. All coordinates in mils."""
    x1: float
    y1: float
    x2: float
    y2: float
    width: float
    layer: str       # 'F.Cu', 'B.Cu', etc.
    net: int = 0


@dataclass
class Via:
    """A via connecting copper layers. All dimensions in mils."""
    x: float
    y: float
    drill: float
    size: float
    layers: tuple = ('F.Cu', 'B.Cu')
    net: int = 0


@dataclass
class Fill:
    """A copper fill rectangle. All coordinates in mils."""
    x1: float
    y1: float
    x2: float
    y2: float
    layer: str       # 'F.Cu', 'B.Cu', etc.
    net: int = 0


@dataclass
class FreeString:
    """A text string on a PCB layer. Coordinates in mils."""
    x: float
    y: float
    text: str
    height: float     # character height in mils
    rotation: float   # degrees
    layer: str        # 'F.Cu', 'B.Cu', 'F.SilkS', etc.
    width: float = 5.0  # stroke width in mils


@dataclass
class PCBData:
    filename: str
    version: str = ""
    file_size: int = 0
    total_records: int = 0
    record_counts: dict = field(default_factory=dict)
    components: list[Component] = field(default_factory=list)
    tracks: list[Track] = field(default_factory=list)
    vias: list[Via] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    free_strings: list[FreeString] = field(default_factory=list)
    polygons: list = field(default_factory=list)
    board_outline: list[tuple[float, float, float, float]] = field(default_factory=list)
    nets: dict[int, str] = field(default_factory=lambda: {0: ""})
    # Raw record lists for later processing
    all_records: list[RawRecord] = field(default_factory=list)

    @property
    def position_coverage(self) -> tuple[int, int, float]:
        """Return (matched, total, pct) for positioned components.

        A component is 'positioned' if _position_source is set and not
        'unmatched' or 'none'. Components with (0,0) and source='unmatched'
        or '' are considered unpositioned.
        """
        total = len(self.components)
        matched = sum(
            1 for c in self.components
            if c._position_source and c._position_source not in ('unmatched', 'none', '')
        )
        pct = (matched / total * 100) if total > 0 else 0.0
        return (matched, total, pct)


# ============================================================
# Parser
# ============================================================

class Protel99Parser:

    def __init__(self, filepath: str | Path):
        self.filepath = Path(filepath)
        self.data = b""

    def parse(self, ground_truth_path: str | Path | None = None,
              ascii_rosetta_path: str | Path | None = None) -> PCBData:
        self.data = self.filepath.read_bytes()
        pcb = PCBData(
            filename=str(self.filepath.name),
            file_size=len(self.data),
        )

        self._parse_header(pcb)
        self._extract_records(pcb)
        self._group_components(pcb)

        # Priority chain for position + copper data:
        # 1. Explicit --ascii-rosetta PATH
        # 2. Auto-discovered ASCII rosetta stone
        # 3. Explicit --ground-truth PATH (backward compat)
        # 4. Auto-discovered KiCad ground truth
        # 5. Binary-only pad centroid fallback (no copper)

        if ascii_rosetta_path is not None:
            ascii_rosetta_path = Path(ascii_rosetta_path)
            logger.info("[rosetta_stone] Using explicit ASCII rosetta: %s", ascii_rosetta_path)
            print(f"[rosetta_stone] Using explicit ASCII rosetta: {ascii_rosetta_path}",
                  file=sys.stderr)
            self._apply_rosetta_stone(pcb, ascii_rosetta_path)
        else:
            from protel99_parser.rosetta_stone import find_ascii_rosetta_file
            discovered = find_ascii_rosetta_file(self.filepath)
            if discovered is not None:
                logger.info("[rosetta_stone] Auto-discovered ASCII rosetta: %s", discovered)
                print(f"[rosetta_stone] Auto-discovered ASCII rosetta: {discovered}",
                      file=sys.stderr)
                if ground_truth_path is not None:
                    print("[rosetta_stone] NOTE: --ground-truth is deprecated when "
                          "ASCII rosetta stone is available. Using rosetta stone.",
                          file=sys.stderr)
                self._apply_rosetta_stone(pcb, discovered)
            elif ground_truth_path is not None:
                logger.info("[ground_truth] Using explicit KiCad ground truth: %s",
                            ground_truth_path)
                self._apply_ground_truth(pcb, ground_truth_path)
                self._apply_copper_ground_truth(pcb, ground_truth_path)
            else:
                from protel99_parser.kicad_ground_truth import find_ground_truth_file
                kicad_gt = find_ground_truth_file(self.filepath)
                if kicad_gt is not None:
                    logger.info("[ground_truth] Auto-discovered KiCad ground truth: %s",
                                kicad_gt)
                    self._apply_ground_truth(pcb, kicad_gt)
                    self._apply_copper_ground_truth(pcb, kicad_gt)
                else:
                    # Binary-only fallback - pad centroid positions, no copper
                    logger.info("[fallback] No ground truth source found, using binary pad centroids")
                    print("[fallback] No ground truth found - using binary pad centroids only",
                          file=sys.stderr)
                    self._apply_binary_fallback(pcb)

        return pcb

    def _apply_rosetta_stone(self, pcb: PCBData, ascii_path: Path):
        """Apply ASCII rosetta stone: text11 positions + ASCII v2.70 copper.

        1. Loads component positions from text11 ground truth
        2. For each component: ASCII exact -> binary pad centroid -> unmatched
        3. Loads copper tracks, vias, board outline from ASCII v2.70
        """
        from protel99_parser.rosetta_stone import (
            load_ground_truth, decode_coordinate, parse_ascii_v270_copper,
            _build_component_record_groups, load_component_layers,
            load_component_rotations, find_text11_file,
        )

        # Auto-discover text11 file for this specific board
        text11_path = find_text11_file(self.filepath)
        if text11_path is None:
            # Fallback: the ASCII rosetta file itself may be text11 format,
            # in which case the same file serves both roles.
            text11_path = ascii_path
            logger.info("[rosetta_stone] Using ASCII rosetta as text11 source: %s", text11_path)
        else:
            logger.info("[rosetta_stone] Auto-discovered text11 file: %s", text11_path)
        print(f"[rosetta_stone] text11 source: {text11_path}", file=sys.stderr)

        # Load text11 positions for this board
        gt = load_ground_truth(text11_path)

        # Load component layers (top/bottom) from text11 pad data
        comp_layers = load_component_layers(text11_path)

        # Load component rotations from text11 (more reliable than binary parser)
        comp_rotations = load_component_rotations(text11_path)

        # Build binary record groups for pad centroid fallback
        record_groups = _build_component_record_groups(pcb)

        matched = 0
        pad_centroid = 0
        unmatched = 0
        unmatched_desigs = []
        rotation_overrides = 0

        for comp in pcb.components:
            # Assign layer from text11 data
            if comp.designator in comp_layers:
                comp.layer = comp_layers[comp.designator]

            # Override rotation from text11 data (binary parser rotation is unreliable)
            if comp.designator in comp_rotations:
                text11_rot = comp_rotations[comp.designator]
                if abs(comp.rotation - text11_rot) > 1 and abs(comp.rotation - text11_rot - 360) > 1 and abs(text11_rot - comp.rotation - 360) > 1:
                    rotation_overrides += 1
                comp.rotation = text11_rot

            if comp.designator in gt:
                comp.x, comp.y = gt[comp.designator]
                comp._position_source = 'ascii_exact'
                matched += 1
            else:
                # Try binary pad centroid fallback
                x, y, source = decode_coordinate(
                    comp.designator,
                    ascii_positions={},  # empty - we already checked gt
                    binary_pcb_data=pcb,
                    _record_groups=record_groups,
                )
                comp.x = x
                comp.y = y
                comp._position_source = source
                if source == 'binary_pad_centroid':
                    pad_centroid += 1
                else:
                    unmatched += 1
                    unmatched_desigs.append(comp.designator)

        total = len(pcb.components)
        logger.info(
            "[rosetta_stone] Positions: %d ascii_exact, %d binary_pad_centroid, "
            "%d unmatched (%d total), %d rotation overrides",
            matched, pad_centroid, unmatched, total, rotation_overrides,
        )
        print(
            f"[rosetta_stone] Positions: {matched} ascii_exact, "
            f"{pad_centroid} binary_pad_centroid, {unmatched} unmatched "
            f"({total} total), {rotation_overrides} rotation overrides",
            file=sys.stderr,
        )
        if unmatched_desigs:
            print(f"[rosetta_stone] Unmatched: {', '.join(sorted(unmatched_desigs))}",
                  file=sys.stderr)

        # ── Inject text11-only components (not found in binary) ──────
        from protel99_parser.rosetta_stone import parse_text11_footprints
        text11_footprints = parse_text11_footprints(text11_path)
        existing_desigs = {c.designator for c in pcb.components}
        text11_only = set(gt.keys()) - existing_desigs

        for d in sorted(text11_only):
            pcb.components.append(Component(
                index=len(pcb.components),
                designator=d,
                footprint=text11_footprints.get(d, ''),
                x=gt[d][0],
                y=gt[d][1],
                layer=comp_layers.get(d, 'F.Cu'),
                rotation=comp_rotations.get(d, 0.0),
                _position_source='text11_injected',
            ))

        if text11_only:
            logger.info("[rosetta_stone] Injected %d text11-only components", len(text11_only))
            print(
                f"[rosetta_stone] Injected {len(text11_only)} text11-only components "
                f"(total now {len(pcb.components)})",
                file=sys.stderr,
            )

        # Load copper data from ASCII v2.70
        copper = parse_ascii_v270_copper(ascii_path)
        pcb.tracks = copper['tracks']
        pcb.vias = copper['vias']
        pcb.fills = copper.get('fills', [])
        pcb.free_strings = copper.get('free_strings', [])
        pcb.board_outline = copper['board_outline']

        n_tracks = len(pcb.tracks)
        n_vias = len(pcb.vias)
        n_edges = len(pcb.board_outline)
        n_fills = len(pcb.fills)
        n_strings = len(pcb.free_strings)
        logger.info(
            "[rosetta_stone] Copper: %d tracks, %d vias, %d board edges, %d fills, %d strings",
            n_tracks, n_vias, n_edges, n_fills, n_strings,
        )
        print(
            f"[rosetta_stone] Copper: {n_tracks} tracks, {n_vias} vias, "
            f"{n_edges} board edges, {n_fills} fills, {n_strings} strings",
            file=sys.stderr,
        )

    def _apply_binary_fallback(self, pcb: PCBData):
        """Binary-only fallback: use pad centroid positions, no copper data.

        Called when no ASCII rosetta stone or KiCad ground truth is found.
        """
        from protel99_parser.rosetta_stone import (
            decode_coordinate, _build_component_record_groups,
        )

        record_groups = _build_component_record_groups(pcb)

        for comp in pcb.components:
            x, y, source = decode_coordinate(
                comp.designator,
                ascii_positions={},
                binary_pcb_data=pcb,
                _record_groups=record_groups,
            )
            comp.x = x
            comp.y = y
            comp._position_source = source

    def _parse_header(self, pcb: PCBData):
        if len(self.data) < 30:
            raise ValueError("File too small")
        if self.data[1] != MARKER_BYTE:
            raise ValueError(f"Bad marker: 0x{self.data[1]:02X}")

        end = 2
        while end < min(len(self.data), 50) and 0x20 <= self.data[end] <= 0x7e:
            end += 1
        version = self.data[2:end].decode('ascii').strip()

        if not version.startswith("PCB FILE 9 VERSION 2.70"):
            raise ValueError(f"Unsupported: '{version}'")
        pcb.version = version

    def _extract_records(self, pcb: PCBData):
        positions = []
        i = 0
        while i < len(self.data) - 1:
            if self.data[i + 1] == MARKER_BYTE and 1 <= self.data[i] <= 0x20:
                positions.append((i, self.data[i]))
            i += 1

        records = []
        for idx in range(len(positions)):
            start, rec_type = positions[idx]
            end = positions[idx + 1][0] if idx + 1 < len(positions) else len(self.data)
            records.append(RawRecord(offset=start, record_type=rec_type, data=self.data[start + 2:end]))

        pcb.all_records = records
        pcb.total_records = len(records)
        pcb.record_counts = dict(Counter(r.record_type for r in records))

    # Record types that start a component group
    _COMPONENT_START_TYPES = {0x06, 0x08, 0x04, 0x0B, 0x09, 0x0A}
    _DESIG_RE = re.compile(r'^([A-Z]+\d+)')
    _VALUE_RE = re.compile(r'^[A-Za-z0-9.\-+/]+')

    def _group_components(self, pcb: PCBData):
        """
        Group records into components using generalized pattern detection.

        Component boundaries are defined by records of types:
          0x06 (COMP), 0x08 (PSHP), 0x04 (FP), 0x0B (LIB), 0x09 (CFP), 0x0A (VIA)

        Each group runs from one start record to the next start record (or EOF).
        Within each group, extracts designator, value, footprint, pad count, and
        component name using record type semantics.

        For 0x06-started groups, also detects the legacy Pattern A (SMD) and
        Pattern B (THT) sub-patterns and uses their more specific extraction
        (name from second REC_06, designator from REC_02).
        """
        records = pcb.all_records
        types = [r.record_type for r in records]
        n = len(types)

        # Phase 1: Find all component group boundaries
        start_indices = [i for i in range(n)
                         if types[i] in self._COMPONENT_START_TYPES]

        # Phase 2: Classify 0x06 starts as Pattern A, B, or generic
        pattern_ab_starts = set()
        for i in range(n - 4):
            if types[i] != 0x06:
                continue
            # Pattern A (SMD): 06 17 17 02 17
            if (types[i+1] == 0x17 and types[i+2] == 0x17
                    and types[i+3] == 0x02 and types[i+4] == 0x17):
                pattern_ab_starts.add(i)
            # Pattern B (THT): 06 01 17 17
            elif (types[i+1] == 0x01
                  and types[i+2] == 0x17 and types[i+3] == 0x17):
                pattern_ab_starts.add(i)

        # Phase 3: Build component groups
        seen_designators: dict[str, int] = {}  # designator -> record index
        components = []

        for si in range(len(start_indices)):
            start_idx = start_indices[si]
            end_idx = start_indices[si + 1] if si + 1 < len(start_indices) else n

            comp = Component(index=len(components))
            start_rec = records[start_idx]
            start_type = types[start_idx]

            # -- Footprint: from start record --
            comp.footprint = start_rec.first_string()

            # -- Pattern A/B specific extraction for 0x06 groups --
            if start_idx in pattern_ab_starts:
                self._extract_pattern_ab(
                    comp, records, types, start_idx, end_idx)
            else:
                self._extract_generic(
                    comp, records, types, start_idx, end_idx, start_type)

            # Skip groups with no designator (non-component records)
            if not comp.designator:
                continue

            # Deduplicate: first occurrence wins
            if comp.designator in seen_designators:
                continue
            seen_designators[comp.designator] = start_idx

            # Attempt absolute position decoding
            self._decode_component_position(comp, records, start_idx, end_idx)

            components.append(comp)

        # Re-index after filtering
        for i, c in enumerate(components):
            c.index = i

        pcb.components = components
        logger.info("Grouped %d components (%d Pattern A/B, %d generic)",
                     len(components),
                     sum(1 for c in components
                         if seen_designators.get(c.designator, -1) in pattern_ab_starts),
                     sum(1 for c in components
                         if seen_designators.get(c.designator, -1) not in pattern_ab_starts))

    def _extract_pattern_ab(self, comp: Component, records: list,
                            types: list, start_idx: int, end_idx: int):
        """Extract fields using Pattern A (SMD) or Pattern B (THT) logic.

        Pattern A: 06 17 17 02(designator) 17 [06(name)] ...
        Pattern B: 06 01 17 17 ... (designator from REC_03 in pad block)
        """
        is_smd = (types[start_idx+1] == 0x17)  # Pattern A
        scan_header_end = min(end_idx, start_idx + 8)

        for j in range(start_idx + 1, scan_header_end):
            r = records[j]
            # Designator from REC_02 (Pattern A)
            if r.record_type == 0x02 and not comp.designator:
                s = r.first_string()
                m = self._DESIG_RE.match(s) if s else None
                if m:
                    comp.designator = m.group(1)
            # Name from second REC_06
            elif r.record_type == 0x06 and not comp.name:
                comp.name = r.first_string()

        # Scan body for metadata
        scan_start = start_idx + (6 if is_smd else 4)
        self._scan_body(comp, records, types, scan_start, end_idx)

    def _extract_generic(self, comp: Component, records: list,
                         types: list, start_idx: int, end_idx: int,
                         start_type: int):
        """Extract fields for generalized component pattern.

        Pattern: [start_type] GEO GEO TEXT(designator) GEO [value] PAD...
        Start types: 0x08 (PSHP), 0x04 (FP), 0x0B (LIB), 0x09 (CFP),
                     0x0A (VIA), 0x06 (non-AB COMP)
        """
        # For 0x0B and 0x09, footprint is actually a library ref
        if start_type in (0x0B, 0x09):
            comp.library_ref = comp.footprint

        self._scan_body(comp, records, types, start_idx + 1, end_idx)

    def _scan_body(self, comp: Component, records: list,
                   types: list, scan_start: int, end_idx: int):
        """Scan records in a component group body for designator, value, pads, etc."""
        values_collected = []

        for j in range(scan_start, end_idx):
            r = records[j]
            rtype = r.record_type

            if rtype == 0x17:
                fval = r.ascii_float()
                if fval is not None and fval in (0.0, 90.0, 180.0, 270.0, 360.0):
                    comp.rotation = fval

            elif rtype in (0x03, 0x02):
                s = r.first_string()
                if not s:
                    continue
                m = self._DESIG_RE.match(s)
                if m and not comp.designator:
                    comp.designator = m.group(1)
                elif not m:
                    vm = self._VALUE_RE.match(s)
                    if vm:
                        values_collected.append(vm.group())

            elif rtype in (0x04, 0x05):
                s = r.first_string()
                if s:
                    values_collected.append(s)

            elif rtype == 0x08:
                s = r.first_string()
                if s and not comp.pad_shape:
                    comp.pad_shape = s

            elif rtype == 0x09:
                s = r.first_string()
                if s and not comp.library_ref:
                    comp.library_ref = s

            elif rtype == 0x0B:
                s = r.first_string()
                if s:
                    comp.library_ref = s

            elif rtype == 0x01:
                comp.pin_count += 1

            elif rtype == 0x02:
                comp.wire_count += 1

        if values_collected:
            comp.value = values_collected[0]

    def _decode_component_position(self, comp: Component, records: list,
                                   start_idx: int, end_idx: int):
        """Try to decode absolute board position for a component.

        Searches all records in the component group for board-range coordinate
        pairs using decode_absolute_position(). Uses the first successful decode.

        NOTE: The absolute coordinate encoding in Protel 99 SE v2.70 is NOT fully
        resolved. decode_absolute_position() is a heuristic that finds some
        board-range uint16 pairs, but Y values may be unreliable. See
        coordinate_decoder.py module docstring for full encoding status.
        """
        from protel99_parser.coordinate_decoder import decode_absolute_position

        for j in range(start_idx, end_idx):
            r = records[j]
            pos = decode_absolute_position(r)
            if pos is not None:
                comp.x, comp.y = pos
                comp._position_source = (
                    f"rec[{j}] type=0x{r.record_type:02X} offset={r.offset}"
                )
                return

        # No position found - log for diagnostics
        desig = comp.designator or f"comp#{comp.index}"
        logger.info(
            "Position decode failed for %s: no board-range XY pair in records %d-%d",
            desig, start_idx, end_idx - 1
        )

    def _apply_ground_truth(self, pcb: PCBData, kicad_path: str | Path):
        """Override component positions with KiCad ground truth data.

        For each component with a matching designator in the ground truth,
        sets x, y, and _position_source. Unmatched components get
        x=0.0, y=0.0, _position_source='unmatched'.
        """
        from protel99_parser.coordinate_decoder import load_ground_truth_cached, lookup_position

        ground_truth = load_ground_truth_cached(str(kicad_path))

        matched = 0
        unmatched = 0
        unmatched_desigs = []

        for comp in pcb.components:
            pos = lookup_position(comp.designator, ground_truth)
            if pos is not None:
                comp.x, comp.y = pos
                comp._position_source = 'kicad_ground_truth'
                matched += 1
            else:
                comp.x = 0.0
                comp.y = 0.0
                comp._position_source = 'unmatched'
                unmatched += 1
                unmatched_desigs.append(comp.designator)

        logger.info(
            "Ground truth applied: %d matched, %d unmatched",
            matched, unmatched
        )
        print(
            f"[ground_truth] Applied: {matched} matched, {unmatched} unmatched",
            file=sys.stderr
        )
        if unmatched_desigs:
            print(
                f"[ground_truth] Unmatched: {', '.join(sorted(unmatched_desigs))}",
                file=sys.stderr
            )

    def _apply_copper_ground_truth(self, pcb: PCBData, kicad_path: str | Path):
        """Load copper tracks, vias, and board outline from KiCad ground truth.

        Populates pcb.tracks, pcb.vias, and pcb.board_outline.
        Prints summary counts to stderr for observability.
        """
        from protel99_parser.kicad_ground_truth import load_kicad_copper, load_kicad_board_outline

        tracks, vias = load_kicad_copper(kicad_path)
        pcb.tracks = tracks
        pcb.vias = vias

        pcb.board_outline = load_kicad_board_outline(kicad_path)

        n_tracks = len(pcb.tracks)
        n_vias = len(pcb.vias)
        n_edges = len(pcb.board_outline)
        print(
            f"[ground_truth] Copper ground truth: {n_tracks} tracks, "
            f"{n_vias} vias, {n_edges} board edges",
            file=sys.stderr
        )


# ============================================================
# Plotting
# ============================================================

# Board outline from Gerber GM1 (mils)
BOARD_OUTLINE = {
    'x_min': 6397, 'x_max': 13602,
    'y_min': 6822, 'y_max': 9420,
}


def _plot_component_positions(pcb: PCBData, save_path: str | None = None):
    """Plot component positions overlaid on the board outline rectangle.

    Requires matplotlib. Gracefully reports if not installed.
    """
    try:
        import matplotlib.pyplot as plt
        import matplotlib.patches as patches
    except ImportError:
        print("ERROR: matplotlib not installed. Install with: pip install matplotlib")
        print("Parser works without matplotlib; --plot requires it.")
        return

    positioned = [(c.designator or f"#{c.index}", c.x, c.y)
                  for c in pcb.components if c.x != 0.0 or c.y != 0.0]
    unpositioned = [c.designator or f"#{c.index}"
                    for c in pcb.components if c.x == 0.0 and c.y == 0.0]

    fig, ax = plt.subplots(1, 1, figsize=(14, 6))

    # Board outline rectangle
    bx = BOARD_OUTLINE['x_min']
    by = BOARD_OUTLINE['y_min']
    bw = BOARD_OUTLINE['x_max'] - bx
    bh = BOARD_OUTLINE['y_max'] - by
    rect = patches.Rectangle((bx, by), bw, bh,
                              linewidth=2, edgecolor='black',
                              facecolor='lightyellow', label='Board outline')
    ax.add_patch(rect)

    # Component positions
    if positioned:
        xs = [p[1] for p in positioned]
        ys = [p[2] for p in positioned]
        ax.scatter(xs, ys, c='blue', s=40, zorder=5, label=f'Components ({len(positioned)})')

        # Label each component
        for desig, x, y in positioned:
            ax.annotate(desig, (x, y), textcoords="offset points",
                        xytext=(5, 5), fontsize=7, color='darkblue')

    # Axis setup
    margin = 500
    ax.set_xlim(BOARD_OUTLINE['x_min'] - margin, BOARD_OUTLINE['x_max'] + margin)
    ax.set_ylim(BOARD_OUTLINE['y_min'] - margin, BOARD_OUTLINE['y_max'] + margin)
    ax.set_aspect('equal')
    ax.set_xlabel('X (mils)')
    ax.set_ylabel('Y (mils)')
    ax.set_title(f'{pcb.filename} - Component Positions\n'
                 f'{len(positioned)} positioned, {len(unpositioned)} unpositioned')
    ax.legend(loc='upper right')
    ax.grid(True, alpha=0.3)

    if unpositioned:
        ax.text(0.01, 0.01, f"Unpositioned: {', '.join(unpositioned)}",
                transform=ax.transAxes, fontsize=7, color='red',
                verticalalignment='bottom')

    plt.tight_layout()

    if save_path:
        plt.savefig(save_path, dpi=150, bbox_inches='tight')
        print(f"[plot] Saved component plot to: {save_path}", file=__import__('sys').stderr)
    else:
        # Save to file instead of showing (works in headless environments)
        out_path = Path(pcb.filename).stem + '_positions.png'
        plt.savefig(out_path, dpi=150)
        print(f"Plot saved to: {out_path}")
    print(f"  Positioned: {len(positioned)}/{len(pcb.components)} components")
    if positioned:
        print(f"  X range: {min(p[1] for p in positioned):.0f}-{max(p[1] for p in positioned):.0f}")
        print(f"  Y range: {min(p[2] for p in positioned):.0f}-{max(p[2] for p in positioned):.0f}")
    plt.close()


def _plot_copper(pcb: PCBData, save_path: str | None = None):
    """Plot copper tracks, vias, and board outline with per-layer coloring.

    F.Cu tracks: red, B.Cu tracks: blue, Edge.Cuts: black, vias: green circles.
    If save_path is given, saves PNG to that path. Otherwise calls plt.show().
    """
    import sys

    try:
        import matplotlib
        matplotlib.use('Agg')  # headless backend - must be before pyplot import
        import matplotlib.pyplot as plt
        from matplotlib.collections import LineCollection
    except ImportError:
        print("ERROR: matplotlib not installed. Install with: pip install matplotlib",
              file=sys.stderr)
        return

    if not pcb.tracks:
        print("[plot_copper] WARNING: No tracks loaded - skipping copper plot",
              file=sys.stderr)
        return

    fig, ax = plt.subplots(1, 1, figsize=(16, 7))

    # Board outline - thick black lines
    if pcb.board_outline:
        outline_segs = [[(x1, y1), (x2, y2)] for x1, y1, x2, y2 in pcb.board_outline]
        outline_lc = LineCollection(outline_segs, colors='black', linewidths=2,
                                    label=f'Edge.Cuts ({len(pcb.board_outline)})')
        ax.add_collection(outline_lc)

    # Separate tracks by layer
    fcu_segs = [[(t.x1, t.y1), (t.x2, t.y2)] for t in pcb.tracks if t.layer == 'F.Cu']
    bcu_segs = [[(t.x1, t.y1), (t.x2, t.y2)] for t in pcb.tracks if t.layer == 'B.Cu']
    other_segs = [[(t.x1, t.y1), (t.x2, t.y2)] for t in pcb.tracks
                  if t.layer not in ('F.Cu', 'B.Cu')]

    # B.Cu first (bottom layer drawn behind)
    if bcu_segs:
        bcu_lc = LineCollection(bcu_segs, colors='blue', linewidths=0.5, alpha=0.7,
                                label=f'B.Cu ({len(bcu_segs)})')
        ax.add_collection(bcu_lc)

    # F.Cu on top
    if fcu_segs:
        fcu_lc = LineCollection(fcu_segs, colors='red', linewidths=0.5, alpha=0.7,
                                label=f'F.Cu ({len(fcu_segs)})')
        ax.add_collection(fcu_lc)

    # Other layers (gray, if any)
    if other_segs:
        other_lc = LineCollection(other_segs, colors='gray', linewidths=0.3, alpha=0.5,
                                  label=f'Other ({len(other_segs)})')
        ax.add_collection(other_lc)

    # Vias - green circles
    if pcb.vias:
        vx = [v.x for v in pcb.vias]
        vy = [v.y for v in pcb.vias]
        sizes = [max(v.size * 0.05, 8) for v in pcb.vias]  # scale drill size for visibility
        ax.scatter(vx, vy, c='green', s=sizes, zorder=5, alpha=0.8,
                   label=f'Vias ({len(pcb.vias)})')

    ax.autoscale()
    ax.set_aspect('equal')
    ax.set_xlabel('X (mils)')
    ax.set_ylabel('Y (mils)')

    stem = Path(pcb.filename).stem
    ax.set_title(f'Copper - {stem}\n'
                 f'{len(pcb.tracks)} tracks, {len(pcb.vias)} vias, '
                 f'{len(pcb.board_outline)} board edges')
    ax.legend(loc='upper right', fontsize=8)
    ax.grid(True, alpha=0.2)

    plt.tight_layout()

    if save_path:
        plt.savefig(save_path, dpi=150, bbox_inches='tight')
        print(f"[plot_copper] Saved copper plot to: {save_path}", file=sys.stderr)
    else:
        plt.show()

    plt.close()


# ============================================================
# CLI
# ============================================================

def main():
    import sys
    import json

    usage = (
        "Usage: protel99-parse <file.PCB> [-o/--output PATH] [--ascii-rosetta PATH] "
        "[--pcblib PATH ...] [--ground-truth PATH] [--stats] [--components] "
        "[--position-stats] [--json] [--raw] [--plot] [--plot-copper] [--save-plot PATH]"
    )

    if len(sys.argv) < 2:
        print(usage, file=sys.stderr)
        sys.exit(1)

    if sys.argv[1] in ('-h', '--help'):
        print(usage)
        sys.exit(0)

    filepath = sys.argv[1]
    if not Path(filepath).is_file():
        print(f"Error: input file not found: {filepath}", file=sys.stderr)
        sys.exit(1)

    # Parse flags with optional value arguments
    ground_truth_override = None
    ascii_rosetta_override = None
    save_plot_path = None
    output_path = None
    pcblib_paths = []
    args = sys.argv[2:]
    flags = set()
    i = 0
    while i < len(args):
        if args[i] == '--ground-truth' and i + 1 < len(args):
            ground_truth_override = args[i + 1]
            i += 2
        elif args[i] == '--ascii-rosetta' and i + 1 < len(args):
            ascii_rosetta_override = args[i + 1]
            i += 2
        elif args[i] == '--save-plot' and i + 1 < len(args):
            save_plot_path = args[i + 1]
            i += 2
        elif args[i] in ('--output', '-o') and i + 1 < len(args):
            output_path = args[i + 1]
            i += 2
        elif args[i] == '--pcblib' and i + 1 < len(args):
            pcblib_paths.append(args[i + 1])
            i += 2
        else:
            flags.add(args[i])
            i += 1

    parser = Protel99Parser(filepath)
    try:
        pcb = parser.parse(
            ground_truth_path=ground_truth_override,
            ascii_rosetta_path=ascii_rosetta_override,
        )
    except ValueError as e:
        print(f"Error: cannot parse {filepath}: {e}", file=sys.stderr)
        sys.exit(1)

    # Generate KiCad output if -o / --output was specified
    if output_path:
        from protel99_parser.kicad_generator import generate_kicad_pcb
        from protel99_parser.pcblib_parser import parse_pcblib as _parse_pcblib

        # Load explicitly specified libraries, or pass None for auto-discovery
        libraries = None
        if pcblib_paths:
            libraries = {}
            for lib_path in pcblib_paths:
                try:
                    fps = _parse_pcblib(lib_path)
                    libraries[Path(lib_path).stem] = fps
                except Exception as e:
                    print(f"[output] WARNING: Failed to parse library {lib_path}: {e}", file=sys.stderr)

        stats = generate_kicad_pcb(pcb, output_path, libraries=libraries,
                                   pcb_source_path=filepath)
        print(
            f"[output] Generated {output_path}: {stats['component_count']} components, "
            f"{stats['track_count']} tracks, {stats['via_count']} vias, "
            f"{stats['skipped_count']} skipped",
            file=sys.stderr
        )

    if "--stats" in flags or not (flags - {'--position-stats'}):
        print(f"File: {pcb.filename}")
        print(f"Version: {pcb.version}")
        print(f"Size: {pcb.file_size:,} bytes")
        print(f"Records: {pcb.total_records}")
        print(f"Components: {len(pcb.components)}")
        print()
        print("Record types:")
        for rtype in sorted(pcb.record_counts.keys()):
            name = RECORD_TYPES.get(rtype, f"unknown_{rtype:02x}")
            count = pcb.record_counts[rtype]
            print(f"  0x{rtype:02X} {name:>12s}: {count:5d}")

    if "--components" in flags or not (flags - {'--position-stats'}):
        print()
        print(f"{'#':>3} {'Desig':<8} {'Value':<12} {'Footprint':<12} {'Pad':<10} {'LibRef':<14} {'Name':<16} {'Rot':>5} {'X':>7} {'Y':>7} {'Pins':>4} {'Wires':>5}")
        print("-" * 114)
        for c in pcb.components:
            print(
                f"{c.index:3d} {c.designator:<8} {c.value:<12} {c.footprint:<12} "
                f"{c.pad_shape:<10} {c.library_ref:<14} {c.name:<16} "
                f"{c.rotation:5.0f} {c.x:7.0f} {c.y:7.0f} {c.pin_count:4d} {c.wire_count:5d}"
            )

    if "--json" in flags:
        out = {
            "filename": pcb.filename,
            "version": pcb.version,
            "file_size": pcb.file_size,
            "total_records": pcb.total_records,
            "record_counts": {f"0x{k:02X}": v for k, v in pcb.record_counts.items()},
            "components": [
                {k: v for k, v in c.__dict__.items() if k != 'pad_records'}
                for c in pcb.components
            ],
        }
        print(json.dumps(out, indent=2))

    if "--plot" in flags:
        _plot_component_positions(pcb, save_path=save_plot_path)

    if "--plot-copper" in flags:
        _plot_copper(pcb, save_path=save_plot_path)

    if "--position-stats" in flags:
        matched, total, pct = pcb.position_coverage
        unmatched_count = total - matched
        print()
        print(f"Position coverage: {matched}/{total} ({pct:.1f}%)")
        positioned_sources = Counter(
            c._position_source for c in pcb.components
            if c._position_source and c._position_source not in ('unmatched', 'none', '')
        )
        for src, cnt in positioned_sources.most_common():
            print(f"  Positioned ({src}): {cnt}")
        print(f"  Unpositioned: {unmatched_count}")

        # Copper stats
        n_tracks = len(pcb.tracks)
        n_vias = len(pcb.vias)
        n_edges = len(pcb.board_outline)
        print(f"Copper: {n_tracks} tracks, {n_vias} vias, {n_edges} board edges")

        if unmatched_count > 0:
            unmatched_desigs = sorted(
                c.designator for c in pcb.components
                if not c._position_source or c._position_source in ('unmatched', 'none', '')
            )
            print(f"\nUnmatched designators ({len(unmatched_desigs)}):")
            for d in unmatched_desigs:
                print(f"  {d}")

    if "--raw" in flags:
        print(f"\n{'Offset':>8} {'Type':>6} {'Size':>5} {'Strings'}")
        print("-" * 60)
        for r in pcb.all_records[:200]:
            strs = ' | '.join(r.strings()[:3])
            fval = r.ascii_float()
            extra = f" float={fval}" if fval is not None else ""
            print(f"{r.offset:8d} 0x{r.record_type:02X}{r.type_name:>10s} {len(r.data):5d} {strs}{extra}")


if __name__ == "__main__":
    main()
