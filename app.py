import os
import re
import urllib.request
import yt_dlp
from flask import Flask, request, jsonify, render_template, Response, stream_with_context
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ── Cookies file (Netscape format) ──
_COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")

# ── Modern Chrome User-Agent ──
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


# ══════════════════════════════════════════════════════
#  GLOBAL JSON ERROR HANDLERS
#  (Flask by default returns HTML error pages — Android
#   app expects JSON, so we override all error responses)
# ══════════════════════════════════════════════════════

@app.errorhandler(400)
def bad_request(e):
    return jsonify({"success": False, "error": str(e)}), 400

@app.errorhandler(404)
def not_found(e):
    return jsonify({"success": False, "error": "Not found"}), 404

@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({"success": False, "error": "Method not allowed"}), 405

@app.errorhandler(500)
def internal_error(e):
    return jsonify({"success": False, "error": "Internal server error"}), 500

@app.errorhandler(Exception)
def handle_exception(e):
    return jsonify({"success": False, "error": str(e)}), 500


def normalize_url(link):
    """Fix https:/ → https:// (double slash stripped by URL routing)."""
    link = link.strip()
    if link.startswith("https:/") and not link.startswith("https://"):
        link = "https://" + link[7:]
    elif link.startswith("http:/") and not link.startswith("http://"):
        link = "http://" + link[6:]
    elif not link.startswith("http"):
        link = "https://" + link
    return link


def _base_opts():
    """Shared quiet/network options used by every extraction path."""
    import shutil
    node_path = shutil.which("node") or "node"
    return {
        "quiet":              True,
        "no_warnings":        True,
        "skip_download":      True,
        "noplaylist":         True,
        "retries":            5,
        "fragment_retries":   5,
        "skip_unavailable_fragments": True,
        "http_headers":       {"User-Agent": _USER_AGENT},
        "js_runtimes":        {"node": {"path": node_path}},
    }


def get_ydl_opts():
    """Return yt-dlp opts with cookies (primary path, works on Render)."""
    opts = _base_opts()
    if os.path.isfile(_COOKIE_FILE):
        opts["cookiefile"] = _COOKIE_FILE
    return opts


def _android_vr_opts():
    """Fallback opts: Android VR player, no cookies, process=False-friendly."""
    opts = _base_opts()
    opts["extractor_args"] = {
        "youtube": {
            "player_client": ["android_vr"],
            "skip_webpage":  ["1"],
        }
    }
    return opts


def extract_info(url):
    """Extract video info with automatic fallback.

    1. Primary  : cookies + js_runtimes + process=True  (works on Render
                  with valid cookies.txt).
    2. Fallback : Android VR player + process=False      (works anywhere
                  without cookies or a JS runtime).
    """
    # Primary: cookies path
    try:
        with yt_dlp.YoutubeDL(get_ydl_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
        # Verify we actually got usable formats (not just images)
        fmts = info.get("formats", [])
        if any(f.get("url") and f.get("vcodec", "none") not in (None, "none") for f in fmts):
            return info
    except Exception:
        pass

    # Fallback: Android VR player, bypass format selector entirely
    with yt_dlp.YoutubeDL(_android_vr_opts()) as ydl:
        return ydl.extract_info(url, download=False, process=False)


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
#  ROUTES
# ══════════════════════════════════════════════════════

def _is_direct_url(url):
    """True when the URL is a direct byte-stream (videoplayback), not an HLS/DASH manifest."""
    if not url:
        return False
    return "videoplayback" in url and "manifest" not in url


def _safe_filename(title, ext):
    safe = re.sub(r'[^\w\s\-]', '', title or "video")
    safe = re.sub(r'\s+', '_', safe.strip())[:80]
    return f"{safe}.{ext}"


def _find_format_by_quality(quality, combined, video_only, audio_only):
    """Return (stream_url, ext, content_type) for a given quality string.

    Video qualities : '1080p', '720p', '480p', '360p', '240p', '144p'
    Audio qualities : '128', '48'  (kbps as a plain integer string)

    Only direct videoplayback URLs are returned — HLS/manifest URLs are
    skipped so the proxy can stream raw bytes to the client.
    """
    q = quality.strip().lower()

    # ── Audio ───────────────────────────────────────────────────
    if q.isdigit():
        target_abr = int(q)
        candidates = [f for f in audio_only if _is_direct_url(f.get("url"))]
        # If no direct URL, accept any audio URL
        if not candidates:
            candidates = audio_only
        best = min(candidates, key=lambda f: abs((f.get("abr") or 0) - target_abr), default=None)
        if best:
            ext = best.get("ext", "m4a")
            ct  = "audio/mp4" if ext == "m4a" else "audio/webm"
            return best["url"], ext, ct
        return None, None, None

    # ── Video ───────────────────────────────────────────────────
    if q.endswith("p") and q[:-1].isdigit():
        target_h = int(q[:-1])

        # Prefer direct combined (video + audio in one file) — exact then closest
        direct_combined = [f for f in combined if _is_direct_url(f.get("url"))]
        for fmt in direct_combined:
            if fmt.get("height") == target_h:
                ext = fmt.get("ext", "mp4")
                return fmt["url"], ext, "video/mp4"
        for fmt in direct_combined:
            if (fmt.get("height") or 0) <= target_h:
                ext = fmt.get("ext", "mp4")
                return fmt["url"], ext, "video/mp4"

        # Fall back to direct video-only — exact then closest
        direct_video = [f for f in video_only if _is_direct_url(f.get("url"))]
        for fmt in direct_video:
            if fmt.get("height") == target_h:
                ext = fmt.get("ext", "mp4")
                return fmt["url"], ext, "video/mp4"
        for fmt in direct_video:
            if (fmt.get("height") or 0) <= target_h:
                ext = fmt.get("ext", "mp4")
                return fmt["url"], ext, "video/mp4"

        # Last resort: any combined/video format (no direct URL filter)
        for fmt in combined:
            if fmt.get("height") == target_h:
                ext = fmt.get("ext", "mp4")
                return fmt["url"], ext, "video/mp4"
        for fmt in combined:
            if (fmt.get("height") or 0) <= target_h:
                ext = fmt.get("ext", "mp4")
                return fmt["url"], ext, "video/mp4"
        for fmt in video_only:
            if (fmt.get("height") or 0) <= target_h:
                ext = fmt.get("ext", "mp4")
                return fmt["url"], ext, "video/mp4"

    return None, None, None


def _proxy_stream(stream_url, filename, content_type):
    """Stream bytes from stream_url through Flask with a download header.

    Forwards Range requests so Android DownloadManager can resume/seek.
    Passes Content-Length through so the client can show progress.
    """
    req_headers = {
        "User-Agent": _USER_AGENT,
        "Referer":    "https://www.youtube.com/",
    }
    range_hdr = request.headers.get("Range")
    if range_hdr:
        req_headers["Range"] = range_hdr

    req = urllib.request.Request(stream_url, headers=req_headers)
    try:
        upstream = urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as e:
        return jsonify({"success": False, "error": f"Upstream error {e.code}"}), 502
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

    status        = upstream.status
    content_len   = upstream.headers.get("Content-Length")
    accept_ranges = upstream.headers.get("Accept-Ranges", "bytes")

    resp_headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Accept-Ranges":       accept_ranges,
    }
    if content_len:
        resp_headers["Content-Length"] = content_len

    def generate():
        try:
            while True:
                chunk = upstream.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            upstream.close()

    return Response(
        stream_with_context(generate()),
        status=status,
        content_type=content_type,
        headers=resp_headers,
    )


@app.route("/")
def index():
    raw_url = request.args.get("url", "").strip()
    quality  = request.args.get("quality", "").strip()

    # ── Case 1: url + quality → stream binary video/audio ──────────────
    if raw_url and quality:
        url = normalize_url(raw_url)
        try:
            info = extract_info(url)
            combined, video_only, audio_only = parse_formats(info)
            stream_url, ext, content_type = _find_format_by_quality(
                quality, combined, video_only, audio_only)
            if stream_url:
                title    = info.get("title", "video")
                filename = _safe_filename(title, ext)
                return _proxy_stream(stream_url, filename, content_type)
            return jsonify({"success": False, "error": f"Quality '{quality}' not available"}), 404
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    # ── Case 2: url only → return JSON with video info + formats ────────
    # Android app calls /?url=... (no quality) to get the available
    # qualities before showing the selection UI.  Always return JSON here,
    # never HTML, so the app can parse it correctly.
    if raw_url:
        url = normalize_url(raw_url)
        try:
            info = extract_info(url)
            combined, video_only, audio_only = parse_formats(info)
            vid_id    = info.get("id", "")
            title     = info.get("title") or ""
            channel   = info.get("uploader") or info.get("channel") or ""
            thumbnail = (info.get("thumbnail")
                         or f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg")
            duration  = format_duration(info.get("duration"))

            # Build a simple ordered list of available quality labels so
            # the app can show a quality picker.
            seen_q = set()
            qualities = []
            for fmt in combined + video_only:
                h = fmt.get("height")
                if h:
                    label = f"{h}p"
                    if label not in seen_q:
                        seen_q.add(label)
                        qualities.append(label)
            for fmt in audio_only:
                abr = fmt.get("abr") or fmt.get("tbr") or 0
                label = f"{int(abr)}" if abr else "48"
                if label not in seen_q:
                    seen_q.add(label)
                    qualities.append(label)

            # Return every common field-name variant so any app version matches
            return jsonify({
                # ── status ──────────────────────────────────────────────
                "success":          True,
                "status":           "ok",
                # ── identity ─────────────────────────────────────────────
                "id":               vid_id,
                "videoId":          vid_id,
                # ── title (multiple aliases) ─────────────────────────────
                "title":            title,
                "videoTitle":       title,
                "name":             title,
                # ── channel (multiple aliases) ───────────────────────────
                "channel":          channel,
                "channelName":      channel,
                "author":           channel,
                "uploader":         channel,
                # ── media metadata ───────────────────────────────────────
                "thumbnail":        thumbnail,
                "thumbnailUrl":     thumbnail,
                "duration":         duration,
                "durationSeconds":  info.get("duration"),
                # ── quality list (simple labels for picker UI) ───────────
                "qualities":        qualities,
                "availableQualities": qualities,
                # ── full format detail (for advanced clients) ────────────
                "formats": {
                    "combined":   combined,
                    "video_only": video_only,
                    "audio_only": audio_only,
                },
                "formats_flat":   combined + video_only + audio_only,
                "formats_count":  len(combined) + len(video_only) + len(audio_only),
            })
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    # ── Case 3: no params → serve the web UI ────────────────────────────
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
