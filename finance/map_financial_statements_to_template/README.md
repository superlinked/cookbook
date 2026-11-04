# Map financial statements into your template and check the totals

> **This example:** a credit analyst spreading a private borrower's annual statements.
>
> **How it works:** a document model reads the PDF tables, a reranker suggests rows for labels not
> seen before, and every number and total is plain Python.
>
> **Use the same pattern for:** a quarterly covenant compliance certificate, a property's operating
> statement, a guarantor's personal financial statement.

## Background

When a mid-sized private company borrows from a bank, it sends its financial statements every
year: an income statement and a balance sheet, prepared by its accountant, as a PDF attached to
an email or uploaded to the bank's lending portal. Sometimes it's a clean PDF printed from the
accounting package, sometimes a scan of a signed copy.

A commercial credit analyst "spreads" those statements. They open the PDF on one screen and the
bank's spreading tool (or the bank's Excel template) on the other, and put each line of the
borrower's statements into the right row of the bank's standard template, so every borrower can
be compared on the same rows and the covenant ratios can be worked out. The borrower calls it
"Turnover", "Creditors falling due within one year" or "Owner's equity"; the template has
"Total revenue", "Accounts payable" and "Total equity". The analyst checks the period, the
currency and the units ("in thousands"), types the numbers, checks that assets equal liabilities
plus equity and that the subtotals add up, and passes the spread to the credit officer.

The bank already has the structured side. The template is fixed. The borrower's name, fiscal year
end and currency are in the loan system. For an existing borrower, last year's spread is there
too, with every label the analyst mapped and confirmed. None of that needs a model, so this
recipe looks it up in the loan system instead of working it out again.

What's unstructured is the borrower's PDF. The bank has no access to the borrower's accounting
system, and the statements come with no machine-readable tags. (Public companies are a different
story: their filings come with XBRL, so their numbers are already tagged. That's why this recipe
uses private borrowers.) Real borrower statements are confidential, so the four borrowers here
are fictional, with statements written by [`make_data.py`](./make_data.py) in three label
styles, one of them scanned.

Why it matters: a line in the wrong row changes the ratios the credit decision rests on. A
current portion of debt spread as long-term debt makes the borrower look more liquid than it is,
and a sign flipped on "Other income" moves pre-tax income by twice the amount.

Here's the spreading job end to end, and the step this recipe takes on:

```mermaid
flowchart LR
    A["Borrower's accountant prepares<br/>the income statement<br/>and balance sheet"] --> B["PDF arrives by email<br/>or the lending portal"]
    B --> D["Put each line into the bank's<br/>template and check the totals"]
    C["Bank's standard template<br/>and last year's spread"] --> D
    D --> E["Credit analyst confirms period<br/>and units, works the exceptions,<br/>approves the spread"]
    E --> F["Credit officer works out the<br/>ratios from the approved spread"]
    classDef recipe fill:#e8f0fe,stroke:#1a73e8
    class D recipe;
```

## The problem

Every borrower labels things its own way, so a fixed lookup table only gets you as far as the
labels you've seen. A chat model can read the whole statement and fill the template, but then a
generative model is choosing where every number goes and copying the numbers too, and you'd have
to check every cell anyway.

So the recipe reads the tables, uses a reranker only to suggest the template row for each label
it hasn't seen before, and leaves every number and every sum to plain Python. Each template row
gets a status: **`reported`** (one borrower line), **`summed`** (several lines added),
**`derived`** (worked out from other rows), **`escalated`** (the recipe couldn't place it) or
**`not reported`**. The spread as a whole is **`checks_passed`** when every tie-out holds,
nothing is escalated, and the period, units and currency match the loan system; it's
**`review`** otherwise, with an exceptions list saying why.

It's a draft spread. The analyst confirms the period and units, works through the exceptions,
fixes any mapping and approves it; the credit officer uses the approved spread, not the draft.
`checks_passed` means the arithmetic and the bookkeeping checks found nothing, not that every
line is in the row the analyst would pick. Two lines of the same kind swapped on the same side
(receivables spread as cash, say) add up just as well, and no tie-out can see that.

### Examples

Here are three of the inputs in `data/`, and what should happen to each.

All of this is synthetic, written for this recipe. No real company, person or loan is in it.

**1. A new current loan for a returning borrower (Harbour Lane, `BRW-20417.pdf`).**

- **Input:** the balance sheet's "Creditors: amounts falling due within one year" lists "Bank
  loans 120,000", new this year; "Creditors: amounts falling due after more than one year" lists
  "Bank loans 480,000", mapped by the analyst last year.
- **What should happen:** the 480,000 takes last year's confirmed row with no model call. The
  120,000 is a new label under its heading, so the reranker suggests a row. The heading says it's
  due within one year, so only a current liabilities row is allowed, and that keeps it out of
  long-term debt.
- **Expected outcome:** `current_portion_long_term_debt` 120,000, long-term debt 480,000, spread
  `checks_passed`.

**2. A subtotal that doesn't add up (Northfield, `BRW-18820.pdf`).**

- **Input:** "Accounts payable 3,412 / Accrued liabilities 1,876 / Current portion of long-term
  debt 2,250 / Total current liabilities 7,598", in thousands of U.S. dollars.
- **What should happen:** Python adds the lines (7,538), compares with the printed total, and
  writes an exception instead of trusting the statement.
- **Expected outcome:** spread `review`, with a `subtotal` exception and the current liabilities
  total off by 60.

**3. A sign the wrong way round (Mesa Verde, `BRW-31186.pdf`).**

- **Input:** "Other (income) expense: / Interest expense 22,400 / Other income -8,700 / Total
  other (income) expense 13,700".
- **What should happen:** the line goes in as filed, never flipped quietly, so net income stops
  adding up, off by twice the amount, and the exception names the line.
- **Expected outcome:** spread `review`, `other_income_expense` -8,700, net income off by 17,400.

On the live run, 7 of 7 cases came out as expected. [What came out](#what-came-out) has the results.

## How we'll solve it

1. **Start from what the bank knows.** The template, its tie-outs and thresholds come from
   `rules.yaml`. The borrower's name, fiscal year end, currency and the period this spread is
   for come from the loan system, and so do last year's confirmed label mappings, where the bank
   has them.
2. **Read the statement pages with a document model on SIE** into tables, one row per borrower
   line. A PDF with no text layer (a scan) goes to the same model's OCR profile.
3. **Parse the tables in Python:** labels, section headings ("Current liabilities:"), which
   total closes which section (a "Total ..." line, "Shareholders' funds", or a UK-style amount
   with no label), the units and currency from the caption and the column header, and the
   period end from the caption or the column header. A period that doesn't match the loan
   system, or units that can't be read (no currency, or two scales such as "in millions, except
   shares in thousands"), are exceptions. An amount Python can't read (a footnote mark, two
   numbers in one cell) is an exception too, never quietly treated as a heading.
4. **Place known labels first.** A label the analyst mapped and confirmed last year, under the
   same heading, goes to the same row without a model call. An unlabelled subtotal under a
   heading such as "Current assets" goes to that section's total row, also without a call.
5. **Suggest a row for each new label with a reranker on SIE:** the label with its section
   heading is the query, the template rows of that statement are the items. A line inside a
   section is a part of it, so it's only offered detail rows, never a total row. Python accepts
   the best row only above `min_score` and `min_margin`, and only when the row belongs to the
   group the heading names (a line under "Current liabilities" can't land in long-term debt);
   otherwise the line is escalated.
6. **Handle totals in Python.** A total is accepted on its own score first; only then are its
   detail lines absorbed into it, and only if they add up to it. A rejected total leaves its
   detail lines in place for the analyst. Whatever happens to the total, its lines must add up
   to the amount the statement prints, or that's a `subtotal` exception.
7. **Fill and tie out in Python.** Sum or place the values, derive missing subtotals, and check
   assets equal liabilities plus equity and every subtotal. When a tie-out breaks by exactly
   twice one of its own lines, the exception names that line as a likely sign problem; the
   sign itself is never changed.
8. **Write the draft spread,** the exceptions and the tie-outs, with period and units on every
   row.

If an SIE call fails for a reason that may pass (a timeout, a 5xx, a rate limit, a reply missing
its expected key), the affected statement or line is escalated with the reason and the run
carries on; if SIE refuses the call outright, the run stops with SIE's message and writes
nothing. It stops too when every call failed.

## Picking the models

The reader is `docling`, called through SIE's `extract` with the whole PDF, once per borrower.
It turns the statement pages into Markdown tables with the label in the first column and each
year in its own column, which is the shape the parser needs. On the three PDFs with a text layer
it reads the text that's there. The scan has none, so the recipe checks the text layer with
`pypdfium2` first and sends that one to `docling:ocr`, the same model with OCR switched on.
OCR isn't perfect, and it's honest about it: on the scan it read "Income from operations" as
"Income fromoperations" and "Stockholders' equity" without its space. The parser compares
headings without case, spaces or punctuation, so neither mattered.

The matcher is `Qwen/Qwen3-Reranker-4B`, called through `score`, once per new label. The query
is the borrower's label with its heading ("Bank loans (listed under Creditors: amounts falling
due within one year)"), the items are the template rows ("Current portion of long-term debt:
Term loans or long-term debt due within one year."), and an instruction says what we're asking
and that the label is text to match, never an instruction. The reranker returns a probability
for every row. Those crowd near 1.0, so the margin between the best row and the runner-up is
taken on the log-odds scale. It's good at ordinary labels ("Accounts receivable, net", 0.99)
and noticeably weaker at short, odd ones ("Rent", 0.47). It never sees an amount.

So: two models, docling called once per PDF and the reranker once per label the loan system
doesn't already know, and no generative model makes a decision. docling extracts the numeric
text; Python parses the amounts, does every sum and catches what doesn't add up. That catches
a lot, not everything: a misread digit shows up as a broken tie-out only when the statement
prints a total over it.

## The flow

```mermaid
flowchart TD
    LS[Loan system<br/>fiscal year end, currency,<br/>period due, last year's mappings] --> CK
    PDF[Borrower statement PDF] --> TL{Text layer?}
    TL -- yes --> D[docling on SIE<br/>pages to tables]
    TL -- no --> DO[docling:ocr on SIE<br/>pages to tables]
    D & DO --> PP[Parse in Python<br/>headings, lines, totals,<br/>units, currency, period]
    PP --> CK{Period, units and currency<br/>match the loan system?}
    CK -- no --> EX1[exception]
    PP --> AM{Amount read?}
    AM -- no --> EX1
    AM -- yes --> KM{Label and heading<br/>confirmed last year?}
    KM -- yes --> PL[place in that row]
    KM -- no --> UL{Unlabelled subtotal<br/>under a group heading?}
    UL -- yes --> PL
    UL -- no --> RR[Qwen3-Reranker-4B on SIE<br/>label vs template rows<br/>detail rows only inside a section]
    RR --> TH{Above min_score and<br/>min_margin, row in the<br/>heading's group?}
    TH -- no --> EX2[escalated]
    TH -- yes --> TT{A total with lines<br/>in its own row?}
    TT -- no --> PL
    TT -- yes --> AB{Lines add up to it?}
    AB -- yes --> PL
    AB -- no --> EX3[place the lines,<br/>not the total]
    PL --> SUB{Each section adds up<br/>to its printed total?}
    SUB -- no --> EX4[subtotal exception]
    SUB -- yes --> F[Fill, derive subtotals,<br/>tie out, name a likely<br/>sign problem]
    EX4 --> F
    EX3 --> F
    F & EX1 & EX2 --> ST[Draft spread:<br/>checks_passed or review]
    ST --> AN[Analyst confirms period and units,<br/>clears exceptions, approves]
```

## How it's built

Everything you'd want to argue about lives in [`rules.yaml`](./rules.yaml): the three model
names, the reranker's instruction, the template (25 balance-sheet rows and 13 income-statement
rows, each with the name and description the reranker reads, and a group for the balance-sheet
detail rows), the 15 tie-outs, the 13 derived rows and their order, the statement titles, the
total and summary labels, the scale and currency markers, the section headings for each group,
and every threshold. [`loan_system.py`](./loan_system.py) stands in for the bank's loan system:
it loads `data/loan_borrowers.csv` and `data/loan_mappings.csv` into an in-memory SQLite
database and answers two lookups, a borrower by ID and a confirmed mapping by borrower,
statement, heading and label (exact, ignoring case, spaces and punctuation).
[`recipe.py`](./recipe.py) is about 1,200 lines and reads top to bottom in the same order as the
flow.

| Step | Code | SIE call | What it does |
| --- | --- | --- | --- |
| Look up the borrower | `LoanSystem.find_borrower` | none | Name, fiscal year end, currency and the period this spread is for. An unknown ID is an exception |
| Read the PDF | `has_text_layer`, `read_markdown` | `extract` (docling or docling:ocr) | One call per PDF. A reply without `data.markdown` is a failed call |
| Parse | `parse_statements`, `add_table`, `parse_number` | none | Headings, lines and totals from the first amount column. A column of bare "$" signs is dropped. An unreadable amount is kept as a line with no value |
| Period, units, currency | `check_statement`, `read_period`, `read_units` | none | Against the loan system. A column header naming another year than the date is a problem, not a guess |
| Place known labels | `place_lines`, `LoanSystem.confirmed_mapping` | none | Last year's row for the same label under the same heading; an unlabelled subtotal by its heading's group |
| Place new labels | `rank_line`, `judge` | `score` | One call per new label; detail rows only inside a section. Every row must come back scored once, in [0, 1], or it's a failed call. Accepted above `min_score` and `min_margin` and in the heading's group |
| Totals | `settle_totals` | none | Absorb lines only into an accepted total they add up to; a total across several rows is checked, not placed; a section that doesn't add up is a `subtotal` exception |
| Fill | `fill` | none | Lines in a detail row are summed; lines in a total row must agree or the row is escalated |
| Derive and tie out | `derive`, `tie_out`, `check_rows_in` | none | Missing rows worked out in order, stopped by an escalated part, directly or through a row it stopped; checks on reported totals, plus the balance check; a break of twice one line in the check names that line |
| SIE failures | `Calls.call`, `is_fatal`, `stop_message`, `stop_on_refusal` | none | A transient failure or a reply missing its key escalates the statement or line; a refusal, or every call failing, stops the run without outputs |
| Outputs | `write_outputs`, `csv_safe` | none | `spreads.csv`, `spread-<borrower>.csv`, `exceptions.csv`, `checks.csv`, `lines.json`. Text a spreadsheet would run as a formula gets a leading `'`; numbers are written as numbers, so `-96500` stays a number |

[`selfcheck.py`](./selfcheck.py) checks all of this offline with hand-written statements and a
stand-in for both models: amounts in every style it should read ("$ 35,934", "(14,264)",
"-96,500", a minus sign, a dash for nil) and the ones it shouldn't ("1,234*", "1234 567",
"1,2345"); headings, unlabelled subtotals and summary lines; periods in three date styles and
reversed columns; the loan-system lookups; the current "Bank loans" a confidently wrong reranker
puts in long-term debt; last year's mappings keyed on the heading; the sign hint, including a
coincidence it must not name; absorbed, unplaced, rejected and cross-row totals; two lines in
one total row; the SIE failure rule (a transient error, a refusal, every call failing, replies
missing their key, a reply with one row scored or NaN scores); an adversarial set (a summary
line forced into long-term debt, a dropped and a misread row, a label that tries to pick its own
row, two dates in a caption); and the committed data. [`make_data.py`](./make_data.py) writes the
four PDFs and the two loan-system seeds.

## Run it

**Run this recipe using [SIE Serverless](https://superlinked.com/serverless?utm_source=github&utm_medium=docs&utm_campaign=sie-cookbook&utm_content=finance-map-financial-statements-to-template), [SIE Cluster](https://superlinked.com/docs/deployment?utm_source=github&utm_medium=docs&utm_campaign=sie-cookbook&utm_content=finance-map-financial-statements-to-template) or [SIE Local](https://superlinked.com/docs/quickstart?utm_source=github&utm_medium=docs&utm_campaign=sie-cookbook&utm_content=finance-map-financial-statements-to-template).** The code runs on your machine either way. With SIE Serverless you pay per call and there are no GPUs to manage yourself: get an [SIE API key](https://console.superlinked.com/console/keys?utm_source=github&utm_medium=docs&utm_campaign=sie-cookbook&utm_content=finance-map-financial-statements-to-template) and read the [API docs](https://superlinked.com/docs/reference/api?utm_source=github&utm_medium=docs&utm_campaign=sie-cookbook&utm_content=finance-map-financial-statements-to-template). To run the models yourself, start from the [SIE repo](https://github.com/superlinked/sie).

You need [uv](https://docs.astral.sh/uv/) and, for SIE Serverless, an SIE API key
([create one](https://console.superlinked.com/console/keys?utm_source=github&utm_medium=docs&utm_campaign=sie-cookbook&utm_content=finance-map-financial-statements-to-template)).

```bash
cp .env.example .env   # then set SIE_API_KEY
uv run recipe.py
uv run selfcheck.py    # optional: offline checks, no SIE call
uv run --group data make_data.py   # optional: rebuild data/
```

`uv run` installs this recipe's few dependencies into its own `.venv` on the first run. Each run
writes `runs/<timestamp>/`. A run makes 98 SIE calls on this data: 3 to docling, 1 to
docling:ocr and 94 to the reranker (one per label the loan system didn't know). Our last warm
run on 2026-11-04 (UTC) took 30 s. The first calls can wait a few minutes while SIE Serverless starts
docling and the reranker.

## What came out

### The three examples

**1. A new current loan for a returning borrower (Harbour Lane, `BRW-20417.pdf`).**

- **Expected outcome:** `current_portion_long_term_debt` 120,000, long-term debt 480,000, spread
  `checks_passed`.
- **What came back:** `current_portion_long_term_debt` 120,000 (score 0.97, margin 4.41), long-term
  debt 480,000 from last year's mapping, spread `checks_passed` with 0 exceptions.
- **Verdict:** Pass.

**2. A subtotal that doesn't add up (Northfield, `BRW-18820.pdf`).**

- **Expected outcome:** spread `review`, with a `subtotal` exception and the current liabilities
  total off by 60.
- **What came back:** spread `review`, with the `subtotal` exception "add up to 7538; the statement
  prints 7598" and `current_liabilities` breaking by 60.
- **Verdict:** Pass. It also had two exceptions we didn't write in, from "Other assets" (below).

**3. A sign the wrong way round (Mesa Verde, `BRW-31186.pdf`).**

- **Expected outcome:** spread `review`, `other_income_expense` -8,700, net income off by 17,400.
- **What came back:** spread `review`, `other_income_expense` -8,700 and `net_income` breaking by
  17,400: "The difference is twice 'Other income' (-8700)".
- **Verdict:** Pass. It also shows the limit on a first-time borrower: six more exceptions came from
  labels the reranker couldn't place (below), and the analyst has to place them.

### Results

On the live run on 2026-11-04 (UTC), 7 of 7 cases came out as expected. Northfield and
Mesa Verde also had exceptions we didn't write in, from labels the reranker couldn't place; each
only adds to a spread already in `review` (see below).

**Where it works:** the numbers. Harbour Lane gives three of the cases: known labels placed from
last year with no model call, the new "Bank loans" kept out of long-term debt, and "Net assets" and
"Shareholders' funds" (both 712,600) giving one equity value, not two added together. Mesa Verde
gives two: the sign the wrong way round, and "Owner's draws" at -96,500 read as a negative amount.
Northfield's subtotal that doesn't add up and Kestrel's scanned statements for the wrong year (June
30, 2026, when the loan system expects December 31, 2025) make seven.

**Where it fails:** placing labels it hasn't seen. "Rent", "Vehicle and equipment expenses" and
"Owner's capital" on Mesa Verde and "Other assets" on Northfield all came back as exceptions for the
analyst, and "Owner's capital" scored the wrong row highest. The one to worry about is the one this
run didn't test: two lines of the same kind swapped on the same side add up just as well, so no
tie-out can see it, and that spread would still say `checks_passed`.

The input is four borrowers in [`data/`](./data):

- `statements/<borrower id>.pdf`: each borrower's income statement and balance sheet, one page
  each, as uploaded through the lending portal
- `loan_borrowers.csv`: the loan system's borrower records (name, fiscal year end, currency, the
  fiscal year end this spread is for)
- `loan_mappings.csv`: last year's confirmed label mappings (statement, heading, label, template
  row, period confirmed), 22 of them, all for Harbour Lane, the one returning borrower

> **All of it is synthetic.** [`make_data.py`](./make_data.py) writes three PDFs by hand with a
> Helvetica text layer and draws the fourth with Pillow as an image-only scan, and rebuilds all
> of them byte for byte. The PDFs carry no document info (no title, author, producer or dates).
> The borrowers, their figures and their loan records are invented, and every page says so in
> its footer.

Harbour Lane is `checks_passed`; the other three are `review`: Northfield with 4 exceptions, Mesa
Verde with 7 and Kestrel with 2. Of the 60 tie-outs, 38 tied, 6 broke and 16 were skipped (the total
wasn't reported, or it was derived and there was nothing to check). Treat that as a demo that the
pipeline works, not as a measure of accuracy: four borrowers tell you each path through the checks
runs, not how often the reranker picks the row your analyst would.

A few things worth noticing:

- **The returning borrower is the easy one.** Harbour Lane had 26 lines and needed 2 score
  calls. The two first-time borrowers needed one per line (33 each). That's the case for
  writing confirmed mappings back to the loan system: the first spread is the expensive one.
  Here's the new loan next to the old one, from `spread-BRW-20417.csv`:

  ```csv
  statement,item_id,item,value,units,period,status,borrower_lines,source,score,margin,review
  balance_sheet,current_portion_long_term_debt,Current portion of long-term debt,120000,GBP,2026-03-31,reported,Creditors: amounts falling due within one year > Bank loans,reranker,0.97,4.41,
  balance_sheet,long_term_debt,Long-term debt,480000,GBP,2026-03-31,reported,Creditors: amounts falling due after more than one year > Bank loans,last year,,,
  ```

- **Mesa Verde's sign shows up two rows away.** There's no pre-tax line in an LLC's statement,
  so the recipe derives pre-tax income (150,800) from operating income, interest and the -8,700,
  and the break lands on net income instead. The hint still finds the line, because it follows
  the derived row down to its parts. From `exceptions.csv`:

  ```text
  Check net_income breaks by 17400: net_income is 168200, computed 150800 from net_income = pretax_income - income_tax; no value, counted as 0: income_tax. The difference is twice 'Other income' (-8700): the statement may show that line with the opposite sign to the template's other_income_expense.
  ```

- **Mesa Verde's other six exceptions come from the reranker, not from anything we wrote in.**
  "Rent" (best row `sga`, 0.47) and "Vehicle and equipment expenses" (`other_operating_expenses`,
  0.47) fell just under `min_score` 0.5, so the operating expense and operating income tie-outs
  break by 109,300, the two lines' total. "Owner's capital" came back as `other_equity` 0.72
  against `share_capital`
  0.54, which is the wrong way round; the margin (0.79) was under 1.0, so it was escalated, and
  the equity tie-out breaks by its 150,000. Three lines, each a short label, each one a question
  the analyst answers once. "Salaries and wages" scraped in at 0.50, which is less reassuring
  than it sounds.
- **Northfield's "Other assets" is genuinely ambiguous.** It sits under "ASSETS" after the
  current assets have closed, and the reranker scored current (0.84) and non-current (0.82)
  almost the same. Escalated, so `total_assets` breaks by its 412. The subtotal we wrote in is the
  other two exceptions.
- **The scan read cleanly enough.** docling's OCR profile read all 26 of Kestrel's lines, with a
  couple of spacing slips in labels, and every tie-out held. The only thing wrong with Kestrel's
  spread is that it's for the wrong year.

### What you do next

The credit analyst takes each draft spread and confirms the period and units against the loan
system, then works down `exceptions.csv`: Kestrel's wrong year, Northfield's subtotal and "Other
assets", and Mesa Verde's sign and the three labels to place. They fix any mapping, approve the
spread, and pass it to the credit officer, who works out the ratios from the approved spread,
not the draft. A `checks_passed` spread like Harbour Lane's still gets a look for two lines of
the same kind swapped on the same side, which no tie-out can see.

## Potential improvements

- **Change the template.** Edit `template` in `rules.yaml`. Each row's `name` and `description`
  are what the reranker reads, and `group` ties a balance-sheet detail row to its section
  headings. Quote names that contain a comma.
- **Change the checks.** The tie-outs and derived rows are `checks` and `derived` in
  `rules.yaml`, written as signed sums of template ids. A check runs only on a total the
  borrower reported, except one marked `derived_ok`.
- **Tune the thresholds on your own borrowers.** `min_score`, `min_margin` (on the log-odds
  scale, because the reranker's scores crowd near 1.0) and `tie_tolerance` are starting points.
  Mesa Verde's "Rent" missed `min_score` by 0.03.
- **Teach it your borrowers' formats.** Statement titles are under `statements`; total labels,
  summary labels ("Net assets"), scale phrases and currency markers under `parsing`; section
  headings under `section_groups`. A statement the recipe can't find sends all its rows to
  review, and the usual cause is a title the file doesn't list.
- **Write the analyst's decisions back.** Once the analyst approves a spread, its label
  mappings belong in the loan system, so next year's statements need fewer score calls. The
  recipe reads `loan_mappings.csv`; it doesn't write it.
- **Prior years.** The recipe spreads the first amount column (the current year). For the
  comparatives, read the other columns in `add_table`; the period check already refuses a first
  column headed with another year.
- **More cases worth adding:** a borrower whose name on the statements differs from the
  loan system's, a statement in a currency the loan system doesn't expect, and two receivables
  lines swapped on the same side, which no tie-out can catch.
- **Pro-tip:** add your borrowers' section headings to `section_groups` before you trust the
  group check. A heading the list doesn't name ("Creditors due within 12 months", say) puts no
  limit on its lines, so the check that kept "Bank loans" out of long-term debt quietly does
  nothing for them.

## Try another model

Each model is one line under `models:` in `rules.yaml`. We swapped one model at a time, changed
nothing else, and ran the recipe using SIE Serverless on 2026-11-04 (UTC) on this data.

| `rules.yaml` key | Swap in | What happened on this data |
| --- | --- | --- |
| `document` (and `document_ocr`) | `docling:ocr` for every PDF | Every spread and every tie-out identical to the default. The three text PDFs read the same with OCR on |
| `rerank` | `Qwen/Qwen3-Reranker-0.6B` | All four borrowers went to `review`, with 34 exceptions instead of 13. Harbour Lane's new "Distribution costs" was ambiguous (0.98 against 0.94). It put Mesa Verde's "Current maturities of long-term debt" first in `long_term_debt` (0.985 against 0.962 for the current portion); the margin, not the group check, kept it out. It couldn't tell "Total current liabilities" from "Total liabilities" (0.997 each) on three borrowers. Northfield's subtotal and Kestrel's period were still raised |

See [MODELS.md](../../MODELS.md) for every model these recipes use.

## Run it using SIE Cluster or SIE Local

The same code runs on your machine and uses SIE Cluster or SIE Local
([SELF_HOSTING.md](../../SELF_HOSTING.md) says what SIE Local can serve). Set `SIE_BASE_URL` in `.env`; an API key
is then optional. The table gives the SIE v0.10.1 image each model runs on, from SIE's
[compatibility matrix](https://github.com/superlinked/sie/blob/main/docs/compatibility.md).

| `rules.yaml` key | Model | Image | Tested on |
| --- | --- | --- | --- |
| `document` | `docling` | `cuda12-default` | L4 |
| `document_ocr` | `docling:ocr` (docling's OCR profile) | `cuda12-default` | L4 |
| `rerank` | `Qwen/Qwen3-Reranker-4B` | `cuda12-default` | L4 |

One container serves them all:

```bash
docker run --gpus all -p 8080:8080 -v sie-hf-cache:/app/.cache/huggingface \
  ghcr.io/superlinked/sie-server:v0.10.1-cuda12-default
# then set SIE_BASE_URL=http://localhost:8080 in .env and run: uv run recipe.py
```

## Data and licenses

Everything in `data/` is synthetic and written by [`make_data.py`](./make_data.py) for this
recipe, under the repository's license. No real company, person or loan is in it. The scan is
drawn with Pillow's built-in font, so keep `pillow` below 13 (the `data` dependency group pins
it) if you want the rebuild to match byte for byte.

## Files

- `recipe.py`: the pipeline
- `loan_system.py`: the loan-system lookups over the CSV seeds
- `selfcheck.py`: offline checks with hand-written statements and stand-in replies, no SIE call
- `make_data.py`: writes the four statement PDFs and the loan-system seeds
- `rules.yaml`: template, groups, checks, derived rows, parsing, thresholds and models
- `data/`: the statement PDFs and the two loan-system CSVs
- `pyproject.toml`, `uv.lock`: this recipe's dependencies

See the SIE task page for [reranking](https://superlinked.com/rerank?utm_source=github&utm_medium=docs&utm_campaign=sie-cookbook&utm_content=finance-map-financial-statements-to-template).
