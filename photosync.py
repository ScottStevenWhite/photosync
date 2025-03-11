import os
import json
import pickle
import requests
import datetime
from datetime import timezone
from pathlib import Path
from typing import Dict, Set, List

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow

# === PATH CONFIGURATION ===

DATA_DIR = Path("data")
LOCAL_PHOTOS_DIR = Path("/Users/scwhite/Pictures")  # Base pictures folder

CONFIG_FILE = Path("sync_config.json")  # user can edit "days" and "albums"

SCOPES = [
    "https://www.googleapis.com/auth/photoslibrary",
    "https://www.googleapis.com/auth/photoslibrary.sharing"
]

################################################################
# DATA STRUCTURE in photos_map.json:
# {
#   "mediaItemId": {
#     "filename": "IMG_1234.HEIC",
#     "localFolder": "Wedding Quick Recap" OR "",
#     "isStarred": true/false,
#     "inLastNDays": true/false,
#     "albums": ["Wedding Quick Recap", ...] // album titles or IDs
#   },
#   ...
# }
################################################################

class PhotoSync:
    """
    Extends the original photo sync logic to handle:
     - 'inLastNDays'
     - 'isStarred'
     - multiple album membership
     - Single local copy, moving files when states change
    """

    def __init__(self):
        # Credentials & local DB
        self.credentials_json = DATA_DIR / "credentials.json"
        self.token_file = DATA_DIR / "token.json"
        self.photos_map_file = DATA_DIR / "photos_map.json"

        self.creds = None

        # { mediaItemId: {...} }
        self.photos_map: Dict[str, Dict] = self._load_photos_map()

        # Load user config (days + album list)
        self.config = self._load_user_config()

        # We'll collect known album IDs here to avoid repeated lookups
        self.album_title_to_id: Dict[str, str] = {}

    def _load_photos_map(self) -> Dict[str, Dict]:
        if self.photos_map_file.exists():
            with open(self.photos_map_file, "r") as f:
                return json.load(f)
        return {}

    def _save_photos_map(self):
        with open(self.photos_map_file, "w") as f:
            json.dump(self.photos_map, f, indent=2)

    def _load_user_config(self) -> dict:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        else:
            print(f"Config file '{CONFIG_FILE}' not found. Using defaults.")
            return {
                "days": 90,
                "albums": []
            }

    def authenticate(self):
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
        if not self.creds or not self.creds.valid:
            self.creds.refresh(Request())
        return {
            "Authorization": f"Bearer {self.creds.token}",
            "Content-Type": "application/json"
        }

    ################################################################
    # Part 1: Gathering "Starred" + "Last N Days" + Album Items
    ################################################################

    def gather_is_starred(self) -> Set[str]:
        """
        Use featureFilter to find FAVORITES (starred) images.
        If your environment doesn't support this filter,
        you might need a custom approach (like a "Starred" album).
        """
        print("\nGathering STARRED (Favorite) photos from Google Photos...")
        url = "https://photoslibrary.googleapis.com/v1/mediaItems:search"
        headers = self._get_headers()
        body = {
            "pageSize": 100,
            "filters": {
                "featureFilter": {
                    "includedFeatures": ["FAVORITES"]
                }
            }
        }
        starred_ids = set()
        next_page_token = None

        while True:
            if next_page_token:
                body["pageToken"] = next_page_token

            resp = requests.post(url, headers=headers, json=body)
            if resp.status_code != 200:
                print("Error searching for starred photos:", resp.status_code, resp.text)
                return starred_ids

            data = resp.json()
            items = data.get("mediaItems", [])
            for item in items:
                starred_ids.add(item["id"])
                self._update_photos_map_entry(item, is_starred=True)

            next_page_token = data.get("nextPageToken")
            if not next_page_token:
                break

        return starred_ids
    
    def recheck_inLastNDays_for_existing(self):
        n_days = self.config.get("days", 90)
        cutoff = datetime.datetime.now(timezone.utc) - datetime.timedelta(days=n_days)

        for mid, rec in self.photos_map.items():
            # If we haven't stored creationTime, we can fetch it from Google Photos
            # or store it the first time we see the item.
            # For demonstration, assume we stored it in rec["creationTime"] as ISO string
            ctime_str = rec.get("creationTime")
            if not ctime_str:
                # We might do a fetch to fill it in:
                # item_data = self._fetch_item(mid) ...
                continue

            dt = datetime.datetime.fromisoformat(ctime_str.replace("Z", "+00:00"))
            if dt < cutoff:
                rec["inLastNDays"] = False
            else:
                rec["inLastNDays"] = True

        self._save_photos_map()

    def gather_last_n_days(self) -> Set[str]:
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
        self._search_and_tag(body, keep_ids, in_last_n_days=True)
        return keep_ids

    def gather_album(self, album_title: str) -> Set[str]:
        """
        1) Lookup album ID
        2) Fetch all media
        3) Mark each item as belonging to that album
        4) Return set of item IDs
        """
        album_id = self._get_album_id_by_title(album_title)
        if not album_id:
            print(f"Album '{album_title}' not found in Google Photos.")
            return set()

        # We'll store album IDs so we can add media items properly later
        self.album_title_to_id[album_title] = album_id

        print(f"Gathering items from album '{album_title}' (ID={album_id})...")
        url = "https://photoslibrary.googleapis.com/v1/mediaItems:search"
        headers = self._get_headers()

        album_item_ids = set()
        body = {
            "albumId": album_id,
            "pageSize": 100
        }

        next_page_token = None
        while True:
            if next_page_token:
                body["pageToken"] = next_page_token

            resp = requests.post(url, headers=headers, json=body)
            if resp.status_code != 200:
                print("Error searching album media:", resp.status_code, resp.text)
                break

            data = resp.json()
            items = data.get("mediaItems", [])
            for item in items:
                media_id = item["id"]
                album_item_ids.add(media_id)
                self._update_photos_map_entry(item, album_title=album_title)

            next_page_token = data.get("nextPageToken")
            if not next_page_token:
                break

        return album_item_ids

    ################################################################
    # Updating the local photos_map with new info from Google Photos
    ################################################################

    def _search_and_tag(self, body: dict, keep_ids: Set[str], in_last_n_days=False):
        """
        Generic search that tags items as inLastNDays if in_last_n_days=True.
        """
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
                mid = item["id"]
                keep_ids.add(mid)
                self._update_photos_map_entry(item, in_last_n_days=in_last_n_days)

            next_page_token = data.get("nextPageToken")
            if not next_page_token:
                break

    def _get_album_id_by_title(self, title: str) -> str:
        """
        Return album ID or None. Cache results in self.album_title_to_id.
        """
        if title in self.album_title_to_id:
            return self.album_title_to_id[title]

        url = "https://photoslibrary.googleapis.com/v1/albums"
        headers = self._get_headers()
        page_token = None

        while True:
            params = {"pageSize": 50}
            if page_token:
                params["pageToken"] = page_token

            resp = requests.get(url, headers=headers, params=params)
            if resp.status_code != 200:
                print(f"Error listing albums:", resp.status_code, resp.text)
                return None

            data = resp.json()
            albums = data.get("albums", [])
            for album in albums:
                if album.get("title") == title:
                    self.album_title_to_id[title] = album["id"]
                    return album["id"]

            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return None

    def _update_photos_map_entry(
        self,
        item: dict,
        is_starred: bool = False,
        in_last_n_days: bool = False,
        album_title: str = None
    ):
        """
        Updates an entry in photos_map based on new info from Google Photos.
        - item["id"]
        - item["filename"]
        - set isStarred, inLastNDays, and albums.
        We do NOT yet move local files. We'll do that in a later step.
        """
        mid = item["id"]
        filename = item["filename"]

        if mid not in self.photos_map:
            # Create a fresh record
            self.photos_map[mid] = {
                "filename": filename,
                "localFolder": "",
                "isStarred": False,
                "inLastNDays": False,
                "albums": []
            }

        entry = self.photos_map[mid]

        # If the filename changed for some reason, update it
        entry["filename"] = filename

        # Mark starred if needed
        if is_starred:
            entry["isStarred"] = True

        # Mark last n days if needed
        if in_last_n_days:
            entry["inLastNDays"] = True

        # Mark album if provided
        if album_title and (album_title not in entry["albums"]):
            entry["albums"].append(album_title)

    ################################################################
    # Part 2: Resolve local state (download, upload, move files, etc.)
    ################################################################

    def reconcile_local_changes(self):
        """
        For each item in photos_map:
          - Decide correct local folder
          - If not downloaded yet but needed, download
          - If local file doesn't match 'localFolder', move it
          - If local item is missing from Google Photos, upload it
          - If local item is in an album folder, ensure it's assigned in that album
        We'll do a final pass to remove anything that no longer meets any keep criteria.
        """

        # Step 1: Download any that we haven't downloaded yet, if we actually want them
        #         (We detect "we want them" if isStarred=True OR inLastNDays=True OR albums non-empty)
        for mid, rec in self.photos_map.items():
            need_local_copy = (rec["isStarred"] or rec["inLastNDays"] or len(rec["albums"]) > 0)
            local_path = self._compute_local_path(rec)

            if need_local_copy:
                # If file doesn't exist locally, attempt to download
                if not local_path.exists():
                    # We'll fetch the item from Google Photos
                    self._download_if_needed(mid, rec)
            else:
                # We explicitly don't want a local copy -> we'll delete later in cleanup
                pass

        # Step 2: Upload or add to albums any local files that are new or missing from the album
        self._upload_local_new_files()

        # Step 3: Move or rename local files if localFolder changed
        self._sync_local_file_paths()

    def _compute_local_path(self, rec: dict) -> Path:
        """
        Given a photos_map record, figure out the actual Path in the file system.
        localFolder="" means top-level /Users/scwhite/Pictures
        otherwise /Users/scwhite/Pictures/localFolder
        """
        folder_name = rec["localFolder"]
        if folder_name:
            return LOCAL_PHOTOS_DIR / folder_name / rec["filename"]
        else:
            return LOCAL_PHOTOS_DIR / rec["filename"]

    def _download_if_needed(self, mid: str, rec: dict):
        """
        Actually fetch the photo bits from Google Photos if not present locally.
        We'll do mediaItems.get or mediaItems.search if needed.
        """
        # If we haven't recorded the baseUrl yet, we may need to do a mediaItems.get call
        # for simplicity, let's do a quick search approach:
        url = f"https://photoslibrary.googleapis.com/v1/mediaItems/{mid}"
        resp = requests.get(url, headers=self._get_headers())
        if resp.status_code != 200:
            print(f"Cannot find item {mid} in Google Photos. Not downloading.")
            return

        data = resp.json()
        base_url = data.get("baseUrl")
        if not base_url:
            print(f"No baseUrl for item {mid}, can't download.")
            return

        download_url = base_url + "=d"
        r = requests.get(download_url)
        if r.status_code != 200:
            print(f"Download failed for {mid} : {r.status_code}")
            return

        local_path = self._compute_local_path(rec)
        local_folder = local_path.parent
        if not local_folder.exists():
            local_folder.mkdir(parents=True, exist_ok=True)

        # If there's a naming conflict, create a unique name
        if local_path.exists():
            local_path = self._unique_filename(local_path)

        with open(local_path, "wb") as f:
            f.write(r.content)

        # Attempt to set creationTime as OS mod time
        creation_time_str = data.get("mediaMetadata", {}).get("creationTime")
        if creation_time_str:
            try:
                dt = datetime.datetime.fromisoformat(creation_time_str.replace("Z", "+00:00"))
                ts = dt.timestamp()
                os.utime(local_path, (ts, ts))
            except ValueError:
                pass

        print(f"Downloaded item {mid} -> {local_path}")

    def _upload_local_new_files(self):
        """
        For each local file, if it doesn't exist in Google Photos, or it's missing from an album, fix it.
        1) If we see an item in the album folder but not in Google Photos at all, we create new media.
        2) If the item is in Google Photos but not in the correct album, we call batchAddMediaItems.
        """
        # We do a local scan of pictures folder. For each file, see if we find it in photos_map.
        # If not found in photos_map, we treat it as brand new => upload to Google Photos
        # Also we check if the folder indicates an album -> add to that album if not present.

        # a) Build a set of known local files from photos_map, so we can see if there's a stray file
        known_local_paths = set()
        for mid, rec in self.photos_map.items():
            local_path = self._compute_local_path(rec)
            known_local_paths.add(local_path.resolve())

        # b) Walk the top-level PICTURES folder and subfolders
        #    If we see a file that is not in photos_map, we upload it.
        for root, dirs, files in os.walk(LOCAL_PHOTOS_DIR):
            for fname in files:
                file_path = Path(root) / fname
                if file_path.suffix.lower() in [".jpg", ".jpeg", ".png", ".gif", ".heic", ".heif", ".raw", ".mp4", ".mov"]:
                    if file_path.resolve() not in known_local_paths:
                        # It's brand new local file => upload
                        self._handle_new_local_file(file_path)

        # c) If the local folder indicates an album, ensure that item is in that album in Photos
        #    We'll rely on the final step: _ensure_album_membership(...)
        #    to add the item. We'll do that in the next function or here.
        self._ensure_album_membership()

    def _handle_new_local_file(self, file_path: Path):
        """
        For a brand new local file not in photos_map at all, we:
          1) Upload it to Google Photos
          2) Add it to an album if the folder name matches one of our known albums
          3) Mark isStarred/inLastNDays as needed => by default false
        """
        print(f"New local file found, attempting upload: {file_path}")
        # Try upload
        media_id = self._upload_file(file_path)
        if not media_id:
            print("Upload failed or item not created.")
            return

        # Determine album from folder
        local_folder = ""
        rel_path = file_path.relative_to(LOCAL_PHOTOS_DIR)
        parts = rel_path.parts
        # parts[0] might be album folder if there's more than 1 part
        # e.g. Wedding Quick Recap/IMG_1234.HEIC => localFolder = "Wedding Quick Recap"
        if len(parts) > 1:
            local_folder = parts[0]

        # Update photos_map with new record if not created by _upload_file
        if media_id not in self.photos_map:
            self.photos_map[media_id] = {
                "filename": file_path.name,
                "localFolder": local_folder,
                "isStarred": False,
                "inLastNDays": False,
                "albums": []
            }
        else:
            self.photos_map[media_id]["localFolder"] = local_folder

        # If local_folder is one of our known albums, link it
        if local_folder:
            album_id = self.album_title_to_id.get(local_folder, None)
            if album_id:
                # add the media item to that album
                self._add_media_item_to_album(album_id, media_id)
                if local_folder not in self.photos_map[media_id]["albums"]:
                    self.photos_map[media_id]["albums"].append(local_folder)

        self._save_photos_map()

    def _ensure_album_membership(self):
        """
        For each item in photos_map, if localFolder is an album folder, ensure it belongs to that album in Google Photos.
        If it doesn't, call batchAddMediaItems.
        """
        for mid, rec in self.photos_map.items():
            folder = rec["localFolder"]
            if folder and folder in self.album_title_to_id:
                if folder not in rec["albums"]:
                    # We need to add it to that album
                    album_id = self.album_title_to_id[folder]
                    print(f"Ensuring item {mid} is in album {folder}...")
                    self._add_media_item_to_album(album_id, mid)
                    rec["albums"].append(folder)
        self._save_photos_map()

    def _sync_local_file_paths(self):
        """
        For each item in photos_map, compute the correct localFolder based on:
          - If rec['albums'] is non-empty, pick the first album name (or some logic) as localFolder
          - else if isStarred or inLastNDays => localFolder=""
          - else => we remove it in cleanup
        Then physically move the file if localFolder changed from old to new.
        """
        for mid, rec in self.photos_map.items():
            old_folder = rec["localFolder"]
            # Decide the new folder
            new_folder = self._choose_local_folder(rec)

            if new_folder != old_folder:
                self._move_local_file(rec, old_folder, new_folder)
                rec["localFolder"] = new_folder

        self._save_photos_map()

    def _choose_local_folder(self, rec: dict) -> str:
        """
        Decide which folder a photo belongs in:
         1) If rec["albums"] is non-empty, pick the FIRST album in alphabetical or
            some consistent order. (For simplicity, let's pick the first in alphabetical.)
         2) else if isStarred==True or inLastNDays==True, localFolder=""
         3) else it's not needed locally (will be removed in cleanup).
        """
        if rec["albums"]:
            sorted_albums = sorted(rec["albums"])
            return sorted_albums[0]  # pick the first album name
        elif rec["isStarred"] or rec["inLastNDays"]:
            return ""  # top-level
        else:
            # This case means we actually don't want it local, it will be removed in cleanup.
            return ""  # We'll rely on cleanup if no reason to keep

    def _move_local_file(self, rec: dict, old_folder: str, new_folder: str):
        """
        Physically move or rename the file on disk from old_folder to new_folder.
        If old_folder == new_folder, do nothing.
        If the file doesn't exist, do nothing.
        """
        filename = rec["filename"]
        old_path = (LOCAL_PHOTOS_DIR / old_folder / filename) if old_folder else (LOCAL_PHOTOS_DIR / filename)
        new_path = (LOCAL_PHOTOS_DIR / new_folder / filename) if new_folder else (LOCAL_PHOTOS_DIR / filename)

        if old_folder == new_folder:
            return

        if not old_path.exists():
            return

        if not new_path.parent.exists():
            new_path.parent.mkdir(parents=True, exist_ok=True)

        # If there's a naming conflict in new_path, pick a unique name
        if new_path.exists():
            new_path = self._unique_filename(new_path)

        print(f"Moving file from {old_path} to {new_path}...")
        old_path.rename(new_path)

    ################################################################
    # Part 3: Cleanup
    ################################################################

    def cleanup_local(self):
        """
        Remove local files that no longer meet ANY criteria:
          isStarred==False, inLastNDays==False, albums=[]
        We'll also remove them from photos_map.
        """
        print("\nCleaning up local files no longer needed...")

        to_remove = []
        for mid, rec in self.photos_map.items():
            keep = (rec["isStarred"] or rec["inLastNDays"] or len(rec["albums"]) > 0)
            if not keep:
                to_remove.append(mid)

        for mid in to_remove:
            entry = self.photos_map[mid]
            old_path = self._compute_local_path(entry)
            if old_path.exists():
                try:
                    old_path.unlink()
                    print(f"Deleted local file: {old_path}")
                except Exception as e:
                    print(f"Error deleting {old_path}: {e}")
            del self.photos_map[mid]

        if to_remove:
            self._save_photos_map()
        print("Cleanup done.")

    ################################################################
    # Upload logic (from original code, adapted for new structure)
    ################################################################

    def _upload_file(self, file_path: Path) -> str:
        """
        Upload a local file to Google Photos, return its mediaItem ID.
        """
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
            print(f"Uploaded {file_path}, media_id={media_id}")
            return media_id
        else:
            print(f"Upload error: {message}")
            return None

    def _add_media_item_to_album(self, album_id: str, media_id: str):
        """
        batchAddMediaItems to add media_id into album_id.
        """
        url = f"https://photoslibrary.googleapis.com/v1/albums/{album_id}:batchAddMediaItems"
        headers = self._get_headers()
        body = {"mediaItemIds": [media_id]}
        resp = requests.post(url, headers=headers, json=body)
        if resp.status_code != 200:
            print(f"Error adding {media_id} to album {album_id}: {resp.status_code} {resp.text}")
        else:
            print(f"Added media {media_id} to album {album_id}")

    ################################################################
    # Utility
    ################################################################

    def _unique_filename(self, path: Path) -> Path:
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

    # 1) Before we gather new items, re-check any existing photos to see if
    #    they've 'aged out' of the last N days.
    #    This will unset inLastNDays for items older than the cutoff date.
    syncer.recheck_inLastNDays_for_existing()

    # 2) Gather "starred" / favorites
    syncer.gather_is_starred()

    # 3) Gather photos within the last N days (which sets inLastNDays = True for them)
    syncer.gather_last_n_days()

    # 4) Gather each album from config, marking album membership in the photos_map
    for album_title in syncer.config.get("albums", []):
        syncer.gather_album(album_title)

    # 5) Reconcile local changes (download missing, upload new local, move files, etc.)
    syncer.reconcile_local_changes()

    # 6) Clean up anything that no longer meets the keep criteria
    syncer.cleanup_local()

    print("\nAll sync operations complete!")


if __name__ == "__main__":
    main()
