#!/usr/bin/env python3
"""
snov_emails.py — CLI tool to pull emails for a client's domain from the Snov.io
Domain Search API, with a cost estimate (in credits) shown BEFORE anything is
charged, and a choice between:

  1. Validated emails only  -> prospect emails with a verified status
                               (green = valid, yellow = unknown). Charged 1
                               credit per prospect for whom a verified email
                               is found.
  2. All domain emails      -> every email on the domain (unverified).
                               Charged 1 credit per page of up to 50 emails.

The tool always shows the estimated credit cost and asks for confirmation
before spending any credits on the paid retrieval step.

Credit model (per Snov.io Domain Search API docs, verified 2026-07):
  - The initial "request company info" POST costs 1 credit (only if it returns
    results). This is used to read the counts so we can estimate cost.
  - Domain-emails: 1 credit per page of up to 50 unverified emails.
  - Prospect email search: 1 credit per prospect for whom a verified
    (green/yellow) email is found; nothing charged if none found.

NOTE ON PRICING: Snov.io bills in CREDITS, not dollars. The dollar value of a
credit depends on your plan (roughly 0.9c-3.9c each). Pass --credit-price to
also show an estimated dollar figure.

Requires: requests   (pip install requests)

Set your credentials as environment variables (recommended):
  export SNOV_CLIENT_ID=your_client_id
  export SNOV_CLIENT_SECRET=your_client_secret

Or pass --client-id / --client-secret on the command line.
"""

import argparse
import csv
import math
import os
import sys
import time
from typing import Optional

try:
    import requests
except ImportError:
    sys.exit("This tool requires the 'requests' package. Install it with:\n"
             "    pip install requests")


API_BASE = "https://api.snov.io"
AUTH_URL = f"{API_BASE}/v1/oauth/access_token"

# Domain Search API v2 endpoints
DS_START = f"{API_BASE}/v2/domain-search/start"
DS_RESULT = f"{API_BASE}/v2/domain-search/result/"
DOMAIN_EMAILS_START = f"{API_BASE}/v2/domain-search/domain-emails/start"
DOMAIN_EMAILS_RESULT = f"{API_BASE}/v2/domain-search/domain-emails/result/"
PROSPECTS_START = f"{API_BASE}/v2/domain-search/prospects/start"
PROSPECTS_RESULT = f"{API_BASE}/v2/domain-search/prospects/result/"
PROSPECT_EMAIL_RESULT = f"{API_BASE}/v2/domain-search/prospects/search-emails/result/"

# Free endpoint: returns the number of emails Snov.io has for a domain (0 credits).
EMAIL_COUNT_URL = f"{API_BASE}/v1/get-domain-emails-count"

POLL_INTERVAL = 2.0     # seconds between polls
POLL_TIMEOUT = 60.0     # give up on a single task after this many seconds
EMAILS_PER_PAGE = 50
PROSPECTS_PER_PAGE = 20


class SnovError(Exception):
    pass


# ----------------------------------------------------------------------------
# Auth
# ----------------------------------------------------------------------------
def get_access_token(client_id: str, client_secret: str) -> str:
    resp = requests.post(AUTH_URL, data={
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    })
    if resp.status_code != 200:
        raise SnovError(f"Authentication failed ({resp.status_code}): {resp.text}")
    token = resp.json().get("access_token")
    if not token:
        raise SnovError(f"No access_token in auth response: {resp.text}")
    return token


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ----------------------------------------------------------------------------
# Generic polling helper for start/result endpoints
# ----------------------------------------------------------------------------
def _poll_result(result_url: str, token: str) -> dict:
    """Poll a GET result endpoint until status is completed (or timeout)."""
    deadline = time.time() + POLL_TIMEOUT
    while True:
        resp = requests.get(result_url, headers=_auth_headers(token))
        if resp.status_code != 200:
            raise SnovError(f"Result request failed ({resp.status_code}): {resp.text}")
        data = resp.json()
        status = data.get("status")
        # Explicitly completed -> done.
        if status == "completed":
            return data
        # Still working, OR a result task that hasn't populated yet (status
        # missing/empty and no data yet). Keep polling until the deadline.
        still_working = status in ("in_progress", "pending", "processing")
        not_ready_yet = (status in (None, "")) and not data.get("data") \
            and not _extract_next_url(data)
        if still_working or not_ready_yet:
            if time.time() > deadline:
                raise SnovError(f"Timed out waiting for result at {result_url}")
            time.sleep(POLL_INTERVAL)
            continue
        # Unknown but non-error status, or data is present -> return it.
        return data


def _extract_next_url(body: dict) -> Optional[str]:
    """Find the 'next' page URL. Snov.io has returned 'links' as either a dict
    ({'next': ...}) or a list of link objects, and sometimes a top-level 'next'.
    Handle all of these without assuming a shape."""
    # Top-level next.
    top = body.get("next")
    if isinstance(top, str) and top:
        return top
    links = body.get("links")
    if isinstance(links, dict):
        nxt = links.get("next")
        if isinstance(nxt, str) and nxt:
            return nxt
    elif isinstance(links, list):
        for entry in links:
            if isinstance(entry, dict):
                # e.g. {"rel": "next", "href": "..."} or {"next": "..."}
                if entry.get("rel") == "next" and entry.get("href"):
                    return entry["href"]
                if isinstance(entry.get("next"), str) and entry["next"]:
                    return entry["next"]
    return None


def _extract_result_url(body: dict) -> Optional[str]:
    """Snov.io returns the result link either at top level or under links.
    'links' may be a dict or a list of link objects."""
    top = body.get("result")
    if isinstance(top, str) and top:
        return top
    links = body.get("links")
    if isinstance(links, dict):
        res = links.get("result")
        if isinstance(res, str) and res:
            return res
    elif isinstance(links, list):
        for entry in links:
            if isinstance(entry, dict):
                if entry.get("rel") == "result" and entry.get("href"):
                    return entry["href"]
                if isinstance(entry.get("result"), str) and entry["result"]:
                    return entry["result"]
    return None


def _start_and_result(start_url: str, params: dict, token: str) -> dict:
    """POST to a start endpoint, then follow the result link (or task_hash)."""
    resp = requests.post(start_url, headers=_auth_headers(token), data=params)
    if resp.status_code not in (200, 201, 202):
        raise SnovError(f"Start request failed ({resp.status_code}): {resp.text}")
    body = resp.json()
    result_url = _extract_result_url(body)
    if not result_url:
        task_hash = body.get("task_hash") or body.get("meta", {}).get("task_hash")
        if not task_hash:
            raise SnovError(f"No result URL or task_hash returned: {body}")
        raise SnovError(f"No result URL returned (task_hash={task_hash}).")
    return _poll_result(result_url, token)


# ----------------------------------------------------------------------------
# Step 1: company info (needed for the cost estimate)
# ----------------------------------------------------------------------------
def get_free_email_count(domain: str, token: str) -> Optional[int]:
    """Free endpoint: number of domain emails Snov.io has. 0 credits.
    Returns None for webmail domains or on failure."""
    resp = requests.post(EMAIL_COUNT_URL, data={"access_token": token, "domain": domain})
    if resp.status_code != 200:
        return None
    body = resp.json()
    if not body.get("success"):
        return None
    if body.get("webmail"):
        return None  # Snov.io can't count emails for webmail domains
    return body.get("result", 0)


def get_company_info(domain: str, token: str) -> dict:
    """Returns the 'meta' dict with counts. Costs 1 credit if results found."""
    data = _start_and_result(DS_START, {"domain": domain}, token)
    return data


def summarize_counts(company_data: dict) -> dict:
    meta = company_data.get("meta", {})
    return {
        "prospects_count": meta.get("prospects_count", 0),
        "emails_count": meta.get("emails_count", 0),
        "generic_contacts_count": meta.get("generic_contacts_count", 0),
        "company_name": company_data.get("data", {}).get("company_name", domain_of(company_data)),
    }


def domain_of(company_data: dict) -> str:
    return company_data.get("meta", {}).get("domain", "")


# ----------------------------------------------------------------------------
# Cost estimation
# ----------------------------------------------------------------------------
def estimate_all_emails_cost(emails_count: int) -> int:
    """1 credit per page of up to 50 domain emails."""
    if emails_count <= 0:
        return 0
    return math.ceil(emails_count / EMAILS_PER_PAGE)


def estimate_validated_cost(prospects_count: int) -> int:
    """
    Worst-case: 1 credit per prospect that yields a verified email. We can't
    know in advance how many prospects have verified emails, so this is an
    UPPER BOUND (every prospect charged). Actual charge is only for prospects
    where a verified email (smtp_status valid or unknown) is found.

    Also note: retrieving the prospect list itself costs 1 credit per page of
    up to 20 prospects.
    """
    if prospects_count <= 0:
        return 0
    list_cost = math.ceil(prospects_count / PROSPECTS_PER_PAGE)
    email_cost = prospects_count  # upper bound
    return list_cost + email_cost


def fmt_dollars(credits: int, credit_price: Optional[float]) -> str:
    if credit_price is None:
        return ""
    return f"  (~${credits * credit_price:,.2f} at ${credit_price:.4f}/credit)"


# ----------------------------------------------------------------------------
# Step 2a: retrieve ALL domain emails (unverified)
# ----------------------------------------------------------------------------
def fetch_all_domain_emails(domain: str, token: str, max_pages: Optional[int] = None):
    resp = requests.post(DOMAIN_EMAILS_START, headers=_auth_headers(token),
                         data={"domain": domain})
    if resp.status_code not in (200, 201, 202):
        raise SnovError(f"Domain-emails start failed ({resp.status_code}): {resp.text}")
    result_url = _extract_result_url(resp.json())
    if not result_url:
        raise SnovError(f"No result URL for domain emails: {resp.text}")

    emails = []
    pages = 0
    # First page: GET the result URL. Subsequent pages: the 'links.next' value
    # is a full domain-emails/start?...&next=... URL that must be POSTed, which
    # returns another result URL to GET. We loop that start->result cycle.
    next_result_url = result_url
    while next_result_url:
        data = _poll_result(next_result_url, token)
        for item in data.get("data", []):
            # Domain emails only carry an 'email' field; they are unverified.
            if isinstance(item, dict):
                emails.append({"email": item.get("email", "")})
            else:
                emails.append({"email": str(item)})
        pages += 1
        if max_pages and pages >= max_pages:
            break
        # Follow the next page, if any.
        next_start_url = _extract_next_url(data)
        if not next_start_url:
            break
        r = requests.post(next_start_url, headers=_auth_headers(token))
        if r.status_code not in (200, 201, 202):
            break
        next_result_url = _extract_result_url(r.json())
    return emails


# ----------------------------------------------------------------------------
# Step 2b: retrieve VALIDATED prospect emails
# ----------------------------------------------------------------------------
def fetch_validated_emails(domain: str, token: str, max_prospects: Optional[int] = None):
    """Walk prospect pages, then resolve each prospect's verified email."""
    results = []
    page = 1
    resolved = 0
    while True:
        resp = requests.post(PROSPECTS_START, headers=_auth_headers(token),
                             data={"domain": domain, "page": page})
        if resp.status_code not in (200, 201, 202):
            raise SnovError(f"Prospects start failed ({resp.status_code}): {resp.text}")
        result_url = _extract_result_url(resp.json())
        if not result_url:
            break
        page_data = _poll_result(result_url, token)
        prospects = page_data.get("data", [])
        if not prospects:
            break

        for p in prospects:
            emails_start = p.get("search_emails_start")
            if not emails_start:
                continue
            # Kick off the per-prospect email search.
            r = requests.post(emails_start, headers=_auth_headers(token))
            if r.status_code not in (200, 201, 202):
                continue
            email_result_url = _extract_result_url(r.json())
            if not email_result_url:
                continue
            edata = _poll_result(email_result_url, token)
            email_list = edata.get("data", {}).get("emails", []) if isinstance(
                edata.get("data"), dict) else []
            for em in email_list:
                # Per Snov.io docs, the field is 'smtp_status' with values
                # 'valid' (green) or 'unknown' (yellow). Both count as verified.
                smtp_status = (em.get("smtp_status") or "").lower()
                if smtp_status in ("valid", "unknown"):
                    results.append({
                        "first_name": p.get("first_name", ""),
                        "last_name": p.get("last_name", ""),
                        "position": p.get("position", ""),
                        "email": em.get("email", ""),
                        "smtp_status": em.get("smtp_status", ""),
                        "source_page": p.get("source_page", ""),
                    })
            resolved += 1
            if max_prospects and resolved >= max_prospects:
                return results

        total = page_data.get("meta", {}).get("total_count", 0)
        if page * PROSPECTS_PER_PAGE >= total:
            break
        page += 1
    return results


# ----------------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------------
def write_csv(rows, path, fieldnames):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})


def confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        print(prompt + " [auto-yes]")
        return True
    ans = input(prompt + " [y/N]: ").strip().lower()
    return ans in ("y", "yes")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Pull emails for a client domain from Snov.io, with a "
                    "credit-cost estimate shown before anything is charged.")
    parser.add_argument("domain", help="Client domain, e.g. example.com")
    parser.add_argument("--client-id", default=os.environ.get("SNOV_CLIENT_ID"))
    parser.add_argument("--client-secret", default=os.environ.get("SNOV_CLIENT_SECRET"))
    parser.add_argument("--mode", choices=["validated", "all", "ask"], default="ask",
                        help="'validated' = verified prospect emails only; "
                             "'all' = every domain email (unverified); "
                             "'ask' = show costs and prompt (default).")
    parser.add_argument("--credit-price", type=float, default=None,
                        help="Optional $ per credit for a dollar estimate "
                             "(e.g. 0.039 for the Starter plan).")
    parser.add_argument("--out", default=None, help="Output CSV path.")
    parser.add_argument("--yes", action="store_true",
                        help="Skip confirmation prompts (use with care - spends credits).")
    parser.add_argument("--max-prospects", type=int, default=None,
                        help="Cap prospects resolved in validated mode (limits credit spend).")
    parser.add_argument("--max-pages", type=int, default=None,
                        help="Cap pages fetched in all-emails mode (limits credit spend).")
    parser.add_argument("--free-estimate", action="store_true",
                        help="Use the FREE email-count endpoint to estimate the "
                             "'all emails' cost without spending the 1-credit "
                             "domain lookup, then exit. (Prospect count for the "
                             "validated estimate is unavailable in this mode.)")
    args = parser.parse_args()

    if not args.client_id or not args.client_secret:
        sys.exit("Missing credentials. Set SNOV_CLIENT_ID and SNOV_CLIENT_SECRET, "
                 "or pass --client-id / --client-secret.")

    try:
        print("Authenticating with Snov.io...")
        token = get_access_token(args.client_id, args.client_secret)

        if args.free_estimate:
            print(f"Checking free email count for '{args.domain}' (0 credits)...")
            count = get_free_email_count(args.domain, token)
            if count is None:
                print("No free count available (domain may be webmail or not in "
                      "Snov.io's database). Run without --free-estimate for a full lookup.")
                return
            all_cost = estimate_all_emails_cost(count)
            print("\n" + "=" * 60)
            print(f"Domain: {args.domain}")
            print(f"  Emails in Snov.io database: {count}")
            print(f"  Estimated 'all emails' cost: {all_cost} credits"
                  f"{fmt_dollars(all_cost, args.credit_price)}")
            print("=" * 60)
            print("This was a free estimate; no credits were spent. Run without "
                  "--free-estimate to see the validated-email option and retrieve emails.")
            return

        print(f"Looking up '{args.domain}' (this initial lookup costs 1 credit)...")
        company = get_company_info(args.domain, token)
        counts = summarize_counts(company)

        prospects_n = counts["prospects_count"]
        emails_n = counts["emails_count"]

        all_cost = estimate_all_emails_cost(emails_n)
        validated_cost = estimate_validated_cost(prospects_n)

        print("\n" + "=" * 60)
        print(f"Company:  {counts['company_name']}  ({args.domain})")
        print(f"  Prospect profiles found:        {prospects_n}")
        print(f"  Total domain emails found:      {emails_n}")
        print("=" * 60)
        print("\nEstimated credit cost for each option:\n")
        print(f"  [1] VALIDATED emails only (verified prospect emails)")
        print(f"        up to {validated_cost} credits"
              f"{fmt_dollars(validated_cost, args.credit_price)}")
        print(f"        (upper bound - you're only charged for prospects with a")
        print(f"         green/yellow email; no charge when none is found)\n")
        print(f"  [2] ALL domain emails (unverified, everything on the domain)")
        print(f"        {all_cost} credits"
              f"{fmt_dollars(all_cost, args.credit_price)}")
        print(f"        ({emails_n} emails at 1 credit per {EMAILS_PER_PAGE})\n")
        print("Note: credit values are Snov.io's billing unit; dollar figures")
        print("(if shown) depend on your plan's per-credit rate.")
        print("=" * 60 + "\n")

        # Decide mode
        mode = args.mode
        if mode == "ask":
            choice = input("Choose an option — [1] validated only, "
                           "[2] all emails, [q] quit: ").strip().lower()
            if choice == "1":
                mode = "validated"
            elif choice == "2":
                mode = "all"
            else:
                print("Cancelled. No further credits spent.")
                return

        if mode == "validated":
            est = args.max_prospects if args.max_prospects else validated_cost
            if not confirm(f"Proceed with VALIDATED emails? "
                           f"Up to ~{est} credits will be spent.", args.yes):
                print("Cancelled. No further credits spent.")
                return
            print("Fetching validated prospect emails...")
            rows = fetch_validated_emails(args.domain, token, args.max_prospects)
            fields = ["first_name", "last_name", "position", "email",
                      "smtp_status", "source_page"]
            default_out = f"{args.domain}_validated_emails.csv"

        else:  # all
            est = (args.max_pages if args.max_pages else all_cost)
            if not confirm(f"Proceed with ALL domain emails? "
                           f"~{est} credits will be spent.", args.yes):
                print("Cancelled. No further credits spent.")
                return
            print("Fetching all domain emails...")
            rows = fetch_all_domain_emails(args.domain, token, args.max_pages)
            fields = ["email"]
            default_out = f"{args.domain}_all_emails.csv"

        out_path = args.out or default_out
        write_csv(rows, out_path, fields)
        print(f"\nDone. {len(rows)} rows written to: {out_path}")

    except SnovError as e:
        sys.exit(f"\nSnov.io API error: {e}")
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")


if __name__ == "__main__":
    main()
