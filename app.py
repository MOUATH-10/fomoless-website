"""
FoMoLess — Trend Radar (Streamlit front end)

Reads the curated `articles` collection that 06_load_mongodb keeps in sync
with gold.articles, and presents it as a daily trend radar.

Layout of this file:
  1. Settings            <- MOCK_MODE switch lives here
  2. Data layer          <- the ONLY section that talks to a database
  3. Pure helpers        <- query building, tag normalisation, stats
  4. HTML components     <- every block of custom markup
  5. Page                <- assembles the page top to bottom

Run:  streamlit run app.py
"""

from __future__ import annotations

import html
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st

# =============================================================================
# 1. SETTINGS
# =============================================================================
MOCK_MODE = False  # <- set to False to read from MongoDB (settings in .streamlit/secrets.toml)

MONGO_COLLECTION = "articles"
PAGE_STEP = 6
RIYADH = ZoneInfo("Asia/Riyadh")

CANONICAL_SOURCES = [
    "dev.to", "Medium", "Towards Data Science", "BBC", "Hacker News",
    "Stack Overflow Blog", "Stack Exchange", "KDnuggets", "The Guardian",
]
SOURCE_COLORS = {
    "dev.to": "#9B8CFF", "Medium": "#E8E6DF", "Towards Data Science": "#5BC0EB",
    "BBC": "#FF6B6B", "Hacker News": "#FF9F43", "Stack Overflow Blog": "#F4C542",
    "Stack Exchange": "#4FD1C5", "KDnuggets": "#7BD88F", "The Guardian": "#5B8DEF",
}
# label -> MongoDB sort spec (ties broken by the next key)
SORT_OPTIONS = {
    "Trending now": [("final_trend_score", -1), ("Published_date", -1)],
    "Newest first": [("Published_date", -1)],
    "Most discussed": [("Engagement", -1), ("final_trend_score", -1)],
}

# Tag clean-up for display: sources spell the same topic differently.
TAG_ALIASES = {
    "dataengineering": "data engineering", "deltalake": "delta lake",
    "datascience": "data science", "bigdata": "big data",
    "machinelearning": "machine learning", "ai agent": "ai agents",
}
IGNORED_TAGS = {"editor's picks", "deep dives"}
ACRONYMS = {"ai", "llm", "llms", "rag", "sql", "etl", "elt", "api", "ml", "gpu", "cdf"}

st.set_page_config(
    page_title="FoMoLess — Trend Radar",
    page_icon=":material/radar:",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# =============================================================================
# 2. DATA LAYER — the only section that knows where the data lives
# =============================================================================
CATALOG_FIELDS = {
    "_id": 0, "Article_ID": 1, "Source_name": 1, "tags": 1,
    "final_trend_score": 1, "Published_date": 1, "Ingested_at": 1,
}
CARD_FIELDS = {
    "_id": 0, "Article_ID": 1, "Title": 1, "Source_name": 1, "Author": 1,
    "Published_date": 1, "Url": 1, "Summary": 1, "tags": 1, "Engagement": 1,
    "trend_weight": 1, "momentum_multiplier": 1, "final_trend_score": 1,
}


SUBSCRIBERS_COLLECTION = "subscribers"
NUMERIC_FIELDS = ("final_trend_score", "trend_weight", "momentum_multiplier", "Engagement")


class DataUnavailable(Exception):
    """A database problem, already translated into a plain-language explanation."""


def _secrets() -> dict:
    try:
        return dict(st.secrets)
    except Exception:  # no secrets.toml at all
        return {}


def _connection_uri() -> str:
    """Option A: one full connection string (`mongo_uri`).
    Option B: separate fields, which get percent-encoded here."""
    secrets = _secrets()
    if secrets.get("mongo_uri"):
        return str(secrets["mongo_uri"]).strip()
    if all(secrets.get(k) for k in ("mongo_user", "mongo_password", "mongo_cluster")):
        import urllib.parse
        user = urllib.parse.quote_plus(secrets["mongo_user"])
        password = urllib.parse.quote_plus(secrets["mongo_password"])
        return (f"mongodb+srv://{user}:{password}@{secrets['mongo_cluster']}/"
                f"?retryWrites=true&w=majority&authSource=admin")
    raise DataUnavailable(
        "No MongoDB settings found. Create .streamlit/secrets.toml next to app.py "
        "(copy secrets.toml.example) and put your connection string in mongo_uri."
    )


def _explain(exc: Exception) -> DataUnavailable:
    """Turns driver errors into something actionable. Never echoes the connection string."""
    if isinstance(exc, DataUnavailable):
        return exc
    name = type(exc).__name__
    code = getattr(exc, "code", None)
    text = str(exc).lower()
    if code in (18, 8000) or ("auth" in text and "fail" in text):
        return DataUnavailable(
            "MongoDB rejected the username or password. Re-copy the password into secrets.toml; "
            "inside a full connection string, characters like @ : / % must be percent-encoded.")
    if code == 13 or "not authorized" in text:
        return DataUnavailable(
            f"Connected, but this database user isn't allowed to do that on the '{_db_name()}' database. "
            "Ask for the 'read' role (or 'readWrite' to save subscribers) on it.")
    if name == "ServerSelectionTimeoutError":
        return DataUnavailable(
            "Couldn't reach the cluster within 8 seconds. The usual cause is that your current IP "
            "address isn't on the cluster's Network Access list, so ask whoever manages Atlas to add it.")
    if name in ("ConfigurationError", "InvalidURI"):
        return DataUnavailable(
            "The connection string is malformed. It should start with mongodb+srv:// and contain "
            "no placeholder like <password>.")
    return DataUnavailable(f"Database error ({name}{f', code {code}' if code else ''}).")


def _db_name() -> str:
    return str(_secrets().get("mongo_db", "fomoless"))


@st.cache_resource(show_spinner=False)
def get_database():
    """One client per server process. The ping makes a bad setting fail here, loudly."""
    from pymongo import MongoClient
    client = MongoClient(_connection_uri(), tz_aware=True,
                         serverSelectionTimeoutMS=8000, appname="fomoless-web")
    client.admin.command("ping")
    return client[_db_name()]


@st.cache_resource(show_spinner=False)
def _mock_collection():
    from mock_data import MockCollection
    return MockCollection()


def get_collection():
    if MOCK_MODE:
        return _mock_collection()
    return get_database()[str(_secrets().get("mongo_collection", MONGO_COLLECTION))]


def _number(value):
    """Gold writes decimal(10,4); depending on the loader that arrives as Decimal128,
    Decimal, float or int. The page only ever sees float or None."""
    if value is None:
        return None
    if hasattr(value, "to_decimal"):  # bson.Decimal128
        value = value.to_decimal()
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


def _clean(doc: dict) -> dict:
    for field in NUMERIC_FIELDS:
        if field in doc:
            doc[field] = _number(doc[field])
    return doc


@st.cache_data(ttl=600, show_spinner=False)
def load_catalog() -> list[dict]:
    """Light projection of every article: feeds the stats, radar, topics and filter options."""
    try:
        return [_clean(d) for d in get_collection().find({}, CATALOG_FIELDS)]
    except Exception as exc:
        raise _explain(exc) from exc


@st.cache_data(ttl=600, show_spinner=False)
def load_feed(query: dict, sort: list, limit: int) -> list[dict]:
    """Filtering, sorting and limiting all happen inside the database."""
    try:
        return [_clean(d) for d in get_collection().find(query, CARD_FIELDS).sort(sort).limit(limit)]
    except Exception as exc:
        raise _explain(exc) from exc


@st.cache_data(ttl=600, show_spinner=False)
def count_matches(query: dict) -> int:
    try:
        return get_collection().count_documents(query)
    except Exception as exc:
        raise _explain(exc) from exc


def save_subscriber(email: str) -> str:
    """Upserts on email, so subscribing twice never creates a duplicate. Returns 'new' or 'existing'."""
    try:
        subscribers = get_database()[SUBSCRIBERS_COLLECTION]
        try:
            subscribers.create_index("email", unique=True)  # idempotent; skipped if not permitted
        except Exception:
            pass
        result = subscribers.update_one(
            {"email": email},
            {"$setOnInsert": {"subscribed_at": datetime.now(timezone.utc), "source": "website"},
             "$set": {"active": True}},
            upsert=True,
        )
        return "new" if result.upserted_id is not None else "existing"
    except Exception as exc:
        raise _explain(exc) from exc


# =============================================================================
# 3. PURE HELPERS
# =============================================================================
esc = html.escape


def as_utc(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None


def relative_time(value, now: datetime) -> str:
    dt = as_utc(value)
    if dt is None:
        return "date unknown"
    seconds = max((now - dt).total_seconds(), 0)
    if seconds < 3600:
        return f"{max(int(seconds // 60), 1)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    days = int(seconds // 86400)
    return "yesterday" if days == 1 else f"{days}d ago"


def riyadh_label(value) -> str:
    dt = as_utc(value)
    return dt.astimezone(RIYADH).strftime("%b %d, %H:%M") if dt else "—"


def safe_url(url) -> str:
    return url if isinstance(url, str) and url.startswith(("https://", "http://")) else "#"


def display_summary(text, limit: int = 280) -> str:
    """Strips feed boilerplate the sources append to every summary."""
    if not text:
        return ""
    text = re.sub(r"\s*The post .{0,300}? appeared first on .{0,80}$", "", str(text))
    text = re.sub(r"\s*Continue reading on .{0,60}»\s*$", "", text).strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def topic_key(tag) -> str | None:
    key = re.sub(r"[\s_\-]+", " ", str(tag).strip().lower())
    key = TAG_ALIASES.get(key, key)
    return None if not key or key in IGNORED_TAGS else key


def topic_label(key: str) -> str:
    return " ".join(w.upper() if w in ACRONYMS else w.capitalize() for w in key.split())


def build_topic_index(catalog: list[dict]) -> dict:
    """topic key -> {label, raw tags, article count, summed trend score}."""
    index: dict[str, dict] = {}
    for doc in catalog:
        seen = set()
        for raw in doc.get("tags") or []:
            key = topic_key(raw)
            if not key:
                continue
            entry = index.setdefault(key, {"label": topic_label(key), "raw": set(), "count": 0, "score": 0.0})
            entry["raw"].add(raw)
            if key not in seen:
                entry["count"] += 1
                entry["score"] += float(doc.get("final_trend_score") or 0)
                seen.add(key)
    return dict(sorted(index.items(), key=lambda kv: (-kv[1]["score"], -kv[1]["count"], kv[0])))


def build_overview(catalog: list[dict], topics: dict, now: datetime) -> dict:
    published = [as_utc(d.get("Published_date")) for d in catalog]
    ingested = [as_utc(d.get("Ingested_at")) for d in catalog]
    return {
        "articles": len(catalog),
        "fresh_24h": sum(1 for p in published if p and now - p <= timedelta(hours=24)),
        "sources": len({d.get("Source_name") for d in catalog if d.get("Source_name")}),
        "topics": len(topics),
        "last_refresh": max((i for i in ingested if i), default=None),
        "max_score": max((float(d.get("final_trend_score") or 0) for d in catalog), default=0.0) or 1.0,
    }


def build_query(search: str, sources: list[str], topic_keys: list[str], topics: dict) -> dict:
    """UI selections -> MongoDB filter. Empty selection means 'no restriction'."""
    query: dict = {}
    if sources:
        query["Source_name"] = {"$in": sorted(sources)}
    if topic_keys:
        raw = sorted({r for k in topic_keys for r in topics.get(k, {}).get("raw", ())})
        query["tags"] = {"$in": raw}
    term = (search or "").strip()
    if term:
        # re.escape: typed text is matched literally, never run as a regex
        pattern = re.escape(term)
        query["$or"] = [
            {"Title": {"$regex": pattern, "$options": "i"}},
            {"Summary": {"$regex": pattern, "$options": "i"}},
        ]
    return query


# =============================================================================
# 4. HTML COMPONENTS
# =============================================================================
def html_block(markup: str) -> None:
    # Collapse to one line: Streamlit's markdown turns indented or
    # blank-line-separated HTML into code blocks.
    st.markdown(" ".join(line.strip() for line in markup.splitlines() if line.strip()),
                unsafe_allow_html=True)


def source_chip(name: str) -> str:
    color = SOURCE_COLORS.get(name, "#A3ADA7")
    return f'<span class="fl-src"><i style="background:{color}"></i>{esc(name)}</span>'


def tag_chips(tags, limit: int = 4) -> str:
    keys, out = set(), []
    for raw in tags or []:
        key = topic_key(raw)
        if key and key not in keys:
            keys.add(key)
            out.append(f'<span class="fl-tag">#{esc(key.replace(" ", "-"))}</span>')
    return f'<div class="fl-tags">{"".join(out[:limit])}</div>' if out else ""


BRAND_MARK = (
    '<svg viewBox="0 0 32 32" aria-hidden="true"><circle cx="16" cy="16" r="14" fill="none" stroke="#24302B" stroke-width="1.5"/>'
    '<circle cx="16" cy="16" r="8.5" fill="none" stroke="#24302B" stroke-width="1.5"/>'
    '<path d="M16 16 L16 2 A14 14 0 0 1 28.1 9 Z" fill="#C8F169" fill-opacity=".35"/>'
    '<circle cx="22" cy="10.5" r="2.2" fill="#C8F169"/><circle cx="16" cy="16" r="1.8" fill="#ECEAE2"/></svg>'
)


def render_nav(overview: dict) -> None:
    if MOCK_MODE:
        status = '<span class="fl-live preview"><i class="fl-dot"></i>Preview data</span>'
    else:
        status = (f'<span class="fl-live"><i class="fl-dot"></i>'
                  f'Updated {esc(riyadh_label(overview["last_refresh"]))} · Riyadh</span>')
    html_block(f"""
    <div class="fl fl-nav">
      <div class="fl-brand">{BRAND_MARK}FoMoLess</div>
      <nav class="fl-links">
        <a href="#spotlight">Today's #1</a><a href="#topics">Topics</a>
        <a href="#explore">Explore</a><a href="#sources">Sources</a><a href="#subscribe">Digest</a>
      </nav>
      {status}
    </div>""")


def radar_blips(topics: dict, n: int = 6) -> str:
    top = list(topics.values())[:n]
    if not top:
        return ""
    best = top[0]["score"] or 1.0
    out = []
    for i, t in enumerate(top):
        heat = t["score"] / best
        radius = 12 + (1 - heat) * 30  # hotter topics sit closer to the centre
        angle = math.radians(-70 + i * (360 / len(top)))
        x, y = 50 + radius * math.cos(angle), 50 + radius * math.sin(angle)
        cls = "fl-blip" + (" hot" if i == 0 else "") + (" left" if x > 62 else "")
        out.append(f'<div class="{cls}" style="left:{x:.1f}%;top:{y:.1f}%"><i></i><span>{esc(t["label"])}</span></div>')
    return "".join(out)


def render_hero(overview: dict, topics: dict) -> None:
    html_block(f"""
    <div class="fl fl-hero">
      <div>
        <div class="fl-h1" role="heading" aria-level="1">Catch it <em>before</em> it’s an interview question.</div>
        <p class="fl-lede">Every morning, FoMoLess collects new articles from {overview["sources"]} tech sources
        and ranks them by how recent they are and how reliable their source is. The top articles are listed here.</p>
        <div class="fl-hero-cta">
          <a class="fl-btn fl-btn-primary" href="#explore">Explore today’s feed →</a>
          <a class="fl-btn fl-btn-ghost" href="#subscribe">Get the daily digest</a>
        </div>
      </div>
      <div class="fl-radar-wrap">
        <div class="fl-radar" role="img" aria-label="Radar of today's hottest topics">
          <div class="fl-sweep"></div><div class="fl-core"></div>{radar_blips(topics)}
        </div>
        <div class="fl-radar-note">closer to the centre = hotter right now</div>
      </div>
    </div>""")

    html_block(f"""
    <div class="fl fl-stats">
      <div class="fl-stat"><b>{overview["articles"]:,}</b><span>Articles on the radar</span></div>
      <div class="fl-stat"><b>{overview["fresh_24h"]:,}</b><span>New in the last 24 hours</span></div>
      <div class="fl-stat"><b>{overview["sources"]}</b><span>Sources reporting</span></div>
      <div class="fl-stat"><b>{overview["topics"]:,}</b><span>Topics in motion</span></div>
    </div>""")



def render_spotlight(doc: dict, now: datetime) -> None:
    author = f'<span>by {esc(doc["Author"])}</span>' if doc.get("Author") else ""
    summary = display_summary(doc.get("Summary"), 360)

    html_block(f"""
    <div class="fl fl-section" id="spotlight">
      <div class="fl-eyebrow">Today’s #1 signal</div>
      <div class="fl-h2" role="heading" aria-level="2">If you read one thing today</div>
    </div>
    <div class="fl fl-spot">
      <div>
        <div class="fl-meta"><span class="fl-rank">#1 TRENDING</span>{source_chip(doc.get("Source_name", ""))}
          <span>{esc(relative_time(doc.get("Published_date"), now))}</span>{author}</div>
        <div class="fl-spot-title"><a href="{esc(safe_url(doc.get("Url")))}" target="_blank" rel="noopener">{esc(doc.get("Title", ""))}</a></div>
        {f'<p class="fl-spot-sum">{esc(summary)}</p>' if summary else ""}
        {tag_chips(doc.get("tags"), 6)}
        <a class="fl-btn fl-btn-primary" href="{esc(safe_url(doc.get("Url")))}" target="_blank" rel="noopener">Read the article ↗</a>
      </div>
    </div>""")


def render_topics_and_method(topics: dict) -> None:
    top = list(topics.values())[:5]
    best = top[0]["score"] if top and top[0]["score"] else 1.0
    rows = "".join(
        f'<div class="fl-topic"><span class="n">{i:02d}</span><span class="name">{esc(t["label"])}</span>'
        f'<div class="fl-bar"><b style="width:{max(t["score"] / best * 100, 4):.0f}%"></b></div>'
        f'<span class="v">{t["count"]} art.</span></div>'
        for i, t in enumerate(top, 1)
    ) or '<div class="fl-sub">No tagged articles yet.</div>'
    steps = [
        ("Collect", "New articles are pulled from public RSS feeds and APIs."),
        ("Validate", "Each article is checked, and invalid records are kept aside with the reason."),
        ("Score", "Newer articles and articles from stronger sources get a higher score."),
        ("Deliver", "This page is updated daily, and subscribers get a morning email."),
    ]
    step_html = "".join(
        f'<div class="fl-step"><i>{i:02d}</i><div><b>{name}</b><span>{text}</span></div></div>'
        for i, (name, text) in enumerate(steps, 1)
    )
    html_block(f"""
    <div class="fl fl-section" id="topics">
      <div class="fl-eyebrow">Rising topics</div>
      <div class="fl-h2" role="heading" aria-level="2">What the field is talking about</div>
      <div class="fl-sub">The five topics with the highest total score across today's articles.</div>
    </div>
    <div class="fl fl-grid-2">
      <div class="fl-panel"><div class="fl-panel-title">Topic leaderboard<span>by trend score</span></div>{rows}</div>
      <div class="fl-panel"><div class="fl-panel-title">How the radar works<span>daily at 06:00</span></div>{step_html}</div>
    </div>""")


def article_card(doc: dict, rank: int | None, max_score: float, now: datetime) -> str:
    score = float(doc.get("final_trend_score") or 0)
    pct = max(min(score / max_score * 100, 100), 3)
    hot = '<span class="fl-hot">HOT</span>' if rank is not None and rank <= 3 else ""
    engagement = doc.get("Engagement")
    right_meta = f"{int(engagement):,} interactions" if engagement is not None else "editorial"
    summary = display_summary(doc.get("Summary"))
    if not summary and engagement is not None:
        summary = f"{int(engagement):,} people have upvoted or replied to this on {doc.get('Source_name', 'the source')} so far."
    url = esc(safe_url(doc.get("Url")))
    return f"""
    <article class="fl-card">
      <div class="fl-card-top"><span>{source_chip(doc.get("Source_name", ""))}{hot}</span>
        <span>{esc(relative_time(doc.get("Published_date"), now))}</span></div>
      <div class="fl-card-title"><a href="{url}" target="_blank" rel="noopener">{esc(doc.get("Title", ""))}</a></div>
      {f'<p class="fl-card-sum">{esc(summary)}</p>' if summary else ""}
      {tag_chips(doc.get("tags"), 3)}
      <div class="fl-card-foot">
        <div class="fl-meter"><div class="fl-bar"><b style="width:{pct:.0f}%"></b></div>
          <small><span>trend {score:.2f}</span><span>{right_meta}</span></small></div>
        <a class="fl-read" href="{url}" target="_blank" rel="noopener">Read ↗</a>
      </div>
    </article>"""


def render_sources(catalog: list[dict]) -> None:
    counts: dict[str, int] = {}
    for d in catalog:
        counts[d.get("Source_name")] = counts.get(d.get("Source_name"), 0) + 1
    tiles = []
    for name in sorted((s for s in CANONICAL_SOURCES if counts.get(s)), key=lambda s: -counts[s]):
        n = counts[name]
        tiles.append(
            f'<div class="fl-source">{source_chip(name)}'
            f'<div class="row"><b>{n:,}</b><small>{"article" if n == 1 else "articles"}</small></div></div>'
        )
    html_block(f"""
    <div class="fl fl-section" id="sources">
      <div class="fl-eyebrow">Sources</div>
      <div class="fl-h2" role="heading" aria-level="2">Where the signal comes from</div>
      <div class="fl-sub">All articles come from public RSS feeds and APIs.</div>
    </div>
    <div class="fl fl-sources">{"".join(tiles)}</div>""")


# =============================================================================
# 5. PAGE
# =============================================================================
st.markdown(f"<style>{(Path(__file__).parent / 'styles.css').read_text()}</style>", unsafe_allow_html=True)

def render_data_error(message: str) -> None:
    html_block(f"""
    <div class="fl fl-empty" style="text-align:left;border-color:rgba(255,138,101,.45)">
      <div class="fl-eyebrow" style="color:var(--hot)">Database connection</div>
      <div class="fl-sub-h" role="heading" aria-level="2">Can’t load articles right now</div>
      <div class="fl-sub-p" style="margin-bottom:0">{esc(message)}</div>
    </div>""")


def fetch(loader, *args):
    """Runs a data-layer call; on failure shows the explanation and stops the page."""
    try:
        return loader(*args)
    except DataUnavailable as exc:
        render_data_error(str(exc))
        st.stop()


now = datetime.now(timezone.utc)
catalog = fetch(load_catalog)
topics = build_topic_index(catalog)
overview = build_overview(catalog, topics, now)

render_nav(overview)
render_hero(overview, topics)

if not catalog:
    html_block('<div class="fl fl-empty">No articles yet — the radar fills up after the first pipeline run.</div>')
    st.stop()

top_article = fetch(load_feed, {}, SORT_OPTIONS["Trending now"], 1)
if top_article:
    render_spotlight(top_article[0], now)

render_topics_and_method(topics)

# ---- Explore: native widgets drive a MongoDB query ----
html_block("""
<div class="fl fl-section" id="explore">
  <div class="fl-eyebrow">Explore</div>
  <div class="fl-h2" role="heading" aria-level="2">Everything on the radar</div>
  <div class="fl-sub">Search, narrow by source or topic, and sort. Filtering runs inside the database, not in the browser.</div>
</div>""")

source_counts: dict[str, int] = {}
for d in catalog:
    source_counts[d.get("Source_name")] = source_counts.get(d.get("Source_name"), 0) + 1
source_options = sorted((s for s in source_counts if s), key=lambda s: -source_counts[s])

with st.container(key="filterbar"):
    c1, c2, c3, c4 = st.columns([2.3, 2, 2, 1.3], vertical_alignment="bottom")
    search = c1.text_input("Search", placeholder="Try “delta”, “RAG”, “orchestrator”…", key="f_search")
    sources_sel = c2.multiselect(
        "Sources", source_options, placeholder="All sources", key="f_sources",
        format_func=lambda s: f"{s} · {source_counts[s]}",
    )
    topics_sel = c3.multiselect(
        "Topics", list(topics), placeholder="All topics", key="f_topics",
        format_func=lambda k: f'{topics[k]["label"]} · {topics[k]["count"]}',
    )
    sort_label = c4.selectbox("Sort by", list(SORT_OPTIONS), key="f_sort")

query = build_query(search, sources_sel, topics_sel, topics)

# Reset "load more" whenever the filters change.
signature = (search, tuple(sources_sel), tuple(topics_sel), sort_label)
if st.session_state.get("feed_signature") != signature:
    st.session_state["feed_signature"] = signature
    st.session_state["feed_limit"] = PAGE_STEP
limit = st.session_state["feed_limit"]

articles = fetch(load_feed, query, SORT_OPTIONS[sort_label], limit)
total = fetch(count_matches, query)
ranked = sort_label == "Trending now" and not query

html_block(f'<div class="fl fl-count">Showing {len(articles)} of {total} articles</div>')
if articles:
    cards = "".join(
        article_card(doc, i if ranked else None, overview["max_score"], now)
        for i, doc in enumerate(articles, 1)
    )
    html_block(f'<div class="fl fl-cards">{cards}</div>')
else:
    html_block('<div class="fl fl-empty">Nothing matches those filters. Try a broader search or clear a filter.</div>')

if total > len(articles):
    def _more():
        st.session_state["feed_limit"] += PAGE_STEP
    _, mid, _ = st.columns([2, 1.2, 2])
    mid.button(f"Load {min(PAGE_STEP, total - len(articles))} more", on_click=_more, width="stretch")

st.write("")
render_sources(catalog)

# ---- Subscribe ----
with st.container(key="subscribe"):
    html_block("""
    <div class="fl" id="subscribe">
      <div class="fl-eyebrow">Daily digest</div>
      <div class="fl-sub-h" role="heading" aria-level="2">Three minutes every morning. Zero tabs.</div>
      <div class="fl-sub-p">The top trends and the articles behind them, in your inbox at 06:00 Riyadh time.
      One email a day, unsubscribe in one click.</div>
    </div>""")
    with st.form("subscribe_form", border=False, clear_on_submit=True):
        f1, f2 = st.columns([3, 1.3], vertical_alignment="bottom")
        email = f1.text_input("Email address", placeholder="you@company.com", label_visibility="collapsed")
        submitted = f2.form_submit_button("Get the daily digest", type="primary", width="stretch")
    if submitted:
        email = (email or "").strip()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]{2,}", email):
            st.error("That doesn’t look like a valid email address.")
        elif MOCK_MODE:
            st.success(f"Thanks — {email} is noted for this preview. Saving subscribers goes live when MongoDB is linked.")
        else:
            try:
                outcome = save_subscriber(email.lower())
            except DataUnavailable as exc:
                st.error(f"Couldn’t save your email: {exc}")
            else:
                if outcome == "new":
                    st.success(f"You’re in. The first digest goes to {email} at 06:00 Riyadh time.")
                else:
                    st.info(f"{email} is already subscribed. See you at 06:00.")

html_block(f"""
<div class="fl fl-foot">
  <span>FoMoLess</span>
  <span>Databricks · Delta Lake · Azure Data Factory · MongoDB Atlas</span>
  <span>Data refreshed daily{'' if MOCK_MODE else ' · last run ' + esc(riyadh_label(overview["last_refresh"]))}</span>
</div>""")
