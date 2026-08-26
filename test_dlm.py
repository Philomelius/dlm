from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from dlm import (
    QBittorrent,
    TorrentIds,
    command_remove,
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
