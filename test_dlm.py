import curses
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, call

from dlm import (
    ArrowTapTracker,
    QBittorrent,
    TUI_CTRL_DOWN,
    TUI_CTRL_UP,
    TUI_ESCAPE_DELAY_MS,
    TUI_SHIFT_DOWN,
    TUI_SHIFT_UP,
    TUI_INPUT_TIMEOUT_MS,
    TUI_ACTIONS,
    Transmission,
    TorrentIds,
    add_magnet_to_queue,
    command_remove,
    decode_tui_key,
    ensure_selected_visible,
    execute_tui_action,
    execute_tui_actions,
    extend_torrent_selection,
    filter_torrents,
    filtered_torrents,
    header_sort_key,
    known_seed_count,
    marquee_name,
    navigation_selection,
    next_sort_order,
    parse_arguments,
    parse_active_limit,
    parse_download_limit,
    pending_key,
    source_badge,
    sort_header_label,
    sort_torrents,
    stats_control_key,
    torrent_table,
    truncate_name,
    tui_rows,
    tui_stats,
    tui_stats_line,
    validate_magnet_uri,
    visible_torrent_name,
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
        self.assertIn("SEEDS", table.splitlines()[0])
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
                "seeds": 13,
            },
            {"hash": "b", "name": "Second", "progress": 0, "size": 0},
        ]
        rows, _, prefix_width = tui_rows(torrents, {"a": 20, "b": 21}, 76)
        self.assertEqual(len(rows), 3)
        self.assertIsNone(rows[1])
        self.assertEqual(rows[0]["torrent_index"], 0)
        self.assertEqual(rows[2]["torrent_index"], 1)
        self.assertEqual(rows[0]["name"], torrents[0]["name"])
        self.assertEqual(rows[0]["hash"], "a")
        self.assertEqual(rows[0]["seeds"], "     13")
        self.assertEqual(prefix_width, 63)

    def test_seed_counts_ignore_unknown_values_and_use_the_highest_report(self):
        self.assertEqual(known_seed_count(-1, None, "8", 3), 8)
        self.assertEqual(known_seed_count(-1, None), 0)

    def test_names_are_truncated_and_opt_in_marquee_moves_to_the_right(self):
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
        self.assertEqual(
            visible_torrent_name(name, 4, True, False, 10),
            "ABC…",
        )
        self.assertEqual(
            visible_torrent_name(name, 4, True, True, 0.25),
            "CDEF",
        )
        self.assertEqual(
            visible_torrent_name(name, 4, False, True, 0.25),
            "ABC…",
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

    def test_tui_actions_map_to_transmission_rpc_operations(self):
        qbit = Mock()
        transmission = Mock()
        torrent = {
            "hash": "transmission:abc",
            "source": "transmission",
            "source_hash": "abc",
            "source_id": 17,
        }

        self.assertEqual(
            execute_tui_action(qbit, torrent, "start", transmission),
            "STARTED",
        )
        transmission.start.assert_called_once_with(17)

        self.assertEqual(
            execute_tui_action(qbit, torrent, "stop", transmission),
            "PAUSED / STOPPED",
        )
        transmission.stop.assert_called_once_with(17)

        self.assertEqual(
            execute_tui_action(qbit, torrent, "delete", transmission),
            "DELETED TORRENT + DATA",
        )
        transmission.remove_with_files.assert_called_once_with(17)
        qbit.assert_not_called()

    def test_batch_actions_group_torrents_by_client(self):
        qbit = Mock()
        transmission = Mock()
        torrents = [
            {"hash": "qbit-a"},
            {
                "hash": "transmission:abc",
                "source": "transmission",
                "source_id": 17,
            },
            {"hash": "qbit-b"},
        ]

        self.assertEqual(
            execute_tui_actions(qbit, torrents, "start", transmission),
            "STARTED 3 TORRENTS",
        )
        qbit.configure_queue.assert_called_once_with()
        qbit.start.assert_called_once_with("qbit-a|qbit-b")
        transmission.start.assert_called_once_with([17])

        qbit.reset_mock()
        transmission.reset_mock()
        self.assertEqual(
            execute_tui_actions(qbit, torrents, "stop", transmission),
            "PAUSED / STOPPED 3 TORRENTS",
        )
        qbit.stop.assert_called_once_with("qbit-a|qbit-b")
        transmission.stop.assert_called_once_with([17])

        qbit.reset_mock()
        transmission.reset_mock()
        self.assertEqual(
            execute_tui_actions(qbit, torrents, "delete", transmission),
            "DELETED 3 TORRENTS + DATA",
        )
        qbit.remove_with_files.assert_called_once_with("qbit-a|qbit-b")
        transmission.remove_with_files.assert_called_once_with([17])

    def test_batch_action_validation_happens_before_any_client_is_changed(self):
        qbit = Mock()
        torrents = [
            {"hash": "qbit-a"},
            {"hash": "transmission:abc", "source": "transmission"},
        ]

        with self.assertRaisesRegex(RuntimeError, "Transmission RPC"):
            execute_tui_actions(qbit, torrents, "stop")
        qbit.assert_not_called()

    def test_transmission_actions_use_expected_remote_rpc_calls(self):
        transmission = Transmission("http://transmission.invalid/rpc")
        transmission.call = Mock(return_value={})

        transmission.start(7)
        transmission.call.assert_called_once_with("torrent-start", {"ids": [7]})

        transmission.call.reset_mock()
        transmission.stop("hash")
        transmission.call.assert_called_once_with(
            "torrent-stop", {"ids": ["hash"]}
        )

        transmission.call.reset_mock()
        transmission.remove_with_files(7)
        transmission.call.assert_called_once_with(
            "torrent-remove",
            {"ids": [7], "delete-local-data": True},
        )

        transmission.call.reset_mock()
        transmission.start([7, "hash"])
        transmission.call.assert_called_once_with(
            "torrent-start", {"ids": [7, "hash"]}
        )

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

    def test_status_line_clicks_target_active_and_down_controls(self):
        stats = tui_stats_line([], "active documentary", 93)
        inner_x = 2
        active_x = inner_x + stats.rfind("ACTIVE ")
        down_x = inner_x + stats.rfind("DOWN ")

        self.assertEqual(stats_control_key(active_x, inner_x, stats), "active")
        self.assertEqual(stats_control_key(down_x, inner_x, stats), "down")
        self.assertIsNone(stats_control_key(inner_x, inner_x, stats))

    def test_setting_values_are_validated_and_speed_uses_kibibytes(self):
        self.assertEqual(parse_active_limit("3"), 3)
        with self.assertRaises(ValueError):
            parse_active_limit("0")
        with self.assertRaises(ValueError):
            parse_active_limit("2.5")

        self.assertEqual(parse_download_limit("-1"), 0)
        self.assertEqual(parse_download_limit("500"), 500 * 1024)
        self.assertEqual(parse_download_limit("1.5M"), round(1.5 * 1024**2))
        self.assertEqual(parse_download_limit("2GiB/s"), 2 * 1024**3)
        with self.assertRaises(ValueError):
            parse_download_limit("0")

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
                        "peersSendingToUs": 3,
                        "trackerStats": [
                            {"seederCount": 12},
                            {"seederCount": 27},
                            {"seederCount": -1},
                        ],
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
        self.assertEqual(torrents[0]["seeds"], 27)
        self.assertEqual(torrents[0]["state"], "downloading")

    def test_transmission_is_optional_when_its_daemon_is_offline(self):
        transmission = Transmission("http://transmission.invalid/rpc")
        transmission.call = Mock(side_effect=OSError("offline"))

        self.assertEqual(transmission.torrents(), [])
        self.assertEqual(transmission.last_error, "offline")

    def test_qbittorrent_and_transmission_are_merged_and_badged(self):
        qbit = Mock()
        qbit.torrents.return_value = [
            {
                "hash": "q",
                "name": "Qbit job",
                "state": "stoppedDL",
                "num_complete": 18,
                "num_seeds": 2,
            }
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
        qbit_torrent = next(
            item for item in torrents if item["source"] == "qbittorrent"
        )
        self.assertEqual(qbit_torrent["seeds"], 18)

    def test_search_requires_every_term_in_the_torrent_name(self):
        torrents = [
            {"name": "Cape Fear S01E01 2160p"},
            {"name": "Cape Fear S01E02 1080p"},
            {"name": "Slow Horses S01E01 2160p"},
        ]
        matches = filter_torrents(torrents, "fear 2160P")
        self.assertEqual(matches, [torrents[0]])
        self.assertEqual(filter_torrents(torrents, ""), torrents)

    def test_click_sorting_toggles_high_low_then_low_high(self):
        self.assertEqual(next_sort_order(None, True, "done"), ("done", True))
        self.assertEqual(
            next_sort_order("done", True, "done"),
            ("done", False),
        )
        self.assertEqual(
            next_sort_order("done", False, "name"),
            ("name", True),
        )

    def test_sortable_headers_map_to_exact_click_regions(self):
        self.assertEqual(header_sort_key(4, 2, 2), "number")
        self.assertEqual(header_sort_key(7, 2, 2), "done")
        self.assertIsNone(header_sort_key(15, 2, 2))
        self.assertEqual(header_sort_key(26, 2, 2), "down")
        self.assertEqual(header_sort_key(37, 2, 2), "up")
        self.assertEqual(header_sort_key(48, 2, 2), "eta")
        self.assertEqual(header_sort_key(57, 2, 2), "seeds")
        self.assertEqual(header_sort_key(65, 2, 2), "name")

    def test_torrents_sort_in_both_directions_for_every_header(self):
        torrents = [
            {
                "hash": "a",
                "name": "Alpha",
                "progress": 0.2,
                "dlspeed": 30,
                "upspeed": 2,
                "eta": 30,
                "seeds": 9,
            },
            {
                "hash": "c",
                "name": "Charlie",
                "progress": 0.9,
                "dlspeed": 10,
                "upspeed": 3,
                "eta": -1,
                "seeds": 17,
            },
            {
                "hash": "b",
                "name": "Bravo",
                "progress": 0.5,
                "dlspeed": 20,
                "upspeed": 1,
                "eta": 10,
                "seeds": 2,
            },
        ]
        ids = {"a": 12, "b": 42, "c": 3}

        self.assertEqual(
            [item["name"] for item in sort_torrents(torrents, "number", True, ids)],
            ["Bravo", "Alpha", "Charlie"],
        )
        self.assertEqual(
            [item["name"] for item in sort_torrents(torrents, "number", False, ids)],
            ["Charlie", "Alpha", "Bravo"],
        )
        self.assertEqual(
            [item["name"] for item in sort_torrents(torrents, "name", True)],
            ["Charlie", "Bravo", "Alpha"],
        )
        self.assertEqual(
            [item["name"] for item in sort_torrents(torrents, "done", False)],
            ["Alpha", "Bravo", "Charlie"],
        )
        self.assertEqual(
            [item["name"] for item in sort_torrents(torrents, "down", True)],
            ["Alpha", "Bravo", "Charlie"],
        )
        self.assertEqual(
            [item["name"] for item in sort_torrents(torrents, "up", True)],
            ["Charlie", "Alpha", "Bravo"],
        )
        self.assertEqual(
            [item["name"] for item in sort_torrents(torrents, "seeds", True)],
            ["Charlie", "Alpha", "Bravo"],
        )
        self.assertEqual(
            [item["name"] for item in sort_torrents(torrents, "seeds", False)],
            ["Bravo", "Alpha", "Charlie"],
        )
        self.assertEqual(
            [item["name"] for item in sort_torrents(torrents, "eta", True)],
            ["Alpha", "Bravo", "Charlie"],
        )
        self.assertEqual(
            [item["name"] for item in sort_torrents(torrents, "eta", False)],
            ["Bravo", "Alpha", "Charlie"],
        )

    def test_active_sort_header_shows_its_direction(self):
        self.assertEqual(
            sort_header_label("#", "number", 2, "number", True),
            "#▼",
        )
        self.assertEqual(
            sort_header_label("DONE", "done", 7, "done", True),
            " DONE ▼",
        )
        self.assertEqual(
            sort_header_label("NAME", "name", None, "name", False),
            "NAME ▲",
        )

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
        self.assertEqual((TUI_CTRL_UP, TUI_CTRL_DOWN), (567, 526))
        self.assertEqual((TUI_SHIFT_UP, TUI_SHIFT_DOWN), (568, 527))
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
        self.assertEqual(
            navigation_selection(TUI_CTRL_UP, rows, 9, 5, 10),
            0,
        )
        self.assertEqual(
            navigation_selection(TUI_CTRL_DOWN, rows, 0, 5, 10),
            9,
        )

    def test_shift_arrows_extend_and_shrink_a_contiguous_selection(self):
        torrents = [
            {"hash": letter, "name": letter.upper()}
            for letter in ("a", "b", "c", "d")
        ]

        index, anchor, selected = extend_torrent_selection(
            torrents, 1, None, 1
        )
        self.assertEqual((index, anchor, selected), (2, "b", {"b", "c"}))

        index, anchor, selected = extend_torrent_selection(
            torrents, index, anchor, 1
        )
        self.assertEqual((index, anchor, selected), (3, "b", {"b", "c", "d"}))

        index, anchor, selected = extend_torrent_selection(
            torrents, index, anchor, -1
        )
        self.assertEqual((index, anchor, selected), (2, "b", {"b", "c"}))

        index, anchor, selected = extend_torrent_selection(
            torrents, index, anchor, -1
        )
        self.assertEqual((index, anchor, selected), (1, "b", {"b"}))

    def test_double_arrow_taps_move_by_a_page_without_single_tap_delay(self):
        torrents = [
            {"hash": str(index), "name": f"Torrent {index}", "size": 0}
            for index in range(20)
        ]
        rows, _, _ = tui_rows(
            torrents,
            {str(index): index + 1 for index in range(20)},
            100,
        )
        tracker = ArrowTapTracker(timeout=0.35)

        first_up = tracker.navigate(curses.KEY_UP, rows, 10, 5, 20, now=1.0)
        self.assertEqual(first_up, 9)
        second_up = tracker.navigate(
            curses.KEY_UP,
            rows,
            first_up,
            5,
            20,
            now=1.1,
        )
        self.assertEqual(second_up, 7)

        first_down = tracker.navigate(
            curses.KEY_DOWN,
            rows,
            second_up,
            5,
            20,
            now=2.0,
        )
        self.assertEqual(first_down, 8)
        second_down = tracker.navigate(
            curses.KEY_DOWN,
            rows,
            first_down,
            5,
            20,
            now=2.1,
        )
        self.assertEqual(second_down, 10)

    def test_slow_arrow_taps_remain_single_steps(self):
        torrents = [
            {"hash": str(index), "name": f"Torrent {index}", "size": 0}
            for index in range(10)
        ]
        rows, _, _ = tui_rows(
            torrents,
            {str(index): index + 1 for index in range(10)},
            100,
        )
        tracker = ArrowTapTracker(timeout=0.35)
        selected = tracker.navigate(curses.KEY_DOWN, rows, 2, 5, 10, now=1.0)
        selected = tracker.navigate(
            curses.KEY_DOWN,
            rows,
            selected,
            5,
            10,
            now=1.5,
        )
        self.assertEqual(selected, 4)

    def test_pending_input_poll_restores_fast_screen_timeout(self):
        screen = Mock()
        screen.getch.return_value = 13

        self.assertEqual(pending_key(screen), 13)
        self.assertEqual(
            screen.timeout.call_args_list,
            [call(0), call(TUI_INPUT_TIMEOUT_MS)],
        )

    def test_split_modified_arrow_sequences_are_reassembled(self):
        cases = (
            ("[1;5A", TUI_CTRL_UP),
            ("[1;5B", TUI_CTRL_DOWN),
            ("[1;2A", TUI_SHIFT_UP),
            ("[1;2B", TUI_SHIFT_DOWN),
        )
        for sequence, expected in cases:
            with self.subTest(sequence=sequence):
                screen = Mock()
                screen.getch.side_effect = [ord(value) for value in sequence]
                self.assertEqual(decode_tui_key(screen, 27), expected)
                self.assertEqual(
                    screen.timeout.call_args_list,
                    [
                        call(TUI_ESCAPE_DELAY_MS),
                        call(TUI_INPUT_TIMEOUT_MS),
                    ],
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

    def test_magnet_uri_validation_accepts_v1_and_v2_torrents(self):
        v1 = f"magnet:?xt=urn:btih:{'0' * 40}&dn=Example"
        v2 = f"magnet:?xt=urn:btmh:1220{'a' * 64}&dn=Example"

        self.assertEqual(validate_magnet_uri(f"  {v1}  "), v1)
        self.assertEqual(validate_magnet_uri(v2), v2)

    def test_magnet_uri_validation_rejects_invalid_input(self):
        for value in (
            "",
            "https://example.invalid/file.torrent",
            "magnet:?dn=Missing+Identifier",
            "magnet:?xt=urn:btih:",
            "magnet:?xt=urn:btih:0123456789abcdef",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_magnet_uri(value)

    def test_magnet_is_added_under_managed_queue_preferences(self):
        magnet_uri = f"magnet:?xt=urn:btih:{'0' * 40}&dn=Example"
        qbit = Mock()

        self.assertEqual(add_magnet_to_queue(qbit, magnet_uri), magnet_uri)
        qbit.configure_queue.assert_called_once_with()
        qbit.add_magnet.assert_called_once_with(magnet_uri)

    def test_qbittorrent_add_magnet_uses_web_api(self):
        magnet_uri = f"magnet:?xt=urn:btih:{'0' * 40}&dn=Example"
        qbit = QBittorrent.__new__(QBittorrent)
        qbit.post = Mock(return_value="Ok.")

        qbit.add_magnet(magnet_uri)

        qbit.post.assert_called_once_with(
            "/api/v2/torrents/add",
            urls=magnet_uri,
        )

        qbit.post.return_value = "Fails."
        with self.assertRaisesRegex(RuntimeError, "rejected"):
            qbit.add_magnet(magnet_uri)

    def test_clickable_limits_use_qbittorrent_preferences_and_transfer_api(self):
        qbit = QBittorrent.__new__(QBittorrent)
        qbit.post = Mock()

        qbit.set_max_active_downloads(4)
        path, fields = qbit.post.call_args.args[0], qbit.post.call_args.kwargs
        preferences = json.loads(fields["json"])
        self.assertEqual(path, "/api/v2/app/setPreferences")
        self.assertEqual(preferences["max_active_downloads"], 4)
        self.assertEqual(preferences["max_active_torrents"], -1)
        self.assertTrue(preferences["dont_count_slow_torrents"])

        qbit.post.reset_mock()
        qbit.set_download_limit(0)
        qbit.post.assert_called_once_with(
            "/api/v2/transfer/setDownloadLimit",
            limit="0",
        )

    def test_resuming_queue_does_not_reset_user_active_limit(self):
        qbit = QBittorrent.__new__(QBittorrent)
        qbit.post = Mock()
        qbit.configure_queue()

        fields = qbit.post.call_args.kwargs
        preferences = json.loads(fields["json"])
        self.assertNotIn("max_active_downloads", preferences)

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
