# IRIS

IRIS is a full-screen terminal music TUI powered by [Verome API](https://github.com/Kirazul/Verome-API). It can search YouTube Music, fetch stream URLs, play audio through a local player, show lyrics, generate radio mixes, list trending music, and render a hot pink terminal visualizer while music plays.

## Requirements

- Python 3.10+
- One audio player installed: `mpv`, `ffplay` from FFmpeg, or `vlc`
- Python packages from `requirements.txt` (`textual`, `rich`, and `yt-dlp`)

Install the UI dependencies:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

## Usage

```bash
.venv/bin/python iris.py
.venv/bin/python iris.py tui
.venv/bin/python iris.py shell
.venv/bin/python iris.py search "Blinding Lights"
.venv/bin/python iris.py play "Blinding Lights" --first
.venv/bin/python iris.py lyrics "Blinding Lights" --first
.venv/bin/python iris.py radio "Blinding Lights" --first
.venv/bin/python iris.py trending US
.venv/bin/python iris.py recent
.venv/bin/python iris.py favorites
.venv/bin/python iris.py favorites --add "Blinding Lights" --first
```

## TUI Keybindings

The default `tui` mode shows the IRIS TUI, a full-screen Textual interface inspired by terminal music apps like `spotatui`.
Search results and stream URLs are cached during the session, and the app prefetches audio for the first visible tracks so playback starts faster after results load.
While music plays, the fuchsia visualizer panel shows an animated spectrum plus a progress bar. With `mpv`, IRIS reads the real player position and duration; other players fall back to an estimated timer.

```text
/        focus search
Enter    play selected track
p        play selected track
Space    pause/resume current song
n        next song in the current list
b        previous song in the current list
f        add/remove selected song from favourites
g        show recently played songs
v        show favourite songs
t        load US trending music
r        load radio from selected track
l        load lyrics for selected track
Up/Down  move selection
?        toggle help
q        quit
```

The older prompt shell is still available with `.venv/bin/python iris.py shell`.

Inside the shell:

```text
search <song>
play <song>
lyrics <song>
radio <song>
trending [country]
recent
favorites
favorite <song>
help
clear
quit
```

The legacy interactive shell uses ANSI colors, bordered panels, and a dashboard-style home screen when your terminal supports it. Set `NO_COLOR=1` to disable colors.

## Configuration

Use a different Verome API host:

```bash
VEROME_API=http://localhost:8000 python3 iris.py search "Numb"
```

Use a custom player command:

```bash
IRIS_PLAYER="mpv --no-video" python3 iris.py play "Numb" --first
```

Pass extra options to `yt-dlp`, for example if YouTube asks for sign-in cookies:

```bash
IRIS_YTDLP_ARGS="--cookies /path/to/cookies.txt" python3 iris.py play "Numb" --first
```

Playback prefers `yt-dlp` direct audio URLs for reliability. If YouTube blocks anonymous extraction, IRIS automatically tries browser cookies from Chrome/Brave before falling back to Verome's `/api/stream?id=` endpoint. You can still pass a cookies file manually with `IRIS_YTDLP_ARGS`.

Recently played and favourite songs are saved to `~/.local/share/iris/library.json` unless `XDG_DATA_HOME` is set. Existing `~/.local/share/climusic/library.json` data is still read if the new IRIS library has not been created yet.
