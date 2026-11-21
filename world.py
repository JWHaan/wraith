"""Static map model: height grid, line of sight, cover scores and an incremental flow field."""
import json
import math
import os
import time
from array import array
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))


class World:
    def __init__(self, path=os.path.join(HERE, "grid.bin")):
        self.ok = False
        try:
            with open(path, "rb") as f:
                hdr = json.loads(f.readline())
                self.x0, self.y0, self.cell = hdr["x0"], hdr["y0"], hdr["cell"]
                self.nx, self.ny = hdr["nx"], hdr["ny"]
                n = self.nx * self.ny
                g = array("h"); g.frombytes(f.read(n * 2))
                s = array("h"); s.frombytes(f.read(n * 2))
                self.solid = f.read(n)
            self.ground = [v / 100.0 for v in g]
            self.surf = [v / 100.0 for v in s]
            self.ok = True
        except Exception:
            self.x0, self.y0, self.cell, self.nx, self.ny = -512.0, -333.0, 2.0, 534, 361
            n = self.nx * self.ny
            self.ground = [0.0] * n
            self.surf = [0.0] * n
            self.solid = bytes(n)
        self._build_coarse()

    # ---- fine grid -------------------------------------------------------------
    def idx(self, x, y):
        i = int((x - self.x0) / self.cell)
        j = int((y - self.y0) / self.cell)
        if i < 0: i = 0
        elif i >= self.nx: i = self.nx - 1
        if j < 0: j = 0
        elif j >= self.ny: j = self.ny - 1
        return j * self.nx + i

    def ground_at(self, x, y):
        return self.ground[self.idx(x, y)]

    def surf_at(self, x, y):
        return self.surf[self.idx(x, y)]

    def is_roof(self, x, y):
        k = self.idx(x, y)
        return self.surf[k] - self.ground[k] > 1.5

    def los(self, a, b, step=2.5):
        """True if the segment a->b (3D) clears buildings and terrain."""
        dx, dy, dz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        d = math.hypot(dx, dy)
        if d < 4:
            return True
        n = int(d / step)
        surf, idx = self.surf, self.idx
        for k in range(1, n):
            f = k / n
            if surf[idx(a[0] + dx * f, a[1] + dy * f)] > a[2] + dz * f + 0.3:
                return False
        return True

    # ---- coarse grid (4 m) for navigation & cover --------------------------------
    def _build_coarse(self):
        C = 2
        self.cnx, self.cny = (self.nx + 1) // C, (self.ny + 1) // C
        self.ccell = self.cell * C
        h = [0.0] * (self.cnx * self.cny)
        solidc = bytearray(self.cnx * self.cny)
        for cj in range(self.cny):
            for ci in range(self.cnx):
                m = -1e9
                sol = 0
                for dj in range(C):
                    j = cj * C + dj
                    if j >= self.ny: continue
                    for di in range(C):
                        i = ci * C + di
                        if i >= self.nx: continue
                        k = j * self.nx + i
                        if self.surf[k] > m: m = self.surf[k]
                        if self.solid[k]: sol = 1
                h[cj * self.cnx + ci] = m
                solidc[cj * self.cnx + ci] = sol
        self.ch = h
        self.csolid = solidc
        # cover score: building cells within ~12 m
        R = 3
        cover = [0] * len(h)
        cnx, cny = self.cnx, self.cny
        for cj in range(cny):
            for ci in range(cnx):
                if solidc[cj * cnx + ci]:
                    continue
                c = 0
                for dj in range(-R, R + 1):
                    j = cj + dj
                    if j < 0 or j >= cny: continue
                    row = j * cnx
                    for di in range(-R, R + 1):
                        i = ci + di
                        if 0 <= i < cnx and solidc[row + i]:
                            c += 1
                cover[cj * cnx + ci] = c
        self.cover = cover
        # neighbour offsets
        self.nbrs = [(1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
                     (1, 1, 1.41), (1, -1, 1.41), (-1, 1, 1.41), (-1, -1, 1.41)]
        self.flow = None

    def cidx(self, x, y):
        i = int((x - self.x0) / self.ccell)
        j = int((y - self.y0) / self.ccell)
        i = min(max(i, 0), self.cnx - 1)
        j = min(max(j, 0), self.cny - 1)
        return j * self.cnx + i

    def ccenter(self, k):
        j, i = divmod(k, self.cnx)
        return (self.x0 + (i + 0.5) * self.ccell, self.y0 + (j + 0.5) * self.ccell)

    def cover_at(self, x, y):
        return self.cover[self.cidx(x, y)]

    # ---- incremental BFS flow field --------------------------------------------
    def start_flow(self, gx, gy, gr):
        """Begin a new distance field toward the disc (gx, gy, gr). Old field stays usable until done."""
        INF = 1e9
        dist = [INF] * (self.cnx * self.cny)
        q = deque()
        cc, cnx = self.ccell, self.cnx
        cj0 = int((gy - gr - self.y0) / cc); cj1 = int((gy + gr - self.y0) / cc) + 1
        for cj in range(max(0, cj0), min(self.cny, cj1 + 1)):
            cy = self.y0 + (cj + 0.5) * cc
            h2 = gr * gr - (cy - gy) ** 2
            if h2 < 0:
                continue
            h = math.sqrt(h2)
            i0 = max(0, int(math.ceil((gx - h - self.x0) / cc - 0.5)))
            i1 = min(cnx - 1, int(math.floor((gx + h - self.x0) / cc - 0.5)))
            if i1 < i0:
                continue
            row = cj * cnx
            for k in range(row + i0, row + i1 + 1):
                dist[k] = 0.0
            q.extend(range(row + i0, row + i1 + 1))
        if not q:
            k = self.cidx(gx, gy)
            dist[k] = 0.0
            q.append(k)
        self.pending = (dist, q, (gx, gy, gr))

    def step_flow(self, budget_s):
        if getattr(self, "pending", None) is None:
            return
        dist, q, goal = self.pending
        h, cnx, cny = self.ch, self.cnx, self.cny
        end = time.perf_counter() + budget_s
        n = 0
        while q:
            u = q.popleft()
            du = dist[u]
            uj, ui = divmod(u, cnx)
            hu = h[u]
            for di, dj, c in self.nbrs:
                i, j = ui + di, uj + dj
                if i < 0 or j < 0 or i >= cnx or j >= cny:
                    continue
                v = j * cnx + i
                # moving v -> u must be walkable: climbing at most ~2 m per 4 m cell
                dh = hu - h[v]
                if dh > 2.2:
                    continue
                nd = du + c + (0.5 if dh < -6 else 0.0)
                if nd < dist[v]:
                    dist[v] = nd
                    q.append(v)
            n += 1
            if (n & 255) == 0 and time.perf_counter() > end:
                return
        self.flow = (dist, goal)
        self.pending = None

    def flow_dir(self, x, y):
        """Unit direction along the flow field, or None."""
        if self.flow is None:
            return None
        dist, _ = self.flow
        cnx, cny = self.cnx, self.cny
        k = self.cidx(x, y)
        kj, ki = divmod(k, cnx)
        best, bk = dist[k], None
        # look two rings out for a smoother direction
        for dj in range(-2, 3):
            for di in range(-2, 3):
                if di == 0 and dj == 0:
                    continue
                i, j = ki + di, kj + dj
                if i < 0 or j < 0 or i >= cnx or j >= cny:
                    continue
                v = j * cnx + i
                dv = dist[v] + 0.15 * math.hypot(di, dj)
                if dv < best - 0.01:
                    # require the straight path to be monotone (avoid cutting corners through walls)
                    if abs(di) == 2 or abs(dj) == 2:
                        mid = (kj + dj // 2 if abs(dj) == 2 else kj + dj) * cnx + (ki + di // 2 if abs(di) == 2 else ki + di)
                        if dist[mid] >= 1e8 or self.ch[mid] - self.ch[k] > 2.2:
                            continue
                    best, bk = dv, v
        if bk is None:
            return None
        cx, cy = self.ccenter(bk)
        dx, dy = cx - x, cy - y
        d = math.hypot(dx, dy)
        if d < 1e-6:
            return None
        return (dx / d, dy / d)

    def flow_dist(self, x, y):
        if self.flow is None:
            return None
        return self.flow[0][self.cidx(x, y)]
