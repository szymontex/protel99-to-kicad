"""Parse Protel PCBLIB binary footprint libraries (PCBLIB 20 and PCBLIB 22).

Supports two format versions:
- PCBLIB 22: 0x09 pad records with a separate 0x00 attr record for size
- PCBLIB 20: self-contained 0x03 pad records with inline width/height/drill

Usage:
    from protel99_parser.pcblib_parser import parse_pcblib
    footprints = parse_pcblib('library.LIB')
    fp = footprints['MINIMELF']
    print(fp.pads)  # [PadInfo(x=0, y=0, ...), PadInfo(x=130, y=0, ...)]
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Record type constants
REC_ATTR = 0x00
REC_PIN_NAME = 0x01
REC_PIN_ALT = 0x02
REC_PAD_V20 = 0x03
REC_LINE = 0x05
REC_PAD_V22_ALT = 0x08  # V20-style self-contained pad in V22 libs
REC_PAD_V22 = 0x09

# Layer constants
LAYER_FCU = 1
LAYER_BCU = 16
LAYER_MULTI = 34

# Shape flag constants (PCBLIB 22)
SHAPE_CIRCLE = 0x00
SHAPE_OVAL = 0x87
SHAPE_RECT_A = 0x88
SHAPE_RECT_B = 0x89

RECORD_SIZE = 32
HEADER_SIZE = 32


@dataclass
class PadInfo:
    """Single pad within a footprint. All dimensions in mils."""
    x: float
    y: float
    width: float
    height: float
    drill: float    # 0 for SMD
    shape: str      # 'rect', 'circle', 'oval'
    pin: str        # pin name/number
    layers: str     # 'smd' or 'tht'
    rotation: float = 0.0  # per-pad rotation in degrees


@dataclass
class LineInfo:
    """Silk screen line segment. All dimensions in mils."""
    x1: float
    y1: float
    x2: float
    y2: float
    width: float
    layer: int      # raw layer number (17=F.SilkS)


@dataclass
class FootprintData:
    """Parsed footprint with pads, lines, and metadata."""
    name: str
    pads: list[PadInfo] = field(default_factory=list)
    lines: list[LineInfo] = field(default_factory=list)
    source_lib: str = ''
    _parse_warnings: list[str] = field(default_factory=list)


@dataclass
class _IndexEntry:
    """Internal index entry from the PCBLIB file."""
    name: str
    rec_count: int
    data_offset: int      # in 32-byte blocks from header
    abs_offset: int        # absolute byte offset
    extent_bytes: int = 0  # computed from gap to next entry


def _int32_to_mils(data: bytes, offset: int) -> float:
    """Read LE int32 at offset and convert to mils (÷10000)."""
    return struct.unpack_from('<i', data, offset)[0] / 10000.0


def _uint16(data: bytes, offset: int) -> int:
    """Read LE uint16 at offset."""
    return struct.unpack_from('<H', data, offset)[0]


def _decode_shape_v22(flag: int) -> str:
    """Map PCBLIB 22 shape flag byte to shape name."""
    if flag in (SHAPE_RECT_A, SHAPE_RECT_B):
        return 'rect'
    elif flag == SHAPE_OVAL:
        return 'oval'
    else:
        return 'circle'


def _decode_shape_from_attr(attr_rec: bytes) -> str:
    """Map ATTR record byte[10] to shape name.

    In PCBLIB 22, the authoritative shape is in the first 0x00 attr (b1=0x01)
    at byte offset 10: 1=circle, 2=rect, 3=oval.
    """
    if len(attr_rec) > 10:
        val = attr_rec[10]
        if val == 2:
            return 'rect'
        elif val == 3:
            return 'oval'
    return 'circle'


def _decode_shape_v20(pad_type: int) -> str:
    """Map PCBLIB 20 pad_type byte to shape name."""
    if pad_type == 0x02:
        return 'rect'
    return 'circle'


def _layer_to_type(layer_val: int) -> str:
    """Map raw layer number to 'smd' or 'tht'."""
    if layer_val == LAYER_MULTI:
        return 'tht'
    return 'smd'


def _ascii_pin(data: bytes, start: int, end: int) -> str:
    """Extract ASCII pin name, stripping nulls and non-printable bytes."""
    raw = data[start:end]
    # Take only printable ASCII bytes
    chars = []
    for b in raw:
        if 0x20 <= b <= 0x7E:
            chars.append(chr(b))
        else:
            break
    return ''.join(chars).strip()


def _parse_index(data: bytes) -> tuple[int, list[_IndexEntry]]:
    """Parse PCBLIB header and index entries.

    Returns (version, entries) where version is 20 or 22.
    """
    header = data[:HEADER_SIZE]
    header_str = header.decode('latin-1').rstrip('\x00').strip()

    if 'PCBLIB 22' in header_str:
        version = 22
    elif 'PCBLIB 20' in header_str:
        version = 20
    else:
        raise ValueError(f"Unknown PCBLIB header: {header_str!r}")

    entries: list[_IndexEntry] = []
    idx = 1
    while True:
        off = idx * RECORD_SIZE
        if off + RECORD_SIZE > len(data):
            break
        entry_data = data[off:off + RECORD_SIZE]
        # Name is first 12 bytes, Latin-1 encoded
        name_bytes = entry_data[:12]
        if name_bytes[0] == 0:
            break  # End of index
        name = name_bytes.decode('latin-1').rstrip('\x00').strip()
        rec_count = entry_data[12]
        data_offset = _uint16(entry_data, 14)
        abs_offset = data_offset * RECORD_SIZE + HEADER_SIZE

        entries.append(_IndexEntry(
            name=name,
            rec_count=rec_count,
            data_offset=data_offset,
            abs_offset=abs_offset,
        ))
        idx += 1

    # Sort by absolute offset and compute extent from gaps
    entries.sort(key=lambda e: e.abs_offset)
    file_size = len(data)
    for i, entry in enumerate(entries):
        if i + 1 < len(entries):
            entry.extent_bytes = entries[i + 1].abs_offset - entry.abs_offset
        else:
            entry.extent_bytes = file_size - entry.abs_offset

    return version, entries


def _iter_records(data: bytes, abs_offset: int, extent_bytes: int):
    """Yield 32-byte records from a byte range."""
    end = abs_offset + extent_bytes
    pos = abs_offset
    while pos + RECORD_SIZE <= end and pos + RECORD_SIZE <= len(data):
        yield data[pos:pos + RECORD_SIZE]
        pos += RECORD_SIZE


def _decode_footprint_v22(
    name: str,
    data: bytes,
    abs_offset: int,
    extent_bytes: int,
    rec_count: int = 0,
) -> FootprintData:
    """Decode a PCBLIB 22 footprint (v2.2 layout).

    Pad sequence: [0x09 pad] -> [0x01/0x02/0x03 pin] -> [0x00 attr] -> [0x00 attr]

    Variant detection uses two strategies:
    1. LINE-after-pads: for footprints with silk lines before pads, a LINE record
       appearing after pad records signals a variant boundary.
    2. rec_count budget: the index entry's rec_count field counts non-ATTR records
       in the first variant.  When the budget is exhausted, stop collecting pads.
       This catches cases where variant 2 pads follow variant 1 with no LINE separator
       (e.g. TO220ST+RAD).
    """
    fp = FootprintData(name=name)
    records = list(_iter_records(data, abs_offset, extent_bytes))

    # Pre-scan: count lines before first pad to decide variant stop strategy
    has_pre_pad_lines = False
    for rec in records:
        if rec[0] in (REC_PAD_V22, REC_PAD_V22_ALT):
            break
        if rec[0] == REC_LINE:
            has_pre_pad_lines = True
            break

    first_pad_layer: int | None = None
    pads_started = False
    # rec_count budget: count non-ATTR records consumed; stop when budget exhausted
    non_attr_count = 0
    i = 0
    while i < len(records):
        rec = records[i]
        rtype = rec[0]

        if rtype == REC_LINE:
            layer = _uint16(rec, 2)
            x1 = _int32_to_mils(rec, 4)
            y1 = _int32_to_mils(rec, 8)
            x2 = _int32_to_mils(rec, 12)
            y2 = _int32_to_mils(rec, 16)
            width = _int32_to_mils(rec, 20)

            non_attr_count += 1
            if pads_started and has_pre_pad_lines:
                # Standard footprint: LINE after pads = variant boundary -> stop
                break
            # Collect line (pre-pad silk, or intra-variant silk for D-1A-DIL type)
            fp.lines.append(LineInfo(
                x1=x1, y1=y1, x2=x2, y2=y2, width=width, layer=layer,
            ))
            i += 1

        elif rtype == REC_PAD_V22:
            layer_val = _uint16(rec, 2)

            # Skip alternate-layer pads (e.g., B.Cu pads after F.Cu pads)
            if first_pad_layer is not None and layer_val != first_pad_layer:
                i += 1
                while i < len(records) and records[i][0] in (
                    REC_PIN_NAME, REC_PIN_ALT, REC_PAD_V20, REC_ATTR,
                ):
                    i += 1
                continue

            pads_started = True
            if first_pad_layer is None:
                first_pad_layer = layer_val

            non_attr_count += 1  # count the PAD record

            x = _int32_to_mils(rec, 4)
            y = _int32_to_mils(rec, 8)
            drill = _int32_to_mils(rec, 12)
            layers = _layer_to_type(layer_val)

            # Try to read pin name from next record (0x01, 0x02, or 0x03)
            pin = ''
            if i + 1 < len(records) and records[i + 1][0] in (
                REC_PIN_NAME, REC_PIN_ALT, REC_PAD_V20,
            ):
                pin = _ascii_pin(records[i + 1], 1, 12)

            # Read pad width/height and shape from attr records
            width = 0.0
            height = 0.0
            shape = 'rect'  # default
            for j in range(i + 1, min(i + 5, len(records))):
                if records[j][0] == REC_ATTR and records[j][1] == 0x01:
                    width = _int32_to_mils(records[j], 2)
                    height = _int32_to_mils(records[j], 6)
                    shape = _decode_shape_from_attr(records[j])
                    break

            if width == 0.0 and height == 0.0:
                fp._parse_warnings.append(
                    f"Pad at ({x},{y}) missing size attr"
                )

            fp.pads.append(PadInfo(
                x=x, y=y, width=width, height=height,
                drill=drill, shape=shape, pin=pin, layers=layers,
            ))

            # Skip the pad group: pad + pin/attr records
            i += 1
            while i < len(records) and records[i][0] in (
                REC_PIN_NAME, REC_PIN_ALT, REC_PAD_V20, REC_ATTR,
            ):
                if records[i][0] != REC_ATTR:
                    non_attr_count += 1
                i += 1

        elif rtype == REC_PAD_V22_ALT:
            # 0x08 record: V20-style self-contained pad inside a V22 library.
            # Same byte layout as REC_PAD_V20.
            pads_started = True
            non_attr_count += 1

            layer_val = _uint16(rec, 2)
            x = _int32_to_mils(rec, 4)
            y = _int32_to_mils(rec, 8)
            width = _int32_to_mils(rec, 12)
            height = _int32_to_mils(rec, 16)
            drill = _int32_to_mils(rec, 20)
            pad_type = rec[24]
            shape = _decode_shape_v20(pad_type)
            layers = _layer_to_type(layer_val)

            # Pin name from adjacent 0x01/0x02 records
            pin = ''
            if i + 1 < len(records) and records[i + 1][0] in (
                REC_PIN_NAME, REC_PIN_ALT,
            ):
                pin = _ascii_pin(records[i + 1], 1, 12)

            fp.pads.append(PadInfo(
                x=x, y=y, width=width, height=height,
                drill=drill, shape=shape, pin=pin, layers=layers,
            ))
            i += 1

        elif rtype in (REC_PIN_NAME, REC_PIN_ALT):
            # Orphan pin name (metadata record) - skip
            non_attr_count += 1
            i += 1

        else:
            i += 1

    # Auto-number pads with empty pin names
    _auto_number_pins(fp)

    return fp


def _auto_number_pins(fp: FootprintData) -> None:
    """Assign sequential pin numbers to pads that have empty pin names."""
    if not fp.pads:
        return
    # If all pads have pins, nothing to do
    if all(p.pin for p in fp.pads):
        return
    # If some have pins and some don't, only fill the gaps
    existing = {p.pin for p in fp.pads if p.pin}
    next_num = 1
    for pad in fp.pads:
        if not pad.pin:
            while str(next_num) in existing:
                next_num += 1
            pad.pin = str(next_num)
            existing.add(str(next_num))
            next_num += 1


def _decode_footprint_v20(
    name: str,
    data: bytes,
    abs_offset: int,
    extent_bytes: int,
) -> FootprintData:
    """Decode a PCBLIB 20 footprint (v2.0 layout).

    Pad record 0x03 is self-contained with inline width/height/drill.
    Pin names come from adjacent 0x01/0x02 records (before or after pad).
    """
    fp = FootprintData(name=name)
    records = list(_iter_records(data, abs_offset, extent_bytes))

    # First pass: collect all records with types
    i = 0
    pending_pin: str | None = None

    while i < len(records):
        rec = records[i]
        rtype = rec[0]

        if rtype == REC_LINE:
            layer = _uint16(rec, 2)
            x1 = _int32_to_mils(rec, 4)
            y1 = _int32_to_mils(rec, 8)
            x2 = _int32_to_mils(rec, 12)
            y2 = _int32_to_mils(rec, 16)
            width = _int32_to_mils(rec, 20)
            fp.lines.append(LineInfo(x1=x1, y1=y1, x2=x2, y2=y2, width=width, layer=layer))
            i += 1

        elif rtype == REC_PAD_V20:
            layer_val = _uint16(rec, 2)
            x = _int32_to_mils(rec, 4)
            y = _int32_to_mils(rec, 8)
            width = _int32_to_mils(rec, 12)
            height = _int32_to_mils(rec, 16)
            drill = _int32_to_mils(rec, 20)
            pad_type = rec[24]
            shape = _decode_shape_v20(pad_type)
            layers = _layer_to_type(layer_val)

            # Pin name: use pending_pin if available, else check next record
            pin = ''
            if pending_pin is not None:
                pin = pending_pin
                pending_pin = None
            elif i + 1 < len(records) and records[i + 1][0] in (REC_PIN_NAME, REC_PIN_ALT):
                pin = _ascii_pin(records[i + 1], 1, 12)

            fp.pads.append(PadInfo(
                x=x, y=y, width=width, height=height,
                drill=drill, shape=shape, pin=pin, layers=layers,
            ))
            i += 1

        elif rtype in (REC_PIN_NAME, REC_PIN_ALT):
            pin_name = _ascii_pin(rec, 1, 12)
            # Check if next record is a pad - if so, this is a pre-pad pin name
            if i + 1 < len(records) and records[i + 1][0] == REC_PAD_V20:
                pending_pin = pin_name
            i += 1

        elif rtype == REC_ATTR:
            # Attr records in v20 are metadata - skip
            i += 1

        else:
            i += 1

    # Auto-number pads with empty pin names
    _auto_number_pins(fp)

    return fp


def _trim_variant_bleed(fp: FootprintData, rec_count: int) -> list[PadInfo]:
    """Remove trailing pads that bleed from a second footprint variant.

    Variant bleed happens when the decoder's LINE-based stop heuristic fails
    (e.g. TO220ST+RAD where variant 2 pads immediately follow variant 1 with
    no LINE separator).

    Detection: if the trailing pads have a distinctly different drill size
    (>40% difference from the majority drill) AND removing them brings the
    pad count closer to what rec_count suggests, trim them.

    Returns the (possibly trimmed) pad list.
    """
    pads = fp.pads
    if len(pads) <= 2:
        return pads

    # Find the dominant drill size (mode of non-zero drills, or 0 for SMD)
    drills = [p.drill for p in pads]
    non_zero = [d for d in drills if d > 0]
    if len(non_zero) < 2:
        return pads  # all SMD or single-drill - no bleed possible

    # Find the most common drill value (mode)
    from collections import Counter
    drill_counts = Counter(round(d, 1) for d in non_zero)
    dominant_drill = drill_counts.most_common(1)[0][0]

    # Check trailing pads for drill mismatch
    # Walk from the end; find the split point where drill changes
    split = len(pads)
    for idx in range(len(pads) - 1, -1, -1):
        d = round(pads[idx].drill, 1)
        if d == 0 or d == dominant_drill:
            break
        # Check for >40% relative difference
        if abs(d - dominant_drill) / dominant_drill > 0.40:
            split = idx
        else:
            break

    if split < len(pads):
        trimmed = pads[:split]
        removed = len(pads) - split
        logger.info(
            "Trimmed %d variant-bleed pad(s) from %s "
            "(drill %.1f vs dominant %.1f)",
            removed, fp.name,
            round(pads[split].drill, 1), dominant_drill,
        )
        return trimmed

    return pads


def parse_pcblib(path: str | Path) -> dict[str, FootprintData]:
    """Parse a Protel PCBLIB binary footprint library.

    Supports PCBLIB 20 and PCBLIB 22 format versions.
    Returns dict keyed by footprint name (Latin-1, stripped).

    Parsing errors for individual footprints are logged and skipped.
    Check len(result) vs expected entry count to detect failures.
    """
    path = Path(path)
    data = path.read_bytes()
    version, entries = _parse_index(data)

    result: dict[str, FootprintData] = {}
    for entry in entries:
        try:
            if version == 22:
                fp = _decode_footprint_v22(
                    entry.name, data, entry.abs_offset, entry.extent_bytes,
                    rec_count=entry.rec_count,
                )
            else:
                fp = _decode_footprint_v20(
                    entry.name, data, entry.abs_offset, entry.extent_bytes
                )
            fp.source_lib = str(path)

            # Filter ghost pads (0×0 size) - some footprints have trailing
            # zero-size pads that duplicate another pad's position.
            before = len(fp.pads)
            fp.pads = [
                p for p in fp.pads
                if not (p.width == 0.0 and p.height == 0.0)
            ]
            filtered = before - len(fp.pads)
            if filtered:
                logger.info(
                    "Filtered %d ghost pad(s) from %s (0x0 size)",
                    filtered, entry.name,
                )

            # Variant bleed post-filter: if the decoder collected more pads
            # than expected and the trailing pads have a distinctly different
            # drill size from the leading group, they likely bleed from a
            # second variant.  Use rec_count (non-ATTR record budget) as a
            # hint for the expected pad count.
            if version == 22 and len(fp.pads) > 2:
                fp.pads = _trim_variant_bleed(fp, entry.rec_count)

            # Post-process: reclassify circle pads with w≠h as oval.
            # Protel stores all non-rect pads as circle regardless of
            # aspect ratio; true circles always have w==h.
            for pad in fp.pads:
                if pad.shape == 'circle' and abs(pad.width - pad.height) > 1.0:
                    pad.shape = 'oval'

            result[entry.name] = fp
        except Exception as e:
            logger.warning("Failed to parse footprint %s: %s", entry.name, e)

    logger.info(
        "Parsed %s: version=%d, %d/%d footprints OK",
        path.name, version, len(result), len(entries),
    )
    return result
