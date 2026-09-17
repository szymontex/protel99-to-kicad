#!/usr/bin/env python3
"""Write a KiCad 9 board straight from a Protel board file.

Reads whatever generation the file is written in - Autotrax and Easytrax, the
PCB ASCII exports of every vintage, the `PCB FILE 9` binary and the boards
stored as text inside a `.ddb` design database. `formats.py` is the list. Every
reader returns the same model and the same layer numbering, so this writer
never asks which one it got.

No footprint libraries, no ASCII export, no ground truth: the source file
carries every pad, track, arc, fill and text, so the board is emitted from
that alone.

Frame: KiCad is Y-down, Protel is Y-up. X_mm = (x - X0) * 0.0254,
Y_mm = (Y0 - y) * 0.0254, with (X0, Y0) the top-left of the board's extent
rounded out to 1000 mil. Rotations keep their sign (the Y flip restores the
original image, so counter-clockwise stays counter-clockwise on screen).

Footprints carry their pads, silk graphics and the two component texts in
footprint-local coordinates: local = R(-rot) * (world - anchor). Component
copper (tracks, arcs, fills on copper layers) is written at board level so
it is copper in KiCad too, not footprint artwork.

Usage:
    protel-to-kicad BOARD.PCB -o BOARD.kicad_pcb
    protel-to-kicad --batch IN_DIR OUT_DIR     # every readable board under IN_DIR
"""
from __future__ import annotations

import argparse
import math
import sys
import uuid
from pathlib import Path

from protel99_parser import ddb, formats, pcb9

MIL_TO_MM = 0.0254
KICAD_VERSION = 20241229

# Protel 99 layer numbers -> KiCad layer names.
LAYERS = {
    1: "F.Cu", 16: "B.Cu",
    17: "F.SilkS", 18: "B.SilkS",
    19: "F.Paste", 20: "B.Paste",
    21: "F.Mask", 22: "B.Mask",
    27: "Dwgs.User",      # drill guide
    34: "Dwgs.User",      # multilayer, for the rare text or track placed there
    28: "Margin",         # keep-out
    29: "Edge.Cuts",      # mechanical 1 - board outline in this archive (see --outline-layer)
    30: "Dwgs.User", 31: "Cmts.User", 32: "Eco1.User",   # mechanical 2-4
    33: "Eco2.User",      # drill drawing
}
COPPER = {1, 16} | set(range(2, 16))
MULTILAYER = 34

# Protel's own silkscreen gerbers in this archive carry the comments and, on
# most boards, no designators at all (measured 14.09.2026 on 70 boards with a
# top silk plot: designator boxes empty, comment boxes inked). The hide flag
# itself is not identified in the binary, so this is a switch, not a decode.
HIDE_DESIGNATORS = False


def _uuid() -> str:
    return str(uuid.uuid4())


def _f(v: float) -> str:
    return f"{v:.5f}".rstrip("0").rstrip(".") if abs(v) > 1e-9 else "0"


class Frame:
    def __init__(self, x0_mil: float, y0_mil: float):
        self.x0 = x0_mil
        self.y0 = y0_mil

    def pt(self, x: float, y: float) -> tuple[float, float]:
        return ((x - self.x0) * MIL_TO_MM, (self.y0 - y) * MIL_TO_MM)

    def mm(self, v: float) -> float:
        return v * MIL_TO_MM


def board_extent(b: pcb9.Board) -> tuple[float, float, float, float]:
    """The rectangle every emitted object fits in, in mils.

    This decides the frame origin, so anything left out of it lands at a
    negative KiCad coordinate - above and to the left of the page, off the
    sheet. An earlier version read free tracks and fills, component bounding
    boxes and component tracks, and nothing else. On one archive board that
    saw 5098 x 3749 mil of a board that is 18242 x 14602: its arcs, texts,
    vias and the pads that reach past their component's box were all outside
    the frame. Every object class the generator writes is measured here.
    """
    xs, ys = [], []

    def span(src):
        for t in src.tracks:
            xs.extend((t.x1, t.x2)); ys.extend((t.y1, t.y2))
        for a in src.arcs:
            xs.extend((a.x - a.radius, a.x + a.radius))
            ys.extend((a.y - a.radius, a.y + a.radius))
        for f in src.fills:
            xs.extend((f.x1, f.x2)); ys.extend((f.y1, f.y2))
        for v in src.vias:
            xs.extend((v.x - v.diameter / 2, v.x + v.diameter / 2))
            ys.extend((v.y - v.diameter / 2, v.y + v.diameter / 2))
        for p in src.pads:
            sx, sy, _sh = pad_geometry(p)
            r = max(sx, sy, p.hole) / 2.0
            xs.extend((p.x - r, p.x + r)); ys.extend((p.y - r, p.y + r))
        for t in src.texts:
            if t.bbox:
                xs.extend((t.bbox[0], t.bbox[2])); ys.extend((t.bbox[1], t.bbox[3]))

    span(b)
    for c in b.components:
        # Only `PCB FILE 9` stores a component bounding box. The ASCII formats
        # do not, and their readers leave it all zero - which is truthy, so
        # measuring it drags the frame back to the origin and draws the whole
        # board in one corner of an otherwise empty sheet. A box with no width
        # and no height describes nothing; the component's own primitives,
        # measured just below, describe where it is.
        if c.bbox and (c.bbox[2] > c.bbox[0] or c.bbox[3] > c.bbox[1]):
            xs.extend((c.bbox[0], c.bbox[2])); ys.extend((c.bbox[1], c.bbox[3]))
        span(c)
        for t in (c.designator, c.comment):
            if t is not None and t.text and t.bbox:
                xs.extend((t.bbox[0], t.bbox[2])); ys.extend((t.bbox[1], t.bbox[3]))

    if not xs:
        return 0.0, 0.0, 1000.0, 1000.0
    return min(xs), min(ys), max(xs), max(ys)


def layer_name(layer: int, warn: set) -> str:
    if layer in LAYERS:
        return LAYERS[layer]
    if 2 <= layer <= 15:
        return f"In{layer - 1}.Cu"
    warn.add(layer)
    return "Cmts.User"


# --------------------------------------------------------------------------
# geometry helpers
# --------------------------------------------------------------------------

def rotate(dx: float, dy: float, deg: float) -> tuple[float, float]:
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    return dx * c - dy * s, dx * s + dy * c


def arc_points(a: pcb9.Arc):
    """Start, mid, end of a Protel arc (counter-clockwise from start to end, Y-up).

    Returns None for a full circle.
    """
    s, e = a.start % 360.0, a.end % 360.0
    sweep = (e - s) % 360.0
    if sweep == 0.0:
        return None
    mid = s + sweep / 2.0
    def p(deg):
        r = math.radians(deg)
        return a.x + a.radius * math.cos(r), a.y + a.radius * math.sin(r)
    return p(s), p(mid), p(e)


# --------------------------------------------------------------------------
# emitters
# --------------------------------------------------------------------------

def _q(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


class Nets:
    """Net index as Protel stores it on objects: tag 0x02, 1-based into the `N`
    records, 0 = no net. Anything out of range is reported and written as 0."""

    def __init__(self, board: pcb9.Board):
        self.names = [n.name for n in board.nets]
        self.bad = 0

    def code(self, k: int) -> int:
        if 1 <= k <= len(self.names):
            return k
        if k:
            self.bad += 1
        return 0

    def clause(self, k: int, with_name: bool = False) -> str:
        c = self.code(k)
        if with_name:
            return f'(net {c} "{_q(self.names[c - 1]) if c else ""}")'
        return f"(net {c})"


# Drawing sheets KiCad knows, landscape, in mm. Ordered small to large so the
# first that fits is the smallest that fits.
PAGES = [("A5", 210.0, 148.0), ("A4", 297.0, 210.0), ("A3", 420.0, 297.0),
         ("A2", 594.0, 420.0), ("A1", 841.0, 594.0), ("A0", 1189.0, 841.0)]

# Space between the board and the edge of the sheet, in mils. KiCad draws its
# frame border about 10 mm in; a board laid against the corner starts outside
# it. 1000 mil is 25.4 mm, which clears the border and keeps the origin on the
# same 1000-mil grid the frame already rounds to.
MARGIN_MIL = 1000.0


def choose_paper(width_mm: float, height_mm: float) -> str:
    """The smallest sheet the board fits on, or a custom one when none does.

    The page used to be `A3` for every board. Anything bigger than 420 x 297 mm
    - and this archive has panels well past that - was drawn hanging off the
    right-hand side of its own sheet.
    """
    w = width_mm + 2 * MARGIN_MIL * MIL_TO_MM
    h = height_mm + 2 * MARGIN_MIL * MIL_TO_MM
    for name, pw, ph in PAGES:
        if w <= pw and h <= ph:
            return f'(paper "{name}")'
        if w <= ph and h <= pw:
            return f'(paper "{name}" portrait)'
    # KiCad accepts a user page between 25.4 mm and 1200 mm on a side.
    return f'(paper "User" {_f(min(max(w, 100.0), 1200.0))} {_f(min(max(h, 100.0), 1200.0))})'


def emit_header(out: list, copper_layers: int, nets: Nets, paper: str = '(paper "A3")'):
    out.append("(kicad_pcb")
    out.append(f"  (version {KICAD_VERSION})")
    out.append('  (generator "pcb9_to_kicad")')
    out.append('  (generator_version "9.0")')
    out.append("  (general (thickness 1.6) (legacy_teardrops no))")
    out.append(f"  {paper}")
    out.append("  (layers")
    out.append('    (0 "F.Cu" signal)')
    for i in range(1, copper_layers - 1):
        out.append(f'    ({i} "In{i}.Cu" signal)')
    out.append('    (31 "B.Cu" signal)')
    for idx, (name, kind, alias) in enumerate((
        ("B.Adhes", "user", "B.Adhesive"), ("F.Adhes", "user", "F.Adhesive"),
        ("B.Paste", "user", None), ("F.Paste", "user", None),
        ("B.SilkS", "user", "B.Silkscreen"), ("F.SilkS", "user", "F.Silkscreen"),
        ("B.Mask", "user", None), ("F.Mask", "user", None),
        ("Dwgs.User", "user", "User.Drawings"), ("Cmts.User", "user", "User.Comments"),
        ("Eco1.User", "user", "User.Eco1"), ("Eco2.User", "user", "User.Eco2"),
        ("Edge.Cuts", "user", None), ("Margin", "user", None),
        ("B.CrtYd", "user", "B.Courtyard"), ("F.CrtYd", "user", "F.Courtyard"),
        ("B.Fab", "user", None), ("F.Fab", "user", None),
    ), start=32):
        out.append(f'    ({idx} "{name}" {kind}' + (f' "{alias}"' if alias else "") + ")")
    out.append("  )")
    # Protel's mask opening on board C is the pad plus 4 mil on the diameter
    # (GTS aperture 44 for a 40 mil pad): 2 mil a side = 0.0508 mm.
    out.append("  (setup (pad_to_mask_clearance 0.0508) (allow_soldermask_bridges_in_footprints no)"
               " (pcbplotparams (layerselection 0x00010fc_ffffffff)"
               " (plot_on_all_layers_selection 0x0000000_00000000)))")
    out.append('  (net 0 "")')
    for i, name in enumerate(nets.names, start=1):
        out.append(f'  (net {i} "{_q(name)}")')


def emit_segment(out: list, fr: Frame, x1, y1, x2, y2, width, layer, net="(net 0)", indent="  "):
    a = fr.pt(x1, y1); b = fr.pt(x2, y2)
    out.append(f'{indent}(segment (start {_f(a[0])} {_f(a[1])}) (end {_f(b[0])} {_f(b[1])})'
               f' (width {_f(fr.mm(width))}) (layer "{layer}") {net} (uuid "{_uuid()}"))')


def emit_gr_line(out: list, fr: Frame, x1, y1, x2, y2, width, layer, indent="  "):
    a = fr.pt(x1, y1); b = fr.pt(x2, y2)
    out.append(f'{indent}(gr_line (start {_f(a[0])} {_f(a[1])}) (end {_f(b[0])} {_f(b[1])})'
               f' (stroke (width {_f(fr.mm(width))}) (type solid)) (layer "{layer}") (uuid "{_uuid()}"))')


def emit_arc(out: list, fr: Frame, a: pcb9.Arc, layer: str, copper: bool, net="(net 0)", indent="  "):
    pts = arc_points(a)
    w = _f(fr.mm(a.width))
    if pts is None:
        if copper:
            # full circle as two half arcs
            for s0 in (0.0, 180.0):
                half = pcb9.Arc(a.offset, a.x, a.y, a.radius, s0, s0 + 180.0, a.width, a.layer, a.tail)
                emit_arc(out, fr, half, layer, copper, net, indent)
            return
        c = fr.pt(a.x, a.y); e = fr.pt(a.x + a.radius, a.y)
        out.append(f'{indent}(gr_circle (center {_f(c[0])} {_f(c[1])}) (end {_f(e[0])} {_f(e[1])})'
                   f' (stroke (width {w}) (type solid)) (fill no) (layer "{layer}") (uuid "{_uuid()}"))')
        return
    s, m, e = (fr.pt(*p) for p in pts)
    if copper:
        out.append(f'{indent}(arc (start {_f(s[0])} {_f(s[1])}) (mid {_f(m[0])} {_f(m[1])})'
                   f' (end {_f(e[0])} {_f(e[1])}) (width {w}) (layer "{layer}") {net} (uuid "{_uuid()}"))')
    else:
        out.append(f'{indent}(gr_arc (start {_f(s[0])} {_f(s[1])}) (mid {_f(m[0])} {_f(m[1])})'
                   f' (end {_f(e[0])} {_f(e[1])}) (stroke (width {w}) (type solid))'
                   f' (layer "{layer}") (uuid "{_uuid()}"))')


def emit_fill(out: list, fr: Frame, f: pcb9.Fill, layer: str, copper: bool, nets: Nets = None, indent="  "):
    a = fr.pt(f.x1, f.y1); b = fr.pt(f.x2, f.y2)
    x1, x2 = sorted((a[0], b[0])); y1, y2 = sorted((a[1], b[1]))
    if copper:
        pts = f"(xy {_f(x1)} {_f(y1)}) (xy {_f(x2)} {_f(y1)}) (xy {_f(x2)} {_f(y2)}) (xy {_f(x1)} {_f(y2)})"
        code = nets.code(f.net) if nets else 0
        name = _q(nets.names[code - 1]) if code else ""
        out.append(f'{indent}(zone (net {code}) (net_name "{name}") (layer "{layer}") (uuid "{_uuid()}")'
                   f' (hatch edge 0.5) (connect_pads (clearance 0))'
                   f' (min_thickness 0.1) (filled_areas_thickness no)'
                   f' (fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5))'
                   f' (polygon (pts {pts})) (filled_polygon (layer "{layer}") (pts {pts})))')
    else:
        out.append(f'{indent}(gr_rect (start {_f(x1)} {_f(y1)}) (end {_f(x2)} {_f(y2)})'
                   f' (stroke (width 0) (type solid)) (fill yes) (layer "{layer}") (uuid "{_uuid()}"))')


def emit_text(out: list, fr: Frame, t: pcb9.Text, layer: str, indent="  "):
    if not t.text.strip():
        return
    p = fr.pt(t.x, t.y)
    h = fr.mm(t.height) if t.height > 0 else 1.0
    s = fr.mm(t.stroke) if t.stroke > 0 else h * 0.15
    txt = _q(t.text)
    mirror = " mirror" if t.mirror else ""
    out.append(f'{indent}(gr_text "{txt}" (at {_f(p[0])} {_f(p[1])} {_f(t.rotation % 360)}) (layer "{layer}")'
               f' (uuid "{_uuid()}") (effects (font (size {_f(h)} {_f(h)}) (thickness {_f(s)}))'
               f' (justify left bottom{mirror})))')


def pad_geometry(p: pcb9.Pad):
    """Pick the pad size that applies to the pad's layer; return (sx, sy, shape)."""
    if p.layer == 1:
        cands = (p.top, p.mid, p.bot)
    elif p.layer == 16:
        cands = (p.bot, p.mid, p.top)
    else:
        cands = (p.top, p.bot, p.mid)
    for sx, sy, shape in cands:
        if sx > 0 and sy > 0:
            return sx, sy, shape
    return cands[0]


def pad_side(size: tuple, hole: float, fallback: tuple) -> tuple:
    """Size of one copper side (top, mid or bottom) of a through pad.

    Protel keeps three sizes per pad and allows a side with no copper at all
    (0 x 0). KiCad needs a size on every copper layer, so such a side gets the
    hole diameter: a ring of zero width, which after drilling leaves no copper,
    the same as Protel plots.
    """
    sx, sy, shape = size
    if sx > 0 and sy > 0:
        return sx, sy, shape
    if hole > 0:
        return hole, hole, 1
    return fallback


def kicad_pad_shape(shape: int, sx: float, sy: float) -> tuple:
    """Protel shape code -> (KiCad shape token, extra clauses)."""
    if shape == 1:
        return ("circle" if abs(sx - sy) < 1e-6 else "oval"), ""
    if shape == 3:
        return "rect", " (chamfer_ratio 0.29) (chamfer top_left top_right bottom_left bottom_right)"
    return "rect", ""


def padstack_clause(fr, top: tuple, mid: tuple, bot: tuple) -> str:
    """KiCad 9 padstack for a through pad whose sides differ.

    `front_inner_back` mode: the pad's own shape and size are the front, the
    two entries carry the inner layers and the back. Measured 14.09.2026 with
    kicad-cli 9.0.8: F.Cu and B.Cu gerbers flash the two sizes, pcbnew reads
    them back (`GetSize(F_Cu)`, `GetSize(B_Cu)`).
    """
    if mid == top and bot == top:
        return ""
    parts = []
    for lname, (mx, my, msh) in (("Inner", mid), ("B.Cu", bot)):
        ks, ex = kicad_pad_shape(msh, mx, my)
        parts.append(f'(layer "{lname}" (shape {ks}) (size {_f(fr.mm(mx))} {_f(fr.mm(my))}){ex})')
    return " (padstack (mode front_inner_back) " + " ".join(parts) + ")"


def emit_pad(out: list, fr: Frame, p: pcb9.Pad, ax: float, ay: float, arot: float,
             comp_layer: str, nets: Nets = None, indent="    "):
    lx, ly = rotate(p.x - ax, p.y - ay, -arot)
    lx_mm, ly_mm = lx * MIL_TO_MM, -ly * MIL_TO_MM
    prot = (p.rotation - arot) % 360.0
    through = p.hole > 0 or p.layer == MULTILAYER
    stack = ""
    if through and p.layer == MULTILAYER:
        # Three sizes in the source, one per copper side; KiCad 9 keeps them
        # in a padstack. Before 14.09.2026 only the top size was written, and
        # on board D six 7 mm bottom pads came out as 1 mm.
        fallback = pad_geometry(p)
        top = pad_side(p.top, p.hole, fallback)
        mid = pad_side(p.mid, p.hole, fallback)
        bot = pad_side(p.bot, p.hole, fallback)
        sx, sy, shape = top
        stack = padstack_clause(fr, top, mid, bot)
    else:
        sx, sy, shape = pad_geometry(p)
    if through:
        kind = "thru_hole"
        layers = '"*.Cu" "*.Mask"'
    else:
        kind = "smd"
        side = "B" if p.layer == 16 else "F"
        layers = f'"{side}.Cu" "{side}.Paste" "{side}.Mask"'
    kshape, extra = kicad_pad_shape(shape, sx, sy)
    drill = f" (drill {_f(fr.mm(p.hole))})" if through and p.hole > 0 else ""
    if through and p.hole <= 0:
        drill = " (drill 0.1)"
    if sx <= 0 or sy <= 0:
        sx = sy = max(p.hole * 1.5, 10.0)
    name = _q(p.name)
    rot = f" {_f(prot)}" if prot else ""
    netc = f" {nets.clause(p.net, True)}" if nets and nets.code(p.net) else ""
    out.append(f'{indent}(pad "{name}" {kind} {kshape} (at {_f(lx_mm)} {_f(ly_mm)}{rot})'
               f' (size {_f(fr.mm(sx))} {_f(fr.mm(sy))}){drill}{extra} (layers {layers}){stack}{netc} (uuid "{_uuid()}"))')


def emit_fp_text(out: list, fr: Frame, t: pcb9.Text, kind: str, ax: float, ay: float, arot: float,
                 layer: str, indent="    "):
    lx, ly = rotate(t.x - ax, t.y - ay, -arot)
    h = fr.mm(t.height) if t.height > 0 else 1.0
    s = fr.mm(t.stroke) if t.stroke > 0 else h * 0.15
    txt = _q(t.text)
    trot = (t.rotation - arot) % 360.0
    mirror = " mirror" if t.mirror else ""
    hide = " (hide yes)" if (not t.text.strip() or (kind == "Reference" and HIDE_DESIGNATORS)) else ""
    out.append(f'{indent}(property "{kind}" "{txt}" (at {_f(lx * MIL_TO_MM)} {_f(-ly * MIL_TO_MM)} {_f(trot)})'
               f' (layer "{layer}"){hide} (uuid "{_uuid()}")'
               f' (effects (font (size {_f(h)} {_f(h)}) (thickness {_f(s)})) (justify left bottom{mirror})))')


def emit_component(out: list, fr: Frame, c: pcb9.Component, warn: set, nets: Nets):
    bottom = c.mirror == 1
    side = "B" if bottom else "F"
    ax, ay = fr.pt(c.x, c.y)
    rot = c.rotation % 360.0
    name = _q(c.footprint)
    out.append(f'  (footprint "{name}" (layer "{side}.Cu") (uuid "{_uuid()}")'
               f' (at {_f(ax)} {_f(ay)} {_f(rot)})')
    desig_layer = layer_name(c.designator.layer, warn) if c.designator.layer else f"{side}.SilkS"
    comment_layer = layer_name(c.comment.layer, warn) if c.comment.layer else f"{side}.Fab"
    emit_fp_text(out, fr, c.designator, "Reference", c.x, c.y, rot, desig_layer)
    emit_fp_text(out, fr, c.comment, "Value", c.x, c.y, rot, comment_layer)
    for kind in ("Footprint", "Datasheet", "Description"):
        out.append(f'    (property "{kind}" "" (at 0 0 0) (layer "{side}.Fab") (hide yes) (uuid "{_uuid()}")'
                   f' (effects (font (size 1 1) (thickness 0.15))))')
    through = any(p.hole > 0 or p.layer == MULTILAYER for p in c.pads)
    out.append(f'    (attr {"through_hole" if through else "smd"})')
    for t in c.texts:
        lx, ly = rotate(t.x - c.x, t.y - c.y, -rot)
        h = fr.mm(t.height) if t.height > 0 else 1.0
        s = fr.mm(t.stroke) if t.stroke > 0 else h * 0.15
        txt = _q(t.text)
        if txt.strip():
            out.append(f'    (fp_text user "{txt}" (at {_f(lx * MIL_TO_MM)} {_f(-ly * MIL_TO_MM)} {_f((t.rotation - rot) % 360)})'
                       f' (layer "{layer_name(t.layer, warn)}") (uuid "{_uuid()}")'
                       f' (effects (font (size {_f(h)} {_f(h)}) (thickness {_f(s)})) (justify left bottom{" mirror" if t.mirror else ""})))')
    # non-copper footprint graphics in local coordinates
    for t in c.tracks:
        if t.layer in COPPER:
            continue
        a = rotate(t.x1 - c.x, t.y1 - c.y, -rot); b = rotate(t.x2 - c.x, t.y2 - c.y, -rot)
        out.append(f'    (fp_line (start {_f(a[0] * MIL_TO_MM)} {_f(-a[1] * MIL_TO_MM)})'
                   f' (end {_f(b[0] * MIL_TO_MM)} {_f(-b[1] * MIL_TO_MM)})'
                   f' (stroke (width {_f(fr.mm(t.width))}) (type solid)) (layer "{layer_name(t.layer, warn)}") (uuid "{_uuid()}"))')
    for a in c.arcs:
        if a.layer in COPPER:
            continue
        pts = arc_points(a)
        w = _f(fr.mm(a.width))
        lname = layer_name(a.layer, warn)
        if pts is None:
            cx, cy = rotate(a.x - c.x, a.y - c.y, -rot)
            out.append(f'    (fp_circle (center {_f(cx * MIL_TO_MM)} {_f(-cy * MIL_TO_MM)})'
                       f' (end {_f((cx + a.radius) * MIL_TO_MM)} {_f(-cy * MIL_TO_MM)})'
                       f' (stroke (width {w}) (type solid)) (fill no) (layer "{lname}") (uuid "{_uuid()}"))')
        else:
            s, m, e = (rotate(px - c.x, py - c.y, -rot) for px, py in pts)
            out.append(f'    (fp_arc (start {_f(s[0] * MIL_TO_MM)} {_f(-s[1] * MIL_TO_MM)})'
                       f' (mid {_f(m[0] * MIL_TO_MM)} {_f(-m[1] * MIL_TO_MM)})'
                       f' (end {_f(e[0] * MIL_TO_MM)} {_f(-e[1] * MIL_TO_MM)})'
                       f' (stroke (width {w}) (type solid)) (layer "{lname}") (uuid "{_uuid()}"))')
    for f in c.fills:
        if f.layer in COPPER:
            continue
        a = rotate(f.x1 - c.x, f.y1 - c.y, -rot); b = rotate(f.x2 - c.x, f.y2 - c.y, -rot)
        out.append(f'    (fp_rect (start {_f(a[0] * MIL_TO_MM)} {_f(-a[1] * MIL_TO_MM)})'
                   f' (end {_f(b[0] * MIL_TO_MM)} {_f(-b[1] * MIL_TO_MM)})'
                   f' (stroke (width 0) (type solid)) (fill yes) (layer "{layer_name(f.layer, warn)}") (uuid "{_uuid()}"))')
    for p in c.pads:
        emit_pad(out, fr, p, c.x, c.y, rot, f"{side}.Cu", nets)
    out.append("  )")


def emit_free_pad(out: list, fr: Frame, p: pcb9.Pad, nets: Nets):
    """A pad outside any component becomes a one-pad footprint."""
    ax, ay = fr.pt(p.x, p.y)
    side = "B" if p.layer == 16 else "F"
    out.append(f'  (footprint "FreePad" (layer "{side}.Cu") (uuid "{_uuid()}") (at {_f(ax)} {_f(ay)} 0)')
    out.append(f'    (property "Reference" "" (at 0 0 0) (layer "{side}.SilkS") (hide yes) (uuid "{_uuid()}")'
               f' (effects (font (size 1 1) (thickness 0.15))))')
    out.append(f'    (property "Value" "{_q(p.name)}" (at 0 0 0) (layer "{side}.Fab") (hide yes) (uuid "{_uuid()}")'
               f' (effects (font (size 1 1) (thickness 0.15))))')
    out.append(f'    (attr {"through_hole" if (p.hole > 0 or p.layer == MULTILAYER) else "smd"} board_only exclude_from_pos_files exclude_from_bom)')
    emit_pad(out, fr, p, p.x, p.y, 0.0, f"{side}.Cu", nets)
    out.append("  )")


def generate(b: pcb9.Board, outline_layer: int) -> tuple[str, dict]:
    warn: set = set()
    layers = dict(LAYERS)
    if outline_layer != 29:
        layers.pop(29, None)
        layers[outline_layer] = "Edge.Cuts"
    LAYERS.clear(); LAYERS.update(layers)

    x0, y0, x1, y1 = board_extent(b)
    fr = Frame(math.floor(x0 / 1000.0) * 1000.0 - MARGIN_MIL,
               math.ceil(y1 / 1000.0) * 1000.0 + MARGIN_MIL)
    paper = choose_paper((x1 - x0) * MIL_TO_MM, (y1 - y0) * MIL_TO_MM)

    used_copper = {t.layer for t in b.tracks} | {t.layer for c in b.components for t in c.tracks}
    used_copper |= {f.layer for f in b.fills} | {f.layer for c in b.components for f in c.fills}
    inner = [L for L in used_copper if 2 <= L <= 15]
    copper_layers = 2 + (max(inner) - 1 if inner else 0)
    if copper_layers % 2:
        copper_layers += 1

    nets = Nets(b)
    out: list[str] = []
    emit_header(out, copper_layers, nets, paper)
    stats = dict(components=0, segments=0, vias=0, arcs_copper=0, arcs_graphic=0, fills=0,
                 outline=0, texts=0, free_pads=0, gr_lines=0, nets=len(nets.names),
                 polygons=0, parse_errors=len(b.errors))

    # board-level copper from components and free objects
    def via(v):
        p = fr.pt(v.x, v.y)
        out.append(f'  (via (at {_f(p[0])} {_f(p[1])}) (size {_f(fr.mm(v.diameter))}) (drill {_f(fr.mm(v.hole))})'
                   f' (layers "F.Cu" "B.Cu") {nets.clause(v.net)} (uuid "{_uuid()}"))')
        stats["vias"] += 1

    for c in b.components:
        for t in c.tracks:
            if t.layer in COPPER:
                emit_segment(out, fr, t.x1, t.y1, t.x2, t.y2, t.width, layer_name(t.layer, warn), nets.clause(t.net)); stats["segments"] += 1
        for a in c.arcs:
            if a.layer in COPPER:
                emit_arc(out, fr, a, layer_name(a.layer, warn), True, nets.clause(a.net)); stats["arcs_copper"] += 1
        for f in c.fills:
            if f.layer in COPPER:
                emit_fill(out, fr, f, layer_name(f.layer, warn), True, nets); stats["fills"] += 1
        for v in c.vias:
            via(v)
    for t in b.tracks:
        lname = layer_name(t.layer, warn)
        if t.layer in COPPER:
            emit_segment(out, fr, t.x1, t.y1, t.x2, t.y2, t.width, lname, nets.clause(t.net)); stats["segments"] += 1
        elif t.layer == outline_layer:
            emit_gr_line(out, fr, t.x1, t.y1, t.x2, t.y2, 0.1 / MIL_TO_MM, "Edge.Cuts"); stats["outline"] += 1
        else:
            emit_gr_line(out, fr, t.x1, t.y1, t.x2, t.y2, t.width, lname); stats["gr_lines"] += 1
    for v in b.vias:
        via(v)
    for a in b.arcs:
        copper = a.layer in COPPER
        emit_arc(out, fr, a, layer_name(a.layer, warn), copper, nets.clause(a.net))
        stats["arcs_copper" if copper else "arcs_graphic"] += 1
    for f in b.fills:
        copper = f.layer in COPPER
        emit_fill(out, fr, f, layer_name(f.layer, warn), copper, nets); stats["fills"] += 1
    for t in b.texts:
        emit_text(out, fr, t, layer_name(t.layer, warn)); stats["texts"] += 1
    for c in b.components:
        emit_component(out, fr, c, warn, nets); stats["components"] += 1
        stats["arcs_graphic"] += sum(1 for a in c.arcs if a.layer not in COPPER)
    for p in b.pads:
        emit_free_pad(out, fr, p, nets); stats["free_pads"] += 1
    for pg in b.polygons:
        # Vertex axis order inferred from two archive files only (hatch track
        # endpoints land on the outline when read as (y, x), never as (x, y)).
        # Written as an unfilled zone outline so a wrong guess is visible, not copper.
        if len(pg.vertices) < 3:
            continue
        pts = " ".join(f"(xy {_f(px)} {_f(py)})" for px, py in (fr.pt(vx, vy) for vy, vx in pg.vertices))
        lname = layer_name(pg.layer, warn)
        code = nets.code(pg.net)
        name = _q(nets.names[code - 1]) if code else ""
        out.append(f'  (zone (net {code}) (net_name "{name}") (layer "{lname}") (uuid "{_uuid()}")'
                   f' (hatch edge 0.5) (connect_pads (clearance 0.5)) (min_thickness {_f(fr.mm(pg.width) or 0.25)})'
                   f' (fill (thermal_gap 0.5) (thermal_bridge_width 0.5)) (polygon (pts {pts})))')
        stats["polygons"] = stats.get("polygons", 0) + 1
    out.append(")")
    stats["unmapped_layers"] = sorted(warn)
    stats["net_index_out_of_range"] = nets.bad
    stats["frame_origin_mil"] = (fr.x0, fr.y0)
    return "\n".join(out) + "\n", stats


HEADER9 = b"PCB FILE 9 VERSION 2.70"


def convert(binary: Path, output: Path, outline_layer: int | None = None,
            quiet: bool = True, document: str | None = None) -> dict:
    """Convert one board in any supported Protel format.

    `outline_layer` defaults to whatever the detected format uses, because the
    generations disagree: 29 in `PCB FILE 9`, 28 in `PCB FILE 6`. Passing a
    number overrides that for boards that break the habit.

    `document` picks one board out of a `.ddb`, which holds a whole project
    rather than a single layout. Without it the largest board in the database
    is taken - in a project with one layout and a pile of libraries, that is
    the layout.
    """
    if document is not None:
        b = ddb.parse(binary, document)
        fmt = formats.identify(binary)
    else:
        b, fmt = formats.parse(binary)
    text, stats = generate(b, fmt.outline_layer if outline_layer is None
                           else outline_layer)
    stats["format"] = fmt.label
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    if not quiet:
        print(f"[protel-to-kicad] {binary.name} ({fmt.label}) -> {output}", file=sys.stderr)
        print(f"[protel-to-kicad] {stats}", file=sys.stderr)
        if b.unknown_tags:
            print(f"[protel-to-kicad] WARNING unknown tags {b.unknown_tags}", file=sys.stderr)
    return stats


def batch(in_dir: Path, out_dir: Path, outline_layer: int | None = None) -> int:
    """Convert every readable Protel board under in_dir, mirroring the tree.

    One line per board. Boards read in salvage mode are flagged; boards in a
    format that is recognised but not decoded are reported by name rather than
    silently skipped, so a directory full of them does not look empty.
    """
    files = sorted(p for p in in_dir.rglob("*")
                   if p.is_file() and p.suffix.lower() in formats.readable_suffixes())
    done = failed = 0
    undecoded: dict = {}
    unknown = 0
    for p in files:
        fmt = formats.identify(p)
        if fmt is None:
            unknown += 1
            continue
        if fmt.reader is None:
            label = f"{fmt.label} not decoded yet"
            undecoded[label] = undecoded.get(label, 0) + 1
            continue
        if fmt.key == "ddb":
            # A design database is a project, not a board. Converting only the
            # first one found would silently drop the rest.
            found = ddb.boards(p)
            if not found:
                try:
                    ddb.parse(p)
                except ddb.ParseError as e:
                    undecoded[str(e)] = undecoded.get(str(e), 0) + 1
                continue
            for n, b in enumerate(found):
                suffix = "" if len(found) == 1 else f"-{n}"
                target = (out_dir / p.relative_to(in_dir)
                          .with_suffix("")).with_name(
                              p.stem + suffix + ".kicad_pcb")
                text, stats = generate(b, fmt.outline_layer if outline_layer is None
                                       else outline_layer)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
                done += 1
                flag = (f"  SALVAGED ({stats['parse_errors']} errors)"
                        if stats["parse_errors"] else "")
                print(f"ok    {target.name}  [ddb]  components {stats['components']}"
                      f" segments {stats['segments']} vias {stats['vias']}"
                      f" nets {stats['nets']}{flag}")
            continue

        target = out_dir / p.relative_to(in_dir).with_suffix(".kicad_pcb")
        try:
            stats = convert(p, target, outline_layer)
        except formats.UnsupportedFormat as e:
            # The format table said there is a reader, and the reader turned
            # the file down - a vintage it will not claim to understand, or a
            # design database with nothing readable inside. That is the same
            # answer as a format with no reader at all, so it counts the same
            # way. Calling it a failure would put a red line against files
            # this package never claimed.
            undecoded[str(e).split(": ", 1)[-1]] = \
                undecoded.get(str(e).split(": ", 1)[-1], 0) + 1
            continue
        except Exception as e:  # noqa: BLE001 - keep the batch going, report at the end
            failed += 1
            print(f"FAIL  {p.relative_to(in_dir)}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        done += 1
        flag = f"  SALVAGED ({stats['parse_errors']} errors)" if stats["parse_errors"] else ""
        print(f"ok    {p.relative_to(in_dir)}  [{fmt.key}]  components {stats['components']}"
              f" segments {stats['segments']} vias {stats['vias']} nets {stats['nets']}{flag}")
    print(f"batch: {done} converted, {failed} failed", file=sys.stderr)
    for label, n in sorted(undecoded.items()):
        print(f"       {n} skipped: {label}", file=sys.stderr)
    if unknown:
        print(f"       {unknown} skipped: header not recognised", file=sys.stderr)
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("binary", type=Path, nargs="?")
    ap.add_argument("-o", "--output", type=Path)
    ap.add_argument("--batch", nargs=2, metavar=("IN_DIR", "OUT_DIR"), type=Path)
    ap.add_argument("--outline-layer", type=int, default=None,
                    help="Protel layer holding the board outline "
                         "(default: per format, 29 for PCB FILE 9, 28 for PCB FILE 6)")
    ap.add_argument("--list", action="store_true",
                    help="list the documents inside a .ddb design database and stop")
    ap.add_argument("--document", metavar="INDEX_OR_FORMAT", default=None,
                    help="which board to take out of a .ddb (see --list); "
                         "default is the largest one in it")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--hide-designators", action="store_true",
                    help="write component designators as hidden fields, as Protel's silkscreen plots in this archive show them")
    a = ap.parse_args()
    global HIDE_DESIGNATORS
    HIDE_DESIGNATORS = a.hide_designators
    if a.list:
        if a.binary is None:
            ap.error("--list needs a file")
        try:
            print(ddb.listing(a.binary))
        except ddb.ParseError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        return 0
    if a.batch:
        return batch(a.batch[0], a.batch[1], a.outline_layer)
    if a.binary is None or a.output is None:
        ap.error("need BOARD.PCB -o OUT.kicad_pcb, or --batch IN_DIR OUT_DIR")
    try:
        convert(a.binary, a.output, a.outline_layer, a.quiet, a.document)
    except (formats.UnsupportedFormat, ddb.ParseError) as e:
        # A file this package cannot read is an ordinary outcome, not a crash.
        # A traceback here buries the one line that says which format it is.
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
