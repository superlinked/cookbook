"""Build data/: a public Banking77 slice and a synthetic parsed-document quality set.

Each is split into train and test.

uv run --group data make_data.py
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import yaml

HERE = Path(__file__).parent
TRAIN_PER_LABEL = 17
PUBLIC_TEST_PER_LABEL = 25
PRIVATE_TEST_PER_LABEL = 10
# The Parquet export of PolyAI/banking77 (its refs/convert/parquet branch) at this commit.
BANKING77_REVISION = "5724874eb34f7571b611868e14cba91202b9b2a9"
BANKING77_INTENTS = {
    "card_arrival": "card has not arrived",
    "lost_or_stolen_card": "card lost or stolen",
    "exchange_rate": "exchange rate question",
    "transfer_not_received_by_recipient": "transfer not received by recipient",
    "top_up_failed": "top-up failed",
    "wrong_amount_of_cash_received": "wrong amount of cash from ATM",
}

VENDORS = [
    "Northwind Supply",
    "Harbor Freight Lines",
    "Kestrel Analytics",
    "Bluepeak Energy",
    "Orchard Medical",
    "Granite Insurance",
    "Lumen Logistics",
    "Cedar & Pine Legal",
]
DOCS = [
    "invoice",
    "purchase order",
    "lease agreement",
    "insurance claim form",
    "bank statement",
    "delivery note",
    "service contract",
    "expense report",
]
SENTENCES = [
    "Payment is due within {n} days of the invoice date.",
    "The {doc} covers services delivered between {m1} and {m2} {y}.",
    "{vendor} agrees to provide the goods listed in Schedule A.",
    "Please quote reference {ref} on all correspondence.",
    "The total amount payable, including VAT, is {amt}.",
    "Either party may terminate this agreement with {n} days' written notice.",
    "Goods were received in good condition on {d} {m1} {y}.",
    "The account holder is responsible for charges incurred after the notice date.",
    "Late payments accrue interest at {p}% per month.",
    "This {doc} supersedes all previous versions issued by {vendor}.",
]
MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]
FOREIGN = [
    "La factura debe pagarse dentro de los {n} días siguientes a la fecha de emisión.",
    "Der Vertrag kann von beiden Parteien mit einer Frist von {n} Tagen gekündigt werden.",
    "Le montant total à payer, TVA comprise, s'élève à {amt}.",
    "Veuillez indiquer la référence {ref} dans toute correspondance.",
    "Die Ware wurde am {d}. {m1} {y} in einwandfreiem Zustand geliefert.",
    "El arrendatario es responsable de los gastos posteriores a la fecha de aviso.",
    "Il contratto sostituisce tutte le versioni precedenti emesse da {vendor}.",
    "Os pagamentos em atraso acumulam juros de {p}% ao mês.",
]
OCR_SWAPS = {
    "a": "@",
    "e": "3",
    "i": "1",
    "l": "|",
    "o": "0",
    "s": "$",
    "m": "rn",
    "n": "ii",
    "t": "+",
    "c": "(",
}


def _fill(rng: random.Random, template: str) -> str:
    return template.format(
        n=rng.choice([14, 30, 45, 60, 90]),
        doc=rng.choice(DOCS),
        vendor=rng.choice(VENDORS),
        m1=rng.choice(MONTHS),
        m2=rng.choice(MONTHS),
        y=rng.choice([2024, 2025, 2026]),
        d=rng.randint(1, 28),
        ref=f"{rng.choice('ABCDEFGH')}{rng.randint(10000, 99999)}",
        amt=f"${rng.randint(200, 98000):,}.{rng.randint(0, 99):02d}",
        p=rng.choice([1, 1.5, 2]),
    )


def _clean(rng: random.Random, k: int = 3) -> str:
    head = f"{rng.choice(DOCS).title()} from {rng.choice(VENDORS)}."
    return " ".join([head, *(_fill(rng, t) for t in rng.sample(SENTENCES, k))])


def _garble(rng: random.Random) -> str:
    text = _clean(rng)
    out = []
    for ch in text:
        low = ch.lower()
        if low in OCR_SWAPS and rng.random() < 0.35:
            out.append(OCR_SWAPS[low])
        elif ch == " " and rng.random() < 0.15:
            continue
        else:
            out.append(ch)
        if rng.random() < 0.03:
            out.append(rng.choice("~^`¦§¬"))
    return "".join(out)


def _table(rng: random.Random) -> str:
    cols = rng.choice(
        [
            ["Item", "Qty", "Unit price", "Total"],
            ["Date", "Description", "Debit", "Credit", "Balance"],
            ["SKU", "Description", "Qty", "Amount"],
        ]
    )
    cells = list(cols)
    for _ in range(rng.randint(3, 6)):
        for c in cols:
            if c in ("Qty",):
                cells.append(str(rng.randint(1, 40)))
            elif c in ("Date",):
                cells.append(f"{rng.randint(1, 28):02d}/{rng.randint(1, 12):02d}")
            elif c in ("Item", "Description"):
                cells.append(
                    rng.choice(
                        [
                            "Pallet wrap",
                            "Freight",
                            "Consulting hours",
                            "Card payment",
                            "Transfer in",
                            "Steel bolts M8",
                            "Licence fee",
                            "Courier",
                        ]
                    )
                )
            elif c == "SKU":
                cells.append(f"SKU-{rng.randint(1000, 9999)}")
            else:
                cells.append(f"{rng.randint(5, 9000)}.{rng.randint(0, 99):02d}")
    lead = rng.choice(["", f"{rng.choice(DOCS).title()} {rng.choice(VENDORS)} "])
    return lead + " ".join(cells)


def _truncated(rng: random.Random) -> str:
    text = _clean(rng, k=4)
    cut = rng.randint(int(len(text) * 0.55), int(len(text) * 0.85))
    while cut < len(text) and text[cut] != " ":
        cut += 1
    tail = rng.choice(["", " [page 2 of 4]", " -", ""])
    return text[:cut].rstrip(" .,") + tail


def _handwriting(rng: random.Random) -> str:
    head = _clean(rng, k=2)
    marks = rng.sample(
        [
            "[illegible]",
            "[handwritten note]",
            "[signature]",
            "[unreadable]",
            "[handwriting]",
            "[scribble]",
            "[initials]",
        ],
        k=rng.randint(2, 4),
    )
    middle = " ".join(f"{m} " + rng.choice(["", "?? ", "... "]) for m in marks)
    return f"{head} {middle}".strip()


def _foreign(rng: random.Random) -> str:
    return " ".join(_fill(rng, t) for t in rng.sample(FOREIGN, 3))


PRIVATE_MAKERS = {
    "clean readable text": _clean,
    "garbled OCR characters": _garble,
    "table flattened into run-on numbers": _table,
    "document cut off mid-sentence": _truncated,
    "handwriting left untranscribed": _handwriting,
    "text not in English": _foreign,
}


def _row(text: str, label: str, labels: list[str]) -> dict[str, object]:
    return {"text": text, "all_labels": labels, "true_labels": [label]}


def _write(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def build_private(labels: list[str], seed: int) -> None:
    rng = random.Random(seed)
    train, test = [], []
    for label in labels:
        made = [PRIVATE_MAKERS[label](rng) for _ in range(TRAIN_PER_LABEL + PRIVATE_TEST_PER_LABEL)]
        train += [_row(t, label, labels) for t in made[:TRAIN_PER_LABEL]]
        test += [_row(t, label, labels) for t in made[TRAIN_PER_LABEL:]]
    rng.shuffle(train)
    rng.shuffle(test)
    _write(HERE / "data/private/train.jsonl", train)
    _write(HERE / "data/private/test.jsonl", test)


def build_public(labels: list[str], seed: int) -> None:
    from datasets import load_dataset

    rng = random.Random(seed)
    ds = load_dataset("PolyAI/banking77", revision=BANKING77_REVISION)
    names = ds["train"].features["label"].names
    splits = {}
    for split, per in (("train", TRAIN_PER_LABEL), ("test", PUBLIC_TEST_PER_LABEL)):
        rows = []
        for intent, label in BANKING77_INTENTS.items():
            assert label in labels, label
            idx = names.index(intent)
            texts = [r["text"] for r in ds[split] if r["label"] == idx]
            rows += [_row(t, label, labels) for t in rng.sample(texts, per)]
        rng.shuffle(rows)
        splits[split] = rows
    _write(HERE / "data/public/train.jsonl", splits["train"])
    _write(HERE / "data/public/test.jsonl", splits["test"])


def main() -> None:
    rules = yaml.safe_load((HERE / "rules.yaml").read_text())
    seed = rules["train"]["seed"]
    build_private(list(rules["datasets"]["private"]["labels"]), seed)
    build_public(list(rules["datasets"]["public"]["labels"]), seed)
    for name in ("public", "private"):
        for split in ("train", "test"):
            n = sum(1 for _ in (HERE / f"data/{name}/{split}.jsonl").open())
            print(f"data/{name}/{split}.jsonl: {n} rows")


if __name__ == "__main__":
    main()
