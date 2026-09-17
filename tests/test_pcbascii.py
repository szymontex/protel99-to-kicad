"""Protel PCB ASCII, and the design databases some of it is found inside.

Fixtures are the record examples printed in the vendor reference, `Protel 99 SE
PCB ASCII File Format Reference`, which is where the field names come from.
Both value spellings in circulation are exercised: the reference writes
` 1.00000000000000E+0001mil` and the editor writes `10mil`.
"""
from __future__ import annotations

import pytest

from protel99_parser import ddb, formats, pcbascii


def write(tmp_path, name, text: str):
    p = tmp_path / name
    p.write_bytes(text.encode("latin-1"))
    return p


BOARD = ("|RECORD=Board|FILENAME=demo.PCB|KIND=Protel_Advanced_PCB|VERSION=3.00\n")


def board(tmp_path, body: str, head: str = BOARD):
    return pcbascii.parse(write(tmp_path, "b.pcb", head + body))


# --------------------------------------------------------------------------
# values
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("10mil", 10.0),
    (" 1.00000000000000E+0001mil", 10.0),
    ("9995.2283mil", 9995.2283),
    ("-40mil", -40.0),
    ("25.4mm", 1000.0),                 # a millimetre value becomes mils
    ("0", 0.0),
])
def test_dimension_spellings(text, expected):
    assert pcbascii.dim(text) == pytest.approx(expected)


def test_missing_or_unreadable_dimension_falls_back(tmp_path):
    assert pcbascii.dim(None, 7.0) == 7.0
    assert pcbascii.dim("wide", 7.0) == 7.0


# --------------------------------------------------------------------------
# objects
# --------------------------------------------------------------------------

def test_track_from_the_reference_example(tmp_path):
    b = board(tmp_path,
              "|RECORD=Track|LAYER=BOTTOMSOLDER|X1= 6.18000000000000E+0003mil"
              "|Y1= 1.19000000000000E+0004mil|X2= 6.32000000000000E+0003mil"
              "|Y2= 1.19000000000000E+0004mil|WIDTH= 8.00000000000000E+0000mil\n")
    t = b.tracks[0]
    assert (t.x1, t.y1, t.x2, t.y2) == (6180.0, 11900.0, 6320.0, 11900.0)
    assert t.width == 8.0
    assert t.layer == 22               # bottom solder mask


def test_pad_from_the_reference_example(tmp_path):
    b = board(tmp_path,
              "|RECORD=Pad|LAYER=MULTILAYER|NAME=4|X= 3.52000000000000E+0003mil"
              "|Y= 8.22000000000000E+0003mil|XSIZE= 6.00000000000000E+0001mil"
              "|YSIZE= 6.00000000000000E+0001mil|SHAPE=ROUND"
              "|HOLESIZE= 3.00000000000000E+0001mil|ROTATION=0.000\n")
    p = b.pads[0]
    assert (p.x, p.y) == (3520.0, 8220.0)
    assert p.name == "4"
    assert p.hole == 30.0
    assert p.layer == 34
    assert p.top == (60.0, 60.0, 1)


def test_arc_and_via_and_fill(tmp_path):
    b = board(tmp_path,
              "|RECORD=Arc|LAYER=TOP|LOCATION.X=12220mil|LOCATION.Y=12620mil"
              "|RADIUS=1492.6487mil|STARTANGLE=0.000|ENDANGLE=360.000|WIDTH=10mil\n"
              "|RECORD=Via|LAYER=MULTILAYER|X=3560mil|Y=8240mil|DIAMETER=50mil"
              "|HOLESIZE=28mil\n"
              "|RECORD=Fill|LAYER=TOP|X1=10760mil|Y1=5000mil|X2=15340mil"
              "|Y2=7120mil|ROTATION=0.000\n")
    a, v, f = b.arcs[0], b.vias[0], b.fills[0]
    assert (a.x, a.y, a.radius) == (12220.0, 12620.0, pytest.approx(1492.6487))
    assert (a.start, a.end) == (0.0, 360.0)
    assert (v.diameter, v.hole) == (50.0, 28.0)
    assert (f.x1, f.y1, f.x2, f.y2) == (10760.0, 5000.0, 15340.0, 7120.0)


def test_text_carries_its_body_and_a_usable_box(tmp_path):
    b = board(tmp_path,
              "|RECORD=Text|LAYER=BOTTOM|X=3380mil|Y=8740mil|HEIGHT=60mil"
              "|FONT=DEFAULT|ROTATION=0.000|MIRROR=FALSE|TEXT=Testing|WIDTH=10mil\n")
    t = b.texts[0]
    assert t.text == "Testing"
    assert t.layer == 16
    assert t.bbox != (0.0, 0.0, 0.0, 0.0)
    assert t.bbox[2] > t.bbox[0] and t.bbox[3] > t.bbox[1]


# --------------------------------------------------------------------------
# what points at what
# --------------------------------------------------------------------------

COMPONENT = (
    "|RECORD=Net|ID=0|NAME=GND\n"
    "|RECORD=Net|ID=1|NAME=VCC\n"
    "|RECORD=Component|ID=0|LAYER=TOP|X=9995mil|Y=7508mil|PATTERN=DIP14"
    "|ROTATION=90.000\n"
    "|RECORD=Component|ID=1|LAYER=BOTTOM|X=100mil|Y=200mil|PATTERN=SO8"
    "|ROTATION=0.000\n"
    "|RECORD=Text|COMPONENT=0|LAYER=TOPOVERLAY|X=9995mil|Y=7600mil|HEIGHT=60mil"
    "|ROTATION=0.000|MIRROR=FALSE|TEXT=U1|WIDTH=10mil\n"
    "|RECORD=Text|COMPONENT=0|LAYER=TOPOVERLAY|X=9995mil|Y=7400mil|HEIGHT=60mil"
    "|ROTATION=0.000|MIRROR=FALSE|TEXT=7400|WIDTH=10mil\n"
    "|RECORD=Pad|COMPONENT=0|NET=1|LAYER=TOP|NAME=1|X=9870mil|Y=7165mil"
    "|XSIZE=80mil|YSIZE=24mil|SHAPE=ROUND|HOLESIZE=0mil|ROTATION=90.000\n"
    "|RECORD=Track|NET=0|LAYER=TOP|X1=0mil|Y1=0mil|X2=10mil|Y2=0mil|WIDTH=10mil\n"
    "|RECORD=Track|LAYER=TOP|X1=0mil|Y1=5mil|X2=10mil|Y2=5mil|WIDTH=10mil\n"
)


def test_objects_join_the_component_they_name(tmp_path):
    b = board(tmp_path, COMPONENT)
    assert len(b.components) == 2
    first = b.components[0]
    assert first.footprint == "DIP14"
    assert first.rotation == 90.0
    assert len(first.pads) == 1
    assert b.pads == []                     # nothing free


def test_a_bottom_component_is_mirrored(tmp_path):
    assert board(tmp_path, COMPONENT).components[1].mirror == 1


def test_the_first_two_strings_become_designator_and_comment(tmp_path):
    """The component record has no name of its own - only text pointing at it."""
    c = board(tmp_path, COMPONENT).components[0]
    assert c.designator.text == "U1"
    assert c.comment.text == "7400"
    assert c.texts == []


def test_a_component_with_no_strings_still_gets_both(tmp_path):
    """The KiCad writer places both unconditionally, so neither may be None."""
    c = board(tmp_path, COMPONENT).components[1]
    assert c.designator is not None and c.comment is not None
    assert c.designator.text == ""


def test_net_indices_shift_because_zero_means_no_net(tmp_path):
    b = board(tmp_path, COMPONENT)
    assert [n.name for n in b.nets] == ["GND", "VCC"]
    assert b.components[0].pads[0].net == 2      # NET=1 is the second net
    assert b.tracks[0].net == 1                  # NET=0 is the first
    assert b.tracks[1].net == 0                  # no NET= at all


def test_unknown_layer_is_not_guessed(tmp_path):
    b = board(tmp_path, "|RECORD=Track|LAYER=SOMETHINGELSE|X1=0mil|Y1=0mil"
                        "|X2=1mil|Y2=0mil|WIDTH=1mil\n")
    assert b.tracks[0].layer == pcbascii.UNKNOWN_LAYER


def test_file_without_records_is_rejected(tmp_path):
    with pytest.raises(pcbascii.ParseError):
        pcbascii.parse(write(tmp_path, "b.pcb", "PCB FILE 4\nENDPCB\n"))


def test_dispatcher_routes_ascii(tmp_path):
    p = write(tmp_path, "b.pcb", BOARD + COMPONENT)
    b, fmt = formats.parse(p)
    assert fmt.key == "pcbascii"
    assert "3.00" in b.version


# --------------------------------------------------------------------------
# design databases
# --------------------------------------------------------------------------

JET = b"\x00\x01\x00\x00Standard Jet DB\x00" + b"\x00" * 64


def test_database_with_an_ascii_board_yields_it(tmp_path):
    p = tmp_path / "p.ddb"
    p.write_bytes(JET + BOARD.encode() + COMPONENT.encode())
    b = ddb.parse(p)
    assert len(b.components) == 2
    assert b.version.endswith("in .ddb")


def test_database_with_a_text_board_carves_it_out(tmp_path):
    """`PCB FILE 4/5/6` documents end with `ENDPCB`, so both ends are findable."""
    doc = ("PCB FILE 4\r\nFT\r\n100 200 300 400 12 1 1\r\nENDPCB\r\n").encode()
    p = tmp_path / "p.ddb"
    p.write_bytes(JET + b"\x05\x00junk" + doc + b"\x00trailing junk")
    b = ddb.parse(p)
    assert len(b.tracks) == 1
    assert b.tracks[0].x1 == 100.0
    assert "in .ddb" in b.version


def test_database_holding_only_binary_says_what_is_in_it(tmp_path):
    p = tmp_path / "p.ddb"
    p.write_bytes(JET + b"\x13PCB 4.0 Binary File" + b"\x00" * 200
                  + b"Protel for Windows - Schematic Capture")
    with pytest.raises(ddb.ParseError) as e:
        ddb.parse(p)
    msg = str(e.value)
    assert "PCB 4.0 binary board" in msg and "schematic" in msg
    assert "save the board as PCB ASCII" in msg


def test_database_with_no_board_at_all_does_not_tell_you_to_save_the_board(tmp_path):
    """A project of schematics has no board to re-save, so saying so is wrong.

    The advice that fits a database full of undecoded boards is nonsense in a
    database that holds none, and a user following it looks for a board that
    was never there.
    """
    p = tmp_path / "p.ddb"
    p.write_bytes(JET + b"Protel for Windows - Schematic Capture" * 8)
    with pytest.raises(ddb.ParseError) as e:
        ddb.parse(p)
    msg = str(e.value)
    assert "no board document inside" in msg
    assert "8 x schematic" in msg
    assert "PCB ASCII" not in msg


# --------------------------------------------------------------------------
# picking one document out of a project
# --------------------------------------------------------------------------

def two_document_database() -> bytes:
    small = b"PCB FILE 4\r\nFT\r\n0 0 10 10 12 1 1\r\nENDPCB\r\n"
    big = ("PCB FILE 6 VERSION 2.80\r\n0 0 0 0 0 0 0 0 0 0\r\n"
           + "FT\r\n0 0 100000 200000 300000 400000 10000 1 0 0 0\r\n 0 0 1 0 0\r\n" * 20
           + "ENDPCB\r\n").encode()
    return JET + b"pad" * 50 + small + b"pad" * 50 + big + b"trailing"


def test_documents_are_listed_with_sizes_and_whether_they_can_be_read(tmp_path):
    p = tmp_path / "p.ddb"
    p.write_bytes(two_document_database() + b"\x13PCB 4.0 Binary File")
    docs = ddb.documents(p.read_bytes())
    assert [d.label for d in docs] == ["Autotrax board", "PCB ASCII board",
                                       "PCB 4.0 binary board"]
    assert [d.readable for d in docs] == [True, True, False]
    assert all(d.size > 0 for d in docs)


def test_a_document_stops_at_its_own_terminator(tmp_path):
    """Two boards stored back to back must not be read as one.

    Without the `ENDPCB` bound the first document would swallow the second and
    the reader would report a board that does not exist.
    """
    p = tmp_path / "p.ddb"
    p.write_bytes(two_document_database())
    data = p.read_bytes()
    first = ddb.documents(data)[0]
    assert data[first.start:first.end].endswith(b"ENDPCB")
    assert b"PCB FILE 6" not in data[first.start:first.end]


def test_the_largest_board_is_taken_by_default(tmp_path):
    """A project holds one layout and a pile of small documents."""
    p = tmp_path / "p.ddb"
    p.write_bytes(two_document_database())
    assert "PCB FILE 6" in ddb.parse(p).version


@pytest.mark.parametrize("which,expected", [(0, "PCB FILE 4"), (1, "PCB FILE 6"),
                                            ("autotrax", "PCB FILE 4"),
                                            ("ascii", "PCB FILE 6")])
def test_a_document_can_be_picked_by_index_or_by_format(tmp_path, which, expected):
    p = tmp_path / "p.ddb"
    p.write_bytes(two_document_database())
    assert expected in ddb.parse(p, which).version


def test_picking_something_that_is_not_there_says_so(tmp_path):
    p = tmp_path / "p.ddb"
    p.write_bytes(two_document_database())
    with pytest.raises(ddb.ParseError):
        ddb.parse(p, 9)
    with pytest.raises(ddb.ParseError):
        ddb.parse(p, "eagle")


def test_every_readable_board_comes_out_not_just_the_first(tmp_path):
    """A batch run over a project must not silently drop its other layouts."""
    p = tmp_path / "p.ddb"
    p.write_bytes(two_document_database())
    found = ddb.boards(p)
    assert len(found) == 2
    assert {"PCB FILE 4", "PCB FILE 6 VERSION 2.80"} == {
        b.version.replace(" in .ddb", "") for b in found}


def test_a_pcb_file_9_document_is_found_and_marked_readable(tmp_path):
    """The binary this package decodes is worth carving out of a database too.

    No public `.ddb` in the sample set contains one, so what is tested here is
    that the document would be found and handed to the reader rather than the
    caller being told to go and re-save the board as text.
    """
    p = tmp_path / "p.ddb"
    p.write_bytes(JET + b"\x17\xa1PCB FILE 9 VERSION 2.70" + b"\x00" * 64)
    docs = ddb.documents(p.read_bytes())
    assert [d.label for d in docs] == ["PCB FILE 9 binary board"]
    assert docs[0].readable


def test_listing_names_the_documents_the_database_mentions(tmp_path):
    p = tmp_path / "p.ddb"
    p.write_bytes(two_document_database() + b"\x00board rev2.pcb\x00notes.txt")
    text = ddb.listing(p)
    assert "2 documents" in text
    assert "board rev2.pcb" in text
    assert "notes.txt" not in text


def test_database_with_options_but_no_objects_is_not_an_empty_board(tmp_path):
    """Saving a board as ASCII writes option records even when nothing is placed.

    Returning a board with nothing in it would look like a successful
    conversion of an empty design.
    """
    p = tmp_path / "p.ddb"
    p.write_bytes(JET + BOARD.encode() + b"|RECORD=PrinterOptions|DEVICE=\n")
    with pytest.raises(ddb.ParseError):
        ddb.parse(p)


def test_dispatcher_recognises_a_database(tmp_path):
    p = tmp_path / "p.ddb"
    p.write_bytes(JET + BOARD.encode() + COMPONENT.encode())
    b, fmt = formats.parse(p)
    assert fmt.key == "ddb"
    assert len(b.components) == 2


# --------------------------------------------------------------------------
# framing
# --------------------------------------------------------------------------

def test_a_component_with_no_bounding_box_does_not_drag_the_frame_to_zero(tmp_path):
    """Only `PCB FILE 9` stores a component box; the ASCII readers leave zeros.

    An all-zero box is truthy, so measuring it puts the corner (0, 0) inside
    the board extent. A board drawn at 30000 mil from the origin - which is
    where Protel puts one, the sheet corner being the origin - then comes out
    on a sheet ten times its own size with the board in one corner. Measured
    across 140 converted sample boards this affected 54 of them.
    """
    from protel99_parser import pcb9_kicad

    far = ("|RECORD=Component|ID=0|LAYER=TOP|X=30000mil|Y=28000mil|PATTERN=R\n"
           "|RECORD=Pad|COMPONENT=0|LAYER=TOP|NAME=1|X=30000mil|Y=28000mil"
           "|XSIZE=60mil|YSIZE=60mil|SHAPE=ROUND|HOLESIZE=30mil|ROTATION=0.000\n"
           "|RECORD=Track|LAYER=TOP|X1=30000mil|Y1=28000mil|X2=30500mil"
           "|Y2=28000mil|WIDTH=10mil\n")
    b = board(tmp_path, far)
    x0, y0, x1, y1 = pcb9_kicad.board_extent(b)
    assert x0 > 29000 and y0 > 27000
    assert x1 - x0 < 1000 and y1 - y0 < 1000
