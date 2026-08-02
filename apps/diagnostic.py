from __future__ import annotations

import logging
import os
import shutil
import signal
import socket
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from zero2w_epaper import Display


LOGGER = logging.getLogger("zero2w-diagnostic")

WIDTH = 250
HEIGHT = 122
PAGE_SECONDS = 8
RUNNING = True


def handle_shutdown(signum: int, _frame: object) -> None:
    global RUNNING
    RUNNING = False
    LOGGER.info("Diagnostics received signal %s", signum)
    raise SystemExit(0)


def read_text(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def get_uptime() -> str:
    raw = read_text("/proc/uptime")

    try:
        seconds = int(float(raw.split()[0]))
    except (ValueError, IndexError):
        return "unknown"

    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60

    if days:
        return f"{days}d {hours}h {minutes}m"

    return f"{hours}h {minutes}m"


def get_temperature() -> str:
    raw = read_text(
        "/sys/class/thermal/thermal_zone0/temp"
    )

    try:
        value = float(raw)

        if value > 1000:
            value /= 1000

        return f"{value:.1f} C"
    except ValueError:
        return "unknown"


def get_memory() -> str:
    total_kb = 0
    available_kb = 0

    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                total_kb = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                available_kb = int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return "unknown"

    used_mb = max(0, total_kb - available_kb) // 1024
    total_mb = total_kb // 1024

    return f"{used_mb}/{total_mb} MB"


def get_disk() -> str:
    usage = shutil.disk_usage("/")
    used_gb = usage.used / (1024 ** 3)
    total_gb = usage.total / (1024 ** 3)

    return f"{used_gb:.1f}/{total_gb:.1f} GB"


def get_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "not connected"
    finally:
        sock.close()


def create_page(
    title: str,
    rows: list[tuple[str, str]],
) -> Image.Image:
    image = Image.new("1", (WIDTH, HEIGHT), 255)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()

    draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), outline=0)
    draw.text((7, 5), title, font=font, fill=0)
    draw.line((6, 18, WIDTH - 7, 18), fill=0)

    y = 27

    for label, value in rows:
        draw.text((8, y), f"{label}:", font=font, fill=0)
        draw.text((78, y), value[:27], font=font, fill=0)
        y += 15

    return image


def system_page() -> Image.Image:
    try:
        load = " ".join(f"{value:.2f}" for value in os.getloadavg())
    except OSError:
        load = "unknown"

    return create_page(
        "DIAGNOSTICS 1/2",
        [
            ("Host", socket.gethostname()),
            ("Uptime", get_uptime()),
            ("Temp", get_temperature()),
            ("Load", load),
            ("Memory", get_memory()),
            ("Disk", get_disk()),
        ],
    )


def controls_page() -> Image.Image:
    return create_page(
        "DIAGNOSTICS 2/2",
        [
            ("IP", get_ip()),
            ("PID", str(os.getpid())),
            ("Display", "Waveshare 2.13 V4"),
            ("Buttons", "LRADC detected"),
            ("Next", "Automatic 8 sec"),
            ("Exit", "Hold KEY_ENTER"),
        ],
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    pages = [system_page, controls_page]
    page_index = 0

    LOGGER.info("Starting Diagnostics application")

    with Display() as display:
        while RUNNING:
            try:
                display.show(pages[page_index]())
                LOGGER.info(
                    "Displayed diagnostics page %d",
                    page_index + 1,
                )
                page_index = (page_index + 1) % len(pages)
            except Exception:
                LOGGER.exception(
                    "Failed to update diagnostics display"
                )

            deadline = time.monotonic() + PAGE_SECONDS

            while RUNNING and time.monotonic() < deadline:
                time.sleep(0.2)

    LOGGER.info("Diagnostics application stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
