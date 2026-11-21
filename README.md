# wraith — Mech Royale bot

Survival-first mech AI for the Builders Buildathon Mech Royale track (protocol mechfight/1).

- `bot.py` — decision loop (zone, evade/fight/loot, aiming)
- `world.py` — map model: height grid, line of sight, cover, flow field
- `grid.bin` — precomputed 2 m grid, built by `tools/build_grid.py <arena_dir> grid.bin`

## Local simulator

`tools/sim.py` is a rough practice simulator (wraith vs 15 house-style hunters with shared vision that never
fight each other), calibrated so the kit's example bot scores about 1 (real board: 1.15). Use it for paired A/B
runs only; absolute numbers don't transfer.

```sh
python3 tools/sim.py --bot bot.py --compare path/to/old/bot.py --matches 96
python3 tools/trace.py bot.py 1003      # second-by-second trace of one match
```

Ideas credited to other public entries: quiet walking under the step-sound speed, laser over mortar, committed
zone rotation, finishing low-HP targets (LanceHuang245/mech-royale, moonriver0922/wgsrobot), and practice vs
free-for-all postures (Kgc3105/MAGIC-Royale, niranjankumarnk/mech-royale). No code was copied.
