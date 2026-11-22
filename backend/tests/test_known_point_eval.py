import math

from tools.vrs_known_point_eval import IF_A, IF_B, L1_WL, L2_WL, compare

MARKER = (4005474.1188, -4963530.7910, 5200.8584)
SATS = {
    "G05": (15_600_000.0, -10_200_000.0, 19_100_000.0),
    "G07": (20_100_000.0, -18_400_000.0, -9_300_000.0),
    "G12": (19_800_000.0, -21_500_000.0, 9_900_000.0),
    "G15": (24_000_000.0, -9_100_000.0, 8_700_000.0),
    "G20": (11_500_000.0, -18_000_000.0, -15_000_000.0),
}


def _obs(rx, sat, clk):
    rho = math.dist(rx, sat) + clk
    return {"C1": rho, "P2": rho, "L1": rho / L1_WL, "L2": rho / L2_WL}


def test_compare_recovers_position_offset():
    offset = (0.3, -0.2, 0.5)
    vrs_rx = tuple(MARKER[i] + offset[i] for i in range(3))
    vrs, real, cache = {}, {}, {}
    for k in range(30):
        key = f"2026-08-20T10:00:{k:02d}"
        sats = {s: (x + 900.0 * k, y - 500.0 * k, z + 300.0 * k) for s, (x, y, z) in SATS.items()}
        cache[key] = {s: {"x": p[0], "y": p[1], "z": p[2]} for s, p in sats.items()}
        vrs[key] = {s: _obs(vrs_rx, p, 10.0 + k) for s, p in sats.items()}
        real[key] = {s: _obs(MARKER, p, -3.0) for s, p in sats.items()}

    res = compare(vrs, real, {"antenna_delta_hen": [0, 0, 0]}, MARKER, cache, min_elev=-90)
    assert res["epochs_compared"] == 30
    assert res["code_rms_m"] < 1e-3
    bias = res["code_bias_enu_m"]
    assert math.sqrt(sum(b * b for b in bias)) - math.sqrt(sum(o * o for o in offset)) < 1e-2
    assert res["phase_slips"] == 0
