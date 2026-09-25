# Daily earned media roundup — architecture overview

## What this system does

Every morning, this system quietly checks the web for press, podcast, speaking, and backlink opportunities relevant to each portfolio company's spokespeople, then sends each person a short roundup by email of what it found. Nobody has to remember to run it, and nobody has to sift through search results by hand.

## The big idea: something that wakes up, rather than something that's always on

The original instinct was a container that runs continuously with a small database inside it. That works, but it's built for a job that never sleeps — a chat server, a web app, something that needs to be ready to respond at any second. This job only needs to do its work once a day, for a few minutes, so it's a better fit for what's called serverless computing: instead of renting a machine that sits idle for 23 hours and 55 minutes, you write a small program that Azure runs on demand and then shuts back down. You're only billed for the minutes it's actually working, there's no server to patch or reboot, and there's nothing to accidentally leave running over a long weekend.

The Azure product that does this is called an Azure Function. Think of it less as "a server" and more as "a task Azure will run for you when something tells it to."

## What tells it to run: a timer trigger

A timer trigger is exactly what it sounds like — a schedule, similar to a cron job, that tells the Azure Function "wake up and run at this time every day." You set the schedule once, and Azure handles firing it reliably from then on. No always-on process is needed to watch the clock.

## Where the roster lives: Azure Table Storage

The function needs to know who it's researching for — which companies, which spokespeople, and what topics or expertise areas each person wants to be pitched around. Rather than a full relational database, this fits nicely into Azure Table Storage, which is best thought of as a simple, durable filing cabinet: each entry has a label (Azure calls this a "partition key" and "row key") that tells the system exactly where to find it, and reading or writing one entry is fast and cheap. It's a much lighter-weight tool than a SQL database, and it suits a small, structured list like a roster well.

## Who does the actual searching: the Claude API with its web search tool

This is the part that replaces "search the web" in the original description. Rather than building a custom scraper or wiring up a separate search API, the function makes one call per person to the Claude API with a built-in capability called the web search tool turned on. You describe what counts as a good opportunity for that person — their expertise areas, the kinds of outlets or podcasts worth pitching, prior interviews — and Claude runs the searches itself, reads what comes back, and returns a short, cited writeup of anything worth surfacing. It's a single request that handles searching, judgment, and drafting in one step, instead of a pipeline of separate services.

## How it remembers what's already been sent: Azure Table Storage again

The same filing-cabinet tool used for the roster also solves the "have we already told this person about this" problem. Every opportunity the function finds gets a fingerprint — a short hash of its web address — filed under that person's name. Before sending anything new, the function checks whether that fingerprint has been filed before. If it has, the item is skipped; if it hasn't, it's new, so it goes into the roundup and gets filed away so it isn't sent again tomorrow.

## How the email actually goes out: Microsoft Graph API

Microsoft Graph API is Microsoft's toolkit for letting a program read and send mail, check calendars, and do similar tasks on behalf of a mailbox — the same technology already set up and authorized for the shared intake mailbox. Sending the daily roundup can reuse that same authorization, so there's no new login flow or credential to create; the function simply calls Graph's mail-sending endpoint once per person with their roundup.

## A day in the life of the system

Each morning, the timer trigger wakes the function up. It reads the roster from Table Storage to see who it's researching for and what each person cares about. For each person, it makes one call to the Claude API, which searches the web and comes back with a short list of possible opportunities. The function checks each one against Table Storage to filter out anything already sent, then puts a plain-language roundup together from what's left. Finally, it uses Microsoft Graph to send that roundup to the person's inbox and records the new items in storage so they aren't repeated tomorrow. Then everything shuts back down until the next morning.

## Why this is simpler than the container-and-database version

The container-and-database approach means keeping a machine running and healthy at all times, plus a database file that needs a persistent disk to survive between runs — more infrastructure than a once-a-day job really needs. The version described here uses one Azure resource (the Function App) instead of a container plus a registry plus scaling rules, one storage technology (Table Storage) instead of a database file and a separate disk to hold it, and a search step that lives inside a single API call instead of a standalone service to build and maintain.

## A note on cost

Any specific pricing figures for this setup — Azure Function consumption pricing, Claude API usage, and so on — count as financial information, so per policy, those numbers should go through IT leadership for written approval before they're used in any planning document. Happy to help put those figures together whenever that review is useful.
