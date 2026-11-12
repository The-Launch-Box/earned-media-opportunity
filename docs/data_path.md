# Data path

How an opportunity moves from the morning job, into a roundup email, through Go / No-Go / Make it LinkedIn, and back into the next Claude call.

Two timers in `function_app.py` drive the backend. The same steps run from `python -m pipeline run` on a laptop.

| Timer | Schedule | What it does |
|---|---|---|
| `morning_roundup` | `0 0 7 * * *` (07:00 on the Function App clock) | One pass of the shared roster: research, score, send the team blast |
| `portco_schedules` | every minute | Start any company whose saved cadence is due, send queued Go / LinkedIn drafts, then read the sending mailbox for decline replies |

A company run from the dashboard uses the same research path as the morning job. On Azure the button only queues the job (`DASHBOARD_EXECUTION=defer`); the minute timer starts it, because the HTTP request would close before Claude finished. Locally the job starts in a background thread.

## Where rows live

`STORAGE_BACKEND=sqlite` (the local default) writes `data/pipeline.db`. `STORAGE_BACKEND=tables` writes Azure Table Storage. Each company has its own database: a SQLite file, or tables prefixed with that company's slug inside its storage account. The Launch Box run also reads the shared SME workbook.

| What | SQLite table | Azure table |
|---|---|---|
| Spokespeople | `roster` | `Roster` |
| URLs already emailed to a person | `sent_opportunities` | `SentOpportunities` |
| Go / No-Go / LinkedIn rows | `opportunity_feedback` | `OpportunityFeedback` |
| Which email item 1, 2, 3 belong to | `roundup_batches` | `RoundupBatches` |
| Queued pitch and LinkedIn drafts | `action_jobs` | `ActionJobs` |
| Inbox messages already applied | `ingested_messages` | `IngestedMessages` |
| Cloudflare Radar ranks (7 days) | `radar_domains` | `RadarDomains` |
| Spotify / Rephonic lookups (7 days) | `podcast_shows` | `PodcastShows` |

Company schedule, recipients, and research notes live on the tenant record in the control database (`pipeline/registry.py`), not in the opportunity tables. Generated mail is also copied under `outbox/` as `.txt`, `.html`, and `.json`.

Two ids follow a row the whole way:

- **`person_id`** — first 16 hex characters of SHA-1(`firm|name`), lowercased. Stable across runs.
- **`fingerprint`** — first 24 hex characters of SHA-256 of the URL after lowercasing and stripping the query, fragment, and trailing slash. This is “already sent this link to this person,” and “the team already rejected this URL.”

## 1. The morning job

`morning_roundup` calls `run_daily()` in `pipeline/runner.py`. A company whose cadence is due calls `run_for_tenant()` instead. Both end in `_execute_run`.

```
SME workbook or dashboard speakers
        │
        ▼
roster  (person_id per spokesperson)
        │
        │  for each person
        ▼
Claude Messages API + web search
        │  JSON: up to 5 opportunities
        ▼
drop URLs already in sent_opportunities
drop URLs whose latest verdict is no
        │
        ▼
Cloudflare Radar rank, then podcast audience
        │
        ▼
issue a feedback token per item
build one Time-Sensitive / Other email
save roundup_batches (emo-########)
        │
        ▼
outbox files, then Microsoft Graph send
        │
        ▼
sent_opportunities  (only after a successful send)
```

### Roster in

The shared morning job loads `TLB PortCo SME List.xlsx` (`ROSTER_SOURCE=file`, or the blob named by `ROSTER_BLOB_NAME`) and upserts every spokesperson into `roster`. Optional addresses in `config/emails.json` are merged at that sync. Anyone whose `person_id` was not already in the table is marked new and listed last in the email.

A company that is not The Launch Box skips the workbook. Its speakers are the rows already saved from the dashboard speaker form (firm, name, title, LinkedIn, topics, prior work, bio, email).

### Research

For each person, `research_person()` in `pipeline/research.py` sends one Messages API call:

- **System prompt** — find press, podcast, speaking, journalist-query, and backlink opportunities; use web search; return JSON only; do not invent URLs.
- **User prompt** — that person's firm, name, title, topics, LinkedIn, prior work, and the first 800 characters of the bio. It asks for items from roughly the last 14–30 days, capped at five.
- **Standing rules** — the latest No-Go reasons, when any exist. See [How decisions reach Claude](#4-how-it-feeds-into-claude).
- **Company instructions** — `research_notes` on the tenant, when this is a company run. The dashboard notes form is the place those sentences are saved. They are inserted into the user prompt and told to win when they conflict with the default mix of opportunity types.

The tool is the Anthropic web search tool (`WEB_SEARCH_TOOL_TYPE`, default `web_search_20250305`, at most `MAX_SEARCH_USES` searches). If Claude returns `pause_turn`, the same conversation continues for up to four turns. Items with no URL are dropped.

Claude is always tried first. If that call errors and `RESEARCH_FALLBACK=foundry`, the same prompt continues on Azure AI Foundry (`pipeline/fallback.py`). A Foundry model whose name starts with `claude` uses the Messages API with web search; any other model uses the Responses API. After the first failure, the rest of that morning's people stay on Foundry. With no fallback configured, that person is recorded as an error and the run continues with everyone else. A credit-balance error stops further Claude calls for the rest of the run and is named at the top of the email.

`--dry-run` and `DRY_RUN_RESEARCH` skip the API and return stub opportunities.

### What is kept

Each kept URL gets a fingerprint. `filter_new` drops a URL already stored in `sent_opportunities` for that `person_id`, and drops duplicates inside the same reply. `rejected_fingerprints` then drops any URL whose newest decision is `no`. A Go on that same URL clears the block, because the latest decision is what counts.

### Scoring

Remaining items are enriched before the email is written. Neither lookup is required; a missing key omits the line and the blast still sends.

- **Cloudflare Radar** (`CLOUDFLARE_API_TOKEN`) ranks the outlet's own domain from DNS queries to 1.1.1.1. Platform hosts such as Spotify, YouTube, and Apple are skipped. Ranks are cached in `radar_domains` for `RADAR_CACHE_DAYS` (default 7).
- **Podcast audience** (`pipeline/podcasts.py`) runs for items that look like podcasts. Spotify supplies the show, episode count, and newest episode date. Rephonic fills subscribers and listeners when `REPHONIC_API_KEY` is set. Lookups are cached in `podcast_shows` for `PODCAST_CACHE_DAYS` (default 7), including misses.

### Tokens and the batch

`issue_feedback` writes one `opportunity_feedback` row per remaining item and stores the token on the opportunity. The token is `secrets.token_urlsafe(18)`. A row that is still undecided for the same person and fingerprint is reused, so a retry does not mint a second link. The verdict column starts empty. The row also stores person, fingerprint, URL, title, outlet, type, why-it-fits, and suggested angle — the fields the later pitch and LinkedIn drafts read.

`build_roundup` numbers the items in the order they appear in the email (Time-Sensitive first, then Other, newcomers last) and computes a batch key: `emo-` plus the first 8 hex characters of SHA-256(today's date, then those fingerprints in order). The same date and the same items produce the same key. That key is appended to the subject, stored on each feedback row as `batch_key` and `item_number`, and saved in `roundup_batches` together with the subject and the recipient list (`reply_to`). Reply declines and follow-up mail both look the batch up by that key.

When `FEEDBACK_PUBLIC_URL` points at the public site and this process is not already writing that site's tables, `publish_public_feedback` copies the new tokens and the batch into the company tables the site searches. The email links would otherwise open a page that cannot find the opportunity.

## 2. The roundup going back out

`build_roundup` in `pipeline/roundup.py` produces one team email, not one email per person.

Subject: `Earned Media Opportunities Roundup [Month D, YYYY] [emo-########]`.

Body, plain text and HTML:

1. A line naming the model that did the research, and the credit-balance notice when Claude could not run.
2. The batch id.
3. **Time-Sensitive** — deadlines of about 48 hours or less, grouped by spokesperson.
4. **Other** — everything else, grouped by spokesperson.
5. For each item: number, title, deadline, outlet, type, URL, Radar rank, podcast line, why, angle, and the three buttons.
6. Instructions to reply with lines shaped like `2. reason you are passing`.

An empty day still sends, with a “nothing new” note, unless the CLI was started with `--skip-empty`.

`deliver()` in `pipeline/emailer.py` writes `{timestamp}_EMO-Roundup.txt`, `.html`, and `.json` under `outbox/` (or `outbox/<company-slug>/` for a company run). `DELIVERY_MODE` then decides the rest:

| Mode | What the recipient gets |
|---|---|
| `outbox` | The files only |
| `stdout` | The text printed by the process |
| `none` | Nothing written |
| `graph` | The files, then HTML via Microsoft Graph |

Graph send (`pipeline/graph_mail.py`) posts to `/me/sendMail` after a delegated device-code login, or to `/users/{MAIL_FROM}/sendMail` when `AZURE_CLIENT_SECRET` is set. On the Function App the delegated cache is the Key Vault secret named by `GRAPH_TOKEN_SECRET` (default `graph-token-cache`), uploaded with `python -m pipeline auth --upload`. Recipients are the company's `blast_to`, or the global `BLAST_TO` for the shared morning job. `SEND_TO_SELF` redirects every send to `MAIL_FROM`.

Fingerprints are written to `sent_opportunities` only after the send is treated as successful. In `graph` mode a failed send leaves them unrecorded, so the next run can try the same URLs again. Each sent row stores `person_id`, fingerprint, URL, title, outlet, and `sent_at`.

## 3. Go, No-Go, and Make it LinkedIn

Each item's buttons are links, because a mail client will not run a form. They are omitted when `FEEDBACK_PUBLIC_URL` is empty.

| Button | URL |
|---|---|
| Go | `{FEEDBACK_PUBLIC_URL}/feedback/{token}/go` |
| No-Go | `{FEEDBACK_PUBLIC_URL}/feedback/{token}/nogo` |
| Make it LinkedIn | `{FEEDBACK_PUBLIC_URL}/feedback/{token}/linkedin` |

`site` in `function_app.py` (or `python -m pipeline serve` locally) hands `/feedback/...` to `handle_feedback_request` in `pipeline/feedback.py`. The handler looks the token up in each company store, then the legacy database.

**Opening the link does not write.** Mail scanners prefetch URLs, so GET only renders a page. The page button is a POST, and POST is the call that stores a verdict.

### No-Go

The page asks why. `decline_from_form` in `pipeline/decisions.py` accepts two shapes:

- A sentence of at least three characters. That reason is saved on the item they opened.
- Numbered lines (`2. pay-to-play webinar`). Each line is saved on that item number in the same batch. A line that merely repeats the item title is ignored.

`record_feedback` sets `verdict` to `no`, stores the reason, and sets `decided_at`. The row stays in `opportunity_feedback`; nothing else is created. The next morning reads it. See [section 4](#4-how-it-feeds-into-claude).

A reply to the roundup is a second way to store the same No-Go. The minute timer calls `process_pending_followups`, which calls `ingest_inbox` after it drains the draft queue:

1. `list_recent_inbox` reads the newest 25 messages in the sending mailbox (`Mail.Read` on Graph). The body is reduced to text.
2. A message is considered only when the subject starts with `Re:`, `Aw:`, `Sv:`, or `Antw:`. The roundup sitting in the mailbox is not treated as a list of declines.
3. The parser takes the `emo-` key from the subject or the new text, ignores the quoted original, and reads numbered lines.
4. The first company store that has those item numbers gets `record_feedback(..., "no", reason)` for each line.
5. The Graph message id is stored in `ingested_messages` after it is applied, and also when it is not a decline reply, so the same mail is not applied twice. A reply that names a batch this process does not have yet is left unremembered and tried again on a later minute.

Until `Mail.Read` is granted, inbox replies are skipped and the No-Go page is the path that saves the reason.

### Go

POST `/feedback/{token}/go` writes `verdict = go` on that feedback row (the reason is cleared). It then inserts an `action_jobs` row with `kind = go` and `status` queued, and returns the “saved” page. The page tells the reader a pitch will be emailed. Nothing is sent to the outlet.

`enqueue_followup` starts the draft immediately in a local thread. On Azure it waits for the minute timer, which calls `process_action`:

1. Claim the job so a second timer tick does not write it twice.
2. Load the feedback row and the spokesperson from `roster`.
3. `draft_pitch` calls Claude with web search. The prompt is the spokesperson plus the stored title, outlet, type, URL, why, and angle. Claude is asked for a why-spend-time paragraph, angle guidance, public contacts (name, role, email, and the page the email was copied from), a subject, and a pitch body. An email is kept only when it was paired with an `http` or `https` source URL and is not a placeholder domain.
4. The draft is rendered as `EMO-Pitch` mail and sent to `roundup_batches.reply_to` for that batch (otherwise `BLAST_TO`), by the same `DELIVERY_MODE` rules as the roundup.
5. The job is marked `sent`, or `error` with the message stored on the row.

Go replaces an earlier No-Go on that same row. The pitch is the signal that this opportunity should be pursued, so the next run is allowed to consider that URL again.

### Make it LinkedIn

POST `/feedback/{token}/linkedin` writes `verdict = linkedin` and queues an `action_jobs` row with `kind = linkedin`. The same timer or thread runs `draft_linkedin`. That Claude call does not use web search. It returns JSON with a first-person post of about 80–160 words and a one-sentence note on what to personalize. The email is `EMO-LinkedIn`, sent to the same recipient as a pitch. Nothing is published to LinkedIn.

A LinkedIn click on a row that is already `no` leaves the No-Go in place. `stored_decision` returns “do not change the row,” the job still sends the draft, and later research keeps treating the URL as rejected. A LinkedIn verdict is also excluded from `latest_decisions`, so a post draft never counts as approval.

A second click while a job for that token and kind is still queued or running returns the existing job id. A finished job can be queued again.

`python -m pipeline feedback` prints the current Go and No-Go counts from `latest_decisions`. `python -m pipeline replies` applies a decline file, or reads the mailbox once, without waiting for the timer.

## 4. How it feeds into Claude

Claude is called in three places. Each call reads rows that earlier steps wrote, and the research call is the one that changes because of Go and No-Go.

| Call | When | What it reads | Web search |
|---|---|---|---|
| `research_person` | Morning job, or a company run, once per spokesperson | `roster` fields, tenant `research_notes`, latest No-Go reasons | Yes |
| `draft_pitch` | After Go is posted | That feedback row plus the roster person | Yes, to find a public contact |
| `draft_linkedin` | After Make it LinkedIn is posted | That feedback row plus the roster person | No |

Before the per-person loop, `_execute_run` loads `store.rejections()`: the newest decision per fingerprint, keeping rows whose verdict is `no` and whose reason is non-empty, newest first, at most 25. `avoidance_guidance` turns those into a block appended to every research prompt that morning:

- The title, outlet, type, spokesperson, and the reason the team typed.
- An instruction to skip the same URLs.
- An instruction to skip that outlet, format, quality, or kind of ask when the reason was about the opportunity itself.
- Permission to suggest the same item for a different spokesperson when the reason was only that it did not fit the person it was attached to.

Separately, `rejected_fingerprints()` removes a URL after Claude returns it, when the newest verdict for that fingerprint is `no`. A reason is what generalizes to the next search. The fingerprint is what blocks that exact link even when the reason was too new to be inside the 25-line prompt, or when the model repeats it anyway.

A Go stored on that row becomes the latest decision, so the URL is no longer in the rejected set and is not listed in the standing rules. The sent-fingerprint table still suppresses it for the person it was already emailed to.

The pitch and LinkedIn drafts do not write opportunities back into the research set. They read the feedback row, call Claude, and send another email. The only decision data that changes the next morning's research is the verdict and reason on `opportunity_feedback`.
