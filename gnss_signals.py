"""NMEA satellite numbering and NMEA 4.11 signal bands.

Source: receiver NMEA interface documentation (links in README).
Vendor/older-NMEA signal definitions may be overridden explicitly by the user.
"""
import math

SYSTEM_NAMES = {'G': 'GPS', 'E': 'Galileo', 'R': 'GLONASS', 'C': 'BeiDou', 'J': 'QZSS'}
TALKERS = {'GP': 'G', 'GA': 'E', 'GL': 'R', 'GB': 'C', 'BD': 'C', 'GQ': 'J', 'QZ': 'J'}
# Keys are NMEA signal IDs (including hexadecimal letters), not RINEX observation codes.
# Values are carrier frequencies in MHz; GLONASS FDMA is resolved from each ephemeris.
BANDS = {
    'G': {'1': 1575.42, '2': 1575.42, '3': 1575.42, '4': 1227.60,
          '5': 1227.60, '6': 1227.60, '7': 1176.45, '8': 1176.45},
    'E': {'1': 1176.45, '2': 1207.14, '3': 1191.795, '4': 1278.75,
          '5': 1278.75, '6': 1575.42, '7': 1575.42},
    'C': {'1': 1561.098, '2': 1561.098, '3': 1575.42, '4': 1575.42,
          '5': 1176.45, '6': 1207.14, '7': 1191.795, '8': 1268.52,
          '9': 1268.52, 'A': 1268.52, 'B': 1207.14, 'C': 1207.14},
    'J': {'1': 1575.42, '2': 1575.42, '3': 1575.42, '4': 1575.42,
          '5': 1227.60, '6': 1227.60, '7': 1176.45, '8': 1176.45,
          '9': 1278.75, 'A': 1278.75},
}


def satellite_id(talker, number):
    """Map supported talker/number combinations to an unambiguous RINEX ID.

    Accept both local numbers and documented constellation offsets, including
    QZSS numbers carried by GP. Reject out-of-range numbers and mixed GN
    talkers because the number alone is insufficient for safe classification.
    """
    system = TALKERS.get(talker)
    if system is None:
        return None
    n = int(number)
    if system == 'G' and 193 <= n <= 202:
        system, n = 'J', n - 192
    elif system == 'R' and 65 <= n <= 96:
        n -= 64
    elif system == 'E' and 301 <= n <= 336:
        n -= 300
    elif system == 'C':
        if 201 <= n <= 263:
            n -= 200
        elif 101 <= n <= 163:
            n -= 100
    elif system == 'J' and 193 <= n <= 202:
        n -= 192
    maximum = {'G': 32, 'R': 32, 'E': 36, 'C': 63, 'J': 10}[system]
    return f'{system}{n:02d}' if 1 <= n <= maximum else None


def frequency_mhz(system, sid, eph, overrides=None):
    """Resolve a signal carrier frequency in MHz, allowing explicit overrides.

    Keys have the form SYSTEM:ID or SYSTEM:legacy. Missing legacy IDs use
    conventional first-band assumptions. GLONASS FDMA frequencies depend on
    the broadcast channel in eph.values[7]; reject missing or invalid channels
    instead of substituting a nominal centre frequency.
    """
    key = f'{system}:{sid or "legacy"}'
    if overrides and key in overrides:
        return overrides[key]
    if not sid:
        # Legacy GSV carries no band ID: retain conventional first-band assumption.
        sid = '7' if system == 'E' else '1'
    if system == 'R':
        if sid not in ('1', '2', '3', '4'):
            raise ValueError(f'Unknown GLONASS signal {sid}; supply --signal-frequency {key}=MHz')
        channel = eph.values[7]
        if not math.isfinite(channel) or channel != int(channel):
            raise ValueError('Missing GLONASS frequency channel')
        channel = int(channel)
        if channel > 128:  # Some files retain the unsigned broadcast representation.
            channel -= 256
        if not -7 <= channel <= 6:
            raise ValueError(f'Invalid GLONASS frequency channel {channel}')
        return (1602 + channel * .5625) if sid in ('1', '2') else (1246 + channel * .4375)
    value = BANDS.get(system, {}).get(sid)
    if value is None:
        raise ValueError(f'Unknown/combined signal {key}; supply --signal-frequency {key}=MHz or filter it out')
    return value


def native_time(utc, gps_epoch, system, gps_utc_offset):
    """Express an observation UTC epoch on the broadcast constellation scale.

    GPS, QZSS and Galileo use UTC plus the supplied GPS-UTC offset; BeiDou
    subtracts another 14 seconds. GLONASS RINEX uses UTC directly. Small
    GST/GPST differences are neglected at this antenna-pattern accuracy.
    """
    elapsed = (utc - gps_epoch).total_seconds()
    return elapsed if system == 'R' else elapsed + gps_utc_offset - (14 if system == 'C' else 0)


def healthy(eph, sid):
    """Check broadcast health for the requested constellation and signal band.

    GLONASS and non-Galileo Kepler records use their respective health fields.
    For Galileo, combine the data-source mask with the relevant three health/
    data-validity bits. Reject bands whose health a single supported record
    cannot establish, including combined E5a+b and E6.
    """
    p = eph.values
    if eph.system == 'R':
        return p[3] == 0
    if eph.system != 'E':
        return p[21] == 0
    # Galileo health/DVS bits are signal-specific; F/NAV does not describe E5b.
    source, flags = int(p[17]), int(p[21])
    sid = sid or '7'
    if sid == '1':
        return bool(source & 2) and (flags >> 3) & 7 == 0
    if sid == '2':
        return bool(source & 5) and (flags >> 6) & 7 == 0
    if sid in ('6', '7'):
        return bool(source & 7) and flags & 7 == 0
    if sid == '3':
        return False  # No single F/NAV or I/NAV record establishes both E5 bands.
    return False  # E6 health is not carried in RINEX 3 OS navigation records.


def parse_signal_values(specs):
    """Parse repeatable SYSTEM:ID=value options into finite numeric overrides.

    Normalize constellation and hexadecimal signal letters to uppercase and
    the legacy key to lowercase. Later occurrences replace earlier values.
    Physical range validation is left to the frequency/reference consumer.
    """
    result = {}
    for spec in specs:
        try:
            key, value = spec.split('=')
            system, sid = key.split(':')
            system, sid = system.upper(), sid.upper()
            if system not in SYSTEM_NAMES or (sid != 'LEGACY' and (len(sid) != 1 or sid not in '0123456789ABCDEF')):
                raise ValueError()
            value = float(value)
            if not math.isfinite(value):
                raise ValueError()
        except ValueError as exc:
            raise ValueError(f'Expected SYSTEM:ID=value, e.g. C:1=1561.098; got {spec!r}') from exc
        result[f'{system}:{sid.lower() if sid == "LEGACY" else sid}'] = value
    return result
