"""
topic-engine: Daily research pipeline that surfaces high-opportunity AI/productivity
video topics where SEARCH DEMAND is high but EXISTING CONTENT is weak.

Daily flow:
  1. Discover candidate problems from Reddit (r/ChatGPT, r/ClaudeAI, r/singularity,
     r/Productivity, r/ArtificialIntelligence, r/OpenAI, r/MachineLearning)
  2. Filter to "people are stuck / asking how to" patterns
  3. For each candidate, score DEMAND (Reddit metrics, recency, repetition)
  4. For each candidate, score SUPPLY (YouTube search: count of results, recency,
     view counts, relevance) using YouTube Data API search
  5. Compute opportunity = demand / supply
  6. Gemini ranks and writes a one-page brief for the top 5: the actual question,
     the pain point, what existing content misses, suggested angle, script outline
  7. Emit a daily Markdown report committed to the repo + (optional) email/notify

You wake up, open today's report, pick one. The brief tells you what to film.

Required env:
  GEMINI_API_KEY        Google AI Studio
  YT_API_KEY            Simple YouTube Data API key (NOT OAuth, just a read key)
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from urllib.parse import quote_plus

import requests
import yaml
from google import genai
from google.genai import types as genai_types

ROOT = Path(__file__).parent.resolve()
REPORTS_DIR = ROOT / "reports"
STATE_FILE = ROOT / "state.json"
CONFIG_FILE = ROOT / "config.yaml"

# ----------------------------- config + state -----------------------------

def load_config() -> dict:
    with CONFIG_FILE.open() as f:
        return yaml.safe_load(f)

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"surfaced_topics": []}  # keep recent so we don't resurface same topic

def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))

# ----------------------------- data model -----------------------------

@dataclass
class Candidate:
    title: str
    url: str
    subreddit: str
    upvotes: int
    num_comments: int
    age_days: float
    body_snippet: str = ""
    # filled in later:
    inferred_query: str = ""             # what someone would search on YouTube
    demand_score: float = 0.0
    supply_score: float = 0.0
    opportunity_score: float = 0.0
    yt_competitors: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

# ----------------------------- demand: reddit harvest -----------------------------

# "How do I" / "is there a / an AI" / pain-point patterns
PAIN_PATTERNS = re.compile(
    r"\b("
    r"how (?:do|can|to) (?:i|you)|"
    r"what(?:'s| is) the best|"
    r"is there an? (?:ai|tool|way)|"
    r"i (?:can't|cant|cannot|am struggl|am stuck|need help|need a)|"
    r"recommend(?:ation)?s? for|"
    r"alternatives? to|"
    r"better than chatgpt|"
    r"workflow for|"
    r"prompt(?:s)? for|"
    r"automate"
    r")\b",
    re.I,
)

NEGATIVE_PATTERNS = re.compile(
    r"\b("
    r"meme|joke|funny|drama|controvers|"
    r"banned|hate|lol|wtf|"
    r"sam altman|elon musk|"  # commentary topics, not problems
    r"benchmark|leaderboard|"
    r"announced|launches|releases|just dropped"  # news, saturated
    r")\b",
    re.I,
)

DEFAULT_SUBREDDITS = [
    "ChatGPT",
    "ClaudeAI",
    "OpenAI",
    "ArtificialInteligence",
    "ArtificialIntelligence",
    "LocalLLaMA",
    "PromptEngineering",
    "Productivity",
    "GetStudying",
    "Notion",
    "automation",
    "selfhosted",
    "Entrepreneur",
]

def fetch_reddit(subreddit: str, period: str = "week", limit: int = 50) -> list[Candidate]:
    out: list[Candidate] = []
    try:
        resp = requests.get(
            f"https://www.reddit.com/r/{subreddit}/top.json?t={period}&limit={limit}",
            headers={"User-Agent": "topic-engine/1.0"},
            timeout=20,
        )
        if resp.status_code != 200:
            print(f"[warn] reddit r/{subreddit}: HTTP {resp.status_code}", file=sys.stderr)
            return out
        now = dt.datetime.now(dt.timezone.utc).timestamp()
        for post in resp.json().get("data", {}).get("children", []):
            d = post.get("data", {})
            title = (d.get("title") or "").strip()
            body = (d.get("selftext") or "")[:1500]
            if not title:
                continue
            combined = title + " " + body
            if NEGATIVE_PATTERNS.search(combined):
                continue
            # we WANT pain-point posts; but if the post is highly upvoted and
            # in r/Productivity, even without a pattern hit it might be relevant
            has_pattern = bool(PAIN_PATTERNS.search(combined))
            upvotes = int(d.get("score", 0))
            comments = int(d.get("num_comments", 0))
            # Keep it if it matches pattern OR has strong engagement
            if not has_pattern and not (upvotes > 200 and comments > 30):
                continue
            created = d.get("created_utc", now)
            age_days = max(0.0, (now - created) / 86400.0)
            out.append(Candidate(
                title=title,
                url=f"https://reddit.com{d.get('permalink', '')}",
                subreddit=subreddit,
                upvotes=upvotes,
                num_comments=comments,
                age_days=age_days,
                body_snippet=body[:800],
            ))
    except Exception as e:
        print(f"[warn] reddit r/{subreddit} fetch: {e}", file=sys.stderr)
    return out

def gather_candidates(cfg: dict) -> list[Candidate]:
    subs = cfg.get("subreddits", DEFAULT_SUBREDDITS)
    cands: list[Candidate] = []
    for sub in subs:
        cands += fetch_reddit(sub, period="week", limit=50)
        time.sleep(1)  # be polite to reddit
    # de-dupe by title similarity (rough)
    seen: set[str] = set()
    deduped: list[Candidate] = []
    for c in cands:
        # normalize to first 8 words lowercase
        key = " ".join(c.title.lower().split()[:8])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(c)
    return deduped

# ----------------------------- demand scoring -----------------------------

def compute_demand_score(c: Candidate) -> float:
    """Demand = engagement weighted by recency.

    Heavy upvotes/comments on a recent post = high demand.
    Old post = penalize but not crush (mature pain points persist).
    """
    recency_factor = max(0.3, 1.0 - (c.age_days / 30.0))  # 0.3 floor for older posts
    base = c.upvotes + 5 * c.num_comments  # comments worth ~5x an upvote (engagement)
    return base * recency_factor

# ----------------------------- query inference -----------------------------

QUERY_INFER_PROMPT = """For each Reddit post below, infer the SHORT YouTube search query a viewer would type to find a video that solves the post's underlying problem.

Rules:
  - 3-7 words, lowercase, no punctuation
  - Use action words: "how to", "best ai for", "fix", "automate"
  - Generalize from the specific complaint to the searchable need
  - If the post is venting/news/opinion (not a how-to), return an empty string

Examples:
  Post: "I keep losing track of all my AI tool subscriptions, is there an automated way to track them?"
  Query: "how to track ai subscriptions"

  Post: "ChatGPT can't remember anything between sessions. I tried memories but it forgets. What do you all do?"
  Query: "how to make chatgpt remember between chats"

  Post: "Why is Anthropic releasing Claude 4.7 already lol"
  Query: ""

Return STRICT JSON:
{{"queries": ["<query for post 0>", "<query for post 1>", ...]}}

Posts (index | title | body snippet):
{posts}
"""

def infer_queries(candidates: list[Candidate], client: genai.Client) -> None:
    # Process in batches to keep prompts within limits
    BATCH = 25
    for i in range(0, len(candidates), BATCH):
        batch = candidates[i:i + BATCH]
        lines = []
        for j, c in enumerate(batch):
            snippet = c.body_snippet[:200].replace("\n", " ")
            lines.append(f"{j} | {c.title} | {snippet}")
        prompt = QUERY_INFER_PROMPT.format(posts="\n".join(lines))
        try:
            resp = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.3,
                ),
            )
            data = json.loads(resp.text)
            queries = data.get("queries", [])
        except Exception as e:
            print(f"[warn] gemini query infer batch {i}: {e}", file=sys.stderr)
            queries = ["" for _ in batch]
        for c, q in zip(batch, queries):
            c.inferred_query = (q or "").strip().lower()
        time.sleep(2)  # gentle on free tier

# ----------------------------- supply: YouTube competition lookup -----------------------------

YT_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
YT_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"

def youtube_competition(query: str, api_key: str, max_results: int = 8) -> list[dict]:
    """Return list of top YT results with view count + age + title."""
    try:
        params = {
            "part": "snippet",
            "q": query,
            "type": "video",
            "maxResults": max_results,
            "order": "relevance",
            "key": api_key,
        }
        r = requests.get(YT_SEARCH_URL, params=params, timeout=20)
        if r.status_code != 200:
            return []
        items = r.json().get("items", [])
        ids = [it["id"]["videoId"] for it in items if it.get("id", {}).get("videoId")]
        if not ids:
            return []
        # Fetch view counts + duration in a single call
        params2 = {
            "part": "snippet,statistics,contentDetails",
            "id": ",".join(ids),
            "key": api_key,
        }
        r2 = requests.get(YT_VIDEOS_URL, params=params2, timeout=20)
        if r2.status_code != 200:
            return []
        out = []
        now = dt.datetime.now(dt.timezone.utc)
        for v in r2.json().get("items", []):
            snip = v.get("snippet", {})
            stats = v.get("statistics", {})
            published = snip.get("publishedAt", "")
            age_days = 9999.0
            if published:
                try:
                    pub_dt = dt.datetime.fromisoformat(published.replace("Z", "+00:00"))
                    age_days = (now - pub_dt).total_seconds() / 86400.0
                except Exception:
                    pass
            out.append({
                "id": v["id"],
                "title": snip.get("title", ""),
                "channel": snip.get("channelTitle", ""),
                "views": int(stats.get("viewCount", 0)),
                "likes": int(stats.get("likeCount", 0)),
                "age_days": age_days,
                "published": published[:10],
                "url": f"https://youtu.be/{v['id']}",
            })
        return out
    except Exception as e:
        print(f"[warn] yt search '{query}': {e}", file=sys.stderr)
        return []

def compute_supply_score(competitors: list[dict]) -> float:
    """Supply score: lower = better opportunity.

    High supply when: many results, recent, high views per result.
    Low supply when: few results, old, low views.
    """
    if not competitors:
        return 0.5  # no data: assume medium supply (don't reward null results too much)
    # Average view count of top 3 (the ones a searcher actually clicks)
    top3 = competitors[:3]
    avg_views = sum(c["views"] for c in top3) / max(1, len(top3))
    # Average age of top 3 — older = stale, opportunity to refresh
    avg_age = sum(c["age_days"] for c in top3) / max(1, len(top3))
    # Total results count
    count = len(competitors)

    # Normalize:
    # views: 0-1M maps to 0-1 (logarithmic-ish)
    import math
    views_factor = math.log10(max(1, avg_views)) / 6.0  # log10(1M) = 6
    # age: <90 days = fresh (bad for us), >365 = stale (good)
    age_factor = max(0.0, 1.0 - (avg_age / 365.0))  # 0 if older than 1yr, 1 if today
    # count: 8+ results = saturated, 1-2 = wide open
    count_factor = min(1.0, count / 8.0)

    # Combine: each factor 0-1, supply = average. Higher = more competition.
    supply = (views_factor + age_factor + count_factor) / 3.0
    return supply

# ----------------------------- opportunity ranking -----------------------------

def compute_opportunity(candidates: list[Candidate]) -> None:
    # Normalize demand to 0-1 across the pool
    max_demand = max((c.demand_score for c in candidates), default=1.0) or 1.0
    for c in candidates:
        norm_demand = c.demand_score / max_demand
        # opportunity = demand × (1 - supply)  i.e. high demand with low competition wins
        c.opportunity_score = norm_demand * (1.0 - c.supply_score)

# ----------------------------- brief generation -----------------------------

BRIEF_PROMPT = """You are writing a one-page video brief for a YouTube creator in the AI/productivity niche.

Topic context:
  - Source pain point (Reddit): {title}
  - Body snippet: {body}
  - Inferred search query: "{query}"
  - Demand signals: {upvotes} upvotes, {comments} comments, {age_days:.0f} days old, from r/{subreddit}

Existing YouTube competition for this query:
{competition}

Write a STRICT JSON brief:
{{
  "headline": "<the video title the creator should use, written for high CTR and clear value, <=70 chars>",
  "thesis": "<one sentence on what unique angle this video should take that the existing competition misses>",
  "pain_summary": "<2-3 sentences on what people are actually struggling with, in plain language>",
  "what_competition_misses": "<2-3 sentences on what existing videos fail at: e.g. outdated, too theoretical, missing real workflow, no comparison>",
  "outline": [
    "<section 1: hook + the specific problem (30s)>",
    "<section 2: ...>",
    "<section 3: ...>",
    "<section 4: ...>",
    "<section 5: closing CTA / what to try next (30s)>"
  ],
  "tools_to_test": ["<3-5 specific AI tools or workflows the creator should actually try on camera>"],
  "estimated_video_length_minutes": <int 5-10>,
  "affiliate_opportunity": "<short note: which tools likely pay affiliate commission for signups>"
}}
"""

def write_brief(c: Candidate, client: genai.Client) -> dict:
    comp_lines = []
    for v in c.yt_competitors[:5]:
        comp_lines.append(
            f"- {v['title']} ({v['channel']}, {v['views']:,} views, "
            f"{v['age_days']:.0f}d old) — {v['url']}"
        )
    competition = "\n".join(comp_lines) if comp_lines else "(no significant competition found)"
    prompt = BRIEF_PROMPT.format(
        title=c.title,
        body=c.body_snippet[:600],
        query=c.inferred_query,
        upvotes=c.upvotes,
        comments=c.num_comments,
        age_days=c.age_days,
        subreddit=c.subreddit,
        competition=competition,
    )
    try:
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.6,
            ),
        )
        return json.loads(resp.text)
    except Exception as e:
        print(f"[warn] brief gen failed for '{c.inferred_query}': {e}", file=sys.stderr)
        return {}

# ----------------------------- report rendering -----------------------------

def render_report(date_str: str, top_candidates: list[tuple[Candidate, dict]]) -> str:
    lines = [
        f"# Topic Report — {date_str}",
        "",
        f"Top {len(top_candidates)} video opportunities ranked by **opportunity score**",
        "(high search demand × low existing supply). Pick one and film it.",
        "",
        "---",
        "",
    ]
    for rank, (c, brief) in enumerate(top_candidates, 1):
        headline = brief.get("headline", c.inferred_query or c.title)
        lines.append(f"## #{rank}: {headline}")
        lines.append("")
        lines.append(f"**Search query**: `{c.inferred_query}`")
        lines.append("")
        lines.append(
            f"**Opportunity**: {c.opportunity_score:.3f} "
            f"(demand {c.demand_score:.0f}, supply {c.supply_score:.2f})"
        )
        lines.append("")
        if brief.get("thesis"):
            lines.append(f"**The angle**: {brief['thesis']}")
            lines.append("")
        if brief.get("pain_summary"):
            lines.append(f"**The pain**: {brief['pain_summary']}")
            lines.append("")
        if brief.get("what_competition_misses"):
            lines.append(f"**What existing videos miss**: {brief['what_competition_misses']}")
            lines.append("")
        if brief.get("outline"):
            lines.append("**Suggested outline**:")
            for step in brief["outline"]:
                lines.append(f"  - {step}")
            lines.append("")
        if brief.get("tools_to_test"):
            lines.append(f"**Tools to test on camera**: {', '.join(brief['tools_to_test'])}")
            lines.append("")
        est = brief.get("estimated_video_length_minutes")
        if est:
            lines.append(f"**Target length**: {est} minutes")
            lines.append("")
        if brief.get("affiliate_opportunity"):
            lines.append(f"**Affiliate angle**: {brief['affiliate_opportunity']}")
            lines.append("")
        lines.append(f"**Source post**: [{c.title}]({c.url}) — r/{c.subreddit}, "
                     f"{c.upvotes}↑ {c.num_comments}💬, {c.age_days:.0f}d old")
        lines.append("")
        if c.yt_competitors:
            lines.append("**Top YouTube competition**:")
            for v in c.yt_competitors[:5]:
                lines.append(
                    f"  - [{v['title']}]({v['url']}) — {v['channel']}, "
                    f"{v['views']:,} views, {v['age_days']:.0f}d old"
                )
            lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)

# ----------------------------- driver -----------------------------

def main() -> int:
    REPORTS_DIR.mkdir(exist_ok=True)
    cfg = load_config()
    state = load_state()

    surfaced = set(state.get("surfaced_topics", []))

    # API clients
    yt_api_key = os.environ["YT_API_KEY"]
    gemini_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    # 1. gather
    print("[1/6] Gathering Reddit pain-point candidates...")
    candidates = gather_candidates(cfg)
    print(f"      {len(candidates)} candidates after dedup + filters")
    if not candidates:
        print("[error] no candidates found", file=sys.stderr)
        return 1

    # 2. demand scoring
    print("[2/6] Scoring demand (engagement x recency)...")
    for c in candidates:
        c.demand_score = compute_demand_score(c)
    # Keep top N by demand to limit downstream Gemini + YouTube API calls
    top_by_demand = sorted(candidates, key=lambda c: c.demand_score, reverse=True)
    cap = int(cfg.get("max_candidates_for_supply_check", 40))
    top_by_demand = top_by_demand[:cap]
    print(f"      keeping top {len(top_by_demand)} by demand for supply analysis")

    # 3. infer queries
    print("[3/6] Inferring YouTube search queries via Gemini...")
    infer_queries(top_by_demand, gemini_client)
    # drop ones Gemini couldn't infer (news/opinion posts)
    top_by_demand = [c for c in top_by_demand if c.inferred_query]
    # drop ones we've already surfaced recently
    top_by_demand = [c for c in top_by_demand if c.inferred_query not in surfaced]
    print(f"      {len(top_by_demand)} have actionable queries (not seen before)")

    # 4. YouTube supply lookup
    print("[4/6] Probing YouTube competition for each query...")
    for i, c in enumerate(top_by_demand):
        c.yt_competitors = youtube_competition(c.inferred_query, yt_api_key)
        c.supply_score = compute_supply_score(c.yt_competitors)
        if (i + 1) % 5 == 0:
            print(f"      ...probed {i+1}/{len(top_by_demand)}")
        time.sleep(0.3)  # gentle on quota

    # 5. opportunity ranking
    print("[5/6] Computing opportunity scores (demand / supply)...")
    compute_opportunity(top_by_demand)
    ranked = sorted(top_by_demand, key=lambda c: c.opportunity_score, reverse=True)
    top_n = int(cfg.get("daily_topic_count", 5))
    finalists = ranked[:top_n]
    print(f"      finalists: {len(finalists)}")

    # 6. briefs + report
    print("[6/6] Writing briefs for finalists...")
    enriched: list[tuple[Candidate, dict]] = []
    for c in finalists:
        brief = write_brief(c, gemini_client)
        enriched.append((c, brief))
        time.sleep(2)

    date_str = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    md = render_report(date_str, enriched)
    report_path = REPORTS_DIR / f"{date_str}.md"
    report_path.write_text(md, encoding="utf-8")
    # Also overwrite a stable LATEST.md for easy linking
    (REPORTS_DIR / "LATEST.md").write_text(md, encoding="utf-8")
    print(f"      wrote {report_path.relative_to(ROOT)}")

    # update state
    for c, _ in enriched:
        surfaced.add(c.inferred_query)
    state["surfaced_topics"] = list(surfaced)[-500:]
    save_state(state)

    print("[done]")
    return 0

if __name__ == "__main__":
    sys.exit(main())
