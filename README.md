# 🎵 B-Iris

B-Iris is a premium, full-screen terminal music TUI (Textual User Interface) designed for the modern terminal. Powered by the Verome API and YouTube Music, it brings a sleek, high-fidelity music experience directly to your command line.

Featuring a vibrant **hot pink visualizer**, seamless **caching**, and **MPRIS support**, B-Iris is built for audiophiles who live in the terminal.

---

## ✨ Features

- 🔍 **Universal Search**: Find any track, album, or artist on YouTube Music.
- 📻 **Smart Radio**: Generate instant radio mixes based on your favorite tracks.
- 🎤 **Lyrics Support**: Fetch synced or plain lyrics with a single keypress.
- 📊 **Trending Charts**: Stay updated with the latest music trends (US and global).
- 🎨 **Dynamic Visualizer**: Real-time terminal spectrum analyzer.
- ⚡ **Performance Optimized**: Background prefetching and caching for zero-lag playback.
- 🛠️ **MPRIS Integration**: Control your music from system media widgets (Waybar, Polybar, etc.).

---

## 🚀 Getting Started

Follow these steps to get B-Iris up and running on your system.

### 1. Prerequisites

Ensure you have the following installed on your system:
- **Python 3.10+**
- **One of these media players**:
  - `mpv` (Recommended for best performance and visualizer accuracy)
  - `ffplay` (Part of FFmpeg)
  - `vlc`

### 2. Installation

1.  **Clone the Repository**:
    ```bash
    git clone https://github.com/shivamkumar15/B-Iris.git
    cd B-Iris
    ```

2.  **Create a Virtual Environment** (Optional but recommended):
    ```bash
    python3 -m venv .venv
    source .venv/bin/activate
    ```

3.  **Install Dependencies**:
    ```bash
    pip install -r requirements.txt
    ```

---

## 🎮 How to Use

### Launching the Player

To start the full-screen TUI experience:
```bash
python iris.py
```

### Keybindings (TUI Mode)

| Key | Action |
| :--- | :--- |
| `/` | Focus search bar |
| `Enter` / `p` | Play selected track |
| `Space` | Toggle Play/Pause |
| `n` / `b` | Next / Previous track |
| `f` | Toggle Favorite |
| `r` | Start Radio from selection |
| `l` | View Lyrics |
| `t` | Show Trending tracks |
| `g` | View Recently played |
| `v` | View Favorites |
| `?` | Toggle Help |
| `q` | Quit |

---

## 🛠️ Advanced Configuration

B-Iris can be customized using environment variables:

| Variable | Description | Example |
| :--- | :--- | :--- |
| `IRIS_PLAYER` | Override the default media player | `export IRIS_PLAYER="mpv --no-video"` |
| `IRIS_YTDLP_ARGS` | Pass custom arguments to yt-dlp | `export IRIS_YTDLP_ARGS="--cookies /path/to/cookies.txt"` |
| `IRIS_YTDLP_COOKIES_BROWSER` | Auto-extract cookies from browser | `export IRIS_YTDLP_COOKIES_BROWSER="firefox"` |

---

## 📂 Data Storage

- **Library Data**: Favorites and history are saved to `~/.local/share/iris/library.json`.
- **Cache**: Stream URLs and thumbnails are cached in your system's temp directory.

---

## 🤝 Contributing

Contributions are welcome! Feel free to open issues or submit pull requests to improve B-Iris.

---

<p align="center">
  Made with ❤️ for the Terminal community.
</p>
