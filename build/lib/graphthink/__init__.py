"""GraphThink — Sovereign Graph Memory for AI Agents.

Store, search, and retrieve semantic memories using a graph database.
Self-hosted. Zero API costs. No vendor lock-in.

Usage:
    pip install graphthink
    graphthink serve
    
    from graphthink import GraphThink
    gt = GraphThink()
    gt.store("user", "I like Python")
    results = gt.search("what programming language")
"""

from .client import GraphThink, __version__
from .cli import main as cli

__all__ = ["GraphThink", "__version__", "cli"]
__version__ = "0.1.1"
