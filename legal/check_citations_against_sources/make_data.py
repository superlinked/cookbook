"""Write data/draft_brief.docx, the draft motion the recipe cite-checks.

The draft is a fictional Westmark trial-court motion (Quellmark Instruments v. Tessaly) as the
associate left it the night before filing. Its text lives below as plain paragraphs; this script
writes them into a minimal Word file with the standard library only, the way Word saves them:
curly quotes, and a non-breaking space after a section sign. The zip entries are stored with a
fixed timestamp, so the file is the same byte for byte on every run.

The research folder (data/research_folder/ and its index), the citator seed and the docket seed
are hand-written files, committed as they are; this script doesn't touch them.

    uv run make_data.py
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent
DRAFT_PATH = ROOT / "data" / "draft_brief.docx"
# Every zip entry gets this timestamp, so a rebuild matches the committed file byte for byte.
FIXED_TIME = (2026, 11, 3, 0, 0, 0)

# Word's typography, written as code points so this file stays ASCII.
OPEN, CLOSE, APOS = chr(0x201C), chr(0x201D), chr(0x2019)
SECTION = chr(0xA7) + chr(0xA0)  # a section sign followed by a non-breaking space

# (style, text). Styles: Caption, Title, Heading1, Heading2, Body.
DRAFT: list[tuple[str, str]] = [
    ("Caption", "IN THE SUPERIOR COURT OF THE STATE OF WESTMARK"),
    ("Caption", "IN AND FOR HARLOW COUNTY"),
    ("Caption", "QUELLMARK INSTRUMENTS, INC., Plaintiff, v. DANA TESSALY, Defendant."),
    ("Caption", "No. 26-2-04418-1"),
    ("Title", f"PLAINTIFF{APOS}S MOTION FOR PRELIMINARY INJUNCTION AND MEMORANDUM IN SUPPORT"),
    ("Caption", "DRAFT for cite check, 3 November 2026"),
    ("Heading1", "I. INTRODUCTION"),
    (
        "Body",
        f"Dana Tessaly spent nine years building Quellmark{APOS}s customer relationships in "
        "Harlow and Brandt counties. Eleven days after she resigned, she was calling on those "
        f"same customers for Ostrander Metrology, Quellmark{APOS}s closest competitor. Quellmark "
        "asks the Court to enforce the twelve-month covenant she signed and to enjoin her from "
        f"using Quellmark{APOS}s customer list.",
    ),
    ("Heading1", "II. ARGUMENT"),
    ("Heading2", "A. The covenant is reasonable and protects a legitimate business interest."),
    (
        "Body",
        "Westmark enforces a covenant not to compete that is reasonable. "
        f"{OPEN}A covenant not to compete is enforceable only to the extent it is reasonable in "
        "duration and geographic scope and necessary to protect a legitimate business interest "
        f"such as customer goodwill or confidential information.{CLOSE} Ardley v. Brightfield "
        "Supply Co., 214 Westm. 2d 377, 381 (2019).",
    ),
    (
        "Body",
        "The Legislature has since drawn a line on duration: only a covenant that runs longer "
        "than eighteen months after employment ends is presumed unreasonable. Westm. Rev. Code "
        f"{SECTION}23.40.020(2) (2021). Tessaly{APOS}s covenant runs for twelve months.",
    ),
    (
        "Body",
        f"Quellmark{APOS}s customer list is exactly the kind of information the law protects. "
        "Information is a trade secret when it derives independent economic value from not "
        "being generally known and is the subject of reasonable efforts to keep it secret. "
        f"Westm. Rev. Code {SECTION}23.10.010(3). Quellmark keeps the list on an access-controlled "
        "system and lets only its sales staff see it.",
    ),
    (
        "Body",
        f"Restrictions of this kind are enforced where they are {OPEN}reasonably necessary to "
        "protect a legitimate business interest such as customer goodwill or confidential "
        f"information.{CLOSE} Ardley, 214 Westm. 2d at 381. Quellmark{APOS}s interest is its "
        "goodwill with the forty-two accounts Tessaly served.",
    ),
    (
        "Body",
        "Twelve months is, if anything, modest. Westmark courts recognize that an employer"
        f"{APOS}s long-standing client relationships justify a restriction of up to three years. "
        "Pell v. Drayton Insurance Agency, 141 Westm. App. 90, 97 (2008).",
    ),
    (
        "Heading2",
        "B. If the Court finds the covenant too broad, it should enforce it as rewritten.",
    ),
    (
        "Body",
        "Should the Court find the two-county territory broader than necessary, it need not "
        "deny relief. Westmark courts may rewrite an overbroad covenant, substituting a "
        "reasonable restraint for the one the parties chose. Ardley, 214 Westm. 2d at 383. "
        "Quellmark would accept a covenant limited to the accounts Tessaly personally served.",
    ),
    (
        "Body",
        "Ardena, by contrast, enforces such covenants as written or not at all. Cf. Kessler "
        f"Tools, Inc. v. Marrow, 88 Ard. App. 412, 415 (2020). Westmark{APOS}s approach gives the "
        "Court more room, not less.",
    ),
    ("Heading2", f"C. Tessaly{APOS}s counterclaims do not defeat the motion."),
    (
        "Body",
        "Tessaly asks the Court to recognize a new claim for bad-faith enforcement of a "
        "noncompetition covenant. Whether to create a new form of liability is a question for "
        "the legislature, not the courts. Ludvik v. Ashgrove, 302 Ard. 588, 594 (2012).",
    ),
    (
        "Body",
        "Her negligent misrepresentation counterclaim fares no better. The economic loss rule "
        "bars a negligence claim for purely economic loss; the remedy lies in contract. Carrow "
        "Builders, Inc. v. Ellery Holdings, 135 Westm. 2d 260, 266 (1998).",
    ),
    (
        "Body",
        "Quellmark also handled the deposit on the relocation apartment it sublet to Tessaly as "
        "the law requires. A landlord has forty-five days after the tenancy ends to give the "
        "tenant a statement of the basis for retaining any of the deposit. Westm. Rev. Code "
        f"{SECTION}59.18.280 (2009). Quellmark sent its statement on day thirty-eight.",
    ),
    (
        "Body",
        f"Quellmark paid Tessaly{APOS}s accrued vacation with her final paycheck, as the statute "
        f"requires: {OPEN}Accrued and unused vacation pay is wages and shall be paid to the "
        "employee at the end of the established pay period in which employment ends, whether "
        f"the employee quits or is discharged.{CLOSE} Westm. Rev. Code {SECTION}49.48.210(2).",
    ),
    (
        "Body",
        "By cashing that check without protest, Tessaly gave up any claim to a penalty. An "
        "employee who accepts payment of disputed wages without reservation waives the "
        "statutory penalty for late payment. Tolliver v. Ransom Manufacturing Co., 203 Westm. "
        "App. 118, 124 (2021).",
    ),
    ("Heading1", "III. CONCLUSION"),
    ("Body", "Quellmark asks the Court to grant the preliminary injunction."),
    ("Caption", "ASHBY & ROURKE LLP, Attorneys for Plaintiff Quellmark Instruments, Inc."),
]

PKG = "http://schemas.openxmlformats.org/package/2006"
DOC = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
WML = "application/vnd.openxmlformats-officedocument.wordprocessingml"
XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'

CONTENT_TYPES = (
    XML_HEAD
    + f'<Types xmlns="{PKG}/content-types">\n'
    + '<Default Extension="rels" '
    + 'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
    + '<Default Extension="xml" ContentType="application/xml"/>\n'
    + f'<Override PartName="/word/document.xml" ContentType="{WML}.document.main+xml"/>\n'
    + f'<Override PartName="/word/styles.xml" ContentType="{WML}.styles+xml"/>\n'
    + "</Types>\n"
)

PACKAGE_RELS = (
    XML_HEAD
    + f'<Relationships xmlns="{PKG}/relationships">\n'
    + f'<Relationship Id="rId1" Type="{DOC}/officeDocument" Target="word/document.xml"/>\n'
    + "</Relationships>\n"
)

DOCUMENT_RELS = (
    XML_HEAD
    + f'<Relationships xmlns="{PKG}/relationships">\n'
    + f'<Relationship Id="rId1" Type="{DOC}/styles" Target="styles.xml"/>\n'
    + "</Relationships>\n"
)

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

STYLES = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="{W}">
<w:style w:type="paragraph" w:default="1" w:styleId="Body"><w:name w:val="Body"/>
<w:pPr><w:spacing w:after="200" w:line="360" w:lineRule="auto"/></w:pPr>
<w:rPr><w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman"/><w:sz w:val="24"/></w:rPr>
</w:style>
<w:style w:type="paragraph" w:styleId="Caption"><w:name w:val="Caption"/><w:basedOn w:val="Body"/>
<w:pPr><w:jc w:val="center"/><w:spacing w:after="0"/></w:pPr></w:style>
<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Body"/>
<w:pPr><w:jc w:val="center"/><w:spacing w:before="240"/></w:pPr><w:rPr><w:b/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/>
<w:basedOn w:val="Body"/>
<w:pPr><w:jc w:val="center"/></w:pPr><w:rPr><w:b/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/>
<w:basedOn w:val="Body"/>
<w:rPr><w:b/></w:rPr></w:style>
</w:styles>
"""


def paragraph_xml(style: str, text: str) -> str:
    return (
        f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr>'
        f'<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'
    )


def document_xml(paragraphs: list[tuple[str, str]]) -> str:
    body = "\n".join(paragraph_xml(style, text) for style, text in paragraphs)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<w:document xmlns:w="{W}"><w:body>\n{body}\n'
        '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/>'
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>'
        "</w:body></w:document>\n"
    )


def write_docx(path: Path, paragraphs: list[tuple[str, str]]) -> None:
    """A minimal .docx: content types, package and document relationships, styles, body."""
    parts = [
        ("[Content_Types].xml", CONTENT_TYPES),
        ("_rels/.rels", PACKAGE_RELS),
        ("word/_rels/document.xml.rels", DOCUMENT_RELS),
        ("word/styles.xml", STYLES),
        ("word/document.xml", document_xml(paragraphs)),
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, content in parts:
            info = zipfile.ZipInfo(name, date_time=FIXED_TIME)
            info.external_attr = 0o644 << 16
            archive.writestr(info, content.encode("utf-8"))


def main() -> None:
    write_docx(DRAFT_PATH, DRAFT)
    print(f"wrote {DRAFT_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
