from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from pathlib import Path
import json
import os
import re

PORT = 8765
VALID_RANGES = {"6mo", "1y", "2y", "5y", "10y", "max"}
WEB_ROOT = Path(__file__).resolve().parent
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
DAILY_CHUNK_SECONDS = 9 * 365 * 24 * 60 * 60


def fetch_yahoo_chart(symbol, params):
    query = urlencode(params)
    request = Request(
        f"{YAHOO_CHART_URL.format(symbol)}?{query}",
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/125.0 Safari/537.36"
            )
        }
    )
    with urlopen(request, timeout=20) as response:
        return json.loads(response.read())


def fetch_full_daily_history(symbol):
    # Yahoo's `range=max` silently changes daily data to monthly for long histories.
    # Use that response only to discover the earliest available date, then fetch
    # daily bars in sub-10-year windows and merge them by timestamp.
    overview = fetch_yahoo_chart(symbol, {
        "range": "max",
        "interval": "1mo",
        "events": "div,splits"
    })
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
        response_data = fetch_yahoo_chart(symbol, {
            "period1": period_start,
            "period2": period_end,
            "interval": "1d",
            "events": "div,splits"
        })
        chart = response_data.get("chart", {})
        if chart.get("error"):
            error = chart["error"]
            raise RuntimeError(error.get("description") or error.get("code") or "Yahoo Finance error")

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

    if not bars_by_timestamp or latest_result is None:
        raise RuntimeError("Yahoo Finance returned no daily price history")

    ordered_timestamps = sorted(bars_by_timestamp)
    quote_fields = set().union(*(bar.keys() for bar in bars_by_timestamp.values()))
    latest_result["timestamp"] = ordered_timestamps
    latest_result.setdefault("indicators", {})["quote"] = [{
        field: [bars_by_timestamp[timestamp].get(field) for timestamp in ordered_timestamps]
        for field in quote_fields
    }]
    latest_result.setdefault("meta", {})["dataGranularity"] = "1d"
    overview["chart"]["result"] = [latest_result]
    overview["chart"]["error"] = None
    return overview


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            return self.send_json(200, {"status": "ok"})
        if path in {"/", "/ema-candlestick-chart.html"}:
            return self.send_chart_page()
        if path != "/api/chart":
            return self.send_json(404, {"error": "Not found"})

        params = parse_qs(urlparse(self.path).query)
        symbol = params.get("symbol", [""])[0].upper()
        period = params.get("range", ["1y"])[0]

        if not re.fullmatch(r"[A-Z0-9^=._-]{1,20}", symbol):
            return self.send_json(400, {"error": "Invalid ticker symbol"})

        if period not in VALID_RANGES:
            return self.send_json(400, {"error": "Invalid history range"})

        try:
            if period == "max":
                data = fetch_full_daily_history(symbol)
            else:
                data = fetch_yahoo_chart(symbol, {
                    "range": period,
                    "interval": "1d",
                    "events": "div,splits"
                })
            body = json.dumps(data).encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        except HTTPError as error:
            self.send_json(
                502,
                {"error": f"Yahoo Finance returned HTTP {error.code}"}
            )
        except Exception as error:
            self.send_json(
                502,
                {"error": f"Could not fetch Yahoo data: {error}"}
            )

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
        body = chart_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)


HOST = "0.0.0.0"
server = ThreadingHTTPServer((HOST, int(os.environ.get("PORT", PORT))), Handler)

print(f"Chart server listening on {HOST}:{server.server_port}")

server.serve_forever()