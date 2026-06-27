"""Per-footprint origin corrections for PCBLIB footprints.

PCBLIB pad coordinates are stored relative to the library footprint
centroid, while Protel places components by a separate reference point.
Each entry maps a footprint name -> (ox_mil, oy_mil) to subtract from
the PCBLIB pad coordinates, shifting the origin to the Protel reference
point.

Users supply their own corrections, typically derived from a
"Place Whole Library" Pick and Place export of their library:

    correction = PCBLIB_centroid - PnP_ref_to_mid

A correction is only valid when it falls within roughly 3x the footprint
extent of the centroid. The dictionary below ships with a couple of
generic examples; extend it with the footprints in your own libraries.
"""

FOOTPRINT_ORIGIN_CORRECTIONS: dict[str, tuple[float, float]] = {
    # Generic examples - replace with corrections for your own library.
    'R0805': (0.0, 0.0),
    'SOT-23': (0.0, 0.0),
}
