import requests
import time
import logging
import os
from datetime import datetime, timedelta
import pytz
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ================= LOGGING SETUP =================
# Render best practice: log to stdout only

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
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
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json",
}

try:
    TIMEZONE = pytz.timezone(TIMEZONE_STR)
except pytz.exceptions.UnknownTimeZoneError:
    logger.error(f"Invalid timezone: {TIMEZONE_STR}. Using UTC.")
    TIMEZONE = pytz.UTC

# store last seen 2-form per team
last_seen_form = {}

# ================= SESSION =================

def create_session():
    session = requests.Session()

    retry_strategy = Retry(
        total=MAX_RETRIES,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )

    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    return session

# ================= DATA FETCH =================

def get_top5_team_forms(session):
    """
    Fetch top 5 teams and their form directly from standings.
    No season endpoint is used (confirmed unavailable).
    """
    try:
        url = f"{BASE_URL}/standings"
        r = session.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()

        competition = data.get("competitionStandings")
        if not competition:
            logger.warning("No competition standings found")
            return []

        participants = competition[0].get("participantStandings")
        if not participants:
            logger.warning("No participant standings found")
            return []

        top5 = participants[:5]

        return [
            {
                "team": t.get("name", "Unknown"),
                "form": t.get("form", []),
            }
            for t in top5
        ]

    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch standings: {e}")
        return []
    except (KeyError, IndexError, TypeError) as e:
        logger.error(f"Unexpected response structure: {e}")
        return []

# ================= CLOCK ALIGN =================

def sleep_until_next_check():
    """
    Align checks to:
    :02 :07 :12 :17 :22 :27 :32 :37 :42 :47 :52 :57
    """
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
        logger.error(f"Sleep alignment error: {e}")
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

            teams = get_top5_team_forms(session)
            if not teams:
                consecutive_errors += 1
                logger.warning("No teams retrieved")
                if consecutive_errors >= max_consecutive_errors:
                    logger.critical("Too many errors, recreating session")
                    session = create_session()
                    consecutive_errors = 0
                continue

            consecutive_errors = 0

            for t in teams:
                team = t["team"]
                form = t["form"][:2] if t["form"] else []

                previous = last_seen_form.get(team)

                # Detect NEW D,D only
                if form == ["D", "D"] and previous != ["D", "D"]:
                    timestamp = datetime.now(TIMEZONE).strftime("%H:%M:%S")
                    msg = f"🚨 ALERT 🚨 | {team} has D,D | Time {timestamp}"
                    logger.warning(msg)
                    print(msg)

                last_seen_form[team] = form

        except KeyboardInterrupt:
            logger.info("Monitor stopped by user")
            break
        except Exception as e:
            logger.error("Unexpected error in main loop", exc_info=True)
            time.sleep(60)

# ================= ENTRY =================

if __name__ == "__main__":
    main()
