import requests
import time
from datetime import datetime, timedelta
import pytz

# ================= CONFIG =================

BASE_URL = "https://www.betpawa.ng/api/sportsbook/virtual/v1"
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json"
}

TIMEZONE = pytz.timezone("Africa/Lagos")  # Nigeria time

# store last seen 2-form per season+team
last_seen_form = {}

# =========================================


def get_current_season():
    url = f"{BASE_URL}/seasons/list/current"
    r = requests.get(url, headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json()["id"]


def get_top5_team_forms(season_id):
    url = f"{BASE_URL}/standings/by-season/{season_id}"
    r = requests.get(url, headers=HEADERS, timeout=10)
    r.raise_for_status()
    data = r.json()

    teams = data["competitionStandings"][0]["participantStandings"][:5]

    return [
        {
            "team": t["name"],
            "form": t.get("form", [])
        }
        for t in teams
    ]


def sleep_until_next_check():
    """
    Align checks to exactly:
    :02 :07 :12 :17 :22 :27 :32 :37 :42 :47 :52 :57
    """
    now = datetime.now(TIMEZONE)

    base_minute = (now.minute // 5) * 5
    base_time = now.replace(minute=base_minute, second=0, microsecond=0)

    check_time = base_time + timedelta(minutes=2)

    if check_time <= now:
        check_time += timedelta(minutes=5)

    sleep_seconds = (check_time - now).total_seconds()
    time.sleep(sleep_seconds)


# ================= MAIN LOOP =================

print("✅ Betpawa DD Monitor Started (Clock-Aligned)")

while True:
    sleep_until_next_check()

    try:
        season_id = get_current_season()
        teams = get_top5_team_forms(season_id)

        for t in teams:
            team = t["team"]
            form = t["form"][:2]

            key = f"{season_id}:{team}"
            previous = last_seen_form.get(key)

            # Detect NEW D,D only
            if form == ["D", "D"] and previous != ["D", "D"]:
                print(
                    f"🚨 ALERT 🚨 | {team} has D,D | "
                    f"Season {season_id} | "
                    f"Time {datetime.now(TIMEZONE).strftime('%H:%M:%S')}"
                )

            last_seen_form[key] = form

    except Exception as e:
        print("❌ Error:", e)
