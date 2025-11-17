import os
import requests
from datetime import datetime, timedelta
import pytz

# ---------------- CONFIG ----------------
IST = pytz.timezone("Asia/Kolkata")
UTC = pytz.utc

# Read secure tokens from GitHub Secrets
SLACK_TOKEN = os.getenv("SLACK_TOKEN")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL")
QA_OPS_GROUP_ID = os.getenv("QA_OPS_GROUP_ID")

PROJECT_ID = int(os.getenv("PROJECT_ID"))
AUTH_TOKEN = os.getenv("AUTH_TOKEN")

BS_API_KEY = os.getenv("BS_API_KEY")
BS_AUTH_OVERRIDE = os.getenv("BS_AUTH_OVERRIDE")

HEADERS_LCNC = {"Authorization": f"Bearer {AUTH_TOKEN}", "Accept": "application/json"}

BS_HEADERS = {
    "X-Service-API-Key": BS_API_KEY,
    "X-Auth-Override": BS_AUTH_OVERRIDE
}

# Endpoints
API_BASE = "https://api-observability.browserstack.com/api/v1/projects/LCNC_API_Tests/builds/v2/"

WEBAPP_BASE_QUERY_PREFIX = f"https://lcnc-api-preprod.bsstag.com/api/v1/projects/{PROJECT_ID}/builds?sortKey=created_at&sortOrder=desc&query="
WEBAPP_SUITE_QUERIES = {"PZero": "PZero", "POne": "POne", "PTwo": "PTwo"}

DESKTOP_URLS = {
    "Mac": "https://api-observability.browserstack.com/api/v1/projects/LCNC+Desktop+Tests+-+Mac/builds/v2",
    "Windows": "https://api-observability.browserstack.com/api/v1/projects/LCNC+Desktop+SDK+Build/builds/v2/"
}


# ---------------- SLACK HELPERS ----------------
def post_slack(text):
    url = "https://slack.com/api/chat.postMessage"
    headers = {"Authorization": f"Bearer {SLACK_TOKEN}", "Content-Type": "application/json"}
    return requests.post(url, json={"channel": SLACK_CHANNEL, "text": text}, headers=headers).json()


def post_slack_thread(text, thread_ts):
    url = "https://slack.com/api/chat.postMessage"
    headers = {"Authorization": f"Bearer {SLACK_TOKEN}", "Content-Type": "application/json"}
    payload = {"channel": SLACK_CHANNEL, "text": text, "thread_ts": thread_ts}
    return requests.post(url, json=payload, headers=headers).json()


# ---------------- UTIL ----------------
def within_last_week(dt):
    today = datetime.now(IST).date()
    last_monday = today - timedelta(days=7)
    last_friday = last_monday + timedelta(days=4)
    return last_monday <= dt.date() <= last_friday


def calc_stability(passed, failed):
    total = passed + failed
    if total == 0:
        return None
    return round((passed / total) * 100, 2)


# ---------------- API BUILDS ----------------
def fetch_api_builds():
    all_builds = []
    search_after = None

    while True:
        url = API_BASE
        if search_after:
            url += f"?searchAfter={search_after}"

        resp = requests.get(url, headers=BS_HEADERS).json()

        builds = resp.get("builds", [])
        next_token_list = resp.get("pagingParams", {}).get("searchAfter", [])
        next_token = next_token_list[0] if next_token_list else None

        if not builds:
            break

        for b in builds:
            if not b.get("finishedAt"):
                continue

            dt = datetime.fromisoformat(b["finishedAt"].replace("Z", "+00:00")).astimezone(IST)
            if within_last_week(dt):
                all_builds.append((dt, b))
            else:
                # Past last week → stop
                if dt.date() < (datetime.now(IST).date() - timedelta(days=7)):
                    return all_builds

        if not next_token:
            break

        search_after = next_token

    return all_builds


# ---------------- WEBAPP BUILDS ----------------
def fetch_webapp_builds():
    builds = []
    for suite, query in WEBAPP_SUITE_QUERIES.items():
        url = WEBAPP_BASE_QUERY_PREFIX + query
        resp = requests.get(url, headers=HEADERS_LCNC).json()
        for b in resp.get("message", {}).get("data", []):
            dt = datetime.strptime(b["createdAt"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC).astimezone(IST)
            if within_last_week(dt):
                b["_suite"] = suite
                builds.append((dt, b))
    return builds


# ---------------- DESKTOP BUILDS ----------------
def fetch_desktop_builds():
    all_builds = []
    for platform, url in DESKTOP_URLS.items():
        search_after = None
        while True:
            req_url = url
            if search_after:
                req_url += f"?searchAfter={search_after}"

            resp = requests.get(req_url, headers=BS_HEADERS).json()
            builds = resp.get("builds", [])
            next_token_list = resp.get("pagingParams", {}).get("searchAfter", [])
            next_token = next_token_list[0] if next_token_list else None

            if not builds:
                break

            for b in builds:
                if not b.get("finishedAt"):
                    continue

                dt = datetime.fromisoformat(b["finishedAt"].replace("Z", "+00:00")).astimezone(IST)
                if within_last_week(dt):
                    b["_platform"] = platform
                    all_builds.append((dt, b))
                else:
                    return all_builds

            if not next_token:
                break

            search_after = next_token
    return all_builds


# ---------------- AGGREGATION LOGIC ----------------
def compute_weekly(builds, suite_extractor, stats_extractor, suite_keys):
    today = datetime.now(IST).date()
    last_monday = today - timedelta(days=7)

    weekdays = {last_monday + timedelta(days=i): {suite: [] for suite in suite_keys} for i in range(5)}

    for dt, b in builds:
        suite = suite_extractor(b)
        if suite not in suite_keys:
            continue

        stats = stats_extractor(b)
        passed = stats.get("passed", 0)
        failed = stats.get("failed", 0)
        stab = calc_stability(passed, failed)
        if stab is None:
            continue

        weekdays[dt.date()][suite].append(stab)

    weekly_avg = {}
    for suite in suite_keys:
        vals = []
        for d in weekdays:
            if weekdays[d][suite]:
                vals.append(max(weekdays[d][suite]))
        weekly_avg[suite] = round(sum(vals) / len(vals), 2) if vals else None

    return weekly_avg, weekdays


# ---------------- MAIN REPORT ----------------
def main():
    # ---------------- API ----------------
    api_builds = fetch_api_builds()
    api_suites = ["prod_api_tests", "preprod_api_tests", "regression_api_tests"]

    api_weekly, api_days = compute_weekly(
        api_builds,
        suite_extractor=lambda b: b.get("name"),
        stats_extractor=lambda b: b.get("statusStats", {}),
        suite_keys=api_suites
    )

    # ---------------- Webapp ----------------
    webapp_builds = fetch_webapp_builds()
    webapp_suites = ["PZero", "POne", "PTwo"]

    webapp_weekly, webapp_days = compute_weekly(
        webapp_builds,
        suite_extractor=lambda b: b.get("_suite"),
        stats_extractor=lambda b: b.get("details", {}),
        suite_keys=webapp_suites
    )

    # ---------------- Desktop ----------------
    desktop_builds = fetch_desktop_builds()
    desktop_suites = ["Mac", "Windows"]

    desktop_weekly, desktop_days = compute_weekly(
        desktop_builds,
        suite_extractor=lambda b: b.get("_platform"),
        stats_extractor=lambda b: b.get("statusStats", {}),
        suite_keys=desktop_suites
    )

    # ---------------- SUMMARY ----------------
    summary = f"""
*LCNC Weekly Stability (Mon–Fri)*

*API*
• Prod: {api_weekly['prod_api_tests']}
• Preprod: {api_weekly['preprod_api_tests']}
• Regression: {api_weekly['regression_api_tests']}

*Webapp*
• PZero: {webapp_weekly['PZero']}
• POne: {webapp_weekly['POne']}
• PTwo: {webapp_weekly['PTwo']}

*Desktop*
• Mac: {desktop_weekly['Mac']}
• Windows: {desktop_weekly['Windows']}

cc <!subteam^{QA_OPS_GROUP_ID}>
"""

    # Post summary
    resp = post_slack(summary)
    thread_ts = resp.get("ts")

    # ---------------- PER PRODUCT THREADS ----------------
    # API Thread
    api_text = ["*API Daily Breakdown (Mon–Fri)*"]
    for day in sorted(api_days.keys()):
        api_text.append(f"\n*{day.strftime('%a %d-%b')}*")
        for suite in api_suites:
            vals = api_days[day][suite]
            api_text.append(f"• {suite}: {max(vals) if vals else 'No builds'}")
    post_slack_thread("\n".join(api_text), thread_ts)

    # Webapp Thread
    web_text = ["*Webapp Daily Breakdown (Mon–Fri)*"]
    for day in sorted(webapp_days.keys()):
        web_text.append(f"\n*{day.strftime('%a %d-%b')}*")
        for suite in webapp_suites:
            vals = webapp_days[day][suite]
            web_text.append(f"• {suite}: {max(vals) if vals else 'No builds'}")
    post_slack_thread("\n".join(web_text), thread_ts)

    # Desktop Thread
    desk_text = ["*Desktop Daily Breakdown (Mon–Fri)*"]
    for day in sorted(desktop_days.keys()):
        desk_text.append(f"\n*{day.strftime('%a %d-%b')}*")
        for suite in desktop_suites:
            vals = desktop_days[day][suite]
            desk_text.append(f"• {suite}: {max(vals) if vals else 'No builds'}")
    post_slack_thread("\n".join(desk_text), thread_ts)


if __name__ == "__main__":
    main()
