import requests
import time
import logging
import os
from datetime import datetime, timedelta
import pytz
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ================= LOGGING SETUP =================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('betpawa_monitor.log')
    ]
)
logger = logging.getLogger(__name__)

# ================= CONFIG =================

BASE_URL = os.getenv(
    "BETPAWA_BASE_URL",
    "https://www.betpawa.ng/api/sportsbook/virtual/v1"
)
TIMEZONE_STR = os.getenv("TIMEZONE", "Africa/Lagos")
TIMEOUT = int(os.getenv("API_TIMEOUT", "10"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "x-pawa-brand": "betpawa-nigeria"
}

try:
    TIMEZONE = pytz.timezone(TIMEZONE_STR)
except pytz.exceptions.UnknownTimeZoneError:
    logger.error(f"Invalid timezone: {TIMEZONE_STR}. Using UTC.")
    TIMEZONE = pytz.UTC

# store last seen 2-form per season+team
last_seen_form = {}

# ================= HELPERS =================

def create_session():
    session = requests.Session()
    retry_strategy = Retry(
        total=MAX_RETRIES,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def get_current_season(session):
    """Fetch the base season from actual seasons list and add 1 for the live season"""
    try:
        url = f"{BASE_URL}/seasons/list/actual"
        r = session.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()

        items = data.get("items", [])
        if not items:
            logger.error("No seasons found in actual seasons list")
            return None

        base_season = items[0]
        base_season_id = base_season.get("id")
        base_season_name = base_season.get("name", "Unknown")

        live_season_id = int(base_season_id) + 1
        logger.info(
            f"Base season: #{base_season_id} ({base_season_name}) | "
            f"Live season for DD check: #{live_season_id}"
        )
        return str(live_season_id)

    except Exception as e:
        logger.error(f"Failed to fetch current season: {e}")
        return None


def get_top5_team_forms(session, season_id):
    """Fetch top 5 teams and their forms"""
    try:
        url = f"{BASE_URL}/standings/by-season/{season_id}"
        r = session.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()

        standings = data.get("competitionStandings")
        if not standings or "participantStandings" not in standings[0]:
            logger.warning(f"❌ DD data NOT found for season {season_id}")
            return []

        teams = standings[0]["participantStandings"][:5]
        logger.info(f"✅ DD data FOUND for season {season_id} - Monitoring {len(teams)} teams")

        return [
            {
                "team": t.get("name", "Unknown"),
                "form": t.get("form", [])
            }
            for t in teams
        ]

    except Exception as e:
        logger.error(f"❌ Failed to fetch standings: {e}")
        return []


def sleep_until_next_check():
    """Align checks to :02 :07 :12 :17 :22 :27 :32 :37 :42 :47 :52 :57"""
    try:
        now = datetime.now(TIMEZONE)
        base_minute = (now.minute // 5) * 5
        base_time = now.replace(minute=base_minute, second=0, microsecond=0)

        check_time = base_time + timedelta(minutes=2)
        if check_time <= now:
            check_time += timedelta(minutes=5)

        time.sleep(max(0, (check_time - now).total_seconds()))
    except Exception as e:
        logger.error(f"Sleep alignment error: {e}")
        time.sleep(300)

# ================= MAIN LOOP =================

def main():
    logger.info("✅ Betpawa DD Monitor Started (Clock-Aligned)")
    session = create_session()

    while True:
        try:
            sleep_until_next_check()

            season_id = get_current_season(session)
            if not season_id:
                continue

            teams = get_top5_team_forms(session, season_id)
            if not teams:
                continue

            for t in teams:
                team = t["team"]
                full_form = t["form"]

                # ✅ CORRECT: ONLY LAST TWO MATCHES
                last_two = full_form[-2:] if len(full_form) >= 2 else []

                key = f"{season_id}:{team}"
                previous = last_seen_form.get(key)

                if last_two == ["D", "D"] and previous != ["D", "D"]:
                    timestamp = datetime.now(TIMEZONE).strftime('%H:%M:%S')
                    msg = f"🚨 ALERT 🚨 | {team} has LAST TWO = D,D | Season {season_id} | {timestamp}"
                    logger.warning(msg)
                    print(msg)

                last_seen_form[key] = last_two

        except KeyboardInterrupt:
            logger.info("Monitor stopped by user")
            break
        except Exception as e:
            logger.error(f"Unexpected error: {e}", exc_info=True)
            time.sleep(60)


if __name__ == "__main__":
    main()
