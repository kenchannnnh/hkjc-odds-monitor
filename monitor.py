#!/usr/bin/env python3
"""HKJC GraphQL one-off comparison diagnostic, v1.4 (GZIP fix).

Read-only manual test: A = browser-observed HAD/EHA, B = target HAD/HDC/HIL.
Does not write docs/data.json or change historic data. Scheduled runs skip.
This is an unofficial diagnostic and not an endorsed HKJC client.
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
# GraphQL document transcribed from the official football-page GET shared by the user.
QUERY = 'query matchList($startIndex:Int$endIndex:Int$startDate:String$endDate:String$matchIds:[String]$tournIds:[String]$fbOddsTypes:[FBOddsType]!$fbOddsTypesM:[FBOddsType]!$inplayOnly:Boolean$featuredMatchesOnly:Boolean$frontEndIds:[String]$earlySettlementOnly:Boolean$showAllMatch:Boolean){matches(startIndex:$startIndex endIndex:$endIndex startDate:$startDate endDate:$endDate matchIds:$matchIds tournIds:$tournIds fbOddsTypes:$fbOddsTypesM inplayOnly:$inplayOnly featuredMatchesOnly:$featuredMatchesOnly frontEndIds:$frontEndIds earlySettlementOnly:$earlySettlementOnly showAllMatch:$showAllMatch){id frontEndId matchDate kickOffTime status updateAt sequence esIndicatorEnabled homeTeam{id name_en name_ch}awayTeam{id name_en name_ch}tournament{id frontEndId nameProfileId isInteractiveServiceAvailable code name_en name_ch}isInteractiveServiceAvailable inplayDelay venue{code name_en name_ch}tvChannels{code name_en name_ch}liveEvents{id code}featureStartTime featureMatchSequence poolInfo{normalPools inplayPools sellingPools ntsInfo entInfo definedPools ngsInfo{str name_en name_ch instNo}agsInfo{str name_en name_ch}}runningResult{homeScore awayScore corner homeCorner awayCorner}runningResultExtra{homeScore awayScore corner homeCorner awayCorner}adminOperation{remark{typ}}foPools(fbOddsTypes:$fbOddsTypes){id status oddsType instNo inplay name_ch name_en updateAt expectedSuspendDateTime lines{lineId status condition main combinations{combId str status offerEarlySettlement currentOdds selections{selId str name_ch name_en}}}}}}'


def make_variables(markets):
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


def decode_body(raw, encoding=""):
    """Decode official server responses; gzip is indicated by 1F 8B bytes.

    Many HTTP clients (including urllib) don't automatically decompress gzip.
    """
    encoding = (encoding or "").lower()
    if raw.startswith(b"\x1f\x8b") or "gzip" in encoding:
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8-sig"))


def get_once(markets, urlopen=urllib.request.urlopen):
    variables = make_variables(markets)
    qs = urllib.parse.urlencode({
        "query": QUERY,
        "variables": json.dumps(variables, separators=(",", ":")),
    })
    url = ENDPOINT + "?" + qs
    print(f"REQUEST: method=GET, endpoint=/graphql/base-get/, markets={','.join(markets)}, URL characters={len(url)}", flush=True)
    req = urllib.request.Request(url, headers={
        "Accept": "*/*",
        "Accept-Language": "zh-HK,zh;q=0.9",
        "Cache-Control": "no-cache",
        "Referer": "https://bet.hkjc.com/",
        "User-Agent": "OddsHistoryResearch/1.4 (read-only diagnostic)",
    }, method="GET")
    try:
        with urlopen(req, timeout=30) as response:
            status = getattr(response, "status", 200)
            raw = response.read()
            encoding = response.headers.get("Content-Encoding", "")
            print(f"HTTP STATUS: {status}; response bytes={len(raw)}; content-encoding={encoding or 'not specified'}; gzip-magic={raw.startswith(bytes([0x1f,0x8b]))}", flush=True)
    except urllib.error.HTTPError as exc:
        raw = exc.read(4096)
        encoding = exc.headers.get("Content-Encoding", "")
        try:
            detail = decode_body(raw, encoding)
            detail = json.dumps(detail, ensure_ascii=False)
        except (OSError, UnicodeError, ValueError):
            detail = raw.decode("utf-8", errors="replace")
        print(f"HTTP ERROR: {exc.code}; body: {' '.join(detail.split())[:1300]}", flush=True)
        return None
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"NETWORK ERROR: {type(exc).__name__}: {exc}", flush=True)
        return None
    try:
        obj = decode_body(raw, encoding)
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"DECODE ERROR: {type(exc).__name__}: {str(exc)[:350]}", flush=True)
        return None
    if not isinstance(obj, dict):
        print("FORMAT ERROR: top-level response is not an object", flush=True)
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
        if not isinstance(match, dict):
            continue
        for pool in (match.get("foPools") or []):
            if not isinstance(pool, dict):
                continue
            code = str(pool.get("oddsType") or "?")
            pools[code] = pools.get(code, 0) + 1
            for line in (pool.get("lines") or []):
                if not isinstance(line, dict):
                    continue
                for combo in (line.get("combinations") or []):
                    if isinstance(combo, dict) and combo.get("currentOdds") not in (None, "", "---"):
                        quotes += 1
    print(f"GRAPHQL OK: matches={len(matches)}, pool counts={pools}, nonempty odds fields={quotes}", flush=True)
    return {"matches": len(matches), "pools": pools, "quotes": quotes}


def main():
    if os.getenv("GITHUB_EVENT_NAME") == "schedule":
        print("DIAGNOSTIC MODE: scheduled run skipped; no requests or data writes.", flush=True)
        return 0
    print("HKJC GRAPHQL DIAGNOSTIC v1.4 — no docs/ files will be changed", flush=True)
    print("STEP A: browser-observed HAD+EHA", flush=True)
    baseline = get_once(("HAD", "EHA"))
    if baseline is None:
        print("RESULT: baseline failed. Review the response above; target was not tested.", flush=True)
        return 1
    time.sleep(2)
    print("STEP B: same GET shape, target HAD+HDC+HIL", flush=True)
    target = get_once(("HAD", "HDC", "HIL"))
    if target is None:
        print("RESULT: baseline succeeded but target failed. Market combination is a possible cause.", flush=True)
        return 1
    print("RESULT: both requests returned valid GraphQL data. TEST ONLY: no odds history was saved.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
