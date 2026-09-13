"""Exercise five-constellation parsing, timing, propagation and grouping.

Synthetic broadcast records make epoch offsets and orbit geometry explicit.
An independent adaptive solver checks GLONASS RK4 propagation; these tests
do not establish positioning accuracy against precise orbit products.
"""
import csv
import io
import json
import math
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
from scipy.integrate import solve_ivp

import gnss_antenna_pattern as gp
from gnss_signals import satellite_id, frequency_mhz, native_time, healthy


def orbital_values(system, second=18):
    # Build Kepler fields with known radius and node orientation, keeping each native epoch explicit.
    p = [0.] * 28
    p[7] = math.sqrt(26560000)
    p[8] = 86400 + second
    p[10] = (7.292115e-5 if system == 'C' else gp.OMEGA) * p[8]
    p[12] = .96
    p[17] = 1 if system == 'E' else 0
    return p


def mixed_nav():
    # Encode all five systems at the same UTC instant, including the extra RINEX 3.05 GLONASS line.
    lines = [f'{3.05:9.2f}' + ' ' * 11 + 'N' + ' ' * 39 + 'RINEX VERSION / TYPE\n',
             ' ' * 60 + 'END OF HEADER\n']
    for system in 'GRECJ':
        sec = 0 if system == 'R' else (4 if system == 'C' else 18)
        prn = 6 if system == 'C' else 1
        lines.append(f'{system}{prn:02d} 2024 01 01 00 00 {sec:02d}' + ''.join(f'{0:19.12E}' for _ in range(3)) + '\n')
        p = [25500, 0, 0, 0, 0, 3, 0, -7, 0, 1, 0, 0, 0, 0, 0, 0] if system == 'R' else orbital_values(system, sec)
        for i in range(0, len(p), 4):
            lines.append('    ' + ''.join(' ' * 19 if system == 'E' and j == 19 else f'{p[j]:19.12E}'
                                         for j in range(i, i + 4)) + '\n')
    return ''.join(lines)


class MultiGNSSTests(unittest.TestCase):
    def test_satellite_numbers(self):
        # Check local and offset numbering so constellations cannot silently share satellite identities.
        for talker, raw, expected in [('GP', 1, 'G01'), ('GP', 193, 'J01'), ('GL', 65, 'R01'),
                                      ('GL', 1, 'R01'), ('GA', 301, 'E01'), ('GA', 1, 'E01'),
                                      ('GB', 201, 'C01'), ('BD', 101, 'C01'), ('GB', 1, 'C01'),
                                      ('GQ', 193, 'J01'), ('GQ', 1, 'J01')]:
            self.assertEqual(satellite_id(talker, raw), expected)
        self.assertIsNone(satellite_id('GP', 33))
        self.assertIsNone(satellite_id('GN', 1))
        self.assertIsNone(satellite_id('GL', 0))

    def test_native_times(self):
        # The same UTC instant must acquire the correct GPST, BDT or UTC calendar offset.
        utc = datetime(2024, 1, 1, tzinfo=timezone.utc)
        t = (utc - gp.GPS_EPOCH).total_seconds()
        for system, delta in [('G', 18), ('E', 18), ('J', 18), ('C', 4), ('R', 0)]:
            self.assertEqual(native_time(utc, gp.GPS_EPOCH, system, 18), t + delta)

    def test_all_orbits_and_week_rollover(self):
        # Synthetic circular orbits give known positions and expose incorrect toe week unwrapping.
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'mixed.nav'
            p.write_text(mixed_nav())
            nav = gp.read_nav(p)
        self.assertEqual(set(nav), {'G01', 'R01', 'E01', 'C06', 'J01'})
        for sat, records in nav.items():
            e = records[0]
            expected = [25500000 if sat[0] == 'R' else 26560000, 0, 0]
            np.testing.assert_allclose(gp.satellite_ecef(e, e.toe), expected, atol=.001)
        for system in 'GECJ':
            p = orbital_values(system)
            p[8] = 604790
            eph = gp.Ephemeris(1, 604810, p, system)
            self.assertEqual(eph.toe, 604790)

    def test_beidou_geo_tilt(self):
        # A low-inclination GEO fixture isolates the extra tilted-frame transformation.
        p = orbital_values('C', 0)
        p[7] = math.sqrt(42164000)
        p[12] = .02
        p[3] = math.pi / 2
        e = gp.Ephemeris(1, 86400, p, 'C')
        tilt = p[12] + math.radians(5)
        np.testing.assert_allclose(gp.satellite_ecef(e, e.toe),
                                   [0, 42164000 * math.cos(tilt), 42164000 * math.sin(tilt)], atol=.001)

    def test_glonass_integration(self):
        # Compare fixed-step RK4 with adaptive DOP853 to detect propagation and step-direction errors.
        p = [15000, -1, 1e-6, 0, 18000, 2, -2e-6, 6, 10000, 2.4, 1e-6, 0]
        e = gp.Ephemeris(1, 1000, p, 'R')
        state = np.array([15000, 18000, 10000, -1, 2, 2.4]) * 1000
        acceleration = np.array([1e-6, -2e-6, 1e-6]) * 1000
        for dt in (-1800, 0, 1800):
            if dt:
                ref = solve_ivp(lambda t, y: gp.glonass_derivative(y, acceleration), (0, dt), state,
                                method='DOP853', rtol=1e-12, atol=1e-6).y[:3, -1]
            else:
                ref = state[:3]
            np.testing.assert_allclose(gp.glonass_ecef(e, e.toc + dt), ref, atol=.1, rtol=0)

    def test_frequencies_and_galileo_health(self):
        # Exercise band mapping, FDMA channels and Galileo source/health masks independently.
        e = gp.Ephemeris(1, 0, orbital_values('E'), 'E')
        self.assertTrue(healthy(e, '7'))
        self.assertFalse(healthy(e, '1'))  # Only I/NAV present.
        e.values[21] = 8  # E5a invalid does not mark E1 invalid.
        self.assertTrue(healthy(e, '7'))
        e.values[21] = 1
        self.assertFalse(healthy(e, '7'))
        for sys, sid, expected in [('G', '1', 1575.42), ('E', '7', 1575.42), ('E', '1', 1176.45),
                                    ('C', '1', 1561.098), ('C', 'B', 1207.14), ('J', '7', 1176.45)]:
            self.assertEqual(frequency_mhz(sys, sid, e), expected)
        e.values[7] = -7
        self.assertEqual(frequency_mhz('R', '1', e), 1598.0625)
        e.values[7] = 6
        self.assertEqual(frequency_mhz('R', '3', e), 1248.625)
        with self.assertRaises(ValueError):
            frequency_mhz('C', '0', e)
        self.assertEqual(frequency_mhz('C', '0', e, {'C:0': 1561.098}), 1561.098)

    def test_multisystem_end_to_end(self):
        # Run five-system processing with plotting mocked, then inspect separate group files and metadata.
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / 'mixed.nav').write_text(mixed_nav())
            (folder / 'input.nmea').write_text('$GNRMC,000000,A,0000,N,00000,E,0,0,010124,,,A\n'
                '$GPGSV,1,1,1,01,90,000,40,1\n$GLGSV,1,1,1,65,90,000,42,1\n'
                '$GAGSV,1,1,1,01,90,000,44,7\n$GBGSV,1,1,1,06,90,000,46,1\n'
                '$GQGSV,1,1,1,193,90,000,48,1\n')
            samples, _ = gp.read_nmea(folder / 'input.nmea')
            self.assertEqual({s[2] for s in samples}, {'G01', 'R01', 'E01', 'C06', 'J01'})
            selected, _ = gp.read_nmea(folder / 'input.nmea', systems='EJ')
            self.assertEqual({s[2] for s in selected}, {'E01', 'J01'})
            argv = ['prog', '--nmea', str(folder / 'input.nmea'), '--nav', str(folder / 'mixed.nav'),
                    '--reference-cn0', '45', '--gps-utc-offset', '18', '--gas-model', 'none',
                    '--output', str(folder / 'out'), '--no-progress']
            with patch('sys.argv', argv), patch.object(gp, 'plot_pattern') as plot, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                gp.main()
                self.assertEqual(plot.call_count, 5)
            metadata = json.loads((folder / 'out' / 'metadata.json').read_text())
            self.assertEqual(set(metadata['groups']), {'G_1', 'R_1', 'E_7', 'C_1', 'J_1'})
            with (folder / 'out' / 'pattern.csv').open() as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 5)
            # Different measured levels must be independently normalized, never pooled.
            self.assertTrue(all(float(row['relative_gain_db']) == 0 for row in rows))
            for group in metadata['groups']:
                self.assertTrue((folder / 'out' / group / 'horizontal_pattern.csv').is_file())
                self.assertTrue((folder / 'out' / group / 'metadata.json').is_file())
