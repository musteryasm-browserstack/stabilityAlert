# a_weekly_report_single_thread.py
import requests
from datetime import datetime, timedelta
import pytz
import os
import json

# ---------------- CONFIG ----------------
IST = pytz.timezone("Asia/Kolkata")
UTC = pytz.utc

# Slack config - replace with your actual tokens
SLACK_TOKEN = os.getenv("SLACK_TOKEN")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL")
QA_OPS_GROUP_ID = os.getenv("QA_OPS_GROUP_ID")

# API / Auth config (replace tokens if needed)
PROJECT_ID = os.getenv("PROJECT_ID")
AUTH_TOKEN = os.getenv("AUTH_TOKEN")
HEADERS_LCNC = {"Authorization": f"Bearer {AUTH_TOKEN}", "Accept": "application/json"}

BS_HEADERS = {
    "X-Service-API-Key": os.getenv("BS_SERVICE_API_KEY"),
    "X-Auth-Override": os.getenv("BS_AUTH_OVERRIDE")
}

# Endpoints
API_BASE = "https://api-observability.browserstack.com/api/v1/projects/LCNC_API_Tests/builds/v2/"
WEBAPP_BASE = f"https://lcnc-api-preprod.bsstag.com/api/v1/projects/{PROJECT_ID}/builds?query=&sortKey=created_at&sortOrder=desc"
DESKTOP_URLS = {
    "Mac": "https://api-observability.browserstack.com/api/v1/projects/LCNC+Desktop+Tests+-+Mac/builds/v2",
    "Windows": "https://api-observability.browserstack.com/api/v1/projects/LCNC+Desktop+SDK+Build/builds/v2/"
}

# Webapp suites filter using query strings
WEBAPP_SUITE_QUERIES = {"PZero": "PZero", "POne": "POne", "PTwo": "PTwo"}
WEBAPP_BASE_QUERY_PREFIX = f"https://lcnc-api-preprod.bsstag.com/api/v1/projects/{PROJECT_ID}/builds?sortKey=created_at&sortOrder=desc&query="

# ---------------- HELPERS ----------------
def post_slack(text):
    url = "https://slack.com/api/chat.postMessage"
    payload = {"channel": SLACK_CHANNEL, "text": text}
    headers = {"Authorization": f"Bearer {SLACK_TOKEN}", "Content-Type": "application/json"}
    r = requests.post(url, json=payload, headers=headers)
    r.raise_for_status()
    return r.json()

def post_slack_thread(text, thread_ts):
    url = "https://slack.com/api/chat.postMessage"
    payload = {"channel": SLACK_CHANNEL, "text": text, "thread_ts": thread_ts}
    headers = {"Authorization": f"Bearer {SLACK_TOKEN}", "Content-Type": "application/json"}
    r = requests.post(url, json=payload, headers=headers)
    r.raise_for_status()
    return r.json()

# Stability util
def calc_stability_from_stats(stats):
    passed = stats.get("passed", 0)
    failed = stats.get("failed", 0)
    total = passed + failed
    if total == 0:
        return None
    return round((passed / total) * 100, 2)

# ---------------- FETCH FUNCTIONS ----------------
def fetch_api_builds_for_last_week():
    """Paginated fetch for API builds (name field contains suite names like prod_api_tests etc)."""
    today = datetime.now(IST).date()
    last_monday = today - timedelta(days=7)
    last_friday = last_monday + timedelta(days=4)
    all_builds = []
    search_after = None

    while True:
        url = API_BASE
        if search_after:
            url += f"?searchAfter={search_after}"

        resp = requests.get(url, headers=BS_HEADERS)
        resp.raise_for_status()
        data = resp.json()

        builds = data.get("builds", [])
        paging = data.get("pagingParams", {}) or {}
        next_token_list = paging.get("searchAfter", [])
        next_token = next_token_list[0] if next_token_list else None

        if not builds:
            break

        for b in builds:
            finished = b.get("finishedAt")
            dt = datetime.fromisoformat(finished.replace("Z", "+00:00")).astimezone(IST)
            build_day = dt.date()
            if last_monday <= build_day <= last_friday:
                all_builds.append((dt, b))
            if build_day < last_monday:
                return all_builds

        if not next_token:
            break
        search_after = next_token
    return all_builds

def fetch_webapp_suite_builds(query_name):
    url = WEBAPP_BASE_QUERY_PREFIX + query_name
    resp = requests.get(url, headers=HEADERS_LCNC)
    resp.raise_for_status()
    return resp.json().get("message", {}).get("data", [])

def fetch_desktop_builds_for_last_week(url):
    today = datetime.now(IST).date()
    last_monday = today - timedelta(days=7)
    last_friday = last_monday + timedelta(days=4)
    all_builds = []
    search_after = None
    while True:
        req_url = url
        if search_after:
            req_url += f"?searchAfter={search_after}"
        resp = requests.get(req_url, headers=BS_HEADERS)
        resp.raise_for_status()
        data = resp.json()
        builds = data.get("builds", [])
        paging = data.get("pagingParams", {}) or {}
        sa_list = paging.get("searchAfter", [])
        next_token = sa_list[0] if sa_list else None
        if not builds:
            break
        for b in builds:
            finished = b.get("finishedAt")
            dt = datetime.fromisoformat(finished.replace("Z", "+00:00")).astimezone(IST)
            build_day = dt.date()
            if last_monday <= build_day <= last_friday:
                all_builds.append((dt, b))
            if build_day < last_monday:
                return all_builds
        if not next_token:
            break
        search_after = next_token
    return all_builds

# ---------------- AGGREGATION (common pattern) ----------------
def compute_best_per_day_from_builds(builds, get_suite_name_fn, extract_stats_fn, suites_list):
    """
    builds: list of (dt, build_obj)
    get_suite_name_fn: function(build_obj) -> suite_key (e.g., 'prod_api_tests' or 'PZero')
    extract_stats_fn: function(build_obj) -> dict with passed/failed keys or statusStats
    suites_list: keys to include
    """
    today = datetime.now(IST).date()
    last_monday = today - timedelta(days=7)
    days = {last_monday + timedelta(days=i): {s: [] for s in suites_list} for i in range(5)}

    for dt, b in builds:
        day = dt.date()
        if day not in days:
            continue
        suite_key = get_suite_name_fn(b)
        if suite_key not in suites_list:
            continue
        stats = extract_stats_fn(b)
        if not stats:
            continue
        passed = stats.get("passed", 0)
        failed = stats.get("failed", 0)
        total = passed + failed
        if total == 0:
            continue
        stability = round((passed / total) * 100, 2)
        days[day][suite_key].append((stability, passed, failed, dt))
    # choose best per day per suite (highest stability)
    best_per_day = {}
    weekly_avg = {s: [] for s in suites_list}
    for day in sorted(days.keys()):
        best_per_day[day] = {}
        for s in suites_list:
            runs = days[day][s]
            if not runs:
                best_per_day[day][s] = None
            else:
                best = max(runs, key=lambda x: x[0])
                best_per_day[day][s] = {"stability": best[0], "passed": best[1], "failed": best[2], "time": best[3]}
                weekly_avg[s].append(best[0])
    # compute weekly averages
    weekly_avg_values = {s: (round(sum(weekly_avg[s]) / len(weekly_avg[s]), 2) if weekly_avg[s] else None) for s in suites_list}
    return best_per_day, weekly_avg_values

# ---------------- BUILD THE REPORT ----------------
def build_combined_weekly_report():
    # 1) API
    api_builds = fetch_api_builds_for_last_week()
    # get suite name for API (build['name'] contains suite)
    api_get_suite = lambda b: b.get("name")
    api_extract_stats = lambda b: b.get("statusStats", {})
    api_suites = ["prod_api_tests", "preprod_api_tests", "regression_api_tests"]
    api_best_per_day, api_week_avg = compute_best_per_day_from_builds(api_builds, api_get_suite, api_extract_stats, api_suites)

    # 2) Webapp - fetch per-suite using query (no pagination for our filtered usage)
    web_builds_all = []
    for key, q in WEBAPP_SUITE_QUERIES.items():
        items = fetch_webapp_suite_builds(q)
        for b in items:
            created = datetime.strptime(b["createdAt"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC).astimezone(IST)
            web_builds_all.append((created, b))
    web_get_suite = lambda b: next((k for k,v in WEBAPP_SUITE_QUERIES.items() if v in (b.get("buildName","") or b.get("buildName",""))), None)
    # For webapp, we will detect suite by matching buildName substring; buildName usually contains PZero/POne/PTwo
    def web_get_suite_name(build):
        name = build.get("buildName") or build.get("buildName", "")
        for k, q in WEBAPP_SUITE_QUERIES.items():
            if q in name:
                return k
        # fallback: check testSuiteHashedId
        suite_id = build.get("testSuiteHashedId")
        for k, v in SUITE_IDS.items():
            if v == suite_id:
                return k
        return None
    # web stats extractor uses 'details' key
    web_extract_stats = lambda b: b.get("details", {})
    web_suites = list(WEBAPP_SUITE_QUERIES.keys())
    web_best_per_day, web_week_avg = compute_best_per_day_from_builds(web_builds_all, web_get_suite_name, web_extract_stats, web_suites)

    # 3) Desktop
    desktop_builds_all = []
    for platform, url in DESKTOP_URLS.items():
        items = fetch_desktop_builds_for_last_week(url)
        for dt, b in items:
            # attach platform info by adding a wrapper dict key
            b["_platform"] = platform
            desktop_builds_all.append((dt, b))
    desktop_get_suite = lambda b: b.get("_platform")
    desktop_extract_stats = lambda b: b.get("statusStats", {})
    desktop_suites = ["Mac", "Windows"]
    desktop_best_per_day, desktop_week_avg = compute_best_per_day_from_builds(desktop_builds_all, desktop_get_suite, desktop_extract_stats, desktop_suites)

    summary_lines = [
        "*LCNC Weekly Stability (Last Week : Mon–Fri)*",
        "",
        "*API*",
        f"• Prod: {api_week_avg.get('prod_api_tests', 'N/A')}%",
        f"• Preprod: {api_week_avg.get('preprod_api_tests', 'N/A')}%",
        f"• Regression: {api_week_avg.get('regression_api_tests', 'N/A')}%",
        "",
        "-------------------------------------",
        "",
        "*Webapp*",
        f"• PZero: {web_week_avg.get('PZero', 'N/A')}%",
        f"• POne: {web_week_avg.get('POne', 'N/A')}%",
        f"• PTwo: {web_week_avg.get('PTwo', 'N/A')}%",
        "",
        "-------------------------------------",
        "",
        "*Desktop*",
        f"• Mac: {desktop_week_avg.get('Mac', 'N/A')}%",
        f"• Windows: {desktop_week_avg.get('Windows', 'N/A')}%",
        ""
    ]

    summary_text = "\n".join(summary_lines)
    print("Summary Text:\n", summary_text)

    # Detailed combined breakdown text (one message)
    detailed_lines = ["*Detailed: Last Week (Mon→Fri)*"]
    # Day list (sorted)
    days_sorted = sorted(api_best_per_day.keys())
    for day in days_sorted:
        dstr = day.strftime("%a %d-%b")
        detailed_lines.append(f"\n*{dstr}*")
        # API suites
        detailed_lines.append("API:")
        for s in api_suites:
            v = api_best_per_day[day].get(s)
            if v:
                detailed_lines.append(f"  • {s}: {v['stability']}% (Passed {v['passed']}, Failed {v['failed']})")
            else:
                detailed_lines.append(f"  • {s}: No builds")
        # Webapp suites
        detailed_lines.append("Webapp:")
        for s in web_suites:
            v = web_best_per_day[day].get(s)
            if v:
                detailed_lines.append(f"  • {s}: {v['stability']}% (Passed {v['passed']}, Failed {v['failed']})")
            else:
                detailed_lines.append(f"  • {s}: No builds")
        # Desktop platforms
        detailed_lines.append("Desktop:")
        for s in desktop_suites:
            v = desktop_best_per_day[day].get(s)
            if v:
                detailed_lines.append(f"  • {s}: {v['stability']}% (Passed {v['passed']}, Failed {v['failed']})")
            else:
                detailed_lines.append(f"  • {s}: No builds")

    return summary_text, "\n".join(detailed_lines)

# ---------------- SLACK POSTING FLOW (single thread) ----------------

def send_weekly_report_per_product_threads():
    summary_text, detailed_text = build_combined_weekly_report()

    # Post summary
    resp = post_slack(summary_text + f"\ncc <!subteam^{QA_OPS_GROUP_ID}>")
    ts = resp.get("ts")
    if not ts:
        print("Failed to post summary:", resp)
        return

    # Now post one threaded reply per product with focused detail
    # We'll build three small blocks: API details, Webapp details, Desktop details
    # Recreate per-product detail strings from the combined detailed_text (or recompute)
    api_builds = fetch_api_builds_for_last_week()
    api_get_suite = lambda b: b.get("name")
    api_extract_stats = lambda b: b.get("statusStats", {})
    api_suites = ["prod_api_tests", "preprod_api_tests", "regression_api_tests"]
    api_best_per_day, api_week_avg = compute_best_per_day_from_builds(api_builds, api_get_suite, api_extract_stats, api_suites)

    # Prepare API message
    api_lines = ["*API — Last week daily*"]
    for day in sorted(api_best_per_day.keys()):
        api_lines.append("\n" + day.strftime("%a %d-%b"))
        for s in api_suites:
            v = api_best_per_day[day].get(s)
            if v:
                api_lines.append(f"  • {s}: {v['stability']}% (Passed {v['passed']}, Failed {v['failed']})")
            else:
                api_lines.append(f"  • {s}: No builds")
    # Post API thread reply
    post_slack_thread("\n".join(api_lines), ts)

    # Webapp message
    web_builds_all = []
    for key, q in WEBAPP_SUITE_QUERIES.items():
        items = fetch_webapp_suite_builds(q)
        for b in items:
            created = datetime.strptime(b["createdAt"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC).astimezone(IST)
            web_builds_all.append((created, b))
    web_get_suite = lambda b: next((k for k,v in WEBAPP_SUITE_QUERIES.items() if v in (b.get("buildName","") or "")), None)
    web_extract_stats = lambda b: b.get("details", {})
    web_suites = list(WEBAPP_SUITE_QUERIES.keys())
    web_best_per_day, web_week_avg = compute_best_per_day_from_builds(web_builds_all, web_get_suite, web_extract_stats, web_suites)

    web_lines = ["*Webapp — Last week daily*"]
    for day in sorted(web_best_per_day.keys()):
        web_lines.append("\n" + day.strftime("%a %d-%b"))
        for s in web_suites:
            v = web_best_per_day[day].get(s)
            if v:
                web_lines.append(f"  • {s}: {v['stability']}% (Passed {v['passed']}, Failed {v['failed']})")
            else:
                web_lines.append(f"  • {s}: No builds")
    post_slack_thread("\n".join(web_lines), ts)

    # Desktop message
    desktop_builds_all = []
    for platform, url in DESKTOP_URLS.items():
        items = fetch_desktop_builds_for_last_week(url)
        for dt, b in items:
            b["_platform"] = platform
            desktop_builds_all.append((dt, b))
    desktop_get_suite = lambda b: b.get("_platform")
    desktop_extract_stats = lambda b: b.get("statusStats", {})
    desktop_suites = ["Mac", "Windows"]
    desktop_best_per_day, desktop_week_avg = compute_best_per_day_from_builds(desktop_builds_all, desktop_get_suite, desktop_extract_stats, desktop_suites)

    desk_lines = ["*Desktop — Last week daily*"]
    for day in sorted(desktop_best_per_day.keys()):
        desk_lines.append("\n" + day.strftime("%a %d-%b"))
        for s in desktop_suites:
            v = desktop_best_per_day[day].get(s)
            if v:
                desk_lines.append(f"  • {s}: {v['stability']}% (Passed {v['passed']}, Failed {v['failed']})")
            else:
                desk_lines.append(f"  • {s}: No builds")
    post_slack_thread("\n".join(desk_lines), ts)

# Use whichever flow you want
if __name__ == "__main__":
    # choose: send_weekly_report_single_thread()  OR  send_weekly_report_per_product_threads()
    # send_weekly_report_single_thread()
    send_weekly_report_per_product_threads()

