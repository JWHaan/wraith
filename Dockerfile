# The match runs it with no network, a read-only root (writable /tmp), 1 CPU, 512 MB, and the map at /arena.
FROM python:3.13-slim
WORKDIR /bot
COPY mechfight.py world.py nav.py brain.py bot.py grid.bin los.bin ./
ENTRYPOINT ["python3", "-u", "/bot/bot.py"]
