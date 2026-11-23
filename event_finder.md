# Event finder / scheduler — adaptation plan

This plan turns the earned-media opportunity finder (EMOF) into an **event finder and scheduler**. It reuses the parts of EMOF that are already hard (roster, tenants, Claude + web search with Foundry fallback, Yes/No feedback loop, Graph mail, Azure timers) and replaces the parts that only make sense for press opportunities.

## 0. One-week cut (deadline: 5 working days)

The original 12-week plan does not compress to one week, so section 7 now schedules only this cut and lists the rest as follow-on milestones. This cut keeps the parts that make the feature useful and trustworthy, and defers everything that needs other people's approval or a second storage backend. It assumes about 40 hours of focused work by one developer, and The Launch Box only.

**In (ships by Friday)**

- Event model and fingerprint, SQLite tables.
- One discovery call per run using TLB's topics and event criteria, through the existing Claude + Foundry fallback.
- Verification pass: open each event page, drop past, cancelled or unverifiable events. This is the one stage not worth cutting.
- Dedupe by event, with matched speakers listed under each event. No-Go reasons feed the next prompt.
- Weekly digest email: **Closing soon** and **Coming up**, with deadlines shown in the digest instead of a separate reminders timer.
- Yes/No links reused. **Add to calendar** emails a tentative `.ics` the speaker accepts.
- Deployed as a **Linux cron job** (the hosting path `earned-media-opportunity-architecture.md` already names for testing), sent to the TLB team after one send-to-self run.

**Deferred (follow-on, in this order)**

1. Azure Table Storage parity and Azure timers (SQLite does not survive a Function run, so Azure waits).
2. Graph calendar write and conflict checks. The IT request is filed on day 1 so it is ready when this starts.
3. Reminders timer, CFP drafting, "Promote it", annual-conference watchlist.
4. Dashboard Events tab and company event preferences.
5. Improv, Hyperscayle and Echelon.

**Trade-offs to accept**

- The cron host (a laptop or a Linux box) has to be on at send time. Azure moves it off a laptop later.
- Verification is tested against a 10-event golden set, not 20, so expect a few bad dates in the first digests. Marketing reviews the first digest before it goes to the wider team.
- Deadline warnings arrive with the weekly digest, so a deadline that falls mid-week can be missed. Daily reminders are the first follow-on.

| Day | Work | Hours |
|---|---|---|
| Mon | Kickoff, criteria, golden set; model; SQLite tables; file IT request | 9.5 |
| Tue | Discovery prompt and schema; verification pass | 10 |
| Wed | Dedupe and matching; CLI; golden-set check and tuning | 7.5 |
| Thu | Digest; feedback links and `.ics` | 9 |
| Fri | Cron deploy and first send; review and fixes (also the slip buffer) | 4 |

## 1. What "event finder / scheduler" means here

I am assuming this scope. Section 10 lists the questions that could change it.

- **Find** upcoming events each portfolio-company spokesperson or marketing team should know about: conferences, panels, webinars, trade shows, local meetups and association events. Include **speaking calls (CFPs)**, **attend-only** events and **sponsor/exhibit** events, with their dates, location, cost and deadlines.
- **Verify** each one against its own event page. Press links go stale in days. Events get cancelled, moved or sold out, and the model will confidently cite last year's edition.
- **Schedule** the ones the team approves: put a tentative hold on a calendar, track the registration, CFP and early-bird deadlines, remind people before they pass, and keep a status for each event from "found" through "attended."

EMOF answers "what should we pitch this week?" The event finder answers "where should we be in the next 90 days, and what has to happen by when?" That is a calendar-and-deadline problem, so the pipeline changes in three ways:

1. The research output is a dated **event** with a lifecycle, not a one-off link.
2. The unit of dedupe is the **event**, not the (person, URL) pair, because one conference fits several speakers.
3. The pipeline gets a second job, **reminders**, that runs on its own timer and does not need a new research call.

## 2. What stays, what changes, what is new

| Area | EMOF today | Event finder |
|---|---|---|
| Roster, tenants, sign-in, dashboard shell | `roster.py`, `registry.py`, `dashboard.py` | **Keep.** Add event preferences (geo, budget, types) per company. |
| Research call | `research.py` `SYSTEM` + `_user_prompt`, up to 5 items from the last 14–30 days | **Replace the prompt and schema.** Look-ahead window, dates, venue, cost, CFP and registration deadlines. Keep `_run_live_research`, `pause_turn` handling, credit-error handling. |
| Foundry fallback | `fallback.py` | **Keep.** Pass the event prompt through it. `fallback_research(person, …)` is person-shaped today and needs a prompt parameter. |
| Dedupe | `(person_id, url fingerprint)` in `sent_opportunities` | **Replace.** Event fingerprint = normalized name + start date + city, with URL as a fallback. One event row, many matched speakers. A changed date re-surfaces the event. |
| Time-sensitive rule | Deadline ≤ ~48 h (`models.py` `is_urgent`) | **Replace.** "Closing soon" = CFP, early-bird or registration deadline within 7 days, or an approved event with an open task. |
| Enrichment | Cloudflare Radar rank, Spotify audience (`radar.py`, `spotify.py`, `podcasts.py`) | **Swap.** Keep Radar on the *organizer's* domain as a legitimacy signal. Drop Spotify/Rephonic. Add cost, expected attendance, past-speaker quality and a pay-to-play flag. |
| Roundup email | `roundup.py`, daily, Time-Sensitive / Other, by person | **New `digest.py`.** Weekly by default: Closing soon / Coming up (next 30–90 days) / Already on your calendar. Grouped by date, with matched speakers listed under each event. |
| Yes/No/LinkedIn links | `feedback.py`, `decisions.py`, `opportunity_feedback` | **Keep the mechanism.** Yes becomes **Add to calendar**, No keeps the reason that feeds the next prompt, LinkedIn becomes **Promote it**. New verdict: **Maybe/remind me**. |
| Go action | `followup.py` `draft_pitch` (web search for a contact, email a pitch) | **Replace with `schedule`**: hold the date, check conflicts, create reminders. Add `cfp` (draft session title/abstract/bio) when the event takes speakers. |
| Delivery | `emailer.py`, `graph_mail.py` (Mail.Send, Mail.Read) | **Keep.** Add `.ics` attachments, and later Graph **Calendars.ReadWrite**. |
| Timers | `morning_roundup`, `portco_schedules` in `function_app.py` | **Add** `event_digest` (weekly) and `event_reminders` (daily). Reuse the minute timer for queued actions. |
| Storage | SQLite (`storage.py`) and Table Storage (`table_store.py`) | **Add tables in both.** See section 4. |

Most of the platform code is reused as is. The new code is concentrated in five places: the event model, the research and verification stages, the digest, the scheduler, and the reminders timer.

## 3. Target flow

```
Roster + company event preferences (geo, budget, types, min lead time)
        │
        ▼
Discovery  ── Claude + web search, one call per company-topic cluster
        │       (Foundry fallback unchanged)
        ▼
Verification ── fetch each event page; confirm name, dates, venue, status;
        │         drop past, cancelled, duplicate-edition or unverifiable events
        ▼
Dedupe + match ── one event row; attach every speaker whose topics fit
        │           drop rejected events and patterns (No-Go reasons)
        ▼
Score ── fit, cost, pay-to-play flag, organizer domain rank (Radar)
        │
        ▼
Conflict check ── free/busy for matched speakers (when calendar access exists)
        │
        ▼
Digest email ── Closing soon / Coming up / On your calendar
        │          each item: Add to calendar · Not for us (why?) · Remind me · Promote it
        ▼
Action queue ── schedule (hold + .ics) · cfp draft · promote draft
        │
        ▼
Lifecycle + reminders (own timer)
   found → proposed → approved → held → registered | submitted → attended | declined | expired
```

Discovery runs per company, with the speakers' topics clustered, rather than once per person. An events search for "AI in tax and accounting" does not change by speaker, and EMOF's per-person loop would bill the same search ten times and then list the same conference ten times. Matching speakers to events happens after discovery.

## 4. Data model

New or changed objects. Names are suggestions.

**`Event`** (`pipeline/models.py`)

`title`, `organizer`, `url`, `event_type` (conference | panel | webinar | trade_show | meetup | workshop | other), `format` (in_person | virtual | hybrid), `start_date`, `end_date`, `timezone`, `city`, `region`, `venue`, `cost_note`, `est_attendance`, `speaking_opportunity` (cfp | invited | none), `cfp_deadline`, `registration_deadline`, `early_bird_deadline`, `status_from_source` (confirmed | postponed | cancelled | unknown), `verified_at`, `verification_note`, `fingerprint`, `fit_score`, `pay_to_play`, `why_relevant`, `suggested_role` (speak | attend | sponsor | exhibit), `radar` (organizer domain).

**`EventMatch`**: `(event_fingerprint, person_id, fit_note, suggested_role)`. This is the many-to-many that replaces per-person dedupe.

**`EventState`**: `event_fingerprint`, `status` (see lifecycle), `owner_person_id`, `decided_by`, `decided_at`, `reason`, `next_action`, `next_action_due`.

**`CalendarHold`**: `event_fingerprint`, `person_id`, `calendar_kind` (ics | graph), `external_id`, `created_at`, `last_synced_at`, `conflict_note`.

**Tables** (SQLite name / Azure Table name), added to both `storage.py` and `table_store.py`:

| SQLite | Azure | Purpose |
|---|---|---|
| `events` | `Events` | One row per verified event |
| `event_matches` | `EventMatches` | Speaker ↔ event |
| `event_state` | `EventState` | Lifecycle and next action |
| `calendar_holds` | `CalendarHolds` | What was written where |
| `event_digests` | `EventDigests` | Batch key → items, for reply handling (mirrors `roundup_batches`) |

`opportunity_feedback` and `action_jobs` are reused with a `kind` column ("event" vs "emof") and new action kinds (`schedule`, `cfp`, `promote`). The `ingested_messages`, `radar_domains` tables stay.

Fingerprint: SHA-256 of `lower(name) + start_date + lower(city or "virtual")`, first 24 hex characters, the same truncation as `url_fingerprint`. A second lookup by URL catches renamed events. If the dates change on an event already in `events`, update the row and mark it "changed" so the digest shows it again once.

**Retention.** EMOF keeps search memory for one month. Events need the opposite: keep an event until its end date, then keep a slim "attended/declined" record for a year so the annual-conference watchlist can use it.

## 5. Stage-by-stage changes

### 5.1 Discovery (`pipeline/research.py`, new `pipeline/events/discover.py`)

- New system and user prompts. Ask for events starting between `EVENT_MIN_LEAD_DAYS` (default 14) and `EVENT_LOOKAHEAD_DAYS` (default 120) from today, matching each company's topics, geography and event types.
- Inputs per company: topic clusters from the roster `topics` column, the company's **Search notes** (already supported as `research_notes`), home region and travel radius, budget cap, whether the company wants speaking, attending or sponsoring.
- Output: JSON array of `Event` fields. Cap at 15 per call, ordered by fit.
- Keep the existing "do not invent URLs" rule, and require the event's own page, not an aggregator listing, when one exists.
- Increase `MAX_SEARCH_USES` for this job. Event discovery needs more searches than press research (listing sites, CFP sites, association calendars). Measure cost in M1 (Wednesday's live run).
- **Annual-conference watchlist.** A small table of recurring events and the month they usually happen. Each run re-checks the ones due in the look-ahead window. This catches the "dates announced, nobody searched for them" case and costs far less than open discovery.
- Candidate structured sources to evaluate after week 1 (week 1 uses web search only): CFP aggregators, event platforms with public search, association calendars with ICS/RSS feeds. Check each one's API availability, terms and cost before committing. Web search stays the fallback because it works across every industry the portfolio covers.

### 5.2 Verification (new stage, `pipeline/events/verify.py`)

This is the stage that decides whether the feature is trustworthy, and EMOF has no equivalent.

- For each discovered event, fetch the event URL (the Anthropic web fetch tool or a plain `httpx` fetch plus a short extraction call) and compare name, dates, city and status against the claim.
- Drop events that are in the past, cancelled, a prior-year edition, or whose page cannot be fetched or does not mention the claimed dates.
- Record `verified_at` and a one-line `verification_note` so the digest can say "dates confirmed on the event site on Nov 20."
- Anything the model found but the page does not support is kept in a "needs a look" bucket in the dashboard, not emailed.

### 5.3 Match, dedupe, score (`pipeline/events/match.py`)

- Upsert the event by fingerprint. Create `EventMatch` rows for each speaker whose topics fit, with a one-sentence reason from the model.
- Remove events whose latest verdict is No (the `rejected_fingerprints` idea, keyed by event fingerprint) and apply the No-Go reasons to the next prompt exactly as `avoidance_guidance` does now. Reasons like "pay-to-play" and "wrong region" carry over well.
- Score: fit (model), cost vs budget, pay-to-play flag, and Radar rank on the organizer's domain via the existing `radar.py`. Keep Radar optional, as today.

### 5.4 Conflict check (`pipeline/events/calendar.py`)

- If the speaker's calendar is reachable, check free/busy over the event dates and note any conflict in the digest ("Overlaps with Board meeting, Mar 4").
- If not, skip the check and say nothing is known. Do not guess.

### 5.5 Digest (`pipeline/events/digest.py`, reusing `emailer.py` and `graph_mail.py`)

- Subject: `Upcoming Events Digest [Month D, YYYY] [evt-########]`, using the same batch-key idea as `emo-`.
- Sections: **Closing soon** (a deadline within 7 days), **Coming up** (grouped by month, then date), **Already on your calendar** (approved events with their status and next step), **Changed since last digest** (date or status changes).
- Each item: title, dates and location, format, cost, deadlines, verification note, matched speakers and role, why it fits, and the action links.
- The email's reply-with-numbers flow (`2. reason`) carries over unchanged for No-Go reasons.
- Default cadence **weekly** (Monday 7:00 local). EMOF's `schedule.py` already supports weekly. Add a separate daily **reminders** email only if people ask for one, since reminders can ride on the weekly digest and calendar alerts.

### 5.6 Actions (`pipeline/followup.py` → new `pipeline/events/actions.py`)

| Link | Verdict stored | Job queued | Result |
|---|---|---|---|
| Add to calendar | `go` | `schedule` | Tentative hold + `.ics`; conflict note; reminder tasks for deadlines |
| Not for us | `no` + reason | none | Reason feeds the next prompt; event suppressed |
| Remind me | `maybe` | none | Re-surfaces in the digest 7 days before its nearest deadline |
| Promote it | `promote` | `promote` | LinkedIn post draft ("I'll be speaking at…" or "See you at…"), emailed. Nothing is published. |
| Draft a CFP submission (only on events with an open CFP) | `go` | `cfp` | Session title, abstract, speaker bio from the roster, and the submission link, emailed to the speaker |

Same safety posture as EMOF: opening a link never writes (GET renders, POST saves); nothing is sent to the event organizer; nothing is published to LinkedIn; drafts go to the team.

### 5.7 Scheduling and reminders (`pipeline/events/schedule.py`, new timer)

Calendar write has three tiers. Ship them in this order.

1. **ICS attachment (no new permissions, works across tenants).** The approval email includes a tentative `.ics` event. The speaker accepts it into whatever calendar they use. This is the default for Improv, Hyperscayle and Echelon, which are separate Microsoft tenants.
2. **Graph calendar write, The Launch Box tenant.** Add `Calendars.ReadWrite` (delegated) to `DELEGATED_SCOPES` in `graph_mail.py`. Create events with `showAs: tentative` and a category such as "Event Finder," so they can be found and removed. Needs a fresh device-code sign-in and a new token cache upload to Key Vault, because `DELEGATED_SCOPES` today lists only `Mail.Send` and `User.Read`, so the cached token has no calendar access.
3. **Graph per company tenant (opt-in).** Needs each company's admin to consent to the app. If application permissions are used, scope them with an Exchange application access policy so the app can only touch named mailboxes. Do this only if a company asks for it.

**Reminders timer** (`event_reminders`, daily 7:00 local): finds `EventState` rows with `next_action_due` within 14 / 7 / 1 days and emails the owner a short reminder ("Early-bird ends Friday. Register or pass."). It does not call Claude. A reminder that is acted on updates the state ("registered," "submitted," "declined") through a one-click page, the same GET-renders / POST-saves pattern.

## 6. Azure deployment notes

The repo already deploys on every push to `master` through `.github/workflows/azure-functions-deploy.yml`, and `host.json` already allows 1 hour per function. Three things matter for this change:

- **Feature flag.** Add `EVENTS_ENABLED` (default `false`) and have the new timers return immediately when it is off. Without it, merging to `master` turns the new timers on in production.
- **Staging first.** Use a staging slot or a second Function App with its own storage account and `SEND_TO_SELF=true` until the first TLB digests (M3) have been reviewed.
- **Token cache re-consent.** Before the Graph calendar tier can work on Azure, run `python -m pipeline auth --upload` again with the new scope, then confirm the Function App identity still has Key Vault `get` and `set`.

Timeouts are not a problem for the weekly digest on the current plan, but verification adds one fetch per candidate. Measure the run time in M1 and queue per-company jobs the way the dashboard already does if a run approaches the limit.

## 7. Milestones and deployment gates

Sections 1–6 describe the full target design. The plan of record is the **one-week cut in section 0: 40 hours, 5 working days, The Launch Box only**. Week-1 milestones are below. Follow-on milestones come after it and are not scheduled until week 1 ships.

### Week 1 milestones (40 hours)

| # | Milestone | Day | Hours | Deploys to | Exit criteria (gate to the next milestone) |
|---|---|---|---|---|---|
| **M0** | **Scope lock** | Mon | 9.5 | Nothing | TLB event criteria signed off. 10-event golden set exists. Event model and SQLite tables in place. IT request for Graph calendar scopes filed (for the follow-on). |
| **M1** | **Event research engine, local** | Tue–Wed | 17.5 | Laptop (`--dry-run` and live) | On the golden set: **zero** past or cancelled events reach the digest, and most real events are found. Events are deduped across speakers. `events run` and `events list` work. |
| **M2** | **Digest and feedback loop, local** | Thu | 9 | Laptop, `outbox/` and `SEND_TO_SELF` | A digest builds with Closing soon and Coming up sections. Yes/No links save. "Add to calendar" produces an `.ics` that opens in Outlook. |
| **M3** | **Deploy to TLB** | Fri | 4 | **Linux cron, TLB only** | One send-to-self run clean, then the first digest goes to the TLB team. Marketing has reviewed it and fixes are in. |

Rollback: remove the cron entry. EMOF is untouched because the event tables and commands are separate.

### Follow-on milestones (after week 1, not scheduled)

Estimates are carried over from the earlier 12-week plan and have not been re-baselined. Some of the earlier M0–M2 work is now inside week 1, so treat these as upper bounds. Total about 129 hours.

| # | Milestone | Hours | Deploys to | Exit criteria |
|---|---|---|---|---|
| **F1** | **Reminders, CFP drafts, Promote it, watchlist** | 27 | Cron (TLB) | Reminders fire at T-14, T-7, T-1 in a test. CFP and LinkedIn drafts email correctly. Watchlist re-checks recurring events. |
| **F2** | **Graph calendar write and conflict checks** | 14 | Cron (TLB) | Needs the IT consent request from Monday. An approved event produces a tentative hold with no errors. Conflicts are flagged. Removing a hold works. |
| **F3** | **Azure parity and staging deploy** | 32 | **Staging** Function App | Full run on Table Storage in staging. Timers fire on schedule. Token cache re-uploaded. `EVENTS_ENABLED=false` is verified inert in production. |
| **F4** | **Dashboard, company preferences, other companies** | 40 | **Production, all four companies**, one at a time | Events tab shows statuses and deadlines. Each company has preferences and recipients and has received a digest. Docs published. |
| **F5** | **Hardening and handoff** | 16 | Production | Retention job running. Cost and failure alerts in place. Runbook written. |

### Success metrics (set the targets on Monday)

- **Precision:** share of listed events with correct dates, location and status. Target ≥ 95% by the end of week 2.
- **Relevance:** share rated worthwhile in the digest. Target ≥ 70% by the end of week 2.
- **Lead time:** median days between "found" and the event start. Target ≥ 30.
- **Missed deadlines:** CFP or registration deadlines that passed on an approved event with no action. Target 0 once F1 ships.
- **Cost per run:** under the cap agreed on Monday. Measure it on Wednesday's live run.

## 8. Testing approach

- Reuse the patterns in `tests/` (fake Claude client, `--dry-run` stubs).
- Add a **golden set** of about 20 real events: a mix of real and upcoming, past, cancelled, postponed, renamed and prior-year pages. Run discovery and verification against it on every prompt change.
- Unit tests: fingerprinting, date-change re-surfacing, deadline math, reminder windows, `.ics` output, lifecycle transitions.
- Contract tests for Graph calendar calls with a fake HTTP client, as `tests/test_graph_token_cache.py` does for mail.
- A staging end-to-end run before every production enable.

## 9. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Model cites an event that does not exist or has the wrong dates | Loss of trust in the digest | Verification stage is mandatory. Unverified events never email. Golden-set gate. |
| Web search alone misses niche events and CFPs | Low recall | Annual watchlist, structured-source spike, company Search notes. |
| Companies are separate Microsoft tenants | Graph calendar write cannot reach them without admin consent | ICS first; Graph per company only on request. |
| Adding the calendar scope breaks the saved mail token | Digests and replies stop | Add the scope in staging first, re-upload to Key Vault, keep mail send on its own working path. |
| Cost per run is higher than EMOF | Budget | Per-company (not per-person) discovery, caching, weekly cadence, watchlist re-checks. Cost cap in M0. |
| Calendar spam if holds are noisy | Users ignore it | Only create a hold on an explicit "Add to calendar." One event, one hold per person. |
| Merge to `master` turns the feature on | Surprise emails | `EVENTS_ENABLED` flag defaults to off. |
| Pay-to-play events dominate results | Wasted time | Pay-to-play flag, budget filter, No-Go reasons feed the prompt. |

## 10. Open questions

1. **Scope.** Is "scheduler" mainly "put it on the calendar and remind us," or does it also include scheduling meetings with attendees at events, or booking travel?
2. **Audience.** Are digests for the marketing team (as today), the individual speakers, or both?
3. **Roles.** Are we finding events to *speak at*, to *attend*, to *sponsor*, or all three? That changes the prompt and the CFP step.
4. **Geography and budget.** Is there a travel radius or a budget cap per company?
5. **Calendar.** Which calendar is the source of truth: each speaker's Outlook, a shared marketing calendar, or both? Is a shared "Events" calendar acceptable instead of writing to personal calendars?
6. **Tenants.** Can Improv, Hyperscayle and Echelon admins grant Graph consent, or is ICS enough?
7. **Replace or add.** Does this replace EMOF, or run next to it on the same roster? The plan assumes next to it, behind a flag.
8. **Sources.** Are there paid event databases or CFP services the team already has access to?
