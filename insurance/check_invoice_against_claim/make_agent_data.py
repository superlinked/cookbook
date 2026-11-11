"""Write the agent's claims-system seed and one repair shop's invoices in data/agent/.

Everything is invented, like data/: the insurer, the insureds, the shops, the addresses and
every number, and every invoice says so in its footer. data/agent/ is a second, full seed for
claims_system.py: nine claims that Brightwater Roofing & Gutters Ltd invoiced (or, for siu-09,
printed an invoice for), so the agent has a shop's year to look over. siu-01 and siu-02 are the
same pages as clm-01 and clm-09 in data/. Nothing in data/ changes.

    uv run make_agent_data.py

writes claims.csv, claim_vendors.csv, authorizations.csv, claim_documents.csv,
brightwater-invoices.pdf (image-only, no document metadata) and jobs.yaml. Rerunning rebuilds
every file byte for byte.
"""

from __future__ import annotations

import argparse
import dataclasses
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from make_data import CLAIMS, Claim, Invoice, LineItem, _save_pdf, _write_csv, draw_invoice

ROOT = Path(__file__).resolve().parent
AGENT_DIR = ROOT / "data" / "agent"
INVOICE_PDF = "brightwater-invoices.pdf"
D = Decimal

SHOP = "Brightwater Roofing & Gutters Ltd"
SHOP_ADDRESS = "220 Millrace Road, Ashby Vale, CO 80910"
FLAT_RATE = (LineItem("Seamless gutter replacement, 2-storey (flat rate)", 1, D("1450.00")),)
JOB = """\
# The SIU request the agent works on. agent.py reads the vendor and year from here for its
# completeness check, never from the agent.
vendor: Brightwater Roofing & Gutters Ltd
year: 2026
asked_by: SIU analyst
text: >-
  A desk adjuster thinks they saw the same Brightwater invoice twice last week. Pull every claim
  Brightwater Roofing & Gutters Ltd invoiced us in 2026, check each invoice against its claim,
  and tell me what looks like a pattern across them. Keep what a failed check backs up apart
  from what is only a question for the adjuster.
"""


@dataclass(frozen=True)
class Filed:
    """One invoice on one claim in the agent's seed."""

    claim: Claim
    status: str  # the claim's status in the claims system
    received: date


def _brightwater(
    document: str,
    claim_number: str,
    insured: str,
    loss: date,
    address: str,
    number: str,
    issued: date,
    items: tuple[LineItem, ...],
    authorized: Decimal | None,
    vendors: tuple[str, ...] = (SHOP,),
) -> Claim:
    """A homeowners claim and the Brightwater invoice for it, claim number printed."""
    policy = f"HO-20{claim_number[4:6]}-0{claim_number[-5:]}"
    return Claim(
        document,
        "",
        claim_number,
        policy,
        "Homeowners",
        insured,
        loss,
        address,
        "",
        "",
        vendors,
        (("estimate", authorized, loss.replace(day=min(loss.day + 3, 28))),),
        Invoice(SHOP, SHOP_ADDRESS, number, issued, insured, claim_number, address, items, D("6")),
    )


def _from_data(document: str, source: str) -> Claim:
    """A claim from data/, under the agent seed's document id, with the very same page."""
    claim = next(c for c in CLAIMS if c.document == source)
    return dataclasses.replace(claim, document=document)


CLM_09 = _from_data("siu-02", "clm-09")

FILED = [
    Filed(_from_data("siu-01", "clm-01"), "open", date(2026, 10, 20)),
    Filed(CLM_09, "open", date(2026, 10, 20)),
    # The same invoice as siu-02, this time with her claim number, on Margit Allanby's own claim.
    Filed(
        _brightwater(
            "siu-03",
            "CLM-26-133540",
            "Margit Allanby",
            date(2026, 9, 30),
            "7 Thornfield Close, Ashby Vale, CO 80912",
            "BRG-10457",
            date(2026, 10, 14),
            CLM_09.invoice.items,
            None,
        ),
        "open",
        date(2026, 10, 16),
    ),
    # Two neighbours of siu-01, same storm, one flat-rate line each: the same total twice.
    Filed(
        _brightwater(
            "siu-04",
            "CLM-26-140918",
            "Owen Calloway",
            date(2026, 10, 4),
            "12 Fernhollow Lane, Ashby Vale, CO 80911",
            "BRG-10448",
            date(2026, 10, 13),
            FLAT_RATE,
            None,
        ),
        "open",
        date(2026, 10, 21),
    ),
    Filed(
        _brightwater(
            "siu-05",
            "CLM-26-141205",
            "Ruth Abernathy",
            date(2026, 10, 4),
            "18 Fernhollow Lane, Ashby Vale, CO 80911",
            "BRG-10451",
            date(2026, 10, 15),
            FLAT_RATE,
            None,
        ),
        "open",
        date(2026, 10, 21),
    ),
    # Closed in July: the workflow won't check it.
    Filed(
        _brightwater(
            "siu-06",
            "CLM-26-152733",
            "Desmond Achterberg",
            date(2026, 6, 11),
            "5 Kingfisher Row, Brookmere, CO 80421",
            "BRG-10297",
            date(2026, 6, 26),
            (
                LineItem("Remove storm-damaged shingles", 1, D("520.00")),
                LineItem("Architectural shingles (bundle)", 20, D("42.50")),
                LineItem("Roofing labor (hours)", 11, D("85.00")),
            ),
            D("2500.00"),
        ),
        "closed",
        date(2026, 7, 2),
    ),
    # Over the authorized amount by 412.00, and nothing else.
    Filed(
        _brightwater(
            "siu-07",
            "CLM-26-127764",
            "Wen Ostrowski",
            date(2026, 9, 12),
            "260 Saddleback Road, Brookmere, CO 80422",
            "BRG-10391",
            date(2026, 9, 24),
            (
                LineItem("Tear-off and disposal", 1, D("979.00")),
                LineItem("Architectural shingles (bundle)", 38, D("42.50")),
                LineItem("Ridge vent (ft)", 34, D("18.90")),
                LineItem("Roofing labor (hours)", 12, D("85.00")),
            ),
            D("4100.00"),
        ),
        "open",
        date(2026, 9, 30),
    ),
    # Last year's invoice: outside the request.
    Filed(
        _brightwater(
            "siu-08",
            "CLM-25-398120",
            "Elspeth Varga",
            date(2025, 11, 20),
            "33 Larkfield Drive, Pell Harbor, CO 80633",
            "BRG-10102",
            date(2025, 12, 10),
            (
                LineItem("Gutter run, seamless aluminum (ft)", 70, D("14.50")),
                LineItem("Downspout and elbows", 3, D("85.00")),
                LineItem("Install labor (hours)", 5, D("85.00")),
            ),
            D("1900.00"),
        ),
        "open",
        date(2025, 12, 19),
    ),
    # A Brightwater page on a claim whose approved vendor is another shop: no lookup by vendor
    # finds it.
    Filed(
        _brightwater(
            "siu-09",
            "CLM-26-160044",
            "Corin Haldane",
            date(2026, 10, 4),
            "64 Fernhollow Lane, Ashby Vale, CO 80911",
            "BRG-10466",
            date(2026, 10, 16),
            (
                LineItem("Gutter run, seamless aluminum (ft)", 60, D("14.50")),
                LineItem("Downspout and elbows", 3, D("85.00")),
                LineItem("Install labor (hours)", 9, D("85.00")),
                LineItem("Fascia board repair", 1, D("250.00")),
            ),
            D("2300.00"),
            vendors=("Ashby Vale Exteriors",),
        ),
        "open",
        date(2026, 10, 18),
    ),
]


def write_seeds(folder: Path) -> None:
    _write_csv(
        folder / "claims.csv",
        [
            *("claim_number", "status", "policy_number", "line", "named_insured", "loss_date"),
            *("loss_address", "vehicle", "vin"),
        ],
        [
            [
                *(f.claim.claim_number, f.status, f.claim.policy_number, f.claim.line),
                *(f.claim.insured, f.claim.loss_date.isoformat(), f.claim.loss_address),
                *(f.claim.vehicle, f.claim.vin),
            ]
            for f in FILED
        ],
    )
    _write_csv(
        folder / "claim_vendors.csv",
        ["claim_number", "vendor"],
        [[f.claim.claim_number, vendor] for f in FILED for vendor in f.claim.vendors],
    )
    rows = []
    for f in FILED:
        for kind, amount, approved in f.claim.authorizations:
            # An estimate given as None is the invoice total: the shop billed exactly that.
            value = amount if amount is not None else f.claim.invoice.total
            rows.append([f.claim.claim_number, kind, f"{value:.2f}", approved.isoformat()])
    _write_csv(
        folder / "authorizations.csv", ["claim_number", "kind", "amount", "approved_on"], rows
    )
    _write_csv(
        folder / "claim_documents.csv",
        ["document", "claim_number", "kind", "received", "file", "page"],
        [
            [
                *(f.claim.document, f.claim.claim_number, "repair_invoice"),
                *(f.received.isoformat(), INVOICE_PDF, str(page)),
            ]
            for page, f in enumerate(FILED, start=1)
        ],
    )


def main() -> None:
    argparse.ArgumentParser(
        description="Write the agent's claims-system seed and Brightwater's invoices."
    ).parse_args()
    AGENT_DIR.mkdir(parents=True, exist_ok=True)
    write_seeds(AGENT_DIR)
    _save_pdf(AGENT_DIR / INVOICE_PDF, [draw_invoice(f.claim) for f in FILED])
    (AGENT_DIR / "jobs.yaml").write_text(JOB, encoding="utf-8")
    print(f"wrote {len(FILED)} claims and their invoices to {AGENT_DIR}")


if __name__ == "__main__":
    main()
