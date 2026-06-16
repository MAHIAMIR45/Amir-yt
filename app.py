import os
import re
import subprocess
import urllib.parse
import urllib.request
import yt_dlp
from flask import Flask, request, jsonify, render_template, Response, stream_with_context
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

_COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def normalize_url(link):
    link = link.strip()
    if link.startswith("https:/") and not link.startswith("https://"):
        link = "https://" + link[7:]
    elif link.startswith("http:/") and not link.startswith("http://"):
        link = "http://" + link[6:]
    elif not link.startswith("http"):
        link = "https://" + link
    return link


def get_ydl_opts():
    import shutil
    node_path = shutil.which("node") or "node"

    opts = {
        "quiet":             True,
        "no_warnings":       True,
        "skip_download":     True,
        "noplaylist":        True,
        "retries":           5,
        "fragment_retries":  5,
        "skip_unavailable_fragments": True,
        "http_headers":      {"User-Agent": _USER_AGENT},
        "js_runtimes":       {"node": {"path": node_path}},
    }
    if os.path.isfile(_COOKIE_FILE):
        opts["cookiefile"] = _COOKIE_FILE
    return opts


def extract_info(url):
    opts = get_ydl_opts()
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception:
        opts_no_cookie = {k: v for k, v in opts.items() if k != "cookiefile"}
        with yt_dlp.YoutubeDL(opts_no_cookie) as ydl:
            return ydl.extract_info(url, download=False)


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


@app.route("/search")
def search():
    query = request.args.get("q", "").strip()
    limit = int(request.args.get("limit", 12))
    if not query:
        return jsonify({"error": "Query is required"}), 400
    try:
        opts = get_ydl_opts()
        opts["extract_flat"] = True
        opts["playlistend"]  = limit
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
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
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
