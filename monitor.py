#!/usr/bin/env python3
"""HKJC GraphQL one-off comparison diagnostic (2026-11-21).

Temporary drop-in replacement for monitor.py. It performs at most TWO read-only
requests on manual GitHub Actions workflow_dispatch runs:
  1. Website-observed HAD+EHA request (baseline).
  2. Target HAD+HDC+HIL request, only if baseline returned valid data.

The diagnostic NEVER changes docs/data.json, does not archive data, does not
attempt to bypass server-side blocks, and does no polling on scheduled runs.
Restore a production monitor after completing the diagnostic.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ENDPOINT = "https://info.cld.hkjc.com/graphql/base-get/"

# Exact GraphQL document text captured from the official football-page GET.
QUERY = 'query matchList($startIndex:Int$endIndex:Int$startDate:String$endDate:String$matchIds:[String]$tournIds:[String]$fbOddsTypes:[FBOddsType]!$fbOddsTypesM:[FBOddsType]!$inplayOnly:Boolean$featuredMatchesOnly:Boolean$frontEndIds:[String]$earlySettlementOnly:Boolean$showAllMatch:Boolean){matches(startIndex:$startIndex endIndex:$endIndex startDate:$startDate endDate:$endDate matchIds:$matchIds tournIds:$tournIds fbOddsTypes:$fbOddsTypesM inplayOnly:$inplayOnly featuredMatchesOnly:$featuredMatchesOnly frontEndIds:$frontEndIds earlySettlementOnly:$earlySettlementOnly showAllMatch:$showAllMatch){id frontEndId matchDate kickOffTime status updateAt sequence esIndicatorEnabled homeTeam{id name_en name_ch}awayTeam{id name_en name_ch}tournament{id frontEndId nameProfileId isInteractiveServiceAvailable code name_en name_ch}isInteractiveServiceAvailable inplayDelay venue{code name_en name_ch}tvChannels{code name_en name_ch}liveEvents{id code}featureStartTime featureMatchSequence poolInfo{normalPools inplayPools sellingPools ntsInfo entInfo definedPools ngsInfo{str name_en name_ch instNo}agsInfo{str name_en name_ch}}runningResult{homeScore awayScore corner homeCorner awayCorner}runningResultExtra{homeScore awayScore corner homeCorner awayCorner}adminOperation{remark{typ}}foPools(fbOddsTypes:$fbOddsTypes){id status oddsType instNo inplay name_ch name_en updateAt expectedSuspendDateTime lines{lineId status condition main combinations{combId str status offerEarlySettlement currentOdds selections{selId str name_ch name_en}}}}}}'


def make_variables(markets):
    """Keep browser-captured variable names, order and null/false values."""
    return {
        "fbOddsTypes": list(markets),
        "fbOddsTypesM": list(markets),
        "inplayOnly": False,
        "featuredMatchesOnly": False,
        "startDate": None,
        "endDate": None,
        "tournIds": None,
        "matchIds": None,
        "tournId": None,
        "tournProfileId": None,
        "subType": None,
        "startIndex": 1,
        "endIndex": 60,
        "frontEndIds": None,
        "earlySettlementOnly": False,
        "showAllMatch": False,
        "tday": None,
        "tIdList": None,
    }


def get_once(markets, urlopen=urllib.request.urlopen):
    variables = make_variables(markets)
    query_string = urllib.parse.urlencode({
        "query": QUERY,
        "variables": json.dumps(variables, separators=(",", ":")),
    })
    url = ENDPOINT + "?" + query_string
    # Do not print full URLs: just the high-level request shape.
    print(f"REQUEST: method=GET, endpoint=/graphql/base-get/, "
          f"markets={','.join(markets)}, URL characters={len(url)}", flush=True)
    request = urllib.request.Request(url, headers={
        "Accept": "*/*",
        "Accept-Language": "zh-HK,zh;q=0.9",
        "Cache-Control": "no-cache",
        "Referer": "https://bet.hkjc.com/",
        "User-Agent": "OddsHistoryResearch/1.3 (read-only diagnostic)",
    }, method="GET")
    try:
        with urlopen(request, timeout=30) as response:
            status = getattr(response, "status", 200)
            raw = response.read()
            print(f"HTTP STATUS: {status}; response bytes={len(raw)}", flush=True)
    except urllib.error.HTTPError as exc:
        body = exc.read(4096).decode("utf-8", errors="replace")
        msg = " ".join(body.split())[:1600]
        print(f"HTTP ERROR: {exc.code}; body: {msg or '(empty)'}", flush=True)
        return None
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"NETWORK ERROR: {type(exc).__name__}: {exc}", flush=True)
        return None
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        print(f"DECODE ERROR: {type(exc).__name__}: {str(exc)[:300]}", flush=True)
        return None
    if not isinstance(obj, dict):
        print("FORMAT ERROR: top-level JSON is not an object", flush=True)
        return None
    if obj.get("errors"):
        print("GRAPHQL ERROR: " + json.dumps(obj["errors"], ensure_ascii=False)[:1300], flush=True)
        return None
    matches = (obj.get("data") or {}).get("matches")
    if not isinstance(matches, list):
        print("FORMAT ERROR: missing data.matches; keys: " + str(list(obj.keys())), flush=True)
        return None
    pools = {}
    quotes = 0
    for match in matches:
        for pool in (match.get("foPools") or []):
            code = str(pool.get("oddsType") or "?")
            pools[code] = pools.get(code, 0) + 1
            for line in (pool.get("lines") or []):
                for combo in (line.get("combinations") or []):
                    if combo.get("currentOdds") not in (None, "", "---"):
                        quotes += 1
    print(f"GRAPHQL OK: matches={len(matches)}, pool counts={pools}, "
          f"nonempty odds fields={quotes}", flush=True)
    return {"matches": len(matches), "pools": pools, "quotes": quotes}


def main():
    if os.getenv("GITHUB_EVENT_NAME") == "schedule":
        print("DIAGNOSTIC MODE: scheduled run skipped; no requests or data writes.")
        return 0
    print("HKJC GRAPHQL DIAGNOSTIC — no docs/ files will be changed", flush=True)
    print("STEP 1: compare with browser-observed HAD+EHA", flush=True)
    baseline = get_once(("HAD", "EHA"))
    if baseline is None:
        print("RESULT: baseline failed. Do not infer that HDC/HIL are invalid. "
              "Review HTTP body and provider access conditions.", flush=True)
        return 1
    time.sleep(2)
    print("STEP 2: same request shape, target HAD+HDC+HIL", flush=True)
    target = get_once(("HAD", "HDC", "HIL"))
    if target is None:
        print("RESULT: baseline succeeded but target failed. "
              "The difference is the requested market combination.", flush=True)
        return 1
    print("RESULT: both requests returned valid GraphQL data. "
          "This was a TEST ONLY: no odds history was saved.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
