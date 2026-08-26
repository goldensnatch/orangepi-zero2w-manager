from __future__ import annotations

import io
import json
import logging
import os
import signal
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from PIL import Image, ImageDraw, ImageFont, ImageOps
from zero2w_epaper import Display


LOGGER = logging.getLogger("zero2w-tesserae")

WIDTH = 250
HEIGHT = 122

CONFIG_FILE = Path(
    "/opt/zero2w-manager/config/tesserae-client.json"
)

DEFAULT_SERVER = "http://127.0.0.1:8765"
DEFAULT_POLL_SECONDS = 30
WEB_SERVICE_CACHE_PATH = Path("/opt/zero2w-manager/runtime/config/web-service-cache.json")
PUBLIC_BASE_URL = os.environ.get("ROCKY_PUBLIC_BASE_URL", "").strip()

RUNNING = True


def detect_public_host() -> str:
    if PUBLIC_BASE_URL:
        configured = PUBLIC_BASE_URL if "://" in PUBLIC_BASE_URL else f"http://{PUBLIC_BASE_URL}"
        parsed = urlparse(configured)
        if parsed.hostname:
            return parsed.hostname
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def publicize_service_url(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    hostname = parsed.hostname or detect_public_host()
    if hostname in {"127.0.0.1", "localhost"}:
        hostname = detect_public_host()
    netloc = hostname
    if parsed.port:
        netloc = f"{hostname}:{parsed.port}"
    return urlunparse((parsed.scheme or "http", netloc, parsed.path or "/", "", parsed.query, ""))


def publish_qr_cache(server_url: str) -> None:
    public_url = publicize_service_url(server_url.rstrip("/"))
    try:
        cache: dict[str, Any] = {}
        if WEB_SERVICE_CACHE_PATH.is_file():
            loaded = json.loads(WEB_SERVICE_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cache = loaded
        cache["tesserae"] = {
            "last_url": public_url,
            "last_tokenized_proxy_url": public_url,
            "last_seen_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": "tesserae-direct-url",
        }
        WEB_SERVICE_CACHE_PATH.parent.mkdir(mode=0o775, parents=True, exist_ok=True)
        WEB_SERVICE_CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        LOGGER.info("Published Tesserae QR cache target: %s", public_url)
    except Exception:
        LOGGER.exception("Failed to publish Tesserae QR cache target")


def handle_shutdown(signum: int, _frame: object) -> None:
    global RUNNING

    LOGGER.info("Tesserae client received signal %s", signum)
    RUNNING = False


def load_config() -> dict[str, Any]:
    config: dict[str, Any] = {}

    try:
        loaded = json.loads(CONFIG_FILE.read_text())

        if isinstance(loaded, dict):
            config.update(loaded)
    except (OSError, json.JSONDecodeError):
        pass

    # Environment variables override the JSON configuration.
    if os.environ.get("TESSERAE_URL"):
        config["server_url"] = os.environ["TESSERAE_URL"]

    if os.environ.get("TESSERAE_DEVICE_ID"):
        config["device_id"] = os.environ["TESSERAE_DEVICE_ID"]

    if os.environ.get("TESSERAE_DEVICE_TOKEN"):
        config["device_token"] = os.environ["TESSERAE_DEVICE_TOKEN"]

    return config


def create_message_page(
    title: str,
    rows: list[str],
) -> Image.Image:
    image = Image.new("1", (WIDTH, HEIGHT), 255)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()

    draw.rectangle(
        (0, 0, WIDTH - 1, HEIGHT - 1),
        outline=0,
    )

    draw.text(
        (7, 5),
        title[:34],
        font=font,
        fill=0,
    )

    draw.line(
        (6, 18, WIDTH - 7, 18),
        fill=0,
    )

    y = 26

    for row in rows[:7]:
        draw.text(
            (8, y),
            row[:39],
            font=font,
            fill=0,
        )
        y += 13

    return image


def setup_page() -> Image.Image:
    return create_message_page(
        "TESSERAE SETUP",
        [
            "Device is not registered.",
            "",
            "Create pairing code in:",
            "Tesserae > Settings",
            "> Devices > Pair",
            "",
            "Hold ENTER to exit.",
        ],
    )


def error_page(message: str) -> Image.Image:
    return create_message_page(
        "TESSERAE ERROR",
        [
            message,
            "",
            "Server:",
            "127.0.0.1:8765",
            "",
            "Will retry automatically.",
        ],
    )


def waiting_page(device_id: str) -> Image.Image:
    return create_message_page(
        "TESSERAE",
        [
            f"Device: {device_id}",
            "",
            "Waiting for a frame.",
            "",
            "Bind this device to a",
            "dashboard in Tesserae.",
            "",
        ],
    )


def normalize_image(image: Image.Image) -> Image.Image:
    # Flatten transparency against white.
    if image.mode in ("RGBA", "LA"):
        background = Image.new("RGBA", image.size, "white")
        background.alpha_composite(image.convert("RGBA"))
        image = background.convert("RGB")

    # Keep aspect ratio and pad unused space with white.
    image = ImageOps.contain(
        image.convert("L"),
        (WIDTH, HEIGHT),
        method=Image.Resampling.LANCZOS,
    )

    canvas = Image.new("L", (WIDTH, HEIGHT), 255)

    x = (WIDTH - image.width) // 2
    y = (HEIGHT - image.height) // 2

    canvas.paste(image, (x, y))

    # Floyd-Steinberg conversion to the monochrome panel format.
    return canvas.convert("1")


def send_heartbeat(
    server_url: str,
    device_id: str,
    token: str,
    *,
    state: str = "online",
    last_paint_at: str | None = None,
    last_error: str | None = None,
) -> dict[str, Any]:
    """Send device telemetry and receive server polling settings."""
    url = (
        f"{server_url.rstrip('/')}"
        f"/api/v1/device/{device_id}/status"
    )

    payload = {
        "state": state,
        "fw_version": "zero2w-manager-1.0",
        "platform": "Orange Pi Zero 2W",
        "panel": "Waveshare 2.13 V4",
        "width": WIDTH,
        "height": HEIGHT,
        "last_paint_at": last_paint_at,
        "last_error": last_error,
    }

    body = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Zero2W-Tesserae/1.0",
        },
    )

    with urllib.request.urlopen(
        request,
        timeout=15,
    ) as response:
        response_body = response.read()

    if not response_body:
        return {}

    loaded = json.loads(response_body.decode("utf-8"))

    if isinstance(loaded, dict):
        return loaded

    return {}



def fetch_binary_url(
    url: str,
    token: str,
    etag: str | None = None,
) -> tuple[bytes | None, str | None, str]:
    headers = {
        "Accept": "image/png,image/bmp,application/octet-stream",
        "User-Agent": "Zero2W-Tesserae/1.0",
    }

    # Local Tesserae render URLs may not require authentication, but
    # including the device token is harmless for same-server URLs.
    if token:
        headers["Authorization"] = f"Bearer {token}"

    if etag:
        headers["If-None-Match"] = etag

    request = urllib.request.Request(
        url,
        headers=headers,
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=30,
        ) as response:
            payload = response.read()
            new_etag = response.headers.get("ETag")
            content_type = (
                response.headers.get("Content-Type", "")
                .split(";", 1)[0]
                .strip()
                .lower()
            )

    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return None, etag, ""

        raise

    return payload, new_etag or etag, content_type


def fetch_frame(
    server_url: str,
    device_id: str,
    token: str,
    etag: str | None,
) -> tuple[Image.Image | None, str | None]:
    endpoint = (
        f"{server_url.rstrip('/')}"
        f"/api/v1/device/{device_id}/frame"
    )

    payload, response_etag, content_type = fetch_binary_url(
        endpoint,
        token,
        etag,
    )

    if payload is None:
        return None, response_etag

    LOGGER.info(
        "Frame endpoint returned content-type=%s bytes=%d",
        content_type or "(missing)",
        len(payload),
    )

    # Tesserae may return a JSON envelope pointing at the actual
    # rendered image rather than returning image bytes directly.
    if (
        content_type == "application/json"
        or payload.lstrip().startswith(b"{")
    ):
        try:
            envelope = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Invalid JSON frame response: {exc}"
            ) from exc

        if not isinstance(envelope, dict):
            raise RuntimeError(
                "Frame response JSON was not an object"
            )

        LOGGER.info(
            "Frame envelope keys: %s",
            sorted(envelope.keys()),
        )

        frame_url = (
            envelope.get("url")
            or envelope.get("frame_url")
            or envelope.get("image_url")
            or envelope.get("render_url")
        )

        # Some APIs nest frame information.
        if not frame_url:
            nested = envelope.get("frame")

            if isinstance(nested, dict):
                frame_url = (
                    nested.get("url")
                    or nested.get("frame_url")
                    or nested.get("image_url")
                    or nested.get("render_url")
                )

        if not frame_url:
            status = envelope.get("status")
            message = (
                envelope.get("error")
                or envelope.get("message")
                or envelope.get("detail")
            )

            raise RuntimeError(
                "Frame response did not contain an image URL"
                f"; status={status!r}"
                f"; message={message!r}"
                f"; keys={sorted(envelope.keys())}"
            )

        frame_url = str(frame_url)

        # Convert relative render paths such as /renders/example.png
        # into a complete local Tesserae URL.
        if frame_url.startswith("/"):
            frame_url = (
                server_url.rstrip("/")
                + frame_url
            )
        elif not frame_url.startswith(
            ("http://", "https://")
        ):
            frame_url = (
                server_url.rstrip("/")
                + "/"
                + frame_url.lstrip("/")
            )

        LOGGER.info(
            "Downloading Tesserae render: %s",
            frame_url,
        )

        payload, response_etag, content_type = fetch_binary_url(
            frame_url,
            token,
            etag,
        )

        if payload is None:
            return None, response_etag

        LOGGER.info(
            "Render URL returned content-type=%s bytes=%d",
            content_type or "(missing)",
            len(payload),
        )

    if not payload:
        raise RuntimeError("Tesserae returned an empty frame")

    try:
        with Image.open(io.BytesIO(payload)) as source:
            LOGGER.info(
                "Decoded frame format=%s mode=%s size=%s",
                source.format,
                source.mode,
                source.size,
            )

            frame = normalize_image(source.copy())

    except Exception as exc:
        preview = payload[:160].decode(
            "utf-8",
            errors="replace",
        )

        raise RuntimeError(
            "Could not decode Tesserae frame as an image; "
            f"content_type={content_type!r}; "
            f"bytes={len(payload)}; "
            f"response_start={preview!r}"
        ) from exc

    return frame, response_etag



def status_poll_seconds(status: dict[str, Any]) -> int:
    value = status.get("next_poll_s", DEFAULT_POLL_SECONDS)

    try:
        seconds = int(value)
    except (TypeError, ValueError):
        seconds = DEFAULT_POLL_SECONDS

    return max(5, min(seconds, 3600))


def interruptible_sleep(seconds: int) -> None:
    deadline = time.monotonic() + seconds

    while RUNNING and time.monotonic() < deadline:
        time.sleep(0.2)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s %(levelname)s "
            "%(name)s: %(message)s"
        ),
    )

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    config = load_config()

    server_url = str(
        config.get("server_url") or DEFAULT_SERVER
    ).rstrip("/")

    device_id = str(
        config.get("device_id") or ""
    ).strip()

    token = str(
        config.get("device_token") or ""
    ).strip()

    LOGGER.info(
        "Starting Tesserae client for device %s",
        device_id or "(not configured)",
    )
    publish_qr_cache(server_url)

    with Display() as display:
        if not device_id or not token:
            display.show(setup_page())

            while RUNNING:
                time.sleep(0.2)

            return 0

        etag: str | None = None
        first_request = True
        last_paint_at: str | None = None
        last_error: str | None = None

        while RUNNING:
            poll_seconds = DEFAULT_POLL_SECONDS

            try:
                status = send_heartbeat(
                    server_url,
                    device_id,
                    token,
                    state="online",
                    last_paint_at=last_paint_at,
                    last_error=last_error,
                )
                publish_qr_cache(server_url)

                last_error = None

                poll_seconds = status_poll_seconds(status)

                frame, etag = fetch_frame(
                    server_url,
                    device_id,
                    token,
                    etag,
                )

                if frame is not None:
                    display.show(frame)

                    last_paint_at = time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ",
                        time.gmtime(),
                    )

                    LOGGER.info(
                        "Displayed Tesserae frame for %s",
                        device_id,
                    )

                    first_request = False

                elif first_request:
                    display.show(waiting_page(device_id))
                    first_request = False

                else:
                    LOGGER.info(
                        "Frame unchanged for %s",
                        device_id,
                    )

            except urllib.error.HTTPError as exc:
                last_error = (
                    f"HTTP {exc.code}: {exc.reason}"
                )

                LOGGER.error(
                    "Tesserae HTTP error %d: %s",
                    exc.code,
                    exc.reason,
                )

                if first_request:
                    if exc.code in (401, 403):
                        message = "Token rejected."
                    elif exc.code == 404:
                        message = "Device/frame not found."
                    else:
                        message = f"HTTP error {exc.code}."

                    display.show(error_page(message))
                    first_request = False

            except urllib.error.URLError as exc:
                last_error = (
                    f"Connection error: {exc.reason}"
                )

                LOGGER.error(
                    "Could not reach Tesserae: %s",
                    exc.reason,
                )

                if first_request:
                    display.show(
                        error_page("Server unavailable.")
                    )
                    first_request = False

            except Exception:
                LOGGER.exception(
                    "Tesserae frame update failed"
                )

                if first_request:
                    display.show(
                        error_page("Frame update failed.")
                    )
                    first_request = False

            interruptible_sleep(poll_seconds)

    LOGGER.info("Tesserae client stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
