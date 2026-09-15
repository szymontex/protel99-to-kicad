"""Format detection and the PCB FILE 6 reader, against synthetic files.

Detection is tested on headers alone because that is all detection reads. The
reader is tested on a board assembled here rather than on a real file: real
boards are not bundled with this repository.
"""
from __future__ import annotations

import pytest

from protel99_parser import formats, pcb6


def write(tmp_path, name, data: bytes):
    p = tmp_path / name
    p.write_bytes(data)
    return p


# --------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------

@pytest.mark.parametrize("head,key", [
    (b"\x17\xa1PCB FILE 9 VERSION 2.70\x00", "pcb9"),
    (b"PCB FILE 6 VERSION 2.80\r\n0 1 2 3", "pcb6"),
    (b"PCB FILE 5 VERSION 3.0\r\n", "pcb5"),
    (b"PCB FILE 4 VERSION 1.0\r\n", "pcb4"),
    (b"\x13PCB 4.0 Binary File" + b"\x00" * 40, "advpcb"),
    (b"\x1bPCB 4.0 Binary Library File" + b"\x00" * 30, "advpcb_lib"),
])
def test_identify(tmp_path, head, key):
    assert formats.identify(write(tmp_path, "b.pcb", head)).key == key


def test_library_wins_over_board(tmp_path):
    """`PCB 4.0 Binary Library File` starts with the board magic's text.

    Ordering in FORMATS is what keeps these apart. If the board entry were
    tested first a library would be reported as a board, and the difference
    matters: one holds footprints, the other a layout.
    """
    p = write(tmp_path, "lib.pcb", b"\x1bPCB 4.0 Binary Library File" + b"\x00" * 30)
    assert formats.identify(p).key == "advpcb_lib"


def test_unknown_header(tmp_path):
    p = write(tmp_path, "x.pcb", b"not a protel file at all" + b"\x00" * 40)
    assert formats.identify(p) is None
    with pytest.raises(formats.UnsupportedFormat) as e:
        formats.parse(p)
    assert "unrecognised" in str(e.value)


def test_recognised_but_undecoded_says_so(tmp_path):
    """A format we can name but not read must not look like an unknown file.

    The two call for different next steps - one is a gap in this package, the
    other may not be a Protel board at all - so the message distinguishes them.
    """
    p = write(tmp_path, "adv.pcb", b"\x13PCB 4.0 Binary File" + b"\x00" * 40)
    with pytest.raises(formats.UnsupportedFormat) as e:
        formats.parse(p)
    msg = str(e.value)
    assert "not" in msg and "decoded" in msg
    assert "PCB 4.0 Binary File" in msg


def test_every_format_has_a_distinct_key():
    keys = [f.key for f in formats.FORMATS]
    assert len(keys) == len(set(keys))


def test_readers_declare_an_outline_layer():
    for f in formats.FORMATS:
        if f.reader is not None:
            assert f.outline_layer > 0


# --------------------------------------------------------------------------
# PCB FILE 6 reader
# --------------------------------------------------------------------------

def board_text(records: str, counts: str = "0 1 0 0 0 0 1 0 2 0") -> bytes:
    return ("PCB FILE 6 VERSION 2.80\r\n" + counts + "\r\n"
            + records + "ENDPCB\r\n").encode("latin-1")


def test_reads_a_component_with_designator_and_comment(tmp_path):
    rec = ("COMP\r\nDIP14\r\n0 0 1000000 2000000 1 0 0 0 0 0 0  0.000 1 1 1\r\n"
           "CS\r\n0 0 1000000 2000000 60000 0.000 0 1 17 0 0 0 0 10000 1 0 0\r\nU1\r\n"
           "CS\r\n0 0 0 0 60000 0.000 0 1 17 0 0 0 0 10000 1 0 0\r\n7400\r\n"
           "ENDCOMP\r\n")
    b = pcb6.parse(write(tmp_path, "b.pcb", board_text(rec)))
    assert len(b.components) == 1
    c = b.components[0]
    assert c.footprint == "DIP14"
    assert (c.x, c.y) == (1000.0, 2000.0)          # 1/1000 mil -> mil
    assert c.designator.text == "U1"
    assert c.comment.text == "7400"
    assert c.texts == []                            # neither lands on the list


def test_component_string_without_a_position_moves_to_the_component(tmp_path):
    """A comment written as 0 0 must not stay at the board origin.

    Left alone it draws every comment in the archive in one heap outside the
    outline, and - because the estimated text box is what the board extent is
    measured from - drags the extent back to the origin with it.
    """
    rec = ("COMP\r\nSO8\r\n0 0 5000000 5000000 1 0 0 0 0 0 0  0.000 1 1 1\r\n"
           "CS\r\n0 0 5000000 5000000 60000 0.000 0 1 17 0 0 0 0 10000 1 0 0\r\nU9\r\n"
           "CS\r\n0 0 0 0 60000 0.000 0 1 17 0 0 0 0 10000 1 0 0\r\nLM358\r\n"
           "ENDCOMP\r\n")
    b = pcb6.parse(write(tmp_path, "b.pcb", board_text(rec)))
    c = b.components[0]
    assert (c.comment.x, c.comment.y) == (5000.0, 5000.0)
    assert c.comment.bbox[0] >= 5000.0
    assert c.comment.bbox[1] >= 5000.0


def test_text_box_is_never_all_zero(tmp_path):
    """An all-zero box is truthy, so it would be measured as a real corner."""
    rec = ("FS\r\n0 0 3000000 4000000 50000 0.000 0 1 17 0 0 0 0 10000 1 0 0\r\nHELLO\r\n")
    b = pcb6.parse(write(tmp_path, "b.pcb", board_text(rec)))
    t = b.texts[0]
    assert t.bbox != (0.0, 0.0, 0.0, 0.0)
    assert t.bbox[2] > t.bbox[0] and t.bbox[3] > t.bbox[1]


def test_free_objects_land_on_the_board(tmp_path):
    rec = ("FT\r\n0 0 100000 200000 300000 400000 10000 1 0 0 0\r\n 0 0 1 0 0\r\n"
           "FV\r\n0 0 500000 600000 50000 25000 0 0 17 0 0 0 1 0 0 1 16\r\n")
    b = pcb6.parse(write(tmp_path, "b.pcb", board_text(rec)))
    assert len(b.tracks) == 1 and len(b.vias) == 1
    t = b.tracks[0]
    assert (t.x1, t.y1, t.x2, t.y2) == (100.0, 200.0, 300.0, 400.0)
    assert t.width == 10.0 and t.layer == 1
    v = b.vias[0]
    assert (v.x, v.y) == (500.0, 600.0)
    assert v.diameter == 50.0 and v.hole == 25.0


def test_counts_hold_the_header_not_the_parse(tmp_path):
    """`Board.counts` is the file's claim, so the header check means something.

    Filling both sides from the same parse makes a reader agree with itself on
    every board - a green check that tests nothing.
    """
    rec = ("FT\r\n0 0 100000 200000 300000 400000 10000 1 0 0 0\r\n 0 0 1 0 0\r\n")
    b = pcb6.parse(write(tmp_path, "b.pcb", board_text(rec, "0 7 0 0 0 0 0 0 0 0")))
    assert b.counts["tracks"] == 7          # the header said 7
    assert len(b.tracks) == 1               # the stream held 1
    assert pcb6.header_disagreements(b)["tracks"] == (7, 1)


def test_damaged_record_resyncs_instead_of_derailing(tmp_path):
    """Bit-flipped bytes appear inside otherwise sound records in real archives.

    The reader must lose that record and keep the rest, recording where.
    """
    rec = ("FT\r\n0 0 100000 200000 300000 400000 10000 1 0 0 0\r\n 0 0 1 0 0\r\n"
           "FT\r\n0 0 1p0000 200000 300000 400000 10000 1 0 0 0\r\n 0 0 1 0 0\r\n"
           "FT\r\n0 0 700000 800000 900000 100000 10000 1 0 0 0\r\n 0 0 1 0 0\r\n")
    b = pcb6.parse(write(tmp_path, "b.pcb", board_text(rec)))
    assert len(b.tracks) == 2
    assert len(b.errors) == 1
    assert b.tracks[1].x1 == 700.0          # the reader found its way back


def test_rejects_a_foreign_header(tmp_path):
    with pytest.raises(pcb6.ParseError):
        pcb6.parse(write(tmp_path, "b.pcb", b"PCB FILE 9 VERSION 2.70\r\n0 0\r\n"))
