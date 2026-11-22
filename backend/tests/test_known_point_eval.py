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


def test_phase_position_bias_recovers_offset():
    import math
    from tools.vrs_known_point_eval import phase_position_bias

    dx = (0.05, -0.12, 0.30)
    keys = [f"t{i:04d}" for i in range(240)]
    rows = []
    for i, key in enumerate(keys):
        clock = 3.0 + 0.01 * i
        for s in range(7):
            az = 0.9 * s + 0.004 * i
            el = 0.3 + 0.12 * s + 0.002 * i * (1 if s % 2 else -1)
            u = (math.cos(el) * math.cos(az), math.cos(el) * math.sin(az), math.sin(el))
            amb = 10.0 * s + 0.37
            sd = -(u[0] * dx[0] + u[1] * dx[1] + u[2] * dx[2]) + clock + amb
            rows.append((key, f"G{s:02d}", sd, u))
    out = phase_position_bias(rows, set(), keys, iterations=60)
    assert out is not None
    for a, b in zip(out["dx"], dx):
        assert abs(a - b) < 0.01
