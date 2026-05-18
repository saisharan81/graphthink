"""GraphThink client — wraps the Memory API."""

__version__ = "0.1.1"

import json
import urllib.request
import urllib.error

class GraphThink:
    """Client for the GraphThink Memory API.

    Args:
        base_url: URL of the GraphThink Gateway (default: http://localhost:18788)
        api_key: Optional API key for managed tiers
    """

    def __init__(self, base_url: str = "http://localhost:18788", api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Content-Type": "application/json",
        }
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"

    def store(self, content: str, role: str = "user", session_id: str = "default") -> dict:
        """Store a memory in the graph.

        Args:
            content: The message or fact to remember
            role: "user" or "assistant"
            session_id: Group memories by session (e.g., user_id)

        Returns:
            {"ok": true, "msg": "Stored in session ..."}
        """
        payload = {
            "session_id": session_id,
            "role": role,
            "content": content,
        }
        return self._post("/v1/memory/store", payload)

    def search(self, query: str, limit: int = 5, min_score: float = 0.3) -> list:
        """Search stored memories semantically.

        Args:
            query: Natural language query
            limit: Max results (default: 5)
            min_score: Minimum similarity score 0-1 (default: 0.3)

        Returns:
            List of matching memories with scores
        """
        payload = {
            "query": query,
            "limit": limit,
            "min_score": min_score,
        }
        result = self._post("/v1/memory/search", payload)
        return result.get("results", [])

    def stats(self) -> dict:
        """Get graph statistics.

        Returns:
            {"conversation": N, "message": N, "entity": N, ...}
        """
        result = self._get("/v1/memory/stats")
        return result.get("stats", {})

    def _post(self, path: str, payload: dict) -> dict:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, headers=self.headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            return {"ok": False, "error": f"HTTP {e.code}: {body}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _get(self, path: str) -> dict:
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            return {"error": str(e)}
