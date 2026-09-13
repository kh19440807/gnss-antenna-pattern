"""Test NMEA/NAV parsing, BRDC cache safety, geometry and CLI artifacts.

Generated records and mocked downloads avoid external network dependencies.
Temporary directories isolate outputs, including empty cuts and metadata.
"""
import math
import gzip
import io
import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from datetime import date, datetime, timezone
from urllib.error import HTTPError, URLError
from pathlib import Path
import numpy as np
import gnss_antenna_pattern as gp


def nav_text(version=3):
    # Build a circular GPS orbit in fixed-width RINEX 2 or 3 format for deterministic parser tests.
    p = [0.0] * 28
    p[7] = math.sqrt(26560000)
    p[8] = 86400
    p[10] = gp.OMEGA * p[8]
    p[12] = .96
    p[18] = 2295
    first = 'G01 2024 01 01 00 00 00' if version == 3 else ' 1 24  1  1  0  0  0.0'
    first = first.ljust(23 if version == 3 else 22) + ''.join(f'{0:19.12E}' for _ in range(3))
    lines = [f'{version:9.2f}' + ' ' * 11 + 'N' + ' ' * 39 + 'RINEX VERSION / TYPE\n',
             ' ' * 60 + 'END OF HEADER\n', first + '\n']
    for i in range(7):
        lines.append(' ' * (4 if version == 3 else 3) + ''.join(f'{v:19.12E}' for v in p[i*4:i*4+4]) + '\n')
    return ''.join(lines)


class Tests(unittest.TestCase):
    def test_download_cache_and_refresh(self):
        # Mock compressed downloads to verify historical cache reuse and explicit refresh without network access.
        payload = gzip.compress(nav_text().encode())
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            day = date(2024, 1, 1)
            now = datetime(2024, 1, 4, tzinfo=timezone.utc)
            with patch.object(gp, 'urlopen', return_value=io.BytesIO(payload)) as download:
                nav, source = gp.fetch_bkg_day(day, cache, now=now)
                self.assertIn('G01', nav)
                self.assertTrue(source['url'].endswith('/2024/001/BRDC00WRD_S_20240010000_01D_MN.rnx.gz'))
                download.assert_called_once()
            with patch.object(gp, 'urlopen', side_effect=AssertionError('Unexpected network')):
                self.assertTrue(gp.fetch_bkg_day(day, cache, now=now)[1]['cached'])
            with patch.object(gp, 'urlopen', return_value=io.BytesIO(payload)) as download:
                gp.fetch_bkg_day(day, cache, refresh=True, now=now)
                download.assert_called_once()
            # A partial historical snapshot must be refreshed after the day has ended.
            old = datetime(2024, 1, 1, 12, tzinfo=timezone.utc).timestamp()
            os.utime(source['path'], (old, old))
            with patch.object(gp, 'urlopen', return_value=io.BytesIO(payload)) as download:
                gp.fetch_bkg_day(day, cache, now=now)
                download.assert_called_once()

    def test_current_day_and_failed_download(self):
        # A current-day cache must refresh; unsuccessful replacement must preserve the previously cached file.
        payload = gzip.compress(nav_text().encode())
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            now = datetime(2024, 1, 1, 12, tzinfo=timezone.utc)
            with patch.object(gp, 'urlopen', side_effect=lambda *a, **k: io.BytesIO(payload)) as download:
                _, source = gp.fetch_bkg_day(now.date(), cache, now=now)
                gp.fetch_bkg_day(now.date(), cache, now=now)
                self.assertEqual(download.call_count, 2)
            original = Path(source['path']).read_bytes()
            for failure in (io.BytesIO(b'not gzip'),):
                with patch.object(gp, 'urlopen', return_value=failure):
                    with self.assertRaises(OSError):
                        gp.fetch_bkg_day(now.date(), cache, now=now)
            self.assertEqual(Path(source['path']).read_bytes(), original)
            self.assertEqual(len(list(cache.iterdir())), 1)

    def test_automatic_dates_and_errors(self):
        # Include preceding UTC days and tolerate only optional-day 404 responses.
        samples = [(datetime(2024, 1, d, tzinfo=timezone.utc),) for d in (1, 2)]
        with patch.object(gp, 'fetch_bkg_day', return_value=({1: ['orbit']}, {})) as fetch:
            nav, sources = gp.automatic_nav(samples, Path('/tmp/unused'))
            self.assertEqual([call.args[0] for call in fetch.call_args_list],
                             [date(2023, 12, 31), date(2024, 1, 1), date(2024, 1, 2)])
            self.assertEqual(len(nav[1]), 3)
            self.assertEqual(len(sources), 3)
        missing = HTTPError('https://example.invalid', 404, 'Not found', {}, None)
        with patch.object(gp, 'fetch_bkg_day', side_effect=[missing, ({1: []}, {}), ({1: []}, {})]):
            self.assertTrue(gp.automatic_nav(samples, Path('/tmp/unused'))[1][0]['unavailable'])
        for error in (missing, URLError('offline')):
            with patch.object(gp, 'fetch_bkg_day', side_effect=[({1: []}, {}), error]):
                with self.assertRaisesRegex(ValueError, 'BKG BRDC 2024-01-01'):
                    gp.automatic_nav(samples, Path('/tmp/unused'))

    def test_rinex305_glonass_alignment(self):
        # Consume the extra GLONASS record line so the following satellite starts at the correct column.
        text = nav_text().replace('     3.00', '     3.05', 1)
        lines = text.splitlines(keepends=True)
        state = [25000, 0, 0, 0, 0, 3, 0, -7, 0, 1, 0, 0, 0, 0, 0, 0]
        lines[2:2] = ['R01 2024 01 01 00 00 00\n'] + [
            '    ' + ''.join(f'{v:19.12E}' for v in state[i:i + 4]) + '\n' for i in range(0, 16, 4)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'mixed.rnx'
            path.write_text(''.join(lines))
            self.assertEqual(len(gp.read_nav(path)['G01']), 1)

    def test_checksum(self):
        # Accept valid sentence checksums and reject altered payloads.
        self.assertEqual(gp.nmea_fields('$GPGLL,4916.45,N,12311.12,W,225444,A,*1D')[0], 'GPGLL')
        with self.assertRaises(ValueError):
            gp.nmea_fields('$GPGLL,4916.45,N,12311.12,W,225444,A,*00')

    def test_midnight(self):
        # Associate GSV across midnight while ensuring invalid fixes prevent stale-position reuse.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'data.nmea'
            path.write_text('$GPRMC,235959,A,0000,N,00000,E,0,0,010124,,,A\n'
                            '$GPGSV,1,1,01,01,80,000,45\n'
                            '$GPGGA,000001,0000,N,00000,E,1,10,1,10,M,20,M,,\n'
                            '$GPGSV,1,1,01,01,80,000,46\n'
                            '$GPRMC,000002,V,0000,N,00000,E,0,0,020124,,,A\n'
                            '$GPGSV,1,1,01,01,80,000,47\n')
            samples, _ = gp.read_nmea(path)
            self.assertEqual(len(samples), 2)
            self.assertEqual((samples[1][0] - samples[0][0]).total_seconds(), 2)
            self.assertEqual(samples[1][1][2], 30)

    def test_split_nmea(self):
        # Rotation between sentences must preserve the date, fix, height and
        # duplicate set, including a midnight rollover in the next gzip file.
        first = ('$GPRMC,235959,A,0000,N,00000,E,0,0,010124,,,A\n'
                 '$GPGGA,235959,0000,N,00000,E,1,10,1,10,M,20,M,,\n')
        second = ('$GPGSV,1,1,01,01,80,000,45\n'
                  '$GPGSV,1,1,01,01,80,000,46\n'
                  '$GPGGA,000001,0000,N,00000,E,1,10,1,,,M,,M,,\n'
                  '$GPGSV,1,1,01,01,80,000,47\n')
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            a, b, merged = (folder / n for n in ('part1.log', 'part2.log.gz', 'all.nmea'))
            a.write_text(first)
            b.write_bytes(gzip.compress(second.encode('ascii')))
            merged.write_text(first + second)
            samples, stats = gp.read_nmea([a, b, b])
            expected, expected_stats = gp.read_nmea(merged)
            self.assertEqual(samples, expected)
            self.assertEqual(stats, expected_stats)
            self.assertEqual(len(samples), 2)
            self.assertEqual(samples[0][4], 45)
            self.assertEqual(samples[1][0].day, 2)
            self.assertEqual(samples[1][1][2], 30)
            with self.assertRaises(OSError):
                gp.read_nmea([a, folder / 'missing.log'])

    def test_binary_contamination(self):
        # Bad bytes must neither abort buffered decoding nor silently change a
        # C/N0 field. Require a new fix after contamination, even across files.
        fix = b'$GPRMC,000000,A,0000,N,00000,E,0,0,010124,,,A\n'
        good = b'$GPGSV,1,1,01,01,80,000,45\n'
        damaged = b'$GPGSV,1,1,01,02,80,000,4\xb55\n'
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for suffix in ('.log', '.log.gz'):
                with self.subTest(suffix=suffix):
                    first, second = folder / ('first' + suffix), folder / ('second' + suffix)
                    payloads = [fix + good + damaged + b'\xb5\x62\xff\n',
                                good.replace(b'01,80', b'03,80') +
                                fix.replace(b'000000,A', b'000001,A') + good + b'\xff']
                    for path, payload in zip((first, second), payloads):
                        path.write_bytes(gzip.compress(payload) if suffix.endswith('.gz') else payload)
                    samples, stats = gp.read_nmea([first, second])
                    self.assertEqual(len(samples), 2)
                    self.assertEqual([r[4] for r in samples], [45, 45])
                    self.assertEqual(stats['non_ascii_line'], 3)
                    self.assertEqual(stats['gsv_without_fix'], 1)
            nav = folder / 'bad.nav'
            nav.write_bytes(nav_text().encode('ascii') + b'\xb5\n')
            with self.assertRaises(UnicodeDecodeError):
                gp.read_nav(nav)

    def test_orbit(self):
        # Check a synthetic circular position and reception geometry against known geometric expectations.
        with tempfile.TemporaryDirectory() as tmp:
            for version in (2, 3):
                path = Path(tmp) / 'orbit.nav'
                path.write_text(nav_text(version))
                eph = gp.read_nav(path)['G01'][0]
                np.testing.assert_allclose(gp.satellite_ecef(eph, eph.toe), [26560000, 0, 0], atol=.001)
                distance, _, elevation = gp.geometry(eph, eph.toe, (0, 0, 0))
                self.assertAlmostEqual(distance, 26560000 - 6378137, delta=1)
                self.assertGreater(elevation, 89.99)
                self.assertAlmostEqual(eph.toe, eph.toc)

    def test_bins(self):
        # Verify directional binning and zero-peak normalization of median residuals.
        rows = [dict(theta_deg=20, phi_deg=359, gain_residual_db=x) for x in (1, 3)]
        rows.append(dict(theta_deg=30, phi_deg=0, gain_residual_db=5))
        bins = gp.aggregate(rows, 5)
        self.assertEqual(bins[0]['count'], 2)
        self.assertEqual(bins[0]['relative_gain_db'], -3)
        self.assertEqual(bins[1]['relative_gain_db'], 0)
        self.assertEqual(bins[0]['azimuth_deg'], bins[0]['phi_deg'])
        self.assertEqual(bins[0]['elevation_deg'], 90 - bins[0]['theta_deg'])

    def test_directional_cuts(self):
        # Check fixed-angle selection, including circular azimuth separation near north.
        rows = [dict(azimuth_deg=a, elevation_deg=e) for a, e in
                ((357.5, 42.5), (2.5, 47.5), (180, 45), (20, 10))]
        cuts = gp.directional_cuts(rows, 0, 45, 10)
        self.assertEqual([r['azimuth_deg'] for r in cuts['azimuth_cut']], [2.5, 180, 357.5])
        self.assertEqual([r['elevation_deg'] for r in cuts['elevation_cut']], [42.5, 47.5])
        self.assertEqual(gp.directional_cuts(rows, 90, 80, 10), {'azimuth_cut': [], 'elevation_cut': []})

    def test_principal_planes(self):
        # Check the 15-degree band and both sides of each vertical plane with correct plotting angles.
        rows = [dict(azimuth_deg=a, elevation_deg=e) for a, e in
                ((357.5, 30), (182.5, 40), (92.5, 50), (267.5, 60), (45, 15), (45, 30))]
        planes = gp.principal_plane_cuts(rows, 10)
        self.assertEqual([r['plane_angle_deg'] for r in planes['horizontal_pattern']], [45])
        self.assertEqual([(r['side'], r['plane_angle_deg']) for r in planes['vertical_ns_pattern']],
                         [('N', 30), ('S', 140)])
        self.assertEqual([(r['side'], r['plane_angle_deg']) for r in planes['vertical_ew_pattern']],
                         [('E', 50), ('W', 120)])
        self.assertEqual(gp.principal_plane_cuts([rows[-1]], 10),
                         {'horizontal_pattern': [], 'vertical_ns_pattern': [], 'vertical_ew_pattern': []})

    def test_cli(self):
        # Run the CLI on temporary synthetic files and inspect gas corrections, cut artifacts and combined-output options.
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / 'orbit.nav').write_text(nav_text())
            (folder / 'data.nmea').write_text('$GPRMC,000000,A,0000,N,00000,E,0,0,010124,,,A\n'
                                             '$GPGSV,1,1,01,01,90,000,45\n')
            # Exercise multiple paths and repeated options with a boundary before GSV.
            lines = (folder / 'data.nmea').read_text().splitlines(keepends=True)
            (folder / 'part1.log').write_text(lines[0])
            (folder / 'part2.log').write_text(lines[1])
            result = subprocess.run([sys.executable, str(Path(gp.__file__)), '--nmea',
                                     str(folder / 'part1.log'), str(folder / 'part2.log'),
                                     '--nmea', str(folder / 'part2.log'),
                                     '--nav', str(folder / 'orbit.nav'), '--output', str(folder / 'out'),
                                     '--reference-cn0', '45', '--gps-utc-offset', '18'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            for name in ('samples.csv', 'pattern.csv', 'azimuth_cut.csv', 'elevation_cut.csv',
                         'horizontal_pattern.csv', 'horizontal_pattern.png',
                         'vertical_ns_pattern.csv', 'vertical_ns_pattern.png',
                         'vertical_ew_pattern.csv', 'vertical_ew_pattern.png',
                         'azimuth_cut.png', 'elevation_cut.png', 'theta_cut.png', 'phi_cut.png', 'pattern_3d.png', 'metadata.json'):
                self.assertGreater((folder / 'out' / name).stat().st_size, 0)
            with (folder / 'out' / 'samples.csv').open() as stream:
                corrected = next(csv.DictReader(stream))
            gas = float(corrected['gas_attenuation_db'])
            self.assertGreater(gas, 0)
            self.assertAlmostEqual(float(corrected['vacuum_model_cn0_dbhz']) - float(corrected['model_cn0_dbhz']), gas)
            metadata = json.loads((folder / 'out' / 'metadata.json').read_text())
            self.assertEqual(metadata['arguments']['nmea'],
                             [str(folder / n) for n in ('part1.log', 'part2.log', 'part2.log')])
            self.assertEqual(metadata['samples'], 1)
            self.assertTrue(metadata['gas_model']['enabled'])
            self.assertIsNotNone(metadata['combined'])
            for cut in ('horizontal_pattern', 'vertical_ns_pattern', 'vertical_ew_pattern'):
                self.assertTrue((folder / 'out' / 'combined' / (cut + '.png')).is_file())
                self.assertTrue((folder / 'out' / 'combined' / (cut + '_curve.csv')).is_file())
            self.assertEqual(metadata['principal_planes']['horizontal_elevation_deg'], 15)
            self.assertEqual(metadata['principal_planes']['vertical_ns_azimuths_deg'], [0, 180])
            self.assertEqual(metadata['principal_planes']['vertical_ew_azimuths_deg'], [90, 270])
            with (folder / 'out' / 'horizontal_pattern.csv').open() as stream:
                self.assertEqual(list(csv.DictReader(stream)), [])
            self.assertEqual(metadata['cuts']['fixed_azimuth_deg'], 0)
            self.assertEqual(metadata['cuts']['fixed_elevation_deg'], 45)
            # Zenith-only fixture has no data in the default elevation 45-degree cut.
            with (folder / 'out' / 'azimuth_cut.csv').open() as stream:
                reader = csv.DictReader(stream)
                self.assertIn('azimuth_deg', reader.fieldnames)
                self.assertEqual(list(reader), [])
            # Disabling the correction reproduces the earlier C/N0 model exactly.
            result = subprocess.run([sys.executable, str(Path(gp.__file__)), '--nmea', str(folder / 'data.nmea'),
                                     '--nav', str(folder / 'orbit.nav'), '--output', str(folder / 'disabled'),
                                     '--reference-cn0', '45', '--gps-utc-offset', '18', '--gas-model', 'none', '--no-combined'],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((folder / 'disabled' / 'combined').exists())
            with (folder / 'disabled' / 'samples.csv').open() as stream:
                uncorrected = next(csv.DictReader(stream))
            self.assertEqual(float(uncorrected['gas_attenuation_db']), 0)
            self.assertAlmostEqual(float(corrected['gain_residual_db']) - float(uncorrected['gain_residual_db']), gas)
