"""Combine relative GNSS cuts and draw explicitly labelled interpolated estimates."""
import argparse
import csv
import json
import math
import os
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy.interpolate import PchipInterpolator


def merge_nodes(points):
    """Merge equal cut angles using observation-count-weighted means in dB.

    Inputs are already normalized group/bin medians, not raw observations.
    Round angles to nine decimal places to avoid insignificant floating-point
    differences creating duplicate nodes. Report source groups and weighted
    between-bin spread for traceability; that spread is not a confidence interval.
    """
    buckets = defaultdict(list)
    for row in points:
        angle = float(row['plane_angle_deg'])
        gain, count = float(row['relative_gain_db']), int(row['count'])
        if not math.isfinite(angle) or not math.isfinite(gain) or count <= 0:
            raise ValueError('Combined cuts require finite angles/gains and positive sample counts')
        buckets[round(angle, 9)].append((gain, count, row['signal_group']))
    nodes = []
    for angle, values in sorted(buckets.items()):
        weights = np.array([v[1] for v in values])
        gains = np.array([v[0] for v in values])
        mean = float(np.average(gains, weights=weights))
        nodes.append(dict(plane_angle_deg=angle, relative_gain_db=mean,
                          sample_count=int(weights.sum()), source_bin_count=len(values),
                          source_groups=';'.join(sorted({v[2] for v in values})),
                          between_bin_std_db=float(np.sqrt(np.average((gains - mean) ** 2, weights=weights)))))
    return nodes


def merge_spatial_nodes(patterns):
    """Merge full-sky bins only when both azimuth and elevation agree.

    Preserve each input group's peak normalization and average bin medians
    in dB with observation counts as weights, just as for combined cuts.
    Unlike cut projection, different elevations must remain distinct. No
    unobserved direction is interpolated, and the merged peak is not reset.
    """
    elevations = defaultdict(list)
    for row in patterns:
        az, el = float(row['azimuth_deg']), float(row['elevation_deg'])
        if not math.isfinite(az) or not math.isfinite(el) or not 0 <= el <= 90:
            raise ValueError('3D bins require finite azimuth and elevation in [0, 90] degrees')
        elevations[round(el, 9)].append(dict(row, plane_angle_deg=az % 360))
    result = []
    for elevation, rows in sorted(elevations.items()):
        for node in merge_nodes(rows):
            azimuth = node.pop('plane_angle_deg')
            result.append(dict(azimuth_deg=azimuth, elevation_deg=elevation,
                               theta_deg=90 - elevation, phi_deg=azimuth, **node))
    return result


def draw_pattern_3d(rows, out, title):
    """Plot observed direction bins in local east/north/up coordinates.

    Radius is relative power 10**(gain_dB/10), not field amplitude. Colour
    encodes gain in dB. Use identical axis limits for every signal and the
    combined estimate, preserving missing directions as gaps in a scatter
    plot rather than filling them with an unsupported surface.
    """
    import matplotlib.pyplot as plt

    az = np.radians([r['azimuth_deg'] for r in rows])
    el = np.radians([r['elevation_deg'] for r in rows])
    gain = np.array([r['relative_gain_db'] for r in rows], dtype=float)
    radius = 10 ** (gain / 10)
    fig = plt.figure(figsize=(8, 7))
    ax = fig.add_subplot(111, projection='3d')
    points = ax.scatter(radius * np.cos(el) * np.sin(az),
                        radius * np.cos(el) * np.cos(az), radius * np.sin(el),
                        c=gain, cmap='viridis', s=20)
    ax.set(xlabel='East', ylabel='North', zlabel='Up',
           title=f'{title}\nRelative pattern (linear power radius)',
           xlim=(-1, 1), ylim=(-1, 1), zlim=(-1, 1))
    ax.set_box_aspect((1, 1, 1))
    fig.colorbar(points, ax=ax, label='Relative gain (dB)', shrink=.65)
    fig.savefig(Path(out) / 'pattern_3d.png', dpi=160, bbox_inches='tight')
    plt.close(fig)


def interpolate_nodes(nodes, circular=False, max_gap_deg=30, step_deg=.5):
    """Return shape-preserving PCHIP samples only within connected observed arcs.

    Nodes must have unique increasing angles; circular angles lie in [0,360).
    Split gaps larger than max_gap_deg and omit singleton curves. A complete
    circle uses repeated neighbours to match values and slopes at the seam;
    an incomplete circle starts after its largest gap. Keep wrapped angles
    for display and unwrapped angles for continuity, with segment IDs marking
    curves that must not be joined. Interpolation does not add measured coverage.
    """
    if not 0 < max_gap_deg <= 360 or not math.isfinite(max_gap_deg):
        raise ValueError('Combined maximum gap must be in (0, 360] degrees')
    if not math.isfinite(step_deg) or step_deg <= 0:
        raise ValueError('Interpolation step must be positive')
    if len(nodes) < 2:
        return []
    x = np.array([n['plane_angle_deg'] for n in nodes])
    y = np.array([n['relative_gain_db'] for n in nodes])
    if np.any(np.diff(x) <= 0):
        raise ValueError('Interpolation nodes must have unique increasing angles')
    if circular:
        if x[0] < 0 or x[-1] >= 360:
            raise ValueError('Circular nodes must have angles in [0, 360)')
        gaps = np.diff(np.r_[x, x[0] + 360])
        if np.max(gaps) <= max_gap_deg:
            # Neighbours on both sides give matching values and slopes at 0/360.
            fn = PchipInterpolator(np.r_[x - 360, x, x + 360], np.tile(y, 3), extrapolate=False)
            grid = np.linspace(0, 360, math.ceil(360 / step_deg) + 1)
            return [dict(segment_id=0, plane_angle_deg=float(a % 360),
                         unwrapped_angle_deg=float(a), interpolated_gain_db=float(g)) for a, g in zip(grid, fn(grid))]
        # Cut the circular sequence at its largest missing arc, then unwrap the remainder.
        start = (int(np.argmax(gaps)) + 1) % len(x)
        x = np.r_[x[start:], x[:start] + 360]
        y = np.r_[y[start:], y[:start]]
    boundaries = np.flatnonzero(np.diff(x) > max_gap_deg) + 1
    curve = []
    for segment, indices in enumerate(np.split(np.arange(len(x)), boundaries)):
        if len(indices) < 2:
            continue
        sx, sy = x[indices], y[indices]
        grid = np.unique(np.r_[np.linspace(sx[0], sx[-1], math.ceil((sx[-1] - sx[0]) / step_deg) + 1), sx])
        values = PchipInterpolator(sx, sy, extrapolate=False)(grid)
        curve.extend(dict(segment_id=segment, plane_angle_deg=float(a % 360 if circular else a),
                          unwrapped_angle_deg=float(a), interpolated_gain_db=float(g)) for a, g in zip(grid, values))
    return curve


def save_csv(path, rows, fields):
    """Write a cut table with explicit columns, even when no rows are available.

    The shared schema preserves machine-readable empty outputs for uncovered
    directions and keeps points, weighted nodes and interpolated samples separate.
    """
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def draw_combined(name, points, nodes, curve, out, group_colors, floor, title, circular, vertical_labels):
    """Render source bins and interpolated estimates in polar and Cartesian panels.

    Reuse group colours and the gain floor across cuts. Polar radii are shifted
    by the floor but tick labels remain in dB. Respect segment boundaries in
    both panels and split wrapped angles in the Cartesian panel to prevent
    a spurious line across 0/360 degrees. Missing coverage remains visible.
    """
    import matplotlib.pyplot as plt
    fig = plt.figure(figsize=(12, 6.2))
    polar = fig.add_subplot(121, projection='polar')
    cart = fig.add_subplot(122)
    polar.set_theta_zero_location('N' if circular else 'E')
    polar.set_theta_direction(-1 if circular else 1)
    maximum = 360 if circular else (180 if vertical_labels else 90)
    if circular:
        polar.set_thetagrids([0, 90, 180, 270], ['N', 'E', 'S', 'W'])
    else:
        polar.set_thetamin(0)
        polar.set_thetamax(maximum)
        if vertical_labels:
            polar.set_thetagrids([0, 30, 60, 90, 120, 150, 180],
                                 [vertical_labels[0], '30°', '60°', 'Zenith', '60°', '30°', vertical_labels[1]])
    for group, color in group_colors.items():
        selected = [p for p in points if p['signal_group'] == group]
        if not selected:
            continue
        angles = [p['plane_angle_deg'] for p in selected]
        gains = np.array([p['relative_gain_db'] for p in selected])
        polar.scatter(np.radians(angles), gains - floor, s=20, color=color, alpha=.6, label=group)
        cart.scatter(angles, gains, s=20, color=color, alpha=.6)
    if nodes:
        polar.scatter(np.radians([n['plane_angle_deg'] for n in nodes]),
                      [n['relative_gain_db'] - floor for n in nodes], marker='x', color='black', s=25, label='Weighted nodes')
    segments = defaultdict(list)
    for row in curve:
        segments[row['segment_id']].append(row)
    for i, rows in enumerate(segments.values()):
        angles = np.array([r['plane_angle_deg'] for r in rows])
        gains = np.array([r['interpolated_gain_db'] for r in rows])
        polar.plot(np.radians([r['unwrapped_angle_deg'] for r in rows]), gains - floor,
                   color='black', lw=2, label='PCHIP estimate' if i == 0 else None)
        # Cartesian coordinates jump backward at north; never draw that jump as a line.
        for index in np.split(np.arange(len(rows)), np.flatnonzero(np.diff(angles) < 0) + 1):
            cart.plot(angles[index], gains[index], color='black', lw=2)
    polar.set_ylim(0, -floor)
    ticks = np.arange(floor, 1, 10)
    polar.set_yticks(ticks - floor, [f'{v:g}' for v in ticks], fontsize=8)
    cart.set(xlim=(0, maximum), ylim=(floor, 1), ylabel='Relative gain (dB)',
             xlabel='Azimuth (deg)' if circular else 'Cut angle (deg)')
    if vertical_labels:
        cart.set_xticks([0, 45, 90, 135, 180], [vertical_labels[0], '45°', 'Zenith', '45°', vertical_labels[1]])
    cart.grid(alpha=.3)
    if not points:
        cart.text(.5, .5, 'No observations in this cut', transform=cart.transAxes, ha='center')
    elif not curve:
        cart.text(.5, .95, 'Insufficient adjacent nodes for interpolation', transform=cart.transAxes, ha='center', va='top')
    handles, labels = polar.get_legend_handles_labels()
    if handles:
        fig.legend(handles, labels, loc='lower center', ncol=min(6, len(handles)), fontsize=9)
    fig.suptitle(f'{title}\nCombined estimate; individual signal peaks aligned to 0 dB', fontsize=13)
    fig.tight_layout(rect=(0, .09, 1, .89))
    fig.savefig(out / f'{name}.png', dpi=160)
    plt.close(fig)


def export_combined(patterns, out, cut_width=10, azimuth_cut=0, elevation_cut=45, max_gap_deg=30):
    # Deferred import lets the standalone command reuse the same cut definitions.
    """Export five combined cuts and a full 3D pattern from signal-group bins.

    Reuse the main module cut selectors, combine equal-angle bins by counts,
    and interpolate connected nodes without extrapolation. Each cut saves
    source points, weighted nodes, curve samples and a PNG; metadata records
    the assumptions and counts. Peak alignment is illustrative and does not
    estimate transmitter offsets or calibrate cross-frequency antenna gain.
    The separate 3D CSV and scatter plot merge equal azimuth/elevation bins
    across the full observed sky without interpolation or peak renormalization.
    """
    from gnss_antenna_pattern import principal_plane_cuts, directional_cuts
    os.environ.setdefault('MPLCONFIGDIR', '/tmp/gnss-antenna-matplotlib')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    if not patterns:
        raise ValueError('No patterns to combine')
    if not math.isfinite(cut_width) or not 0 < cut_width <= 180:
        raise ValueError('Cut width must be in (0, 180] degrees')
    if not math.isfinite(azimuth_cut) or not 0 <= azimuth_cut < 360 or not math.isfinite(elevation_cut) or not 0 <= elevation_cut <= 90:
        raise ValueError('Invalid cut angles')
    # Validate even if every cut is empty.
    interpolate_nodes([], max_gap_deg=max_gap_deg)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    cuts = principal_plane_cuts(patterns, cut_width)
    for name, selected in directional_cuts(patterns, azimuth_cut, elevation_cut, cut_width).items():
        cuts[name] = [dict(r, plane_angle_deg=r['azimuth_deg'] if name == 'azimuth_cut' else r['elevation_deg']) for r in selected]
    titles = {'horizontal_pattern': 'Horizontal cut: elevation 15 deg', 'vertical_ns_pattern': 'North - Zenith - South',
              'vertical_ew_pattern': 'East - Zenith - West', 'azimuth_cut': f'Azimuth cut: elevation {elevation_cut:g} deg',
              'elevation_cut': f'Elevation cut: azimuth {azimuth_cut:g} deg'}
    colors = {g: plt.get_cmap('tab10')(i % 10) for i, g in enumerate(sorted({r['signal_group'] for r in patterns}))}
    floor = 10 * math.floor(min(-30, min(float(r['relative_gain_db']) for r in patterns) - 3) / 10)
    metadata = dict(normalization='Reuse independent peak=0 dB normalization of each input constellation/signal group',
                    node_aggregation='Count-weighted mean of bin medians in dB at equal cut angles',
                    interpolation='PCHIP, no extrapolation; circular azimuth seam supported',
                    maximum_gap_deg=max_gap_deg, grid_step_deg=.5, cut_full_width_deg=cut_width,
                    fixed_azimuth_deg=azimuth_cut, fixed_elevation_deg=elevation_cut,
                    caveat='Illustrative combined response, not calibrated gain; differing signal frequencies and group peak coverage may bias the result',
                    groups=sorted(colors), cuts={})
    for name, selected in cuts.items():
        points = [dict(signal_group=r['signal_group'], plane_angle_deg=float(r['plane_angle_deg']),
                       azimuth_deg=float(r['azimuth_deg']), elevation_deg=float(r['elevation_deg']),
                       relative_gain_db=float(r['relative_gain_db']), count=int(r['count'])) for r in selected]
        nodes = merge_nodes(points)
        circular = name in ('horizontal_pattern', 'azimuth_cut')
        curve = interpolate_nodes(nodes, circular=circular, max_gap_deg=max_gap_deg)
        save_csv(out / f'{name}_points.csv', points, ['signal_group', 'plane_angle_deg', 'azimuth_deg', 'elevation_deg', 'relative_gain_db', 'count'])
        save_csv(out / f'{name}_nodes.csv', nodes, ['plane_angle_deg', 'relative_gain_db', 'sample_count', 'source_bin_count', 'source_groups', 'between_bin_std_db'])
        save_csv(out / f'{name}_curve.csv', curve, ['segment_id', 'plane_angle_deg', 'unwrapped_angle_deg', 'interpolated_gain_db'])
        labels = ('N', 'S') if name == 'vertical_ns_pattern' else (('E', 'W') if name == 'vertical_ew_pattern' else None)
        draw_combined(name, points, nodes, curve, out, colors, floor, titles[name], circular, labels)
        metadata['cuts'][name] = dict(source_bins=len(points), samples=sum(p['count'] for p in points),
                                    nodes=len(nodes), curve_points=len(curve), segments=len({r['segment_id'] for r in curve}))
    # Full 3D merging uses every direction bin, independent of cut widths.
    spatial = merge_spatial_nodes(patterns)
    save_csv(out / 'pattern_3d.csv', spatial,
             ['azimuth_deg', 'elevation_deg', 'theta_deg', 'phi_deg', 'relative_gain_db',
              'sample_count', 'source_bin_count', 'source_groups', 'between_bin_std_db'])
    draw_pattern_3d(spatial, out, 'Combined estimate; individual signal peaks aligned to 0 dB')
    metadata['pattern_3d'] = dict(bins=len(spatial), source_bins=len(patterns),
                                 samples=sum(r['sample_count'] for r in spatial),
                                 aggregation='Count-weighted dB mean at equal azimuth/elevation bin centres',
                                 interpolation='None; observed direction bins only',
                                 radius='Relative power: 10**(relative_gain_db/10)',
                                 csv='pattern_3d.csv', plot='pattern_3d.png')
    (out / 'metadata.json').write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf-8')
    return metadata


def main():
    """Rebuild combined plots from pattern.csv without repeating orbit calculations.

    Preserve signal_group as text and parse numeric columns as floats. Cut
    settings default independently of the original run, so callers must repeat
    any nondefault angles or width. Report invalid files or values as CLI errors.
    """
    parser = argparse.ArgumentParser(description='Generate combined cut plots from an existing signal-separated pattern.csv')
    parser.add_argument('--pattern-csv', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cut-width', type=float, default=10)
    parser.add_argument('--azimuth-cut', type=float, default=0)
    parser.add_argument('--elevation-cut', type=float, default=45)
    parser.add_argument('--max-gap', type=float, default=30)
    args = parser.parse_args()
    try:
        with args.pattern_csv.open() as stream:
            patterns = [{k: v if k == 'signal_group' else float(v) for k, v in r.items()} for r in csv.DictReader(stream)]
        meta = export_combined(patterns, args.output, args.cut_width, args.azimuth_cut, args.elevation_cut, args.max_gap)
        print(f'Combined {len(meta["groups"])} signal groups -> {args.output}')
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(2, f'Error: {exc}\n')


if __name__ == '__main__':
    main()
