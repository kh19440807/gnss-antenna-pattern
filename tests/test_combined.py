"""Verify count-weighted merging and coverage-preserving interpolation.

Synthetic angles exercise bounds, large gaps, isolated nodes and the
azimuth seam without needing satellite data or a plotting backend.
"""
import unittest

import numpy as np

from combined_patterns import merge_nodes, merge_spatial_nodes, interpolate_nodes


class CombinedTests(unittest.TestCase):
    def test_spatial_merge(self):
        # Equal azimuth alone must not collapse distinct elevation bins.
        # North's circular seam is equivalent, and all five systems contribute.
        rows = [dict(signal_group=g, azimuth_deg=az, elevation_deg=el,
                     relative_gain_db=gain, count=count)
                for g, az, el, gain, count in [
                    ('G_1', 0, 15, 0, 1), ('E_7', 360, 15, -20, 3),
                    ('R_1', 0, 45, -5, 2), ('C_1', 90, 15, -10, 4),
                    ('J_1', 0, 45, -15, 2)]]
        nodes = merge_spatial_nodes(rows)
        self.assertEqual(len(nodes), 3)
        by_direction = {(n['azimuth_deg'], n['elevation_deg']): n for n in nodes}
        self.assertEqual(by_direction[0, 15]['relative_gain_db'], -15)
        self.assertEqual(by_direction[0, 15]['source_groups'], 'E_7;G_1')
        self.assertEqual(by_direction[0, 45]['relative_gain_db'], -10)
        self.assertEqual(sum(n['sample_count'] for n in nodes), 12)
        self.assertEqual(max(n['relative_gain_db'] for n in nodes), -10)
        self.assertEqual(by_direction[0, 45]['theta_deg'], 45)
        with self.assertRaises(ValueError):
            merge_spatial_nodes([dict(rows[0], elevation_deg=float('nan'))])

    def test_weighted_merge(self):
        # Unequal sample counts distinguish a weighted dB mean from equal-group averaging.
        points = [dict(signal_group=g, plane_angle_deg=15, relative_gain_db=y, count=n)
                  for g, y, n in [('G_1', 0, 1), ('E_7', -20, 3)]]
        nodes = merge_nodes(points)
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]['relative_gain_db'], -15)
        self.assertEqual(nodes[0]['sample_count'], 4)
        self.assertEqual(nodes[0]['source_groups'], 'E_7;G_1')

    def test_shape_preservation_and_no_extrapolation(self):
        # A sharp interior peak must stay bounded and reproduce every observed node.
        nodes = [dict(plane_angle_deg=x, relative_gain_db=y) for x, y in [(10, -20), (20, -2), (30, -15)]]
        curve = interpolate_nodes(nodes)
        self.assertEqual(curve[0]['plane_angle_deg'], 10)
        self.assertEqual(curve[-1]['plane_angle_deg'], 30)
        self.assertTrue(all(-20 <= r['interpolated_gain_db'] <= -2 for r in curve))
        for node in nodes:
            result = next(r for r in curve if r['plane_angle_deg'] == node['plane_angle_deg'])
            self.assertAlmostEqual(result['interpolated_gain_db'], node['relative_gain_db'])

    def test_large_gap_and_singletons(self):
        # Two observed arcs must stay disconnected; an isolated node cannot define a curve.
        nodes = [dict(plane_angle_deg=x, relative_gain_db=-10) for x in (10, 20, 100, 110, 180)]
        curve = interpolate_nodes(nodes, max_gap_deg=30)
        self.assertEqual(len({r['segment_id'] for r in curve}), 2)
        self.assertTrue(all(10 <= r['plane_angle_deg'] <= 20 or 100 <= r['plane_angle_deg'] <= 110 for r in curve))
        self.assertEqual(interpolate_nodes(nodes[:1]), [])
        self.assertEqual(interpolate_nodes([]), [])

    def test_azimuth_wrap(self):
        # Observations straddling north should form one short arc, leaving the opposite sky empty.
        nodes = [dict(plane_angle_deg=x, relative_gain_db=-10) for x in (5, 15, 345, 355)]
        curve = interpolate_nodes(nodes, circular=True)
        self.assertEqual(len({r['segment_id'] for r in curve}), 1)
        self.assertTrue(all(r['plane_angle_deg'] >= 345 or r['plane_angle_deg'] <= 15 for r in curve))
        self.assertTrue(any(r['unwrapped_angle_deg'] > 360 for r in curve))
        self.assertTrue(any(r['plane_angle_deg'] == 0 for r in curve))

    def test_closed_azimuth_curve(self):
        # Dense full-circle coverage permits closure with equal seam values.
        nodes = [dict(plane_angle_deg=x, relative_gain_db=-10 + np.cos(np.radians(x))) for x in range(0, 360, 15)]
        curve = interpolate_nodes(nodes, circular=True)
        self.assertEqual(curve[0]['plane_angle_deg'], curve[-1]['plane_angle_deg'])
        self.assertAlmostEqual(curve[0]['interpolated_gain_db'], curve[-1]['interpolated_gain_db'])
        self.assertEqual(curve[-1]['unwrapped_angle_deg'], 360)

    def test_invalid_input(self):
        # Reject invalid interpolation settings and non-finite input gains before rendering.
        with self.assertRaises(ValueError):
            interpolate_nodes([], max_gap_deg=0)
        with self.assertRaises(ValueError):
            merge_nodes([dict(signal_group='G_1', plane_angle_deg=15, relative_gain_db=float('nan'), count=1)])
