#!/usr/bin/env python3
"""HKJC public GraphQL football odds monitor. Python 3.12, standard library.

Reads HAD (1X2), HDC (handicap), HIL (over/under) before kick-off.
This is an unofficial, read-only experiment, not a supported HKJC data service.
Never uses account credentials or bypasses access controls; stops on HTTP 403/429.
"""
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
PAGE_SIZE = 60
MAX_PAGES = 3
DAYS_AHEAD = 2  # today plus tomorrow, filtered locally after unfiltered official query
MAX_HISTORY = 30000

# MatchList query follows the official football page's user-captured GET
# shape and field order. Unofficial read-only client; no guaranteed support.
# Market codes differ from the captured HAD/EHA and require live verification.
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


def graphql_request(start_index):
    # As captured from the HKJC football home page: GET /graphql/base-get/
    # with a query-string GraphQL document and JSON-encoded variables.
    # The observed page uses HAD/EHA; here we request our target HAD/HDC/HIL.
    # Acceptance of the changed market list must be verified by a real run.
    variables = {
        "fbOddsTypes": list(MARKETS),
        "fbOddsTypesM": list(MARKETS),
        "inplayOnly": False,
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
    }
    params = urllib.parse.urlencode({
        "query": QUERY,
        "variables": json.dumps(variables, separators=(",", ":"), ensure_ascii=False),
    })
    url = ENDPOINT + "?" + params
    headers = {
        "Accept": "*/*",
        "Accept-Language": "zh-HK,zh;q=0.9",
        "Cache-Control": "no-cache",
        "Referer": "https://bet.hkjc.com/",
        "User-Agent": "OddsHistoryResearch/1.2 (read-only public data)",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            obj = json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        retry = err.headers.get("Retry-After") or "unknown"
        raise RuntimeError(f"HKJC HTTP {err.code}; Retry-After={retry}. Stopping (no bypass).") from err
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as err:
        raise RuntimeError(f"HKJC response/network error: {err}") from err
    if not isinstance(obj, dict) or obj.get("errors"):
        raise RuntimeError(f"HKJC GraphQL error: {str(obj.get('errors', obj))[:900]}")
    matches = (obj.get("data") or {}).get("matches")
    if not isinstance(matches, list):
        raise RuntimeError("HKJC response is missing data.matches; API query may have changed")
    return matches


def fetch_all_matches():
    # Official page sends null for startDate/endDate. Fetch the first pages
    # then restrict the displayed monitoring window in HK time locally.
    out = {}
    requests = 0
    for page in range(MAX_PAGES):
        start = 1 + page * PAGE_SIZE
        chunk = graphql_request(start)
        requests += 1
        print(f"HKJC GraphQL GET, page {page+1}: {len(chunk)} matches")
        for m in chunk:
            if isinstance(m, dict) and m.get("id") is not None:
                out[str(m["id"])] = m
        if len(chunk) < PAGE_SIZE:
            break
        if page < MAX_PAGES - 1:
            time.sleep(1.5)  # conservative request spacing
    dates = {hkt_date() + timedelta(days=i) for i in range(DAYS_AHEAD)}
    selected = []
    unknown_time = 0
    for m in out.values():
        ko = parse_datetime(m.get("kickOffTime"))
        if ko is None:
            unknown_time += 1
            continue
        if ko.astimezone(HKT).date() in dates:
            selected.append(m)
    print(f"Received {len(out)} fixtures; {len(selected)} in next {DAYS_AHEAD} HK dates; {unknown_time} unparseable kickoff times")
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
