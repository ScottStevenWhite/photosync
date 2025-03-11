#!/usr/bin/env python3
"""
Entry point for the photo sync tool.
"""

from photosync.syncer import PhotoSync


def main():
    # Instantiate the PhotoSync orchestrator
    syncer = PhotoSync()

    # Authenticate with Google Photos
    syncer.authenticate()

    # 1) Re-check if photos have aged out of last N days
    syncer.recheck_inLastNDays_for_existing()

    # 2) Gather "starred"/favorites (and un-star any that no longer appear)
    syncer.gather_is_starred()

    # 3) Gather last N days
    syncer.gather_last_n_days()

    # 4) Gather configured albums (and remove items that are no longer in them)
    for album_title in syncer.config.get("albums", []):
        syncer.gather_album(album_title)

    # 5) Reconcile local changes (download, upload, move files, etc.)
    syncer.reconcile_local_changes()

    # 6) Clean up anything that no longer meets keep criteria
    syncer.cleanup_local()

    print("\nAll sync operations complete!")


if __name__ == "__main__":
    main()
