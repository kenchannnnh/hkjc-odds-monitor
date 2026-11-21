#!/usr/bin/env python3
"""HKJC public GraphQL football odds monitor v1.7. Python 3.12, standard library.

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
MAX_PAGES = 1  # Official browser request is 1..60; 61..120 returned HTTP 400 in real Actions
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
