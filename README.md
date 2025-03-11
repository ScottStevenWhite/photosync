 # photosync

 A Python script that syncs photos to and from Google Photos:

 - Uploads local images from /Users/scwhite/Pictures to Google Photos (if not already present).
 - Downloads any new images from:
 - The last 3 months
 - A specific album (e.g., "Wedding Quick Recap")
 - (Optional) a "Starred" album

 ## Prerequisites

 1. Enable the Google Photos Library API in the Google Cloud Console.
 2. Create OAuth credentials (Desktop app) and download the JSON file.
 3. Place the JSON credentials file into ./data/credentials.json.

 ## Setup & Usage

 1. Install requirements:
 bash  pip install -r requirements.txt  
 2. Run the script:  bash  python photosync.py   - On first run, it will open a browser window to complete OAuth.  - Once finished, a token.json file is stored in ./data/. 
 3. Automation (optional):
 - Use a cron job or a LaunchAgent on macOS to run python photosync.py once a day or hour.  - The script automatically syncs new photos to and from Google Photos. 
 ## Customizing

 - Edit sync_config.json to adjust how many days you want to sync ("days") and which albums ("albums").  - Edit LOCAL_PHOTOS_DIR in photosync.py if you store photos elsewhere on your Mac.  - Adjust or add any additional logic for renaming or organizing files locally as needed. 
 ## Caveats

 - This script references a local JSON photos_map.json to keep track of what’s been downloaded and uploaded, preventing duplicates.  - If you manually rename or move files, the script might treat them as new. Adjust code accordingly.