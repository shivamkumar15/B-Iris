#!/usr/bin/env python3
"""IRIS terminal music client powered by the Verome API."""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import json
import math
import os
import signal
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import re
import urllib.error
import urllib.parse
import urllib.request
from ytmusicapi import YTMusic
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

try:
    from gi.repository import GLib
    import pydbus
    HAS_MPRIS = True
except ImportError:
    HAS_MPRIS = False


DEFAULT_BASE_URL = "https://verome-api.deno.dev"
APP_NAME = "IRIS"
APP_SUBTITLE = "TUI"
PLAYER_ENV = "IRIS_PLAYER"
LEGACY_PLAYER_ENV = "CLIMUSIC_PLAYER"
YTDLP_ARGS_ENV = "IRIS_YTDLP_ARGS"
YTDLP_COOKIES_BROWSER_ENV = "IRIS_YTDLP_COOKIES_BROWSER"
PLAYER_LOG = os.path.join(tempfile.gettempdir(), "climusic-player.log")
PLAYER_IPC_SOCKETS: dict[int, str] = {}
DATA_HOME = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
LIBRARY_FILE = os.path.join(DATA_HOME, "iris", "library.json")
CACHE_DIR = os.path.join(tempfile.gettempdir(), "climusic-cache")
OLD_LIBRARY_FILE = os.path.join(DATA_HOME, "climusic", "library.json")
# Pre-computed visualizer bar segments: _VIS_BARS[color_idx][level] = "[#color]char[/]"
_VIS_BAR_CHARS = "▁▂▃▄▅▆▇█"
_VIS_COLORS = ["#ff1493", "#ff2d95", "#f10086", "#ff4db8", "#c026d3", "#fb7185"]
_VIS_BARS = [
    [f"[{c}]{_VIS_BAR_CHARS[lvl]}[/]" for lvl in range(len(_VIS_BAR_CHARS))]
    for c in _VIS_COLORS
]
_VIS_NUM_BARS = len(_VIS_BAR_CHARS) - 1  # 7
_VIS_NUM_COLORS = len(_VIS_COLORS)       # 6

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
CYAN = "\033[36m"
MAGENTA = "\033[35m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
BLUE = "\033[34m"


class CliMusicError(Exception):
    """Raised for user-facing CLI errors."""


@dataclass(frozen=True)
class Track:
    title: str
    artists: str
    video_id: str
    fallback_video_id: str | None = None
    album: str | None = None
    thumbnail: str | None = None
    duration: Any | None = None

    @property
    def playback_id(self) -> str:
        return self.fallback_video_id or self.video_id


MPRIS_XML = """
<node>
  <interface name='org.mpris.MediaPlayer2'>
    <method name='Raise'/>
    <method name='Quit'/>
    <property name='CanQuit' type='b' access='read'/>
    <property name='CanRaise' type='b' access='read'/>
    <property name='HasTrackList' type='b' access='read'/>
    <property name='Identity' type='s' access='read'/>
    <property name='SupportedUriSchemes' type='as' access='read'/>
    <property name='SupportedMimeTypes' type='as' access='read'/>
  </interface>
  <interface name='org.mpris.MediaPlayer2.Player'>
    <method name='Next'/>
    <method name='Previous'/>
    <method name='Pause'/>
    <method name='PlayPause'/>
    <method name='Stop'/>
    <method name='Play'/>
    <method name='Seek'>
      <arg direction='in' name='Offset' type='x'/>
    </method>
    <method name='SetPosition'>
      <arg direction='in' name='TrackId' type='o'/>
      <arg direction='in' name='Position' type='x'/>
    </method>
    <method name='OpenUri'>
      <arg direction='in' name='Uri' type='s'/>
    </method>
    <signal name='Seeked'>
      <arg name='Position' type='x'/>
    </signal>
    <property name='PlaybackStatus' type='s' access='read'/>
    <property name='LoopStatus' type='s' access='readwrite'/>
    <property name='Rate' type='d' access='readwrite'/>
    <property name='Shuffle' type='b' access='readwrite'/>
    <property name='Metadata' type='a{sv}' access='read'/>
    <property name='Volume' type='d' access='readwrite'/>
    <property name='Position' type='x' access='read'/>
    <property name='MinimumRate' type='d' access='read'/>
    <property name='MaximumRate' type='d' access='read'/>
    <property name='CanGoNext' type='b' access='read'/>
    <property name='CanGoPrevious' type='b' access='read'/>
    <property name='CanPlay' type='b' access='read'/>
    <property name='CanPause' type='b' access='read'/>
    <property name='CanSeek' type='b' access='read'/>
    <property name='CanControl' type='b' access='read'/>
  </interface>
</node>
"""


class MprisProvider:
    def __init__(self, app) -> None:
        self.app = app
        self._playback_status = "Stopped"
        self._metadata = {}

    # org.mpris.MediaPlayer2
    def Raise(self): pass
    def Quit(self): self.app.action_quit()

    @property
    def CanQuit(self) -> bool: return True
    @property
    def CanRaise(self) -> bool: return False
    @property
    def HasTrackList(self) -> bool: return False
    @property
    def Identity(self) -> str: return APP_NAME
    @property
    def SupportedUriSchemes(self) -> list[str]: return ["https"]
    @property
    def SupportedMimeTypes(self) -> list[str]: return ["audio/mpeg", "audio/mp4", "audio/ogg"]

    # org.mpris.MediaPlayer2.Player
    def Next(self): self.app.call_from_thread(self.app.action_next_track)
    def Previous(self): self.app.call_from_thread(self.app.action_previous_track)
    def Pause(self): self.app.call_from_thread(self.app.action_toggle_pause)
    def PlayPause(self): self.app.call_from_thread(self.app.action_toggle_pause)
    def Stop(self): self.app.call_from_thread(self.app.stop_player)
    def Play(self): self.app.call_from_thread(self.app.action_toggle_pause)
    def Seek(self, Offset): pass
    def SetPosition(self, TrackId, Position): pass
    def OpenUri(self, Uri): pass

    @property
    def PlaybackStatus(self) -> str: return self._playback_status
    @property
    def LoopStatus(self) -> str: return "None"
    @LoopStatus.setter
    def LoopStatus(self, value): pass
    @property
    def Rate(self) -> float: return 1.0
    @Rate.setter
    def Rate(self, value): pass
    @property
    def Shuffle(self) -> bool: return False
    @Shuffle.setter
    def Shuffle(self, value): pass
    @property
    def Metadata(self) -> dict: return self._metadata
    @property
    def Volume(self) -> float: return 1.0
    @Volume.setter
    def Volume(self, value): pass
    @property
    def Position(self) -> int: return 0
    @property
    def MinimumRate(self) -> float: return 1.0
    @property
    def MaximumRate(self) -> float: return 1.0
    @property
    def CanGoNext(self) -> bool: return True
    @property
    def CanGoPrevious(self) -> bool: return True
    @property
    def CanPlay(self) -> bool: return True
    @property
    def CanPause(self) -> bool: return True
    @property
    def CanSeek(self) -> bool: return False
    @property
    def CanControl(self) -> bool: return True

    def update_state(self, track: Track | None, status: str):
        self._playback_status = status
        if track:
            # Use remote URL initially, then update to local file if available
            self._metadata = {
                "mpris:trackid": f"/org/mpris/MediaPlayer2/Track/{track.playback_id.replace('-', '_')}",
                "xesam:title": track.title,
                "xesam:artist": [track.artists],
                "mpris:artUrl": f"https://img.youtube.com/vi/{track.playback_id}/hqdefault.jpg"
            }
            # Start background download to local cache
            threading.Thread(target=self._cache_art, args=(track,), daemon=True).start()
        else:
            self._metadata = {}

    def _cache_art(self, track: Track):
        os.makedirs(CACHE_DIR, exist_ok=True)
        local_path = os.path.join(CACHE_DIR, f"{track.playback_id}.jpg")
        if not os.path.exists(local_path):
            url = f"https://img.youtube.com/vi/{track.playback_id}/hqdefault.jpg"
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "CLIMusicPlayer/1.0"})
                with urllib.request.urlopen(request, timeout=10) as response:
                    with open(local_path, "wb") as f:
                        f.write(response.read())
            except Exception:
                return

        if os.path.exists(local_path):
            self._metadata["mpris:artUrl"] = f"file://{local_path}"
            # In pydbus, we don't always need to emit manually if we just update the dict,
            # but Waybar might need a nudge or it will just poll.
            # Actually, pydbus properties don't automatically emit signals on dict mutation.
            # We'll just hope Waybar picks it up on next poll or we could try to emit.


def track_key(track: Track) -> str:
    return track.playback_id


def track_to_dict(track: Track) -> dict[str, Any]:
    data = {
        "title": track.title,
        "artists": track.artists,
        "video_id": track.video_id,
        "album": track.album,
        "thumbnail": track.thumbnail,
        "duration": track.duration,
    }
    if track.fallback_video_id:
        data["fallback_video_id"] = track.fallback_video_id
    return data


def track_from_dict(item: Any) -> Track | None:
    if not isinstance(item, dict):
        return None
    title = item.get("title")
    artists = item.get("artists")
    video_id = item.get("video_id") or item.get("videoId") or item.get("id")
    if not title or not artists or not video_id:
        return None
    
    return Track(
        title=str(title),
        artists=str(artists),
        video_id=str(video_id),
        fallback_video_id=item.get("fallback_video_id") or item.get("fallbackVideoId"),
        album=item.get("album"),
        thumbnail=item.get("thumbnail"),
        duration=item.get("duration")
    )


def load_library() -> dict[str, list[Track]]:
    library_path = LIBRARY_FILE if os.path.exists(LIBRARY_FILE) else OLD_LIBRARY_FILE
    try:
        with open(library_path, encoding="utf-8") as library_file:
            data = json.load(library_file)
    except (OSError, json.JSONDecodeError):
        return {"recent": [], "favorites": []}

    return {
        "recent": [track for item in data.get("recent", []) if (track := track_from_dict(item))],
        "favorites": [track for item in data.get("favorites", []) if (track := track_from_dict(item))],
    }


def save_library(recent: list[Track], favorites: list[Track]) -> None:
    os.makedirs(os.path.dirname(LIBRARY_FILE), exist_ok=True)
    data = {
        "recent": [track_to_dict(track) for track in recent[:50]],
        "favorites": [track_to_dict(track) for track in favorites],
    }
    with open(LIBRARY_FILE, "w", encoding="utf-8") as library_file:
        json.dump(data, library_file, indent=2)


class YTMusicClient:
    def __init__(self, base_url: str | None = None, timeout: int = 20) -> None:
        self.yt = YTMusic()
        self.timeout = timeout
        # base_url is ignored as ytmusicapi has its own endpoint logic

    def search(self, query: str, filter_name: str = "songs") -> list[Track]:
        # Map our internal filter names to ytmusicapi filters
        api_filter = None
        if filter_name == "songs":
            api_filter = "songs"
        elif filter_name == "albums":
            api_filter = "albums"
        elif filter_name == "artists":
            api_filter = "artists"
        
        try:
            results = self.yt.search(query, filter=api_filter)
            tracks = [track for item in results if (track := self._parse_track(item))]
            
            # If we searched for songs and got nothing, try a general search as fallback
            if not tracks and api_filter == "songs":
                results = self.yt.search(query)
                tracks = [track for item in results if (track := self._parse_track(item))]
                
            return tracks
        except Exception as exc:
            # Fallback to general search on any API error
            try:
                results = self.yt.search(query)
                return [track for item in results if (track := self._parse_track(item))]
            except Exception:
                raise CliMusicError(f"Search failed: {exc}")

    def _parse_track(self, item: dict[str, Any]) -> Track | None:
        try:
            video_id = item.get("videoId") or item.get("id")
            if not video_id:
                return None
            
            # Extract title
            title = item.get("title", "Unknown Title")
            
            # Extract artists
            artists = item.get("artists", [])
            artist_names = []
            if isinstance(artists, list):
                for a in artists:
                    if isinstance(a, dict) and a.get("name"):
                        artist_names.append(a["name"])
                    elif isinstance(a, str):
                        artist_names.append(a)
            elif isinstance(artists, str):
                artist_names = [artists]
            
            artist_name = ", ".join(artist_names) if artist_names else "Unknown Artist"
            
            # Extract album
            album = item.get("album", {})
            album_name = None
            if isinstance(album, dict):
                album_name = album.get("name")
            elif isinstance(album, str):
                album_name = album
            
            # Extract thumbnail
            thumbnails = item.get("thumbnails", [])
            thumb = thumbnails[-1].get("url") if thumbnails else ""
            
            # Extract duration
            duration = item.get("duration_seconds") or item.get("duration")
            
            return Track(
                title=str(title),
                artists=str(artist_name),
                video_id=str(video_id),
                album=album_name,
                thumbnail=str(thumb),
                duration=duration
            )
        except Exception:
            return None

    def get_radio(self, video_id: str) -> list[Track]:
        try:
            results = self.yt.get_watch_playlist(videoId=video_id)
            tracks = results.get("tracks", [])
            return [track for item in tracks if (track := self._parse_track(item))]
        except Exception:
            return []

    def get_trending(self) -> list[Track]:
        return self.trending("US")

    def radio(self, video_id: str) -> list[Track]:
        return self.get_radio(video_id)

    def trending(self, country: str = "US") -> list[Track]:
        try:
            charts = self.yt.get_charts(country=country or "US")
            songs = charts.get("songs", {}).get("items", [])
            if songs:
                return [track for item in songs if (track := self._parse_track(item))]
            videos = charts.get("videos", [])
            if videos and isinstance(videos[0], dict) and videos[0].get("playlistId"):
                playlist = self.yt.get_playlist(videos[0]["playlistId"], limit=20)
                return [track for item in playlist.get("tracks", []) if (track := self._parse_track(item))]
            return []
        except Exception:
            return []

    def _parse_lrc_timed(self, lrc_text: str) -> list[tuple[float, str]]:
        timed: list[tuple[float, str]] = []
        for raw_line in lrc_text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            matches = re.findall(r"\[(\d+):(\d+(?:\.\d+)?)\]", line)
            if not matches:
                continue
            lyric_text = re.sub(r"\[(\d+):(\d+(?:\.\d+)?)\]", "", line).strip()
            if not lyric_text:
                continue
            for minutes, seconds in matches:
                try:
                    timed.append((int(minutes) * 60 + float(seconds), lyric_text))
                except ValueError:
                    continue
        timed.sort(key=lambda item: item[0])
        return timed

    def _extract_timed_lyrics(self, lyrics_data: dict[str, Any]) -> list[tuple[float, str]]:
        timed_raw = (
            lyrics_data.get("timedLyrics")
            or lyrics_data.get("timedlyrics")
            or lyrics_data.get("syncedLyrics")
            or lyrics_data.get("timed_lyrics")
        )
        if isinstance(timed_raw, str):
            return self._parse_lrc_timed(timed_raw)
        if not isinstance(timed_raw, list):
            return []

        timed: list[tuple[float, str]] = []
        for entry in timed_raw:
            if not isinstance(entry, dict):
                continue
            text = str(entry.get("text") or entry.get("lyric") or entry.get("line") or "").strip()
            if not text:
                continue
            stamp = entry.get("startTimeMs") or entry.get("timeMs") or entry.get("start") or entry.get("time")
            try:
                if isinstance(stamp, str) and ":" in stamp:
                    parsed = self._parse_lrc_timed(f"[{stamp}]{text}")
                    if parsed:
                        timed.extend(parsed)
                        continue
                time_value = float(stamp) / (1000.0 if float(stamp) > 1000 else 1.0)
            except (TypeError, ValueError):
                continue
            timed.append((time_value, text))
        timed.sort(key=lambda item: item[0])
        return timed

    def get_lyrics_data(self, video_id: str) -> dict[str, Any]:
        try:
            watch = self.yt.get_watch_playlist(videoId=video_id)
            lyrics_id = watch.get("lyrics")
            if not lyrics_id:
                return {"text": "No lyrics found for this track.", "timed": []}
            lyrics_data = self.yt.get_lyrics(lyrics_id)
            text = str(lyrics_data.get("lyrics") or "Lyrics not available.")
            timed = self._extract_timed_lyrics(lyrics_data)
            return {"text": text, "timed": timed}
        except Exception as exc:
            return {"text": f"Error fetching lyrics: {exc}", "timed": []}

    def get_lyrics(self, video_id: str) -> str:
        return str(self.get_lyrics_data(video_id).get("text") or "Lyrics not available.")

    def stream_url(self, video_id: str) -> tuple[str, dict[str, Any]]:
        # ytmusicapi doesn't provide stream URLs
        raise CliMusicError("ytmusicapi does not provide direct streaming URLs")

        # Compatibility methods for existing calls
    def lyrics(self, title: str, artist: str) -> str:
        # We need a video_id for ytmusicapi lyrics, so we search first
        try:
            results = self.search(f"{title} {artist}", filter_name="songs")
            if results:
                return self.get_lyrics(results[0].playback_id)
            return "Could not find track to fetch lyrics."
        except Exception:
            return "Lyrics not available."

VeromeClient = YTMusicClient


def stream_score(item: dict[str, Any]) -> tuple[int, int, int]:
    mime_type = str(item.get("mimeType") or item.get("mime") or "")
    bitrate = int(item.get("bitrate") or 0)
    is_audio = 1 if mime_type.startswith("audio/") else 0
    # ffplay handles MP4/AAC more reliably across systems than WebM/Opus.
    mp4_preference = 1 if mime_type.startswith("audio/mp4") else 0
    return is_audio, mp4_preference, bitrate


def parse_track(item: Any) -> Track | None:
    if not isinstance(item, dict):
        return None
    title = str(item.get("title") or item.get("name") or "Untitled")
    video_id = item.get("videoId") or item.get("id")
    fallback_video_id = item.get("fallbackVideoId")
    if not video_id and not fallback_video_id:
        return None

    artists = item.get("artists") or item.get("artist") or item.get("author") or item.get("uploader") or "Unknown artist"
    if isinstance(artists, list):
        names = [str(artist.get("name")) for artist in artists if isinstance(artist, dict) and artist.get("name")]
        artists_text = ", ".join(names) if names else "Unknown artist"
    else:
        artists_text = str(artists)

    return Track(
        title=title,
        artists=artists_text,
        video_id=str(video_id or fallback_video_id),
        fallback_video_id=str(fallback_video_id) if fallback_video_id else None,
    )


@lru_cache(maxsize=1)
def use_color() -> bool:
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ


def color(text: str, code: str) -> str:
    if not use_color():
        return text
    return f"{code}{text}{RESET}"


def term_width(max_width: int = 88) -> int:
    return max(48, min(max_width, shutil.get_terminal_size((88, 24)).columns - 4))


def truncate(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return f"{text[: max(0, width - 1)]}…" if use_color() else f"{text[: max(0, width - 3)]}..."


def panel(title: str, lines: list[str], width: int | None = None) -> None:
    width = width or term_width()
    inner = width - 4
    top = f"╭─ {title} " + "─" * max(0, inner - len(title) - 3) + "╮"
    bottom = "╰" + "─" * (width - 2) + "╯"
    print(color(top, MAGENTA))
    for line in lines:
        for wrapped in textwrap.wrap(line, width=inner) or [""]:
            print(color("│ ", MAGENTA) + f"{wrapped:<{inner}}" + color(" │", MAGENTA))
    print(color(bottom, MAGENTA))


def hero() -> None:
    width = term_width()
    logo = [
        "    ____   ____   ____  _____",
        "   /  _/  / __ \\ /  _/ / ___/",
        "   / /   / /_/ / / /   \\__ \\",
        " _/ /   / _, _/_/ /   ___/ /",
        "/___/  /_/ |_|/___/  /____/ ",
    ]
    print()
    for index, line in enumerate(logo):
        shade = CYAN if index % 2 == 0 else BLUE
        print(color(line.center(width), shade))
    print(color("IRIS TUI player".center(width), DIM))
    print()


def render_home() -> None:
    if sys.stdout.isatty():
        print("\033[2J\033[H", end="")
    hero()
    panel(
        "Commands",
        [
            "search <song>       Find tracks on YouTube Music",
            "play <song>         Search, select, and play audio",
            "lyrics <song>       Fetch synced or plain lyrics",
            "radio <song>        Build a related-track radio mix",
            "trending [country]  Show trending music, default US",
            "recent              Show recently played songs",
            "favorites           Show favourite songs",
            "favorite <song>     Add/remove a favourite song",
            "quit                Leave the player",
        ],
    )
    print()


def print_tracks(tracks: list[Track], limit: int | None = None) -> None:
    if not tracks:
        panel("No Results", ["No tracks found. Try another search."])
        return
    shown = tracks[:limit] if limit else tracks
    width = len(str(len(shown)))
    display_width = term_width()
    title_width = max(18, display_width - 34)
    print(color(f"\n{'#':>{width}}  {'Track':<{title_width}}  Artist", BOLD))
    print(color("─" * display_width, DIM))
    for index, track in enumerate(shown, start=1):
        title = truncate(track.title, title_width)
        artist = truncate(track.artists, max(12, display_width - title_width - width - 5))
        print(
            f"{color(str(index).rjust(width), CYAN)}  "
            f"{color(title.ljust(title_width), BOLD)}  "
            f"{artist} {color('[' + track.playback_id + ']', DIM)}"
        )
    print()


def choose_track(tracks: list[Track]) -> Track | None:
    print_tracks(tracks)
    if not tracks:
        return None
    while True:
        choice = input(color("Select track number, or press Enter to cancel: ", GREEN)).strip()
        if not choice:
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(tracks):
            return tracks[int(choice) - 1]
        print(color("Invalid selection.", RED))


@lru_cache(maxsize=1)
def find_player() -> list[str]:
    configured = os.environ.get(PLAYER_ENV) or os.environ.get(LEGACY_PLAYER_ENV)
    if configured:
        return configured.split()

    candidates = (
        ["mpv", "--no-video"],
        ["ffplay", "-nodisp", "-vn", "-autoexit", "-volume", "100", "-loglevel", "warning"],
        ["vlc", "--intf", "dummy"],
    )
    for command in candidates:
        if shutil.which(command[0]):
            return command
    raise CliMusicError(
        "No supported player found. Install mpv, ffmpeg/ffplay, or vlc, "
        f"or set {PLAYER_ENV}='your-player-command'."
    )


@lru_cache(maxsize=1)
def find_yt_dlp() -> list[str] | None:
    # First, try to run as a module via the current python executable.
    # This is the most robust way to avoid shebang/interpreter issues.
    try:
        import yt_dlp
        return [sys.executable, "-m", "yt_dlp"]
    except ImportError:
        pass

    # Fallback to binary in PATH
    executable = shutil.which("yt-dlp")
    if executable:
        return [executable]

    # Fallback to binary in venv
    local_executable = os.path.join(os.path.dirname(sys.executable), "yt-dlp")
    if os.path.exists(local_executable) and os.access(local_executable, os.X_OK):
        return [local_executable]

    return None


def yt_dlp_stream_url(video_id: str) -> str:
    yt_dlp = find_yt_dlp()
    if not yt_dlp:
        raise CliMusicError("yt-dlp is not installed")

    url = f"https://www.youtube.com/watch?v={video_id}"
    extra_args = shlex.split(os.environ.get(YTDLP_ARGS_ENV, ""))
    cookies_browser = os.environ.get(YTDLP_COOKIES_BROWSER_ENV)
    if cookies_browser and "--cookies-from-browser" not in extra_args and "--cookies" not in extra_args:
        extra_args.extend(["--cookies-from-browser", cookies_browser])

    attempts = [extra_args]
    if not any(arg in extra_args for arg in ("--cookies", "--cookies-from-browser")):
        attempts.extend(
            [
                ["--cookies-from-browser", "chrome+kwallet"],
                ["--cookies-from-browser", "brave+kwallet"],
                ["--cookies-from-browser", "chrome"],
                ["--cookies-from-browser", "brave"],
            ]
        )

    last_error = "yt-dlp could not resolve audio"
    for attempt_args in attempts:
        try:
            result = subprocess.run(
                [*yt_dlp, *attempt_args, "-f", "bestaudio", "-g", "--no-playlist", url],
                check=False,
                capture_output=True,
                text=True,
                timeout=45,
            )
        except subprocess.TimeoutExpired:
            last_error = "yt-dlp timed out while resolving the audio stream"
            continue
        except OSError as exc:
            raise CliMusicError(f"Could not run yt-dlp: {exc}") from exc

        stream_urls = [line.strip() for line in result.stdout.splitlines() if line.strip().startswith("http")]
        if stream_urls:
            return stream_urls[-1]
        if result.stderr.strip():
            last_error = result.stderr.strip().splitlines()[-1]

    raise CliMusicError(last_error)


def yt_dlp_search(query: str, limit: int = 20) -> list[Track]:
    yt_dlp = find_yt_dlp()
    if not yt_dlp:
        raise CliMusicError("yt-dlp is not installed")

    extra_args = shlex.split(os.environ.get(YTDLP_ARGS_ENV, ""))
    cookies_browser = os.environ.get(YTDLP_COOKIES_BROWSER_ENV)
    if cookies_browser and "--cookies-from-browser" not in extra_args and "--cookies" not in extra_args:
        extra_args.extend(["--cookies-from-browser", cookies_browser])

    try:
        result = subprocess.run(
            [
                *yt_dlp,
                *extra_args,
                "--dump-json",
                "--flat-playlist",
                "--playlist-end",
                str(limit),
                f"ytsearch{limit}:{query}",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=25,
        )
    except subprocess.TimeoutExpired as exc:
        raise CliMusicError("yt-dlp timed out while searching") from exc
    except OSError as exc:
        raise CliMusicError(f"Could not run yt-dlp: {exc}") from exc

    tracks: list[Track] = []
    for line in result.stdout.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        video_id = item.get("id") or item.get("url")
        if not video_id:
            continue
        tracks.append(
            Track(
                title=str(item.get("title") or "Untitled"),
                artists=str(item.get("uploader") or item.get("channel") or item.get("creator") or "Unknown artist"),
                video_id=str(video_id),
            )
        )

    if tracks:
        return tracks

    error = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "yt-dlp returned no search results"
    raise CliMusicError(error)


def search_tracks(client: VeromeClient, query: str, filter_name: str = "songs", limit: int = 20) -> list[Track]:
    if filter_name != "songs" or not find_yt_dlp():
        return client.search(query, filter_name)

    # Run yt-dlp and API search in parallel, return first successful result
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        ytdlp_future = executor.submit(yt_dlp_search, query, limit)
        api_future = executor.submit(client.search, query, filter_name)

        for future in concurrent.futures.as_completed([ytdlp_future, api_future]):
            try:
                result = future.result()
                if result:
                    # Cancel the other future (best-effort)
                    ytdlp_future.cancel()
                    api_future.cancel()
                    return result
            except Exception:
                continue

    # Both failed — fall back to API one more time (may raise)
    return client.search(query, filter_name)


def playback_stream_url(client: VeromeClient, track: Track) -> tuple[str, dict[str, Any], str]:
    yt_dlp_error = ""
    if find_yt_dlp():
        try:
            return yt_dlp_stream_url(track.playback_id), {}, "yt-dlp"
        except CliMusicError as exc:
            yt_dlp_error = str(exc)

    try:
        stream_url, metadata = client.stream_url(track.playback_id)
        if yt_dlp_error:
            metadata = {**metadata, "ytDlpError": yt_dlp_error}
        return stream_url, metadata, "Verome"
    except CliMusicError as exc:
        detail = f" yt-dlp also failed: {yt_dlp_error}" if yt_dlp_error else ""
        raise CliMusicError(
            f"{exc}. YouTube is blocking direct extraction. Try exporting cookies or run with "
            f"{YTDLP_COOKIES_BROWSER_ENV}=firefox or {YTDLP_ARGS_ENV}='--cookies-from-browser chrome'.{detail}"
        ) from exc


def play_track(client: VeromeClient, track: Track) -> None:
    stream_url, metadata, source = playback_stream_url(client, track)
    title = metadata.get("title") if isinstance(metadata.get("title"), str) else track.title
    panel("IRIS Player", [f"{title}", f"Artist: {track.artists}", f"Source: {source} / {track.playback_id}"])
    player = find_player()
    try:
        subprocess.run([*player, stream_url], check=False)
    except FileNotFoundError as exc:
        raise CliMusicError(f"Player executable not found: {player[0]}") from exc


def add_recent_track(track: Track) -> None:
    library = load_library()
    recent = [item for item in library["recent"] if track_key(item) != track_key(track)]
    recent.insert(0, track)
    save_library(recent, library["favorites"])


def toggle_favorite_saved(track: Track) -> bool:
    library = load_library()
    key = track_key(track)
    favorites = library["favorites"]
    if any(track_key(item) == key for item in favorites):
        favorites = [item for item in favorites if track_key(item) != key]
        added = False
    else:
        favorites.insert(0, track)
        added = True
    save_library(library["recent"], favorites)
    return added


def start_player(stream_url: str) -> subprocess.Popen[Any]:
    player = find_player()
    ipc_socket = ""
    command = [*player, stream_url]
    if player and os.path.basename(player[0]) == "mpv" and not any(arg.startswith("--input-ipc-server") for arg in player):
        ipc_socket = os.path.join(tempfile.gettempdir(), f"iris-mpv-{os.getpid()}-{time.monotonic_ns()}.sock")
        command = [*player, f"--input-ipc-server={ipc_socket}", stream_url]
    with open(PLAYER_LOG, "w", encoding="utf-8") as log:
        log.write(f"Running: {' '.join(command[:-1])} <stream-url>\n")
    try:
        log_file = open(PLAYER_LOG, "a", encoding="utf-8")
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log_file, stderr=log_file)
            if ipc_socket:
                PLAYER_IPC_SOCKETS[process.pid] = ipc_socket
            return process
        finally:
            log_file.close()
    except FileNotFoundError as exc:
        raise CliMusicError(f"Player executable not found: {player[0]}") from exc


def cleanup_player_ipc(process: subprocess.Popen[Any] | None) -> None:
    if not process:
        return
    ipc_socket = PLAYER_IPC_SOCKETS.pop(process.pid, "")
    if ipc_socket:
        try:
            os.unlink(ipc_socket)
        except OSError:
            pass


def mpv_property(process: subprocess.Popen[Any] | None, name: str) -> float | None:
    """Query a single mpv property (kept for backward compatibility)."""
    results = mpv_properties(process, [name])
    return results.get(name)


def mpv_properties(process: subprocess.Popen[Any] | None, names: list[str]) -> dict[str, float | None]:
    """Query multiple mpv properties in a single socket connection."""
    result: dict[str, float | None] = {n: None for n in names}
    if not process:
        return result
    ipc_socket = PLAYER_IPC_SOCKETS.get(process.pid)
    if not ipc_socket or not os.path.exists(ipc_socket):
        return result
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.2)
            client.connect(ipc_socket)
            payload = b""
            for idx, name in enumerate(names):
                payload += json.dumps({"command": ["get_property", name], "request_id": idx}).encode() + b"\n"
            client.sendall(payload)
            
            response = ""
            start_time = time.monotonic()
            while time.monotonic() - start_time < 0.3:
                try:
                    chunk = client.recv(4096).decode("utf-8", errors="replace")
                    if not chunk:
                        break
                    response += chunk
                    if all(f'"request_id":{idx}' in response.replace(" ", "") for idx in range(len(names))):
                        break
                except socket.timeout:
                    break
    except OSError:
        return result

    for line in response.splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        req_id = data.get("request_id")
        value = data.get("data")
        if isinstance(req_id, int) and 0 <= req_id < len(names):
            if isinstance(value, (int, float)):
                result[names[req_id]] = float(value)
    return result


def send_mpv_command(process: subprocess.Popen[Any] | None, command: list[Any]) -> None:
    if not process:
        return
    ipc_socket = PLAYER_IPC_SOCKETS.get(process.pid)
    if not ipc_socket or not os.path.exists(ipc_socket):
        return
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.2)
            client.connect(ipc_socket)
            payload = json.dumps({"command": command}).encode() + b"\n"
            client.sendall(payload)
    except OSError:
        pass


def stream_duration(stream_url: str, metadata: dict[str, Any]) -> float | None:
    for value in (metadata.get("duration"), metadata.get("lengthSeconds")):
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
        if isinstance(value, str):
            try:
                parsed = float(value)
            except ValueError:
                continue
            if parsed > 0:
                return parsed
    stream = metadata.get("stream")
    if isinstance(stream, dict):
        value = stream.get("dur") or stream.get("duration")
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
        if isinstance(value, str):
            try:
                parsed = float(value)
            except ValueError:
                parsed = 0.0
            if parsed > 0:
                return parsed
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(stream_url).query)
        dur = query.get("dur", [""])[0]
        return float(dur) if dur else None
    except (TypeError, ValueError):
        return None


def read_player_log(max_chars: int = 500) -> str:
    try:
        with open(PLAYER_LOG, encoding="utf-8", errors="replace") as log:
            return log.read()[-max_chars:].strip()
    except OSError:
        return ""


def command_search(client: VeromeClient, args: argparse.Namespace) -> None:
    tracks = search_tracks(client, args.query, args.filter, args.limit)
    print_tracks(tracks, args.limit)


def command_play(client: VeromeClient, args: argparse.Namespace) -> None:
    if args.video_id:
        track = Track(args.video_id, "Direct video ID", args.video_id)
    else:
        if not args.query:
            raise CliMusicError("Provide a search query or --video-id")
        tracks = search_tracks(client, args.query, "songs")
        track = tracks[0] if args.first and tracks else choose_track(tracks)
    if track:
        add_recent_track(track)
        play_track(client, track)


def command_lyrics(client: VeromeClient, args: argparse.Namespace) -> None:
    title = args.title
    artist = args.artist
    if (title and not artist) or (artist and not title):
        raise CliMusicError("Use --title and --artist together")
    if not title or not artist:
        if not args.query:
            raise CliMusicError("Provide a search query or both --title and --artist")
        tracks = search_tracks(client, args.query, "songs")
        track = tracks[0] if args.first and tracks else choose_track(tracks)
        if not track:
            return
        title = track.title
        artist = track.artists.split(", ", maxsplit=1)[0]
    panel("Lyrics", client.lyrics(title, artist).splitlines())


def command_radio(client: VeromeClient, args: argparse.Namespace) -> None:
    video_id = args.video_id
    if not video_id:
        if not args.query:
            raise CliMusicError("Provide a search query or --video-id")
        tracks = search_tracks(client, args.query, "songs")
        track = tracks[0] if args.first and tracks else choose_track(tracks)
        if not track:
            return
        video_id = track.playback_id
    tracks = client.radio(video_id)
    print_tracks(tracks, args.limit)


def command_trending(client: VeromeClient, args: argparse.Namespace) -> None:
    print_tracks(client.trending(args.country), args.limit)


def command_recent(_: VeromeClient, args: argparse.Namespace) -> None:
    print_tracks(load_library()["recent"], args.limit)


def command_favorites(client: VeromeClient, args: argparse.Namespace) -> None:
    library = load_library()
    if args.add:
        tracks = search_tracks(client, args.add, "songs")
        track = tracks[0] if args.first and tracks else choose_track(tracks)
        if not track:
            return
        added = toggle_favorite_saved(track)
        print(color(f"{'Added to' if added else 'Removed from'} favourites: {track.title}", GREEN))
        return
    print_tracks(library["favorites"], args.limit)


def run_tui(client: VeromeClient) -> None:
    try:
        from textual.app import App, ComposeResult
        from textual.containers import Horizontal, Vertical
        from textual.events import Click
        from textual.widgets import DataTable, Footer, Header, Input, Static
    except ImportError as exc:
        raise CliMusicError("Textual UI needs dependencies. Install them with: python3 -m pip install -r requirements.txt") from exc


    class InteractiveVisualizer(Static):
        def on_click(self, event: Click) -> None:
            app = self.app
            if not getattr(app, "player_process", None): return
            if event.y == 3:
                bar_width = 28
                if 0 <= event.x < bar_width:
                    ratio = event.x / bar_width
                    app.seek_to_ratio(ratio)

    class InteractiveDetails(Static):
        def on_click(self, event: Click) -> None:
            app = self.app
            if not getattr(app, "player_process", None): return
            if event.y == 8:
                bar_width = 28
                if 0 <= event.x < bar_width:
                    ratio = event.x / bar_width
                    app.seek_to_ratio(ratio)

    class InteractiveProgress(Static):
        def on_click(self, event: Click) -> None:
            app = self.app
            if not getattr(app, "player_process", None):
                return
            if event.x < 0 or event.x >= self.size.width:
                return
            ratio = event.x / self.size.width
            ratio = max(0.0, min(1.0, ratio))
            app.seek_to_ratio(ratio)

    class CliMusicTui(App[None]):
        CSS = """
        Screen {
            background: #120712;
            color: #fce7f3;
        }

        Header, Footer {
            background: #2a0a22;
            color: #ff1493;
        }

        #body {
            height: 1fr;
        }

        #sidebar {
            width: 24;
            min-width: 22;
            background: #1a0a18;
            border: round #ff1493;
            padding: 1;
        }

        #main {
            width: 1fr;
            border: round #5b143f;
        }

        #visualizer {
            height: 8;
            min-height: 8;
            padding: 0 1;
            background: #160516;
            border-top: heavy #ff1493;
color: #ff1493;
         }

         #progress_bar {
             width: 28;
             height: 1;
             margin: 1 0;
             color: #ff1493;
         }

         #details {
            width: 30;
            min-width: 26;
            background: #1a0a18;
            border: round #ff1493;
            padding: 1;
        }

        #search {
            dock: top;
            margin: 0 1 1 1;
        }

        #table {
            height: 1fr;
        }

        #lyrics_view {
            height: 1fr;
            padding: 1 2;
            text-align: center;
            content-align: center middle;
            display: none;
        }

        #status {
            dock: bottom;
            height: 3;
            padding: 0 1;
            background: #21081d;
            color: #f9a8d4;
        }

        .title {
            color: #ff1493;
            text-style: bold;
        }
        """

        BINDINGS = [
            ("/", "focus_search", "Search"),
            ("enter,p", "play_selected", "Play"),
            ("space", "toggle_pause", "Pause/Resume"),
            ("n", "next_track", "Next"),
            ("b", "previous_track", "Previous"),
            ("right", "seek_forward", "Forward"),
            ("left", "seek_backward", "Backward"),
            ("f", "toggle_favorite", "Favorite"),
            ("t", "trending", "Trending"),
            ("r", "radio", "Radio"),
            ("g", "recent", "Recent"),
            ("v", "favorites", "Favorites"),
            ("l", "lyrics", "Lyrics"),
            ("m", "toggle_lyrics_mode", "Lyrics mode"),
            ("?", "toggle_help", "Help"),
            ("q", "quit", "Quit"),
        ]

        def __init__(self, api_client: VeromeClient) -> None:
            super().__init__()
            self.client = api_client
            self.tracks: list[Track] = []
            self.now_playing: Track | None = None
            self.now_playing_index: int | None = None
            self.player_process: subprocess.Popen[Any] | None = None
            self.is_paused = False
            self.playback_started_at: float | None = None
            self.playback_paused_at: float | None = None
            self.playback_paused_total = 0.0
            self.playback_duration = 240.0
            self.player_position: float | None = None
            self.player_duration: float | None = None
            self.last_progress_probe = 0.0
            self.playback_started_ok = False
            library = load_library()
            self.recent_tracks = library["recent"]
            self.favorite_tracks = library["favorites"]
            self._favorite_ids: set[str] = {track_key(t) for t in self.favorite_tracks}
            self.stream_cache: dict[str, tuple[str, dict[str, Any], str]] = {}
            self.search_cache: dict[tuple[str, str], list[Track]] = {}
            self.prefetching: set[str] = set()
            self.current_view = "Search"
            self.search_query = ""
            self.last_query = ""
            self.help_visible = False
            self.details_override: str | None = None
            self.playback_generation = 0
            self.visualizer_frame = 0
            self.busy_label = ""
            self._pending_details_refresh = False
            self._details_refresh_timer: Any = None
            self._save_timer: object | None = None
            self.mpris = None
            self.lyrics_text = ""
            self.lyrics_timed: list[tuple[float, str]] = []
            self.lyrics_track_id: str | None = None
            self.lyrics_focus_index = 0.0
            self.lyrics_mode = "karaoke"
            if HAS_MPRIS:
                try:
                    self.mpris = MprisProvider(self)
                    bus = pydbus.SessionBus()
                    bus.publish("org.mpris.MediaPlayer2.IRIS", self.mpris)
                    threading.Thread(target=GLib.MainLoop().run, daemon=True).start()
                except Exception:
                    self.mpris = None

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            with Horizontal(id="body"):
                yield Static(self.nav_text(), id="sidebar")
                with Vertical(id="main"):
                    yield Input(placeholder="Search YouTube Music with Verome...", id="search")
                    table = DataTable(id="table", cursor_type="row", zebra_stripes=True)
                    table.add_columns("#", "Track", "Artist", "ID")
                    yield table
                    yield Static("", id="lyrics_view")
                    yield InteractiveVisualizer(self.visualizer_text(), id="visualizer")
                    yield InteractiveProgress(self.progress_text(), id="progress_bar")
                    yield Static(
                        "/ search   enter/p play   l lyrics   m mode   space pause   n next   f fav   q quit   left/right seek",
                        id="status",
                    )
                yield InteractiveDetails(self.details_text(), id="details")
            yield Footer()

        class InteractiveProgress(Static):
            def on_click(self, event: Click) -> None:
                app = self.app
                if not getattr(app, "player_process", None):
                    return
                if event.x < 0 or event.x >= self.size.width:
                    return
                ratio = event.x / self.size.width
                ratio = max(0.0, min(1.0, ratio))
                app.seek_to_ratio(ratio)

        def on_mount(self) -> None:
            self.title = APP_NAME
            self.sub_title = f" {APP_SUBTITLE}"
            self.query_one("#search", Input).focus()
            self.set_status("Type a search and press Enter.")
            self.set_interval(0.12, self.tick_visualizer)

        def set_status(self, message: str) -> None:
            self.query_one("#status", Static).update(message)

        def set_tracks(self, view_name: str, tracks: list[Track]) -> None:
            self.current_view = view_name
            self.details_override = None
            self.tracks = tracks
            self.refresh_sidebar()
            table = self.query_one("#table", DataTable)
            table.clear()
            for i, track in enumerate(tracks, 1):
                table.add_row(
                    str(i),
                    track.title,
                    track.artists,
                    track.playback_id,
                    key=f"{i}:{track.playback_id}"
                )
            if tracks:
                table.focus()
            self.refresh_center_pane()
            self.refresh_details()
            asyncio.create_task(self.prefetch_streams(tracks[:5]))

        def refresh_center_pane(self) -> None:
            table = self.query_one("#table", DataTable)
            lyrics_view = self.query_one("#lyrics_view", Static)
            showing_lyrics = self.current_view == "Lyrics"
            table.styles.display = "none" if showing_lyrics else "block"
            lyrics_view.styles.display = "block" if showing_lyrics else "none"
            if showing_lyrics:
                lyrics_view.update(self.render_lyrics_view())

        def stop_player(self) -> None:
            if self.player_process:
                try:
                    self.player_process.terminate()
                    self.player_process.wait(timeout=0.5)
                except Exception:
                    try:
                        self.player_process.kill()
                    except Exception:
                        pass
                cleanup_player_ipc(self.player_process)
                self.player_process = None

        def check_player_started(self, generation: int, track: Track) -> None:
            if generation == self.playback_generation and not self.playback_started_ok:
                if self.player_process and self.player_process.poll() is None:
                    self.set_status(f"Playing {track.title}...")
                else:
                    log = read_player_log(100)
                    self.set_status(f"Playback failed. Log: {log}")

        def handle_player_end(self) -> None:
            if self.player_process and self.player_process.poll() is not None:
                self.stop_player()
                if self.now_playing_index is not None and self.now_playing_index + 1 < len(self.tracks):
                    asyncio.create_task(self.play_track_at(self.now_playing_index + 1))
                else:
                    self.set_status("Playback finished.")
                self.refresh_details()

        async def prefetch_streams(self, tracks: list[Track]) -> None:
            tasks = []
            for track in tracks:
                if track.playback_id not in self.stream_cache and track.playback_id not in self.prefetching:
                    self.prefetching.add(track.playback_id)
                    tasks.append(self._prefetch_one(track))
            if tasks:
                await asyncio.gather(*tasks)

        async def _prefetch_one(self, track: Track) -> None:
            try:
                await asyncio.to_thread(self.cached_playback_stream_url, track)
            except Exception:
                pass
            finally:
                self.prefetching.discard(track.playback_id)

        def add_recent(self, track: Track) -> None:
            key = track_key(track)
            self.recent_tracks = [t for t in self.recent_tracks if track_key(t) != key]
            self.recent_tracks.insert(0, track)
            self.recent_tracks = self.recent_tracks[:50]
            self.refresh_sidebar()
            self.save_library_state()

        def nav_text(self) -> str:
            def item(key: str, label: str, active: bool) -> str:
                text = f"  [b]{key} [/] {label}"
                return f"[dim]{text}[/]" if active else text

            return "\n".join(
                [
                    "[b #ff1493]IRIS[/]",
                    "",
                    "",
                    f"> Search: [white]{self.last_query or '...'}[/]",
                    "",
                    "[b]Library[/]",
                    item("/", "Search", self.current_view == "Search"),
                    item("t", "Trending", self.current_view == "Trending"),
                    item("r", "Radio from selected", self.current_view.startswith("Radio")),
                    item("g", "Recently played", self.current_view == "Recently Played"),
                    item("v", "Favourite songs", self.current_view == "Favourite Songs"),
                    item("l", "Lyrics", self.current_view == "Lyrics"),
                    f"  [b]m [/] Lyrics mode: {self.lyrics_mode.title()}",
                    "",
                    "[b]Playback[/]",
                    "  [b]p [/] Play selected",
                    "  [b]space [/] Pause/resume",
                    "  [b]n [/] Next track",
                    "  [b]b [/] Previous track",
                    "  [b]f [/] Favourite selected",
                    "",
                    f"{len(self.recent_tracks)} recent / {len(self._favorite_ids)} favourites",
                    "",
                    "[b]Help[/]",
                    "  [b]? [/] Toggle keys",
                    "  [b]q [/] Quit",
                ]
            )

        def selected_track(self) -> Track | None:
            try:
                table = self.query_one("#table", DataTable)
                row_index = table.cursor_row
                if 0 <= row_index < len(self.tracks):
                    return self.tracks[row_index]
            except Exception:
                pass
            return None

        def index_for_track(self, track: Track) -> int | None:
            key = track_key(track)
            for index, item in enumerate(self.tracks):
                if track_key(item) == key:
                    return index
            return None

        def is_favorite(self, track: Track) -> bool:
            return track_key(track) in self._favorite_ids

        def cached_playback_stream_url(self, track: Track) -> tuple[str, dict[str, Any], str]:
            if track.playback_id not in self.stream_cache:
                self.stream_cache[track.playback_id] = playback_stream_url(self.client, track)
            return self.stream_cache[track.playback_id]

        def save_library_state(self) -> None:
            if self._save_timer is not None:
                self._save_timer.stop()
            self._save_timer = self.set_timer(1.0, self._do_save_library)

        def _do_save_library(self) -> None:
            self._save_timer = None
            recent = list(self.recent_tracks)
            favorites = list(self.favorite_tracks)
            asyncio.create_task(asyncio.to_thread(save_library, recent, favorites))

        def toggle_favorite_track(self, track: Track) -> bool:
            key = track_key(track)
            if key in self._favorite_ids:
                self._favorite_ids.discard(key)
                self.favorite_tracks = [item for item in self.favorite_tracks if track_key(item) != key]
                added = False
            else:
                self._favorite_ids.add(key)
                self.favorite_tracks.insert(0, track)
                added = True
            self.save_library_state()
            self.refresh_sidebar()
            self.refresh_details()
            return added

        def details_text(self) -> str:
            if self.details_override is not None:
                return self.details_override
            if self.help_visible:
                return "\n".join(
                    [
                        "[b #ff1493]Help[/]",
                        "/ Search focus",
                        "Enter or p Play selected",
                        "Space Pause/resume",
                        "n/b Next/previous",
                        "f Toggle favourite",
                        "t Trending",
                        "r Radio from selected",
                        "g Recent",
                        "v Favourites",
                        "l Lyrics",
                        "m Toggle lyrics mode",
                        "q Quit",
                    ]
                )

            track = self.selected_track()
            display_track = self.now_playing or track
            if not display_track:
                return "[dim]No track selected[/]"

            duration = display_track.duration
            duration_text = self.format_duration(float(duration)) if isinstance(duration, (int, float)) else str(duration or "N/A")
            return "\n".join(
                [
                    f"[b #ff1493]{display_track.title}[/]",
                    f"by {display_track.artists}",
                    f"Album: {display_track.album or 'N/A'}",
                    f"ID: {display_track.playback_id}",
                    f"Duration: {duration_text}",
                    "",
                    "[b]Player Status[/]",
                    f"State: {'Paused' if self.is_paused else 'Playing' if self.player_process else 'Idle'}",
                    self.progress_text(),
                    "",
                    "[b]Selection[/]",
                    f"Track {(self.index_for_track(display_track) or 0) + 1 if self.tracks else '?'} of {len(self.tracks)}",
                    "Favorited: " + ("Yes" if self.is_favorite(display_track) else "No"),
                ]
            )

        def refresh_sidebar(self) -> None:
            self.query_one("#sidebar", Static).update(self.nav_text())

        def refresh_details(self) -> None:
            self.query_one("#details", Static).update(self.details_text())

        def _flush_details_refresh(self) -> None:
            self._details_refresh_timer = None
            if self._pending_details_refresh:
                self._pending_details_refresh = False
                self.refresh_details()

        def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
            self._pending_details_refresh = True
            if self._details_refresh_timer is None:
                self._details_refresh_timer = self.set_timer(0.05, self._flush_details_refresh)

        async def on_input_submitted(self, event: Input.Submitted) -> None:
            if event.input.id == "search":
                query = event.value.strip()
                if not query:
                    return
                self.last_query = query
                self.set_status(f"Searching for '{query}'...")
                self.busy_label = "Searching"
                try:
                    tracks = await asyncio.to_thread(self.client.search, query, "songs")
                    self.set_tracks("Search", tracks)
                    if not tracks:
                        self.set_status(f"No results found for '{query}'.")
                except CliMusicError as exc:
                    self.set_status(f"Error: {exc}")
                self.busy_label = ""

        async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
            await self.play_track_at(event.cursor_row)

        def playback_elapsed(self) -> float:
            if self.player_position is not None:
                return self.player_position
            if self.playback_started_at is None:
                return 0.0
            current = self.playback_paused_at if self.is_paused and self.playback_paused_at else time.monotonic()
            return max(0.0, current - self.playback_started_at - self.playback_paused_total)

        def format_duration(self, seconds: float) -> str:
            minutes, remaining = divmod(int(seconds), 60)
            return f"{minutes}:{remaining:02d}"

        def progress_text(self) -> str:
            if not self.player_process:
                return "[dim]No progress[/]"
            elapsed = self.playback_elapsed()
            duration = max(1.0, self.player_duration or self.playback_duration)
            ratio = min(elapsed / duration, 1.0)
            width = 28
            filled = int(width * ratio)
            bar = "█" * filled + "░" * (width - filled)
            state = "paused" if self.is_paused else "playing"
            return (
                f"[#ff1493]{bar}[/] "
                f"[white]{self.format_duration(elapsed)}[/][dim]/{self.format_duration(duration)} {state}[/]"
            )

        def tick_visualizer(self) -> None:
            self.visualizer_frame += 1
            active = bool(self.player_process and self.player_process.poll() is None)
            if self.current_view == "Lyrics":
                self.query_one("#lyrics_view", Static).update(self.render_lyrics_view())
            if active:
                self.update_player_progress()
                self.handle_player_end()
                self.query_one("#visualizer", Static).update(self.visualizer_text())
                self.refresh_details()
            elif self.visualizer_frame % 4 == 0:
                # Idle: pulse dots change every 4 frames, skip redundant refreshes
                self.handle_player_end()
                self.query_one("#visualizer", Static).update(self.visualizer_text())
            # Update progress bar
            self.query_one("#progress_bar", InteractiveProgress).update(self.progress_text())


        def seek_to_ratio(self, ratio: float) -> None:
            if not self.player_process: return
            duration = max(1.0, self.player_duration or self.playback_duration)
            target_time = max(0.0, min(duration, duration * ratio))
            send_mpv_command(self.player_process, ["set_property", "time-pos", target_time])
            self.player_position = target_time
            self.update_player_progress()
            self.query_one("#visualizer", InteractiveVisualizer).update(self.visualizer_text())
            self.refresh_details()

        def action_seek_forward(self) -> None:
            if not self.player_process: return
            duration = max(1.0, self.player_duration or self.playback_duration)
            current = self.playback_elapsed()
            target = min(duration, current + 10.0)
            self.seek_to_ratio(target / duration)

        def action_seek_backward(self) -> None:
            if not self.player_process: return
            duration = max(1.0, self.player_duration or self.playback_duration)
            current = self.playback_elapsed()
            target = max(0.0, current - 10.0)
            self.seek_to_ratio(target / duration)

        def update_player_progress(self) -> None:
            if not self.player_process or self.player_process.poll() is not None:
                return
            now = time.monotonic()
            if now - self.last_progress_probe < 0.5:
                return
            self.last_progress_probe = now
            
            # Give the player a tiny bit of time to create the socket on first attempt
            if self.player_position is None and self.playback_started_at and now - self.playback_started_at < 1.0:
                time.sleep(0.05)
                
            props = mpv_properties(self.player_process, ["time-pos", "duration"])
            position = props.get("time-pos")
            duration = props.get("duration")
            if position is not None:
                self.player_position = max(0.0, position)
                self.playback_started_ok = True
            if duration is not None and duration > 0:
                self.player_duration = duration

        def visualizer_text(self) -> str:
            active = bool(self.player_process and self.player_process.poll() is None)
            title = self.now_playing.title if self.now_playing else "No track playing"
            if not active:
                pulse = "." * ((self.visualizer_frame // 4) % 4)
                message = self.busy_label or "Pick a track and press Enter"
                return "\n".join(
                    [
                        "[b #ff1493]AUDIO SPECTRUM[/] [dim]idle[/]",
                        "",
                        f"[#ff4db8]{message}{pulse}[/]",
                        "[dim]Visualizer lights up while music is playing.[/]",
                    ]
                )

            # Use pre-computed styled bar segments from _VIS_BARS lookup table
            frame = self.visualizer_frame
            frame_wave = frame * 0.42
            frame_bounce = frame * 0.19
            half_frame = frame >> 1  # frame // 2
            sin = math.sin
            columns = []
            append = columns.append
            for index in range(42):
                wave = sin(frame_wave + index * 0.65)
                bounce = sin(frame_bounce + index * 1.7)
                level = int(((wave + bounce + 2) * 0.25) * _VIS_NUM_BARS)
                color_idx = (index + half_frame) % _VIS_NUM_COLORS
                append(_VIS_BARS[color_idx][max(0, min(level, _VIS_NUM_BARS))])
            row = "".join(columns)
            return "\n".join(
                [
                    f"[b #ff1493]NOW PLAYING[/] [white]{title}[/]",
                    row,
                    "".join(reversed(columns)),
                    self.progress_text(),
                    "[dim]Generated terminal visualizer[/]",
                ]
            )

        def render_lyrics_view(self) -> str:
            if not self.lyrics_text:
                return "[dim]No lyrics loaded. Press l on a selected track.[/]"
            lines = [line.strip() for line in self.lyrics_text.splitlines() if line.strip()]
            if not lines:
                return "[dim]Lyrics not available.[/]"
            if not self.lyrics_timed:
                total_lines = len(lines)
                elapsed = self.playback_elapsed()
                duration = max(1.0, self.player_duration or self.playback_duration)
                weighted_lengths = [max(8, len(line)) for line in lines]
                total_weight = float(sum(weighted_lengths))
                if total_weight <= 0:
                    active_index = 0
                else:
                    target_weight = min(max(elapsed / duration, 0.0), 1.0) * total_weight
                    running = 0.0
                    active_index = total_lines - 1
                    for i, weight in enumerate(weighted_lengths):
                        running += weight
                        if target_weight <= running:
                            active_index = i
                            break

                if active_index >= self.lyrics_focus_index:
                    self.lyrics_focus_index += min(0.25, active_index - self.lyrics_focus_index)
                else:
                    self.lyrics_focus_index -= min(0.35, self.lyrics_focus_index - active_index)

                center_index = int(round(self.lyrics_focus_index))
                karaoke_mode = self.lyrics_mode == "karaoke"
                window = 2 if karaoke_mode else 6
                start = max(0, center_index - window)
                end = min(total_lines, center_index + window + 1)
                pulse_on = (self.visualizer_frame // 3) % 2 == 0
                mode_label = "KARAOKE MODE" if karaoke_mode else "EXTENDED MODE"
                shown: list[str] = [f"[b #ff1493]LYRICS ({mode_label})[/]"]
                for i in range(start, end):
                    text = lines[i]
                    if i == active_index:
                        marker = ">" if pulse_on else "*"
                        shown.append(f"[b #ffd6ea]{marker} {text}[/]")
                    elif karaoke_mode and i == center_index:
                        shown.append(f"[#f9a8d4]  {text}[/]")
                    else:
                        shown.append(f"[dim]{text}[/]")
                return "\n".join(shown)

            elapsed = self.playback_elapsed()
            active_index = 0
            for i, (stamp, _) in enumerate(self.lyrics_timed):
                if stamp <= elapsed:
                    active_index = i
                else:
                    break

            if active_index >= self.lyrics_focus_index:
                self.lyrics_focus_index += min(0.35, active_index - self.lyrics_focus_index)
            else:
                self.lyrics_focus_index -= min(0.55, self.lyrics_focus_index - active_index)

            center_index = int(round(self.lyrics_focus_index))
            karaoke_mode = self.lyrics_mode == "karaoke"
            window = 2 if karaoke_mode else 6
            start = max(0, center_index - window)
            end = min(len(self.lyrics_timed), center_index + window + 1)
            pulse_on = (self.visualizer_frame // 3) % 2 == 0
            mode_label = "KARAOKE MODE" if karaoke_mode else "EXTENDED MODE"
            shown: list[str] = [f"[b #ff1493]{mode_label}[/]"]
            for i in range(start, end):
                text = self.lyrics_timed[i][1]
                if i == active_index:
                    marker = ">" if pulse_on else "*"
                    shown.append(f"[b #ffd6ea]{marker} {text}[/]")
                elif karaoke_mode and i == center_index:
                    shown.append(f"[#f9a8d4]  {text}[/]")
                else:
                    shown.append(f"[dim]{text}[/]")
            return "\n".join(shown)

        async def play_track_at(self, index: int) -> None:
            if index < 0 or index >= len(self.tracks):
                self.set_status("No track available in that direction.")
                return
            track = self.tracks[index]
            if not track:
                self.set_status("Select a track before playing.")
                return
            self.now_playing = track
            self.now_playing_index = index
            self.refresh_details()
            cached = track.playback_id in self.stream_cache
            self.busy_label = "Starting playback" if cached else "Resolving audio stream"
            self.set_status(f"Fetching stream for {track.title}...")
            try:
                stream_url, metadata, source = await asyncio.to_thread(self.cached_playback_stream_url, track)
                self.stop_player()
                self.player_process = start_player(stream_url)
                self.is_paused = False
                self.playback_started_at = time.monotonic()
                self.playback_paused_at = None
                self.playback_paused_total = 0.0
                self.playback_duration = stream_duration(stream_url, metadata) or 240.0
                self.player_position = None
                self.player_duration = self.playback_duration
                self.last_progress_probe = 0.0
                self.playback_started_ok = False
                self.playback_generation += 1
                self.update_mpris("Playing")
            except CliMusicError as exc:
                self.set_status(f"Error: {exc}")
                return
            self.busy_label = ""
            self.add_recent(track)
            self.refresh_details()
            yt_dlp_error = metadata.get("ytDlpError") if isinstance(metadata.get("ytDlpError"), str) else ""
            warning = "" if not yt_dlp_error else f" yt-dlp failed: {yt_dlp_error}"
            self.set_status(f"Player launched for {track.title} via {source}.{warning}")
            self.set_timer(1.0, lambda: self.check_player_started(self.playback_generation, track))

        async def action_trending(self) -> None:
            self.busy_label = "Fetching trending"
            self.set_status("Loading trending charts...")
            try:
                tracks = await asyncio.to_thread(self.client.get_trending)
                self.set_tracks("Trending", tracks)
            except CliMusicError as exc:
                self.set_status(f"Error: {exc}")
            self.busy_label = ""

        async def action_radio(self) -> None:
            track = self.selected_track() or self.now_playing
            if not track:
                self.set_status("Select a track to start radio.")
                return
            self.busy_label = "Generating radio"
            self.set_status(f"Generating radio from {track.title}...")
            try:
                tracks = await asyncio.to_thread(self.client.get_radio, track.video_id)
                self.set_tracks(f"Radio: {track.title}", tracks)
            except CliMusicError as exc:
                self.set_status(f"Error: {exc}")
            self.busy_label = ""

        async def action_recent(self) -> None:
            self.set_tracks("Recently Played", self.recent_tracks)

        async def action_favorites(self) -> None:
            self.set_tracks("Favourite Songs", self.favorite_tracks)

        async def action_lyrics(self) -> None:
            track = self.selected_track() or self.now_playing
            if not track:
                self.set_status("Select a track to view lyrics.")
                return
            self.set_status(f"Fetching lyrics for {track.title}...")
            self.busy_label = "Fetching lyrics"
            try:
                lyrics_data = await asyncio.to_thread(self.client.get_lyrics_data, track.video_id)
                self.lyrics_text = str(lyrics_data.get("text") or "Lyrics not available.")
                timed = lyrics_data.get("timed")
                self.lyrics_timed = timed if isinstance(timed, list) else []
                self.lyrics_track_id = track.playback_id
                self.lyrics_focus_index = 0.0
                self.current_view = "Lyrics"
                self.details_override = None
                self.refresh_sidebar()
                self.refresh_center_pane()
                self.refresh_details()
                if self.lyrics_timed:
                    self.set_status(f"Displaying synced lyrics for {track.title} in the center pane.")
                else:
                    self.set_status(f"Displaying lyrics for {track.title} in the center pane.")
            except CliMusicError as exc:
                self.set_status(f"Error: {exc}")
            self.busy_label = ""

        async def action_play_selected(self) -> None:
            track = self.selected_track()
            if not track:
                self.set_status("Select a track before playing.")
                return
            index = self.index_for_track(track)
            await self.play_track_at(index if index is not None else 0)

        def action_focus_search(self) -> None:
            self.query_one("#search", Input).focus()
            self.set_status("Type a search and press Enter.")

        def action_toggle_pause(self) -> None:
            if not self.player_process or self.player_process.poll() is not None:
                self.set_status("Nothing is playing. Select a track and press Enter.")
                return
            try:
                self.player_process.send_signal(signal.SIGCONT if self.is_paused else signal.SIGSTOP)
            except OSError as exc:
                self.set_status(f"Could not toggle pause: {exc}")
                return
            if self.is_paused:
                if self.playback_paused_at is not None:
                    self.playback_paused_total += time.monotonic() - self.playback_paused_at
                self.playback_paused_at = None
                self.is_paused = False
            else:
                self.playback_paused_at = time.monotonic()
                self.is_paused = True
            self.update_mpris()
            self.refresh_details()
            title = self.now_playing.title if self.now_playing else "track"
            self.set_status(f"{'Paused' if self.is_paused else 'Resumed'} {title}.")

        async def action_next_track(self) -> None:
            if not self.tracks:
                self.set_status("No playlist loaded for next track.")
                return
            index = self.now_playing_index if self.now_playing_index is not None else self.query_one("#table", DataTable).cursor_row
            await self.play_track_at((index + 1) % len(self.tracks))

        async def action_previous_track(self) -> None:
            if not self.tracks:
                self.set_status("No playlist loaded for previous track.")
                return
            index = self.now_playing_index if self.now_playing_index is not None else self.query_one("#table", DataTable).cursor_row
            await self.play_track_at((index - 1) % len(self.tracks))

        def action_toggle_favorite(self) -> None:
            track = self.selected_track() or self.now_playing
            if not track:
                self.set_status("Select or play a track before adding a favourite.")
                return
            added = self.toggle_favorite_track(track)
            self.set_status(f"{'Added to' if added else 'Removed from'} favourites: {track.title}")

        def action_show_recent(self) -> None:
            self.set_tracks("Recently Played", self.recent_tracks)
            self.query_one("#table", DataTable).focus()

        def action_show_favorites(self) -> None:
            self.set_tracks("Favourite Songs", self.favorite_tracks)
            self.query_one("#table", DataTable).focus()

        def action_quit(self) -> None:
            self.stop_player()
            self.exit()

        def call_from_thread(self, callback, *args):
            """Helper to call an app method safely from the MPRIS/GLib thread."""
            import inspect
            if inspect.iscoroutinefunction(callback):
                asyncio.run_coroutine_threadsafe(callback(*args), self._loop)
            else:
                self.call_next(callback, *args)

        def update_mpris(self, status: str | None = None):
            if not self.mpris:
                return
            mpris_status = "Stopped"
            if self.player_process and self.player_process.poll() is None:
                mpris_status = "Paused" if self.is_paused else "Playing"
            elif status:
                mpris_status = status
            self.mpris.update_state(self.now_playing, mpris_status)

        def action_toggle_help(self) -> None:
            self.help_visible = not self.help_visible
            self.refresh_details()
            self.set_status("Help shown." if self.help_visible else "Help hidden.")

        def action_toggle_lyrics_mode(self) -> None:
            self.lyrics_mode = "extended" if self.lyrics_mode == "karaoke" else "karaoke"
            self.refresh_sidebar()
            if self.current_view == "Lyrics":
                self.query_one("#lyrics_view", Static).update(self.render_lyrics_view())
            self.set_status(f"Lyrics mode: {self.lyrics_mode.title()}")

    CliMusicTui(client).run()


def interactive(client: VeromeClient) -> None:
    render_home()
    while True:
        try:
            line = input(color("IRIS ", CYAN) + color("❯ ", MAGENTA)).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        command, _, value = line.partition(" ")
        command = command.lower()
        value = value.strip()
        try:
            if command in {"quit", "exit", "q"}:
                return
            if command in {"help", "h", "?"}:
                render_home()
                continue
            if command in {"clear", "cls"}:
                render_home()
                continue
            if command in {"search", "s"} and value:
                print_tracks(search_tracks(client, value), 10)
            elif command in {"play", "p"} and value:
                track = choose_track(search_tracks(client, value, "songs"))
                if track:
                    add_recent_track(track)
                    play_track(client, track)
            elif command in {"favorite", "favourite", "fav", "f"} and value:
                track = choose_track(search_tracks(client, value, "songs"))
                if track:
                    added = toggle_favorite_saved(track)
                    print(color(f"{'Added to' if added else 'Removed from'} favourites: {track.title}", GREEN))
            elif command in {"favorites", "favourites", "favs"}:
                print_tracks(load_library()["favorites"], 10)
            elif command in {"recent", "history"}:
                print_tracks(load_library()["recent"], 10)
            elif command in {"lyrics", "l"} and value:
                track = choose_track(search_tracks(client, value, "songs"))
                if track:
                    artist = track.artists.split(", ", maxsplit=1)[0]
                    panel("Lyrics", client.lyrics(track.title, artist).splitlines())
            elif command in {"radio", "r"} and value:
                track = choose_track(search_tracks(client, value, "songs"))
                if track:
                    print_tracks(client.radio(track.playback_id), 10)
            elif command in {"trending", "t"}:
                print_tracks(client.trending(value.upper() or "US"), 10)
            else:
                print(color("Unknown command or missing search text. Type help for commands.", YELLOW))
        except CliMusicError as exc:
            print(color(f"Error: {exc}", RED), file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="iris",
        description="IRIS TUI music search, lyrics, radio, and playback powered by Verome API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            f"""
            Examples:
              python3 iris.py search "Blinding Lights"
              python3 iris.py play "Blinding Lights" --first
              python3 iris.py lyrics "Blinding Lights" --first
              python3 iris.py radio "Blinding Lights" --first

            Playback needs mpv, ffplay, or vlc. Override with {PLAYER_ENV}.
            """
        ).strip(),
    )
    parser.add_argument("--api", default=os.environ.get("VEROME_API", DEFAULT_BASE_URL), help="Verome API base URL")
    parser.add_argument("--timeout", type=int, default=20, help="API timeout in seconds")
    subparsers = parser.add_subparsers(dest="command")

    search = subparsers.add_parser("search", help="Search for music")
    search.add_argument("query")
    search.add_argument("--filter", default="songs", choices=("songs", "albums", "artists"))
    search.add_argument("--limit", type=int, default=10)
    search.set_defaults(func=command_search)

    play = subparsers.add_parser("play", help="Search and play a track")
    play.add_argument("query", nargs="?", help="Search query")
    play.add_argument("--video-id", help="Play a known YouTube video ID directly")
    play.add_argument("--first", action="store_true", help="Play the first result without prompting")
    play.set_defaults(func=command_play)

    lyrics = subparsers.add_parser("lyrics", help="Find synced/plain lyrics")
    lyrics.add_argument("query", nargs="?", help="Search query")
    lyrics.add_argument("--title", help="Exact title for lyrics lookup")
    lyrics.add_argument("--artist", help="Exact artist for lyrics lookup")
    lyrics.add_argument("--first", action="store_true", help="Use the first search result without prompting")
    lyrics.set_defaults(func=command_lyrics)

    radio = subparsers.add_parser("radio", help="Generate a radio mix from a song")
    radio.add_argument("query", nargs="?", help="Search query")
    radio.add_argument("--video-id", help="Use a known YouTube video ID directly")
    radio.add_argument("--first", action="store_true", help="Use the first search result without prompting")
    radio.add_argument("--limit", type=int, default=20)
    radio.set_defaults(func=command_radio)

    trending = subparsers.add_parser("trending", help="Show trending music")
    trending.add_argument("country", nargs="?", default="US", help="Country code, for example US or IN")
    trending.add_argument("--limit", type=int, default=20)
    trending.set_defaults(func=command_trending)

    recent = subparsers.add_parser("recent", help="Show recently played songs")
    recent.add_argument("--limit", type=int, default=20)
    recent.set_defaults(func=command_recent)

    favorites = subparsers.add_parser("favorites", aliases=["favourites"], help="Show or edit favourite songs")
    favorites.add_argument("--limit", type=int, default=20)
    favorites.add_argument("--add", help="Search for a song and toggle it in favourites")
    favorites.add_argument("--first", action="store_true", help="Use the first search result when adding")
    favorites.set_defaults(func=command_favorites)

    subparsers.add_parser("tui", help="Open full-screen Textual interface")
    subparsers.add_parser("shell", help="Open legacy prompt shell")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    client = VeromeClient(args.api, args.timeout)

    try:
        if args.command == "tui" or not args.command:
            run_tui(client)
        elif args.command == "shell":
            interactive(client)
        else:
            args.func(client, args)
    except CliMusicError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print()
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
