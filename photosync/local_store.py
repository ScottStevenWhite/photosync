import json
import os
import datetime
from pathlib import Path
from typing import Dict

from photosync.config import DATA_DIR, LOCAL_PHOTOS_DIR


PHOTOS_MAP_FILE = DATA_DIR / "photos_map.json"


def load_photos_map() -> Dict[str, dict]:
    """
    Load photos_map.json into a dictionary. Return empty if file doesn't exist.
    """
    if PHOTOS_MAP_FILE.exists():
        with open(PHOTOS_MAP_FILE, "r") as f:
            return json.load(f)
    return {}


def save_photos_map(photos_map: Dict[str, dict]):
    """
    Write the in-memory photos_map to photos_map.json.
    """
    with open(PHOTOS_MAP_FILE, "w") as f:
        json.dump(photos_map, f, indent=2)


def delete_local_file(path: Path):
    """
    Safely delete a local file if it exists.
    """
    if path.exists():
        try:
            path.unlink()
            print(f"Deleted local file: {path}")
        except Exception as e:
            print(f"Error deleting {path}: {e}")


def unique_filename(path: Path) -> Path:
    """
    If 'path' already exists, append (1), (2), etc. until we find a free name.
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


def compute_local_path(rec: dict) -> Path:
    """
    Given a photos_map record, figure out the actual path in the file system.
    localFolder="" => top-level in LOCAL_PHOTOS_DIR
    """
    folder_name = rec.get("localFolder", "")
    filename = rec.get("filename", "unknown.jpg")

    if folder_name:
        return LOCAL_PHOTOS_DIR / folder_name / filename
    else:
        return LOCAL_PHOTOS_DIR / filename


def move_local_file(old_path: Path, new_path: Path):
    """
    Move or rename a local file from old_path to new_path, ensuring no conflicts.
    """
    if not old_path.exists():
        return

    if not new_path.parent.exists():
        new_path.parent.mkdir(parents=True, exist_ok=True)

    if new_path.exists():
        new_path = unique_filename(new_path)

    print(f"Moving file from {old_path} to {new_path}...")
    old_path.rename(new_path)
