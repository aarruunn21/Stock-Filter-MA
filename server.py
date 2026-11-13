from http.cookiejar import CookieJar
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener
import json
import os
import re
import signal
import threading
import time

PORT = 8765
VALID_RANGES = {"6mo", "1y", "2y", "5y", "10y", "max"}
WEB_ROOT = Path(__file__).resolve().parent
YAHOO_HOSTS = [
    "https://query1.finance.yahoo.com",
    "https://query2.finance.yahoo.com",
]
DAILY_CHUNK_SECONDS = 9 * 365 * 24 * 60 * 60
CACHE_TTL_SECONDS = 300
CRUMB_TTL_SECONDS = 1800  # Refresh session every 30 minutes

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

_cache = {}
_cache_lock = threading.Lock()

_session_lock = threading.Lock()
_yahoo_session = {"opener": None, "crumb": None, "ts": 0.0}


def get_cached(key):
    with _cache_lock:
        entry = _cache.get(key)
        if entry and (time.monotonic() - entry["ts"]) < CACHE_TTL_SECONDS:
            return entry["data"]
        _cache.pop(key, None)
    return None


def set_cached(key, data):
    with _cache_lock:
        if len(_cache) >= 100:
            _cache.pop(next(iter(_cache)), None)
        _cache[key] = {"ts": time.monotonic(), "data": data}


def get_yahoo_session(force_refresh=False):
    with _session_lock:
        now = time.monotonic()
        if (
            not force_refresh
            and _yahoo_session["opener"] is not None
            and _yahoo_session["crumb"]
            and (now - _yahoo_session["ts"]) < CRUMB_TTL_SECONDS
        ):
            return _yahoo_session["opener"], _yahoo_session["crumb"]

        jar = CookieJar()
        opener = build_opener(HTTPCookieProcessor(jar))
        opener.addheaders = [
            ("User-Agent", USER_AGENT),
            ("Accept", "*/*"),
            ("Accept-Language", "en-US,en;q=0.9"),
        ]

        # Step 1: Collect Yahoo session cookies; this endpoint may return 404.
        try:
            opener.open("https://fc.yahoo.com", timeout=15).read()
        except OSError:
            pass

        # Step 2: Request the crumb tied to those cookies
        crumb = None
        for host in YAHOO_HOSTS:
            try:
                with opener.open(f"{host}/v1/test/getcrumb", timeout=15) as resp:
                    text = resp.read().decode("utf-8").strip()
                    if text and "<html" not in text.lower():
                        crumb = text
                        break
            except Exception:
                continue

        if not crumb:
            raise RuntimeError("Failed to obtain Yahoo Finance session crumb")

        _yahoo_session["opener"] = opener
        _yahoo_session["crumb"] = crumb
        _yahoo_session["ts"] = now
        return opener, crumb


def fetch_yahoo_chart(symbol, params, retries=2):
    force_refresh = False

    for attempt in range(retries + 1):
        opener, crumb = get_yahoo_session(force_refresh=force_refresh)
        force_refresh = False

        query_params = dict(params)
        query_params["crumb"] = crumb
        query = urlencode(query_params)

        host = YAHOO_HOSTS[attempt % len(YAHOO_HOSTS)]
        url = f"{host}/v8/finance/chart/{symbol}?{query}"

        try:
            req = Request(url, headers={"User-Agent": USER_AGENT})
            with opener.open(req, timeout=20) as response:
                return json.loads(response.read())
        except HTTPError as err:
            if err.code in (401, 403) and attempt < retries:
                # Crumb expired or rejected; force a fresh cookie/crumb session
                force_refresh = True
                time.sleep(0.5)
                continue
            if err.code == 429 and attempt < retries:
                # Rate-limited on this host; back off and rotate to query2
                time.sleep(1.5 * (attempt + 1))
                continue
            raise


def fetch_full_daily_history(symbol):
    overview = fetch_yahoo_chart(
        symbol, {"range": "max", "interval": "1mo", "events": "div,splits"}
    )
    overview_result = (overview.get("chart", {}).get("result") or [None])[0]
    if not overview_result or not overview_result.get("timestamp"):
        return overview

    timestamps = overview_result["timestamp"]
    start = min(timestamps)
    end = max(timestamps) + 24 * 60 * 60
    bars_by_timestamp = {}
    latest_result = None

    for period_start in range(start, end, DAILY_CHUNK_SECONDS):
        period_end = min(period_start + DAILY_CHUNK_SECONDS, end)
        response_data = fetch_yahoo_chart(
            symbol,
            {
                "period1": period_start,
                "period2": period_end,
                "interval": "1d",
                "events": "div,splits",
            },
        )
        chart = response_data.get("chart", {})
        if chart.get("error"):
            error = chart["error"]
            raise RuntimeError(
                error.get("description")
                or error.get("code")
                or "Yahoo Finance error"
            )
        result = (chart.get("result") or [None])[0]
        if not result or not result.get("timestamp"):
            continue

        quote = (result.get("indicators", {}).get("quote") or [{}])[0]
        for index, timestamp in enumerate(result["timestamp"]):
            bars_by_timestamp[timestamp] = {
                key: values[index] if index < len(values) else None
                for key, values in quote.items()
                if isinstance(values, list)
            }
        latest_result = result
        time.sleep(0.15)

    if not bars_by_timestamp or latest_result is None:
        raise RuntimeError("Yahoo Finance returned no daily price history")

    ordered_timestamps = sorted(bars_by_timestamp)
    quote_fields = set().union(*(bar.keys() for bar in bars_by_timestamp.values()))
    latest_result["timestamp"] = ordered_timestamps
    latest_result.setdefault("indicators", {})["quote"] = [
        {
            field: [
                bars_by_timestamp[timestamp].get(field)
                for timestamp in ordered_timestamps
            ]
            for field in quote_fields
        }
    ]
    latest_result.setdefault("meta", {})["dataGranularity"] = "1d"
    overview["chart"]["result"] = [latest_result]
    overview["chart"]["error"] = None
    return overview


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def log_message(self, format, *args):
        if "/healthz" not in (args if args else ""):
            print(f"{self.address_string()} - {format % args}", flush=True)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            return self.send_json(200, {"status": "ok"})
        if path in {"/", "/ema-candlestick-chart.html"}:
            return self.send_chart_page()
        if path != "/api/chart":
            return self.send_json(404, {"error": "Not found"})

        params = parse_qs(urlparse(self.path).query)
        symbol = params.get("symbol", [""])[0].strip().upper()
        period = params.get("range", ["1y"])[0]

        if not re.fullmatch(r"[A-Z0-9^=._-]{1,20}", symbol):
            return self.send_json(400, {"error": "Invalid ticker symbol"})
        if period not in VALID_RANGES:
            return self.send_json(400, {"error": "Invalid history range"})

        cache_key = (symbol, period)
        cached_body = get_cached(cache_key)
        if cached_body is not None:
            return self.send_raw_json(200, cached_body)

        try:
            if period == "max":
                data = fetch_full_daily_history(symbol)
            else:
                data = fetch_yahoo_chart(
                    symbol,
                    {"range": period, "interval": "1d", "events": "div,splits"},
                )
            body = json.dumps(data).encode("utf-8")
            set_cached(cache_key, body)
            self.send_raw_json(200, body)
        except HTTPError as error:
            self.send_json(
                502, {"error": f"Yahoo Finance returned HTTP {error.code}"}
            )
        except Exception as error:
            self.send_json(
                502, {"error": f"Could not fetch Yahoo data: {error}"}
            )

    def send_raw_json(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=60")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_chart_page(self):
        chart_path = WEB_ROOT / "ema-candlestick-chart.html"
        if not chart_path.exists():
            return self.send_json(500, {"error": "Chart HTML file missing"})
        body = chart_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    HOST = "0.0.0.0"
    server = ThreadingHTTPServer(
        (HOST, int(os.environ.get("PORT", PORT))), Handler
    )

    def shutdown_handler(signum, frame):
        _ = signum, frame
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
