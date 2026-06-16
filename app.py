import os
import re
import time
import threading
import subprocess
import urllib.parse
import urllib.request
import yt_dlp
from flask import Flask, request, jsonify, render_template, Response, stream_with_context
from flask_cors import CORS

try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
except Exception:
    pass

app = Flask(__name__)
CORS(app)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# ══════════════════════════════════════════════════════
#  COOKIE ROTATION SYSTEM
#  Place cookie files as: cookies1.txt, cookies2.txt, ... cookies10.txt
#  Legacy cookies.txt is also supported as fallback
# ══════════════════════════════════════════════════════

_COOLDOWN_SECONDS = 300  # 5 min cooldown after a cookie gets blocked

class CookiePool:
    """
    Strict round-robin cookie rotation.

    Cookie files are loaded in this order:
      cookies.txt  →  cookies1.txt  →  cookies2.txt  →  ...  →  cookies10.txt

    Each request gets the NEXT cookie in the sequence.
    If a cookie gets a YouTube block error it is put on cooldown and the
    next available slot is used for that request only — the counter keeps
    moving forward so all cookies stay balanced.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._index = 0
        self._blocked_until = {}      # path -> unblock timestamp
        self._use_count = {}          # path -> total uses

    def _load_cookies(self):
        """Return ordered list of existing cookie files."""
        candidates = []
        # cookies.txt first (slot 0)
        legacy = os.path.join(_BASE_DIR, "cookies.txt")
        if os.path.isfile(legacy):
            candidates.append(legacy)
        # cookies1.txt … cookies10.txt
        for i in range(1, 11):
            p = os.path.join(_BASE_DIR, f"cookies{i}.txt")
            if os.path.isfile(p):
                candidates.append(p)
        return candidates

    def get_next(self):
        """
        Advance the round-robin counter and return the assigned cookie.
        Skips cookies currently on cooldown; falls back to the least-blocked
        one if every cookie is on cooldown.
        Returns None when no cookie files exist at all.
        """
        with self._lock:
            cookies = self._load_cookies()
            if not cookies:
                return None

            now = time.time()
            total = len(cookies)

            # Walk forward until we find an unblocked slot
            for _ in range(total):
                path = cookies[self._index % total]
                self._index = (self._index + 1) % total
                if now >= self._blocked_until.get(path, 0):
                    self._use_count[path] = self._use_count.get(path, 0) + 1
                    return path

            # Every cookie is on cooldown — use the one that unblocks soonest
            soonest = min(cookies, key=lambda p: self._blocked_until.get(p, 0))
            self._use_count[soonest] = self._use_count.get(soonest, 0) + 1
            return soonest

    def mark_blocked(self, cookie_path):
        """Put a cookie on cooldown."""
        with self._lock:
            self._blocked_until[cookie_path] = time.time() + _COOLDOWN_SECONDS
            print(f"[CookiePool] BLOCKED: {os.path.basename(cookie_path)} "
                  f"— cooldown {_COOLDOWN_SECONDS}s")

    def status(self):
        """Return list of dicts describing every cookie's current state."""
        cookies = self._load_cookies()
        now = time.time()
        out = []
        for p in cookies:
            remaining = max(0, self._blocked_until.get(p, 0) - now)
            out.append({
                "file":                  os.path.basename(p),
                "status":                "blocked" if remaining > 0 else "active",
                "cooldown_remaining_sec": int(remaining),
                "total_uses":            self._use_count.get(p, 0),
            })
        return out


_cookie_pool = CookiePool()


def normalize_url(link):
    link = link.strip()
    if link.startswith("https:/") and not link.startswith("https://"):
        link = "https://" + link[7:]
    elif link.startswith("http:/") and not link.startswith("http://"):
        link = "http://" + link[6:]
    elif not link.startswith("http"):
        link = "https://" + link
    return link


def _is_cookie_error(exc):
    """Detect if an exception is caused by YouTube bot/cookie block."""
    msg = str(exc).lower()
    return any(k in msg for k in [
        "sign in", "signin", "bot", "429", "too many requests",
        "confirm you're not a bot", "this video is unavailable",
        "blocked", "cookie", "captcha", "please sign in",
    ])


def _is_nsig_error(exc):
    """Detect YouTube n-challenge / signature solving failure."""
    msg = str(exc).lower()
    return any(k in msg for k in [
        "signature solving failed", "n challenge", "requested format is not available",
        "only images are available",
    ])


def get_ydl_opts(cookie_path=None, player_client=None):
    import shutil
    node_path = shutil.which("node") or "node"
    # Ensure node directory is in PATH so yt-dlp can find it
    node_dir = os.path.dirname(node_path)
    current_path = os.environ.get("PATH", "")
    if node_dir and node_dir not in current_path:
        os.environ["PATH"] = node_dir + ":" + current_path

    opts = {
        "quiet":                      True,
        "no_warnings":                True,
        "skip_download":              True,
        "noplaylist":                 True,
        "retries":                    3,
        "fragment_retries":           3,
        "skip_unavailable_fragments": True,
        "http_headers":               {"User-Agent": _USER_AGENT},
        "js_runtimes":                {"node": {"path": node_path}},
    }
    if cookie_path and os.path.isfile(cookie_path):
        opts["cookiefile"] = cookie_path
    if player_client:
        opts["extractor_args"] = {"youtube": {"player_client": [player_client]}}
    return opts


def _ydl_extract(opts, url):
    """Run yt-dlp extract_info and raise if result has no real formats."""
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    # Reject storyboard-only results (no real video/audio)
    fmts = info.get("formats", [])
    real = [f for f in fmts if f.get("ext") not in ("mhtml", None) and f.get("url")]
    if not real:
        raise RuntimeError("No downloadable formats found (storyboard only)")
    return info


def extract_info(url):
    """
    Extract info with smart fallback strategy:

    Round 1 — assigned cookie + default player (handles age-gated content)
    Round 2 — if n-challenge/sig fails: try all other cookies with default player
    Round 3 — mediaconnect client without cookie (works when JS solving fails)
    Round 4 — mediaconnect client with each cookie (final attempt)
    """
    assigned  = _cookie_pool.get_next()
    others    = [c for c in _cookie_pool._load_cookies() if c != assigned]
    last_exc  = None
    is_cookie = False
    is_nsig   = False

    # ── Round 1: assigned cookie, default player ──────────────────────
    try:
        info = _ydl_extract(get_ydl_opts(assigned), url)
        if assigned:
            print(f"[YDL] OK cookie={os.path.basename(assigned)}")
        return info
    except Exception as e:
        last_exc  = e
        is_cookie = bool(assigned and _is_cookie_error(e))
        is_nsig   = _is_nsig_error(e)
        if not (is_cookie or is_nsig):
            raise
        if is_cookie:
            _cookie_pool.mark_blocked(assigned)

    # ── Round 2: other cookies, default player (only on cookie error) ──
    if is_cookie:
        for c in others:
            try:
                info = _ydl_extract(get_ydl_opts(c), url)
                print(f"[YDL] OK cookie fallback={os.path.basename(c)}")
                return info
            except Exception as e:
                last_exc = e
                if _is_cookie_error(e):
                    _cookie_pool.mark_blocked(c)

    # ── Round 3: mediaconnect without cookie (bypasses n-challenge) ───
    try:
        info = _ydl_extract(get_ydl_opts(player_client="mediaconnect"), url)
        print("[YDL] OK mediaconnect no-cookie")
        return info
    except Exception as e:
        last_exc = e

    # ── Round 4: mediaconnect with each cookie ─────────────────────────
    for c in ([assigned] if assigned else []) + others:
        try:
            info = _ydl_extract(get_ydl_opts(c, player_client="mediaconnect"), url)
            print(f"[YDL] OK mediaconnect cookie={os.path.basename(c)}")
            return info
        except Exception as e:
            last_exc = e

    raise last_exc


def format_duration(seconds):
    if not seconds:
        return "N/A"
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def format_filesize(size):
    if not size:
        return None
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    elif size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    else:
        return f"{size / (1024 * 1024 * 1024):.2f} GB"


def build_format_entry(fmt):
    vcodec   = fmt.get("vcodec") or "none"
    acodec   = fmt.get("acodec") or "none"
    has_video = vcodec not in (None, "none")
    has_audio = acodec not in (None, "none")
    height   = fmt.get("height")
    width    = fmt.get("width")
    size     = fmt.get("filesize") or fmt.get("filesize_approx")
    return {
        "format_id":      fmt.get("format_id"),
        "ext":            fmt.get("ext"),
        "resolution":     fmt.get("resolution") or (
                          f"{width}x{height}" if width and height else "audio only"),
        "height":         height,
        "width":          width,
        "fps":            fmt.get("fps"),
        "vcodec":         vcodec,
        "acodec":         acodec,
        "abr":            fmt.get("abr") or 0,
        "vbr":            fmt.get("vbr"),
        "tbr":            fmt.get("tbr"),
        "filesize":       size,
        "filesize_human": format_filesize(size),
        "format_note":    fmt.get("format_note") or "",
        "has_video":      has_video,
        "has_audio":      has_audio,
        "url":            fmt.get("url"),
    }


def parse_formats(info):
    combined   = []
    video_only = []
    audio_only = []
    seen       = set()
    for fmt in info.get("formats", []):
        if not fmt.get("url"):
            continue
        fid = fmt.get("format_id")
        if fid in seen:
            continue
        seen.add(fid)
        entry     = build_format_entry(fmt)
        has_video = entry["has_video"]
        has_audio = entry["has_audio"]
        if has_video and has_audio:
            combined.append(entry)
        elif has_video:
            video_only.append(entry)
        elif has_audio:
            audio_only.append(entry)

    combined.sort(key=lambda x: x.get("height") or 0, reverse=True)
    video_only.sort(key=lambda x: x.get("height") or 0, reverse=True)
    audio_only.sort(key=lambda x: x.get("abr") or 0, reverse=True)
    return combined, video_only, audio_only


# ══════════════════════════════════════════════════════
#  FFMPEG STREAMING HELPERS
# ══════════════════════════════════════════════════════

_FFMPEG_HEADERS = (
    f"User-Agent: {_USER_AGENT}\r\n"
    "Referer: https://www.youtube.com/\r\n"
)


def _run_ffmpeg_to_tempfile(cmd, suffix):
    """Run ffmpeg writing to a temp file; return (path, size_bytes) or raise on failure."""
    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp.close()
    full_cmd = cmd + [tmp.name]
    result = subprocess.run(full_cmd, capture_output=True)
    if result.returncode != 0:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace")[:400])
    size = os.path.getsize(tmp.name)
    if size == 0:
        os.unlink(tmp.name)
        raise RuntimeError("ffmpeg produced empty output")
    return tmp.name, size


def _serve_tempfile(path, size, content_type, filename, delete_after=True):
    """Stream a temp file to the client with Content-Length, then delete it."""
    def generate():
        try:
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    yield chunk
        finally:
            if delete_after:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    return Response(
        stream_with_context(generate()),
        content_type=content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length":      str(size),
            "Accept-Ranges":       "bytes",
        },
    )


def _ffmpeg_merge_video_audio(video_url, audio_url, filename):
    """Merge separate video + audio streams → temp MP4 → serve with Content-Length."""
    cmd = [
        "ffmpeg", "-y",
        "-loglevel", "error",
        "-headers", _FFMPEG_HEADERS,
        "-i", video_url,
        "-headers", _FFMPEG_HEADERS,
        "-i", audio_url,
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "faststart",
        "-f", "mp4",
    ]
    path, size = _run_ffmpeg_to_tempfile(cmd, ".mp4")
    return _serve_tempfile(path, size, "video/mp4", filename)


def _ffmpeg_combined_video(video_url, filename):
    """Re-mux a combined stream → temp MP4 → serve with Content-Length."""
    cmd = [
        "ffmpeg", "-y",
        "-loglevel", "error",
        "-headers", _FFMPEG_HEADERS,
        "-i", video_url,
        "-c", "copy",
        "-movflags", "faststart",
        "-f", "mp4",
    ]
    path, size = _run_ffmpeg_to_tempfile(cmd, ".mp4")
    return _serve_tempfile(path, size, "video/mp4", filename)


def _ffmpeg_audio_mp3(audio_url, filename):
    """Convert audio → temp MP3 → serve with Content-Length."""
    cmd = [
        "ffmpeg", "-y",
        "-loglevel", "error",
        "-headers", _FFMPEG_HEADERS,
        "-i", audio_url,
        "-vn",
        "-c:a", "libmp3lame",
        "-q:a", "2",
        "-f", "mp3",
    ]
    mp3_filename = filename.rsplit(".", 1)[0] + ".mp3"
    path, size = _run_ffmpeg_to_tempfile(cmd, ".mp3")
    return _serve_tempfile(path, size, "audio/mpeg", mp3_filename)


def _safe_filename(title, ext):
    safe = re.sub(r'[^\w\s\-]', '', title or "video")
    safe = re.sub(r'\s+', '_', safe.strip())[:80]
    return f"{safe}.{ext}"


# ══════════════════════════════════════════════════════
#  QUALITY → STREAM SELECTION (ffmpeg-based, no URL filter)
# ══════════════════════════════════════════════════════

def _pick_video_for_quality(target_h, combined, video_only):
    """Find the best video stream at or below target height (any URL type)."""
    all_video = combined + video_only
    all_video.sort(key=lambda x: x.get("height") or 0, reverse=True)

    exact = [f for f in all_video if (f.get("height") or 0) == target_h]
    if exact:
        return exact[0]
    below = [f for f in all_video if (f.get("height") or 0) < target_h]
    if below:
        return below[0]
    return all_video[0] if all_video else None


def _pick_best_audio(audio_only, combined):
    """Pick the best audio-only stream; fall back to combined."""
    if audio_only:
        return audio_only[0]
    if combined:
        return combined[0]
    return None


def _handle_quality_download(quality, info, combined, video_only, audio_only):
    """
    Build the appropriate ffmpeg response for a given quality string.

    Video qualities : '1080p', '720p', '480p', '360p', '240p', '144p'
    Audio qualities : '128', '48'  (kbps as plain integer strings)
    """
    title = info.get("title", "video")
    q = quality.strip().lower()

    # ── Audio ────────────────────────────────────────────────
    if q.isdigit():
        target_abr = int(q)
        if audio_only:
            best = min(audio_only, key=lambda f: abs((f.get("abr") or 0) - target_abr))
        elif combined:
            best = combined[0]
        else:
            return jsonify({"error": "No audio stream found"}), 404

        filename = _safe_filename(title, "mp3")
        return _ffmpeg_audio_mp3(best["url"], filename)

    # ── Video ────────────────────────────────────────────────
    if q.endswith("p") and q[:-1].isdigit():
        target_h = int(q[:-1])

        video_fmt = _pick_video_for_quality(target_h, combined, video_only)
        if not video_fmt:
            return jsonify({"error": f"No video stream found for quality '{quality}'"}), 404

        filename = _safe_filename(title, "mp4")

        if video_fmt["has_audio"]:
            return _ffmpeg_combined_video(video_fmt["url"], filename)

        audio_fmt = _pick_best_audio(audio_only, combined)
        if not audio_fmt:
            return _ffmpeg_combined_video(video_fmt["url"], filename)

        return _ffmpeg_merge_video_audio(video_fmt["url"], audio_fmt["url"], filename)

    return jsonify({"error": f"Unknown quality '{quality}'"}), 400


# ══════════════════════════════════════════════════════
#  ROUTES
# ══════════════════════════════════════════════════════

def _bytes_to_human(b):
    if not b or b <= 0:
        return None
    if b < 1024 * 1024:
        return f"{b / 1024:.1f} KB"
    if b < 1024 * 1024 * 1024:
        return f"{b / (1024 * 1024):.1f} MB"
    return f"{b / (1024 * 1024 * 1024):.2f} GB"


def _fmt_raw_bytes(fmt):
    """Return raw byte count from a format entry, using approx if exact not available."""
    return fmt.get("filesize") or fmt.get("tbr") and None or None


def _build_apk_response(info, combined, video_only, audio_only, youtube_url):
    """Build JSON response in the exact format the VidTube APK expects."""
    base = request.host_url.rstrip("/")
    encoded_url = urllib.parse.quote(youtube_url, safe="")

    # Best audio bytes for merged size estimation
    best_audio = audio_only[0] if audio_only else None
    best_audio_bytes = best_audio.get("filesize") if best_audio else 0

    # ── Collect distinct video heights available ──────────────────
    seen_heights = set()
    video_formats = []
    standard_heights = [2160, 1440, 1080, 720, 480, 360, 240, 144]

    for target_h in standard_heights:
        fmt = _pick_video_for_quality(target_h, combined, video_only)
        if not fmt:
            continue
        actual_h = fmt.get("height") or target_h
        if actual_h in seen_heights:
            continue
        seen_heights.add(actual_h)

        # Merged size = video bytes + audio bytes
        vid_bytes = fmt.get("filesize")
        if vid_bytes and best_audio_bytes:
            size = _bytes_to_human(vid_bytes + best_audio_bytes) or "Unknown"
        elif vid_bytes:
            size = _bytes_to_human(vid_bytes) or "Unknown"
        else:
            size = fmt.get("filesize_human") or "Unknown"

        video_formats.append({
            "quality":     f"{actual_h}p",
            "extension":   "MP4",
            "size":        size,
            "downloadUrl": f"{base}/?url={encoded_url}&quality={actual_h}p",
        })

    # ── Audio formats ─────────────────────────────────────────────
    audio_formats = []
    audio_targets = [(128, "128k"), (48, "48k")]
    seen_abr = set()
    for target_abr, label in audio_targets:
        if not audio_only:
            break
        best = min(audio_only, key=lambda f: abs((f.get("abr") or 0) - target_abr))
        abr_key = round(best.get("abr") or 0)
        if abr_key in seen_abr:
            continue
        seen_abr.add(abr_key)
        abr_bytes = best.get("filesize")
        size = _bytes_to_human(abr_bytes) if abr_bytes else (best.get("filesize_human") or "Unknown")
        quality_num = str(target_abr)
        audio_formats.append({
            "quality":     label,
            "extension":   "MP3",
            "size":        size,
            "downloadUrl": f"{base}/?url={encoded_url}&quality={quality_num}",
        })

    return jsonify({
        "success": True,
        "video": {
            "title":     info.get("title", "Unknown Title"),
            "channel":   info.get("uploader") or info.get("channel") or "Unknown",
            "duration":  format_duration(info.get("duration")),
            "thumbnail": info.get("thumbnail", ""),
        },
        "formats": {
            "video": video_formats,
            "audio": audio_formats,
        },
    })


@app.route("/")
def index():
    raw_url = request.args.get("url", "").strip()
    quality  = request.args.get("quality", "").strip()

    if raw_url:
        url = normalize_url(raw_url)
        try:
            info = extract_info(url)
            combined, video_only, audio_only = parse_formats(info)

            # With quality → stream/download the file
            if quality:
                return _handle_quality_download(quality, info, combined, video_only, audio_only)

            # Without quality → return APK-compatible JSON
            return _build_apk_response(info, combined, video_only, audio_only, url)

        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    return render_template("index.html")


@app.route("/cookie-status")
def cookie_status():
    """Show status of all cookie files — which are active and which are on cooldown."""
    return jsonify({
        "cookies": _cookie_pool.status(),
        "cooldown_seconds": _COOLDOWN_SECONDS,
    })


@app.route("/search")
def search():
    query = request.args.get("q", "").strip()
    limit = int(request.args.get("limit", 12))
    if not query:
        return jsonify({"error": "Query is required"}), 400

    def _do_search(cookie_path=None, player_client=None):
        opts = get_ydl_opts(cookie_path, player_client)
        opts["extract_flat"] = True
        opts["playlistend"]  = limit
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(f"ytsearch{limit}:{query}", download=False)

    assigned = _cookie_pool.get_next()
    info = None
    last_exc = None

    # Round 1: assigned cookie
    try:
        info = _do_search(assigned)
        if assigned:
            print(f"[Search] OK → {os.path.basename(assigned)}")
    except Exception as e:
        last_exc = e
        is_cookie = assigned and _is_cookie_error(e)
        is_nsig   = _is_nsig_error(e)
        if is_cookie:
            _cookie_pool.mark_blocked(assigned)
        if not (is_cookie or is_nsig):
            return jsonify({"error": str(e)}), 500

    # Round 2: other cookies (only if cookie error)
    if info is None and is_cookie:
        for c in [x for x in _cookie_pool._load_cookies() if x != assigned]:
            try:
                info = _do_search(c)
                print(f"[Search] OK fallback → {os.path.basename(c)}")
                break
            except Exception as e:
                last_exc = e
                if _is_cookie_error(e):
                    _cookie_pool.mark_blocked(c)

    # Round 3: mediaconnect without cookie
    if info is None:
        try:
            info = _do_search(player_client="mediaconnect")
            print("[Search] OK → mediaconnect (no cookie)")
        except Exception as e:
            last_exc = e

    if info is None:
        return jsonify({"error": str(last_exc)}), 500

    videos = []
    for entry in info.get("entries", []):
        if not entry:
            continue
        vid_id     = entry.get("id", "")
        thumbnails = entry.get("thumbnails") or []
        thumb = (thumbnails[-1]["url"] if thumbnails
                 else f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg")
        videos.append({
            "id":        vid_id,
            "title":     entry.get("title", ""),
            "thumbnail": thumb,
            "duration":  format_duration(entry.get("duration")),
            "channel":   entry.get("channel") or entry.get("uploader") or "",
            "views":     entry.get("view_count"),
            "url":       entry.get("url") or f"https://www.youtube.com/watch?v={vid_id}",
        })
    return jsonify({"results": videos})


@app.route("/download/audio")
@app.route("/download/audio/<path:link>")
def download_audio(link=None):
    raw = request.args.get("url") or link or ""
    if not raw:
        return jsonify({"status": "error", "error": "url parameter required"}), 400
    url = normalize_url(raw)
    try:
        info = extract_info(url)
        _, _, audio_only = parse_formats(info)
        best = audio_only[0] if audio_only else None

        return jsonify({
            "status":            "ok",
            "title":             info.get("title"),
            "thumbnail":         info.get("thumbnail"),
            "duration":          format_duration(info.get("duration")),
            "channel":           info.get("uploader"),
            "best_audio":        best,
            "all_audio_formats": audio_only,
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/download/video")
@app.route("/download/video/<path:link>")
def download_video(link=None):
    raw = request.args.get("url") or link or ""
    if not raw:
        return jsonify({"status": "error", "error": "url parameter required"}), 400
    url = normalize_url(raw)
    try:
        info = extract_info(url)
        combined, video_only, audio_only = parse_formats(info)

        return jsonify({
            "status":         "ok",
            "title":          info.get("title"),
            "thumbnail":      info.get("thumbnail"),
            "duration":       format_duration(info.get("duration")),
            "channel":        info.get("uploader"),
            "description":    (info.get("description") or "")[:300],
            "formats": {
                "combined":   combined,
                "video_only": video_only,
                "audio_only": audio_only,
            },
            "formats_flat":   combined + video_only + audio_only,
            "formats_count":  len(combined) + len(video_only) + len(audio_only),
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
