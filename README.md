# wraith — Mech Royale bot

Survival-first mech AI for the Builders Buildathon Mech Royale track (protocol mechfight/1).

- `bot.py` — decision loop (zone, evade/fight/loot, aiming)
- `world.py` — map model: height grid, line of sight, cover, flow field
- `grid.bin` — precomputed 2 m grid, built by `tools/build_grid.py <arena_dir> grid.bin`
