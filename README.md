# snov_emails

A command-line tool for pulling email addresses for a company's domain from the
[Snov.io](https://snov.io) Domain Search API. Its defining feature is that it
**estimates the credit cost up front and asks for confirmation before spending
anything** — so you always know what a lookup will cost before it charges your
account.

Given a domain, the tool can retrieve either:

1. **Validated prospect emails** — named people at the company (first name, last
   name, position) whose email addresses Snov.io has verified.
2. **All domain emails** — every address Snov.io holds for the domain,
   unverified.

Results are written to a CSV file.

---

## How it works

Snov.io's Domain Search API is asynchronous: you POST to a `start` endpoint,
receive a task reference, then poll a `result` endpoint until the task is
complete. This tool handles that start/poll/result cycle for you, along with
pagination, and normalizes the several response shapes Snov.io returns (the
"next page" and "result" links can arrive as a top-level field, a dictionary, or
a list of link objects, and the tool handles all of them).

Every run follows the same shape:

1. Authenticate using your Snov.io client credentials.
2. Look up the domain to read how many prospects and emails Snov.io has.
3. Show the estimated credit cost for each retrieval option.
4. Wait for your confirmation, then retrieve and save to CSV.

---

## Requirements

- Python 3.6 or newer
- The [`requests`](https://pypi.org/project/requests/) library
- A Snov.io account with API access (client ID and client secret)

Install the dependency:

```bash
pip install -r requirements.txt
```

---

## Credentials

The tool needs your Snov.io API client ID and secret, which you can generate in
your Snov.io account under the API settings.

The recommended way to supply them is via environment variables:

```bash
export SNOV_CLIENT_ID=your_client_id
export SNOV_CLIENT_SECRET=your_client_secret
```

Alternatively, pass them on the command line with `--client-id` and
`--client-secret`. If neither is available, the tool exits with an error rather
than making any calls.

---

## Usage

```bash
python snov_emails.py DOMAIN [options]
```

`DOMAIN` is the company domain you want emails for, e.g. `example.com`.

By default the tool runs in **ask** mode: it looks up the domain, prints the
estimated cost of each option, and prompts you to choose. For example:

```bash
python snov_emails.py acme.com
```

produces something like:

```
Authenticating with Snov.io...
Looking up 'acme.com' (this initial lookup costs 1 credit)...

============================================================
Company:  Acme Corporation  (acme.com)
  Prospect profiles found:        142
  Total domain emails found:      318
============================================================

Estimated credit cost for each option:

  [1] VALIDATED emails only (verified prospect emails)
        up to 150 credits
        (upper bound - you're only charged for prospects with a
         green/yellow email; no charge when none is found)

  [2] ALL domain emails (unverified, everything on the domain)
        7 credits
        (318 emails at 1 credit per 50)

Note: credit values are Snov.io's billing unit; dollar figures
(if shown) depend on your plan's per-credit rate.
============================================================

Choose an option — [1] validated only, [2] all emails, [q] quit:
```

Choosing an option asks for one final confirmation before any credits are spent,
then writes the results to a CSV.

### Options

| Option | Description |
| --- | --- |
| `--client-id ID` | Snov.io client ID (defaults to `SNOV_CLIENT_ID`). |
| `--client-secret SECRET` | Snov.io client secret (defaults to `SNOV_CLIENT_SECRET`). |
| `--mode {validated,all,ask}` | Skip the menu and run a specific mode. `ask` (default) shows costs and prompts. |
| `--credit-price PRICE` | Dollars per credit, to also show an estimated dollar cost (e.g. `0.039`). |
| `--out PATH` | Output CSV path. Defaults to `DOMAIN_validated_emails.csv` or `DOMAIN_all_emails.csv`. |
| `--yes` | Skip confirmation prompts. Use with care — this spends credits without asking. |
| `--max-prospects N` | In validated mode, stop after resolving `N` prospects (caps credit spend). |
| `--max-pages N` | In all-emails mode, stop after `N` pages of 50 (caps credit spend). |
| `--free-estimate` | Use the free email-count endpoint to estimate the "all emails" cost, then exit without spending any credits. |

### Examples

Retrieve only validated prospect emails, showing dollar estimates at $0.039 per
credit, and cap spend at 50 prospects:

```bash
python snov_emails.py acme.com --mode validated --credit-price 0.039 --max-prospects 50
```

Retrieve all domain emails to a specific file, without prompting:

```bash
python snov_emails.py acme.com --mode all --out acme_all.csv --yes
```

Check the likely cost of the "all emails" option without spending a single
credit:

```bash
python snov_emails.py acme.com --free-estimate
```

---

## Output

Both modes write a CSV file.

**Validated mode** columns:

| Column | Meaning |
| --- | --- |
| `first_name` | Prospect's first name |
| `last_name` | Prospect's last name |
| `position` | Job title / role |
| `email` | The verified email address |
| `smtp_status` | Snov.io verification status (`valid` = green, `unknown` = yellow) |
| `source_page` | Where Snov.io found the prospect |

**All-emails mode** produces a single `email` column, since these addresses are
unverified and carry no additional metadata.

---

## Understanding the cost estimates

Snov.io bills in **credits**, not currency, and the dollar value of a credit
depends on your plan. Keep the following in mind when reading the estimates:

- **The initial domain lookup costs 1 credit** (charged only when it returns
  results). This is what lets the tool read the prospect and email counts needed
  to estimate the rest.
- **All domain emails** cost 1 credit per page of up to 50 emails. This estimate
  is exact, because the email count is known ahead of time.
- **Validated emails** cost 1 credit per prospect for whom a verified email is
  found, plus 1 credit per page of up to 20 prospects to walk the list. Because
  there is no way to know in advance how many prospects have a verified email,
  the validated figure is shown as an **upper bound** — you are only charged for
  prospects who actually yield a green or yellow email, so real spend is often
  lower.

To limit spend, use `--max-prospects` or `--max-pages`, or start with
`--free-estimate` to gauge the all-emails cost for free.

---

## Notes and limitations

- The tool only counts emails with an `smtp_status` of `valid` (green) or
  `unknown` (yellow) as "validated"; both are treated as usable.
- The free count endpoint (`--free-estimate`) does not work for webmail domains
  and cannot provide a prospect count for the validated estimate.
- Each async task is polled for up to 60 seconds before the tool gives up on it.
- Network or API errors surface as a clear message and a non-zero exit code;
  interrupting with Ctrl-C exits cleanly.

---

## Author

Fulcanelli
