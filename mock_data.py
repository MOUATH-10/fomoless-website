"""
Preview data for FoMoLess while MongoDB isn't linked yet.

Documents follow the exact shape of gold.articles / the MongoDB `articles`
collection (same field names, same types), and their trend numbers are
computed with the same TR-11 / TR-12 / TR-13 / TR-14 rules as
04_gold_aggregation, so the page looks the way it will with real data.

Titles, summaries and URLs for Towards Data Science, Medium, Stack Overflow
Blog and BBC come from the pipeline's own gold table. The dev.to, Hacker News
and Stack Exchange items are generic stand-ins (those sources had no rows in
the sample) and point at each site's home page.

MockCollection implements only the slice of pymongo's Collection API that
app.py uses: find(filter, projection).sort(...).limit(n) and count_documents.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

HALF_LIFE_DAYS = 7.0
MOMENTUM = {"BBC": 3.0, "The Guardian": 3.0}  # mirrors MOMENTUM_MULTIPLIERS in 00_config
DEFAULT_MOMENTUM = 1.0
ENGAGEMENT_SOURCES = {"dev.to", "Hacker News", "Stack Exchange"}

# (Article_ID, Title, Source_name, Author, hours_ago, Url, Summary, tags, raw_upvotes, raw_comments)
_SEED = [
    (1889544093, "When Does Graph RAG Actually Add Value? A Hands-On Experiment",
     "Towards Data Science", "Arijit Ghoshal", 3,
     "https://towardsdatascience.com/when-does-graph-rag-actually-add-value-a-hands-on-experiment/",
     "I built four AI retrieval architectures on a laptop and benchmarked them against the same set of "
     "documents and questions. Here’s what the results taught me about the trade-offs between plain RAG, "
     "graph RAG, and simply putting everything into a frontier model’s context window. The post When Does "
     "Graph RAG Actually Add Value? A Hands-On Experiment appeared first on Towards Data Science .",
     ["Large Language Models", "Knowledge Graph", "Context Engineering", "Editor's Picks", "Rag", "Llm"], None, None),
    (900001, "Incremental pipelines with Delta Lake Change Data Feed, explained",
     "dev.to", "Lina M.", 5, "https://dev.to/",
     "Stop recomputing whole tables every run. A walkthrough of readChangeFeed, availableNow triggers and "
     "MERGE, with the mistakes that cost me a weekend.",
     ["dataengineering", "databricks", "deltalake", "tutorial"], 86, 14),
    (1885234929, "From Static to Dynamic Skills: A Different Model for Agent Knowledge",
     "Towards Data Science", "Tomer Mesika", 9,
     "https://towardsdatascience.com/from-static-to-dynamic-skills-a-different-model-for-agent-knowledge/",
     "Why the skill-inflation panic is aimed at the wrong thing, and what it costs to make agent knowledge a "
     "build artifact instead of a file. The post From Static to Dynamic Skills: A Different Model for Agent "
     "Knowledge appeared first on Towards Data Science .",
     ["Agentic AI", "Ai Agent", "Deep Dives", "Agent Skills", "Llm Applications", "Llm Evaluation"], None, None),
    (900003, "Show HN: A 300-line orchestrator for small data pipelines",
     "Hacker News", "pipelines_pat", 14, "https://news.ycombinator.com/",
     None, [], 212, 97),
    (900005, "How do I read a Delta table's change feed incrementally with Structured Streaming?",
     "Stack Exchange", "sparky_dev", 20, "https://stackoverflow.com/",
     None, ["apache-spark", "delta-lake", "pyspark", "databricks"], 7, 2),
    (900002, "Medallion architecture in practice: lessons from a student capstone",
     "dev.to", "Omar K.", 26, "https://dev.to/",
     "Bronze, silver, gold sounds tidy on a slide. Here is what actually broke when we built it on a shared "
     "class workspace, and how we fixed it.",
     ["dataengineering", "architecture", "beginners"], 41, 9),
    (1919162257, "Inside DolphinScheduler’s Master Startup Process: A Source Code Walkthrough",
     "Medium", "Apache DolphinScheduler", 30,
     "https://medium.com/@ApacheDolphinScheduler/inside-dolphinschedulers-master-startup-process-a-source-code-walkthrough-be42faa09742",
     "Apache DolphinScheduler is a distributed, highly extensible, and visual workflow scheduler designed for "
     "creating, scheduling, and… Continue reading on Medium »",
     ["data-science", "data-engineering", "apache-dolphinscheduler", "big-data", "open-source"], None, None),
    (900004, "Ask HN: What does your data stack look like this year?",
     "Hacker News", "curious_eng", 40, "https://news.ycombinator.com/",
     None, [], 158, 203),
    (1976865958, "AI cybersecurity is a cat and mouse game",
     "Stack Overflow Blog", "Phoebe Sajor", 52,
     "https://stackoverflow.blog/2026/09/11/ai-cybersecurity-is-a-cat-and-mouse-game/",
     "Ryan chats with Sam Curry, CSO at Zscaler, about where human intelligence sits in the new security "
     "landscape with AI, why shifting security protections closer to applications helps limit probes for "
     "vulnerabilities, and why building more resilient code infrastructure is the best way to address the "
     "vulnerabilities AI does discover.",
     [], None, None),
    (900007, "Data contracts: the boring idea that saves pipelines",
     "Medium", "Sara A.", 60, "https://medium.com/",
     "Schema drift is not a data problem, it is an agreement problem. How a one-page contract between "
     "producers and consumers cut our incident count in half. Continue reading on Medium »",
     ["data-engineering", "data-quality", "data-contracts"], None, None),
    (900006, "MongoDB bulk_write upsert is slow with many UpdateOne operations",
     "Stack Exchange", "nosql_nina", 70, "https://stackoverflow.com/",
     None, ["mongodb", "pymongo", "performance"], 4, 3),
    (1965833506, "AI, JD, and other letters of the law",
     "Stack Overflow Blog", "Phoebe Sajor", 120,
     "https://stackoverflow.blog/2026/09/15/ai-jd-and-other-letters-of-the-law/",
     "Ryan chats with Kevin Frazier, director of the AI Innovation and Law program at the University of Texas "
     "School of Law, about the legal and social impacts of data centers, the realities of workforce disruption, "
     "and regulating AI for child safety using existing consumer protection laws.",
     [], None, None),
    (1982512906, "Elevating security, control, and accessibility: Stack Internal 2026.6",
     "Stack Overflow Blog", "Carrie Koos", 200,
     "https://stackoverflow.blog/2026/09/03/security-control-and-accessibility-si-2026-6/",
     "In our 2026.6 release, we are shipping updates across administrative security, programmatic API control, "
     "developer portal integrations, and platform-wide accessibility—ensuring both your engineers and your AI "
     "agents act on verified, decision-grade knowledge.",
     [], None, None),
    (1884864731, "Tech Now", "BBC", None, 290,
     "https://www.bbc.co.uk/iplayer/episode/m0031d1p",
     "Laura Cress has the highlights from Gamescom, the biggest gaming convention in the world.",
     [], None, None),
]


def build_mock_articles(now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    docs = []
    for (article_id, title, source, author, hours_ago, url, summary, tags,
         upvotes, comments) in _SEED:
        published = now - timedelta(hours=hours_ago)
        ingested = min(published + timedelta(hours=2), now)
        # TR-11 uses DATEDIFF, i.e. whole calendar days, same as gold.
        days = (now.date() - published.date()).days
        trend_weight = min(0.5 ** (days / HALF_LIFE_DAYS), 1.0)
        momentum = MOMENTUM.get(source, DEFAULT_MOMENTUM)
        engagement = (upvotes or 0) + (comments or 0) if source in ENGAGEMENT_SOURCES else None
        docs.append({
            "Article_ID": article_id,
            "Title": title,
            "Source_name": source,
            "Author": author,
            "Published_date": published,
            "Url": url,
            "Has_tag": bool(tags),
            "Summary": summary,
            "Ingested_at": ingested,
            "tags": tags,
            "raw_upvotes": upvotes,
            "raw_comments": comments,
            "trend_weight": round(trend_weight, 4),
            "Engagement": engagement,
            "momentum_multiplier": momentum,
            "final_trend_score": round(trend_weight * momentum, 4),
        })
    return docs


# --------------------------------------------------------------------------
# Minimal pymongo look-alike
# --------------------------------------------------------------------------
def _matches(doc: dict, filt: dict) -> bool:
    for key, cond in filt.items():
        if key == "$or":
            if not any(_matches(doc, sub) for sub in cond):
                return False
            continue
        value = doc.get(key)
        if isinstance(cond, dict):
            if "$in" in cond:
                wanted = set(cond["$in"])
                if isinstance(value, list):
                    if not wanted.intersection(value):
                        return False
                elif value not in wanted:
                    return False
            if "$regex" in cond:
                flags = re.IGNORECASE if "i" in cond.get("$options", "") else 0
                if not isinstance(value, str) or not re.search(cond["$regex"], value, flags):
                    return False
        elif isinstance(value, list):
            if cond not in value:
                return False
        elif value != cond:
            return False
    return True


def _project(doc: dict, projection: dict | None) -> dict:
    if not projection:
        return dict(doc)
    return {k: doc.get(k) for k, include in projection.items() if include and k != "_id" and k in doc}


class _Cursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, key_or_list, direction=None):
        keys = [(key_or_list, direction or 1)] if isinstance(key_or_list, str) else list(key_or_list)
        # Apply keys last-to-first with stable sorts = multi-key sort.
        for field, d in reversed(keys):
            present = [x for x in self._docs if x.get(field) is not None]
            missing = [x for x in self._docs if x.get(field) is None]
            present.sort(key=lambda x: x[field], reverse=(d == -1))
            # MongoDB ranks null lowest: last when descending, first when ascending.
            self._docs = present + missing if d == -1 else missing + present
        return self

    def limit(self, n: int):
        return _Cursor(self._docs[:n]) if n else self

    def __iter__(self):
        return iter(self._docs)


class MockCollection:
    def __init__(self):
        self._docs = build_mock_articles()

    def find(self, filt: dict | None = None, projection: dict | None = None):
        filt = filt or {}
        return _Cursor(_project(d, projection) for d in self._docs if _matches(d, filt))

    def count_documents(self, filt: dict) -> int:
        return sum(1 for d in self._docs if _matches(d, filt))
