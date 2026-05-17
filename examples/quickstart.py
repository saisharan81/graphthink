"""GraphThink Quick Start — run this to see graph memory in action.
No API key needed. Run on your machine.

Usage:
    pip install graphthink
    python quickstart.py
"""

from graphthink import GraphThink
import time

gt = GraphThink()

print("🔮 GraphThink — Sovereign Memory for AI Agents")
print("=" * 50)

# Step 1: Store memories
print("\n📝 Storing memories...")
gt.store("user", "My name is Alex and I'm building a customer support bot")
gt.store("assistant", "Great! I'll remember that. What framework are you using?")
gt.store("user", "I'm using FastAPI with PostgreSQL")
time.sleep(0.5)
print("   ✅ 3 memories stored")

# Step 2: Search
print("\n🔍 Searching...")
results = gt.search("what's the user's tech stack", limit=2)
print(f"   Found {len(results)} results:")
for r in results:
    print(f"   [{r['score']:.2f}] {r['content']}")

# Step 3: Cross-session retrieval
print("\n🔄 Cross-session test...")
results = gt.search("what is the user building")
for r in results:
    print(f"   [{r['score']:.2f}] {r['content']}")

# Step 4: Stats
print(f"\n📊 Graph stats: {gt.stats()}")
print("\n✅ Your agent now has persistent memory!")
