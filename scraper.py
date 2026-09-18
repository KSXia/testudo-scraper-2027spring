import requests
from bs4 import BeautifulSoup
import csv
import time
import os
import random
from datetime import datetime
import json

# --- CONFIGURATION ---
TERM = "202701" # Spring 2027

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

def get_csv_path():
    """Ensures the data directory exists and returns the path for today's CSV."""
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)
    
    current_date = datetime.now().strftime("%Y-%m-%d")
    return os.path.join(DATA_DIR, f"{current_date}.csv")

# Masking our script to look like a normal Chrome browser
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest" 
}

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
        requests.post(webhook_url, json={"content": message})
    except Exception as e:
        print(f"Failed to send Discord alert to {channel_type}: {e}")

# --- SCRAPER FUNCTIONS ---
def get_department_prefixes():
    """Scrapes the main SOC page to get a list of all department prefixes."""
    response = requests.get(BASE_URL, headers=HEADERS)
    soup = BeautifulSoup(response.text, 'html.parser')
    
    prefixes = []
    for prefix_elem in soup.find_all('span', class_='prefix-abbrev'):
        prefixes.append(prefix_elem.text.strip())
        
    return prefixes

def save_to_csv(data):
    if not data:
        return 
        
    target_path = get_csv_path() 
    file_exists = os.path.isfile(target_path)
    
    with open(target_path, mode='a', newline='', encoding='utf-8') as file:
        writer = csv.writer(file)
        if not file_exists:
            writer.writerow(['Timestamp', 'Course', 'Section', 'Instructor', 'Total_Seats', 'Open_Seats', 'Waitlist', 'Fetch_Time', 'Actual_Log_Time'])
        writer.writerows(data)

def scrape_departments(prefixes, batch_timestamp):
    """Loops through departments, grabs course IDs, and fetches sections via AJAX."""
    total_sections_scraped = 0
    
    for prefix in prefixes:
        print(f"Scraping {prefix}...", end=" ")
        dept_url = f"{BASE_URL}/{prefix}"
        
        try:
            response = requests.get(dept_url, headers=HEADERS)
            soup = BeautifulSoup(response.text, 'html.parser')
            
            course_divs = soup.find_all('div', class_='course-id')
            course_ids = [div.text.strip() for div in course_divs]
            
            if not course_ids:
                print("Saved 0 sections.")
                continue
                
            dept_data = []
            
            for course_id in course_ids:
                ajax_url = f"{SECTIONS_URL}{course_id}"
                ajax_response = requests.get(ajax_url, headers=HEADERS)
                ajax_soup = BeautifulSoup(ajax_response.text, 'html.parser')
                
                sections = ajax_soup.find_all('div', class_='section')
                
                for section in sections:
                    section_id = section.find('span', class_='section-id').text.strip()
                    total_seats = section.find('span', class_='total-seats-count').text.strip()
                    open_seats = section.find('span', class_='open-seats-count').text.strip()
                    waitlist = section.find('span', class_='waitlist-count').text.strip()
                    
                    instructor_elem = section.find('span', class_='section-instructor')
                    instructor = instructor_elem.text.strip() if instructor_elem else "TBA"
                    
                    fetch_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    actual_log_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    
                    dept_data.append([batch_timestamp, course_id, section_id, instructor, total_seats, open_seats, waitlist, fetch_time, actual_log_time])
                    
                    # --- SEAT ALERT LOGIC ---
                    open_seats_int = int(open_seats) if open_seats.isdigit() else 0
                    
                    if course_id in WATCHLIST:
                        # Check if the section list is empty (track all) OR if this specific section is in the list
                        if not WATCHLIST[course_id] or section_id in WATCHLIST[course_id]:
                            if open_seats_int > 0:
                                send_discord_alert(f"<@&1493113652272955443> 🚨 **SEAT OPEN!** {course_id} (Sec {section_id}) has {open_seats_int} seats! Taught by: {instructor}")
                
                time.sleep(0.1) 
                
            save_to_csv(dept_data)
            total_sections_scraped += len(dept_data)
            print(f"Saved {len(dept_data)} sections.")
            
        except Exception as e:
            print(f"Failed! Error: {e}")
            
        time.sleep(1)
        
    return total_sections_scraped

# --- MAIN EXECUTION BLOCK ---
if __name__ == "__main__":
    start_time_obj = datetime.now()
    batch_timestamp = start_time_obj.strftime("%Y-%m-%d %H:%M:%S")
    
    print(f"Starting university-wide scrape at {batch_timestamp}")
    print(f"Data will be saved to the 'data/' directory.\n")
    
    send_discord_alert(f"▶️ **Testudo Scraper Started** at {batch_timestamp}", "log")

    try:
        prefixes = get_department_prefixes()
        print(f"Found {len(prefixes)} departments to scrape.\n")
        
        total_saved = scrape_departments(prefixes, batch_timestamp)
        
        end_time_obj = datetime.now()
        end_time_str = end_time_obj.strftime("%Y-%m-%d %H:%M:%S")
        duration_mins = round((end_time_obj - start_time_obj).total_seconds() / 60, 2)
        
        print(f"\nDone! Successfully scraped {total_saved} total sections at {end_time_str} in {duration_mins} minutes.")
        send_discord_alert(f"✅ **Testudo Scraper Finished at {end_time_str}!** Scraped {total_saved} sections in {duration_mins} minutes.", "log")
        
    except Exception as e:
        error_msg = f"❌ **Scraper Crashed!** Error: {e}"
        print(f"Critical error: {e}")
        
        send_discord_alert(error_msg, "log")
        send_discord_alert(error_msg, "error")
