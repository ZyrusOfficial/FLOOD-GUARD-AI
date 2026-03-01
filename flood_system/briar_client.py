import requests
import logging
import time
import urllib.parse

logger = logging.getLogger("BriarClient")

class BriarClient:
    def __init__(self, api_url="http://127.0.0.1:7000", api_token=None):
        self.api_url = api_url.rstrip('/')
        if not self.api_url.endswith('/v1'):
            self.api_url += '/v1'
        self.headers = {
            "Authorization": f"Bearer {api_token}" if api_token else "",
            "Content-Type": "application/json"
        }
        self.forum_name = "Flood Alerts"
        self._forum_id = None

    def _request(self, method, endpoint, data=None):
        url = f"{self.api_url}/{endpoint.lstrip('/')}"
        try:
            response = requests.request(method, url, headers=self.headers, json=data, timeout=10)
            response.raise_for_status()
            return response.json() if response.content else {}
        except Exception as e:
            logger.error(f"Briar API error ({method} {endpoint}): {e}")
            return None

    def check_connection(self):
        """Check if Briar Headless is reachable."""
        return self._request("GET", "/contacts") is not None

    def get_link(self):
        """Get the Briar link for this peer."""
        return self._request("GET", "/contacts/add/link")

    def add_contact(self, link, alias):
        """Add a new contact by their Briar link."""
        return self._request("POST", "/contacts/add/pending", {
            "link": link,
            "alias": alias
        })

    def get_forums(self):
        """List all joined/created forums."""
        return self._request("GET", "/forums")

    def create_forum(self, name):
        """Create a new forum."""
        return self._request("POST", "/forums", {"name": name})

    def post_to_forum(self, forum_id, message):
        """Post a message to a specific forum."""
        encoded_id = urllib.parse.quote(forum_id, safe='')
        data = {
            "text": message,
            "timestamp": int(time.time() * 1000)
        }
        return self._request("POST", f"/forums/{encoded_id}/posts", data)

    def sync_forum(self):
        """Ensure the 'Flood Alerts' forum exists and cache its ID."""
        forums = self.get_forums()
        if forums is None:
            return False

        # Find existing forum by name
        for forum in forums:
            if forum.get('name') == self.forum_name:
                self._forum_id = forum.get('id')
                logger.info(f"Found existing Briar forum: {self.forum_name} ({self._forum_id})")
                return True

        # Not found, create it
        logger.info(f"Creating new Briar forum: {self.forum_name}")
        new_forum = self.create_forum(self.forum_name)
        if new_forum:
            self._forum_id = new_forum.get('id')
            return True
        
        return False

    def send_alert(self, message):
        """Broadcast an alert to the Flood Alerts forum."""
        if not self._forum_id:
            if not self.sync_forum():
                logger.error("Could not sync with Briar forums.")
                return False

        result = self.post_to_forum(self._forum_id, message)
        if result is not None:
            logger.info("Alert posted to Briar forum.")
            return True
        return False
