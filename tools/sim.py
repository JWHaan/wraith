"""Rough local practice simulator: one bot vs 15 house-style hunters (shared vision, never fight each other).

Not the real game. Use it for A/B comparisons and smoke tests, then confirm with real submissions.

  python3 tools/sim.py --bot bot.py --matches 24 --seed 1
  python3 tools/sim.py --bot ../kit/mechfight-kit/python/example_bot.py --matches 24
  python3 tools/sim.py --bot bot.py --compare old_bot_dir/bot.py --matches 48     # paired A/B on same seeds
"""
import argparse
import copy
import importlib.util
import json
import math
import multiprocessing as mp
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from world import World  # noqa: E402

TAU = 2 * math.pi
DT = 0.05


def angdiff(a, b):
    return (a - b + math.pi) % TAU - math.pi


def norm2(x, y):
    d = math.hypot(x, y)
    return (x / d, y / d) if d > 1e-9 else (0.0, 0.0)


HELLO = json.load(open(os.path.join(HERE, "hello.json")))
CFG = HELLO["config"]
_WORLD = None


def world():
    global _WORLD
    if _WORLD is None:
        _WORLD = World()
    return _WORLD


# ----------------------------------------------------------------------------- bot loading
def load_bot_class(path):
    import mechfight
    captured = []
    real_run = mechfight.run
    mechfight.run = lambda cls: captured.append(cls)
    d = os.path.dirname(os.path.abspath(path))
    sys.path.insert(0, d)
    # make sure the bot's own helper modules are loaded from its folder
    for m in ("world",):
        if m in sys.modules and not getattr(sys.modules[m], "__file__", "").startswith(d):
            del sys.modules[m]
    name = "botmod_%d" % abs(hash(os.path.abspath(path)))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    mod.__name__ = name
    spec.loader.exec_module(mod)
    mechfight.run = real_run
    sys.path.remove(d)
    if captured:
        return captured[0]
    return mod.Bot


# ----------------------------------------------------------------------------- entities
NOISE_MULT = float(os.environ.get("SIM_NOISE", "13.0"))
HUNT_MULT = float(os.environ.get("SIM_HUNT", "1.0"))
TIERS = ["easy"] * 4 + ["medium"] * 4 + ["hard"] * 4 + ["expert"] * 3
TIER = {
    # aim noise (rad), reaction delay (s), lead?, keeps distance, sound-chase prob
    "easy": dict(noise=math.radians(4.0), react=0.5, lead=True, dist=70, chase=0.3, zone_late=True, hunt=150, fire=110),
    "medium": dict(noise=math.radians(2.5), react=0.4, lead=True, dist=80, chase=0.5, zone_late=False, hunt=260, fire=140),
    "hard": dict(noise=math.radians(1.3), react=0.3, lead=True, dist=90, chase=0.7, zone_late=False, hunt=420, fire=170),
    "expert": dict(noise=math.radians(0.7), react=0.2, lead=True, dist=90, chase=0.9, zone_late=False, hunt=1e9, fire=200),
}


class Mech:
    def __init__(self, mid, pos, house, tier=None):
        m = CFG["mech"]
        self.id = mid
        self.pos = list(pos)
        self.vel = [0.0, 0.0, 0.0]
        self.aim = [0.0, 0.0]
        self.cmd = {}
        self.house = house
        self.tier = tier
        self.stats = {k: v for k, v in m.items()}
        self.base_hp = m["hp"]
        self.max_hp = m["hp"]
        self.hp = m["hp"]
        self.energy = m["energy"]
        self.grounded = True
        self.cubes = 0
        self.wstats = {k: dict(v) for k, v in CFG["weapons"].items()}
        g = self.wstats["gun"]
        self.weapons = [{"kind": "gun", "cooldown": 0.0, "ammo": g["mag"], "reload": 0.0}, None]
        self.upgrades = []
        self.up_count = {}
        self.alive = True
        self.last_combat = -99
        self.last_spend = -99
        self.events = []
        self.offers = []
        self.offer_id = 0
        self.offer_expires = 0
        self.thresholds_hit = 0
        self.short_until = -1
        self.step_acc = 0.0
        self.charge_hold = 0.0
        self.death_t = None
        self.place = None
        self.kills = 0
        self.last_shot_t = -99

    @property
    def scale(self):
        return 1 + CFG["cubes"]["size_per_cube"] * self.cubes

    @property
    def radius(self):
        return CFG["mech"]["radius"] * self.scale

    @property
    def height(self):
        return CFG["mech"]["height"] * self.scale

    def eye(self):
        return (self.pos[0], self.pos[1], self.pos[2] + CFG["mech"]["eye_height"] * self.scale)

    def muzzle(self):
        return (self.pos[0], self.pos[1], self.pos[2] + CFG["mech"]["muzzle_height"] * self.scale)

    def center(self):
        return (self.pos[0], self.pos[1], self.pos[2] + self.height / 2)

    def has(self, kind):
        return any(w and w["kind"] == kind for w in self.weapons)

    def dmg_mult(self):
        c = CFG["cubes"]
        return 1 + c["damage_per_cube"] * min(self.cubes, c["stat_cap"])

    def recompute_hp(self):
        c = CFG["cubes"]
        mul = 1.0
        for u in self.upgrades:
            for e in UPGRADES[u]["effects"]:
                if e["path"] == "mech.hp":
                    mul *= e.get("mul", 1)
        new = self.base_hp * mul * (1 + c["hp_per_cube"] * min(self.cubes, c["stat_cap"]))
        if new > self.max_hp:
            self.hp += new - self.max_hp
        self.max_hp = new
        self.hp = min(self.hp, self.max_hp)


UPGRADES = {u["id"]: u for u in CFG["upgrades"]}


def apply_upgrade(m, uid):
    u = UPGRADES[uid]
    m.upgrades.append(uid)
    m.up_count[uid] = m.up_count.get(uid, 0) + 1
    for e in u["effects"]:
        parts = e["path"].split(".")
        if parts[0] == "mech":
            k = parts[1]
            if k == "hp":
                continue
            v = m.stats.get(k, 0)
            m.stats[k] = v * e["mul"] if "mul" in e else v + e.get("add", 0)
        elif parts[0] == "weapons":
            st = m.wstats[parts[1]]
            v = st.get(parts[2], 0)
            st[parts[2]] = v * e["mul"] if "mul" in e else v + e.get("add", 0)
    m.recompute_hp()
    m.events.append({"type": "upgrade", "id": uid})


# ----------------------------------------------------------------------------- match
class Match:
    def __init__(self, seed, bot_cls, slot=None):
        self.rng = random.Random(seed)
        self.W = world()
        self.t = 0.0
        self.tick = 0
        self.next_id = {"npc": 100, "crate": 1000, "cube": 5000, "pick": 8000, "drop": 20000}
        self.slot = slot if slot is not None else self.rng.randrange(16)
        self.setup_zone()
        self.mechs = []
        spawns = self.spawn_points(16)
        tiers = TIERS[:]
        self.rng.shuffle(tiers)
        ti = 0
        for i in range(16):
            if i == self.slot:
                self.mechs.append(Mech(i, spawns[i], False))
            else:
                self.mechs.append(Mech(i, spawns[i], True, tiers[ti]))
                ti += 1
        for m in self.mechs:
            m.aim = [self.rng.uniform(-math.pi, math.pi), 0.0]
        self.me = self.mechs[self.slot]
        self.projectiles = []
        self.pickups = {}
        self.crates = {}
        self.cubes = {}
        self.npcs = []
        self.drops = []
        self.beams = []
        self.sounds = []  # (t, kind, pos, src_id)
        self.alive_count = 16
        self.house_mem = None  # (t_seen, pos)
        self.house_heard = None
        self.spawn_loot()
        hello = copy.deepcopy(HELLO)
        hello["slot"] = self.slot
        hello["seed"] = self.rng.randrange(1 << 31)
        hello["map"]["dir"] = os.path.join(ROOT, "..", "kit", "mechfight-kit", "arena")
        self.bot = bot_cls(hello)
        self.hb = {m.id: {"wp": None, "wp_t": -99, "stuck": 0, "climb": -1, "strafe": 1, "strafe_t": 0,
                          "react": 0, "inv": None} for m in self.mechs if m.house}
        self.bot_ms = []
        self.flow_goal = None

    # --- setup
    def free_ground(self, cx, cy, r, tries=200):
        W = self.W
        b = (-480, 520, -300, 355)
        for _ in range(tries):
            a = self.rng.uniform(0, TAU)
            rr = r * math.sqrt(self.rng.random())
            x, y = cx + rr * math.cos(a), cy + rr * math.sin(a)
            if not (b[0] < x < b[1] and b[2] < y < b[3]):
                continue
            if W.is_roof(x, y) or W.is_roof(x + 3, y) or W.is_roof(x - 3, y) or W.is_roof(x, y + 3) or W.is_roof(x, y - 3):
                continue
            return (x, y, W.surf_at(x, y))
        return (cx, cy, W.surf_at(cx, cy))

    def roof_point(self):
        W = self.W
        for _ in range(500):
            x, y = self.rng.uniform(-480, 520), self.rng.uniform(-300, 355)
            if W.is_roof(x, y) and W.surf_at(x, y) - W.ground_at(x, y) < 40:
                return (x, y, W.surf_at(x, y))
        return self.free_ground(0, 0, 300)

    def spawn_points(self, n):
        pts = []
        for _ in range(n):
            best = None
            for _ in range(60):
                p = self.free_ground(41.3, -72.8, 450)
                md = min((math.dist(p[:2], q[:2]) for q in pts), default=1e9)
                if md >= CFG["match"]["spawn_min_separation"]:
                    best = p
                    break
                if best is None or md > best[1]:
                    best = (p, md)
            if isinstance(best[0], tuple):
                best = best[0]
            pts.append(best)
        return pts

    def setup_zone(self):
        z = CFG["zone"]
        c, r = list(z["start_center"]), z["start_radius"]
        t = 0.0
        self.phases = []
        for ph in z["phases"]:
            nr = ph["radius"]
            for _ in range(100):
                a = self.rng.uniform(0, TAU)
                d = (r - nr) * math.sqrt(self.rng.random()) * 0.9
                nc = [c[0] + d * math.cos(a), c[1] + d * math.sin(a)]
                if -380 < nc[0] < 420 and -230 < nc[1] < 280:
                    break
            s0 = t + ph["hold_s"]
            s1 = s0 + ph["shrink_s"]
            self.phases.append(dict(center=c, radius=r, next_center=nc, next_radius=nr, shrink_start=s0, shrink_end=s1,
                                    dps=ph["dps"], start=t))
            c, r, t = nc, nr, s1

    def zone_phase(self):
        for ph in self.phases:
            if self.t < ph["shrink_end"]:
                return ph
        return self.phases[-1]

    def zone_now(self):
        ph = self.zone_phase()
        if self.t <= ph["shrink_start"]:
            return ph["center"], ph["radius"], ph["dps"]
        f = min(1.0, (self.t - ph["shrink_start"]) / (ph["shrink_end"] - ph["shrink_start"]))
        c = [ph["center"][0] + (ph["next_center"][0] - ph["center"][0]) * f,
             ph["center"][1] + (ph["next_center"][1] - ph["center"][1]) * f]
        return c, ph["radius"] + (ph["next_radius"] - ph["radius"]) * f, ph["dps"]

    def nid(self, k):
        self.next_id[k] += 1
        return self.next_id[k]

    def spawn_loot(self):
        kinds = ["mortar", "laser", "lightning"]
        for i in range(CFG["loot"]["floor_count"]):
            p = self.roof_point() if self.rng.random() < CFG["loot"]["floor_roof_fraction"] else self.free_ground(41, -72, 480)
            self.pickups[self.nid("pick")] = {"kind": self.rng.choice(kinds), "pos": list(p)}
        for i in range(CFG["crates"]["count"]):
            p = self.roof_point() if self.rng.random() < CFG["crates"]["roof_fraction"] else self.free_ground(41, -72, 480)
            self.crates[self.nid("crate")] = {"pos": list(p), "hp": CFG["crates"]["hp"]}
        for i in range(CFG["npc"]["count"]):
            p = self.free_ground(41, -72, 450)
            self.npcs.append({"id": self.nid("npc"), "pos": list(p), "home": p, "hp": CFG["npc"]["hp"], "vel": [0, 0, 0],
                              "aim": [0, 0], "cd": 0, "target": None, "alive": True, "wp": p})

    # --- sounds
    def sound(self, kind, pos, src):
        self.sounds.append((self.t, kind, tuple(pos), src))
        if src != self.me.id and kind == "gun" and False:
            pass

    # --- damage
    def damage(self, target, amount, attacker, weapon, frm, effect=None):
        if not target.alive:
            return
        mult = 1.0
        rear = False
        if frm is not None:
            yaw = math.atan2(frm[1] - target.pos[1], frm[0] - target.pos[0])
            if abs(angdiff(yaw, target.aim[0])) > math.radians(target.stats["front_arc_deg"]) / 2:
                mult *= target.stats["rear_mult"]
                rear = True
        if weapon != "zone":
            mult *= max(0.0, 1 - target.stats.get("armor", 0))
        amt = amount * mult
        target.hp -= amt
        target.last_combat = self.t
        bearing = None if frm is None else math.atan2(frm[1] - target.pos[1], frm[0] - target.pos[0])
        target.events.append({"type": "damaged", "amount": round(amt, 2), "weapon": weapon, "bearing": bearing,
                              "by": attacker.id if attacker else None, "rear": rear})
        if attacker is not None:
            attacker.last_combat = self.t
            attacker.events.append({"type": "hit", "target": target.id, "amount": round(amt, 2), "weapon": weapon, "rear": rear})
            if not attacker.house and target.house:
                pass
        if target.hp <= 0:
            self.kill(target, attacker)

    def kill(self, m, attacker):
        m.alive = False
        m.death_t = self.t
        m.place = self.alive_count  # same-tick deaths fixed below
        self.dead_this_tick.append(m)
        if attacker is not None and attacker is not m:
            attacker.kills += 1
            attacker.events.append({"type": "kill", "target": m.id})
        for _ in range(m.cubes + CFG["cubes"]["death_drop_extra"]):
            self.cubes[self.nid("cube")] = {"pos": [m.pos[0] + self.rng.uniform(-3, 3), m.pos[1] + self.rng.uniform(-3, 3), m.pos[2]]}
        for w in m.weapons[1:]:
            if w and w["kind"] != "gun":
                self.pickups[self.nid("pick")] = {"kind": w["kind"], "pos": list(m.pos)}
        self.sound("death", m.pos, m.id)

    def damage_npc(self, n, amount, attacker):
        n["hp"] -= amount
        n["target"] = attacker
        attacker.last_combat = self.t
        attacker.events.append({"type": "hit", "target": n["id"], "amount": amount, "weapon": "x"})
        if n["hp"] <= 0 and n["alive"]:
            n["alive"] = False
            for _ in range(CFG["npc"]["cubes"]):
                self.cubes[self.nid("cube")] = {"pos": [n["pos"][0] + self.rng.uniform(-2, 2), n["pos"][1] + self.rng.uniform(-2, 2), n["pos"][2]]}

    def damage_crate(self, cid, amount, attacker):
        c = self.crates.get(cid)
        if not c:
            return
        c["hp"] -= amount
        attacker.events.append({"type": "hit", "target": cid, "amount": amount, "weapon": "x"})
        if c["hp"] <= 0:
            del self.crates[cid]
            self.sound("crate", c["pos"], -1)
            self.cubes[self.nid("cube")] = {"pos": list(c["pos"])}
            if self.rng.random() < CFG["loot"]["crate_weapon_chance"]:
                self.pickups[self.nid("pick")] = {"kind": self.rng.choice(["mortar", "laser", "lightning"]), "pos": list(c["pos"])}

    # --- line hit helpers
    def mech_hit(self, p, exclude):
        for m in self.mechs:
            if not m.alive or m is exclude:
                continue
            if (p[0] - m.pos[0]) ** 2 + (p[1] - m.pos[1]) ** 2 <= (m.radius + 0.3) ** 2 and m.pos[2] <= p[2] <= m.pos[2] + m.height:
                return m
        return None

    def npc_hit(self, p):
        for n in self.npcs:
            if n["alive"] and (p[0] - n["pos"][0]) ** 2 + (p[1] - n["pos"][1]) ** 2 <= 2.1 ** 2 and n["pos"][2] <= p[2] <= n["pos"][2] + 3.5:
                return n
        return None

    def crate_hit(self, p):
        for cid, c in self.crates.items():
            if (p[0] - c["pos"][0]) ** 2 + (p[1] - c["pos"][1]) ** 2 <= 1.7 ** 2 and c["pos"][2] <= p[2] <= c["pos"][2] + 2.4:
                return cid
        return None

    # --- weapons
    def fire_weapons(self, m):
        cmd = m.cmd
        fire = cmd.get("fire") or [False, False]
        for si in range(2):
            w = m.weapons[si]
            if not w:
                continue
            st = m.wstats[w["kind"]]
            held = bool(fire[si]) if si < len(fire) else False
            k = w["kind"]
            if k == "gun":
                if w["reload"] > 0:
                    continue
                if held and w["cooldown"] <= 0 and w["ammo"] > 0:
                    sp = math.radians(st["spread_deg"])
                    yaw = m.aim[0] + self.rng.gauss(0, sp)
                    pit = m.aim[1] + self.rng.gauss(0, sp)
                    d = (math.cos(pit) * math.cos(yaw), math.cos(pit) * math.sin(yaw), math.sin(pit))
                    mz = m.muzzle()
                    o = [mz[0] + d[0] * 3, mz[1] + d[1] * 3, mz[2] + d[2] * 3]
                    self.projectiles.append({"id": self.nid("pick") + 100000, "kind": "gun", "pos": o,
                                             "vel": [d[0] * st["speed"], d[1] * st["speed"], d[2] * st["speed"]],
                                             "owner": m, "dmg": st["damage"] * m.dmg_mult(), "left": st["range"], "g": 0})
                    w["ammo"] -= 1
                    w["cooldown"] = st["cooldown"]
                    m.last_shot_t = self.t
                    self.sound("gun", m.pos, m.id)
                    if w["ammo"] <= 0:
                        w["reload"] = st["reload"]
            elif k == "lightning":
                if held and w["cooldown"] <= 0 and m.energy >= st["energy"]:
                    m.energy -= st["energy"]
                    m.last_spend = self.t
                    w["cooldown"] = st["cooldown"]
                    self.sound("lightning", m.pos, m.id)
                    self.lightning(m, st)
            elif k == "laser":
                if self.t < m.short_until:
                    w["charge"] = 0
                    continue
                ch = w.get("charge", 0)
                if held and w["cooldown"] <= 0:
                    if ch < st["charge"]:
                        w["charge"] = min(st["charge"], ch + DT)
                        if ch == 0:
                            self.sound("laser_charge", m.pos, m.id)
                    else:
                        m.charge_hold += DT
                        if m.charge_hold >= st["hold_s"]:
                            self.laser(m, w, st)
                elif ch > 0:
                    if ch >= st["min_charge"]:
                        self.laser(m, w, st)
                    else:
                        w["charge"] = ch + DT
            elif k == "mortar":
                if held and w["cooldown"] <= 0:
                    w["cooldown"] = st["cooldown"]
                    d = (math.cos(m.aim[1]) * math.cos(m.aim[0]), math.cos(m.aim[1]) * math.sin(m.aim[0]), math.sin(m.aim[1]))
                    o = [m.pos[0] + d[0] * 3, m.pos[1] + d[1] * 3, m.pos[2] + m.height + 0.5 + d[2] * 3]
                    self.projectiles.append({"id": self.nid("pick") + 100000, "kind": "mortar", "pos": o,
                                             "vel": [d[0] * st["speed"], d[1] * st["speed"], d[2] * st["speed"]],
                                             "owner": m, "dmg": st["damage"] * m.dmg_mult(), "left": 1e9, "g": st["gravity"],
                                             "ttl": st["ttl"], "st": st})
                    self.sound("mortar", m.pos, m.id)

    def friendly(self, a, b):
        return a.house and b.house

    def laser(self, m, w, st):
        frac = st["min_frac"] + (1 - st["min_frac"]) * min(1.0, w["charge"] / st["charge"])
        dmg = st["damage"] * frac * m.dmg_mult()
        w["charge"] = 0
        m.charge_hold = 0
        w["cooldown"] = st["cooldown"]
        self.sound("laser", m.pos, m.id)
        mz = m.muzzle()
        d = (math.cos(m.aim[1]) * math.cos(m.aim[0]), math.cos(m.aim[1]) * math.sin(m.aim[0]), math.sin(m.aim[1]))
        p = mz
        end = p
        for s in range(3, int(st["range"]), 2):
            q = (mz[0] + d[0] * s, mz[1] + d[1] * s, mz[2] + d[2] * s)
            end = q
            if self.W.surf_at(q[0], q[1]) > q[2]:
                break
            t = self.mech_hit(q, m)
            if t and not self.friendly(m, t):
                self.damage(t, dmg * (1 + st["cookoff"] if t.has("mortar") else dmg) if False else dmg * ((1 + st["cookoff"]) if t.has("mortar") else 1), m, "laser", mz)
                break
            n = self.npc_hit(q)
            if n:
                self.damage_npc(n, dmg, m)
                break
            c = self.crate_hit(q)
            if c:
                self.damage_crate(c, dmg, m)
                break
        self.beams.append((self.t, m.id, "laser", [list(mz), list(end)]))

    def lightning(self, m, st):
        mz = m.muzzle()
        best, bd = None, 1e9
        cone = math.radians(st["cone_deg"]) / 2
        cands = []
        for o in self.mechs:
            if o is not m and o.alive and not self.friendly(m, o):
                cands.append(("m", o, o.center()))
        for n in self.npcs:
            if n["alive"]:
                cands.append(("n", n, (n["pos"][0], n["pos"][1], n["pos"][2] + 1.7)))
        for cid, c in self.crates.items():
            cands.append(("c", cid, (c["pos"][0], c["pos"][1], c["pos"][2] + 1.2)))
        for kind, o, p in cands:
            d = math.dist(p[:2], mz[:2])
            if d > st["range"] or d >= bd:
                continue
            if abs(angdiff(math.atan2(p[1] - mz[1], p[0] - mz[0]), m.aim[0])) > cone:
                continue
            if not self.W.los(mz, p):
                continue
            best, bd = (kind, o, p), d
        path = [list(mz)]
        if not best:
            path.append([mz[0] + math.cos(m.aim[0]) * st["range"], mz[1] + math.sin(m.aim[0]) * st["range"], mz[2]])
            self.beams.append((self.t, m.id, "lightning", path))
            return
        dmg = st["damage"] * m.dmg_mult()
        hit = set()
        cur = best
        for j in range(int(st["chain"]) + 1):
            kind, o, p = cur
            hit.add(id(o) if kind != "c" else o)
            path.append(list(p))
            if kind == "m":
                self.damage(o, dmg, m, "lightning", mz)
                if o.has("laser"):
                    o.short_until = self.t + st["short_s"]
                    for w in o.weapons:
                        if w and w["kind"] == "laser":
                            w["charge"] = 0
            elif kind == "n":
                self.damage_npc(o, dmg, m)
            else:
                self.damage_crate(o, dmg, m)
            dmg *= st["chain_falloff"]
            nxt, nd = None, 1e9
            for c in cands:
                key = id(c[1]) if c[0] != "c" else c[1]
                if key in hit:
                    continue
                d = math.dist(c[2], p)
                if d < st["chain_range"] and d < nd:
                    nxt, nd = c, d
            if not nxt:
                break
            cur = nxt
        self.beams.append((self.t, m.id, "lightning", path))

    def explode(self, pr, p, direct):
        st = pr["st"]
        own = pr["owner"]
        self.sound("explosion", p, own.id)
        if direct is not None and not self.friendly(own, direct):
            self.damage(direct, pr["dmg"], own, "mortar", p)
        for m in self.mechs:
            if not m.alive or m is direct:
                continue
            d = math.dist(m.center(), p)
            if d > st["splash_radius"] + m.radius:
                continue
            if m is not own and self.friendly(own, m):
                continue
            f = 1 - (1 - st["splash_edge"]) * min(1, max(0, d - m.radius) / st["splash_radius"])
            amt = st["splash"] * f * (own.dmg_mult() / 1)
            if m is own:
                amt *= CFG["mech"]["self_damage_mult"]
            self.damage(m, amt, own if m is not own else None, "mortar", p)
            kx, ky = norm2(m.pos[0] - p[0], m.pos[1] - p[1])
            m.vel[0] += kx * st["knock"] * f
            m.vel[1] += ky * st["knock"] * f
            m.vel[2] += st["knock_up"] * f
            m.grounded = False
            m.energy = max(0, m.energy - st["drain"] * f)
        for n in self.npcs:
            if n["alive"] and math.dist(n["pos"], p) < st["splash_radius"]:
                self.damage_npc(n, st["splash"] * 0.6, own)
        for cid in list(self.crates):
            c = self.crates.get(cid)
            if c and math.dist(c["pos"], p) < st["splash_radius"]:
                self.damage_crate(cid, st["splash"] * 0.6, own)

    def step_projectiles(self):
        keep = []
        for pr in self.projectiles:
            alive = True
            v = pr["vel"]
            n = 4
            for s in range(n):
                p = pr["pos"]
                if pr["g"]:
                    v[2] -= pr["g"] * DT / n
                q = [p[0] + v[0] * DT / n, p[1] + v[1] * DT / n, p[2] + v[2] * DT / n]
                pr["left"] -= math.dist(p, q)
                pr["pos"] = q
                if self.W.surf_at(q[0], q[1]) > q[2]:
                    if pr["kind"] == "mortar":
                        self.explode(pr, q, None)
                    alive = False
                    break
                own = pr["owner"]
                hm = self.mech_hit(q, own if isinstance(own, Mech) else None)
                if hm and not (isinstance(own, Mech) and self.friendly(own, hm)):
                    if pr["kind"] == "mortar":
                        self.explode(pr, q, hm)
                    elif pr["kind"] == "npc":
                        self.damage(hm, pr["dmg"], None, "npc", p)
                    else:
                        self.damage(hm, pr["dmg"], own, "gun", p)
                    alive = False
                    break
                if pr["kind"] != "npc":
                    hn = self.npc_hit(q)
                    if hn:
                        if pr["kind"] == "mortar":
                            self.explode(pr, q, None)
                        else:
                            self.damage_npc(hn, pr["dmg"], own)
                        alive = False
                        break
                    hc = self.crate_hit(q)
                    if hc:
                        if pr["kind"] == "mortar":
                            self.explode(pr, q, None)
                        else:
                            self.damage_crate(hc, pr["dmg"], own)
                        alive = False
                        break
                if pr["left"] <= 0:
                    alive = False
                    break
            if pr["kind"] == "mortar":
                pr["ttl"] -= DT
                if pr["ttl"] <= 0:
                    alive = False
            if alive:
                keep.append(pr)
        self.projectiles = keep

    # --- physics
    def footprint_surf(self, x, y, r):
        W = self.W
        h = W.surf_at(x, y)
        for dx, dy in ((r, 0), (-r, 0), (0, r), (0, -r)):
            h = max(h, W.surf_at(x + dx * 0.8, y + dy * 0.8))
        return h

    def physics(self, m):
        st = m.stats
        cmd = m.cmd
        mv = cmd.get("move") or [0, 0]
        mx, my = float(mv[0]), float(mv[1])
        L = math.hypot(mx, my)
        if L > 1:
            mx, my = mx / L, my / L
        boost = bool(cmd.get("boost"))
        speed = st["walk_speed"]
        charging = any(w and w["kind"] == "laser" and w.get("charge", 0) > 0 for w in m.weapons)
        if m.grounded and boost and L > 0.1 and m.energy > 0:
            speed = st["boost_speed"]
            m.energy = max(0, m.energy - st["boost_drain"] * DT)
            m.last_spend = self.t
        if charging:
            speed *= st["charge_move_mult"]
        tvx, tvy = mx * speed, my * speed
        acc = st["ground_accel"] if m.grounded else st["air_accel"]
        dvx, dvy = tvx - m.vel[0], tvy - m.vel[1]
        dl = math.hypot(dvx, dvy)
        mxd = acc * DT
        if dl > mxd:
            dvx, dvy = dvx / dl * mxd, dvy / dl * mxd
        m.vel[0] += dvx
        m.vel[1] += dvy
        if cmd.get("jump") and m.grounded:
            m.vel[2] = st["jump_speed"]
            m.grounded = False
            self.sound("jet", m.pos, m.id)
        if not m.grounded:
            if boost and m.energy > 0:
                if m.vel[2] < st["max_rise_speed"]:
                    m.vel[2] = min(st["max_rise_speed"], m.vel[2] + st["jet_accel"] * DT)
                m.energy = max(0, m.energy - st["jet_drain"] * DT)
                m.last_spend = self.t
            m.vel[2] -= st["gravity"] * DT
        # horizontal with wall sliding
        r = m.radius
        x, y, z = m.pos
        nx, ny = x + m.vel[0] * DT, y + m.vel[1] * DT
        lim = z + st["step_up"]
        if self.footprint_surf(nx, ny, r) > lim:
            if self.footprint_surf(nx, y, r) <= lim:
                ny = y; m.vel[1] = 0
            elif self.footprint_surf(x, ny, r) <= lim:
                nx = x; m.vel[0] = 0
            else:
                nx, ny = x, y
                m.vel[0] = m.vel[1] = 0
        nx = min(max(nx, -510), 553)
        ny = min(max(ny, -331), 386)
        nz = z + m.vel[2] * DT
        g = self.footprint_surf(nx, ny, r * 0.5)
        if nz <= g or (m.grounded and nz - g < st["step_up"] and m.vel[2] <= 0 and g >= nz - 0.6 and False):
            nz = g
            m.vel[2] = 0
            m.grounded = True
        else:
            if m.grounded and nz - g > 0.6:
                m.grounded = False
            elif m.grounded:
                nz = g
        m.pos = [nx, ny, nz]
        # aim slew
        a = cmd.get("aim")
        if a:
            dy = angdiff(float(a[0]), m.aim[0])
            ty = st["turn_rate"] * DT
            m.aim[0] = (m.aim[0] + max(-ty, min(ty, dy)) + math.pi) % TAU - math.pi
            tp = max(st["min_pitch"], min(st["max_pitch"], float(a[1])))
            dp = tp - m.aim[1]
            pr = st["pitch_rate"] * DT
            m.aim[1] += max(-pr, min(pr, dp))
        # sounds
        hs = math.hypot(m.vel[0], m.vel[1])
        if m.grounded and hs > CFG["sound"]["step_speed"]:
            m.step_acc += DT
            if m.step_acc >= CFG["sound"]["step_every"]:
                m.step_acc = 0
                self.sound("boost" if speed > st["walk_speed"] + 0.1 else "step", m.pos, m.id)
        if not m.grounded and boost and m.energy > 0 and self.tick % 6 == 0:
            self.sound("jet", m.pos, m.id)
        # energy regen
        if self.t - m.last_spend >= st["energy_regen_delay"]:
            m.energy = min(st["energy"], m.energy + st["energy_regen"] * DT)

    def timers(self, m):
        for w in m.weapons:
            if not w:
                continue
            w["cooldown"] = max(0.0, w["cooldown"] - DT)
            if w["kind"] == "gun" and w["reload"] > 0:
                w["reload"] -= DT
                if w["reload"] <= 0:
                    w["reload"] = 0
                    w["ammo"] = int(m.wstats["gun"]["mag"])

    # --- vision
    def sees(self, a, pos, top_h, near_bonus=0):
        eye = a.eye()
        d = math.dist(eye[:2], pos[:2])
        near = CFG["vision"]["near_range"] + a.stats.get("near_range_bonus", 0)
        rng = CFG["vision"]["range"] + a.stats.get("vision_bonus", 0)
        if d > rng:
            return False
        if d > near:
            if abs(angdiff(math.atan2(pos[1] - eye[1], pos[0] - eye[0]), a.aim[0])) > math.radians(CFG["vision"]["fov_deg"]) / 2:
                return False
        c = (pos[0], pos[1], pos[2] + top_h * 0.5)
        if self.W.los(eye, c):
            return True
        return self.W.los(eye, (pos[0], pos[1], pos[2] + top_h))

    # --- observation for our bot
    def observe(self, m):
        st = dict(m.stats)
        st["radius"] = m.radius
        st["height"] = m.height
        st["eye_height"] = CFG["mech"]["eye_height"] * m.scale
        st["muzzle_height"] = CFG["mech"]["muzzle_height"] * m.scale
        st["hp"] = m.max_hp
        weapons = []
        for w in m.weapons:
            if not w:
                weapons.append(None)
                continue
            d = {"kind": w["kind"], "cooldown": round(w["cooldown"], 3), "stats": dict(m.wstats[w["kind"]])}
            if w["kind"] == "gun":
                d["ammo"] = w["ammo"]; d["reload"] = round(max(0, w["reload"]), 3)
            if w["kind"] == "laser":
                d["charge"] = round(w.get("charge", 0), 3)
            weapons.append(d)
        zc, zr, _ = self.zone_now()
        ph = self.zone_phase()
        self_obs = {"id": m.id, "pos": [round(v, 2) for v in m.pos], "vel": [round(v, 2) for v in m.vel],
                    "aim": [round(m.aim[0], 3), round(m.aim[1], 3)], "hp": round(m.hp, 2), "max_hp": round(m.max_hp, 2),
                    "energy": round(m.energy, 1), "max_energy": st["energy"], "grounded": m.grounded, "cubes": m.cubes,
                    "upgrades": list(m.upgrades), "weapons": weapons,
                    "in_zone": math.dist(m.pos[:2], zc) <= zr, "stats": st}
        mem = self.bot_mem
        mechs = []
        for o in self.mechs:
            if o is m or not o.alive:
                continue
            if self.sees(m, o.pos, o.height):
                mem[o.id] = self.t
            if self.t - mem.get(o.id, -99) <= CFG["vision"]["memory_s"]:
                charging = any(w and w["kind"] == "laser" and w.get("charge", 0) > 0 for w in o.weapons)
                mechs.append({"id": o.id, "kind": "mech", "pos": [round(v, 2) for v in o.pos], "vel": [round(v, 2) for v in o.vel],
                              "aim": [round(o.aim[0], 3), round(o.aim[1], 3)], "hp": round(o.hp, 2), "max_hp": round(o.max_hp, 2),
                              "cubes": o.cubes, "weapons": [w["kind"] for w in o.weapons if w], "charging": charging})
        for n in self.npcs:
            if not n["alive"]:
                continue
            key = n["id"]
            if self.sees(m, n["pos"], 3.5):
                mem[key] = self.t
            if self.t - mem.get(key, -99) <= CFG["vision"]["memory_s"]:
                mechs.append({"id": n["id"], "kind": "npc", "pos": [round(v, 2) for v in n["pos"]], "vel": list(n["vel"]),
                              "aim": list(n["aim"]), "hp": n["hp"], "max_hp": CFG["npc"]["hp"]})
        def vis_items(d, h):
            out = []
            for k, it in d.items():
                if math.dist(it["pos"][:2], m.pos[:2]) < 210 and self.sees(m, it["pos"], h):
                    out.append((k, it))
            return out
        crates = [{"id": k, "pos": c["pos"], "hp": c["hp"]} for k, c in vis_items(self.crates, 2.4)]
        cubes = [{"id": k, "pos": c["pos"]} for k, c in vis_items(self.cubes, 1.0)]
        picks = [{"id": k, "kind": c["kind"], "pos": c["pos"]} for k, c in vis_items(self.pickups, 1.0)]
        projs = []
        for pr in self.projectiles:
            if pr["owner"] is m:
                continue
            if math.dist(pr["pos"][:2], m.pos[:2]) < 200 and self.W.los(m.eye(), pr["pos"]):
                projs.append({"id": pr["id"], "kind": pr["kind"], "pos": [round(v, 2) for v in pr["pos"]], "vel": [round(v, 2) for v in pr["vel"]]})
        sounds = []
        hn = math.radians(CFG["sound"]["bearing_noise_deg"]) * m.stats.get("hearing", 1)
        for (ts, kind, pos, src) in self.sounds:
            if src == m.id or ts <= self.last_obs_t:
                continue
            d = math.dist(pos[:2], m.pos[:2])
            if d > CFG["sound"]["range"].get(kind, 100):
                continue
            b = math.atan2(pos[1] - m.pos[1], pos[0] - m.pos[0]) + self.rng.gauss(0, hn)
            sounds.append({"kind": kind, "bearing": round(b, 3), "dist": "near" if d < 60 else ("mid" if d < 160 else "far"),
                           "age": round(self.t - ts, 2)})
        beams = [{"by": b[1], "kind": b[2], "path": b[3], "age": round(self.t - b[0], 2)} for b in self.beams
                 if b[0] > self.last_obs_t and b[1] != m.id]
        drops = []
        for dr in self.drops:
            if dr["taken"]:
                continue
            drops.append({"id": dr["id"], "pos": dr["pos"], "radius": CFG["drops"]["radius"],
                          "lands_in": round(max(0, dr["land_t"] - self.t), 2), "capture_s": CFG["drops"]["capture_s"],
                          "progress": round(dr["progress"], 2), "holder": dr["holder"]})
        offer = None
        if m.offers:
            offer = {"id": m.offer_id, "expires": m.offer_expires, "choices": [
                {"id": u, "name": UPGRADES[u]["name"], "desc": UPGRADES[u]["desc"]} for u in m.offers[0]]}
        ev = m.events
        m.events = []
        return {"type": "obs", "tick": self.tick, "t": round(self.t, 2), "alive": self.alive_count, "self": self_obs,
                "mechs": mechs, "projectiles": projs, "crates": crates, "cubes": cubes, "pickups": picks, "sounds": sounds,
                "beams": beams, "events": ev, "drops": drops,
                "zone": {"center": ph["center"], "radius": ph["radius"], "next_center": ph["next_center"],
                         "next_radius": ph["next_radius"], "shrink_start": ph["shrink_start"], "shrink_end": ph["shrink_end"],
                         "dps": ph["dps"]},
                "offer": offer}

    # --- offers, loot
    def maybe_offer(self, m):
        th = CFG["cubes"]["upgrade_thresholds"]
        while m.thresholds_hit < len(th) and m.cubes >= th[m.thresholds_hit]:
            m.thresholds_hit += 1
            pool = [u for u, d in UPGRADES.items() if m.up_count.get(u, 0) < d["max"]
                    and (not d.get("requires") or m.has(d["requires"]))]
            self.rng.shuffle(pool)
            m.offers.append(pool[:3])
            if len(m.offers) == 1:
                m.offer_id += 1
                m.offer_expires = self.t + CFG["bot"]["offer_timeout_s"]

    def resolve_offer(self, m, pick):
        if not m.offers:
            return
        ch = m.offers[0]
        if pick is None:
            if self.t < m.offer_expires:
                return
            pick = 0
        try:
            pick = int(pick)
        except (TypeError, ValueError):
            return
        if 0 <= pick < len(ch):
            apply_upgrade(m, ch[pick])
            m.offers.pop(0)
            if m.offers:
                m.offer_id += 1
                m.offer_expires = self.t + CFG["bot"]["offer_timeout_s"]

    def do_take(self, m):
        cmd = m.cmd
        tid = cmd.get("take")
        dslot = cmd.get("drop")
        if tid is None:
            if dslot in (0, 1) and m.weapons[dslot]:
                w = m.weapons[dslot]
                self.pickups[self.nid("pick")] = {"kind": w["kind"], "pos": list(m.pos)}
                m.weapons[dslot] = None
            return
        p = self.pickups.get(tid)
        if not p:
            m.events.append({"type": "take_failed", "id": tid, "reason": "gone"}); return
        if math.dist(p["pos"][:2], m.pos[:2]) > CFG["loot"]["pickup_radius"] or abs(p["pos"][2] - m.pos[2]) > 4:
            m.events.append({"type": "take_failed", "id": tid, "reason": "range"}); return
        if m.has(p["kind"]):
            m.events.append({"type": "take_failed", "id": tid, "reason": "have"}); return
        slot = None
        if dslot in (0, 1):
            slot = int(dslot)
        else:
            for i in (1, 0):
                if m.weapons[i] is None:
                    slot = i
                    break
        if slot is None:
            m.events.append({"type": "take_failed", "id": tid, "reason": "full"}); return
        old = m.weapons[slot]
        if old:
            self.pickups[self.nid("pick")] = {"kind": old["kind"], "pos": list(m.pos)}
        del self.pickups[tid]
        st = m.wstats[p["kind"]]
        w = {"kind": p["kind"], "cooldown": max(CFG["loot"]["equip_s"], st.get("cooldown", 0))}
        if p["kind"] == "gun":
            w.update(ammo=int(st["mag"]), reload=0.0)
        if p["kind"] == "laser":
            w["charge"] = 0
        m.weapons[slot] = w
        m.events.append({"type": "took", "id": tid, "kind": p["kind"], "slot": slot})

    def pick_cubes(self):
        R = CFG["cubes"]["pickup_radius"]
        for cid in list(self.cubes):
            c = self.cubes[cid]
            best, bd = None, 1e9
            for m in self.mechs:
                if not m.alive:
                    continue
                d = math.dist(m.pos[:2], c["pos"][:2])
                if d <= R and abs(m.pos[2] - c["pos"][2]) <= 4 and d < bd:
                    best, bd = m, d
            if best:
                del self.cubes[cid]
                best.cubes += 1
                best.recompute_hp()
                best.events.append({"type": "pickup", "cubes": best.cubes})
                self.maybe_offer(best)

    # --- npcs
    def step_npcs(self):
        nc = CFG["npc"]
        for n in self.npcs:
            if not n["alive"]:
                continue
            n["cd"] = max(0, n["cd"] - DT)
            tgt = n["target"]
            if tgt is not None and (not tgt.alive or math.dist(tgt.pos[:2], n["pos"][:2]) > nc["deaggro_range"]
                                   or math.dist(n["pos"][:2], n["home"][:2]) > nc["chase_leash"]):
                tgt = n["target"] = None
            if tgt is None and math.dist(n["pos"][:2], n["home"][:2]) < nc["leash"]:
                for m in self.mechs:
                    if m.alive and math.dist(m.pos[:2], n["pos"][:2]) < nc["aggro_range"]:
                        if self.W.los((n["pos"][0], n["pos"][1], n["pos"][2] + 3), m.center()):
                            tgt = m
                            break
            goal = tgt.pos if tgt is not None and math.dist(tgt.pos[:2], n["pos"][:2]) > 25 else (n["home"] if tgt is None else None)
            if goal is not None and math.dist(goal[:2], n["pos"][:2]) > 3:
                dx, dy = norm2(goal[0] - n["pos"][0], goal[1] - n["pos"][1])
                nx, ny = n["pos"][0] + dx * nc["speed"] * DT, n["pos"][1] + dy * nc["speed"] * DT
                if self.W.surf_at(nx, ny) - n["pos"][2] < 1.5:
                    n["pos"] = [nx, ny, self.W.surf_at(nx, ny)]
                n["vel"] = [dx * nc["speed"], dy * nc["speed"], 0]
            else:
                n["vel"] = [0, 0, 0]
            if tgt is not None and n["cd"] <= 0:
                src = (n["pos"][0], n["pos"][1], n["pos"][2] + 3)
                c = tgt.center()
                if self.W.los(src, c) and math.dist(src, c) < 100:
                    yaw = math.atan2(c[1] - src[1], c[0] - src[0]) + self.rng.gauss(0, math.radians(nc["spread_deg"]))
                    pit = math.atan2(c[2] - src[2], math.dist(src[:2], c[:2])) + self.rng.gauss(0, math.radians(nc["spread_deg"]))
                    n["aim"] = [yaw, pit]
                    d = (math.cos(pit) * math.cos(yaw), math.cos(pit) * math.sin(yaw), math.sin(pit))
                    sp = nc["projectile_speed"]
                    self.projectiles.append({"id": self.nid("pick") + 100000, "kind": "npc", "pos": [src[0] + d[0] * 2.5, src[1] + d[1] * 2.5, src[2] + d[2] * 2.5],
                                             "vel": [d[0] * sp, d[1] * sp, d[2] * sp], "owner": n, "dmg": nc["damage"], "left": 120, "g": 0})
                    n["cd"] = nc["cooldown"]
                    self.sound("npc_gun", src, -1)

    # --- supply drops
    def step_drops(self):
        dc = CFG["drops"]
        for i, tt in enumerate(dc["times"]):
            if len(self.drops) == i and self.t >= tt:
                ph = self.zone_phase()
                p = self.free_ground(ph["next_center"][0], ph["next_center"][1], ph["next_radius"] * dc["spread"])
                self.drops.append({"id": self.nid("drop"), "pos": list(p), "land_t": tt + dc["fall_s"], "progress": 0.0,
                                   "holder": None, "taken": False})
        for dr in self.drops:
            if dr["taken"] or self.t < dr["land_t"]:
                continue
            if abs(self.t - dr["land_t"]) < DT / 2:
                self.sound("drop", dr["pos"], -1)
            inside = [m for m in self.mechs if m.alive and math.dist(m.pos[:2], dr["pos"][:2]) <= dc["radius"]]
            if len(inside) == 1:
                m = inside[0]
                if dr["holder"] != m.id:
                    dr["holder"] = m.id
                    dr["progress"] = 0
                dr["progress"] += DT
                if dr["progress"] >= dc["capture_s"]:
                    dr["taken"] = True
                    m.cubes += dc["cubes"]
                    m.recompute_hp()
                    m.hp = m.max_hp
                    m.energy = m.stats["energy"]
                    m.events.append({"type": "drop", "id": dr["id"], "cubes": m.cubes})
                    self.maybe_offer(m)
            elif len(inside) == 0:
                dr["holder"] = None
                dr["progress"] = 0

    # --- house bot brain
    def house_flow(self):
        ph = self.zone_phase()
        c, r = ph["next_center"], ph["next_radius"]
        g = (round(c[0]), round(c[1]), round(r))
        if g != self.flow_goal:
            self.flow_goal = g
            self.W.start_flow(c[0], c[1], max(4, r * 0.7))
            self.W.step_flow(10.0)

    def house_think(self, m):
        S = self.hb[m.id]
        T = TIER[m.tier]
        me = self.me
        cmd = {"move": [0, 0], "fire": [False, False]}
        ph = self.zone_phase()
        zc, zr, _ = self.zone_now()
        nc, nr = ph["next_center"], ph["next_radius"]
        x, y, z = m.pos
        if m.offers:
            cmd["pick"] = 0
        known = self.house_mem is not None and self.t - self.house_mem <= CFG["vision"]["memory_s"] and me.alive
        # zone pressure
        late = T["zone_late"]
        need_zone = math.dist((x, y), nc) > nr * 0.85 and (not late or self.t > ph["shrink_start"] - 5 or math.dist((x, y), zc) > zr - 10)
        target = None
        move = (0.0, 0.0)
        aimpt = None
        dme = math.dist(me.pos[:2], (x, y))
        if known and dme <= T["hunt"] * HUNT_MULT:
            d = dme
            S["react"] = S.get("react", 0) + 0.1
            target = me
            want = 30 if m.has("lightning") else T["dist"]
            ax, ay = norm2(me.pos[0] - x, me.pos[1] - y)
            if self.t > S["strafe_t"]:
                S["strafe"] = -S["strafe"]
                S["strafe_t"] = self.t + self.rng.uniform(0.8, 2.0)
            sx, sy = -ay * S["strafe"], ax * S["strafe"]
            if d > want + 10 or not self.W.los(m.muzzle(), me.center()):
                move = norm2(ax + sx * 0.3, ay + sy * 0.3)
            elif d < want - 20:
                move = norm2(-ax + sx * 0.5, -ay + sy * 0.5)
            else:
                move = (sx, sy)
            if need_zone and math.dist((x, y), zc) > zr - 15:
                fd = self.W.flow_dir(x, y) or norm2(nc[0] - x, nc[1] - y)
                move = norm2(move[0] + fd[0] * 2, move[1] + fd[1] * 2)
        else:
            S["react"] = 0
            if S["inv"] is not None and self.t - S["inv"][0] < 10 and not need_zone:
                gp = S["inv"][1]
                if math.dist(gp[:2], (x, y)) < 10:
                    S["inv"] = None
                move = norm2(gp[0] - x, gp[1] - y)
            elif need_zone:
                move = self.W.flow_dir(x, y) or norm2(nc[0] - x, nc[1] - y)
            else:
                # loot a weapon if nearby
                if m.weapons[1] is None:
                    best, bd = None, 70
                    for k, p in self.pickups.items():
                        d = math.dist(p["pos"][:2], (x, y))
                        if d < bd and abs(p["pos"][2] - z) < 3 and not m.has(p["kind"]):
                            best, bd = (k, p), d
                    if best:
                        if bd < 3.5:
                            cmd["take"] = best[0]
                        move = norm2(best[1]["pos"][0] - x, best[1]["pos"][1] - y)
                if move == (0.0, 0.0):
                    if S["wp"] is None or math.dist(S["wp"][:2], (x, y)) < 8 or self.t - S["wp_t"] > 25 or \
                            math.dist(S["wp"][:2], nc) > nr:
                        S["wp"] = self.free_ground(nc[0], nc[1], nr * 0.8)
                        S["wp_t"] = self.t
                    move = norm2(S["wp"][0] - x, S["wp"][1] - y)
        # stuck handling
        hs = math.hypot(m.vel[0], m.vel[1])
        if math.hypot(*move) > 0.5 and hs < 2 and m.grounded:
            S["stuck"] += 0.1
        else:
            S["stuck"] = max(0, S["stuck"] - 0.05)
        if S["stuck"] > 0.5:
            S["climb"] = self.t + 2.5
            S["stuck"] = 0
        cmd["move"] = list(move)
        if self.t < S["climb"]:
            if m.grounded:
                cmd["jump"] = True
            else:
                cmd["boost"] = m.energy > 2
        # aim & fire
        if target is not None and S["react"] >= T["react"]:
            src = m.muzzle()
            c = me.center()
            spd = m.wstats["gun"]["speed"]
            if T["lead"]:
                tt = math.dist(src, c) / spd
                c = (c[0] + me.vel[0] * tt, c[1] + me.vel[1] * tt, c[2])
            nz = T["noise"] * NOISE_MULT
            yaw = math.atan2(c[1] - src[1], c[0] - src[0]) + self.rng.gauss(0, nz)
            pit = math.atan2(c[2] - src[2], math.dist(src[:2], c[:2])) + self.rng.gauss(0, nz * 0.5)
            cmd["aim"] = [yaw, pit]
            err = abs(angdiff(m.aim[0], yaw))
            d = math.dist(src[:2], c[:2])
            los = self.W.los(src, c) and d < T["fire"]
            for si, w in enumerate(m.weapons):
                if not w:
                    continue
                if w["kind"] == "gun":
                    cmd["fire"][si] = los and err < 0.06 and d < 220
                elif w["kind"] == "lightning":
                    cmd["fire"][si] = los and d < 44 and err < 0.2
                elif w["kind"] == "laser":
                    ch = w.get("charge", 0)
                    if ch > 0:
                        cmd["fire"][si] = not (ch >= m.wstats["laser"]["charge"] and err < 0.03 and los)
                    else:
                        cmd["fire"][si] = los and d < 260 and err < 0.3 and w["cooldown"] <= 0
                elif w["kind"] == "mortar":
                    if 40 < d < 200 and not los:
                        stm = m.wstats["mortar"]
                        v, g = stm["speed"], stm["gravity"]
                        disc = v ** 4 - g * (g * d * d)
                        if disc > 0:
                            th = math.atan2(v * v + math.sqrt(disc), g * d)
                            cmd["aim"] = [yaw, min(1.25, th)]
                            cmd["fire"][si] = abs(m.aim[1] - th) < 0.05 and err < 0.05
        else:
            if math.hypot(*move) > 0.1:
                cmd["aim"] = [math.atan2(move[1], move[0]) + 0.6 * math.sin(self.t * 0.8 + m.id), 0]
        m.cmd = cmd

    # --- main loop
    def run(self, max_t=None):
        self.bot_mem = {}
        self.last_obs_t = -1
        max_t = max_t or CFG["match"]["duration_s"]
        while True:
            self.dead_this_tick = []
            decide = self.tick % CFG["match"]["decide_every"] == 0
            if decide:
                self.house_flow()
                # shared house vision of our bot
                if self.me.alive:
                    for m in self.mechs:
                        if m.house and m.alive and self.sees(m, self.me.pos, self.me.height):
                            self.house_mem = self.t
                            break
                    # house bots investigate our loud sounds
                    for (ts, kind, pos, src) in self.sounds:
                        if src == self.me.id and ts > self.last_obs_t and kind in ("gun", "laser", "mortar", "lightning", "step", "boost", "jet"):
                            rng = CFG["sound"]["range"][kind]
                            for m in self.mechs:
                                if m.house and m.alive and math.dist(m.pos[:2], pos[:2]) < rng:
                                    if self.rng.random() < TIER[m.tier]["chase"] * (0.15 if kind in ("step", "jet", "boost") else 0.05):
                                        self.hb[m.id]["inv"] = (self.t, pos)
                for m in self.mechs:
                    if m.house and m.alive:
                        self.house_think(m)
                if self.me.alive:
                    obs = self.observe(self.me)
                    t0 = time.perf_counter()
                    try:
                        a = self.bot.act(obs) or {}
                    except Exception as e:  # keep coasting like the wrapper
                        import traceback
                        traceback.print_exc()
                        a = {k: v for k, v in self.me.cmd.items() if k in ("move", "boost", "aim")}
                    self.bot_ms.append((time.perf_counter() - t0) * 1000)
                    a = dict(a)
                    a.pop("debug", None)
                    self.me.cmd = a
                    self.last_obs_t = self.t
                for m in self.mechs:
                    if m.alive:
                        self.resolve_offer(m, m.cmd.get("pick"))
                        if m.cmd.get("take") is not None or m.cmd.get("drop") is not None:
                            self.do_take(m)
                        if m.cmd.get("reload") and m.weapons[0] and m.weapons[0]["kind"] == "gun":
                            w = m.weapons[0]
                            if w["reload"] == 0 and w["ammo"] < m.wstats["gun"]["mag"]:
                                w["reload"] = m.wstats["gun"]["reload"]
                                self.sound("reload", m.pos, m.id)
            for m in self.mechs:
                if m.alive:
                    self.timers(m)
                    self.fire_weapons(m)
                    self.physics(m)
                    # one-shots only on the decision tick
                    if not decide:
                        pass
            # clear one-shot fields after first tick of the decision
            if not decide:
                for m in self.mechs:
                    for k in ("jump", "take", "drop", "pick", "reload"):
                        m.cmd.pop(k, None)
            self.step_projectiles()
            self.step_npcs()
            self.pick_cubes()
            self.step_drops()
            # zone & regen
            zc, zr, dps = self.zone_now()
            for m in self.mechs:
                if not m.alive:
                    continue
                inz = math.dist(m.pos[:2], zc) <= zr
                if not inz:
                    m.hp -= dps * DT
                    m.last_combat = self.t
                    if self.tick % 10 == 0:
                        m.events.append({"type": "damaged", "amount": dps * 0.5, "weapon": "zone", "bearing": None, "by": None})
                    if m.hp <= 0:
                        self.kill(m, None)
                elif self.t - m.last_combat >= m.stats["regen_delay"]:
                    m.hp = min(m.max_hp, m.hp + m.stats["regen_per_s"] * DT)
                if m.offers and self.t >= m.offer_expires:
                    self.resolve_offer(m, None)
            if self.dead_this_tick:
                place = self.alive_count
                for m in self.dead_this_tick:
                    m.place = place
                self.alive_count -= len(self.dead_this_tick)
            self.sounds = [s for s in self.sounds if self.t - s[0] < 0.5]
            self.beams = [b for b in self.beams if self.t - b[0] < 0.3]
            self.tick += 1
            self.t = self.tick * DT
            if not self.me.alive or self.alive_count <= 1 or self.t >= max_t:
                break
        if self.me.alive:
            if self.alive_count <= 1:
                self.me.place = 1
            else:
                alive = sorted([m for m in self.mechs if m.alive], key=lambda m: -m.hp)
                self.me.place = alive.index(self.me) + 1
        return self.me.place


def run_one(args):
    path, seed = args
    random.seed(seed)
    cls = load_bot_class(path)
    m = Match(seed, cls)
    t0 = time.time()
    place = m.run()
    me = m.me
    ms = sorted(m.bot_ms) or [0]
    return {"seed": seed, "place": place, "score": 16 - place, "death_t": me.death_t, "kills": me.kills,
            "cubes": me.cubes, "wall": time.time() - t0, "p99ms": ms[int(len(ms) * 0.99) - 1] if len(ms) > 1 else ms[0],
            "maxms": ms[-1], "weapons": [w["kind"] for w in me.weapons if w], "upgrades": me.upgrades,
            "hp_end": round(me.hp), "alive_end": m.alive_count}


def run_many(path, seeds, procs):
    with mp.Pool(procs) as pool:
        return pool.map(run_one, [(path, s) for s in seeds])


def summarize(name, res):
    sc = [r["score"] for r in res]
    mean = sum(sc) / len(sc)
    sd = (sum((s - mean) ** 2 for s in sc) / max(1, len(sc) - 1)) ** 0.5
    dt = [r["death_t"] for r in res if r["death_t"] is not None]
    print(f"{name}: mean {mean:.2f} ± {sd / len(sc) ** 0.5:.2f} (n={len(sc)})  kills {sum(r['kills'] for r in res) / len(res):.2f}  "
          f"cubes {sum(r['cubes'] for r in res) / len(res):.1f}  median death {sorted(dt)[len(dt) // 2] if dt else '-'}s  "
          f"survived {sum(1 for r in res if r['death_t'] is None)}  maxms {max(r['maxms'] for r in res):.1f}")
    return mean


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot", default=os.path.join(ROOT, "bot.py"))
    ap.add_argument("--compare", default=None)
    ap.add_argument("--matches", type=int, default=24)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--procs", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    seeds = list(range(a.seed * 1000, a.seed * 1000 + a.matches))
    t0 = time.time()
    res = run_many(a.bot, seeds, a.procs)
    if a.v:
        for r in res:
            print(r)
    summarize(os.path.relpath(a.bot), res)
    if a.compare:
        res2 = run_many(a.compare, seeds, a.procs)
        summarize(os.path.relpath(a.compare), res2)
        diffs = [x["score"] - y["score"] for x, y in zip(res, res2)]
        md = sum(diffs) / len(diffs)
        sd = (sum((d - md) ** 2 for d in diffs) / max(1, len(diffs) - 1)) ** 0.5
        print(f"paired diff (bot - compare): {md:+.2f} ± {sd / len(diffs) ** 0.5:.2f}")
    print(f"wall {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
