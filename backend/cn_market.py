from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any


_EASTMONEY_QUOTE = "https://push2.eastmoney.com/api/qt/stock/get"
_EASTMONEY_KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
_TENCENT_QUOTE = "https://qt.gtimg.cn/q="
_USER_AGENT = "Mozilla/5.0 (paper-trading; A-share delayed quote service)"


def _normalize_code(raw: str) -> tuple[str, str]:
    value = str(raw or "").strip().upper()
    if not value:
        raise ValueError("stock code is required")

    market = ""
    code = value
    if "." in value:
        code, market = value.split(".", 1)
    elif value.startswith(("SH", "SZ")) and len(value) >= 8:
        market, code = value[:2], value[2:]

    if not (len(code) == 6 and code.isdigit()):
        raise ValueError(f"invalid A-share code: {raw}")

    if market not in {"SH", "SZ"}:
        # Shanghai: 6xxxxx stocks, 5xxxxx ETFs/funds, 68xxxx STAR Market, 9xxxxx B shares.
        # Shenzhen: 0/3 stocks and most 1xxxxx funds/ETFs.
        market = "SH" if code[0] in {"5", "6", "9"} else "SZ"
    return code, market


def _secid(code: str, market: str) -> str:
    return f"{1 if market == 'SH' else 0}.{code}"


def _http_get(url: str, timeout: float = 8.0) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://quote.eastmoney.com/",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def _scaled(value: Any, divisor: float) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        return round(float(value) / divisor, 4)
    except (TypeError, ValueError):
        return None


def _number(value: Any) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _eastmoney_quote(raw_code: str) -> dict[str, Any]:
    code, market = _normalize_code(raw_code)
    fields = "f57,f58,f43,f44,f45,f46,f47,f48,f60,f169,f170"
    params = urllib.parse.urlencode({"secid": _secid(code, market), "fields": fields})
    payload = json.loads(_http_get(f"{_EASTMONEY_QUOTE}?{params}").decode("utf-8"))
    data = payload.get("data") or {}
    if not data or data.get("f43") in (None, "-"):
        raise RuntimeError(f"Eastmoney returned no quote for {code}.{market}")

    return {
        "code": code,
        "symbol": f"{code}.{market}",
        "name": data.get("f58") or "",
        "price": _scaled(data.get("f43"), 1000),
        "open": _scaled(data.get("f46"), 1000),
        "high": _scaled(data.get("f44"), 1000),
        "low": _scaled(data.get("f45"), 1000),
        "preClose": _scaled(data.get("f60"), 1000),
        "change": _scaled(data.get("f169"), 1000),
        "changePct": _scaled(data.get("f170"), 100),
        "volume": _number(data.get("f47")),
        "amount": _number(data.get("f48")),
        "source": "eastmoney",
        "retrievedAt": datetime.now(timezone.utc).isoformat(),
    }


def _tencent_quote(raw_code: str) -> dict[str, Any]:
    code, market = _normalize_code(raw_code)
    prefix = market.lower()
    body = _http_get(f"{_TENCENT_QUOTE}{prefix}{code}").decode("gb18030", errors="replace")
    if '="' not in body:
        raise RuntimeError(f"Tencent returned no quote for {code}.{market}")
    text = body.split('="', 1)[1].rsplit('"', 1)[0]
    parts = text.split("~")
    if len(parts) < 36 or not parts[3]:
        raise RuntimeError(f"Tencent returned malformed quote for {code}.{market}")

    price = _number(parts[3])
    pre_close = _number(parts[4])
    change = _number(parts[31]) if len(parts) > 31 else None
    change_pct = _number(parts[32]) if len(parts) > 32 else None
    if change is None and price is not None and pre_close is not None:
        change = round(price - pre_close, 4)
    if change_pct is None and change is not None and pre_close:
        change_pct = round(change / pre_close * 100, 4)

    return {
        "code": code,
        "symbol": f"{code}.{market}",
        "name": parts[1] if len(parts) > 1 else "",
        "price": price,
        "open": _number(parts[5]) if len(parts) > 5 else None,
        "high": _number(parts[33]) if len(parts) > 33 else None,
        "low": _number(parts[34]) if len(parts) > 34 else None,
        "preClose": pre_close,
        "change": change,
        "changePct": change_pct,
        "volume": _number(parts[6]) if len(parts) > 6 else None,
        "amount": _number(parts[37]) if len(parts) > 37 else None,
        "source": "tencent",
        "retrievedAt": datetime.now(timezone.utc).isoformat(),
    }


def get_quote(raw_code: str) -> dict[str, Any]:
    errors: list[str] = []
    try:
        return _eastmoney_quote(raw_code)
    except Exception as exc:  # noqa: BLE001 - public market fallback
        errors.append(f"eastmoney: {exc}")
    try:
        quote = _tencent_quote(raw_code)
        quote["fallbackReason"] = errors[-1]
        return quote
    except Exception as exc:  # noqa: BLE001
        errors.append(f"tencent: {exc}")
    raise RuntimeError("; ".join(errors))


def get_quotes(raw_codes: list[str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in raw_codes:
        raw = str(raw).strip()
        if not raw:
            continue
        try:
            result.append(get_quote(raw))
        except Exception as exc:  # noqa: BLE001 - keep batch partially useful
            try:
                code, market = _normalize_code(raw)
                symbol = f"{code}.{market}"
            except Exception:
                symbol = raw
            result.append({"code": raw, "symbol": symbol, "error": str(exc)})
    return result


def get_kline(raw_code: str, period: str = "daily", limit: int = 100) -> dict[str, Any]:
    code, market = _normalize_code(raw_code)
    period_map = {"daily": 101, "day": 101, "1d": 101, "weekly": 102, "week": 102, "monthly": 103, "month": 103}
    klt = period_map.get(str(period).lower())
    if klt is None:
        raise ValueError("period must be daily, weekly, or monthly")
    limit = max(1, min(int(limit), 1000))
    params = urllib.parse.urlencode(
        {
            "secid": _secid(code, market),
            "klt": klt,
            "fqt": 0,
            "lmt": limit,
            "end": "20500101",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        }
    )
    payload = json.loads(_http_get(f"{_EASTMONEY_KLINE}?{params}").decode("utf-8"))
    data = payload.get("data") or {}
    rows = data.get("klines") or []
    bars: list[dict[str, Any]] = []
    for row in rows:
        p = row.split(",")
        if len(p) < 7:
            continue
        bars.append(
            {
                "date": p[0],
                "open": _number(p[1]),
                "close": _number(p[2]),
                "high": _number(p[3]),
                "low": _number(p[4]),
                "volume": _number(p[5]),
                "amount": _number(p[6]),
                "amplitudePct": _number(p[7]) if len(p) > 7 else None,
                "changePct": _number(p[8]) if len(p) > 8 else None,
                "change": _number(p[9]) if len(p) > 9 else None,
                "turnoverPct": _number(p[10]) if len(p) > 10 else None,
            }
        )
    return {
        "code": code,
        "symbol": f"{code}.{market}",
        "name": data.get("name") or "",
        "period": str(period).lower(),
        "source": "eastmoney",
        "bars": bars,
        "retrievedAt": datetime.now(timezone.utc).isoformat(),
    }
