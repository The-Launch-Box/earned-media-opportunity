# Earned media opportunity — architecture
Hello! This is the earned media opportunity finder agent architecture. 
This project is Kaylee's (kaylee.scanlin@thelaunchbox.com) so feel free
to reach out with any questions.

## What this system does

Every morning, this agentic pipeline researches press, podcast, speaking, journalist-query, and backlink opportunities for each portfolio-company spokesperson, filters out anything already sent to that person, and delivers a short roundup. The goal is a quiet daily assist: no one has to remember to search, and no one has to dig through raw results.

## Design principle: a batch job, not a service

Earned-media research is a once-a-day batch job. The architecture therefore favors:

- A **CLI entrypoint** (`python -m pipeline …`) that does one pass and exits
- **Durable local state** only where memory across runs matters (roster + sent fingerprints)
- **File-based delivery** today, with a clear seam for Microsoft Graph email later

The research job itself is still once a day. The one always-on piece is the small feedback server (`python -m pipeline serve`): email buttons are links, and someone may click them hours after the morning job has exited.

## End-to-end flow

```
┌─────────────────────┐
│ TLB PortCo SME List │
│        .xlsx        │
└──────────┬──────────┘
           │ sync (openpyxl)
           ▼
┌─────────────────────┐     optional
│  SQLite roster      │◄──── config/emails.json
│  (data/pipeline.db) │
└──────────┬──────────┘
           │ for each spokesperson
           ▼
┌─────────────────────┐
│ Claude + web search │  (default)
│ or stub research    │  (--dry-run)
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ Dedupe vs           │
│ sent_opportunities  │
└──────────┬──────────┘
           │ new items only
           ▼
┌─────────────────────┐
│ Radar site traffic  │  DNS rank for the outlet domain
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│ Spotify audience    │  podcast subscribers / listeners
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ Team blast          │  Time-Sensitive / Other
│                     │  grouped by person
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│ Outbox delivery     │  .txt / .html / .json
│ (Graph email later) │
└──────────┬──────────┘
           │ record fingerprints
           ▼
┌─────────────────────┐
│ sent_opportunities  │
└─────────────────────┘
```

A single `run` invocation:

1. Reloads the SME workbook into SQLite and flags anyone new on the list
2. Optionally filters by firm, person name, or limit
3. Researches each person (live Claude, Azure AI Foundry when Claude credits are exhausted, or stubs with `--dry-run`)
4. Drops opportunities whose URL fingerprint was already filed for that person
5. Scores remaining outlet domains via Cloudflare Radar (traffic rank). Podcast items also get Spotify subscribers and monthly listeners when the show object includes them
6. Builds one team blast: Time-Sensitive, then Other, grouped by person (new roster names appended last)
7. Writes delivery artifacts under `outbox/`
8. Records newly sent fingerprints so the next run skips them

## Hosting 
When testing we can host this as a Linux cron job (run every 24 hours ~7 AM), but ideally we will be moving this to an Azure function once it is verified as working.

## Component map

| Concern | Module | Role |
|---|---|---|
| Settings / paths | `pipeline/config.py` | Root paths, Anthropic settings, delivery mode from `.env` |
| Domain models | `pipeline/models.py` | `Spokesperson`, `Opportunity`, `ResearchResult`, `Roundup`; `person_id` and `url_fingerprint` helpers |
| Roster ingest | `pipeline/roster.py` | Parse SME Excel; merge optional email overlay |
| Persistence | `pipeline/storage.py` | SQLite stand-in for Azure Table Storage |
| Research | `pipeline/research.py` | Claude Messages API + web search tool, or deterministic stubs |
| Research fallback | `pipeline/fallback.py` | Azure AI Foundry web search when Claude credits are exhausted |
| Site traffic | `pipeline/radar.py` | Cloudflare Radar domain rank (DNS to 1.1.1.1) |
| Podcast audience | `pipeline/spotify.py` | Spotify subscribers, monthly listeners, episode count |
| Roundup copy | `pipeline/roundup.py` | Subject + plain-text + HTML bodies |
| Delivery | `pipeline/emailer.py` | Write outbox files (or stdout / none) |
| Orchestration | `pipeline/runner.py` | Sync → research → dedupe → Radar → Spotify → roundup → deliver → record |
| CLI | `pipeline/__main__.py` | `sync`, `roster`, `run`, `auth`, `sent`, `site`, `radar`, `podcast`, `serve`, `feedback` |
| Feedback | `pipeline/feedback.py` | Yes/No pages and the rejection text added to research prompts |

## Local stand-ins for the cloud design

| Cloud piece (target) | Local stand-in (this repo) |
|---|---|
| Azure Function + timer trigger | `python -m pipeline run` (cron can wrap it later) |
| Azure Table Storage — roster | Excel → SQLite `roster` |
| Azure Table Storage — sent fingerprints | SQLite `sent_opportunities` |
| Claude API + web search | Anthropic SDK (`python -m pipeline run`) |
| Microsoft Graph mail send | Files under `outbox/` |

Keeping the stages identical makes the later move to Azure a swap of adapters (timer, table client, mail client), not a rewrite of research or roundup logic.

## Data

### Source of truth for people

`TLB PortCo SME List.xlsx` is the human-edited roster: firm, expert name, title, LinkedIn, topics, prior work, bio. Firms may be listed once and then left blank on following rows; the loader carries the current firm forward.

Emails are not yet in the spreadsheet but are being added as testing progresses. Optional addresses live in `config/emails.json`, keyed by `"Name"` or `"Firm|Name"`, and are merged at sync time.

### Identifiers

- **`person_id`**: SHA-1 of `firm|name` (lowercased), truncated — stable key across runs, analogous to a Table Storage row key.
- **`fingerprint`**: SHA-256 of a normalized URL (lowercase, trailing slash and query/fragment stripped), truncated — the “already sent this link to this person” key.

### SQLite schema (`data/pipeline.db`)

**`roster`** — one row per spokesperson after sync (upsert; people added to the Excel file are detected as new and appended last in the blast).

**`sent_opportunities`** — primary key `(person_id, fingerprint)`; stores URL, title, outlet, and `sent_at` so a previously sent opportunity is not included again for that person.

**`radar_domains`** — one cached Cloudflare Radar ranking per root domain. Fresh for `RADAR_CACHE_DAYS` (default 7).

**`podcast_lookups`** — one cached Spotify search or show lookup. Fresh for `PODCAST_CACHE_DAYS` (default 7). Misses are cached too, so a show that is not on Spotify is not searched again every morning.

**`opportunity_feedback`** — one row per Yes/No link (`token`). Stores the opportunity plus `verdict` (`yes` or `no`) and the reason given for a no. The latest decision for a URL is what later runs honor.

Both the database and `outbox/` are gitignored; they are runtime artifacts.

## Research stage

For each person, the research module builds a prompt from firm, title, topics, LinkedIn, prior work, and bio, then asks Claude (with the web search tool enabled) for up to five concrete, cited opportunities from roughly the last 14–30 days.

Opportunity types: `press`, `podcast`, `speaking`, `backlink`, `journalist_query`, `other`.

## Roundup and delivery

The roundup is one team blast matching [Example_e-mail_blast.md](./Example_e-mail_blast.md): subject dated to today, a **Time-Sensitive** list (deadlines of about 48 hours or less, including “24 hours to respond”), then **Other**. Each list is grouped by spokesperson name. Anyone newly added to `TLB PortCo SME List.xlsx` is researched automatically and appended after existing names. Empty days get a “nothing new” note unless `--skip-empty` is used.

Each opportunity includes a **Site** line when `CLOUDFLARE_API_TOKEN` is set. Cloudflare Radar ranks the outlet’s own domain from DNS queries to 1.1.1.1: exact global rank for the top 100, a bucket such as top 10K otherwise, or outside the top 1M. That is a traffic-popularity rank, not an estimated visit count. Platform hosts (Spotify, YouTube, Apple, and similar) are skipped. A missing token omits the line and does not block the blast.

A **Podcast** line is added for `podcast` opportunities that are on Spotify: subscribers (the follower total) and monthly listeners when the show object includes them, plus the show name, episode count, publisher, and newest episode date. The public Spotify Web API usually omits both audience counts. A match requires a Spotify show/episode URL, or an outlet name that matches a Spotify show after light normalization. Other opportunity types are not looked up.

When `FEEDBACK_PUBLIC_URL` is set, each item also asks **Is this a worthwhile opportunity?** with Yes and No links. Those links open pages on `python -m pipeline serve`. No asks why before saving. The reason is stored in `opportunity_feedback` and included in the next research prompt so Claude avoids opportunities like that one. Yes is recorded and does not filter later searches. A missing URL omits the links; the rest of the blast still sends. Mail clients cannot run a form inside the message, so the email buttons are links and the save happens on the page (a GET does not write, because link scanners prefetch mail URLs).

Delivery modes (`DELIVERY_MODE`):

- **`outbox`** (default) — writes `{timestamp}_EMO-Roundup.txt`, `.html`, and `.json` metadata
- **`graph`** — writes the outbox copy, then sends HTML via Microsoft Graph `/me/sendMail` (device-code login)
- **`stdout`** — prints the text body
- **`none`** — skips writing

## CLI surface - use when testing

| Command | Purpose |
|---|---|
| `python -m pipeline sync` | Load Excel into SQLite |
| `python -m pipeline roster` | Print current roster table |
| `python -m pipeline run` | Claude + web search for every SME on the roster |
| `python -m pipeline run --dry-run` | Stub research, no API calls |
| `python -m pipeline run --firm … / --person … / --limit N` | Testing: subset of the roster |
| `python -m pipeline auth` | Microsoft device-code login (cached for Graph send) |
| `python -m pipeline sent [--dump]` | Inspect sent-fingerprint store |
| `python -m pipeline site <domain-or-url>` | Cloudflare Radar site traffic for one outlet |
| `python -m pipeline radar <domain-or-url>` | Same Radar lookup as `site` |
| `python -m pipeline podcast <name-or-spotify-url>` | Spotify subscribers and monthly listeners for one podcast |
| `python -m pipeline serve` | Leave the Yes/No pages running |
| `python -m pipeline feedback` | Show saved worthwhile and rejected decisions |

## Configuration

Environment (see `.env.example`): Anthropic key and model, web-search tool type and max uses, delivery mode, from-name, optional blast recipient, optional forced dry-run research, Cloudflare Radar token (optional, for the Site line), Spotify client id and secret (optional, for podcast audience lines).

Paths are fixed relative to the repo root unless code is pointed elsewhere: SME workbook, email overlay, SQLite file, outbox directory.

## Trust and safety boundaries

- Research instructs the model not to invent URLs; only items with a URL are kept.
- Dedupe is per-person, not global — the same outlet link can still go to different spokespeople if relevant.
- A failed person retrieval does not fail the whole morning run.
- The morning run sends one team blast, not one email per person.
- Memory for searches is stored for 1 month only to prevent old search results from being found- after that, it is erased.

## TO-DO 08/25/2026

1. **Today** — local CLI, Excel roster, SQLite memory, outbox files.
2. **Next** — optional cron wrapper; Graph mail send behind the same `deliver()` boundary; email overlay folded into the SME source if desired.
3. **Cloud** — Azure Function timer, Table Storage for roster + fingerprints, same Claude research and roundup stages, Graph delivery — as outlined in the pipeline architecture overview.

## Related docs

- [README.md](./README.md) — setup and day-to-day commands
- [earned-media-pipeline-architecture.md](./earned-media-pipeline-architecture.md) — Azure / serverless target design
