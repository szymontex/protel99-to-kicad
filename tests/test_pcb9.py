"""Native `PCB FILE 9` decoder (pcb9) - stream primitives and the record
layouts of every vintage on synthetic bytes, plus a full-board check when the
reference boards are present.
"""
import os
import re
import struct
from pathlib import Path

import pytest

from protel99_parser import pcb9, pcb9_kicad


def dim_bytes(mil: float) -> bytes:
    q = round(mil * 10000)
    return struct.pack("<HH", q >> 16, q & 0xFFFF)


def tag(number: int, data: bytes) -> bytes:
    return bytes([number, 0xA3]) + data


def u16(value: int) -> bytes:
    return struct.pack("<H", value)


def pstring(text: str) -> bytes:
    raw = text.encode("latin-1")
    return bytes([len(raw), 0xA1]) + raw + (b"\x00" if len(raw) & 1 else b"")


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


class TestVintages:
    """Advanced PCB 2.0 and 2.6 write the same stream with shorter records.

    Every figure here was measured by walking the markers of the demo boards
    that ship in both an Advanced PCB and a Protel for Windows vintage, and is
    confirmed by each file's own header counts: a record one field too short or
    too long desynchronises the stream and the counts stop matching.
    """

    def test_a_pad_before_270_ends_at_its_name_with_no_rotation(self):
        data = (tag(0x17, dim_bytes(50)) + tag(0x18, dim_bytes(60))
                + tag(0x19, dim_bytes(50)) + tag(0x1A, dim_bytes(60))
                + tag(0x1B, dim_bytes(52)) + tag(0x1C, dim_bytes(62))
                + tag(0x0C, dim_bytes(25)) + tag(0x01, u16(34))
                + tag(0x0E, dim_bytes(1000)) + tag(0x0F, dim_bytes(2000))
                + u16(1) + u16(1) + u16(2) + pstring("A1")
                + pstring("E"))
        s = pcb9.Stream(data, pcb9.V260)
        pad = pcb9.parse_pad(s)
        assert (pad.x, pad.y, pad.name) == (1000.0, 2000.0, "A1")
        assert pad.top == (50.0, 60.0, 1)
        assert pad.bot == (52.0, 62.0, 2)
        assert pad.rotation == 0.0 and pad.raw_tail == ()
        assert s.peek_marker() == b"E"        # the cursor stopped in the right place

    def test_version_200_has_one_pad_size_for_every_layer(self):
        data = (tag(0x09, dim_bytes(50)) + tag(0x0A, dim_bytes(60))
                + tag(0x0C, dim_bytes(25)) + tag(0x01, u16(34))
                + tag(0x0E, dim_bytes(1000)) + tag(0x0F, dim_bytes(2000))
                + u16(3) + pstring("7") + pstring("E"))
        s = pcb9.Stream(data, pcb9.V200)
        pad = pcb9.parse_pad(s)
        assert pad.top == pad.mid == pad.bot == (50.0, 60.0, 3)
        assert s.peek_marker() == b"E"

    def test_a_track_before_270_carries_one_trailing_u16(self):
        data = (tag(0x03, dim_bytes(8)) + tag(0x01, u16(1))
                + dim_bytes(100) + dim_bytes(300) + dim_bytes(200) + u16(0)
                + pstring("tv"))
        s = pcb9.Stream(data, pcb9.V260)
        track = pcb9.parse_track(s, "h")
        assert (track.x1, track.x2, track.y1) == (100.0, 300.0, 200.0)
        assert track.width == 8.0 and track.tail == (0,)
        assert s.peek_marker() == b"tv"

    def test_a_via_before_270_carries_one_trailing_u16(self):
        data = (tag(0x05, dim_bytes(50)) + tag(0x06, dim_bytes(28))
                + dim_bytes(1899) + dim_bytes(1859) + u16(1) + pstring("P"))
        s = pcb9.Stream(data, pcb9.V260)
        via = pcb9.parse_via(s)
        assert (via.x, via.y, via.diameter, via.hole) == (1899.0, 1859.0, 50.0, 28.0)
        assert via.raw_tail == (1,)
        assert s.peek_marker() == b"P"

    def test_a_net_tail_is_four_u16_in_260_and_none_in_200(self):
        body = (pstring("N") + pstring("IORQ") + dim_bytes(0) + dim_bytes(0)
                + u16(2) + u16(2)
                + pstring("(") + u16(1) + u16(4) + pstring(")")
                + pstring("{") + u16(1) + u16(2) + u16(3) + u16(4) + pstring("}"))
        s = pcb9.Stream(body + u16(2) + u16(1) + u16(0) + u16(34) + pstring("N"),
                        pcb9.V260)
        net = pcb9.parse_net(s, 0)
        assert net.name == "IORQ" and net.members == [(1, 4)]
        assert net.tail == (2, 1, 0, 34)
        assert s.peek_marker() == b"N"

        s = pcb9.Stream(body + pstring("N"), pcb9.V200)
        assert pcb9.parse_net(s, 0).tail == ()
        assert s.peek_marker() == b"N"


def empty_board(version: str) -> bytes:
    """A board with a header and every section present and empty."""
    head = pstring(version) + u16(0) + struct.pack("<9I", *([0] * 9)) + u16(0)
    sections = b"".join(pstring(m) for m in
                        ("G", "X", "TH", "TV", "T+", "T-", "TN", "F", "A", "V", "P", "D"))
    return head + sections


class TestVintageSelection:
    def test_each_known_vintage_picks_its_own_layout(self, tmp_path):
        for version, layout in (("PCB FILE 9 VERSION 2.70", pcb9.V270),
                                ("PCB FILE 9 VERSION 2.60", pcb9.V260),
                                ("PCB FILE 9 VERSION 2.00", pcb9.V200)):
            path = tmp_path / f"{layout.version}.PCB"
            path.write_bytes(empty_board(version))
            board = pcb9.parse(path, strict=True)
            assert board.version == version
            assert board.errors == []

    def test_a_vintage_nobody_has_decoded_is_refused_by_name(self, tmp_path):
        path = tmp_path / "later.PCB"
        path.write_bytes(empty_board("PCB FILE 9 VERSION 3.50"))
        with pytest.raises(pcb9.ParseError) as e:
            pcb9.parse(path)
        assert "3.50" in str(e.value) and "2.70" in str(e.value)


class TestHeaderWitness:
    """The file states its author's own object totals. A parse that does not
    reproduce them lost or invented records, and nothing raises when it does -
    so this check is the only thing between a quiet loss and a clean-looking
    conversion."""

    def board(self, counts: dict, **objects) -> pcb9.Board:
        b = pcb9.Board(Path("x.PCB"), "PCB FILE 9 VERSION 2.70", counts)
        for name, value in objects.items():
            setattr(b, name, value)
        return b

    def test_a_header_that_matches_the_parse_says_nothing(self):
        b = self.board({"tracks": 2, "vias": 1},
                       tracks=[object(), object()], vias=[object()])
        assert pcb9.header_disagreements(b) == {}

    def test_a_header_that_differs_names_both_numbers(self):
        b = self.board({"tracks": 5, "vias": 1},
                       tracks=[object(), object()], vias=[object()])
        assert pcb9.header_disagreements(b) == {"tracks": (5, 2)}

    def test_an_all_zero_header_is_not_a_disagreement(self):
        """Some exporters write the count line and leave it at zero - every
        `PCB FILE 6 VERSION 1.10` board seen does. Reading that as "this file
        should be empty" would flag every board from those tools and say
        nothing true about any of them."""
        counts = dict.fromkeys(
            ("components", "tracks", "pads", "texts", "fills", "arcs", "vias", "nets"), 0)
        b = self.board(counts, tracks=[object()] * 2381, arcs=[object()] * 194)
        assert pcb9.header_disagreements(b) == {}

    def test_a_counter_the_header_does_not_carry_is_not_compared(self):
        b = self.board({"tracks": 2}, tracks=[object(), object()],
                       vias=[object()] * 9)
        assert pcb9.header_disagreements(b) == {}


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


class TestConcerns:
    """Everything that goes wrong without raising has to reach the caller in
    words, or a conversion that lost objects reads exactly like one that did
    not."""

    def test_a_clean_conversion_says_nothing(self):
        assert pcb9_kicad.concerns({"parse_errors": 0, "header_disagreements": {},
                                    "net_index_out_of_range": 0,
                                    "unmapped_layers": []}) == []

    def test_each_kind_of_quiet_loss_gets_its_own_line(self):
        told = pcb9_kicad.concerns({
            "parse_errors": 3,
            "header_disagreements": {"nets": (1095, 780)},
            "net_index_out_of_range": 14857,
            "unmapped_layers": [23, 24],
        })
        assert len(told) == 4
        joined = " ".join(told)
        assert "nets says 1095, read 780" in joined
        assert "3 records" in joined
        assert "14857 objects" in joined
        assert "[23, 24]" in joined


def test_one_board_does_not_change_the_layer_map_of_the_next(tmp_path):
    """A batch converts boards from several generations in one process.

    The outline sits on layer 29 from `PCB FILE 9` 2.70 onwards and on 28
    before that and in `PCB FILE 6`. Building each board's layer map from the
    previous board's map instead of from the default drops 29 permanently the
    first time an older board goes through, and every later board then loses
    its outline - silently, because an unmapped layer is drawn on Cmts.User.
    """
    board = pcb9.Board(Path("x.PCB"), "PCB FILE 9 VERSION 2.70", {})
    board.tracks = [pcb9.Track(0, "h", 0.0, 0.0, 1000.0, 0.0, 10.0, 29, 0, ())]

    def outline_lines(layer: int) -> int:
        text, _ = pcb9_kicad.generate(board, layer)
        return text.count('"Edge.Cuts"')

    first = outline_lines(29)
    outline_lines(28)                       # an older generation goes through
    assert outline_lines(29) == first       # the newer one is unharmed
    assert pcb9_kicad.DEFAULT_LAYERS[29] == "Edge.Cuts"


class _FreePad:
    """Minimal stand-in for a pad outside any component."""
    x = y = 1000.0
    layer = 1
    hole = 30.0
    net = 0
    rotation = 0.0
    top = mid = bot = (50.0, 50.0, 1)

    def __init__(self, name):
        self.name = name


def test_pad_name_from_the_file_cannot_break_out_of_the_s_expression():
    """A string out of the binary must stay a string in the generated board.

    Pad names, footprint names and text all come from the file and all end up
    inside quoted atoms. One of the two places that write a pad name used the
    raw value, so a name holding a quote closed the atom and opened a node of
    the attacker's choosing - extra footprints, zones or copper in a board a
    designer then sends to a fab.
    """
    from protel99_parser import pcb9_kicad as K

    out = []
    K.emit_free_pad(out, K.Frame(0.0, 10000.0),
                    _FreePad('ID")) (footprint "EVIL'), None)
    blob = "\n".join(out)

    # Every quote from the file is escaped, so no atom ends early.
    assert '(property "Value" "ID\\")) (footprint \\"EVIL"' in blob

    # And the block still parses: strings blanked out, parentheses balance.
    depth = 0
    for ch in re.sub(r'"(?:[^"\\]|\\.)*"', '""', blob):
        depth += (ch == "(") - (ch == ")")
    assert depth == 0
