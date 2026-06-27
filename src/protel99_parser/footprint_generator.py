"""Generate KiCad .kicad_mod s-expressions from parsed Protel footprint data.

Converts FootprintData (from pcblib_parser) into valid KiCad 6+ footprint files.
Also provides resolve_footprint() for matching footprint names to library
entries with suffix stripping and standard-dimension fallback generation.

Usage:
    from protel99_parser.pcblib_parser import parse_pcblib
    from protel99_parser.footprint_generator import generate_kicad_mod, resolve_footprint

    libs = {'mylib': parse_pcblib('mylib.LIB')}
    fp, match_type = resolve_footprint('MINIMELF', libs)
    kicad_mod = generate_kicad_mod('MINIMELF', fp)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from protel99_parser.pcblib_parser import FootprintData, PadInfo

logger = logging.getLogger(__name__)

# Conversion factor: 1 mil = 0.0254 mm
MIL_TO_MM = 0.0254

# Generic / standard footprint names this module knows how to handle.
# Extend with the footprint names present in your own libraries.
KNOWN_FOOTPRINT_NAMES: list[str] = [
    'MINIMELF', '1206', 'SOT-23', 'DIL8-1',
    'DIL8-10', 'TO-92-37',
    'R0-2', 'SO-10',
    'SIP1-2', 'SIP1-20',
    'DIN41612C',
]

# Suffix characters that can be stripped from footprint names for matching
_SUFFIX_CHARS = ('0', 'E', 'N', 'Z', '|', '7')


def _mil_to_mm(val: float) -> float:
    """Convert mils to mm, rounding to 4 decimal places."""
    return round(val * MIL_TO_MM, 4)


def _fmt(val: float) -> str:
    """Format a float for KiCad s-expression (remove trailing zeros)."""
    s = f"{val:.4f}"
    # Strip trailing zeros but keep at least one decimal
    s = s.rstrip('0').rstrip('.')
    if '.' not in s:
        s += '.0'
    return s


def _pad_sexp(pad: 'PadInfo') -> str:
    """Generate a single (pad ...) s-expression."""
    x_mm = _mil_to_mm(pad.x)
    y_mm = _mil_to_mm(pad.y)
    w_mm = _mil_to_mm(pad.width)
    h_mm = _mil_to_mm(pad.height)
    drill_mm = _mil_to_mm(pad.drill)

    pin = pad.pin if pad.pin else "1"

    if pad.layers == 'tht':
        pad_type = 'thru_hole'
        layers = '"*.Cu" "*.Mask"'
        drill_part = f' (drill {_fmt(drill_mm)})' if drill_mm > 0 else ''
    else:
        pad_type = 'smd'
        layers = '"F.Cu" "F.Paste" "F.Mask"'
        drill_part = ''

    shape = pad.shape  # 'rect', 'circle', 'oval'
    if shape not in ('rect', 'circle', 'oval'):
        shape = 'rect'

    return (
        f'  (pad "{pin}" {pad_type} {shape}'
        f' (at {_fmt(x_mm)} {_fmt(y_mm)})'
        f' (size {_fmt(w_mm)} {_fmt(h_mm)})'
        f'{drill_part}'
        f' (layers {layers}))'
    )


def generate_kicad_mod(name: str, fp: 'FootprintData') -> str:
    """Generate a KiCad .kicad_mod s-expression string from a FootprintData.

    All coordinates and dimensions are converted from mils to mm (× 0.0254).

    Args:
        name: Footprint name for the (footprint "name" ...) wrapper
        fp: Parsed footprint data with pads and lines

    Returns:
        Complete .kicad_mod s-expression string with balanced parentheses
    """
    # Determine if through-hole or SMD
    has_tht = any(p.layers == 'tht' for p in fp.pads)
    attr = 'through_hole' if has_tht else 'smd'

    lines: list[str] = []
    lines.append(f'(footprint "{name}"')
    lines.append(f'  (layer "F.Cu")')
    lines.append(f'  (attr {attr})')

    # Pads
    for pad in fp.pads:
        lines.append(_pad_sexp(pad))

    # Silk lines
    for line in fp.lines:
        x1 = _fmt(_mil_to_mm(line.x1))
        y1 = _fmt(_mil_to_mm(line.y1))
        x2 = _fmt(_mil_to_mm(line.x2))
        y2 = _fmt(_mil_to_mm(line.y2))
        w = _fmt(_mil_to_mm(line.width))
        lines.append(
            f'  (fp_line (start {x1} {y1}) (end {x2} {y2})'
            f' (layer "F.SilkS") (width {w}))'
        )

    lines.append(')')

    result = '\n'.join(lines)

    # Verify balanced parentheses
    open_count = result.count('(')
    close_count = result.count(')')
    if open_count != close_count:
        logger.warning(
            "Unbalanced parentheses in %s: %d open, %d close",
            name, open_count, close_count,
        )

    return result


def _generate_standard_footprint(name: str) -> 'FootprintData | None':
    """Generate standard footprints for known component types.

    Returns FootprintData for recognized standard names, None otherwise.
    """
    from protel99_parser.pcblib_parser import FootprintData, PadInfo

    if name == 'R0-2':
        # Standard 0805 (2012 Metric): 2 SMD pads
        return FootprintData(
            name='R0-2',
            pads=[
                PadInfo(x=0.0, y=0.0, width=50.0, height=60.0,
                        drill=0.0, shape='rect', pin='1', layers='smd'),
                PadInfo(x=50.0, y=0.0, width=50.0, height=60.0,
                        drill=0.0, shape='rect', pin='2', layers='smd'),
            ],
            source_lib='generated',
        )

    if name == 'SO-10':
        # Standard SOIC-10: 10 pads, 5 per side, 50 mil pitch, 300 mil row spacing
        pads = []
        pitch = 50.0  # 1.27mm
        row_spacing = 300.0  # 7.62mm
        for i in range(5):
            # Left side (pins 1-5)
            pads.append(PadInfo(
                x=0.0, y=i * pitch,
                width=25.0, height=60.0,
                drill=0.0, shape='rect', pin=str(i + 1), layers='smd',
            ))
        for i in range(5):
            # Right side (pins 10-6, mirrored)
            pads.append(PadInfo(
                x=row_spacing, y=i * pitch,
                width=25.0, height=60.0,
                drill=0.0, shape='rect', pin=str(10 - i), layers='smd',
            ))
        return FootprintData(
            name='SO-10',
            pads=pads,
            source_lib='generated',
        )

    if name == 'SIP1-2':
        # Generic 2-pin THT: 100 mil pitch
        return FootprintData(
            name=name,
            pads=[
                PadInfo(x=0.0, y=0.0, width=60.0, height=60.0,
                        drill=32.0, shape='circle', pin='1', layers='tht'),
                PadInfo(x=100.0, y=0.0, width=60.0, height=60.0,
                        drill=32.0, shape='circle', pin='2', layers='tht'),
            ],
            source_lib='generated',
        )

    if name == 'SIP1-20':
        # SIP 20-pin: 100 mil pitch, single row
        pads = []
        for i in range(20):
            pads.append(PadInfo(
                x=i * 100.0, y=0.0,
                width=50.0, height=50.0,
                drill=32.0, shape='circle', pin=str(i + 1), layers='tht',
            ))
        return FootprintData(
            name='SIP1-20',
            pads=pads,
            source_lib='generated',
        )

    if name == 'TO-92-37':
        # TO-92 variant: 3 THT pads at ~98 mil pitch
        return FootprintData(
            name='TO-92-37',
            pads=[
                PadInfo(x=0.0, y=-98.4252, width=70.0, height=90.0,
                        drill=31.5, shape='rect', pin='1', layers='tht'),
                PadInfo(x=0.0, y=0.0, width=70.0, height=90.0,
                        drill=31.5, shape='rect', pin='2', layers='tht'),
                PadInfo(x=0.0, y=98.4252, width=70.0, height=90.0,
                        drill=31.5, shape='rect', pin='3', layers='tht'),
            ],
            source_lib='generated',
        )

    if name == 'DIN41612C':
        # DIN 41612 type C: 96 pins (3 rows of 32), 100 mil pitch
        pads = []
        pin = 1
        for row in range(3):
            row_letter = chr(ord('a') + row)
            for col in range(32):
                pads.append(PadInfo(
                    x=col * 100.0, y=row * 100.0,
                    width=60.0, height=60.0,
                    drill=40.0, shape='circle',
                    pin=f'{row_letter}{col + 1}', layers='tht',
                ))
                pin += 1
        return FootprintData(
            name='DIN41612C',
            pads=pads,
            source_lib='generated',
        )

    return None


def resolve_footprint(
    name: str,
    libraries: dict[str, dict[str, 'FootprintData']],
) -> tuple['FootprintData | None', str | None]:
    """Resolve a footprint name to a FootprintData from parsed libraries.

    Resolution strategy:
    1. Exact name match across all libraries
    2. Suffix strip: remove trailing 0/E/N/Z/|/7, also handle (X) -> (N) pattern
    3. Generate standard footprints for known types (R0-2, SO-10, etc.)

    Args:
        name: Footprint name to resolve
        libraries: dict of {lib_path: {footprint_name: FootprintData}}

    Returns:
        (FootprintData, match_type) where match_type is 'exact'|'suffix'|'generated'|None.
        Returns (None, None) if unresolvable.
    """
    # Step 1: exact match
    for lib_path, lib_fps in libraries.items():
        if name in lib_fps:
            logger.debug("resolve_footprint(%s): exact match in %s", name, lib_path)
            return lib_fps[name], 'exact'

    # Step 2: suffix strip
    # Try removing last character if it's a known suffix
    if len(name) > 1 and name[-1] in _SUFFIX_CHARS:
        base = name[:-1]
        for lib_path, lib_fps in libraries.items():
            if base in lib_fps:
                logger.debug(
                    "resolve_footprint(%s): suffix match '%s' in %s",
                    name, base, lib_path,
                )
                return lib_fps[base], 'suffix'

    # Handle (X) -> (N) pattern: e.g., FOO-FU(0) -> FOO-FU(N)
    if name.endswith(')') and len(name) > 2 and name[-2] != '(':
        variant_base = name[:-2] + 'N)'
        if variant_base != name:
            for lib_path, lib_fps in libraries.items():
                if variant_base in lib_fps:
                    logger.debug(
                        "resolve_footprint(%s): variant match '%s' in %s",
                        name, variant_base, lib_path,
                    )
                    return lib_fps[variant_base], 'suffix'

    # Step 3: generate standard footprints
    generated = _generate_standard_footprint(name)
    if generated is not None:
        logger.debug("resolve_footprint(%s): generated standard footprint", name)
        return generated, 'generated'

    logger.warning("resolve_footprint(%s): no match found", name)
    return None, None
