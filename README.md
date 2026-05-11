# topic-engine

A daily research pipeline that surfaces the **best AI/productivity video topics to film today**, ranked by opportunity score (high search demand × low existing supply).

This is NOT a content generator. It's a **strategic research assistant**. Every morning, you get a Markdown report with 5 ranked video opportunities and a one-page brief for each:
- The headline to use
- The unique angle that beats existing competition
- What real people are stuck on (with source links)
- A suggested outline
- Which AI tools to actually test on camera
- The estimated affiliate revenue opportunity

You pick one, do 30-60 min of recording + editing, upload, repeat.

## Why this approach

The hard part of monetized YouTube isn't making videos. It's picking topics where people are searching AND existing content sucks. This pipeline does the topic research for you using:

- **Demand signals**: Reddit pain-point posts from 13 AI/productivity subreddits (filtered to "how do I", "best AI for", "is there an AI" patterns), scored by upvotes × comments × recency
- **Supply signals**: YouTube Data API search for each inferred query — how many results exist, how old, how many views, channel quality
- **Opportunity = demand × (1 - supply)**: high demand with weak existing supply ranks at the top

You'll see opportunities like "people are asking X every week, the top YouTube result is from 2023 with only 8k views" — those are the videos worth making.

## What you do

Daily:
1. Open the auto-committed `reports/LATEST.md` in your repo
2. Read the 5 briefs (takes 5 min)
3. Pick the one you actually want to film
4. Test the AI tools on camera (15-30 min)
5. Record voiceover + screen capture
6. Edit, upload

## Setup (~10 min)

### 1. Push this repo to GitHub (public for unlimited Actions minutes)

### 2. Get a Gemini API key (free)
- https://aistudio.google.com/apikey
- "Create API key" → "in new project"
- ⚠️ If you get `limit: 0` errors, try `gemini-2.5-flash` (already set) and a fresh project

### 3. Get a YouTube Data API key (free, read-only)
This is DIFFERENT from your YouTube OAuth setup. It's a simple read-only API key for searching YouTube.
- https://console.cloud.google.com/
- Pick any project (or create a new one)
- APIs & Services → Library → enable **YouTube Data API v3** (if not already)
- APIs & Services → Credentials → **+ Create Credentials** → **API key**
- Copy the key

You'll use ~4,000 of 10,000 daily quota units per run. Plenty of headroom.

### 4. Add GitHub secrets
Settings → Secrets and variables → Actions → New repository secret:

| Name | Value |
|---|---|
| `GEMINI_API_KEY` | from step 2 |
| `YT_API_KEY` | from step 3 |

### 5. Run it
Actions → daily-topic-report → Run workflow → green button. Takes ~10 minutes the first time.

When done, open `reports/LATEST.md` in your repo. That's today's intel.

## Reading a report

Each opportunity has:
- **Opportunity score** (0-1): how attractive (higher = better)
- **Demand score** (raw): how much engagement the underlying pain point has
- **Supply score** (0-1): how saturated YouTube already is (lower = better)
- **The angle**: the specific approach that beats existing videos
- **What competition misses**: actionable gaps (outdated, theoretical, missing comparison)
- **Outline**: a 5-section structure
- **Tools to test**: what to actually demo on camera
- **Affiliate angle**: which tools likely pay you for signups

## Tuning

- **Wrong topic types?** Edit `subreddits` in `config.yaml`. Add `r/learnpython`, `r/Notion`, niche subs — anything where YOUR audience hangs out.
- **Want news topics?** Edit `NEGATIVE_PATTERNS` in `topic_engine.py` — currently filters out news/launches because those saturate fast.
- **Want longer/shorter briefs?** Edit `BRIEF_PROMPT`.
- **Want more topics per day?** Bump `daily_topic_count` in config.
- **YouTube quota worries?** Lower `max_candidates_for_supply_check`.

## Files

```
.
├── .github/workflows/daily.yml   # daily 06:00 UTC scheduler
├── topic_engine.py               # the pipeline
├── config.yaml                   # subreddits + thresholds
├── requirements.txt
├── state.json                    # auto-managed: avoids resurfacing topics
└── reports/
    ├── LATEST.md                 # always today's report
    └── YYYY-MM-DD.md             # daily archive
```

## Honest expectations

- **Not every topic the engine surfaces will be a winner.** It's a filter, not an oracle. Treat it as "here are 5 candidates worth considering" not "make this exact video."
- **The competition data is approximate.** Real SEO tools (Ahrefs/Semrush) cost money. YouTube search results + Reddit metrics are a 70%-accurate proxy.
- **It takes 3-10 videos to find your format.** Don't judge from one report. Run the engine for 2 weeks, make 5-10 videos, see what gets traction, then iterate.
- **The brief tells you what to film, NOT what to say.** You bring the perspective; that's why your videos can rank above generic AI-generated content.

## Troubleshooting

- **`limit: 0` Gemini error** → make a fresh Gemini key in a new project.
- **YT API quota exceeded** → wait 24h. Or lower `max_candidates_for_supply_check`.
- **No candidates found** → Reddit being weird; re-run later.
- **All topics look the same / boring** → expand subreddits in config, or edit `NEGATIVE_PATTERNS` to filter different stuff.
