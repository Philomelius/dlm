#!/usr/bin/env python3
"""DLM: manage Beast's qBittorrent download queue from the terminal."""

from __future__ import annotations

import argparse
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
    torrents = filtered_torrents(qbit, active_only)
    id_map = ids.sync(torrents)
    torrents.sort(
        key=lambda torrent: (
            initial_order(torrent),
            id_map[str(torrent.get("hash") or "").casefold()],
        )
    )
    print(
        styled(
            f"DLM — {datetime.now().astimezone():%Y-%m-%d %H:%M:%S %Z}",
            ANSI_BOLD_CYAN,
            terminal_supports_color(),
        )
    )
    print(torrent_table(torrents, id_map))


def command_list(qbit: QBittorrent, ids: TorrentIds, args: argparse.Namespace) -> None:
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
        help="refresh continuously (default: every 2 seconds)",
    )
    list_parser.add_argument(
        "-a",
        "--active",
        action="store_true",
        help="hide queued, paused, and stopped jobs",
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
