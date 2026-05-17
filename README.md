# GraphThink

**Sovereign graph memory for AI agents. Self-hosted. Zero API costs.**

Your agent loses context between sessions. Existing memory APIs charge per token and lock you into their cloud. GraphThink gives you persistent, semantic memory on your own infrastructure.

## Quick Start

```bash
pip install graphthink
```

```python
from graphthink import GraphThink

gt = GraphThink()

# Store memories
gt.store("user", "I prefer FastAPI over Django")
gt.store("user", "My project is a customer support bot")

# Search semantically — works across sessions
results = gt.search("what framework do I use")
# → [{"content": "I prefer FastAPI over Django", "score": 0.89}]

# See what's in your graph
print(gt.stats())
# → {"conversation": 2, "message": 3, "entity": 4, ...}
```

## Why GraphThink?

| Feature | Mem0 | Zep | GraphThink |
|---------|------|-----|------------|
| Graph relationships | ❌ | ❌ | ✅ |
| Self-hosted | ❌ | ❌ | ✅ |
| Zero per-query costs | ❌ | ❌ | ✅ |
| Free web search | ❌ | ❌ | ✅ |
| API key required | ✅ | ✅ | ❌ (open) |

## Architecture

```
Your App ──→ GraphThink Client ──→ GraphThink API ──→ Memgraph
(pip install)   (localhost:18788)     (your Docker)
```

All data stays on your machine. No third-party servers.

## Use Cases

- **Customer support bots** — remember user history across tickets
- **Personal AI assistants** — persist preferences indefinitely
- **Multi-agent systems** — share context between agents
- **Cost-sensitive teams** — eliminate per-token memory costs

## Self-Hosted Setup

```bash
# Start Memgraph (you need this once)
docker run -d --name memgraph -p 7687:7687 memgraph/memgraph-platform

# Start GraphThink API
graphthink serve

# That's it. Your memories, your infrastructure.
```

## License

MIT — free for personal and commercial use.
