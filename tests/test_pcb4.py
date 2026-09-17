"""The Autotrax and Easytrax reader, against boards assembled here.

The sample in the vendor's own `AUTOTFAQ.TXT` is used as the fixture wherever
it covers the case, because a reader that agrees with the published
specification is the thing worth testing. Real boards are not bundled with this
repository.
"""
from __future__ import annotations

import pytest

from protel99_parser import formats, pcb4


def write(tmp_path, name, text: str):
    p = tmp_path / name
    p.write_bytes(text.replace("\n", "\r\n").encode("latin-1"))
    return p


def board(tmp_path, body: str, header: str = "PCB FILE 4"):
    return pcb4.parse(write(tmp_path, "b.pcb", f"{header}\n{body}ENDPCB\n"))


# --------------------------------------------------------------------------
# the specification's own example
# --------------------------------------------------------------------------

SPEC_EXAMPLE = """COMP
SW2
RADO.2
PB RESET
 327 1507 60 3 10 7
 407 1507 60 3 10 7
225 1425 1 1 2
CP
225 1425 62 70 1 30 2 13
1
CP
225 1225 62 70 1 30 1 13
2
CT
300 1150 300 1500 12 7 1
ENDCOMP
FS
5775 1975 108 1 12 1
Component Side
FT
925 1475 1175 1225 12 1 1
FA
1425 9220 325 1 10 7
FF
6200 2295 6450 2315 1
FV
175 4150 50 28
FP
3475 4825 40 40 1 30 1 13
255
NETDEF
BAUDCLK
0
(
SW2-1
SW2-2
)
{
1 2 0
}
"""


def test_reads_the_published_example(tmp_path):
    b = board(tmp_path, SPEC_EXAMPLE)
    assert len(b.components) == 1
    assert pcb4.parsed_counts(b) == {
        "components": 1, "tracks": 2, "pads": 3, "texts": 3,
        "fills": 1, "arcs": 1, "vias": 1, "nets": 1,
    }
    assert b.errors == []


def test_component_primitives_stay_with_their_component(tmp_path):
    """A `C*` object belongs to the open component, an `F*` to the board.

    Getting this backwards still gives the right totals, which is why it is
    worth a test of its own.
    """
    b = board(tmp_path, SPEC_EXAMPLE)
    c = b.components[0]
    assert len(c.tracks) == 1 and len(c.pads) == 2
    assert len(b.tracks) == 1 and len(b.pads) == 1


def test_component_carries_its_names_and_reference_point(tmp_path):
    c = board(tmp_path, SPEC_EXAMPLE).components[0]
    assert c.footprint == "RADO.2"
    assert c.designator.text == "SW2"
    assert c.comment.text == "PB RESET"
    assert (c.x, c.y) == (225.0, 1425.0)     # xref yref, whole mils
    assert (c.designator.x, c.designator.y) == (407.0, 1507.0)


def test_units_are_whole_mils(tmp_path):
    """The later ASCII vintages count 1/1000 mil; this one does not."""
    t = board(tmp_path, "FT\n925 1475 1175 1225 12 1 1\n").tracks[0]
    assert (t.x1, t.y1, t.x2, t.y2) == (925.0, 1475.0, 1175.0, 1225.0)
    assert t.width == 12.0


def test_layers_are_translated_to_the_later_numbering(tmp_path):
    """Autotrax numbers bottom copper 6 and the silkscreen 7; nothing else does.

    Translating in the reader is what lets the KiCad writer stay ignorant of
    which generation produced the board.
    """
    b = board(tmp_path, "FT\n0 0 10 10 12 6 1\nFT\n0 0 10 10 12 7 1\n"
                        "FT\n0 0 10 10 12 12 1\n")
    assert [t.layer for t in b.tracks] == [16, 17, 28]


def test_multilayer_pad_becomes_a_through_pad(tmp_path):
    p = board(tmp_path, "FP\n100 200 62 70 1 30 1 13\nA1\n").pads[0]
    assert (p.x, p.y) == (100.0, 200.0)
    assert p.name == "A1"
    assert p.hole == 30.0
    assert p.layer == 34                      # multi layer
    assert p.top == p.mid == p.bot == (62.0, 70.0, 1)


def test_track_without_the_user_routed_flag(tmp_path):
    """Older files write six fields, not seven. Both forms occur in the wild."""
    b = board(tmp_path, "FT\n100 200 300 400 12 1\n")
    assert len(b.tracks) == 1 and b.errors == []
    assert b.tracks[0].layer == 1


def test_easytrax_header_is_accepted(tmp_path):
    b = board(tmp_path, "FT\n100 200 300 400 12 1 1\n", header="PCB FILE 5")
    assert b.version == "PCB FILE 5"
    assert len(b.tracks) == 1


def test_rejects_a_foreign_header(tmp_path):
    with pytest.raises(pcb4.ParseError):
        board(tmp_path, "", header="PCB FILE 6 VERSION 2.80")


# --------------------------------------------------------------------------
# arcs
# --------------------------------------------------------------------------

@pytest.mark.parametrize("mask,expected", [
    (15, (0.0, 360.0)),         # every quadrant: a full circle
    (1, (0.0, 90.0)),           # upper right only
    (3, (0.0, 180.0)),          # upper half
    (6, (90.0, 270.0)),         # left half
    (9, (270.0, 450.0)),        # right half, wrapping past 360
    (12, (180.0, 360.0)),       # lower half
])
def test_quadrant_mask_becomes_angles(mask, expected):
    assert pcb4.quadrant_angles(mask) == expected


def test_arc_reads_its_mask(tmp_path):
    a = board(tmp_path, "FA\n1425 9220 325 9 10 7\n").arcs[0]
    assert (a.x, a.y, a.radius) == (1425.0, 9220.0, 325.0)
    assert (a.start, a.end) == (270.0, 450.0)
    assert a.layer == 17


# --------------------------------------------------------------------------
# nets
# --------------------------------------------------------------------------

def test_net_nodes_reach_the_pads_they_name(tmp_path):
    """This format puts net membership nowhere but the node list.

    Pads carry no net field, so unless `DESIGNATOR-PADNAME` is resolved back to
    a pad the converted board has nets with nothing in them.
    """
    b = board(tmp_path, SPEC_EXAMPLE)
    pads = {p.name: p for p in b.components[0].pads}
    assert pads["1"].net == 1
    assert pads["2"].net == 1
    assert b.pads[-1].net == 0            # the free pad is in no net


def test_unresolvable_node_is_ignored_rather_than_guessed(tmp_path):
    body = SPEC_EXAMPLE.replace("SW2-1", "U99-1")
    b = board(tmp_path, body)
    pads = {p.name: p for p in b.components[0].pads}
    assert pads["1"].net == 0             # the node named a component that is absent
    assert pads["2"].net == 1


# --------------------------------------------------------------------------
# through the dispatcher
# --------------------------------------------------------------------------

@pytest.mark.parametrize("header,key", [("PCB FILE 4", "pcb4"), ("PCB FILE 5", "pcb5")])
def test_dispatcher_routes_both_headers(tmp_path, header, key):
    p = write(tmp_path, "b.pcb", f"{header}\nFT\n0 0 10 10 12 1 1\nENDPCB\n")
    b, fmt = formats.parse(p)
    assert fmt.key == key
    assert fmt.outline_layer == pcb4.OUTLINE_LAYER
    assert len(b.tracks) == 1
