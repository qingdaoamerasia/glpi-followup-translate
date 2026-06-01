"""GLPI API client for managing ticket followups via OAuth2."""

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
        self.access_token: Optional[str] = None
        self.token_expires_at: float = 0
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    def _ensure_token(self) -> None:
        """Ensure we have a valid OAuth2 access token, refreshing if needed."""
        if self.access_token and time.time() < self.token_expires_at - 30:
            return

        logger.info("Requesting new OAuth2 access token...")
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

        except requests.RequestException as e:
            logger.error("Failed to obtain OAuth2 token: %s", e)
            raise

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
