#!/usr/bin/env python3
"""HKJC GraphQL diagnostic v1.5: isolate handicap / over-under pools.

Temporary read-only diagnostic for MANUAL GitHub Actions runs. Uses the exact
matchList query body captured from the public football site by the user and
changes only the odds-type variables under test. It never saves odds or edits
any docs/ files. Scheduled runs make no requests.
"""
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ENDPOINT = "https://info.cld.hkjc.com/graphql/base-get/"
QUERY = 'query matchList($startIndex:Int$endIndex:Int$startDate:String$endDate:String$matchIds:[String]$tournIds:[String]$fbOddsTypes:[FBOddsType]!$fbOddsTypesM:[FBOddsType]!$inplayOnly:Boolean$featuredMatchesOnly:Boolean$frontEndIds:[String]$earlySettlementOnly:Boolean$showAllMatch:Boolean){matches(startIndex:$startIndex endIndex:$endIndex startDate:$startDate endDate:$endDate matchIds:$matchIds tournIds:$tournIds fbOddsTypes:$fbOddsTypesM inplayOnly:$inplayOnly featuredMatchesOnly:$featuredMatchesOnly frontEndIds:$frontEndIds earlySettlementOnly:$earlySettlementOnly showAllMatch:$showAllMatch){id frontEndId matchDate kickOffTime status updateAt sequence esIndicatorEnabled homeTeam{id name_en name_ch}awayTeam{id name_en name_ch}tournament{id frontEndId nameProfileId isInteractiveServiceAvailable code name_en name_ch}isInteractiveServiceAvailable inplayDelay venue{code name_en name_ch}tvChannels{code name_en name_ch}liveEvents{id code}featureStartTime featureMatchSequence poolInfo{normalPools inplayPools sellingPools ntsInfo entInfo definedPools ngsInfo{str name_en name_ch instNo}agsInfo{str name_en name_ch}}runningResult{homeScore awayScore corner homeCorner awayCorner}runningResultExtra{homeScore awayScore corner homeCorner awayCorner}adminOperation{remark{typ}}foPools(fbOddsTypes:$fbOddsTypes){id status oddsType instNo inplay name_ch name_en updateAt expectedSuspendDateTime lines{lineId status condition main combinations{combId str status offerEarlySettlement currentOdds selections{selId str name_ch name_en}}}}}}'


def make_variables(markets):
    return {
        "fbOddsTypes": list(markets), "fbOddsTypesM": list(markets),
        "inplayOnly": False, "featuredMatchesOnly": False,
        "startDate": None, "endDate": None, "tournIds": None, "matchIds": None,
        "tournId": None, "tournProfileId": None, "subType": None,
        "startIndex": 1, "endIndex": 60, "frontEndIds": None,
        "earlySettlementOnly": False, "showAllMatch": False,
        "tday": None, "tIdList": None,
    }


def decode_body(raw, encoding=""):
    if raw.startswith(b"\x1f\x8b") or "gzip" in encoding.lower():
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8-sig"))


def safe_detail(raw, encoding):
    try:
        obj = decode_body(raw, encoding)
        return json.dumps(obj, ensure_ascii=False)[:900]
    except (OSError, UnicodeError, ValueError):
        return raw.decode("utf-8", errors="replace")[:900]


def test_markets(label, markets):
    qs = urllib.parse.urlencode({
        "query": QUERY,
        "variables": json.dumps(make_variables(markets), separators=(",", ":")),
    })
    url = ENDPOINT + "?" + qs
    request = urllib.request.Request(url, headers={
        "Accept": "*/*",
        "Accept-Language": "zh-HK,zh;q=0.9",
        "Cache-Control": "no-cache",
        "Referer": "https://bet.hkjc.com/",
        "User-Agent": "OddsHistoryResearch/1.5 (read-only diagnostic)",
    }, method="GET")
    print(f"{label}: testing {','.join(markets)}; url_length={len(url)}", flush=True)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            encoding = response.headers.get("Content-Encoding", "")
            status = getattr(response, "status", 200)
            print(f"{label}: HTTP {status}; bytes={len(raw)}; encoding={encoding or 'none'}", flush=True)
    except urllib.error.HTTPError as err:
        raw = err.read(4096)
        print(f"{label}: HTTP {err.code}; response={safe_detail(raw, err.headers.get('Content-Encoding', ''))}", flush=True)
        return False
    except (urllib.error.URLError, TimeoutError) as err:
        print(f"{label}: NETWORK ERROR {type(err).__name__}: {err}", flush=True)
        return False
    try:
        obj = decode_body(raw, encoding)
    except (OSError, UnicodeError, ValueError) as err:
        print(f"{label}: DECODE ERROR {type(err).__name__}: {err}", flush=True)
        return False
    if not isinstance(obj, dict):
        print(f"{label}: INVALID RESPONSE TYPE: {type(obj).__name__}", flush=True)
        return False
    if obj.get("errors"):
        print(f"{label}: GRAPHQL ERROR {json.dumps(obj['errors'], ensure_ascii=False)[:900]}", flush=True)
        return False
    matches = (obj.get("data") or {}).get("matches")
    if not isinstance(matches, list):
        print(f"{label}: NO data.matches; keys={list(obj)}", flush=True)
        return False
    pools = {}
    quotes = 0
    for match in matches:
        if not isinstance(match, dict):
            continue
        for pool in match.get("foPools") or []:
            if not isinstance(pool, dict):
                continue
            key = str(pool.get("oddsType") or "?")
            pools[key] = pools.get(key, 0) + 1
            for line in pool.get("lines") or []:
                if not isinstance(line, dict):
                    continue
                for combo in line.get("combinations") or []:
                    if isinstance(combo, dict) and combo.get("currentOdds") not in (None, "", "---"):
                        quotes += 1
    print(f"{label}: GRAPHQL OK; matches={len(matches)}; pools={pools}; nonempty_odds={quotes}", flush=True)
    return True


def main():
    if os.getenv("GITHUB_EVENT_NAME") == "schedule":
        print("Diagnostic-only: scheduled run skipped; no network calls or file writes.", flush=True)
        return 0
    print("HKJC v1.5 ODDS-TYPE DIAGNOSTIC — no docs/ changes", flush=True)
    # Baseline HAD+EHA has already succeeded in v1.4. Test suspected markets separately.
    tests = [
        ("TEST A", ("HAD", "HDC")),
        ("TEST B", ("HAD", "HIL")),
        ("TEST C", ("HDC",)),
        ("TEST D", ("HIL",)),
    ]
    results = []
    for i, (label, markets) in enumerate(tests):
        if i:
            time.sleep(2)
        results.append((label, markets, test_markets(label, markets)))
    print("SUMMARY: " + "; ".join(
        f"{label} ({'+'.join(markets)})={'OK' if ok else 'FAILED'}"
        for label, markets, ok in results), flush=True)
    print("DIAGNOSTIC ONLY: no data.json updates and no live odds monitoring.", flush=True)
    return 0 if any(ok for _, _, ok in results) else 1


if __name__ == "__main__":
    sys.exit(main())
