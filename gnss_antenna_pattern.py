#!/usr/bin/env python3
"""Estimate signal-separated GNSS antenna patterns from NMEA and RINEX NAV."""
import argparse
import csv
import gzip
import json
import math
import os
import shutil
import tempfile
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

import numpy as np

from atmosphere import StandardGasModel
from combined_patterns import export_combined, draw_pattern_3d
from gnss_signals import SYSTEM_NAMES, TALKERS, satellite_id, frequency_mhz, native_time, healthy, parse_signal_values

# Calendar origin for elapsed seconds; native constellation offsets are applied separately.
# Orbital constants below use SI units: m^3/s^2, rad/s and m/s.
GPS_EPOCH = datetime(1980, 1, 6, tzinfo=timezone.utc)
MU = 3.986005e14
OMEGA = 7.2921151467e-5
C = 299792458.0
BKG_BRDC = 'https://igs.bkg.bund.de/root_ftp/IGS/BRDC'


def progress_samples(samples, enabled=True):
    """Yield observations while reporting completed work to standard error.

    Counting resumes after each yield, so samples rejected by the caller still
    advance the bar. A monotonic clock avoids wall-clock adjustments affecting
    the ETA. The final update also runs on interruption, retaining the actual
    completed count; aggregation and plotting are outside this progress total.
    """
    if not enabled:
        yield from samples
        return
    total = len(samples)
    done = 0
    start = last_update = time.monotonic()
    interactive = sys.stderr.isatty()

    def draw(final=False):
        elapsed = time.monotonic() - start
        fraction = done / total if total else 1
        filled = int(25 * fraction)
        rate = done / elapsed if elapsed > 0 else 0
        eta = f'{(total - done) / rate:.0f}s' if rate > 0 else '--'
        line = (f'Calculating [{"#" * filled}{"-" * (25 - filled)}] '
                f'{fraction:6.1%} {done}/{total} samples | elapsed {elapsed:.0f}s | ETA {eta}')
        print(('\r' if interactive else '') + line + (' ' * 8 if interactive else ''),
              end='\n' if final or not interactive else '', file=sys.stderr, flush=True)

    draw()
    try:
        for sample in samples:
            yield sample
            done += 1
            now = time.monotonic()
            # Redirected logs get sparse updates rather than carriage returns.
            if done < total and now - last_update >= (0.2 if interactive else 10):
                draw()
                last_update = now
    finally:
        draw(final=True)


def fetch_bkg_day(day, cache_dir, refresh=False, now=None):
    """Fetch one UTC daily BRDC product and return (ephemerides, source metadata).

    Recent products may still be growing. Reuse a cached file only when its
    modification time indicates acquisition at least two UTC midnights after
    the product date. Parse a temporary download before atomic replacement so
    a network or parsing failure cannot overwrite an existing cache. The
    optional now argument makes cache timing deterministic in tests.
    """
    now = now or datetime.now(timezone.utc)
    name = f'BRDC00WRD_S_{day:%Y%j}0000_01D_MN.rnx.gz'
    url = f'{BKG_BRDC}/{day:%Y}/{day:%j}/{name}'
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / name
    # Never freeze a snapshot downloaded while that day's product was updating.
    final_after = datetime.combine(day + timedelta(days=2), datetime.min.time(), timezone.utc)
    if target.exists() and not refresh and target.stat().st_mtime >= final_after.timestamp():
        try:
            records = read_nav(target)
            return records, dict(path=str(target), url=url, cached=True)
        except (ValueError, OSError, EOFError):
            pass  # A corrupt cache is replaced only after a successful download.
    print(f'Downloading BKG BRDC: {day} ...', file=sys.stderr)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=cache_dir, suffix='.rnx.gz', delete=False) as stream:
            temporary = Path(stream.name)
            with urlopen(url, timeout=30) as response:
                shutil.copyfileobj(response, stream)
        records = read_nav(temporary)
        os.replace(temporary, target)
        os.utime(target, (now.timestamp(), now.timestamp()))
        return records, dict(path=str(target), url=url, cached=False)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def automatic_nav(samples, cache_dir, refresh=False):
    """Merge navigation records for every observation day and its previous day.

    The previous day supplies ephemerides near midnight. Only an HTTP 404 for
    a day containing no observations is optional; required-day failures and
    other download errors propagate with guidance to supply a local NAV file.
    Return the satellite-indexed records and an audit trail of source files.
    """
    days = {sample[0].date() for sample in samples}
    # Previous-day ephemerides cover observations near midnight, including year boundaries.
    requested = days | {day - timedelta(days=1) for day in days}
    records, sources = defaultdict(list), []
    for day in sorted(requested):
        try:
            daily, source = fetch_bkg_day(day, cache_dir, refresh)
        except HTTPError as exc:
            if exc.code == 404 and day not in days:
                print(f'BKG: optional previous day {day} unavailable (404)', file=sys.stderr)
                sources.append(dict(date=str(day), unavailable=True, http_status=404))
                continue
            raise ValueError(f'BKG BRDC {day}: HTTP {exc.code}; product may not be available. '
                             'Retry later or supply --nav.') from exc
        except (URLError, OSError, ValueError, EOFError) as exc:
            raise ValueError(f'BKG BRDC {day}: {exc}. Retry or supply --nav.') from exc
        source['date'] = str(day)
        sources.append(source)
        for prn, ephemerides in daily.items():
            records[prn].extend(ephemerides)
    return records, sources


def open_text(path, errors='strict'):
    """Open plain or gzip-compressed ASCII text with a chosen decoding policy.

    Compression is selected by the .gz suffix. Strict decoding exposes corrupt
    or incompatible input instead of silently changing fixed-width fields.
    NMEA callers use replacement markers to detect and skip contaminated lines;
    navigation files retain the strict default.
    """
    return (gzip.open if str(path).endswith('.gz') else open)(
        path, 'rt', encoding='ascii', errors=errors)


def coordinate(value, hemisphere):
    """Convert NMEA degrees-and-minutes notation to signed decimal degrees.

    The integer part before the final two digits is degrees; the remaining
    minutes are divided by 60. South and west coordinates receive a minus sign.
    """
    x = float(value)
    result = int(x / 100) + (x % 100) / 60
    return -result if hemisphere in ('S', 'W') else result


def nmea_fields(line):
    """Extract comma-separated fields after the first dollar sign in a log line.

    Logger prefixes are ignored. When a checksum is present, XOR the sentence
    characters between $ and * and compare against its hexadecimal value.
    Return None for non-NMEA lines and raise ValueError for invalid checksums.
    """
    if '$' not in line:
        return None
    sentence = line[line.index('$') + 1:].strip()
    if '*' in sentence:
        sentence, checksum = sentence.split('*', 1)
        actual = 0
        for char in sentence:
            actual ^= ord(char)
        if actual != int(checksum[:2], 16):
            raise ValueError('NMEA checksum mismatch')
    return sentence.split(',')


def read_nmea(path, signal_id=None, default_height=0.0, systems='GRECJ'):
    """Associate GSV C/N0 measurements with the preceding valid dated position.

    Return sample tuples (UTC datetime, (latitude_deg, longitude_deg,
    ellipsoidal_height_m), RINEX satellite ID, signal ID, C/N0_dBHz) and
    rejection counters. RMC establishes the date; GGA can update time and
    height, including a midnight rollover inferred from a backward clock jump.
    An invalid fix clears the position association. GSV has no own timestamp,
    so this assumes receiver sentences remain in acquisition order. Repeated
    (epoch, satellite, signal) entries are retained only once.

    path accepts one path or an iterable of paths in acquisition order.
    Receiver state and duplicate detection span file boundaries so rotated
    PyGPSClient logs behave like one continuous stream of complete sentences.
    Files are opened one at a time; no intermediate concatenated file is made.
    """
    samples, stats = [], Counter()
    date = None
    epoch = None
    position = None
    height = default_height
    previous_time = None
    seen = set()
    paths = [path] if isinstance(path, (str, os.PathLike)) else path
    for source in paths:
        with open_text(source, errors='replace') as stream:
            for line in stream:
                # Never delete bad bytes inside a numeric field: that could turn
                # a damaged measurement into a different, apparently valid value.
                # Skip the entire contaminated line and require a fresh fix,
                # because that line might have contained a time/position update.
                if '\ufffd' in line:
                    stats['non_ascii_line'] += 1
                    epoch, position = None, None
                    continue
                try:
                    f = nmea_fields(line)
                    if not f:
                        continue
                    kind = f[0][-3:]
                    if kind not in ('RMC', 'GGA', 'GSV'):
                        continue
                    if kind in ('RMC', 'GGA'):
                        epoch, position = None, None
                        valid = f[2] == 'A' if kind == 'RMC' else int(f[6] or 0) > 0
                        if not valid:
                            stats['invalid_fix'] += 1
                            continue
                        if kind == 'RMC':
                            date = datetime.strptime(f[9], '%d%m%y').replace(tzinfo=timezone.utc)
                        if date is None:
                            stats['missing_date'] += 1
                            continue
                        seconds = int(f[1][:2]) * 3600 + int(f[1][2:4]) * 60 + float(f[1][4:])
                        if kind == 'GGA' and previous_time is not None and seconds < previous_time - 43200:
                            date += timedelta(days=1)
                        previous_time = seconds
                        if kind == 'GGA':
                            if f[9] and f[11]:
                                height = float(f[9]) + float(f[11])
                            lat, lon = coordinate(f[2], f[3]), coordinate(f[4], f[5])
                        else:
                            lat, lon = coordinate(f[3], f[4]), coordinate(f[5], f[6])
                        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                            raise ValueError('Invalid position')
                        epoch = date.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(seconds=seconds)
                        position = (lat, lon, height)
                    elif f[0][:2] in TALKERS:
                        if epoch is None or position is None:
                            stats['gsv_without_fix'] += 1
                            continue
                        fields = f[4:]
                        sid = fields[-1].upper() if len(fields) % 4 == 1 else ''
                        if sid and (len(sid) != 1 or sid not in '0123456789ABCDEF'):
                            raise ValueError('Invalid signal ID')
                        if len(fields) % 4 not in (0, 1):
                            raise ValueError('Invalid GSV field count')
                        if signal_id is not None and sid != signal_id:
                            stats['other_signal'] += 1
                            continue
                        for offset in range(0, len(fields) - 3, 4):
                            prn, _, _, cn0 = fields[offset:offset + 4]
                            if not prn or not cn0:
                                continue
                            satellite = satellite_id(f[0][:2], prn)
                            if satellite is None:
                                stats['unsupported_satellite_number'] += 1
                                continue
                            if satellite[0] not in systems:
                                stats['filtered_constellation'] += 1
                                continue
                            value = float(cn0)
                            if not 0 < value <= 99:
                                continue
                            key = (epoch, satellite, sid)
                            if key in seen:
                                continue
                            seen.add(key)
                            samples.append((epoch, position, satellite, sid, value))
                    elif kind == 'GSV':
                        stats['unsupported_gsv_talker'] += 1
                except (ValueError, IndexError):
                    stats['invalid_sentence'] += 1
    if not samples:
        raise ValueError('No selected GNSS GSV C/N0 samples with a preceding dated valid fix. ' + str(dict(stats)))
    return samples, stats


@dataclass
class Ephemeris:
    """Store one broadcast record with its native calendar time and field order.

    toc is seconds since the GPS epoch calendar origin in the constellation
    time scale, not necessarily GPST. values contains continuation fields in
    RINEX order; GLONASS state vectors use a different layout from Kepler
    elements. Satellite clock coefficients are not used by this power model.
    """
    prn: int
    toc: float
    values: list
    system: str = 'G'

    @property
    def toe(self):
        """Resolve seconds-of-week to the closest week around the full toc epoch.

        The half-week modular expression handles week rollover without relying
        on a potentially truncated broadcast week number. GLONASS instead uses
        the state-vector epoch directly.
        """
        if self.system == 'R':
            return self.toc  # GLONASS state epoch is UTC, not GPS time.
        # Resolve broadcast week against the full calendar epoch, including rollover.
        toe = self.values[8]
        return self.toc + (toe - self.toc % 604800 + 302400) % 604800 - 302400


def read_nav(path):
    """Parse RINEX 2 GPS or RINEX 3 navigation records by fixed column widths.

    Return lists indexed by satellite ID. Consume complete unsupported SBAS
    and NavIC blocks to keep following records aligned; RINEX 3.05 GLONASS
    has one extra continuation line. Required orbit fields must be finite,
    while unused spare fields may remain NaN. Calendar labels are stored on
    their native time scale; observation UTC conversion occurs in native_time.
    """
    records = defaultdict(list)
    with open_text(path) as stream:
        first = stream.readline()
        version = float(first[:9])
        if not 2 <= version < 4 or first[20:21] != 'N':
            raise ValueError('Expected RINEX 2/3 navigation file (not observation or RINEX 4).')
        for line in stream:
            if 'END OF HEADER' in line:
                break
        else:
            raise ValueError('RINEX END OF HEADER missing')
        for line in stream:
            if not line.strip():
                continue
            system = line[0] if version >= 3 else 'G'
            if system not in 'GRECJSI':
                raise ValueError('Unsupported RINEX constellation: ' + system)
            count = (4 if version >= 3.05 else 3) if system == 'R' else (3 if system == 'S' else 7)
            block = [stream.readline() for _ in range(count)]
            if any(not row for row in block):
                raise ValueError('Truncated RINEX navigation record')
            if system not in SYSTEM_NAMES:
                continue
            if version >= 3:
                prn = int(line[1:3])
                parts = line[4:23].split()
                start = 4
            else:
                prn = int(line[:2])
                parts = line[3:22].split()
                start = 3
            year, month, day, hour, minute = map(int, parts[:5])
            if year < 100:
                year += 2000 if year < 80 else 1900
            stamp = datetime(year, month, day, hour, minute, tzinfo=timezone.utc) + timedelta(seconds=float(parts[5]))
            values = []
            for row in block:
                for col in range(4):
                    field = row[start + 19 * col:start + 19 * (col + 1)].strip()
                    values.append(float(field.replace('D', 'E').replace('d', 'e')) if field else math.nan)
            # Spare fields (e.g. Galileo orbit 5) may legitimately be blank.
            required = values[:11] if system == 'R' else values[:17] + [values[21]]
            if system == 'E':
                required += [values[17]]
            if not all(math.isfinite(v) for v in required):
                raise ValueError(f'Missing required {system}{prn:02d} orbital parameters')
            if system == 'R':
                if np.linalg.norm(np.array(values[:12:4])) < 10000:
                    raise ValueError('Invalid GLONASS state vector')
            elif not (0 <= values[5] < 1 and values[7] > 0):
                raise ValueError(f'Invalid {system}{prn:02d} orbit')
            records[f'{system}{prn:02d}'].append(Ephemeris(prn, (stamp - GPS_EPOCH).total_seconds(), values, system))
    if not records:
        raise ValueError('No supported GNSS ephemerides in RINEX file')
    return records


def satellite_ecef(eph, gps_seconds):
    """Propagate broadcast elements to an ECEF position in metres.

    Despite its historical name, gps_seconds must be in the ephemeris native
    time scale. Solve Kepler equation by Newton iteration, apply broadcast
    second-harmonic corrections to argument of latitude, radius and inclination,
    and rotate the orbital plane into ECEF. BeiDou GEO additionally requires
    the tilted-frame transformation. GLONASS is delegated to state integration;
    health and ephemeris-age selection are responsibilities of the caller.
    """
    if eph.system == 'R':
        return glonass_ecef(eph, gps_seconds)
    p = eph.values
    tk = gps_seconds - eph.toe
    a = p[7] ** 2
    mu = 3.986004418e14 if eph.system in 'EC' else MU
    omega = 7.292115e-5 if eph.system == 'C' else OMEGA
    mean = p[3] + (math.sqrt(mu / a ** 3) + p[2]) * tk
    # Newton solve: E - e*sin(E) = M. Broadcast angular quantities are radians.
    eccentric = mean
    for _ in range(20):
        delta = (eccentric - p[5] * math.sin(eccentric) - mean) / (1 - p[5] * math.cos(eccentric))
        eccentric -= delta
        if abs(delta) < 1e-13:
            break
    true = math.atan2(math.sqrt(1 - p[5] ** 2) * math.sin(eccentric), math.cos(eccentric) - p[5])
    phi = true + p[14]
    sin2, cos2 = math.sin(2 * phi), math.cos(2 * phi)
    # RINEX harmonic terms correct argument of latitude, orbital radius and inclination.
    u = phi + p[6] * sin2 + p[4] * cos2
    r = a * (1 - p[5] * math.cos(eccentric)) + p[1] * sin2 + p[13] * cos2
    inc = p[12] + p[16] * tk + p[11] * sin2 + p[9] * cos2
    # BDS GEO broadcast elements use a tilted frame. Distinguish low-inclination
    # GEO from the high-inclination IGSO using the broadcast orbit itself.
    geo = eph.system == 'C' and a > 4e7 and abs(p[12]) < .3
    node = p[10] + (p[15] - (0 if geo else omega)) * tk - omega * p[8]
    x, y = r * math.cos(u), r * math.sin(u)
    xyz = np.array([x * math.cos(node) - y * math.cos(inc) * math.sin(node),
                    x * math.sin(node) + y * math.cos(inc) * math.cos(node), y * math.sin(inc)])
    if geo:
        angle = math.radians(-5)
        tilt = np.array([[1, 0, 0], [0, math.cos(angle), math.sin(angle)],
                         [0, -math.sin(angle), math.cos(angle)]])
        angle = omega * tk
        spin = np.array([[math.cos(angle), math.sin(angle), 0],
                         [-math.sin(angle), math.cos(angle), 0], [0, 0, 1]])
        xyz = spin @ tilt @ xyz
    return xyz


def glonass_derivative(state, acceleration):
    """Return time derivatives of rotating-frame position and velocity in SI.

    state is [x, y, z, vx, vy, vz] in metres and metres/second. acceleration
    is the broadcast external acceleration in metres/second squared. Forces
    include central gravity, J2 oblateness, centrifugal and Coriolis terms;
    the latter signs correspond to the Earth-fixed rotating frame.
    """
    position, velocity = state[:3], state[3:]
    r2 = float(position @ position)
    r = math.sqrt(r2)
    mu, j2, radius, omega = 3.9860044e14, 1.0826257e-3, 6378136.0, 7.292115e-5
    factor = 1.5 * j2 * mu * radius ** 2 / r ** 5
    gravity = -mu / r ** 3 - factor * (1 - 5 * position[2] ** 2 / r2)
    force = gravity * position + acceleration
    force[2] -= 2 * factor * position[2]
    force[:2] += omega ** 2 * position[:2]
    force[0] += 2 * omega * velocity[1]
    force[1] -= 2 * omega * velocity[0]
    return np.concatenate((velocity, force))


def glonass_ecef(eph, native_seconds, step_seconds=60):
    """Integrate a GLONASS broadcast state in rotating PZ-90 coordinates.

    Convert position, velocity and external acceleration from broadcast km
    units to SI once. Signed RK4 steps allow propagation before or after toc,
    with each step no longer than step_seconds. Return position in metres;
    no precise PZ-90 to WGS84 frame transformation is applied.
    """
    p = np.array(eph.values)
    state = np.concatenate((p[[0, 4, 8]], p[[1, 5, 9]])) * 1000
    acceleration = p[[2, 6, 10]] * 1000
    interval = native_seconds - eph.toc
    steps = max(1, math.ceil(abs(interval) / step_seconds))
    step = interval / steps
    for _ in range(steps):
        k1 = glonass_derivative(state, acceleration)
        k2 = glonass_derivative(state + step * k1 / 2, acceleration)
        k3 = glonass_derivative(state + step * k2 / 2, acceleration)
        k4 = glonass_derivative(state + step * k3, acceleration)
        state += step * (k1 + 2 * k2 + 2 * k3 + k4) / 6
    return state[:3]


def receiver_ecef(lat, lon, height):
    """Convert WGS84 geodetic degrees and ellipsoidal height to ECEF metres.

    The prime-vertical radius n accounts for ellipsoid flattening. Height here
    is above the ellipsoid, distinct from the atmospheric model MSL height.
    """
    lat, lon = np.radians([lat, lon])
    e2 = 6.69437999014e-3
    n = 6378137 / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    return np.array([(n + height) * math.cos(lat) * math.cos(lon),
                     (n + height) * math.cos(lat) * math.sin(lon),
                     (n * (1 - e2) + height) * math.sin(lat)])


def geometry(eph, gps_seconds, position):
    """Return geometric range (m), azimuth (deg), and elevation (deg).

    position is geodetic latitude, longitude and ellipsoidal height. The time
    argument uses the satellite native scale. Four travel-time iterations
    evaluate the satellite at transmission and rotate it into the reception
    ECEF frame. Project the line of sight onto local east/north/up axes:
    azimuth increases clockwise from true north and elevation from the horizon.
    """
    receiver = receiver_ecef(*position)
    travel = 0.075
    for _ in range(4):
        satellite = satellite_ecef(eph, gps_seconds - travel)
        angle = OMEGA * travel
        rotation = np.array([[math.cos(angle), math.sin(angle), 0],
                             [-math.sin(angle), math.cos(angle), 0], [0, 0, 1]])
        delta = rotation @ satellite - receiver
        distance = np.linalg.norm(delta)
        travel = distance / C
    lat, lon = np.radians(position[:2])
    east = -math.sin(lon) * delta[0] + math.cos(lon) * delta[1]
    north = -math.sin(lat) * math.cos(lon) * delta[0] - math.sin(lat) * math.sin(lon) * delta[1] + math.cos(lat) * delta[2]
    up = math.cos(lat) * math.cos(lon) * delta[0] + math.cos(lat) * math.sin(lon) * delta[1] + math.sin(lat) * delta[2]
    return float(distance), math.degrees(math.atan2(east, north)) % 360, math.degrees(math.atan2(up, math.hypot(east, north)))


def write_csv(path, rows, fieldnames=None):
    """Write dictionaries as UTF-8 CSV, including a header for empty cuts.

    Supply fieldnames when rows may be empty; otherwise the first row defines
    the column order. newline="" lets the CSV writer control line endings.
    """
    with open(path, 'w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames or list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def aggregate(rows, width):
    """Bin one signal group and normalize its median residuals to a 0 dB peak.

    Use rectangular theta/phi bins of width degrees, wrapping azimuth at 360
    and keeping theta=90 in the final bin. Each populated bin reports its
    centre, observation count, median and population standard deviation.
    The caller must pass a nonempty single group: mixing groups here would
    confound their different reference powers and frequency responses.
    """
    groups = defaultdict(list)
    for row in rows:
        groups[(min(int(row['theta_deg'] // width), round(90 / width) - 1),
                int((row['phi_deg'] % 360) // width))].append(row['gain_residual_db'])
    result = []
    for (theta, phi), gains in sorted(groups.items()):
        result.append(dict(azimuth_deg=(phi + .5) * width, elevation_deg=90 - (theta + .5) * width,
                           theta_deg=(theta + .5) * width, phi_deg=(phi + .5) * width,
                           count=len(gains), gain_residual_db=float(np.median(gains)),
                           std_db=float(np.std(gains))))
    peak = max(row['gain_residual_db'] for row in result)
    for row in result:
        row['relative_gain_db'] = row['gain_residual_db'] - peak
    return result


def directional_cuts(rows, azimuth, elevation, width):
    """Select observed 2D bin centres within half the full cut width.

    The azimuth cut holds elevation fixed; the elevation cut holds azimuth
    fixed with circular angular separation at north. Preserve individual bins
    and their full-pattern normalization without averaging the other angle.
    """
    return {
        'azimuth_cut': sorted((r for r in rows if abs(r['elevation_deg'] - elevation) <= width / 2),
                              key=lambda r: (r['azimuth_deg'], r['elevation_deg'])),
        'elevation_cut': sorted((r for r in rows if abs((r['azimuth_deg'] - azimuth + 180) % 360 - 180) <= width / 2),
                                key=lambda r: (r['elevation_deg'], r['azimuth_deg'])),
    }


def principal_plane_cuts(rows, width):
    """Select the elevation-15-degree band and both halves of vertical planes.

    Horizontal plotting angle equals azimuth. Vertical plotting angle is
    elevation on the north/east side and 180-elevation on the south/west side,
    placing zenith at 90 degrees. Return copied rows carrying side and
    plane_angle_deg; the cut width selects nearby bin centres, not raw samples.
    """
    result = {'horizontal_pattern': [], 'vertical_ns_pattern': [], 'vertical_ew_pattern': []}
    for row in rows:
        az, el = row['azimuth_deg'], row['elevation_deg']
        if abs(el - 15) <= width / 2:
            result['horizontal_pattern'].append(dict(row, plane_angle_deg=az, side='horizontal'))
        for name, forward, labels in (('vertical_ns_pattern', 0, ('N', 'S')),
                                       ('vertical_ew_pattern', 90, ('E', 'W'))):
            separation = abs((az - forward + 180) % 360 - 180)
            if separation <= width / 2:
                result[name].append(dict(row, plane_angle_deg=el, side=labels[0]))
            elif 180 - separation <= width / 2:
                result[name].append(dict(row, plane_angle_deg=180 - el, side=labels[1]))
    return {name: sorted(selected, key=lambda r: r['plane_angle_deg']) for name, selected in result.items()}


def plot_principal_planes(rows, out, width, plt):
    """Draw the three principal polar cuts using one common gain floor.

    Shift dB values to nonnegative plotting radii and label ticks in original
    dB units. Horizontal angles follow compass bearings; vertical cuts show
    only the upper hemisphere. Empty selections are explicitly labelled.
    """
    floor = 10 * math.floor(min(-30, min(r['relative_gain_db'] for r in rows) - 3) / 10)
    titles = {'horizontal_pattern': 'Horizontal cut (elevation 15 deg)',
              'vertical_ns_pattern': 'Vertical plane: North - Zenith - South',
              'vertical_ew_pattern': 'Vertical plane: East - Zenith - West'}
    for name, selected in principal_plane_cuts(rows, width).items():
        fig, ax = plt.subplots(subplot_kw={'projection': 'polar'})
        horizontal = name == 'horizontal_pattern'
        ax.set_theta_zero_location('N' if horizontal else 'E')
        ax.set_theta_direction(-1 if horizontal else 1)
        if horizontal:
            ax.set_thetagrids([0, 90, 180, 270], ['N', 'E', 'S', 'W'])
        else:
            ax.set_thetamin(0)
            ax.set_thetamax(180)
            labels = ('N', 'S') if name == 'vertical_ns_pattern' else ('E', 'W')
            ax.set_thetagrids([0, 30, 60, 90, 120, 150, 180],
                             [labels[0], '30°', '60°', 'Zenith', '60°', '30°', labels[1]])
        ax.scatter(np.radians([r['plane_angle_deg'] for r in selected]),
                   [r['relative_gain_db'] - floor for r in selected], s=18)
        ax.set_ylim(0, -floor)
        ticks = np.arange(floor, 1, 10)
        ax.set_yticks(ticks - floor, [f'{v:g} dB' for v in ticks])
        ax.set_title(f'{titles[name]}\nAngular selection +/- {width / 2:g} deg')
        if not selected:
            ax.text(.5, .5, 'No observations in this plane band', transform=ax.transAxes, ha='center')
        fig.savefig(out / (name + '.png'), dpi=160, bbox_inches='tight')
        plt.close(fig)


def plot_pattern(rows, out, args):
    """Export signal-specific principal, arbitrary, legacy and 3D plots.

    Use a noninteractive backend for batch runs and close figures after saving.
    Observed bins remain discrete: interpolation belongs to the separate
    combined exporter. The 3D radius represents relative power, 10**(dB/10),
    while colour preserves the logarithmic relative gain.
    """
    os.environ.setdefault('MPLCONFIGDIR', '/tmp/gnss-antenna-matplotlib')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plot_principal_planes(rows, out, args.cut_width, plt)
    theta = np.array([r['theta_deg'] for r in rows])
    phi = np.array([r['phi_deg'] for r in rows])
    gain = np.array([r['relative_gain_db'] for r in rows])
    cuts = directional_cuts(rows, args.azimuth_cut, args.elevation_cut, args.cut_width)
    for name, selected in cuts.items():
        is_azimuth = name == 'azimuth_cut'
        coordinate = 'azimuth_deg' if is_azimuth else 'elevation_deg'
        fig, ax = plt.subplots(subplot_kw={'projection': 'polar'})
        ax.set_theta_zero_location('N' if is_azimuth else 'E')
        ax.set_theta_direction(-1 if is_azimuth else 1)
        if not is_azimuth:
            ax.set_thetamin(0)
            ax.set_thetamax(90)
        floor = 10 * math.floor(min(-30, float(gain.min()) - 3) / 10)
        # Polar radii must be nonnegative; tick labels retain the dB values.
        ax.scatter(np.radians([r[coordinate] for r in selected]),
                   [r['relative_gain_db'] - floor for r in selected], s=18)
        ax.set_ylim(0, -floor)
        ticks = np.arange(floor, 1, 10)
        ax.set_yticks(ticks - floor, [f'{v:g} dB' for v in ticks])
        fixed = f'Elevation {args.elevation_cut:g}' if is_azimuth else f'Azimuth {args.azimuth_cut:g}'
        ax.set_title(f'{name.replace("_", " ").title()}\n{fixed} +/- {args.cut_width / 2:g} deg')
        if not selected:
            ax.text(.5, .5, 'No observations in this cut', transform=ax.transAxes, ha='center')
        fig.savefig(out / (name + '.png'), dpi=160, bbox_inches='tight')
        plt.close(fig)
    for name, angle, mask in (
        ('theta_cut', theta, np.abs((phi - args.phi_cut + 180) % 360 - 180) <= args.cut_width / 2),
        ('phi_cut', phi, np.abs(theta - args.theta_cut) <= args.cut_width / 2)):
        fig, ax = plt.subplots(subplot_kw={'projection': 'polar'})
        ax.set_theta_zero_location('N')
        ax.set_theta_direction(-1)
        # Points only: do not invent a continuous cut across unobserved directions.
        ax.scatter(np.radians(angle[mask]), gain[mask], s=18)
        ax.set_ylim(min(-30, float(gain.min()) - 3), 0)
        fixed = f'phi={args.phi_cut:g}' if name == 'theta_cut' else f'theta={args.theta_cut:g}'
        ax.set_title(f'{name}: {fixed} +/- {args.cut_width / 2:g} deg\nRelative gain (dB)')
        if not mask.any():
            ax.text(.5, .5, 'No observations in this cut', transform=ax.transAxes, ha='center')
        fig.savefig(out / (name + '.png'), dpi=160, bbox_inches='tight')
        plt.close(fig)
    # Every retained constellation/signal uses the same ENU rendering path.
    groups = sorted({r['signal_group'] for r in rows})
    label = ', '.join(f'{SYSTEM_NAMES[g[0]]} ({g})' for g in groups)
    draw_pattern_3d(rows, out, label)


def main():
    """Run the command-line analysis from input validation through audited exports.

    Resolve signal frequencies and native times before orbit selection, reject
    unhealthy or old records, apply geometric and gas-loss corrections, then
    aggregate each constellation/signal independently. Save the original
    samples, normalized bins, cut plots and processing assumptions alongside
    the optional illustrative combined patterns.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--nmea', type=Path, nargs='+', action='extend', required=True,
                        help='NMEA log files in acquisition order; accepts multiple paths and repeated --nmea')
    parser.add_argument('--nav', type=Path, help='Local RINEX NAV; omit to download BKG real-time BRDC by NMEA date')
    parser.add_argument('--nav-cache', type=Path, default=Path('.cache/brdc'), help='BKG download cache directory')
    parser.add_argument('--refresh-nav', action='store_true', help='Download again even for cached historical days')
    parser.add_argument('--output', type=Path, default=Path('output'))
    parser.add_argument('--no-progress', action='store_true', help='Disable calculation progress and stage messages')
    parser.add_argument('--no-combined', action='store_true', help='Disable combined interpolated cut plots')
    parser.add_argument('--combined-max-gap', type=float, default=30, help='Maximum angular gap to interpolate in combined cuts, degrees (default: 30)')
    parser.add_argument('--reference-cn0', type=float, required=True, help='Model C/N0 before gas absorption at reference range, dB-Hz; calibration required for absolute gain')
    parser.add_argument('--gas-model', choices=('standard', 'none'), default='standard', help='Standard-atmosphere gas absorption (default: standard)')
    parser.add_argument('--frequency-mhz', type=float, help='Override frequency for a single selected constellation/signal group')
    parser.add_argument('--signal-frequency', action='append', default=[], metavar='SYSTEM:ID=MHZ', help='Per-signal frequency override; repeatable, e.g. C:1=1561.098')
    parser.add_argument('--signal-reference-cn0', action='append', default=[], metavar='SYSTEM:ID=DBHZ', help='Per-signal reference C/N0 override')
    parser.add_argument('--constellations', default='GRECJ', help='RINEX system letters to process (default: GRECJ; comma separators allowed)')
    parser.add_argument('--atmosphere-height-m', type=float, default=0, help='Fixed station height above mean sea level for atmosphere, m (default: 0)')
    parser.add_argument('--reference-range-m', type=float, default=20200000)
    parser.add_argument('--gps-utc-offset', type=int, required=True, help='GPS minus UTC seconds for the observation date')
    parser.add_argument('--signal-id', help='Exact NMEA signal ID filter within selected constellations; empty string selects legacy GSV')
    parser.add_argument('--height-m', type=float, default=0, help='Fallback ellipsoidal height when GGA height is unavailable')
    parser.add_argument('--min-elevation', type=float, default=10)
    parser.add_argument('--max-ephemeris-age', type=float, default=7200, help='Maximum absolute time from toe, seconds')
    parser.add_argument('--bin-deg', type=float, default=5)
    parser.add_argument('--azimuth-cut', '--phi-cut', dest='azimuth_cut', type=float, default=0,
                        help='Fixed azimuth for elevation cut, degrees (default: 0, true north)')
    cut_angle = parser.add_mutually_exclusive_group()
    cut_angle.add_argument('--elevation-cut', type=float, help='Fixed elevation for azimuth cut, degrees (default: 45)')
    cut_angle.add_argument('--theta-cut', type=float, help='Legacy zenith angle for azimuth cut (elevation = 90 - theta)')
    parser.add_argument('--cut-width', type=float, default=10)
    args = parser.parse_args()
    args.constellations = args.constellations.upper().replace(',', '')
    if not args.constellations or any(s not in SYSTEM_NAMES for s in args.constellations):
        parser.error('--constellations must contain G, R, E, C, J')
    if args.signal_id is not None:
        args.signal_id = args.signal_id.upper()
    if args.elevation_cut is None:
        args.elevation_cut = 90 - args.theta_cut if args.theta_cut is not None else 45
    args.theta_cut = 90 - args.elevation_cut
    args.phi_cut = args.azimuth_cut
    if any(not math.isfinite(v) for v in vars(args).values() if isinstance(v, float)):
        parser.error('Numeric arguments must be finite')
    if not 0 <= args.min_elevation < 90 or args.reference_range_m <= 0 or args.max_ephemeris_age <= 0:
        parser.error('Invalid elevation, reference range, or ephemeris age')
    if not 0 < args.bin_deg <= 90 or abs(90 / args.bin_deg - round(90 / args.bin_deg)) > 1e-8:
        parser.error('--bin-deg must divide 90 exactly')
    if not 0 <= args.theta_cut <= 90 or not 0 <= args.phi_cut < 360 or not 0 < args.cut_width <= 180:
        parser.error('Invalid cut angles or width')
    if args.gas_model == 'standard' and args.min_elevation < 5:
        parser.error('Standard gas model requires --min-elevation >= 5 degrees')
    if not 0 < args.combined_max_gap <= 360:
        parser.error('--combined-max-gap must be in (0, 360] degrees')
    try:
        def stage(message):
            if not args.no_progress:
                print(message, file=sys.stderr, flush=True)

        frequency_overrides = parse_signal_values(args.signal_frequency)
        references = parse_signal_values(args.signal_reference_cn0)
        if any(not 1000 <= v <= 2000 for v in frequency_overrides.values()):
            raise ValueError('Signal frequency must be 1000..2000 MHz')
        gas_models = {}
        stage('Reading NMEA...')
        samples, stats = read_nmea(args.nmea, args.signal_id, args.height_m, args.constellations)
        observed_groups = {(s[2][0], s[3]) for s in samples}
        if args.frequency_mhz is not None:
            if len(observed_groups) != 1 or not 1000 <= args.frequency_mhz <= 2000:
                raise ValueError('--frequency-mhz requires one selected constellation/signal and 1000..2000 MHz; use --signal-frequency for multiple groups')
            system, sid = next(iter(observed_groups))
            frequency_overrides[f'{system}:{sid or "legacy"}'] = args.frequency_mhz
        stage('Loading navigation data...')
        if args.nav is not None:
            nav = read_nav(args.nav)
            nav_sources = [dict(path=str(args.nav), local=True)]
        else:
            nav, nav_sources = automatic_nav(samples, args.nav_cache, args.refresh_nav)
        rows, signal_errors = [], {}
        for epoch, position, satellite, sid, cn0 in progress_samples(samples, enabled=not args.no_progress):
            system = satellite[0]
            group = f'{system}_{sid or "legacy"}'
            gps_seconds = native_time(epoch, GPS_EPOCH, system, args.gps_utc_offset)
            max_age = min(args.max_ephemeris_age, 1800) if system == 'R' else args.max_ephemeris_age
            # Health depends on the signal band; select the nearest usable native-time epoch.
            candidates = [e for e in nav.get(satellite, []) if healthy(e, sid) and abs(gps_seconds - e.toe) <= max_age]
            if not candidates:
                stats[f'{group}:missing_healthy_recent_ephemeris'] += 1
                continue
            eph = min(candidates, key=lambda e: abs(gps_seconds - e.toe))
            try:
                frequency = frequency_mhz(system, sid, eph, frequency_overrides)
            except ValueError as exc:
                stats[f'{group}:unknown_frequency'] += 1
                signal_errors[group] = str(exc)
                continue
            distance, azimuth, elevation = geometry(eph, gps_seconds, position)
            if elevation < args.min_elevation:
                stats[f'{group}:below_elevation_mask'] += 1
                continue
            # Cache by actual carrier frequency, including distinct GLONASS FDMA channels.
            if args.gas_model == 'standard' and frequency not in gas_models:
                gas_models[frequency] = StandardGasModel(frequency, args.atmosphere_height_m)
            gas_model = gas_models.get(frequency)
            dry_loss, wet_loss = gas_model.attenuation(elevation) if gas_model else (0.0, 0.0)
            gas_loss = dry_loss + wet_loss
            reference = references.get(f'{system}:{sid or "legacy"}', args.reference_cn0)
            # Inverse-square power loss becomes -20*log10(range ratio) in dB.
            # Subtract gas loss from the model before forming measured-minus-model residuals.
            vacuum_cn0 = reference - 20 * math.log10(distance / args.reference_range_m)
            ideal = vacuum_cn0 - gas_loss
            rows.append(dict(utc=epoch.isoformat(), satellite=satellite, constellation=SYSTEM_NAMES[system],
                             signal_id=sid, signal_group=group, frequency_mhz=frequency, reference_cn0_dbhz=reference,
                             latitude_deg=position[0], longitude_deg=position[1], height_m=position[2],
                             range_m=distance, azimuth_deg=azimuth, elevation_deg=elevation,
                             theta_deg=90 - elevation, phi_deg=azimuth, measured_cn0_dbhz=cn0,
                             vacuum_model_cn0_dbhz=vacuum_cn0, gas_dry_attenuation_db=dry_loss,
                             gas_water_vapour_attenuation_db=wet_loss, gas_attenuation_db=gas_loss,
                             model_cn0_dbhz=ideal, gain_residual_db=cn0 - ideal))
        if not rows:
            raise ValueError('No usable observations after orbit/elevation filtering: ' + str(dict(stats)))
        for group, message in signal_errors.items():
            print(f'{group}: {message}', file=sys.stderr)
        stage('Aggregating signal-separated patterns and rendering plots...')
        groups = defaultdict(list)
        for row in rows:
            groups[row['signal_group']].append(row)
        args.output.mkdir(parents=True, exist_ok=True)
        write_csv(args.output / 'samples.csv', rows)
        all_pattern, group_metadata = [], {}
        for group, group_rows in sorted(groups.items()):
            stage(f'Writing {group}: {len(group_rows)} samples...')
            out = args.output / group if len(groups) > 1 else args.output
            out.mkdir(parents=True, exist_ok=True)
            # Normalize within each signal group before any illustrative cross-group merge.
            pattern = [dict(signal_group=group, **r) for r in aggregate(group_rows, args.bin_deg)]
            all_pattern.extend(pattern)
            write_csv(out / 'samples.csv', group_rows)
            write_csv(out / 'pattern.csv', pattern)
            plane_cuts = principal_plane_cuts(pattern, args.cut_width)
            for name, selected in plane_cuts.items():
                write_csv(out / (name + '.csv'), selected,
                          fieldnames=list(pattern[0]) + ['plane_angle_deg', 'side'])
            for name, selected in directional_cuts(pattern, args.azimuth_cut, args.elevation_cut, args.cut_width).items():
                write_csv(out / (name + '.csv'), selected, fieldnames=list(pattern[0]))
            plot_pattern(pattern, out, args)
            group_metadata[group] = dict(path=str(out.relative_to(args.output)), samples=len(group_rows),
                                         bins=len(pattern), constellation=group_rows[0]['constellation'],
                                         signal_id=group_rows[0]['signal_id'],
                                         legacy_frequency_assumption=not group_rows[0]['signal_id'] and
                                         f'{group[0]}:legacy' not in frequency_overrides,
                                         frequencies_mhz=sorted({r['frequency_mhz'] for r in group_rows}),
                                         plane_bin_counts={name: len(selected) for name, selected in plane_cuts.items()})
        write_csv(args.output / 'pattern.csv', all_pattern)
        combined_metadata = None
        if not args.no_combined:
            stage('Combining constellation cuts and rendering PCHIP estimates...')
            combined_metadata = export_combined(all_pattern, args.output / 'combined', args.cut_width,
                                                args.azimuth_cut, args.elevation_cut, args.combined_max_gap)
        # Keep model assumptions, input provenance and rejection counts with the results.
        metadata = dict(arguments={k: [str(p) for p in v] if k == 'nmea' else str(v) if isinstance(v, Path) else v
                                   for k, v in vars(args).items()},
                        samples=len(rows), bins=len(all_pattern), skipped=dict(stats), nav_sources=nav_sources,
                        groups=group_metadata, signal_errors=signal_errors,
                        normalization='Independent peak=0 dB for each constellation and NMEA signal ID; combined/ contains separately labelled illustrative estimates',
                        combined=combined_metadata,
                        model='reference_cn0 - 20*log10(range/reference_range) - gas_attenuation_db',
                        gas_model=dict(enabled=args.gas_model == 'standard'),
                        gas_models={str(f): m.metadata for f, m in gas_models.items()},
                        frame='Local ENU: azimuth clockwise from true north, elevation above horizon; theta=90-elevation, phi=azimuth; fixed antenna assumed',
                        time_scales='GPS/QZSS/Galileo: GPST approximation; BeiDou: GPST-14s; GLONASS RINEX: UTC',
                        cuts=dict(fixed_azimuth_deg=args.azimuth_cut, fixed_elevation_deg=args.elevation_cut,
                                  full_width_deg=args.cut_width, selection='Observed 2D bin centers within half-width'),
                        principal_planes=dict(horizontal_elevation_deg=15, vertical_ns_azimuths_deg=[0, 180],
                                              vertical_ew_azimuths_deg=[90, 270], full_width_deg=args.cut_width,
                                              vertical_angle='0=N/E horizon, 90=zenith, 180=S/W horizon; upper hemisphere only'),
                        limitation='Relative system response, not absolute antenna gain; signal-separated plots are not interpolated, combined/ is an illustrative interpolated estimate. Native ECEF frames treated as WGS84 at antenna-pattern accuracy.')
        for group, info in group_metadata.items():
            if info['path'] != '.':
                (args.output / info['path'] / 'metadata.json').write_text(
                    json.dumps(dict(metadata, selected_group=group), indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        (args.output / 'metadata.json').write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        print(f'{len(rows)} samples, {len(all_pattern)} bins, {len(groups)} signal groups -> {args.output}')
        print('Skipped:', dict(stats))
    except (ValueError, OSError, EOFError) as exc:
        parser.exit(2, f'Error: {exc}\n')


if __name__ == '__main__':
    main()
