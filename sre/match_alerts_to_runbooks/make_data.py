"""Write the synthetic alert storm, the log platform seed and the wiki seed in data/.

Alertmanager posts one webhook per notification, and every notification for a group repeats
all of the group's alerts that are still firing, so the same alert turns up several times. This
script plays four monitoring stacks through one storm and writes what a webhook receiver would
have logged:

- data/alertmanager-webhooks.jsonl: one webhook POST body per line, in the shape of
  Alertmanager's webhook payload (version 4). Each stack names its labels its own way.
- data/logs.csv: the log platform's lines for the same morning, split into time, environment,
  service, level and message.
- data/wiki_runbooks.csv: the runbook pages in the team wiki, one runbook per row.

Everything is invented. The alerts and log lines imitate the OpenTelemetry demo shop with five
of its fault flags on; they were not captured from a running demo.

    uv run --group data make_data.py
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
WIKI = "https://wiki.example.internal/sre/runbooks"
# Alertmanager waits this long before the first notification for a new group, and this long
# between notifications while a group keeps getting new alerts.
GROUP_WAIT = timedelta(seconds=30)
GROUP_INTERVAL = timedelta(minutes=5)
NOT_ENDED = "0001-01-01T00:00:00Z"


@dataclass(frozen=True)
class Stack:
    """One monitoring stack and how its alert rules name things."""

    name: str
    receiver: str
    service_label: str
    env_label: str
    severity_label: str
    summary_annotation: str


PLATFORM = Stack("platform", "noc-pager", "service", "env", "severity", "summary")
STOREFRONT = Stack("storefront", "storefront-noc", "job", "environment", "priority", "description")
DATA = Stack("data", "data-noc", "app", "env", "severity", "summary")
PAYMENTS = Stack("payments", "payments-oncall", "service", "env", "severity", "summary")
STACKS = {stack.name: stack for stack in (PLATFORM, STOREFRONT, DATA, PAYMENTS)}
SEVERITY = {"critical": "P1", "warning": "P2", "info": "P3"}


@dataclass(frozen=True)
class Alert:
    stack: str
    alertname: str
    service: str
    severity: str
    starts: str  # HH:MM:SS on 2026-10-14, UTC
    summary: str
    env: str = "production"
    runbook: str = ""  # page slug in the wiki, for the stacks that set runbook_url
    ends: str = ""  # HH:MM:SS when the alert resolved


# The storm, in the order the alerts started. The comment names the fault flag behind each one.
ALERTS = [
    # Resolved before the storm: a short cart blip, sent once firing and once resolved.
    Alert("storefront", "CartValkeyLatencyHigh", "cart", "warning", "08:41:00",
          "cart GetCart p95 latency 650ms (threshold 300ms)", ends="08:52:00"),
    Alert("platform", "PaymentChargeErrorRateHigh", "payment", "critical", "09:02:10",
          "oteldemo.PaymentService/Charge error rate 48% over 5m (threshold 5%)"),  # payment
    Alert("storefront", "AdServiceCpuHigh", "ad", "warning", "09:02:40",
          "ad container CPU at 97% of its limit for 10m"),  # adHighCpu
    Alert("platform", "CheckoutPlaceOrderErrors", "checkout", "critical", "09:03:05",
          "PlaceOrder failing for 47% of orders: payment Charge returned 'Payment request "
          "failed. Invalid token.'"),  # payment
    Alert("storefront", "RecommendationMemoryHigh", "recommendation", "warning", "09:03:30",
          "recommendation container memory at 92% of its limit, growing steadily for 40m"),
    Alert("data", "KafkaConsumerLagHigh", "accounting", "warning", "09:03:55",
          "accounting consumer group lag on Kafka topic 'orders' is 48,000 messages and "
          "rising"),  # kafkaQueueProblems
    Alert("storefront", "AdServiceLatencyHigh", "ad", "warning", "09:04:20",
          "oteldemo.AdService/GetAds p95 latency 2.4s (threshold 300ms)"),
    Alert("storefront", "ProductCatalogGetProductErrors", "product-catalog", "critical",
          "09:04:45", "oteldemo.ProductCatalogService/GetProduct error rate 11%; every failure "
          "is for product id OLJCESPC7Z"),  # productCatalogFailure
    Alert("platform", "PaymentChargeSloBurn", "payment", "critical", "09:05:10",
          "payment Charge availability SLO burning at 14x over 1h"),
    Alert("storefront", "RecommendationCacheSizeGrowing", "recommendation", "warning",
          "09:05:35", "recommendation app.cache_size keeps rising without bound; cache hit "
          "ratio 0%"),  # recommendationCacheFailure
    Alert("data", "FraudDetectionConsumerLag", "fraud-detection", "warning", "09:06:00",
          "fraud-detection consumer group lag on Kafka topic 'orders' is 47,500 messages and "
          "rising"),
    Alert("platform", "CheckoutPaymentCallErrors", "checkout", "critical", "09:06:25",
          "checkout to payment Charge gRPC calls returning UNKNOWN for 46% of requests"),
    Alert("storefront", "AdServiceCpuThrottling", "ad", "warning", "09:06:50",
          "ad container CPU throttled in 78% of scheduling periods"),
    Alert("platform", "FrontendProductPageErrors", "frontend", "warning", "09:07:15",
          "GET /api/products/OLJCESPC7Z returns 500 on every request; product catalog "
          "GetProduct fails"),
    Alert("storefront", "RecommendationLatencyHigh", "recommendation", "warning", "09:07:40",
          "oteldemo.RecommendationService/ListRecommendations p95 latency 1.9s while "
          "recommendation memory grows"),
    Alert("data", "KafkaBrokerRequestQueueHigh", "kafka", "warning", "09:08:05",
          "Kafka broker request queue time 3.1s; consumers of topic 'orders' falling behind"),
    Alert("platform", "FrontendCheckoutErrors", "frontend", "critical", "09:08:30",
          "POST /api/checkout returns 500 for 45% of requests; error says payment charge "
          "failed"),
    Alert("platform", "FrontendAdsSlow", "frontend", "info", "09:08:55",
          "frontend calls to ad GetAds p95 2.6s; ad banner times out while ad CPU is "
          "saturated"),
    Alert("storefront", "RecommendationOOMKilled", "recommendation", "critical", "09:09:20",
          "recommendation container restarted after OOMKilled (exit 137); memory grew until "
          "the limit"),
    Alert("storefront", "ProductCatalogSpanErrors", "product-catalog", "warning", "09:09:45",
          "product-catalog GetProduct spans with status ERROR: 'Product Catalog Fail Feature "
          "Flag Enabled'"),
    Alert("data", "AccountingOrdersDelayed", "accounting", "warning", "09:10:10",
          "accounting consumes orders from Kafka topic 'orders' 25 minutes behind; consumer "
          "lag rising"),
    Alert("platform", "PaymentSpanErrors", "payment", "warning", "09:10:35",
          "payment Charge spans with status ERROR: 'Payment request failed. Invalid token.' "
          "app.loyalty.level=gold"),
    Alert("storefront", "AdServiceRequestTimeouts", "ad", "warning", "09:11:00",
          "ad GetAds DEADLINE_EXCEEDED for 22% of requests while ad CPU is saturated"),
    Alert("storefront", "RecommendationErrors", "recommendation", "warning", "09:11:25",
          "recommendation ListRecommendations error rate 9% during OOM restarts"),
    Alert("platform", "CheckoutGetProductErrors", "checkout", "warning", "09:11:50",
          "checkout PrepareOrderItems: product catalog GetProduct fails for product id "
          "OLJCESPC7Z"),
    Alert("data", "KafkaProducerSendLatency", "kafka", "warning", "09:12:15",
          "Kafka producer send latency to topic 'orders' p95 4.8s; broker request queue "
          "backing up"),
    Alert("platform", "OrdersPlacedDrop", "checkout", "critical", "09:12:40",
          "orders placed per minute down 52% vs 1h ago while payment Charge failures rise"),
    Alert("platform", "FrontendRecommendationsSlow", "frontend", "info", "09:13:05",
          "frontend calls to recommendation ListRecommendations p95 2.1s"),
    Alert("storefront", "AdServiceJvmCpuHigh", "ad", "warning", "09:13:30",
          "ad JVM process CPU utilization 0.96 for 15m"),
    # The payments team's own rules carry a runbook link (meant to come out linked_runbook).
    Alert("payments", "PaymentErrorBudgetBurn", "payment", "critical", "09:13:55",
          "payment error budget for Charge burning at 12x over 6h",
          runbook="rb-01-payment-charge-failing"),
    Alert("payments", "PaymentChargeFailuresByLoyaltyLevel", "payment", "warning", "09:14:20",
          "Charge failures concentrated on app.loyalty.level=gold (49% of gold charges fail)",
          runbook="rb-01-payment-charge-failing"),
    # Staging, same words, a different incident (meant to come out a separate group).
    Alert("platform", "PaymentChargeErrorRateHigh", "payment", "critical", "09:14:45",
          "oteldemo.PaymentService/Charge error rate 100% over 5m (threshold 5%)",
          env="staging"),
    Alert("platform", "CheckoutPlaceOrderErrors", "checkout", "critical", "09:15:10",
          "PlaceOrder failing for 100% of orders: payment Charge returned 'Payment request "
          "failed. Invalid token.'", env="staging"),
]  # fmt: skip

# The log platform's lines: (HH:MM:SS, env, service, level, message).
LOGS = [
    ("08:41:12", "production", "cart", "warn", "GetCart took 702 ms; valkey-cart slow to answer"),
    ("08:51:40", "production", "cart", "info", "valkey-cart latency back to normal"),
    ("08:59:58", "production", "flagd", "info", "flag configuration updated; flags on: "
     "paymentFailure, adHighCpu, recommendationCacheFailure, productCatalogFailure, "
     "kafkaQueueProblems"),
    ("09:01:44", "production", "payment", "error", "Payment request failed. Invalid token. "
     "app.loyalty.level=gold"),
    ("09:01:52", "production", "checkout", "error", "failed to charge card: could not charge the "
     "card: rpc error: code = Unknown desc = Payment request failed. Invalid token. "
     "app.loyalty.level=gold"),
    ("09:02:03", "production", "frontend", "error", "POST /api/checkout 500: failed to place "
     "order: failed to charge card"),
    ("09:02:18", "production", "ad", "info", "High CPU-Load problempattern enabled by feature "
     "flag adHighCpu"),
    ("09:02:31", "production", "recommendation", "info", "Cache miss for product ids; "
     "app.cache_size=1048576 app.products.count=10"),
    ("09:02:47", "production", "product-catalog", "error", "Error: Product Catalog Fail Feature "
     "Flag Enabled product_id=OLJCESPC7Z"),
    ("09:03:09", "production", "accounting", "warn", "Consumed order with delay; consumer lag on "
     "topic orders growing"),
    ("09:03:21", "production", "kafka", "warn", "Producer request on topic orders throttled; "
     "request queue time 3104 ms"),
    ("09:03:40", "production", "payment", "error", "Payment request failed. Invalid token. "
     "app.loyalty.level=gold"),
    ("09:04:02", "production", "frontend", "error", "GET /api/products/OLJCESPC7Z 500: 13 "
     "INTERNAL: Error: Product Catalog Fail Feature Flag Enabled"),
    ("09:04:26", "production", "ad", "warn", "GetAds took 2381 ms"),
    ("09:04:58", "production", "fraud-detection", "warn", "Consumer poll returned 500 records; lag "
     "on topic orders 47500"),
    ("09:05:15", "production", "recommendation", "info", "Cache miss for product ids; "
     "app.cache_size=33554432 app.products.count=10"),
    ("09:06:40", "production", "checkout", "error", "failed to prepare order: failed to get "
     "product OLJCESPC7Z: rpc error: code = Internal desc = Error: Product Catalog Fail Feature "
     "Flag Enabled"),
    ("09:07:02", "production", "ad", "warn", "GetAds took 2655 ms"),
    ("09:08:44", "production", "recommendation", "info", "Cache miss for product ids; "
     "app.cache_size=268435456 app.products.count=10"),
    ("09:09:18", "production", "recommendation", "error", "container recommendation exited with "
     "code 137 (OOMKilled); restarting"),
    ("09:09:51", "production", "accounting", "warn", "Consumed order 25m after it was produced"),
    ("09:10:12", "production", "frontend", "error", "POST /api/checkout 500: failed to place "
     "order: failed to charge card"),
    ("09:11:03", "production", "ad", "error", "GetAds failed: DEADLINE_EXCEEDED"),
    ("09:11:29", "production", "recommendation", "error", "ListRecommendations failed: connection "
     "reset during restart"),
    ("09:12:20", "production", "kafka", "warn", "Producer send to topic orders took 4810 ms"),
    # Staging: no flag change, but a deploy of a new card-processor client ten minutes earlier.
    ("09:04:30", "staging", "argocd", "info", "sync payment to v1.9.0 in staging: card-processor "
     "client upgraded"),
    ("09:14:02", "staging", "payment", "error", "Payment request failed. Invalid token. card "
     "processor sandbox rejected the API key"),
    ("09:14:31", "staging", "checkout", "error", "failed to charge card: rpc error: code = "
     "Unknown desc = Payment request failed. Invalid token."),
    ("09:14:58", "staging", "payment", "error", "Payment request failed. Invalid token. card "
     "processor sandbox rejected the API key"),
]  # fmt: skip

# The wiki's runbook pages: (slug, title, owner team, symptoms, check, mitigate, escalate).
RUNBOOKS = [
    ("rb-01-payment-charge-failing", "Payment: Charge requests failing", "payments",
     "`oteldemo.PaymentService/Charge` returns errors; checkout `PlaceOrder` fails with "
     "\"failed to charge card\"; the frontend returns 500 on `POST /api/checkout`; orders per "
     "minute drop.",
     "1. In traces, filter payment Charge spans with status ERROR. Note the error message and "
     "the `app.loyalty.level` attribute: failures limited to one loyalty level point to the "
     "`paymentFailure` feature flag, not the card processor. 2. Open the flagd UI and read the "
     "`paymentFailure` value (0 = off; 0.5 = half of charges fail).",
     "Set `paymentFailure` to `off` in flagd. If the flag is already off, roll back the last "
     "payment deploy. Charge errors should fall below 5% within 5 minutes.",
     "Payments team on-call if errors persist 15 minutes after mitigation."),
    ("rb-02-ad-high-cpu", "Ad service: high CPU and slow GetAds", "ads",
     "The ad container runs near its CPU limit and is throttled; `GetAds` p95 latency rises; "
     "GetAds returns DEADLINE_EXCEEDED; the frontend ad banner times out.",
     "1. Compare ad CPU to its limit and look for throttled periods. 2. Read the ad service log "
     "for \"High CPU-Load problempattern\"; it means the `adHighCpu` flag is on.",
     "Set `adHighCpu` to `off` in flagd. Otherwise scale the ad deployment out by one replica. "
     "Ads are not on the checkout path, so this is not a revenue emergency.",
     "Ads team in business hours."),
    ("rb-03-recommendation-memory", "Recommendation: memory growth and cache failure",
     "recommendations",
     "Recommendation memory grows steadily toward its limit; `app.cache_size` keeps rising "
     "while the cache hit ratio is near zero; ListRecommendations slows down; the container is "
     "OOMKilled (exit 137) and restarts.",
     "1. Plot `app.cache_size` against container memory. Growth without hits means the cache "
     "is never read back, which is what the `recommendationCacheFailure` flag does. 2. Count "
     "restarts with reason OOMKilled in the last hour.",
     "Set `recommendationCacheFailure` to `off` in flagd, then restart the recommendation "
     "deployment to free the memory. Do not raise the memory limit; it only delays the next "
     "OOM kill.",
     "Recommendations team on-call if memory keeps growing after the restart."),
    ("rb-04-product-catalog-getproduct", "Product catalog: GetProduct failing", "catalog",
     "`oteldemo.ProductCatalogService/GetProduct` returns errors; product pages return 500; "
     "checkout fails to prepare order items.",
     "1. Group GetProduct failures by `app.product.id`. Failures for a single product id with "
     "the message \"Product Catalog Fail Feature Flag Enabled\" mean the "
     "`productCatalogFailure` flag is on. 2. If failures span many product ids, check the "
     "catalog data file was loaded at startup.",
     "Set `productCatalogFailure` to `off` in flagd. For many-product failures, restart "
     "product-catalog to reload the catalog.",
     "Catalog team on-call."),
    ("rb-05-cart-valkey", "Cart: Valkey errors and EmptyCart failures", "cart",
     "Cart `AddItem`, `GetCart` or `EmptyCart` fail; the cart page shows an empty cart; the "
     "cart service logs Valkey connection errors.",
     "1. Check that the valkey-cart container is running and reachable from cart. 2. Read the "
     "`cartFailure` flag in flagd; when on, EmptyCart fails on purpose.",
     "Set `cartFailure` to `off`. Restart valkey-cart if it is down; carts are not persisted, "
     "so shoppers lose their carts.",
     "Cart team on-call."),
    ("rb-06-currency-conversion", "Currency: conversion errors", "storefront",
     "`Convert` or `GetSupportedCurrencies` fail; prices show as 0 or the frontend shows a "
     "currency error.",
     "Read the currency service log for unsupported currency codes and confirm the "
     "exchange-rate table loaded at startup.",
     "Restart the currency service. Roll back the last deploy if errors continue.",
     "Storefront team in business hours."),
    ("rb-07-shipping-quote", "Shipping: quote and ShipOrder failures", "fulfilment",
     "`GetQuote` or `ShipOrder` fail or slow down; checkout shows no shipping cost.",
     "Check the shipping service's calls to the quote service and their latency.",
     "Restart quote, then shipping. If quote is slow, scale it out by one replica.",
     "Fulfilment team on-call."),
    ("rb-08-email-confirmations", "Email: order confirmations not sent", "storefront",
     "`send_order_confirmation` calls fail or slow down; email memory grows.",
     "Read the `emailMemoryLeak` flag in flagd and the email container's memory trend.",
     "Set `emailMemoryLeak` to `off` and restart the email service. Confirmations are not "
     "retried, so list the affected order ids for the support team.",
     "Storefront team in business hours."),
    ("rb-09-frontend-proxy-503", "Frontend proxy: 503s and upstream connect errors", "platform",
     "frontend-proxy (Envoy) returns 503 with \"upstream connect error\" for every route; the "
     "load generator reports connection failures.",
     "Check that the frontend container is up and that Envoy's cluster health checks pass. "
     "Errors on only one API route point to the service behind that route, not to the proxy.",
     "Restart frontend, then frontend-proxy.",
     "Platform team on-call."),
    ("rb-10-flagd-unavailable", "Feature flags: flagd unavailable", "platform",
     "Services log \"flag evaluation failed\" or fall back to default flag values; the flagd UI "
     "does not load.",
     "Check that the flagd container is running and that `demo.flagd.json` is valid JSON.",
     "Restore the last good `demo.flagd.json` and restart flagd. Services pick up the flags "
     "again without a restart.",
     "Platform team on-call."),
]  # fmt: skip


def at(clock: str) -> datetime:
    return datetime.fromisoformat(f"2026-10-14T{clock}Z")


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def fingerprint(labels: dict[str, str]) -> str:
    """Prometheus's label-set fingerprint: FNV-1a 64 over sorted names and values."""
    value = 0xCBF29CE484222325
    for name in sorted(labels):
        for part in (name, labels[name]):
            for byte in part.encode("utf-8") + b"\xff":
                value = ((value ^ byte) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"{value:016x}"


def labels(alert: Alert) -> dict[str, str]:
    stack = STACKS[alert.stack]
    severity = SEVERITY[alert.severity] if stack.severity_label == "priority" else alert.severity
    return {
        "alertname": alert.alertname,
        stack.service_label: alert.service,
        stack.env_label: alert.env,
        stack.severity_label: severity,
    }


def payload_alert(alert: Alert, resolved: bool) -> dict[str, object]:
    stack = STACKS[alert.stack]
    annotations = {stack.summary_annotation: alert.summary}
    if alert.runbook:
        annotations["runbook_url"] = f"{WIKI}/{alert.runbook}"
    alert_labels = labels(alert)
    return {
        "status": "resolved" if resolved else "firing",
        "labels": alert_labels,
        "annotations": annotations,
        "startsAt": stamp(at(alert.starts)),
        "endsAt": stamp(at(alert.ends)) if resolved else NOT_ENDED,
        "generatorURL": f"http://prometheus-{stack.name}.example.internal/graph",
        "fingerprint": fingerprint(alert_labels),
    }


def webhooks() -> list[dict[str, object]]:
    """Every notification each stack's Alertmanager sent, in the order they were sent.

    Each stack groups by its service and environment labels. A group's first notification goes
    out GROUP_WAIT after its first alert; after that, one goes out every GROUP_INTERVAL while new
    alerts keep arriving or one resolves, and each carries every alert of the group.
    """
    groups: dict[tuple[str, str, str], list[Alert]] = {}
    for alert in ALERTS:
        groups.setdefault((alert.stack, alert.service, alert.env), []).append(alert)
    sent: list[tuple[datetime, dict[str, object]]] = []
    for (stack_name, service, env), members in groups.items():
        stack = STACKS[stack_name]
        changes = sorted({at(a.starts) for a in members} | {at(a.ends) for a in members if a.ends})
        moment, last = changes[0] + GROUP_WAIT, changes[0] - GROUP_WAIT
        while True:
            if any(last < change <= moment for change in changes):
                alerts = [
                    payload_alert(a, bool(a.ends) and at(a.ends) <= moment)
                    for a in members
                    if at(a.starts) <= moment
                ]
                group_labels = {stack.service_label: service, stack.env_label: env}
                firing = any(a["status"] == "firing" for a in alerts)
                body = {
                    "version": "4",
                    "groupKey": "{}:{"
                    + ", ".join(f'{k}="{v}"' for k, v in sorted(group_labels.items()))
                    + "}",
                    "truncatedAlerts": 0,
                    "status": "firing" if firing else "resolved",
                    "receiver": stack.receiver,
                    "groupLabels": group_labels,
                    "commonLabels": group_labels,
                    "commonAnnotations": {},
                    "externalURL": f"http://alertmanager-{stack.name}.example.internal",
                    "alerts": alerts,
                }
                sent.append((moment, body))
                last = moment
            if not any(change > moment for change in changes):
                break
            moment += GROUP_INTERVAL
    return [body for _, body in sorted(sent, key=lambda pair: (pair[0], str(pair[1]["groupKey"])))]


def write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def main() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    lines = [json.dumps(body, ensure_ascii=False) for body in webhooks()]
    (DATA_DIR / "alertmanager-webhooks.jsonl").write_text("\n".join(lines) + "\n", "utf-8")
    logs = sorted(LOGS, key=lambda row: (row[0], row[1]))
    write_csv(
        DATA_DIR / "logs.csv",
        ["time", "env", "service", "level", "message"],
        [[stamp(at(clock)), env, service, level, msg] for clock, env, service, level, msg in logs],
    )
    write_csv(
        DATA_DIR / "wiki_runbooks.csv",
        ["runbook_id", "url", "title", "owner_team", "symptoms", "check", "mitigate", "escalate"],
        [
            [f"RB-{slug[3:5]}", f"{WIKI}/{slug}", title, team, *sections]
            for slug, title, team, *sections in RUNBOOKS
        ],
    )
    print(f"wrote {len(lines)} webhooks, {len(logs)} log lines, {len(RUNBOOKS)} runbooks")


if __name__ == "__main__":
    main()
