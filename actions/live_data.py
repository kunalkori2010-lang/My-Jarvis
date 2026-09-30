"""
Live market and world data — real numbers, fetched now.

Every source here is keyless and public, chosen so the assistant can be asked a
question about the world and answer with a fact rather than a recollection:

    stocks / indices / crypto   Yahoo Finance's public chart endpoint
    forex                       Frankfurter (European Central Bank reference
                                rates) — one request covers a currency pair

Deliberately not included: sports scores. The only keyless scoreboard that
answered reliably started returning 403 after a handful of requests, and a tool
that works twice and then silently fails is worse than one that does not exist —
the model would report a scoreline it had invented. A key-based sports feed is
the honest fix, and it can be added when there is a key to give it.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
       "Accept": "application/json"}

# Symbol hints, because the model will pass "bitcoin" far more often than
# "BTC-USD" and a 404 read as "no such thing" is a bad answer to "what's bitcoin".
_ALIASES = {
    "bitcoin": "BTC-USD", "btc": "BTC-USD", "ethereum": "ETH-USD",
    "eth": "ETH-USD", "solana": "SOL-USD", "sol": "SOL-USD",
    "dogecoin": "DOGE-USD", "doge": "DOGE-USD", "cardano": "ADA-USD",
    "ripple": "XRP-USD", "xrp": "XRP-USD", "polkadot": "DOT-USD",
    "litecoin": "LTC-USD", "chainlink": "LINK-USD",
    "s&p 500": "^GSPC", "s&p500": "^GSPC", "sp500": "^GSPC", "s&p": "^GSPC",
    "dow jones": "^DJI", "dow": "^DJI", "nasdaq": "^IXIC",
    "nifty 50": "^NSEI", "nifty": "^NSEI", "sensex": "^BSESN",
    "gold": "GC=F", "silver": "SI=F", "crude oil": "CL=F", "oil": "CL=F",
    "brent": "BZ=F", "natural gas": "NG=F",
}

# Popular tickers, so "how's AAPL" or "Tata Motors" resolves without a search.
_KNOWN = {
    "aapl": "AAPL", "apple": "AAPL", "msft": "MSFT", "microsoft": "MSFT",
    "googl": "GOOGL", "google": "GOOGL", "amzn": "AMZN", "amazon": "AMZN",
    "meta": "META", "facebook": "META", "tsla": "TSLA", "tesla": "TSLA",
    "nvda": "NVDA", "nvidia": "NVDA", "amd": "AMD", "intel": "INTC",
    "ibm": "IBM", "oracle": "ORACLE", "uber": "UBER", "netflix": "NFLX",
    "tcs": "TCS", "infy": "INFY", "infosys": "INFY", "hdfc": "HDFCBANK.NS",
    "reliance": "RELIANCE.NS", "tata": "TATAMOTORS.NS",
}


def _get(url: str, timeout: int = 10):
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _resolve(symbol: str) -> str:
    s = (symbol or "").strip()
    low = s.lower()
    if low in _ALIASES:
        return _ALIASES[low]
    if low in _KNOWN:
        return _KNOWN[low]
    return s.upper()


def _quote(symbol: str) -> dict | None:
    sym = _resolve(symbol)
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
           f"?range=5d&interval=1d")
    data = _get(url)
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        return None
    meta = result[0].get("meta") or {}
    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    if price is None:
        closes = [c for c in (result[0].get("indicators", {})
                              .get("quote", [{}])[0].get("close") or [])
                  if c is not None]
        if not closes:
            return None
        price, prev = closes[-1], closes[0] if len(closes) > 1 else closes[0]
    change = pct = None
    if prev:
        change = price - prev
        pct = (change / prev) * 100
    return {
        "symbol": meta.get("symbol", sym),
        "name":   meta.get("shortName") or meta.get("longName") or sym,
        "price":  price,
        "currency": meta.get("currency", ""),
        "change": change,
        "pct":    pct,
    }


def _fmt_quote(q: dict) -> str:
    cur = f" {q['currency']}" if q.get("currency") else ""
    line = f"  {q['name']} ({q['symbol']}): {_num(q['price'])}{cur}"
    if q.get("pct") is not None:
        arrow = "▲" if q["pct"] >= 0 else "▼"
        line += (f"   {arrow} {_num(q['change'])} "
                 f"({_num(q['pct'])}%) over 5 days")
    return line


def _num(v, places: int = 2) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "?"
    if abs(v) >= 1000:
        return f"{v:,.{places}f}"
    return f"{v:,.{places}f}".rstrip("0").rstrip(".")


def _symbols(raw) -> list[str]:
    items = raw if isinstance(raw, list) else [raw]
    out = []
    for i in items:
        if isinstance(i, str) and i.strip():
            out.extend(p.strip() for p in i.split(",") if p.strip())
    return out[:6]      # six is already more than anyone asks aloud


# ── forex ─────────────────────────────────────────────────────────────────────

def _forex(base: str, targets: list[str]) -> str:
    base = (base or "USD").strip().upper()
    targets = [t.strip().upper() for t in targets if t.strip()][:6]
    if not targets:
        return "Which currency do you want to convert to?"
    url = (f"https://api.frankfurter.app/latest?from={base}"
           f"&to={','.join(targets)}")
    data = _get(url)
    rates = data.get("rates") or {}
    if not rates:
        return f"I have no rate for {base} to {', '.join(targets)}."
    date = data.get("date", "")
    lines = [f"1 {base} on {date}:"]
    for cur, rate in rates.items():
        lines.append(f"  {rate:,.4f} {cur}")
    return "\n".join(lines)


def _quote_one(s: str):
    """Fetch and format one symbol. Returns (symbol, text_or_None, error_or_None)."""
    try:
        q = _quote(s)
    except (urllib.error.URLError, OSError, ValueError) as e:
        return s, None, type(e).__name__
    if not q:
        return s, None, "not found"
    return s, _fmt_quote(q), None


def _fetch_quotes(syms: list[str]) -> tuple[list[str], list[str]]:
    """Quote every symbol concurrently.

    Sequentially, "what's bitcoin, apple, nifty and gold" costs four round
    trips back to back — which in the morning briefing is the difference
    between the market line arriving and the section being dropped as a
    timeout. They are independent requests, so there is no reason to serialise
    them.
    """
    if len(syms) == 1:
        results = [_quote_one(syms[0])]
    else:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(8, len(syms))) as pool:
            results = list(pool.map(_quote_one, syms))

    lines, bad = [], []
    for s, text, err in results:
        if text:
            lines.append(text)
        else:
            bad.append(f"{s} ({err})")
    return lines, bad


# ── entry point ───────────────────────────────────────────────────────────────

def market_action(parameters: dict, player=None, session_memory=None) -> str:
    topic = (parameters.get("topic") or "stocks").strip().lower()
    what = parameters.get("symbols") or parameters.get("symbol") or parameters.get("query")

    try:
        if topic in ("forex", "currency", "fx", "exchange rate"):
            base = parameters.get("base") or "USD"
            targets = _symbols(what) or ["INR"]
            msg = _forex(base, targets)
        else:
            syms = _symbols(what)
            if not syms:
                return ("Which one do you mean? Give me a ticker like AAPL or "
                        "BTC-USD, a company name, or a crypto coin.")
            lines, bad = _fetch_quotes(syms)
            if not lines:
                return f"I couldn't get a quote for {', '.join(bad)}."
            head = ("Live quotes (5-day change):" if len(syms) > 1
                    else "Live quote:")
            msg = head + "\n" + "\n".join(lines)
            if bad:
                msg += f"\n  (no data for {', '.join(bad)})"
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        msg = f"The market data service didn't answer: {e}"
    except Exception as e:
        # ThreadPoolExecutor surfaces a dead worker as BrokenThreadPool rather
        # than the URLError the per-symbol handler already caught, and an
        # unhandled exception here would take down the whole tool call.
        msg = f"The market data service didn't answer: {e}"

    print(f"[LiveData] {msg}")
    if player:
        try:
            player.write_log(f"JARVIS: {msg}")
        except Exception:
            pass
    return msg


TOOL = {
    "name": "live_market_data",
    "description": (
        "Live market and world data: share prices, index levels, crypto prices "
        "and currency exchange rates, fetched right now. Use this for ANY "
        "question about what something costs or is trading at — 'how much is "
        "Bitcoin', 'what's Tesla at', 'USD to INR', 'is the Nifty up' — and never "
        "answer those from memory, because the answer you remember is out of "
        "date by definition. Accepts plain names: company names, 'bitcoin', "
        "'nifty', 'gold', 'crude oil' all resolve."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "topic": {
                "type": "STRING",
                "description": ("'stocks' for shares/indices/crypto (the "
                                "default), or 'forex' for exchange rates."),
            },
            "symbols": {
                "type": "STRING",
                "description": ("What to look up: a ticker ('AAPL', 'NVDA'), a "
                                "company, a coin ('bitcoin'), an index ('nifty'), "
                                "a commodity ('gold'). Comma-separated for "
                                "several. For forex, the target currency."),
            },
            "base": {
                "type": "STRING",
                "description": ("For forex only: the source currency code, e.g. "
                                "'USD' or 'EUR'. Default USD."),
            },
        },
        "required": ["symbols"],
    },
    "handler": market_action,
}
