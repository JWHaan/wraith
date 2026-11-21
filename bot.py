"""Survival-first mech bot: stay in the zone, stay out of sight, fight facing the threat, loot when quiet."""
import math
import random
import sys
import time

import mechfight as mf
from world import World

TAU = 2 * math.pi
UPGRADE_PRIORITY = ["armor", "plating", "nanites", "gun_damage", "gun_rate", "lightning_efficiency",
                    "laser_damage", "laser_charge", "lightning_range", "gun_mag", "sensors", "servos",
                    "capacitor", "reactor", "jets", "gun_velocity", "gyros", "lightning_chain",
                    "mortar_reload", "mortar_splash", "mortar_velocity", "laser_pierce"]
WEAPON_PREF = {"lightning": 3, "laser": 2, "mortar": 1, "gun": 0}


def angdiff(a, b):
    return (a - b + math.pi) % TAU - math.pi


def norm2(x, y):
    d = math.hypot(x, y)
    return (x / d, y / d) if d > 1e-9 else (0.0, 0.0)


class Bot:
    name = "survivor"

    def __init__(self, hello):
        self.cfg = hello["config"]
        self.rng = random.Random(hello.get("seed", 1))
        self.world = World()
        self.mem = {}            # mech id -> dict(pos, vel, t, hp, weapons, aim, charging)
        self.threat_dirs = []    # (t, bearing, weight)
        self.flow_goal = None
        self.camp = None
        self.camp_t = -99
        self.cover_pt = None
        self.cover_t = -99
        self.strafe_sign = 1
        self.strafe_until = 0
        self.stuck_t = 0.0
        self.climb_until = -1
        self.last_pos = None
        self.last_dmg_t = -99
        self.crate_target = None
        self.mode = "start"

    # ------------------------------------------------------------------ helpers
    def zone_now(self, z, t):
        if t <= z["shrink_start"]:
            return z["center"], z["radius"]
        if t >= z["shrink_end"]:
            return z["next_center"], z["next_radius"]
        f = (t - z["shrink_start"]) / max(1e-6, z["shrink_end"] - z["shrink_start"])
        c = [z["center"][0] + (z["next_center"][0] - z["center"][0]) * f,
             z["center"][1] + (z["next_center"][1] - z["center"][1]) * f]
        return c, z["radius"] + (z["next_radius"] - z["radius"]) * f

    def safe_circle(self, obs):
        """Circle we want to be inside: the next circle (shrunk by a margin)."""
        z = obs["zone"]
        c, r = z["next_center"], z["next_radius"]
        margin = min(25.0, 0.25 * r)
        return c, max(r - margin, min(r, 6.0))

    def in_circle(self, x, y, c, r):
        return (x - c[0]) ** 2 + (y - c[1]) ** 2 <= r * r

    def ensure_flow(self, c, r, t):
        g = (round(c[0], 1), round(c[1], 1), round(r, 1))
        if g != self.flow_goal:
            self.flow_goal = g
            self.world.start_flow(c[0], c[1], max(r, 4.0))

    # ------------------------------------------------------------------ main
    def act(self, obs):
        t0 = time.perf_counter()
        me = obs["self"]
        t = obs["t"]
        x, y, z = me["pos"]
        scale = 1 + self.cfg["cubes"]["size_per_cube"] * me.get("cubes", 0)
        eye = [x, y, z + me["stats"].get("eye_height", 8)]
        muzzle_h = me["stats"].get("muzzle_height", 7)
        act = {}
        hp_frac = me["hp"] / max(1, me["max_hp"])

        # --- upgrades
        if obs.get("offer"):
            ch = obs["offer"]["choices"]
            best = min(range(len(ch)), key=lambda i: UPGRADE_PRIORITY.index(ch[i]["id"]) if ch[i]["id"] in UPGRADE_PRIORITY else 99)
            act["pick"] = best

        # --- events / memory
        for ev in obs.get("events", []):
            if ev["type"] == "damaged":
                self.last_dmg_t = t
                if ev.get("bearing") is not None and ev.get("weapon") != "zone":
                    self.threat_dirs.append((t, ev["bearing"], 2.0))
        for s in obs.get("sounds", []):
            if s["kind"] in ("gun", "laser", "laser_charge", "lightning", "mortar", "step", "boost", "jet"):
                w = {"near": 1.5, "mid": 0.8, "far": 0.3}.get(s["dist"], 0.3)
                self.threat_dirs.append((t, s["bearing"], w))
        self.threat_dirs = [d for d in self.threat_dirs if t - d[0] < 4.0][-30:]

        mechs = [m for m in obs["mechs"] if m["kind"] == "mech"]
        npcs = [m for m in obs["mechs"] if m["kind"] == "npc"]
        for m in mechs:
            self.mem[m["id"]] = {**m, "t": t}
        self.mem = {k: v for k, v in self.mem.items() if t - v["t"] < 10}
        threats = []
        for k, m in self.mem.items():
            age = t - m["t"]
            p = m["pos"]
            if age > 0:  # dead-reckon a little
                f = min(age, 2.0)
                p = [p[0] + m["vel"][0] * f, p[1] + m["vel"][1] * f, p[2]]
            threats.append((m, p, age))
        threats.sort(key=lambda q: math.dist(q[1][:2], (x, y)))
        near_threats = [q for q in threats if math.dist(q[1][:2], (x, y)) < 220]

        # --- zone
        sc, sr = self.safe_circle(obs)
        zc, zr = self.zone_now(obs["zone"], t)
        self.ensure_flow(sc, sr, t)
        in_safe = self.in_circle(x, y, sc, sr)
        out_zone = not me["in_zone"]
        # time until current shrinking edge reaches us (rough)
        z = obs["zone"]
        urgent = out_zone or (not in_safe and t > z["shrink_start"] - 20)

        # --- choose movement goal
        goal = None
        mode = "camp"
        speed = 1.0
        visible_mechs = [m for m in mechs]
        engaged = len(visible_mechs) > 0 or (t - self.last_dmg_t < 3)

        if urgent:
            mode = "zone"
        elif near_threats and (engaged or near_threats[0][2] < 6):
            # decide fight or flight
            strong = sum(1 for m, p, age in near_threats if age < 3)
            tgt = near_threats[0][0]
            their_hp = tgt.get("hp", 1400)
            if strong <= 1 and hp_frac > 0.45 and their_hp <= me["hp"] * 1.15:
                mode = "fight"
            else:
                mode = "evade"
        else:
            mode = "loot"

        # build target selection for shooting regardless of mode
        target = self.pick_target(me, mechs, npcs, obs, mode)

        move = (0.0, 0.0)
        boost = False
        if mode == "zone":
            d = self.world.flow_dir(x, y)
            if d is None:
                d = norm2(sc[0] - x, sc[1] - y)
            move = d
            boost = me["energy"] > 25 or out_zone
        elif mode == "evade":
            move, boost = self.evade_move(me, near_threats, sc, sr, t)
        elif mode == "fight":
            move, boost = self.fight_move(me, near_threats[0][1], target, sc, sr, t)
        else:
            move, speed = self.loot_move(me, obs, sc, sr, t, target)

        # --- dodge incoming projectiles and laser lines
        dodge = self.dodge(me, obs, mechs, t)
        if dodge is not None:
            move = norm2(move[0] * 0.4 + dodge[0], move[1] * 0.4 + dodge[1])
            speed = 1.0

        # keep inside zone: if move would take us out, bend it
        if not urgent and not self.in_circle(x + move[0] * 8, y + move[1] * 8, zc, zr - 5):
            cx, cy = norm2(zc[0] - x, zc[1] - y)
            move = norm2(move[0] + cx * 1.5, move[1] + cy * 1.5)

        # --- stuck / climbing
        jump = False
        hs = math.hypot(me["vel"][0], me["vel"][1])
        mv = math.hypot(*move) * speed
        if mv > 0.5 and hs < 2.5 and me["grounded"]:
            self.stuck_t += 0.1
        else:
            self.stuck_t = max(0.0, self.stuck_t - 0.05)
        if self.stuck_t > 0.4 and me["energy"] > 30:
            jump = True
            self.climb_until = t + 2.5
            self.stuck_t = 0
            # wiggle a little to unstick from corners
            a = math.atan2(move[1], move[0]) + self.rng.uniform(-0.6, 0.6)
            move = (math.cos(a), math.sin(a))
        if t < self.climb_until and not me["grounded"]:
            boost = me["energy"] > 3
        elif t < self.climb_until and me["grounded"]:
            jump = True

        # --- aim & fire
        aim, fire = self.aim_and_fire(me, target, obs, t, mode)
        act["aim"] = aim
        act["fire"] = fire
        if me["weapons"][0] and me["weapons"][0]["kind"] == "gun":
            w = me["weapons"][0]
            if target is None and w.get("ammo", 99) < w["stats"]["mag"] * 0.5 and w.get("reload", 0) == 0:
                act["reload"] = True

        # --- loot pickup
        tk = self.take_pickup(me, obs)
        if tk:
            act.update(tk)

        # laser charging slows us; that's fine
        act["move"] = [move[0] * speed, move[1] * speed]
        act["boost"] = bool(boost)
        act["jump"] = jump
        self.mode = mode

        # spend leftover budget on the flow field
        used = time.perf_counter() - t0
        self.world.step_flow(max(0.0, 0.015 - used))
        mf.draw.label(f"{mode} hp{me['hp']:.0f} thr{len(near_threats)}")
        return act

    # ------------------------------------------------------------------ targeting
    def pick_target(self, me, mechs, npcs, obs, mode):
        x, y, z = me["pos"]
        best, bs = None, 1e18
        for m in mechs:
            d = math.dist(m["pos"][:2], (x, y))
            if d > 230:
                continue
            s = d + m.get("hp", 1400) * 0.05
            if s < bs:
                bs, best = s, ("mech", m)
        if best:
            return best
        if mode == "loot" and me["hp"] / me["max_hp"] > 0.6:
            for m in npcs:
                d = math.dist(m["pos"][:2], (x, y))
                if d < 90 and d < bs:
                    bs, best = d, ("npc", m)
            if best:
                return best
            # crates
            for c in obs.get("crates", []):
                d = math.dist(c["pos"][:2], (x, y))
                if d < 110 and d < bs:
                    bs, best = d, ("crate", {"pos": c["pos"], "vel": [0, 0, 0], "id": c["id"]})
            if best:
                return best
        return None

    def lead(self, src, p, v, speed, zoff):
        tx, ty, tz = p[0], p[1], p[2] + zoff
        tt = 0.0
        for _ in range(3):
            tt = math.dist(src, (tx, ty, tz)) / speed
            tx, ty, tz = p[0] + v[0] * tt, p[1] + v[1] * tt, p[2] + zoff + (v[2] * tt if abs(v[2]) < 15 else 0)
        return (tx, ty, tz), tt

    def aim_and_fire(self, me, target, obs, t, mode):
        x, y, z = me["pos"]
        mh = me["stats"].get("muzzle_height", 7)
        src = (x, y, z + mh)
        fire = [False, False]
        weapons = me["weapons"]
        if target is None:
            # face the threat direction, or scan
            bearing = self.threat_bearing(t)
            if bearing is None:
                if self.mode == "zone":
                    sc = obs["zone"]["next_center"]
                    base = math.atan2(sc[1] - y, sc[0] - x)
                    bearing = base + 0.9 * math.sin(t * 1.3)
                else:
                    bearing = (t * 1.1) % TAU
            # keep charging laser off
            return [bearing, 0.0], fire
        kind, m = target
        zoff = 3.0 if kind == "npc" else (0.5 if kind == "crate" else 4.5 * (1 + 0.05 * m.get("cubes", 0)))
        gun = weapons[0] if weapons[0] and weapons[0]["kind"] == "gun" else None
        speed = gun["stats"]["speed"] if gun else 252
        pt, _ = self.lead(src, m["pos"], m.get("vel", [0, 0, 0]), speed, zoff)
        dx, dy, dz = pt[0] - src[0], pt[1] - src[1], pt[2] - src[2]
        dh = math.hypot(dx, dy)
        yaw = math.atan2(dy, dx)
        pitch = math.atan2(dz, dh)
        aim = [yaw, pitch]
        err = abs(angdiff(me["aim"][0], yaw))
        perr = abs(me["aim"][1] - pitch)
        tol = math.atan2(2.2, max(dh, 1)) + 0.01
        clear = self.world.los(src, pt)
        visible = kind != "mech" or m.get("t", t) >= t - 0.01 or True
        # gun (slot 0 or whatever slot holds it)
        for si, w in enumerate(weapons):
            if not w:
                continue
            k = w["kind"]
            st = w["stats"]
            if k == "gun":
                if dh < st["range"] and err < tol and perr < tol * 1.5 + 0.02 and clear and w.get("reload", 0) == 0:
                    fire[si] = True
            elif k == "lightning":
                if kind != "crate" and dh < st["range"] * 0.95 and err < math.radians(st["cone_deg"]) * 0.45 and me["energy"] > st.get("energy", 3) + 5 and clear:
                    fire[si] = True
            elif k == "laser":
                charge = w.get("charge", 0) or 0
                full = st["charge"]
                if kind == "crate":
                    continue
                if charge > 0:
                    # hold until full and on target, then release
                    if charge >= full * 0.98 and err < tol and clear:
                        fire[si] = False
                    else:
                        fire[si] = True
                elif w["cooldown"] <= 0 and dh < st["range"] and clear and err < 0.35 and kind == "mech":
                    fire[si] = True
            elif k == "mortar":
                if kind == "mech" and 35 < dh < 220 and w["cooldown"] <= 0:
                    sol = self.mortar_pitch(src[2] + 2.5, m["pos"], m.get("vel", [0, 0, 0]), (x, y), st)
                    if sol is not None:
                        myaw, mp = sol
                        # only fire mortar if we aim for it: use when gun can't see target
                        if not clear or not gun:
                            aim = [myaw, mp]
                            if abs(angdiff(me["aim"][0], myaw)) < 0.05 and abs(me["aim"][1] - mp) < 0.05:
                                fire[si] = True
        return aim, fire

    def mortar_pitch(self, sz, p, v, xy, st):
        g, sp = st["gravity"], st["speed"]
        tx, ty = p[0], p[1]
        for _ in range(2):
            dx, dy = tx - xy[0], ty - xy[1]
            d = math.hypot(dx, dy)
            h = p[2] + 1 - sz
            # tan(theta) = (v^2 - sqrt(v^4 - g(g d^2 + 2 h v^2))) / (g d)  (low arc)
            disc = sp ** 4 - g * (g * d * d + 2 * h * sp * sp)
            if disc < 0:
                return None
            th = math.atan2(sp * sp + math.sqrt(disc), g * d)  # high arc, clears buildings
            tof = d / max(1e-3, sp * math.cos(th))
            tx, ty = p[0] + v[0] * tof, p[1] + v[1] * tof
        if th > 1.25:
            th = 1.25
        return math.atan2(ty - xy[1], tx - xy[0]), th

    def threat_bearing(self, t):
        if not self.threat_dirs:
            return None
        sx = sy = 0.0
        for (tt, b, w) in self.threat_dirs:
            k = w * max(0.1, 1 - (t - tt) / 4)
            sx += math.cos(b) * k
            sy += math.sin(b) * k
        if abs(sx) + abs(sy) < 1e-6:
            return None
        return math.atan2(sy, sx)

    # ------------------------------------------------------------------ movement modes
    def evade_move(self, me, threats, sc, sr, t):
        x, y, z = me["pos"]
        if self.cover_pt is None or t - self.cover_t > 0.6 or math.dist(self.cover_pt, (x, y)) < 3:
            self.cover_pt = self.find_cover(me, threats, sc, sr)
            self.cover_t = t
        cp = self.cover_pt
        if cp is None:
            # run directly away from the nearest threat, toward the safe circle
            p = threats[0][1]
            ax, ay = norm2(x - p[0], y - p[1])
            cx, cy = norm2(sc[0] - x, sc[1] - y)
            return norm2(ax + 0.6 * cx, ay + 0.6 * cy), me["energy"] > 40
        d = math.dist(cp, (x, y))
        if d < 2.5:
            return self.strafe(me, threats[0][1], t, 0.5), False
        dirv = norm2(cp[0] - x, cp[1] - y)
        return dirv, me["energy"] > 35

    def find_cover(self, me, threats, sc, sr):
        x, y, z = me["pos"]
        W = self.world
        ths = [(p, m) for m, p, age in threats[:4]]
        best, bs = None, -1e18
        for ring in (12, 24, 40, 60):
            n = 12
            off = self.rng.uniform(0, TAU)
            for k in range(n):
                a = off + TAU * k / n
                px, py = x + ring * math.cos(a), y + ring * math.sin(a)
                if W.is_roof(px, py):
                    continue
                gz = W.ground_at(px, py)
                # path must be roughly walkable: sample midpoint
                if W.is_roof((x + px) / 2, (y + py) / 2):
                    continue
                s = 0.0
                for p, m in ths:
                    if not W.los((p[0], p[1], p[2] + 8), (px, py, gz + 6)):
                        s += 60
                    dd = math.dist((px, py), p[:2])
                    s += min(dd, 150) * 0.25
                    # don't run past them
                    if dd < math.dist((x, y), p[:2]) - 5:
                        s -= 30
                s += W.cover_at(px, py) * 0.8
                s -= ring * 0.25
                if not self.in_circle(px, py, sc, sr + 10):
                    s -= 80
                if s > bs:
                    bs, best = s, (px, py)
        return best

    def strafe(self, me, threat_pos, t, w=1.0):
        x, y, _ = me["pos"]
        if t > self.strafe_until:
            self.strafe_sign = -self.strafe_sign
            self.strafe_until = t + self.rng.uniform(0.6, 1.4)
        ax, ay = norm2(threat_pos[0] - x, threat_pos[1] - y)
        return (-ay * self.strafe_sign * w, ax * self.strafe_sign * w)

    def fight_move(self, me, tp, target, sc, sr, t):
        x, y, z = me["pos"]
        d = math.dist(tp[:2], (x, y))
        has_ltg = any(w and w["kind"] == "lightning" for w in me["weapons"])
        want = 32 if has_ltg else 70
        ax, ay = norm2(tp[0] - x, tp[1] - y)
        sx, sy = self.strafe(me, tp, t)
        if d > want + 15:
            mvx, mvy = ax * 0.8 + sx * 0.6, ay * 0.8 + sy * 0.6
        elif d < want - 15:
            mvx, mvy = -ax * 0.7 + sx * 0.7, -ay * 0.7 + sy * 0.7
        else:
            mvx, mvy = sx, sy
        return norm2(mvx, mvy), False

    def loot_move(self, me, obs, sc, sr, t, target):
        x, y, z = me["pos"]
        W = self.world
        cands = []
        # cubes
        for c in obs.get("cubes", []):
            if self.in_circle(c["pos"][0], c["pos"][1], sc, sr + 20):
                d = math.dist(c["pos"][:2], (x, y))
                if d < 100:
                    cands.append((d * 0.7, c["pos"]))
        # weapons worth taking
        cur = me["weapons"][1]
        cur_pref = WEAPON_PREF.get(cur["kind"], -1) if cur else -1
        held = {w["kind"] for w in me["weapons"] if w}
        for p in obs.get("pickups", []):
            if p["kind"] in held:
                continue
            if WEAPON_PREF.get(p["kind"], -1) > cur_pref:
                d = math.dist(p["pos"][:2], (x, y))
                if d < 120 and abs(p["pos"][2] - z) < 25 and self.in_circle(p["pos"][0], p["pos"][1], sc, sr + 30):
                    cands.append((d * (0.6 if cur is None else 0.9), p["pos"]))
        # supply drops on the ground, uncontested
        for dr in obs.get("drops", []):
            d = math.dist(dr["pos"][:2], (x, y))
            if dr["lands_in"] < d / 11 + 1 and d < 140 and (dr["holder"] in (None, me["id"])):
                cands.append((d * 0.8 - 20, dr["pos"]))
        # crates / npcs targeted: approach to shooting distance
        if target and target[0] in ("crate", "npc"):
            tp = target[1]["pos"]
            d = math.dist(tp[:2], (x, y))
            if target[0] == "npc":
                if d > 60:
                    cands.append((d, tp))
                else:
                    sx, sy = self.strafe(me, tp, t)
                    return (sx, sy), 0.8
            elif d > 70:
                cands.append((d, tp))
        if cands:
            cands.sort(key=lambda c: c[0])
            gp = cands[0][1]
            return self.walk_to(me, gp), 1.0
        # camp: pick a covered spot inside the safe circle
        if self.camp is None or t - self.camp_t > 8 or not self.in_circle(self.camp[0], self.camp[1], sc, sr):
            self.camp = self.find_camp(me, sc, sr)
            self.camp_t = t
        d = math.dist(self.camp, (x, y))
        if d < 3:
            return (0.0, 0.0), 0.0
        return self.walk_to(me, self.camp), (1.0 if d > 25 else 0.42)

    def walk_to(self, me, p):
        x, y, _ = me["pos"]
        W = self.world
        d = math.dist(p[:2], (x, y))
        dv = norm2(p[0] - x, p[1] - y)
        if d > 12:
            # check straight line for walls; if blocked, fall back to the zone flow
            mx, my = x + dv[0] * 10, y + dv[1] * 10
            if W.surf_at(mx, my) - W.surf_at(x, y) > 2.0 and not W.is_roof(p[0], p[1]):
                fd = W.flow_dir(x, y)
                if fd:
                    # sidestep along the wall
                    perp = (-dv[1], dv[0])
                    if perp[0] * fd[0] + perp[1] * fd[1] < 0:
                        perp = (dv[1], -dv[0])
                    return norm2(perp[0] + dv[0] * 0.3, perp[1] + dv[1] * 0.3)
        return dv

    def find_camp(self, me, sc, sr):
        x, y, _ = me["pos"]
        W = self.world
        best, bs = None, -1e18
        rr = max(4.0, sr * 0.75)
        for _ in range(80):
            a = self.rng.uniform(0, TAU)
            r = rr * math.sqrt(self.rng.random())
            px, py = sc[0] + r * math.cos(a), sc[1] + r * math.sin(a)
            if W.is_roof(px, py):
                continue
            s = W.cover_at(px, py) * 1.0 - math.dist((px, py), (x, y)) / 12.0 - r / max(sr, 1) * 6
            if s > bs:
                bs, best = s, (px, py)
        return best or (sc[0], sc[1])

    def dodge(self, me, obs, mechs, t):
        x, y, z = me["pos"]
        W = self.world
        g = self.cfg["weapons"]["mortar"]["gravity"]
        best = None
        for p in obs.get("projectiles", []):
            if p["kind"] != "mortar":
                continue
            px, py, pz = p["pos"]
            vx, vy, vz = p["vel"]
            # find impact time against ground (approx)
            for k in range(1, 70):
                tt = k * 0.1
                qz = pz + vz * tt - 0.5 * g * tt * tt
                qx, qy = px + vx * tt, py + vy * tt
                if qz <= W.surf_at(qx, qy):
                    if math.dist((qx, qy), (x, y)) < 12:
                        best = norm2(x - qx, y - qy)
                    break
        if best:
            return best
        for m in mechs:
            if m.get("charging"):
                b = math.atan2(y - m["pos"][1], x - m["pos"][0])
                if abs(angdiff(m["aim"][0], b)) < 0.15:
                    return self.strafe(me, m["pos"], t)
        return None

    def take_pickup(self, me, obs):
        x, y, z = me["pos"]
        reach = self.cfg["loot"]["pickup_radius"]
        held = {w["kind"] for w in me["weapons"] if w}
        cur = me["weapons"][1]
        if cur and cur["kind"] == "laser" and (cur.get("charge") or 0) > 0:
            return None
        cur_pref = WEAPON_PREF.get(cur["kind"], -1) if cur else -1
        best = None
        for p in obs.get("pickups", []):
            if p["kind"] in held:
                continue
            if math.dist(p["pos"][:2], (x, y)) > reach - 0.3 or abs(p["pos"][2] - z) > 3.8:
                continue
            pref = WEAPON_PREF.get(p["kind"], -1)
            if pref > cur_pref and (best is None or pref > WEAPON_PREF[best["kind"]]):
                best = p
        if not best:
            return None
        if cur is None:
            return {"take": best["id"]}
        return {"take": best["id"], "drop": 1}


if __name__ == "__main__":
    mf.run(Bot)
