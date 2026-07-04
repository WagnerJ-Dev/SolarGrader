"""
Solar energy model — pure functions over roof planes and irradiance.

Two systems are reported per roof: the *maximum* (every panel that fits) and the
*residential* (best-producing panels up to the kW cap — the sellable system). If a
LiDAR-derived skyline is supplied, the direct beam is removed during hours the sun
sits below an obstruction, and the lost fraction is reported as ``shade_loss_pct``.
"""

from __future__ import annotations

import numpy as np

from .config import Config
from .geometry import TilePoints
from .models import RoofPlane, SystemResult


def panels_on_plane(plane_area_m2: float, cfg: Config) -> int:
    """Whole panels that physically fit on a roof plane of this area.

    The plane is approximated as an equal-area square, eroded inward by the fire-code
    setback on all sides, packed at ``panel_packing``, then divided by module area.
    Small/cut-up roofs lose proportionally more — behaviour a flat usable-% can't
    capture.
    """
    side = np.sqrt(plane_area_m2)
    usable_side = max(0.0, side - 2 * cfg.roof_setback_m)
    usable_area = (usable_side ** 2) * cfg.panel_packing
    return int(usable_area // cfg.panel_area_m2)


def compute_horizon(tile_points: TilePoints, observer_xyz, cfg: Config,
                    exclude_poly=None) -> np.ndarray:
    """Build the roof's skyline: the maximum obstruction elevation (degrees above the
    roof) per compass direction, from surrounding LiDAR points (trees, neighbours)
    within ``shade_radius_m``. Points inside ``exclude_poly`` (the building's OWN
    footprint, in tile CRS) are dropped so a roof can't shade itself. Returns an
    array indexed by azimuth bin (0=N, clockwise); all zeros = wide-open sky.
    """
    import shapely  # lazy: only needed when excluding the own footprint

    pts, X, Y = tile_points
    ox, oy, oz = observer_xyz
    n_bins = cfg.horizon_bins
    radius_m = cfg.shade_radius_m
    horizon = np.zeros(n_bins)

    m = ((X >= ox - radius_m) & (X <= ox + radius_m)
         & (Y >= oy - radius_m) & (Y <= oy + radius_m))
    if not m.any():
        return horizon

    sx, sy = X[m], Y[m]
    sz = np.asarray(pts["Z"], dtype=float)[m]
    if exclude_poly is not None:
        outside = ~shapely.contains_xy(exclude_poly, sx, sy)
        sx, sy, sz = sx[outside], sy[outside], sz[outside]

    dx, dy, dz = sx - ox, sy - oy, sz - oz
    horiz = np.sqrt(dx * dx + dy * dy)
    keep = (horiz <= radius_m) & (horiz > 1.0) & (dz > 0)   # within radius, above roof
    if not keep.any():
        return horizon

    dx, dy, dz, horiz = dx[keep], dy[keep], dz[keep], horiz[keep]
    elev = np.degrees(np.arctan2(dz, horiz))                 # elevation angle
    az = (np.degrees(np.arctan2(dx, dy)) + 360) % 360        # 0=N, 90=E (compass)
    bins = (az / (360.0 / n_bins)).astype(int) % n_bins
    np.maximum.at(horizon, bins, elev)                       # tallest per direction
    return horizon


def annual_energy(lat: float, lon: float, tmy, planes: list[RoofPlane], cfg: Config,
                  horizon: np.ndarray | None = None) -> SystemResult | None:
    """Pack panels onto every viable plane, compute plane-of-array irradiance, and
    return the residential + maximum systems (or ``None`` if no plane is viable).
    """
    import pvlib  # lazy: pvlib pulls pandas machinery

    location = pvlib.location.Location(latitude=lat, longitude=lon, tz="Etc/GMT+5")
    solar_pos = location.get_solarposition(tmy.index)

    # Per-hour direct-beam shading mask: sun up but below the roof's skyline.
    sun_blocked = None
    if horizon is not None:
        sun_elev = 90.0 - solar_pos["apparent_zenith"].values
        sun_az = solar_pos["azimuth"].values
        sun_bins = (sun_az / (360.0 / len(horizon))).astype(int) % len(horizon)
        sun_blocked = (sun_elev > 0) & (sun_elev <= horizon[sun_bins])

    viable = []   # (per_panel_shaded, per_panel_unshaded, n_panels, plane)
    for plane in planes:
        if abs(plane.azimuth_deg - 180) > cfg.max_azimuth_offset:
            continue
        n_panels = panels_on_plane(plane.area_m2, cfg)
        if n_panels <= 0:
            continue

        poa = pvlib.irradiance.get_total_irradiance(
            surface_tilt=plane.tilt_deg,
            surface_azimuth=plane.azimuth_deg,
            solar_zenith=solar_pos["apparent_zenith"],
            solar_azimuth=solar_pos["azimuth"],
            ghi=tmy["ghi"], dni=tmy["dni"], dhi=tmy["dhi"],
        )
        glob = poa["poa_global"].fillna(0).values
        unshaded_annual = glob.sum() / 1000  # kWh/m²/yr, full sun
        if sun_blocked is not None:
            direct = poa["poa_direct"].fillna(0).values
            shaded_annual = (glob - direct * sun_blocked).sum() / 1000
        else:
            shaded_annual = unshaded_annual

        per_kwh = cfg.panel_area_m2 * cfg.module_efficiency * cfg.system_losses
        viable.append((shaded_annual * per_kwh, unshaded_annual * per_kwh, n_panels, plane))

    if not viable:
        return None

    # Max system: every panel that physically fits the roof.
    max_panels = sum(n for _, _, n, _ in viable)
    max_kwh = sum(s * n for s, _, n, _ in viable)
    max_unshaded = sum(u * n for _, u, n, _ in viable)
    max_kw = max_panels * cfg.panel_watts / 1000.0
    shade_loss_pct = 100.0 * (1 - max_kwh / max_unshaded) if max_unshaded > 0 else 0.0

    # Residential system: fill the best-producing panels first, up to the kW cap.
    cap_panels = cfg.residential_panel_cap
    res_panels = 0
    res_kwh = 0.0
    for ppk_shaded, _, n_panels, _ in sorted(viable, key=lambda v: v[0], reverse=True):
        take = min(n_panels, cap_panels - res_panels)
        if take <= 0:
            break
        res_panels += take
        res_kwh += take * ppk_shaded
    res_kw = res_panels * cfg.panel_watts / 1000.0

    best_plane = max(viable, key=lambda v: v[0])[3]
    return SystemResult(
        res_kwh=res_kwh, res_kw=res_kw, res_panels=res_panels,
        max_kwh=max_kwh, max_kw=max_kw, max_panels=max_panels,
        shade_loss_pct=shade_loss_pct, best_plane=best_plane,
    )
