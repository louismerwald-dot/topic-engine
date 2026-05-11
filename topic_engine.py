"""
topic-engine: YouTube Auto-Suggest Scraper & Analyzer
Surfaces high-search-volume, low-competition video topics.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import string
import sys
import time
from dataclasses import dataclass, field
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

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"surfaced_topics": []}

def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))

# ----------------------------- data model -----------------------------

@dataclass
class QueryCandidate:
    query: str
    supply_score: float = 0.0
    opportunity_score: float = 0.0
    yt_competitors: list[dict] = field(default_factory=list)

# ----------------------------- demand: youtube autosuggest -----------------------------

BASE_SEEDS = [
    "how to use ai to ",
    "how to use ai for ",
    "best ai tool for ",
    "ai workflow for ",
    "how to automate ",
    "chatgpt for ",
]

def fetch_autosuggest(query: str) -> list[str]:
    """Hits YouTube's internal autosuggest API (no key needed)."""
    url = f"http://suggestqueries.google.com/complete/search?client=firefox&ds=yt&q={quote_plus(query)}"
    try:
        resp = requests.get(
            url, 
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0"},
            timeout=10
        )
        if resp.status_code == 200:
            # Firefox format returns: ["search term", ["suggestion1", "suggestion2"]]
            data = resp.json()
            if len(data) > 1 and isinstance(data[1], list):
                return data[1]
    except Exception as e:
        print(f"[warn] autosuggest failed for '{query}': {e}", file=sys.stderr)
    return []

def gather_search_queries() -> list[str]:
    """Uses the 'alphabet soup' method to find long-tail search queries."""
    all_queries = set()
    
    for seed in BASE_SEEDS:
        # First get the base suggestions
        suggestions = fetch_autosuggest(seed)
        all_queries.update(suggestions)
        time.sleep(0.5)
        
        # Then append a-z to dig into long-tail specific problems
        for letter in string.ascii_lowercase:
            deep_seed = f"{seed}{letter}"
            deep_suggestions = fetch_autosuggest(deep_seed)
            all_queries.update(deep_suggestions)
            time.sleep(0.5) # Gentle on the API
            
    # Filter out very short or generic queries
    clean_queries = [q.lower().strip() for q in all_queries if len(q.split()) >= 4]
    return list(set(clean_queries))

# ----------------------------- supply: youtube data api -----------------------------

YT_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
YT_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"

def youtube_competition(query: str, api_key: str, max_results: int = 5) -> list[dict]:
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
                "age_days": age_days,
                "url": f"https://youtu.be/{v['id']}",
            })
        return out
    except Exception as e:
        print(f"[warn] yt search '{query}': {e}", file=sys.stderr)
        return []

def compute_supply_score(competitors: list[dict]) -> float:
    """Supply score: lower = weaker competition = better opportunity."""
    if not competitors:
        return 0.1 # Absolute goldmine
    
    top3 = competitors[:3]
    avg_views = sum(c["views"] for c in top3) / max(1, len(top3))
    avg_age = sum(c["age_days"] for c in top3) / max(1, len(top3))
    
    import math
    # Views: punishing if they have millions of views, easy if under 10k
    views_factor = min(1.0, math.log10(max(1, avg_views)) / 6.0) 
    # Age: 0 if older than 1yr (stale), 1 if today (fresh and hard to beat)
    age_factor = max(0.0, 1.0 - (avg_age / 365.0)) 

    # Supply is mostly based on views, slightly weighted by how fresh the videos are
    return (views_factor * 0.7) + (age_factor * 0.3)

# ----------------------------- brief generation -----------------------------

BRIEF_PROMPT = """You are an expert YouTube strategist. I have found a highly-searched, low-competition keyword for an AI/Productivity channel.

Target Search Query: "{query}"

Existing YouTube competition for this query (Notice how weak/old/irrelevant they are):
{competition}

Write a STRICT JSON video brief helping the creator dominate this specific search query:
{{
  "headline": "<the perfect video title, written for high CTR, keeping the target query in mind, <=70 chars>",
  "thesis": "<one sentence on what unique angle this video should take to easily beat the existing competition>",
  "what_competition_misses": "<2 sentences on what the current ranking videos fail at (e.g., outdated UI, too theoretical, bad audio)>",
  "outline": [
    "<section 1: hook + address the search intent (30s)>",
    "<section 2: step-by-step workflow...>",
    "<section 3: real world example...>",
    "<section 4: closing CTA (30s)>"
  ],
  "tools_to_demo": ["<2-3 specific AI tools to screen-record for this>"]
}}
"""

def write_brief(c: QueryCandidate, client: genai.Client) -> dict:
    comp_lines = []
    for v in c.yt_competitors[:3]:
        comp_lines.append(
            f"- {v['title']} ({v['channel']}, {v['views']:,} views, {v['age_days']:.0f}d old)"
        )
    competition = "\n".join(comp_lines) if comp_lines else "(No relevant competition found!)"
    
    prompt = BRIEF_PROMPT.format(
        query=c.query,
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
        print(f"[warn] brief gen failed for '{c.query}': {e}", file=sys.stderr)
        return {}

# ----------------------------- report rendering -----------------------------

def render_report(date_str: str, top_candidates: list[tuple[QueryCandidate, dict]]) -> str:
    lines = [
        f"# Search Opportunity Report — {date_str}",
        "",
        "These are highly searched YouTube queries with weak, old, or low-view competition.",
        "",
        "---",
        "",
    ]
    for rank, (c, brief) in enumerate(top_candidates, 1):
        headline = brief.get("headline", c.query.title())
        lines.append(f"## #{rank}: {headline}")
        lines.append("")
        lines.append(f"**Target Search Query**: `{c.query}`")
        lines.append("")
        lines.append(f"**Competition Score**: {c.supply_score:.2f}/1.00 *(Lower is better)*")
        lines.append("")
        if brief.get("thesis"):
            lines.append(f"**The Angle**: {brief['thesis']}")
            lines.append("")
        if brief.get("what_competition_misses"):
            lines.append(f"**Why we will win**: {brief['what_competition_misses']}")
            lines.append("")
        if brief.get("outline"):
            lines.append("**Suggested Outline**:")
            for step in brief["outline"]:
                lines.append(f"  - {step}")
            lines.append("")
        if brief.get("tools_to_demo"):
            lines.append(f"**Tools to Demo**: {', '.join(brief['tools_to_demo'])}")
            lines.append("")
        if c.yt_competitors:
            lines.append("**Current Top Ranking Videos**:")
            for v in c.yt_competitors[:3]:
                lines.append(f"  - [{v['title']}]({v['url']}) — {v['views']:,} views, {v['age_days']:.0f}d old")
            lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)

# ----------------------------- driver -----------------------------

def main() -> int:
    REPORTS_DIR.mkdir(exist_ok=True)
    state = load_state()
    surfaced = set(state.get("surfaced_topics", []))

    yt_api_key = os.environ.get("YT_API_KEY")
    gemini_api_key = os.environ.get("GEMINI_API_KEY")
    
    if not yt_api_key or not gemini_api_key:
        print("[error] Missing required API keys.", file=sys.stderr)
        return 1

    gemini_client = genai.Client(api_key=gemini_api_key)

    # 1. Gather Search Queries directly from YouTube
    print("[1/4] Scraping YouTube Auto-Suggest for high-demand queries...")
    raw_queries = gather_search_queries()
    # Filter out ones we've already done
    new_queries = [q for q in raw_queries if q not in surfaced]
    print(f"      Found {len(new_queries)} fresh, high-volume search queries.")

    if not new_queries:
        print("[error] No new queries found.", file=sys.stderr)
        return 1

    # 2. YouTube Competition Lookup
    # We only check a random sample of 30 to save API quota, since autosuggest gives us hundreds
    import random
    sample_queries = random.sample(new_queries, min(30, len(new_queries)))
    
    print(f"[2/4] Checking competition metrics for {len(sample_queries)} queries...")
    candidates = []
    for i, q in enumerate(sample_queries):
        comp = youtube_competition(q, yt_api_key)
        score = compute_supply_score(comp)
        candidates.append(QueryCandidate(query=q, supply_score=score, yt_competitors=comp))
        time.sleep(0.2)
        if (i + 1) % 10 == 0:
            print(f"      ...checked {i+1}/{len(sample_queries)}")

    # 3. Opportunity Ranking (Lowest supply score wins, because demand is guaranteed by Autosuggest)
    print("[3/4] Ranking opportunities...")
    ranked = sorted(candidates, key=lambda c: c.supply_score)
    finalists = ranked[:5]

    # 4. Briefs + Report
    print("[4/4] Generating video strategies with Gemini...")
    enriched = []
    for c in finalists:
        brief = write_brief(c, gemini_client)
        enriched.append((c, brief))
        surfaced.add(c.query)

    date_str = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    md = render_report(date_str, enriched)
    report_path = REPORTS_DIR / f"{date_str}.md"
    report_path.write_text(md, encoding="utf-8")
    (REPORTS_DIR / "LATEST.md").write_text(md, encoding="utf-8")
    
    state["surfaced_topics"] = list(surfaced)[-500:]
    save_state(state)

    print(f"[done] Wrote report to {report_path.relative_to(ROOT)}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
