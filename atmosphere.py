"""Standard-atmosphere gas absorption for ground-based GNSS receivers.

P.676-12 line absorption integrated through a P.835-6 atmosphere along a
straight ray in spherical shells. This is not refractive ray tracing.
"""
import math

import numpy as np


class StandardGasModel:
    """Cache a standard-atmosphere absorption profile for one carrier frequency.

    The profile is independent of satellite direction, so many elevation
    queries reuse the same layer coefficients. Heights are above mean sea
    level and absorption is signal loss only; weather variability, refraction,
    scintillation and atmospheric contributions to receiver noise are excluded.
    """
    def __init__(self, frequency_mhz=1575.42, station_height_m=0, step_km=0.05):
        """Precompute dry-air and water-vapour attenuation coefficients in dB/km.

        frequency_mhz is restricted to 1000..2000 MHz, station_height_m to
        0..10000 m MSL, and step_km bounds the layer thickness. Pin P.676-12
        and P.835-6 explicitly, sample each shell at its midpoint, and retain
        the model versions and physical assumptions in metadata for reproducibility.
        """
        if not math.isfinite(frequency_mhz) or not 1000 <= frequency_mhz <= 2000:
            raise ValueError('Gas model frequency must be between 1000 and 2000 MHz')
        if not math.isfinite(station_height_m) or not 0 <= station_height_m <= 10000:
            raise ValueError('Atmosphere station height must be 0..10000 m above mean sea level')
        if not math.isfinite(step_km) or not 0 < step_km <= 1:
            raise ValueError('Atmosphere integration step must be 0..1 km (exclusive of zero)')
        try:
            import itur
            from itur.models import itu676, itu835
        except ImportError as exc:
            raise ValueError('Standard gas model requires ITU-Rpy: pip install -r requirements.txt; '
                             'or use --gas-model none to disable gas absorption') from exc
        itu676.change_version(12)
        itu835.change_version(6)
        height_km = station_height_m / 1000
        self.radius_km = 6371.0 + height_km
        self.edges_km = np.linspace(height_km, 100, math.ceil((100 - height_km) / step_km) + 1)
        # Midpoint quadrature stores one absorption coefficient per spherical shell.
        mid = (self.edges_km[:-1] + self.edges_km[1:]) / 2
        temperature = itu835.standard_temperature(mid).to_value('K')
        total_pressure = itu835.standard_pressure(mid).to_value('hPa')
        density = itu835.standard_water_vapour_density(mid, h_0=2, rho_0=7.5).to_value('g / m3')
        # P.676 line widths use dry-air pressure, not total barometric pressure.
        dry_pressure = total_pressure - density * temperature / 216.7
        # ITU-Rpy expects GHz, whereas the public API and signal maps use MHz.
        frequency_ghz = frequency_mhz / 1000
        self.dry_db_km = itu676.gamma0_exact(frequency_ghz, dry_pressure, density, temperature).to_value('dB / km')
        self.wet_db_km = itu676.gammaw_exact(frequency_ghz, dry_pressure, density, temperature).to_value('dB / km')
        if not np.all(np.isfinite(self.dry_db_km + self.wet_db_km)):
            raise ValueError('Non-finite atmospheric absorption coefficients')
        self.metadata = dict(enabled=True, absorption='ITU-R P.676-12 (ITU-Rpy)',
                             atmosphere='ITU-R P.835-6 standard atmosphere', itur_version=itur.__version__,
                             frequency_mhz=frequency_mhz, station_height_m_msl=station_height_m,
                             sea_level_temperature_k=288.15, sea_level_total_pressure_hpa=1013.25,
                             sea_level_water_vapour_density_g_m3=7.5, water_vapour_scale_height_km=2,
                             top_height_km=100, maximum_layer_thickness_km=step_km,
                             geometry='Straight ray through spherical shells, Earth radius 6371 km; no refraction',
                             minimum_elevation_deg=5,
                             exclusions='Scintillation, rain/cloud loss, atmospheric emission and receiver noise changes')

    def attenuation(self, elevation_deg):
        """Integrate dry-air and water-vapour path loss, returning two dB values.

        For elevations 5..90 degrees, intersect a straight outward ray with each
        spherical shell. Rationalizing the quadratic root avoids cancellation
        near the receiver. Differences of cumulative distances give layer lengths
        in km; their dot products with dB/km coefficients give total attenuation.
        """
        if not math.isfinite(elevation_deg) or not 5 <= elevation_deg <= 90:
            raise ValueError('Standard gas model supports elevations from 5 to 90 degrees')
        sin_el = math.sin(math.radians(elevation_deg))
        delta_h = self.edges_km - self.edges_km[0]
        # Stable intersection distance of the outward ray with each spherical shell.
        radial_difference = delta_h * (2 * self.radius_km + delta_h)
        distances = radial_difference / (
            np.sqrt((self.radius_km * sin_el) ** 2 + radial_difference) + self.radius_km * sin_el)
        lengths = np.diff(distances)
        return float(self.dry_db_km @ lengths), float(self.wet_db_km @ lengths)
