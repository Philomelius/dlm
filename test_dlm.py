import curses
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from dlm import (
    QBittorrent,
    TUI_ACTIONS,
    Transmission,
    TorrentIds,
    command_remove,
    ensure_selected_visible,
    execute_tui_action,
    filter_torrents,
    filtered_torrents,
    marquee_name,
    navigation_selection,
    parse_arguments,
    source_badge,
    torrent_table,
    truncate_name,
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

    def test_existing_transmission_ids_migrate_to_source_qualified_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            ids = TorrentIds(path)
            ids.sync([{"hash": "ABC", "source": "transmission"}])

            mapping = ids.sync(
                [
                    {
                        "hash": "transmission:abc",
                        "source_hash": "abc",
                        "source": "transmission",
                    }
                ]
            )

            self.assertNotIn("abc", mapping)
            self.assertEqual(mapping["transmission:abc"], 1)

    def test_table_has_numbers_and_truncates_names_to_one_line(self):
        name = "A long torrent name which must remain on exactly one row"
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
        self.assertIn("…", table)
        torrent_rows = table.splitlines()[2:-2]
        self.assertEqual(len(torrent_rows), 1)
        self.assertLessEqual(len(torrent_rows[0]), 76)

    def test_long_unbroken_name_never_wraps_to_the_left_margin(self):
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
        self.assertEqual(len(name_lines), 1)
        self.assertLessEqual(len(name_lines[0]), 76)
        self.assertTrue(name_lines[0].endswith("…"))

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

    def test_tui_rows_keep_one_row_per_torrent_and_add_spacing(self):
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
        self.assertEqual(len(rows), 3)
        self.assertIsNone(rows[1])
        self.assertEqual(rows[0]["torrent_index"], 0)
        self.assertEqual(rows[2]["torrent_index"], 1)
        self.assertEqual(rows[0]["name"], torrents[0]["name"])
        self.assertEqual(prefix_width, 55)

    def test_names_are_truncated_and_selected_names_marquee_to_the_right(self):
        name = "ABCDEFGHIJ"
        self.assertEqual(truncate_name(name, 5), "ABCD…")
        self.assertEqual(truncate_name("short", 5), "short")
        self.assertEqual(marquee_name(name, 4, 0), "ABCD")
        self.assertEqual(
            marquee_name(name, 4, 3.1, step_time=1.0),
            "CDEF",
        )
        self.assertEqual(
            marquee_name(name, 4, 7.1, step_time=1.0),
            "GHIJ",
        )

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
        self.assertIn("QB 1 / TR 0", stats)
        self.assertIn("ACTIVE 1", stats)
        self.assertIn("DONE  50.0%", stats)
        self.assertIn("DOWN 1KiB/s", stats)

    def test_transmission_torrents_are_normalized_for_the_shared_list(self):
        transmission = Transmission("http://transmission.invalid/rpc")
        transmission.call = Mock(
            return_value={
                "torrents": [
                    {
                        "id": 7,
                        "name": "Legacy download",
                        "hashString": "ABCDEF",
                        "percentDone": 0.25,
                        "sizeWhenDone": 2_000,
                        "totalSize": 1_000,
                        "haveValid": 500,
                        "rateDownload": 120,
                        "rateUpload": 30,
                        "eta": 60,
                        "status": 4,
                    }
                ]
            }
        )

        torrents = transmission.torrents()

        self.assertEqual(len(torrents), 1)
        self.assertEqual(torrents[0]["hash"], "transmission:abcdef")
        self.assertEqual(torrents[0]["source_hash"], "abcdef")
        self.assertEqual(torrents[0]["source"], "transmission")
        self.assertEqual(torrents[0]["source_id"], 7)
        self.assertEqual(torrents[0]["progress"], 0.25)
        self.assertEqual(torrents[0]["size"], 2_000)
        self.assertEqual(torrents[0]["state"], "downloading")

    def test_transmission_is_optional_when_its_daemon_is_offline(self):
        transmission = Transmission("http://transmission.invalid/rpc")
        transmission.call = Mock(side_effect=OSError("offline"))

        self.assertEqual(transmission.torrents(), [])
        self.assertEqual(transmission.last_error, "offline")

    def test_qbittorrent_and_transmission_are_merged_and_badged(self):
        qbit = Mock()
        qbit.torrents.return_value = [
            {"hash": "q", "name": "Qbit job", "state": "stoppedDL"}
        ]
        transmission = Mock()
        transmission.torrents.return_value = [
            {
                "hash": "t",
                "name": "Transmission job",
                "source": "transmission",
                "state": "stoppedDL",
            }
        ]

        torrents = filtered_torrents(qbit, False, transmission)

        self.assertEqual(len(torrents), 2)
        self.assertEqual({source_badge(item) for item in torrents}, {"QB", "TR"})

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
