"""Write the seed data for the five systems the recipe looks things up in: seven incidents at a
fictional online shop.

Everything here is invented: Larkspur Outfitters, its services, its engineers and every hostname
(under the reserved `.example` domain). Each CSV seeds one lookup module in this folder, which
loads it into an in-memory SQLite database and answers the questions the real system's API
would:

    data/incidents.csv        incident_tool.py   ID, title, Slack channel, affected services
    data/slack_users.csv      slack.py           the workspace directory (id, handle, name)
    data/slack_messages.csv   slack.py           every incident channel's messages (`ts` is epoch
                                                 seconds, as Slack gives it)
    data/alert_events.csv     alert_history.py   Alertmanager-style firing and resolved events
                                                 (with startsAt and endsAt) for every service
    data/deploys.csv          deploy_log.py      deploys, rollbacks and certificate renewals
    data/flag_audit.csv       flag_audit.py      feature-flag flips and runtime config changes

Alerts and changes aren't tagged with an incident; the recipe asks for them by service and time,
so the seeds also hold events for unrelated services. The cases they test are in the README.

    uv run make_data.py
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"

USERS = {
    "U01MAYA": ("maya.ortiz", "Maya Ortiz"),
    "U02DEV": ("dev.patel", "Dev Patel"),
    "U03SAM": ("sam.okafor", "Sam Okafor"),
    "U04LENA": ("lena.fischer", "Lena Fischer"),
    "U05THEO": ("theo.marsh", "Theo Marsh"),
    "U06PRIYA": ("priya.raman", "Priya Raman"),
    "U07JULES": ("jules.bernard", "Jules Bernard"),
}
USER_IDS = {handle: user_id for user_id, (handle, _name) in USERS.items()}

# (incident, title, channel, services, date)
INCIDENTS = [
    (
        "INC-0101",
        "Checkout returns 500s",
        "inc-0101-checkout-500s",
        ["checkout", "frontend"],
        "2026-10-02",
    ),
    (
        "INC-0102",
        "Order pages time out",
        "inc-0102-orders-db",
        ["orders", "orders-db"],
        "2026-10-07",
    ),
    ("INC-0103", "Mobile API TLS failures", "inc-0103-gateway-tls", ["api-gateway"], "2026-10-12"),
    ("INC-0104", "Search latency spike", "inc-0104-search-latency", ["search"], "2026-10-15"),
    (
        "INC-0105",
        "Payment webhook backlog",
        "inc-0105-webhooks",
        ["payments-webhooks"],
        "2026-10-19",
    ),
    (
        "INC-0106",
        "Avatar uploads failing",
        "inc-0106-avatar-uploads",
        ["media-upload"],
        "2026-10-22",
    ),
    (
        "INC-0107",
        "Fulfillment queue lag",
        "inc-0107-fulfillment-lag",
        ["fulfillment-worker"],
        "2026-10-26",
    ),
]

# incident -> [(time, user, text)]; the case it tests is in the comment above each one.
MESSAGES: dict[str, list[tuple[str, str, str]]] = {
    # Clean: a deploy shrinks a connection pool; rollback fixes it. A clock time is on the
    # incident's date; a full "date time" is a message on another day.
    "INC-0101": [
        (
            "13:57:10",
            "maya.ortiz",
            "Paged for CheckoutHigh5xxRate. Checkout is returning 500s for "
            "about a quarter of orders. I'm IC; opening this channel.",
        ),
        (
            "13:59:40",
            "dev.patel",
            "checkout v2.14.0 went out at 13:52, a few minutes before the errors started.",
        ),
        (
            "14:04:15",
            "dev.patel",
            "Logs show 'connection pool exhausted: payment-client max=8' on "
            "every failed order. v2.14.0 moved the payment client to an async pool with a max of 8 "
            "connections, down from 64.",
        ),
        ("14:06:02", "maya.ortiz", "Decision: roll back checkout to v2.13.4. Dev, go ahead."),
        (
            "14:07:30",
            "jules.bernard",
            "Support has 41 tickets from customers who saw an error "
            "page at checkout or were told their card was declined.",
        ),
        ("14:22:48", "dev.patel", "Rollback to v2.13.4 finished. Error rate is dropping."),
        (
            "14:33:05",
            "maya.ortiz",
            "5xx alert resolved. Orders are completing normally again. Moving to monitoring.",
        ),
        (
            "14:45:20",
            "sam.okafor",
            "Why did this pass staging? Staging only runs 5 concurrent "
            "checkouts, so a pool of 8 was never exhausted there.",
        ),
        (
            "14:47:00",
            "maya.ortiz",
            "Action items: Dev adds a load test with production-level "
            "concurrency to the checkout pipeline. Sam adds an alert on payment-client pool "
            "saturation. Jules sends the follow-up email to affected customers.",
        ),
        ("14:48:30", "maya.ortiz", "Closing the incident. Postmortem draft due Friday."),
        # Late impact confirmation: support's final count, the next morning.
        (
            "2026-10-03 08:40:00",
            "jules.bernard",
            "Final count from support for yesterday's checkout errors: 63 tickets. Payments "
            "logs show 1,140 failed checkout attempts from 812 customers.",
        ),
    ],
    # Clean: a feature flag sends one query per line item and exhausts DB connections.
    "INC-0102": [
        (
            "10:06:00",
            "theo.marsh",
            "Paged: orders-db is at 498 of 500 connections and the orders "
            "API p95 is 4.8 seconds. Order history pages are timing out.",
        ),
        (
            "10:11:30",
            "priya.raman",
            "I turned on recommendations_on_order_page at 09:58. It "
            "queries orders-db once per line item on the order page.",
        ),
        (
            "10:15:45",
            "theo.marsh",
            "pg_stat_activity shows hundreds of identical SELECTs from the "
            "recommendations widget. That matches Priya's flag.",
        ),
        (
            "10:18:20",
            "theo.marsh",
            "The flag went to 100% of users at once; there was no percentage rollout.",
        ),
        ("10:40:55", "theo.marsh", "Turning the flag off now."),
        (
            "10:50:10",
            "theo.marsh",
            "Connections back to 120, p95 under 300 ms. Both alerts resolved.",
        ),
        (
            "10:58:00",
            "lena.fischer",
            "Customers could still place orders; only order history and "
            "order detail pages timed out.",
        ),
        (
            "11:05:30",
            "theo.marsh",
            "Follow-ups: Priya batches the recommendation queries into one "
            "query per order. Lena makes percentage rollout the default for new flags. I'll add a "
            "per-feature connection limit on orders-db.",
        ),
    ],
    # Near miss: a deploy lands mid-incident, but the cause is an expired certificate.
    "INC-0103": [
        (
            "09:03:00",
            "sam.okafor",
            "Paged: TLS handshake failures on api.larkspur.example, almost "
            "every mobile app request is failing.",
        ),
        ("09:14:20", "lena.fischer", "Gateway v3.8.1 just rolled out. Could it be the deploy?"),
        (
            "09:17:05",
            "sam.okafor",
            "No. The failures started at 09:00:40 and the deploy started "
            "at 09:12:30. The deploy came after the errors.",
        ),
        (
            "09:21:40",
            "sam.okafor",
            "Found it: the certificate for api.larkspur.example expired at "
            "09:00:00 UTC. The auto-renew job has been failing since September because its DNS API "
            "token was rotated.",
        ),
        ("09:41:30", "sam.okafor", "Renewed the certificate by hand and reloaded the gateway."),
        ("09:45:00", "sam.okafor", "Handshake failures back to zero, alert resolved."),
        (
            "09:52:10",
            "jules.bernard",
            "Mobile app users saw 'cannot connect' the whole time; web "
            "checkout was not affected because it uses a different hostname.",
        ),
        (
            "10:01:00",
            "sam.okafor",
            "Action items: I'll fix the renew job's DNS token and add an "
            "alert when any certificate is under 14 days from expiry. Lena adds a check that the "
            "renew job succeeded in the last 7 days.",
        ),
    ],
    # Escalate: nobody found the cause; it recovered on its own.
    "INC-0104": [
        (
            "15:54:30",
            "lena.fischer",
            "Paged: search p99 is 3.9 seconds. Results are slow but still returning.",
        ),
        (
            "16:02:10",
            "lena.fischer",
            "No search deploys today and no flag changes. CPU and memory "
            "on the search nodes look normal.",
        ),
        (
            "16:09:45",
            "dev.patel",
            "Index shard sizes look normal too. Nothing obvious in the slow query log.",
        ),
        (
            "16:21:00",
            "lena.fischer",
            "Latency is back to normal on its own and the alert "
            "resolved. We still don't know what caused it.",
        ),
        (
            "16:25:30",
            "lena.fischer",
            "Action item: Dev keeps per-shard latency metrics for this "
            "window so we can dig in tomorrow. Root cause still open.",
        ),
    ],
    # Escalate: the paging alert is still firing when the export was taken.
    "INC-0105": [
        (
            "08:07:40",
            "priya.raman",
            "Paged: 12,400 payment webhooks undelivered. Merchants are not "
            "getting payment confirmations.",
        ),
        (
            "08:20:15",
            "priya.raman",
            "The partner endpoint answers 429 Too Many Requests. v1.9.0 "
            "raised the retry ceiling and our retries are hammering it.",
        ),
        ("08:34:50", "theo.marsh", "Paused retries for the partner; the backlog stopped growing."),
        (
            "08:58:30",
            "priya.raman",
            "Backlog is draining slowly, alert still firing. Handing over "
            "to the EU on-call; keep this channel open.",
        ),
    ],
    # Escalate: customer-reported; only a warning alert, never a page. The warning fired soon
    # after the 10:15 deploy, more than the alert lookback before the first message, so only its
    # resolved event is in the record (truncated alert history).
    "INC-0106": [
        (
            "11:42:00",
            "jules.bernard",
            "Customers report avatar uploads fail with 'something went "
            "wrong'. 17 tickets in the last hour.",
        ),
        (
            "11:50:30",
            "dev.patel",
            "media-upload returns 500 for HEIC images since v4.2.0 went out "
            "at 10:15; the new libvips build has no HEIC support.",
        ),
        ("12:05:10", "dev.patel", "Rolled media-upload back to v4.1.3."),
        ("12:20:00", "jules.bernard", "Customers confirm uploads work again."),
        (
            "12:31:00",
            "dev.patel",
            "Action items: I'll add HEIC samples to the upload tests. Sam "
            "makes the upload error-rate alert page when it is above 5%.",
        ),
    ],
    # Clean, with a flapping alert: the window runs from the first firing to the last resolve.
    "INC-0107": [
        (
            "18:43:00",
            "theo.marsh",
            "Paged: fulfillment queue lag over 15 minutes. Shipping labels are being created late.",
        ),
        ("18:54:00", "theo.marsh", "Lag alert resolved by itself. Watching."),
        ("19:05:00", "theo.marsh", "It fired again. This is not transient."),
        (
            "19:12:40",
            "lena.fischer",
            "At 18:30 I lowered consumer_concurrency from 16 to 2 to cut "
            "costs. Evening order volume is more than 2 consumers can keep up with.",
        ),
        ("19:22:30", "lena.fischer", "Reverted consumer_concurrency to 16."),
        ("19:32:00", "theo.marsh", "Lag back under a minute, alert resolved."),
        (
            "19:40:00",
            "theo.marsh",
            "Action items: Lena adds a capacity check before changing "
            "worker concurrency. I'll make the lag alert wait 10 minutes before resolving so it "
            "does not flap.",
        ),
    ],
}

# (fingerprint, alertname, service, severity, starts, ends or None, summary). `starts`/`ends` are
# full timestamps; an alert with an end gets a firing event and a resolved event.
ALERTS: list[tuple[str, str, str, str, str, str | None, str]] = [
    (
        "a1f0",
        "CheckoutHigh5xxRate",
        "checkout",
        "critical",
        "2026-10-02T13:55:30Z",
        "2026-10-02T14:31:30Z",
        "checkout 5xx rate 23% over 5m (threshold 2%)",
    ),
    (
        "a1f1",
        "FrontendErrorBudgetBurn",
        "frontend",
        "warning",
        "2026-10-02T13:58:00Z",
        "2026-10-02T14:40:00Z",
        "frontend availability error budget burning at 6x over 1h",
    ),
    (
        "b2c0",
        "OrdersDbConnectionsSaturated",
        "orders-db",
        "critical",
        "2026-10-07T10:04:00Z",
        "2026-10-07T10:46:30Z",
        "orders-db connections at 498/500",
    ),
    (
        "b2c1",
        "OrdersApiLatencyHigh",
        "orders",
        "critical",
        "2026-10-07T10:05:20Z",
        "2026-10-07T10:49:00Z",
        "orders API p95 latency 4.8s (threshold 500ms)",
    ),
    (
        "c3d0",
        "GatewayTlsHandshakeFailures",
        "api-gateway",
        "critical",
        "2026-10-12T09:00:40Z",
        "2026-10-12T09:44:10Z",
        "TLS handshake failures 96% on api.larkspur.example",
    ),
    (
        "d4e0",
        "SearchLatencyP99High",
        "search",
        "critical",
        "2026-10-15T15:52:10Z",
        "2026-10-15T16:20:40Z",
        "search p99 latency 3.9s (threshold 800ms)",
    ),
    (
        "e5f0",
        "WebhookDeliveryBacklog",
        "payments-webhooks",
        "critical",
        "2026-10-19T08:05:00Z",
        None,
        "12,400 undelivered payment webhooks",
    ),
    (
        "f6a0",
        "MediaUploadErrorRate",
        "media-upload",
        "warning",
        "2026-10-22T10:21:00Z",
        "2026-10-22T12:09:30Z",
        "media-upload 5xx rate 3.1% over 15m (threshold 1%)",
    ),
    (
        "a7b0",
        "FulfillmentQueueLagHigh",
        "fulfillment-worker",
        "critical",
        "2026-10-26T18:41:00Z",
        "2026-10-26T18:52:30Z",
        "fulfillment queue lag 16m (threshold 15m)",
    ),
    (
        "a7b0",
        "FulfillmentQueueLagHigh",
        "fulfillment-worker",
        "critical",
        "2026-10-26T19:03:10Z",
        "2026-10-26T19:31:20Z",
        "fulfillment queue lag 22m (threshold 15m)",
    ),
    # Unrelated: other services, same days. The recipe must leave these out.
    (
        "x900",
        "RecommendationMemoryHigh",
        "recommendation",
        "warning",
        "2026-10-02T14:10:00Z",
        "2026-10-02T15:30:00Z",
        "recommendation container memory at 91% of its limit",
    ),
    (
        "x901",
        "SearchIndexLagHigh",
        "search",
        "warning",
        "2026-10-12T09:20:00Z",
        "2026-10-12T09:35:00Z",
        "search index lag 6m",
    ),
]

# The deploy pipeline's history: (time, kind, service, version, actor, note).
DEPLOYS: list[tuple[str, str, str, str, str, str]] = [
    (
        "2026-10-02T11:20:00Z",
        "deploy",
        "search",
        "v5.2.1",
        "ci-pipeline-8790",
        "synonym list update",
    ),
    (
        "2026-10-02T13:52:04Z",
        "deploy",
        "checkout",
        "v2.14.0",
        "ci-pipeline-8812",
        "move payment client to async pool",
    ),
    ("2026-10-02T14:21:55Z", "rollback", "checkout", "v2.13.4", "dev.patel", "roll back v2.14.0"),
    (
        "2026-10-12T09:12:30Z",
        "deploy",
        "api-gateway",
        "v3.8.1",
        "ci-pipeline-9031",
        "scheduled release: header normalization",
    ),
    (
        "2026-10-12T09:41:15Z",
        "cert_renewal",
        "api-gateway",
        "",
        "sam.okafor",
        "api.larkspur.example renewed, valid until 2027-01-10T09:41:00Z",
    ),
    (
        "2026-10-15T13:00:00Z",
        "deploy",
        "checkout",
        "v2.14.1",
        "ci-pipeline-9120",
        "async payment pool, max 64",
    ),
    (
        "2026-10-19T07:40:00Z",
        "deploy",
        "payments-webhooks",
        "v1.9.0",
        "ci-pipeline-9188",
        "raise retry backoff ceiling",
    ),
    (
        "2026-10-22T10:15:00Z",
        "deploy",
        "media-upload",
        "v4.2.0",
        "ci-pipeline-9240",
        "switch image library to libvips 8.16",
    ),
    ("2026-10-22T12:04:40Z", "rollback", "media-upload", "v4.1.3", "dev.patel", "roll back v4.2.0"),
]

# The flag system's audit log, runtime config included: (time, kind, service, name, change,
# actor, note).
FLAG_CHANGES: list[tuple[str, str, str, str, str, str, str]] = [
    (
        "2026-10-07T09:58:40Z",
        "flag",
        "orders",
        "recommendations_on_order_page",
        "on, rollout 100%",
        "priya.raman",
        "",
    ),
    (
        "2026-10-07T10:41:10Z",
        "flag",
        "orders",
        "recommendations_on_order_page",
        "off",
        "theo.marsh",
        "",
    ),
    (
        "2026-10-26T18:30:00Z",
        "config",
        "fulfillment-worker",
        "consumer_concurrency",
        "16 -> 2",
        "lena.fischer",
        "cost reduction",
    ),
    (
        "2026-10-26T19:22:00Z",
        "config",
        "fulfillment-worker",
        "consumer_concurrency",
        "2 -> 16",
        "lena.fischer",
        "revert",
    ),
]

ZERO_TIME = "0001-01-01T00:00:00Z"  # Alertmanager's endsAt for an alert that is still firing


def _epoch(date: str, clock: str) -> str:
    """Epoch seconds as Slack writes `ts`; `clock` is a time on `date`, or a full date and time."""
    stamp = clock if " " in clock else f"{date} {clock}"
    moment = datetime.fromisoformat(stamp.replace(" ", "T") + "+00:00").astimezone(UTC)
    return f"{moment.timestamp():.6f}"


def _csv(header: list[str], rows: list[list[str]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue()


def incidents_csv() -> str:
    rows = [
        [incident, title, channel, ";".join(services)]
        for incident, title, channel, services, _date in INCIDENTS
    ]
    return _csv(["incident", "title", "channel", "services"], rows)


def slack_users_csv() -> str:
    rows = [[user_id, handle, name] for user_id, (handle, name) in USERS.items()]
    return _csv(["id", "handle", "real_name"], rows)


def slack_messages_csv() -> str:
    rows = [
        [channel, _epoch(date, clock), USER_IDS[user], text]
        for incident, _title, channel, _services, date in INCIDENTS
        for clock, user, text in MESSAGES[incident]
    ]
    return _csv(["channel", "ts", "user", "text"], rows)


def alert_events_csv() -> str:
    """One firing event per alert, and a resolved event for every alert that has ended."""
    events = []
    for fingerprint, name, service, severity, starts, ends, summary in ALERTS:
        base = [fingerprint, name, service, severity, starts]
        events.append(["firing", *base, ZERO_TIME, summary])
        if ends is not None:
            events.append(["resolved", *base, ends, summary])
    events.sort(key=lambda e: e[5] if e[0] == "firing" else e[6])
    header = ["status", "fingerprint", "alertname", "service", "severity", "starts_at"]
    return _csv([*header, "ends_at", "summary"], events)


def deploys_csv() -> str:
    return _csv(
        ["at", "kind", "service", "version", "actor", "note"], [list(row) for row in DEPLOYS]
    )


def flag_audit_csv() -> str:
    return _csv(
        ["at", "kind", "service", "name", "change", "actor", "note"],
        [list(row) for row in FLAG_CHANGES],
    )


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    files = {
        "incidents.csv": incidents_csv(),
        "slack_users.csv": slack_users_csv(),
        "slack_messages.csv": slack_messages_csv(),
        "alert_events.csv": alert_events_csv(),
        "deploys.csv": deploys_csv(),
        "flag_audit.csv": flag_audit_csv(),
    }
    for name, text in files.items():
        (DATA_DIR / name).write_text(text, encoding="utf-8", newline="")
    print(f"wrote {len(INCIDENTS)} incidents and {len(files)} seed files to {DATA_DIR}")


if __name__ == "__main__":
    main()
