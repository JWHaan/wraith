"""Precompute a 2 m height grid of the arena (ground + solid buildings) for the bot.

Run:  python3 tools/build_grid.py <arena_dir> grid.bin
Output: header (json line) + int16 cm arrays: ground, surface (ground or roof top), flags.
"""
import json
import sys

import numpy as np
from matplotlib.path import Path

CELL = 2.0


def main(arena, out):
    w = json.load(open(f"{arena}/v2/world.json"))
    t = w["terrain"]
    terr = np.fromfile(f"{arena}/v2/terrain.i16", dtype="<i2").reshape(t["ny"], t["nx"]).astype(np.float32) * t["scale"]
    b = w["bounds"]
    x0, y0 = b["minX"], b["minY"]
    nx = int((b["maxX"] - x0) / CELL) + 1
    ny = int((b["maxY"] - y0) / CELL) + 1
    xs = x0 + (np.arange(nx) + 0.5) * CELL
    ys = y0 + (np.arange(ny) + 0.5) * CELL
    gx, gy = np.meshgrid(xs, ys)
    # bilinear ground sample
    fi = np.clip((gx - t["minX"]) / t["step"], 0, t["nx"] - 1.001)
    fj = np.clip((gy - t["minY"]) / t["step"], 0, t["ny"] - 1.001)
    i0, j0 = fi.astype(int), fj.astype(int)
    di, dj = fi - i0, fj - j0
    ground = (terr[j0, i0] * (1 - di) * (1 - dj) + terr[j0, i0 + 1] * di * (1 - dj)
              + terr[j0 + 1, i0] * (1 - di) * dj + terr[j0 + 1, i0 + 1] * di * dj)
    surface = ground.copy()
    solid = np.zeros_like(ground, dtype=bool)  # cell holds a building wall/roof
    pts = np.stack([gx.ravel(), gy.ravel()], axis=1)
    for p in w["prisms"]:
        ring = np.array(p["ring"], dtype=float).reshape(-1, 2)
        lo, hi = ring.min(0), ring.max(0)
        ci0 = max(0, int((lo[0] - x0) / CELL) - 1); ci1 = min(nx, int((hi[0] - x0) / CELL) + 2)
        cj0 = max(0, int((lo[1] - y0) / CELL) - 1); cj1 = min(ny, int((hi[1] - y0) / CELL) + 2)
        if ci1 <= ci0 or cj1 <= cj0:
            continue
        sub = np.stack([gx[cj0:cj1, ci0:ci1].ravel(), gy[cj0:cj1, ci0:ci1].ravel()], axis=1)
        inside = Path(ring).contains_points(sub, radius=0.5)
        for h in p.get("holes") or []:
            hr = np.array(h, dtype=float).reshape(-1, 2)
            if len(hr) >= 3:
                inside &= ~Path(hr).contains_points(sub)
        inside = inside.reshape(cj1 - cj0, ci1 - ci0)
        g = ground[cj0:cj1, ci0:ci1]
        if p["kind"] == "bridge" and p["base"] - g.mean() > 9.5:
            continue  # walk under
        s = surface[cj0:cj1, ci0:ci1]
        top = np.full_like(s, p["top"])
        upd = inside & (top > s)
        s[upd] = top[upd]
        if p["kind"] != "bridge":
            solid[cj0:cj1, ci0:ci1] |= inside
    hdr = {"x0": x0, "y0": y0, "cell": CELL, "nx": nx, "ny": ny}
    with open(out, "wb") as f:
        f.write((json.dumps(hdr) + "\n").encode())
        f.write(np.round(ground * 100).astype("<i2").tobytes())
        f.write(np.round(surface * 100).astype("<i2").tobytes())
        f.write(solid.astype(np.uint8).tobytes())
    print(hdr, "buildings cells:", int(solid.sum()), "max roof", float((surface - ground).max()))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
