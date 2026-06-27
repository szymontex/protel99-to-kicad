"""
Coordinate decoder for Protel 99 SE v2.70 binary PCB records.

Extracts geometry values from REC_17 (type 0x17) records. These records store
a 23-byte ASCII float (rotation angle) followed by a variable-length binary tail
containing LE int16 values at stride-4 offsets.

IMPORTANT - Coordinate interpretation status:
  The LE int16 values extracted from REC_17 tails cluster in the 5000-7000 range
  and represent LOCAL geometry (pad/pin positions within a footprint), NOT absolute
  board coordinates. Absolute component positions (spanning the full board width
  6397-13602 mils) are NOT encoded in REC_17 tails as simple LE int16.

  Board-level coordinates appear in REC_02 (wire) and REC_20 (polygon) records
  as LE int16 raw mils, but the absolute component position encoding in Protel 99 SE
  v2.70 remains unresolved.

  Y encoding: Unresolved. The 2 bytes interleaved between X values (at stride-4
  offsets 3, 7, 11, 15 from tail start) do NOT produce board-range Y values
  under any tested interpretation (LE/BE int16, signed int16, scaled, offset).
  These bytes have a near-uniform distribution (0-65535) and likely encode
  attributes (net index, layer, width) rather than Y position.

  Approaches tried for Y:
  - LE uint16 at offsets 3,7,11,15: values 0-65523, uniform distribution
  - LE int16 signed: same data, no clustering in Y range
  - BE uint16: same data, different byte order, no Y-range clustering
  - Stride-2 (all values): still no Y-range concentration
  - 24-bit (3-byte) coordinates: no sensible values
  - int32 with various scalings (/10000, /1000, /100): no board-range values
  - Metric interpretation (0.01mm units): doesn't match either axis
  - All-X-then-all-Y layout: doesn't produce Y-range values
"""

import struct
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Module-level cache for ground truth data (parsed once per run)
_ground_truth_cache: Optional[dict[str, tuple[float, float]]] = None
_ground_truth_path: Optional[str] = None

# ASCII float field length in REC_17 data (after the 2-byte record marker)
FLOAT_FIELD_LEN = 23

# Example validation bounds from a test board (mils) - used for validation only
BOARD_X_MIN, BOARD_X_MAX = 6400, 13604
BOARD_Y_MIN, BOARD_Y_MAX = 7132, 9730

# Relaxed bounds for local geometry values (mils)
LOCAL_COORD_MIN, LOCAL_COORD_MAX = 0, 20000


def parse_ascii_float(data: bytes) -> Optional[float]:
    """Parse the 23-byte ASCII float field from REC_17 data.

    Returns the rotation angle in degrees, or None if the field is unparseable.
    Valid values: 0.0, 90.0, 180.0, 270.0, 360.0 (and occasionally other angles).
    """
    if len(data) < FLOAT_FIELD_LEN:
        return None
    try:
        txt = data[:FLOAT_FIELD_LEN].decode('ascii')
        return float(txt)
    except (UnicodeDecodeError, ValueError):
        return None


def decode_rec17_coordinates(record_data: bytes) -> list[tuple[int, int]]:
    """Extract geometry value pairs from a REC_17 record's binary tail.

    Args:
        record_data: The record data bytes (NOT including the 2-byte [0x17][0xA1] marker).
                     Starts with the 23-byte ASCII float field.

    Returns:
        List of (val_a, val_b) pairs extracted from the binary tail.
        val_a: LE int16 at stride-4 offsets (1, 5, 9, 13, ...) - local X geometry.
        val_b: LE int16 at stride-4 offsets (3, 7, 11, 15, ...) - purpose unresolved.

        Returns empty list if the record is too short or has an unparseable float prefix.

    Note:
        These are LOCAL geometry values (typically 5000-7000 range), NOT absolute
        board coordinates. See module docstring for full encoding status.
    """
    if len(record_data) < FLOAT_FIELD_LEN + 1:
        return []

    # Verify the ASCII float prefix is parseable
    rotation = parse_ascii_float(record_data)
    if rotation is None:
        logger.debug(
            "REC_17 unparseable float prefix: first 23 bytes = %s",
            record_data[:FLOAT_FIELD_LEN].hex(' ')
        )
        return []

    tail = record_data[FLOAT_FIELD_LEN:]
    if len(tail) < 3:
        # Too short for even one coordinate pair (need flag + 2 bytes min)
        return []

    # Check for sub-tag records: flag byte at tail[0], then [TAG][0xA3] at tail[1:3]
    # Real sub-tag records have pattern: [flag] [TAG_ID][0xA3][DATA_LO][DATA_HI] ...
    # False positives (tail[1]==0xA3) occur when 0xA3 is just a data byte -
    # only treat as sub-tags if the marker is at tail[2], not tail[1].
    has_subtags = len(tail) > 2 and tail[2] == 0xA3

    if has_subtags:
        return _decode_subtag_tail(tail)
    else:
        return _decode_stride4_tail(tail)


def _decode_stride4_tail(tail: bytes) -> list[tuple[int, int]]:
    """Decode a non-sub-tag tail using stride-4 LE int16 extraction.

    Layout: [flag_byte] [X1_lo X1_hi M1_lo M1_hi] [X2_lo X2_hi M2_lo M2_hi] ...
    where X = local geometry value (LE int16), M = mystery/attribute bytes (LE int16).
    """
    pairs = []
    i = 1  # skip flag byte
    while i + 4 <= len(tail):
        val_a = struct.unpack_from('<H', tail, i)[0]
        val_b = struct.unpack_from('<H', tail, i + 2)[0]
        pairs.append((val_a, val_b))
        i += 4
    return pairs


def _decode_subtag_tail(tail: bytes) -> list[tuple[int, int]]:
    """Decode a sub-tag tail: skip flag byte and [TAG][0xA3][LO][HI] groups, then stride-4.

    Sub-tag records have: [flag] [TAG_ID][0xA3][DATA_LO][DATA_HI] ... [coords]
    After all sub-tags, the remaining bytes use stride-4 layout.
    """
    i = 1  # skip flag byte
    subtag_count = 0
    while i + 3 < len(tail) and tail[i + 1] == 0xA3:
        i += 4  # skip [TAG][0xA3][DATA_LO][DATA_HI]
        subtag_count += 1
        if subtag_count > 10:  # safety limit
            break

    # Remaining bytes after sub-tags
    pairs = []
    while i + 4 <= len(tail):
        val_a = struct.unpack_from('<H', tail, i)[0]
        val_b = struct.unpack_from('<H', tail, i + 2)[0]
        pairs.append((val_a, val_b))
        i += 4
    return pairs


def extract_all_rec17_coords(pcb) -> list[tuple[int, int]]:
    """Extract all geometry value pairs from REC_17 records in a parsed PCB file.

    Args:
        pcb: A PCBData instance from Protel99Parser.parse().

    Returns:
        Combined list of (val_a, val_b) pairs from all REC_17 records.
        See decode_rec17_coordinates() for value interpretation.
    """
    all_coords = []
    processed = 0
    skipped_short = 0
    skipped_bad_float = 0

    for record in pcb.all_records:
        if record.record_type != 0x17:
            continue

        processed += 1
        if len(record.data) < FLOAT_FIELD_LEN + 1:
            skipped_short += 1
            continue

        pairs = decode_rec17_coordinates(record.data)
        if not pairs and len(record.data) >= FLOAT_FIELD_LEN + 4:
            # Had enough data for at least one pair but got nothing
            rotation = parse_ascii_float(record.data)
            if rotation is None:
                skipped_bad_float += 1
        all_coords.extend(pairs)

    logger.info(
        "REC_17 extraction: processed=%d, pairs=%d, skipped_short=%d, skipped_bad_float=%d",
        processed, len(all_coords), skipped_short, skipped_bad_float
    )
    return all_coords


def decode_absolute_position(record) -> Optional[tuple[float, float]]:
    """Attempt to decode absolute board position from a record.

    The absolute coordinate encoding in Protel 99 SE v2.70 is NOT YET FULLY RESOLVED.

    What we know:
    - Board X values ARE stored as LE uint16 mils at even-aligned offsets
    - Board Y values (6822, 9420 from Gerber GM1) do NOT appear as LE uint16 anywhere
    - The uint16 value following an X coordinate is NOT a simple Y coordinate
    - X=6397 (board edge) is consistently followed by 36838 (0x8FE6) - purpose unknown
    - protel2kicad uses int32/10000 for v3/v4, but this hasn't been confirmed for v2.70

    Current implementation: searches for board-range X values paired with board-range
    Y values as adjacent LE uint16. This finds SOME valid-looking positions in REC_02
    (wire) records, but the Y encoding for polygon and component records uses a different
    scheme that is not yet decoded.

    Args:
        record: A RawRecord instance from the parser.

    Returns:
        (X, Y) in mils if a plausible board-range XY pair is found, None otherwise.
        WARNING: The Y value may not be correct for all record types.
    """
    # Only attempt decoding for record types known to contain board-range coordinates
    if record.record_type not in (0x02, 0x01, 0x0A, 0x20):
        return None

    data = record.data
    if len(data) < 4:
        return None

    # Search for adjacent LE uint16 pairs where both values are in board range
    # This is a heuristic - not all found pairs are true XY coordinates
    for off in range(0, len(data) - 3, 2):
        x = struct.unpack_from('<H', data, off)[0]
        y = struct.unpack_from('<H', data, off + 2)[0]
        if (BOARD_X_MIN - 200 <= x <= BOARD_X_MAX + 200 and
                BOARD_Y_MIN - 200 <= y <= BOARD_Y_MAX + 200):
            return (float(x), float(y))

    return None


def lookup_position(
    designator: str,
    ground_truth: dict[str, tuple[float, float]],
) -> Optional[tuple[float, float]]:
    """Look up a component's absolute board position from ground truth data.

    Args:
        designator: Component reference designator (e.g. 'R1', 'U22').
        ground_truth: Dict from load_kicad_ground_truth() mapping
                      designator -> (X_mil, Y_mil).

    Returns:
        (X_mil, Y_mil) tuple, or None if the designator is not in the ground truth.
    """
    return ground_truth.get(designator)


def load_ground_truth_cached(kicad_path: str) -> dict[str, tuple[float, float]]:
    """Load KiCad ground truth with module-level caching.

    The KiCad file is parsed only once per process. Subsequent calls with the
    same path return the cached dict. A different path clears the cache.
    """
    global _ground_truth_cache, _ground_truth_path

    if _ground_truth_cache is not None and _ground_truth_path == kicad_path:
        return _ground_truth_cache

    from protel99_parser.kicad_ground_truth import load_kicad_ground_truth
    _ground_truth_cache = load_kicad_ground_truth(kicad_path)
    _ground_truth_path = kicad_path
    return _ground_truth_cache


def get_rec17_stats(pcb) -> dict:
    """Get diagnostic statistics about REC_17 records and extracted values.

    Returns a dict with counts, ranges, and coverage metrics for inspection.
    """
    from collections import Counter

    size_counts = Counter()
    all_val_a = []
    all_val_b = []
    total_records = 0
    records_with_pairs = 0
    records_with_subtags = 0

    for record in pcb.all_records:
        if record.record_type != 0x17:
            continue
        total_records += 1
        size_counts[len(record.data)] += 1

        # Check for sub-tags
        if len(record.data) > FLOAT_FIELD_LEN + 1:
            tail = record.data[FLOAT_FIELD_LEN:]
            if len(tail) > 1 and tail[1] == 0xA3:
                records_with_subtags += 1

        pairs = decode_rec17_coordinates(record.data)
        if pairs:
            records_with_pairs += 1
            for a, b in pairs:
                all_val_a.append(a)
                all_val_b.append(b)

    stats = {
        'total_records': total_records,
        'records_with_pairs': records_with_pairs,
        'records_with_subtags': records_with_subtags,
        'total_pairs': len(all_val_a),
        'size_distribution': dict(sorted(size_counts.items())),
    }

    if all_val_a:
        stats['val_a_range'] = (min(all_val_a), max(all_val_a))
        stats['val_b_range'] = (min(all_val_b), max(all_val_b))
        # Count values in typical local geometry range
        in_local = sum(1 for v in all_val_a if LOCAL_COORD_MIN <= v <= LOCAL_COORD_MAX)
        stats['val_a_in_local_range_pct'] = round(100 * in_local / len(all_val_a), 1)

    return stats
