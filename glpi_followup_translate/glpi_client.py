"""GLPI API client for managing ticket followups.

Supports OAuth2 Password Flow authentication (recommended for services/daemons).
Uses GLPI High-Level REST API v2.3 endpoints.
"""

import logging
import time
from typing import Any, Dict, List, Optional

import requests
import urllib3

from .config import GlpiConfig

# Suppress warnings for self-signed certificates on internal servers
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)


class GlpiClient:
    """Client for interacting with GLPI API v2.3."""

    def __init__(self, config: GlpiConfig):
        self.config = config
        self.api_url = config.api_url.rstrip("/")
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.token_expires_at: float = 0
        self.session = requests.Session()
        # Accept self-signed certificates on internal GLPI servers
        self.session.verify = False
        # Don't set Content-Type globally - let requests handle it

    def _auth_oauth2_password(self) -> bool:
        """Try OAuth2 Password (Resource Owner Password Credentials) authentication.

        This is the recommended flow for daemon/service applications.

        Returns:
            True if authentication succeeded
        """
        if not self.config.username or not self.config.password:
            logger.debug("OAuth2 Password flow requires username and password")
            return False

        logger.info("Trying OAuth2 Password flow...")
        token_url = f"{self.api_url}/token"

        try:
            resp = self.session.post(
                token_url,
                data={
                    "grant_type": "password",
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                    "username": self.config.username,
                    "password": self.config.password,
                    "scope": "api",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

            self.access_token = data["access_token"]
            self.refresh_token = data.get("refresh_token")
            expires_in = data.get("expires_in", 3600)
            self.token_expires_at = time.time() + expires_in

            self.session.headers.update(
                {"Authorization": f"Bearer {self.access_token}"}
            )
            logger.info("OAuth2 Password token obtained, expires in %ds", expires_in)
            return True

        except requests.RequestException as e:
            logger.debug("OAuth2 Password flow failed: %s", e)
            return False

    def _refresh_access_token(self) -> bool:
        """Try to refresh the OAuth2 access token using the refresh token.

        Returns:
            True if refresh succeeded
        """
        if not self.refresh_token:
            return False

        logger.info("Refreshing OAuth2 access token...")
        token_url = f"{self.api_url}/token"

        try:
            resp = self.session.post(
                token_url,
                data={
                    "grant_type": "refresh_token",
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                    "refresh_token": self.refresh_token,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

            self.access_token = data["access_token"]
            self.refresh_token = data.get("refresh_token", self.refresh_token)
            expires_in = data.get("expires_in", 3600)
            self.token_expires_at = time.time() + expires_in

            self.session.headers.update(
                {"Authorization": f"Bearer {self.access_token}"}
            )
            logger.info("Token refreshed, expires in %ds", expires_in)
            return True

        except requests.RequestException as e:
            logger.debug("Token refresh failed: %s", e)
            return False

    def _ensure_token(self) -> None:
        """Ensure we have a valid OAuth2 access token."""
        # If we have a valid token, return
        if self.access_token and time.time() < self.token_expires_at - 60:
            return

        # Token expiring soon, try refresh first
        if self.access_token and self._refresh_access_token():
            return

        # No token or refresh failed, get new one
        if not self._auth_oauth2_password():
            raise RuntimeError(
                "OAuth2 Password authentication failed. "
                "Check client_id, client_secret, username, and password."
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
            method: HTTP method (GET, POST, PATCH, etc.)
            endpoint: API endpoint path (without base URL)
            json_data: JSON body for POST/PATCH requests
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

    def get_tickets(self, limit: int = 100, offset: int = 0) -> List[Dict]:
        """Fetch all tickets from GLPI.

        Args:
            limit: Maximum number of tickets to return
            offset: Pagination offset

        Returns:
            List of ticket dictionaries
        """
        params = {"range": f"{offset}-{offset + limit - 1}"}
        result = self._request("GET", "/Assistance/Ticket", params=params)
        return result if isinstance(result, list) else []

    def get_ticket(self, ticket_id: int) -> Dict:
        """Fetch a single ticket by ID.

        Args:
            ticket_id: The GLPI ticket ID

        Returns:
            Ticket dictionary
        """
        return self._request("GET", f"/Assistance/Ticket/{ticket_id}")

    def create_ticket(self, name: str, content: str, **kwargs) -> Dict:
        """Create a new ticket.

        Args:
            name: Ticket title/subject
            content: Ticket description
            **kwargs: Additional ticket fields (e.g., type, urgency, priority)

        Returns:
            Created ticket data (with id and href)
        """
        payload = {
            "name": name,
            "content": content,
            **kwargs,
        }
        return self._request("POST", "/Assistance/Ticket", json_data=payload)

    def update_ticket(self, ticket_id: int, **fields) -> Dict:
        """Update a ticket's fields.

        Args:
            ticket_id: The GLPI ticket ID
            **fields: Fields to update (e.g., name, content)

        Returns:
            Updated ticket data
        """
        return self._request("PATCH", f"/Assistance/Ticket/{ticket_id}", json_data=fields)

    def get_ticket_followups(self, ticket_id: int) -> List[Dict]:
        """Fetch all followups for a specific ticket.

        Args:
            ticket_id: The GLPI ticket ID

        Returns:
            List of followup dictionaries (wrapped in timeline format)
        """
        result = self._request("GET", f"/Assistance/Ticket/{ticket_id}/Timeline/Followup")
        if isinstance(result, list):
            # Extract the actual followup items from the timeline wrapper
            followups = []
            for item in result:
                if isinstance(item, dict) and "item" in item:
                    followups.append(item["item"])
                elif isinstance(item, dict) and "id" in item:
                    followups.append(item)
            return followups
        return []

    def create_followup(self, ticket_id: int, content: str, **kwargs) -> Dict:
        """Create a new followup on a ticket.

        Args:
            ticket_id: The GLPI ticket ID
            content: Followup content
            **kwargs: Additional followup fields

        Returns:
            Created followup data
        """
        payload = {
            "content": content,
            **kwargs,
        }
        return self._request(
            "POST",
            f"/Assistance/Ticket/{ticket_id}/Timeline/Followup",
            json_data=payload,
        )

    def update_followup(self, ticket_id: int, followup_id: int, content: str) -> Dict:
        """Update a followup's content.

        Args:
            ticket_id: The GLPI ticket ID
            followup_id: The GLPI followup ID
            content: New content for the followup

        Returns:
            Updated followup data
        """
        payload = {"content": content}
        return self._request(
            "PATCH",
            f"/Assistance/Ticket/{ticket_id}/Timeline/Followup/{followup_id}",
            json_data=payload,
        )
