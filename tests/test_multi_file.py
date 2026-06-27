"""Version-string handling and ground-truth file discovery.

Pure unit tests: minimal binary headers and stem-derivation logic, built in
tmp_path - no sample boards required.
"""

from protel99_parser import Protel99Parser
from protel99_parser.kicad_ground_truth import find_ground_truth_file
import pytest


class TestVersionStringRelaxation:
    """Version prefix matching accepts v2.70 variants and rejects non-v2.70."""

    @pytest.mark.parametrize("version_suffix", ["", "n", "N", "0", "p"])
    def test_version_prefix_accepts_variants(self, version_suffix, tmp_path):
        """A minimal header with each v2.70 variant must not be rejected on version."""
        version_str = f"PCB FILE 9 VERSION 2.70{version_suffix}"
        header = bytes([0x00, 0xA1]) + version_str.encode("ascii") + b"\x00"
        content = header + b"\x00" * 256

        pcb_file = tmp_path / "test.PCB"
        pcb_file.write_bytes(content)

        parser = Protel99Parser(str(pcb_file))
        try:
            parser.parse(ground_truth_path=None)
        except ValueError as e:
            # Only a version rejection is a test failure; other parse errors are fine.
            assert "Unsupported" not in str(e), f"Version wrongly rejected: {e}"

    @pytest.mark.parametrize("bad_version", [
        "PCB FILE 6 VERSION 2.80",
        "ACCEL_ASCII",
        "PCB FILE 9 VERSION 3.00",
        "PCB FILE 9 VERSION 2.60",
    ])
    def test_version_rejects_wrong_format(self, bad_version, tmp_path):
        """Non-v2.70 version strings must raise ValueError."""
        if bad_version == "ACCEL_ASCII":
            content = bad_version.encode("ascii") + b"\x00" * 256
        else:
            content = bytes([0x00, 0xA1]) + bad_version.encode("ascii") + b"\x00" * 256

        pcb_file = tmp_path / "test.PCB"
        pcb_file.write_bytes(content)

        parser = Protel99Parser(str(pcb_file))
        with pytest.raises(ValueError):
            parser.parse(ground_truth_path=None)


class TestGroundTruthDiscovery:
    """find_ground_truth_file() derives the reference name from the PCB stem."""

    def test_find_ground_truth_derives_from_stem(self, tmp_path):
        """Given BOARD.PCB, look for BOARD_smart.kicad_pcb, not a hardcoded name."""
        pcb_path = tmp_path / "BOARD.PCB"
        pcb_path.touch()
        assert find_ground_truth_file(str(pcb_path)) is None

        gt_file = tmp_path / "BOARD_smart.kicad_pcb"
        gt_file.touch()
        result = find_ground_truth_file(str(pcb_path))
        assert result is not None
        assert result.name == "BOARD_smart.kicad_pcb"

    def test_find_ground_truth_returns_none_for_unknown(self, tmp_path):
        """A PCB file with no matching kicad_pcb returns None."""
        pcb_path = tmp_path / "NONEXISTENT_BOARD.PCB"
        pcb_path.touch()
        assert find_ground_truth_file(str(pcb_path)) is None
