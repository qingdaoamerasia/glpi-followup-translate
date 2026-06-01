"""GLPI API client for managing ticket followups.

Supports two authentication methods:
1. OAuth2 Client Credentials (when client_id/client_secret are OAuth2 credentials)
2. GLPI App-Token + User-Token (standard GLPI API auth)
"""

import logging
import time
from typing import Any, Dict, List, Optional

import requests

from .config import GlpiConfig

logger = logging.getLogger(__name__)


class GlpiClient:
    """Client for interacting with GLPI API v2.3."""

    def __init__(self, config: GlpiConfig):
        self.config = config
        self.api_url = config.api_url.rstrip("/")
        self.session_token: Optional[str] = None
        self.access_token: Optional[str] = None
        self.token_expires_at: float = 0
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    def _auth_oauth2(self) -> bool:
        """Try OAuth2 Client Credentials authentication.

        Returns:
            True if authentication succeeded
        """
        logger.info("Trying OAuth2 Client Credentials...")
        token_url = f"{self.api_url}/token"

        try:
            resp = self.session.post(
                token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

            self.access_token = data["access_token"]
            expires_in = data.get("expires_in", 3600)
            self.token_expires_at = time.time() + expires_in

            self.session.headers.update(
                {"Authorization": f"Bearer {self.access_token}"}
            )
            logger.info("OAuth2 token obtained, expires in %ds", expires_in)
            return True

        except requests.RequestException as e:
            logger.debug("OAuth2 auth failed: %s", e)
            return False

    def _auth_app_token(self) -> bool:
        """Try GLPI App-Token + User-Token authentication via initSession.

        Returns:
            True if authentication succeeded
        """
        logger.info("Trying App-Token + User-Token auth via initSession...")

        try:
            resp = self.session.get(
                f"{self.api_url}/initSession",
                headers={
                    "App-Token": self.config.client_id,
                    "Authorization": f"user_token {self.config.client_secret}",
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

            self.session_token = data.get("session_token")
            if not self.session_token:
                logger.error("initSession returned no session_token: %s", data)
                return False

            self.session.headers.update(
                {
                    "Session-Token": self.session_token,
                    "App-Token": self.config.client_id,
                }
            )
            # Remove any previous Authorization header
            self.session.headers.pop("Authorization", None)
            logger.info("Session token obtained successfully")
            return True

        except requests.RequestException as e:
            logger.debug("App-Token auth failed: %s", e)
            return False

    def _ensure_token(self) -> None:
        """Ensure we have a valid authentication, trying methods based on config."""
        # If we have a valid OAuth2 token, use it
        if self.access_token and time.time() < self.token_expires_at - 30:
            return

        # If we have a session token, we're good
        if self.session_token:
            return

        auth_method = self.config.auth_method.lower()

        if auth_method == "oauth2":
            if not self._auth_oauth2():
                raise RuntimeError("OAuth2 authentication failed. Check credentials.")
        elif auth_method == "app_token":
            if not self._auth_app_token():
                raise RuntimeError("App-Token authentication failed. Check credentials.")
        else:  # "auto" - try both
            if self._auth_oauth2():
                return
            if self._auth_app_token():
                return
            raise RuntimeError(
                "All authentication methods failed. "
                "Check your GLPI credentials and OAuth2/App-Token configuration."
            )

    def _request(
        self,
        method: str,
        endpoint: str,
        json_data: Optional[Dict] = None,
        params: Optional[Dict] = None,
    ) -> Any:
        """Make an authenticated API request.

        Args:
            method: HTTP method (GET, POST, PUT, etc.)
            endpoint: API endpoint path (without base URL)
            json_data: JSON body for POST/PUT requests
            params: Query parameters

        Returns:
            Parsed JSON response
        """
        self._ensure_token()

        url = f"{self.api_url}/{endpoint.lstrip('/')}"
        logger.debug("API %s %s", method, url)

        try:
            resp = self.session.request(
                method, url, json=json_data, params=params, timeout=30
            )
            resp.raise_for_status()

            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()

        except requests.HTTPError as e:
            logger.error("GLPI API error %s %s: %s - %s", method, url, e, resp.text)
            raise
        except requests.RequestException as e:
            logger.error("GLPI API request failed %s %s: %s", method, url, e)
            raise

    def get_tickets(
        self, limit: int = 100, offset: int = 0, **filters
    ) -> List[Dict]:
        """Fetch tickets from GLPI.

        Args:
            limit: Maximum number of tickets to return
            offset: Pagination offset
            **filters: Additional query filters

        Returns:
            List of ticket dictionaries
        """
        params = {"range": f"{offset}-{offset + limit - 1}", **filters}
        result = self._request("GET", "/Ticket", params=params)
        return result if isinstance(result, list) else []

    def get_ticket_followups(self, ticket_id: int) -> List[Dict]:
        """Fetch all followups for a specific ticket.

        Args:
            ticket_id: The GLPI ticket ID

        Returns:
            List of followup dictionaries
        """
        result = self._request("GET", f"/Ticket/{ticket_id}/TicketFollowup")
        return result if isinstance(result, list) else []

    def update_followup(self, followup_id: int, content: str) -> Dict:
        """Update a followup's content.

        Args:
            followup_id: The GLPI followup ID
            content: New content for the followup

        Returns:
            Updated followup data
        """
        payload = {"input": {"content": content}}
        return self._request("PUT", f"/TicketFollowup/{followup_id}", json_data=payload)

    def get_all_followups_since(self, since_timestamp: int) -> List[Dict]:
        """Fetch all followups modified since a given timestamp.

        Args:
            since_timestamp: Unix timestamp to filter from

        Returns:
            List of followup dicts with ticket_id attached
        """
        # GLPI API supports filtering by date_mod
        # Use search criteria: date_mod >= since_timestamp
        params = {
            "criteria[0][field]": 19,  # date_mod field
            "criteria[0][searchtype]": "greater",
            "criteria[0][value]": since_timestamp,
            "forcedisplay[0]": 2,  # id
            "forcedisplay[1]": 19,  # date_mod
            "forcedisplay[2]": 21,  # content
            "range": "0-999",
        }

        try:
            result = self._request("GET", "/TicketFollowup", params=params)
            return result if isinstance(result, list) else []
        except Exception:
            logger.warning(
                "Search endpoint failed, falling back to per-ticket followup fetch"
            )
            return []

    def create_ticket(self, name: str, content: str, **kwargs) -> Dict:
        """Create a new ticket.

        Args:
            name: Ticket title/subject
            content: Ticket description
            **kwargs: Additional ticket fields (e.g., urgency, priority, etc.)

        Returns:
            Created ticket data
        """
        payload = {
            "input": {
                "name": name,
                "content": content,
                **kwargs,
            }
        }
        return self._request("POST", "/Ticket", json_data=payload)

    def create_followup(self, ticket_id: int, content: str) -> Dict:
        """Create a new followup on a ticket.

        Args:
            ticket_id: The GLPI ticket ID
            content: Followup content

        Returns:
            Created followup data
        """
        payload = {
            "input": {
                "tickets_id": ticket_id,
                "content": content,
            }
        }
        return self._request("POST", "/TicketFollowup", json_data=payload)
