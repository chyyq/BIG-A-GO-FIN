from __future__ import annotations

import json
import math
import random
import re
import time
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from typing import Any, Callable, TypeVar
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
T = TypeVar("T")

source_health: dict[str, dict[str, Any]] = {}
errors: list[str] = []


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(CN_TZ)
    today = now.strftime("%Y-%m-%d")
    previous_latest = load_json(LATEST_PATH, {})
    history = load_json(BOARD_HISTORY_PATH, {"items": []})

    boards = fetch_all_boards()
    board_rank_map = {board["code"]: index + 1 for index, board in enumerate(boards)}
    evaluated_boards = evaluate_boards(boards, history, board_rank_map)
    qualified_boards = [board for board in evaluated_boards if board["qualified"]]
    recommendations = build_recommendations(qualified_boards)
    news = fetch_news() or previous_latest.get("news", [])[:10]

    latest = {
        "meta": {
            "schemaVersion": 3,
            "generatedAt": now.isoformat(),
            "tradingDate": today,
            "mode": "GitHub Actions 早盘主升量化更新",
            "sourceHealth": source_list(),
            "errors": errors,
        },
        "market": {
            "recommendationCount": len(recommendations),
            "qualifiedBoardCount": len(qualified_boards),
        },
        "boards": evaluated_boards[:12],
        "recommendations": recommendations,
        "news": news,
    }

    if not boards:
        latest["meta"]["mode"] = "行情源暂时不可用，已生成空推荐数据"
        errors.append("东方财富板块接口本次未返回可用数据，Action 已不中断；稍后自动或手动重跑即可。")

    write_json(LATEST_PATH, latest)
    update_board_history(history, today, evaluated_boards)


def fetch_all_boards() -> list[dict[str, Any]]:
    boards: list[dict[str, Any]] = []
    boards.extend(safe_call("行业板块列表", lambda: fetch_board_list("行业板块", "m:90+t:2"), []))
    time.sleep(0.2)
    boards.extend(safe_call("概念板块列表", lambda: fetch_board_list("概念板块", "m:90+t:3"), []))

    deduped = {}
    for board in boards:
        deduped[board["code"]] = board
    result = list(deduped.values())
    result.sort(key=lambda item: (item.get("pct") or -999), reverse=True)
    return result[:80]


def fetch_board_list(kind: str, fs: str) -> list[dict[str, Any]]:
    fields = "f12,f14,f2,f3,f4,f5,f6,f7,f8,f15,f16,f17,f18,f20,f62"
    rows = eastmoney_clist(kind, fs, fields, page_size=300)
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
                "high": number(row.get("f15")),
                "low": number(row.get("f16")),
                "open": number(row.get("f17")),
                "preClose": number(row.get("f18")),
                "mainNet": number(row.get("f62")),
            }
        )
    return result


def evaluate_boards(
    boards: list[dict[str, Any]],
    history: dict[str, Any],
    board_rank_map: dict[str, int],
) -> list[dict[str, Any]]:
    evaluated = []
    for board in boards[:36]:
        members = safe_call(f"{board['name']} 成分股", lambda board=board: fetch_board_members(board["code"]), [])
        time.sleep(0.08)
        kline = safe_call(f"{board['name']} K线", lambda board=board: fetch_kline(f"90.{board['code']}", limit=40), [])

        limit_up_count = sum(1 for item in members if is_limit_up(item))
        big_up_count = sum(1 for item in members if (item.get("pct") or 0) >= 5)
        amount_ratio = ratio_to_previous_avg(board.get("amount"), [row["amount"] for row in kline])
        position_ok = board_position_ok(kline)
        continuous_ok = board_continuous_ok(board["code"], board_rank_map, history)
        leader_ok = limit_up_count >= 1 or has_trend_leader(members)

        criteria = [
            ("板块连续强势", continuous_ok),
            ("涨停≥2且大涨≥5", limit_up_count >= 2 and big_up_count >= 5),
            ("成交额较5日均值放大≥30%", amount_ratio is not None and amount_ratio >= 1.3),
            ("出现2板核心或趋势龙头", leader_ok),
            ("指数平台突破或新高附近", position_ok),
        ]
        passed_labels = [label for label, ok in criteria if ok]
        passed = len(passed_labels)
        rank = board_rank_map.get(board["code"], 80)
        score = round((passed / 5) * 100 + max(0, 18 - rank * 0.25), 2)
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
                "amountRatio": amount_ratio,
                "members": members[:30],
            }
        )
    evaluated.sort(key=lambda item: (item["qualified"], item["score"], item.get("pct") or 0), reverse=True)
    return evaluated


def build_recommendations(qualified_boards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for board in qualified_boards[:10]:
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
    for quote, board in candidates[:120]:
        evaluated = safe_call(
            f"{quote.get('name', quote.get('code'))} 个股评估",
            lambda quote=quote, board=board: evaluate_stock(quote, board),
            None,
        )
        if evaluated:
            recommendations.append(evaluated)
        if len(recommendations) >= 8:
            break

    recommendations.sort(key=lambda item: (item["buyPlan"].get("priority", 0), item["winRate"]), reverse=True)
    for index, item in enumerate(recommendations[:5], start=1):
        item["rank"] = index
    return recommendations[:5]


def evaluate_stock(quote: dict[str, Any], board: dict[str, Any]) -> dict[str, Any] | None:
    if is_late_chase(quote):
        return None

    secid = stock_secid(quote["code"])
    kline = fetch_kline(secid, limit=80)
    if len(kline) < 25:
        return None
    flow = safe_call(f"{quote['name']} 资金流", lambda: fetch_flow(secid), [])
    trends = safe_call(f"{quote['name']} 分时", lambda: fetch_trends(secid), [])

    price = quote.get("price") or kline[-1]["close"]
    pre_close = quote.get("preClose") or kline[-2]["close"]
    today_amount = quote.get("amount") or kline[-1]["amount"]
    amount_ratio = ratio_to_average(today_amount, [row["amount"] for row in kline[-6:-1]])

    previous_high = max(row["high"] for row in kline[-26:-1])
    breakout_edge_ok = previous_high * 0.985 <= price <= previous_high * 1.035
    breakout_ok = price >= previous_high * 1.01 or (quote.get("high") or 0) >= previous_high * 1.02
    flow_ok = flow_continuous_ok(flow, quote)
    ma5 = average([row["close"] for row in kline[-5:]])
    ma10 = average([row["close"] for row in kline[-10:]])
    prev_ma5 = average([row["close"] for row in kline[-6:-1]])
    ma_ok = (ma5 is not None and ma10 is not None and ma5 > ma10) or (
        ma5 is not None and prev_ma5 is not None and ma5 > prev_ma5
    )
    turnover = quote.get("turnover")
    turnover_ok = turnover is not None and 6 <= turnover <= 32
    intraday_ok = intraday_strength_ok(trends, quote)

    criteria = [
        ("成交额放大>1.5倍", amount_ratio is not None and amount_ratio > 1.5),
        ("平台/前高/箱体突破沿", breakout_edge_ok or breakout_ok),
        ("主力资金连续流入且超大单为正", flow_ok),
        ("5日线上穿10日线或持续向上", ma_ok),
        ("换手率6%-32%", turnover_ok),
        ("分时站稳均价线", intraday_ok),
    ]
    passed_labels = [label for label, ok in criteria if ok]
    if len(passed_labels) < 5:
        return None

    buy_plan_data = choose_buy_plan(quote, trends, kline, pre_close)
    if not buy_plan_data:
        return None

    entry = buy_plan_data["priceRange"][0]
    stop_loss = estimate_stop_loss(entry, kline)
    target = estimate_sell_target(entry, price, pre_close, kline, quote, board, buy_plan_data)
    win_rate = round(
        min(96, (len(passed_labels) / 6) * 52 + (board.get("passed", 0) / 5) * 28 + buy_plan_data["quality"]),
        1,
    )

    return {
        "rank": None,
        "code": quote["code"],
        "name": quote["name"],
        "market": quote.get("market"),
        "price": round2(price),
        "pct": quote.get("pct"),
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
        "buyPlan": buy_plan_data,
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
                "跌破均价线10分钟不收回",
                "跌破启动点或平台位",
                "单票最大亏损不超过5%",
            ],
        },
        "sourceLinks": stock_source_links(quote["code"]),
        "sparkline": [round2(row["close"]) for row in kline[-30:]],
    }


def choose_buy_plan(
    quote: dict[str, Any],
    trends: list[dict[str, Any]],
    kline: list[dict[str, Any]],
    pre_close: float,
) -> dict[str, Any] | None:
    price = quote.get("price")
    open_price = quote.get("open")
    low = quote.get("low")
    if not price or not open_price or not pre_close:
        return None

    pct = quote.get("pct") or 0
    previous_high = max(row["high"] for row in kline[-26:-1])
    ma5 = average([row["close"] for row in kline[-5:]]) or price
    avg_price = latest_avg_price(trends) or open_price
    gap = (open_price / pre_close - 1) * 100
    low_drawdown = (low / pre_close - 1) * 100 if low else -99

    if is_late_chase(quote):
        return None

    if 1 <= gap <= 4 and low_drawdown >= -2 and price > max(open_price, avg_price) and pct <= 6.8:
        trigger = "高开不追板，站稳均价线后在开盘价附近确认"
        entry = max(open_price, avg_price, price * 0.992)
        return make_buy_plan("弱转强早盘确认", entry, "09:25-09:40", trigger, 16, priority=5)

    if previous_high * 0.985 <= price <= previous_high * 1.025 and pct <= 6.5:
        trigger = "接近平台/前高突破沿，放量站稳后买，不等涨停"
        entry = max(previous_high * 0.995, ma5, price * 0.988)
        return make_buy_plan("平台突破前沿", entry, "09:30-09:50", trigger, 15, priority=4)

    if 2 <= pct <= 5.8 and price >= avg_price and price >= ma5 * 0.995:
        trigger = "主升板块内温和放量，贴近5日线/均价线低吸"
        entry = max(ma5, avg_price, price * 0.985)
        return make_buy_plan("主升确认低吸", entry, "09:30-10:00", trigger, 13, priority=3)

    if 3 <= pct <= 7.2 and intraday_pullback_ok(trends):
        trigger = "拉升后第一次缩量回踩均价线，重新放量拐头"
        entry = max(avg_price, price * 0.985)
        return make_buy_plan("第一次分时回踩", entry, "09:40-10:00", trigger, 12, priority=2)

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


def estimate_stop_loss(entry: float, kline: list[dict[str, Any]]) -> float:
    platform = recent_platform_stop(kline)
    return round2(max(entry * 0.95, min(entry * 0.985, platform)))


def estimate_sell_target(
    entry: float,
    price: float,
    pre_close: float,
    kline: list[dict[str, Any]],
    quote: dict[str, Any],
    board: dict[str, Any],
    buy_plan: dict[str, Any],
) -> dict[str, Any]:
    recent_high = max(row["high"] for row in kline[-20:])
    board_boost = 0.01 if (board.get("passed") or 0) >= 5 else 0
    liquidity_boost = 0.01 if (quote.get("volumeRatio") or 0) >= 2 else 0
    base_gain = 0.055 + board_boost + liquidity_boost
    if buy_plan["type"] in {"弱转强早盘确认", "平台突破前沿"}:
        base_gain += 0.015
    target_price = max(entry * (1 + base_gain), recent_high * 1.01, price * 1.025)
    limit_price = pre_close * (1.2 if quote["code"].startswith(("30", "68")) else 1.1)
    target_price = round2(min(target_price, limit_price * 0.985))

    if buy_plan["type"] in {"弱转强早盘确认", "平台突破前沿"}:
        target_time = "当日 10:00-14:30；若承接强可延至次日早盘"
    elif buy_plan["type"] == "主升确认低吸":
        target_time = "当日午后至次日早盘"
    else:
        target_time = "当日 10:30-14:30"

    return {
        "targetPrice": target_price,
        "targetTime": target_time,
        "strategy": "到达预估峰值、分时跌破均价线或板块退潮时分批卖出",
    }


def fetch_board_members(board_code: str) -> list[dict[str, Any]]:
    fields = "f12,f13,f14,f2,f3,f4,f5,f6,f7,f8,f10,f15,f16,f17,f18,f20,f21,f62,f66,f100"
    rows = eastmoney_clist("东方财富", f"b:{board_code}+f:!50", fields, page_size=120)
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
    return 1.2 <= pct <= 8.2 and amount >= 150_000_000


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


def fetch_kline(secid: str, limit: int = 60) -> list[dict[str, Any]]:
    params = {
        "secid": secid,
        "klt": "101",
        "fqt": "1",
        "beg": "20200101",
        "end": "20500101",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
    }
    data = eastmoney_json("东方财富", "https://push2his.eastmoney.com/api/qt/stock/kline/get", params)
    rows = data.get("data", {}).get("klines", []) if data else []
    result = []
    for raw in rows[-limit:]:
        parts = raw.split(",")
        if len(parts) < 11:
            continue
        result.append(
            {
                "date": parts[0],
                "open": number(parts[1]) or 0,
                "close": number(parts[2]) or 0,
                "high": number(parts[3]) or 0,
                "low": number(parts[4]) or 0,
                "volume": number(parts[5]) or 0,
                "amount": number(parts[6]) or 0,
                "amplitude": number(parts[7]) or 0,
                "pct": number(parts[8]) or 0,
                "change": number(parts[9]) or 0,
                "turnover": number(parts[10]) or 0,
            }
        )
    return result


def fetch_trends(secid: str) -> list[dict[str, Any]]:
    params = {
        "secid": secid,
        "ndays": "1",
        "iscr": "0",
        "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
    }
    data = eastmoney_json("东方财富", "https://push2his.eastmoney.com/api/qt/stock/trends2/get", params)
    rows = data.get("data", {}).get("trends", []) if data else []
    result = []
    for raw in rows:
        parts = raw.split(",")
        if len(parts) < 4:
            continue
        result.append({"time": parts[0], "price": number(parts[1]), "avg": number(parts[2]), "volume": number(parts[3])})
    return [row for row in result if row["price"] is not None]


def fetch_flow(secid: str) -> list[dict[str, Any]]:
    params = {
        "secid": secid,
        "klt": "101",
        "lmt": "5",
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63",
    }
    data = eastmoney_json("东方财富", "https://push2.eastmoney.com/api/qt/stock/fflow/daykline/get", params)
    rows = data.get("data", {}).get("klines", []) if data else []
    result = []
    for raw in rows:
        parts = raw.split(",")
        if len(parts) < 6:
            continue
        result.append({"date": parts[0], "mainNet": number(parts[1]), "superNet": number(parts[5])})
    return result


def fetch_news() -> list[dict[str, Any]]:
    sources = [
        ("东方财富", "https://finance.eastmoney.com/a/cjjsp.html"),
        ("同花顺", "https://stock.10jqka.com.cn/"),
        ("第一财经", "https://www.yicai.com/news/"),
    ]
    news: list[dict[str, Any]] = []
    for name, url in sources:
        html = safe_call(f"{name} 新闻", lambda name=name, url=url: fetch_text(name, url), "")
        if html:
            news.extend(extract_news(name, url, html)[:5])
    seen = set()
    unique_news = []
    for item in news:
        key = item["title"]
        if key not in seen:
            seen.add(key)
            unique_news.append(item)
    return unique_news[:10]


def extract_news(source: str, base_url: str, html: str) -> list[dict[str, Any]]:
    html = re.sub(r"\s+", " ", html)
    items = []
    pattern = re.compile(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I)
    for href, label in pattern.findall(html):
        title = clean_html(label)
        if useful_news_title(title):
            items.append({"source": source, "title": title, "url": parse.urljoin(base_url, href), "time": ""})
    if items:
        mark_source(source, True, base_url, "新闻线索已更新")
    return items


def eastmoney_clist(source: str, fs: str, fields: str, page_size: int = 500) -> list[dict[str, Any]]:
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
    data = eastmoney_json(source, "https://push2.eastmoney.com/api/qt/clist/get", params)
    return data.get("data", {}).get("diff", []) if data else []


def eastmoney_json(source: str, url: str, params: dict[str, Any]) -> dict[str, Any]:
    query = parse.urlencode(params)
    urls = [f"{url}?{query}"]
    if "push2.eastmoney.com" in url:
        urls.extend(
            [
                f"{url.replace('push2.eastmoney.com', '61.push2.eastmoney.com')}?{query}",
                f"{url.replace('push2.eastmoney.com', '82.push2.eastmoney.com')}?{query}",
            ]
        )
    if "push2his.eastmoney.com" in url:
        urls.extend(
            [
                f"{url.replace('push2his.eastmoney.com', '53.push2his.eastmoney.com')}?{query}",
                f"{url.replace('push2his.eastmoney.com', '78.push2his.eastmoney.com')}?{query}",
            ]
        )

    last_exc: Exception | None = None
    for full_url in urls:
        try:
            payload = fetch_bytes(source, full_url, referer="https://quote.eastmoney.com/").decode("utf-8", "ignore")
            mark_source(source, True, full_url, "行情接口正常")
            return json.loads(strip_jsonp(payload))
        except Exception as exc:
            last_exc = exc
    raise RuntimeError(f"{source} 多节点请求失败：{last_exc}")


def fetch_text(source: str, url: str) -> str:
    raw, charset = fetch_response(source, url, accept="text/html,application/xhtml+xml", retries=3)
    mark_source(source, True, url, "网页可访问")
    return raw.decode(charset or "utf-8", "ignore")


def fetch_bytes(source: str, url: str, referer: str = "") -> bytes:
    raw, _charset = fetch_response(source, url, referer=referer, retries=5)
    return raw


def fetch_response(
    source: str,
    url: str,
    referer: str = "",
    accept: str = "application/json,text/plain,*/*",
    retries: int = 5,
) -> tuple[bytes, str | None]:
    last_exc: Exception | None = None
    for attempt in range(retries):
        req = request.Request(
            url,
            headers={
                "User-Agent": UA,
                "Accept": accept,
                "Referer": referer or url,
                "Cache-Control": "no-cache",
            },
        )
        try:
            with request.urlopen(req, timeout=20) as response:
                return response.read(), response.headers.get_content_charset()
        except error.HTTPError as exc:
            last_exc = exc
            if exc.code not in {429, 500, 502, 503, 504}:
                break
        except (error.URLError, TimeoutError, OSError) as exc:
            last_exc = exc
        time.sleep((0.7 * (attempt + 1)) + random.random() * 0.5)

    mark_source(source, False, url, f"请求失败：{last_exc}")
    raise RuntimeError(f"{source} 请求失败：{last_exc}")


def strip_jsonp(payload: str) -> str:
    payload = payload.strip()
    if payload.startswith("{"):
        return payload
    match = re.search(r"\((\{.*\})\)\s*;?$", payload, re.S)
    return match.group(1) if match else payload


def safe_call(label: str, func: Callable[[], T], default: T) -> T:
    try:
        return func()
    except Exception as exc:
        message = f"{label}失败：{exc}"
        print(message)
        errors.append(message)
        return default


def board_continuous_ok(code: str, current_ranks: dict[str, int], history: dict[str, Any]) -> bool:
    current = current_ranks.get(code, 999)
    previous = [item.get("ranks", {}).get(code, 999) for item in history.get("items", [])[-2:]]
    if current <= 10 and previous and previous[-1] <= 10:
        return True
    if current <= 20 and len(previous) >= 2 and previous[-1] <= 20 and previous[-2] <= 20:
        return True
    return False


def update_board_history(history: dict[str, Any], today: str, boards: list[dict[str, Any]]) -> None:
    ranks = {board["code"]: board["rank"] for board in boards if board.get("rank")}
    items = [entry for entry in history.get("items", []) if entry.get("date") != today]
    items.append({"date": today, "ranks": ranks})
    write_json(BOARD_HISTORY_PATH, {"items": items[-30:]})


def board_position_ok(kline: list[dict[str, Any]]) -> bool:
    if len(kline) < 20:
        return False
    close = kline[-1]["close"]
    previous_high = max(row["high"] for row in kline[-21:-1])
    ma5 = average([row["close"] for row in kline[-5:]])
    ma20 = average([row["close"] for row in kline[-20:]])
    return close >= previous_high * 0.97 and ma5 is not None and ma20 is not None and ma5 >= ma20


def has_trend_leader(members: list[dict[str, Any]]) -> bool:
    return any((item.get("pct") or 0) >= 7 and (item.get("amount") or 0) >= 500_000_000 for item in members[:8])


def flow_continuous_ok(flow: list[dict[str, Any]], quote: dict[str, Any]) -> bool:
    if len(flow) >= 2:
        last_two = flow[-2:]
        return all((row.get("mainNet") or 0) > 0 for row in last_two) and (last_two[-1].get("superNet") or 0) > 0
    return (quote.get("mainNet") or 0) > 0 and (quote.get("superNet") or 0) > 0


def intraday_strength_ok(trends: list[dict[str, Any]], quote: dict[str, Any]) -> bool:
    usable = [row for row in trends if row.get("avg") and row.get("price")]
    if usable:
        above = sum(1 for row in usable if row["price"] >= row["avg"])
        return above / len(usable) >= 0.58
    price = quote.get("price")
    open_price = quote.get("open")
    return bool(price and open_price and price >= open_price and (quote.get("pct") or 0) > 2)


def intraday_pullback_ok(trends: list[dict[str, Any]]) -> bool:
    usable = [row for row in trends if row.get("avg") and row.get("price")]
    if len(usable) < 20:
        return True
    first_half = usable[: max(10, len(usable) // 2)]
    later = usable[len(first_half) :]
    first_high = max(row["price"] for row in first_half)
    pullback = any(abs(row["price"] / row["avg"] - 1) <= 0.008 for row in later if row.get("avg"))
    reclaim = usable[-1]["price"] > usable[-1]["avg"]
    return first_high > usable[0]["price"] * 1.03 and pullback and reclaim


def latest_avg_price(trends: list[dict[str, Any]]) -> float | None:
    for row in reversed(trends):
        if row.get("avg"):
            return row["avg"]
    return None


def recent_platform_stop(kline: list[dict[str, Any]]) -> float:
    recent_lows = [row["low"] for row in kline[-8:] if row["low"]]
    return min(recent_lows) if recent_lows else kline[-1]["close"] * 0.95


def ratio_to_previous_avg(today_value: float | None, series: list[float]) -> float | None:
    if today_value is None or len(series) < 6:
        return None
    return ratio_to_average(today_value, series[-6:-1])


def ratio_to_average(value: float | None, previous: list[float]) -> float | None:
    clean = [item for item in previous if item and item > 0]
    if value is None or not clean:
        return None
    avg = sum(clean) / len(clean)
    return round(value / avg, 3) if avg > 0 else None


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


def stock_secid(code: str) -> str:
    return f"1.{code}" if code.startswith(("6", "9")) else f"0.{code}"


def stock_market(code: str) -> str:
    if code.startswith("6"):
        return "SH"
    if code.startswith(("4", "8")):
        return "BJ"
    return "SZ"


def stock_source_links(code: str) -> list[dict[str, str]]:
    prefix = "sh" if code.startswith("6") else "sz"
    return [
        {"name": "东方财富行情", "url": f"https://quote.eastmoney.com/{prefix}{code}.html"},
        {"name": "同花顺个股", "url": f"https://stockpage.10jqka.com.cn/{code}/"},
        {"name": "第一财经资讯", "url": "https://www.yicai.com/news/"},
    ]


def useful_news_title(title: str) -> bool:
    if len(title) < 8 or len(title) > 80:
        return False
    keywords = ("A股", "股市", "市场", "板块", "资金", "沪指", "深成指", "创业板", "证券", "行情")
    return any(keyword in title for keyword in keywords)


def clean_html(raw: str) -> str:
    raw = re.sub(r"<script.*?</script>", "", raw, flags=re.S | re.I)
    raw = re.sub(r"<style.*?</style>", "", raw, flags=re.S | re.I)
    raw = re.sub(r"<[^>]+>", "", raw)
    return unescape(raw).strip()


def average(values: list[float]) -> float | None:
    clean = [item for item in values if item is not None and not math.isnan(item)]
    return sum(clean) / len(clean) if clean else None


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


def source_list() -> list[dict[str, Any]]:
    defaults = [
        ("东方财富", "https://quote.eastmoney.com/", "行情、板块、K线、资金"),
        ("同花顺", "https://www.10jqka.com.cn/", "新闻线索"),
        ("第一财经", "https://www.yicai.com/", "新闻线索"),
    ]
    for name, url, note in defaults:
        source_health.setdefault(name, {"name": name, "ok": False, "url": url, "note": note})
    return list(source_health.values())


if __name__ == "__main__":
    main()
