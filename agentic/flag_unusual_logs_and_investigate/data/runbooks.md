# Runbooks for the shop's services

Written by the platform team for whoever is on call. The shop and its services are made up.

## When to page

Page the on-call engineer when customers can't pay, can't place an order or can't sign in, or when
errors on any service keep rising across two 15 minute windows. Don't page for INFO lines, for
WARN lines at their usual rate, or for a problem a rollback has already stopped. When in doubt,
post a note in the incident channel and let the on-call engineer decide.

## Payments: card token decryption errors

Every charge decrypts the stored card token first. "decrypt failed" errors mean no charge can go
through, so customers can't pay. If they start within an hour of a payments deploy, roll that
deploy back and page the payments on-call. If there was no deploy, check the key service's health
page and page the security team, because a key may have been rotated or removed.

## Payments: card declines

Card declines with a reason such as "insufficient funds" or "expired card" come from the customer's
bank. A few per 15 minutes is normal. Only look into them if they are many times their usual rate
on every card type at once, which points at the payment processor.

## Checkout: payment step failing

Checkout calls payments for every order. "payment step failed" with "card declined" is the
customer's card. With an upstream error such as 502, payments itself is failing: check payments'
errors and deploys first, because checkout usually recovers on its own when payments does.

## Checkout: slow requests

Checkout should finish an order in under 300 ms. Slow requests usually come from the database or
from payments. Compare request times across the day before acting, because a slow climb is easy to
miss in any one window.

## Auth: token refresh failures

Signed-in customers refresh their token every few minutes. "token refresh failed ... upstream
timeout" means the identity provider didn't answer in time. One or two per 15 minutes is normal.
Far more than that means customers are being signed out. Check auth-config changes in the last
hour first: if one lowered the identity provider timeout, revert it and page the identity team.

## Inventory: stock cache

Inventory keeps stock levels in a cache. Lines about warming or loading the cache, at INFO level,
are expected when an instance starts and need no action. Look into them only if stock reservations
start failing or the "stock low" warnings jump at the same time.

## Frontend: rate limiting

A 429 from the frontend means one client sent too many requests. A few per 15 minutes is normal
and needs no action. Many from different clients at once can mean the limit is set too low after a
deploy.

## Search: slow queries

"slow query" warnings above 1000 ms are normal at a rate of one or two per 15 minutes. If they rise
with no deploy, the search index may be rebuilding.

## Worker: failed jobs

Background jobs retry up to three times. A failed job on its third retry needs a ticket for the
owning team, not a page, unless many jobs fail at once.

## Any service: a new line right after a deploy

A line that has never been seen before, starting within minutes of a deploy to the same service,
usually comes from that deploy. Read the deploy's change note. If the new line is an error and
customers are affected, roll back. If it is INFO and nothing else changed, it needs no action.
