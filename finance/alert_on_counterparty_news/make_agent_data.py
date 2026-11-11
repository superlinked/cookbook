"""Write the agent's synthetic inputs in data/agent/.

    uv run make_agent_data.py

writes three files, all invented for this recipe:

- news.jsonl: two more articles for the news queue, in the same fields as data/news.jsonl.
  A-01 says a borrower is reviewing the future of a yard; nothing has closed. A-02 is a
  borrower saying it paid its suppliers on time, after a post on a trade forum said it hadn't.
- credit_files.csv: one row per borrower from the loan file: sector, business, main sites,
  largest customers, facilities, security and the date of the last annual review.
- credit_notes.csv: dated notes on the credit file (annual reviews, alerts, relationship
  manager calls).

The workflow (recipe.py) never reads these files, so its results on data/news.jsonl don't
change. Rerunning rebuilds every file byte for byte.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Sequence
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent / "data" / "agent"

NEWS = [
    {
        "id": "A-01",
        "source": "Coastal Shipping Gazette",
        "url": "https://coastal-shipping.example/2026/11/ostley-brackwater",
        "published": "2026-11-03T07:40:00Z",
        "headline": "Ostley Marine reviews future of Brackwater yard",
        "body": (
            "Ostley Marine Services is reviewing the future of its Brackwater repair yard after "
            "a fall in orders, the company told staff on Monday. No decision has been made, and "
            "the yard will keep working through the winter."
        ),
    },
    {
        "id": "A-02",
        "source": "Chilled Food Forum Digest",
        "url": "https://chilled-food-digest.example/2026/11/pellridge-invoices",
        "published": "2026-11-03T09:15:00Z",
        "headline": "Pellridge says October supplier invoices were paid on time",
        "body": (
            "Pellridge Foods said on Tuesday that all of its October supplier invoices were paid "
            "on time, after a post on a trade forum claimed that packaging suppliers had not "
            "been paid. The company said the post was wrong and that it had asked the forum to "
            "remove it."
        ),
    },
]

FILE_FIELDS = (
    "counterparty_id",
    "sector",
    "business",
    "main_sites",
    "largest_customers",
    "facilities",
    "security",
    "last_annual_review",
)

FILES = [
    (
        "CP-1001",
        "food manufacturing",
        "chilled ready meals for supermarkets",
        "two kitchens, at Pellridge and Marlow Cross",
        "two supermarket chains (about 70% of revenue)",
        "term loan 6.5m to 2028; revolving credit 2.0m",
        "debenture over both kitchens",
        "2026-03-14",
    ),
    (
        "CP-1002",
        "building materials distribution",
        "timber, bricks and insulation for builders' merchants and housebuilders",
        "three depots in the north of the region",
        "regional housebuilders; builders' merchants",
        "revolving credit 5.0m; overdraft 1.0m",
        "charge over stock and receivables",
        "2026-05-02",
    ),
    (
        "CP-1003",
        "cold storage",
        "frozen and chilled storage for food retailers and wholesalers",
        "four depots, the largest at Halden",
        "a regional supermarket group; two frozen food wholesalers",
        "term loan 8.0m to 2030",
        "mortgage over the Halden depot",
        "2026-02-20",
    ),
    (
        "CP-1004",
        "road freight",
        "parcel and pallet delivery for retailers",
        "hubs at Dunmere and Carrow",
        "national parcel carrier (about a third of revenue); two supermarket chains",
        "asset finance 3.2m; overdraft 0.5m",
        "charge over the vehicle fleet",
        "2026-06-11",
    ),
    (
        "CP-1005",
        "energy",
        "gas storage and wholesale supply to regional utilities",
        "Kessing gas storage site",
        "three regional utilities",
        "revolving credit 12.0m to 2028",
        "guarantee from the listed parent",
        "2026-04-08",
    ),
    (
        "CP-1006",
        "industrial engineering",
        "pumps and industrial controls for water companies",
        "one plant at Quillfield",
        "two water companies (about half of revenue)",
        "term loan 4.5m to 2029; revolving credit 1.5m",
        "debenture over the Quillfield plant",
        "2026-07-21",
    ),
    (
        "CP-1007",
        "packaging",
        "recycled food trays and cartons; part of a group whose parent is listed as LSE:TLPK",
        "two plants, at Tolland and Fenby",
        "supermarket chains and ready-meal makers",
        "revolving credit 3.0m",
        "charge over plant and machinery",
        "2026-01-29",
    ),
    (
        "CP-1008",
        "dairy",
        "cheese and butter for supermarkets and food service",
        "creamery at Ferncastle",
        "two supermarket chains; a food service wholesaler",
        "term loan 2.8m to 2028",
        "mortgage over the creamery",
        "2026-08-05",
    ),
    (
        "CP-1009",
        "ship repair",
        "repair and refit of fishing boats, ferries and workboats",
        "Brackwater repair yard (about 60% of revenue); Ostley Quay workshop",
        "a ferry operator; regional fishing fleets",
        "term loan 4.0m to 2029",
        "secured on the Brackwater yard",
        "2026-04-30",
    ),
]

NOTE_FIELDS = ("counterparty_id", "date", "kind", "note")

NOTES = [
    (
        "CP-1001",
        "2026-03-14",
        "annual_review",
        "Margins down on energy costs; covenants met with little headroom.",
    ),
    (
        "CP-1001",
        "2026-11-02",
        "alert",
        "Missed the October interest payment on the term loan (news alert N-01).",
    ),
    (
        "CP-1003",
        "2026-02-20",
        "annual_review",
        "Steady trading; the Halden depot was refitted in 2025.",
    ),
    (
        "CP-1004",
        "2026-06-11",
        "annual_review",
        "Reliance on one parcel carrier is the main risk; that contract is up for renewal in "
        "December 2026.",
    ),
    (
        "CP-1004",
        "2026-09-30",
        "rm_call",
        "Management said talks on the parcel carrier contract were going well.",
    ),
    (
        "CP-1005",
        "2026-04-08",
        "annual_review",
        "Storage margins weaker; the parent is refinancing its 2028 bonds.",
    ),
    (
        "CP-1006",
        "2026-07-21",
        "annual_review",
        "Order book full to mid-2027; looking at software acquisitions.",
    ),
    (
        "CP-1007",
        "2026-01-29",
        "annual_review",
        "New tray line at Fenby running; trading in line with plan.",
    ),
    (
        "CP-1008",
        "2026-08-05",
        "annual_review",
        "Milk prices squeezed margins; covenants met.",
    ),
    (
        "CP-1009",
        "2026-04-30",
        "annual_review",
        "Repair orders down on last year; the yard is the main asset behind the loan.",
    ),
    (
        "CP-1009",
        "2026-09-18",
        "rm_call",
        "Management said winter bookings at Brackwater were thinner than usual.",
    ),
    (
        "CP-1002",
        "2026-05-02",
        "annual_review",
        "Housebuilder orders held up; new warehouse planned for the north of the region.",
    ),
]


def write_csv(path: Path, fields: tuple[str, ...], rows: Sequence[tuple[str, ...]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(fields)
        writer.writerows(rows)


def main() -> None:
    AGENT_DIR.mkdir(parents=True, exist_ok=True)
    (AGENT_DIR / "news.jsonl").write_text(
        "".join(json.dumps(article) + "\n" for article in NEWS), encoding="utf-8"
    )
    write_csv(AGENT_DIR / "credit_files.csv", FILE_FIELDS, FILES)
    write_csv(AGENT_DIR / "credit_notes.csv", NOTE_FIELDS, NOTES)
    print(f"wrote {AGENT_DIR}")


if __name__ == "__main__":
    main()
