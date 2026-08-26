import curses
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from dlm import (
    QBittorrent,
    TUI_ACTIONS,
    TorrentIds,
    command_remove,
    ensure_selected_visible,
    execute_tui_action,
    filter_torrents,
    navigation_selection,
    parse_arguments,
    torrent_table,
    tui_rows,
    tui_stats,
)


class DlmTests(unittest.TestCase):
    def test_ids_are_persistent_and_new_ids_are_not_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            ids = TorrentIds(path)
            first = ids.sync([{"hash": "AAA"}, {"hash": "BBB"}])
            self.assertEqual(first, {"aaa": 1, "bbb": 2})

            reloaded = TorrentIds(path)
            second = reloaded.sync([{"hash": "BBB"}, {"hash": "CCC"}])
            self.assertEqual(second["bbb"], 2)
            self.assertEqual(second["ccc"], 3)

    def test_table_has_numbers_and_readable_wrapped_names(self):
        name = "A long torrent name which needs to wrap cleanly without truncation"
        table = torrent_table(
            [
                {
                    "hash": "abc",
                    "name": name,
                    "progress": 0.125,
                    "size": 1024,
                    "completed": 128,
                }
            ],
            {"abc": 7},
            terminal_width=76,
        )
        self.assertIn("#", table)
        self.assertIn("12.50%", table)
        self.assertNotIn("HASH", table)
        self.assertNotIn("…", table)
        for word in name.split():
            self.assertIn(word, table)

    def test_long_unbroken_name_wraps_inside_name_column(self):
        name = "Cape.Fear.S01E10.The.Executioners.2160p." * 3
        table = torrent_table(
            [
                {
                    "hash": "abc",
                    "name": name,
                    "progress": 0,
                    "size": 0,
                }
            ],
            {"abc": 20},
            terminal_width=76,
            use_color=False,
        )
        lines = table.splitlines()
        name_lines = lines[2:-2]
        self.assertGreater(len(name_lines), 1)
        self.assertTrue(all(len(line) <= 76 for line in name_lines))
        self.assertTrue(all(line.startswith(" " * 53) for line in name_lines[1:]))

    def test_torrents_have_a_blank_line_between_them(self):
        torrents = [
            {"hash": "a", "name": "First", "progress": 0, "size": 0},
            {"hash": "b", "name": "Second", "progress": 0, "size": 0},
        ]
        table = torrent_table(
            torrents,
            {"a": 1, "b": 2},
            terminal_width=100,
            use_color=False,
        )
        self.assertIn("First\n\n", table)

    def test_colors_can_be_enabled(self):
        table = torrent_table([], {}, terminal_width=76, use_color=True)
        self.assertIn("\033[1;36m", table)
        self.assertIn("\033[0m", table)

    def test_tui_rows_wrap_long_names_and_add_spacing(self):
        torrents = [
            {
                "hash": "a",
                "name": "Cape.Fear.S01E10.The.Executioners.2160p." * 3,
                "progress": 0.25,
                "size": 1024,
            },
            {"hash": "b", "name": "Second", "progress": 0, "size": 0},
        ]
        rows, _, prefix_width = tui_rows(torrents, {"a": 20, "b": 21}, 76)
        continuation_rows = [
            row for row in rows if row and bool(row["continuation"])
        ]
        self.assertTrue(continuation_rows)
        self.assertTrue(
            all(
                len(str(row["name"])) <= 76 - prefix_width
                for row in continuation_rows
            )
        )
        self.assertIn(None, rows)
        self.assertEqual(rows[0]["torrent_index"], 0)

    def test_selected_torrent_is_scrolled_into_view(self):
        torrents = [
            {"hash": str(index), "name": f"Torrent {index}", "size": 0}
            for index in range(4)
        ]
        rows, _, _ = tui_rows(
            torrents,
            {str(index): index + 1 for index in range(4)},
            100,
        )
        scroll = ensure_selected_visible(
            rows,
            selected_index=2,
            scroll=0,
            page_size=3,
        )
        self.assertEqual(scroll, 2)

    def test_no_subcommand_defaults_to_full_torrent_list(self):
        args = parse_arguments([])
        self.assertEqual(args.command, "list")
        self.assertFalse(args.active)
        self.assertFalse(args.plain)
        self.assertIsNone(args.watch)

    def test_tui_actions_map_to_qbittorrent_operations(self):
        self.assertEqual(
            TUI_ACTIONS,
            (
                ("START / RESUME", "start"),
                ("PAUSE / STOP", "stop"),
                ("DELETE + DATA", "delete"),
            ),
        )
        torrent = {"hash": "abc"}

        start_client = Mock()
        self.assertEqual(execute_tui_action(start_client, torrent, "start"), "STARTED")
        start_client.configure_queue.assert_called_once_with()
        start_client.start.assert_called_once_with("abc")

        stop_client = Mock()
        self.assertEqual(
            execute_tui_action(stop_client, torrent, "stop"),
            "PAUSED / STOPPED",
        )
        stop_client.stop.assert_called_once_with("abc")

        delete_client = Mock()
        self.assertEqual(
            execute_tui_action(delete_client, torrent, "delete"),
            "DELETED TORRENT + DATA",
        )
        delete_client.remove_with_files.assert_called_once_with("abc")

    def test_tui_stats_summarize_the_queue(self):
        stats = tui_stats(
            [
                {
                    "size": 100,
                    "completed": 50,
                    "dlspeed": 1024,
                    "upspeed": 0,
                }
            ]
        )
        self.assertIn("TORRENTS 1", stats)
        self.assertIn("ACTIVE 1", stats)
        self.assertIn("DONE  50.0%", stats)
        self.assertIn("DOWN 1KiB/s", stats)

    def test_search_requires_every_term_in_the_torrent_name(self):
        torrents = [
            {"name": "Cape Fear S01E01 2160p"},
            {"name": "Cape Fear S01E02 1080p"},
            {"name": "Slow Horses S01E01 2160p"},
        ]
        matches = filter_torrents(torrents, "fear 2160P")
        self.assertEqual(matches, [torrents[0]])
        self.assertEqual(filter_torrents(torrents, ""), torrents)

    def test_held_arrow_keys_remain_clamped_without_invalid_selection(self):
        torrents = [
            {"hash": str(index), "name": f"Torrent {index}", "size": 0}
            for index in range(10)
        ]
        rows, _, _ = tui_rows(
            torrents,
            {str(index): index + 1 for index in range(10)},
            100,
        )
        selected = 0
        for _ in range(1_000):
            selected = navigation_selection(
                curses.KEY_DOWN, rows, selected, 5, len(torrents)
            )
        self.assertEqual(selected, 9)
        for _ in range(1_000):
            selected = navigation_selection(
                curses.KEY_UP, rows, selected, 5, len(torrents)
            )
        self.assertEqual(selected, 0)

    def test_page_home_top_and_end_navigation(self):
        torrents = [
            {"hash": str(index), "name": f"Torrent {index}", "size": 0}
            for index in range(10)
        ]
        rows, _, _ = tui_rows(
            torrents,
            {str(index): index + 1 for index in range(10)},
            100,
        )
        page_down = navigation_selection(
            curses.KEY_NPAGE, rows, 0, 5, len(torrents)
        )
        self.assertGreater(page_down, 0)
        self.assertEqual(
            navigation_selection(
                curses.KEY_PPAGE, rows, page_down, 5, len(torrents)
            ),
            0,
        )
        self.assertEqual(
            navigation_selection(curses.KEY_END, rows, 0, 5, len(torrents)),
            9,
        )
        self.assertEqual(
            navigation_selection(curses.KEY_HOME, rows, 9, 5, len(torrents)),
            0,
        )
        self.assertEqual(navigation_selection(ord("g"), rows, 9, 5, 10), 0)
        self.assertEqual(navigation_selection(ord("G"), rows, 0, 5, 10), 9)
        self.assertEqual(
            navigation_selection(curses.KEY_MOUSE, rows, 4, 5, 10),
            4,
        )

    def test_remove_explicitly_deletes_downloaded_files(self):
        qbit = QBittorrent.__new__(QBittorrent)
        qbit.post = Mock()
        qbit.remove_with_files("hash")
        qbit.post.assert_called_once_with(
            "/api/v2/torrents/delete",
            hashes="hash",
            deleteFiles="true",
        )

    def test_remove_command_deletes_selected_torrent_and_files(self):
        qbit = Mock()
        qbit.torrents.return_value = [
            {"hash": "abc", "name": "Example", "progress": 0.5}
        ]
        ids = Mock()
        ids.current_by_id.return_value = qbit.torrents.return_value[0]

        command_remove(qbit, ids, 7, assume_yes=True)

        qbit.remove_with_files.assert_called_once_with("abc")


if __name__ == "__main__":
    unittest.main()
