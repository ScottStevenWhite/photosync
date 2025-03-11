import os
import json
import pickle
import requests
import datetime
from datetime import timezone
from pathlib import Path
from typing import Dict, Set

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow

# === PATH CONFIGURATION ===

DATA_DIR = Path("data")
LOCAL_PHOTOS_DIR = Path("/Users/scwhite/Pictures")  # Base pictures folder

# We'll store the user config in a JSON file, e.g. "sync_config.json"
CONFIG_FILE = Path("sync_config.json")  # user can edit "days" and "albums"

# Scopes for Google Photos read/write
SCOPES = [
    "https://www.googleapis.com/auth/photoslibrary",
    "https://www.googleapis.com/auth/photoslibrary.sharing"
]

class PhotoSync:
    """
    Syncs:
      - "Last N Days" of Google Photos to /Users/scwhite/Pictures
      - Specific albums (from config) to subfolders
    Also handles:
      - Uploading local photos to Google Photos
      - Automatic cleanup of local files no longer needed
    """

    def __init__(self):
        # Credentials & local DB
        self.credentials_json = DATA_DIR / "credentials.json"
        self.token_file = DATA_DIR / "token.json"
        self.photos_map_file = DATA_DIR / "photos_map.json"

        self.creds = None

        # mediaItem.id -> relative path (str)
        self.photos_map: Dict[str, str] = self._load_photos_map()

        # Load user config from JSON
        self.config = self._load_user_config()

    def _load_user_config(self) -> dict:
        """
        Loads sync settings from CONFIG_FILE, e.g.:
        {
          "days": 90,
          "albums": ["Wedding Quick Recap", "Lexi Weekend"]
        }
        """
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        else:
            # If not found, return defaults or raise an error
            print(f"Config file '{CONFIG_FILE}' not found. Using defaults.")
            return {
                "days": 90,
                "albums": []
            }

    def _load_photos_map(self) -> Dict[str, str]:
        if self.photos_map_file.exists():
            with open(self.photos_map_file, "r") as f:
                return json.load(f)
        return {}

    def _save_photos_map(self):
        with open(self.photos_map_file, "w") as f:
            json.dump(self.photos_map, f, indent=2)

    def authenticate(self):
        """
        Performs OAuth via InstalledAppFlow, storing & refreshing token as needed.
        """
        if self.token_file.exists():
            with open(self.token_file, "rb") as token:
                try:
                    self.creds = pickle.load(token)
                except:
                    print("Token file corrupt. Re-authenticating.")
                    self.token_file.unlink()
                    self.creds = None

        if not self.creds or not self.creds.valid:
            if self.creds and self.creds.expired and self.creds.refresh_token:
                self.creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self.credentials_json),
                    SCOPES
                )
                self.creds = flow.run_local_server(port=0)
            with open(self.token_file, "wb") as token:
                pickle.dump(self.creds, token)

    def _get_headers(self):
        """
        Ensure token is fresh before using it in any request.
        """
        if not self.creds or not self.creds.valid:
            self.creds.refresh(Request())
        return {
            "Authorization": f"Bearer {self.creds.token}",
            "Content-Type": "application/json"
        }

    # ======================================
    #  GATHER LAST N DAYS
    # ======================================

    def gather_last_n_days(self) -> Set[str]:
        """
        1) Search for last N days of Google Photos (N from config).
        2) Download missing items.
        3) Return set of mediaItem IDs that should be kept locally.
        """
        n_days = self.config.get("days", 90)
        print(f"\nGathering LAST {n_days} DAYS of Photos...")
        end = datetime.datetime.now(timezone.utc)
        start = end - datetime.timedelta(days=n_days)

        date_filter = {
            "ranges": [{
                "startDate": {
                    "year": start.year,
                    "month": start.month,
                    "day": start.day
                },
                "endDate": {
                    "year": end.year,
                    "month": end.month,
                    "day": end.day
                }
            }]
        }
        body = {
            "pageSize": 100,
            "filters": {
                "dateFilter": date_filter
            }
        }

        keep_ids = set()
        self._search_and_download(body, keep_ids)
        return keep_ids

    def _search_and_download(self, body: dict, keep_ids: Set[str]):
        url = "https://photoslibrary.googleapis.com/v1/mediaItems:search"
        headers = self._get_headers()
        next_page_token = None

        while True:
            if next_page_token:
                body["pageToken"] = next_page_token

            resp = requests.post(url, headers=headers, json=body)
            if resp.status_code != 200:
                print("Error searching photos:", resp.status_code, resp.text)
                return

            data = resp.json()
            media_items = data.get("mediaItems", [])
            for item in media_items:
                media_id = item["id"]
                keep_ids.add(media_id)
                self._download_item(item)

            next_page_token = data.get("nextPageToken")
            if not next_page_token:
                break

    def _download_item(self, item: dict):
        """
        Downloads a single media item if not already in photos_map.
        """
        media_id = item["id"]
        if media_id in self.photos_map:
            return  # already downloaded

        filename = item["filename"]
        base_url = item["baseUrl"]
        creation_time_str = item.get("mediaMetadata", {}).get("creationTime")

        resp = requests.get(base_url + "=d")
        if resp.status_code != 200:
            print(f"Failed to download {filename}: {resp.status_code}")
            return

        local_path = LOCAL_PHOTOS_DIR / filename
        local_path = self._unique_filename(local_path)

        with open(local_path, "wb") as f:
            f.write(resp.content)

        # set OS mod time
        if creation_time_str:
            try:
                dt = datetime.datetime.fromisoformat(creation_time_str.replace("Z", "+00:00"))
                ts = dt.timestamp()
                os.utime(local_path, (ts, ts))
            except ValueError:
                pass

        # record in photos_map
        self.photos_map[media_id] = local_path.name
        self._save_photos_map()
        print(f"Downloaded: {local_path}")

    # ======================================
    #  ALBUM SYNC
    # ======================================

    def gather_album(self, album_title: str) -> Set[str]:
        """
        1) Ensure local subfolder named album_title
        2) Download album's missing photos
        3) Upload new local photos
        4) Returns set of album media IDs
        """
        print(f"\n=== Syncing Album: {album_title} ===")
        album_id = self._get_album_id_by_title(album_title)
        if not album_id:
            print(f"Album '{album_title}' not found in Google Photos.")
            return set()

        local_album_dir = LOCAL_PHOTOS_DIR / album_title
        local_album_dir.mkdir(exist_ok=True)

        album_media_ids = self._download_album(album_id, local_album_dir)
        self._upload_album_photos(album_id, local_album_dir)

        return album_media_ids

    def _get_album_id_by_title(self, title: str) -> str:
        url = "https://photoslibrary.googleapis.com/v1/albums"
        headers = self._get_headers()
        page_token = None

        while True:
            params = {"pageSize": 50}
            if page_token:
                params["pageToken"] = page_token
            resp = requests.get(url, headers=headers, params=params)
            if resp.status_code != 200:
                print("Error listing albums:", resp.status_code, resp.text)
                return None

            data = resp.json()
            albums = data.get("albums", [])
            for album in albums:
                if album.get("title") == title:
                    return album["id"]

            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return None

    def _download_album(self, album_id: str, local_album_dir: Path) -> Set[str]:
        """
        Download missing items from the album, return set of media IDs
        """
        print(f"Downloading photos for album '{album_id}' -> {local_album_dir}")
        url = "https://photoslibrary.googleapis.com/v1/mediaItems:search"
        headers = self._get_headers()
        body = {
            "albumId": album_id,
            "pageSize": 100
        }

        album_media_ids = set()
        next_page_token = None

        while True:
            if next_page_token:
                body["pageToken"] = next_page_token
            resp = requests.post(url, headers=headers, json=body)
            if resp.status_code != 200:
                print("Error searching album media:", resp.status_code, resp.text)
                break

            data = resp.json()
            media_items = data.get("mediaItems", [])
            for item in media_items:
                media_id = item["id"]
                album_media_ids.add(media_id)
                self._download_item_to_folder(item, local_album_dir)

            next_page_token = data.get("nextPageToken")
            if not next_page_token:
                break

        return album_media_ids

    def _download_item_to_folder(self, item: dict, folder: Path):
        """
        Download item if not already in photos_map
        """
        media_id = item["id"]
        if media_id in self.photos_map:
            return

        filename = item["filename"]
        base_url = item["baseUrl"]
        creation_time_str = item.get("mediaMetadata", {}).get("creationTime")

        r = requests.get(base_url + "=d")
        if r.status_code != 200:
            print(f"Failed to download {filename}: {r.status_code}")
            return

        local_path = folder / filename
        local_path = self._unique_filename(local_path)

        with open(local_path, "wb") as f:
            f.write(r.content)

        if creation_time_str:
            try:
                dt = datetime.datetime.fromisoformat(creation_time_str.replace("Z", "+00:00"))
                ts = dt.timestamp()
                os.utime(local_path, (ts, ts))
            except ValueError:
                pass

        rel_path = local_path.relative_to(LOCAL_PHOTOS_DIR)
        self.photos_map[media_id] = str(rel_path)
        self._save_photos_map()
        print(f"Downloaded: {local_path}")

    def _upload_album_photos(self, album_id: str, local_album_dir: Path):
        """
        Upload new local photos from local_album_dir to the album
        """
        print(f"Uploading new photos from {local_album_dir} -> album {album_id}")
        for item in local_album_dir.iterdir():
            if item.is_file() and item.suffix.lower() in [".jpg", ".jpeg", ".png", ".gif", ".heic", ".heif", ".raw", ".mp4", ".mov"]:
                if not self._filename_in_photos_map(item):
                    media_id = self._upload_file(item)
                    if media_id:
                        self._add_media_item_to_album(album_id, media_id)

    def _filename_in_photos_map(self, file_path: Path) -> bool:
        rel_path = file_path.relative_to(LOCAL_PHOTOS_DIR)
        return any(stored_path == str(rel_path) for stored_path in self.photos_map.values())

    def _upload_file(self, file_path: Path) -> str:
        print(f"Uploading {file_path}...")
        upload_url = "https://photoslibrary.googleapis.com/v1/uploads"
        headers = {
            "Authorization": f"Bearer {self.creds.token}",
            "Content-type": "application/octet-stream",
            "X-Goog-Upload-File-Name": file_path.name,
            "X-Goog-Upload-Protocol": "raw"
        }
        with open(file_path, "rb") as f:
            resp = requests.post(upload_url, headers=headers, data=f.read())
        if resp.status_code != 200 or not resp.text:
            print(f"Upload failed: {resp.status_code} {resp.text}")
            return None

        upload_token = resp.text

        create_url = "https://photoslibrary.googleapis.com/v1/mediaItems:batchCreate"
        create_body = {
            "newMediaItems": [
                {
                    "description": "Uploaded via PhotoSync",
                    "simpleMediaItem": {
                        "uploadToken": upload_token
                    }
                }
            ]
        }
        resp2 = requests.post(create_url, headers=self._get_headers(), json=create_body)
        if resp2.status_code != 200:
            print("batchCreate failed:", resp2.status_code, resp2.text)
            return None

        result_data = resp2.json()
        new_items = result_data.get("newMediaItemResults", [])
        if not new_items:
            print("No media items created.")
            return None

        new_item = new_items[0]
        status_code = new_item.get("status", {}).get("code", -1)
        message = new_item.get("status", {}).get("message", "Unknown")

        if status_code == 0:
            media_id = new_item["mediaItem"]["id"]
            rel_path = file_path.relative_to(LOCAL_PHOTOS_DIR)
            self.photos_map[media_id] = str(rel_path)
            self._save_photos_map()
            print(f"Uploaded {file_path}, media_id={media_id}")
            return media_id
        else:
            print(f"Upload error: {message}")
            return None

    def _add_media_item_to_album(self, album_id: str, media_id: str):
        url = f"https://photoslibrary.googleapis.com/v1/albums/{album_id}:batchAddMediaItems"
        headers = self._get_headers()
        body = {"mediaItemIds": [media_id]}
        resp = requests.post(url, headers=headers, json=body)
        if resp.status_code != 200:
            print(f"Error adding {media_id} to album {album_id}: {resp.status_code} {resp.text}")
        else:
            print(f"Added media {media_id} to album {album_id}")

    # ======================================
    #  CLEANUP
    # ======================================

    def cleanup_local(self, keep_ids: Set[str]):
        """
        Removes any local file that doesn't belong to a mediaItem in keep_ids.
        Also removes that file from photos_map.
        """
        print("\nCleaning up local files no longer in 'keep_ids'...")

        to_remove = [m_id for m_id in self.photos_map if m_id not in keep_ids]

        for m_id in to_remove:
            local_rel = self.photos_map[m_id]
            local_path = LOCAL_PHOTOS_DIR / local_rel

            if local_path.exists():
                try:
                    local_path.unlink()
                    print(f"Deleted local file: {local_path}")
                except Exception as e:
                    print(f"Error deleting {local_path}: {e}")

            del self.photos_map[m_id]

        if to_remove:
            self._save_photos_map()
        print("Cleanup done.")

    # ======================================
    #  UTIL
    # ======================================

    def _unique_filename(self, path: Path) -> Path:
        """
        If path already exists, append (1), (2), etc. to avoid overwriting.
        """
        if not path.exists():
            return path
        base = path.stem
        ext = path.suffix
        counter = 1
        while True:
            new_name = f"{base}({counter}){ext}"
            new_path = path.with_name(new_name)
            if not new_path.exists():
                return new_path
            counter += 1

def main():
    syncer = PhotoSync()
    syncer.authenticate()

    # 1) Gather the last N days (N from sync_config.json)
    last_n_days_ids = syncer.gather_last_n_days()

    # 2) For each album in config, gather and union their IDs
    albums = syncer.config.get("albums", [])
    album_ids_sets = []
    for album_title in albums:
        album_ids_sets.append(syncer.gather_album(album_title))

    # Union all sets of album IDs + last N days
    keep_ids = last_n_days_ids
    for s in album_ids_sets:
        keep_ids = keep_ids.union(s)

    # Cleanup anything not in keep_ids
    syncer.cleanup_local(keep_ids)

    print("\nAll sync and cleanup operations complete!")

if __name__ == "__main__":
    main()
