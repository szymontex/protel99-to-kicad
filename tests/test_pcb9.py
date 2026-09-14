"""Native `PCB FILE 9 VERSION 2.70` decoder (pcb9) - stream primitives on
synthetic bytes, plus a full-board check when the reference boards are present.
"""
import os
import struct
from pathlib import Path

import pytest

from protel99_parser import pcb9


def dim_bytes(mil: float) -> bytes:
    q = round(mil * 10000)
    return struct.pack("<HH", q >> 16, q & 0xFFFF)


class TestPrimitives:
    def test_coordinate_is_high_word_first_in_ten_thousandths(self):
        assert pcb9.dim(bytes.fromhex("BB1454F9")) == pytest.approx(34786.338, abs=1e-9)
        assert pcb9.dim(dim_bytes(41396.3385)) == pytest.approx(41396.3385, abs=1e-9)

    def test_string_is_length_prefixed_and_padded_to_even(self):
        s = pcb9.Stream(b"\x06\xa1SOT-23\x01\xa1M\xff\x02\xa1th")
        assert s.string() == "SOT-23"
        assert s.pos == 8
        assert s.string() == "M"          # odd length: one pad byte skipped
        assert s.pos == 12
        assert s.peek_marker() == b"th"

    def test_tags_go_into_one_global_state(self):
        s = pcb9.Stream(b"\x01\xa3\x12\x00" + b"\x03\xa3" + dim_bytes(20.0) + b"\x00\x00")
        s.tags()
        assert s.st_u16(0x01) == 18
        assert s.st_dim(0x03) == pytest.approx(20.0)
        assert s.pos == 10

    def test_block_filler_is_skipped_before_a_primitive(self):
        data = bytearray(b"\x00" * pcb9.BLOCK)
        data[pcb9.BLOCK - 4:pcb9.BLOCK] = pcb9.FILLER * 2
        data += struct.pack("<H", 4321)
        s = pcb9.Stream(bytes(data))
        s.pos = pcb9.BLOCK - 4
        assert s.u16() == 4321
        assert s.filler_skips == 1

    def test_unreadable_float_is_logged_and_parsing_continues(self):
        # a bit-flipped exponent digit: the true value is unknowable, the stream must stay in sync
        s = pcb9.Stream(b"\x17\xa1 2.70000000000000E+0\x1000\x00\x02\xa1th")
        value = s.float_str()
        assert isinstance(value, float)
        assert len(s.errors) == 1
        assert s.peek_marker() == b"th"


# Reference boards are not shipped: they belong to the archive this parser was
# written for. Point PCB9_REFERENCE_DIR at a directory holding them to run these
# checks; without it they skip. The counts are the ones the file's own header
# states, so a mismatch means the parser lost or invented an object.
REFERENCE_DIR = os.environ.get("PCB9_REFERENCE_DIR")

REFERENCE = [
    ("board-a.PCB",
     dict(components=244, tracks=3342, arcs=18, vias=122, fills=394, pads=516)),
    ("board-b.PCB",
     dict(components=91, tracks=872, arcs=8, vias=42, fills=4, pads=159)),
]


@pytest.mark.parametrize("name,expected", REFERENCE, ids=lambda v: v if isinstance(v, str) else "")
def test_reference_board_counts_match_header(name, expected):
    if not REFERENCE_DIR:
        pytest.skip("set PCB9_REFERENCE_DIR to run reference-board checks")
    path = Path(REFERENCE_DIR) / name
    if not path.exists():
        pytest.skip(f"reference board not available: {name}")
    b = pcb9.parse(path, strict=True)
    got = dict(
        components=len(b.components),
        tracks=len(b.tracks) + sum(len(c.tracks) for c in b.components),
        arcs=len(b.arcs) + sum(len(c.arcs) for c in b.components),
        vias=len(b.vias) + sum(len(c.vias) for c in b.components),
        fills=len(b.fills) + sum(len(c.fills) for c in b.components),
        pads=len(b.pads) + sum(len(c.pads) for c in b.components),
    )
    assert got == expected
    assert {k: b.counts[k] for k in expected} == expected
    assert b.unknown_tags == {}
    assert b.errors == []
