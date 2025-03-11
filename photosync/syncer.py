import os
import requests
import datetime
from pathlib import Path
from typing import Dict, Set
from datetime import timezone

from photosync.auth import AuthManager
from photosync.config import load_user_config, LOCAL_PHOTOS_DIR
from photosync.local_store import (
    load_photos_map,
    save_photos_map,
    compute_local_path,
    delete_local_file,
    move_local_file,
    unique_filename,
)
from photosync import google_photos_api as gapi


class PhotoSync:
    """
    Main class orchestrating the photo sync logic:
     - last N days
     - starred
     - albums
     - single local copy
     - cleanup
    """

    def __init__(self):
        # Build the AuthManager
        self.auth_manager = AuthManager()
        self.creds = None

        # Load or create our local photos map
        self.photos_map: Dict[str, dict] = load_photos_map()

        # Load user config (days + album list)
        self.config = load_user_config()

        # Cache album title -> album ID
        self.album_title_to_id: Dict[str, str] = {}

    def authenticate(self):
        self.creds = self.auth_manager.authenticate()

    # -----------------------------
    # 1) STARS, LAST N DAYS, ALBUM
    # -----------------------------

    def recheck_inLastNDays_for_existing(self):
        """
        For every photo in photos_map, check if it's older than 'days' cutoff,
        and set inLastNDays accordingly.
        """
        n_days = self.config.get("days", 90)
        cutoff = datetime.datetime.now(timezone.utc) - datetime.timedelta(days=n_days)
        changed = False

        for mid, rec in self.photos_map.items():
            ctime_str = rec.get("creationTime")
            if not ctime_str:
                # If unknown creationTime, skip or assume older
                continue

            dt = datetime.datetime.fromisoformat(ctime_str.replace("Z", "+00:00"))
            was_in = rec.get("inLastNDays", False)
            now_in = (dt >= cutoff)
            if was_in != now_in:
                rec["inLastNDays"] = now_in
                changed = True

        if changed:
            save_photos_map(self.photos_map)

    def gather_is_starred(self) -> Set[str]:
        """
        Fetch the current list of starred mediaItems from Google Photos.
        Update photos_map for items that are starred, and unset for those unstarred.
        """
        print("\nGathering STARRED (Favorite) photos from Google Photos...")

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

            data = gapi.search_media_items(self.creds, body)
            if not data:
                break

            items = data.get("mediaItems", [])
            for item in items:
                mid = item["id"]
                starred_ids.add(mid)
                self._update_photos_map_entry(item, is_starred=True)

            next_page_token = data.get("nextPageToken")
            if not next_page_token:
                break

        # Negative check: un-star items no longer in starred_ids
        changed = False
        for mid, rec in self.photos_map.items():
            if rec.get("isStarred") and mid not in starred_ids:
                rec["isStarred"] = False
                changed = True

        if changed:
            save_photos_map(self.photos_map)

        return starred_ids

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
        4) Remove that album from any items that are no longer in it
        """
        album_id = self._get_album_id_by_title(album_title)
        if not album_id:
            print(f"Album '{album_title}' not found in Google Photos.")
            return set()

        self.album_title_to_id[album_title] = album_id

        print(f"Gathering items from album '{album_title}' (ID={album_id})...")
        album_item_ids = set()

        body = {
            "albumId": album_id,
            "pageSize": 100
        }
        next_page_token = None

        while True:
            if next_page_token:
                body["pageToken"] = next_page_token

            data = gapi.search_media_items(self.creds, body)
            if not data:
                break

            items = data.get("mediaItems", [])
            for item in items:
                mid = item["id"]
                album_item_ids.add(mid)
                self._update_photos_map_entry(item, album_title=album_title)

            next_page_token = data.get("nextPageToken")
            if not next_page_token:
                break

        # Negative check: remove album membership for items not in album_item_ids
        changed = False
        for mid, rec in self.photos_map.items():
            if album_title in rec["albums"] and mid not in album_item_ids:
                rec["albums"].remove(album_title)
                changed = True

        if changed:
            save_photos_map(self.photos_map)

        return album_item_ids

    # -----------------------------
    # 2) INTERNAL HELPERS
    # -----------------------------

    def _search_and_tag(self, body: dict, keep_ids: Set[str], in_last_n_days=False):
        """
        Generic search that tags items as inLastNDays if in_last_n_days=True.
        """
        next_page_token = None

        while True:
            if next_page_token:
                body["pageToken"] = next_page_token

            data = gapi.search_media_items(self.creds, body)
            if not data:
                return

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
        Return album ID or None. Caches results in self.album_title_to_id.
        """
        if title in self.album_title_to_id:
            return self.album_title_to_id[title]

        all_albums = gapi.list_albums(self.creds)
        for alb in all_albums:
            if alb.get("title") == title:
                self.album_title_to_id[title] = alb["id"]
                return alb["id"]
        return None

    def _update_photos_map_entry(
        self,
        item: dict,
        is_starred: bool = False,
        in_last_n_days: bool = False,
        album_title: str = None
    ):
        """
        Updates or creates a photos_map entry based on new info.
        """
        mid = item["id"]
        filename = item["filename"]
        if mid not in self.photos_map:
            self.photos_map[mid] = {
                "filename": filename,
                "localFolder": "",
                "isStarred": False,
                "inLastNDays": False,
                "albums": [],
                "creationTime": item.get("mediaMetadata", {}).get("creationTime", None)
            }

        entry = self.photos_map[mid]
        # Keep the filename up to date
        entry["filename"] = filename
        # Set starred
        if is_starred:
            entry["isStarred"] = True
        # Set last N days
        if in_last_n_days:
            entry["inLastNDays"] = True
        # Add album
        if album_title and album_title not in entry["albums"]:
            entry["albums"].append(album_title)

        # Save if any changes
        save_photos_map(self.photos_map)

    # -----------------------------
    # 3) RECONCILE LOCAL vs. GOOGLE
    # -----------------------------

    def reconcile_local_changes(self):
        """
        Download missing, upload new local, move or rename local files, etc.
        """
        # Step 1: Download any that we haven't downloaded yet
        for mid, rec in self.photos_map.items():
            need_local_copy = (rec["isStarred"] or rec["inLastNDays"] or len(rec["albums"]) > 0)
            if need_local_copy:
                local_path = compute_local_path(rec)
                if not local_path.exists():
                    self._download_if_needed(mid, rec)

        # Step 2: Upload new local files
        self._upload_local_new_files()

        # Step 3: Move local files if localFolder changed
        self._sync_local_file_paths()

    def _download_if_needed(self, mid: str, rec: dict):
        """
        Fetch the photo bits from Google Photos if not present locally.
        """
        data = gapi.get_media_item(self.creds, mid)
        if not data:
            return  # can't find item in Google Photos

        base_url = data.get("baseUrl")
        if not base_url:
            print(f"No baseUrl for item {mid}, can't download.")
            return

        download_url = base_url + "=d"
        r = requests.get(download_url)
        if r.status_code != 200:
            print(f"Download failed for {mid}: {r.status_code}")
            return

        local_path = compute_local_path(rec)
        if not local_path.parent.exists():
            local_path.parent.mkdir(parents=True, exist_ok=True)

        # If naming conflict, pick a unique name
        if local_path.exists():
            local_path = unique_filename(local_path)

        with open(local_path, "wb") as f:
            f.write(r.content)

        # Attempt to set OS mod time
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
        For any file not tracked in photos_map, upload it to Google Photos
        and set correct album membership if it's in a subfolder.
        """
        known_local_paths = set()
        for mid, rec in self.photos_map.items():
            known_local_paths.add(compute_local_path(rec).resolve())

        for root, dirs, files in os.walk(LOCAL_PHOTOS_DIR):
            for fname in files:
                file_path = Path(root) / fname
                if file_path.suffix.lower() in [".jpg", ".jpeg", ".png", ".gif",
                                                ".heic", ".heif", ".raw",
                                                ".mp4", ".mov"]:
                    if file_path.resolve() not in known_local_paths:
                        self._handle_new_local_file(file_path)

        # Ensure album membership in Google Photos matches localFolder
        self._ensure_album_membership()

    def _handle_new_local_file(self, file_path: Path):
        """
        Upload the new local file, add to photos_map, and set album if folder matches one.
        """
        print(f"New local file found, attempting upload: {file_path}")
        media_id = gapi.upload_file_to_photos(self.creds, file_path)
        if not media_id:
            return

        # Determine localFolder from path
        rel_path = file_path.relative_to(LOCAL_PHOTOS_DIR)
        parts = rel_path.parts
        local_folder = parts[0] if len(parts) > 1 else ""

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

        # If local_folder is an album we know, add the item
        if local_folder and local_folder in self.album_title_to_id:
            album_id = self.album_title_to_id[local_folder]
            gapi.add_media_item_to_album(self.creds, album_id, media_id)
            if local_folder not in self.photos_map[media_id]["albums"]:
                self.photos_map[media_id]["albums"].append(local_folder)

        save_photos_map(self.photos_map)

    def _ensure_album_membership(self):
        """
        For each photo in photos_map, if localFolder is an album folder,
        ensure it's in that album in Google Photos.
        """
        changed = False
        for mid, rec in self.photos_map.items():
            folder = rec.get("localFolder", "")
            if folder and folder in self.album_title_to_id:
                if folder not in rec["albums"]:
                    album_id = self.album_title_to_id[folder]
                    print(f"Ensuring item {mid} is in album '{folder}'...")
                    gapi.add_media_item_to_album(self.creds, album_id, mid)
                    rec["albums"].append(folder)
                    changed = True

        if changed:
            save_photos_map(self.photos_map)

    def _sync_local_file_paths(self):
        """
        Move or rename local files if localFolder changed.
        The actual logic is: pick the correct local folder, then compare with stored.
        """
        changed = False
        for mid, rec in list(self.photos_map.items()):
            old_folder = rec["localFolder"]
            new_folder = self._choose_local_folder(rec)

            if new_folder != old_folder:
                old_path = compute_local_path({"localFolder": old_folder, "filename": rec["filename"]})
                new_path = compute_local_path({"localFolder": new_folder, "filename": rec["filename"]})
                move_local_file(old_path, new_path)
                rec["localFolder"] = new_folder
                changed = True

        if changed:
            save_photos_map(self.photos_map)

    def _choose_local_folder(self, rec: dict) -> str:
        """
        Decide which folder a photo belongs in:
         1) If rec["albums"] is non-empty, pick the first album name in alphabetical order
         2) else if isStarred==True or inLastNDays==True => store at top-level ""
         3) else => no local copy => leads to removal in cleanup.
        """
        if rec["albums"]:
            sorted_albums = sorted(rec["albums"])
            return sorted_albums[0]
        elif rec["isStarred"] or rec["inLastNDays"]:
            return ""
        else:
            return ""

    # -----------------------------
    # 4) CLEANUP
    # -----------------------------

    def cleanup_local(self):
        """
        Remove local files no longer in (starred, in last N days, or any album).
        """
        print("\nCleaning up local files no longer needed...")

        to_remove = []
        for mid, rec in self.photos_map.items():
            keep = (rec["isStarred"] or rec["inLastNDays"] or len(rec["albums"]) > 0)
            if not keep:
                to_remove.append(mid)

        if not to_remove:
            print("No files to remove.")
            return

        for mid in to_remove:
            entry = self.photos_map[mid]
            local_path = compute_local_path(entry)
            delete_local_file(local_path)
            del self.photos_map[mid]

        save_photos_map(self.photos_map)
        print("Cleanup done.")
