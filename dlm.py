#!/usr/bin/env python3
"""DLM: manage Beast's qBittorrent download queue from the terminal."""

from __future__ import annotations

import argparse
import curses
from datetime import datetime
import http.cookiejar
import json
import os
from pathlib import Path
import shutil
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request


BASE_URL = os.environ.get("DLM_QBITTORRENT_URL", "http://192.168.1.27:8080").rstrip(
    "/"
)
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
ANSI_RESET = "\033[0m"
ANSI_BOLD_CYAN = "\033[1;36m"
ANSI_DIM = "\033[2m"
ANSI_BLUE = "\033[94m"
ANSI_GREEN = "\033[92m"
ANSI_CYAN = "\033[96m"
ANSI_MAGENTA = "\033[95m"
ANSI_YELLOW = "\033[93m"
ANSI_WHITE = "\033[97m"
TUI_MIN_WIDTH = 74
TUI_MIN_HEIGHT = 12
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
        for torrent in torrents:
            torrent_hash = str(torrent.get("hash") or "").casefold()
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
        self.post(
            "/api/v2/app/setPreferences",
            json=json.dumps(QUEUE_PREFERENCES, separators=(",", ":")),
        )

    def remove_with_files(self, torrent_hash: str) -> None:
        self.post(
            "/api/v2/torrents/delete",
            hashes=torrent_hash,
            deleteFiles="true",
        )


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
    name_width = max(20, width - prefix_width)
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
        name = str(torrent.get("name") or "")
        wrapped_name = textwrap.wrap(
            name,
            width=name_width,
            # Dot-separated release names can be wider than the terminal and
            # otherwise get wrapped by the terminal itself back at column 0.
            break_long_words=True,
            break_on_hyphens=False,
        ) or [""]
        progress = float(torrent.get("progress") or 0)
        down_speed = int(torrent.get("dlspeed") or 0)
        up_speed = int(torrent.get("upspeed") or 0)
        eta = format_eta(torrent.get("eta"))
        row_prefix = "".join(
            (
                styled(f"{torrent_id:>{id_width}}", ANSI_BLUE, color),
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
        rows.append(
            f"{row_prefix}{styled(wrapped_name[0], ANSI_WHITE, color)}"
        )
        rows.extend(
            f"{'':{prefix_width}}{styled(line, ANSI_WHITE, color)}"
            for line in wrapped_name[1:]
        )
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


def filtered_torrents(qbit: QBittorrent, active_only: bool) -> list[dict]:
    torrents = qbit.torrents()
    torrents.sort(key=initial_order)
    if active_only:
        torrents = [
            torrent
            for torrent in torrents
            if str(torrent.get("state") or "") not in IDLE_STATES
        ]
    return torrents


def show_list(qbit: QBittorrent, ids: TorrentIds, active_only: bool) -> None:
    torrents, id_map = list_torrents(qbit, ids, active_only)
    print(
        styled(
            f"DLM — {datetime.now().astimezone():%Y-%m-%d %H:%M:%S %Z}",
            ANSI_BOLD_CYAN,
            terminal_supports_color(),
        )
    )
    print(torrent_table(torrents, id_map))


def list_torrents(
    qbit: QBittorrent, ids: TorrentIds, active_only: bool
) -> tuple[list[dict], dict[str, int]]:
    torrents = filtered_torrents(qbit, active_only)
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
    prefix_width = id_width + 51
    name_width = max(10, width - prefix_width)
    rows: list[dict | None] = []
    for index, torrent in enumerate(torrents):
        torrent_id = ids[str(torrent.get("hash") or "").casefold()]
        wrapped_name = textwrap.wrap(
            str(torrent.get("name") or ""),
            width=name_width,
            break_long_words=True,
            break_on_hyphens=False,
        ) or [""]
        common = {
            "id": f"{torrent_id:>{id_width}}",
            "done": f"{float(torrent.get('progress') or 0) * 100:6.2f}%",
            "total": f"{format_bytes(torrent.get('size') or 0):>10}",
            "down": f"{format_rate(torrent.get('dlspeed') or 0):>10}",
            "up": f"{format_rate(torrent.get('upspeed') or 0):>10}",
            "eta": f"{format_eta(torrent.get('eta')):>8}",
            "active": bool(
                int(torrent.get("dlspeed") or 0) + int(torrent.get("upspeed") or 0)
            ),
        }
        rows.append({**common, "name": wrapped_name[0], "continuation": False})
        rows.extend(
            {
                **common,
                "name": line,
                "continuation": True,
            }
            for line in wrapped_name[1:]
        )
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
        "id": curses.COLOR_BLUE,
        "done": curses.COLOR_GREEN,
        "total": curses.COLOR_CYAN,
        "down": curses.COLOR_GREEN,
        "up": curses.COLOR_MAGENTA,
        "eta": curses.COLOR_YELLOW,
        "name": curses.COLOR_WHITE,
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
    return (
        f"TORRENTS {len(torrents)}  //  ACTIVE {active}  //  DONE {progress:5.1f}%"
        f"  //  DOWN {format_rate(down)}  //  UP {format_rate(up)}"
    )


def draw_tui(
    screen: curses.window,
    torrents: list[dict],
    ids: dict[str, int],
    scroll: int,
    interval: float,
    attributes: dict[str, int],
    error: str | None,
) -> tuple[int, int]:
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
        return 0, 1

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
    tui_addstr(
        screen,
        2,
        inner_x,
        tui_stats(torrents),
        attributes["footer"],
        inner_width,
    )
    tui_hline(screen, 3, attributes["border"])

    rows, id_width, prefix_width = tui_rows(torrents, ids, inner_width)
    header = (
        f"{'#':>{id_width}} {'DONE':>7} {'TOTAL':>10} {'DOWN':>10} "
        f"{'UP':>10} {'ETA':>8} NAME"
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
        if bool(row["continuation"]):
            tui_addstr(
                screen,
                y,
                inner_x + prefix_width,
                str(row["name"]),
                attributes["name"],
                inner_width - prefix_width,
            )
            continue
        x = inner_x
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
                attributes[key],
                field_width,
            )
            x += field_width + 1
        name_attribute = attributes["name"] | (
            curses.A_BOLD if bool(row["active"]) else 0
        )
        tui_addstr(
            screen,
            y,
            x,
            str(row["name"]),
            name_attribute,
            inner_width - prefix_width,
        )

    tui_hline(screen, height - 3, attributes["border"])
    if error:
        status = f"ERROR // {error}  //  [R] RETRY  [Q] QUIT"
        status_attribute = attributes["error"]
    else:
        status = (
            f"[Q] QUIT  [R] REFRESH  [J/K or arrows] SCROLL  "
            f"[PGUP/PGDN] PAGE  //  AUTO {interval:g}s"
        )
        status_attribute = attributes["footer"]
    first_visible = scroll + 1 if rows else 0
    position = (
        f"{first_visible}-{min(len(rows), scroll + content_height)} / {len(rows)}"
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
    return max_scroll, content_height


def run_tui(
    screen: curses.window,
    qbit: QBittorrent,
    ids: TorrentIds,
    active_only: bool,
    interval: float,
) -> None:
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.keypad(True)
    screen.timeout(100)
    attributes = tui_attributes()
    torrents: list[dict] = []
    id_map: dict[str, int] = {}
    error: str | None = None
    scroll = 0
    next_refresh = 0.0

    while True:
        now = time.monotonic()
        if now >= next_refresh:
            try:
                torrents, id_map = list_torrents(qbit, ids, active_only)
                error = None
            except Exception as refresh_error:
                error = str(refresh_error)
            next_refresh = now + interval

        max_scroll, page_size = draw_tui(
            screen,
            torrents,
            id_map,
            scroll,
            interval,
            attributes,
            error,
        )
        scroll = min(scroll, max_scroll)
        key = screen.getch()
        if key in {ord("q"), ord("Q")}:
            return
        if key in {ord("r"), ord("R")}:
            next_refresh = 0.0
        elif key in {curses.KEY_DOWN, ord("j"), ord("J")}:
            scroll = min(max_scroll, scroll + 1)
        elif key in {curses.KEY_UP, ord("k"), ord("K")}:
            scroll = max(0, scroll - 1)
        elif key in {curses.KEY_NPAGE, ord(" ")}:
            scroll = min(max_scroll, scroll + page_size)
        elif key == curses.KEY_PPAGE:
            scroll = max(0, scroll - page_size)
        elif key == curses.KEY_HOME:
            scroll = 0
        elif key == curses.KEY_END:
            scroll = max_scroll


def command_list(qbit: QBittorrent, ids: TorrentIds, args: argparse.Namespace) -> None:
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if interactive and not args.plain:
        interval = max(0.5, args.watch if args.watch is not None else 2.0)
        try:
            curses.wrapper(run_tui, qbit, ids, args.active, interval)
        except KeyboardInterrupt:
            pass
        return
    if args.watch is None:
        show_list(qbit, ids, args.active)
        return
    interval = max(0.5, args.watch)
    try:
        while True:
            print("\033[2J\033[H", end="")
            try:
                show_list(qbit, ids, args.active)
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
    commands = result.add_subparsers(dest="command", required=True)

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


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    try:
        qbit = QBittorrent()
        qbit.login()
        ids = TorrentIds()
        if args.command == "list":
            command_list(qbit, ids, args)
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
