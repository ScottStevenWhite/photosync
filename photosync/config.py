from pathlib import Path
import json

# === PATH CONFIGURATION ===
DATA_DIR = Path("data")
LOCAL_PHOTOS_DIR = Path("/Users/scwhite/Pictures")  # or read from env

CONFIG_FILE = Path("sync_config.json")

# === SCOPES ===
SCOPES = [
    "https://www.googleapis.com/auth/photoslibrary",
    "https://www.googleapis.com/auth/photoslibrary.sharing"
]


def load_user_config() -> dict:
    """
    Load the user's sync_config.json (days, albums).
    Fallback to defaults if not found.
    """
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    else:
        print(f"Config file '{CONFIG_FILE}' not found. Using defaults.")
        return {
            "days": 90,
            "albums": []
        }
