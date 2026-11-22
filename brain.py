"""wraith v9 brain.

Decision rules follow the public MIT-licensed giddygiddyboy bot (Copyright (c) 2026 giddygiddyboy; see
NOTICE.md): engage tests, target scoring, burst-based finishing, the practice "losing" rule, the 16-step
fight-move scorer, BFS cover search, deadline-based zone timing, loot utility and slew-aware firing.
This file is an independent Python implementation on wraith's own map model (world.py / nav.py).
"""
import math
import random

LAT = 0.075
TAU = 2 * math.pi

UPGRADES = ["plating", "armor", "gun_damage", "nanites", "gun_rate", "lightning_efficiency", "lightning_range",
            "laser_damage", "servos", "laser_charge", "gun_mag", "gun_velocity", "gyros", "lightning_chain", "reactor",
            "capacitor", "mortar_reload", "mortar_splash", "mortar_velocity", "sensors", "jets", "laser_pierce"]
UPGRADES_LASER = ["plating", "armor", "laser_damage", "laser_charge", "nanites", "gun_damage", "gun_rate",
                  "lightning_efficiency", "lightning_range", "servos", "gun_mag", "gun_velocity", "gyros",
                  "lightning_chain", "reactor", "capacitor", "mortar_reload", "mortar_splash", "mortar_velocity",
                  "sensors", "jets", "laser_pierce"]
WEAPON_RANK = {"lightning": 3, "laser": 2, "mortar": 1}


def angdiff(a, b):
    return (a - b + math.pi) % TAU - math.pi


def clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


class Params:
    """Free-for-all profile (default) and the practice-squad profile."""

    def __init__(self):
        self.retreat_hp, self.recover_hp = 0.38, 0.8
        self.gun_range, self.light_range, self.laser_range = 85.0, 28.0, 85.0
        self.engage_dist = 60.0
        self.farm_npc = True
        self.ghost = True
        self.home_expo = 120.0
        self.loot_radius = 90.0
        self.crate_until = 150.0
        self.dodge = True
        self.no_mortar = True
        self.peek = True
        self.laser_first = True
        self.crowd_hide = True
        self.laser_seek = 0.0
        self.lethal = False
        self.stick = 0.0
        self.hpw = 60.0 / 1400
        self.low_bonus = 0.0
        self.max_target = 215.0
        self.lose_rule = False
        self.fight_loot = 0.0
        self.farm_hp = 0.7
        self.laser_up = False
        self.finish = 0.0
        self.finish_hp = 0.38

    def squad(self):
        p = Params()
        p.__dict__.update(self.__dict__)
        p.ghost = False
        p.peek = False
        p.laser_first = True
        p.laser_range = 120.0
        p.loot_radius = 150.0
        p.crate_until = 1e9
        p.stick = 120.0
        p.hpw = 0.2
        p.low_bonus = 100.0
        p.max_target = 240.0
        p.lose_rule = True
        p.farm_hp = 0.5
        p.laser_seek = 240.0
        p.laser_up = True
        p.lethal = True
        p.finish = 20.0
        p.fight_loot = 40.0
        return p


class Track:
    __slots__ = ("id", "kind", "pos", "vel", "aim", "hp", "max_hp", "cubes", "weapons", "charging",
                 "seen", "hit_me", "dist", "los", "closing", "engage")

    def __init__(self, mid):
        self.id = mid
        self.hit_me = -100.0
        self.engage = False
        self.los = False
        self.closing = 0.0
        self.dist = 1e9


class Brain:
    def __init__(self, hello, world, nav):
        self.cfg = hello["config"]
        self.W = world
        self.N = nav
        self.rng = random.Random(hello.get("seed", 1))
        self.base = Params()
        self.P = self.base
        self.tracks, self.npcs = {}, {}
        self.crates, self.cubes, self.pickups = {}, {}, {}
        self.bad = {}
        self.now = 0.0
        self.path, self.path_goal, self.path_at, self.path_i = None, (0.0, 0.0), -99.0, 0
        self.strafe, self.strafe_until = 1.0, 0.0
        self.retreat = False
        self.cover_at, self.cover_goal, self.cover_ok = -1.0, (0.0, 0.0), False
        self.last_hurt, self.hurt_bearing = -99.0, 0.0
        self.loot_id, self.loot_since = -1, 0.0
        self.hold, self.hold_start = None, None
        self.home_xy, self.home_at, self.home_set = (0.0, 0.0), -99.0, False
        self.stuck_since, self.unstuck_until, self.unstuck_dir = -1.0, -1.0, (0.0, 0.0)
        self.last_threat_at = -99.0
        self.last_aim = [0.0, 0.0]
        self.esc_ok, self.esc_flying, self.esc_at, self.esc_land = False, False, -99.0, None
        self.npc_hurt, self.npc_bearing = -99.0, 0.0
        self.not_practice, self.prac_on = False, False
        self.start_alive, self.kills, self.other_dead = None, 0, 0
        self.stick_id, self.stick_at, self.lose_at = -1, -99.0, -99.0
        self.mode = "start"
        m = self.cfg["mech"]
        self.mech_r, self.mech_h, self.eye_h = m["radius"], m["height"], m["eye_height"]
        self.size_per_cube = self.cfg["cubes"]["size_per_cube"]

    # ---- helpers ---------------------------------------------------------------
    def scale(self, cubes):
        return 1 + self.size_per_cube * (cubes or 0)

    def center(self, t):
        return (t.pos[0], t.pos[1], t.pos[2] + self.mech_h * self.scale(t.cubes) * 0.5)

    def eye(self, t):
        return (t.pos[0], t.pos[1], t.pos[2] + self.eye_h * self.scale(t.cubes))

    def clear(self, a, b):
        return self.W.los(a, b)

    @staticmethod
    def weapon(ws, kind):
        for i, w in enumerate(ws):
            if w and w["kind"] == kind:
                return i, w
        return -1, None

    # ---- memory ----------------------------------------------------------------
    def observe(self, o):
        me = o["self"]
        now = o["t"]
        st = me["stats"]
        x, y, z = me["pos"]
        eye = (x, y, z + st.get("eye_height", 8))
        muzzle = (x, y, z + st.get("muzzle_height", 7))
        for m in o["mechs"]:
            tab = self.npcs if m["kind"] == "npc" else self.tracks
            t = tab.get(m["id"])
            if t is None:
                t = tab[m["id"]] = Track(m["id"])
            t.kind = m["kind"]
            t.pos, t.vel, t.aim = m["pos"], m.get("vel", [0, 0, 0]), m.get("aim", [0, 0])
            t.hp, t.max_hp = m.get("hp", 1400), m.get("max_hp", 1400)
            t.cubes, t.weapons, t.charging = m.get("cubes", 0) or 0, m.get("weapons") or [], bool(m.get("charging"))
            t.seen = now
            dx, dy = m["pos"][0] - x, m["pos"][1] - y
            t.dist = math.hypot(dx, dy)
            if m["kind"] == "npc":
                c = (m["pos"][0], m["pos"][1], m["pos"][2] + self.cfg["npc"]["height"] * 0.5)
            else:
                c = self.center(t)
            t.los = self.clear(muzzle, c)
            if not t.los and m["kind"] == "mech":
                t.los = self.clear(muzzle, (m["pos"][0], m["pos"][1], m["pos"][2] + self.mech_h * self.scale(t.cubes) * 0.85))
            if t.dist > 1:
                t.closing = -((t.vel[0] - me["vel"][0]) * dx + (t.vel[1] - me["vel"][1]) * dy) / t.dist
        for tab, life in ((self.tracks, 20), (self.npcs, 120)):
            for k in [k for k, t in tab.items() if now - t.seen > life]:
                del tab[k]

        def upd(tab, items):
            for it in items:
                k = tab.get(it["id"])
                if k is None:
                    k = tab[it["id"]] = {}
                k.update(id=it["id"], kind=it.get("kind"), pos=it["pos"], hp=it.get("hp"), seen=now)
            for kid in [kid for kid, k in tab.items() if k["seen"] != now]:
                k = tab[kid]
                d = math.hypot(k["pos"][0] - x, k["pos"][1] - y)
                if d < 40 and now - k["seen"] > 0.35 and self.clear(eye, (k["pos"][0], k["pos"][1], k["pos"][2] + 1.5)):
                    del tab[kid]
        upd(self.crates, o.get("crates", []))
        upd(self.cubes, o.get("cubes", []))
        upd(self.pickups, o.get("pickups", []))
        for e in o.get("events", []):
            if e["type"] == "damaged":
                if e.get("weapon") == "npc" and e.get("bearing") is not None:
                    self.npc_hurt, self.npc_bearing = now, e["bearing"]
                if e.get("by") is not None:
                    self.last_hurt = now
                    if e.get("bearing") is not None:
                        self.hurt_bearing = e["bearing"]
                    t = self.tracks.get(e["by"])
                    if t:
                        t.hit_me = now
                    n = self.npcs.get(e["by"])
                    if n:
                        n.hit_me = now
            elif e["type"] == "kill":
                self.kills += 1
                self.tracks.pop(e.get("target"), None)
        if self.start_alive is None:
            self.start_alive = o["alive"]
        if self.start_alive - o["alive"] - self.kills > 0:
            self.other_dead += 1
        else:
            self.other_dead = 0
        if self.other_dead >= 5:
            self.not_practice = True
        on = not self.not_practice and now > 20
        if on != self.prac_on:
            self.prac_on = on
            self.P = self.base.squad() if on else self.base
        zz = o["zone"]
        if self.hold_start != zz["shrink_start"]:
            self.hold_start = zz["shrink_start"]
            self.hold = (zz["center"][0], zz["center"][1], zz["radius"])
        elif now <= zz["shrink_start"]:
            self.hold = (zz["center"][0], zz["center"][1], zz["radius"])

    # ---- zone ------------------------------------------------------------------
    def zone_at(self, zz, t):
        if t <= zz["shrink_start"] or zz["shrink_end"] <= zz["shrink_start"]:
            return self.hold
        f = clamp((t - zz["shrink_start"]) / (zz["shrink_end"] - zz["shrink_start"]), 0, 1)
        hx, hy, hr = self.hold
        return (hx + (zz["next_center"][0] - hx) * f, hy + (zz["next_center"][1] - hy) * f,
                hr + (zz["next_radius"] - hr) * f)

    def cur_zone(self, zz):
        live = (zz["center"][0], zz["center"][1], zz["radius"])
        if self.now <= zz["shrink_start"]:
            return live
        if abs(live[2] - self.hold[2]) > 0.01 or abs(live[0] - self.hold[0]) > 0.01:
            return live
        return self.zone_at(zz, self.now)

    def zone_deadline(self, zz, x, y, pad):
        if math.hypot(x - zz["next_center"][0], y - zz["next_center"][1]) <= zz["next_radius"] - pad:
            return math.inf
        for k in range(25):
            t = zz["shrink_start"] + (zz["shrink_end"] - zz["shrink_start"]) * k / 24
            c = self.zone_at(zz, t)
            if math.hypot(x - c[0], y - c[1]) > c[2] - pad:
                return t
        return zz["shrink_end"]

    def zone_penalty(self, zz, x, y):
        c = self.cur_zone(zz)
        p = 0.0
        d = math.hypot(x - c[0], y - c[1]) - c[2]
        if d > -5:
            p += 150 + 5 * (d + 5)
        dn = math.hypot(x - zz["next_center"][0], y - zz["next_center"][1]) - zz["next_radius"]
        if dn > -5:
            soon = clamp(1 - (zz["shrink_start"] - self.now) / 25, 0.1, 1)
            p += soon * (25 + 0.8 * (dn + 5))
        return p

    def zone_goal(self, zz, x, y):
        nr = zz["next_radius"]
        ncx, ncy = zz["next_center"]
        if nr <= 30:
            return ncx, ncy
        margin = clamp(0.3 * nr, 3, 30)
        dx, dy = x - ncx, y - ncy
        d = math.hypot(dx, dy)
        if d < 1:
            return x, y
        r = max(0.0, nr - margin - 5)
        if d <= r:
            return x, y
        return ncx + dx / d * r, ncy + dy / d * r

    # ---- movement --------------------------------------------------------------
    def travel(self, me, gx, gy):
        x, y = me["pos"][0], me["pos"][1]
        d = math.hypot(gx - x, gy - y)
        if d < 0.8:
            return 0.0, 0.0

        def unit(tx, ty):
            l = math.hypot(tx - x, ty - y)
            return (0.0, 0.0) if l < 1e-6 else ((tx - x) / l, (ty - y) / l)
        N = self.N
        if me["pos"][2] - self.W.ground_at(x, y) > 3 and not N.free(x, y):
            return unit(gx, gy)
        if d < 45 and N.line_walkable(x, y, gx, gy):
            return unit(gx, gy)
        if self.path is None or math.hypot(gx - self.path_goal[0], gy - self.path_goal[1]) > 5 or self.now - self.path_at > 2.5:
            self.path = N.path_points(x, y, gx, gy)
            self.path_goal, self.path_at, self.path_i = (gx, gy), self.now, 0
        if not self.path:
            return unit(gx, gy)
        path = self.path
        bi, bd = self.path_i, math.inf
        for i in range(self.path_i, min(len(path), self.path_i + 10)):
            px, py = path[i]
            dd = math.hypot(px - x, py - y)
            if dd < bd:
                bi, bd = i, dd
        self.path_i = bi
        if bd > 12:
            self.path = None
            return unit(gx, gy)
        for i in range(min(bi + 14, len(path) - 1), bi, -1):
            px, py = path[i]
            if N.line_walkable(x, y, px, py):
                if i == len(path) - 1 and math.hypot(px - gx, py - gy) < 4 and N.line_walkable(x, y, gx, gy):
                    return unit(gx, gy)
                return unit(px, py)
        if bi + 1 < len(path):
            return unit(*path[bi + 1])
        return unit(gx, gy)

    def escape(self, me):
        """Jet out of a walled-off pocket (courtyard, roof) back to the main walkable area."""
        x, y, zf = me["pos"]
        N = self.N
        if me["grounded"]:
            if N.on_main(x, y):
                self.esc_ok = self.esc_flying = False
                return None
            self.esc_flying = False
            if not self.esc_ok or self.now - self.esc_at > 6:
                self.esc_land = N.main_near(x, y)
                self.esc_at = self.now
                self.esc_ok = self.esc_land is not None
                if not self.esc_ok:
                    return None
            if me["energy"] < min(70, me["max_energy"] * 0.7):
                return (0.0, 0.0, False, False)
            self.esc_flying = True
            jump = True
        elif not self.esc_flying:
            return None
        else:
            jump = False
        lx, ly = self.esc_land
        dx, dy = lx - x, ly - y
        d = math.hypot(dx, dy)
        if d < 1.5:
            self.esc_flying = False
            return (0.0, 0.0, False, jump)
        mx, my = dx / d, dy / d
        need = -1e9
        t = 0.0
        while t <= min(d, 30):
            need = max(need, self.W.surf_at(x + mx * t, y + my * t))
            t += 1.0
        return (mx, my, zf < need + 2.5 and me["energy"] > 1, jump)

    def pref_range(self, me, t):
        _, light = self.weapon(me["weapons"], "lightning")
        _, laser = self.weapon(me["weapons"], "laser")
        r = self.P.gun_range
        if light is not None and me["energy"] > 12:
            r = self.P.light_range
        elif laser is not None:
            r = self.P.laser_range
        if t.kind == "npc":
            return 58.0
        if light is None:
            if "lightning" in t.weapons:
                r = max(r, 64.0)
            if "mortar" in t.weapons:
                r = max(r, 80.0)
        return r

    def fight_move(self, o, tg, seen, want_los, rp=None):
        me = o["self"]
        x, y = me["pos"][0], me["pos"][1]
        if rp is None:
            rp = self.pref_range(me, tg)
        bx, by = tg.pos[0] - x, tg.pos[1] - y
        bd = math.hypot(bx, by) + 1e-9
        ux, uy = bx / bd, by / bd
        if self.now >= self.strafe_until:
            if self.rng.random() < 0.6:
                self.strafe = -self.strafe
            self.strafe_until = self.now + 0.3 + self.rng.random() * 0.7
        sx, sy = -uy * self.strafe, ux * self.strafe
        tc = self.center(tg) if tg.kind == "mech" else (tg.pos[0], tg.pos[1], tg.pos[2] + 1.75)
        mh = me["stats"].get("muzzle_height", 7)
        cube = None
        cd = math.inf
        if me.get("cubes", 0) < self.cfg["cubes"]["stat_cap"]:
            for c in self.cubes.values():
                d = math.hypot(c["pos"][0] - x, c["pos"][1] - y)
                if d < 40 and d < cd and abs(c["pos"][2] - me["pos"][2]) < 4:
                    cube, cd = c, d
        N = self.N
        others = [e for e in seen if e is not tg and math.hypot(e.pos[0] - x, e.pos[1] - y) < 190]
        best, mx, my = -math.inf, 0.0, 0.0
        blocked_side = False
        rings = 2 if self.P.peek else 1
        height = me["stats"].get("height", self.mech_h)
        for k in range(-1, 16 * rings):
            if k >= 0:
                a = (k % 16) * math.pi / 8
                r = 6.0 * (1 + k // 16)
                dx, dy = math.cos(a), math.sin(a)
                cx, cy = x + dx * r, y + dy * r
                if not N.line_walkable(x, y, cx, cy) or not N.free(cx, cy):
                    if k < 16 and dx * sx + dy * sy > 0.9:
                        blocked_side = True
                    continue
            else:
                cx, cy, dx, dy, r = x, y, 0.0, 0.0, 0.0
            cz = N.walk_at(cx, cy)
            d = math.hypot(tg.pos[0] - cx, tg.pos[1] - cy)
            s = -1.1 * abs(d - rp) - r * 0.4
            los = self.clear((cx, cy, cz + mh), tc)
            if los:
                s += 45 * want_los
            body = (cx, cy, cz + height * 0.6)
            for e in others:
                if math.hypot(e.pos[0] - cx, e.pos[1] - cy) <= 170 and self.clear(self.eye(e), body):
                    s -= 30
            s -= self.zone_penalty(o["zone"], cx, cy)
            if cube is not None:
                s += 1.5 * (cd - math.hypot(cube["pos"][0] - cx, cube["pos"][1] - cy))
            cl = N.clearance(cx, cy)
            if cl < 4:
                s -= (4 - cl) * 2.5
            if want_los > 0:
                s += 9 * (dx * sx + dy * sy)
            if k < 0:
                s -= 8
                if want_los < 0 and not los:
                    s += 14
            if s > best:
                best, mx, my = s, dx, dy
        if blocked_side:
            self.strafe = -self.strafe
            self.strafe_until = self.now + 0.5
        return mx, my

    def cover_move(self, o, seen):
        me = o["self"]
        x, y = me["pos"][0], me["pos"][1]
        threats = sorted([t for t in seen if math.hypot(t.pos[0] - x, t.pos[1] - y) < 230],
                         key=lambda t: math.hypot(t.pos[0] - x, t.pos[1] - y))[:4]
        if not threats:
            return None
        h = me["stats"].get("height", self.mech_h)
        eyes = [self.eye(t) for t in threats]
        N = self.N

        def hidden(cx, cy):
            cz = N.walk_at(cx, cy)
            for e in eyes:
                if self.clear(e, (cx, cy, cz + h)) or self.clear(e, (cx, cy, cz + h * 0.5)):
                    return False
            return True
        if self.cover_ok and self.now - self.cover_at < 0.4:
            return self.travel_cover(me)
        self.cover_at = self.now
        zz = o["zone"]
        if hidden(x, y) and self.zone_penalty(zz, x, y) == 0:
            self.cover_ok = True
            self.cover_goal = (x, y)
            return (0.0, 0.0)
        g = N.cover_bfs(x, y, hidden, lambda cx, cy: self.zone_penalty(zz, cx, cy),
                        [(t.pos[0], t.pos[1]) for t in threats])
        if g is None:
            self.cover_ok = False
            return None
        self.cover_ok = True
        self.cover_goal = g
        return self.travel_cover(me)

    def travel_cover(self, me):
        if math.hypot(self.cover_goal[0] - me["pos"][0], self.cover_goal[1] - me["pos"][1]) < 1.5:
            return (0.0, 0.0)
        return self.travel(me, *self.cover_goal)

    def hide_from_bearing(self, o, seen):
        me = o["self"]
        ghost = Track(-1)
        ghost.kind = "mech"
        ghost.pos = [me["pos"][0] + math.cos(self.hurt_bearing) * 80, me["pos"][1] + math.sin(self.hurt_bearing) * 80, me["pos"][2]]
        ghost.cubes = 0
        return self.cover_move(o, [ghost] + list(seen))

    def dodge_shell(self, o):
        me = o["self"]
        mst = self.cfg["weapons"]["mortar"]
        g = mst.get("gravity", 22) or 22
        rad = mst["splash_radius"] + me["stats"].get("radius", self.mech_r) + 2
        for p in o.get("projectiles", []):
            if p["kind"] != "mortar":
                continue
            px, py, pz = p["pos"]
            vx, vy, vz = p["vel"]
            t = 0.0
            while t < 4:
                px += vx * 0.05
                py += vy * 0.05
                pz += vz * 0.05
                vz -= g * 0.05
                if self.W.surf_at(px, py) >= pz:
                    d = math.hypot(px - me["pos"][0], py - me["pos"][1])
                    if d < rad and t < 2.5:
                        dx, dy = me["pos"][0] - px, me["pos"][1] - py
                        if d < 0.5:
                            dx, dy = -vy, vx
                            d = math.hypot(dx, dy) or 1
                        dx, dy = dx / d, dy / d
                        for rot in (0, 0.7, -0.7, 1.4, -1.4):
                            c, s = math.cos(rot), math.sin(rot)
                            ex, ey = dx * c - dy * s, dx * s + dy * c
                            if self.N.line_walkable(me["pos"][0], me["pos"][1], me["pos"][0] + ex * 6, me["pos"][1] + ey * 6):
                                return ex, ey
                    break
                t += 0.05
        return None

    def dodge(self, o):
        me = o["self"]
        cx, cy = me["pos"][0], me["pos"][1]
        cz = me["pos"][2] + me["stats"].get("height", self.mech_h) * 0.56
        reach = me["stats"].get("radius", self.mech_r) + 1.1
        dx = dy = 0.0
        laser = False
        for p in o.get("projectiles", []):
            if p["kind"] == "mortar":
                continue
            v = p["vel"]
            v2 = v[0] * v[0] + v[1] * v[1] + v[2] * v[2]
            if v2 < 1:
                continue
            tc = ((cx - p["pos"][0]) * v[0] + (cy - p["pos"][1]) * v[1] + (cz - p["pos"][2]) * v[2]) / v2
            if tc <= 0 or tc > 0.7:
                continue
            mxx, myy = p["pos"][0] + v[0] * tc - cx, p["pos"][1] + v[1] * tc - cy
            if math.hypot(mxx, myy) < reach:
                vh = math.hypot(v[0], v[1])
                if vh < 1:
                    continue
                px, py = -v[1] / vh, v[0] / vh
                if px * mxx + py * myy > 0:
                    px, py = -px, -py
                dx += px
                dy += py
        for m in o["mechs"]:
            if m["kind"] != "mech" or not m.get("charging"):
                continue
            to_me = math.atan2(cy - m["pos"][1], cx - m["pos"][0])
            if abs(angdiff(m["aim"][0], to_me)) > 0.15:
                continue
            dx += math.cos(to_me + math.pi / 2 * self.strafe) * 1.5
            dy += math.sin(to_me + math.pi / 2 * self.strafe) * 1.5
            laser = True
        l = math.hypot(dx, dy)
        if l > 0.01:
            return dx / l, dy / l, laser
        return None

    # ---- loot ------------------------------------------------------------------
    def want_pickup(self, me, kind):
        ws = me["weapons"]
        if kind == "gun":
            return len(ws) > 0 and ws[0] is None
        if self.weapon(ws, kind)[0] >= 0:
            return False
        if kind == "mortar" and self.P.no_mortar:
            return False

        def rank(k):
            if self.P.laser_first and k == "laser":
                return 4
            return WEAPON_RANK.get(k, 0)
        cur = rank(ws[1]["kind"]) if len(ws) > 1 and ws[1] else 0
        return rank(kind) > cur

    def choose_loot(self, o, hp_frac, danger):
        me = o["self"]
        x, y, zf = me["pos"]
        zz = o["zone"]
        now = self.now
        cur = self.cur_zone(zz)
        walk = me["stats"].get("walk_speed", 11.7)
        best, best_u = None, 0.0

        def consider(kind, iid, pos, value):
            nonlocal best, best_u
            if self.bad.get(iid, -1) > now:
                return
            d = math.hypot(pos[0] - x, pos[1] - y)
            if d > 260:
                return
            if math.hypot(pos[0] - cur[0], pos[1] - cur[1]) > cur[2] - 6:
                return
            if now + d * 1.4 / walk + 5 > self.zone_deadline(zz, pos[0], pos[1], 6):
                return
            if pos[2] - self.W.ground_at(pos[0], pos[1]) > 3.2:
                return
            if not self.N.reachable(pos[0], pos[1]):
                return
            u = value / (d + 18)
            if iid == self.loot_id:
                u *= 1.35
            if u > best_u:
                best_u, best = u, (kind, iid, pos, value)
        P = self.P
        ws = me["weapons"]
        # Before the first duel a reachable laser is worth a detour past ordinary loot.
        arm_up = now < 30 and not danger and hp_frac > 0.7
        laser_seek = max(P.laser_seek, 18 * walk) if arm_up else P.laser_seek
        for p in self.pickups.values():
            rad = P.loot_radius
            seek = p["kind"] == "laser" and laser_seek > rad
            if seek:
                rad = laser_seek
            if self.want_pickup(me, p["kind"]) and math.hypot(p["pos"][0] - x, p["pos"][1] - y) < rad:
                v = 130.0 if (len(ws) > 1 and ws[1] is None) else 60.0
                if seek:
                    v = max(v, 350.0 if arm_up else 150.0)
                consider("pickup", p["id"], p["pos"], v)
        cap = self.cfg["cubes"]["stat_cap"]
        capped = me.get("cubes", 0) >= cap
        for c in self.cubes.values():
            if not capped and math.hypot(c["pos"][0] - x, c["pos"][1] - y) < max(60, P.loot_radius * 0.7):
                consider("cube", c["id"], c["pos"], 75.0)
        if hp_frac > P.farm_hp and not danger and now < P.crate_until:
            no_second = len(ws) > 1 and ws[1] is None
            for c in self.crates.values():
                if (not capped or no_second) and math.hypot(c["pos"][0] - x, c["pos"][1] - y) < P.loot_radius:
                    consider("crate", c["id"], c["pos"], 45.0)
            if P.farm_npc and not capped:
                for n in self.npcs.values():
                    if now - n.seen < 12 and math.hypot(n.pos[0] - x, n.pos[1] - y) < P.loot_radius:
                        consider("npc", n.id, n.pos, 85.0)
        if best is None:
            self.loot_id = -1
            return None
        if best[1] != self.loot_id:
            self.loot_id, self.loot_since = best[1], now
        elif now - self.loot_since > 30:
            self.bad[best[1]] = now + 60
            self.loot_id = -1
            return None
        return best

    def home(self, o):
        zz = o["zone"]
        ncx, ncy = zz["next_center"]
        nr = zz["next_radius"]
        out = not self.home_set or math.hypot(self.home_xy[0] - ncx, self.home_xy[1] - ncy) > nr * 0.8
        if out or self.now - self.home_at > 12:
            self.home_at = self.now
            best, bc = None, math.inf
            for _ in range(40):
                a = self.rng.random() * TAU
                r = math.sqrt(self.rng.random()) * nr * 0.7
                px, py = ncx + math.cos(a) * r, ncy + math.sin(a) * r
                if not self.N.reachable(px, py):
                    continue
                c = self.home_cost(o, px, py)
                if c < bc:
                    best, bc = (px, py), c
            if best is not None and (out or bc < self.home_cost(o, *self.home_xy) - 15):
                self.home_xy, self.home_set = best, True
        return self.home_xy if self.home_set else None

    def home_cost(self, o, px, py):
        zz = o["zone"]
        crowd = 0.0
        for t in self.tracks.values():
            if self.now - t.seen < 20:
                crowd += max(0.0, 80 - math.hypot(t.pos[0] - px, t.pos[1] - py)) / 80
        expo = max(0.0, 1 - self.W.cover_at(px, py) / 20.0)
        edge = math.hypot(px - zz["next_center"][0], py - zz["next_center"][1]) / max(30, zz["next_radius"])
        return self.P.home_expo * expo + 0.25 * math.hypot(px - o["self"]["pos"][0], py - o["self"]["pos"][1]) + 40 * crowd + 10 * edge

    # ---- aiming ----------------------------------------------------------------
    @staticmethod
    def lead(muzzle, tp, tv, speed, grounded):
        speed = speed or 252
        t = 0.0
        px = py = pz = 0.0
        for _ in range(4):
            tt = LAT + t
            px, py, pz = tp[0] + tv[0] * tt, tp[1] + tv[1] * tt, tp[2]
            if not grounded:
                pz += tv[2] * tt * 0.5
            d = math.sqrt((px - muzzle[0]) ** 2 + (py - muzzle[1]) ** 2 + (pz - muzzle[2]) ** 2)
            t = max(0.0, d - 3) / speed
        return math.atan2(py - muzzle[1], px - muzzle[0]), math.atan2(pz - muzzle[2], math.hypot(px - muzzle[0], py - muzzle[1]))

    def pick_upgrade(self, offer):
        order = UPGRADES_LASER if self.P.laser_up else UPGRADES
        best, br = 0, 1 << 30
        for i, c in enumerate(offer["choices"]):
            r = order.index(c["id"]) if c["id"] in order else len(order)
            if r < br:
                best, br = i, r
        return best

    # ---- main step ---------------------------------------------------------------
    def act(self, o):
        act = {}
        me = o["self"]
        self.now = now = o["t"]
        self.observe(o)
        st = me["stats"]
        x, y, zf = me["pos"]
        zz = o["zone"]
        hp_frac = me["hp"] / max(1, me["max_hp"])
        P = self.P
        if o.get("offer") and o["offer"].get("choices"):
            act["pick"] = self.pick_upgrade(o["offer"])
        ws = me["weapons"]
        gi, gun = self.weapon(ws, "gun")
        li, light = self.weapon(ws, "lightning")
        zi, laser = self.weapon(ws, "laser")
        late = zz["next_radius"] <= 25 or o["alive"] <= 3

        seen = []
        target = hidden = None
        best_score = math.inf
        for t in self.tracks.values():
            if now - t.seen > 2.5:
                continue
            t.engage = False
            seen.append(t)
            if t.seen != now:
                t.los = False
                continue
            to_me = math.atan2(y - t.pos[1], x - t.pos[0])
            aimed = abs(angdiff(t.aim[0], to_me)) < 0.22
            if P.ghost:
                t.engage = (t.dist < P.engage_dist or now - t.hit_me < 4 or (aimed and t.charging and t.dist < 120)
                            or (t.hp < 350 and t.dist < 120) or o["alive"] <= 2)
            elif t.dist < P.engage_dist or now - t.hit_me < 6 or late:
                t.engage = True
            elif t.closing > 3 and t.dist < 180:
                t.engage = True
            elif t.hp < 0.4 * t.max_hp and t.dist < 160:
                t.engage = True
            elif aimed and t.dist < 190:
                t.engage = True
            if t.engage and not t.los and t.dist < P.max_target and (hidden is None or t.dist < hidden.dist):
                hidden = t
            if not t.engage or not t.los or t.dist > P.max_target:
                continue
            s = t.dist + P.hpw * t.hp
            if P.low_bonus > 0 and t.hp <= 0.3 * t.max_hp:
                s -= P.low_bonus
            if P.stick > 0 and t.id == self.stick_id and now - self.stick_at < 2:
                s -= P.stick
            if now - t.hit_me < 4:
                s -= 40
            if t.closing > 3:
                s -= 15
            if s < best_score:
                best_score, target = s, t
        if target is not None:
            self.stick_id, self.stick_at = target.id, now
        foe = target
        if foe is None and P.peek:
            foe = hidden
        danger = False
        crowd = 0
        for t in seen:
            if t.engage or now - t.hit_me < 4:
                danger = True
            if t.seen == now and (t.engage or (t.los and t.dist < 150)):
                crowd += 1
        if danger or now - self.last_hurt < 3:
            self.last_threat_at = now

        finishing = False
        cube_mult = 1 + self.cfg["cubes"]["damage_per_cube"] * min(me.get("cubes", 0), self.cfg["cubes"]["stat_cap"])
        if target is not None and target.dist < 120 and crowd <= 2:
            burst = 0.0
            if gun is not None and gun["stats"]["cooldown"] > 0:
                burst += 1.5 * 0.5 * gun["stats"]["damage"] / gun["stats"]["cooldown"]
            if laser is not None and laser["cooldown"] < 1 and target.dist < laser["stats"]["range"]:
                burst += 0.8 * laser["stats"]["damage"]
            finishing = target.hp < max(200.0, burst * cube_mult)
        outgunned = target is not None and "laser" in target.weapons and laser is None and target.dist > 55 and hp_frac < 0.8
        swamped = P.crowd_hide and (crowd >= 3 or (crowd == 2 and hp_frac < 0.65) or outgunned)
        if not self.retreat and ((hp_frac < P.retreat_hp and now - self.last_threat_at < 2) or swamped):
            self.retreat = True
            self.cover_at = -1
        if self.retreat and not swamped and (hp_frac > P.recover_hp or (now - self.last_threat_at > 7 and hp_frac > 0.55)):
            self.retreat = False
        if P.lose_rule:
            near = sum(1 for t in seen if t.seen == now and t.dist < 160)
            losing = (near >= 2 and hp_frac < 0.55) or (near >= 3 and hp_frac < 0.8)
            if target is not None:
                tf = target.hp / max(1, target.max_hp)
                if hp_frac < 0.35 and tf > hp_frac + 0.1:
                    losing = True
                if tf < 0.25 and hp_frac > 0.2 and near <= 2:
                    losing = False
            if losing:
                self.lose_at = now
            self.retreat = now - self.lose_at < 1.5 and self.lose_at > 0

        mx = my = 0.0
        boost = False
        mode = "idle"
        farm = farm_pos = None
        zgx, zgy = self.zone_goal(zz, x, y)
        zdist = math.hypot(zgx - x, zgy - y)
        walk = st.get("walk_speed", 11.7)
        t_need = zdist * 1.45 / walk + 4
        deadline = self.zone_deadline(zz, x, y, 8)
        zone_must = (not me["in_zone"]) or now + t_need + 3 >= deadline
        zone_should = now + t_need + 14 >= deadline
        esc_jump = False

        esc = self.escape(me)
        shell = None if esc is not None else self.dodge_shell(o)
        if esc is not None:
            mx, my, boost, esc_jump = esc
            mode = "escape"
        elif now < self.unstuck_until:
            mx, my = self.unstuck_dir
            mode = "unstuck"
        elif shell is not None:
            mx, my = shell
            boost = me["energy"] > 15
            mode = "dodge"
        elif zone_must and zdist > 2:
            mx, my = self.travel(me, zgx, zgy)
            boost = ((not me["in_zone"]) or now + t_need >= deadline) and me["energy"] > 5
            mode = "zone"
        elif self.retreat and not finishing and not late:
            cm = self.cover_move(o, seen)
            if cm is not None:
                mx, my = cm
                boost = me["energy"] > 10 and (mx != 0 or my != 0)
                mode = "cover"
            elif target is not None:
                mx, my = self.fight_move(o, target, seen, 1)
                mode = "cornered"
            else:
                mx, my = self.travel(me, zgx, zgy)
                if now - self.npc_hurt < 3 and mx == 0 and my == 0:
                    side = 1.0 if (now % 3) <= 1.5 else -1.0
                    mx, my = -math.sin(self.npc_bearing) * side, math.cos(self.npc_bearing) * side
                mode = "flee"
        elif target is None and now - self.last_hurt < 2.5 and not late and P.ghost and self._set(
                cm := self.hide_from_bearing(o, seen)) is not None:
            mx, my = cm
            boost = me["energy"] > 30 and (mx != 0 or my != 0)
            mode = "cover"
        elif foe is not None:
            ready = gun is not None and gun.get("ammo", 0) > 0 and gun.get("reload", 0) <= 0.25 and foe.dist <= gun["stats"]["range"] - 10
            hot = laser is not None and (laser.get("charge") or 0) >= laser["stats"]["charge"] - 0.3 and foe.dist <= laser["stats"]["range"] - 5
            if hot or (light is not None and foe.dist <= light["stats"]["range"] + 6 and me["energy"] >= light["stats"].get("energy", 3) * 3):
                ready = True
            if not P.peek and (light is not None or laser is not None):
                ready = True
            want_los = 1.0 if ready else -1.0
            if P.peek and not hot:
                for t in seen:
                    if t.seen == now and t.charging and abs(angdiff(t.aim[0], math.atan2(y - t.pos[1], x - t.pos[0]))) < 0.3:
                        want_los = -1.6
            rp = None
            if P.finish > 0 and foe.hp < P.finish_hp * foe.max_hp and hp_frac > 0.42 and crowd <= 2:
                rp = P.finish
            mx, my = self.fight_move(o, foe, seen, want_los, rp)
            if light is not None and foe.dist > 50 and me["energy"] > 55 and want_los > 0:
                boost = True
            mode = "fight"
            if P.fight_loot > 0 and me.get("cubes", 0) < self.cfg["cubes"]["stat_cap"]:
                r = P.fight_loot * (0.57 if hp_frac <= 0.5 else 1.0)
                bc = None
                for c in self.cubes.values():
                    d = math.hypot(c["pos"][0] - x, c["pos"][1] - y)
                    if d < r:
                        r, bc = d, c["pos"]
                if bc is not None:
                    mx, my = self.travel(me, bc[0], bc[1])
                    mode = "fight:cube"
        elif zone_should and zdist > 2:
            mx, my = self.travel(me, zgx, zgy)
            mode = "zone"
        else:
            lc = self.choose_loot(o, hp_frac, danger)
            if lc is not None:
                kind, iid, pos, _ = lc
                mode = "loot:" + kind
                if kind == "npc":
                    t = self.npcs.get(iid)
                    if t is not None and now - t.seen < 1 and t.los and t.dist < 75:
                        farm = t
                        mx, my = self.fight_move(o, t, seen, 1)
                    else:
                        mx, my = self.travel(me, pos[0], pos[1])
                elif kind == "crate":
                    d = math.hypot(pos[0] - x, pos[1] - y)
                    cp = (pos[0], pos[1], pos[2] + self.cfg["crates"]["height"] * 0.5)
                    if d < 50 and self.clear((x, y, zf + st.get("muzzle_height", 7)), cp):
                        farm_pos = cp
                        if d < 12:
                            mx, my = (x - pos[0]) / max(d, 0.1), (y - pos[1]) / max(d, 0.1)
                    else:
                        mx, my = self.travel(me, pos[0], pos[1])
                else:
                    mx, my = self.travel(me, pos[0], pos[1])
                    if kind == "pickup" and self.pickups[iid]["kind"] == "laser" and now < 30 and not danger:
                        boost = me["grounded"] and me["energy"] > 55 and math.hypot(pos[0] - x, pos[1] - y) > 12
            elif zdist > 3 and (not P.ghost or zz["next_radius"] <= 30):
                mx, my = self.travel(me, zgx, zgy)
                mode = "zone-walk"
            elif P.ghost:
                h = self.home(o)
                if h is not None and math.hypot(h[0] - x, h[1] - y) > 3:
                    mx, my = self.travel(me, h[0], h[1])
                    if not danger and now - self.last_hurt > 6:
                        q = min(1.0, 4.6 / walk)
                        mx, my = mx * q, my * q
                    mode = "home"

        if esc_jump:
            act["jump"] = True
        if P.dodge and mode not in ("unstuck", "escape"):
            dg = self.dodge(o)
            if dg is not None:
                dx, dy, lz = dg
                mx, my = mx * 0.4 + dx * 0.9, my * 0.4 + dy * 0.9
                l = math.hypot(mx, my)
                if l > 1:
                    mx, my = mx / l, my / l
                if lz and me["energy"] > 25:
                    boost = True

        if mode == "escape":
            self.stuck_since = -1
        elif (mx != 0 or my != 0) and math.hypot(me["vel"][0], me["vel"][1]) < 1.2 and me["grounded"]:
            if self.stuck_since < 0:
                self.stuck_since = now
            elif now - self.stuck_since > 0.7:
                a = self.rng.random() * TAU
                self.unstuck_dir = (math.cos(a), math.sin(a))
                self.unstuck_until = now + 0.5
                self.stuck_since = -1
                self.path = None
                act["jump"] = True
        else:
            self.stuck_since = -1

        # ---- aim and fire ----
        muzzle = (x + me["vel"][0] * LAT, y + me["vel"][1] * LAT, zf + st.get("muzzle_height", 7))
        aim = [me["aim"][0], 0.0]
        have_aim = False
        fire = [False, False]
        defend = False
        if target is None and now - self.npc_hurt < 3:
            worst, best_off = None, 0.7
            for n in self.npcs.values():
                if now - n.seen > 0.5 or not n.los or n.dist > 150:
                    continue
                off = abs(angdiff(math.atan2(n.pos[1] - y, n.pos[0] - x), self.npc_bearing))
                if now - n.hit_me < 3:
                    off = 0.0
                if off < best_off:
                    best_off, worst = off, n
            if worst is not None:
                farm, farm_pos, defend = worst, None, True
        shootable = is_mech = False
        tr = th = 0.0
        tp = tv = None
        if target is not None:
            sc = self.scale(target.cubes)
            tr, th = self.mech_r * sc, self.mech_h * sc
            tp = [target.pos[0], target.pos[1], target.pos[2] + th * 0.5]
            tv = target.vel
            if not self.clear(muzzle, tp):
                tp[2] = target.pos[2] + th * 0.85
            shootable = is_mech = True
        elif farm is not None:
            tr, th = self.cfg["npc"]["radius"], self.cfg["npc"]["height"]
            tp = [farm.pos[0], farm.pos[1], farm.pos[2] + th * 0.5]
            tv = farm.vel
            shootable = True
        elif farm_pos is not None:
            tr, th = self.cfg["crates"]["radius"], self.cfg["crates"]["height"]
            tp, tv = list(farm_pos), [0.0, 0.0, 0.0]
            shootable = True
        if not is_mech and target is None and (self.retreat or hp_frac < 0.5) and not defend:
            shootable = False

        if shootable:
            dist = math.hypot(tp[0] - x, tp[1] - y)
            grounded = abs(tv[2]) < 0.5
            gy_ = gp_ = 0.0
            gun_ok = False
            if gun is not None:
                gy_, gp_ = self.lead(muzzle, tp, tv, gun["stats"]["speed"], grounded)
                gun_ok = dist <= gun["stats"]["range"] * 0.96
            dp = (tp[0] + tv[0] * (LAT + 0.05), tp[1] + tv[1] * (LAT + 0.05), tp[2])
            hy = math.atan2(dp[1] - muzzle[1], dp[0] - muzzle[0])
            hp = math.atan2(dp[2] - muzzle[2], math.hypot(dp[0] - muzzle[0], dp[1] - muzzle[1]))
            aim = [gy_, gp_] if gun is not None else [hy, hp]
            have_aim = True
            tol_y = math.atan2(tr * 0.75, max(dist, 1))
            tol_p = math.atan2(th * 0.4, max(dist, 1))
            turn, prate = st.get("turn_rate", 4), st.get("pitch_rate", 3)

            def slew(wy, wp):
                ey = max(0.0, abs(angdiff(wy, me["aim"][0])) - turn * 0.05)
                ep = max(0.0, abs(wp - me["aim"][1]) - prate * 0.05)
                return ey < tol_y and ep < tol_p
            for i, w in enumerate(ws[:2]):
                if not w:
                    continue
                k = w["kind"]
                wst = w["stats"]
                if k == "gun":
                    if gun_ok and w.get("ammo", 0) > 0 and w.get("reload", 0) <= 0 and slew(gy_, gp_):
                        fire[i] = True
                elif k == "lightning":
                    cone = math.radians(wst["cone_deg"])
                    if dist < wst["range"] - 1.5 and abs(angdiff(hy, me["aim"][0])) < cone * 0.5 - 0.05 and me["energy"] >= wst.get("energy", 3):
                        fire[i] = True
                elif k == "laser":
                    if not is_mech or dist > wst["range"] * 0.97:
                        continue
                    full = wst["charge"]
                    ch = w.get("charge") or 0.0
                    if P.lethal and ch >= wst["min_charge"] and ch < full - 0.051:
                        dmg = wst["damage"] * (wst["min_frac"] + (1 - wst["min_frac"]) * ch / full) * cube_mult
                        if "mortar" in target.weapons:
                            dmg *= 1 + wst.get("cookoff", 0)
                        if dmg * 0.8 >= target.hp and abs(angdiff(hy, me["aim"][0])) < tol_y * 0.9 and abs(hp - me["aim"][1]) < tol_p:
                            aim = [hy, hp]
                            fire[i] = False
                            continue
                    if ch < full - 0.051:
                        fire[i] = True
                        if ch > full * 0.45:
                            aim = [hy, hp]
                    else:
                        aim = [hy, hp]
                        ey = abs(angdiff(hy, me["aim"][0]))
                        ep = abs(hp - me["aim"][1])
                        fire[i] = not (ey < tol_y * 0.9 and ep < tol_p)
        if target is None and foe is not None:
            for i, w in enumerate(ws[:2]):
                if w and w["kind"] == "laser" and w["cooldown"] <= 0.05 and foe.dist < w["stats"]["range"] and \
                        (w.get("charge") or 0) < w["stats"]["charge"] + w["stats"]["hold_s"] - 0.35:
                    fire[i] = True
        if target is None:
            stk = self.tracks.get(self.stick_id)
            if stk is not None and now - self.stick_at < 1 and now - stk.seen < 1:
                for i, w in enumerate(ws[:2]):
                    if not w or w["kind"] != "laser" or (w.get("charge") or 0) <= 0 or fire[i] or stk.dist >= w["stats"]["range"]:
                        continue
                    fire[i] = True
                    c = self.center(stk)
                    aim = [math.atan2(c[1] - muzzle[1], c[0] - muzzle[0]),
                           math.atan2(c[2] - muzzle[2], math.hypot(c[0] - muzzle[0], c[1] - muzzle[1]))]
                    have_aim = True
        if mode in ("cover", "flee", "escape") or (mode == "zone" and not me["in_zone"]):
            for i, w in enumerate(ws[:2]):
                if w and w["kind"] == "laser" and (w.get("charge") or 0) <= 0:
                    fire[i] = False
        if not have_aim:
            watch = None
            for t in seen:
                if t.engage or now - t.hit_me < 6:
                    if watch is None or t.dist < watch.dist:
                        watch = t
            if watch is not None:
                aim[0] = math.atan2(watch.pos[1] - y, watch.pos[0] - x)
            elif now - self.npc_hurt < 2.5:
                aim[0] = self.npc_bearing
            elif now - self.last_hurt < 2.5:
                aim[0] = self.hurt_bearing
            elif mx != 0 or my != 0:
                aim[0] = math.atan2(my, mx) + 1.15 * math.sin(now * 1.3)
            else:
                aim[0] = me["aim"][0] + 0.35
            aim[1] = 0.0
        aim[1] = clamp(aim[1], st.get("min_pitch", -0.7), st.get("max_pitch", 1.3))
        if not (math.isfinite(aim[0]) and math.isfinite(aim[1])):
            aim = list(self.last_aim)
        self.last_aim = list(aim)
        act["aim"] = aim
        act["fire"] = fire

        if gun is not None and gun.get("reload", 0) <= 0 and gun.get("ammo", 0) < gun["stats"]["mag"]:
            if target is None and farm is None and farm_pos is None and (gun.get("ammo", 0) <= gun["stats"]["mag"] * 0.6 or now - self.last_threat_at > 3):
                act["reload"] = True

        slot1 = ws[1]["kind"] if len(ws) > 1 and ws[1] else ""
        reach = self.cfg["loot"]["pickup_radius"]
        for p in o.get("pickups", []):
            if math.hypot(p["pos"][0] - x, p["pos"][1] - y) > reach - 0.3 or abs(p["pos"][2] - zf) > 3.6:
                continue
            if not self.want_pickup(me, p["kind"]):
                continue
            act["take"] = p["id"]
            if slot1 and ws[0] is not None:
                if slot1 == "laser" and (ws[1].get("charge") or 0) > 0:
                    act.pop("take", None)
                    break
                act["drop"] = 1
            break

        if not (math.isfinite(mx) and math.isfinite(my)):
            mx = my = 0.0
        act["move"] = [mx, my]
        act["boost"] = bool(boost and (mx != 0 or my != 0))
        self.mode = mode
        return act

    def _set(self, v):
        return v
