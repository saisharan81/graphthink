"""GraphThink client — wraps the Memory API."""

__version__ = "0.2.0"

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

    def create_memory(self, name: str = "") -> "Memory":
        """Create a dedicated isolated memory instance.

        Each Memory is fully isolated — data stored in one cannot
        be seen by another. Perfect for multi-tenant apps.

        Args:
            name: Optional label for this memory (e.g., "alice-co", "project-alpha")

        Returns:
            A Memory object with its own store/search/stats methods.
        """
        import uuid
        tenant_id = name or f"mem-{uuid.uuid4().hex[:8]}"
        return Memory(self, tenant_id)

    def store(self, content: str, role: str = "user", 
              session_id: str = "default", tenant_id: str = "") -> dict:
        """Store a memory in the graph.

        Args:
            content: The message or fact to remember
            role: "user" or "assistant" (default: "user")
            session_id: Group memories by conversation (default: "default")
            tenant_id: Isolate data per tenant (optional)

        Returns:
            {"ok": true, "msg": "Stored in session ..."}
        """
        payload = {"session_id": session_id, "role": role, "content": content}
        if tenant_id:
            payload["tenant_id"] = tenant_id
        return self._post("/v1/memory/store", payload)

    def search(self, query: str, limit: int = 5, min_score: float = 0.3,
               tenant_id: str = "") -> list:
        """Search stored memories semantically.

        Args:
            query: Natural language query
            limit: Max results (default: 5)
            min_score: Minimum similarity score 0-1 (default: 0.3)
            tenant_id: Only search within this tenant's data (optional)

        Returns:
            List of matching memories with scores
        """
        payload = {"query": query, "limit": limit, "min_score": min_score}
        if tenant_id:
            payload["tenant_id"] = tenant_id
        result = self._post("/v1/memory/search", payload)
        return result.get("results", [])

    def stats(self) -> dict:
        """Get graph statistics."""
        return self._get("/v1/memory/stats").get("stats", {})

    def _post(self, path, payload):
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

    def _get(self, path):
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            return {"error": str(e)}


class Memory:
    """An isolated memory instance.
    
    Created via GraphThink.create_memory(). Data in one Memory
    cannot be seen by another.
    
    Usage:
        gt = GraphThink()
        alice = gt.create_memory("alice-app")
        alice.store("I like Python")
        results = alice.search("what do I like")
    """

    def __init__(self, client: GraphThink, tenant_id: str):
        self._client = client
        self._tenant_id = tenant_id
        self.name = tenant_id

    def store(self, content: str, role: str = "user", session_id: str = "default") -> dict:
        """Store a fact in this isolated memory."""
        return self._client.store(
            content, role=role,
            session_id=f"{self._tenant_id}:{session_id}",
            tenant_id=self._tenant_id
        )

    def search(self, query: str, limit: int = 5) -> list:
        """Search only within this memory instance."""
        return self._client.search(query, limit=limit, tenant_id=self._tenant_id)

    def stats(self) -> dict:
        """Get graph stats for this memory."""
        # Note: stats are currently global, not per-tenant
        return self._client.stats()
