import os
import re
import time
import threading
import subprocess
import json
import urllib.parse
import urllib.request
import urllib.error
import socket
import datetime
import functools
import yt_dlp
from flask import Flask, request, jsonify, make_response, render_template, Response, stream_with_context, redirect, url_for, session, abort
from flask_cors import CORS

try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
except Exception:
    pass

app = Flask(__name__)
CORS(app)
app.secret_key = os.environ.get("ADMIN_SECRET_KEY", "amir-admin-panel-secret-2026")

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# Overall hard cap for a single yt-dlp extraction (seconds).
# Prevents infinite "loading" on hosting like Replit when YouTube stalls.
_EXTRACT_TIMEOUT_SECONDS = 25
# Maximum total time spent across ALL fallback attempts for one URL.
# With 7 clients + up to 5 cookies this bounds worst-case hang (~2.5 min).
_EXTRACT_BUDGET_SECONDS = 150
# The FIRST mediaconnect attempt (no cookies) is the best shot on datacenter
# IPs but can be slow on weak hosts (Render free tier = 0.1 CPU). Give it a
# long single timeout instead of starving it with the per-attempt default.
_MEDIACONNECT_FIRST_TIMEOUT = 60
# Clients tried in order when YouTube blocks the default web client (403 / bot).
# mediaconnect + tv/android/ios/web_safari dodge the n-challenge most of the time.
_FALLBACK_PLAYER_CLIENTS = ["mediaconnect", "tv", "android", "ios", "web_safari", "mweb"]

# ══════════════════════════════════════════════════════
#  COOKIE ROTATION SYSTEM
#  Place cookie files as: cookies1.txt, cookies2.txt, ... cookies10.txt
#  Legacy cookies.txt is also supported as fallback
# ══════════════════════════════════════════════════════

_COOLDOWN_SECONDS = 300  # 5 min cooldown after a cookie gets blocked
_INFO_CACHE_TTL = 180  # Keep extracted metadata/stream URLs warm for 3 minutes
_FFMPEG_TIMEOUT_SECONDS = 900  # Never leave a download request hanging forever
_TIKTOK_API_URL = "https://api.nexray.eu.cc/downloader/tiktok"
_TIKTOK_BACKUP_API_URL = "https://mkzstyleee.vercel.app/download/tiktok"
_TIKTOK_BACKUP_API_URL_V2 = "https://mkzstyleee.vercel.app/download/tiktok-v2"
_TIKTOK_BACKUP_APIKEY = "FREE-XODVNWPL-ERUF"
_TIKTOK_BACKUP_API_URL_2 = "https://jerrycoder.oggyapi.workers.dev/down/tiktok"
_PINTEREST_API_URL = "https://api.nexray.eu.cc/downloader/pinterest"
_PINTEREST_BACKUP_API_URL = "https://mkzstyleee.vercel.app/download/pinterest"
_PINTEREST_BACKUP_APIKEY = "FREE-XODVNWPL-ERUF"
_PINTEREST_BACKUP_API_URL_2 = "https://api.theresav.eu/api/download/pinterest"
_PINTEREST_BACKUP_APIKEY_2 = "QkX9K"
_FACEBOOK_API_URL = "https://api.theresav.eu/api/download/facebook"
_FACEBOOK_APIKEY = "QkX9K"
_FACEBOOK_BACKUP_API_URL = "https://eliteprotech-apis.zone.id/facebook1"
_FACEBOOK_BACKUP_API_URL_2 = "https://jerrycoder.oggyapi.workers.dev/down/fb"
_NEXRAY_YT_API_URL = "https://api.nexray.eu.cc/downloader/v1/ytmp4"
_NEXRAY_YT_TIMEOUT = 60  # nexray can be slow, allow up to 60 s
# YouTube backup APIs (work from datacenter IPs where yt-dlp is blocked,
# same trick the site uses for TikTok/Pinterest/Facebook).
_YT_BACKUP_API_MP4 = "https://eliteprotech-apis.zone.id/ytmp4"      # combined 360p mp4
_YT_BACKUP_API_MULTI = "https://jerrycoder.oggyapi.workers.dev/down/youtube"  # multi quality
_YT_BACKUP_TIMEOUT = 25
_YTDL_RACE_WINDOW = 12  # how long we wait for yt-dlp before using the backup
_info_cache = {}
_info_cache_lock = threading.Lock()


# ══════════════════════════════════════════════════════
#  ADMIN PANEL + ANALYTICS SYSTEM
#  Login: /admin  (username: amir, password: amir)
# ══════════════════════════════════════════════════════

_ADMIN_USER = "amir"
_ADMIN_PASS = "amir"
_ADMIN_DATA_FILE = os.path.join(_BASE_DIR, "admin_data.json")

# ── In-memory analytics state ──────────────────────────
_request_log = []                          # recent requests (ring buffer)
_request_lock = threading.Lock()
_platform_counts = {                       # total platform request counts
    "youtube": 0, "tiktok": 0, "pinterest": 0, "facebook": 0, "other": 0,
    "admin_page": 0, "unknown": 0,
}
_platform_errors = {}
_ip_sessions = {}                          # ip -> session record
_country_cache = {}
_hourly_traffic = {}                       # "YYYY-MM-DDTHH" -> count
_daily_traffic = {}                        # "YYYY-MM-DD" -> count
_process_start = datetime.datetime.now()

# Registered APIs for health monitoring
# (name, kind, build_full_url, is_healthy_check)
_HEALTH_TARGETS = [
    {"name": "Nexray TikTok", "platform": "tiktok",
     "url": lambda u: f"{_TIKTOK_API_URL}?url={u}"},
    {"name": "TikTok Backup 1", "platform": "tiktok",
     "url": lambda u: f"{_TIKTOK_BACKUP_API_URL}?url={u}&apikey={_TIKTOK_BACKUP_APIKEY}"},
    {"name": "TikTok Backup 2", "platform": "tiktok",
     "url": lambda u: f"{_TIKTOK_BACKUP_API_URL_V2}?url={u}&apikey={_TIKTOK_BACKUP_APIKEY}"},
    {"name": "TikTok Backup 3 (jerrycoder)", "platform": "tiktok",
     "url": lambda u: f"{_TIKTOK_BACKUP_API_URL_2}?url={u}"},
    {"name": "Nexray Pinterest", "platform": "pinterest",
     "url": lambda u: f"{_PINTEREST_API_URL}?url={u}"},
    {"name": "Pinterest Backup 1", "platform": "pinterest",
     "url": lambda u: f"{_PINTEREST_BACKUP_API_URL}?url={u}&apikey={_PINTEREST_BACKUP_APIKEY}"},
    {"name": "Pinterest Backup 2 (theresav)", "platform": "pinterest",
     "url": lambda u: f"{_PINTEREST_BACKUP_API_URL_2}?url={u}", "header": {"x-apikey": _PINTEREST_BACKUP_APIKEY_2}},
    {"name": "Facebook (theresav)", "platform": "facebook",
     "url": lambda u: f"{_FACEBOOK_API_URL}?url={u}", "header": {"x-apikey": _FACEBOOK_APIKEY}},
    {"name": "Facebook Backup (elitepro)", "platform": "facebook",
     "url": lambda u: f"{_FACEBOOK_BACKUP_API_URL}?url={u}"},
    {"name": "Facebook Backup 2 (jerrycoder)", "platform": "facebook",
     "url": lambda u: f"{_FACEBOOK_BACKUP_API_URL_2}?url={u}"},
    {"name": "Nexray YouTube", "platform": "youtube",
     "url": lambda u: f"{_NEXRAY_YT_API_URL}?url={u}"},
]
_health_status = {}                        # name -> {"status","latency","last_check","last_ok"}
_health_status_lock = threading.Lock()


def _now_iso():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _get_client_ip():
    """Best effort extract the real client IP."""
    fwd = request.headers.get("X-Forwarded-For") or request.headers.get("X-Real-IP")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "0.0.0.0"


def _flag_emoji(country_code):
    """Return a flag emoji for a 2-letter ISO country code."""
    code = (country_code or "XX").upper()
    if len(code) != 2:
        return "\U0001F310"
    try:
        return chr(ord(code[0]) + 0x1F1E6 - ord("A")) + chr(ord(code[1]) + 0x1F1E6 - ord("A"))
    except Exception:
        return "\U0001F310"


def _lookup_country(ip):
    """Resolve country (and city) for an IP with caching + timeout."""
    if not ip or ip in ("127.0.0.1", "0.0.0.0", "::1", "localhost"):
        return {"country": "Local/Unknown", "country_code": "XX", "city": "", "isp": "", "region": ""}
    if ip in _country_cache:
        return _country_cache[ip]
    try:
        req = urllib.request.Request(
            f"https://ipwho.is/{ip}",
            headers={"User-Agent": _USER_AGENT},
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        info = {
            "country": data.get("country") or "Unknown",
            "country_code": (data.get("country_code") or "XX").upper(),
            "city": data.get("city") or "",
            "isp": data.get("connection", {}).get("isp") or "",
            "region": data.get("region") or "",
        }
    except Exception:
        info = {"country": "Unknown", "country_code": "XX", "city": "", "isp": "", "region": ""}
    if len(_country_cache) < 5000:
        _country_cache[ip] = info
    return info


def _detect_platform(url):
    """Map a URL to a platform name for analytics."""
    try:
        host = urllib.parse.urlparse(normalize_url(url)).hostname or ""
    except Exception:
        return "other"
    host = host.lower().rstrip(".")
    if host.endswith(".tiktok.com") or host == "tiktok.com":
        return "tiktok"
    if host.endswith(".pinterest.com") or host.endswith(".pinimg.com") or host in ("pin.it",):
        return "pinterest"
    if host.endswith(".facebook.com") or host.endswith(".fb.watch") or host in ("facebook.com", "fb.watch"):
        return "facebook"
    if host.endswith(".youtube.com") or host in ("youtu.be",):
        return "youtube"
    return "other"


def track_request(url=None, kind=None, error=None, status_code=None):
    """Record an incoming request for analytics. Called from routes/middleware."""
    global _request_log
    ip = _get_client_ip()
    path = request.path
    now = datetime.datetime.now()
    hour_key = now.strftime("%Y-%m-%dT%H")
    day_key = now.strftime("%Y-%m-%d")

    platform = _detect_platform(url) if url else (kind or "unknown")
    geo = _lookup_country(ip)
    ua = (request.headers.get("User-Agent") or "")[:200]
    referrer = (request.headers.get("Referer") or "")[:200]
    method = request.method

    rec = {
        "ip": ip,
        "country": geo["country"],
        "country_code": geo["country_code"],
        "city": geo["city"],
        "isp": geo["isp"],
        "region": geo["region"],
        "path": path,
        "method": method,
        "url": (url or "")[:300],
        "platform": platform,
        "status": status_code,
        "error": (error or "")[:200],
        "ua": ua,
        "referrer": referrer,
        "time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "ts": now.timestamp(),
    }

    with _request_lock:
        _request_log.append(rec)
        if len(_request_log) > 3000:
            _request_log = _request_log[-3000:]

        key = platform if platform in _platform_counts else "other"
        _platform_counts[key] = _platform_counts.get(key, 0) + 1
        if error:
            _platform_errors[platform] = _platform_errors.get(platform, 0) + 1
        _hourly_traffic[hour_key] = _hourly_traffic.get(hour_key, 0) + 1
        _daily_traffic[day_key] = _daily_traffic.get(day_key, 0) + 1

        # Session tracking per IP with a rich per-IP profile
        if ip not in _ip_sessions:
            _ip_sessions[ip] = {
                "first_seen": rec["time"],
                "last_seen": rec["time"],
                "first_ts": now.timestamp(),
                "last_ts": now.timestamp(),
                "count": 1,
                "country": geo["country"],
                "country_code": geo["country_code"],
                "city": geo["city"],
                "isp": geo["isp"],
                "region": geo["region"],
                "ua": ua,
                "platforms": {platform: 1},
                "paths": {path: 1},
                "requests": [{
                    "url": rec["url"], "path": path, "platform": platform,
                    "status": status_code, "time": rec["time"], "method": method,
                }],
            }
        else:
            s = _ip_sessions[ip]
            s["last_seen"] = rec["time"]
            s["last_ts"] = now.timestamp()
            s["count"] = s["count"] + 1
            s["platforms"][platform] = s["platforms"].get(platform, 0) + 1
            s["paths"][path] = s["paths"].get(path, 0) + 1
            s["requests"].append({
                "url": rec["url"], "path": path, "platform": platform,
                "status": status_code, "time": rec["time"], "method": method,
            })
            s["requests"] = s["requests"][-30:]
            s["city"] = geo["city"]
            s["isp"] = geo["isp"]
            if not s.get("ua"):
                s["ua"] = ua
    return rec


def _health_check_single(target, encoded):
    """Check a single API target and store its result.

    Classification:
      - "up"       -> service responded with usable JSON (probe OK)
      - "degraded" -> service is reachable but returned an HTTP error (bad probe URL)
      - "down"     -> service is unreachable (timeout / connection refused)
    """
    rate_limited = False
    latency = None
    error = None
    try:
        api_url = target["url"](encoded)
        headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
        headers.update(target.get("header", {}))
        req = urllib.request.Request(api_url, headers=headers)
        start = time.time()
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # Service is reachable but returned 4xx/5xx — treat as degraded
            latency = round((time.time() - start) * 1000)
            limit_status = e.code in (429, 403)
            rate_limited = limit_status
            payload = {}
            error = f"HTTP {e.code}"
        latency = latency or round((time.time() - start) * 1000)
        ok_flag = bool(payload.get("status") is True or payload.get("success") is True)
        status = "up" if ok_flag else ("degraded" if not rate_limited else "down")
        with _health_status_lock:
            _health_status[target["name"]] = {
                "name": target["name"],
                "platform": target["platform"],
                "status": status,
                "latency": latency,
                "last_check": _now_iso(),
                "last_ok": _now_iso() if status == "up" else _health_status.get(target["name"], {}).get("last_ok"),
                "probe_ok": ok_flag,
                "error": error,
            }
    except Exception as e:
        with _health_status_lock:
            _health_status[target["name"]] = {
                "name": target["name"],
                "platform": target["platform"],
                "status": "down",
                "latency": latency,
                "last_check": _now_iso(),
                "last_ok": _health_status.get(target["name"], {}).get("last_ok"),
                "error": (error or str(e))[:200],
                "probe_ok": False,
            }


def _health_check_once():
    """Run all health checks in parallel threads for speed."""
    probe_urls = {
        "tiktok": "https://www.tiktok.com/@willsmith/video/7126995370870933806",
        "pinterest": "https://www.pinterest.com/pin/824758858221769000/",
        "facebook": "https://www.facebook.com/watch/?v=10156908315493390",
        "youtube": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    }
    encoded_by_platform = {
        k: urllib.parse.quote(v, safe="") for k, v in probe_urls.items()
    }
    threads = []
    for target in _HEALTH_TARGETS:
        encoded = encoded_by_platform.get(target.get("platform"), encoded_by_platform["youtube"])
        t = threading.Thread(target=_health_check_single, args=(target, encoded), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()


def _health_monitor_loop():
    """Background thread: check API health every 45 seconds."""
    while True:
        try:
            _health_check_once()
        except Exception as e:
            print(f"[HealthMonitor] error: {e}", flush=True)
        time.sleep(45)


def _persist_admin_data():
    try:
        snapshot = {
            "platform_counts": _platform_counts,
            "platform_errors": _platform_errors,
            "ip_sessions": _ip_sessions,
            "daily_traffic": _daily_traffic,
            "health_status": dict(_health_status),
            "process_start": _process_start.strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(_ADMIN_DATA_FILE, "w") as f:
            json.dump(snapshot, f)
    except Exception:
        pass


def _admin_required(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if session.get("admin_logged_in") is not True:
            if request.path.startswith("/admin/api/"):
                return jsonify({"error": "unauthorized"}), 401
            return redirect(url_for("admin_login"))
        return fn(*args, **kwargs)
    return wrapper


# ── Admin routes ───────────────────────────────────────

@app.route("/admin")
def admin_login():
    if session.get("admin_logged_in") is True:
        return redirect(url_for("admin_dashboard"))
    return render_template("admin_login.html")


@app.route("/admin/login", methods=["POST"])
def admin_do_login():
    data = request.get_json(silent=True) or request.form
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if username == _ADMIN_USER and password == _ADMIN_PASS:
        session["admin_logged_in"] = True
        session["admin_user"] = username
        if request.path.endswith("/admin/login") and request.is_json:
            return jsonify({"success": True})
        return redirect(url_for("admin_dashboard"))
    return jsonify({"success": False, "error": "Invalid credentials"}), 401


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_logged_in", None)
    session.pop("admin_user", None)
    return redirect(url_for("admin_login"))


@app.route("/admin/dashboard")
@_admin_required
def admin_dashboard():
    resp = make_response(render_template("admin_dashboard.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/admin/api/stats")
@_admin_required
def admin_api_stats():
    with _request_lock:
        total = sum(_platform_counts.values())
        sessions = list(_ip_sessions.values())
        hourly = dict(sorted(_hourly_traffic.items())[-24:])
    countries = {}
    for s in sessions:
        code = s.get("country_code") or "XX"
        countries.setdefault(code, {"country": s.get("country") or "Unknown", "count": 0, "ips": 0})
        countries[code]["count"] += s.get("count", 1)
        countries[code]["ips"] += 1
    cc = sorted(countries.items(), key=lambda x: -x[1]["count"])[:40]
    cc_out = [{"code": k, "country": v["country"], "count": v["count"], "ips": v["ips"], "flag": _flag_emoji(k)} for k, v in cc]
    return jsonify({
        "total_requests": total,
        "platform_counts": _platform_counts,
        "platform_errors": _platform_errors,
        "unique_ips": len(sessions),
        "countries": cc_out,
        "hourly_traffic": hourly,
        "daily_traffic": dict(sorted(_daily_traffic.items())[-30:]),
        "uptime": time.time() - _process_start.timestamp(),
        "process_start": _process_start.strftime("%Y-%m-%d %H:%M:%S"),
    })


@app.route("/admin/api/health")
@_admin_required
def admin_api_health():
    with _health_status_lock:
        return jsonify({
            "apis": list(_health_status.values()),
            "last_check": _now_iso(),
        })


@app.route("/admin/api/sessions")
@_admin_required
def admin_api_sessions():
    limit = int(request.args.get("limit", 100))
    with _request_lock:
        sorteds = sorted(_ip_sessions.items(), key=lambda kv: kv[1]["last_seen"], reverse=True)[:limit]
        out = []
        for ip, s in sorteds:
            reqs = s.get("requests", [])
            duration = 0
            if s.get("first_ts") and s.get("last_ts") and s.get("last_ts") > s.get("first_ts"):
                duration = int(s["last_ts"] - s["first_ts"])
            out.append({
                "ip": ip,
                **s,
                "flag": _flag_emoji(s.get("country_code")),
                "request_count": len(reqs),
                "duration_seconds": duration,
            })
        return jsonify({"sessions": out, "total": len(_ip_sessions)})


@app.route("/admin/api/requests")
@_admin_required
def admin_api_requests():
    limit = int(request.args.get("limit", 200))
    platform = request.args.get("platform")
    with _request_lock:
        items = _request_log if not platform else [r for r in _request_log if r.get("platform") == platform]
        items = items[-limit:]
        items = list(reversed(items))
        return jsonify({"requests": items, "total_filtered": len(items)})


@app.route("/admin/ping")
def admin_ping():
    """Endpoint hit by the dashboard to register the admin's own IP in analytics."""
    _platform_counts["admin_page"] = _platform_counts.get("admin_page", 0) + 1
    return jsonify({"ok": True, "ip": _get_client_ip()})


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


def is_tiktok_url(link):
    """Return True for TikTok links, including vm.tiktok.com short URLs."""
    try:
        host = urllib.parse.urlparse(normalize_url(link)).hostname or ""
        host = host.lower().rstrip(".")
        return host == "tiktok.com" or host.endswith(".tiktok.com")
    except Exception:
        return False


def is_pinterest_url(link):
    """Return True for Pinterest links, including pin.it short URLs."""
    try:
        host = urllib.parse.urlparse(normalize_url(link)).hostname or ""
        host = host.lower().rstrip(".")
        return (
            host == "pinterest.com"
            or host.endswith(".pinterest.com")
            or host.endswith(".pinimg.com")
            or host == "pin.it"
        )
    except Exception:
        return False


def is_facebook_url(link):
    """Return True for Facebook links, including fb.watch short URLs."""
    try:
        host = urllib.parse.urlparse(normalize_url(link)).hostname or ""
        host = host.lower().rstrip(".")
        return (
            host == "facebook.com"
            or host.endswith(".facebook.com")
            or host == "fb.watch"
            or host.endswith(".fb.watch")
        )
    except Exception:
        return False


def _fetch_tiktok_result(url):
    """Fetch TikTok metadata and temporary media URLs.

    Primary: Nexray API. Backup: mkzstyleee Vercel API (v1 then v2) with apikey.
    """
    # ── Primary API (Nexray)
    try:
        api_url = (
            f"{_TIKTOK_API_URL}?url="
            f"{urllib.parse.quote(url, safe='')}"
        )
        req = urllib.request.Request(
            api_url,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))

        if payload.get("status") is True and payload.get("result"):
            return payload["result"]
        print(f"[TikTok] Primary API returned error: {payload}")
    except Exception as e:
        print(f"[TikTok] Primary API failed: {e}")

    # ── Backup API No.1 (mkzstyleee /download/tiktok)
    for backup_api in (_TIKTOK_BACKUP_API_URL, _TIKTOK_BACKUP_API_URL_V2):
        try:
            print(f"[TikTok] Trying backup API: {backup_api}")
            api_url = (
                f"{backup_api}?url="
                f"{urllib.parse.quote(url, safe='')}"
                f"&apikey={_TIKTOK_BACKUP_APIKEY}"
            )
            req = urllib.request.Request(
                api_url,
                headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=60) as response:
                payload = json.loads(response.read().decode("utf-8"))

            if payload.get("status") is True or payload.get("success") is True:
                result = payload.get("result") or payload.get("data") or payload
                if isinstance(result, dict):
                    return result
                raise RuntimeError("Invalid backup API response format")
            message = (
                payload.get("message")
                or payload.get("error")
                or "TikTok video could not be fetched from backup API"
            )
            print(f"[TikTok] Backup API error: {message}")
        except Exception as e:
            print(f"[TikTok] Backup API failed: {e}")

    # ── Backup API No.2 (jerrycoder /down/tiktok)
    try:
        print(f"[TikTok] Trying backup API: {_TIKTOK_BACKUP_API_URL_2}")
        api_url = (
            f"{_TIKTOK_BACKUP_API_URL_2}?url="
            f"{urllib.parse.quote(url, safe='')}"
        )
        req = urllib.request.Request(
            api_url,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))

        if str(payload.get("status")).lower() == "success" and payload.get("result"):
            data = payload["result"].get("data") or {}
            play = (data.get("play") or "").strip()
            if play:
                return {
                    "id": data.get("id"),
                    "title": data.get("title") or "TikTok video",
                    "cover": data.get("cover") or data.get("origin_cover") or "",
                    "duration": int(data.get("duration") or 0),
                    "play": play,
                    "wmplay": (data.get("wmplay") or "").strip() or None,
                    "hdplay": (data.get("hdplay") or "").strip() or None,
                    "music": (data.get("music") or "").strip() or None,
                    "size": data.get("size"),
                    "wm_size": data.get("wm_size"),
                    "hd_size": data.get("hd_size"),
                    "source": "jerrycoder",
                }
            raise RuntimeError("Backup API returned no playable video URL")
        message = (
            str(payload.get("result", {}).get("msg"))
            or payload.get("message")
            or payload.get("error")
            or "TikTok video could not be fetched from backup API 2"
        )
        print(f"[TikTok] Backup API error: {message}")
    except Exception as e:
        print(f"[TikTok] Backup API failed: {e}")

    raise RuntimeError("All TikTok APIs failed (primary and backups)")


def _tiktok_download_url(url, kind):
    encoded = urllib.parse.quote(url, safe="")
    return f"/download/tiktok/file?url={encoded}&type={kind}"


def _build_tiktok_response(url, result):
    """Convert the upstream TikTok response into the UI's format contract."""
    title = result.get("title") or "TikTok video"
    author = result.get("author") or {}
    audio = result.get("music_info") or {}
    video_size = result.get("size_nowm_hd") or result.get("size_nowm")
    audio_size = audio.get("size")

    video_format = {
        "format_id": "tiktok-nowm-hd",
        "ext": "mp4",
        "quality": "HD",
        "height": None,
        "filesize": video_size,
        "filesize_human": _bytes_to_human(video_size) or "Unknown",
        "format_note": "No watermark · Video + Audio",
        "has_audio": True,
        "download_url": _tiktok_download_url(url, "video"),
    }
    audio_format = {
        "format_id": "tiktok-original-audio",
        "ext": "mp3",
        "quality": "Original",
        "abr": None,
        "filesize": audio_size,
        "filesize_human": _bytes_to_human(audio_size) or "Unknown",
        "format_note": "Original TikTok sound",
        "download_url": _tiktok_download_url(url, "audio"),
    }

    return {
        "status": "ok",
        "platform": "tiktok",
        "source": result.get("source") or "nexray",
        "title": title,
        "thumbnail": result.get("cover") or "",
        "duration": result.get("duration") or "N/A",
        "channel": author.get("fullname") or author.get("nickname") or author.get("id") or "",
        "formats": {
            "video_audio": [video_format] if (result.get("data") or result.get("play")) else [],
            "audio_only": [audio_format] if (audio.get("url") or result.get("music")) else [],
        },
    }


# ══════════════════════════════════════════════════════
#  NEXRAY YOUTUBE BACKUP API
#  Used as automatic fallback when yt-dlp extraction fails
# ══════════════════════════════════════════════════════

def _is_youtube_url(url):
    """Return True for YouTube links."""
    try:
        host = urllib.parse.urlparse(url).hostname or ""
        host = host.lower().rstrip(".")
        return host in ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be")
    except Exception:
        return False


def _quality_to_resolusi(quality):
    """Convert quality string like '720p' to nexray resolusi param like '720'."""
    q = quality.strip().lower()
    if q.endswith("p") and q[:-1].isdigit():
        return q[:-1]
    if q.isdigit():
        return q
    return "720"


def _fetch_nexray_yt_result(url, resolusi="720"):
    """Call nexray YouTube API and return (result_dict, raw_json)."""
    api_url = (
        f"{_NEXRAY_YT_API_URL}?url="
        f"{urllib.parse.quote(url, safe='')}"
        f"&resolusi={resolusi}"
    )
    req = urllib.request.Request(
        api_url,
        headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=_NEXRAY_YT_TIMEOUT) as response:
        payload = json.loads(response.read().decode("utf-8"))

    if payload.get("status") is not True or not payload.get("result"):
        message = (
            payload.get("message")
            or payload.get("error")
            or "Nexray YouTube backup failed"
        )
        raise RuntimeError(message)
    return payload["result"], payload


def _nexray_fallback_download(url, quality="720"):
    """
    Download YouTube video via nexray backup API and stream to client.
    Returns a Flask Response or None if the API also fails.
    """
    resolusi = _quality_to_resolusi(quality)
    print(f"[Nexray] Fallback → url={url} resolusi={resolusi}")
    try:
        result, _ = _fetch_nexray_yt_result(url, resolusi)
    except Exception as exc:
        print(f"[Nexray] API error: {exc}")
        return None

    media_url = result.get("url")
    if not media_url:
        print("[Nexray] No download URL in response")
        return None

    title = result.get("title") or "video"
    filename = _safe_filename(title, "mp4")
    print(f"[Nexray] Streaming {filename} from {media_url[:80]}...")

    req = urllib.request.Request(
        media_url,
        headers={"User-Agent": _USER_AGENT, "Referer": "https://www.youtube.com/"},
    )
    try:
        upstream = urllib.request.urlopen(req, timeout=60)
    except Exception as exc:
        print(f"[Nexray] Failed to open media URL: {exc}")
        return None

    content_length = upstream.headers.get("Content-Length")
    content_type = upstream.headers.get("Content-Type", "video/mp4")

    def generate():
        try:
            while True:
                chunk = upstream.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            upstream.close()

    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if content_length:
        headers["Content-Length"] = content_length

    return Response(
        stream_with_context(generate()),
        content_type=content_type,
        headers=headers,
    )


def _nexray_fallback_info(url):
    """
    Get basic video info from nexray (title, thumbnail, etc.) without downloading.
    Used to build a response when yt-dlp fails entirely.
    """
    try:
        result, _ = _fetch_nexray_yt_result(url, "720")
    except Exception:
        return None

    return {
        "title": result.get("title") or "",
        "thumbnail": result.get("thumbnail") or "",
        "duration": result.get("duration"),
        "uploader": result.get("author") or "",
    }


# ══════════════════════════════════════════════════════
#  YOUTUBE BACKUP API (third-party, bypasses Render IP block)
# ══════════════════════════════════════════════════════

def _fetch_yt_backup_combined(url):
    """
    Fetch a COMBINED (video+audio) mp4 via the eliteprotech ytmp4 API.
    Returns a dict or None:
      {title, url, size, ext:'mp4', height:360}
    """
    try:
        api_url = f"{_YT_BACKUP_API_MP4}?url={urllib.parse.quote(url, safe='')}"
        req = urllib.request.Request(
            api_url,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=_YT_BACKUP_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        print(f"[YT-Backup] combined API error: {exc}")
        return None

    res = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    media_url = res.get("url")
    if payload.get("status") is not True or not media_url:
        return None
    return {
        "title": res.get("title") or "",
        "url": media_url,
        "size": res.get("size"),
        "ext": "mp4",
        "height": 360,
    }


def _fetch_yt_backup_multi(url):
    """
    Fetch multi-quality output (video-only mp4/webm + m4a/opus audio) via the
    jerrycoder API. Returns dict(meta, medias) or None:
      meta = {title, thumbnail, duration, channel}
      medias = [{formatId, label, ext, height, abr, url, vcodec, acodec}, ...]
    """
    try:
        api_url = f"{_YT_BACKUP_API_MULTI}?url={urllib.parse.quote(url, safe='')}"
        req = urllib.request.Request(
            api_url,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=_YT_BACKUP_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        print(f"[YT-Backup] multi API error: {exc}")
        return None

    if payload.get("status") != "success" or not isinstance(payload.get("medias"), list):
        return None

    medias = []
    for m in payload["medias"]:
        if not isinstance(m, dict) or not m.get("url"):
            continue
        m_url = m.get("url")
        label = m.get("label") or m.get("quality") or ""
        height = m.get("height")
        ext = (m.get("ext") or "").lower()
        if not ext and label:
            em = re.search(r"(mp4|webm|m4a|opus|3gp)", label)
            ext = em.group(1).lower() if em else ""
        is_video = "video" in label.lower()
        is_audio = "audio" in label.lower()
        # itag 18 / 22 / 37 style = combined, everything else video-only
        fmt_id = str(m.get("formatId") or "")
        combined = fmt_id in ("18", "22", "37", "59") or (
            is_video and not re.match(r"\d{3}", fmt_id or "") is None and fmt_id in ("18", "22", "37", "59")
        )
        medias.append({
            "format_id":   fmt_id,
            "ext":         ext,
            "height":      height,
            "abr":         m.get("bitrate") or _bitrate_from_label(label),
            "vcodec":      "avc1" if ext == "mp4" and is_video else
                           ("vp9" if ext == "webm" and is_video else None),
            "acodec":      "mp4a" if ext.endswith(("m4a", "mp4")) and is_audio else
                           ("opus" if ext == "opus" else None),
            "combined":    combined,
            "title":       payload.get("title") or "",
            "url":         m_url,
        })
    if not medias:
        return None

    return {
        "meta": {
            "title": payload.get("title") or "",
            "thumbnail": payload.get("thumbnail") or payload.get("thumbnail_url") or "",
            "duration": payload.get("duration"),
            "channel": payload.get("channel") or payload.get("author") or "",
        },
        "medias": medias,
    }


def _bitrate_from_label(label):
    m = re.search(r"(\d+)\s*kb/s", label or "")
    if m:
        return m.group(1)
    return None


def _backup_dl_path(base_url, media_url):
    return (
        f"{base_url}/api/yt-backup-dl?url="
        f"{urllib.parse.quote(media_url, safe='')}"
    )


def _build_yt_backup_response(url, base_url):
    """Build the /download/video JSON purely from backup APIs (fast, works on
    blocked datacenter IPs). Returns a Flask Response or None if all backups fail."""
    combo = _fetch_yt_backup_combined(url)
    multi = _fetch_yt_backup_multi(url)

    if not combo and not multi:
        return None

    meta_title = (combo or {}).get("title") or (multi or {}).get("meta", {}).get("title") or "YouTube video"
    meta_thumb = (multi or {}).get("meta", {}).get("thumbnail") or ""
    meta_dur = (multi or {}).get("meta", {}).get("duration")
    meta_chan = (multi or {}).get("meta", {}).get("channel") or ""

    video_audio = []
    if combo:
        dl = _backup_dl_path(base_url, combo["url"])
        video_audio.append({
            "format_id":      "bk-360",
            "ext":            "mp4",
            "height":         360,
            "has_audio":      True,
            "quality":        "360p",
            "format_note":    "Video + Audio (backup)",
            "filesize_human": format_filesize(combo.get("size")) or "Unknown",
            "download_url":   dl,
            "url":            dl,
        })

    video_only = []
    audio_only = []
    if multi:
        for m in multi["medias"]:
            if m.get("ext") == "mp4" and (m.get("vcodec") or "").lower() != "none":
                m_ext = m.get("ext")
                height = m.get("height")
                if not m.get("combined"):
                    # video-only stream from the backup provider — proxy it
                    dl = _backup_dl_path(base_url, m["url"])
                    video_only.append({
                        "format_id":      f"bk-{m['format_id']}",
                        "ext":            m_ext,
                        "height":         height,
                        "has_audio":      False,
                        "quality":        f"{height}p" if height else m.get("label"),
                        "format_note":    "Video only",
                        "vcodec":         m.get("vcodec"),
                        "filesize_human": "Unknown",
                        "download_url":   dl,
                        "url":            dl,
                    })
                else:
                    dl = _backup_dl_path(base_url, m["url"])
                    video_audio.append({
                        "format_id":      f"bk-{m['format_id']}",
                        "ext":            m_ext,
                        "height":         height,
                        "has_audio":      True,
                        "quality":        f"{height}p" if height else m.get("label"),
                        "format_note":    "Video + Audio (backup)",
                        "vcodec":         m.get("vcodec"),
                        "acodec":         "mp4a",
                        "filesize_human": "Unknown",
                        "download_url":   dl,
                        "url":            dl,
                    })
            elif m.get("ext") in ("m4a", "opus") or (m.get("acodec")):
                dl = _backup_dl_path(base_url, m["url"])
                audio_only.append({
                    "format_id":      f"bk-{m['format_id']}",
                    "ext":            m.get("ext"),
                    "has_audio":      True,
                    "quality":        "Audio only",
                    "format_note":    "Audio only",
                    "abr":            m.get("abr"),
                    "acodec":         m.get("acodec"),
                    "filesize_human": "Unknown",
                    "download_url":   dl,
                    "url":            dl,
                })

    return jsonify({
        "status":    "ok",
        "backup":    True,
        "title":     meta_title,
        "thumbnail": meta_thumb,
        "duration":  format_duration(meta_dur),
        "channel":   meta_chan,
        "description": "",
        "formats": {
            "video_audio": video_audio,
            "combined":   video_audio,
            "video_only": video_only,
            "audio_only": audio_only,
        },
        "formats_flat":  video_audio + video_only + audio_only,
        "formats_count": len(video_audio) + len(video_only) + len(audio_only),
    })


def _build_ytdl_video_response(url, info, base_url):
    """Build the /download/video JSON from a successful yt-dlp extraction."""
    combined, video_only, audio_only = parse_formats(info)
    merged_formats = _build_video_audio_formats(
        combined,
        video_only,
        audio_only,
        url,
        base_url,
    )
    return jsonify({
        "status":         "ok",
        "title":          info.get("title"),
        "thumbnail":      info.get("thumbnail"),
        "duration":       format_duration(info.get("duration")),
        "channel":        info.get("uploader"),
        "description":    (info.get("description") or "")[:300],
        "formats": {
            "video_audio": merged_formats,
            "combined":   combined,
            "video_only": video_only,
            "audio_only": audio_only,
        },
        "formats_flat":   combined + video_only + audio_only,
        "formats_count":  len(combined) + len(video_only) + len(audio_only),
    })


def _youtube_race(url, base_url):
    """
    Race yt-dlp (fast on clean IPs like Replit) against the third-party backup
    API (which works even when YouTube blocks the server IP, like Render).
    Returns a Flask Response, or None if every source fails.
    """
    info_box = {}

    def _run_ytdl():
        try:
            info_box["info"] = extract_info(url)
        except Exception as e:
            info_box["err"] = e

    t = threading.Thread(target=_run_ytdl, daemon=True)
    t.start()

    # 1) Short window for yt-dlp to win on clean IPs.
    t.join(timeout=_YTDL_RACE_WINDOW)
    if info_box.get("info"):
        return _build_ytdl_video_response(url, info_box["info"], base_url)

    # 2) Fast backup path — works even when YouTube blocks the server IP.
    backup = _build_yt_backup_response(url, base_url)
    if backup is not None:
        return backup

    # 3) Backup failed too: let yt-dlp finish its full fallback run.
    t.join(timeout=_MEDIACONNECT_FIRST_TIMEOUT)
    if info_box.get("info"):
        return _build_ytdl_video_response(url, info_box["info"], base_url)

    print(f"[YT-Race] all sources failed: {info_box.get('err')}")
    return None


def _proxy_tiktok_media(media_url, filename, content_type, expected_size=None):
    """Proxy a temporary TikTok media URL as a browser download."""
    req = urllib.request.Request(
        media_url,
        headers={"User-Agent": _USER_AGENT, "Referer": "https://www.tiktok.com/"},
    )
    try:
        upstream = urllib.request.urlopen(req, timeout=45)
    except Exception as e:
        print(f"[TikTok] Upstream open failed: {e}")
        return jsonify({"status": "error", "error": f"Upstream videocloud unavailable: {e}"}), 502
    content_length = upstream.headers.get("Content-Length")

    def generate():
        try:
            while True:
                chunk = upstream.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            upstream.close()

    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if content_length or expected_size:
        headers["Content-Length"] = content_length or str(expected_size)

    return Response(
        stream_with_context(generate()),
        content_type=content_type,
        headers=headers,
    )


# ══════════════════════════════════════════════════════
#  PINTEREST DOWNLOAD (nexray backup API)
# ══════════════════════════════════════════════════════

_PINTEREST_TIMEOUT = 60  # nexray can be slow, allow up to 60 s


def _fetch_pinterest_result(url):
    """Fetch Pinterest metadata and media URL from the primary API, with backup fallback."""
    try:
        api_url = (
            f"{_PINTEREST_API_URL}?url="
            f"{urllib.parse.quote(url, safe='')}"
        )
        req = urllib.request.Request(
            api_url,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=_PINTEREST_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))

        if payload.get("status") is True and payload.get("result"):
            return payload["result"]
        print(f"[Pinterest] Primary API returned error: {payload}")
    except Exception as e:
        print(f"[Pinterest] Primary API failed: {e}")

    # ── Backup API No.1 (mkzstyleee, apikey as query param)
    try:
        print(f"[Pinterest] Trying backup API: {_PINTEREST_BACKUP_API_URL}")
        backup_api_url = (
            f"{_PINTEREST_BACKUP_API_URL}?url="
            f"{urllib.parse.quote(url, safe='')}"
            f"&apikey={_PINTEREST_BACKUP_APIKEY}"
        )
        req = urllib.request.Request(
            backup_api_url,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=_PINTEREST_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))

        if payload.get("status") is True or payload.get("success") is True:
            result = payload.get("result") or payload.get("data") or payload
            if isinstance(result, dict):
                return result
            raise RuntimeError("Invalid backup API response format")

        message = (
            payload.get("message")
            or payload.get("error")
            or "Pinterest video could not be fetched from backup API"
        )
        raise RuntimeError(message)
    except Exception as e:
        print(f"[Pinterest] Backup API also failed: {e}")

    # ── Backup API No.2 (theresav, x-apikey header)
    try:
        print(f"[Pinterest] Trying backup API: {_PINTEREST_BACKUP_API_URL_2}")
        backup_api_url_2 = (
            f"{_PINTEREST_BACKUP_API_URL_2}?url="
            f"{urllib.parse.quote(url, safe='')}"
        )
        req = urllib.request.Request(
            backup_api_url_2,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "application/json",
                "x-apikey": _PINTEREST_BACKUP_APIKEY_2,
            },
        )
        with urllib.request.urlopen(req, timeout=_PINTEREST_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))

        if payload.get("status") is True or payload.get("success") is True:
            result = payload.get("result") or payload.get("data") or payload
            if isinstance(result, dict):
                return result
            raise RuntimeError("Invalid backup API response format")

        message = (
            payload.get("message")
            or payload.get("error")
            or "Pinterest video could not be fetched from backup API"
        )
        raise RuntimeError(message)
    except Exception as e:
        print(f"[Pinterest] Backup API 2 also failed: {e}")

    raise RuntimeError("All Pinterest APIs failed (primary and both backups)")


def _build_pinterest_response(url, result):
    """Convert a Pinterest response into the UI's format contract."""
    title = result.get("title") or "Pinterest video"
    video_url = (
        result.get("download_url")
        or result.get("video")
        or (result.get("download_urls") or [None])[0]
    )

    video_format = {
        "format_id": "pinterest-hd",
        "ext": "mp4",
        "quality": "HD",
        "height": None,
        "format_note": "Pinterest video",
        "has_audio": True,
        "download_url": (
            f"/download/pinterest/file?url={urllib.parse.quote(url, safe='')}"
            if video_url else None
        ),
    }

    return {
        "status": "ok",
        "platform": "pinterest",
        "title": title,
        "thumbnail": result.get("thumbnail") or "",
        "duration": None,
        "channel": result.get("author") or "",
        "formats": {
            "video_audio": [video_format] if video_url else [],
            "audio_only": [],
        },
    }


def _proxy_pinterest_media(url, filename="pinterest.mp4"):
    """Proxy a Pinterest video URL as a browser download."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": _USER_AGENT, "Referer": "https://www.pinterest.com/"},
    )
    upstream = urllib.request.urlopen(req, timeout=60)
    content_length = upstream.headers.get("Content-Length")

    def generate():
        try:
            while True:
                chunk = upstream.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            upstream.close()

    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if content_length:
        headers["Content-Length"] = content_length

    return Response(
        stream_with_context(generate()),
        content_type=upstream.headers.get("Content-Type", "video/mp4"),
        headers=headers,
    )


# ══════════════════════════════════════════════════════
#  FACEBOOK DOWNLOAD (theresav API)
# ══════════════════════════════════════════════════════

_FACEBOOK_TIMEOUT = 60


def _fetch_facebook_result(url):
    """Fetch Facebook media.

    Primary: theresav API (x-apikey header).
    Backup:  eliteprotech-apis /facebook1 (no key, multiple video qualities).
    """
    # ── Primary API (theresav)
    try:
        api_url = (
            f"{_FACEBOOK_API_URL}?url="
            f"{urllib.parse.quote(url, safe='')}"
        )
        req = urllib.request.Request(
            api_url,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "application/json",
                "x-apikey": _FACEBOOK_APIKEY,
            },
        )
        with urllib.request.urlopen(req, timeout=_FACEBOOK_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))

        if payload.get("status") is True:
            return payload
        print(f"[Facebook] Primary API returned error: {payload}")
    except Exception as e:
        print(f"[Facebook] Primary API failed: {e}")

    # ── Backup API (eliteprotech /facebook1)
    try:
        print(f"[Facebook] Trying backup API: {_FACEBOOK_BACKUP_API_URL}")
        api_url = (
            f"{_FACEBOOK_BACKUP_API_URL}?url="
            f"{urllib.parse.quote(url, safe='')}"
        )
        req = urllib.request.Request(
            api_url,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=_FACEBOOK_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))

        if payload.get("success") is True and payload.get("results"):
            results = payload["results"]
            best = _pick_best_facebook_quality(results)
            if best:
                return {
                    "status": True,
                    "type": "video",
                    "title": "Facebook video",
                    "download_url": best.get("url"),
                    "download_urls": [r.get("url") for r in results if r.get("url")],
                    "qualities": results,
                    "source": "eliteprotech",
                }
            raise RuntimeError("Backup API returned no usable video URLs")
        message = (
            payload.get("message")
            or payload.get("error")
            or "Facebook media could not be fetched from backup API"
        )
        raise RuntimeError(message)
    except Exception as e:
        print(f"[Facebook] Backup API also failed: {e}")

    # ── Backup API No.2 (jerrycoder /down/fb)
    try:
        print(f"[Facebook] Trying backup API: {_FACEBOOK_BACKUP_API_URL_2}")
        api_url = (
            f"{_FACEBOOK_BACKUP_API_URL_2}?url="
            f"{urllib.parse.quote(url, safe='')}"
        )
        req = urllib.request.Request(
            api_url,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=_FACEBOOK_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))

        if payload.get("status") in ("success", True) and payload.get("results"):
            results = payload["results"]
            if results and isinstance(results, list):
                urls = [r.get("url") for r in results if r.get("url")]
                if urls:
                    best = results[0]
                    return {
                        "status": True,
                        "type": "video",
                        "title": "Facebook video",
                        "download_url": urls[0],
                        "download_urls": urls,
                        "qualities": results,
                        "source": "jerrycoder",
                    }
            raise RuntimeError("Backup API 2 returned no usable video URLs")
        message = (
            payload.get("message")
            or payload.get("error")
            or "Facebook media could not be fetched from backup API 2"
        )
        raise RuntimeError(message)
    except Exception as e:
        print(f"[Facebook] Backup API 2 also failed: {e}")

    raise RuntimeError("All Facebook APIs failed (primary and backups)")


def _pick_best_facebook_quality(results):
    """Pick the best quality video from the eliteprotech results list."""
    if not results:
        return None
    # Prefer HD (720p) if present, else 1080p, else the first directly-rendered URL
    ordered = []
    for r in results:
        q = (r.get("quality") or "").lower()
        if "720" in q or "hd" in q:
            ordered.append((2, r))
        elif "1080" in q:
            ordered.append((3, r))
        elif "360" in q:
            ordered.append((1, r))
        else:
            ordered.append((0, r))
    ordered.sort(key=lambda x: x[0], reverse=True)
    return ordered[0][1] if ordered else None


def _build_facebook_response(url, result):
    """Convert a Facebook response into the UI's format contract."""
    title = result.get("title") or "Facebook video"
    media_type = (result.get("type") or "video").lower()
    images = result.get("images") or []
    image_url = result.get("image_url") or (images[0].get("image_url") if images else None)

    def _fb_format(qlabel=None, fid=None, fnote=None):
        return {
            "format_id": fid or "facebook-hd",
            "ext": "mp4",
            "quality": qlabel or "HD",
            "height": None,
            "format_note": fnote or "Facebook video",
            "has_audio": True,
            "download_url": (
                f"/download/facebook/file?url={urllib.parse.quote(url, safe='')}"
            ),
        }

    video_formats = []
    qualities = result.get("qualities") or []
    if qualities:
        for q in qualities:
            qlabel = q.get("quality")
            if not q.get("url"):
                continue
            video_formats.append(
                _fb_format(
                    qlabel=qlabel,
                    fid=f"facebook-{_safe_format_id(qlabel)}",
                    fnote=f"Facebook video · {qlabel}",
                )
            )
    else:
        download_url = (
            result.get("download_url")
            or (result.get("download_urls") or [None])[0]
        )
        if media_type == "video" and download_url:
            video_formats.append(_fb_format())

    return {
        "status": "ok",
        "platform": "facebook",
        "media_type": media_type,
        "title": title,
        "thumbnail": image_url or "",
        "duration": None,
        "channel": result.get("creator") or result.get("author") or "",
        "formats": {
            "video_audio": video_formats,
            "images": [{"image_url": img.get("image_url")} for img in images],
            "audio_only": [],
        },
    }


def _safe_format_id(label):
    """Turn a quality label like 'Download 720p (HD)' into a safe id."""
    if not label:
        return "hd"
    import re as _re
    m = _re.search(r"(\d+)p", str(label))
    if m:
        return m.group(1) + "p"
    return _re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "hd"


def _proxy_facebook_media(media_url, filename, content_type):
    """Proxy a Facebook/Vercel media URL as a browser download."""
    req = urllib.request.Request(
        media_url,
        headers={"User-Agent": _USER_AGENT},
    )
    upstream = urllib.request.urlopen(req, timeout=60)
    content_length = upstream.headers.get("Content-Length")

    def generate():
        try:
            while True:
                chunk = upstream.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            upstream.close()

    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if content_length:
        headers["Content-Length"] = content_length

    return Response(
        stream_with_context(generate()),
        content_type=content_type or upstream.headers.get("Content-Type", "application/octet-stream"),
        headers=headers,
    )


def _is_cookie_error(exc):
    """Detect if an exception is caused by YouTube bot/IP/cookie block."""
    msg = str(exc).lower()
    return any(k in msg for k in [
        "sign in", "signin", "bot", "429", "too many requests",
        "confirm you're not a bot",
        "blocked", "cookie", "captcha", "please sign in",
        "forbidden", "403", "unable to download api page",
        "api page", "http error",
    ])


def _is_nsig_error(exc):
    """Detect YouTube n-challenge / signature solving failure."""
    msg = str(exc).lower()
    return any(k in msg for k in [
        "signature solving failed", "n challenge", "requested format is not available",
        "only images are available",
    ])


def _is_definitive_unavailable(exc):
    """Detect errors meaning the video itself is gone/private/geo-blocked.

    These are per-video, not per-IP/cookie — retrying every client & cookie
    would waste minutes. We still try one fallback client before bailing.
    """
    msg = str(exc).lower()
    return any(k in msg for k in [
        "this video is unavailable", "video unavailable",
        "private video", "this video has been removed",
        "unavailable for legal reasons", "removed",
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
        "retries":                    1,
        "fragment_retries":           1,
        "socket_timeout":              25,
        "cachedir":                    False,
        "skip_unavailable_fragments": True,
        "http_headers":               {"User-Agent": _USER_AGENT},
        "js_runtimes":                {"node": {"path": node_path}},
    }
    if cookie_path and os.path.isfile(cookie_path):
        opts["cookiefile"] = cookie_path
    if player_client:
        clients = player_client if isinstance(player_client, (list, tuple)) else [player_client]
        opts["extractor_args"] = {"youtube": {"player_client": clients}}
    return opts


def _ydl_extract(opts, url, timeout=None):
    """Run yt-dlp extract_info under an overall timeout; raise if no real formats."""
    timeout = timeout or _EXTRACT_TIMEOUT_SECONDS
    result = {}

    def _run():
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                result["info"] = ydl.extract_info(url, download=False)
        except Exception as e:
            result["error"] = e

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    if thread.is_alive():
        raise RuntimeError(
            f"Extraction timed out after {timeout}s "
            "(YouTube is stalling or blocking this server IP)"
        )
    if "error" in result:
        raise result["error"]

    info = result["info"]
    # Reject storyboard-only results (no real video/audio)
    fmts = info.get("formats", [])
    real = [f for f in fmts if f.get("ext") not in ("mhtml", None) and f.get("url")]
    if not real:
        raise RuntimeError("No downloadable formats found (storyboard only)")
    return info


def _ydl_download_file(url, format_spec, outtmpl, cookie_path=None, player_client=None, timeout=None, merge_output_format=None):
    """
    Download media via yt-dlp itself (carries cookies + client + POT context)
    instead of handing a raw googlevideo URL to ffmpeg, which YouTube 403s.

    outtmpl must contain %(ext)s. Returns the final local file path.
    """
    timeout = timeout or _EXTRACT_TIMEOUT_SECONDS
    opts = get_ydl_opts(cookie_path, player_client)
    opts.update({
        "format":          format_spec,
        "outtmpl":         outtmpl,
        "skip_download":   False,
        "noprogress":      True,
        "quiet":           True,
        "no_warnings":     True,
        "overwrites":      True,
        "retries":         2,
        "fragment_retries": 2,
        # Only merge when asked; otherwise save the raw stream for ffmpeg.
        "merge_output_format": merge_output_format,
    })
    result = {}

    def _run():
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                result["files"] = ydl.download([url])
        except Exception as e:
            result["error"] = e

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    if thread.is_alive():
        raise RuntimeError(f"Download timed out after {timeout}s")
    if "error" in result:
        raise result["error"]
    import glob
    glob_pattern = outtmpl.replace("%(ext)s", "*")
    outs = glob.glob(glob_pattern)
    if not outs:
        # Fall back to the literal.ext wildcard in case the template used a dot.
        outs = glob.glob(outtmpl.replace("%(ext)s", ".*"))
    for p in outs:
        if os.path.isfile(p) and os.path.getsize(p) > 0:
            return p
    raise RuntimeError("yt-dlp produced no file")


def _download_stream_via_ytdl(youtube_url, format_spec, suffix="mp4", merge_output_format=None):
    """
    Download ONE yt-dlp stream (by a format selector such as
    'bestvideo[height<=720]+bestaudio' or 'bestaudio/best') into a temp file.
    Uses yt-dlp itself so cookies / client / POT context are honored and
    YouTube won't 403 the googlevideo URL.
    Returns the local file path, or None after all attempts fail.
    """
    import tempfile, glob
    if not format_spec:
        return None
    tmpdir = tempfile.mkdtemp(prefix="ytdl_")
    base = os.path.join(tmpdir, "stream")
    pattern = f"{base}.*"

    assigned = _cookie_pool.get_next()
    cookie_candidates = [assigned] if assigned else []
    clients = list(_FALLBACK_PLAYER_CLIENTS) + ["default"]

    for cookie in cookie_candidates + [None]:
        for client in clients:
            try:
                _ydl_download_file(
                    youtube_url, format_spec, f"{base}.%(ext)s",
                    cookie_path=cookie, player_client=client,
                    merge_output_format=merge_output_format,
                )
                outs = glob.glob(pattern)
                if outs:
                    return outs[0]
            except Exception as e:
                print(f"[YDL-DL] fail spec={format_spec} client={client} cookie={bool(cookie)}: {str(e)[:120]}")
                continue
    return None


def extract_info(url):
    """
    Extract info with smart fallback strategy (datacenter-IP friendly).
    Datacenter IPs (Replit/Render) are bot-flagged on YouTube's default web
    client and cross-IP cookies often trip the "Sign in to confirm you're not
    a bot" session check, so we try the app clients WITHOUT cookies first.

    Round 1 — bypass player clients (mediaconnect/tv/android/ios/web_safari/mweb)
               WITHOUT cookies — dodge the n-challenge, work from datacenter IPs
    Round 2 — assigned cookie + every player client
    Round 3 — other cookies with the same client list (only on block errors)
    Round 4 — default web client WITHOUT cookie (last resort)
    """
    now = time.monotonic()
    with _info_cache_lock:
        cached = _info_cache.get(url)
        if cached and now - cached["created_at"] < _INFO_CACHE_TTL:
            return cached["info"]
        if cached:
            _info_cache.pop(url, None)

    def cache_and_return(info):
        with _info_cache_lock:
            if len(_info_cache) >= 64:
                oldest_url = min(
                    _info_cache,
                    key=lambda key: _info_cache[key]["created_at"],
                )
                _info_cache.pop(oldest_url, None)
            _info_cache[url] = {"created_at": time.monotonic(), "info": info}
        return info

    assigned  = _cookie_pool.get_next()
    others    = [c for c in _cookie_pool._load_cookies() if c != assigned]
    bypass    = list(_FALLBACK_PLAYER_CLIENTS)
    all_clients = bypass + ["default"]
    last_exc  = None
    is_block  = False
    budget_start = time.monotonic()

    def _remaining():
        return max(1, _EXTRACT_BUDGET_SECONDS - (time.monotonic() - budget_start))

    def _try(opts, fallback_timeout=None):
        nonlocal last_exc, is_block
        timeout = fallback_timeout or _EXTRACT_TIMEOUT_SECONDS
        timeout = min(timeout, _remaining())
        try:
            info = _ydl_extract(opts, url, timeout=timeout)
            return info
        except Exception as e:
            last_exc = e
            print(f"[YDL] FAIL client={opts.get('extractor_args',{}).get('youtube',{}).get('player_client')} "
                  f"cookie={bool(opts.get('cookiefile'))}: {str(e)[:200]}")
            if _is_cookie_error(e) or _is_nsig_error(e):
                is_block = True
            return None

    def _in_budget():
        return (time.monotonic() - budget_start) < _EXTRACT_BUDGET_SECONDS

    # ── Round 1: mediaconnect WITHOUT cookies — the single best shot for
    #    datacenter IPs. Give it a LONG timeout: on slow hosts (Render free
    #    tier) a successful mediaconnect extraction can take 30-60s, and the
    #    old code starved it by walking every client at 20s each.
    if _in_budget():
        info = _try(get_ydl_opts(player_client="mediaconnect"),
                    fallback_timeout=_MEDIACONNECT_FIRST_TIMEOUT)
        if info is not None:
            print("[YDL] OK no-cookie client=mediaconnect (first shot)")
            return cache_and_return(info)

    # ── Round 2: other bypass clients WITHOUT cookies ──────────────────
    for client in [c for c in bypass if c != "mediaconnect"]:
        if not _in_budget():
            break
        info = _try(get_ydl_opts(player_client=client))
        if info is not None:
            print(f"[YDL] OK no-cookie client={client}")
            return cache_and_return(info)

    # A definitive "video is unavailable / private / removed" is per-video
    # (not an IP/cookie issue). One quick no-cookie fallback client check,
    # then bail — retrying the whole cookie×client matrix wastes minutes.
    if last_exc and _is_definitive_unavailable(last_exc):
        for client in ["mediaconnect", "tv"]:
            if not _in_budget():
                break
            info = _try(get_ydl_opts(player_client=client))
            if info is not None:
                print(f"[YDL] OK unavailable-video fallback client={client}")
                return cache_and_return(info)
        raise last_exc

    # ── Round 3: assigned cookie, walk through all player clients ─────
    for client in all_clients:
        if not _in_budget():
            break
        info = _try(get_ydl_opts(assigned, player_client=client))
        if info is not None:
            print(f"[YDL] OK cookie={os.path.basename(assigned)} client={client}")
            return cache_and_return(info)

    # ── Round 4: other cookies (only after a block error) ──────────────
    if is_block:
        for c in others:
            if not _in_budget():
                break
            for client in all_clients:
                info = _try(get_ydl_opts(c, player_client=client))
                if info is not None:
                    print(f"[YDL] OK cookie fallback={os.path.basename(c)} client={client}")
                    return cache_and_return(info)
            if is_block:
                _cookie_pool.mark_blocked(c)

    # ── Round 5: default web client WITHOUT cookie (last resort) ────────
    if _in_budget():
        info = _try(get_ydl_opts(player_client="default"))
        if info is not None:
            print("[YDL] OK default client no-cookie")
            return cache_and_return(info)

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
    try:
        result = subprocess.run(
            full_cmd,
            capture_output=True,
            timeout=_FFMPEG_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise RuntimeError(
            f"Video conversion timed out after {_FFMPEG_TIMEOUT_SECONDS // 60} minutes"
        )
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


def _ffmpeg_stream_response(cmd, content_type, filename):
    """Start ffmpeg immediately and stream its output to the browser.

    Waiting for ffmpeg to finish writing a temporary file made Chrome show a
    permanently loading tab for larger videos. Fragmented MP4 can be sent as
    it is produced, so the download starts while ffmpeg is still working.
    """
    def generate():
        process = subprocess.Popen(
            cmd + ["pipe:1"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        timer = threading.Timer(
            _FFMPEG_TIMEOUT_SECONDS,
            process.kill,
        )
        timer.daemon = True
        timer.start()
        try:
            while True:
                chunk = process.stdout.read(65536)
                if not chunk:
                    break
                yield chunk

            error_output = process.stderr.read().decode(
                "utf-8", errors="replace"
            )
            return_code = process.wait()
            if return_code != 0:
                print(
                    f"[FFmpeg] download failed ({return_code}): "
                    f"{error_output[:400]}"
                )
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
                process.wait()
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()

    return Response(
        stream_with_context(generate()),
        content_type=content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _RE_ENCODE_VIDEO_CMD(video_path):
    """Build an ffmpeg command that re-encodes video+audio to a WhatsApp-safe MP4."""
    return [
        "ffmpeg", "-y",
        "-loglevel", "error",
        "-i", video_path,
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "48000",
        "-movflags", "+faststart",
        "-f", "mp4",
    ]


def _ffmpeg_merge_video_audio(video_path, audio_path, filename):
    """Merge and normalize local video + audio files into a compatible MP4."""
    cmd = [
        "ffmpeg", "-y",
        "-loglevel", "error",
        "-i", video_path,
        "-i", audio_path,
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "48000",
        "-movflags", "+faststart",
        "-f", "mp4",
    ]
    path, size = _run_ffmpeg_to_tempfile(cmd, ".mp4")
    return _serve_tempfile(path, size, "video/mp4", filename)


def _ffmpeg_combined_video(video_path, filename):
    """Fast-remux a combined stream to a WhatsApp-compatible MP4 (fallback: re-encode)."""
    remux = [
        "ffmpeg", "-y",
        "-loglevel", "error",
        "-i", video_path,
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-c", "copy",
        "-movflags", "+faststart",
        "-f", "mp4",
    ]
    try:
        path, size = _run_ffmpeg_to_tempfile(remux, ".mp4")
    except Exception:
        path, size = _run_ffmpeg_to_tempfile(_RE_ENCODE_VIDEO_CMD(video_path), ".mp4")
    return _serve_tempfile(path, size, "video/mp4", filename)


def _ffmpeg_audio_mp3(audio_path, filename):
    """Convert a local audio file to MP3 and serve it with a known size."""
    cmd = [
        "ffmpeg", "-y",
        "-loglevel", "error",
        "-i", audio_path,
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

def _video_format_score(fmt):
    """Prefer MP4/AVC streams when the final output is an MP4."""
    ext_score = 1 if fmt.get("ext") == "mp4" else 0
    codec_score = 1 if (fmt.get("vcodec") or "").startswith("avc1") else 0
    audio_score = 1 if fmt.get("has_audio") else 0
    return ext_score, codec_score, audio_score, fmt.get("tbr") or 0


def _pick_video_for_quality(target_h, combined, video_only):
    """Find the best video stream at or below target height (any URL type)."""
    all_video = combined + video_only
    all_video.sort(
        key=lambda x: ((x.get("height") or 0), _video_format_score(x)),
        reverse=True,
    )

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


def _handle_quality_download(quality, info, combined, video_only, audio_only, youtube_url=None, raw_info=None):
    """
    Build the appropriate ffmpeg response for a given quality string.

    Streams are downloaded via yt-dlp with a *format selector* (so cookies /
    client / POT context apply and YouTube won't 403), then normalized with
    ffmpeg into a WhatsApp-compatible MP4 / MP3.

    Video qualities : '1080p', '720p', '480p', '360p', '240p', '144p'
    Audio qualities : '128', '48'  (kbps as plain integer strings)
    """
    import glob as _glob
    import shutil as _shutil
    title = info.get("title", "video")
    q = quality.strip().lower()
    webpage = youtube_url or info.get("webpage_url") or (raw_info or {}).get("webpage_url")

    def _cleanup(paths):
        for p in paths:
            if not p:
                continue
            try:
                if os.path.isdir(p):
                    _shutil.rmtree(p, ignore_errors=True)
                else:
                    os.unlink(p)
            except OSError:
                pass

    # ── Audio ────────────────────────────────────────────────
    if q.isdigit():
        filename = _safe_filename(title, "mp3")
        if webpage:
            audio_path = _download_stream_via_ytdl(webpage, "bestaudio/best")
            if audio_path:
                try:
                    return _ffmpeg_audio_mp3(audio_path, filename)
                finally:
                    _cleanup([os.path.dirname(audio_path)])
            return jsonify({"error": "Audio stream could not be downloaded"}), 502

        target_abr = int(q)
        if audio_only:
            best = min(audio_only, key=lambda f: abs((f.get("abr") or 0) - target_abr))
        elif combined:
            best = combined[0]
        else:
            return jsonify({"error": "No audio stream found"}), 404
        return _ffmpeg_audio_mp3(best.get("url") or "", filename)

    # ── Video ────────────────────────────────────────────────
    if q.endswith("p") and q[:-1].isdigit():
        target_h = int(q[:-1])
        filename = _safe_filename(title, "mp4")

        if webpage:
            video_path = _download_stream_via_ytdl(
                webpage,
                f"bestvideo[height<={target_h}]+bestaudio/best[height<={target_h}]",
                merge_output_format="mp4",
            )
            if video_path:
                try:
                    return _ffmpeg_combined_video(video_path, filename)
                finally:
                    _cleanup([os.path.dirname(video_path)])
            return jsonify({"error": f"Could not download video for quality '{quality}'"}), 502

        video_fmt = _pick_video_for_quality(target_h, combined, video_only)
        if not video_fmt:
            return jsonify({"error": f"No video stream found for quality '{quality}'"}), 404

        if video_fmt["has_audio"]:
            return _ffmpeg_combined_video(video_fmt.get("url") or "", filename)

        audio_fmt = _pick_best_audio(audio_only, combined)
        if not audio_fmt:
            return _ffmpeg_combined_video(video_fmt.get("url") or "", filename)
        return _ffmpeg_merge_video_audio(video_fmt.get("url") or "", audio_fmt.get("url") or "", filename)

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


def _build_video_audio_formats(
    combined, video_only, audio_only, youtube_url, base_url
):
    """Expose every distinct video height as a reliable merged MP4 download."""
    candidates = {}
    for fmt in combined + video_only:
        height = fmt.get("height")
        if not height or not fmt.get("url"):
            continue
        current = candidates.get(height)
        if current is None or _video_format_score(fmt) > _video_format_score(current):
            candidates[height] = fmt

    best_audio = _pick_best_audio(audio_only, combined)
    encoded_url = urllib.parse.quote(youtube_url, safe="")
    output = []
    for height in sorted(candidates, reverse=True):
        video = candidates[height]
        video_bytes = video.get("filesize")
        audio_bytes = best_audio.get("filesize") if best_audio else None
        merged_bytes = (
            video_bytes + audio_bytes
            if video_bytes and audio_bytes
            else video_bytes or audio_bytes
        )
        quality_url = (
            f"{base_url}/?url={encoded_url}&quality={int(height)}p"
        )
        merged = dict(video)
        merged.update({
            "format_id": f"{video.get('format_id')}+audio",
            "ext": "mp4",
            "acodec": (best_audio or {}).get("acodec") or "aac",
            "abr": (best_audio or {}).get("abr") or 0,
            "has_audio": True,
            "filesize": merged_bytes,
            "filesize_human": (
                _bytes_to_human(merged_bytes)
                if merged_bytes
                else video.get("filesize_human") or "Unknown"
            ),
            "format_note": "Video + Audio",
            "download_url": quality_url,
            "url": quality_url,
        })
        output.append(merged)
    return output


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
        if is_pinterest_url(url):
            try:
                result = _fetch_pinterest_result(url)
                return jsonify(_build_pinterest_response(url, result))
            except Exception as e:
                return jsonify({"success": False, "error": str(e)}), 502
        if is_facebook_url(url):
            try:
                result = _fetch_facebook_result(url)
                return jsonify(_build_facebook_response(url, result))
            except Exception as e:
                return jsonify({"success": False, "error": str(e)}), 502
        try:
            info = extract_info(url)
            combined, video_only, audio_only = parse_formats(info)

            # With quality → stream/download the file
            if quality:
                return _handle_quality_download(quality, info, combined, video_only, audio_only, youtube_url=url, raw_info=info)

            # Without quality → return APK-compatible JSON
            return _build_apk_response(info, combined, video_only, audio_only, url)

        except Exception as e:
            # ── Backup plan: nexray YouTube API ──────────────────
            if _is_youtube_url(url):
                print(f"[YDL] Failed ({e}); trying nexray backup")
                if quality:
                    response = _nexray_fallback_download(url, quality)
                    if response is not None:
                        return response
                else:
                    fallback_info = _nexray_fallback_info(url)
                    if fallback_info:
                        fallback_info.update({"success": True, "backup": True})
                        return jsonify(fallback_info)

            return jsonify({"success": False, "error": str(e)}), 500

    return render_template("index.html")


@app.route("/cookie-status")
def cookie_status():
    """Show status of all cookie files — which are active and which are on cooldown."""
    return jsonify({
        "cookies": _cookie_pool.status(),
        "cooldown_seconds": _COOLDOWN_SECONDS,
    })


@app.route("/version")
def version_status():
    """Diagnostic: which commit/version is deployed (for Render debugging)."""
    version_raw = "unknown"
    try:
        version_file = os.path.join(_BASE_DIR, "VERSION")
        if os.path.isfile(version_file):
            with open(version_file) as f:
                version_raw = f.read().strip()
    except Exception:
        pass
    try:
        ytdlp_ver = yt_dlp.version.__version__
    except Exception:
        ytdlp_ver = getattr(yt_dlp, "__version__", "unknown")
    try:
        sys_ver = __import__("sys").version.split()[0]
    except Exception:
        sys_ver = "unknown"
    return jsonify({
        "version": version_raw,
        "yt_dlp": ytdlp_ver,
        "extract_timeout": _EXTRACT_TIMEOUT_SECONDS,
        "extract_budget": _EXTRACT_BUDGET_SECONDS,
        "fallback_clients": _FALLBACK_PLAYER_CLIENTS,
        "python": sys_ver,
    })


@app.route("/search")
def search():
    query = request.args.get("q", "").strip()
    limit = int(request.args.get("limit", 12))
    if not query:
        return jsonify({"error": "Query is required"}), 400

    def _do_search(cookie_path=None, player_client=None, timeout=None):
        opts = get_ydl_opts(cookie_path, player_client)
        opts["extract_flat"] = True
        opts["playlistend"]  = limit

        result = {}
        timeout = timeout or _EXTRACT_TIMEOUT_SECONDS

        def _run():
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    result["info"] = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
            except Exception as e:
                result["error"] = e

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        thread.join(timeout=timeout)
        if thread.is_alive():
            raise RuntimeError(f"Search timed out after {timeout}s")
        if "error" in result:
            raise result["error"]
        return result.get("info")

    assigned = _cookie_pool.get_next()
    info = None
    last_exc = None
    clients = list(_FALLBACK_PLAYER_CLIENTS) + ["default"]
    is_block = False
    budget_start = time.monotonic()

    def _in_budget():
        return (time.monotonic() - budget_start) < _EXTRACT_BUDGET_SECONDS

    def _try_search(cookie_path, client, timeout=None):
        nonlocal last_exc, is_block
        try:
            remaining = max(1, _EXTRACT_BUDGET_SECONDS - (time.monotonic() - budget_start))
            return _do_search(cookie_path, player_client=client,
                              timeout=min(timeout or _EXTRACT_TIMEOUT_SECONDS, remaining))
        except Exception as e:
            last_exc = e
            if _is_cookie_error(e) or _is_nsig_error(e):
                is_block = True
            return None

    # Round 1: mediaconnect WITHOUT cookies — the best single shot on slow
    # datacenter hosts (Render free tier). Long timeout so it can finish.
    if _in_budget():
        remaining = max(1, _EXTRACT_BUDGET_SECONDS - (time.monotonic() - budget_start))
        info = _try_search(None, "mediaconnect",
                           timeout=min(_MEDIACONNECT_FIRST_TIMEOUT, remaining))
        if info:
            print("[Search] OK → no-cookie mediaconnect (first shot)")

    # Round 2: assigned cookie, walk through player clients
    if info is None:
        for client in clients:
            if not _in_budget():
                break
            info = _try_search(assigned, client)
            if info:
                if assigned:
                    print(f"[Search] OK → {os.path.basename(assigned)} client={client}")
                break

    # Round 3: other cookies (only on block error)
    if info is None and is_block:
        for c in [x for x in _cookie_pool._load_cookies() if x != assigned]:
            if not _in_budget():
                break
            for client in clients:
                info = _try_search(c, client)
                if info:
                    print(f"[Search] OK fallback → {os.path.basename(c)} client={client}")
                    break
            if info:
                break
            if is_block:
                _cookie_pool.mark_blocked(c)

    # Round 4: remaining clients without cookie
    if info is None and _in_budget():
        for client in clients:
            if not _in_budget():
                break
            info = _try_search(None, client)
            if info:
                print(f"[Search] OK → no-cookie client={client}")
                break

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
    if is_tiktok_url(raw):
        return download_tiktok()
    if is_pinterest_url(raw):
        return download_pinterest()
    if is_facebook_url(raw):
        return download_facebook()
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
        # ── Backup: try the nexray YouTube API directly ──────────────
        if _is_youtube_url(url):
            fallback = _nexray_fallback_info(url)
            if fallback:
                return jsonify({
                    "status":            "ok",
                    "backup":            True,
                    "title":             fallback.get("title"),
                    "thumbnail":         fallback.get("thumbnail"),
                    "duration":          fallback.get("duration") or "N/A",
                    "channel":           fallback.get("uploader"),
                    "best_audio":        None,
                    "all_audio_formats": [],
                    "_backup_url":       url,
                    "note":              "yt-dlp blocked, using nexray backup (audio must go through /api/nexray?quality=128)",
                })
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/download/video")
@app.route("/download/video/<path:link>")
def download_video(link=None):
    raw = request.args.get("url") or link or ""
    if not raw:
        return jsonify({"status": "error", "error": "url parameter required"}), 400
    if is_tiktok_url(raw):
        return download_tiktok()
    if is_pinterest_url(raw):
        return download_pinterest()
    if is_facebook_url(raw):
        return download_facebook()
    url = normalize_url(raw)

    if _is_youtube_url(url):
        base_url = request.host_url.rstrip("/")
        try:
            result = _youtube_race(url, base_url)
        except Exception as e:
            print(f"[download/video] race error: {e}")
            result = None
        if result is not None:
            return result
        return jsonify({
            "status": "error",
            "error": "This YouTube video could not be loaded from any source. "
                     "Try again in a moment.",
        }), 502

    # Non-YouTube URL: keep the original yt-dlp path.
    try:
        info = extract_info(url)
        combined, video_only, audio_only = parse_formats(info)
        merged_formats = _build_video_audio_formats(
            combined,
            video_only,
            audio_only,
            url,
            request.host_url.rstrip("/"),
        )
        return jsonify({
            "status":         "ok",
            "title":          info.get("title"),
            "thumbnail":      info.get("thumbnail"),
            "duration":       format_duration(info.get("duration")),
            "channel":        info.get("uploader"),
            "description":    (info.get("description") or "")[:300],
            "formats": {
                "video_audio": merged_formats,
                "combined":   combined,
                "video_only": video_only,
                "audio_only": audio_only,
            },
            "formats_flat":   combined + video_only + audio_only,
            "formats_count":  len(combined) + len(video_only) + len(audio_only),
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/api/nexray")
def nexray_api():
    """Bypass yt-dlp entirely and go straight through the nexray backup API.

    ?url=<youtube-url>&quality=<720|480|360|...>

    Returns video info JSON (no quality) or streams the file (with quality).
    """
    raw_url = request.args.get("url", "").strip()
    quality = request.args.get("quality", "").strip()
    if not raw_url:
        return jsonify({"status": "error", "error": "url parameter required"}), 400
    if not _is_youtube_url(normalize_url(raw_url)):
        return jsonify({"status": "error", "error": "A YouTube URL is required"}), 400

    url = normalize_url(raw_url)
    if quality:
        response = _nexray_fallback_download(url, quality)
        if response is not None:
            return response
        return jsonify({"status": "error", "error": "Nexray backup download failed"}), 502

    fallback_info = _nexray_fallback_info(url)
    if fallback_info is None:
        return jsonify({"status": "error", "error": "Nexray backup failed"}), 502
    fallback_info["status"] = "ok"
    fallback_info["backup"] = True
    return jsonify(fallback_info)


@app.route("/api/yt-backup-dl")
def yt_backup_dl():
    """
    Stream a googlevideo media URL (obtained from a third-party YouTube backup
    API) through this server. Direct browser hits to googlevideo links are
    IP-bound, so we proxy them here instead.
    """
    media_url = request.args.get("url", "").strip()
    if not media_url.startswith("http://") and not media_url.startswith("https://"):
        return jsonify({"status": "error", "error": "invalid url"}), 400

    req = urllib.request.Request(
        media_url,
        headers={"User-Agent": _USER_AGENT, "Referer": "https://www.youtube.com/"},
    )
    range_header = request.headers.get("Range")
    if range_header:
        req.add_header("Range", range_header)

    try:
        upstream = urllib.request.urlopen(req, timeout=90)
    except Exception as exc:
        print(f"[YT-backup-dl] upstream open failed: {exc}")
        return jsonify({"status": "error", "error": f"Upstream unavailable: {exc}"}), 502

    def generate():
        try:
            while True:
                chunk = upstream.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            upstream.close()

    headers = {
        "Content-Disposition": 'attachment; filename="youtube-video.mp4"',
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    content_length = upstream.headers.get("Content-Length")
    if content_length:
        headers["Content-Length"] = content_length
    content_range = upstream.headers.get("Content-Range")
    status = 200
    if range_header and content_range:
        headers["Content-Range"] = content_range
        status = getattr(upstream, "status", 206)

    return Response(
        stream_with_context(generate()),
        status=status,
        content_type=upstream.headers.get("Content-Type", "video/mp4"),
        headers=headers,
    )


@app.route("/download/tiktok")
def download_tiktok():
    raw = request.args.get("url", "").strip()
    if not raw:
        return jsonify({"status": "error", "error": "url parameter required"}), 400
    if not is_tiktok_url(raw):
        return jsonify({"status": "error", "error": "A TikTok URL is required"}), 400

    try:
        url = normalize_url(raw)
        result = _fetch_tiktok_result(url)
        return jsonify(_build_tiktok_response(url, result))
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 502


@app.route("/download/tiktok/file")
def download_tiktok_file():
    raw = request.args.get("url", "").strip()
    media_type = request.args.get("type", "video").lower()
    if not raw:
        return jsonify({"status": "error", "error": "url parameter required"}), 400
    if media_type not in ("video", "audio"):
        return jsonify({"status": "error", "error": "type must be video or audio"}), 400
    if not is_tiktok_url(raw):
        return jsonify({"status": "error", "error": "A TikTok URL is required"}), 400

    try:
        result = _fetch_tiktok_result(normalize_url(raw))
        if media_type == "video":
            media_url = result.get("data") or result.get("play") or result.get("hdplay")
            expected_size = (
                result.get("size_nowm_hd") or result.get("size_nowm")
                or result.get("hd_size") or result.get("size")
            )
            extension = "mp4"
            content_type = "video/mp4"
        else:
            audio = result.get("music_info") or {}
            media_url = audio.get("url") or result.get("music")
            expected_size = audio.get("size") or result.get("size")
            extension = "mp3"
            content_type = "audio/mpeg"

        if not media_url:
            return jsonify({
                "status": "error",
                "error": f"TikTok {media_type} is not available",
            }), 404

        filename = _safe_filename(result.get("title") or "tiktok", extension)
        return _proxy_tiktok_media(
            media_url,
            filename,
            content_type,
            expected_size=expected_size,
        )
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 502


@app.route("/download/pinterest")
def download_pinterest():
    raw = request.args.get("url", "").strip()
    if not raw:
        return jsonify({"status": "error", "error": "url parameter required"}), 400
    if not is_pinterest_url(raw):
        return jsonify({"status": "error", "error": "A Pinterest URL is required"}), 400

    try:
        url = normalize_url(raw)
        result = _fetch_pinterest_result(url)
        return jsonify(_build_pinterest_response(url, result))
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 502


@app.route("/download/pinterest/file")
def download_pinterest_file():
    raw = request.args.get("url", "").strip()
    if not raw:
        return jsonify({"status": "error", "error": "url parameter required"}), 400
    if not is_pinterest_url(raw):
        return jsonify({"status": "error", "error": "A Pinterest URL is required"}), 400

    try:
        url = normalize_url(raw)
        result = _fetch_pinterest_result(url)
        media_url = (
            result.get("download_url")
            or result.get("video")
            or (result.get("download_urls") or [None])[0]
        )
        if not media_url:
            return jsonify({
                "status": "error",
                "error": "Pinterest video is not available",
            }), 404

        filename = _safe_filename(result.get("title") or "pinterest", "mp4")
        return _proxy_pinterest_media(media_url, filename)
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 502


@app.route("/download/facebook")
def download_facebook():
    raw = request.args.get("url", "").strip()
    if not raw:
        return jsonify({"status": "error", "error": "url parameter required"}), 400
    if not is_facebook_url(raw):
        return jsonify({"status": "error", "error": "A Facebook URL is required"}), 400

    try:
        url = normalize_url(raw)
        result = _fetch_facebook_result(url)
        return jsonify(_build_facebook_response(url, result))
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 502


@app.route("/download/facebook/file")
def download_facebook_file():
    raw = request.args.get("url", "").strip()
    media_type = request.args.get("type", "video").lower()
    quality = request.args.get("quality", "").strip()
    if not raw:
        return jsonify({"status": "error", "error": "url parameter required"}), 400
    if media_type not in ("video", "image"):
        return jsonify({"status": "error", "error": "type must be video or image"}), 400
    if not is_facebook_url(raw):
        return jsonify({"status": "error", "error": "A Facebook URL is required"}), 400

    try:
        url = normalize_url(raw)
        result = _fetch_facebook_result(url)

        if media_type == "image":
            images = result.get("images") or []
            media_url = result.get("image_url") or (
                images[0].get("image_url") if images else None
            )
            if not media_url:
                return jsonify({
                    "status": "error",
                    "error": "Facebook image is not available",
                }), 404
            filename = _safe_filename(result.get("title") or "facebook", "jpg")
            return _proxy_facebook_media(media_url, filename, "image/jpeg")

        # Video: when backup API provided multiple qualities, pick by requested quality
        media_url = None
        qualities = result.get("qualities") or []
        if qualities:
            if quality:
                for q in qualities:
                    if str(quality).lower() in str(q.get("quality") or "").lower():
                        media_url = q.get("url")
                        break
            if not media_url:
                best = _pick_best_facebook_quality(qualities)
                media_url = best.get("url") if best else None
        else:
            media_url = (
                result.get("download_url")
                or (result.get("download_urls") or [None])[0]
            )
        if not media_url:
            return jsonify({
                "status": "error",
                "error": "Facebook video is not available",
            }), 404

        filename = _safe_filename(result.get("title") or "facebook", "mp4")
        return _proxy_facebook_media(media_url, filename, "video/mp4")
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 502


# ══════════════════════════════════════════════════════
#  GLOBAL REQUEST TRACKING (after_request)
#  Logs every request for analytics; skips admin assets/logins.
# ══════════════════════════════════════════════════════

@app.after_request
def _track_all_requests(response):
    try:
        if not request.path.startswith("/admin"):
            url_param = request.args.get("url", "")
            platform = _detect_platform(url_param) if url_param else "other"
            status = response.status_code if response else None
            track_request(url=url_param or None, kind=platform, status_code=status)
    except Exception as _e:
        print(f"[Track] error: {_e}", flush=True)
    return response


if __name__ == "__main__":
    # Start the API health monitor + periodic persistence threads
    _health_thread = threading.Thread(target=_health_monitor_loop, daemon=True)
    _health_thread.start()

    def _persist_loop():
        while True:
            _persist_admin_data()
            time.sleep(30)

    _persist_thread = threading.Thread(target=_persist_loop, daemon=True)
    _persist_thread.start()

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
