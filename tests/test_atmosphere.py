"""Verify gas-loss units, physical trends and numerical convergence.

Adaptive quadrature provides an independent integration check; shell-step
refinement tests discretization error rather than reproducing the algorithm.
"""
import unittest

import numpy as np
from scipy.integrate import quad

from atmosphere import StandardGasModel


class AtmosphereTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Reuse one expensive atmospheric profile across tests that change only elevation.
        cls.model = StandardGasModel()

    def test_elevation_and_height(self):
        # Longer low-elevation paths increase loss; spherical geometry limits the plane-parallel estimate.
        zenith = sum(self.model.attenuation(90))
        low = sum(self.model.attenuation(10))
        self.assertTrue(0 < zenith < 0.1)
        self.assertGreater(low, zenith)
        self.assertLess(low, zenith / np.sin(np.radians(10)))
        self.assertLess(sum(StandardGasModel(station_height_m=2000).attenuation(90)), zenith)
        dry, wet = self.model.attenuation(90)
        self.assertGreater(dry, 0)
        self.assertGreater(wet, 0)

    def test_zenith_against_adaptive_quadrature(self):
        # Independent quadrature checks shell integration and pressure/length units.
        # Compare shell summation with independent adaptive integration across atmospheric layer boundaries.
        from itur.models import itu676, itu835

        def integrand(h):
            t = itu835.standard_temperature(h).value
            rho = itu835.standard_water_vapour_density(h).value
            p = itu835.standard_pressure(h).value - rho * t / 216.7
            return itu676.gamma_exact(1.57542, p, rho, t).value

        reference, _ = quad(integrand, 0, 100, epsabs=1e-8, points=[11, 20, 32, 47, 51, 71, 86])
        self.assertAlmostEqual(sum(self.model.attenuation(90)), reference, delta=1e-6)

    def test_layer_convergence_and_bands(self):
        # Halving shell thickness should converge while other GNSS bands retain finite positive loss.
        refined = StandardGasModel(step_km=0.025)
        for elevation in (5, 10, 45, 90):
            self.assertAlmostEqual(sum(self.model.attenuation(elevation)),
                                   sum(refined.attenuation(elevation)), delta=1e-5)
        for frequency in (1227.60, 1176.45):
            loss = sum(StandardGasModel(frequency_mhz=frequency).attenuation(30))
            self.assertTrue(np.isfinite(loss) and loss > 0)

    def test_invalid_inputs(self):
        # Reject unsupported physical ranges and NaN values instead of returning misleading attenuation.
        for kwargs in ({'frequency_mhz': 999}, {'station_height_m': -1},
                       {'station_height_m': 10001}, {'frequency_mhz': float('nan')}):
            with self.assertRaises(ValueError):
                StandardGasModel(**kwargs)
        for elevation in (0, 4.9, 91, float('nan')):
            with self.assertRaises(ValueError):
                self.model.attenuation(elevation)
