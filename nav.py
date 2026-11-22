"""Navigation layer on wraith's 2 m height grid: blocked cells, clearance, main walkable component,
A* paths, straight-line walkability and cover search.

The rules (2.2 m climb per step, 3.1 m on diagonals, standing clearance, main-component escape) follow
the public MIT-licensed giddygiddyboy bot (see NOTICE.md); this is an independent Python implementation.
"""
import heapq
import math
from collections import deque

SQ2 = math.sqrt(2.0)
NBRS = [(1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
        (1, 1, SQ2), (1, -1, SQ2), (-1, 1, SQ2), (-1, -1, SQ2)]
STEP_UP = 2.2
STEP_UP_DIAG = 3.1


class Nav:
    def __init__(self, world):
        W = self.W = world
        self.nx, self.ny, self.cell = W.nx, W.ny, W.cell
        self.x0, self.y0 = W.x0, W.y0
        nx, ny = self.nx, self.ny
        n = nx * ny
        solid = W.solid
        self.surf = W.surf
        # blocked: a building cell, or one next to it (mech radius 2.5 m on a 2 m grid), or the map edge
        blocked = bytearray(n)
        for j in range(ny):
            row = j * nx
            for i in range(nx):
                k = row + i
                if solid[k]:
                    blocked[k] = 1
                    continue
                if i < 2 or j < 2 or i >= nx - 2 or j >= ny - 2:
                    blocked[k] = 1
                    continue
                if (solid[k - 1] or solid[k + 1] or solid[k - nx] or solid[k + nx] or
                        solid[k - nx - 1] or solid[k - nx + 1] or solid[k + nx - 1] or solid[k + nx + 1]):
                    blocked[k] = 1
        self.blocked = blocked
        # clearance (cells to the nearest blocked cell), capped at 6
        clear = bytearray([6]) * n
        q = deque()
        for k in range(n):
            if blocked[k]:
                clear[k] = 0
                q.append(k)
        while q:
            k = q.popleft()
            c = clear[k]
            if c >= 5:
                continue
            j, i = divmod(k, nx)
            for di, dj, _ in NBRS[:4]:
                ii, jj = i + di, j + dj
                if 0 <= ii < nx and 0 <= jj < ny:
                    v = jj * nx + ii
                    if clear[v] > c + 1:
                        clear[v] = c + 1
                        q.append(v)
        self.clear = clear
        # connected components over walkable steps (undirected: climbable both ways)
        comp = [-1] * n
        sizes = []
        cid = 0
        surf = self.surf
        for s in range(n):
            if blocked[s] or comp[s] >= 0:
                continue
            comp[s] = cid
            q = deque([s])
            size = 0
            while q:
                k = q.popleft()
                size += 1
                j, i = divmod(k, nx)
                hk = surf[k]
                for di, dj, _ in NBRS[:4]:
                    ii, jj = i + di, j + dj
                    if 0 <= ii < nx and 0 <= jj < ny:
                        v = jj * nx + ii
                        if comp[v] < 0 and not blocked[v] and abs(surf[v] - hk) <= STEP_UP:
                            comp[v] = cid
                            q.append(v)
            sizes.append(size)
            cid += 1
        self.comp = comp
        self.main = max(range(len(sizes)), key=lambda c: sizes[c]) if sizes else -1

    # ---- cells -------------------------------------------------------------
    def cellof(self, x, y):
        i = int((x - self.x0) / self.cell)
        j = int((y - self.y0) / self.cell)
        i = 0 if i < 0 else (self.nx - 1 if i >= self.nx else i)
        j = 0 if j < 0 else (self.ny - 1 if j >= self.ny else j)
        return j * self.nx + i

    def cellpos(self, k):
        j, i = divmod(k, self.nx)
        return self.x0 + (i + 0.5) * self.cell, self.y0 + (j + 0.5) * self.cell

    def free(self, x, y):
        return not self.blocked[self.cellof(x, y)]

    def walk_at(self, x, y):
        return self.surf[self.cellof(x, y)]

    def clearance(self, x, y):
        return self.clear[self.cellof(x, y)]

    def nearest_free(self, k, max_r=8):
        if not self.blocked[k]:
            return k
        nx = self.nx
        j0, i0 = divmod(k, nx)
        for r in range(1, max_r + 1):
            best, bd = -1, 1e18
            for dj in range(-r, r + 1):
                for di in (-r, r) if abs(dj) != r else range(-r, r + 1):
                    i, j = i0 + di, j0 + dj
                    if 0 <= i < nx and 0 <= j < self.ny:
                        v = j * nx + i
                        if not self.blocked[v]:
                            d = di * di + dj * dj
                            if d < bd:
                                best, bd = v, d
            if best >= 0:
                return best
        return -1

    def on_main(self, x, y):
        k = self.nearest_free(self.cellof(x, y), 3)
        return k >= 0 and self.comp[k] == self.main

    def reachable(self, x, y):
        return self.on_main(x, y)

    def can_step(self, a, b, diag):
        if self.blocked[b]:
            return False
        return self.surf[b] - self.surf[a] <= (STEP_UP_DIAG if diag else STEP_UP)

    def line_walkable(self, x0, y0, x1, y1):
        dx, dy = x1 - x0, y1 - y0
        d = math.hypot(dx, dy)
        n = int(d / 1.0) + 1
        prev = self.cellof(x0, y0)
        blocked, surf, cellof = self.blocked, self.surf, self.cellof
        for k in range(1, n + 1):
            t = k / n
            c = cellof(x0 + dx * t, y0 + dy * t)
            if c == prev:
                continue
            if blocked[c] or surf[c] - surf[prev] > STEP_UP_DIAG:
                return False
            prev = c
        return True

    # ---- A* -------------------------------------------------------------------
    def path(self, sx, sy, gx, gy, max_expand=6000):
        """Cells from start to goal (or to the closest cell reached). Weighted A*, deterministic."""
        s = self.nearest_free(self.cellof(sx, sy))
        g = self.nearest_free(self.cellof(gx, gy))
        if s < 0 or g < 0:
            return []
        nx = self.nx
        gj, gi = divmod(g, nx)
        cost = {s: 0.0}
        parent = {s: -1}
        heap = [(0.0, s)]
        best, bh = s, 1e18
        n = 0
        blocked, surf = self.blocked, self.surf
        while heap and n < max_expand:
            f, c = heapq.heappop(heap)
            n += 1
            if c == g:
                best = g
                break
            cj, ci = divmod(c, nx)
            h = math.hypot(ci - gi, cj - gj)
            if h < bh:
                bh, best = h, c
            gc = cost[c]
            for di, dj, w in NBRS:
                i, j = ci + di, cj + dj
                if i < 0 or j < 0 or i >= nx or j >= self.ny:
                    continue
                v = j * nx + i
                if blocked[v]:
                    continue
                diag = w > 1.0
                if surf[v] - surf[c] > (STEP_UP_DIAG if diag else STEP_UP):
                    continue
                if diag and (blocked[cj * nx + i] or blocked[j * nx + ci]):
                    continue
                nc = gc + w + (0.6 if self.clear[v] < 2 else 0.0)
                if nc < cost.get(v, 1e18):
                    cost[v] = nc
                    parent[v] = c
                    heapq.heappush(heap, (nc + 1.25 * math.hypot(i - gi, j - gj), v))
        out = []
        c = best
        while c >= 0:
            out.append(c)
            c = parent[c]
        out.reverse()
        return out

    def path_points(self, sx, sy, gx, gy):
        """Waypoints (x, y) toward the goal. Short routes: exact A* on the 2 m grid.
        Long routes: A* on the 4 m grid (cheap enough to cross the campus)."""
        if math.hypot(gx - sx, gy - sy) < 120:
            return [self.cellpos(c) for c in self.path(sx, sy, gx, gy, 6000)]
        return self.path_coarse(sx, sy, gx, gy, 9000)

    def path_coarse(self, sx, sy, gx, gy, max_expand=9000):
        W = self.W
        cnx, cny = W.cnx, W.cny
        ch, csolid = W.ch, W.csolid

        def free(k):
            return not csolid[k]

        def near_free(k):
            if free(k):
                return k
            j0, i0 = divmod(k, cnx)
            for r in range(1, 5):
                for dj in range(-r, r + 1):
                    for di in (-r, r) if abs(dj) != r else range(-r, r + 1):
                        i, j = i0 + di, j0 + dj
                        if 0 <= i < cnx and 0 <= j < cny and free(j * cnx + i):
                            return j * cnx + i
            return -1
        s = near_free(W.cidx(sx, sy))
        g = near_free(W.cidx(gx, gy))
        if s < 0 or g < 0:
            return []
        gj, gi = divmod(g, cnx)
        cost = {s: 0.0}
        parent = {s: -1}
        heap = [(0.0, s)]
        best, bh = s, 1e18
        n = 0
        while heap and n < max_expand:
            f, c = heapq.heappop(heap)
            n += 1
            if c == g:
                best = g
                break
            cj, ci = divmod(c, cnx)
            h = math.hypot(ci - gi, cj - gj)
            if h < bh:
                bh, best = h, c
            gc = cost[c]
            for di, dj, w in NBRS:
                i, j = ci + di, cj + dj
                if i < 0 or j < 0 or i >= cnx or j >= cny:
                    continue
                v = j * cnx + i
                if csolid[v]:
                    continue
                diag = w > 1.0
                if ch[v] - ch[c] > (5.5 if diag else 4.0):
                    continue
                if diag and (csolid[cj * cnx + i] or csolid[j * cnx + ci]):
                    continue
                nc = gc + w
                if nc < cost.get(v, 1e18):
                    cost[v] = nc
                    parent[v] = c
                    heapq.heappush(heap, (nc + 1.15 * math.hypot(i - gi, j - gj), v))
        out = []
        c = best
        while c >= 0:
            out.append(W.ccenter(c))
            c = parent[c]
        out.reverse()
        return out

    # ---- cover search ---------------------------------------------------------
    def cover_bfs(self, x, y, hidden, penalty, threats, max_checks=350, max_steps=50):
        """Nearest walkable cell (BFS order) for which hidden(cx, cy) holds, scored by
        2*steps + zone penalty - 0.25*min(distance to nearest threat, 80). Returns (x, y) or None."""
        s = self.nearest_free(self.cellof(x, y))
        if s < 0:
            return None
        nx = self.nx
        seen = {s: 0}
        q = deque([s])
        best, bs = None, 1e18
        checked = 0
        while q and checked < max_checks:
            c = q.popleft()
            steps = seen[c]
            if steps > max_steps or (best is not None and steps * 2 > bs + 30):
                break
            cj, ci = divmod(c, nx)
            if (ci + cj) % 2 == 0 and steps > 0:
                cx, cy = self.cellpos(c)
                checked += 1
                if hidden(cx, cy):
                    sc = steps * 2 + penalty(cx, cy)
                    near = min((math.hypot(t[0] - cx, t[1] - cy) for t in threats), default=80)
                    sc -= min(near, 80) * 0.25
                    if sc < bs:
                        best, bs = (cx, cy), sc
            for k, (di, dj, w) in enumerate(NBRS):
                i, j = ci + di, cj + dj
                if i < 0 or j < 0 or i >= nx or j >= self.ny:
                    continue
                v = j * nx + i
                if v in seen or not self.can_step(c, v, k >= 4):
                    continue
                if k >= 4 and (self.blocked[cj * nx + i] or self.blocked[j * nx + ci]):
                    continue
                seen[v] = steps + 1
                q.append(v)
        return best

    def main_near(self, x, y, max_r=25):
        """Closest cell of the main component (for jetting out of walled-off pockets)."""
        nx = self.nx
        k0 = self.cellof(x, y)
        j0, i0 = divmod(k0, nx)
        for r in range(1, max_r + 1):
            best, bd = -1, 1e18
            for dj in range(-r, r + 1):
                for di in (-r, r) if abs(dj) != r else range(-r, r + 1):
                    i, j = i0 + di, j0 + dj
                    if 0 <= i < nx and 0 <= j < self.ny:
                        v = j * nx + i
                        if not self.blocked[v] and self.comp[v] == self.main and self.clear[v] >= 2:
                            d = di * di + dj * dj
                            if d < bd:
                                best, bd = v, d
            if best >= 0:
                return self.cellpos(best)
        return None
