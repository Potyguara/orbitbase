from __future__ import annotations

from math import atan2, cos, radians, sin, sqrt, degrees

WGS84_A = 6378137.0
WGS84_F = 1 / 298.257223563
WGS84_E2 = WGS84_F * (2 - WGS84_F)


def parse_approx_xyz(xyz_text: str) -> tuple[float, float, float]:
    if not xyz_text:
        raise ValueError("Texto APPROX POSITION XYZ vazio.")

    parts = xyz_text.split()
    if len(parts) < 3:
        raise ValueError(f"APPROX POSITION XYZ inválido: {xyz_text}")

    x = float(parts[0])
    y = float(parts[1])
    z = float(parts[2])
    return x, y, z


def ecef_to_geodetic(x: float, y: float, z: float) -> tuple[float, float, float]:
    a = WGS84_A
    f = WGS84_F
    e2 = WGS84_E2
    b = a * (1 - f)
    ep2 = (a**2 - b**2) / b**2

    p = sqrt(x * x + y * y)
    th = atan2(a * z, b * p)

    lon = atan2(y, x)
    lat = atan2(
        z + ep2 * b * sin(th) ** 3,
        p - e2 * a * cos(th) ** 3,
    )

    n = a / sqrt(1 - e2 * sin(lat) ** 2)
    h = p / cos(lat) - n

    lat_deg = degrees(lat)
    lon_deg = degrees(lon)
    return lat_deg, lon_deg, h


def geodetic_to_ecef(
    lat_deg: float, lon_deg: float, h: float
) -> tuple[float, float, float]:
    lat = radians(lat_deg)
    lon = radians(lon_deg)

    a = WGS84_A
    e2 = WGS84_E2

    n = a / sqrt(1 - e2 * sin(lat) ** 2)

    x = (n + h) * cos(lat) * cos(lon)
    y = (n + h) * cos(lat) * sin(lon)
    z = (n * (1 - e2) + h) * sin(lat)

    return x, y, z


def geodetic_distance(
    lat1_deg: float,
    lon1_deg: float,
    lat2_deg: float,
    lon2_deg: float,
) -> float:
    # aproximação plana suficiente para seleção relativa de base
    lat1 = radians(lat1_deg)
    lon1 = radians(lon1_deg)
    lat2 = radians(lat2_deg)
    lon2 = radians(lon2_deg)

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    mean_lat = (lat1 + lat2) / 2.0
    r = 6371000.0

    x = dlon * cos(mean_lat) * r
    y = dlat * r
    return sqrt(x * x + y * y)
