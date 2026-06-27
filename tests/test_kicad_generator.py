"""Tests for kicad_generator: coordinate transform, library-based footprint
emission, and minimal generate -> parse-back round trips.

All tests use synthetic PCBData / FootprintData - no sample boards required.
"""

from protel99_parser.kicad_generator import (
    _mils_to_kicad_mm,
    generate_kicad_pcb,
    MIL_TO_MM,
)
from protel99_parser import PCBData, Component, Track, Via
from protel99_parser.kicad_ground_truth import (
    load_kicad_ground_truth,
    load_kicad_copper,
    load_kicad_board_outline,
    _kicad_mm_to_mils,
)
from protel99_parser.pcblib_parser import FootprintData, PadInfo, LineInfo


# Coordinate transform tests

class TestCoordinateTransform:
    """Test _mils_to_kicad_mm against known reference values."""

    def test_board_corner_bottom_left(self):
        x, y = _mils_to_kicad_mm(6400.0, 7132.0)
        assert abs(x - 162.5600) < 0.01
        assert abs(y - 239.2680) < 0.01

    def test_board_corner_top_right(self):
        x, y = _mils_to_kicad_mm(13604.0, 9730.0)
        assert abs(x - 345.5416) < 0.01
        assert abs(y - 173.2788) < 0.01

    def test_round_trip_identity(self):
        """mils -> mm -> mils should return the original value (float precision)."""
        for x_mil, y_mil in [(6400, 7132), (10000, 8000), (13604, 9730)]:
            x_mm, y_mm = _mils_to_kicad_mm(x_mil, y_mil)
            x_back, y_back = _kicad_mm_to_mils(x_mm, y_mm)
            assert abs(x_back - x_mil) < 0.01
            assert abs(y_back - y_mil) < 0.01

    def test_y_axis_inversion(self):
        """Higher Y in mils should produce lower Y in mm (inverted axis)."""
        _, y_low = _mils_to_kicad_mm(5000, 7000)
        _, y_high = _mils_to_kicad_mm(5000, 9000)
        assert y_high < y_low


# Library-based footprint emission tests

class TestLibraryFootprintEmission:
    """generate_kicad_pcb emits real pad geometry from supplied footprint data."""

    @staticmethod
    def _make_test_fp():
        return FootprintData(
            name='TEST_FP',
            pads=[
                PadInfo(x=0.0, y=0.0, width=50.0, height=60.0,
                        drill=0.0, shape='rect', pin='1', layers='smd'),
                PadInfo(x=130.0, y=0.0, width=50.0, height=60.0,
                        drill=0.0, shape='rect', pin='2', layers='smd'),
            ],
            lines=[
                LineInfo(x1=-30.0, y1=-40.0, x2=160.0, y2=-40.0, width=8.0, layer=17),
            ],
            source_lib='test',
        )

    def test_library_pads_emitted(self, tmp_path):
        fp = self._make_test_fp()
        libraries = {'test_lib': {'TEST_FP': fp}}
        pcb = PCBData(
            filename="test.pcb",
            components=[
                Component(index=1, footprint="TEST_FP", designator="R1",
                          name="100k", value="100k", pin_count=2,
                          x=8000.0, y=8000.0, _position_source="test"),
            ],
        )
        out = tmp_path / "lib_test.kicad_pcb"
        stats = generate_kicad_pcb(pcb, out, libraries=libraries)
        content = out.read_text()
        assert '(pad "1" smd rect' in content
        assert '(pad "2" smd rect' in content
        assert stats['footprints_resolved'] == 1
        assert stats['footprints_fallback'] == 0

    def test_silk_lines_emitted(self, tmp_path):
        fp = self._make_test_fp()
        libraries = {'test_lib': {'TEST_FP': fp}}
        pcb = PCBData(
            filename="test.pcb",
            components=[
                Component(index=1, footprint="TEST_FP", designator="R1",
                          x=8000.0, y=8000.0, _position_source="test"),
            ],
        )
        out = tmp_path / "silk_test.kicad_pcb"
        generate_kicad_pcb(pcb, out, libraries=libraries)
        content = out.read_text()
        assert '(fp_line' in content
        assert 'F.SilkS' in content

    def test_tht_pads_with_drill(self, tmp_path):
        fp = FootprintData(
            name='THT_FP',
            pads=[
                PadInfo(x=0.0, y=0.0, width=60.0, height=60.0,
                        drill=32.0, shape='circle', pin='1', layers='tht'),
                PadInfo(x=100.0, y=0.0, width=60.0, height=60.0,
                        drill=32.0, shape='circle', pin='2', layers='tht'),
            ],
            source_lib='test',
        )
        libraries = {'test_lib': {'THT_FP': fp}}
        pcb = PCBData(
            filename="test.pcb",
            components=[
                Component(index=1, footprint="THT_FP", designator="J1",
                          x=8000.0, y=8000.0, _position_source="test"),
            ],
        )
        out = tmp_path / "tht_test.kicad_pcb"
        generate_kicad_pcb(pcb, out, libraries=libraries)
        content = out.read_text()
        assert '(pad "1" thru_hole circle' in content
        assert '(drill' in content
        assert '(attr through_hole)' in content

    def test_fallback_generic_pads(self, tmp_path):
        """With empty libraries, output uses generic pad fallback."""
        pcb = PCBData(
            filename="test.pcb",
            components=[
                Component(index=1, footprint="UNKNOWN_FP", designator="R1",
                          x=8000.0, y=8000.0, _position_source="test", pin_count=2),
            ],
        )
        out = tmp_path / "fallback_test.kicad_pcb"
        stats = generate_kicad_pcb(pcb, out, libraries={})
        content = out.read_text()
        assert '(pad "1" smd circle' in content
        assert stats['footprints_resolved'] == 0
        assert stats['footprints_fallback'] == 1

    def test_stats_dict_keys(self, tmp_path):
        pcb = PCBData(filename="test.pcb")
        out = tmp_path / "stats_test.kicad_pcb"
        stats = generate_kicad_pcb(pcb, out, libraries={})
        assert 'footprints_resolved' in stats
        assert 'footprints_fallback' in stats
        assert 'component_count' in stats
        assert 'skipped_count' in stats


# Minimal generation and round-trip tests

class TestMinimalGeneration:
    """generate_kicad_pcb with small synthetic PCBData."""

    def test_minimal_pcb(self, tmp_path):
        pcb = PCBData(
            filename="test.pcb",
            components=[
                Component(index=1, footprint="1206", designator="R1",
                          name="100k", value="100k", pin_count=2,
                          x=8000.0, y=8000.0, _position_source="test"),
            ],
            tracks=[
                Track(x1=8000, y1=8000, x2=9000, y2=8000, width=10.0, layer="F.Cu", net=0),
            ],
            vias=[
                Via(x=8500, y=8000, drill=32.0, size=52.0, layers=("F.Cu", "B.Cu"), net=0),
            ],
            board_outline=[
                (6397, 6822, 13602, 6822),
                (13602, 6822, 13602, 9420),
                (13602, 9420, 6397, 9420),
                (6397, 9420, 6397, 6822),
            ],
        )
        out = tmp_path / "minimal.kicad_pcb"
        stats = generate_kicad_pcb(pcb, out, libraries={})
        assert stats['component_count'] == 1
        assert stats['track_count'] == 1
        assert stats['via_count'] == 1
        assert stats['edge_count'] == 4
        assert stats['skipped_count'] == 0
        content = out.read_text()
        assert '(kicad_pcb' in content
        assert '(footprint "Protel_Import:1206"' in content
        assert '(segment ' in content
        assert '(via ' in content
        assert '(gr_line ' in content
        assert 'Edge.Cuts' in content
        assert content.strip().endswith(')')

    def test_unmatched_components_excluded(self, tmp_path):
        pcb = PCBData(
            filename="test.pcb",
            components=[
                Component(index=1, footprint="1206", designator="R1",
                          x=8000, y=8000, _position_source="test"),
                Component(index=2, footprint="1206", designator="R2",
                          x=0, y=0, _position_source="unmatched"),
                Component(index=3, footprint="1206", designator="R3",
                          x=0, y=0, _position_source=""),
            ],
        )
        out = tmp_path / "skip_test.kicad_pcb"
        stats = generate_kicad_pcb(pcb, out, libraries={})
        assert stats['component_count'] == 1
        assert stats['skipped_count'] == 2
        content = out.read_text()
        assert '"R1"' in content
        assert '"R2"' not in content
        assert '"R3"' not in content

    def test_minimal_round_trip(self, tmp_path):
        """Generate a minimal PCB and parse it back with the ground-truth loader.

        No board outline is supplied here, so the writer and the reader both use
        the default Y transform and component positions round-trip exactly. (When
        a board outline is present the writer derives the Y origin from it, which
        is exercised separately.)
        """
        pcb = PCBData(
            filename="test.pcb",
            components=[
                Component(index=1, footprint="1206", designator="R1",
                          name="100k", value="100k", pin_count=2,
                          x=8000.0, y=8000.0, _position_source="test"),
            ],
            tracks=[
                Track(x1=8000, y1=8000, x2=9000, y2=8000, width=10.0, layer="F.Cu", net=0),
            ],
            vias=[
                Via(x=8500, y=8000, drill=32.0, size=52.0, net=0),
            ],
        )
        out = tmp_path / "rt_test.kicad_pcb"
        generate_kicad_pcb(pcb, out, libraries={})

        positions = load_kicad_ground_truth(out)
        assert "R1" in positions
        x_back, y_back = positions["R1"]
        assert abs(x_back - 8000.0) < 0.01
        assert abs(y_back - 8000.0) < 0.01

        tracks, vias = load_kicad_copper(out)
        assert len(tracks) == 1
        assert len(vias) == 1

    def test_board_dimensions_round_trip(self, tmp_path):
        """Board outline mil extents round-trip to the expected mm dimensions."""
        pcb = PCBData(
            filename="test.pcb",
            board_outline=[
                (6397, 6822, 13602, 6822),
                (13602, 6822, 13602, 9420),
                (13602, 9420, 6397, 9420),
                (6397, 9420, 6397, 6822),
            ],
        )
        out = tmp_path / "dims.kicad_pcb"
        generate_kicad_pcb(pcb, out, libraries={})
        edges = load_kicad_board_outline(out)
        xs, ys = [], []
        for x1, y1, x2, y2 in edges:
            xs.extend([x1, x2])
            ys.extend([y1, y2])
        width_mm = (max(xs) - min(xs)) * MIL_TO_MM
        height_mm = (max(ys) - min(ys)) * MIL_TO_MM
        assert width_mm > 0
        assert height_mm > 0
