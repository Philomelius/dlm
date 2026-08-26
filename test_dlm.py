from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from dlm import QBittorrent, TorrentIds, command_remove, torrent_table


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
