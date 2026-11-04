"""Write the six synthetic NDAs in data/ as .docx files, and the CLM intake seed for them.

Every company and person below is fictional. Our side is Ostermark Instruments, Inc. One NDA
is on our paper; five are on the counterparty's paper, with their own headings and wording.
The .docx files are written with the standard library only (a minimal Office Open XML zip with
fixed timestamps), so a rerun rebuilds them byte for byte. data/clm_intake.csv is the CLM's
intake record for each request (see clm.py).

    uv run make_data.py

Planted cases:
    nda-brackenridge-analytics.docx  ours, mutual     compliant
    nda-lowfield-ridge-capital.docx  theirs, one-way  five-year confidentiality period
    nda-quillfeather-logistics.docx  theirs, mutual   non-solicit and residuals clauses
    nda-orlaith-marine.docx          theirs, one-way  Singapore law, and no term clause at all
    nda-corrin-fabrication.docx      theirs, mutual   two-year term, confidentiality survives
                                                      five years after it ends
    nda-velloway-robotics.docx       theirs, mutual   a non-solicit that never says "solicit"
"""

from __future__ import annotations

import csv
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
FIXED_TIME = (2026, 10, 1, 0, 0, 0)  # zip entry timestamps, so reruns are byte-identical

US = "Ostermark Instruments, Inc."

Section = tuple[str, list[str]]


def brackenridge() -> tuple[str, list[str], list[Section]]:
    """Our paper, mutual, every position met."""
    them = "Brackenridge Analytics LLC"
    return (
        "MUTUAL NON-DISCLOSURE AGREEMENT",
        [
            f"This Mutual Non-Disclosure Agreement is made on 3 August 2026 between {US}, "
            f'a Delaware corporation ("Ostermark"), and {them}, a Colorado limited liability '
            'company ("Brackenridge"). Each is a "party" and may disclose or receive '
            "Confidential Information.",
        ],
        [
            (
                "1. Purpose",
                [
                    "The parties wish to evaluate a possible joint project on sensor calibration "
                    'data (the "Purpose") and will share Confidential Information only for it.'
                ],
            ),
            (
                "2. Confidential Information",
                [
                    '"Confidential Information" means non-public business, technical and '
                    "financial information a party discloses to the other in connection with "
                    "the Purpose, whether in writing, orally or by demonstration. It does not "
                    "include information that is or becomes public through no fault of the "
                    "recipient, was already known to the recipient, or is independently "
                    "developed without use of the other party's information."
                ],
            ),
            (
                "3. Obligations",
                [
                    "The recipient shall use the other party's Confidential Information only "
                    "for the Purpose and protect it with at least reasonable care."
                ],
            ),
            (
                "4. Permitted Disclosures",
                [
                    "The recipient may disclose Confidential Information to its employees, "
                    "officers and professional advisers who need to know it for the Purpose and "
                    "are bound by confidentiality duties no less protective than this "
                    "Agreement. The recipient may also disclose Confidential Information to the "
                    "extent required by law, regulation or court order, after giving the other "
                    "party prompt written notice where lawful."
                ],
            ),
            (
                "5. Term",
                [
                    "This Agreement and the obligations in it last for two (2) years from the "
                    "date of this Agreement."
                ],
            ),
            (
                "6. Return of Information",
                [
                    "On request, the recipient shall return or destroy the other party's "
                    "Confidential Information and confirm that it has done so."
                ],
            ),
            (
                "7. Governing Law",
                [
                    "This Agreement is governed by the laws of the State of Delaware, without "
                    "regard to its conflict of laws rules."
                ],
            ),
            (
                "8. General",
                [
                    "This Agreement is the entire agreement on its subject and may be changed "
                    "only in a writing signed by both parties. No licence is granted under any "
                    "patent, copyright or other right."
                ],
            ),
        ],
    )


def lowfield_ridge() -> tuple[str, list[str], list[Section]]:
    """Their paper, one-way, five-year term. The definitions run long, so they are split."""
    them = "Lowfield Ridge Capital Partners LP"
    return (
        "CONFIDENTIALITY AGREEMENT",
        [
            f'{them} (the "Discloser") and {US} (the "Recipient") enter into this '
            "Confidentiality Agreement as of 14 September 2026 in connection with the "
            "Recipient's evaluation of a possible acquisition of one of the Discloser's "
            'portfolio companies (the "Transaction").'
        ],
        [
            (
                "Section 1. Definitions",
                [
                    '"Evaluation Material" means all information, data, reports, analyses, '
                    "compilations, forecasts, studies, notes, memoranda and other documents and "
                    "materials, in whatever form maintained, whether documentary, electronic or "
                    "oral, concerning the Discloser, its portfolio companies, its limited "
                    "partners, its investment strategies, its fee arrangements, its pipeline of "
                    "prospective investments or the Transaction, that are furnished to the "
                    "Recipient or its Representatives by or on behalf of the Discloser before, "
                    "on or after the date of this Agreement, together with all notes, analyses, "
                    "compilations, interpretations, forecasts, studies and other documents "
                    "prepared by the Recipient or its Representatives that contain, reflect or "
                    "are based upon, in whole or in part, any such information.",
                    '"Representatives" means, as to any person, its affiliates and its and their '
                    "respective directors, officers, managers, employees, agents, lenders, "
                    "financing sources, consultants, accountants, attorneys and financial "
                    "advisors. A person is an affiliate of another person if it directly or "
                    "indirectly controls, is controlled by, or is under common control with that "
                    "person, where control means the power to direct the management and "
                    "policies of a person through ownership of voting securities, by contract "
                    "or otherwise.",
                    "Evaluation Material does not include information that (a) is or becomes "
                    "generally available to the public other than as a result of a disclosure "
                    "by the Recipient or its Representatives in breach of this Agreement, (b) "
                    "was within the Recipient's possession on a non-confidential basis before "
                    "it was furnished by the Discloser, (c) becomes available to the Recipient "
                    "on a non-confidential basis from a source other than the Discloser that is "
                    "not known by the Recipient to be bound by a duty of confidentiality, or "
                    "(d) is independently developed by the Recipient without use of the "
                    "Evaluation Material.",
                ],
            ),
            (
                "Section 2. Use and Non-Disclosure",
                [
                    "The Recipient shall use the Evaluation Material solely for the purpose of "
                    "evaluating the Transaction and shall keep it strictly confidential."
                ],
            ),
            (
                "Section 3. Disclosure to Representatives; Legal Process",
                [
                    "The Recipient may disclose Evaluation Material to those of its "
                    "Representatives who need to know it for the purpose of evaluating the "
                    "Transaction and who are informed of its confidential nature. If the "
                    "Recipient is required by applicable law or legal process to disclose any "
                    "Evaluation Material, it may do so after notifying the Discloser, to the "
                    "extent legally permitted, so that the Discloser may seek a protective order."
                ],
            ),
            (
                "Section 4. Duration",
                [
                    "The Recipient's obligations under this Agreement shall remain in effect for "
                    "a period of five (5) years from the date of this Agreement."
                ],
            ),
            (
                "Section 5. Governing Law and Venue",
                [
                    "This Agreement shall be governed by and construed in accordance with the "
                    "laws of the State of New York. The parties submit to the courts located in "
                    "New York County."
                ],
            ),
            (
                "Section 6. Miscellaneous",
                [
                    "No failure or delay in exercising any right under this Agreement operates "
                    "as a waiver of it. This Agreement may be signed in counterparts."
                ],
            ),
        ],
    )


def quillfeather() -> tuple[str, list[str], list[Section]]:
    """Their paper, mutual, three-year term (at the limit), non-solicit and residuals."""
    them = "Quillfeather Logistics Ltd"
    return (
        "NON-DISCLOSURE AGREEMENT (MUTUAL)",
        [
            f"THIS AGREEMENT is dated 22 September 2026 and is made between {them}, a company "
            f'registered in England and Wales ("Quillfeather"), and {US} ("Ostermark").'
        ],
        [
            (
                "1. Confidential Information",
                [
                    "Each party may disclose to the other information about its warehouse "
                    "automation plans. Confidential Information means all such information "
                    "marked or described as confidential, or which a reasonable person would "
                    "understand to be confidential."
                ],
            ),
            (
                "2. Undertakings",
                [
                    "Each party shall keep the other party's Confidential Information secret "
                    "and use it only to discuss a possible supply arrangement."
                ],
            ),
            (
                "3. Permitted Disclosure",
                [
                    "A party may disclose Confidential Information to its employees, officers "
                    "and professional advisers who need to know it for that purpose, provided "
                    "they keep it confidential, and as required by law, by any court of "
                    "competent jurisdiction or by any regulatory body."
                ],
            ),
            (
                "4. Non-Solicitation",
                [
                    "During the term of this Agreement and for twelve (12) months afterwards, "
                    "neither party shall directly or indirectly solicit for employment or hire "
                    "any employee of the other party with whom it had contact in connection "
                    "with the discussions."
                ],
            ),
            (
                "5. Residual Knowledge",
                [
                    "Nothing in this Agreement restricts either party from using Residuals for "
                    'any purpose. "Residuals" means information retained in the unaided memory '
                    "of a party's personnel who had access to the other party's Confidential "
                    "Information."
                ],
            ),
            (
                "6. Term",
                [
                    "This Agreement shall continue for three (3) years from the date above, "
                    "after which the obligations of confidentiality shall cease."
                ],
            ),
            (
                "7. Law and Jurisdiction",
                [
                    "This Agreement and any dispute arising from it are governed by the law of "
                    "England and Wales, and the courts of England have exclusive jurisdiction."
                ],
            ),
        ],
    )


def orlaith() -> tuple[str, list[str], list[Section]]:
    """Their paper, one-way, Singapore law, and no term or duration anywhere."""
    them = "Orlaith Marine Systems Pte. Ltd."
    return (
        "UNILATERAL CONFIDENTIALITY UNDERTAKING",
        [
            f'{US} (the "Receiving Party") gives this undertaking on 6 October 2026 to {them} '
            '(the "Disclosing Party") in connection with a demonstration of hull-monitoring '
            "sensors."
        ],
        [
            (
                "A. Confidential Information",
                [
                    "Confidential Information means all technical drawings, test results and "
                    "pricing that the Disclosing Party provides to the Receiving Party for the "
                    "demonstration."
                ],
            ),
            (
                "B. Undertaking",
                [
                    "The Receiving Party shall hold the Confidential Information in confidence, "
                    "shall not copy it except as needed for the demonstration, and shall not "
                    "reverse engineer any sample provided."
                ],
            ),
            (
                "C. Permitted Disclosures",
                [
                    "The Receiving Party may disclose Confidential Information to its employees "
                    "and professional advisers who need to know it, and to the extent required "
                    "by law or by order of a court or governmental authority."
                ],
            ),
            (
                "D. Return of Materials",
                [
                    "When the demonstration ends, the Receiving Party shall return all "
                    "drawings and samples to the Disclosing Party."
                ],
            ),
            (
                "E. Governing Law",
                [
                    "This undertaking shall be governed by and construed in accordance with the "
                    "laws of the Republic of Singapore."
                ],
            ),
        ],
    )


def corrin() -> tuple[str, list[str], list[Section]]:
    """Their paper, mutual. A two-year agreement term, but confidentiality survives for five
    years after the agreement ends, in a section of its own near the end."""
    them = "Corrin Fabrication Works, Inc."
    return (
        "MUTUAL CONFIDENTIALITY AGREEMENT",
        [
            f"This Mutual Confidentiality Agreement is entered into on 9 October 2026 by {them}, "
            f'an Ohio corporation ("Corrin"), and {US} ("Ostermark"), so that the parties can '
            "discuss the contract manufacture of sensor housings."
        ],
        [
            (
                "1. Confidential Information",
                [
                    '"Confidential Information" means any drawing, specification, price or '
                    "process information that one party gives the other for the discussions, "
                    "except information that is already public or that the receiving party "
                    "already lawfully had."
                ],
            ),
            (
                "2. Use",
                [
                    "Each party shall use the other party's Confidential Information only for "
                    "the discussions and shall protect it as carefully as its own."
                ],
            ),
            (
                "3. Permitted Disclosures",
                [
                    "Each party may disclose Confidential Information to its employees, "
                    "contractors and professional advisers who need to know it and who are "
                    "bound to keep it confidential. Each party may also disclose Confidential "
                    "Information where it is required by law or by a court, after telling the "
                    "other party where it is lawful to do so."
                ],
            ),
            (
                "4. Term",
                [
                    "This Agreement takes effect on the date above and continues for two (2) "
                    "years, unless either party ends it earlier by thirty days' written notice."
                ],
            ),
            (
                "5. Governing Law",
                ["This Agreement is governed by the laws of the State of California."],
            ),
            (
                "6. Notices",
                [
                    "Notices under this Agreement must be in writing and sent to the address "
                    "each party gives the other for that purpose."
                ],
            ),
            (
                "7. Survival",
                [
                    "The obligations of confidentiality in this Agreement survive for five (5) "
                    "years after this Agreement expires or is terminated."
                ],
            ),
        ],
    )


def velloway() -> tuple[str, list[str], list[Section]]:
    """Their paper, mutual. A non-solicit in disguise: it never says solicit, hire or recruit."""
    them = "Velloway Robotics Ltd"
    return (
        "CONFIDENTIALITY AND NON-DISCLOSURE AGREEMENT",
        [
            f"THIS AGREEMENT is dated 16 October 2026 and is made between {them}, a company "
            f'registered in England and Wales ("Velloway"), and {US} ("Ostermark"), who intend '
            "to discuss a joint development of gripper force sensors."
        ],
        [
            (
                "1. Confidentiality",
                [
                    "Each party shall keep the other party's Confidential Information secret "
                    "and use it only for the joint development discussions. Confidential "
                    "Information means any information disclosed by one party to the other "
                    "about its products, designs or plans."
                ],
            ),
            (
                "2. Permitted Disclosure",
                [
                    "Either party may disclose Confidential Information to its employees and "
                    "professional advisers who need to know it, and where disclosure is "
                    "required by law or by any court of competent jurisdiction."
                ],
            ),
            (
                "3. Personnel",
                [
                    "While the parties are in discussions and for one (1) year afterwards, "
                    "neither party shall engage any of the other party's personnel, whether "
                    "as an employee, contractor or consultant, without the other party's "
                    "written agreement."
                ],
            ),
            (
                "4. Duration",
                [
                    "This Agreement lasts for two (2) years from the date above, and the "
                    "obligations in it end when it does."
                ],
            ),
            (
                "5. Governing Law",
                ["This Agreement is governed by the law of England and Wales."],
            ),
        ],
    )


NDAS = {
    "nda-brackenridge-analytics.docx": brackenridge,
    "nda-lowfield-ridge-capital.docx": lowfield_ridge,
    "nda-quillfeather-logistics.docx": quillfeather,
    "nda-orlaith-marine.docx": orlaith,
    "nda-corrin-fabrication.docx": corrin,
    "nda-velloway-robotics.docx": velloway,
}

# The CLM intake record for each NDA request, as clm.py reads it. Requesters are invented.
INTAKE_COLUMNS = (
    "request",
    "received",
    "counterparty",
    "our_entity",
    "requester",
    "business_purpose",
    "deal_type",
    "paper",
    "our_role",
    "governing_law_preference",
    "draft_file",
)
INTAKE = [
    (
        "NDA-2026-0412",
        "2026-08-03",
        "Brackenridge Analytics LLC",
        US,
        "Mara Lindqvist (Engineering)",
        "Evaluate a joint project on sensor calibration data",
        "joint development",
        "ours",
        "both",
        "Delaware",
        "nda-brackenridge-analytics.docx",
    ),
    (
        "NDA-2026-0447",
        "2026-09-14",
        "Lowfield Ridge Capital Partners LP",
        US,
        "Devon Achterberg (Corporate Development)",
        "Evaluate buying one of Lowfield Ridge's portfolio companies",
        "acquisition",
        "theirs",
        "receive",
        "Delaware",
        "nda-lowfield-ridge-capital.docx",
    ),
    (
        "NDA-2026-0453",
        "2026-09-22",
        "Quillfeather Logistics Ltd",
        US,
        "Ines Halloran (Procurement)",
        "Discuss a warehouse automation supply arrangement",
        "supply",
        "theirs",
        "both",
        "England and Wales",
        "nda-quillfeather-logistics.docx",
    ),
    (
        "NDA-2026-0468",
        "2026-10-06",
        "Orlaith Marine Systems Pte. Ltd.",
        US,
        "Tomasz Ferreira (Product)",
        "Attend a demonstration of hull-monitoring sensors",
        "product demo",
        "theirs",
        "receive",
        "",
        "nda-orlaith-marine.docx",
    ),
    (
        "NDA-2026-0471",
        "2026-10-09",
        "Corrin Fabrication Works, Inc.",
        US,
        "Lena Okafor (Operations)",
        "Discuss contract manufacture of sensor housings",
        "supply",
        "theirs",
        "both",
        "Delaware",
        "nda-corrin-fabrication.docx",
    ),
    (
        "NDA-2026-0479",
        "2026-10-16",
        "Velloway Robotics Ltd",
        US,
        "Ravi Castellanos (Engineering)",
        "Discuss joint development of gripper force sensors",
        "joint development",
        "theirs",
        "both",
        "England and Wales",
        "nda-velloway-robotics.docx",
    ),
]

# --- Minimal Office Open XML ------------------------------------------------------------------

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
    'officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-'
    'officedocument.wordprocessingml.styles+xml"/>'
    "</Types>"
)
ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships/officeDocument" Target="word/document.xml"/>'
    "</Relationships>"
)
DOCUMENT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships/styles" Target="styles.xml"/>'
    "</Relationships>"
)


def _style(style_id: str, name: str, size_half_points: int, bold: bool) -> str:
    bold_xml = "<w:b/>" if bold else ""
    return (
        f'<w:style w:type="paragraph" w:styleId="{style_id}"><w:name w:val="{name}"/>'
        '<w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>'
        f'<w:rPr>{bold_xml}<w:sz w:val="{size_half_points}"/></w:rPr></w:style>'
    )


STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<w:styles xmlns:w="{W_NS}">'
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/>'
    '<w:rPr><w:sz w:val="22"/></w:rPr></w:style>'
    + _style("Title", "Title", 32, True)
    + _style("Heading1", "heading 1", 24, True)
    + "</w:styles>"
)


def _paragraph(text: str, style: str | None = None) -> str:
    props = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f'<w:p>{props}<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'


def document_xml(title: str, preamble: list[str], sections: list[Section]) -> str:
    body = [_paragraph(title, "Title")]
    body += [_paragraph(text) for text in preamble]
    for heading, paragraphs in sections:
        body.append(_paragraph(heading, "Heading1"))
        body += [_paragraph(text) for text in paragraphs]
    signature = f"Signed for and on behalf of {US}: ______________________"
    body.append(_paragraph(signature))
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{W_NS}"><w:body>{"".join(body)}'
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr></w:body></w:document>'
    )


def write_docx(path: Path, title: str, preamble: list[str], sections: list[Section]) -> None:
    parts = {
        "[Content_Types].xml": CONTENT_TYPES,
        "_rels/.rels": ROOT_RELS,
        "word/_rels/document.xml.rels": DOCUMENT_RELS,
        "word/document.xml": document_xml(title, preamble, sections),
        "word/styles.xml": STYLES,
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            info = zipfile.ZipInfo(name, date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, content.encode("utf-8"))


def write_intake(path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(INTAKE_COLUMNS)
        writer.writerows(INTAKE)


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for name, build in NDAS.items():
        title, preamble, sections = build()
        write_docx(DATA_DIR / name, title, preamble, sections)
        print(DATA_DIR / name)
    write_intake(DATA_DIR / "clm_intake.csv")
    print(DATA_DIR / "clm_intake.csv")


if __name__ == "__main__":
    main()
