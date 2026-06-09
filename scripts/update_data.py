from __future__ import annotations

import hashlib
import json
import math
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib import error, parse, request

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
LATEST_PATH = DATA_DIR / "latest.json"
BOARD_HISTORY_PATH = DATA_DIR / "board_history.json"
CN_TZ = timezone(timedelta(hours=8))

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
EASTMONEY_UT = "bd1d9ddb04089700cf9c27f6f7426281"
EASTMONEY_HOSTS = (
    "push2.eastmoney.com",
    "4.push2.eastmoney.com",
    "60.push2.eastmoney.com",
    "61.push2.eastmoney.com",
    "82.push2.eastmoney.com",
)

STARTED = time.monotonic()
MAX_RUNTIME_SECONDS = 210
HTTP_TIMEOUT_SECONDS = 8
MAX_BOARDS_FOR_MEMBERS = 6
MAX_MEMBERS_PER_BOARD = 50
MAX_STOCK_CANDIDATES = 45

errors: list[str] = []
source_health: dict[str, dict[str, Any]] = {}


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(CN_TZ)
    today = now.strftime("%Y-%m-%d")
    history = load_json(BOARD_HISTORY_PATH, {"items": []})
    previous = load_json(LATEST_PATH, {})

    boards = fetch_all_boards()
    if not boards:
        write_fallback(now, today, previous)
        update_board_history(history, today, [])
        return

    rank_map = {board["code"]: index + 1 for index, board in enumerate(boards)}
    evaluated_boards = evaluate_boards(boards, rank_map, history)
    qualified_boards = [board for board in evaluated_boards if board["qualified"]]
    recommendations = build_recommendations(qualified_boards)
    news = fetch_news() if remaining_seconds() > 25 else []

    latest = {
        "meta": {
            "schemaVersion": 4,
            "generatedAt": now.isoformat(),
            "tradingDate": today,
            "mode": "fast-snapshot",
            "sourceHealth": source_list(),
            "errors": errors[:30],
            "runtimeSeconds": round(time.monotonic() - STARTED, 2),
        },
        "market": {
            "recommendationCount": len(recommendations),
            "qualifiedBoardCount": len(qualified_boards),
        },
        "boards": strip_members(evaluated_boards[:12]),
        "recommendations": recommendations,
        "news": news or previous.get("news", [])[:8],
    }

    write_json(LATEST_PATH, latest)
    update_board_history(history, today, evaluated_boards)


def write_fallback(now: datetime, today: str, previous: dict[str, Any]) -> None:
    previous_boards = previous.get("boards", [])
    previous_recs = previous.get("recommendations", [])
    previous_news = previous.get("news", [])
    if previous_boards or previous_recs:
        mark_source(
            "Historical cache",
            True,
            "data/latest.json",
            "External sources failed; kept the last successful snapshot.",
        )

    latest = {
        "meta": {
            "schemaVersion": 4,
            "generatedAt": now.isoformat(),
            "tradingDate": today,
            "mode": "cached-fallback" if (previous_boards or previous_recs) else "no-current-data",
            "sourceHealth": source_list(previous),
            "errors": (errors or ["No usable quote rows returned this run."])[:30],
            "runtimeSeconds": round(time.monotonic() - STARTED, 2),
        },
        "market": {
            "recommendationCount": len(previous_recs),
            "qualifiedBoardCount": len(previous_boards),
        },
        "boards": previous_boards[:12],
        "recommendations": previous_recs[:5],
        "news": previous_news[:8],
    }
    write_json(LATEST_PATH, latest)


def fetch_all_boards() -> list[dict[str, Any]]:
    boards: list[dict[str, Any]] = []
    boards.extend(fetch_board_list("Industry board", "m:90+t:2"))
    boards.extend(fetch_board_list("Concept board", "m:90+t:3"))

    result = dedupe_boards(boards)
    if result:
        return result[:60]

    stocks = fetch_all_a_shares()
    if stocks:
        mark_source(
            "Eastmoney",
            True,
            "https://quote.eastmoney.com/",
            "Board endpoint unavailable; used A-share quote snapshot grouped by industry.",
        )
        errors.append("Board endpoints returned no rows; used all-A-share industry fallback.")
        return boards_from_a_shares(stocks)[:60]

    return []


def fetch_board_list(kind: str, fs: str) -> list[dict[str, Any]]:
    fields = "f12,f14,f2,f3,f4,f5,f6,f7,f8,f15,f16,f17,f18,f20,f62"
    rows = safe_clist("Eastmoney", fs, fields, page_size=240)
    result = []
    for row in rows:
        code = str(row.get("f12") or "")
        if not code.startswith("BK"):
            continue
        result.append(
            {
                "code": code,
                "name": text(row.get("f14")),
                "kind": kind,
                "price": number(row.get("f2")),
                "pct": number(row.get("f3")),
                "amount": number(row.get("f6")),
                "turnover": number(row.get("f8")),
                "open": number(row.get("f17")),
                "preClose": number(row.get("f18")),
                "mainNet": number(row.get("f62")),
            }
        )
    return result


def fetch_all_a_shares() -> list[dict[str, Any]]:
    fields = "f12,f13,f14,f2,f3,f4,f5,f6,f7,f8,f10,f15,f16,f17,f18,f20,f21,f62,f66,f100"
    fs = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
    rows = safe_clist("Eastmoney", fs, fields, page_size=5000)
    stocks = [normalize_stock_quote(row) for row in rows]
    stocks = [item for item in stocks if item.get("code") and item.get("price")]
    stocks.sort(key=lambda item: (item.get("pct") or -999, item.get("amount") or 0), reverse=True)
    return stocks


def boards_from_a_shares(stocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for stock in stocks:
        industry = stock.get("industry") or "Unclassified"
        grouped.setdefault(industry, []).append(stock)

    boards = []
    for name, members in grouped.items():
        tradable = [item for item in members if item.get("pct") is not None]
        if len(tradable) < 3:
            continue
        amount = sum(item.get("amount") or 0 for item in tradable)
        pct = weighted_average(tradable, "pct", "amount")
        turnover = weighted_average(tradable, "turnover", "amount")
        main_net = sum(item.get("mainNet") or 0 for item in tradable)
        code = "IND_" + hashlib.md5(name.encode("utf-8")).hexdigest()[:8]
        tradable.sort(key=lambda item: (item.get("pct") or -999, item.get("amount") or 0), reverse=True)
        boards.append(
            {
                "code": code,
                "name": name,
                "kind": "Industry fallback",
                "price": None,
                "pct": pct,
                "amount": amount,
                "turnover": turnover,
                "open": None,
                "preClose": None,
                "mainNet": main_net,
                "members": tradable[:MAX_MEMBERS_PER_BOARD],
            }
        )
    boards.sort(key=lambda item: (item.get("pct") or -999, item.get("amount") or 0), reverse=True)
    return boards


def evaluate_boards(
    boards: list[dict[str, Any]],
    rank_map: dict[str, int],
    history: dict[str, Any],
) -> list[dict[str, Any]]:
    evaluated = []
    for board in boards[:MAX_BOARDS_FOR_MEMBERS]:
        if deadline_hit():
            errors.append("Runtime budget reached while evaluating boards.")
            break
        members = board.get("members") or fetch_board_members(board["code"])
        limit_up_count = sum(1 for item in members if is_limit_up(item))
        big_up_count = sum(1 for item in members if (item.get("pct") or 0) >= 5)
        leader_ok = limit_up_count >= 1 or any((item.get("pct") or 0) >= 7 for item in members[:8])
        continuous_ok = board_continuous_ok(board["code"], rank_map, history)
        rank = rank_map.get(board["code"], 99)
        amount_ok = (board.get("amount") or 0) >= 8_000_000_000
        position_proxy_ok = rank <= 20 and (board.get("pct") or 0) >= 1

        criteria = [
            ("Board rank is strong or improving", rank <= 10 or continuous_ok),
            ("Limit-up or large-gain members are active", limit_up_count >= 1 or big_up_count >= 4),
            ("Board turnover amount is active", amount_ok),
            ("Leader stocks are visible", leader_ok),
            ("Board momentum is positive", position_proxy_ok),
        ]
        passed_labels = [label for label, ok in criteria if ok]
        passed = len(passed_labels)
        score = round((passed / 5) * 82 + max(0, 18 - rank * 0.35), 2)

        evaluated.append(
            {
                **board,
                "rank": rank,
                "passed": passed,
                "qualified": passed >= 4,
                "score": min(100, score),
                "criteria": passed_labels,
                "limitUpCount": limit_up_count,
                "bigUpCount": big_up_count,
                "members": members[:30],
            }
        )

    for board in boards[MAX_BOARDS_FOR_MEMBERS:12]:
        rank = rank_map.get(board["code"], 99)
        evaluated.append(
            {
                **board,
                "rank": rank,
                "passed": 2 if rank <= 12 else 1,
                "qualified": False,
                "score": max(0, 48 - rank),
                "criteria": ["Board is near the top of today's movers"],
                "limitUpCount": 0,
                "bigUpCount": 0,
                "members": [],
            }
        )

    evaluated.sort(key=lambda item: (item["qualified"], item["score"], item.get("pct") or 0), reverse=True)
    return evaluated


def build_recommendations(qualified_boards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for board in qualified_boards[:MAX_BOARDS_FOR_MEMBERS]:
        for member in board.get("members", []):
            code = member.get("code")
            if not code or code in seen:
                continue
            seen.add(code)
            if stock_prefilter(member):
                candidates.append((member, board))

    candidates.sort(
        key=lambda pair: (
            pair[1].get("score") or 0,
            pair[0].get("mainNet") or 0,
            pair[0].get("volumeRatio") or 0,
            pair[0].get("pct") or 0,
            pair[0].get("amount") or 0,
        ),
        reverse=True,
    )

    recommendations = []
    for quote, board in candidates[:MAX_STOCK_CANDIDATES]:
        item = evaluate_stock_snapshot(quote, board)
        if item:
            recommendations.append(item)
        if len(recommendations) >= 5:
            break

    for index, item in enumerate(recommendations, start=1):
        item["rank"] = index
    return recommendations


def evaluate_stock_snapshot(quote: dict[str, Any], board: dict[str, Any]) -> dict[str, Any] | None:
    price = quote.get("price")
    pre_close = quote.get("preClose")
    open_price = quote.get("open")
    if not price or not pre_close or not open_price:
        return None

    pct = quote.get("pct") or 0
    if is_late_chase(quote):
        return None

    volume_ratio = quote.get("volumeRatio") or 0
    turnover = quote.get("turnover") or 0
    main_net = quote.get("mainNet") or 0
    super_net = quote.get("superNet") or 0

    criteria = [
        ("Board is in main-rising condition", (board.get("passed") or 0) >= 4),
        ("Stock volume is rising but not exhausted", volume_ratio >= 1.2),
        ("Main capital flow is positive", main_net > 0 or super_net > 0),
        ("Turnover is active", 4 <= turnover <= 32),
        ("Gain is below chase-risk zone", 1.2 <= pct <= 8.2),
        ("Price holds above opening area", price >= open_price * 0.995),
    ]
    passed_labels = [label for label, ok in criteria if ok]
    if len(passed_labels) < 5:
        return None

    buy_plan = choose_buy_plan_snapshot(quote)
    if not buy_plan:
        return None

    entry = buy_plan["priceRange"][0]
    stop_loss = round2(max(entry * 0.95, min(entry * 0.985, open_price * 0.985)))
    target = estimate_target_snapshot(entry, price, pre_close, quote, board, buy_plan)
    win_rate = round(min(94, 48 + len(passed_labels) * 5 + (board.get("passed") or 0) * 3 + buy_plan["quality"]), 1)

    return {
        "rank": None,
        "code": quote["code"],
        "name": quote["name"],
        "market": quote.get("market"),
        "price": round2(price),
        "pct": pct,
        "amount": quote.get("amount"),
        "turnover": turnover,
        "confidence": win_rate,
        "winRate": win_rate,
        "board": {
            "code": board["code"],
            "name": board["name"],
            "passed": board.get("passed"),
            "score": board.get("score"),
        },
        "criteria": {
            "board": board.get("criteria", []),
            "stock": passed_labels,
        },
        "buyPlan": buy_plan,
        "sellPlan": {
            "targetPrice": target["targetPrice"],
            "targetTime": target["targetTime"],
            "strategy": target["strategy"],
            "takeProfit": target["targetPrice"],
            "timeWindow": target["targetTime"],
        },
        "stopPlan": {
            "stopLoss": stop_loss,
            "rules": [
                "Lose VWAP and fail to recover within 10 minutes",
                "Break the entry trigger or intraday platform low",
                "Single-stock loss should stay within 5%",
            ],
        },
        "sourceLinks": stock_source_links(quote["code"]),
        "sparkline": [],
    }


def choose_buy_plan_snapshot(quote: dict[str, Any]) -> dict[str, Any] | None:
    price = quote["price"]
    pre_close = quote["preClose"]
    open_price = quote["open"]
    low = quote.get("low") or price
    pct = quote.get("pct") or 0
    gap = (open_price / pre_close - 1) * 100
    low_drawdown = (low / pre_close - 1) * 100

    if 1 <= gap <= 4 and low_drawdown >= -2 and price >= open_price and pct <= 6.8:
        anchor = max(open_price, price * 0.992)
        return make_buy_plan("Early strength confirmation", anchor, "09:25-09:40", "Confirm near open/VWAP after a controlled gap-up.", 16, 5)
    if 2 <= pct <= 5.8 and price >= open_price * 0.995:
        anchor = max(open_price, price * 0.985)
        return make_buy_plan("Main-rise pullback entry", anchor, "09:30-10:00", "Prefer controlled pullbacks near open/VWAP, not limit-up chasing.", 13, 3)
    if 3 <= pct <= 7.2 and price > open_price:
        anchor = max(open_price, price * 0.985)
        return make_buy_plan("First pullback plan", anchor, "09:40-10:10", "Wait for the first lower-volume pullback after the initial push.", 12, 2)
    return None


def make_buy_plan(
    label: str,
    anchor_price: float,
    window: str,
    trigger: str,
    quality: int,
    priority: int,
) -> dict[str, Any]:
    return {
        "type": label,
        "timeWindow": window,
        "trigger": trigger,
        "priceRange": [round2(anchor_price * 0.992), round2(anchor_price * 1.006)],
        "quality": quality,
        "priority": priority,
    }


def estimate_target_snapshot(
    entry: float,
    price: float,
    pre_close: float,
    quote: dict[str, Any],
    board: dict[str, Any],
    buy_plan: dict[str, Any],
) -> dict[str, Any]:
    base_gain = 0.055
    if (board.get("passed") or 0) >= 5:
        base_gain += 0.012
    if (quote.get("volumeRatio") or 0) >= 2:
        base_gain += 0.01
    if buy_plan["priority"] >= 5:
        base_gain += 0.012
    limit_price = pre_close * (1.2 if quote["code"].startswith(("30", "68")) else 1.1)
    target_price = round2(min(max(entry * (1 + base_gain), price * 1.025), limit_price * 0.985))
    return {
        "targetPrice": target_price,
        "targetTime": "Intraday 10:00-14:30; extend to next morning only if the board stays strong.",
        "strategy": "Take profit in batches near target, on intraday weakness, or if the board cools down.",
    }


def fetch_board_members(board_code: str) -> list[dict[str, Any]]:
    fields = "f12,f13,f14,f2,f3,f4,f5,f6,f7,f8,f10,f15,f16,f17,f18,f20,f21,f62,f66,f100"
    rows = safe_clist("Eastmoney", f"b:{board_code}+f:!50", fields, page_size=MAX_MEMBERS_PER_BOARD)
    members = [normalize_stock_quote(row) for row in rows]
    members = [item for item in members if item.get("code") and item.get("price")]
    members.sort(key=lambda item: (item.get("pct") or -999, item.get("amount") or 0), reverse=True)
    return members


def normalize_stock_quote(row: dict[str, Any]) -> dict[str, Any]:
    code = str(row.get("f12") or "")
    return {
        "code": code,
        "name": text(row.get("f14")),
        "market": stock_market(code),
        "price": number(row.get("f2")),
        "pct": number(row.get("f3")),
        "change": number(row.get("f4")),
        "volume": number(row.get("f5")),
        "amount": number(row.get("f6")),
        "amplitude": number(row.get("f7")),
        "turnover": number(row.get("f8")),
        "volumeRatio": number(row.get("f10")),
        "high": number(row.get("f15")),
        "low": number(row.get("f16")),
        "open": number(row.get("f17")),
        "preClose": number(row.get("f18")),
        "floatMarketCap": number(row.get("f21")),
        "mainNet": number(row.get("f62")),
        "superNet": number(row.get("f66")),
        "industry": text(row.get("f100")),
    }


def stock_prefilter(item: dict[str, Any]) -> bool:
    code = item.get("code") or ""
    name = item.get("name") or ""
    if not re.match(r"^(00|30|60|68|83|87|43)\d{4}$", code):
        return False
    if any(flag in name.upper() for flag in ("ST", "*ST", "退")):
        return False
    turnover = item.get("turnover")
    pct = item.get("pct") or 0
    amount = item.get("amount") or 0
    if turnover is None or turnover < 4 or turnover > 40:
        return False
    if is_late_chase(item):
        return False
    return 1.2 <= pct <= 8.2 and amount >= 120_000_000


def fetch_news() -> list[dict[str, Any]]:
    news = []
    for source, url in [
        ("Eastmoney News", "https://finance.eastmoney.com/a/cjjsp.html"),
        ("10jqka", "https://stock.10jqka.com.cn/"),
        ("Yicai", "https://www.yicai.com/news/"),
    ]:
        if deadline_hit():
            break
        html = fetch_text(source, url)
        if not html:
            continue
        for href, label in re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.I):
            title = clean_html(label)
            if useful_news_title(title):
                news.append({"source": source, "title": title, "url": parse.urljoin(url, href), "time": ""})
            if len(news) >= 8:
                return dedupe_news(news)
    return dedupe_news(news)


def safe_clist(source: str, fs: str, fields: str, page_size: int) -> list[dict[str, Any]]:
    params = {
        "pn": "1",
        "pz": str(page_size),
        "po": "1",
        "np": "1",
        "ut": EASTMONEY_UT,
        "fltt": "2",
        "invt": "2",
        "fid": "f3",
        "fs": fs,
        "fields": fields,
    }
    try:
        data = eastmoney_json(source, "https://push2.eastmoney.com/api/qt/clist/get", params)
        return data.get("data", {}).get("diff", []) if data else []
    except Exception as exc:
        message = f"{source} clist failed: {short_error(exc)}"
        print(message)
        errors.append(message)
        return []


def eastmoney_json(source: str, url: str, params: dict[str, Any]) -> dict[str, Any]:
    query = parse.urlencode(params)
    urls = [f"{url}?{query}"]
    if "push2.eastmoney.com" in url:
        urls = [f"{url.replace('push2.eastmoney.com', host)}?{query}" for host in EASTMONEY_HOSTS]

    last_exc: Exception | None = None
    for full_url in urls:
        if deadline_hit():
            break
        try:
            payload = fetch_bytes(source, full_url, referer="https://quote.eastmoney.com/").decode("utf-8", "ignore")
            parsed = json.loads(strip_jsonp(payload))
            rows = parsed.get("data", {}).get("diff")
            if rows:
                mark_source(source, True, full_url, "OK")
            return parsed
        except Exception as exc:
            last_exc = exc
    raise RuntimeError(last_exc or "request timeout")


def fetch_text(source: str, url: str) -> str:
    try:
        raw = fetch_bytes(source, url, accept="text/html,application/xhtml+xml")
        mark_source(source, True, url, "Optional news source OK")
        return raw.decode("utf-8", "ignore")
    except Exception as exc:
        errors.append(f"{source} news skipped: {short_error(exc)}")
        mark_source(source, True, url, "Optional news source skipped; quote data is unaffected.")
        return ""


def fetch_bytes(
    source: str,
    url: str,
    referer: str = "",
    accept: str = "application/json,text/plain,*/*",
) -> bytes:
    req = request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": accept,
            "Referer": referer or url,
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )
    try:
        with request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return response.read()
    except error.HTTPError as exc:
        if exc.code in (403, 404, 429, 500, 502, 503, 504):
            raise RuntimeError(f"HTTP {exc.code}") from exc
        raise
    except (error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(short_error(exc)) from exc


def strip_jsonp(payload: str) -> str:
    payload = payload.strip()
    if payload.startswith("{"):
        return payload
    match = re.search(r"\((\{.*\})\)\s*;?$", payload, re.S)
    return match.group(1) if match else payload


def dedupe_boards(boards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped = {}
    for board in boards:
        if board.get("code"):
            deduped[board["code"]] = board
    result = list(deduped.values())
    result.sort(key=lambda item: (item.get("pct") or -999, item.get("amount") or 0), reverse=True)
    return result


def weighted_average(items: list[dict[str, Any]], value_key: str, weight_key: str) -> float | None:
    weighted_sum = 0.0
    total_weight = 0.0
    simple_values = []
    for item in items:
        value = item.get(value_key)
        if value is None:
            continue
        simple_values.append(value)
        weight = item.get(weight_key) or 0
        if weight > 0:
            weighted_sum += value * weight
            total_weight += weight
    if total_weight:
        return round2(weighted_sum / total_weight)
    if simple_values:
        return round2(sum(simple_values) / len(simple_values))
    return None


def strip_members(boards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in board.items() if key != "members"} for board in boards]


def board_continuous_ok(code: str, current_ranks: dict[str, int], history: dict[str, Any]) -> bool:
    current = current_ranks.get(code, 999)
    previous = [item.get("ranks", {}).get(code, 999) for item in history.get("items", [])[-2:]]
    return bool(current <= 10 and previous and previous[-1] <= 20)


def update_board_history(history: dict[str, Any], today: str, boards: list[dict[str, Any]]) -> None:
    ranks = {board["code"]: board["rank"] for board in boards if board.get("rank")}
    items = [entry for entry in history.get("items", []) if entry.get("date") != today]
    items.append({"date": today, "ranks": ranks})
    write_json(BOARD_HISTORY_PATH, {"items": items[-30:]})


def is_late_chase(item: dict[str, Any]) -> bool:
    pct = item.get("pct")
    code = item.get("code") or ""
    name = item.get("name") or ""
    if pct is None:
        return False
    if "ST" in name.upper():
        return pct >= 4.2
    if code.startswith(("30", "68")):
        return pct >= 16.5
    return pct >= 8.4


def is_limit_up(item: dict[str, Any]) -> bool:
    pct = item.get("pct")
    code = item.get("code") or ""
    name = item.get("name") or ""
    if pct is None:
        return False
    if "ST" in name.upper():
        return pct >= 4.8
    if code.startswith(("30", "68")):
        return pct >= 19.5
    return pct >= 9.8


def dedupe_news(news: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    result = []
    for item in news:
        if item["title"] in seen:
            continue
        seen.add(item["title"])
        result.append(item)
    return result[:8]


def useful_news_title(title: str) -> bool:
    if len(title) < 8 or len(title) > 80:
        return False
    keywords = ("A股", "股市", "市场", "板块", "资金", "沪指", "深成指", "创业板", "证券", "行情")
    return any(keyword in title for keyword in keywords)


def clean_html(raw: str) -> str:
    raw = re.sub(r"<script.*?</script>", "", raw, flags=re.S | re.I)
    raw = re.sub(r"<style.*?</style>", "", raw, flags=re.S | re.I)
    raw = re.sub(r"<[^>]+>", "", raw)
    return raw.replace("&nbsp;", " ").strip()


def stock_market(code: str) -> str:
    if code.startswith("6"):
        return "SH"
    if code.startswith(("4", "8")):
        return "BJ"
    return "SZ"


def stock_source_links(code: str) -> list[dict[str, str]]:
    prefix = "sh" if code.startswith("6") else "sz"
    return [
        {"name": "Eastmoney quote", "url": f"https://quote.eastmoney.com/{prefix}{code}.html"},
        {"name": "10jqka stock page", "url": f"https://stockpage.10jqka.com.cn/{code}/"},
        {"name": "Yicai news", "url": "https://www.yicai.com/news/"},
    ]


def deadline_hit() -> bool:
    return time.monotonic() - STARTED >= MAX_RUNTIME_SECONDS


def remaining_seconds() -> float:
    return MAX_RUNTIME_SECONDS - (time.monotonic() - STARTED)


def round2(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value) + 1e-9, 2)


def number(value: Any) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def text(value: Any) -> str:
    return str(value or "").strip()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def mark_source(name: str, ok: bool, url: str, note: str) -> None:
    previous = source_health.get(name)
    source_health[name] = {
        "name": name,
        "ok": bool(ok or (previous and previous.get("ok"))),
        "url": url,
        "note": note,
    }


def source_list(previous: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    previous_sources = {}
    if previous:
        for item in previous.get("meta", {}).get("sourceHealth", []):
            if item.get("name"):
                previous_sources[item["name"]] = item

    defaults = [
        ("Eastmoney", "https://quote.eastmoney.com/", "Quote, board, and stock snapshot source", False),
        ("10jqka", "https://www.10jqka.com.cn/", "Optional news/reference source", True),
        ("Yicai", "https://www.yicai.com/", "Optional news/reference source", True),
    ]
    for name, url, note, optional_ok in defaults:
        if name in source_health:
            continue
        previous_item = previous_sources.get(name)
        if previous_item and previous_item.get("ok"):
            source_health[name] = {**previous_item, "note": "Previous successful source status retained during fallback."}
        else:
            source_health[name] = {"name": name, "ok": optional_ok, "url": url, "note": note}
    return list(source_health.values())


def short_error(exc: Any) -> str:
    message = str(exc)
    message = re.sub(r"\s+", " ", message).strip()
    message = message.encode("ascii", "ignore").decode("ascii") or exc.__class__.__name__
    return message[:180]


if __name__ == "__main__":
    main()
