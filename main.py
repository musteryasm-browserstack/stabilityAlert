import os
import requests
from datetime import datetime, timedelta
import pytz

# ------------------------------------------------------------
# CONFIG (READ FROM ENVIRONMENT)
# ------------------------------------------------------------
PROJECT_ID = 22530
AUTH_TOKEN = os.getenv("AUTH_TOKEN")
SLACK_TOKEN = os.getenv("SLACK_TOKEN")
X_SERVICE_API_KEY = os.getenv("X_SERVICE_API_KEY")
X_AUTH_OVERRIDE = os.getenv("X_AUTH_OVERRIDE")

if not all([AUTH_TOKEN, SLACK_TOKEN, X_SERVICE_API_KEY, X_AUTH_OVERRIDE]):
    raise EnvironmentError("❌ Missing one or more required environment variables.")

HEADERS_LCNC = {"Authorization": f"Bearer {AUTH_TOKEN}", "Accept": "application/json"}
BS_HEADERS = {
    "X-Service-API-Key": X_SERVICE_API_KEY,
    "X-Auth-Override": X_AUTH_OVERRIDE,
}

SLACK_CHANNEL = "C06T7FZ0BFZ"
QA_OPS_GROUP_ID = "S07L05V67B7"

IST = pytz.timezone("Asia/Kolkata")
UTC = pytz.utc

SUITE_IDS = {
    "PZero": "1d18ea79258d91e237ddb72a8516172c9271c80c",
    "POne": "4daf33f53ab2550ead6d5e40d77b1dcf3c80ea03",
    "PTwo": "f7c2516ca1a49f728223a21676c621db8c7b08dc"
}

API_V1_URL = (
    f"https://lcnc-api-preprod.bsstag.com/api/v1/projects/{PROJECT_ID}/builds?"
    "query=&sortKey=created_at&sortOrder=desc&users=&device=&status="
)

# ------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------
def get_yesterday_time_range_utc():
    today_ist = datetime.now(IST).date()
    yesterday_ist = today_ist - timedelta(days=1)
    start_ist = IST.localize(datetime.combine(yesterday_ist, datetime.min.time()))
    end_ist = IST.localize(datetime.combine(yesterday_ist, datetime.max.time()))
    return start_ist.astimezone(UTC), end_ist.astimezone(UTC)


def extract_test_counts(build_data):
    if not build_data or not isinstance(build_data, dict):
        return 0, 0, 0, 0, "N/A"
    details = build_data.get("details", {})
    passed = details.get("passed", 0)
    failed = details.get("failed", 0)
    skipped = details.get("skipped", 0)
    total = passed + failed + skipped
    if total == 0:
        return 0, 0, 0, 0, "N/A"
    stability = round((passed / total) * 100, 2)
    return passed, failed, skipped, total, stability


# ------------------------------------------------------------
# SECTION 1 — WEBAPP TESTS
# ------------------------------------------------------------
def fetch_webapp_builds():
    resp = requests.get(API_V1_URL, headers=HEADERS_LCNC)
    resp.raise_for_status()
    return resp.json().get("message", {}).get("data", [])


def get_yesterday_builds(builds):
    start_utc, end_utc = get_yesterday_time_range_utc()
    return [
        b for b in builds
        if "createdAt" in b and start_utc <= datetime.strptime(b["createdAt"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC) <= end_utc
    ]


def get_best_build(builds, suite_id):
    suite_builds = [b for b in builds if b.get("testSuiteHashedId") == suite_id]
    best_build, best_stability = None, -1
    for b in suite_builds:
        _, _, _, _, stability = extract_test_counts(b)
        if stability != "N/A" and stability > best_stability:
            best_build, best_stability = b, stability
    return best_build


def summarize_webapp_best_of_yesterday():
    builds = fetch_webapp_builds()
    yesterday_builds = get_yesterday_builds(builds)
    summaries = []
    total_passed = total_failed = total_total = 0

    for suite_name, suite_id in SUITE_IDS.items():
        best_build = get_best_build(yesterday_builds, suite_id)
        if not best_build:
            summaries.append(f"{suite_name} - N/A [No builds yesterday]")
            continue

        passed, failed, skipped, total, stability = extract_test_counts(best_build)
        created_utc = datetime.strptime(best_build["createdAt"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
        created_ist = created_utc.astimezone(IST).strftime("%H:%M")
        summaries.append(f"{suite_name} - {stability}% ({failed}/{total} failed) [Best run at {created_ist}]")

        total_passed += passed
        total_failed += failed
        total_total += total

    overall = "Overall - N/A"
    if total_total > 0:
        overall_stability = round((total_passed / total_total) * 100, 2)
        overall = f"Overall - {overall_stability}% ({total_failed}/{total_total} failed)"

    return f"*LCNC Webapp Tests*\n{overall}\n" + "\n".join(summaries)


# ------------------------------------------------------------
# SECTION 2 — DESKTOP TESTS (MAC + WINDOWS)
# ------------------------------------------------------------
def summarize_desktop_best_of_yesterday():
    urls = {
        "Mac": "https://api-observability.browserstack.com/api/v1/projects/LCNC+Desktop+Tests+-+Mac/builds/v2",
        "Windows": "https://api-observability.browserstack.com/api/v1/projects/LCNC+Desktop+Tests+-+Windows/builds/v2",
    }

    summaries = ["*LCNC Desktop Tests*"]
    start_utc, end_utc = get_yesterday_time_range_utc()

    for platform, url in urls.items():
        try:
            resp = requests.get(url, headers=BS_HEADERS)
            resp.raise_for_status()
            builds = resp.json().get("builds", [])
            if not builds:
                summaries.append(f"{platform} - N/A (No builds found)")
                continue

            # Filter builds finished yesterday (IST)
            filtered = []
            for b in builds:
                try:
                    finished = datetime.fromisoformat(b["finishedAt"].replace("Z", "+00:00"))
                    if start_utc <= finished <= end_utc:
                        filtered.append(b)
                except Exception:
                    continue

            if not filtered:
                summaries.append(f"{platform} - N/A (No builds yesterday)")
                continue

            # Find best build (highest stability)
            best_build = None
            best_stability = -1
            best_passed = best_failed = best_total = 0

            for b in filtered:
                stats = b.get("statusStats", {})
                passed = stats.get("passed", 0)
                failed = stats.get("failed", 0)
                total = passed + failed
                if total == 0:
                    continue

                stability = round((passed / total) * 100, 2)
                if stability > best_stability:
                    best_stability = stability
                    best_build = b
                    best_passed, best_failed, best_total = passed, failed, total

            if not best_build:
                summaries.append(f"{platform} - N/A (No valid runs yesterday)")
                continue

            when_dt = (
                datetime.fromisoformat(best_build["finishedAt"].replace("Z", "+00:00"))
                .astimezone(IST)
                .strftime("%H:%M")
            )

            summaries.append(
                f"{platform} - {best_stability:.2f}% ({best_failed}/{best_total} failed) [Best run at {when_dt}]"
            )

        except Exception as e:
            summaries.append(f"{platform} - Error fetching: {e}")

    return "\n".join(summaries)


# ------------------------------------------------------------
# SECTION 3 — API TESTS
# ------------------------------------------------------------
def summarize_api_best_of_yesterday():
    url = "https://api-observability.browserstack.com/api/v1/projects/LCNC_API_Tests/builds/v2/"
    TARGETS = ["prod_api_tests", "preprod_api_tests", "regression_api_tests"]

    resp = requests.get(url, headers=BS_HEADERS)
    resp.raise_for_status()
    builds = resp.json().get("builds", [])

    start_utc, end_utc = get_yesterday_time_range_utc()
    summaries = ["*LCNC API Tests*"]

    for target in TARGETS:
        target_builds = [b for b in builds if b.get("name") == target]
        filtered = []
        for b in target_builds:
            try:
                finished = datetime.fromisoformat(b["finishedAt"].replace("Z", "+00:00"))
                if start_utc <= finished <= end_utc:
                    filtered.append(b)
            except Exception:
                continue

        if not filtered:
            summaries.append(f"{target} - N/A (No builds yesterday)")
            continue

        best_build = None
        best_stability = -1
        best_passed = best_failed = best_total = 0

        for b in filtered:
            stats = b.get("statusStats", {})
            passed = stats.get("passed", 0)
            failed = stats.get("failed", 0)
            total = passed + failed
            if total == 0:
                continue
            stability = round((passed / total) * 100, 2)
            if stability > best_stability:
                best_stability = stability
                best_build = b
                best_passed, best_failed, best_total = passed, failed, total

        if not best_build:
            summaries.append(f"{target} - N/A (No valid runs yesterday)")
            continue

        when_dt = datetime.fromisoformat(best_build["finishedAt"].replace("Z", "+00:00")) \
            .astimezone(IST).strftime("%H:%M")

        summaries.append(
            f"{target} - {best_stability:.2f}% ({best_failed}/{best_total} failed) [Best run at {when_dt}]"
        )

    return "\n".join(summaries)


# ------------------------------------------------------------
# SLACK INTEGRATION
# ------------------------------------------------------------
def send_to_slack(message):
    url = "https://slack.com/api/chat.postMessage"
    payload = {"channel": SLACK_CHANNEL, "text": message}
    headers = {"Authorization": f"Bearer {SLACK_TOKEN}", "Content-Type": "application/json"}
    response = requests.post(url, json=payload, headers=headers)
    print("✅ Slack Response:", response.json())


# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------
def main():
    try:
        webapp_msg = summarize_webapp_best_of_yesterday()
        desktop_msg = summarize_desktop_best_of_yesterday()
        api_msg = summarize_api_best_of_yesterday()

        final_message = (
            f"Yesterday's Stability Results\n\n"
            f"{webapp_msg}\n\n"
            f"{desktop_msg}\n\n"
            f"{api_msg}\n\n"
            f"cc <!subteam^{QA_OPS_GROUP_ID}>"
        )

        print(final_message)
        send_to_slack(final_message)
    except Exception as e:
        print(f"❌ Error fetching results: {e}")


if __name__ == "__main__":
    main()
