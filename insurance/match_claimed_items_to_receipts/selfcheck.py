"""Check the recipe's parsing, matching, statuses and request list offline.

The checks use hand-written strings, the committed data/ and hand-written stand-in model
replies. Nothing here calls SIE or replays a recorded response. Run it with
`uv run --frozen python selfcheck.py`; it exits non-zero if a check fails.
"""

from __future__ import annotations

import contextlib
import csv
import io
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from claims_system import ClaimsSystem
from sie_sdk import SIEClient
from sie_sdk.client.errors import (
    AccountInactiveError,
    InputTooLongError,
    InsufficientCreditsError,
    ModelLoadFailedError,
    ModelLoadingError,
    ProvisioningError,
    RateLimitError,
    RequestError,
    ServerError,
    SIEConnectionError,
    SIEError,
    SpendLimitError,
)

import recipe

RULES = recipe.load_rules()
GUIDE = recipe.load_guideline()
DATA = recipe.DATA_DIR


# --- Hand-written OCR pages, in the shape LightOnOCR returns ----------------------------------


def receipt(
    items: list[tuple[str, str]],
    *,
    merchant: str = "LARKSPUR ELECTRONICS",
    when: str = "08/22/2026 15:41",
    discounts: list[tuple[str, str]] | None = None,
    rate: str = "0.08",
    offset: str = "0.00",
) -> str:
    """A receipt page whose lines add up unless `offset` moves the printed total."""
    discounts = discounts or []
    subtotal = sum((Decimal(a) for _, a in items + discounts), Decimal(0))
    tax = (subtotal * Decimal(rate)).quantize(Decimal("0.01"))
    total = subtotal + tax + Decimal(offset)
    body = [f"| {text} | {amount} |" for text, amount in items + discounts]
    return "\n".join(
        [
            f"# {merchant}",
            "220 Mercer Ave, Port Ellery UT 40381",
            f"Date: {when}",
            "Store #001 Reg 2 Trans 4137",
            "| Item | Price |",
            "| --- | --- |",
            *body,
            f"| SUBTOTAL | {subtotal} |",
            f"| SALES TAX | {tax} |",
            f"| **TOTAL** | **{total}** |",
            f"VISA ****4417 {total}",
            "THANK YOU FOR SHOPPING WITH US",
        ]
    )


def inventory(items: list[tuple[str, str]], total: str = "", claim: str = "CLM-1") -> str:
    """A handwritten inventory page as OCR Markdown; the total defaults to the items' sum."""
    total = total or str(sum((recipe.amounts(a)[-1] for _, a in items), Decimal(0)))
    return "\n".join(
        [
            f"Claim {claim} Policy HO-1",
            "Contents inventory - A. Policyholder",
            "On October 14, a pipe burst in the basement.",
            "Items lost or damaged:",
            *(f"- {text} {amount}" for text, amount in items),
            f"Total claimed (with sales tax): {total}",
            "Receipts attached. I confirm this is true.",
        ]
    )


# --- A stand-in for SIEClient -----------------------------------------------------------------


def stand_in_fields(text: str, kind: str) -> dict[str, Any]:
    """What a well-behaved GLiNER2 would return for a window of flattened text."""
    lines = text.splitlines()
    not_item = RULES["labels"]["not_item"]
    priced = [
        line
        for line in lines
        if recipe.amounts(line)
        and not recipe.has_label(line, not_item)
        and not line.startswith(("Date", "Claim"))
    ]
    items = [recipe.describe_line(line) for line in priced]
    total = next((line for line in lines if recipe.has_label(line, ["total"])), "")
    total_amount = str(recipe.amounts(total)[-1]) if recipe.amounts(total) else ""
    if kind == "inventory":
        return {"total_claimed": total_amount, "items": items}
    date = re.search(r"Date: (\S+)", text)
    return {
        "merchant": lines[0] if lines else "",
        "purchase_date": date.group(1) if date else "",
        "total": total_amount,
        "items": items,
    }


class StandIn:
    """Stands in for SIEClient. OCR calls return the page Markdown keyed by the call's item id
    ("ocr/<pdf stem>/p<page>"); extract calls return `stand_in_fields` for the window, or what
    `fields` gives for the item id. `errors` maps an item id prefix to the errors its calls
    raise (one per call), `replies` to a raw reply that replaces the normal one."""

    def __init__(
        self,
        pages: dict[str, str],
        errors: dict[str, list[Exception]] | None = None,
        replies: dict[str, Any] | None = None,
        fields: Callable[[str, str], dict[str, Any]] | None = None,
    ) -> None:
        self.pages, self.errors, self.replies = pages, errors or {}, replies or {}
        self.fields = fields
        self.ids: list[str] = []
        self.extract_options: list[Any] = []  # the `options` of each output_schema call

    def extract(self, model: str, item: Any, **options: Any) -> Any:
        item_id, text = item["id"], item.get("text", "")
        self.ids.append(item_id)
        for prefix, errors in self.errors.items():
            if item_id.startswith(prefix) and errors:
                raise errors.pop(0)
        for prefix, reply in self.replies.items():
            if item_id.startswith(prefix):
                return reply
        if "output_schema" not in options:
            return {"entities": [{"label": "markdown", "text": self.pages[item_id]}]}
        self.extract_options.append(options.get("options"))
        kind = "inventory" if "/inventory" in item_id else "receipt"
        if self.fields is not None:
            return {"data": self.fields(item_id, text)}
        return {"data": stand_in_fields(text, kind)}


@dataclass
class Spec:
    """One claim for a stand-in run: the claims system's side and the OCR pages."""

    number: str = "CLM-1"
    loss: str = "2026-10-14"
    claimed: str = ""  # default: the inventory page's written total
    receipts: list[str] = field(default_factory=list)
    inventory: str | None = None  # the handwritten page; None for a portal inventory
    portal: list[tuple[str, int, str]] = field(default_factory=list)  # description, qty, unit


def run(*specs: Spec, **stand_in: Any) -> tuple[list[recipe.ClaimResult], StandIn, dict[str, str]]:
    """Run the recipe over a temporary claims system and the stand-in client. Returns the
    results, the client and the output files written (name -> text)."""
    with tempfile.TemporaryDirectory() as name:
        folder = Path(name)
        for pdf in ("receipt-photos.pdf", "inventories.pdf"):
            (folder / pdf).write_bytes((DATA / pdf).read_bytes())
        claims, attachments, portal = [], [], []
        pages: dict[str, str] = {}
        receipt_page = inventory_page = 0
        for spec in specs:
            claimed = spec.claimed
            if not claimed and spec.inventory is not None:
                claimed = str(recipe.amounts(spec.inventory.splitlines()[-2])[-1])
            claimed = claimed or "181.43"  # a portal claim: no written total to compare
            source = "handwritten" if spec.inventory is not None else "portal"
            claims.append([spec.number, "HO-1", "A. Policyholder", spec.loss, claimed, source])
            for index, page in enumerate(spec.receipts, start=1):
                receipt_page += 1
                pages[f"ocr/receipt-photos/p{receipt_page}"] = page
                attachments.append(
                    [spec.number, f"receipt-{index}", "receipt", "receipt-photos.pdf", receipt_page]
                )
            if spec.inventory is not None:
                inventory_page += 1
                pages[f"ocr/inventories/p{inventory_page}"] = spec.inventory
                attachments.append(
                    [spec.number, "inventory", "inventory", "inventories.pdf", inventory_page]
                )
            for line, (text, qty, unit) in enumerate(spec.portal, start=1):
                portal.append([spec.number, line, text, qty, unit])
        write(folder / "claims.csv", recipe_columns("claims"), claims)
        write(folder / "attachments.csv", recipe_columns("attachments"), attachments)
        write(folder / "portal_inventory.csv", recipe_columns("portal_inventory"), portal)
        client = StandIn(pages, **stand_in)
        out = folder / "out"
        with contextlib.redirect_stdout(io.StringIO()):
            results, _ = recipe.run(cast(SIEClient, client), RULES, GUIDE, folder)
            recipe.write_outputs(out, results, GUIDE)
        files = {
            str(path.relative_to(out)): path.read_text(encoding="utf-8")
            for path in sorted(out.rglob("*"))
            if path.is_file()
        }
    return results, client, files


def recipe_columns(table: str) -> list[str]:
    import claims_system

    return list(claims_system.TABLES[table][1])


def write(path: Path, header: list[str], rows: list[list[Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def statuses(result: recipe.ClaimResult) -> list[tuple[str, str]]:
    """(description, status) per reconciliation row; a not-claimed row shows the receipt's."""
    return [
        (
            row.item.description
            if row.item
            else cast(recipe.ReceiptLine, row.receipt_line).description,
            row.status,
        )
        for row in result.rows
    ]


TV = [("NIMBUS 55IN 4K TV", "549.00"), ("TILT WALL MOUNT KIT", "39.99"), ("HDMI CABLE 2M", "12.99")]


# --- Checks -----------------------------------------------------------------------------------


def check_parsing() -> None:
    assert recipe.amounts("TOTAL $1,249.97") == [Decimal("1249.97")]
    assert recipe.amounts("INSTANT COUPON | -30.00") == [Decimal("-30.00")]
    # Glued and signed numbers: "$279.00", "279.00USD" and a 27.5 bike size (not money).
    assert recipe.amounts("Headphones $279.00") == [Decimal("279.00")]
    assert recipe.amounts("Headphones 279.00USD") == [Decimal("279.00")]
    assert recipe.amounts("Trail bike 27.5") == []
    assert recipe.line_price("ORBIT 14IN LAPTOP899.00") == (1, Decimal(899), Decimal(899))
    assert recipe.describe_line("ORBIT 14IN LAPTOP899.00") == "ORBIT 14IN LAPTOP"
    # Quantity notation: printed each price, a line price, both, and a mismatch.
    two = (2, Decimal("14.99"), Decimal("29.98"))
    assert recipe.line_price("MIXING BOWL SET 2 @ 14.99 | 29.98") == two
    assert recipe.line_price("MIXING BOWL SET 2@14.99") == two
    assert recipe.line_price("Mixing bowls (2) 29.98") == two
    assert recipe.line_price("Mixing bowls (qty 2) $29.98") == two
    bad = recipe.line_price("MIXING BOWL SET 2 @ 14.99 | 30.98")
    assert isinstance(bad, str), bad
    assert "prices the line at 30.98" in bad, bad
    assert recipe.line_price("NIMBUS 55IN 4K TV | 549.00") == (1, Decimal(549), Decimal(549))
    assert recipe.describe_line("MIXING BOWL SET 2 @ 14.99 | 29.98") == "MIXING BOWL SET"
    assert recipe.describe_line("Mixing bowls (2) $29.98") == "Mixing bowls"
    # Words: digits split from letters, spellings made the same, plurals singular.
    assert recipe.item_words("Nimbus 55in TV", RULES) == {"nimbus", "55", "inch", "tv"}
    assert recipe.item_words("STAND MIXER 5QT RED", RULES) == {
        "stand",
        "mixer",
        "5",
        "quart",
        "red",
    }
    assert recipe.item_words("Mixing bowls", RULES) == {"mixing", "bowl"}
    print("parsing: money, quantity notation, descriptions and words ok")


def check_word_matching() -> None:
    def agree(claimed: str, printed: str) -> bool:
        return recipe.words_agree(claimed, printed, RULES)[0]

    # Every pair from the sample claims that should agree does.
    for claimed, printed in [
        ("Nimbus 55in TV", "NIMBUS 55IN 4K TV"),
        ("TV wall mount", "TILT WALL MOUNT KIT"),
        ("Area rug 5x8", "AREA RUG 5X8 SLATE"),
        ("Brass floor lamp", "FLOOR LAMP BRASS"),
        ("Red stand mixer 5 qt", "STAND MIXER 5QT RED"),
        ("Mixing bowls", "MIXING BOWL SET"),
        ("8 inch chef knife", "CHEF KNIFE 8IN"),
        ("Dehumidifier 35 pint", "DEHUMIDIFIER 35PT"),
        ("U-bar bike lock", "BIKE LOCK U-BAR"),
        ("Walnut bar stool", "BAR STOOL WALNUT"),
        ("Doormat", "DOORMAT"),
    ]:
        assert agree(claimed, printed), (claimed, printed)
    # Near-identical but different: one shared word, another size, another model number.
    for claimed, printed in [
        ("TV stand", "TV WALL MOUNT"),
        ("Orbit 14in laptop", "ORBIT 15IN LAPTOP"),
        ("Trail bike 27.5", "TRAIL BIKE 29 HARDTAIL"),
        ("Laptop sleeve", "ORBIT 14IN LAPTOP"),
        ("Guitar stand", "ACOUSTIC GUITAR DREADNOUGHT"),
        ("Laptop", "LAPTOP SLEEVE 14IN"),
        ("TV", "NIMBUS 55IN 4K TV"),
        # Two of four words shared is below min_word_share 0.6: the lid is not the mixer.
        ("Stand mixer bowl lid", "STAND MIXER"),
    ]:
        assert not agree(claimed, printed), (claimed, printed)
    print("word matching: sample pairs agree; one shared word or another size does not ok")


def check_example_claims() -> None:
    # CLM-26-1001's shape: a mixed receipt, the cable not claimed, the TV written shorter.
    page = inventory([("Nimbus 55in TV", "$549.00"), ("TV wall mount", "$39.99")], "$636.11")
    (result,), _, files = run(Spec(receipts=[receipt(TV)], inventory=page))
    assert result.status == "checks_passed", result.reasons
    assert statuses(result) == [
        ("Nimbus 55in TV", "matched"),
        ("TV wall mount", "matched"),
        ("HDMI CABLE 2M", "not_claimed"),
    ], statuses(result)
    assert "requests/CLM-1.md" not in files
    # CLM-26-1002's shape: quantity line, coupon, 7.25% tax, total written without "$".
    mixer = [
        ("STAND MIXER 5QT RED", "329.99"),
        ("MIXING BOWL SET 2 @ 14.99", "29.98"),
        ("CHEF KNIFE 8IN", "54.00"),
    ]
    page = inventory(
        [
            ("Red stand mixer 5 qt", "329.99"),
            ("Mixing bowls (2)", "29.98"),
            ("8 inch chef knife", "54.00"),
        ],
        "443.98",
    )
    shop = receipt(
        mixer, discounts=[("INSTANT COUPON", "-30.00")], rate="0.0725", when="09/30/2026"
    )
    (result,), _, _ = run(Spec(loss="2026-10-02", receipts=[shop], inventory=page))
    assert result.status == "checks_passed", result.reasons
    assert [i.quantity for i in result.items] == [1, 2, 1]
    # CLM-26-1003's shape: the printed total is 30.00 more; both items still matched.
    bike = [("TRAIL BIKE 27.5 HARDTAIL", "389.00"), ("BIKE LOCK U-BAR", "23.97")]
    page = inventory([("Trail bike 27.5", "389.00"), ("U-bar bike lock", "23.97")], "446.01")
    (result,), _, files = run(Spec(receipts=[receipt(bike, offset="30.00")], inventory=page))
    assert result.status == "review"
    assert [s for _, s in statuses(result)] == ["matched", "matched"]
    assert "receipt 1 doesn't add up" in result.reasons[0], result.reasons
    assert "Nothing to ask the policyholder for" in files["requests/CLM-1.md"]
    # CLM-26-1004's shape: the guitar at 479.00 on the inventory, 279.00 on the receipt.
    guitar = [("ACOUSTIC GUITAR DREADNOUGHT", "279.00"), ("GUITAR STAND", "19.99")]
    page = inventory([("Acoustic guitar", "479.00"), ("Guitar stand", "19.99")], "538.91")
    (result,), _, files = run(Spec(receipts=[receipt(guitar)], inventory=page))
    assert statuses(result) == [("Acoustic guitar", "price_differs"), ("Guitar stand", "matched")]
    assert result.requests == [
        "For the Acoustic guitar: your list says 479.00, and the receipt from LARKSPUR "
        "ELECTRONICS dated 2026-08-22 shows 279.00. Please confirm what you paid, or tell us "
        "what the difference is for."
    ], result.requests
    # CLM-26-1006's shape: one photo unreadable; the item it would cover is not_read, and the
    # request asks for a new photo of it, naming the item.
    shelf = [("BOOKCASE OAK 5-SHELF", "159.00")]
    page = inventory([("Oak bookcase", "159.00"), ("Dehumidifier 35 pint", "219.99")], "409.31")
    (result,), _, _ = run(Spec(receipts=[receipt(shelf), "blurred"], inventory=page))
    assert statuses(result) == [("Oak bookcase", "matched"), ("Dehumidifier 35 pint", "not_read")]
    assert len(result.requests) == 1, result.requests
    assert result.requests[0].startswith("We couldn't read the second receipt photo."), (
        result.requests
    )
    assert "Dehumidifier 35 pint (219.99)" in result.requests[0]
    # CLM-26-1007's shape: the only receipt is dated after the loss.
    boots = [("HIKING BOOTS WOMENS 8", "164.00"), ("RAIN SHELL JACKET", "129.00")]
    page = inventory([("Hiking boots", "164.00"), ("Rain jacket", "129.00")], "316.44")
    late = receipt(boots, when="10/20/2026 12:44")
    (result,), _, _ = run(Spec(receipts=[late], inventory=page))
    assert [s for _, s in statuses(result)] == ["after_loss", "after_loss"]
    assert result.requests == []
    # CLM-26-1008's shape (portal): "TV stand" against "TV WALL MOUNT" at the same price.
    stools = [("BAR STOOL WALNUT 2 @ 64.00", "128.00"), ("TV WALL MOUNT", "39.99")]
    portal = [("Walnut bar stool", 2, "64.00"), ("TV stand", 1, "39.99")]
    (result,), client, _ = run(Spec(receipts=[receipt(stools)], portal=portal))
    assert statuses(result) == [("Walnut bar stool", "matched"), ("TV stand", "ambiguous")]
    assert result.requests == [], result.requests
    assert not any(i.startswith("ocr/inventories") for i in client.ids), "portal rows need no OCR"
    print("example claims: matched, price_differs, not_read, after_loss, ambiguous ok")


def check_proof_threshold() -> None:
    # CLM-26-1005's shape: headphones (279.00) asked for, the doormat (24.00) listed only.
    laptop = [("ORBIT 14IN LAPTOP", "899.00"), ("LAPTOP SLEEVE 14IN", "24.99")]
    items = [
        ("Orbit 14in laptop", "$899.00"),
        ("Laptop sleeve", "$24.99"),
        ("Aurora wireless headphones", "$279.00"),
        ("Doormat", "$24.00"),
    ]
    (result,), _, files = run(
        Spec(receipts=[receipt(laptop)], inventory=inventory(items, "$1,325.15"))
    )
    assert [s for _, s in statuses(result)] == ["matched", "matched", "no_receipt", "no_receipt"]
    proof = [row.proof_requested for row in result.rows]
    assert proof == ["", "", "yes", "no: at or below 100.00"], proof
    assert len(result.requests) == 1
    assert "Aurora wireless headphones (279.00)" in result.requests[0]
    assert "a bank or card statement showing the purchase" in result.requests[0]
    page = files["requests/CLM-1.md"]
    assert "Not asked for (no receipt, at or below 100.00): Doormat (24.00)" in page, page
    assert not any("Doormat" in reason for reason in result.reasons), result.reasons
    table = files["reconciliation.csv"]
    assert "Doormat,1,24.00,,,,,,,,no_receipt,no: at or below 100.00" in table, table
    # The boundary: exactly 100.00 is not asked for, 100.01 is.
    for price, asked in (("100.00", False), ("100.01", True)):
        page_ = inventory([("Orbit 14in laptop", "899.00"), ("Lamp", price)])
        (result,), _, _ = run(Spec(receipts=[receipt(laptop[:1])], inventory=page_))
        assert (len(result.requests) == 1) == asked, (price, result.requests)
    # Two of an item at 60.00 each is 120.00 claimed: above the threshold.
    page_ = inventory([("Orbit 14in laptop", "899.00"), ("Lamps (2)", "120.00")])
    (result,), _, _ = run(Spec(receipts=[receipt(laptop[:1])], inventory=page_))
    assert result.rows[1].proof_requested == "yes"
    # A claim whose only gap is a below-threshold item passes.
    page_ = inventory([("Orbit 14in laptop", "899.00"), ("Doormat (no receipt)", "24.00")])
    (result,), _, files = run(Spec(receipts=[receipt(laptop[:1])], inventory=page_))
    assert result.status == "checks_passed", result.reasons
    assert "requests/CLM-1.md" not in files
    print("proof threshold: above asked, at or below listed only, boundary at 100.00 ok")


def check_review_paths() -> None:
    laptop = [("ORBIT 14IN LAPTOP", "899.00"), ("LAPTOP SLEEVE 14IN", "24.99")]
    items = [("Orbit 14in laptop", "899.00"), ("Laptop sleeve", "24.99")]
    # The written total differs from the claims system's amount.
    page = inventory(items, "997.91")
    (result,), _, _ = run(Spec(claimed="1197.91", receipts=[receipt(laptop)], inventory=page))
    assert result.reasons == [
        "inventory total 997.91 doesn't equal the 1197.91 in the claims system"
    ], result.reasons

    # A priced inventory line the model didn't point at goes to review (a dropped item).
    def miss_sleeve(item_id: str, text: str) -> dict[str, Any]:
        found = stand_in_fields(text, "inventory" if "/inventory" in item_id else "receipt")
        if "/inventory" in item_id:
            found["items"] = [i for i in found["items"] if "sleeve" not in i]
        return found

    (result,), _, _ = run(
        Spec(receipts=[receipt(laptop)], inventory=inventory(items, "997.91")), fields=miss_sleeve
    )
    assert result.status == "review"
    assert any("Laptop sleeve 24.99' has a price but no item was read" in r for r in result.reasons)

    # One model span holding two items on two lines: both are read, neither dropped.
    def glued(item_id: str, text: str) -> dict[str, Any]:
        found = stand_in_fields(text, "inventory" if "/inventory" in item_id else "receipt")
        if "/receipt" in item_id:
            found["items"] = ["ORBIT 14IN LAPTOP | 899.00\nLAPTOP SLEEVE 14IN"]
        return found

    (result,), _, _ = run(
        Spec(receipts=[receipt(laptop)], inventory=inventory(items, "997.91")), fields=glued
    )
    assert result.status == "checks_passed", result.reasons

    # Two different totals read on one receipt go to review, not first wins.
    def two_totals(item_id: str, text: str) -> dict[str, Any]:
        found = stand_in_fields(text, "inventory" if "/inventory" in item_id else "receipt")
        if "/receipt" in item_id:
            found["total"] = ["997.91", "24.99"]
        return found

    shop = receipt(laptop) + "\nTOTAL SAVINGS 24.99"
    (result,), _, _ = run(
        Spec(receipts=[shop], inventory=inventory(items, "997.91")), fields=two_totals
    )
    assert any("total read as 997.91 / 24.99" in r for r in result.reasons), result.reasons
    # No purchase date read: the item is on the receipt, but it can't be dated.
    undated = receipt(laptop).replace("Date: 08/22/2026 15:41", "Thank you")
    (result,), _, _ = run(Spec(receipts=[undated], inventory=inventory(items, "997.91")))
    assert [s for _, s in statuses(result)] == ["not_read", "not_read"]
    assert "receipt 1: purchase date not read" in result.reasons
    assert result.requests == [], "a date the model missed is the handler's, not a new photo"
    # The same item bought before and after the loss: the earlier receipt proves it.
    later = receipt(laptop[:1], when="10/20/2026")
    (result,), _, _ = run(
        Spec(receipts=[later, receipt(laptop)], inventory=inventory(items, "997.91"))
    )
    assert [s for _, s in statuses(result)][:2] == ["matched", "matched"], statuses(result)
    assert "receipt 2 line 1" in result.rows[0].detail
    # The item listed twice against one receipt line: the second can't use that line again.
    # On a handwritten list the second line also has no item of its own, so it's flagged.
    twice = inventory([*items, ("Orbit 14in laptop", "899.00")])
    (result,), _, _ = run(Spec(receipts=[receipt(laptop)], inventory=twice))
    assert result.status == "review"
    assert any("has a price but no item was read" in r for r in result.reasons), result.reasons
    rows = [("Orbit 14in laptop", 1, "899.00"), ("Orbit 14in laptop", 1, "899.00")]
    (result,), _, _ = run(Spec(receipts=[receipt(laptop)], portal=rows))
    assert [s for _, s in statuses(result)] == ["matched", "ambiguous", "not_claimed"]
    assert "has 0 of 1 left for this item" in result.rows[1].detail, result.rows[1].detail
    # Three bowls claimed, two on the receipt: not a match.
    bowls = [("MIXING BOWL 2 @ 14.99", "29.98")]
    page = inventory([("Mixing bowls (3)", "44.97")])
    (result,), _, _ = run(Spec(receipts=[receipt(bowls)], inventory=page))
    assert statuses(result) == [("Mixing bowls", "ambiguous")], statuses(result)
    # Two different receipt lines fit one item equally: ambiguous, not the first one.
    sleeves = [("LAPTOP SLEEVE 13IN", "24.99"), ("LAPTOP SLEEVE 14IN", "24.99")]
    page = inventory([("Laptop sleeve", "24.99")])
    (result,), _, _ = run(Spec(receipts=[receipt(sleeves)], inventory=page))
    assert next(s for _, s in statuses(result)) == "ambiguous"
    assert "two different receipt lines fit" in result.rows[0].detail
    # A shorter name that is part of another line, at that line's price: not a match.
    page = inventory([("Laptop", "24.99")])
    (result,), _, _ = run(Spec(receipts=[receipt(laptop)], inventory=page))
    assert statuses(result)[0] == ("Laptop", "ambiguous"), statuses(result)
    # A portal row with quantity 0 is a reason for review, not a division by zero.
    (result,), _, _ = run(Spec(receipts=[receipt(laptop)], portal=[("Laptop sleeve", 0, "24.99")]))
    assert result.reasons[0] == "inventory: portal row 1 has quantity 0", result.reasons
    # Near-identical model at the same price: ambiguous, never matched.
    other = [("ORBIT 15IN LAPTOP", "899.00")]
    page = inventory([("Orbit 14in laptop", "899.00")])
    (result,), _, _ = run(Spec(receipts=[receipt(other)], inventory=page))
    assert statuses(result)[0] == ("Orbit 14in laptop", "ambiguous"), statuses(result)
    # A receipt line whose quantity and price disagree is not an item price at all.
    bad = [("MIXING BOWL 2 @ 14.99", "30.98")]
    page = inventory([("Mixing bowls (2)", "29.98")])
    (result,), _, _ = run(Spec(receipts=[receipt(bad)], inventory=page))
    assert any("prices the line at 30.98" in r for r in result.reasons), result.reasons
    assert statuses(result)[0][1] != "matched"
    # A price that differs, with another receipt unread: the right line may be on that one.
    guitar = [("ACOUSTIC GUITAR DREADNOUGHT", "279.00")]
    page = inventory([("Acoustic guitar", "479.00")])
    (result,), _, _ = run(Spec(receipts=[receipt(guitar), "blurred"], inventory=page))
    assert statuses(result)[0][1] == "not_read", statuses(result)
    # An unreadable inventory photo: no rows, and a request for a new photo of the list.
    (result,), _, _ = run(Spec(claimed="1.00", receipts=[receipt(guitar)], inventory="too dark"))
    assert result.rows == []
    assert result.status == "review"
    assert result.requests[0].startswith("We couldn't read the list of items you sent.")
    print("review paths: totals, dropped and glued items, conflicts, dates, quantities ok")


def check_statuses() -> None:
    known = {"matched", "price_differs", "ambiguous", "after_loss", "no_receipt", "not_read"}
    assert {
        recipe.MATCHED,
        recipe.PRICE_DIFFERS,
        recipe.AMBIGUOUS,
        recipe.AFTER_LOSS,
        recipe.NO_RECEIPT,
        recipe.NOT_READ,
    } == known
    assert (recipe.CHECKS_PASSED, recipe.REVIEW) == ("checks_passed", "review")
    source = Path(recipe.__file__).read_text(encoding="utf-8")
    for old in ('"verified"', '"adjuster_review"'):
        assert old not in source, f"old status {old} is still in recipe.py"
    laptop = [("ORBIT 14IN LAPTOP", "899.00")]
    (result,), client, files = run(
        Spec(
            receipts=[receipt(laptop)],
            inventory=inventory([("Orbit 14in laptop", "899.00")]),
        )
    )
    # One OCR call per photo and one extract call per window (one window each here).
    assert client.ids == [
        "ocr/receipt-photos/p1",
        "extract/CLM-1/receipt-1-w1",
        "ocr/inventories/p1",
        "extract/CLM-1/inventory-w1",
    ], client.ids
    assert result.status == "checks_passed", result.reasons
    assert set(files) == {"claims.csv", "reconciliation.csv", "claims/CLM-1.json"}, set(files)
    assert "verified" not in files["claims.csv"]
    assert "adjuster" not in files["claims.csv"]
    print("statuses: names, call counts and output files ok")


def check_sie_failure() -> None:
    for error in TRANSIENT:
        assert not recipe.is_fatal(error), f"{type(error).__name__} should be transient"
    for error in FATAL:
        assert recipe.is_fatal(error), f"{type(error).__name__} should stop the run"
    laptop = [("ORBIT 14IN LAPTOP", "899.00")]
    page = inventory([("Orbit 14in laptop", "899.00")])
    two = (
        Spec("CLM-1", receipts=[receipt(laptop)], inventory=page),
        Spec("CLM-2", receipts=[receipt(laptop)], inventory=page),
    )
    # A transient error on one claim's receipt: that claim goes to review with the reason, the
    # item is not_read, and the policyholder is not asked for a new photo; the other passes.
    for error in TRANSIENT:
        results, _, _ = run(*two, errors={"ocr/receipt-photos/p1": [error]})
        assert [r.status for r in results] == ["review", "checks_passed"], type(error).__name__
        assert "not read: the SIE call failed: " in results[0].reasons[0], results[0].reasons
        assert statuses(results[0]) == [("Orbit 14in laptop", "not_read")]
        assert results[0].requests == [], results[0].requests
    # A transient error on the inventory's extraction: review, no rows, nothing asked.
    results, _, _ = run(
        *two, errors={"extract/CLM-1/inventory": [ServerError("busy", status_code=503)]}
    )
    assert results[0].rows == []
    assert results[0].requests == []
    assert "fields not read: the SIE call failed: ServerError" in results[0].reasons[0]
    # A reply missing its key is a failed call on the same path; an empty list is not.
    results, _, _ = run(*two, replies={"ocr/receipt-photos/p1": {"something": []}})
    assert "replied without 'entities'" in results[0].reasons[0], results[0].reasons
    results, _, _ = run(*two, replies={"extract/CLM-1/receipt": {"no_data": {}}})
    assert "replied without 'data'" in results[0].reasons[0], results[0].reasons
    results, _, _ = run(*two, replies={"extract/CLM-1/receipt": {"data": {}}})
    assert not any("SIE call failed" in r for r in results[0].reasons), results[0].reasons
    assert "receipt 1: purchase date not read" in results[0].reasons
    # SIE Cloud's empty-result 500 reads as "no fields", not a failed call.
    empty = ServerError(recipe.EMPTY_RESULT_ERROR, status_code=500)
    results, _, _ = run(*two, errors={"extract/CLM-1/receipt": [empty]})
    assert not any("SIE call failed" in r for r in results[0].reasons), results[0].reasons
    assert any("no fields found" in note for note in results[0].notes)
    # The empty-result 500 is only "nothing found" from the extraction model: from the OCR
    # model it's a failed call.
    results, _, _ = run(*two, errors={"ocr/receipt-photos/p1": [empty]})
    assert "the SIE call failed: ServerError" in results[0].reasons[0], results[0].reasons
    # An OCR reply with entities but no markdown entity is a failed call, counted as one.
    results, _, _ = run(*two, replies={"ocr/receipt-photos/p1": {"entities": []}})
    assert "replied without a markdown entity" in results[0].reasons[0], results[0].reasons
    assert results[0].requests == []
    message = stopped(lambda: run(*two, replies={"ocr/": {"entities": []}}))
    assert message.startswith("Every SIE call failed (4 of 4"), message
    # Every GLiNER2 call asks SIE to fail on a window too long to read whole, not cut it short.
    _, client, _ = run(*two)
    assert client.extract_options, "no extract call made"
    assert all(o == {"overflow_policy": "error"} for o in client.extract_options), (
        client.extract_options
    )
    # Every call failed (a dead SIE_BASE_URL): the run stops, with the full message.
    long = "connection refused " + "x" * 300
    dead = {"": [SIEConnectionError(long) for _ in range(20)]}
    message = stopped(lambda: run(*two, errors=dead))
    assert message.startswith("Every SIE call failed (4 of 4 to lightonai/LightOnOCR-3-4B)"), (
        message
    )
    assert long in message
    assert "Check SIE_BASE_URL and your network." in message, message
    busy = {"": [ModelLoadingError("loading") for _ in range(20)]}
    message = stopped(lambda: run(*two, errors=busy))
    assert "may still be starting or SIE is busy; run again in a few minutes." in message
    # A refusal stops the run at once, with the full message, HTTP status and code.
    refusal = {"ocr/receipt-photos/p1": [InsufficientCreditsError("no credits " + "y" * 300)]}
    message = stopped(lambda: run(*two, errors=refusal))
    assert "InsufficientCreditsError (HTTP 402, code INSUFFICIENT_CREDITS)" in message, message
    assert "y" * 300 in message
    assert "SIE_API_KEY" in message
    message = stopped(
        lambda: run(*two, errors={"": [ModelLoadFailedError("gated", permanent=True)]})
    )
    assert message.startswith("SIE can't load lightonai/LightOnOCR-3-4B"), message
    message = stopped(lambda: run(*two, errors={"": [RequestError("unexpected body")]}))
    assert "SIE_API_KEY" not in message, message
    assert "SIE_BASE_URL" in message, message
    # A bug in our own code is raised as is; main turns only SIEStopped into a clean exit.
    try:
        run(*two, errors={"": [TypeError("bad argument")]})
    except TypeError:
        pass
    else:
        raise AssertionError("TypeError was swallowed")

    def bug() -> None:
        raise RuntimeError("a bug in recipe.py")

    def refused() -> None:
        raise recipe.SIEStopped("SIE refused")

    caught = raised(lambda: recipe.stop_on_refusal(bug))
    assert type(caught) is RuntimeError
    assert str(caught) == "a bug in recipe.py"
    caught = raised(lambda: recipe.stop_on_refusal(refused))
    assert type(caught) is SystemExit
    assert str(caught) == "Stopped: SIE refused"
    print("sie failure: transient to review; missing key fails; refusals and all-failed stop ok")


TRANSIENT: list[SIEError] = [
    SIEConnectionError("connection refused"),
    ServerError("unavailable", status_code=503),
    RateLimitError("slow down"),
    InputTooLongError("too long"),
    ProvisioningError("no GPU yet"),
    ModelLoadingError("loading"),
    ModelLoadFailedError("try later", permanent=False),
    SIEError("an SIE error with no subclass"),
]
FATAL: list[SIEError] = [
    RequestError("bad key", status_code=401),
    RequestError("no such model", status_code=404),
    InsufficientCreditsError("no credits"),
    SpendLimitError("spend limit"),
    AccountInactiveError("inactive"),
    ModelLoadFailedError("gated", permanent=True),
]


def stopped(call: Callable[[], object]) -> str:
    """The SIEStopped message the call raises; fails when it raises nothing."""
    try:
        call()
    except recipe.SIEStopped as error:
        return str(error)
    raise AssertionError("the run did not stop")


def raised(call: Callable[[], object]) -> BaseException:
    """Whatever the call raises, SystemExit included; fails when it raises nothing."""
    try:
        call()
    except BaseException as error:  # SystemExit too: the check is on which one it was
        return error
    raise AssertionError("nothing was raised")


def check_labels_and_totals() -> None:
    not_item = RULES["labels"]["not_item"]
    # Labels match whole words: "cash" is a payment line, "Cashmere sweater" an item.
    assert recipe.has_label("CASH TENDERED 50.00", not_item)
    assert recipe.has_label("TRANSACTION 4137", not_item)
    assert recipe.has_label("INSTANT COUPONS -5.00", RULES["labels"]["discount"])
    assert not recipe.has_label("Cashmere sweater 120.00", not_item)
    assert not recipe.has_label("Totally quiet fan 45.00", not_item)
    sweater = [("CASHMERE SWEATER", "120.00")]
    page = inventory([("Cashmere sweater", "120.00")], "129.60")
    (result,), _, _ = run(Spec(receipts=[receipt(sweater)], inventory=page))
    assert statuses(result) == [("Cashmere sweater", "matched")], statuses(result)
    assert result.status == "checks_passed", result.reasons
    # The items must add up to the written total, with sales tax of at most 10%.
    items = [("Orbit 14in laptop", "899.00"), ("Laptop sleeve", "24.99")]
    laptop = [("ORBIT 14IN LAPTOP", "899.00"), ("LAPTOP SLEEVE 14IN", "24.99")]
    for total in ("1197.91", "900.00"):
        page = inventory(items, total)
        (result,), _, _ = run(Spec(receipts=[receipt(laptop)], inventory=page))
        assert result.status == "review", total
        assert result.reasons == [
            f"inventory items add up to 923.99; the written total {total} is not that plus "
            "sales tax of at most 10%"
        ], result.reasons
    for total in ("923.99", "1016.38"):  # no tax, and 923.99 plus 10%
        page = inventory(items, total)
        (result,), _, _ = run(Spec(receipts=[receipt(laptop)], inventory=page))
        assert result.status == "checks_passed", (total, result.reasons)
    print("labels and totals: whole-word labels; items checked against the written total ok")


def check_csv_safe() -> None:
    assert recipe.csv_safe("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert recipe.csv_safe("-30.00") == "'-30.00"
    assert recipe.csv_safe("CLM-26-1001") == "CLM-26-1001"
    page = inventory([("=CMD lamp", "39.99")])
    (_,), _, files = run(Spec(receipts=[receipt(TV)], inventory=page))
    row = files["reconciliation.csv"].splitlines()[1]
    assert ",'=CMD lamp," in row, row
    print("csv_safe: formula-looking cells are quoted ok")


def check_committed_data() -> None:
    system = ClaimsSystem()
    numbers = system.open_contents_claims()
    assert numbers == [f"CLM-26-100{n}" for n in range(1, 9)], numbers
    assert system.get_claim("CLM-26-1099") is None
    assert system.get_claim("CLM-26-100") is None, "no guessing at a neighbouring claim"
    claim = system.get_claim("CLM-26-1005")
    assert claim is not None
    assert claim.contents_claimed == Decimal("1325.15")
    assert [a.kind for a in system.attachments("CLM-26-1006")] == [
        "receipt",
        "receipt",
        "inventory",
    ]
    rows = system.inventory_rows("CLM-26-1008")
    assert [(r.description, r.quantity, r.unit_price) for r in rows] == [
        ("Walnut bar stool", 2, Decimal("64.00")),
        ("TV stand", 1, Decimal("39.99")),
    ]
    assert system.inventory_rows("CLM-26-1001") == []
    # An attachment's file must be a file name in data/, never a path out of it.
    with tempfile.TemporaryDirectory() as name:
        folder = Path(name)
        for table in ("claims.csv", "portal_inventory.csv"):
            (folder / table).write_bytes((DATA / table).read_bytes())
        header = recipe_columns("attachments")
        outside = str(DATA / "receipt-photos.pdf")  # a real file, but a path, not a name
        write(folder / "attachments.csv", header, [["CLM-1", "r", "receipt", outside, 1]])
        caught = raised(lambda: ClaimsSystem(folder))
        assert isinstance(caught, ValueError), caught
        assert f"no document file {outside!r}" in str(caught), caught
    for name in ("receipt-photos.pdf", "inventories.pdf"):
        raw = (DATA / name).read_bytes()
        for marker in (b"/Info", b"/Title", b"/Author", b"/CreationDate", b"Exif", b"xmpmeta"):
            assert marker not in raw, f"{name} carries {marker!r}"
    # The inventories in make_data.py (read as text: it needs the data group to run).
    source = (Path(recipe.__file__).parent / "make_data.py").read_text(encoding="utf-8")
    assert 'Owned("HDMI cable"' not in source, "the cable is no longer on CLM-26-1001's list"
    for item in (
        'Owned("Acoustic guitar", "479.00")',
        'Owned("Aurora wireless headphones", "279.00")',
        'Owned("Doormat", "24.00")',
        'Owned("TV stand", "39.99")',
    ):
        assert item in source, item
    # CLM-26-1005's written total and claims-system amount include the doormat: the four
    # items plus the 8% the policyholder adds.
    items = sum(Decimal(a) for a in ("899.00", "24.99", "279.00", "24.00"))
    assert (items * Decimal("1.08")).quantize(Decimal("0.01")) == Decimal("1325.15")
    assert Decimal(GUIDE["proof_threshold"]) == Decimal("100.00")
    print("committed data: 8 claims in the claims system, no PDF metadata, inventory items ok")


if __name__ == "__main__":
    check_parsing()
    check_word_matching()
    check_example_claims()
    check_proof_threshold()
    check_review_paths()
    check_statuses()
    check_sie_failure()
    check_labels_and_totals()
    check_csv_safe()
    check_committed_data()
    print("all checks passed")
