#!/usr/bin/env python3
"""HKJC public GraphQL football odds monitor. Python 3.12, standard library.

Reads HAD (1X2), HDC (handicap), HIL (over/under) before kick-off.
This is an unofficial, read-only experiment, not a supported HKJC data service.
Never uses account credentials or bypasses access controls; stops on HTTP 403/429.
"""
import gzip
import json
import os
import sys
import time
import math
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_PATH = ROOT / "docs" / "data.json"
ARCHIVE_PATH = ROOT / "docs" / "tipsme-archive.json"
ENDPOINT = "https://info.cld.hkjc.com/graphql/base-get/"
HKT = timezone(timedelta(hours=8))
MARKETS = ("HAD", "HDC", "HIL")
# Exact grouped market values captured from bet.hkjc.com football pages.
# EHA/EDC/EHL are requested as the page does, but only HAD/HDC/HIL are stored.
MARKET_REQUESTS = {
    "HAD": ("HAD", "EHA"),
    "HDC": ("HDC", "EDC"),
    "HIL": ("HIL", "EHL"),
}
PAGE_SIZE = 60
MAX_PAGES = 3
DAYS_AHEAD = 2  # today plus tomorrow, filtered locally after unfiltered official query
MAX_HISTORY = 30000

# GraphQL query captured directly from the football pages (HAD/HDC/HIL).
# Per-page variables differ: HAD explicitly sets inplayOnly=false; HDC/HIL
# omit it. This is an unofficial read-only client, not a supported API.
QUERY = 'query matchList($startIndex:Int$endIndex:Int$startDate:String$endDate:String$matchIds:[String]$tournIds:[String]$fbOddsTypes:[FBOddsType]!$fbOddsTypesM:[FBOddsType]!$inplayOnly:Boolean$featuredMatchesOnly:Boolean$frontEndIds:[String]$earlySettlementOnly:Boolean$showAllMatch:Boolean){matches(startIndex:$startIndex endIndex:$endIndex startDate:$startDate endDate:$endDate matchIds:$matchIds tournIds:$tournIds fbOddsTypes:$fbOddsTypesM inplayOnly:$inplayOnly featuredMatchesOnly:$featuredMatchesOnly frontEndIds:$frontEndIds earlySettlementOnly:$earlySettlementOnly showAllMatch:$showAllMatch){id frontEndId matchDate kickOffTime status updateAt sequence esIndicatorEnabled homeTeam{id name_en name_ch}awayTeam{id name_en name_ch}tournament{id frontEndId nameProfileId isInteractiveServiceAvailable code name_en name_ch}isInteractiveServiceAvailable inplayDelay venue{code name_en name_ch}tvChannels{code name_en name_ch}liveEvents{id code}featureStartTime featureMatchSequence poolInfo{normalPools inplayPools sellingPools ntsInfo entInfo definedPools ngsInfo{str name_en name_ch instNo}agsInfo{str name_en name_ch}}runningResult{homeScore awayScore corner homeCorner awayCorner}runningResultExtra{homeScore awayScore corner homeCorner awayCorner}adminOperation{remark{typ}}foPools(fbOddsTypes:$fbOddsTypes){id status oddsType instNo inplay name_ch name_en updateAt expectedSuspendDateTime lines{lineId status condition main combinations{combId str status offerEarlySettlement currentOdds selections{selId str name_ch name_en}}}}}}'


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def hkt_date():
    return utcnow().astimezone(HKT).date()


def parse_datetime(value):
    """HKJC date strings may carry a timezone or be local HK time."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        d = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return (d.replace(tzinfo=HKT) if d.tzinfo is None else d).astimezone(timezone.utc)
    except ValueError:
        return None


def new_state():
    return {"schema_version": 2, "provider": "HKJC_GRAPHQL",
            "generated_at": None, "matches": {}, "snapshots": [], "runs": []}


def load_state():
    if not DATA_PATH.exists():
        return new_state(), None
    old = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    if old.get("schema_version") == 2 and old.get("provider") == "HKJC_GRAPHQL":
        return old, None
    # Archive the older Tipsme-based dataset rather than relabel it as direct HKJC.
    return new_state(), old


def decode_response(raw, encoding=""):
    """urllib does not automatically decompress the GZIP response seen in Actions."""
    if raw.startswith(b"\x1f\x8b") or "gzip" in (encoding or "").lower():
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8-sig"))


def make_variables(market, start_index):
    types = MARKET_REQUESTS[market]
    # Key order and null values follow the user's real browser GET requests.
    variables = {
        "fbOddsTypes": list(types),
        "fbOddsTypesM": list(types),
    }
    if market == "HAD":
        variables["inplayOnly"] = False
    variables.update({
        "featuredMatchesOnly": False,
        "startDate": None,
        "endDate": None,
        "tournIds": None,
        "matchIds": None,
        "tournId": None,
        "tournProfileId": None,
        "subType": None,
        "startIndex": start_index,
        "endIndex": start_index + PAGE_SIZE - 1,
        "frontEndIds": None,
        "earlySettlementOnly": False,
        "showAllMatch": False,
        "tday": None,
        "tIdList": None,
    })
    return variables


def graphql_request(market, start_index):
    variables = make_variables(market, start_index)
    params = urllib.parse.urlencode({
        "query": QUERY,
        "variables": json.dumps(variables, separators=(",", ":"), ensure_ascii=False),
    })
    url = ENDPOINT + "?" + params
    req = urllib.request.Request(url, headers={
        "Accept": "*/*",
        "Accept-Language": "zh-HK,zh;q=0.9",
        "Cache-Control": "no-cache",
        "Referer": "https://bet.hkjc.com/",
        "User-Agent": "OddsHistoryResearch/1.6 (read-only public data)",
    }, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            obj = decode_response(res.read(), res.headers.get("Content-Encoding", ""))
    except urllib.error.HTTPError as err:
        body = err.read(4096)
        try:
            detail = json.dumps(decode_response(body, err.headers.get("Content-Encoding", "")), ensure_ascii=False)
        except (OSError, UnicodeError, ValueError):
            detail = body.decode("utf-8", errors="replace")
        detail = " ".join(detail.split())[:1200]
        raise RuntimeError(f"HKJC {market} HTTP {err.code}; Response: {detail or '(empty)'}; stopping (no bypass)") from err
    except (urllib.error.URLError, TimeoutError, OSError, UnicodeError, ValueError) as err:
        raise RuntimeError(f"HKJC {market} response/network error: {err}") from err
    if not isinstance(obj, dict) or obj.get("errors"):
        raise RuntimeError(f"HKJC {market} GraphQL error: {str(obj.get('errors', obj))[:900]}")
    matches = (obj.get("data") or {}).get("matches")
    if not isinstance(matches, list):
        raise RuntimeError(f"HKJC {market} response missing data.matches")
    return matches


def fetch_all_matches():
    """Fetch each official market-page shape, then merge pools by match ID."""
    out = {}
    requests = 0
    for market_index, market in enumerate(MARKETS):
        if market_index:
            time.sleep(1.5)
        for page in range(MAX_PAGES):
            if page:
                time.sleep(1.5)
            start_index = 1 + page * PAGE_SIZE
            chunk = graphql_request(market, start_index)
            requests += 1
            pool_count = sum(
                1 for match in chunk if isinstance(match, dict)
                for pool in (match.get("foPools") or [])
                if isinstance(pool, dict) and pool.get("oddsType") == market
            )
            print(f"HKJC {market} GET page {page+1}: {len(chunk)} matches, {pool_count} {market} pools", flush=True)
            for match in chunk:
                if not isinstance(match, dict) or match.get("id") is None:
                    continue
                match_id = str(match["id"])
                if match_id not in out:
                    out[match_id] = dict(match)
                    out[match_id]["foPools"] = []
                saved_pools = out[match_id]["foPools"]
                for pool in (match.get("foPools") or []):
                    if not isinstance(pool, dict) or pool.get("oddsType") != market:
                        continue
                    # Within a match, each market/instance/ID pair is unique.
                    pool_key = (str(pool.get("oddsType")), str(pool.get("instNo")), str(pool.get("id")))
                    existing_keys = {(str(p.get("oddsType")), str(p.get("instNo")), str(p.get("id"))) for p in saved_pools}
                    if pool_key not in existing_keys:
                        saved_pools.append(pool)
            if len(chunk) < PAGE_SIZE:
                break
    dates = {hkt_date() + timedelta(days=i) for i in range(DAYS_AHEAD)}
    selected = []
    unknown_time = 0
    for match in out.values():
        kickoff = parse_datetime(match.get("kickOffTime"))
        if kickoff is None:
            unknown_time += 1
            continue
        if kickoff.astimezone(HKT).date() in dates:
            selected.append(match)
    print(f"Received {len(out)} distinct fixtures; {len(selected)} in next {DAYS_AHEAD} HKT dates; "
          f"{unknown_time} unparseable kickoff times", flush=True)
    return selected, requests


def txt(info):
    if isinstance(info, dict):
        return str(info.get("name_ch") or info.get("name_en") or "?")
    return str(info or "?")


def number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) and 1 < n < 10000 else None
    except (TypeError, ValueError):
        return None


def parse_match(m, observed_at):
    mid = str(m["id"])
    kickoff = parse_datetime(m.get("kickOffTime"))
    match = {"id": mid, "front_end_id": str(m.get("frontEndId") or ""),
             "home": txt(m.get("homeTeam")), "away": txt(m.get("awayTeam")),
             "competition": txt(m.get("tournament")), "kickoff_utc": iso(kickoff) if kickoff else None,
             "status": str(m.get("status") or "unknown"), "last_seen": observed_at,
             "provider": "HKJC_GRAPHQL"}
    rows = []
    for pool in (m.get("foPools") or []):
        if not isinstance(pool, dict) or pool.get("oddsType") not in MARKETS:
            continue
        if pool.get("inplay") is True:  # pre-match data only for version 1
            continue
        market = pool["oddsType"]
        pool_time = parse_datetime(pool.get("updateAt"))
        for line in (pool.get("lines") or []):
            if not isinstance(line, dict):
                continue
            condition = str(line.get("condition") or "")
            line_id = str(line.get("lineId") or "")
            for combo in (line.get("combinations") or []):
                if not isinstance(combo, dict):
                    continue
                price = number(combo.get("currentOdds"))
                if price is None:
                    continue
                sels = [s for s in (combo.get("selections") or []) if isinstance(s, dict)]
                label = " + ".join(txt(s) for s in sels) if sels else str(combo.get("str") or "?")
                selection_key = str(combo.get("str") or "") or ":".join(str(s.get("selId") or s.get("str") or "") for s in sels)
                if not selection_key:
                    continue
                rows.append({
                    "source": "HKJC_GRAPHQL", "match_id": mid,
                    "market": market, "line_id": line_id, "condition": condition,
                    "is_main_line": bool(line.get("main")), "selection": selection_key,
                    "selection_name": label, "odds": price,
                    "pool_status": str(pool.get("status") or "unknown"),
                    "line_status": str(line.get("status") or "unknown"),
                    "selection_status": str(combo.get("status") or "unknown"),
                    "source_updated_at": iso(pool_time) if pool_time else None,
                    "observed_at": observed_at,
                    "inplay": False,
                })
    return match, rows


def row_key(row):
    return (row["match_id"], row["market"], row["line_id"], row["condition"], row["selection"])


def row_value(row):
    return (row["odds"], row["pool_status"], row["line_status"], row["selection_status"])


def add_changes(state, fresh_rows):
    latest = {}
    for row in state["snapshots"]:
        latest[row_key(row)] = row_value(row)
    count = 0
    for row in fresh_rows:
        k = row_key(row)
        if latest.get(k) != row_value(row):
            state["snapshots"].append(row)
            latest[k] = row_value(row)
            count += 1
    state["snapshots"] = state["snapshots"][-MAX_HISTORY:]
    return count


def run():
    state, old = load_state()
    started = iso(utcnow())
    matches, nreq = fetch_all_matches()  # errors do not overwrite previous data
    fresh = []
    for m in matches:
        meta, rows = parse_match(m, started)
        if rows:
            state["matches"][meta["id"]] = meta
            fresh.extend(rows)
    # Never replace a previous dataset with an empty result. This also
    # preserves the original Tipsme file until we have real GraphQL quotes.
    if not fresh:
        raise RuntimeError(
            "No usable HKJC odds returned; existing data.json preserved."
        )
    by_market = {market: sum(row["market"] == market for row in fresh) for market in MARKETS}
    print(f"Parsed valid pre-match quotes by market: {by_market}", flush=True)
    if not all(by_market.values()):
        print("NOTICE: at least one target market has no valid quotes for the current date window", flush=True)
    added = add_changes(state, fresh)
    state["generated_at"] = iso(utcnow())
    state["runs"].append({"time": state["generated_at"], "requests": nreq,
                          "matches_returned": len(matches), "matches_with_odds": len({x['match_id'] for x in fresh}),
                          "quotes_received": len(fresh), "changes_saved": added})
    state["runs"] = state["runs"][-150:]
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Only archive after first successful GraphQL response/parse.
    if old is not None and not ARCHIVE_PATH.exists():
        ARCHIVE_PATH.write_text(json.dumps(old, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"Archived previous Tipsme data at {ARCHIVE_PATH}")
    DATA_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"HKJC GraphQL success: {len(matches)} fixtures; {len(fresh)} valid quotes; {added} new/changed records")


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        print(f"COLLECTION FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
