#!/usr/bin/env python3
"""iPhone-first read-only HKJC odds history: GitHub Actions + GitHub Pages.

Only stdlib. No wagering, no account login, no circumvention. One scheduled
run every 2h. Secrets supplied through environment, never stored in repo.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
DATA_FILE = ROOT / "docs" / "data.json"
URL = "https://api.tipsme.hk/v1/football"
HKT = timezone(timedelta(hours=8))
ODDS_DAILY_CAP = 26   # 30 on Free; allow some headroom
FIXTURE_DAILY_CAP = 46  # 50 on Free; allow some headroom
PICK_COUNT = 2
MAX_SNAPSHOTS = 9000


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_time(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def name(x):
    return (x.get("name") or x.get("nameOriginal") or "?") if isinstance(x, dict) else str(x or "?")


def empty_state():
    return {"generated_at": None, "matches": {}, "snapshots": [], "runs": [], "counters": {}}


def load():
    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        data = empty_state()
    for key, default in empty_state().items():
        data.setdefault(key, default)
    return data


def daily_counter(state):
    day = now().astimezone(HKT).date().isoformat()
    # Only retain a small history, but protect across future scheduled runs.
    counts = state["counters"].setdefault(day, {"odds": 0, "fixtures": 0})
    state["counters"] = {d: v for d, v in state["counters"].items() if d >= (now().astimezone(HKT).date() - timedelta(days=10)).isoformat()}
    return counts


def request_json(state, kind, route, query=None):
    caps = {"odds": ODDS_DAILY_CAP, "fixtures": FIXTURE_DAILY_CAP}
    counts = daily_counter(state)
    if counts[kind] >= caps[kind]:
        print(f"SKIP: {kind} 日內本地上限 {caps[kind]} 已達")
        return None
    key = os.environ.get("TIPSME_API_KEY", "").strip()
    if not key:
        raise RuntimeError("缺少 GitHub Actions Secret: TIPSME_API_KEY")
    suffix = "?" + urllib.parse.urlencode(query) if query else ""
    url = URL + route + suffix
    headers = {"Authorization": "Bearer " + key, "Accept": "application/json", "User-Agent": "odds-monitor-iphone/0.1"}
    # Count attempts, including HTTP/network errors, to avoid spending API quota unexpectedly.
    counts[kind] += 1
    req = urllib.request.Request(url, headers=headers)
    print(f"GET {route} [kind={kind}] [used today={counts[kind]}]")
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {route} (Retry-After: {e.headers.get('Retry-After','?')})")
        if e.code in (401, 403):
            raise RuntimeError("Tipsme API 拒絕授權 (401/403)。請喺 GitHub Secrets 檢查 TIPSME_API_KEY；勿將 Key 放入公開 Repository。") from None
        return None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        print(f"Network / JSON error: {type(e).__name__}")
        return None
    if not isinstance(value, dict):
        print("Unexpected JSON envelope, ignored")
        return None
    return value


def fixture_rows(api_response):
    rows = api_response.get("data", []) if isinstance(api_response, dict) else []
    return [m for m in rows if isinstance(m, dict) and m.get("isHkjc") is True and m.get("id") is not None]


def select_matches(state, candidates):
    clock = now()
    dedup = {}
    for m in candidates:
        kickoff = parse_time(m.get("kickoffUtc"))
        if not kickoff or not timedelta(minutes=2) <= kickoff-clock <= timedelta(hours=30):
            continue
        mid = str(m["id"])
        state["matches"][mid] = {
            "id": mid, "home": name(m.get("home")), "away": name(m.get("away")),
            "competition": name(m.get("competition")), "kickoff_utc": iso(kickoff)
        }
        dedup[mid] = (kickoff, mid)
    return [mid for _, mid in sorted(dedup.values())[:PICK_COUNT]]


def extract_hkjc_1x2(payload, mid, seen_utc):
    data = payload.get("data", {}) if isinstance(payload, dict) else {}
    rows = data.get("matchResult", []) if isinstance(data, dict) else []
    result = []
    if not isinstance(rows, list):
        return result
    # The HKJC endpoint includes recorded odds history, so store source UTC
    # times alongside the first time this monitor observed each source quote.
    for quote in rows:
        if not isinstance(quote, dict):
            continue
        source_time = quote.get("recordedUtc") or seen_utc
        for s in ("home", "draw", "away"):
            try:
                value = float(str(quote.get(s, "")).strip())
            except (TypeError, ValueError):
                continue
            if not 1.0 < value < 100000 or not value == value:
                continue
            result.append({"source": "HKJC", "match_id": mid, "market": "1X2", "selection": s,
                           "odds": value, "source_time": source_time, "first_seen": seen_utc})
    return result


def append_new(state, incoming):
    rows = state["snapshots"]
    fingerprints = {(x.get("source"), str(x.get("match_id")), x.get("market"), x.get("selection"),
                     x.get("source_time"), x.get("odds")) for x in rows}
    n = 0
    for x in incoming:
        fingerprint = (x["source"], x["match_id"], x["market"], x["selection"], x["source_time"], x["odds"])
        if fingerprint not in fingerprints:
            rows.append(x)
            fingerprints.add(fingerprint)
            n += 1
    rows.sort(key=lambda x: (str(x.get("source_time","")), str(x.get("match_id","")), str(x.get("selection",""))))
    # Capped to prevent large static page payloads. Export/archive separately if needed.
    state["snapshots"] = rows[-MAX_SNAPSHOTS:]
    return n


def run():
    state = load()
    first = now()
    hk_today = first.astimezone(HKT).date()
    candidates = []
    err = None
    for day in (hk_today, hk_today + timedelta(days=1)):
        response = request_json(state, "fixtures", "/matches", {"date": day.isoformat(), "tz": 480, "page": 1, "pageSize": 100, "locale": "zh-HK"})
        candidates.extend(fixture_rows(response))
    selected = select_matches(state, candidates)
    new_records = 0
    for mid in selected:
        response = request_json(state, "odds", f"/matches/{mid}/odds/hkjc", {"markets": "matchResult", "locale": "zh-HK"})
        if response is not None:
            new_records += append_new(state, extract_hkjc_1x2(response, mid, iso(now())))
    state["generated_at"] = iso(now())
    state["runs"].append({"time": state["generated_at"], "selected": selected, "added": new_records})
    state["runs"] = state["runs"][-90:]
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":"), indent=1) + "\n", encoding="utf-8")
    print(f"Selected matches: {selected}, new odds rows: {new_records}, total snapshots: {len(state['snapshots'])}")
    print(f"Saved {DATA_FILE}")


if __name__ == "__main__":
    try:
        run()
    except RuntimeError as e:
        print("設定錯誤：" + str(e), file=sys.stderr)
        sys.exit(1)
