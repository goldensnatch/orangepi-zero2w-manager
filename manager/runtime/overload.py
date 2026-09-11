"""Load-average helpers for the Zero 2W.

A 4-core board can look “100% busy” from Jellyseerr, Docker, Pwnagotchi, or
Rocky probing Docker too often. Soft overload skips extra probes so we do not
make a bad situation worse. Hard overload is for operator recover / shed.
"""

from __future__ import annotations

import os


# Skip docker inspect / compose retries once the board is already struggling.
DOCKER_PROBE_LOADAVG = 1.6
# Radio (Pwnagotchi/Bettercap/Ragnar) plus media cannot share this SoC.
HARD_OVERLOAD_LOADAVG = 2.8


def loadavg_1() -> float:
    try:
        return float(os.getloadavg()[0])
    except OSError:
        return 0.0


def is_overloaded(threshold: float | None = None) -> bool:
    limit = DOCKER_PROBE_LOADAVG if threshold is None else float(threshold)
    return loadavg_1() >= limit


def should_skip_docker_probe(load_1: float | None = None) -> bool:
    value = loadavg_1() if load_1 is None else float(load_1)
    return value >= DOCKER_PROBE_LOADAVG


def should_shed_workloads(load_1: float | None = None) -> bool:
    value = loadavg_1() if load_1 is None else float(load_1)
    return value >= HARD_OVERLOAD_LOADAVG
