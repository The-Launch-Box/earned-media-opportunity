# Earned media daily roundup (local)

Local implementation of [earned-media-pipeline-architecture.md](./earned-media-pipeline-architecture.md). Same morning job — research → dedupe → roundup — running on your machine instead of Azure.

| Architecture piece | Local stand-in |
|---|---|
| Azure Function + timer | `python -m pipeline run` (cron later) |
| Azure Table Storage (roster) | `TLB PortCo SME List.xlsx` → SQLite |
| Azure Table Storage (sent fingerprints) | SQLite `sent_opportunities` |
| Claude API + web search | Anthropic SDK (`python -m pipeline run`) |
| Site traffic | `CLOUDFLARE_API_TOKEN` → Cloudflare Radar rank on each outlet domain |
| Podcast audience | `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` → subscribers and monthly listeners for podcasts |
| Microsoft Graph email | `DELIVERY_MODE=graph` (device-code login) + `outbox/` copy |

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # add ANTHROPIC_API_KEY, and optionally Cloudflare and Spotify keys
```

Optional: put real addresses in `config/emails.json` (the spreadsheet has no email column). Outbox delivery works without them.

Claude is the research model. If that account is out of credits and `RESEARCH_FALLBACK=foundry`, the run continues on Azure AI Foundry (`FOUNDRY_ENDPOINT`, `FOUNDRY_API_KEY`, `FOUNDRY_MODEL`) using that API's web search tool. With no fallback configured, the roundup still sends. The top of the email names the model used. When Claude credits are exhausted it also includes error `anthropic_credit_balance`, asks the reader to contact their Claude administrator, and says to run the pipeline again after the balance is restored in the Anthropic admin console.

Add `CLOUDFLARE_API_TOKEN` (Account → Radar → Read) for a Site line from Cloudflare Radar. That rank comes from DNS queries to 1.1.1.1: an exact global rank for the top 100, otherwise a bucket such as top 10K, or “outside the top 1M.” It is a traffic-popularity rank, not an estimated visit count. Links on Spotify, YouTube, Apple, and similar hosts are skipped, because that rank would describe the platform. Without the token, the Site line is omitted and the roundup still sends.

Podcast opportunities that are on Spotify get a Podcast line. Subscriber count is Spotify’s follower total, and monthly listeners are included when the show object returns them. The public Web API usually omits both, and then the line shows episode count, publisher, and the newest episode date. A show is included when the link is a Spotify show or episode URL, or when the outlet name matches a Spotify show. Press, speaking, and other types are not sent to Spotify.

## Commands

```bash
# Load / refresh roster from the Excel file
python -m pipeline sync
python -m pipeline roster

# Claude + web search for every SME on TLB PortCo SME List.xlsx (default)
python -m pipeline run

# Testing overrides (subset of the roster, or stubs instead of Claude)
python -m pipeline run --firm Echelon
python -m pipeline run --person "Amy Kenly"
python -m pipeline run --limit 1
python -m pipeline run --dry-run

# Sign in to Microsoft once (device code + MFA), then send the blast via Graph
python -m pipeline auth
python -m pipeline run --dry-run    # test send without Claude
python -m pipeline run              # live search + send

# Inspect dedupe store
python -m pipeline sent

# Cloudflare Radar site traffic for one outlet domain
python -m pipeline site cio.com
python -m pipeline radar --dry-run example.com

# Spotify subscribers and monthly listeners for one podcast
python -m pipeline podcast "Acquired"

# Yes/No on each opportunity (leave the server running; set FEEDBACK_PUBLIC_URL)
python -m pipeline serve
python -m pipeline feedback
```

Roundups land in `outbox/` as a single `{timestamp}_EMO-Roundup` `.txt` / `.html` / `.json` blast. Re-running skips URLs already filed under that person. New rows on `TLB PortCo SME List.xlsx` are picked up on the next run and appended to the email.

## Worthwhile? Yes / No

Each opportunity can ask **Is this a worthwhile opportunity?** with Yes and No links. Set `FEEDBACK_PUBLIC_URL` to an address the reader can open, then leave `python -m pipeline serve` running. The morning job is not listening when someone clicks later.

No opens a page that asks why. That reason is stored in SQLite and added to the next Claude prompt so later runs avoid opportunities like it. Yes is stored and is not used as a filter. Opening a link does not save anything — mail security tools prefetch links — so the page has the button that writes the answer.

## Layout

```
pipeline/          # roster, storage, research, Radar site traffic, Spotify audience, roundup, emailer, runner, CLI
config/emails.json # optional name → email overlay
data/pipeline.db   # local table-storage stand-in (gitignored)
outbox/            # generated roundups (gitignored)
```

## Addendum — Update 2.3

The app is a morning research job plus a small website for the Yes/No links. On Azure those stay two separate triggers in one Function App: a timer that sends the roundup, and an HTTPS address that saves feedback.

### What happens each morning

`python -m pipeline run` does one pass and exits.

1. It reads `TLB PortCo SME List.xlsx` into SQLite. Each spokesperson gets a stable id from their firm and name.
2. For each person, it asks Claude, with web search turned on, for up to five recent press, podcast, speaking, journalist-query, or backlink opportunities. The prompt includes any “not worthwhile” reasons already saved.
3. It drops links already sent to that person, and links the team has rejected.
4. When `CLOUDFLARE_API_TOKEN` is set, it asks Cloudflare Radar for each outlet domain’s traffic rank and caches that for a week. Podcast items that match a Spotify show get subscriber and monthly listener counts when Spotify returns them, plus episode count, publisher, and the newest episode date.
5. It writes one team email: Time-Sensitive first, then Other, grouped by person. With `DELIVERY_MODE=graph`, Microsoft Graph sends that HTML from your mailbox to `BLAST_TO`. A copy also lands in `outbox/`.
6. It records each URL it sent, so the next run skips it.

The email’s Yes and No buttons are links. They only work if `python -m pipeline serve` is still running, because the morning job has already exited. No opens a page that asks why. Save writes that reason into SQLite. The next morning’s Claude call is told to avoid opportunities like that one.

### How to host it on Azure

Use one Python Function App with two functions:

| Piece today | On Azure |
|---|---|
| `python -m pipeline run` on a cron | Timer trigger, once each morning |
| `python -m pipeline serve` | HTTP trigger on the same app |
| `data/pipeline.db` | Azure Table Storage |
| The Excel roster | The workbook in Blob Storage or SharePoint, read at the start of the run |
| `FEEDBACK_PUBLIC_URL` | The function’s HTTPS address, such as `https://<app>.azurewebsites.net` |
| Keys in `.env` | Function App settings, or Key Vault |

The Yes/No links in the email point at that HTTPS address. A click wakes the HTTP function, saves the verdict, and the next timer run reads it. A cold start of a few seconds is fine for that page.

Four things in the current code have to change before that works. The research and roundup wording can stay as they are.

**Storage.** A Consumption function’s disk disappears when the run ends, so SQLite will not remember yesterday’s links or today’s “no.” The tables already in use — roster, sent links, Radar cache, podcast lookups, and feedback — map onto Azure Table Storage. `pipeline/storage.py` is the adapter to swap.

**Mail login.** Today Graph sends as you, using a device-code sign-in saved in `data/msal_token_cache.bin`. A timer cannot show that prompt. Register an app permission to send as the mailbox (`Mail.Send` with admin consent) and call `/users/{mailbox}/sendMail` instead of `/me/sendMail`.

**How long the morning run is allowed to last.** Consumption functions stop at 10 minutes. One Claude web-search call per spokesperson can run past that for a full roster. Use a Premium plan with a longer timeout, or queue one message per person so each search is its own short function.

**The roster file.** Upload the workbook to Blob Storage or SharePoint and have the timer read it from there. People keep editing the same spreadsheet; the function picks up new rows on the next morning.

The earlier write-up in [earned-media-pipeline-architecture.md](./earned-media-pipeline-architecture.md) describes this same shape. Two details in the running app differ from that note: the email is one team blast, and the feedback pages have to stay reachable all day.
