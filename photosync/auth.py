import pickle
from pathlib import Path
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow

from photosync.config import DATA_DIR, SCOPES


class AuthManager:
    """
    Manages Google Photos API authentication,
    reading/writing token files, refreshing creds, etc.
    """

    def __init__(self):
        self.credentials_json = DATA_DIR / "credentials.json"
        self.token_file = DATA_DIR / "token.json"
        self.creds = None

    def authenticate(self):
        """
        Loads credentials from token file if valid; otherwise performs OAuth flow.
        """
        if self.token_file.exists():
            with open(self.token_file, "rb") as token:
                try:
                    self.creds = pickle.load(token)
                except:
                    print("Token file corrupt. Re-authenticating.")
                    self.token_file.unlink()
                    self.creds = None

        # If no creds, or invalid/expired creds, do the flow
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

        return self.creds
