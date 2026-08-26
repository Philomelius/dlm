#!/usr/bin/env python3
"""DLM: manage Beast's qBittorrent download queue from the terminal."""

from __future__ import annotations

import argparse
import curses
from datetime import datetime
import http.cookiejar
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


BASE_URL = os.environ.get("DLM_QBITTORRENT_URL", "http://192.168.1.27:8080").rstrip(
    "/"
)
TRANSMISSION_URL = os.environ.get(
    "DLM_TRANSMISSION_URL",
    "http://127.0.0.1:9091/transmission/rpc",
).strip()
CREDENTIALS_PATH = Path(
    os.environ.get(
        "DLM_CREDENTIALS_FILE",
        "/media/nicolas/beast22/appdata/media-stack/qbittorrent.credentials",
    )
)
STATE_PATH = Path(
    os.environ.get(
        "DLM_STATE_FILE",
        str(Path.home() / ".local/state/dlm/torrents.json"),
    )
)
UNKNOWN_ETA = 8_640_000
IDLE_STATES = {"pausedDL", "pausedUP", "queuedDL", "queuedUP", "stoppedDL", "stoppedUP"}
TRANSMISSION_STATES = {
    0: "stoppedDL",
    1: "queuedDL",
    2: "checkingDL",
    3: "queuedDL",
    4: "downloading",
    5: "queuedUP",
    6: "uploading",
}
ANSI_RESET = "\033[0m"
ANSI_BOLD_CYAN = "\033[1;36m"
ANSI_DIM = "\033[2m"
ANSI_GREEN = "\033[92m"
ANSI_CYAN = "\033[96m"
ANSI_MAGENTA = "\033[95m"
ANSI_YELLOW = "\033[93m"
ANSI_WHITE = "\033[97m"
TUI_MIN_WIDTH = 74
TUI_MIN_HEIGHT = 12
TUI_INPUT_TIMEOUT_MS = 20
TUI_ESCAPE_DELAY_MS = 25
TUI_DOUBLE_ARROW_SECONDS = 0.35
# ncurses' xterm extended-key codes for kUP5 and kDN5. Python exposes these
# through getch() even though they sit above curses.KEY_MAX.
TUI_CTRL_UP = 567
TUI_CTRL_DOWN = 526
TUI_ACTIONS = (
    ("START / RESUME", "start"),
    ("PAUSE / STOP", "stop"),
    ("DELETE + DATA", "delete"),
)
QUEUE_PREFERENCES = {
    "queueing_enabled": True,
    "max_active_downloads": 1,
    # qBittorrent's slow-torrent exception does not override this separate
    # limit, so it must remain unlimited for stalled jobs not to block queueing.
    "max_active_torrents": -1,
    "max_active_uploads": 1,
    "dont_count_slow_torrents": True,
    "max_ratio": 0,
    "max_ratio_act": 0,
    "max_ratio_enabled": True,
}


def terminal_supports_color() -> bool:
    return (
        sys.stdout.isatty()
        and "NO_COLOR" not in os.environ
        and os.environ.get("TERM", "") != "dumb"
    )


def styled(value: str, ansi: str, enabled: bool) -> str:
    return f"{ansi}{value}{ANSI_RESET}" if enabled else value


def format_bytes(value: int | float) -> str:
    amount = float(value or 0)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if abs(amount) < 1024 or unit == units[-1]:
            return f"{amount:.0f}{unit}" if unit in {"B", "KiB"} else f"{amount:.2f}{unit}"
        amount /= 1024
    return f"{amount:.2f}TiB"


def format_rate(value: int | float) -> str:
    return f"{format_bytes(value)}/s" if value else "0B/s"


def parse_active_limit(value: str) -> int:
    """Parse a positive qBittorrent active-download limit."""
    text = value.strip()
    if not re.fullmatch(r"\d+", text):
        raise ValueError("active limit must be a whole number")
    maximum = int(text)
    if maximum < 1:
        raise ValueError("active limit must be at least 1")
    return maximum


def parse_download_limit(value: str) -> int:
    """Parse a speed limit into bytes/s; -1 is the unlimited UI sentinel."""
    text = value.strip().casefold().replace(" ", "")
    if text == "-1":
        return 0
    match = re.fullmatch(
        r"(\d+(?:\.\d+)?)(k|kb|kib|m|mb|mib|g|gb|gib)?(?:/s)?",
        text,
    )
    if not match:
        raise ValueError("use KiB/s, or suffix the value with K, M, or G")
    amount = float(match.group(1))
    if amount <= 0:
        raise ValueError("download limit must be positive, or -1 for unlimited")
    unit = match.group(2) or "kib"
    multipliers = {
        "k": 1024,
        "kb": 1024,
        "kib": 1024,
        "m": 1024**2,
        "mb": 1024**2,
        "mib": 1024**2,
        "g": 1024**3,
        "gb": 1024**3,
        "gib": 1024**3,
    }
    return max(1, round(amount * multipliers[unit]))


def download_limit_input(bytes_per_second: int) -> str:
    """Represent qBittorrent's zero/unlimited value in the DLM prompt."""
    if bytes_per_second <= 0:
        return "-1"
    kibibytes = bytes_per_second / 1024
    return f"{kibibytes:g}"


def format_eta(value: int | float | None) -> str:
    if value is None:
        return "--"
    seconds = int(value)
    if seconds < 0 or seconds >= UNKNOWN_ETA:
        return "--"
    days, seconds = divmod(seconds, 86_400)
    hours, seconds = divmod(seconds, 3_600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f"{days}d{hours:02d}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m{seconds:02d}s"


def one_line_name(value: object) -> str:
    """Keep untrusted torrent names from creating extra terminal rows."""
    return " ".join(str(value or "").split())


def truncate_name(name: str, width: int) -> str:
    """Fit a name on one terminal row and mark hidden text with an ellipsis."""
    if width <= 0:
        return ""
    if len(name) <= width:
        return name
    if width == 1:
        return "…"
    return f"{name[: width - 1]}…"


def marquee_name(
    name: str,
    width: int,
    elapsed: float,
    start_pause: float = 1.0,
    step_time: float = 0.12,
    end_pause: float = 1.0,
) -> str:
    """Reveal a selected long name by moving its viewport toward the right."""
    if width <= 0:
        return ""
    if len(name) <= width:
        return name
    last_offset = len(name) - width
    travel_time = last_offset * step_time
    cycle_time = start_pause + travel_time + end_pause
    phase = max(0.0, elapsed) % cycle_time
    if phase < start_pause:
        offset = 0
    elif phase >= start_pause + travel_time:
        offset = last_offset
    else:
        offset = min(last_offset, int((phase - start_pause) / step_time))
    return name[offset : offset + width]


class TorrentIds:
    """Persistent integer IDs so users never need to handle torrent hashes."""

    def __init__(self, path: Path = STATE_PATH):
        self.path = path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        except (json.JSONDecodeError, OSError) as error:
            raise RuntimeError(f"Cannot read DLM state {path}: {error}") from error
        self.next_id = max(1, int(data.get("nextId", 1)))
        self.hash_to_id = {
            str(torrent_hash).casefold(): int(torrent_id)
            for torrent_hash, torrent_id in data.get("hashToId", {}).items()
        }

    def sync(self, torrents: list[dict]) -> dict[str, int]:
        changed = False
        qbit_hashes = {
            str(torrent.get("hash") or "").casefold()
            for torrent in torrents
            if torrent.get("source") != "transmission"
        }
        for torrent in torrents:
            torrent_hash = str(torrent.get("hash") or "").casefold()
            source_hash = str(torrent.get("source_hash") or "").casefold()
            if (
                torrent.get("source") == "transmission"
                and torrent_hash not in self.hash_to_id
                and source_hash in self.hash_to_id
                and source_hash not in qbit_hashes
            ):
                # Migrate IDs assigned by the first combined-list build,
                # before Transmission keys became client-qualified.
                self.hash_to_id[torrent_hash] = self.hash_to_id.pop(source_hash)
                changed = True
            if torrent_hash and torrent_hash not in self.hash_to_id:
                self.hash_to_id[torrent_hash] = self.next_id
                self.next_id += 1
                changed = True
        if changed:
            self.save()
        return self.hash_to_id

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {"nextId": self.next_id, "hashToId": self.hash_to_id},
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)

    def current_by_id(self, torrents: list[dict], torrent_id: int) -> dict | None:
        self.sync(torrents)
        return next(
            (
                torrent
                for torrent in torrents
                if self.hash_to_id.get(
                    str(torrent.get("hash") or "").casefold()
                )
                == torrent_id
            ),
            None,
        )


class QBittorrent:
    def __init__(self) -> None:
        credentials = dict(
            line.split("=", 1)
            for line in CREDENTIALS_PATH.read_text(encoding="utf-8").splitlines()
            if "=" in line
        )
        self.username = credentials["username"]
        self.password = credentials["password"]
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def post(self, path: str, **fields: str) -> str:
        request = urllib.request.Request(
            f"{BASE_URL}{path}",
            data=urllib.parse.urlencode(fields).encode(),
            headers={"Referer": BASE_URL, "User-Agent": "Beast DLM"},
        )
        with self.opener.open(request, timeout=15) as response:
            return response.read().decode()

    def login(self) -> None:
        result = self.post(
            "/api/v2/auth/login",
            username=self.username,
            password=self.password,
        )
        if result.strip() not in {"", "Ok."}:
            raise RuntimeError("qBittorrent login failed")

    def torrents(self) -> list[dict]:
        with self.opener.open(f"{BASE_URL}/api/v2/torrents/info", timeout=15) as response:
            return json.load(response)

    def preferences(self) -> dict:
        with self.opener.open(
            f"{BASE_URL}/api/v2/app/preferences", timeout=15
        ) as response:
            return json.load(response)

    def _versioned_action(self, current: str, legacy: str, hashes: str) -> None:
        try:
            self.post(f"/api/v2/torrents/{current}", hashes=hashes)
        except urllib.error.HTTPError as error:
            if error.code not in {404, 405}:
                raise
            self.post(f"/api/v2/torrents/{legacy}", hashes=hashes)

    def stop(self, hashes: str = "all") -> None:
        self._versioned_action("stop", "pause", hashes)

    def start(self, hashes: str) -> None:
        self._versioned_action("start", "resume", hashes)

    def configure_queue(self) -> None:
        preferences = dict(QUEUE_PREFERENCES)
        # The user-adjustable limit is persisted by qBittorrent. Do not reset
        # it whenever DLM resumes the queue.
        preferences.pop("max_active_downloads", None)
        self.post(
            "/api/v2/app/setPreferences",
            json=json.dumps(preferences, separators=(",", ":")),
        )

    def set_max_active_downloads(self, maximum: int) -> None:
        self.post(
            "/api/v2/app/setPreferences",
            json=json.dumps(
                {
                    "queueing_enabled": True,
                    "max_active_downloads": maximum,
                    # Keep this unlimited so stalled torrents do not occupy
                    # the user-selected active-download slots.
                    "max_active_torrents": -1,
                    "dont_count_slow_torrents": True,
                },
                separators=(",", ":"),
            ),
        )

    def download_limit(self) -> int:
        with self.opener.open(
            f"{BASE_URL}/api/v2/transfer/downloadLimit", timeout=15
        ) as response:
            return int(response.read().decode().strip())

    def set_download_limit(self, bytes_per_second: int) -> None:
        self.post(
            "/api/v2/transfer/setDownloadLimit",
            limit=str(max(0, bytes_per_second)),
        )

    def remove_with_files(self, torrent_hash: str) -> None:
        self.post(
            "/api/v2/torrents/delete",
            hashes=torrent_hash,
            deleteFiles="true",
        )


class Transmission:
    """Read and control torrents through Transmission's RPC service."""

    FIELDS = (
        "id",
        "name",
        "hashString",
        "percentDone",
        "totalSize",
        "sizeWhenDone",
        "haveValid",
        "rateDownload",
        "rateUpload",
        "eta",
        "status",
    )

    def __init__(self, url: str = TRANSMISSION_URL) -> None:
        self.url = url
        self.session_id = ""
        self.opener = urllib.request.build_opener()
        self.last_error: str | None = None

    def call(self, method: str, arguments: dict | None = None) -> dict:
        body = json.dumps(
            {"method": method, "arguments": arguments or {}},
            separators=(",", ":"),
        ).encode()
        for _ in range(2):
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "Beast DLM",
            }
            if self.session_id:
                headers["X-Transmission-Session-Id"] = self.session_id
            request = urllib.request.Request(
                self.url,
                data=body,
                headers=headers,
            )
            try:
                with self.opener.open(request, timeout=10) as response:
                    payload = json.load(response)
            except urllib.error.HTTPError as error:
                if error.code != 409:
                    raise
                self.session_id = error.headers.get(
                    "X-Transmission-Session-Id", ""
                )
                if not self.session_id:
                    raise RuntimeError(
                        "Transmission did not provide an RPC session ID"
                    ) from error
                continue
            if payload.get("result") != "success":
                raise RuntimeError(
                    f"Transmission RPC failed: {payload.get('result') or 'unknown error'}"
                )
            return dict(payload.get("arguments") or {})
        raise RuntimeError("Transmission RPC session negotiation failed")

    def start(self, torrent_id: int | str) -> None:
        self.call("torrent-start", {"ids": [torrent_id]})

    def stop(self, torrent_id: int | str) -> None:
        self.call("torrent-stop", {"ids": [torrent_id]})

    def remove_with_files(self, torrent_id: int | str) -> None:
        self.call(
            "torrent-remove",
            {"ids": [torrent_id], "delete-local-data": True},
        )

    def torrents(self) -> list[dict]:
        if not self.url:
            return []
        try:
            result = self.call(
                "torrent-get",
                {"fields": list(self.FIELDS)},
            )
        except (OSError, RuntimeError, ValueError, urllib.error.URLError) as error:
            # Transmission is an optional secondary client. A stopped or
            # absent daemon must not make the qBittorrent dashboard unusable.
            self.last_error = str(error)
            return []
        self.last_error = None
        torrents: list[dict] = []
        for item in result.get("torrents", []):
            source_hash = str(item.get("hashString") or "").casefold()
            raw_progress = float(item.get("percentDone") or 0)
            progress = (
                max(0.0, min(1.0, raw_progress))
                if math.isfinite(raw_progress)
                else 0.0
            )
            state = TRANSMISSION_STATES.get(int(item.get("status") or 0), "unknown")
            if state == "stoppedDL" and progress >= 1:
                state = "stoppedUP"
            torrents.append(
                {
                    # The client prefix keeps DLM IDs unique if the same
                    # info-hash happens to exist in both torrent clients.
                    "hash": f"transmission:{source_hash}",
                    "source_hash": source_hash,
                    "name": str(item.get("name") or ""),
                    "progress": progress,
                    "size": int(
                        item.get("sizeWhenDone")
                        or item.get("totalSize")
                        or 0
                    ),
                    "completed": int(item.get("haveValid") or 0),
                    "dlspeed": int(item.get("rateDownload") or 0),
                    "upspeed": int(item.get("rateUpload") or 0),
                    "eta": int(item.get("eta") or -1),
                    "state": state,
                    "source": "transmission",
                    "source_id": int(item.get("id") or 0),
                }
            )
        return torrents


def initial_order(torrent: dict) -> tuple:
    state = str(torrent.get("state") or "")
    speed = int(torrent.get("dlspeed") or 0) + int(torrent.get("upspeed") or 0)
    progress = float(torrent.get("progress") or 0)
    return (
        not bool(speed),
        state in IDLE_STATES,
        progress >= 1,
        str(torrent.get("name") or "").casefold(),
    )


def torrent_source(torrent: dict) -> str:
    return (
        "transmission"
        if torrent.get("source") == "transmission"
        else "qbittorrent"
    )


def source_badge(torrent: dict) -> str:
    return "TR" if torrent_source(torrent) == "transmission" else "QB"


def torrent_table(
    torrents: list[dict],
    ids: dict[str, int],
    terminal_width: int | None = None,
    use_color: bool | None = None,
) -> str:
    width = terminal_width or shutil.get_terminal_size((140, 24)).columns
    color = terminal_supports_color() if use_color is None else use_color
    id_width = max(2, max((len(str(value)) for value in ids.values()), default=1))
    prefix_width = id_width + 51
    name_width = max(1, width - prefix_width)
    header = (
        f"{'#':>{id_width}} {'DONE':>7} {'TOTAL':>10} {'DOWN':>10} "
        f"{'UP':>10} {'ETA':>8} NAME"
    )
    rows = [
        styled(header, ANSI_BOLD_CYAN, color),
        styled("-" * width, ANSI_DIM, color),
    ]

    for index, torrent in enumerate(torrents):
        torrent_id = ids[str(torrent.get("hash") or "").casefold()]
        name = truncate_name(
            f"{source_badge(torrent)} {one_line_name(torrent.get('name'))}",
            name_width,
        )
        progress = float(torrent.get("progress") or 0)
        down_speed = int(torrent.get("dlspeed") or 0)
        up_speed = int(torrent.get("upspeed") or 0)
        eta = format_eta(torrent.get("eta"))
        row_prefix = "".join(
            (
                styled(f"{torrent_id:>{id_width}}", ANSI_WHITE, color),
                " ",
                styled(f"{progress * 100:6.2f}%", ANSI_GREEN, color),
                " ",
                styled(
                    f"{format_bytes(torrent.get('size') or 0):>10}",
                    ANSI_CYAN,
                    color,
                ),
                " ",
                styled(f"{format_rate(down_speed):>10}", ANSI_GREEN, color),
                " ",
                styled(f"{format_rate(up_speed):>10}", ANSI_MAGENTA, color),
                " ",
                styled(f"{eta:>8}", ANSI_YELLOW, color),
                " ",
            )
        )
        rows.append(f"{row_prefix}{styled(name, ANSI_WHITE, color)}")
        if index < len(torrents) - 1:
            rows.append("")

    total_size = sum(int(item.get("size") or 0) for item in torrents)
    total_completed = sum(int(item.get("completed") or 0) for item in torrents)
    total_down = sum(int(item.get("dlspeed") or 0) for item in torrents)
    total_up = sum(int(item.get("upspeed") or 0) for item in torrents)
    total_progress = total_completed / total_size * 100 if total_size else 0
    rows.extend(
        [
            styled("-" * width, ANSI_DIM, color),
            f"{'':>{id_width}} {total_progress:6.2f}% "
            f"{format_bytes(total_size):>10} "
            f"{format_rate(total_down):>10} "
            f"{format_rate(total_up):>10} "
            f"{'--':>8} TOTAL ({len(torrents)})",
        ]
    )
    return "\n".join(rows)


def filtered_torrents(
    qbit: QBittorrent,
    active_only: bool,
    transmission: Transmission | None = None,
) -> list[dict]:
    torrents = []
    for torrent in qbit.torrents():
        item = dict(torrent)
        item["source"] = "qbittorrent"
        torrents.append(item)
    if transmission is not None:
        torrents.extend(transmission.torrents())
    torrents.sort(key=initial_order)
    if active_only:
        torrents = [
            torrent
            for torrent in torrents
            if str(torrent.get("state") or "") not in IDLE_STATES
        ]
    return torrents


def show_list(
    qbit: QBittorrent,
    transmission: Transmission | None,
    ids: TorrentIds,
    active_only: bool,
) -> None:
    torrents, id_map = list_torrents(qbit, transmission, ids, active_only)
    print(
        styled(
            f"DLM — {datetime.now().astimezone():%Y-%m-%d %H:%M:%S %Z}",
            ANSI_BOLD_CYAN,
            terminal_supports_color(),
        )
    )
    print(torrent_table(torrents, id_map))


def list_torrents(
    qbit: QBittorrent,
    transmission: Transmission | None,
    ids: TorrentIds,
    active_only: bool,
) -> tuple[list[dict], dict[str, int]]:
    torrents = filtered_torrents(qbit, active_only, transmission)
    id_map = ids.sync(torrents)
    torrents.sort(
        key=lambda torrent: (
            initial_order(torrent),
            id_map[str(torrent.get("hash") or "").casefold()],
        )
    )
    return torrents, id_map


def tui_rows(
    torrents: list[dict], ids: dict[str, int], width: int
) -> tuple[list[dict | None], int, int]:
    """Build scrollable display rows without embedding terminal control codes."""
    id_width = max(2, max((len(str(value)) for value in ids.values()), default=1))
    # Two leading cells are reserved for the selection marker and its space.
    prefix_width = id_width + 53
    rows: list[dict | None] = []
    for index, torrent in enumerate(torrents):
        torrent_id = ids[str(torrent.get("hash") or "").casefold()]
        common = {
            "torrent_index": index,
            "id": f"{torrent_id:>{id_width}}",
            "source": torrent_source(torrent),
            "done": f"{float(torrent.get('progress') or 0) * 100:6.2f}%",
            "total": f"{format_bytes(torrent.get('size') or 0):>10}",
            "down": f"{format_rate(torrent.get('dlspeed') or 0):>10}",
            "up": f"{format_rate(torrent.get('upspeed') or 0):>10}",
            "eta": f"{format_eta(torrent.get('eta')):>8}",
            "active": bool(
                int(torrent.get("dlspeed") or 0) + int(torrent.get("upspeed") or 0)
            ),
        }
        rows.append({**common, "name": one_line_name(torrent.get("name"))})
        if index < len(torrents) - 1:
            rows.append(None)
    return rows, id_width, prefix_width


def tui_attributes() -> dict[str, int]:
    attributes = {
        "border": curses.A_DIM,
        "title": curses.A_BOLD,
        "header": curses.A_BOLD,
        "id": curses.A_BOLD,
        "done": 0,
        "total": 0,
        "down": curses.A_BOLD,
        "up": 0,
        "eta": 0,
        "name": 0,
        "selected": curses.A_BOLD,
        "qbittorrent": curses.A_BOLD,
        "transmission": curses.A_BOLD,
        "footer": curses.A_BOLD,
        "error": curses.A_BOLD,
    }
    if "NO_COLOR" in os.environ or not curses.has_colors():
        return attributes
    curses.start_color()
    try:
        curses.use_default_colors()
        background = -1
    except curses.error:
        background = curses.COLOR_BLACK
    colors = {
        "border": curses.COLOR_CYAN,
        "title": curses.COLOR_CYAN,
        "header": curses.COLOR_CYAN,
        "id": curses.COLOR_WHITE,
        "done": curses.COLOR_GREEN,
        "total": curses.COLOR_CYAN,
        "down": curses.COLOR_GREEN,
        "up": curses.COLOR_MAGENTA,
        "eta": curses.COLOR_YELLOW,
        "name": curses.COLOR_WHITE,
        "selected": curses.COLOR_YELLOW,
        "qbittorrent": curses.COLOR_CYAN,
        "transmission": curses.COLOR_MAGENTA,
        "footer": curses.COLOR_CYAN,
        "error": curses.COLOR_RED,
    }
    for pair, (key, foreground) in enumerate(colors.items(), start=1):
        curses.init_pair(pair, foreground, background)
        attributes[key] |= curses.color_pair(pair)
    return attributes


def tui_addstr(
    screen: curses.window,
    y: int,
    x: int,
    value: str,
    attribute: int = 0,
    limit: int | None = None,
) -> None:
    height, width = screen.getmaxyx()
    if y < 0 or y >= height or x < 0 or x >= width:
        return
    available = max(0, width - x - 1)
    if limit is not None:
        available = min(available, max(0, limit))
    if not available:
        return
    try:
        screen.addnstr(y, x, value, available, attribute)
    except curses.error:
        pass


def tui_hline(screen: curses.window, y: int, attribute: int) -> None:
    _, width = screen.getmaxyx()
    if width < 2:
        return
    try:
        screen.hline(y, 1, curses.ACS_HLINE, width - 2, attribute)
    except curses.error:
        pass


def tui_stats(torrents: list[dict]) -> str:
    total_size = sum(int(item.get("size") or 0) for item in torrents)
    completed = sum(int(item.get("completed") or 0) for item in torrents)
    progress = completed / total_size * 100 if total_size else 0
    active = sum(
        1
        for item in torrents
        if int(item.get("dlspeed") or 0) + int(item.get("upspeed") or 0) > 0
    )
    down = sum(int(item.get("dlspeed") or 0) for item in torrents)
    up = sum(int(item.get("upspeed") or 0) for item in torrents)
    qbit_count = sum(
        1 for item in torrents if torrent_source(item) == "qbittorrent"
    )
    transmission_count = len(torrents) - qbit_count
    return (
        f"TORRENTS {len(torrents)} [QB {qbit_count} / TR {transmission_count}]"
        f"  //  ACTIVE {active}  //  DONE {progress:5.1f}%"
        f"  //  DOWN {format_rate(down)}  //  UP {format_rate(up)}"
    )


def tui_stats_line(
    torrents: list[dict], search_query: str, total_torrents: int
) -> str:
    stats = tui_stats(torrents)
    if not search_query:
        return stats
    return (
        f'SEARCH "{search_query}"  //  MATCHES {len(torrents)}/{total_torrents}'
        f"  //  {stats}"
    )


def stats_control_key(mouse_x: int, inner_x: int, stats: str) -> str | None:
    """Map clicks on the visible ACTIVE and DOWN status fields."""
    relative_x = mouse_x - inner_x
    for key, label in (("active", "ACTIVE "), ("down", "DOWN ")):
        start = stats.rfind(label)
        if start < 0:
            continue
        end = stats.find("  //", start)
        if end < 0:
            end = len(stats)
        if start <= relative_x < end:
            return key
    return None


def filter_torrents(torrents: list[dict], query: str) -> list[dict]:
    terms = query.casefold().split()
    if not terms:
        return list(torrents)
    return [
        torrent
        for torrent in torrents
        if all(
            term in str(torrent.get("name") or "").casefold()
            for term in terms
        )
    ]


def sort_torrents(
    torrents: list[dict],
    sort_key: str | None,
    descending: bool,
    ids: dict[str, int] | None = None,
) -> list[dict]:
    """Sort a filtered list while keeping unknown ETAs at the bottom."""
    if sort_key is None:
        return list(torrents)
    if sort_key == "name":
        return sorted(
            torrents,
            key=lambda item: one_line_name(item.get("name")).casefold(),
            reverse=descending,
        )
    if sort_key == "number":
        torrent_ids = ids or {}
        return sorted(
            torrents,
            key=lambda item: int(
                torrent_ids.get(
                    str(item.get("hash") or "").casefold(),
                    0,
                )
            ),
            reverse=descending,
        )
    if sort_key == "eta":
        def eta_value(item: dict) -> int:
            value = item.get("eta")
            return int(value) if value is not None else -1

        known = [
            item
            for item in torrents
            if 0 <= eta_value(item) < UNKNOWN_ETA
        ]
        unknown = [
            item
            for item in torrents
            if not 0 <= eta_value(item) < UNKNOWN_ETA
        ]
        return sorted(
            known,
            key=eta_value,
            reverse=descending,
        ) + unknown
    value_fields = {
        "done": "progress",
        "down": "dlspeed",
        "up": "upspeed",
    }
    field = value_fields.get(sort_key)
    if field is None:
        return list(torrents)
    return sorted(
        torrents,
        key=lambda item: float(item.get(field) or 0),
        reverse=descending,
    )


def next_sort_order(
    current_key: str | None,
    current_descending: bool,
    clicked_key: str,
) -> tuple[str, bool]:
    """A new column starts high-to-low; repeat clicks reverse direction."""
    if clicked_key == current_key:
        return clicked_key, not current_descending
    return clicked_key, True


def header_sort_key(mouse_x: int, inner_x: int, id_width: int) -> str | None:
    """Map a header click to a sortable column; only TOTAL is inert."""
    number_x = inner_x + 2
    if number_x <= mouse_x < number_x + id_width:
        return "number"
    x = number_x + id_width + 1
    for key, field_width in (
        ("done", 7),
        (None, 10),
        ("down", 10),
        ("up", 10),
        ("eta", 8),
    ):
        if x <= mouse_x < x + field_width:
            return key
        x += field_width + 1
    return "name" if mouse_x >= x else None


def is_primary_click(button_state: int) -> bool:
    click_mask = getattr(curses, "BUTTON1_CLICKED", 0) | getattr(
        curses, "BUTTON1_PRESSED", 0
    )
    return bool(button_state & click_mask)


def sort_header_label(
    label: str,
    key: str,
    width: int | None,
    active_key: str | None,
    descending: bool,
) -> str:
    indicator = "▼" if descending else "▲"
    if key == active_key:
        value = f"{label}{indicator}" if label == "#" else f"{label} {indicator}"
    else:
        value = label
    return f"{value:>{width}}" if width is not None else value


def selected_row_span(
    rows: list[dict | None], selected_index: int
) -> tuple[int, int] | None:
    matching = [
        index
        for index, row in enumerate(rows)
        if row is not None and int(row["torrent_index"]) == selected_index
    ]
    return (matching[0], matching[-1]) if matching else None


def ensure_selected_visible(
    rows: list[dict | None],
    selected_index: int,
    scroll: int,
    page_size: int,
) -> int:
    span = selected_row_span(rows, selected_index)
    if span is None:
        return 0
    first, last = span
    if first < scroll:
        return first
    if last >= scroll + page_size:
        return max(0, last - page_size + 1)
    return scroll


def page_selection(
    rows: list[dict | None],
    selected_index: int,
    page_size: int,
    direction: int,
    torrent_count: int,
) -> int:
    if not rows or torrent_count < 1:
        return 0
    span = selected_row_span(rows, selected_index)
    if span is None:
        return min(max(0, selected_index), torrent_count - 1)
    first, last = span
    anchor = first if direction < 0 else last
    target = min(len(rows) - 1, max(0, anchor + direction * page_size))
    positions = (
        range(target, len(rows)) if direction > 0 else range(target, -1, -1)
    )
    for position in positions:
        row = rows[position]
        if row is not None:
            candidate = int(row["torrent_index"])
            if candidate != selected_index:
                return candidate
    return torrent_count - 1 if direction > 0 else 0


def navigation_selection(
    key: int,
    rows: list[dict | None],
    selected_index: int,
    page_size: int,
    torrent_count: int,
) -> int:
    last = max(0, torrent_count - 1)
    if key in {curses.KEY_DOWN, ord("j"), ord("J")}:
        return min(last, selected_index + 1)
    if key in {curses.KEY_UP, ord("k"), ord("K")}:
        return max(0, selected_index - 1)
    if key in {curses.KEY_NPAGE, ord(" ")}:
        return page_selection(rows, selected_index, page_size, 1, torrent_count)
    if key == curses.KEY_PPAGE:
        return page_selection(rows, selected_index, page_size, -1, torrent_count)
    if key in {curses.KEY_HOME, ord("g")}:
        return 0
    if key in {curses.KEY_END, ord("G")}:
        return last
    if key == TUI_CTRL_UP:
        return 0
    if key == TUI_CTRL_DOWN:
        return last
    return selected_index


class ArrowTapTracker:
    """Accelerate a double arrow tap without delaying a normal single tap."""

    def __init__(self, timeout: float = TUI_DOUBLE_ARROW_SECONDS) -> None:
        self.timeout = timeout
        self.last_key: int | None = None
        self.last_at = 0.0
        self.origin = 0

    def reset(self) -> None:
        self.last_key = None
        self.last_at = 0.0

    def navigate(
        self,
        key: int,
        rows: list[dict | None],
        selected_index: int,
        page_size: int,
        torrent_count: int,
        now: float | None = None,
    ) -> int:
        if key not in {curses.KEY_UP, curses.KEY_DOWN}:
            self.reset()
            return navigation_selection(
                key,
                rows,
                selected_index,
                page_size,
                torrent_count,
            )
        timestamp = time.monotonic() if now is None else now
        direction = -1 if key == curses.KEY_UP else 1
        if key == self.last_key and timestamp - self.last_at <= self.timeout:
            origin = self.origin
            self.reset()
            return page_selection(
                rows,
                origin,
                page_size,
                direction,
                torrent_count,
            )
        self.last_key = key
        self.last_at = timestamp
        self.origin = selected_index
        return navigation_selection(
            key,
            rows,
            selected_index,
            page_size,
            torrent_count,
        )


def configure_tui_input(screen: curses.window) -> None:
    """Install low-latency Escape handling and modified-arrow key mappings."""
    try:
        curses.set_escdelay(TUI_ESCAPE_DELAY_MS)
    except (AttributeError, curses.error):
        pass
    for sequence, key_code in (
        ("\x1b[1;5A", TUI_CTRL_UP),
        ("\x1b[1;5B", TUI_CTRL_DOWN),
    ):
        try:
            curses.define_key(sequence, key_code)
        except (AttributeError, curses.error):
            pass
    screen.timeout(TUI_INPUT_TIMEOUT_MS)


def pending_key(screen: curses.window) -> int:
    """Poll without waiting so user input wins over a scheduled refresh."""
    screen.timeout(0)
    try:
        return screen.getch()
    finally:
        screen.timeout(TUI_INPUT_TIMEOUT_MS)


def draw_tui(
    screen: curses.window,
    torrents: list[dict],
    ids: dict[str, int],
    scroll: int,
    selected_index: int,
    interval: float,
    attributes: dict[str, int],
    error: str | None,
    notice: str | None,
    search_query: str,
    total_torrents: int,
    selected_elapsed: float,
    sort_key: str | None,
    sort_descending: bool,
) -> tuple[int, int, list[dict | None]]:
    screen.erase()
    height, width = screen.getmaxyx()
    try:
        screen.attron(attributes["border"])
        screen.border()
        screen.attroff(attributes["border"])
    except curses.error:
        pass

    if width < TUI_MIN_WIDTH or height < TUI_MIN_HEIGHT:
        warning = f"Terminal too small — resize to at least {TUI_MIN_WIDTH}x{TUI_MIN_HEIGHT}"
        tui_addstr(
            screen,
            max(1, height // 2),
            max(1, (width - len(warning)) // 2),
            warning,
            attributes["error"],
        )
        screen.refresh()
        return 0, 1, []

    inner_x = 2
    inner_width = width - 4
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    tui_addstr(
        screen,
        1,
        inner_x,
        "DLM // BEAST DOWNLOAD MANAGER",
        attributes["title"],
        inner_width,
    )
    tui_addstr(
        screen,
        1,
        max(inner_x, width - len(timestamp) - 2),
        timestamp,
        attributes["title"],
        len(timestamp),
    )
    stats = tui_stats_line(torrents, search_query, total_torrents)
    tui_addstr(
        screen,
        2,
        inner_x,
        stats,
        attributes["footer"],
        inner_width,
    )
    tui_hline(screen, 3, attributes["border"])

    rows, id_width, prefix_width = tui_rows(torrents, ids, inner_width)
    header = (
        f"  {sort_header_label('#', 'number', id_width, sort_key, sort_descending)} "
        f"{sort_header_label('DONE', 'done', 7, sort_key, sort_descending)} "
        f"{'TOTAL':>10} "
        f"{sort_header_label('DOWN', 'down', 10, sort_key, sort_descending)} "
        f"{sort_header_label('UP', 'up', 10, sort_key, sort_descending)} "
        f"{sort_header_label('ETA', 'eta', 8, sort_key, sort_descending)} "
        f"{sort_header_label('NAME', 'name', None, sort_key, sort_descending)}"
    )
    tui_addstr(screen, 4, inner_x, header, attributes["header"], inner_width)

    content_top = 5
    content_bottom = height - 3
    content_height = max(1, content_bottom - content_top)
    max_scroll = max(0, len(rows) - content_height)
    scroll = min(max(0, scroll), max_scroll)
    visible_rows = rows[scroll : scroll + content_height]
    for offset, row in enumerate(visible_rows):
        if row is None:
            continue
        y = content_top + offset
        selected = int(row["torrent_index"]) == selected_index
        marker = ">" if selected else " "
        tui_addstr(
            screen,
            y,
            inner_x,
            marker,
            attributes["selected"] if selected else attributes["name"],
            1,
        )
        x = inner_x + 2
        for key, value, field_width in (
            ("id", row["id"], id_width),
            ("done", row["done"], 7),
            ("total", row["total"], 10),
            ("down", row["down"], 10),
            ("up", row["up"], 10),
            ("eta", row["eta"], 8),
        ):
            tui_addstr(
                screen,
                y,
                x,
                str(value),
                attributes["selected"]
                if selected and key == "id"
                else attributes[key],
                field_width,
            )
            x += field_width + 1
        source = str(row["source"])
        badge = "TR" if source == "transmission" else "QB"
        tui_addstr(
            screen,
            y,
            x,
            badge,
            attributes[source],
            2,
        )
        x += 3
        name_width = max(0, inner_width - prefix_width - 3)
        full_name = str(row["name"])
        visible_name = (
            marquee_name(full_name, name_width, selected_elapsed)
            if selected
            else truncate_name(full_name, name_width)
        )
        name_attribute = (
            attributes["selected"] if selected else attributes["name"]
        ) | (
            curses.A_BOLD if bool(row["active"]) else 0
        )
        tui_addstr(
            screen,
            y,
            x,
            visible_name,
            name_attribute,
            name_width,
        )

    tui_hline(screen, height - 3, attributes["border"])
    if error:
        status = f"ERROR // {error}  //  [R] RETRY  [Q] QUIT"
        status_attribute = attributes["error"]
    elif notice:
        status = notice
        status_attribute = attributes["footer"]
    else:
        status = (
            f"[ENTER] ACTION  [S] SEARCH  [Q] QUIT  [CLICK HEADER] SORT  "
            f"[ARROWS] SELECT  "
            f"[PGUP/PGDN] PAGE  [HOME/END] JUMP  //  AUTO {interval:g}s"
        )
        if search_query:
            status = f"[ESC] CLEAR SEARCH  //  {status}"
        status_attribute = attributes["footer"]
    position = (
        f"{selected_index + 1}/{len(torrents)}" if torrents else "0/0"
    )
    tui_addstr(
        screen,
        height - 2,
        inner_x,
        status,
        status_attribute,
        max(1, inner_width - len(position) - 2),
    )
    tui_addstr(
        screen,
        height - 2,
        width - len(position) - 2,
        position,
        attributes["footer"],
        len(position),
    )
    screen.refresh()
    return max_scroll, content_height, rows


def modal_menu(
    screen: curses.window,
    title: str,
    subtitle: str,
    options: tuple[tuple[str, str], ...],
    attributes: dict[str, int],
    selected: int = 0,
) -> str | None:
    height, width = screen.getmaxyx()
    menu_width = min(
        width - 4,
        max(46, len(title) + 6, len(subtitle) + 6, *(len(label) + 8 for label, _ in options)),
    )
    menu_height = len(options) + 6
    if menu_width < 20 or menu_height > height - 2:
        return None
    top = max(1, (height - menu_height) // 2)
    left = max(1, (width - menu_width) // 2)
    menu = curses.newwin(menu_height, menu_width, top, left)
    menu.keypad(True)
    menu.timeout(-1)

    while True:
        menu.erase()
        try:
            menu.attron(attributes["border"])
            menu.border()
            menu.attroff(attributes["border"])
        except curses.error:
            pass
        tui_addstr(menu, 1, 2, title, attributes["title"], menu_width - 4)
        tui_addstr(menu, 2, 2, subtitle, attributes["name"], menu_width - 4)
        for index, (label, _) in enumerate(options):
            marker = ">" if index == selected else " "
            option_attribute = (
                attributes["selected"]
                if index == selected
                else attributes["name"]
            )
            tui_addstr(
                menu,
                4 + index,
                3,
                f"{marker} {label}",
                option_attribute,
                menu_width - 6,
            )
        tui_addstr(
            menu,
            menu_height - 2,
            2,
            "ENTER SELECT  //  ESC CANCEL",
            attributes["footer"],
            menu_width - 4,
        )
        menu.refresh()
        key = menu.getch()
        if key == 27:
            del menu
            screen.touchwin()
            return None
        if key in {curses.KEY_UP, ord("k"), ord("K")}:
            selected = (selected - 1) % len(options)
        elif key in {curses.KEY_DOWN, ord("j"), ord("J")}:
            selected = (selected + 1) % len(options)
        elif key in {curses.KEY_ENTER, 10, 13}:
            value = options[selected][1]
            del menu
            screen.touchwin()
            return value
        elif key == curses.KEY_RESIZE:
            del menu
            screen.touchwin()
            return None


def search_prompt(
    screen: curses.window,
    attributes: dict[str, int],
    initial_query: str = "",
    *,
    title: str = "SEARCH TORRENTS",
    description: str = "All words must appear in the torrent name.",
    replace_on_type: bool = False,
) -> str | None:
    height, width = screen.getmaxyx()
    prompt_width = min(
        width - 4,
        max(50, len(initial_query) + 10, len(title) + 6, len(description) + 6),
    )
    prompt_height = 7
    if prompt_width < 20 or prompt_height > height - 2:
        return None
    top = max(1, (height - prompt_height) // 2)
    left = max(1, (width - prompt_width) // 2)
    prompt = curses.newwin(prompt_height, prompt_width, top, left)
    prompt.keypad(True)
    prompt.timeout(-1)
    query = initial_query
    replace_pending = replace_on_type
    try:
        curses.curs_set(1)
    except curses.error:
        pass

    try:
        while True:
            prompt.erase()
            try:
                prompt.attron(attributes["border"])
                prompt.border()
                prompt.attroff(attributes["border"])
            except curses.error:
                pass
            tui_addstr(
                prompt,
                1,
                2,
                title,
                attributes["title"],
                prompt_width - 4,
            )
            tui_addstr(
                prompt,
                2,
                2,
                description,
                attributes["name"],
                prompt_width - 4,
            )
            input_width = max(1, prompt_width - 7)
            visible_query = query[-input_width:]
            tui_addstr(
                prompt,
                4,
                2,
                f"> {visible_query}",
                attributes["selected"],
                prompt_width - 4,
            )
            tui_addstr(
                prompt,
                prompt_height - 2,
                2,
                "ENTER APPLY  //  ESC CANCEL",
                attributes["footer"],
                prompt_width - 4,
            )
            try:
                prompt.move(4, min(prompt_width - 3, 4 + len(visible_query)))
            except curses.error:
                pass
            prompt.refresh()
            key = prompt.getch()
            if key == 27:
                return None
            if key in {curses.KEY_ENTER, 10, 13}:
                return query.strip()
            if key in {curses.KEY_BACKSPACE, 8, 127}:
                query = "" if replace_pending else query[:-1]
                replace_pending = False
            elif key == curses.KEY_MOUSE:
                try:
                    curses.getmouse()
                except curses.error:
                    pass
            elif 32 <= key <= 126:
                if replace_pending:
                    query = ""
                    replace_pending = False
                query += chr(key)
            elif key == curses.KEY_RESIZE:
                return None
    finally:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        del prompt
        screen.touchwin()


def setting_prompt(
    screen: curses.window,
    attributes: dict[str, int],
    title: str,
    description: str,
    initial_value: str,
) -> str | None:
    return search_prompt(
        screen,
        attributes,
        initial_value,
        title=title,
        description=description,
        replace_on_type=True,
    )


def choose_torrent_action(
    screen: curses.window,
    torrent: dict,
    torrent_id: int,
    attributes: dict[str, int],
) -> str | None:
    name = str(torrent.get("name") or "(unnamed)")
    source = "TR" if torrent_source(torrent) == "transmission" else "QB"
    action = modal_menu(
        screen,
        f"{source} TORRENT #{torrent_id} // ACTION",
        name,
        TUI_ACTIONS,
        attributes,
    )
    if action != "delete":
        return action
    confirmation = modal_menu(
        screen,
        "PERMANENT DELETE",
        f"#{torrent_id} {name}",
        (
            ("CONFIRM DELETE TORRENT + DOWNLOADED DATA", "delete"),
        ),
        attributes,
    )
    return "delete" if confirmation == "delete" else None


def execute_tui_action(
    qbit: QBittorrent,
    torrent: dict,
    action: str,
    transmission: Transmission | None = None,
) -> str:
    if torrent_source(torrent) == "transmission":
        if transmission is None:
            raise RuntimeError("Transmission RPC is unavailable")
        torrent_id: int | str = int(torrent.get("source_id") or 0)
        if not torrent_id:
            torrent_id = str(torrent.get("source_hash") or "")
        if not torrent_id:
            raise RuntimeError("Selected Transmission torrent has no RPC ID")
        if action == "start":
            transmission.start(torrent_id)
            return "STARTED"
        if action == "stop":
            transmission.stop(torrent_id)
            return "PAUSED / STOPPED"
        if action == "delete":
            transmission.remove_with_files(torrent_id)
            return "DELETED TORRENT + DATA"
        raise RuntimeError(f"Unknown torrent action: {action}")

    torrent_hash = str(torrent.get("hash") or "")
    if not torrent_hash:
        raise RuntimeError("Selected torrent has no hash")
    if action == "start":
        qbit.configure_queue()
        qbit.start(torrent_hash)
        return "STARTED"
    if action == "stop":
        qbit.stop(torrent_hash)
        return "PAUSED / STOPPED"
    if action == "delete":
        qbit.remove_with_files(torrent_hash)
        return "DELETED TORRENT + DATA"
    raise RuntimeError(f"Unknown torrent action: {action}")


def _run_tui(
    screen: curses.window,
    qbit: QBittorrent,
    transmission: Transmission | None,
    ids: TorrentIds,
    active_only: bool,
    interval: float,
) -> None:
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.keypad(True)
    configure_tui_input(screen)
    attributes = tui_attributes()
    arrow_taps = ArrowTapTracker()
    all_torrents: list[dict] = []
    torrents: list[dict] = []
    id_map: dict[str, int] = {}
    search_query = ""
    error: str | None = None
    scroll = 0
    selected_index = 0
    notice: str | None = None
    notice_until = 0.0
    next_refresh = 0.0
    selected_since = time.monotonic()
    sort_key: str | None = None
    sort_descending = True
    last_header_click_key: str | None = None
    last_header_click_at = 0.0

    while True:
        now = time.monotonic()
        queued_key = -1
        if now >= next_refresh:
            queued_key = pending_key(screen)
        if now >= next_refresh and queued_key == -1:
            try:
                selected_hash = (
                    str(torrents[selected_index].get("hash") or "")
                    if torrents and selected_index < len(torrents)
                    else ""
                )
                all_torrents, id_map = list_torrents(
                    qbit,
                    transmission,
                    ids,
                    active_only,
                )
                torrents = sort_torrents(
                    filter_torrents(all_torrents, search_query),
                    sort_key,
                    sort_descending,
                    id_map,
                )
                if selected_hash:
                    selected_index = next(
                        (
                            index
                            for index, torrent in enumerate(torrents)
                            if str(torrent.get("hash") or "") == selected_hash
                        ),
                        min(selected_index, max(0, len(torrents) - 1)),
                    )
                else:
                    selected_index = min(selected_index, max(0, len(torrents) - 1))
                current_hash = (
                    str(torrents[selected_index].get("hash") or "")
                    if torrents and selected_index < len(torrents)
                    else ""
                )
                if current_hash != selected_hash:
                    selected_since = now
                error = None
            except Exception as refresh_error:
                error = str(refresh_error)
            next_refresh = now + interval

        active_notice = notice if time.monotonic() < notice_until else None
        max_scroll, page_size, rows = draw_tui(
            screen,
            torrents,
            id_map,
            scroll,
            selected_index,
            interval,
            attributes,
            error,
            active_notice,
            search_query,
            len(all_torrents),
            max(0.0, time.monotonic() - selected_since),
            sort_key,
            sort_descending,
        )
        scroll = min(
            max_scroll,
            ensure_selected_visible(rows, selected_index, scroll, page_size),
        )
        key = queued_key if queued_key != -1 else screen.getch()
        if key not in {curses.KEY_UP, curses.KEY_DOWN}:
            arrow_taps.reset()
        if key in {ord("q"), ord("Q")}:
            return
        if key == curses.KEY_MOUSE:
            try:
                _, mouse_x, mouse_y, _, button_state = curses.getmouse()
            except curses.error:
                continue
            if not is_primary_click(button_state):
                # Mouse-wheel scrolling and non-primary buttons remain inert.
                continue
            if mouse_y == 2:
                stats_key = stats_control_key(
                    mouse_x,
                    2,
                    tui_stats_line(torrents, search_query, len(all_torrents)),
                )
                if stats_key is None:
                    continue
                try:
                    if stats_key == "active":
                        current_maximum = int(
                            qbit.preferences().get("max_active_downloads") or 1
                        )
                        entered = setting_prompt(
                            screen,
                            attributes,
                            "MAXIMUM ACTIVE DOWNLOADS",
                            "qBittorrent slots; stalled torrents stay excluded.",
                            str(max(1, current_maximum)),
                        )
                        if entered is None:
                            next_refresh = max(
                                next_refresh, time.monotonic() + 0.25
                            )
                            continue
                        maximum = parse_active_limit(entered)
                        qbit.set_max_active_downloads(maximum)
                        notice = f"ACTIVE DOWNLOAD LIMIT // {maximum}"
                    else:
                        entered = setting_prompt(
                            screen,
                            attributes,
                            "MAXIMUM DOWNLOAD SPEED",
                            "Plain values use KiB/s; K/M/G accepted; -1 is unlimited.",
                            download_limit_input(qbit.download_limit()),
                        )
                        if entered is None:
                            next_refresh = max(
                                next_refresh, time.monotonic() + 0.25
                            )
                            continue
                        bytes_per_second = parse_download_limit(entered)
                        qbit.set_download_limit(bytes_per_second)
                        limit_label = (
                            "UNLIMITED"
                            if bytes_per_second == 0
                            else format_rate(bytes_per_second)
                        )
                        notice = f"DOWNLOAD LIMIT // {limit_label}"
                    notice_until = time.monotonic() + 4
                    next_refresh = 0.0
                except ValueError as setting_error:
                    notice = f"INVALID SETTING // {setting_error}"
                    notice_until = time.monotonic() + 4
                    next_refresh = max(next_refresh, time.monotonic() + 0.25)
                except Exception as setting_error:
                    notice = f"ERROR // {setting_error}"
                    notice_until = time.monotonic() + 4
                    next_refresh = max(next_refresh, time.monotonic() + 0.25)
                continue
            if mouse_y != 4:
                # Clicks outside the status controls and sortable header are
                # intentionally inert.
                continue
            id_width = max(
                2,
                max((len(str(value)) for value in id_map.values()), default=1),
            )
            clicked_key = header_sort_key(mouse_x, 2, id_width)
            if clicked_key is None:
                continue
            click_time = time.monotonic()
            if (
                clicked_key == last_header_click_key
                and click_time - last_header_click_at < 0.12
            ):
                continue
            last_header_click_key = clicked_key
            last_header_click_at = click_time
            sort_key, sort_descending = next_sort_order(
                sort_key,
                sort_descending,
                clicked_key,
            )
            torrents = sort_torrents(
                filter_torrents(all_torrents, search_query),
                sort_key,
                sort_descending,
                id_map,
            )
            selected_index = 0
            selected_since = click_time
            scroll = 0
            direction = "HIGH TO LOW" if sort_descending else "LOW TO HIGH"
            notice = f"SORT {sort_key.upper()} // {direction}"
            notice_until = click_time + 2
            continue
        if key == 27 and search_query:
            search_query = ""
            torrents = sort_torrents(
                filter_torrents(all_torrents, search_query),
                sort_key,
                sort_descending,
                id_map,
            )
            selected_index = 0
            selected_since = time.monotonic()
            scroll = 0
            notice = "SEARCH CLEARED"
            notice_until = time.monotonic() + 2
            continue
        if key in {ord("s"), ord("S")}:
            new_query = search_prompt(screen, attributes, search_query)
            next_refresh = max(
                next_refresh,
                time.monotonic() + 0.25,
            )
            if new_query:
                search_query = new_query
                torrents = sort_torrents(
                    filter_torrents(all_torrents, search_query),
                    sort_key,
                    sort_descending,
                    id_map,
                )
                selected_index = 0
                selected_since = time.monotonic()
                scroll = 0
                notice = f'SEARCH "{search_query}" // {len(torrents)} MATCHES'
                notice_until = time.monotonic() + 3
            continue
        if key in {ord("r"), ord("R")}:
            next_refresh = 0.0
            continue

        new_selection = arrow_taps.navigate(
            key,
            rows,
            selected_index,
            page_size,
            len(torrents),
        )
        if new_selection != selected_index:
            selected_index = new_selection
            selected_since = time.monotonic()
            scroll = ensure_selected_visible(
                rows, selected_index, scroll, page_size
            )
            continue

        if key in {curses.KEY_ENTER, 10, 13} and torrents:
            selected = torrents[selected_index]
            torrent_id = id_map[str(selected.get("hash") or "").casefold()]
            action = choose_torrent_action(
                screen, selected, torrent_id, attributes
            )
            if action is None:
                next_refresh = max(
                    next_refresh,
                    time.monotonic() + 0.25,
                )
            if action:
                try:
                    result = execute_tui_action(
                        qbit, selected, action, transmission
                    )
                    notice = f"{result} // #{torrent_id} {selected.get('name') or '(unnamed)'}"
                except Exception as action_error:
                    notice = f"ERROR // {action_error}"
                notice_until = time.monotonic() + 4
                next_refresh = 0.0


def run_tui(
    screen: curses.window,
    qbit: QBittorrent,
    transmission: Transmission | None,
    ids: TorrentIds,
    active_only: bool,
    interval: float,
) -> None:
    try:
        curses.mousemask(curses.ALL_MOUSE_EVENTS)
        curses.mouseinterval(150)
    except curses.error:
        pass
    try:
        _run_tui(screen, qbit, transmission, ids, active_only, interval)
    finally:
        try:
            curses.mousemask(0)
        except curses.error:
            pass


def command_list(
    qbit: QBittorrent,
    transmission: Transmission | None,
    ids: TorrentIds,
    args: argparse.Namespace,
) -> None:
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if interactive and not args.plain:
        interval = max(0.5, args.watch if args.watch is not None else 2.0)
        try:
            curses.wrapper(
                run_tui,
                qbit,
                transmission,
                ids,
                args.active,
                interval,
            )
        except KeyboardInterrupt:
            pass
        return
    if args.watch is None:
        show_list(qbit, transmission, ids, args.active)
        return
    interval = max(0.5, args.watch)
    try:
        while True:
            print("\033[2J\033[H", end="")
            try:
                show_list(qbit, transmission, ids, args.active)
            except Exception as error:
                print(f"Unable to read qBittorrent: {error}", file=sys.stderr)
            print(f"\nRefreshing every {interval:g}s — Ctrl+C to exit")
            time.sleep(interval)
    except KeyboardInterrupt:
        pass


def command_stop(qbit: QBittorrent) -> None:
    count = len(qbit.torrents())
    qbit.stop("all")
    print(f"Stopped {count} torrent(s).")


def command_start(qbit: QBittorrent) -> None:
    qbit.configure_queue()
    torrents = qbit.torrents()
    complete = [
        str(torrent["hash"])
        for torrent in torrents
        if float(torrent.get("progress") or 0) >= 1 and torrent.get("hash")
    ]
    incomplete = [
        str(torrent["hash"])
        for torrent in torrents
        if float(torrent.get("progress") or 0) < 1 and torrent.get("hash")
    ]
    if complete:
        qbit.stop("|".join(complete))
    if incomplete:
        qbit.start("|".join(incomplete))
    print(
        f"Started queue with {len(incomplete)} incomplete torrent(s); "
        "completed torrents remain stopped."
    )


def command_remove(
    qbit: QBittorrent,
    ids: TorrentIds,
    torrent_id: int,
    assume_yes: bool = False,
) -> None:
    if torrent_id < 1:
        raise RuntimeError("Torrent number must be a positive integer")
    torrents = qbit.torrents()
    torrent = ids.current_by_id(torrents, torrent_id)
    if torrent is None:
        raise RuntimeError(f"Torrent #{torrent_id} does not exist; run 'dlm list'")
    name = str(torrent.get("name") or "(unnamed)")
    if not assume_yes:
        answer = input(
            f'Delete #{torrent_id} "{name}" and all its downloaded files '
            "from Beast? [y/N] "
        )
        if answer.strip().casefold() not in {"y", "yes"}:
            print("Cancelled. Nothing was deleted.")
            return
    qbit.remove_with_files(str(torrent["hash"]))
    print(
        f"Removed #{torrent_id}: {name}\n"
        "The torrent and its downloaded files were deleted from Beast."
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="dlm",
        description="List and control Beast's qBittorrent download queue.",
    )
    commands = result.add_subparsers(dest="command")

    list_parser = commands.add_parser("list", help="list torrents and progress")
    list_parser.add_argument(
        "-w",
        "--watch",
        nargs="?",
        const=2.0,
        type=float,
        metavar="SECONDS",
        help="set the dashboard refresh interval (default: 2 seconds)",
    )
    list_parser.add_argument(
        "-a",
        "--active",
        action="store_true",
        help="hide queued, paused, and stopped jobs",
    )
    list_parser.add_argument(
        "--plain",
        action="store_true",
        help="print without opening the full-screen interface",
    )
    commands.add_parser("stop", help="stop all torrents")
    commands.add_parser("start", help="restart the single-download queue")
    remove_parser = commands.add_parser(
        "remove", help="delete a torrent and its downloaded files from Beast"
    )
    remove_parser.add_argument("torrent_number", type=int, metavar="NUMBER")
    remove_parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="skip the destructive-action confirmation prompt",
    )
    return result


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    args = parser().parse_args(argv)
    if args.command is None:
        args.command = "list"
        args.watch = None
        args.active = False
        args.plain = False
    return args


def main(argv: list[str] | None = None) -> None:
    args = parse_arguments(argv)
    try:
        qbit = QBittorrent()
        qbit.login()
        transmission = Transmission() if TRANSMISSION_URL else None
        ids = TorrentIds()
        if args.command == "list":
            command_list(qbit, transmission, ids, args)
        elif args.command == "stop":
            command_stop(qbit)
        elif args.command == "start":
            command_start(qbit)
        elif args.command == "remove":
            command_remove(qbit, ids, args.torrent_number, args.yes)
    except Exception as error:
        print(f"dlm: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
