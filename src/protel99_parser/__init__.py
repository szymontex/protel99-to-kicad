"""protel99-to-kicad: Parse Protel 99 SE binary PCB files."""

from protel99_parser.parser import (
    Protel99Parser,
    PCBData,
    Component,
    Track,
    Via,
    main,
)

from protel99_parser.pcblib_parser import (
    parse_pcblib,
    FootprintData,
    PadInfo,
    LineInfo,
)

from protel99_parser.footprint_generator import (
    generate_kicad_mod,
    resolve_footprint,
)

__all__ = [
    'Protel99Parser', 'PCBData', 'Component', 'Track', 'Via', 'main',
    'parse_pcblib', 'FootprintData', 'PadInfo', 'LineInfo',
    'generate_kicad_mod', 'resolve_footprint',
]
