"""
Roof-plane extraction from a building's LiDAR points via RANSAC (Open3D).

Pure with respect to the pipeline — takes points + config, returns ``RoofPlane``
objects. Open3D and SciPy are imported lazily so the rest of the package imports
without them.
"""

from __future__ import annotations

import numpy as np

from .config import Config
from .models import RoofPlane

# Surfaces steeper than this are walls/dormers, not paneling candidates.
_MAX_TILT_DEG = 72.0
# Roughly the maximum number of distinct faces on a residential roof.
_MAX_PLANES = 6


def _ground_density(points_xyz: np.ndarray) -> float:
    """Points per m² of horizontal area, from ONE convex hull over all points.

    Sizing each plane by ``point_count / density`` avoids the area inflation you get
    from summing overlapping per-plane hulls: every point belongs to exactly one
    plane, so the summed areas can't exceed the building's real footprint.
    """
    from scipy.spatial import ConvexHull  # lazy: SciPy is heavy-ish

    all_xy = np.asarray(points_xyz)[:, :2]
    try:
        covered_m2 = float(ConvexHull(all_xy).volume) if len(all_xy) >= 3 else 0.0
    except Exception:
        covered_m2 = 0.0
    return (len(points_xyz) / covered_m2) if covered_m2 > 0 else 0.0


def extract_roof_planes(points_xyz: np.ndarray, cfg: Config) -> list[RoofPlane]:
    """Fit up to a handful of roof planes to an (N, 3) point cloud (tile meters).

    Iteratively segments the dominant plane, records its tilt/azimuth/area, removes
    its inliers, and repeats. Near-vertical and tiny planes are discarded.
    """
    import open3d as o3d  # lazy: Open3D is a large optional dependency

    pcd = o3d.geometry.PointCloud()
    pcd.points = o3d.utility.Vector3dVector(points_xyz)

    ground_density = _ground_density(points_xyz)

    planes: list[RoofPlane] = []
    remaining = pcd

    for _ in range(_MAX_PLANES):
        if len(remaining.points) < cfg.min_roof_points:
            break

        plane_model, inliers = remaining.segment_plane(
            distance_threshold=cfg.ransac_distance_threshold,
            ransac_n=3,
            num_iterations=cfg.ransac_iterations,
        )
        if len(inliers) < cfg.min_roof_points:
            break

        # Plane equation ax + by + cz + d = 0 → normal = [a, b, c]; force it upward.
        normal = np.array(plane_model[:3])
        if normal[2] < 0:
            normal = -normal
        norm_len = np.linalg.norm(normal)
        if norm_len == 0:
            remaining = remaining.select_by_index(inliers, invert=True)
            continue
        nx, ny, nz = normal / norm_len

        tilt_deg = float(np.degrees(np.arccos(min(nz, 1.0))))
        if tilt_deg > _MAX_TILT_DEG:
            remaining = remaining.select_by_index(inliers, invert=True)
            continue

        # Compass azimuth: +X=east, +Y=north → atan2(nx, ny), 0=N, 90=E, 180=S.
        azimuth_deg = float((np.degrees(np.arctan2(nx, ny)) + 360) % 360)

        # Horizontal area from point count ÷ density, lifted to the tilted surface.
        if ground_density > 0:
            ground_area_m2 = len(inliers) / ground_density
            area_m2 = ground_area_m2 / max(np.cos(np.radians(tilt_deg)), 0.1)
        else:
            area_m2 = 0.0

        planes.append(RoofPlane(tilt_deg=tilt_deg, azimuth_deg=azimuth_deg,
                                area_m2=area_m2, point_count=len(inliers)))
        remaining = remaining.select_by_index(inliers, invert=True)

    return planes
