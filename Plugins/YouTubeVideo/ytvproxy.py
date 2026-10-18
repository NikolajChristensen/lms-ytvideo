#!/usr/bin/env python3
"""Local helper for the YouTube Video plugin for Lyrion Music Server.

Lists the signed-in user's YouTube feeds (home, subscriptions, Watch Later,
liked videos, playlists, history) and streams the audio of a video to LMS as
constant-bitrate MP3, with seeking.

Both jobs go through yt-dlp, used as a Python package: its YouTube feed
extractors (":ytsubs", ":ytrec", ...) are maintained upstream, so a YouTube
page change is fixed by updating yt-dlp, which this helper does by itself.

Listens on 127.0.0.1 only. LMS is the only client: players never connect here
(the protocol handler's canDirectStream is 0), and the settings page reaches
the helper through LMS's own /plugins/YouTubeVideo/proxy/ routes.
"""
import argparse
import hashlib
import io
import json
import logging
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VERSION = "0.2.0"

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
# Set from --data-dir (LMS cache dir) in main(); everything we download or
# write lives under DATA_DIR so it survives plugin updates.
DATA_DIR = None
BIN_DIR = None
# The YouTube Music plugin keeps ffmpeg/node here; reused when present so a
# second copy isn't downloaded. Never required.
MUSIC_BIN_DIR = None

NODE_MIN_VERSION = (22, 0, 0)
NODE_DIST_VERSION = "22.23.2"

# ── Audio output ─────────────────────────────────────────────────────────────

# Constant bitrate, so a byte offset maps straight to a time (seeking). Plenty
# for speech and fine for music videos; keep in sync with ProtocolHandler.pm.
MP3_KBPS = 128
MP3_BYTES_PER_SEC = MP3_KBPS * 1000 // 8

_AUDIO = {"codec": "libmp3lame", "format": "mp3", "mime": "audio/mpeg"}

# At most this many ffmpeg streams at once. Each is a CDN download plus an MP3
# encoder; the user's server may be a 1 CPU / 1 GB container. LMS opens the
# next track's stream shortly before the current one ends, so 2 per player.
_stream_slots = threading.BoundedSemaphore(3)
# At most this many yt-dlp extractions at once (each may run node).
_extract_slots = threading.BoundedSemaphore(2)

_active_streams = 0
_PORT = 9877
_active_lock = threading.Lock()

# ── Paths and tools ──────────────────────────────────────────────────────────

def _set_dirs(data_dir):
    global DATA_DIR, BIN_DIR, MUSIC_BIN_DIR
    DATA_DIR = os.path.join(data_dir, "YouTubeVideo")
    BIN_DIR = os.path.join(DATA_DIR, "Bin")
    MUSIC_BIN_DIR = os.path.join(data_dir, "YouTubeMusic", "Bin")
    os.makedirs(BIN_DIR, exist_ok=True)


def _version_tuple(text):
    return tuple(int(x) for x in re.findall(r"\d+", text or "")[:3])


def _node_version(path):
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10).stdout
        return _version_tuple(out)
    except Exception:
        return ()


_node_path = None

def _find_node():
    """A Node.js >= 22 binary: override, ours, the music plugin's, system."""
    global _node_path
    if _node_path and os.path.exists(_node_path):
        return _node_path
    exe = "node.exe" if os.name == "nt" else "node"
    candidates = [
        os.environ.get("YTV_NODE_OVERRIDE"),
        os.path.join(BIN_DIR, "node22", exe),
        os.path.join(MUSIC_BIN_DIR, "node22", exe),
        shutil.which("node"),
        "/usr/bin/node", "/usr/local/bin/node",
    ]
    for c in candidates:
        if c and os.path.isfile(c) and _node_version(c) >= NODE_MIN_VERSION:
            _node_path = c
            return c
    return None


def _find_ffmpeg():
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    for c in (os.environ.get("YTV_FFMPEG_OVERRIDE"),
              os.path.join(BIN_DIR, exe),
              os.path.join(MUSIC_BIN_DIR, exe),
              shutil.which("ffmpeg"),
              "/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"):
        if c and os.path.isfile(c):
            return c
    return None


def _detect_codec():
    """MP3 when ffmpeg has libmp3lame; AAC otherwise (piCorePlayer's
    pcp-ffmpeg lacks it). AAC streams aren't seekable."""
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        return
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=15).stdout
    except Exception as e:
        logging.warning("ffmpeg encoder check failed: %s", e)
        return
    if "libmp3lame" not in out:
        _AUDIO.update(codec="aac", format="adts", mime="audio/aac")
        logging.warning("ffmpeg has no libmp3lame; streaming AAC (no seeking)")


def _platform_node_asset():
    machine = platform.machine().lower()
    system = platform.system().lower()
    v = NODE_DIST_VERSION
    if system == "darwin":
        return f"node-v{v}-darwin-{'arm64' if machine in ('arm64', 'aarch64') else 'x64'}.tar.gz"
    if os.name == "nt":
        return f"node-v{v}-win-x64.zip"
    if machine in ("aarch64", "arm64"):
        return f"node-v{v}-linux-arm64.tar.xz"
    if machine.startswith("armv7") or machine == "armhf":
        return f"node-v{v}-linux-armv7l.tar.xz"
    return f"node-v{v}-linux-x64.tar.xz"


def download_node():
    """Fetch Node.js 22 into BIN_DIR/node22 (only when none is found)."""
    name = _platform_node_asset()
    url = f"https://nodejs.org/dist/v{NODE_DIST_VERSION}/{name}"
    logging.warning("No Node.js 22+ found; downloading %s", url)
    dest_dir = os.path.join(BIN_DIR, "node22")
    os.makedirs(dest_dir, exist_ok=True)
    exe = "node.exe" if os.name == "nt" else "node"
    tmp = os.path.join(dest_dir, exe + ".tmp")
    with urllib.request.urlopen(url, timeout=300) as resp:
        data = resp.read()
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            member = next(n for n in zf.namelist() if n.endswith("/node.exe"))
            with zf.open(member) as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst)
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
            member = next(m for m in tf.getmembers() if m.name.endswith("/bin/node"))
            with tf.extractfile(member) as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst)
    os.chmod(tmp, 0o755)
    if _node_version(tmp) < NODE_MIN_VERSION:
        os.remove(tmp)
        raise RuntimeError("downloaded Node.js does not run on this system")
    os.replace(tmp, os.path.join(dest_dir, exe))
    return os.path.join(dest_dir, exe)

# ── yt-dlp (own copy, kept up to date) ───────────────────────────────────────

_ytdlp_source = "none"   # "own" | "system" | "none"
_restart_pending = False


def _pypi_wheel(project, version=None):
    """(version, url, sha256) of a project's pure-Python wheel on PyPI."""
    api = f"https://pypi.org/pypi/{project}/{version}/json" if version else f"https://pypi.org/pypi/{project}/json"
    with urllib.request.urlopen(api, timeout=30) as resp:
        meta = json.loads(resp.read())
    for f in meta["urls"]:
        if f["packagetype"] == "bdist_wheel" and f["filename"].endswith("py3-none-any.whl"):
            return meta["info"]["version"], f["url"], f["digests"]["sha256"]
    raise RuntimeError(f"no wheel for {project} on PyPI")


def _install_wheel(project, version=None):
    """Download a wheel, check its SHA-256 and unpack it into BIN_DIR,
    replacing the package directory as a whole. Returns (version, wheel)."""
    ver, url, sha = _pypi_wheel(project, version)
    with urllib.request.urlopen(url, timeout=300) as resp:
        data = resp.read()
    if hashlib.sha256(data).hexdigest() != sha:
        raise RuntimeError(f"{project} {ver}: checksum mismatch")
    pkg = project.replace("-", "_")
    staging = os.path.join(BIN_DIR, f".staging-{pkg}")
    shutil.rmtree(staging, ignore_errors=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        zf.extractall(staging)
        names = zf.namelist()
    for entry in os.listdir(staging):
        if entry == pkg or entry.startswith(f"{pkg}-") and entry.endswith(".dist-info"):
            target = os.path.join(BIN_DIR, entry)
            old = target + ".old"
            shutil.rmtree(old, ignore_errors=True)
            if os.path.exists(target):
                os.replace(target, old)
            os.replace(os.path.join(staging, entry), target)
            shutil.rmtree(old, ignore_errors=True)
    shutil.rmtree(staging, ignore_errors=True)
    # Older .dist-info dirs of the same project
    for entry in os.listdir(BIN_DIR):
        if entry.startswith(f"{pkg}-") and entry.endswith(".dist-info") and entry != f"{pkg}-{ver}.dist-info":
            shutil.rmtree(os.path.join(BIN_DIR, entry), ignore_errors=True)
    return ver, names


def _ejs_pin(ytdlp_dir):
    """The yt-dlp-ejs version this yt-dlp wants (Requires-Dist pin)."""
    for entry in os.listdir(ytdlp_dir):
        if entry.startswith("yt_dlp-") and entry.endswith(".dist-info"):
            try:
                with open(os.path.join(ytdlp_dir, entry, "METADATA"), encoding="utf-8") as f:
                    m = re.search(r"^Requires-Dist: yt-dlp-ejs==([\w.]+)", f.read(), re.M)
                    return m.group(1) if m else None
            except OSError:
                return None
    return None


def _own_ytdlp_version():
    for entry in os.listdir(BIN_DIR):
        m = re.match(r"yt_dlp-([\w.]+)\.dist-info$", entry)
        if m and os.path.isdir(os.path.join(BIN_DIR, "yt_dlp")):
            return m.group(1)
    return None


def install_ytdlp():
    """Install or update our own yt-dlp + its pinned yt-dlp-ejs. Returns the
    new version, or None when already up to date."""
    latest, _, _ = _pypi_wheel("yt-dlp")
    have = _own_ytdlp_version()
    if have and _version_tuple(have) >= _version_tuple(latest) and _have_ejs():
        return None
    logging.warning("Installing yt-dlp %s (had %s)", latest, have or "none")
    ver, _ = _install_wheel("yt-dlp", latest)
    pin = _ejs_pin(BIN_DIR)
    try:
        _install_wheel("yt-dlp-ejs", pin)
    except Exception as e:
        logging.warning("yt-dlp-ejs install failed (%s); some videos may not play", e)
    return ver


def _have_ejs():
    return os.path.isdir(os.path.join(BIN_DIR, "yt_dlp_ejs"))


def _load_ytdlp():
    """Import yt_dlp: our own copy (installed on first start), else a system
    one. Must run before anything imports yt_dlp."""
    global _ytdlp_source
    if not _own_ytdlp_version():
        try:
            install_ytdlp()
        except Exception as e:
            logging.warning("Could not download yt-dlp: %s", e)
    if _own_ytdlp_version():
        sys.path.insert(0, BIN_DIR)
    try:
        import yt_dlp  # noqa: F401
        _ytdlp_source = "own" if os.path.dirname(os.path.dirname(yt_dlp.__file__)) == BIN_DIR else "system"
        logging.warning("yt-dlp %s (%s copy)", yt_dlp.version.__version__, _ytdlp_source)
        return True
    except ImportError:
        logging.error("yt-dlp not available: download failed and no system copy")
        return False


def _update_loop():
    """Check PyPI for a newer yt-dlp a minute after start and then daily;
    YouTube changes break old versions. A new version is loaded by restarting
    this process (exec) once nothing is streaming."""
    global _restart_pending
    time.sleep(60)
    while True:
        if _ytdlp_source != "system":
            try:
                if install_ytdlp():
                    _restart_pending = True
            except Exception as e:
                logging.warning("yt-dlp update check failed: %s", e)
        time.sleep(24 * 3600)


def _restart_when_idle():
    while True:
        time.sleep(30)
        with _active_lock:
            idle = _active_streams == 0
        if _restart_pending and idle:
            logging.warning("Restarting helper to load the new yt-dlp")
            if _auth.get("raw"):
                os.environ["YTV_HEADERS"] = _auth["raw"]   # keep the sign-in
            logging.shutdown()
            os.execv(sys.executable, [sys.executable] + sys.argv)

# ── Sign-in ──────────────────────────────────────────────────────────────────

_auth = {"cookie": None, "authuser": "0", "pageid": None, "raw": None}
# Set when YouTube stops accepting the cookie (see _YdlLogger). YouTube then
# answers as if logged out: feeds come back empty rather than with an error.
_auth_rejected = threading.Event()
_auth_lock = threading.Lock()


def _parse_curl_command(raw):
    """cookie/x-goog-authuser/x-goog-pageid from a pasted 'Copy as cURL'."""
    s = raw.strip()
    if not s.lower().startswith("curl"):
        return None
    s = s.replace('^"', '"').replace("^\n", " ")
    s = re.sub(r"\n\s+", " ", s).replace("^%^3D", "=")
    wanted = {"cookie": None, "x-goog-authuser": None, "x-goog-pageid": None}
    for q in ('"', "'"):
        for hdr in re.findall(r"-H\s+%s([^%s]+)%s" % (q, q, q), s):
            m = re.match(r"^([A-Za-z0-9_-]+):\s*(.+)$", hdr)
            if m and m.group(1).lower() in wanted and wanted[m.group(1).lower()] is None:
                wanted[m.group(1).lower()] = m.group(2).strip()
        if not wanted["cookie"]:
            m = re.search(r"-b\s+%s([^%s]+)%s" % (q, q, q), s)
            if m:
                wanted["cookie"] = m.group(1).strip()
    return wanted if wanted["cookie"] else None


def normalize_headers(raw):
    """Find cookie, x-goog-authuser and x-goog-pageid in whatever was pasted:
    "Name: value" lines (Firefox raw, older Chrome), name and value on
    separate lines (current Chrome DevTools table, with :authority etc.), or
    a curl command. Same formats the YouTube Music plugin accepts."""
    raw = raw or ""
    wanted = _parse_curl_command(raw)
    if wanted is None:
        wanted = {"cookie": None, "x-goog-authuser": None, "x-goog-pageid": None}
        pending = None
        for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            line = line.strip()
            if not line:
                continue
            if pending is not None:
                if pending in wanted and wanted[pending] is None:
                    wanted[pending] = line
                pending = None
                continue
            m = re.match(r"^(:?[A-Za-z0-9-]+):(?:\s+(.*))?$", line)
            if m and m.group(2):
                name = m.group(1).lower()
                if name in wanted and wanted[name] is None:
                    wanted[name] = m.group(2).strip()
            elif m or re.match(r"^:?[A-Za-z0-9-]+$", line):
                pending = (m.group(1) if m else line).lower()
        # A bare cookie string ("a=b; c=d") pasted on its own
        if not wanted["cookie"] and "SAPISID=" in raw and "\n" not in raw.strip():
            wanted["cookie"] = raw.strip()
    return {
        "cookie": wanted["cookie"],
        "authuser": (wanted.get("x-goog-authuser") or "0").strip(),
        "pageid": wanted.get("x-goog-pageid"),
    }


def _cookie_file_path():
    return os.path.join(DATA_DIR, "cookies.txt")


def _write_cookie_file(cookie):
    """Netscape cookie file for yt-dlp. Google's sign-in cookies are set for
    .youtube.com, so the music.youtube.com cookie works on www.youtube.com."""
    path = _cookie_file_path()
    expiry = int(time.time()) + 365 * 86400
    lines = ["# Netscape HTTP Cookie File", ""]
    for part in cookie.split(";"):
        name, sep, value = part.strip().partition("=")
        if not sep or not name:
            continue
        lines.append("\t".join([".youtube.com", "TRUE", "/", "TRUE", str(expiry), name, value]))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")


def set_auth(raw):
    parsed = normalize_headers(raw)
    cookie = parsed["cookie"]
    signed_in = bool(cookie and re.search(r"(?:^|;\s*)(?:__Secure-3PAPISID|SAPISID)=", cookie))
    with _auth_lock:
        _auth_rejected.clear()
        if signed_in:
            _write_cookie_file(cookie)
            _auth.update(parsed, raw=raw)
        else:
            _auth.update(cookie=None, authuser="0", pageid=None, raw=None)
            try:
                os.remove(_cookie_file_path())
            except OSError:
                pass
        _close_ydl("feed")
        _close_ydl("stream_auth")
    _STABLE.clear()
    logging.warning("Sign-in %s%s", "set" if signed_in else "cleared",
                    " (brand account)" if parsed.get("pageid") else "")
    return signed_in


def signed_in():
    return bool(_auth["cookie"])

# ── yt-dlp instances ─────────────────────────────────────────────────────────

_ydls = {}
_ydl_locks = {"feed": threading.Lock(), "stream": threading.Lock(), "stream_auth": threading.Lock()}


def _ydl_opts(kind):
    node = _find_node()
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": 15,
        "retries": 3,
        "extractor_retries": 2,
        "cachedir": os.path.join(DATA_DIR, "ytdlp_cache"),
        "js_runtimes": {"node": {"path": node}} if node else {"deno": {}},
        "logger": _YdlLogger(),
    }
    if kind == "feed":
        opts.update(extract_flat="in_playlist", skip_download=True)
    else:
        # A direct (non-HLS) audio stream that ffmpeg can open and seek in.
        opts.update(format="bestaudio[protocol^=http][acodec!=none]/bestaudio/best[protocol^=http]")
    if kind != "stream" and _auth["cookie"]:
        opts["cookiefile"] = _cookie_file_path()
        if _auth.get("authuser") not in (None, "", "0"):
            opts["http_headers"] = {"X-Goog-AuthUser": _auth["authuser"]}
    return opts


class _YdlLogger:
    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        if "cookies are no longer valid" in msg:
            if not _auth_rejected.is_set():
                logging.warning("YouTube no longer accepts the sign-in cookie (rotated or signed out)")
            _auth_rejected.set()
            return
        logging.info("yt-dlp: %s", msg)

    def error(self, msg):
        logging.warning("yt-dlp: %s", msg)


def _get_ydl(kind):
    import yt_dlp
    ydl = _ydls.get(kind)
    if ydl is None:
        ydl = _ydls[kind] = yt_dlp.YoutubeDL(_ydl_opts(kind))
    return ydl


def _close_ydl(kind):
    ydl = _ydls.pop(kind, None)
    if ydl is not None:
        try:
            ydl.close()
        except Exception:
            pass

# ── Lists ────────────────────────────────────────────────────────────────────

FEEDS = {
    # name: (yt-dlp URL, max items, seconds kept). Watch Later and Liked
    # videos are the playlists WL and LL (see get_playlist).
    "home":          (":ytrec", 60, 300),
    "subscriptions": (":ytsubs", 100, 300),
    "history":       (":ythis", 100, 300),
    "playlists":     ("https://www.youtube.com/feed/playlists", 200, 600),
}

# Lists are kept for a while. LMS menus navigate by position and fetch the
# list again on each step, and YouTube returns home/subscriptions in a
# different order on every request, so without this a tap could open a
# different video than the one tapped. Errors aren't kept; a new sign-in
# clears it.
_STABLE = {}
_STABLE_TTL = 600

# Metadata of everything listed, so the queue and Now Playing have title,
# channel and length straight away.
_KNOWN = {}

_show_shorts = False


def _thumb(video_id):
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


def _best_thumb(entry):
    thumbs = [t for t in (entry.get("thumbnails") or []) if t.get("url")]
    if not thumbs:
        return ""
    # Something around 480 px wide: big enough for Material, small for a Radio
    thumbs.sort(key=lambda t: abs((t.get("width") or 480) - 480))
    return thumbs[0]["url"]


def _is_unwanted(entry):
    url = entry.get("url") or ""
    if not _show_shorts and "/shorts/" in url:
        return True
    if entry.get("live_status") in ("is_live", "is_upcoming"):
        return True
    if entry.get("availability") in ("subscriber_only", "premium_only"):
        return True
    title = entry.get("title") or ""
    if title in ("[Private video]", "[Deleted video]"):
        return True
    return False


def _entry_item(entry):
    url = entry.get("url") or ""
    ie = entry.get("ie_key") or ""
    vid = entry.get("id")
    if _is_unwanted(entry):
        return None
    if ie == "Youtube" and vid:
        item = {
            "type": "video",
            "id": vid,
            "title": entry.get("title") or vid,
            "channel": entry.get("channel") or entry.get("uploader") or "",
            "duration": int(entry["duration"]) if entry.get("duration") else 0,
            "thumbnail": _thumb(vid),
        }
        _KNOWN[vid] = item
        return item
    m = re.search(r"[?&]list=([A-Za-z0-9_-]+)", url)
    # Mixes (RD...) are endless, generated lists that can't be opened as a
    # playlist; leave them out.
    if ie == "YoutubeTab" and m and not m.group(1).startswith("RD"):
        return {
            "type": "playlist",
            "id": m.group(1),
            "title": entry.get("title") or m.group(1),
            "channel": entry.get("channel") or entry.get("uploader") or "",
            "count": entry.get("playlist_count") or 0,
            "thumbnail": _best_thumb(entry),
        }
    return None


def _auth_error(msg):
    msg = str(msg)
    return bool(re.search(r"cookies|sign in|login required|log in|not logged", msg, re.I))


def _flat_list(url, limit, kind="feed"):
    lock = _ydl_locks[kind]
    with lock:
        ydl = _get_ydl(kind)
        ydl.params["playlistend"] = limit
        info = ydl.extract_info(url, download=False, process=True)
    items = []
    for entry in (info or {}).get("entries") or []:
        if entry:
            item = _entry_item(entry)
            if item:
                items.append(item)
    return {"title": (info or {}).get("title") or "", "items": items}


def _listing(key, fetch, refresh=False, ttl=_STABLE_TTL):
    entry = _STABLE.get(key)
    if entry and not refresh and time.time() - entry[0] < ttl:
        return entry[1]
    if _auth_rejected.is_set() and signed_in():
        return {"error": "auth_expired"}
    try:
        value = fetch()
    except Exception as e:
        logging.warning("%s failed: %s", key, e)
        if _auth_error(e):
            return {"error": "not_authenticated" if not signed_in() else "auth_expired"}
        return {"error": re.sub(r"^ERROR:\s*", "", str(e))[:300]}
    if _auth_rejected.is_set() and signed_in():
        return {"error": "auth_expired"}
    if not value["items"]:
        return value               # not kept: may be a passing hiccup
    _STABLE[key] = (time.time(), value)
    if len(_STABLE) > 100:
        for k, _ in sorted(_STABLE.items(), key=lambda kv: kv[1][0])[:30]:
            _STABLE.pop(k, None)
    return value


def get_feed(name, refresh=False):
    url, limit, ttl = FEEDS[name]
    if not signed_in():
        return {"error": "not_authenticated"}
    return _listing(f"feed:{name}", lambda: _flat_list(url, limit), refresh, ttl)


def get_playlist(list_id, refresh=False):
    if list_id in ("WL", "LL") and not signed_in():
        return {"error": "not_authenticated"}
    url = f"https://www.youtube.com/playlist?list={list_id}"
    return _listing(f"playlist:{list_id}", lambda: _flat_list(url, 500), refresh)

# ── Stream URLs ──────────────────────────────────────────────────────────────

_RESOLVED = {}          # video_id -> (time, info dict)
_resolve_locks = {}     # video_id -> Lock, so parallel callers share one extraction
_resolve_guard = threading.Lock()


def _url_expiry(url):
    m = re.search(r"[?&]expire=(\d+)", url or "")
    return int(m.group(1)) if m else time.time() + 3600


def _extract_stream(video_id, use_auth):
    kind = "stream_auth" if use_auth else "stream"
    with _extract_slots, _ydl_locks[kind]:
        info = _get_ydl(kind).extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    if not info or not info.get("url"):
        raise RuntimeError("no audio format")
    return info


def resolve(video_id, fresh=False):
    """Audio URL plus metadata for a video, cached until shortly before the
    URL expires. Tries without the cookie first (the path yt-dlp is tested
    on most), then with it (age-restricted, members-only, private)."""
    with _resolve_guard:
        lock = _resolve_locks.setdefault(video_id, threading.Lock())
    with lock:
        cached = _RESOLVED.get(video_id)
        if cached and not fresh and _url_expiry(cached["url"]) - time.time() > 900:
            return cached
        t0 = time.time()
        info, err = None, None
        for use_auth in ((False, True) if signed_in() else (False,)):
            try:
                info = _extract_stream(video_id, use_auth)
                break
            except Exception as e:
                err = e
                logging.warning("Extract %s%s failed: %s", video_id, " (signed in)" if use_auth else "", e)
        if info is None:
            raise RuntimeError(re.sub(r"^ERROR:\s*", "", str(err)))
        res = {
            "url": info["url"],
            "http_headers": info.get("http_headers") or {},
            "title": info.get("title") or video_id,
            "channel": info.get("channel") or info.get("uploader") or "",
            "duration": int(info.get("duration") or 0),
            "thumbnail": _thumb(video_id),
            "is_live": bool(info.get("is_live")),
        }
        _RESOLVED[video_id] = res
        if len(_RESOLVED) > 200:
            for k, _ in sorted(_RESOLVED.items(), key=lambda kv: _url_expiry(kv[1]["url"]))[:50]:
                _RESOLVED.pop(k, None)
        logging.info("Resolved %s in %.1fs", video_id, time.time() - t0)
        return res


def _forget(video_id):
    _RESOLVED.pop(video_id, None)


def prefetch(video_id):
    """Resolve the next track's URL ahead of time. Only the URL - no audio
    is downloaded, so this costs one extraction and no disk or bandwidth."""
    if video_id in _RESOLVED:
        return "cached"
    threading.Thread(target=lambda: _quiet(resolve, video_id), daemon=True).start()
    return "started"


def _quiet(fn, *args):
    try:
        fn(*args)
    except Exception as e:
        logging.info("prefetch: %s", e)


def video_info(video_id):
    known = _KNOWN.get(video_id)
    if known and known.get("duration"):
        return known
    res = resolve(video_id)
    return {"type": "video", "id": video_id, **{k: res[k] for k in ("title", "channel", "duration", "thumbnail")}}

# ── Volume levelling ─────────────────────────────────────────────────────────

# Talks and podcasts differ a lot in loudness, between videos and inside them
# (a quiet guest, a loud intro). ffmpeg's dynaudnorm evens that out as it
# encodes: it looks at the level over a sliding window of about 15 s (31
# frames of 500 ms, centred, so it sees what is coming) and turns each part
# up or down towards a common level, smoothly. Almost no extra CPU.
#   targetrms - aim at an average level, not just at the peaks, so videos
#               end up at a similar loudness
#   maxgain   - never lift more than 6x (about +16 dB)
#   threshold - leave near-silence alone (pauses, room tone, hiss) instead
#               of lifting it
_levelling = True
LEVEL_OPTS = {"framelen": 500, "gausssize": 31, "peak": 0.9, "targetrms": 0.12,
              "maxgain": 6, "threshold": 0.004}
_level_filter = None       # set at start from what this ffmpeg supports


def _init_level_filter():
    """dynaudnorm with the options this ffmpeg knows (older builds lack
    some, and an unknown option would make every stream fail)."""
    global _level_filter
    try:
        out = subprocess.run([_find_ffmpeg(), "-hide_banner", "-h", "filter=dynaudnorm"],
                             capture_output=True, text=True, timeout=15).stdout
    except Exception:
        out = ""
    if "dynaudnorm" not in out:
        logging.warning("ffmpeg has no dynaudnorm filter; volume levelling unavailable")
        return
    known = {k: v for k, v in LEVEL_OPTS.items() if re.search(rf"^\s+{k}\s", out, re.M)}
    missing = set(LEVEL_OPTS) - set(known)
    if missing:
        logging.warning("ffmpeg's dynaudnorm lacks %s; levelling without them", ", ".join(sorted(missing)))
    _level_filter = "dynaudnorm=" + ":".join(f"{k}={v}" for k, v in known.items())

# ── Streaming ────────────────────────────────────────────────────────────────

def _ffmpeg_cmd(source_url, offset):
    cmd = [_find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin"]
    if source_url != "pipe:0":
        cmd += ["-rw_timeout", "60000000"]
    if offset > 0.5:
        cmd += ["-ss", f"{offset:.3f}"]
    cmd += ["-i", source_url, "-vn", "-sn", "-dn", "-map", "0:a:0",
            "-map_metadata", "-1", "-ac", "2", "-ar", "44100"]
    if _levelling and _level_filter:
        cmd += ["-af", _level_filter]
    if _AUDIO["format"] == "mp3":
        cmd += ["-codec:a", "libmp3lame", "-b:a", f"{MP3_KBPS}k",
                "-write_xing", "0", "-id3v2_version", "0", "-write_id3v1", "0", "-f", "mp3"]
    else:
        cmd += ["-codec:a", "aac", "-b:a", f"{MP3_KBPS}k", "-f", "adts"]
    cmd.append("pipe:1")
    return cmd


# YouTube's CDN throttles open-ended range requests ("bytes=N-") to about
# playback speed - 260 KB in 8 s in a test, against 10 MB in 3 s for a
# bounded range - and that's what ffmpeg sends, so seeking into a long video
# took 10-25 s and playback could stutter. ffmpeg therefore reads the audio
# from /src/<id> on this helper, which fetches it from the CDN in bounded
# chunks (as yt-dlp does), and also renews the URL there when it expires or
# the CDN answers 403, without ffmpeg noticing.
FIRST_CHUNK = 512 * 1024        # small first request: quick start after a seek
CHUNK = 8 * 1024 * 1024


def _content_length(res):
    m = re.search(r"[?&]clen=(\d+)", res["url"])
    if m:
        return int(m.group(1))
    req = urllib.request.Request(res["url"], method="HEAD", headers=res["http_headers"])
    with urllib.request.urlopen(req, timeout=20) as r:
        return int(r.headers.get("Content-Length") or 0)


def _cdn_chunks(video_id, start, end):
    """Yield the bytes start..end (inclusive) of the audio file, fetched in
    bounded ranges, retrying broken or refused requests."""
    pos, size, failures = start, FIRST_CHUNK, 0
    while pos <= end:
        res = resolve(video_id)
        last = min(end, pos + size - 1)
        headers = dict(res["http_headers"])
        headers["Range"] = f"bytes={pos}-{last}"
        try:
            with urllib.request.urlopen(urllib.request.Request(res["url"], headers=headers), timeout=30) as r:
                while True:
                    data = r.read(65536)
                    if not data:
                        break
                    pos += len(data)
                    failures = 0
                    yield data
            if pos <= last:
                raise IOError(f"short read at {pos}")
            size = CHUNK
        except urllib.error.HTTPError as e:
            failures += 1
            logging.warning("%s CDN HTTP %s at byte %d", video_id, e.code, pos)
            if failures > 3:
                raise
            _forget(video_id)      # expired/refused URL: resolve a new one
        except (OSError, IOError) as e:
            failures += 1
            logging.info("%s CDN read broke at byte %d: %s", video_id, pos, e)
            if failures > 3:
                raise
            time.sleep(1)


def _stop(*procs):
    """Stop and reap child processes. Their stdout is closed first: a
    process blocked writing to a pipe nobody reads never gets to handle
    SIGTERM, and a killed child that isn't waited for stays a zombie."""
    for p in procs:
        if p is None:
            continue
        for f in (p.stdout, p.stderr):
            try:
                if f:
                    f.close()
            except Exception:
                pass
        if p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=3)
            except Exception:
                try:
                    p.kill()
                    p.wait(timeout=5)
                except Exception:
                    pass


def _ytdlp_pipe_cmd(video_id):
    """yt-dlp downloading to stdout: the fallback when ffmpeg can't read the
    CDN URL itself (HLS-only videos, CDN refusing ffmpeg)."""
    cmd = [sys.executable, "-m", "yt_dlp", "--quiet", "--no-warnings", "--no-part",
           "--no-playlist", "--socket-timeout", "15", "--retries", "5",
           "--http-chunk-size", "10M", "-f", "bestaudio/best",
           "--cache-dir", os.path.join(DATA_DIR, "ytdlp_cache")]
    node = _find_node()
    if node:
        cmd += ["--js-runtimes", f"node:{node}"]
    if signed_in():
        cmd += ["--cookies", _cookie_file_path()]
    return cmd + ["-o", "-", f"https://www.youtube.com/watch?v={video_id}"]


def _pump(proc, counter):
    while True:
        chunk = proc.stdout.read(65536)
        if not chunk:
            return
        counter[0] += len(chunk)
        yield chunk


def stream_audio(video_id, start_byte=0):
    """Yield the encoded audio of a video from start_byte on, at the pace
    LMS reads it (no temp files). If the CDN connection breaks before the
    end, carry on from where it stopped with a fresh URL; if ffmpeg can't
    read the URL at all, let yt-dlp download it instead."""
    res = resolve(video_id)
    if res["is_live"]:
        raise RuntimeError("live streams are not supported")
    duration = res["duration"]
    sent = 0
    offset = start_byte / MP3_BYTES_PER_SEC

    def position():
        return offset + sent / MP3_BYTES_PER_SEC

    def finished():
        # Within 3 s (or 1 %) of the end counts as complete
        return bool(duration) and position() >= duration - max(3.0, duration * 0.01)

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (BIN_DIR if _ytdlp_source == "own" else "", env.get("PYTHONPATH", "")) if p)

    for attempt in range(4):
        use_pipe = attempt == 3 or (attempt > 0 and sent == 0)
        procs = []
        got = [0]
        try:
            if use_pipe:
                logging.warning("%s: streaming through yt-dlp download (attempt %d)", video_id, attempt + 1)
                dl = subprocess.Popen(_ytdlp_pipe_cmd(video_id), stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, env=env)
                procs.append(dl)
                ff = subprocess.Popen(_ffmpeg_cmd("pipe:0", position()), stdin=dl.stdout,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                dl.stdout.close()
            else:
                ff = subprocess.Popen(_ffmpeg_cmd(f"http://127.0.0.1:{_PORT}/src/{video_id}", position()),
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            procs.append(ff)
            for chunk in _pump(ff, got):
                sent += len(chunk)
                yield chunk
            err = ff.stderr.read().decode("utf-8", "replace").strip()
            try:
                ff.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        finally:
            _stop(*reversed(procs))
        if err:
            logging.warning("%s ffmpeg: %s", video_id, err[:400])
        if finished() or (not duration and got[0] and ff.returncode == 0):
            return
        if not duration and not got[0] and sent:
            return
        logging.warning("%s stopped at %.0fs of %ss (got %d bytes); retrying", video_id,
                        position(), duration or "?", got[0])
        if got[0] == 0:
            _forget(video_id)
    if not sent:
        raise RuntimeError(f"could not stream {video_id}")
    logging.warning("%s ended early at %.0fs of %ss", video_id, position(), duration or "?")

# ── HTTP ─────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"ytvproxy/{VERSION}"

    def log_message(self, fmt, *args):
        logging.debug(fmt, *args)

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        if path == "/auth":
            try:
                ok = set_auth(body)
            except Exception as e:
                logging.exception("auth")
                return self._json({"error": str(e)}, 500)
            return self._json({"signed_in": ok})
        if path == "/options":
            global _show_shorts, _levelling
            opts = json.loads(body or "{}")
            if "levelling" in opts:
                _levelling = bool(opts["levelling"])
            if "show_shorts" in opts and bool(opts["show_shorts"]) != _show_shorts:
                _show_shorts = bool(opts["show_shorts"])
                _STABLE.clear()
            return self._json({"ok": True})
        self._json({"error": "not found"}, 404)

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        refresh = q.get("refresh") == "1"
        try:
            if path == "/ping":
                return self._json({"ok": True, "version": VERSION})
            if path == "/status":
                return self._json(status())
            if path == "/codec":
                return self._json({"format": _AUDIO["format"], "kbps": MP3_KBPS})
            if path.startswith("/feed/"):
                name = path[len("/feed/"):]
                if name not in FEEDS:
                    return self._json({"error": "unknown feed"}, 404)
                return self._json(get_feed(name, refresh))
            m = re.match(r"^/(playlist|video|prefetch|stream|src)/([A-Za-z0-9_-]+)$", path)
            if m:
                what, ident = m.groups()
                if what == "playlist":
                    return self._json(get_playlist(ident, refresh))
                if what == "video":
                    return self._json(video_info(ident))
                if what == "prefetch":
                    return self._json({"status": prefetch(ident)})
                if what == "src":
                    return self._src(ident)
                return self._stream(ident)
            if path == "/update_ytdlp":
                return self._update()
            if path == "/quit":
                self._json({"ok": True})
                threading.Thread(target=lambda: (time.sleep(0.2), os._exit(0)), daemon=True).start()
                return
            self._json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            logging.exception("Error on %s", path)
            try:
                self._json({"error": re.sub(r"^ERROR:\s*", "", str(e))[:300]}, 500)
            except Exception:
                pass

    def _update(self):
        global _restart_pending
        if _ytdlp_source == "system":
            return self._json({"status": "error", "message": "using the system yt-dlp; update it with your package manager"})
        try:
            ver = install_ytdlp()
        except Exception as e:
            return self._json({"status": "error", "message": str(e)})
        if ver:
            _restart_pending = True
            return self._json({"status": "ok", "version": ver,
                               "message": f"yt-dlp {ver} installed; the helper restarts when nothing is playing"})
        import yt_dlp
        return self._json({"status": "ok", "version": yt_dlp.version.__version__, "message": "already up to date"})

    def _src(self, video_id):
        """The audio file of a video, seekable, for ffmpeg (see _cdn_chunks)."""
        res = resolve(video_id)
        size = _content_length(res)
        m = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
        start = int(m.group(1)) if m else 0
        end = int(m.group(2)) if m and m.group(2) else size - 1
        if size and start >= size:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        end = min(end, size - 1)
        self.send_response(206 if m else 200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if m:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for data in _cdn_chunks(video_id, start, end):
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass               # ffmpeg seeks by closing and reopening
        self.close_connection = True

    def _stream(self, video_id):
        global _active_streams
        m = re.match(r"bytes=(\d+)-", self.headers.get("Range", ""))
        start = int(m.group(1)) if m else 0
        if _AUDIO["format"] != "mp3":
            start = 0
        if not _stream_slots.acquire(timeout=30):
            logging.warning("Too many streams; refusing %s", video_id)
            return self._json({"error": "busy"}, 503)
        with _active_lock:
            _active_streams += 1
        logging.warning("Stream %s from %ds", video_id, start // MP3_BYTES_PER_SEC)
        gen = None
        try:
            # Headers straight away: resolving can take a while on a slow
            # CPU, and LMS gives up on a server that doesn't answer. Without
            # any audio after that, the empty body makes LMS skip the track.
            # The total length isn't known exactly, so Content-Range carries
            # only the start - the part LMS checks.
            self.send_response(206 if start else 200)
            self.send_header("Content-Type", _AUDIO["mime"])
            if start:
                self.send_header("Content-Range", f"bytes {start}-")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.flush()
            gen = stream_audio(video_id, start)
            try:
                for chunk in gen:
                    self.wfile.write(chunk)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                raise
            except Exception as e:
                logging.warning("Stream %s failed: %s", video_id, e)
        except (BrokenPipeError, ConnectionResetError):
            logging.info("LMS closed stream %s", video_id)
        finally:
            if gen is not None:
                gen.close()          # stops ffmpeg/yt-dlp
            with _active_lock:
                _active_streams -= 1
            _stream_slots.release()
            self.close_connection = True


def status():
    try:
        import yt_dlp
        ytv = yt_dlp.version.__version__
    except Exception:
        ytv = None
    return {
        "version": VERSION,
        "ytdlp": ytv,
        "ytdlp_source": _ytdlp_source,
        "ejs": _have_ejs() if _ytdlp_source == "own" else None,
        "node": _find_node(),
        "ffmpeg": _find_ffmpeg(),
        "codec": _AUDIO["format"],
        "signed_in": signed_in() and not _auth_rejected.is_set(),
        "sign_in_rejected": _auth_rejected.is_set(),
        "brand_account": bool(_auth.get("pageid")),
        "restart_pending": _restart_pending,
        "levelling": bool(_levelling and _level_filter),
    }

# ── Main ─────────────────────────────────────────────────────────────────────

def _watch_parent(ppid):
    """Exit when LMS goes away, so no orphan keeps the port."""
    while True:
        time.sleep(10)
        if os.name != "nt" and os.getppid() != ppid:
            logging.warning("LMS has gone; exiting")
            os._exit(0)


def _bind(bind, port):
    """Bind the port; if an old helper still holds it (LMS was killed
    without stopping it), ask that one to quit first."""
    for attempt in range(20):
        try:
            return ThreadingHTTPServer((bind, port), Handler)
        except OSError:
            if attempt == 0:
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/quit", timeout=3).read()
                except Exception:
                    pass
            time.sleep(0.5)
    raise SystemExit(f"port {port} is in use")


def main():
    global _show_shorts, _PORT, _levelling
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9877)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--data-dir", required=True, help="LMS cache directory")
    ap.add_argument("--log-file", default="")
    ap.add_argument("--log-level", default="WARNING")
    ap.add_argument("--ffmpeg", default="")
    ap.add_argument("--node", default="")
    ap.add_argument("--show-shorts", action="store_true")
    ap.add_argument("--no-levelling", action="store_true")
    args = ap.parse_args()

    _set_dirs(args.data_dir)
    handlers = [logging.StreamHandler(sys.stderr)]
    try:
        from logging.handlers import RotatingFileHandler
        handlers.append(RotatingFileHandler(args.log_file or os.path.join(DATA_DIR, "ytvproxy.log"),
                                            maxBytes=1_000_000, backupCount=1, encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.WARNING),
                        format="%(asctime)s [%(levelname)s] %(message)s", handlers=handlers, force=True)
    if args.ffmpeg:
        os.environ["YTV_FFMPEG_OVERRIDE"] = args.ffmpeg
    if args.node:
        os.environ["YTV_NODE_OVERRIDE"] = args.node
    _show_shorts = args.show_shorts
    _levelling = not args.no_levelling
    _PORT = args.port

    server = _bind(args.bind, args.port)
    server.daemon_threads = True
    logging.warning("ytvproxy %s listening on %s:%d", VERSION, args.bind, args.port)

    _load_ytdlp()
    if not _find_node():
        try:
            download_node()
        except Exception as e:
            logging.warning("Node.js download failed (%s); many videos won't play", e)
    if not _find_ffmpeg():
        logging.error("ffmpeg not found; install it (e.g. apt install ffmpeg)")
    _detect_codec()
    _init_level_filter()
    # Sign-in from LMS, passed in the environment (not visible in ps)
    if os.environ.get("YTV_HEADERS"):
        set_auth(os.environ.pop("YTV_HEADERS"))

    for target, a in ((_update_loop, ()), (_restart_when_idle, ()), (_watch_parent, (os.getppid(),))):
        threading.Thread(target=target, args=a, daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
