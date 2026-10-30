import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
import csv
import time
import os
import json
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

# --- CONFIGURATION ---
TERM = "202701"  # Spring 2027

# Try to load the watchlist from GitHub Secrets, default to empty dict if missing
watchlist_env = os.environ.get('WATCHLIST_JSON', '{}')
try:
    WATCHLIST = json.loads(watchlist_env)
except json.JSONDecodeError:
    print("Error parsing WATCHLIST_JSON secret. Ensure it is valid JSON.")
    WATCHLIST = {}

BASE_URL = f"https://app.testudo.umd.edu/soc/{TERM}"
SECTIONS_URL = f"https://app.testudo.umd.edu/soc/{TERM}/sections?courseIds="

# --- DYNAMIC FILE PATHING ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, 'data')

def get_csv_path(batch_dt):
    """Ensures the data directory exists and returns the CSV path for the batch's start date."""
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)
    
    batch_date = batch_dt.strftime("%Y-%m-%d")
    return os.path.join(DATA_DIR, f"{batch_date}.csv")

# Masking our script to look like a normal Chrome browser
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest"
}

def create_session():
    """Creates a requests Session with connection pooling and automatic retries."""
    session = requests.Session()
    session.headers.update(HEADERS)
    retry_strategy = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

# --- DISCORD ALERT FUNCTION ---
def send_discord_alert(message, channel_type="alert"):
    """Routes a message to the correct Discord webhook channel."""
    if channel_type == "log":
        webhook_url = os.environ.get('DISCORD_WEBHOOK_LOGS')
    elif channel_type == "error":
        webhook_url = os.environ.get('DISCORD_WEBHOOK_ERRORS')
    else:
        webhook_url = os.environ.get('DISCORD_WEBHOOK_ALERTS')
        
    if not webhook_url:
        return

    try:
        requests.post(webhook_url, json={"content": message}, timeout=10)
    except Exception as e:
        print(f"Failed to send Discord alert to {channel_type}: {e}")

# --- HELPER FUNCTIONS ---
def get_span_text(parent, class_name, default="0"):
    """Safely extracts stripped text from a span without raising AttributeError if missing."""
    elem = parent.find('span', class_=class_name)
    if elem and elem.text:
        return " ".join(elem.text.split())
    return default

def get_department_prefixes(session):
    """Scrapes the main SOC page to get a list of all department prefixes."""
    response = session.get(BASE_URL, timeout=15)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    
    prefixes = []
    for prefix_elem in soup.find_all('span', class_='prefix-abbrev'):
        prefixes.append(prefix_elem.text.strip())
        
    return prefixes

def save_to_csv(data, target_path):
    if not data:
        return
        
    file_exists = os.path.isfile(target_path)
    
    with open(target_path, mode='a', newline='', encoding='utf-8') as file:
        writer = csv.writer(file)
        if not file_exists:
            writer.writerow([
                'Timestamp', 'Course', 'Section', 'Instructor',
                'Total_Seats', 'Open_Seats', 'Waitlist',
                'Fetch_Time', 'Actual_Log_Time'
            ])
        writer.writerows(data)

def scrape_departments(session, prefixes, batch_timestamp, csv_path):
    """Loops through departments, grabs course IDs, and fetches sections via AJAX."""
    total_sections_scraped = 0
    failed_courses = []
    
    for prefix in prefixes:
        print(f"Scraping {prefix}...", end=" ")
        dept_url = f"{BASE_URL}/{prefix}"
        dept_data = []
        
        try:
            response = session.get(dept_url, timeout=15)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            
            course_divs = soup.find_all('div', class_='course-id')
            course_ids = [div.text.strip() for div in course_divs if div.text.strip()]
            
            if not course_ids:
                print("Saved 0 sections.")
                continue
            
            for course_id in course_ids:
                try:
                    ajax_url = f"{SECTIONS_URL}{course_id}"
                    ajax_response = session.get(ajax_url, timeout=15)
                    ajax_response.raise_for_status()
                    
                    fetch_time = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S")
                    ajax_soup = BeautifulSoup(ajax_response.text, 'html.parser')
                    
                    # Match section blocks that actually contain a section-id span
                    sections = ajax_soup.find_all('div', class_='section')
                    
                    for section in sections:
                        section_id_elem = section.find('span', class_='section-id')
                        if not section_id_elem:
                            continue
                            
                        section_id = section_id_elem.text.strip()
                        total_seats = get_span_text(section, 'total-seats-count', default="0")
                        open_seats = get_span_text(section, 'open-seats-count', default="0")
                        waitlist = get_span_text(section, 'waitlist-count', default="0")
                        
                        # Handle single or multiple instructors cleanly
                        instructor_elems = section.find_all('span', class_='section-instructor')
                        if instructor_elems:
                            instructors = [
                                " ".join(el.text.split())
                                for el in instructor_elems
                                if el.text.strip()
                            ]
                            instructor = "; ".join(instructors) if instructors else "TBA"
                        else:
                            instructor = "TBA"
                        
                        actual_log_time = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                        
                        dept_data.append([
                            batch_timestamp, course_id, section_id, instructor,
                            total_seats, open_seats, waitlist, fetch_time, actual_log_time
                        ])
                        
                        # --- SEAT ALERT LOGIC ---
                        try:
                            open_seats_int = int(open_seats)
                        except ValueError:
                            open_seats_int = 0
                        
                        if course_id in WATCHLIST:
                            watched_sections = WATCHLIST[course_id]
                            if not watched_sections or section_id in watched_sections:
                                if open_seats_int > 0:
                                    send_discord_alert(
                                        f"<@&1493113652272955443> 🚨 **SEAT OPEN!** {course_id} "
                                        f"(Sec {section_id}) has {open_seats_int} seats! Taught by: {instructor}"
                                    )
                    
                    time.sleep(0.1)
                    
                except Exception as course_err:
                    failed_courses.append(f"{course_id} ({course_err})")
                    print(f"\n  [!] Warning: Failed on course {course_id}: {course_err}")
            
            # Always save whatever sections succeeded in this department
            save_to_csv(dept_data, csv_path)
            total_sections_scraped += len(dept_data)
            print(f"Saved {len(dept_data)} sections.")
            
        except Exception as dept_err:
            # Even if department-level error occurs, save any partial data collected
            if dept_data:
                save_to_csv(dept_data, csv_path)
                total_sections_scraped += len(dept_data)
            print(f"Failed! Error: {dept_err}")
            send_discord_alert(f"⚠️ **Error scraping {prefix}:** {dept_err}", "error")
            
        time.sleep(1)
        
    if failed_courses:
        summary = ", ".join(failed_courses[:10])
        more = f" (+{len(failed_courses) - 10} more)" if len(failed_courses) > 10 else ""
        send_discord_alert(f"⚠️ **Course-level errors ({len(failed_courses)}):** {summary}{more}", "error")
        
    return total_sections_scraped

# --- MAIN EXECUTION BLOCK ---
if __name__ == "__main__":
    start_time_obj = datetime.now(ZoneInfo("America/New_York"))
    batch_timestamp = start_time_obj.strftime("%Y-%m-%d %H:%M:%S")
    csv_path = get_csv_path(start_time_obj)
    
    print(f"Starting university-wide scrape at {batch_timestamp}")
    print(f"Data will be saved to: {csv_path}\n")
    
    send_discord_alert(f"▶️ **Testudo Scraper Started** at {batch_timestamp}", "log")

    try:
        session = create_session()
        prefixes = get_department_prefixes(session)
        print(f"Found {len(prefixes)} departments to scrape.\n")
        
        total_saved = scrape_departments(session, prefixes, batch_timestamp, csv_path)
        
        end_time_obj = datetime.now(ZoneInfo("America/New_York"))
        end_time_str = end_time_obj.strftime("%Y-%m-%d %H:%M:%S")
        duration_mins = round((end_time_obj - start_time_obj).total_seconds() / 60, 2)
        
        print(f"\nDone! Successfully scraped {total_saved} total sections at {end_time_str} in {duration_mins} minutes.")
        send_discord_alert(
            f"✅ **Testudo Scraper Finished at {end_time_str}!** "
            f"Scraped {total_saved} sections in {duration_mins} minutes.",
            "log"
        )
        
    except Exception as e:
        error_msg = f"❌ **Scraper Crashed!** Error: {e}"
        print(f"Critical error: {e}")
        
        send_discord_alert(error_msg, "log")
        send_discord_alert(error_msg, "error")
        sys.exit(1)
