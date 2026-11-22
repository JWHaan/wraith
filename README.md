# wraith — Mech Royale bot

Survival-first mech AI for the Builders Buildathon Mech Royale track (protocol mechfight/1).

- `bot.py` — entry point; runs the v9 brain (set `WRAITH_BRAIN=v7` for the previous decision loop)
- `brain.py` — v9 decision rules (targeting, finishing, fight movement, cover, loot, firing)
- `nav.py` — 2 m navigation grid: blocked cells, clearance, main component, A*, cover search
- `world.py` — map model: height grid, line of sight, cover, flow field
- `grid.bin` — precomputed 2 m grid, built by `tools/build_grid.py <arena_dir> grid.bin`

## Local simulator

`tools/sim.py` is a rough practice simulator (wraith vs 15 house-style hunters with shared vision that never
fight each other), calibrated so the kit's example bot scores about 1. Use it for paired A/B runs only;
absolute numbers don't transfer. The [builders-4 practice scorer](https://www.joinbuilders.org/buildathon/tracks/builders)
averages `16 - place` over 96 private seeds.

```sh
python3 tools/sim.py --bot bot.py --compare path/to/old/bot.py --matches 96
python3 tools/trace.py bot.py 1003      # second-by-second trace of one match
```

Ideas credited to other public entries: quiet walking under the step-sound speed, laser over mortar, committed
zone rotation, finishing low-HP targets (LanceHuang245/mech-royale, moonriver0922/wgsrobot), and practice vs
free-for-all postures (Kgc3105/MAGIC-Royale, niranjankumarnk/mech-royale). No code was copied.

v9's decision and navigation rules follow giddygiddyboy's public MIT-licensed bot, re-implemented in Python;
see `NOTICE.md` for credit and the licence.

## Opening experiment: get the first laser sooner

On the Nov 22 builders-4 board, the public configs for [not even close (#3, 6.21)](https://github.com/Th3r0n/mech-royale-bot/blob/1792993cf1252ab04e682cccce6bc6160da008b0/automata/bot.json)
and [GMUNVIS (#21, 5.93)](https://github.com/KhangNguyen2005/mech-royale/blob/3a5ec99de3a0e44c32a9ba416a3ae925e08d0ec6/automata/bot.json)
both favor searching farther for a laser. They share the same Rust bot family; these are not two independent
tests of that idea. [4141 (#6, 6.17)](https://github.com/sparshagra/AIEthicsTuts/blob/ce3a50aaf55e1deef7703bb0bcb2d525be026da6/strategy.go)
also distinguishes early looting from later play. Team names match the READMEs; the board doesn't identify scored commits.

Wraith's own rule gives a ground laser priority over ordinary loot for the first 30 seconds when healthy and
not under threat. It looks up to 18 seconds' walk away (or its existing search radius, if larger) and boosts
on the approach only while grounded with energy to spare. The usual zone, reachability, and retreat checks still apply.

Paired local placement points against `1e2baf4` (same spawn and opponents for each pair):

| Seeds | Matches | Original | New opening | Paired gain (mean +/- SE) |
| --- | ---: | ---: | ---: | ---: |
| 17000-17023 (development) | 24 | 6.08 | 8.71 | +2.62 +/- 1.17 |
| 83000-83095 (holdout) | 96 | 6.78 | 7.33 | +0.55 +/- 0.56 |
| 137000-137191 (fresh confirmation) | 192 | 6.05 | 6.73 | +0.68 +/- 0.37 |

On the development seeds, laser pickups by 30 seconds rose from 6/24 to 12/24. Across the 288 non-development
seeds, survival to the end rose from 69 to 87 matches. These are simulator results, not an official score.

To repeat a batch, extract the original bot's helpers and grid as well as its entry point:

```sh
baseline=$(mktemp -d)
git archive 1e2baf4 | tar -x -C "$baseline"
python3 tools/sim.py --bot bot.py --compare "$baseline/bot.py" --seed 83 --matches 96 --procs 6
```
