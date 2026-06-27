"""Load footprint data from text11 Place Whole Library exports.

This module provides FootprintData objects from a pre-parsed text11 JSON
file, as an alternative to the PCBLIB binary parser. The text11 data has:
- Correct pad shapes (round->circle/oval, rect)
- Correct pad sizes (copper layer dimensions)
- Correct origin (pin 1 at (0,0) = Protel reference point)
- Correct drill sizes
- Silk screen lines

The JSON is produced from a Protel "Place Whole Library -> Save Copy As
text11" export. The expected file is ``text11_footprints.json`` next to
this module. When the file is absent, the loader returns an empty mapping
so callers can fall back to other footprint sources.
"""

import json
import logging
from pathlib import Path

from protel99_parser.pcblib_parser import FootprintData, PadInfo, LineInfo

logger = logging.getLogger(__name__)

_CACHE: dict[str, FootprintData] | None = None


def _load_json() -> dict:
    """Load the pre-parsed JSON file."""
    json_path = Path(__file__).parent / "text11_footprints.json"
    if not json_path.exists():
        logger.debug("text11_footprints.json not present at %s; "
                     "returning empty footprint set", json_path)
        return {}
    with open(json_path, encoding='utf-8') as f:
        return json.load(f)


def load_text11_footprints() -> dict[str, FootprintData]:
    """Load all footprints from text11 JSON as FootprintData objects.

    Returns {footprint_name: FootprintData}. Cached after first call.
    """
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    raw = _load_json()
    result: dict[str, FootprintData] = {}

    for name, data in raw.items():
        pads = []
        for p in data.get('pads', []):
            pads.append(PadInfo(
                x=p['x'],
                y=p['y'],
                width=p['w'],
                height=p['h'],
                drill=p.get('drill', 0.0),
                shape=p['shape'],
                pin=p.get('pin', ''),
                layers=p.get('layers', 'smd'),
                rotation=p.get('pad_rot', 0.0),
            ))

        lines = []
        for s in data.get('silk', []):
            lines.append(LineInfo(
                x1=s['x1'], y1=s['y1'],
                x2=s['x2'], y2=s['y2'],
                width=s.get('width', 5.0),
                layer=s.get('layer', 17),  # 17 = F.SilkS
            ))

        result[name] = FootprintData(name=name, pads=pads, lines=lines, source_lib='text11')

    _CACHE = result
    logger.info("Loaded %d text11 footprints", len(result))
    return result
