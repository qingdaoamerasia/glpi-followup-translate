"""GLPI API client for managing ticket followups.

Supports OAuth2 Password Flow authentication (recommended for services/daemons).
Uses GLPI High-Level REST API v2.3 endpoints.
"""

import json
import logging
import os
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

    def __init__(
        self, config: GlpiConfig, token_cache_file: Optional[str] = ".glpi_token_cache.json"
    ):
        self.config = config
        self.api_url = config.api_url.rstrip("/")
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.token_expires_at: float = 0
        self.session = requests.Session()
        # Accept self-signed certificates on internal GLPI servers
        self.session.verify = False
        # Send a fixed PHPSESSID cookie so GLPI reuses the same PHP session
        # file instead of creating a new one for every API request. Without
        # this, GLPI's Symfony framework creates a new session file per
        # request (even for stateless Bearer-token API calls), which causes
        # inode exhaustion on the server over time.
        self.session.cookies.set(
            "PHPSESSID", "glpi_translate_client",
            domain=self.api_url.split("//")[-1].split("/")[0].split(":")[0],
        )
        # Don't set Content-Type globally - let requests handle it
        # Cached true_max_ticket_id from the last _scan_tickets_by_id run.
        # Persisted via ProcessedState so the daemon avoids re-probing IDs
        # that were already discovered in a previous pass.
        self._cached_max_ticket_id: int = 0
        # Set of all ticket IDs known to exist, discovered during previous
        # ID scans. Used to skip the full-range downward scan on subsequent
        # passes — instead of scanning every ID from max to 1 (hundreds of
        # 404 requests), we only re-check IDs we know exist.
        self._known_ticket_ids: set[int] = set()
        # Token cache file for cross-process token reuse. When None, caching
        # is disabled (used by unit tests that bypass __init__).
        self.token_cache_file: Optional[str] = token_cache_file
        self._load_token_cache()

    def _load_token_cache(self) -> None:
        """Load OAuth2 token and HTTP session cookies from the cache file.

        Enables cross-process token reuse and GLPI session persistence:
        instead of creating a new OAuth2 session and GLPI PHP session on
        every process start, subsequent processes read the cached token
        and cookies written by a previous process.

        Persisting cookies means the GLPI server reuses the same PHP
        session file instead of creating a new one per process, which
        prevents inode exhaustion from millions of session files.

        Uses ``getattr(self, "token_cache_file", None)`` so that unit-test
        mocks created via ``object.__new__(GlpiClient)`` (which bypass
        ``__init__``) do not raise ``AttributeError``.
        """
        cache_file = getattr(self, "token_cache_file", None)
        if not cache_file:
            return

        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            access_token = data.get("access_token")
            refresh_token = data.get("refresh_token")
            expires_at = data.get("token_expires_at")

            # Security/validation: expiry must be numeric and in the future
            if not isinstance(expires_at, (int, float)) or expires_at <= time.time():
                logger.debug("Token cache expired or invalid, ignoring")
                return

            if not access_token:
                logger.debug("Token cache has no access_token, ignoring")
                return

            self.access_token = access_token
            self.refresh_token = refresh_token
            self.token_expires_at = float(expires_at)

            # Update session headers so the cached token is immediately usable
            self.session.headers.update(
                {"Authorization": f"Bearer {self.access_token}"}
            )

            # Restore HTTP session cookies so GLPI reuses the same PHP session
            cached_cookies = data.get("cookies", {})
            if cached_cookies:
                self.session.cookies.update(cached_cookies)
                logger.debug("Loaded %d cached cookie(s)", len(cached_cookies))

            logger.debug(
                "Loaded cached OAuth2 token (expires in %ds)",
                int(self.token_expires_at - time.time()),
            )
        except FileNotFoundError:
            pass  # No cache file yet — normal on first run
        except (json.JSONDecodeError, IOError, ValueError) as e:
            logger.debug("Failed to load token cache: %s", e)

    def _save_token_cache(self) -> None:
        """Persist the current OAuth2 token and HTTP session cookies.

        Writes to a temporary file first, then atomically replaces the
        cache file via ``os.replace`` to avoid partial reads by other
        processes. Failures are logged at debug level and silently
        ignored — token caching is an optimization, not a requirement.

        Cookies are saved alongside the token so that subsequent
        processes can reuse the same GLPI PHP session, preventing
        inode exhaustion from millions of session files on the server.

        Uses ``getattr(self, "token_cache_file", None)`` for unit-test
        mock compatibility.
        """
        cache_file = getattr(self, "token_cache_file", None)
        if not cache_file:
            return

        try:
            # Convert cookie jar to a plain dict for JSON serialization
            cookies_dict = dict(self.session.cookies)
            payload = {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "token_expires_at": self.token_expires_at,
                "cookies": cookies_dict,
            }
            # Atomic write: temp file + os.replace
            tmp_file = f"{cache_file}.tmp"
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp_file, cache_file)

            # Restrict file permissions on Unix (token is sensitive)
            try:
                os.chmod(cache_file, 0o600)
            except OSError:
                pass  # Windows or insufficient permissions — ignore
        except (IOError, OSError) as e:
            logger.debug("Failed to save token cache: %s", e)

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
            self._save_token_cache()
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
            self._save_token_cache()
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
        headers: Optional[Dict] = None,
    ) -> Any:
        """Make an authenticated API request.

        Args:
            method: HTTP method (GET, POST, PATCH, etc.)
            endpoint: API endpoint path (without base URL)
            json_data: JSON body for POST/PATCH requests
            params: Query parameters
            headers: Per-request headers

        Returns:
            Parsed JSON response
        """
        self._ensure_token()

        url = f"{self.api_url}/{endpoint.lstrip('/')}"
        logger.debug("API %s %s", method, url)

        try:
            resp = self.session.request(
                method, url, json=json_data, params=params, headers=headers, timeout=30
            )
            resp.raise_for_status()

            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()

        except requests.HTTPError as e:
            # 404 responses are expected during ticket ID scanning (probing
            # for deleted ticket IDs). Demote them to debug to avoid log spam
            # while keeping genuine errors (500, 403, etc.) at error level.
            if resp.status_code == 404:
                logger.debug(
                    "GLPI API 404 %s %s: %s", method, url, resp.text[:200]
                )
            else:
                logger.error(
                    "GLPI API error %s %s: %s - %s", method, url, e, resp.text
                )
            raise
        except requests.RequestException as e:
            logger.error("GLPI API request failed %s %s: %s", method, url, e)
            raise

    def get_tickets(
        self,
        limit: int = 100,
        offset: int = 0,
        sort: str = "",
        order: str = "",
        range_style: str = "plain",
    ) -> List[Dict]:
        """Fetch tickets from GLPI.

        Note: Some GLPI API versions ignore range/sort/order parameters and
        always return all tickets. Callers should handle this gracefully.

        Args:
            limit: Maximum number of tickets to return per page
            offset: Pagination offset
            sort: Field to sort by (may be ignored by some API versions)
            order: Sort direction, "ASC" or "DESC" (may be ignored)
            range_style: "plain" sends Range: 0-99, "items" sends
                Range: items=0-99, "query" sends ?range=0-99.

        Returns:
            List of ticket dictionaries
        """
        range_value = f"{offset}-{offset + limit - 1}"
        params: dict = {}
        headers: dict = {}
        if sort:
            params["sort"] = sort
        if order:
            params["order"] = order
        if range_style == "items":
            headers["Range"] = f"items={range_value}"
        elif range_style == "query":
            params["range"] = range_value
        else:
            headers["Range"] = range_value
        result = self._request(
            "GET",
            "/Assistance/Ticket",
            params=params,
            headers=headers or None,
        )
        return result if isinstance(result, list) else []

    def get_all_tickets(self, page_size: int = 100) -> List[Dict]:
        """Fetch tickets across all API pages.

        GLPI caps ticket list responses at 100 items. This method walks the
        range window until the API returns a short page, and stops defensively
        if an API version ignores the range parameter and repeats a page.

        Args:
            page_size: Number of tickets to request per page. Values above 100
                are clamped because the GLPI endpoint caps responses there.

        Returns:
            De-duplicated ticket dictionaries in API response order.
        """
        page_size = max(1, min(page_size, 100))
        tickets, repeated_page = self._get_all_ticket_pages(page_size, "plain")
        if repeated_page:
            logger.warning("Plain Range header was ignored; retrying with items= range")
            tickets, repeated_page = self._get_all_ticket_pages(page_size, "items")
        if repeated_page:
            logger.warning("Range headers were ignored; retrying query range")
            tickets, repeated_page = self._get_all_ticket_pages(page_size, "query")
        if repeated_page:
            tickets = self._scan_tickets_by_id(tickets)

        return tickets

    def _get_all_ticket_pages(
        self, page_size: int, range_style: str
    ) -> tuple[List[Dict], bool]:
        """Fetch ticket pages with one pagination style."""
        tickets: List[Dict] = []
        seen_ids: set[int] = set()
        offset = 0

        while True:
            page = self.get_tickets(
                limit=page_size,
                offset=offset,
                sort="id",
                order="DESC",
                range_style=range_style,
            )
            if not page:
                return tickets, False

            new_count = 0
            for ticket in page:
                ticket_id = ticket.get("id")
                if ticket_id is not None:
                    if ticket_id in seen_ids:
                        continue
                    seen_ids.add(ticket_id)
                tickets.append(ticket)
                new_count += 1

            if len(page) < page_size:
                return tickets, False
            if new_count == 0:
                logger.warning(
                    "Ticket API repeated range %d-%d using %s pagination",
                    offset,
                    offset + page_size - 1,
                    range_style,
                )
                return tickets, True

            offset += page_size

    def _scan_tickets_by_id(self, seed_tickets: List[Dict]) -> List[Dict]:
        """Fallback for GLPI instances that ignore list pagination.

        When /Assistance/Ticket repeats the first 100 rows for every range,
        individual /Assistance/Ticket/{id} calls still work.

        This method performs two phases:

        1. **Upward probe** — Starting from ``seed_max_id + 1``, incrementally
           probe higher IDs to discover tickets that the list endpoint never
           returned (newer tickets or tickets beyond the list's hard cap).
           Stops after ``UPWARD_PROBE_MISS_LIMIT`` consecutive misses so a few
           deleted IDs near the top are tolerated while the probe still
           terminates once it has run past the real end of the ID space.

        2. **Downward scan** — From the *true* maximum ID discovered in phase 1
           (or ``seed_max_id`` if phase 1 found nothing) scan downward to 1,
           filling in any deleted-ID "holes" so that large contiguous blocks
           of deleted tickets do not cause the scan to stop prematurely.

        A total-request safety cap (``MAX_REQUESTS``) prevents runaway
        scanning when the ID space is very large.
        """
        seen_ids = {
            ticket.get("id")
            for ticket in seed_tickets
            if isinstance(ticket.get("id"), int)
        }
        if not seen_ids:
            return seed_tickets

        seed_max_id = max(seen_ids)
        # Use the cached true_max_ticket_id from a previous scan (if any) as
        # the starting floor so we don't re-probe IDs already discovered.
        # getattr with default 0 handles unit-test mocks that bypass __init__.
        cached_max = getattr(self, "_cached_max_ticket_id", 0)
        true_max_id = max(seed_max_id, cached_max)
        tickets = list(seed_tickets)

        # Safety valve: cap total individual GET requests so an extremely large
        # ID space does not produce tens of thousands of HTTP calls.
        MAX_REQUESTS = 10000
        # How many consecutive misses to tolerate before stopping the upward
        # probe. This bridges small holes near the top of the ID space while
        # still terminating promptly once we are past the real end.
        UPWARD_PROBE_MISS_LIMIT = 100

        logger.warning(
            "Falling back to ticket ID scan because list pagination repeated "
            "(seed max id %d, %d seed ticket(s) already known)",
            seed_max_id,
            len(seed_tickets),
        )

        requests_made = 0
        found = 0

        # ------------------------------------------------------------------
        # Phase 1: Upward probe — discover tickets with IDs above seed_max_id.
        # ------------------------------------------------------------------
        probe_id = true_max_id + 1
        consecutive_misses = 0

        logger.info(
            "Upward probe starting from id %d (miss limit %d)",
            probe_id,
            UPWARD_PROBE_MISS_LIMIT,
        )

        while consecutive_misses < UPWARD_PROBE_MISS_LIMIT:
            if requests_made >= MAX_REQUESTS:
                logger.warning(
                    "Upward probe hit total request safety cap of %d; "
                    "stopping at id %d",
                    MAX_REQUESTS,
                    probe_id,
                )
                break

            requests_made += 1

            try:
                ticket = self.get_ticket(probe_id)
            except Exception:  # noqa: BLE001 — tolerate all failure modes
                # Broad on purpose: GLPI returns HTTPError for 404, but unit
                # tests and other callers may raise RuntimeError or other
                # exceptions. We must keep probing upward regardless.
                logger.debug(
                    "Upward probe: id %d not available, consecutive miss %d",
                    probe_id,
                    consecutive_misses + 1,
                )
                consecutive_misses += 1
                probe_id += 1
                continue

            if isinstance(ticket, dict) and ticket.get("id"):
                tickets.append(ticket)
                seen_ids.add(ticket["id"])
                found += 1
                true_max_id = max(true_max_id, ticket["id"])
                consecutive_misses = 0
            else:
                consecutive_misses += 1

            probe_id += 1

        logger.info(
            "Upward probe complete: %d new ticket(s) found above seed max id "
            "%d, true max id is now %d (%d total requests so far)",
            found,
            seed_max_id,
            true_max_id,
            requests_made,
        )

        # ------------------------------------------------------------------
        # Phase 2: Downward scan — re-fetch known-existing tickets that
        # the list API didn't return, and discover any new ones.
        # ------------------------------------------------------------------
        downward_found = 0

        # On the first run _known_ticket_ids is empty, so we scan the full
        # range. On subsequent runs we only re-check IDs we already know
        # exist (skipping the hundreds of 404s from deleted-ID gaps).
        known_ids = getattr(self, "_known_ticket_ids", set())
        if known_ids:
            # Only re-fetch known-existing IDs not already in seed
            ids_to_check = sorted(known_ids - seen_ids, reverse=True)
            logger.info(
                "Downward scan: re-fetching %d known-existing ticket(s) "
                "not in seed (cached %d total)",
                len(ids_to_check),
                len(known_ids),
            )
        else:
            # First run: scan the full range to discover all IDs
            ids_to_check = list(range(true_max_id, 0, -1))
            logger.info(
                "Downward scan starting from id %d down to 1 (first run)",
                true_max_id,
            )

        for ticket_id in ids_to_check:
            if requests_made >= MAX_REQUESTS:
                logger.warning(
                    "Downward scan hit total request safety cap of %d; "
                    "stopping before id %d (%d new ticket(s) found so far)",
                    MAX_REQUESTS,
                    ticket_id,
                    found,
                )
                break

            if ticket_id in seen_ids:
                continue

            requests_made += 1

            try:
                ticket = self.get_ticket(ticket_id)
            except requests.HTTPError:
                logger.debug(
                    "Ticket %d not found (HTTP error), skipping", ticket_id
                )
                continue
            except requests.RequestException as e:
                logger.debug(
                    "Ticket %d request failed (%s), skipping", ticket_id, e
                )
                continue

            if isinstance(ticket, dict) and ticket.get("id"):
                tickets.append(ticket)
                seen_ids.add(ticket["id"])
                found += 1
                downward_found += 1

            if requests_made % 500 == 0:
                logger.info(
                    "Ticket ID scan progress: %d requests made, %d new "
                    "ticket(s) found, currently at id %d",
                    requests_made,
                    found,
                    ticket_id,
                )

        logger.info(
            "Ticket ID scan complete: %d new ticket(s) found via %d total "
            "requests (%d upward, %d downward), %d total ticket(s)",
            found,
            requests_made,
            found - downward_found,
            downward_found,
            len(tickets),
        )
        # Persist the discovered true_max_id and known-existing IDs so the
        # next scan can skip re-probing deleted-ID gaps.
        self._cached_max_ticket_id = true_max_id
        self._known_ticket_ids = seen_ids
        return tickets

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

    # -----------------------------------------------------------------------
    # Followups
    # -----------------------------------------------------------------------

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

    # -----------------------------------------------------------------------
    # Tasks
    # -----------------------------------------------------------------------

    def get_ticket_tasks(self, ticket_id: int) -> List[Dict]:
        """Fetch all tasks for a specific ticket.

        Args:
            ticket_id: The GLPI ticket ID

        Returns:
            List of task dictionaries
        """
        result = self._request("GET", f"/Assistance/Ticket/{ticket_id}/Timeline/Task")
        if isinstance(result, list):
            tasks = []
            for item in result:
                if isinstance(item, dict) and "item" in item:
                    tasks.append(item["item"])
                elif isinstance(item, dict) and "id" in item:
                    tasks.append(item)
            return tasks
        return []

    def update_task(self, ticket_id: int, task_id: int, content: str) -> Dict:
        """Update a task's content.

        Args:
            ticket_id: The GLPI ticket ID
            task_id: The GLPI task ID
            content: New content for the task

        Returns:
            Updated task data
        """
        payload = {"content": content}
        return self._request(
            "PATCH",
            f"/Assistance/Ticket/{ticket_id}/Timeline/Task/{task_id}",
            json_data=payload,
        )

    # -----------------------------------------------------------------------
    # Solutions
    # -----------------------------------------------------------------------

    def get_ticket_solutions(self, ticket_id: int) -> List[Dict]:
        """Fetch all solutions for a specific ticket.

        Args:
            ticket_id: The GLPI ticket ID

        Returns:
            List of solution dictionaries
        """
        result = self._request("GET", f"/Assistance/Ticket/{ticket_id}/Timeline/Solution")
        if isinstance(result, list):
            solutions = []
            for item in result:
                if isinstance(item, dict) and "item" in item:
                    solutions.append(item["item"])
                elif isinstance(item, dict) and "id" in item:
                    solutions.append(item)
            return solutions
        return []

    def update_solution(self, ticket_id: int, solution_id: int, content: str) -> Dict:
        """Update a solution's content.

        Args:
            ticket_id: The GLPI ticket ID
            solution_id: The GLPI solution ID
            content: New content for the solution

        Returns:
            Updated solution data
        """
        payload = {"content": content}
        return self._request(
            "PATCH",
            f"/Assistance/Ticket/{ticket_id}/Timeline/Solution/{solution_id}",
            json_data=payload,
        )

    # -----------------------------------------------------------------------
    # Validations
    # -----------------------------------------------------------------------

    def get_ticket_validations(self, ticket_id: int) -> List[Dict]:
        """Fetch all validations for a specific ticket.

        Args:
            ticket_id: The GLPI ticket ID

        Returns:
            List of validation dictionaries
        """
        result = self._request("GET", f"/Assistance/Ticket/{ticket_id}/Timeline/Validation")
        if isinstance(result, list):
            validations = []
            for item in result:
                if isinstance(item, dict) and "item" in item:
                    validations.append(item["item"])
                elif isinstance(item, dict) and "id" in item:
                    validations.append(item)
            return validations
        return []
