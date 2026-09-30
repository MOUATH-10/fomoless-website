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

# Team section + footer credits
TEAM = [
    {"name": "Mouath Alosaimi", "role": "Data Engineer",
     "email": "mouath1424@gmail.com", "linkedin": "https://www.linkedin.com/in/mouath-alosaimi"},
    {"name": "Azam Alzahrani", "role": "Data Engineer",
     "email": "azamalzahrani80@gmail.com", "linkedin": "https://www.linkedin.com/in/azam--alzahrani"},
]
REPO_URL = "https://github.com/AzamAlzahrani/FOMOless"
PROGRAM = "Saudi Digital Academy · Data Engineering Bootcamp 2026"

APP_ICON = Path(__file__).parent / "assets" / "fomoless-icon.png"

st.set_page_config(
    page_title="FoMoLess — Trend Radar",
    page_icon=str(APP_ICON) if APP_ICON.exists() else ":material/radar:",
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


# FoMoLess wordmark (o-m-o face), embedded so no extra file is needed.
BRAND_LOGO = "data:image/png;base64,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"


def render_nav(overview: dict) -> None:
    if MOCK_MODE:
        status = '<span class="fl-live preview"><i class="fl-dot"></i>Preview data</span>'
    else:
        status = (f'<span class="fl-live"><i class="fl-dot"></i>'
                  f'Updated {esc(riyadh_label(overview["last_refresh"]))} · Riyadh</span>')
    html_block(f"""
    <div class="fl fl-nav">
      <div class="fl-brand"><img src="{BRAND_LOGO}" alt="FoMoLess"></div>
      <nav class="fl-links">
        <a href="#spotlight">Today's #1</a><a href="#topics">Topics</a>
        <a href="#explore">Explore</a><a href="#sources">Sources</a><a href="#subscribe">Digest</a>
        <a href="#team">Team</a>
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
        <div class="fl-h1" role="heading" aria-level="1">Stop scrolling.<br><em>Start Reading, then Knowing.</em></div>
        <p class="fl-lede">Every morning, FoMoLess turns {overview["sources"]} tech sources into one trend digest
        you can read in about 5 minutes. The top articles are ranked here.</p>
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

    html_block("""
    <div class="fl fl-impact" role="group" aria-label="Time saved every day">
      <div class="fl-impact-main">
        <div class="fl-eyebrow">The result</div>
        <div class="fl-impact-nums"><s>34 min</s><span class="fl-impact-arrow">→</span><b>5 min</b></div>
        <div class="fl-impact-cap">Daily time to stay up to date: from about 34 minutes of scrolling
        across sites to about 5 minutes of reading.</div>
      </div>
      <div class="fl-impact-badge"><b>85%</b><span>less time</span></div>
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
        ("Extract", "New articles are pulled from 4 RSS feeds and 3 APIs."),
        ("Load", "The raw data lands untouched in Azure Data Lake Storage (ADLS Gen2)."),
        ("Transform", "Databricks refines it from Bronze to Silver to Gold: invalid records are set aside "
                      "with the reason, and newer articles from stronger sources score higher."),
        ("Serve", "The Gold data goes to MongoDB Atlas, which powers this page and the subscribers’ morning email."),
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
      <div class="fl-panel"><div class="fl-panel-title">How the radar works<span>ELT · daily at 06:00</span></div>{step_html}</div>
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


# Styles for the team section and footer credits. They live here (not in
# styles.css) so the whole feature is contained in this file.
TEAM_CSS = """
.fl-team { display: grid; grid-template-columns: repeat(2, 1fr); gap: 1.1rem; margin-bottom: 3.5rem; }
.fl-member {
  display: flex; gap: 1.3rem; align-items: flex-start;
  padding: 1.6rem 1.7rem;
  border: 1px solid var(--line); border-radius: 18px;
  background: linear-gradient(135deg, rgba(200, 241, 105, .06), transparent 50%), var(--surface);
  transition: border-color .2s;
}
.fl-member:hover { border-color: #3A4A43; }
.fl-avatar {
  flex: none; width: 3.6rem; height: 3.6rem; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  font-family: var(--mono); font-size: 1.05rem; font-weight: 600; letter-spacing: .04em;
  color: var(--signal-text); background: var(--signal-soft); border: 1px solid rgba(200, 241, 105, .35);
}
.fl-member-body { min-width: 0; flex: 1; }
.fl-member-name { font-family: var(--serif); font-size: 1.85rem; line-height: 1.1; color: var(--text); }
.fl-member-role {
  font-family: var(--mono); font-size: .72rem; letter-spacing: .14em; text-transform: uppercase;
  color: var(--muted); margin: .35rem 0 1.1rem;
}
.fl-member-links { display: flex; flex-wrap: wrap; gap: .55rem; }
.fl-member-links .fl-btn { padding: .5rem .95rem; font-size: .84rem; font-weight: 500; }
.fl-member-links .fl-btn { gap: .55rem; }
.fl-logo { display: block; width: 20px; height: 20px; flex: none; border-radius: 3px; }
.fl-logo.gmail { width: 20px; height: 15px; border-radius: 0; }
.fl-member-links .fl-linkedin {
  background: #0A66C2; color: #FFFFFF !important; font-weight: 600;
  border: 1px solid #0A66C2; box-shadow: 0 4px 14px rgba(10, 102, 194, .35);
  transition: background .2s, transform .2s;
}
.fl-member-links .fl-linkedin:hover { background: #0B77E0; border-color: #0B77E0; color: #FFFFFF !important; transform: translateY(-1px); }
.fl-member-links .fl-btn:hover { border-color: var(--signal); color: var(--signal-text) !important; }
.fl-foot-credit { flex-basis: 100%; color: var(--dim); line-height: 1.7; }
.fl-foot-credit a { color: var(--muted) !important; }
.fl-foot-credit a:hover { color: var(--signal-text) !important; }
@media (max-width: 900px) {
  .fl-team { grid-template-columns: 1fr; }
  .fl-member { padding: 1.3rem 1.2rem; }
}
"""

# Official Gmail / LinkedIn logos (supplied by the team), embedded so no extra files are needed.
GMAIL_LOGO = "data:image/png;base64,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"
LINKEDIN_LOGO = "data:image/png;base64,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"


def initials(name: str) -> str:
    return "".join(part[0] for part in name.split()[:2]).upper()


def no_autolink(text: str) -> str:
    """Escapes text and encodes '@' so Streamlit's markdown doesn't wrap an
    email address in a second, nested link."""
    return esc(text).replace("@", "&#64;")


def render_team() -> None:
    cards = "".join(f"""
      <div class="fl-member">
        <div class="fl-avatar" aria-hidden="true">{esc(initials(m["name"]))}</div>
        <div class="fl-member-body">
          <div class="fl-member-name" role="heading" aria-level="3">{esc(m["name"])}</div>
          <div class="fl-member-role">{esc(m["role"])}</div>
          <div class="fl-member-links">
            <a class="fl-btn fl-btn-ghost" href="mailto:{esc(m["email"])}"><img class="fl-logo gmail" src="{GMAIL_LOGO}" alt="Gmail">{no_autolink(m["email"])}</a>
            <a class="fl-btn fl-linkedin" href="{esc(safe_url(m["linkedin"]))}" target="_blank" rel="noopener" aria-label="{esc(m["name"])} on LinkedIn"><img class="fl-logo" src="{LINKEDIN_LOGO}" alt="">Connect on LinkedIn</a>
          </div>
        </div>
      </div>""" for m in TEAM)
    html_block(f"""
    <div class="fl fl-section" id="team">
      <div class="fl-eyebrow">The team</div>
      <div class="fl-h2" role="heading" aria-level="2">The people behind the radar</div>
      <div class="fl-sub">FoMoLess is our team project at the Saudi Digital Academy Data Engineering Bootcamp.
      Questions, feedback or opportunities? We’d love to hear from you.</div>
    </div>
    <div class="fl fl-team">{cards}</div>""")


def render_footer(overview: dict) -> None:
    names = " &amp; ".join(
        f'<a href="{esc(safe_url(m["linkedin"]))}" target="_blank" rel="noopener">{esc(m["name"])}</a>'
        for m in TEAM
    )
    refreshed = "" if MOCK_MODE else " · last run " + esc(riyadh_label(overview["last_refresh"]))
    html_block(f"""
    <div class="fl fl-foot">
      <span>© {datetime.now(RIYADH).year} FoMoLess</span>
      <span>Azure Data Factory · ADLS Gen2 · Databricks · PySpark · Delta Lake · MongoDB Atlas · Logic Apps · Streamlit</span>
      <span>Data refreshed daily{refreshed}</span>
      <div class="fl-foot-credit">Built by {names} · {esc(PROGRAM)} ·
        <a href="{esc(REPO_URL)}" target="_blank" rel="noopener">Source code on GitHub ↗</a></div>
      <div class="fl-foot-credit fl-foot-partners">Saudi Digital Academy · WeCloudData · The Year of AI 2026</div>
    </div>""")


# =============================================================================
# 5. PAGE
# =============================================================================
st.markdown(f"<style>{(Path(__file__).parent / 'styles.css').read_text()}{TEAM_CSS}</style>", unsafe_allow_html=True)

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
      <div class="fl-sub-h" role="heading" aria-level="2">About five minutes every morning. Zero tabs.</div>
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

# ---- Team + footer ----
render_team()
render_footer(overview)
