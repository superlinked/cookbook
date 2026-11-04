"""Check the recipe's sheet checks, PIM lookup, category and attribute rules and SIE error rule.

The checks use hand-written strings, the committed data/ and hand-written stand-in model
replies. Nothing here calls SIE or replays a recorded response. Run it with
`uv run --frozen python selfcheck.py`; it exits non-zero if a check fails.
"""

from __future__ import annotations

import contextlib
import copy
import csv
import functools
import io
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from pim import Pim
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
PRODUCTS = {product.sku: product for product in recipe.load_products()}
EMBED, RERANK, EXTRACT = (RULES["models"][key] for key in ("embed", "rerank", "extract"))


def specs(top_level: str) -> dict[str, dict[str, Any]]:
    found = recipe.attribute_specs(top_level, RULES)
    assert found is not None, top_level
    return found


APPAREL = specs("Apparel & Accessories")
ELECTRONICS = specs("Electronics")
FOOD = specs("Food, Beverages & Tobacco")


def product(sku: str, title: str, description: str) -> recipe.Product:
    return recipe.Product(sku, title, description)


def merged(
    item: recipe.Product, specs: dict[str, dict[str, Any]], **values: str | list[str]
) -> recipe.Decision:
    """Run merge_attributes on hand-written model values (one window, the whole row)."""
    decision = recipe.Decision(product=item)
    found = [
        (name, value, item.text())
        for name, given in values.items()
        for value in ([given] if isinstance(given, str) else given)
    ]
    recipe.merge_attributes(found, specs, decision, RULES)
    return decision


def reasons(decision: recipe.Decision) -> str:
    return " | ".join(decision.reasons)


def raised(call: Callable[[], object]) -> BaseException:
    """Whatever the call raises, SystemExit included; fails when it raises nothing."""
    try:
        call()
    except BaseException as error:  # SystemExit too: the check is on which one it was
        return error
    raise AssertionError("nothing was raised")


def check_data() -> None:
    assert len(PRODUCTS) == 51, len(PRODUCTS)
    for sku in ("TT-A02", "TT-A04", "TT-A08", "TT-E01", "TT-E13", "TT-H12", "TT-F07", "TT-X01"):
        assert sku in PRODUCTS, sku
    every = "".join(path.read_text(encoding="utf-8") for path in recipe.DATA_DIR.glob("*.csv"))
    assert "Corvane" not in every, "the old brand name is still in the data"
    assert "Kestrel" not in every
    assert "Halden" not in every
    # The planted accessory names its host's model number, which is TT-E01's.
    assert "AV-H700" in PRODUCTS["TT-E01"].text()
    assert "for the Aurivox AV-H700" in (PRODUCTS["TT-E13"].text())
    # The trimmed taxonomy has the cushions' category and no Lawn & Garden branch.
    paths = [c.path for c in recipe.load_taxonomy(recipe.taxonomy_path(RULES))]
    assert any(p.endswith("> Headphone Cushions") for p in paths)
    assert not any("Lawn & Garden" in p for p in paths)
    print("data: 51 rows, planted SKUs present, old brand names gone, taxonomy trimmed ok")


def check_sheet() -> None:
    # The planted duplicate-SKU sheet stops before any model call and names both rows.
    caught = raised(
        lambda: recipe.load_products(recipe.DATA_DIR / "supplier-products-duplicate-sku.csv")
    )
    assert type(caught) is SystemExit, caught
    message = str(caught)
    assert (
        "TT-A13 on line 2 ('Classic Crew Tee Black M') and line 3 ('Classic Crew Tee White M')"
        in message
    ), message
    assert "No SIE call was made" in message
    # Case and spacing don't make two SKUs.
    caught = raised(
        lambda: recipe.check_unique_skus(
            [product("tt-a13", "Tee Black", "black"), product("TT-A13", "Tee White", "white")], "s"
        )
    )
    assert "tt-a13 on line 0 ('Tee Black') and line 0 ('Tee White')" in str(caught), caught
    # run() checks too, so a caller that builds products itself can't skip it.
    twice = [product("X-1", "Tee Black", "black"), product("X-1", "Tee White", "white")]
    client = StandIn()
    caught = raised(lambda: recipe.run(cast(SIEClient, client), RULES, twice, None))
    assert type(caught) is SystemExit, caught
    assert not client.models, caught
    # A sheet without the description column, or a row without a SKU, stops too.
    folder = Path(tempfile.mkdtemp())
    (folder / "a.csv").write_text("sku,supplier_title\nX-1,Tee\n", encoding="utf-8")
    assert "no column supplier_description" in str(
        raised(lambda: recipe.load_products(folder / "a.csv"))
    )
    (folder / "b.csv").write_text(
        "sku,supplier_title,supplier_description\n,Tee,cotton\n", encoding="utf-8"
    )
    assert "line 2 has no SKU" in str(raised(lambda: recipe.load_products(folder / "b.csv")))
    print("sheet: duplicate SKU stops naming both rows; missing column and empty SKU stop ok")


def check_pim() -> None:
    pim = Pim()
    # The carry-over socks keep the PIM's category; no model call is needed.
    socks = recipe.check_pim(PRODUCTS["TT-A08"], pim, RULES)
    assert socks.status == recipe.IN_PIM
    assert socks.pim
    assert socks.pim.product_id == "P-10231"
    assert socks.category()[1].endswith("> Crew Socks")
    # Another supplier's TT-A01 is a camping mug: the SKU alone finds nothing for this supplier.
    tee = recipe.check_pim(PRODUCTS["TT-A01"], pim, RULES)
    assert tee.pim is None, tee
    assert not tee.reasons, tee
    assert pim.find_by_supplier_sku("Marrowby Goods", "TT-A01")
    # The same SKU with a different title: a reused SKU? Categorize it and send it to review.
    reused = recipe.check_pim(product("TT-A08", "Wool Beanie Hat Grey", "Knit beanie."), pim, RULES)
    assert reused.pim is None, reasons(reused)
    assert "pim_title_differs" in reasons(reused), reasons(reused)
    # Two PIM records under one SKU: review.
    folder = Path(tempfile.mkdtemp())
    seed = recipe.DATA_DIR / "pim-products.csv"
    lines = seed.read_text(encoding="utf-8").splitlines()
    twin = next(line for line in lines if ",TT-A08," in line).replace("P-10231", "P-10999")
    (folder / "pim.csv").write_text("\n".join([*lines, twin]) + "\n", encoding="utf-8")
    several = recipe.check_pim(PRODUCTS["TT-A08"], Pim(folder / "pim.csv"), RULES)
    assert several.pim is None, reasons(several)
    assert "P-10231, P-10999" in reasons(several), reasons(several)
    # Another variant under the carry-over SKU: same words, another size, pack count or colour.
    # It must not keep the PIM's size L... or grey.
    carry = PRODUCTS["TT-A08"]
    for title, description, reason in (
        ("Merino Hiking Socks Grey L (2 pk)", "Hiking socks. Color: grey. Size: L.", "variant"),
        ("Merino Hiking Socks Grey M (3 pk)", "Hiking socks. Color: grey. Size: M.", "variant"),
        ("Merino Hiking Socks Navy M (2 pk)", "Hiking socks. Color: navy. Size: M.", "title"),
    ):
        other = recipe.check_pim(product("TT-A08", title, description), pim, RULES)
        assert other.pim is None, (title, reasons(other))
        assert f"pim_{reason}_differs" in reasons(other), (title, reasons(other))
    # SKU case is ignored in the PIM lookup the same way as in the duplicate check.
    lower = recipe.check_pim(product("tt-a08", carry.title, carry.description), pim, RULES)
    assert lower.status == recipe.IN_PIM, reasons(lower)
    # A PIM record with no category has nothing to keep: categorize it, and review.
    blank = next(line for line in lines if ",TT-A08," in line).split(",")
    blank[4:6] = ["", ""]
    (folder / "blank.csv").write_text("\n".join([lines[0], ",".join(blank)]) + "\n", "utf-8")
    nocat = recipe.check_pim(carry, Pim(folder / "blank.csv"), RULES)
    assert nocat.pim is None, nocat
    assert "pim_no_category" in reasons(nocat), reasons(nocat)
    print(
        "pim: carry-over SKU in_pim, any case; other supplier's code, reused SKU, other variant, "
        "twins and no category not trusted ok"
    )


def ranked(*pairs: tuple[str, float]) -> recipe.Decision:
    decision = recipe.Decision(product=product("X", "x", "x"))
    decision.ranked = [
        (recipe.Category(f"gid/{i}", path), score) for i, (path, score) in enumerate(pairs)
    ]
    return decision


def check_category() -> None:
    bottles = "Home & Garden > Kitchen & Dining > Food & Beverage Carriers"
    # A clear lead over a different category is trusted.
    clear = ranked((f"{bottles} > Water Bottles", 0.99), (f"{bottles} > Thermoses", 0.50))
    assert recipe.decide_category(clear, RULES)
    assert not clear.reasons
    # Two real categories close together: review, with both side by side. Both are in one
    # branch, so the attributes to read are known and extraction still runs.
    thin = ranked((f"{bottles} > Thermoses", 0.99), (f"{bottles} > Water Bottles", 0.98))
    assert recipe.decide_category(thin, RULES)
    assert "thin_margin" in reasons(thin)
    assert "Water Bottles" in reasons(thin)
    # Close to a path in another branch (Honey vs Honey Jars): which attributes is unknown.
    jars = ranked(
        ("Food, Beverages & Tobacco > Food Items > Honey", 0.98),
        ("Home & Garden > Kitchen & Dining > Food Storage > Honey Jars", 0.97),
    )
    assert not recipe.decide_category(jars, RULES)
    assert "thin_margin" in reasons(jars)
    # A parent and its child are one answer at two depths, never a tie.
    lineage = ranked((f"{bottles} > Thermoses", 0.99), (bottles, 0.989))
    assert recipe.decide_category(lineage, RULES), lineage.reasons
    assert not lineage.reasons, lineage.reasons
    # A low best score: review, the category may be missing from the taxonomy.
    low = ranked((f"{bottles} > Thermoses", 0.30))
    assert not recipe.decide_category(low, RULES)
    assert "low_score" in reasons(low)
    # A confident pick that stops at a top-level branch is not a category.
    top = ranked(("Home & Garden", 0.95), ("Electronics > Audio", 0.10))
    assert not recipe.decide_category(top, RULES)
    assert "top_level_only" in reasons(top)
    # A low-score contrary candidate still counts against a weak leader (margin, not dropped).
    weak = ranked((f"{bottles} > Thermoses", 0.62), ("Electronics > Audio", 0.40))
    assert not recipe.decide_category(weak, RULES)
    assert "thin_margin" in reasons(weak)
    print("category: lead, thin margin, lineage, low score, top level, contrary evidence ok")


def check_attributes() -> None:
    sweater = PRODUCTS["TT-A04"]
    # Whole words only: "M" is not a size read out of "Merino".
    d = merged(sweater, APPAREL, color="charcoal grey", material="100% merino wool", size="M")
    assert "missing_attribute: size" in reasons(d), reasons(d)
    assert "size" not in d.attributes, reasons(d)
    # Values that say "not given" are missing, not a size.
    for value in ("not specified", "N/A", "no size"):
        item = product("X", "Tee", f"Cotton tee in black. Size: {value}.")
        d = merged(item, APPAREL, color="black", material="Cotton", size=value)
        assert "missing_attribute: size" in reasons(d), (value, reasons(d))
    # One span holding two sizes: flagged, never one silently kept.
    item = product("X", "Tee Black", "Cotton tee in black, size M or L.")
    d = merged(item, APPAREL, color="black", material="Cotton", size="M or L")
    assert "lists two" in reasons(d), reasons(d)
    assert "size" not in d.attributes, reasons(d)
    assert "missing_attribute: size" not in reasons(d), "asked twice about one size"
    # Two windows, two sizes: conflicting. A value inside a longer one is the same value.
    item = product("X", "Tee Navy M", "Heather navy cotton tee. Size: L.")
    d = merged(item, APPAREL, color=["navy", "Heather navy"], material="cotton", size=["M", "L"])
    assert "conflicting_attribute: size = M / L" in reasons(d), reasons(d)
    assert d.attributes["color"] == "Heather navy"
    assert "color" not in reasons(d)
    # Only whole words make a value the same: L is not inside XL, AV-H70 not inside AV-H700.
    item = product("X", "Tee Black XL", "Cotton tee in black. Size: L.")
    d = merged(item, APPAREL, color="black", material="Cotton", size=["XL", "L"])
    assert "conflicting_attribute: size" in reasons(d), reasons(d)
    item = product("X", "Aurivox AV-H70 Speaker", "Brand: Aurivox. Model: AV-H700.")
    d = merged(item, ELECTRONICS, brand="Aurivox", model_number=["AV-H70", "AV-H700"])
    assert "conflicting_attribute: model_number" in reasons(d), reasons(d)
    # A clear row passes, with the supplier's words for each value.
    jeans = PRODUCTS["TT-A02"]
    d = merged(jeans, APPAREL, color="indigo", size="28", material="stretch denim")
    assert not d.reasons, reasons(d)
    assert d.attributes["size"] == "28", reasons(d)
    assert "INDIGO 28" in d.evidence["size"], d.evidence
    # A model number must have letters and digits: "2.1" (channels) is not one.
    soundbar = PRODUCTS["TT-E07"]
    d = merged(soundbar, ELECTRONICS, brand="Aurivox", model_number="2.1")
    assert "missing_attribute: model_number" in reasons(d), reasons(d)
    d = merged(soundbar, ELECTRONICS, brand="Aurivox", model_number="AV-SB400")
    assert not d.reasons, reasons(d)
    # The accessory's text names its host: AV-H700 after "for" is not the cushions' model.
    cushions = PRODUCTS["TT-E13"]
    d = merged(cushions, ELECTRONICS, brand="Aurivox", model_number="AV-H700")
    assert "names_another_product: model_number 'AV-H700' follows 'for'" in reasons(d), reasons(d)
    assert "model_number" not in d.attributes
    assert [q["issue"] for q in d.questions] == ["'AV-H700' is the product it fits"], d.questions
    # The headphones themselves keep the same model number: it's theirs.
    d = merged(PRODUCTS["TT-E01"], ELECTRONICS, brand="Aurivox", model_number="AV-H700")
    assert not d.reasons, reasons(d)
    assert d.attributes["model_number"] == "AV-H700", reasons(d)
    # "fits", "compatible with" too; a "for" further back than the window doesn't count.
    item = product("X", "Case", "Hard case, compatible with BM-S5 speakers. Brand: Brenmoor.")
    d = merged(item, ELECTRONICS, brand="Brenmoor", model_number="BM-S5")
    assert "follows 'compatible with'" in reasons(d), reasons(d)
    item = product("X", "Ear Pads", "Ear pads, compatible with the Aurivox AV-H700. By Brenmoor.")
    d = merged(item, ELECTRONICS, brand="Brenmoor", model_number="AV-H700")
    assert "model_number 'AV-H700' follows 'compatible with'" in reasons(d), reasons(d)
    assert "model_number" not in d.attributes, d.attributes
    item = product("X", "Turntable", "Turntable for vinyl records with its own BM-TT1 drive.")
    d = merged(item, ELECTRONICS, brand="Brenmoor", model_number="BM-TT1")
    assert "names_another_product" not in reasons(d), reasons(d)
    # A third-party accessory: the host's brand after "for" is not this product's brand. The
    # cushions' own "Brand: Aurivox" still counts, though "for the Aurivox" follows.
    item = product("X", "Ear Pads for Aurivox AV-H700", "Ear pads for Aurivox headphones.")
    d = merged(item, ELECTRONICS, brand="Aurivox", model_number="AV-H700")
    assert "names_another_product: brand 'Aurivox'" in reasons(d), reasons(d)
    d = merged(cushions, ELECTRONICS, brand="Aurivox")
    assert d.attributes.get("brand") == "Aurivox", reasons(d)
    # Decimals are whole numbers too: "US 9" is not read out of "US 9.5".
    item = product("X", "Canvas Sneaker", "Canvas upper, white. Size: US 9.5.")
    d = merged(item, APPAREL, color="white", material="Canvas", size="US 9")
    assert "missing_attribute: size" in reasons(d), reasons(d)
    # A value that isn't in the row is dropped, not kept.
    d = merged(soundbar, ELECTRONICS, brand="Aurivox", model_number="AV-SB500")
    assert "missing_attribute: model_number" in reasons(d)
    # Net quantity: pack-times-size and glued units read; two different weights conflict.
    water = PRODUCTS["TT-F07"]
    d = merged(water, FOOD, net_quantity="12 x 330 ml")
    assert not d.reasons, reasons(d)
    assert d.attributes["net_quantity"] == "12 x 330 ml", reasons(d)
    rice = PRODUCTS["TT-F10"]
    d = merged(rice, FOOD, net_quantity="2 kg")
    assert not d.reasons, reasons(d)
    item = product(
        "X", "Whole Bean Coffee 340g", "Medium roast whole bean coffee. Net weight: 400 g."
    )
    d = merged(item, FOOD, net_quantity="400 g")
    assert "the row says 340 g and 400 g" in reasons(d), reasons(d)
    assert recipe.scan_values("1L oat drink, 1 l", FOOD["net_quantity"]["scan"]) == ["1 l"]
    assert recipe.scan_values("Lemon 12x330ml", FOOD["net_quantity"]["scan"]) == ["330 ml"]
    # A missing attribute becomes a question for the supplier, with the product's title.
    d = merged(sweater, APPAREL, color="charcoal grey", material="100% merino wool")
    assert {"attribute": "size", "issue": "missing"}.items() <= d.questions[0].items()
    print(
        "attributes: whole words, not given, two in one span, conflicts, model pattern, "
        "'for X' host, net quantity scan, questions ok"
    )


def check_windows() -> None:
    text = " ".join(f"w{i}" for i in range(400))
    parts = recipe.windows(text, 180, 30)
    assert all(len(part.split()) <= 180 for part in parts), [len(p.split()) for p in parts]
    assert parts[0].split()[-30:] == parts[1].split()[:30], "windows don't overlap"
    assert parts[-1].endswith("w399")
    print(f"windows: {len(parts)} windows of at most 180 words, overlapping by 30 ok")


# --- The SIE error rule, through run() with a stand-in client -------------------------------


class StandIn:
    """Stands in for SIEClient. Encode calls take the next listed encode outcome (None for a
    good reply); score calls answer from `scores` by SKU; extract calls take the next reply
    listed for the SKU. An exception is raised, anything else is returned."""

    def __init__(
        self,
        encode: list[Any] | None = None,
        scores: dict[str, Any] | None = None,
        extract: dict[str, list[Any]] | None = None,
    ) -> None:
        self.encode_outcomes = list(encode or [])
        self.scores = dict(scores or {})
        self.extracts = {sku: list(replies) for sku, replies in (extract or {}).items()}
        self.models: list[str] = []
        self.extract_options: list[Any] = []

    @staticmethod
    def _raise_or(reply: Any) -> Any:
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def encode(self, model: str, items: list[Any], **_: Any) -> Any:
        self.models.append(model)
        outcome = self.encode_outcomes.pop(0) if self.encode_outcomes else None
        if outcome is not None:
            return self._raise_or(outcome)
        return [{"dense": [1.0, float(len(item["text"]) % 7 + 1)]} for item in items]

    def score(self, model: str, query: Any, items: list[Any], **_: Any) -> Any:
        self.models.append(model)
        reply = self.scores.get(query["id"], {})
        if not isinstance(reply, dict) or "scores" in reply or isinstance(reply, BaseException):
            return self._raise_or(reply)
        leaf = {item["id"]: item["text"].split(" > ")[-1] for item in items}
        return {
            "scores": [{"item_id": i, "score": reply.get(name, 0.01)} for i, name in leaf.items()]
        }

    def extract(self, model: str, item: Any, **options: Any) -> Any:
        self.models.append(model)
        self.extract_options.append(options.get("options"))
        return self._raise_or(self.extracts[item["id"].split("#")[0]].pop(0))


TAXONOMY = """\
gid/aa : Apparel & Accessories
gid/aa-1 : Apparel & Accessories > Clothing
gid/aa-1-1 : Apparel & Accessories > Clothing > T-Shirts
gid/aa-1-2 : Apparel & Accessories > Clothing > Shirts
gid/el : Electronics
gid/el-1 : Electronics > Audio > Headphones
"""
TEE = product("S-1", "Crew Tee Black M", "Cotton t-shirt in black. Size: M.")
SHIRT = product("S-2", "Oxford Shirt Blue L", "Cotton oxford shirt in blue. Size: L.")
GOOD_SCORES = {"S-1": {"T-Shirts": 0.99, "Shirts": 0.20}, "S-2": {"Shirts": 0.99, "T-Shirts": 0.2}}


def tee_data(color: str = "black", size: str = "M") -> dict[str, Any]:
    return {"data": {"color": color, "size": size, "material": "Cotton"}}


def shirt_data() -> dict[str, Any]:
    return {"data": {"color": "blue", "size": "L", "material": "Cotton"}}


def run(
    client: StandIn,
    products: list[recipe.Product] | None = None,
    folder: Path | None = None,
    batch: int | None = None,
    edit: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[dict[str, recipe.Decision], Path]:
    """Run the recipe on a six-path taxonomy with the stand-in; outputs go to `folder`/out."""
    folder = folder or Path(tempfile.mkdtemp())
    (folder / "taxonomy.txt").write_text(TAXONOMY, encoding="utf-8")
    rules = copy.deepcopy(RULES)
    rules["taxonomy"]["file"] = str(folder / "taxonomy.txt")
    rules["taxonomy"]["embed_batch_size"] = batch or rules["taxonomy"]["embed_batch_size"]
    if edit:
        edit(rules)
    output = folder / "out"
    with contextlib.redirect_stdout(io.StringIO()):  # the run's "1 of 6 failed" line
        decisions, _ = recipe.run(
            cast(SIEClient, client), rules, products or [TEE, SHIRT], output, Pim()
        )
    return {d.product.sku: d for d in decisions}, output


def ok_client(**extract: list[Any]) -> StandIn:
    replies = {"S-1": [tee_data()], "S-2": [shirt_data()]} | extract
    return StandIn(scores=GOOD_SCORES, extract=replies)


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


def check_run_outputs() -> None:
    hostile = product("=HYPERLINK(1)", "=cmd|' /C calc'!A0 Tee", "Cotton tee in black. Size: M.")
    client = StandIn(scores={hostile.sku: {"T-Shirts": 0.99}}, extract={hostile.sku: [tee_data()]})
    decisions, output = run(client, [hostile])
    assert decisions[hostile.sku].status == recipe.CHECKS_PASSED, decisions[hostile.sku].reasons
    with (output / "catalog.csv").open(encoding="utf-8", newline="") as handle:
        row = next(csv.DictReader(handle))
    assert row["sku"] == "'=HYPERLINK(1)", row
    assert row["supplier_title"].startswith("'="), row
    assert row["size"] == "M", row
    assert row["category_path"].endswith("T-Shirts"), row
    # The carry-over SKU keeps the PIM's category with no model call; the new row still runs.
    client = ok_client()
    decisions, output = run(client, [TEE, PRODUCTS["TT-A08"]])
    assert decisions["TT-A08"].status == recipe.IN_PIM
    assert RERANK in client.models, client.models
    assert client.models.count(EXTRACT) == 1, client.models
    with (output / "catalog.csv").open(encoding="utf-8", newline="") as handle:
        rows = {r["sku"]: r for r in csv.DictReader(handle)}
    assert rows["TT-A08"]["status"] == "in_pim"
    assert rows["TT-A08"]["material"] == "merino wool blend"
    # Statuses name what the checks found; the old decision-sounding 'tagged' is gone.
    assert {r["status"] for r in rows.values()} <= {"checks_passed", "review", "in_pim"}
    # The review sheet carries the supplier's own description; questions name the SKU.
    client = ok_client(**{"S-1": [tee_data(size="XS")]})
    decisions, output = run(client)
    with (output / "review.csv").open(encoding="utf-8", newline="") as handle:
        review = list(csv.DictReader(handle))
    assert [r["sku"] for r in review] == ["S-1"]
    assert review[0]["supplier_description"] == TEE.description
    questions = (output / "questions.csv").read_text(encoding="utf-8")
    assert "S-1,Crew Tee Black M,size,missing" in questions, questions
    # A thin margin inside one branch still reads the attributes; one across branches doesn't.
    client = ok_client()
    client.scores["S-1"] = {"T-Shirts": 0.99, "Shirts": 0.98}
    client.scores["S-2"] = {"Shirts": 0.99, "Headphones": 0.98}
    decisions, _ = run(client)
    assert "thin_margin" in reasons(decisions["S-1"])
    assert "thin_margin" in reasons(decisions["S-2"])
    assert decisions["S-1"].attributes
    assert not decisions["S-2"].attributes
    assert client.models.count(EXTRACT) == 1, client.models
    # Every extract call asks SIE to fail a window too long to read whole, not cut it short.
    client = ok_client()
    run(client)
    assert client.extract_options == [{"overflow_policy": "error"}] * 2, client.extract_options
    # A branch rules.yaml has no attribute list for: review, and nothing is read.
    client = ok_client()
    decisions, _ = run(client, edit=lambda rules: rules["attributes"].pop("Apparel & Accessories"))
    for d in decisions.values():
        assert "no_attribute_rules: rules.yaml has no attributes for 'Apparel" in reasons(d), d
        assert d.status == recipe.REVIEW
    assert EXTRACT not in client.models, client.models
    print(
        "outputs: csv cells quoted, in_pim rows skip models, review has description, questions ok"
    )


def check_sie_failure() -> None:
    for error in TRANSIENT:
        assert not recipe.is_fatal(error), f"{type(error).__name__} should be transient"
    for error in FATAL:
        assert recipe.is_fatal(error), f"{type(error).__name__} should stop the run"
    long = "connection refused " + "x" * 300
    # A transient extract failure: that row goes to review with the error, cut short; the
    # other row passes.
    for error in [*TRANSIENT, SIEConnectionError(long)]:
        decisions, _ = run(ok_client(**{"S-1": [error]}))
        why = reasons(decisions["S-1"])
        assert why.startswith("extract_failed: window 1 was not read: the SIE call failed: "), why
        assert decisions["S-1"].status == recipe.REVIEW
        assert decisions["S-2"].status == recipe.CHECKS_PASSED, decisions["S-2"].reasons
        # What the unread window says is unknown: no "missing" reason, no supplier question.
        assert "missing_attribute" not in why, why
        assert not decisions["S-1"].questions, decisions["S-1"].questions
    assert len(why) < 400, why
    # SIE's empty-result HTTP 500 is a window not read: review, never a quiet note.
    empty = ServerError(recipe.EMPTY_RESULT_ERROR, status_code=500)
    decisions, _ = run(ok_client(**{"S-1": [empty]}))
    assert "SIE answered HTTP 500" in reasons(decisions["S-1"]), reasons(decisions["S-1"])
    # A reply missing its key is a failed call on the same path, not "nothing found".
    bad_replies: list[dict[str, Any]] = [
        {"result": {}},
        {"data": None},
        {"error": "item failed", "data": {}},
    ]
    for reply in bad_replies:
        decisions, _ = run(ok_client(**{"S-1": [reply]}))
        assert "extract_failed" in reasons(decisions["S-1"]), (reply, reasons(decisions["S-1"]))
    client = StandIn(
        scores={"S-1": {"no": "scores"}, "S-2": GOOD_SCORES["S-2"]}, extract={"S-2": [shirt_data()]}
    )
    client.scores["S-1"] = {"scores": "missing"}
    decisions, _ = run(client)
    assert "rerank_failed" in reasons(decisions["S-1"]), reasons(decisions["S-1"])
    # An empty `data` is a real "nothing found": the attributes are missing, not a failed call.
    decisions, _ = run(ok_client(**{"S-1": [{"data": {}}]}))
    assert "missing_attribute: size" in reasons(decisions["S-1"])
    assert "extract_failed" not in reasons(decisions["S-1"])
    # A failed taxonomy batch (of two): its paths are in no shortlist, so every row goes to
    # review, and the run goes on with the paths that were embedded.
    client = ok_client()
    client.encode_outcomes = [None, ServerError("busy", status_code=503)]
    decisions, _ = run(client, batch=3)
    for d in decisions.values():
        assert "shortlist_incomplete: 3 of 6 taxonomy paths" in reasons(d), reasons(d)
        assert d.status == recipe.REVIEW
    # A failed product batch: its rows go to review with no shortlist and no rerank call.
    client = StandIn(encode=[None, ServerError("busy", status_code=503)])
    decisions, _ = run(client)
    for d in decisions.values():
        assert "not_embedded: the product's embedding call failed: " in reasons(d), reasons(d)
        assert "busy" in reasons(d), reasons(d)  # SIE's own words, not just "failed"
    assert RERANK not in client.models
    # An encode reply without a vector per text, or scores for the wrong paths, is a failed
    # call too.
    client = StandIn(encode=[None, [{"dense": [1.0, 2.0]}, {"sparse": [1]}]])
    decisions, _ = run(client)
    assert all("not_embedded" in reasons(d) for d in decisions.values())
    client = ok_client()
    client.scores["S-1"] = {"scores": [{"item_id": "c0", "score": 0.99}]}
    decisions, _ = run(client)
    assert "rerank_failed" in reasons(decisions["S-1"]), reasons(decisions["S-1"])
    # A NaN, null or missing score would slip past every threshold: a failed call instead.
    for bad in ({"score": float("nan")}, {"score": None}, {}):
        client = ok_client()
        entries = [{"item_id": f"c{i}", "score": 0.1} for i in range(1, 6)]
        client.scores["S-1"] = {"scores": [{"item_id": "c0", **bad}, *entries]}
        decisions, _ = run(client)
        why = reasons(decisions["S-1"])
        assert why.startswith("rerank_failed: "), (bad, why)
        assert "not numbers" in why, (bad, why)
        assert decisions["S-2"].status == recipe.CHECKS_PASSED
    # A GLiNER2 value that isn't text (a list, a number) is a failed call; null is "not found".
    for data in ({"color": "black", "size": ["M"], "material": "Cotton"}, {"size": 3}):
        decisions, _ = run(ok_client(**{"S-1": [{"data": data}]}))
        why = reasons(decisions["S-1"])
        assert why.startswith("extract_failed: "), (data, why)
        assert "not text" in why, (data, why)
    decisions, _ = run(ok_client(**{"S-1": [tee_data(size=cast(str, None))]}))
    assert reasons(decisions["S-1"]) == "missing_attribute: size not found in the row"

    def stops(client: StandIn) -> str:
        """The stop message; also checks the run wrote nothing."""
        folder = Path(tempfile.mkdtemp())
        message = stopped(functools.partial(run, client, None, folder))
        assert not (folder / "out").exists(), "a stopped run wrote outputs"
        return message

    # Every call failed (a dead SIE_BASE_URL): the run stops, writes nothing, full message.
    message = stops(StandIn(encode=[SIEConnectionError(long)]))
    assert message.startswith(f"Every SIE call failed (1 of 1 to {EMBED})"), message
    assert long in message, message
    assert "Check SIE_BASE_URL and your network." in message, message
    assert "No outputs were written." in message
    message = stops(StandIn(encode=[ProvisioningError("no GPU yet")]))
    assert "may still be starting or SIE is busy; run again in a few minutes." in message, message
    # A refusal on a later call stops the run at once, with SIE's full message.
    message = stops(ok_client(**{"S-2": [InsufficientCreditsError("no credits " + "y" * 300)]}))
    assert message.startswith(f"SIE refused the call to {EXTRACT}, "), message
    assert "InsufficientCreditsError (HTTP 402, code INSUFFICIENT_CREDITS)" in message, message
    assert "y" * 300 in message
    assert "SIE_API_KEY" in message
    message = stops(StandIn(scores={"S-1": RequestError("bad key", status_code=401)}))
    assert message.startswith(f"SIE refused the call to {RERANK}, RequestError (HTTP 401)")
    message = stops(StandIn(encode=[ModelLoadFailedError("gated", permanent=True)]))
    assert message.startswith(f"SIE can't load {EMBED}, ModelLoadFailedError"), message
    message = stops(StandIn(encode=[RequestError("unexpected body")]))
    assert "SIE_API_KEY" not in message, message
    assert "SIE_BASE_URL" in message, message
    # A bug in our own code is not hidden, and main turns only SIEStopped into a clean exit.
    caught = raised(lambda: run(ok_client(**{"S-1": [TypeError("bad argument")]})))
    assert type(caught) is TypeError, f"TypeError became {type(caught).__name__}"

    def bug() -> None:
        raise RuntimeError("a bug in recipe.py")

    def refusal() -> None:
        raise recipe.SIEStopped("SIE refused")

    assert type(raised(lambda: recipe.stop_on_refusal(bug))) is RuntimeError
    caught = raised(lambda: recipe.stop_on_refusal(refusal))
    assert type(caught) is SystemExit
    assert str(caught) == "Stopped: SIE refused"
    print("sie failure: transient errors go to review; refusals and all-failed stop; bugs raise ok")


if __name__ == "__main__":
    check_data()
    check_sheet()
    check_pim()
    check_category()
    check_attributes()
    check_windows()
    check_run_outputs()
    check_sie_failure()
