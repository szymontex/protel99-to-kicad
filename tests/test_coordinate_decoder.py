"""Tests for coordinate_decoder: REC_17 geometry extraction and ASCII float parsing.

These are pure unit tests driven by synthetic byte payloads - no sample boards required.
"""

import struct

from protel99_parser.coordinate_decoder import (
    decode_rec17_coordinates,
    parse_ascii_float,
)


# ASCII float parsing

class TestParseAsciiFloat:
    def test_zero(self):
        data = b' 0.00000000000000E+0000'
        assert parse_ascii_float(data) == 0.0

    def test_270(self):
        data = b' 2.70000000000000E+0002'
        assert parse_ascii_float(data) == 270.0

    def test_360(self):
        data = b' 3.60000000000000E+0002'
        assert parse_ascii_float(data) == 360.0

    def test_90(self):
        data = b' 9.00000000000000E+0001'
        assert parse_ascii_float(data) == 90.0

    def test_too_short(self):
        assert parse_ascii_float(b' 0.0E+00') is None

    def test_non_ascii(self):
        assert parse_ascii_float(b'\xff' * 23) is None

    def test_23_bytes_required(self):
        """Float field is exactly 23 bytes - verify the size."""
        data = b' 0.00000000000000E+0000'
        assert len(data) == 23
        assert parse_ascii_float(data) is not None


# REC_17 decoding

class TestDecodeRec17Coordinates:
    def _make_record(self, rotation: float, tail: bytes) -> bytes:
        """Build a synthetic REC_17 data payload."""
        # Format: ' X.XXXXXXXXXXXXXXE+XXXX' = 23 bytes
        float_str = f' {rotation:.14E}'.encode('ascii')
        float_str = float_str[:23].ljust(23)
        return float_str + tail

    def test_empty_tail(self):
        record = self._make_record(0.0, b'')
        assert decode_rec17_coordinates(record) == []

    def test_short_tail(self):
        """Tails shorter than 3 bytes produce no pairs."""
        record = self._make_record(0.0, b'\x30\x00')
        assert decode_rec17_coordinates(record) == []

    def test_single_pair_tail(self):
        """Tail with flag + 4 bytes = one pair."""
        tail = b'\x30' + struct.pack('<HH', 6305, 12345)
        record = self._make_record(0.0, tail)
        pairs = decode_rec17_coordinates(record)
        assert len(pairs) == 1
        assert pairs[0] == (6305, 12345)

    def test_four_pair_tail(self):
        """17-byte tail (flag + 4x4 bytes) = four pairs - the most common variant."""
        tail = b'\x45'
        for x, m in [(6305, 59469), (6308, 29542), (6313, 24617), (6315, 50704)]:
            tail += struct.pack('<HH', x, m)
        record = self._make_record(0.0, tail)
        pairs = decode_rec17_coordinates(record)
        assert len(pairs) == 4
        assert pairs[0][0] == 6305
        assert pairs[3][0] == 6315

    def test_subtag_record(self):
        """Records with [TAG][0xA3] sub-tags are parsed after skipping sub-tags."""
        tail = (
            b'\x04'                     # flag byte
            b'\x01\xA3\x11\x00'         # sub-tag 0x01, data=0x0011
            b'\x14\xA3\x00\x00'         # sub-tag 0x14, data=0x0000
            + struct.pack('<HH', 5607, 3033)   # pair 1
            + struct.pack('<HH', 6507, 27878)  # pair 2
        )
        record = self._make_record(0.0, tail)
        pairs = decode_rec17_coordinates(record)
        assert len(pairs) == 2
        assert pairs[0][0] == 5607
        assert pairs[1][0] == 6507

    def test_bad_float_returns_empty(self):
        """Record with unparseable ASCII float prefix returns empty list."""
        record = b'\xff' * 30
        assert decode_rec17_coordinates(record) == []
