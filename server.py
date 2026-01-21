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
    """
    Fetch the latest actual season that has matches played,
    then add +1 to form the current/live season.
    """
    try:
        url = f"{BASE_URL}/seasons/list/actual"
        r = session.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        items = data.get("items", [])
        if not items:
            logger.error("No seasons returned from /seasons/list/actual")
            return None

        # Sort seasons descending by ID to start from latest
        seasons_sorted = sorted(items, key=lambda x: int(x["id"]), reverse=True)

        # Find the latest season that has standings with at least one form
        latest_valid_season_id = None
        for season in seasons_sorted:
            season_id = season.get("id")
            try:
                standings_url = f"{BASE_URL}/standings/by-season/{season_id}"
                sr = session.get(standings_url, headers=HEADERS, timeout=TIMEOUT)
                sr.raise_for_status()
                sdata = sr.json()
                participant_standings = sdata.get("competitionStandings", [{}])[0].get("participantStandings", [])
                if participant_standings and any(t.get("form") for t in participant_standings):
                    latest_valid_season_id = int(season_id)
                    break
            except Exception:
                continue  # skip seasons that fail or have no standings

        if not latest_valid_season_id:
            logger.warning("No completed/active season found. Cannot determine current season.")
            return None

        # Add +1 to get current/live season
        current_live_season_id = latest_valid_season_id + 1
        logger.info(f"Using current/live season: #{current_live_season_id} (latest completed season: #{latest_valid_season_id})")
        return str(current_live_season_id)

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
        if not standings or not standings[0].get("participantStandings"):
            logger.warning(f"No participant standings found for season {season_id}")
            return []

        teams = standings[0]["participantStandings"][:5]

        return [
            {
                "team": t.get("name", "Unknown"),
                "form": t.get("form", [])
            }
            for t in teams
        ]

    except Exception as e:
        logger.error(f"Failed to fetch standings: {e}")
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

            season_id = get_current_season(session)
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

            consecutive_errors = 0

            found_dd = False
            for t in teams:
                team = t["team"]
                form = t["form"][-2:] if t["form"] else []

                key = f"{season_id}:{team}"
                previous = last_seen_form.get(key)

                if form == ["D", "D"] and previous != ["D", "D"]:
                    timestamp = datetime.now(TIMEZONE).strftime('%H:%M:%S')
                    alert_msg = f"🚨 ALERT 🚨 | {team} has D,D | Season {season_id} | Time {timestamp}"
                    logger.warning(alert_msg)
                    print(alert_msg)
                    found_dd = True

                last_seen_form[key] = form

            if not found_dd:
                logger.info(f"No D,D found for top 5 teams in season {season_id}")

        except KeyboardInterrupt:
            logger.info("Monitor stopped by user")
            break
        except Exception as e:
            logger.error(f"Unexpected error in main loop: {e}", exc_info=True)
            consecutive_errors += 1
            time.sleep(60)


if __name__ == "__main__":
    main()
