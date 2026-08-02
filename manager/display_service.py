from __future__ import annotations

import socket
from datetime import datetime
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont
from zero2w_epaper import Display

DISPLAY_TIMEZONE = ZoneInfo("America/Chicago")


def show_idle() -> None:
    image = Image.new("1", (250, 122), 255)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()

    hostname = socket.gethostname()
    current_time = datetime.now(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M")

    draw.rectangle((0, 0, 249, 121), outline=0)
    draw.text((8, 8), "ZERO2W MANAGER", font=font, fill=0)
    draw.line((8, 22, 241, 22), fill=0)
    draw.text((8, 34), f"Host: {hostname}", font=font, fill=0)
    draw.text((8, 52), "Mode: IDLE", font=font, fill=0)
    draw.text((8, 70), current_time, font=font, fill=0)
    draw.text((8, 94), "Manager ready", font=font, fill=0)

    with Display() as display:
        display.show(image)
