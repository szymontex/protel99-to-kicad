"""Tests for footprint_generator: KiCad .kicad_mod generation.

Pure unit tests built on synthetic FootprintData - no footprint libraries required.
"""

from __future__ import annotations

from protel99_parser.pcblib_parser import FootprintData, LineInfo, PadInfo
from protel99_parser.footprint_generator import generate_kicad_mod


def _smd_2pad() -> FootprintData:
    """A 2-pad SMD footprint with one silk line (60x50 mil pads at 0 and 130 mil)."""
    return FootprintData(
        name='SMD2',
        pads=[
            PadInfo(x=0.0, y=0.0, width=60.0, height=50.0,
                    drill=0.0, shape='rect', pin='1', layers='smd'),
            PadInfo(x=130.0, y=0.0, width=60.0, height=50.0,
                    drill=0.0, shape='rect', pin='2', layers='smd'),
        ],
        lines=[
            LineInfo(x1=-30.0, y1=-40.0, x2=160.0, y2=-40.0, width=8.0, layer=17),
        ],
        source_lib='test',
    )


def _tht_2pad() -> FootprintData:
    """A 2-pad through-hole footprint with drilled circular pads."""
    return FootprintData(
        name='THT2',
        pads=[
            PadInfo(x=0.0, y=0.0, width=60.0, height=60.0,
                    drill=32.0, shape='circle', pin='1', layers='tht'),
            PadInfo(x=100.0, y=0.0, width=60.0, height=60.0,
                    drill=32.0, shape='circle', pin='2', layers='tht'),
        ],
        source_lib='test',
    )


class TestGenerateKicadMod:
    """Generate .kicad_mod s-expressions from synthetic FootprintData."""

    def test_smd_pad_count(self):
        mod = generate_kicad_mod('SMD2', _smd_2pad())
        assert mod.count('(pad ') == 2

    def test_smd_pad_positions_mm(self):
        """Pads at 0 and 130 mil map to 0.0 mm and ~3.302 mm (130 * 0.0254)."""
        mod = generate_kicad_mod('SMD2', _smd_2pad())
        assert '(at 0.0 0.0)' in mod
        assert '(at 3.302' in mod

    def test_smd_pad_size_mm(self):
        """Pad size 60x50 mil maps to ~1.524 x 1.27 mm."""
        mod = generate_kicad_mod('SMD2', _smd_2pad())
        assert '(size 1.524 1.27)' in mod

    def test_smd_layers(self):
        mod = generate_kicad_mod('SMD2', _smd_2pad())
        assert '"F.Cu" "F.Paste" "F.Mask"' in mod
        assert '(attr smd)' in mod

    def test_smd_no_drill(self):
        mod = generate_kicad_mod('SMD2', _smd_2pad())
        assert '(drill ' not in mod

    def test_silk_lines(self):
        mod = generate_kicad_mod('SMD2', _smd_2pad())
        assert mod.count('(fp_line ') == 1
        assert '"F.SilkS"' in mod

    def test_footprint_wrapper(self):
        mod = generate_kicad_mod('SMD2', _smd_2pad())
        assert mod.startswith('(footprint "SMD2"')
        assert mod.rstrip().endswith(')')

    def test_balanced_parentheses_smd(self):
        mod = generate_kicad_mod('SMD2', _smd_2pad())
        assert mod.count('(') == mod.count(')')

    def test_tht_pad_count(self):
        mod = generate_kicad_mod('THT2', _tht_2pad())
        assert mod.count('(pad ') == 2

    def test_tht_has_drill(self):
        mod = generate_kicad_mod('THT2', _tht_2pad())
        assert mod.count('(drill ') == 2

    def test_tht_layers(self):
        mod = generate_kicad_mod('THT2', _tht_2pad())
        assert '"*.Cu" "*.Mask"' in mod
        assert '(attr through_hole)' in mod

    def test_balanced_parentheses_tht(self):
        mod = generate_kicad_mod('THT2', _tht_2pad())
        assert mod.count('(') == mod.count(')')

    def test_generated_from_minimal_footprint(self):
        """A single-pad SMD footprint generates a valid pad block."""
        fp = FootprintData(
            name='ONE',
            pads=[
                PadInfo(x=0.0, y=0.0, width=50.0, height=50.0,
                        drill=0.0, shape='rect', pin='1', layers='smd'),
            ],
        )
        mod = generate_kicad_mod('ONE', fp)
        assert '(pad "1" smd rect' in mod
        assert mod.count('(') == mod.count(')')
