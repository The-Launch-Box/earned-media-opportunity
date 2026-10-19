# Earned media for your company

This is the guide for running the earned-media roundup for your own firm. You use a website called **Earned media**. You add the people you want watched, choose who receives the email, and set how often it runs. The Launch Box hosts the site. You do not install anything, and you do not change anything in Azure.

The companies on this site are The Launch Box, Improv, Hyperscayle, and Echelon. Sign in with your work Microsoft account. Your email domain decides which company you see. `marissa.salzone@echelon.com` opens Echelon. A colleague at the same company, with the same kind of work account, opens the same speakers, schedule, and sent links. Someone at another company cannot open yours.

## Sign in

1. Open the Earned media link The Launch Box sent you.
2. Choose **Sign in with Microsoft**.
3. Use your work account, the same address your company uses for Microsoft 365. A personal Outlook or Hotmail account is not accepted.

The first time someone from your company signs in, the site creates your company’s workspace and remembers your company’s Microsoft tenant. Later sign-ins have to come from that same tenant.

After you are in, the top of the page shows **Schedule** and **Speakers**, your name, and **Sign out**. The home page is your schedule.

If the page says that Microsoft account can’t use this workspace, the address is not one of the four companies above, or it is a Microsoft tenant other than the one already tied to your company. Contact The Launch Box and send the address you tried.

## Add the people you want watched

Open **Speakers**. A new company starts empty: “No speakers yet. Add the first person your company wants this tool to watch.”

Choose **Add a speaker**. Firm and Name are required. The other fields are what the search uses, so fill them in when you have them.

| Field | What to put there |
|---|---|
| Firm | Your company name, as you want it to appear. Two people can share a firm. The same firm and name together can only be on the list once. |
| Name | The spokesperson’s name. |
| Title | Their role, such as CEO or Chief Marketing Officer. |
| Email | Their address, if you want it on the speaker record. This is not who receives the roundup. That address is on the Schedule page. |
| LinkedIn | A link to their profile. |
| Topics | The subjects they should be pitched on. This is the main hint the search follows. |
| Prior work | Articles, talks, or podcasts they have already done. |
| Bio | A short background, used when drafting an angle. |

Choose **Save speaker**. You can open **Edit** on any row later.

**Delete this speaker** removes them from future roundups. Links already sent for them are cleared, so adding that same person again can bring those opportunities back. The delete page asks you to confirm. **Keep this speaker** leaves them on the list.

## Choose who receives the email

On **Schedule**, fill in **Send the roundup to**. This is the inbox (or inboxes) that get one email for the whole speaker list. Separate addresses with commas:

`marissa.salzone@echelon.com, teammate@echelon.com`

The roundup is one email for your company, grouped by speaker. It does not send a separate email to each speaker unless you put each of those addresses in this box.

## Run it once a day

The schedule page shows the current schedule and the next automatic run. A company that has never set one shows “Only when someone presses Run now,” and nothing is sent on its own.

To send every day:

1. Set **Send the roundup to**.
2. Set **Automatic schedule** to **Daily**.
3. Set **Hour (0–23)** and **Minute**. Hours run from midnight: `7` and `0` is 7:00 a.m., `15` and `30` is 3:30 p.m.
4. Set **Time zone** to Central, Eastern, Mountain, Pacific, or UTC. Central is the starting value.
5. Choose one button:
   - **Run daily** researches every speaker now, emails the addresses above, and keeps the daily schedule. The next automatic run is the following day at that time.
   - **Save schedule** stores the daily time and does not send anything.

**Weekly** uses **Weekly day** (Monday through Sunday) plus the same clock time. **Every N days** uses **Every N days**, from 1 to 90, at that clock time. The count starts the day you save it. The starting value is 14.

**Run now** sends one roundup and leaves the schedule as it already is. Use it when you want a roundup today and the daily time should stay whatever you saved before.

While a run is going, the page refreshes about every 10 seconds. A run that is waiting on the server starts within a minute. If a run is already in progress, wait for it to finish before starting another.

Azure does the checking for you. About once a minute it looks at each company’s saved schedule and starts a company only when that company’s next run is due. Your daily setting is the thing that decides the email. The separate morning job The Launch Box uses for its own roster does not send your list.

## Tell the search what to prefer

On the same page, **Search notes** is optional. These instructions go to the research step on every run. Two that work well: “only target US audiences” or “only look for speaking opportunities.” Notes can be up to 2,000 characters. Choose **Save notes**. The next run follows them. Clearing the box and saving removes them.

**Previously saved notes** keeps the earlier versions, with when they were saved and who saved them.

**Recent runs** lists what has already happened for your company.

## What arrives in your inbox

The subject line looks like this:

`Earned Media Opportunities Roundup [October 19, 2026]`

The body has two groups:

- **Time-Sensitive** — deadlines inside about 48 hours, including journalist queries that expire soon.
- **Other** — the rest.

Under each speaker you get up to five opportunities: a title, the outlet and type (press, podcast, speaking, journalist query, backlink, or other), a link, why it fits, and a suggested angle. When site numbers are available, a line shows estimated monthly visits and a High, Medium, or Low legitimacy label. High means a Semrush Authority Score of 50 or more, Medium is 30–49, and Low is below 30.

A quiet day says “No new earned-media opportunities today.” Links already sent for that speaker are left out the next time. A speaker added since the last email is marked as new on the roster.

## What to do with each opportunity

Each item asks **Is this a worthwhile opportunity?** with **Yes** and **No**. Opening the link from the email does not save anything, because mail security tools open links on their own. The page has the button that writes your decision.

**Yes** marks the item as worthwhile. That answer is stored and is not used to filter later searches. You can change it to No from the confirmation page.

**No** opens **Why isn't this worthwhile?** Write a short reason, at least a few words: the outlet, the audience, or why it does not fit this person. Examples the page suggests: pay-to-play, wrong audience, already pitched, too junior. Choose **Save**. The next search reads that reason and avoids opportunities like it. You can change a saved No back to Yes from the confirmation page.

## When a page says something went wrong

| What you see | What to do |
|---|---|
| That Microsoft account can’t use this workspace | Sign in with your company work account, or ask The Launch Box to confirm your domain is on the list. |
| Sign-in isn’t available | The site’s Microsoft sign-in is not configured yet. Contact The Launch Box. |
| Add the email address that should receive the roundup | Fill in **Send the roundup to** before running. |
| A run is already in progress for your company | Wait until **Recent runs** shows it finished, then start another. |
| Speaker not found | The link is stale. Go back to **Speakers** and open the person from the list. |
| A speaker with this firm and name is already on the list | Edit the existing row, or change the firm or name. |
| Page expired | Go back and try the action again. |
| Something went wrong | Try once more. If it keeps happening, contact The Launch Box. |
