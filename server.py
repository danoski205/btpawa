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
    """Create a requests session with retry strategy"""
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


def get_actual_season(session):
    """Get the actual (active) seasons and return the first one (English League)"""
    try:
        url = f"{BASE_URL}/seasons/list/actual"
        r = session.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()

        if not data or len(data) == 0:
            logger.error("No actual seasons returned")
            return None

        # English League is always the first in the list
        first_season = data[0]
        season_id = first_season.get("id")
        season_name = first_season.get("name", "Unknown")
        logger.info(f"Using season: {season_name} | ID: {season_id}")
        return season_id

    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch actual season: {e}")
        return None


def get_top5_team_forms(session, season_id):
    """Fetch top 5 teams and their forms"""
    try:
        url = f"{BASE_URL}/standings/by-season/{season_id}"
        r = session.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()

        if "competitionStandings" not in data or not data["competitionStandings"]:
            logger.warning(f"No competition standings found for season {season_id}")
            return []

        if "participantStandings" not in data["competitionStandings"][0]:
            logger.warning(f"No participant standings found for season {season_id}")
            return []

        teams = data["competitionStandings"][0]["participantStandings"][:5]

        return [
            {
                "team": t.get("name", "Unknown"),
                "form": t.get("form", [])
            }
            for t in teams
        ]
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch standings: {e}")
        return []
    except (KeyError, IndexError) as e:
        logger.error(f"Unexpected data structure: {e}")
        return []


def sleep_until_next_check():
    """Align checks to 2 minutes after each 5-minute interval"""
    try:
        now = datetime.now(TIMEZONE)
        base_minute = (now.minute // 5) * 5
        base_time = now.replace(minute=base_minute, second=0, microsecond=0)

        check_time = base_time + timedelta(minutes=2)
        if check_time <= now:
            check_time += timedelta(minutes=5)

        sleep_seconds = (check_time - now).total_seconds()
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    except Exception as e:
        logger.error(f"Error in sleep_until_next_check: {e}")
        time.sleep(300)


# ================= MAIN LOOP =================
def main():
    logger.info("✅ Betpawa DD Monitor Started")
    session = create_session()
    consecutive_errors = 0
    max_consecutive_errors = 5

    while True:
        try:
            sleep_until_next_check()

            season_id = get_actual_season(session)
            if not season_id:
                consecutive_errors += 1
                if consecutive_errors >= max_consecutive_errors:
                    logger.critical(f"Too many errors ({consecutive_errors}). Restarting session...")
                    session = create_session()
                    consecutive_errors = 0
                continue

            teams = get_top5_team_forms(session, season_id)
            if not teams:
                logger.warning("No teams retrieved")
                consecutive_errors += 1
                continue

            consecutive_errors = 0  # reset after successful retrieval

            for t in teams:
                team = t["team"]
                form = t["form"][:2] if t["form"] else []

                key = f"{season_id}:{team}"
                previous = last_seen_form.get(key)

                # Detect new D,D
                if form == ["D", "D"] and previous != ["D", "D"]:
                    timestamp = datetime.now(TIMEZONE).strftime('%H:%M:%S')
                    alert_msg = f"🚨 ALERT 🚨 | {team} has D,D | Season {season_id} | Time {timestamp}"
                    logger.warning(alert_msg)
                    print(alert_msg)

                last_seen_form[key] = form

        except KeyboardInterrupt:
            logger.info("Monitor stopped by user")
            break
        except Exception as e:
            logger.error(f"Unexpected error in main loop: {e}", exc_info=True)
            consecutive_errors += 1
            time.sleep(60)


if __name__ == "__main__":
    main()
