# gnss-antenna-pattern

Estimate installed GNSS receiving patterns from ordinary NMEA logs — no RAWX or RINEX observation file required.

[Japanese](README_JP.md)

Estimate **relative receiving-system antenna patterns** from NMEA GNSS C/N0 measurements and RINEX navigation data, separately for each constellation and signal. GPS, Galileo, GLONASS, BeiDou and QZSS are all selected by default.

## Run

Use Python 3.10 or later.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python gnss_antenna_pattern.py \
  --nmea receiver.nmea \
  --reference-cn0 45 --gps-utc-offset 18 --output output
```

45 dB-Hz is an assumed value: specify the expected C/N0 at the reference distance (20,200 km by default), **before gas absorption**. Standard-atmosphere gas attenuation is enabled by default. Supply GPS minus UTC in seconds for the observation date; 18 seconds is an example for 2024 observations. Actual observation files are not distributed with the project.

The calculation progress bar reports percentage, processed samples, elapsed time and estimated remaining time (ETA). Rejected samples also advance the count. Reaching 100% means sample calculations have finished; aggregation, CSV export and plotting follow. Stage messages cover reading and rendering. Progress goes to standard error, updating the same terminal line or emitting a line approximately every 10 seconds when redirected. `--no-progress` disables the bar and stage messages, while download messages, warnings and final results remain. No additional progress library is required.

### Split PyGPSClient logs

Pass multiple NMEA log files after `--nmea` in acquisition order (oldest first). You can also repeat `--nmea`. Single-file commands remain supported.

```bash
python gnss_antenna_pattern.py \
  --nmea logs/part001.log logs/part002.log logs/part003.log \
  --reference-cn0 45 --gps-utc-offset 18 --output output

# Equivalent use of repeated options:
python gnss_antenna_pattern.py \
  --nmea logs/part001.log --nmea logs/part002.log logs/part003.log \
  --reference-cn0 45 --gps-utc-offset 18 --output output
```

The files are processed as one continuous sequence of NMEA sentences without creating a merged file or modifying the originals. Date, position, height and midnight-rollover state carry across file boundaries, including when a new file starts with GSV. Duplicate `(UTC epoch, satellite, signal ID)` measurements across files retain the first occurrence. Plain text and `.gz` files can be mixed; filename extensions such as `.log` or `.nmea` are accepted for plain text.

Supply chronologically ordered parts from the same receiver session and fixed antenna setup. Files are read in the supplied order, not automatically sorted by their contents; shell wildcards are also usable if their expansion order matches acquisition order. Boundaries are assumed to fall between complete NMEA sentences. Separate sessions still need a valid dated fix before their GSV data; do not rely on a previous session's position. BKG acquisition covers all UTC observation dates across the input files. `metadata.json` stores the ordered file list in `arguments.nmea`.

### Automatic BKG BRDC download

Omit `--nav` to download real-time-derived BKG BRDC over HTTPS using the UTC observation dates in the NMEA log. For logs spanning multiple days, each observation day and its preceding day are downloaded and merged. Previous-day records support orbit calculations near midnight.

The URL format is `https://igs.bkg.bund.de/root_ftp/IGS/BRDC/YYYY/DDD/BRDC00WRD_S_YYYYDDD0000_01D_MN.rnx.gz`. This fetches BKG's updated RINEX files when the script runs; it does not maintain an NTRIP connection or automatically repeat the analysis.

- The cache defaults to `.cache/brdc`; change it with `--nav-cache PATH`.
- Current-day and previous-day products are fetched again on subsequent runs. Older cached files are reused only if acquired on or after the second UTC midnight following the product date. Earlier partial snapshots are downloaded again.
- `--refresh-nav` forces historical downloads too. Downloaded files replace cached files only after successful parsing.
- Missing observation-day products, connection failures and invalid files stop the run with an explanation. Only a 404 for an optional preceding day allows processing to continue with a notification.
- Supply `--nav broadcast.nav` to use a local file without network access.
- `metadata.json` records URLs, paths and cache use in `nav_sources`.

If the published product does not yet cover the latest observations, retry later. References: [BKG HTTPS access](https://igs.bkg.bund.de/access), [example BRDC directory](https://igs.bkg.bund.de/root_ftp/IGS/BRDC/2025/003/).

## Inputs

Non-ASCII bytes in NMEA logs do not stop processing. Each affected line is skipped and counted as `non_ascii_line` in metadata `skipped`. Bytes are not deleted from measurement fields. After a skipped line, GSV observations wait for a fresh valid RMC/GGA fix to avoid using a stale time or position. This also applies to `.gz` inputs; RINEX decoding remains strict.

- RMC sentences containing a date, time and valid position, plus supported GSV sentences below, are required. Check the receiver specification to confirm that GSV SNR represents C/N0 in dB-Hz.
- GSV inherits the preceding valid RMC/GGA time and position. An initial RMC is required. Check timing associations beforehand if the log contains long communication gaps or reordered sentences.
- When GGA provides altitude and geoid separation, their sum supplies ellipsoidal height. Otherwise the initial fallback is `--height-m` (default 0 m).
- Checksums are validated when present. Different signal IDs are processed separately; use `--signal-id 1`, for example, to filter. Legacy GSV without a signal ID is supported. Ambiguous GNGSV, SBAS and NavIC are excluded.
- RINEX 2.x GPS NAV and 3.x NAV, including mixed files, are supported. Observation files and RINEX 4 are unsupported. Inputs may be gzip-compressed (`.gz`).
- Antenna orientation is assumed fixed. Theta is measured from zenith; phi increases eastward from true north. Tilt and changing orientation are not corrected.

### Constellations and signals

| Constellation | Code | Supported GSV talkers and satellite numbers | Orbit processing |
| --- | --- | --- | --- |
| GPS | G | GP, 1–32 | Broadcast Kepler elements |
| Galileo | E | GA, 1–36 / 301–336 | Galileo gravitational constant |
| GLONASS | R | GL, 1–32 / 65–96 | RK4 integration of broadcast position, velocity and acceleration |
| BeiDou | C | GB / BD, 1–63 / 101–163 / 201–263 | Separate MEO/IGSO and GEO transformations |
| QZSS | J | GQ / QZ, 1–10 / 193–202; GP 193–202 | GPS-style broadcast elements |

```bash
# Select Galileo and BeiDou.
python gnss_antenna_pattern.py --nmea receiver.nmea \
  --reference-cn0 45 --gps-utc-offset 18 --constellations E,C

# Select GPS L1 only.
python gnss_antenna_pattern.py --nmea receiver.nmea \
  --reference-cn0 45 --gps-utc-offset 18 --constellations G --signal-id 1
```

Carrier frequency is inferred from constellation and NMEA 4.11 signal ID. Examples in MHz: G:1=1575.42, E:7=1575.42, C:1=1561.098 and J:1=1575.42. GLONASS L1/L2 uses the navigation record's frequency channel k: 1602+0.5625k / 1246+0.4375k MHz.

Legacy GSV without an ID assumes GPS/QZSS L1, Galileo E1, BeiDou B1I or GLONASS L1; this assumption is recorded in metadata. NMEA 4.10 or vendor-specific signal definitions may differ. Override frequencies according to the receiver specification, for example `--signal-frequency C:3=1207.14`. This option is repeatable; use `G:legacy=1575.42` for missing IDs. Unknown or all-signal IDs (ID 0) are not assigned frequencies unconditionally.

Galileo processing checks E1/E5a/E5b health flags and the I/NAV or F/NAV data source. Combined E5a+b and E6 are excluded because a single supported RINEX 3 record cannot establish the required health status. GLONASS CDMA is unsupported. For every constellation, processing requires available navigation data, a supported signal and healthy status. Rejection counts are recorded by group in `metadata.json` under `skipped`.

`--reference-cn0` supplies the common reference C/N0; override individual signals with options such as `--signal-reference-cn0 C:1=44`. Each signal pattern is independently normalized to a maximum of 0 dB. The additional `combined/` directory contains illustrative merged patterns described below.

GPS/QZSS/Galileo times are approximated as GPST, BeiDou as GPST minus 14 seconds, and GLONASS RINEX as UTC. Small GST/GPST offsets and precise transformations between native ECEF frames are omitted. This implementation targets antenna-pattern estimation, not precise positioning. GLONASS integration includes J2, Earth rotation and broadcast external acceleration, with steps no longer than 60 seconds and broadcast kilometre units converted to metres. BeiDou GEO is identified by a semi-major axis exceeding 40,000 km and absolute broadcast inclination below 0.3 rad, then transformed through the frame tilted by 5 degrees.

## Model and limitations

### Standard-atmosphere gas attenuation (enabled by default)

The model uses ITU-Rpy 0.4.0 with **ITU-R P.676-12** line absorption and the **P.835-6** standard atmosphere. Recommendation versions are fixed; this does not implement the latest P.676 revision.

Sea-level assumptions are temperature 288.15 K, total pressure 1013.25 hPa, water-vapour density 7.5 g/m³ and water-vapour scale height 2 km. Water-vapour partial pressure is subtracted from total pressure before calculating absorption. From the specified station altitude to 100 km, spherical shells no thicker than 50 m contribute dry-air and water-vapour absorption weighted by ray path length. Earth radius is 6371 km. Rays are straight, without refractive bending, and supported elevations are at least 5 degrees.

```bash
python gnss_antenna_pattern.py --nmea receiver.nmea \
  --reference-cn0 45 --gps-utc-offset 18 \
  --atmosphere-height-m 100
```

- `--frequency-mhz`: frequency override for one selected constellation/signal group, in MHz. Otherwise frequency is inferred from the signal ID. For multiple groups, use `--signal-frequency`. Supported range: 1000–2000 MHz.
- `--atmosphere-height-m`: fixed atmospheric station altitude above mean sea level, 0–10000 m; default 0 m. This is separate from NMEA ellipsoidal height and `--height-m`. Specify the actual station altitude.
- `--gas-model none`: disable gas attenuation.
- `samples.csv` includes `vacuum_model_cn0_dbhz`, `gas_dry_attenuation_db`, `gas_water_vapour_attenuation_db` and total `gas_attenuation_db`.
- `metadata.json` records model versions, atmospheric assumptions and integration settings for each frequency under `gas_models`.

This estimates standard-atmosphere conditions, not the weather on the observation date. Rain, clouds, scintillation and changes in antenna noise temperature due to atmospheric emission are excluded. Only signal-power attenuation is applied to C/N0, assuming constant noise density. References: [ITU-Rpy P.676](https://itu-rpy.readthedocs.io/en/latest/apidoc/itu676.html), [ITU-Rpy P.835](https://itu-rpy.readthedocs.io/en/latest/apidoc/itu835.html).

For sea-level GPS L1, this implementation gives approximately 0.034 dB at zenith, 0.068 dB at 30-degree elevation and 0.192 dB at 10 degrees. Tests compare independent adaptive integration, convergence when halving the layer thickness, L1/L2/L5 frequencies, and CSV outputs with correction enabled and disabled.

```text
model_cn0 = reference_cn0 - 20 log10(range / reference_range) - gas_attenuation_db
gain_residual_db = measured_cn0 - model_cn0
relative_gain_db = bin median residual - maximum bin median within the same constellation/signal
```

Satellite positions are calculated from broadcast ephemerides, accounting for signal travel time and Earth rotation when deriving range, azimuth and elevation. RINEX NAV does not contain actual transmitted power or receiving-system noise, so these inputs alone cannot determine absolute gain in dBi. Results also include differences in satellite power and transmit patterns, receiver/cable response, polarization loss, atmosphere, obstructions and multipath. Absolute calibration requires a known reference antenna/receiving system or equivalent. A constant reference C/N0 change does not affect the normalized relative pattern.

The nearest healthy ephemeris within `--max-ephemeris-age` of toe is selected (default 7,200 seconds). GLONASS is capped at 1,800 seconds regardless of this setting. If an old local BRDC causes rejection, omit `--nav` to fetch an updated product. This check does not individually interpret each broadcast fit interval. Samples below `--min-elevation` (default 10 degrees) are excluded. Unobserved directions are missing data, not evidence of low gain.

## Outputs

### Example plots

The images below show combined estimates from GPS (`G_1`), Galileo (`E_7`), GLONASS (`R_1`) and BeiDou (`C_1`). In the cut plots, coloured points are signal-group bins, black crosses are count-weighted merged nodes, and black curves are PCHIP interpolation. Each input group has its own peak aligned to 0 dB; these examples illustrate relative receiving-system response rather than calibrated absolute antenna gain. Gaps in the curves indicate insufficient adjacent observations.

**Horizontal cut at 15-degree elevation.** The polar panel follows compass bearings; the Cartesian panel shows relative gain against azimuth.

![Combined horizontal antenna-pattern cut at 15-degree elevation, with polar and Cartesian panels](docs/horizontal_pattern.png)

**North–south vertical cut.** Both sides of the upper hemisphere are shown, with zenith between north and south.

![Combined north–zenith–south vertical antenna-pattern cut](docs/vertical_ns_pattern.png)

**East–west vertical cut.** The corresponding vertical plane runs from east through zenith to west.

![Combined east–zenith–west vertical antenna-pattern cut](docs/vertical_ew_pattern.png)

**Additional azimuth cut at 45-degree elevation.** This uses the default fixed elevation for the configurable azimuth cut.

![Combined azimuth antenna-pattern cut at 45-degree elevation](docs/azimuth_cut.png)

**Additional elevation cut at 0-degree azimuth.** This shows the elevation response toward true north.

![Combined elevation antenna-pattern cut at north-facing azimuth zero](docs/elevation_cut.png)

**Combined 3D pattern.** The left panel shows azimuth, elevation and a shifted dB radius on a spherical grid. The right panel shows sky coverage in polar coordinates, with zenith at the centre and relative gain encoded by colour. The scatter plot does not fill unobserved directions with an interpolated surface.

![Combined spherical dB antenna pattern and polar sky coverage map](docs/pattern_3d.png)

### Combined interpolated cuts

By default, `output/combined/` contains a **horizontal azimuth cut at 15-degree elevation, a north–south vertical cut and an east–west vertical cut**, plus arbitrary azimuth/elevation cuts, combining points from all constellation/signal groups. Each figure has polar and Cartesian panels, group-coloured source points, weighted nodes and a black PCHIP curve.

- Existing independent 0 dB peak normalization aligns signal groups. This does not calibrate transmitter offsets using overlapping observations.
- All selected directional bins contribute. At equal plotting angles, bin medians are averaged **in dB**, weighted by sample counts. This is neither the median of all raw observations nor a power-domain average.
- PCHIP (shape-preserving piecewise cubic Hermite interpolation) connects merged nodes without overshooting each interval's endpoint range. It is not statistical smoothing or a physical-model fit.
- Gaps exceeding 30 degrees are not connected by default. Change this with, for example, `--combined-max-gap 60`. The azimuth 0/360-degree seam is supported.
- There is no extrapolation beyond observed arcs. Isolated points remain points, and missing coverage is not forced into a closed curve.
- `--no-combined` disables combined exports; signal-specific outputs are still generated.

Differences in frequency, transmitted power and sampled sky regions remain, so these plots are **illustrative estimates for examining shape and trends**. Unobserved group peaks can leave relative offsets. Interpolated points do not count as new measurements or increased measured coverage.

Each cut produces the following files (using `horizontal_pattern` as an example):

| File | Contents |
| --- | --- |
| `horizontal_pattern.png` | Source points and combined curve in polar and Cartesian coordinates |
| `horizontal_pattern_points.csv` | Source signal-group bins, counts, angles and relative gains |
| `horizontal_pattern_nodes.csv` | Weighted values at equal angles, total counts, source groups and between-bin spread |
| `horizontal_pattern_curve.csv` | Interpolated samples approximately every 0.5 degrees; do not join different `segment_id` values |
| `metadata.json` | Normalization, weighting, interpolation settings and per-cut counts |

In circular-cut CSVs, `plane_angle_deg` lies in [0, 360), while `unwrapped_angle_deg` preserves continuity through the seam. `between_bin_std_db` measures spread among source bin medians; it is not a confidence interval for gain.

Rebuild combined plots from an existing `pattern.csv` without repeating orbit or gas calculations:

```bash
python combined_patterns.py \
  --pattern-csv output/multignss_validation/pattern.csv \
  --output output/multignss_validation/combined \
  --max-gap 30
```

The standalone command also accepts `--cut-width`, `--azimuth-cut` and `--elevation-cut`. Defaults are 10, 0 and 45 degrees, respectively; repeat the original settings if they differed. Reference: [SciPy PchipInterpolator](https://docs.scipy.org/doc/scipy/reference/generated/scipy.interpolate.PchipInterpolator.html).

### 3D patterns for all constellations and combined data

Every retained constellation/signal group generates `pattern_3d.png`, including GPS, Galileo, GLONASS, BeiDou and QZSS. With multiple groups, look in their respective directories, for example `output/E_7/pattern_3d.png`; a single group writes to the output root. Titles identify the constellation and signal.

The default combined export additionally creates `combined/pattern_3d.png` and `combined/pattern_3d.csv`. It uses all observed 2D direction bins, independently of cut angles or widths. Bins at equal azimuth **and** elevation are averaged in dB using observation counts, retaining each group's existing 0 dB peak reference without renormalizing the merged peak. The CSV records angles, relative gain, counts, contributing groups and between-bin spread. Metadata records the 3D aggregation settings.

The default `pattern_3d.png` pairs a 3D spherical plot with a polar sky map. Azimuth is clockwise from true north and elevation runs from horizon to zenith. The 3D display radius is `(gain_dB - floor_dB) / (0 - floor_dB)`, with a floor below the minimum gain rounded down to a multiple of 10 dB (at most -30 dB). This is a graphical dB scale, not a power ratio. The sky map radius is zenith distance (`90 - elevation`) and its colour shows gain, so weak directions remain visible. Each figure reports the number of plotted bins and contributing observations.

All bins are plotted without downsampling. Raw observations have already been aggregated into direction bins; combined bins additionally merge matching azimuth/elevation across signal groups. The old `10**(gain_dB/10)` radius compressed -20 dB points to 0.01 and -30 dB points to 0.001, hiding many points near the origin. That physical power-ratio rendering remains available as `pattern_3d_linear.png`. Combined metadata records `pattern_3d.rendering.plotted_bins`, the dB floor and the absence of downsampling. They are scatter plots of observed bins; missing directions are not interpolated into a surface. The combined result retains the frequency and group-offset limitations described above. `--no-combined` disables both combined cuts and combined 3D output. The standalone `combined_patterns.py` command also regenerates the combined 3D files from an existing `pattern.csv`.

### Signal-specific outputs

When multiple groups have usable samples, their files go in directories such as `output/G_1/`, `output/E_7/`, `output/R_1/`, `output/C_1/` and `output/J_1/`. Missing IDs use names such as `G_legacy`. A single group writes directly into the output root for compatibility.

With multiple groups, root-level `samples.csv` and `pattern.csv` are concatenated listings with a `signal_group` column; they do not contain cross-group averaged gains. Metadata `groups` records each group's path, frequencies, sample count and bin count. Each group folder contains only that group's plots. Old files from other groups are not removed when reusing an output directory; use a fresh directory when changing run settings.

| File | Contents |
| --- | --- |
| samples.csv | UTC, constellation, satellite, signal ID/group, frequency, reference C/N0, position, range, azimuth/elevation, theta/phi, measured/model C/N0 and residual |
| pattern.csv | Azimuth/elevation bin centres, theta/phi, counts, median residual, standard deviation and peak-normalized relative gain |
| horizontal_pattern.csv / horizontal_pattern.png | Azimuth pattern near 15-degree elevation |
| vertical_ns_pattern.csv / vertical_ns_pattern.png | North–zenith–south upper vertical plane |
| vertical_ew_pattern.csv / vertical_ew_pattern.png | East–zenith–west upper vertical plane |
| azimuth_cut.csv / azimuth_cut.png | Azimuth pattern near 45-degree elevation by default |
| elevation_cut.csv / elevation_cut.png | Elevation pattern near 0-degree azimuth by default |
| theta_cut.png | Theta cut near a fixed phi |
| phi_cut.png | Phi cut near a fixed theta |
| pattern_3d.png | Spherical dB-radius pattern and polar sky coverage map, with plotted bin count |
| pattern_3d_linear.png | Legacy 3D scatter with relative power ratio as radius |
| metadata.json | Run settings, accepted/rejected counts, model assumptions and limitations |

`--bin-deg 5` sets the directional bin width, which must divide 90 degrees exactly. The default principal cuts are the **15-degree elevation horizontal cut and the north–south/east–west vertical planes**. The north–south plane combines azimuths 0 and 180 degrees; east–west combines 90 and 270 degrees. Vertical CSV `side` values are N/S/E/W. `plane_angle_deg` is 0 at the north/east horizon, 90 at zenith and 180 at the south/west horizon. Horizontal rows use `side=horizontal` and azimuth as `plane_angle_deg`. All cuts retain their group's full-pattern 0 dB peak reference. Azimuth runs clockwise from true north; elevation runs from the horizon to zenith.

**Horizontal-cut coverage:** default `--cut-width 10` selects bin centres at elevation 15 ± 5 degrees (10–20 degrees), compatible with the default 10-degree mask and gas model. Missing observations produce an explicit empty-plot message and a header-only CSV. This is an azimuth cut near 15-degree elevation, not the horizon at 0 degrees. `--cut-width` changes the selection band's full width.

Additional arbitrary cuts use `--azimuth-cut 0 --elevation-cut 45 --cut-width 10` by default, selecting bin centres within 5 degrees of the requested plane. `--azimuth-cut` fixes azimuth for the elevation plot; `--elevation-cut` fixes elevation for the azimuth plot. For example, `--azimuth-cut 90 --elevation-cut 30` creates an east-facing elevation cut and an azimuth cut at 30-degree elevation. These angle options do not rotate the fixed principal planes.

Cuts select existing 2D bins rather than averaging over all values of the other angle. All use the full-pattern peak for normalization, and empty CSVs retain headers. Legacy theta/phi plots are also exported: `--phi-cut` aliases `--azimuth-cut`, while `--theta-cut` specifies `90 - elevation` and cannot be combined with `--elevation-cut`. Signal-specific plots do not interpolate unobserved regions. Existing output files with the same names are overwritten.

## Validation

```bash
python3 -m unittest discover -s tests -v
```

Synthetic data tests cover satellite numbering, time scales and frequencies for five constellations, the BeiDou GEO transformation, GLONASS comparison against an independent integrator, and separate constellation/signal exports. Positioning-level validation against external precise orbits remains separate work.

Specification references: [IGS RINEX 3.05](https://files.igs.org/pub/data/format/rinex305.pdf), [Trimble GSV](https://receiverhelp.trimble.com/alloy-gnss/en-us/NMEA-0183messages_GSV.html). Signal IDs: [DATAGNSS NMEA 4.11](https://docs.datagnss.com/common/common_protocol_nmea/). Orbit calculations: [ESA GNSS satellite coordinates](https://gssc.esa.int/navipedia/index.php/Computation_of_GNSS_Satellite_Coordinates), [RTKLIB broadcast ephemeris implementation](https://github.com/tomojitakasu/RTKLIB/blob/master/src/ephemeris.c).

## Contact

For bug reports, feature requests and usage questions, please open a [GitHub issue](https://github.com/kh19440807/gnss-antenna-pattern/issues).

For collaboration or private inquiries, contact [alice.higuchi@trident-global.net](mailto:alice.higuchi@trident-global.net).

NMEA logs contain receiver location information. Remove location information before attaching logs to a public issue, or contact us by email to discuss sharing them privately.

## License

This project's code and accompanying documentation are licensed under the [MIT License](LICENSE). Copyright (c) 2026 kh19440807.

Commercial use, modification and redistribution are permitted subject to retaining the copyright and license notices. The software is provided without warranty; see `LICENSE` for the full terms. Third-party dependencies and external data remain subject to their respective licenses and terms.
