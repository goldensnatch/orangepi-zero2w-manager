from __future__ import annotations

import json
import logging
import signal
import textwrap
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from zero2w_epaper import Display


LOGGER = logging.getLogger("zero2w-reader")

WIDTH = 250
HEIGHT = 122

CONTENT_DIRECTORY = Path(
    "/opt/zero2w-manager/content/reader"
)

STATE_FILE = Path(
    "/opt/zero2w-manager/runtime/reader-state.json"
)

CHARACTERS_PER_LINE = 38
LINES_PER_PAGE = 8

RUNNING = True
PAGE_CHANGE = 0


def handle_shutdown(signum: int, _frame: object) -> None:
    global RUNNING

    LOGGER.info("Reader received shutdown signal %s", signum)
    RUNNING = False


def handle_next(_signum: int, _frame: object) -> None:
    global PAGE_CHANGE
    PAGE_CHANGE += 1


def handle_previous(_signum: int, _frame: object) -> None:
    global PAGE_CHANGE
    PAGE_CHANGE -= 1


def load_state() -> dict[str, object]:
    try:
        data = json.loads(STATE_FILE.read_text())

        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass

    return {}


def save_state(filename: str, page_index: int) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    temporary = STATE_FILE.with_suffix(".tmp")

    temporary.write_text(
        json.dumps(
            {
                "filename": filename,
                "page_index": page_index,
            },
            indent=2,
        )
        + "\n"
    )

    temporary.replace(STATE_FILE)


def find_documents() -> list[Path]:
    CONTENT_DIRECTORY.mkdir(parents=True, exist_ok=True)

    return sorted(
        path
        for path in CONTENT_DIRECTORY.glob("*.txt")
        if path.is_file()
    )


def wrap_document(text: str) -> list[str]:
    lines: list[str] = []

    for paragraph in text.replace("\r\n", "\n").split("\n"):
        paragraph = paragraph.strip()

        if not paragraph:
            lines.append("")
            continue

        wrapped = textwrap.wrap(
            paragraph,
            width=CHARACTERS_PER_LINE,
            replace_whitespace=True,
            drop_whitespace=True,
        )

        lines.extend(wrapped or [""])

    return lines


def paginate(lines: list[str]) -> list[list[str]]:
    if not lines:
        return [["This document is empty."]]

    pages = []

    for index in range(0, len(lines), LINES_PER_PAGE):
        pages.append(lines[index:index + LINES_PER_PAGE])

    return pages


def load_document(path: Path) -> list[list[str]]:
    try:
        text = path.read_text(
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        LOGGER.error("Could not read %s: %s", path, exc)
        return [[f"Could not read {path.name}"]]

    return paginate(wrap_document(text))


def draw_reader_page(
    filename: str,
    page: list[str],
    page_index: int,
    page_count: int,
) -> Image.Image:
    image = Image.new("1", (WIDTH, HEIGHT), 255)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()

    draw.rectangle(
        (0, 0, WIDTH - 1, HEIGHT - 1),
        outline=0,
    )

    title = filename[:28]

    draw.text(
        (6, 4),
        title,
        font=font,
        fill=0,
    )

    counter = f"{page_index + 1}/{page_count}"

    counter_width = draw.textbbox(
        (0, 0),
        counter,
        font=font,
    )[2]

    draw.text(
        (WIDTH - counter_width - 7, 4),
        counter,
        font=font,
        fill=0,
    )

    draw.line(
        (5, 17, WIDTH - 6, 17),
        fill=0,
    )

    y = 22

    for line in page:
        draw.text(
            (7, y),
            line,
            font=font,
            fill=0,
        )
        y += 12

    return image


def draw_no_documents() -> Image.Image:
    image = Image.new("1", (WIDTH, HEIGHT), 255)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()

    draw.rectangle(
        (0, 0, WIDTH - 1, HEIGHT - 1),
        outline=0,
    )

    draw.text(
        (8, 8),
        "READER",
        font=font,
        fill=0,
    )

    draw.line(
        (6, 22, WIDTH - 7, 22),
        fill=0,
    )

    rows = [
        "No .txt files found.",
        "",
        "Add documents to:",
        "/opt/zero2w-manager/",
        "content/reader/",
        "",
        "Hold ENTER to exit.",
    ]

    y = 31

    for row in rows:
        draw.text(
            (8, y),
            row,
            font=font,
            fill=0,
        )
        y += 12

    return image


def main() -> int:
    global PAGE_CHANGE

    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s %(levelname)s "
            "%(name)s: %(message)s"
        ),
    )

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGUSR1, handle_next)
    signal.signal(signal.SIGUSR2, handle_previous)

    documents = find_documents()
    state = load_state()

    LOGGER.info(
        "Reader found %d document(s)",
        len(documents),
    )

    with Display() as display:
        if not documents:
            display.show(draw_no_documents())

            while RUNNING:
                time.sleep(0.2)

            return 0

        stored_filename = state.get("filename")
        document_index = 0

        if isinstance(stored_filename, str):
            for index, document in enumerate(documents):
                if document.name == stored_filename:
                    document_index = index
                    break

        document = documents[document_index]
        pages = load_document(document)

        stored_page = state.get("page_index", 0)

        if isinstance(stored_page, int):
            page_index = max(
                0,
                min(stored_page, len(pages) - 1),
            )
        else:
            page_index = 0

        needs_redraw = True

        while RUNNING:
            if PAGE_CHANGE:
                change = PAGE_CHANGE
                PAGE_CHANGE = 0

                page_index += change

                if page_index >= len(pages):
                    page_index = 0

                if page_index < 0:
                    page_index = len(pages) - 1

                save_state(
                    document.name,
                    page_index,
                )

                needs_redraw = True

            if needs_redraw:
                display.show(
                    draw_reader_page(
                        document.name,
                        pages[page_index],
                        page_index,
                        len(pages),
                    )
                )

                LOGGER.info(
                    "Displayed %s page %d/%d",
                    document.name,
                    page_index + 1,
                    len(pages),
                )

                needs_redraw = False

            time.sleep(0.1)

    LOGGER.info("Reader stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
