"""Trace one simulated match: mode, hp, position, nearby house bots every second."""
import collections, math, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sim

def main(path, seed):
    cls = sim.load_bot_class(path)
    m = sim.Match(seed, cls)
    modes = collections.Counter()
    orig = m.bot.act
    log = []
    def act(obs):
        a = orig(obs)
        mode = getattr(m.bot, "mode", "?")
        modes[mode] += 1
        if obs["tick"] % 20 == 0:
            me = obs["self"]
            vis = [(o["id"], round(math.dist(o["pos"][:2], me["pos"][:2]))) for o in obs["mechs"] if o["kind"] == "mech"]
            dmg = sum(e["amount"] for e in obs["events"] if e["type"] == "damaged")
            log.append(f"t={obs['t']:5.1f} {mode:6s} hp={me['hp']:6.0f} e={me['energy']:3.0f} pos=({me['pos'][0]:6.0f},{me['pos'][1]:6.0f},{me['pos'][2]:4.0f}) "
                       f"zone={'in' if me['in_zone'] else 'OUT'} w={[w['kind'] for w in me['weapons'] if w]} cubes={me['cubes']} vis={vis[:4]} alive={obs['alive']}")
        return a
    m.bot.act = act
    place = m.run()
    print("\n".join(log))
    print("place", place, "death", m.me.death_t, "kills", m.me.kills, dict(modes))
    # who damaged us
main(sys.argv[1], int(sys.argv[2]))
