import requests
from typing import Dict, Set
import datetime

from photosync.config import SCOPES


def get_headers(creds):
    """
    Return headers for authorized requests to Google Photos.
    """
    if not creds.valid:
        from google.auth.transport.requests import Request
        creds.refresh(Request())
    return {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json"
    }


def search_media_items(creds, body: dict):
    """
    Generic helper to call mediaItems:search with a given request body.
    Returns JSON response or None on error.
    """
    url = "https://photoslibrary.googleapis.com/v1/mediaItems:search"
    resp = requests.post(url, headers=get_headers(creds), json=body)
    if resp.status_code != 200:
        print("Error searching media items:", resp.status_code, resp.text)
        return None
    return resp.json()


def get_media_item(creds, media_id: str):
    """
    Retrieve a single media item by ID.
    Returns dict or None.
    """
    url = f"https://photoslibrary.googleapis.com/v1/mediaItems/{media_id}"
    resp = requests.get(url, headers=get_headers(creds))
    if resp.status_code != 200:
        print(f"Cannot get media item {media_id}.")
        return None
    return resp.json()


def list_albums(creds):
    """
    List all albums (paginated). Returns a list of album dicts.
    """
    url = "https://photoslibrary.googleapis.com/v1/albums"
    headers = get_headers(creds)
    albums = []
    page_token = None

    while True:
        params = {"pageSize": 50}
        if page_token:
            params["pageToken"] = page_token

        resp = requests.get(url, headers=headers, params=params)
        if resp.status_code != 200:
            print(f"Error listing albums: {resp.status_code} {resp.text}")
            break

        data = resp.json()
        new_albums = data.get("albums", [])
        albums.extend(new_albums)

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return albums


def upload_file_to_photos(creds, file_path):
    """
    Upload a local file (raw bytes) to Google Photos, then call batchCreate.
    Return the new mediaItem ID or None on error.
    """
    # 1) Upload raw bytes
    upload_url = "https://photoslibrary.googleapis.com/v1/uploads"
    headers = {
        "Authorization": f"Bearer {creds.token}",
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

    # 2) batchCreate
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
    resp2 = requests.post(create_url, headers=get_headers(creds), json=create_body)
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
        return media_id
    else:
        print(f"Upload error: {message}")
        return None


def add_media_item_to_album(creds, album_id, media_id):
    """
    batchAddMediaItems to add media_id into album_id.
    """
    url = f"https://photoslibrary.googleapis.com/v1/albums/{album_id}:batchAddMediaItems"
    headers = get_headers(creds)
    body = {"mediaItemIds": [media_id]}
    resp = requests.post(url, headers=headers, json=body)
    if resp.status_code != 200:
        print(f"Error adding {media_id} to album {album_id}: {resp.status_code} {resp.text}")
    else:
        print(f"Added media {media_id} to album {album_id}")
