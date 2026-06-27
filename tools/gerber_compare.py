#!/usr/bin/env python3
"""Gerber-to-Gerber pad comparison tool.

Compares pad flash positions between two Gerber files (RS-274X).
Handles different coordinate formats (MOIN X2.3 vs MOMM X4.6).

Usage:
    python3 gerber_compare.py original.GTL converted.GTL
    python3 gerber_compare.py --threshold 10 original.GTL converted.GTL
"""

import re
import sys
import math
from pathlib import Path


def parse_gerber_flashes(filepath: str) -> tuple[list[tuple[float, float]], dict]:
    """Parse a Gerber file and extract all flash (D03) positions in mils.
    
    Returns (flashes_in_mils, metadata).
    """
    data = Path(filepath).read_text(encoding='latin-1')
    lines = data.strip().split('\n')
    
    # Detect format
    is_mm = False
    scale_x = 1.0
    scale_y = 1.0
    
    for line in lines[:30]:
        if 'MOMM' in line:
            is_mm = True
        if 'MOIN' in line:
            is_mm = False
        # FSLAX may be preceded by *% - strip leading non-alpha chars
        m = re.search(r'FSLAX(\d)(\d)Y(\d)(\d)', line)
        if m:
            x_int, x_dec = int(m.group(1)), int(m.group(2))
            y_int, y_dec = int(m.group(3)), int(m.group(4))
            scale_x = 10 ** -(x_dec)
            scale_y = 10 ** -(y_dec)
    
    cur_x, cur_y = 0, 0
    flashes = []
    
    for line in lines:
        line = line.strip().rstrip('*')
        if line.startswith('%') or line.startswith('G04'):
            continue
            
        # Parse coordinate updates
        m = re.match(r'X(-?\d+)Y(-?\d+)D?(\d*)', line)
        if m:
            cur_x = int(m.group(1))
            cur_y = int(m.group(2))
            d = m.group(3).lstrip('0') or '0'
            if d == '3':
                flashes.append((cur_x, cur_y))
            continue
        
        m = re.match(r'X(-?\d+)D?(\d*)', line)
        if m:
            cur_x = int(m.group(1))
            d = m.group(2).lstrip('0') or '0'
            if d == '3':
                flashes.append((cur_x, cur_y))
            continue
            
        m = re.match(r'Y(-?\d+)D?(\d*)', line)
        if m:
            cur_y = int(m.group(1))
            d = m.group(2).lstrip('0') or '0'
            if d == '3':
                flashes.append((cur_x, cur_y))
            continue
        
        if line == 'D03':
            flashes.append((cur_x, cur_y))
    
    # Convert to mils
    if is_mm:
        # MOMM: value * scale = mm, then mm / 0.0254 = mils
        # Gerber RS-274X always uses Y-up, regardless of MOIN/MOMM
        flashes_mil = [
            (x * scale_x / 0.0254, y * scale_y / 0.0254)
            for x, y in flashes
        ]
    else:
        # MOIN: value * scale = inches, then inches * 1000 = mils
        flashes_mil = [
            (x * scale_x * 1000, y * scale_y * 1000)
            for x, y in flashes
        ]
    
    metadata = {
        'format': 'MOMM' if is_mm else 'MOIN',
        'raw_count': len(flashes),
        'x_range': (min(x for x, y in flashes_mil), max(x for x, y in flashes_mil)) if flashes_mil else (0, 0),
        'y_range': (min(y for x, y in flashes_mil), max(y for x, y in flashes_mil)) if flashes_mil else (0, 0),
    }
    
    return flashes_mil, metadata


def parse_gerber_draws(filepath: str) -> list[tuple[float, float, float, float]]:
    """Parse draw segments (D01) from Gerber. Returns [(x1,y1,x2,y2)] in mils."""
    data = Path(filepath).read_text(encoding='latin-1')
    lines = data.strip().split('\n')
    
    is_mm = False
    scale_x = scale_y = 1.0
    for line in lines[:30]:
        if 'MOMM' in line: is_mm = True
        if 'MOIN' in line: is_mm = False
        m = re.search(r'FSLAX(\d)(\d)Y(\d)(\d)', line)
        if m:
            scale_x = 10 ** -(int(m.group(2)))
            scale_y = 10 ** -(int(m.group(4)))
    
    cur_x, cur_y = 0, 0
    prev_x, prev_y = 0, 0
    segments = []
    
    for line in lines:
        line = line.strip().rstrip('*')
        if line.startswith('%') or line.startswith('G04'):
            continue
        m = re.match(r'X(-?\d+)Y(-?\d+)D?(\d*)', line)
        if m:
            prev_x, prev_y = cur_x, cur_y
            cur_x, cur_y = int(m.group(1)), int(m.group(2))
            d = m.group(3)
            if d == '1':
                segments.append((prev_x, prev_y, cur_x, cur_y))
            continue
        m = re.match(r'X(-?\d+)D?(\d*)', line)
        if m:
            prev_x, prev_y = cur_x, cur_y
            cur_x = int(m.group(1))
            if m.group(2) == '1':
                segments.append((prev_x, prev_y, cur_x, cur_y))
            continue
        m = re.match(r'Y(-?\d+)D?(\d*)', line)
        if m:
            prev_x, prev_y = cur_x, cur_y
            cur_y = int(m.group(1))
            if m.group(2) == '1':
                segments.append((prev_x, prev_y, cur_x, cur_y))
            continue
        if line == 'D01':
            segments.append((prev_x, prev_y, cur_x, cur_y))
    
    if is_mm:
        return [(x1*scale_x/0.0254, y1*scale_y/0.0254, 
                 x2*scale_x/0.0254, y2*scale_y/0.0254) for x1,y1,x2,y2 in segments]
    else:
        return [(x1*scale_x*1000, y1*scale_y*1000,
                 x2*scale_x*1000, y2*scale_y*1000) for x1,y1,x2,y2 in segments]


def compare_flashes(
    original: list[tuple[float, float]],
    converted: list[tuple[float, float]],
    threshold: float = 20.0,
    auto_align: bool = True,
) -> dict:
    """Compare two sets of flash positions.
    
    When auto_align=True (default):
    - Detects Y-sign mismatch (e.g. MOIN Y-up vs MOMM Y-down from KiCad) and corrects
    - No centroid alignment (unreliable when flash counts differ significantly)
    
    Returns dict with match statistics.
    """
    if not original or not converted:
        return {'error': 'Empty flash list'}
    
    # Detect and correct Y-sign mismatch between coordinate systems.
    # KiCad Gerber exports use negative Y (KiCad internal Y-down -> Gerber Y-up),
    # so converted files typically have negative Y while Protel originals have positive Y.
    y_negated = False
    if original and converted:
        orig_y_mean = sum(y for _, y in original) / len(original)
        conv_y_mean = sum(y for _, y in converted) / len(converted)
        if (orig_y_mean > 0) != (conv_y_mean > 0) and abs(orig_y_mean) > 100 and abs(conv_y_mean) > 100:
            converted = [(x, -y) for x, y in converted]
            y_negated = True
            print(f"\n[align] Detected Y-sign mismatch (orig mean Y={orig_y_mean:.0f}, conv mean Y={conv_y_mean:.0f}), negated converted Y")

    # Coarse alignment: if the coordinate ranges don't overlap, shift the
    # converted set to match. Uses mean centroid shift. Handles boards with
    # different protel_to_gerber offsets.
    if auto_align and original and converted:
        orig_cx = sum(x for x, y in original) / len(original)
        orig_cy = sum(y for x, y in original) / len(original)
        conv_cx = sum(x for x, y in converted) / len(converted)
        conv_cy = sum(y for x, y in converted) / len(converted)
        coarse_dx = orig_cx - conv_cx
        coarse_dy = orig_cy - conv_cy
        if abs(coarse_dx) > 200 or abs(coarse_dy) > 200:
            converted = [(x + coarse_dx, y + coarse_dy) for x, y in converted]
            print(f"[align] Coarse centroid alignment: shifted by ({coarse_dx:.1f}, {coarse_dy:.1f}) mils")
    
    # After Y negation, check for Y-mirror: if our Y transform includes an inversion
    # (y_kicad = const - y_protel), then negating Y produces (y_protel - const), which
    # is still offset. Detect by finding the dominant sum (oy + cy) which equals the
    # mirror constant C, then apply y' = C - y.
    if auto_align and original and converted and y_negated:
        # Find X-close pairs and compute oy + cy to detect mirror constant
        y_sums = []
        for ox, oy in original:
            best_xd = float('inf')
            best_cy = None
            for cx, cy in converted:
                xd = abs(ox - cx)
                if xd < best_xd:
                    best_xd = xd
                    best_cy = cy
            if best_xd < 50 and best_cy is not None:
                y_sums.append(round(oy + best_cy))
        
        if len(y_sums) >= 10:
            from collections import Counter as _Counter
            sum_counts = _Counter(y_sums)
            mode_val, mode_count = sum_counts.most_common(1)[0]
            # If the dominant sum appears in >20% of pairs, it's the mirror constant
            if mode_count >= len(y_sums) * 0.2:
                C = mode_val
                converted = [(x, C - y) for x, y in converted]
                print(f"[align] Detected Y-mirror (C={C}), applied: y' = {C} - y")
    
    # Final fine alignment: use best-matching pairs (within 100 mil) to compute
    # residual shift. More robust than median when flash counts differ.
    if auto_align and original and converted:
        fine_pairs = []
        for ox, oy in original:
            best_d = float('inf')
            best_c = None
            for cx, cy in converted:
                d = math.sqrt((ox - cx)**2 + (oy - cy)**2)
                if d < best_d:
                    best_d = d
                    best_c = (cx, cy)
            if best_d < 100 and best_c is not None:
                fine_pairs.append((ox - best_c[0], oy - best_c[1]))
        
        if len(fine_pairs) >= 20:
            # Use median of offsets (robust to outliers)
            sorted_dx = sorted(d[0] for d in fine_pairs)
            sorted_dy = sorted(d[1] for d in fine_pairs)
            fdx = sorted_dx[len(sorted_dx) // 2]
            fdy = sorted_dy[len(sorted_dy) // 2]
            if abs(fdx) > 1.0 or abs(fdy) > 1.0:
                converted = [(x + fdx, y + fdy) for x, y in converted]
                print(f"[align] Fine alignment: shifted by ({fdx:.1f}, {fdy:.1f}) mils using {len(fine_pairs)} pairs")
    
    # For each original flash, find nearest converted flash
    matches = []
    for ox, oy in original:
        best_dist = float('inf')
        best_match = None
        for cx, cy in converted:
            d = math.sqrt((ox - cx)**2 + (oy - cy)**2)
            if d < best_dist:
                best_dist = d
                best_match = (cx, cy)
        matches.append({
            'orig': (ox, oy),
            'match': best_match,
            'dist': best_dist,
        })
    
    distances = [m['dist'] for m in matches]
    distances.sort()
    
    within_5 = sum(1 for d in distances if d <= 5)
    within_10 = sum(1 for d in distances if d <= 10)
    within_20 = sum(1 for d in distances if d <= 20)
    within_50 = sum(1 for d in distances if d <= 50)
    
    return {
        'original_count': len(original),
        'converted_count': len(converted),
        'within_5_mil': within_5,
        'within_10_mil': within_10,
        'within_20_mil': within_20,
        'within_50_mil': within_50,
        'pct_5': within_5 / len(original) * 100,
        'pct_10': within_10 / len(original) * 100,
        'pct_20': within_20 / len(original) * 100,
        'pct_50': within_50 / len(original) * 100,
        'mean_dist': sum(distances) / len(distances),
        'median_dist': distances[len(distances) // 2],
        'max_dist': max(distances),
        'min_dist': min(distances),
        'worst_10': sorted(matches, key=lambda m: -m['dist'])[:10],
        'best_10': sorted(matches, key=lambda m: m['dist'])[:10],
    }


def print_report(stats: dict, label: str = ""):
    """Print a human-readable comparison report."""
    if label:
        print(f"\n{'='*60}")
        print(f"  {label}")
        print(f"{'='*60}")
    
    print(f"\nFlash counts: original={stats['original_count']}, converted={stats['converted_count']}")
    print(f"\nMatch quality (original -> nearest converted):")
    print(f"  ≤5 mil:   {stats['within_5_mil']:4d} / {stats['original_count']}  ({stats['pct_5']:.1f}%)")
    print(f"  ≤10 mil:  {stats['within_10_mil']:4d} / {stats['original_count']}  ({stats['pct_10']:.1f}%)")
    print(f"  ≤20 mil:  {stats['within_20_mil']:4d} / {stats['original_count']}  ({stats['pct_20']:.1f}%)")
    print(f"  ≤50 mil:  {stats['within_50_mil']:4d} / {stats['original_count']}  ({stats['pct_50']:.1f}%)")
    print(f"\n  Mean:   {stats['mean_dist']:.1f} mil")
    print(f"  Median: {stats['median_dist']:.1f} mil")
    print(f"  Min:    {stats['min_dist']:.1f} mil")
    print(f"  Max:    {stats['max_dist']:.1f} mil")
    
    print(f"\nBest 5 matches:")
    for m in stats['best_10'][:5]:
        ox, oy = m['orig']
        cx, cy = m['match']
        print(f"  ({ox:.0f},{oy:.0f}) -> ({cx:.0f},{cy:.0f})  dist={m['dist']:.1f}")
    
    print(f"\nWorst 5 matches:")
    for m in stats['worst_10'][:5]:
        ox, oy = m['orig']
        cx, cy = m['match']
        print(f"  ({ox:.0f},{oy:.0f}) -> ({cx:.0f},{cy:.0f})  dist={m['dist']:.1f}")


def full_compare(original_path: str, converted_path: str, threshold: float = 20.0) -> dict:
    """Full comparison: parse both files, compare, print report, return stats."""
    print(f"Original:  {original_path}")
    print(f"Converted: {converted_path}")
    
    orig_flashes, orig_meta = parse_gerber_flashes(original_path)
    conv_flashes, conv_meta = parse_gerber_flashes(converted_path)
    
    print(f"\nOriginal:  {orig_meta['format']}, {orig_meta['raw_count']} flashes, "
          f"X={orig_meta['x_range'][0]:.0f}..{orig_meta['x_range'][1]:.0f}, "
          f"Y={orig_meta['y_range'][0]:.0f}..{orig_meta['y_range'][1]:.0f}")
    print(f"Converted: {conv_meta['format']}, {conv_meta['raw_count']} flashes, "
          f"X={conv_meta['x_range'][0]:.0f}..{conv_meta['x_range'][1]:.0f}, "
          f"Y={conv_meta['y_range'][0]:.0f}..{conv_meta['y_range'][1]:.0f}")
    
    stats = compare_flashes(orig_flashes, conv_flashes, threshold)
    print_report(stats, "PAD FLASH COMPARISON")
    
    return stats


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Compare Gerber pad positions')
    parser.add_argument('original', help='Original Gerber file (ground truth)')
    parser.add_argument('converted', help='Converted Gerber file (to verify)')
    parser.add_argument('--threshold', type=float, default=20.0, help='Match threshold in mils')
    args = parser.parse_args()
    
    stats = full_compare(args.original, args.converted, args.threshold)
    
    # Exit code: 0 if >90% within threshold, 1 otherwise
    pct = stats[f'within_{int(args.threshold)}_mil'] / stats['original_count'] * 100 if f'within_{int(args.threshold)}_mil' in stats else stats['pct_20']
    sys.exit(0 if pct >= 90 else 1)
