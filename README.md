# DLM

DLM is a dependency-free, full-screen terminal client for managing a
qBittorrent download queue. It provides readable live progress, persistent
numeric torrent IDs, confirmed destructive removal, and opinionated
one-download-at-a-time queue controls.

```text
 #    DONE      TOTAL       DOWN         UP      ETA NAME
------------------------------------------------------------------------
 1   4.21%    5.86GiB   585KiB/s       0B/s    2h40m Example release
 2  18.34%    4.68GiB       0B/s       0B/s       -- Stalled release
------------------------------------------------------------------------
     9.89%   10.54GiB   585KiB/s       0B/s       -- TOTAL (2)
```

## Features

- concise `DONE`, `TOTAL`, `DOWN`, `UP`, `ETA`, and `NAME` display;
- full-screen retro dashboard with a bordered layout and live queue totals;
- color-coded columns and spacing between torrent entries;
- highlighted torrent selection with arrow-key navigation;
- keyboard-only scrolling; mouse-wheel events are ignored;
- name search with live filtering;
- an Enter-key action menu for starting, pausing/stopping, or deleting the
  selected torrent;
- names use the remaining terminal width and wrap back into the `NAME` column,
  including long dot-separated release names;
- persistent numeric IDs, so commands never require torrent hashes;
- automatic refresh plus keyboard navigation and refresh controls;
- stop every torrent and restart the queue from the terminal;
- delete a qBittorrent job together with all of its downloaded files;
- Python standard library only—no runtime packages.

## Requirements

- Python 3.10 or newer;
- qBittorrent with its Web UI enabled;
- a credentials file containing:

```ini
username=your-qbittorrent-user
password=your-qbittorrent-password
```

Protect the credentials file with `chmod 600`.

## Installation

Clone the repository and run:

```sh
./install.sh
```

This installs `dlm` into `~/.local/bin`. On a typical Linux login shell that
directory is already in `PATH`. Otherwise add this to `~/.profile`:

```sh
export PATH="$HOME/.local/bin:$PATH"
```

The same installation can be performed manually:

```sh
install -Dm755 dlm.py "$HOME/.local/bin/dlm"
```

## Configuration

DLM reads these optional environment variables:

| Variable | Default on Beast | Purpose |
| --- | --- | --- |
| `DLM_QBITTORRENT_URL` | `http://192.168.1.27:8080` | qBittorrent Web UI base URL |
| `DLM_CREDENTIALS_FILE` | `/media/nicolas/beast22/appdata/media-stack/qbittorrent.credentials` | Credentials file |
| `DLM_STATE_FILE` | `~/.local/state/dlm/torrents.json` | Persistent numeric-ID state |

For another machine, configure the first two variables in your shell profile:

```sh
export DLM_QBITTORRENT_URL="http://127.0.0.1:8080"
export DLM_CREDENTIALS_FILE="$HOME/.config/dlm/qbittorrent.credentials"
```

Colors are enabled automatically on interactive terminals. Set `NO_COLOR=1`
to disable them.

## Commands

Open the full-screen torrent dashboard. The two commands are equivalent:

```sh
dlm
dlm list
```

The first torrent is highlighted automatically. Use the arrow keys or `j`/`k`
to select another torrent. Held arrow keys are clamped safely at the first and
last torrent. Mouse-wheel events are ignored, so scrolling is keyboard-only.

- Page Up/Page Down move by one visible page;
- Home or `g` selects the first torrent;
- End or `G` selects the final torrent;
- `r` refreshes immediately.

Press `s` to search torrent names. Every space-separated search word must
appear in the name. Press Enter to apply the search. Esc cancels search entry;
once a search is active, Esc clears it and restores the full list.

Press Enter on the selected torrent to open its action menu:

- `START / RESUME` starts the selected torrent under DLM's single-download
  queue settings;
- `PAUSE / STOP` stops the selected torrent;
- `DELETE + DATA` removes the torrent and its downloaded files after a second
  confirmation dialog.

Use the arrow keys and Enter inside a menu. Esc is the consistent cancel/clear
key for search, action menus, and delete confirmation. Press `q` to close DLM.
qBittorrent 5.2 has one stop operation rather than separate pause and stop
operations, so DLM labels that action `PAUSE / STOP`.

Choose another refresh interval:

```sh
dlm list --watch 5
```

Show only started, active, or stalled jobs:

```sh
dlm list --active
```

Print a traditional one-shot listing without opening the dashboard:

```sh
dlm list --plain
```

Stop all qBittorrent jobs:

```sh
dlm stop
```

Restart all incomplete jobs under the managed queue settings:

```sh
dlm start
```

Remove the job shown as `12` by `dlm list`:

```sh
dlm remove 12
```

`remove` displays the torrent name and asks for confirmation, then sends
`deleteFiles=true` to qBittorrent. This permanently deletes the job and its
downloaded files from the qBittorrent host. To skip the prompt deliberately:

```sh
dlm remove 12 --yes
```

## Queue behavior

`dlm start` applies the following qBittorrent preferences before restarting
incomplete jobs:

- queueing enabled;
- maximum active downloads: `1`;
- maximum active uploads: `1`;
- slow or stalled torrents do not count toward the download limit;
- overall active-torrent limit: unlimited, allowing the queue to advance past
  stalled jobs;
- share ratio limit: `0`, with the stop action.

Completed jobs remain stopped when the queue is restarted.

## Testing

Run the standard-library test suite:

```sh
python3 -m unittest -v test_dlm.py
```

## License

[MIT](LICENSE)
