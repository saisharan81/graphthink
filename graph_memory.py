#!/usr/bin/env python3
"""
graph_memory.py — ARCHON Graph Memory Layer (Phase 1)

Adds conversation storage and entity extraction to the Memgraph knowledge graph.
Bidirectional: reads AND writes during conversations.

Node Types Added:
  - Conversation: A conversation session
  - Message: A single conversation turn
  - Entity: A extracted entity (person, concept, tool, preference)

Relationships:
  (Conversation)-[:HAS_MESSAGE]->(Message)
  (Message)-[:NEXT_MESSAGE]->(Message)
  (Message)-[:MENTIONS]->(Entity)
  (Entity)-[:RELATED_TO]->(Entity)

This lives alongside the existing NEXUS_BRIDGE — doesn't replace it.
"""

import sys, os, json, time, re, logging
from pathlib import Path
from typing import Optional, Any
from neo4j import GraphDatabase

# ── Logging ────────────────────────────────────────────────────────
logger = logging.getLogger("graph_memory")

# ── Config ─────────────────────────────────────────────────────
MEMGRAPH_URI = "bolt://localhost:7687"
MAX_MESSAGES_PER_CONTEXT = 20  # How many recent messages to include in context
ENTITY_MIN_CONFIDENCE = 0.5
MAX_SEARCH_AGE_SECONDS = 300  # Cache SearXNG results for 5 minutes
CACHE_INVALIDATE_FILE = "/tmp/archon_nexus_cache_stamp"

# ── Cache Invalidation ──────────────────────────────────────────

def invalidate_nexus_cache():
    """Bump a timestamp file that NEXUS_BRIDGE can check for cache staleness."""
    try:
        stamp = int(time.time())
        Path(CACHE_INVALIDATE_FILE).write_text(str(stamp))
    except:
        pass

def is_nexus_cache_stale(threshold_seconds: int = 30) -> bool:
    """Check if cache was invalidated within last N seconds."""
    try:
        stamp = int(Path(CACHE_INVALIDATE_FILE).read_text().strip())
        return (time.time() - stamp) < threshold_seconds
    except:
        return False


# ── Connection ─────────────────────────────────────────────────

_driver: Optional[Any] = None

def get_driver():
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(MEMGRAPH_URI, auth=("", ""))
    return _driver

def close():
    global _driver
    if _driver:
        _driver.close()
        _driver = None


# ── Schema Setup (run once on startup) ─────────────────────────

KNOWN_CONSTS = {}

def ensure_schema():
    """Create indexes and constraints if they don't exist. Safe to call repeatedly."""
    driver = get_driver()
    with driver.session() as session:
        # Label indexes for fast lookups
        for label, props in [
            ("Conversation", ["session_id"]),
            ("Message", ["timestamp", "session_id"]),
            ("Entity", ["name"]),
        ]:
            for prop in props:
                try:
                    session.run(f"CREATE INDEX ON :{label}({prop})")
                except:
                    pass  # Already exists
        
        # Edge index for message chains
        try:
            session.run("CREATE EDGE INDEX ON :NEXT_MESSAGE")
        except:
            pass


# ── Entity Extraction ──────────────────────────────────────────

# Entity patterns for fast regex-based extraction (no LLM needed for common types)
_ENTITY_PATTERNS = {
    "TOOL": re.compile(r'\b(?:Gateway|Kernel|Nexus|Memgraph|Ollama|SearXNG|DeepSeek|Archon|Sentinel|Forger|Weaver)\b', re.I),
    "PERSON": re.compile(r'\bSai\b'),
    "PROTOCOL": re.compile(r'\b(?:AGENTS\.md|SOUL\.md|MEMORY\.md|IDENTITY\.md|TOOLS\.md|HEARTBEAT\.md)\b', re.I),
    "CONCEPT": re.compile(r'\b(?:knowledge.?graph|agentic.?os|gateway.?brain|break.?test|three.?layer|sovereignty|HITL|MCP|A2A)\b', re.I),
}

def extract_entities(text: str) -> list[dict]:
    """
    Extract entities from text using regex patterns + optional LLM fallback.
    Returns list of {name, type, confidence}.
    """
    entities = []
    seen = set()
    
    for etype, pattern in _ENTITY_PATTERNS.items():
        for match in pattern.finditer(text):
            name = match.group(0).strip()
            key = f"{name}:{etype}"
            if key not in seen:
                seen.add(key)
                entities.append({
                    "name": name,
                    "type": etype,
                    "confidence": 0.9,
                })
    
    # Detect potential Entities by capitalized words (simple heuristic)
    cap_pattern = re.compile(r'\b([A-Z][a-z]+(?: [A-Z][a-z]+)*)\b')
    stopwords = {
        "this", "that", "what", "when", "where", "which", "there", "here",
        "hello", "hi", "hey", "thanks", "please", "yes", "no", "ok",
        "the", "and", "for", "are", "not", "but", "can", "you", "all",
        "has", "have", "had", "was", "were", "been", "being", "with",
        "about", "into", "over", "after", "before", "between", "under",
        "very", "too", "so", "much", "many", "some", "any", "each",
        "every", "both", "few", "more", "most", "other", "such",
        "only", "same", "nor", "yet", "end", "checking", "ready",
        "current", "confirmed", "integration", "session", "knowledge",
    }
    for match in cap_pattern.finditer(text):
        name = match.group(0).strip()
        if not name or len(name) < 3 or len(name) > 30:
            continue
        if name.lower() in stopwords:
            continue
        # Skip single-capital-letter words and common English capitalized words
        if len(name) <= 4 and name.lower() in {"this", "that", "with", "from", "have", "been"}:
            continue
        key = f"{name}:POTENTIAL"
        if key not in seen:
            seen.add(key)
            entities.append({
                "name": name,
                "type": "POTENTIAL",
                "confidence": 0.6,
            })
    
    return [e for e in entities if e["confidence"] >= ENTITY_MIN_CONFIDENCE]


# ── Short-Term Memory: Conversations ──────────────────────────

async def add_message(session_id: str, role: str, content: str, message_id: Optional[str] = None):
    """
    Store a conversation message in the graph.
    Creates Conversation node if it doesn't exist.
    Links to previous message via NEXT_MESSAGE.
    Extracts entities and creates MENTIONS relationships.
    """
    if not content or len(content.strip()) < 3:
        return
    
    driver = get_driver()
    with driver.session() as session:
        msg_id = message_id or f"msg-{int(time.time())}-{hash(content) % 10000}"
        timestamp = int(time.time())
        
        # Get the last message in this session for linking
        last_msg = session.run(
            "MATCH (c:Conversation {session_id: $sid})-[:HAS_MESSAGE]->(m:Message) "
            "WHERE NOT EXISTS((m)-[:NEXT_MESSAGE]->(:Message)) "
            "RETURN m.id AS id ORDER BY m.timestamp DESC LIMIT 1",
            sid=session_id
        ).single()
        prev_id = last_msg["id"] if last_msg else None
        
        # Create conversation if needed
        # Must use separate MATCH/MERGE to handle the count correctly
        # Memgraph's MERGE ... ON MATCH doesn't always increment correctly
        # with later MATCH clauses, so we handle it explicitly
        conv_check = session.run(
            "MATCH (c:Conversation {session_id: $sid}) RETURN c.message_count AS cnt",
            sid=session_id
        ).single()
        
        if conv_check:
            existing_count = conv_check["cnt"] or 0
            session.run(
                "MATCH (c:Conversation {session_id: $sid}) "
                "SET c.message_count = $cnt",
                sid=session_id, cnt=existing_count + 1
            )
        else:
            session.run(
                "CREATE (c:Conversation {session_id: $sid, created_at: $ts, message_count: 1}) "
                "WITH c "
                "MATCH (os:OS {name:'Archon-One'}) "
                "MERGE (c)-[:BELONGS_TO]->(os)",
                sid=session_id, ts=timestamp
            )
        
        # Create message
        if prev_id:
            session.run(
                "MATCH (c:Conversation {session_id: $sid}) "
                "CREATE (m:Message {id: $mid, session_id: $sid, role: $role, content: $content, "
                "  timestamp: $ts, created_at: $ts}) "
                "MERGE (c)-[:HAS_MESSAGE]->(m) "
                "WITH m "
                "MATCH (os:OS {name:'Archon-One'}) "
                "MERGE (m)-[:BELONGS_TO]->(os) "
                "WITH m "
                "MATCH (prev:Message {id: $prev_id}) "
                "MERGE (prev)-[:NEXT_MESSAGE]->(m)",
                sid=session_id, mid=msg_id, role=role, content=content[:2000],
                ts=timestamp, prev_id=prev_id
            )
        else:
            session.run(
                "MATCH (c:Conversation {session_id: $sid}) "
                "CREATE (m:Message {id: $mid, session_id: $sid, role: $role, content: $content, "
                "  timestamp: $ts, created_at: $ts}) "
                "MERGE (c)-[:HAS_MESSAGE]->(m) "
                "WITH m "
                "MATCH (os:OS {name:'Archon-One'}) "
                "MERGE (m)-[:BELONGS_TO]->(os)",
                sid=session_id, mid=msg_id, role=role, content=content[:2000],
                ts=timestamp
            )
        
        # Phase 3: compute and store embedding vector
        emb = compute_embedding(content)
        if emb:
            try:
                session.run(
                    "MATCH (m:Message {id: $mid}) SET m.emb = $emb",
                    mid=msg_id, emb=emb
                )
            except:
                pass
        
        # Extract entities and link
        entities = extract_entities(content)
        for ent in entities:
            if not ent.get("name"):
                continue
            session.run(
                "MERGE (e:Entity {name: $name}) "
                "ON CREATE SET e.type = $etype, e.confidence = $conf, "
                "  e.first_seen = $ts, e.mention_count = 1 "
                "ON MATCH SET e.mention_count = e.mention_count + 1, "
                "  e.last_seen = $ts, "
                "  e.confidence = CASE WHEN $conf > e.confidence THEN $conf ELSE e.confidence END "
                "WITH e "
                "MATCH (m:Message {id: $mid}) "
                "MERGE (m)-[:MENTIONS]->(e) "
                "WITH e "
                "MATCH (os:OS {name:'Archon-One'}) "
                "MERGE (e)-[:BELONGS_TO]->(os)",
                name=ent["name"], etype=ent["type"], conf=ent["confidence"],
                ts=timestamp, mid=msg_id
            )
        
        # Invalidate NEXUS_BRIDGE cache after a graph write
        invalidate_nexus_cache()


# ── Memory Health & Validation ─────────────────────────────────

def validate_context(session_id: str) -> dict:
    """
    Validate conversation graph health before injecting context into LLM.
    Returns diagnostics summary.
    """
    driver = get_driver()
    diagnostics = {"session_id": session_id, "healthy": True, "issues": []}
    with driver.session() as session:
        # Check message chain integrity
        result = session.run(
            "MATCH (c:Conversation {session_id: $sid})-[:HAS_MESSAGE]->(m:Message) "
            "OPTIONAL MATCH (m)-[:NEXT_MESSAGE]->(next:Message) "
            "RETURN count(m) AS total_messages, "
            "  count(next) AS linked_messages",
            sid=session_id
        ).single()
        if result:
            diagnostics["total_messages"] = result["total_messages"]
            total = result["total_messages"]
            linked = result["linked_messages"]
            if total > 0 and total != linked + 1:
                diagnostics["healthy"] = False
                diagnostics["issues"].append(f"Message chain broken: {total} msgs but only {linked} NEXT_MESSAGE links")
        
        # Check for truncated content (Memgraph doesn't allow aggregation inside CASE)
        result = session.run(
            "MATCH (m:Message {session_id: $sid}) "
            "RETURN size(m.content) AS content_length",
            sid=session_id
        )
        truncated_count = sum(1 for r in result if r.get("content_length") == 2000)
        if truncated_count > 0:
            diagnostics["issues"].append(f"{truncated_count} messages at truncation boundary (2000 chars)")
        
        # Check for null properties
        result = session.run(
            "MATCH (n:Conversation {session_id: $sid}) UNWIND keys(n) AS prop "
            "WITH n, prop WHERE n[prop] IS NULL RETURN count(*) AS nulls",
            sid=session_id
        ).single()
        if result and result["nulls"] > 0:
            diagnostics["issues"].append(f"{result['nulls']} null properties on Conversation node")
        
        # Check BELONGS_TO
        result = session.run(
            "MATCH (c:Conversation {session_id: $sid}) "
            "WHERE NOT EXISTS((c)-[:BELONGS_TO]->()) RETURN count(*) AS orphans",
            sid=session_id
        ).single()
        if result and result["orphans"] > 0:
            diagnostics["issues"].append("Conversation missing BELONGS_TO")
        
        diagnostics["healthy"] = len(diagnostics["issues"]) == 0
    
    return diagnostics


def integrity_check() -> dict:
    """
    Full graph integrity scan. Detects common corruption patterns.
    Run periodically (e.g., on startup, via Sentinel).
    """
    driver = get_driver()
    report = {"healthy": True, "checks": {}}
    
    with driver.session() as session:
        # 1. Orphaned Messages (no Conversation parent)
        result = session.run(
            "MATCH (m:Message) WHERE NOT EXISTS((:Conversation)-[:HAS_MESSAGE]->(m)) "
            "RETURN count(*) AS cnt"
        ).single()
        orphans = result["cnt"] if result else 0
        report["checks"]["orphan_messages"] = orphans
        if orphans > 0:
            report["healthy"] = False
        
        # 2. Orphaned Entities (no MENTIONS relationship)
        result = session.run(
            "MATCH (e:Entity) WHERE NOT EXISTS((:Message)-[:MENTIONS]->(e)) "
            "RETURN count(*) AS cnt"
        ).single()
        entity_orphans = result["cnt"] if result else 0
        report["checks"]["orphan_entities"] = entity_orphans
        if entity_orphans > 0:
            report["healthy"] = False
        
        # 3. Entities missing BELONGS_TO
        result = session.run(
            "MATCH (e:Entity) WHERE NOT EXISTS((e)-[:BELONGS_TO]->()) "
            "RETURN count(*) AS cnt"
        ).single()
        no_belongs = result["cnt"] if result else 0
        report["checks"]["entities_no_belongs_to"] = no_belongs
        if no_belongs > 0:
            report["healthy"] = False
        
        # 4. Messages missing session_id
        result = session.run(
            "MATCH (m:Message) WHERE m.session_id IS NULL RETURN count(*) AS cnt"
        ).single()
        no_session = result["cnt"] if result else 0
        report["checks"]["messages_no_session"] = no_session
        if no_session > 0:
            report["healthy"] = False
        
        # 5. Broken message chains (wrong direction)
        result = session.run(
            "MATCH (a:Message)-[:NEXT_MESSAGE]->(b:Message) "
            "WHERE a.timestamp > b.timestamp RETURN count(*) AS cnt"
        ).single()
        broken_chains = result["cnt"] if result else 0
        report["checks"]["broken_next_message_chain"] = broken_chains
        if broken_chains > 0:
            report["healthy"] = False
        
        # 6. Truncated content (exactly 2000 chars)
        result = session.run(
            "MATCH (m:Message) WHERE size(m.content) = 2000 RETURN count(*) AS cnt"
        ).single()
        truncated = result["cnt"] if result else 0
        report["checks"]["messages_at_truncation_boundary"] = truncated
        
        # 7. Cross-session NEXT_MESSAGE contamination
        result = session.run(
            "MATCH (a:Message)-[:NEXT_MESSAGE]->(b:Message) "
            "WHERE a.session_id <> b.session_id AND a.session_id IS NOT NULL "
            "AND b.session_id IS NOT NULL "
            "RETURN count(*) AS cnt"
        ).single()
        cross_session = result["cnt"] if result else 0
        report["checks"]["cross_session_next_message"] = cross_session
        if cross_session > 0:
            report["healthy"] = False
        
        # 8. Total graph summary
        result = session.run(
            "MATCH (c:Conversation) RETURN count(*) AS convs"
        ).single()
        conversations = result["convs"] if result else 0
        result = session.run("MATCH (m:Message) RETURN count(*) AS msgs").single()
        messages = result["msgs"] if result else 0
        result = session.run("MATCH (e:Entity) RETURN count(*) AS ents").single()
        entities = result["ents"] if result else 0
        
        report["summary"] = {
            "conversations": conversations,
            "messages": messages,
            "entities": entities,
        }
    
    return report


def decay_entity_weights(decay_factor: float = 0.95, max_age_hours: int = 72):
    """
    Decay entity weights based on time since last mention.
    Older entities with no recent mentions lose weight.
    Called periodically or on startup.
    """
    driver = get_driver()
    now_ms = int(time.time() * 1000)
    max_age_ms = max_age_hours * 3600 * 1000
    
    with driver.session() as session:
        result = session.run(
            "MATCH (e:Entity) "
            "WHERE e.last_seen IS NOT NULL "
            "  AND e.mention_count > 0 "
            "  AND $now_ms - e.last_seen > $max_age_ms "
            "WITH e, ($now_ms - e.last_seen) / 3600000.0 AS hours_since "
            "SET e.mention_count = CASE "
            "  WHEN hours_since > 0 THEN "
            "    toInteger(e.mention_count * ($decay_factor ^ hours_since)) "
            "  ELSE e.mention_count "
            "END "
            "RETURN count(*) AS decayed",
            now_ms=now_ms, max_age_ms=max_age_ms, decay_factor=decay_factor
        ).single()
        return result["decayed"] if result else 0


# ── Search Result Storage (SearXNG integration) ───────────────

def store_search_result(session_id: str, query_text: str, result: dict):
    """
    Store a SearXNG search result in the graph.
    Creates SearchResult nodes linked to the Conversation that triggered the search.
    """
    driver = get_driver()
    ts = int(time.time())
    
    with driver.session() as session:
        sr_id = f"sr-{ts}-{hash(result.get('url', '')) % 10000}"
        session.run(
            "MATCH (c:Conversation {session_id: $sid}) "
            "CREATE (sr:SearchResult {"
            "  id: $rid, search_query: $sq, url: $url, "
            "  title: $title, snippet: $snippet, "
            "  engine: $engine, score: $score, "
            "  fetched_at: $ts"
            "}) "
            "MERGE (c)-[:TRIGGERED_SEARCH]->(sr) "
            "WITH sr "
            "MATCH (os:OS {name:'Archon-One'}) "
            "MERGE (sr)-[:BELONGS_TO]->(os)",
            sid=session_id, rid=sr_id, sq=query_text[:200],
            url=result.get("url", "")[:500],
            title=result.get("title", "")[:200],
            snippet=result.get("content", "")[:500],
            engine=result.get("engine", "unknown"),
            score=float(result.get("score", 0)),
            ts=ts
        )


def get_cached_search_results(session_id: str, keywords: list, max_age_seconds: int = 300) -> list:
    """
    Check Memgraph for recent search results matching keywords.
    Returns cached results if found within max_age_seconds.
    """
    driver = get_driver()
    min_ts = int(time.time()) - max_age_seconds
    
    with driver.session() as session:
        result = session.run(
            "MATCH (c:Conversation {session_id: $sid})-[:TRIGGERED_SEARCH]->(sr:SearchResult) "
            "WHERE sr.fetched_at >= $min_ts "
            "RETURN sr.title, sr.url, sr.snippet, sr.engine, sr.score, sr.query "
            "ORDER BY sr.score DESC LIMIT 5",
            sid=session_id, min_ts=min_ts
        )
        return list(result)


# ── Context Retrieval ──────────────────────────────────────────

def get_conversation_context(session_id: str, query: str = "", max_messages: int = None) -> str:
    """
    Get conversation history + relevant entities for context injection.
    Replaces (or supplements) the existing NEXUS_BRIDGE get_context_for_query().
    """
    if max_messages is None:
        max_messages = MAX_MESSAGES_PER_CONTEXT
    
    driver = get_driver()
    with driver.session() as session:
        parts = []
        
        # Recent messages
        result = session.run(
            "MATCH (c:Conversation {session_id: $sid})-[:HAS_MESSAGE]->(m:Message) "
            "RETURN m.role AS role, m.content AS content, m.timestamp AS ts "
            "ORDER BY m.timestamp DESC LIMIT $limit",
            sid=session_id, limit=max_messages
        )
        messages = list(result)
        messages.reverse()
        
        if messages:
            parts.append("📝 Recent Conversation:")
            for m in messages:
                role_label = "You" if m["role"] == "user" else "Archon"
                content = (m["content"] or "")[:300]
                parts.append(f"  {role_label}: {content}")
        
        # Entities mentioned in this session
        result = session.run(
            "MATCH (c:Conversation {session_id: $sid})-[:HAS_MESSAGE]->"
            "  (:Message)-[:MENTIONS]->(e:Entity) "
            "RETURN e.name AS name, e.type AS type, e.mention_count AS count "
            "ORDER BY e.mention_count DESC LIMIT 10",
            sid=session_id
        )
        seen_entities = set()
        entity_list = []
        for r in result:
            key = r["name"]
            if key not in seen_entities:
                seen_entities.add(key)
                entity_list.append(r)
        if entity_list:
            parts.append("\n📌 Known Entities:")
            for e in entity_list:
                parts.append(f"  {e['name']} ({e['type']}, mentioned {e['count']}x)")
        
        return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════════
# PHASE 3 — Vector Search (Semantic Memory)
# ═══════════════════════════════════════════════════════════════════

_embedding_model = None

def _get_embedder():
    """Lazy-load sentence-transformers model. First call downloads ~90MB model."""
    global _embedding_model
    if _embedding_model is None:
        try:
            from sentence_transformers import SentenceTransformer
            _embedding_model = SentenceTransformer('all-MiniLM-L6-v2')
        except Exception as e:
            logger.warning(f"Embedding model failed to load: {e}")
            return None
    return _embedding_model


def compute_embedding(text: str) -> Optional[list]:
    """Compute a 384-dim embedding vector for text. Returns None on failure."""
    model = _get_embedder()
    if model is None:
        return None
    try:
        emb = model.encode(str(text)[:500])  # Cap at 500 chars for performance
        return emb.tolist()
    except Exception as e:
        logger.warning(f"Embedding failed: {e}")
        return None


def ensure_vector_index():
    """Create index on Message for embedding lookups (stored as 'emb' property).
    Note: Memgraph 3.9 vector index requires exact dimension — we skip native
    vector index and do Python-side cosine comparison instead."""
    driver = get_driver()
    with driver.session() as session:
        try:
            session.run("CREATE INDEX ON :Message(emb)")
            logger.warning("Index on Message(emb) created for embedding lookups")
        except:
            pass  # Already exists


def semantic_search(query: str, limit: int = 5, min_score: float = 0.3,
                    tenant_id: str = "") -> list[dict]:
    """
    Find semantically similar messages to the query.
    Returns list of {content, role, session_id, score}.
    
    If tenant_id is provided, only searches within that tenant's namespace
    (messages whose session_id starts with tenant_id + ":").
    """
    query_emb = compute_embedding(query)
    if query_emb is None:
        return []
    
    driver = get_driver()
    with driver.session() as session:
        try:
            if tenant_id:
                # Tenant-scoped search — only messages within this namespace
                prefix = tenant_id + ":"
                result = session.run(
                    "MATCH (m:Message) WHERE m.emb IS NOT NULL "
                    "  AND m.session_id STARTS WITH $prefix "
                    "RETURN m.content AS content, m.role AS role, "
                    "  m.session_id AS session_id, m.emb AS embedding "
                    "ORDER BY m.timestamp DESC LIMIT 100",
                    prefix=prefix
                )
            else:
                # Unscoped search — all messages (for personal / self-hosted use)
                result = session.run(
                    "MATCH (m:Message) WHERE m.emb IS NOT NULL "
                    "RETURN m.content AS content, m.role AS role, "
                    "  m.session_id AS session_id, m.emb AS embedding "
                    "ORDER BY m.timestamp DESC LIMIT 100"
                )
            
            from numpy import dot
            from numpy.linalg import norm
            import numpy as np
            
            scored = []
            for row in result:
                emb = row.get("embedding")
                if emb is None or not isinstance(emb, list) or len(emb) != 384:
                    continue
                q = np.array(query_emb)
                e = np.array(emb)
                cos_sim = float(dot(q, e) / (norm(q) * norm(e) + 1e-10))
                if cos_sim >= min_score:
                    scored.append({
                        "content": row.get("content", ""),
                        "role": row.get("role", ""),
                        "session_id": row.get("session_id", ""),
                        "score": round(cos_sim, 4),
                    })
            
            scored.sort(key=lambda x: x["score"], reverse=True)
            return scored[:limit]
        except Exception as e:
            logger.warning(f"Semantic search failed: {e}")
            return []


# ═══════════════════════════════════════════════════════════════════
# PHASE 3 — Reasoning Traces
# ═══════════════════════════════════════════════════════════════════

def record_decision(trace_id: str, input_text: str, model_used: str,
                    reasoning: str, outcome: str, cost: float = 0.0):
    """
    Record a structured reasoning trace in the graph.
    Creates a ThinkingNode with full provenance.
    """
    driver = get_driver()
    ts = int(time.time())
    node_id = f"decision-{ts}-{hash(trace_id) % 10000}"
    
    with driver.session() as session:
        try:
            session.run(
                "CREATE (tn:ThinkingNode {"
                "  id: $nid, trace_id: $tid, timestamp: $ts, "
                "  input: $input, model: $model, "
                "  reasoning: $reasoning, outcome: $outcome, cost: $cost"
                "}) "
                "WITH tn "
                "MATCH (os:OS {name:'Archon-One'}) "
                "MERGE (tn)-[:BELONGS_TO]->(os)",
                nid=node_id, tid=trace_id, ts=ts,
                input=input_text[:500], model=model_used,
                reasoning=reasoning[:1000], outcome=outcome[:500],
                cost=float(cost)
            )
        except Exception as e:
            logger.warning(f"Record decision failed: {e}")


def get_recent_decisions(limit: int = 10) -> list[dict]:
    """Retrieve recent reasoning decisions for audit."""
    driver = get_driver()
    with driver.session() as session:
        try:
            result = session.run(
                "MATCH (tn:ThinkingNode) "
                "RETURN tn.id, tn.trace_id, tn.timestamp, tn.input, "
                "  tn.model, tn.outcome, tn.cost "
                "ORDER BY toInteger(tn.timestamp) DESC LIMIT $limit",
                limit=int(limit)
            )
            return [
                {
                    "id": r["tn.id"],
                    "trace": r["tn.trace_id"],
                    "timestamp": r["tn.timestamp"],
                    "input": (r["tn.input"] or "")[:100],
                    "model": r["tn.model"],
                    "outcome": (r["tn.outcome"] or "")[:100],
                    "cost": r["tn.cost"],
                }
                for r in result
            ]
        except Exception as e:
            logger.warning(f"Get decisions failed: {e}")
            return []


# ═══════════════════════════════════════════════════════════════════
# PHASE 3 — Health Sentinel
# ═══════════════════════════════════════════════════════════════════

def health_sentinel() -> dict:
    """
    Run all health checks and return a summary with severity.
    Designed to be called from a cron job or heartbeat.
    """
    report = integrity_check()
    report["vector_search"] = _embedding_model is not None
    
    # Determine severity
    if report["healthy"] and _embedding_model is not None:
        report["severity"] = "ok"
    elif report["healthy"]:
        report["severity"] = "degraded"
        report["issues"] = ["Embedding model not loaded"]
    else:
        report["severity"] = "critical"
        bad_checks = {k: v for k, v in report["checks"].items() if v > 0}
        report["issues"] = [f"{k}: {v}" for k, v in bad_checks.items()]
    
    return report

if __name__ == "__main__":
    import asyncio
    
    ensure_schema()
    
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        # Run a simple test
        async def test():
            test_session = "test-session-archon"
            
            # Store test messages
            await add_message(test_session, "user", "Hello, I need to fix the Gateway architecture today")
            await add_message(test_session, "assistant", "Let me check the current Gateway setup and suggest improvements")
            await add_message(test_session, "user", "We should separate Gateway from the Brain, like the three-layer architecture")
            
            # Get context
            ctx = get_conversation_context(test_session)
            print("=== Conversation Context ===")
            print(ctx)
            
            print("\n=== Entity Extraction Test ===")
            test_text = "Today Sai and I discussed the Archon Gateway and Memgraph knowledge graph"
            entities = extract_entities(test_text)
            for e in entities:
                print(f"  {e['name']} ({e['type']}, conf={e['confidence']})")
        
        asyncio.run(test())
    
    else:
        print("ARCHON Graph Memory Layer")
        print(f"  Memgraph: {MEMGRAPH_URI}")
        print("  Commands:")
        print("    python3 graph_memory.py test")
