"""Precompute an exposure map on the 4 m grid: for each cell, cast 24 rays from gun height (surface + 7 m)
out to 200 m, stop at the first terrain or building above the ray, and store the mean reach / 200 as uint8.
The measure follows giddygiddyboy's MIT bot (see NOTICE.md). Run: python3 tools/build_exposure.py
"""
import json
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(out=os.path.join(ROOT, "exposure.bin")):
    with open(os.path.join(ROOT, "grid.bin"), "rb") as f:
        h = json.loads(f.readline())
        n = h["nx"] * h["ny"]
        f.read(n * 2)  # ground
        surf = np.frombuffer(f.read(n * 2), "<i2").reshape(h["ny"], h["nx"]).astype(np.float32) / 100
    x0, y0, cell, nx, ny = h["x0"], h["y0"], h["cell"], h["nx"], h["ny"]
    C = 2
    cnx, cny = (nx + 1) // C, (ny + 1) // C
    cc = cell * C
    xs = x0 + (np.arange(cnx) + 0.5) * cc
    ys = y0 + (np.arange(cny) + 0.5) * cc
    gx, gy = np.meshgrid(xs, ys)

    def sample(px, py):
        i = np.clip(((px - x0) / cell).astype(int), 0, nx - 1)
        j = np.clip(((py - y0) / cell).astype(int), 0, ny - 1)
        return surf[j, i], (px < x0) | (py < y0) | (px > x0 + nx * cell) | (py > y0 + ny * cell)
    z, _ = sample(gx, gy)
    z = z + 7.0
    total = np.zeros_like(gx)
    for k in range(24):
        a = k * np.pi / 12
        c, s = np.cos(a), np.sin(a)
        reach = np.full(gx.shape, 200.0, dtype=np.float32)
        alive = np.ones(gx.shape, dtype=bool)
        d = 2.5
        while d < 200:
            sv, oob = sample(gx + c * d, gy + s * d)
            hit = alive & ((sv > z) | oob)
            reach[hit] = d
            alive &= ~hit
            d += 2.5
        total += reach
    expo = total / (24 * 200)
    hdr = {"cnx": int(cnx), "cny": int(cny), "scale": 255}
    with open(out, "wb") as f:
        f.write((json.dumps(hdr) + "\n").encode())
        f.write(np.clip(np.round(expo * 255), 0, 255).astype(np.uint8).tobytes())
    print(hdr, "mean exposure %.2f" % float(expo.mean()), "bytes", cnx * cny)


if __name__ == "__main__":
    main(*sys.argv[1:])
