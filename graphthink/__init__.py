"""GraphThink — Sovereign Graph Memory for AI Agents.

Store, search, and retrieve semantic memories using a graph database.
Self-hosted. Zero API costs. No vendor lock-in.

Usage:
    from graphthink import GraphThink
    gt = GraphThink(base_url="http://localhost:18788")
    gt.store("user", "I like Python")
    results = gt.search("what programming language")
"""

from .client import GraphThink, __version__

__all__ = ["GraphThink", "__version__"]
__version__ = "0.1.0"
