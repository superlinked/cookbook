"""Write the synthetic call recordings and the saved claims they are checked against.

data/claims.csv is the claims-system seed: one saved claim per recorded call, as the intake
agent typed it (claim number, the policy the agent found, insured, callback phone, date of
loss, location, cause, injuries). Blank cells are fields the agent left blank.

data/*.wav are five first-notice-of-loss calls as 16 kHz mono WAV files. They are scripts read
by macOS text-to-speech, not recordings of real people. Every caller, policy number, claim
number, phone number, address and the insurer (Harborline Mutual) is fictional, and the phone
numbers are in the 555-01xx range set aside for fiction. SIE has no text-to-speech model, so
this runs locally on a Mac with the built-in `say` and `afconvert` commands:

    say -v <voice> -o turn.aiff "<line>"                      # one file per turn
    afconvert -f WAVE -d LEI16@16000 -c 1 turn.aiff turn.wav  # 16 kHz mono, 16-bit
    # then the turns are joined with a short pause, using the standard-library `wave` module

The agent speaks with one voice and each caller with another. The WAVs are committed; re-running
this on another macOS version can give slightly different audio, so the committed files are the
inputs of record. claims.csv is rebuilt byte for byte on any machine.

    uv run make_data.py
"""

from __future__ import annotations

import csv
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"

SAMPLE_RATE = 16_000
PAUSE_S = 0.45  # silence between turns
SPEECH_RATE = 185  # words per minute for `say -r`
AGENT_VOICE = "Samantha"


@dataclass(frozen=True)
class Call:
    filename: str
    caller_voice: str
    # (speaker, line): "A" is the claims agent, "C" the caller.
    turns: tuple[tuple[str, str], ...]


GREETING = (
    "A",
    "Thank you for calling Harborline Mutual claims. My name is Dana. Who am I speaking with?",
)
CLOSING = ("A", "Thank you. An adjuster will call you back within two business days.")

CALLS = (
    # Planted: complete. Every required field is given and the saved claim agrees.
    Call(
        "fnol-1021-quintero.wav",
        "Daniel",
        (
            GREETING,
            ("C", "Hi, this is Marisol Quintero."),
            ("A", "Thanks, Marisol. What is your policy number?"),
            ("C", "It's H M 4 4 7 1 2 0 9 3."),
            ("A", "And when did the damage happen?"),
            ("C", "On Tuesday night, October 20th, around nine o'clock."),
            ("A", "Where is the damaged property?"),
            ("C", "At my house, 214 Alder Creek Road, Millbrook."),
            ("A", "What happened?"),
            (
                "C",
                "Hail broke two skylights and the wind tore shingles off the roof. "
                "Rain came into the kitchen.",
            ),
            ("A", "Was anyone hurt?"),
            ("C", "No, nobody was hurt."),
            CLOSING,
        ),
    ),
    # Planted: the caller has no policy number to hand (the agent found the policy by name), and
    # says "87 Maple Street, unit 2" where the agent saved "78 Maple Street".
    Call(
        "fnol-1021-achterberg.wav",
        "Karen",
        (
            GREETING,
            ("C", "Desmond Achterberg."),
            ("A", "Thanks, Desmond. Do you have your policy number?"),
            ("C", "Not with me. The papers are down in the basement and I can't get to them."),
            ("A", "That's fine. When did this happen?"),
            ("C", "Wednesday afternoon, October 21st."),
            ("A", "And the address of the property?"),
            ("C", "87 Maple Street, unit 2, Oakdale."),
            ("A", "What happened?"),
            (
                "C",
                "Flooding. The storm drain backed up and a foot of water came into the basement.",
            ),
            ("A", "Was anyone injured?"),
            ("C", "No one was injured."),
            CLOSING,
        ),
    ),
    # Planted: an injury the agent saved as "No", and the agent never asks when it happened.
    Call(
        "fnol-1021-vellanki.wav",
        "Moira",
        (
            GREETING,
            ("C", "This is Priya Vellanki."),
            ("A", "Thanks, Priya. What is your policy number?"),
            ("C", "P R 5 0 3 8 1 1 6 4."),
            ("A", "What happened?"),
            ("C", "A big oak tree came down on our garage in the storm and crushed the car."),
            ("A", "Where is the property?"),
            ("C", "1630 Quarry Hill Drive, Brightwater."),
            ("A", "Is everyone okay?"),
            (
                "C",
                "My husband cut his arm on the broken glass when he went out to look. "
                "We took him to urgent care and he needed stitches.",
            ),
            ("A", "I'm sorry to hear that. I have everything I need."),
            CLOSING,
        ),
    ),
    # Near miss: "hurt but didn't need an ambulance". The negation is about the ambulance, not
    # the injury; the agent saved the injury field as "No".
    Call(
        "fnol-1021-okafor.wav",
        "Ralph",
        (
            GREETING,
            ("C", "This is Tomas Okafor."),
            ("A", "Thanks, Tomas. What is your policy number?"),
            ("C", "H M 6 2 0 5 7 7 3 1."),
            ("A", "When did the damage happen?"),
            ("C", "Wednesday morning, October 21st, around seven."),
            ("A", "Where is the property?"),
            ("C", "455 Birch Avenue, Oakdale."),
            ("A", "What happened?"),
            ("C", "The wind took a big branch off the maple and it went through the porch roof."),
            ("A", "Was anyone hurt?"),
            ("C", "My son was out on the porch. He was hurt but he didn't need an ambulance."),
            CLOSING,
        ),
    ),
    # Near miss: every field given and saved, but rain is still coming in and the family has
    # moved out. Complete, and it still can't wait two business days.
    Call(
        "fnol-1021-delacroix.wav",
        "Tessa",
        (
            GREETING,
            ("C", "Hello, this is Renata Delacroix."),
            ("A", "Thanks, Renata. What is your policy number?"),
            ("C", "P R 7 1 9 4 0 2 5 8."),
            ("A", "And when did the damage happen?"),
            ("C", "Tuesday night, October 20th."),
            ("A", "Where is the damaged property?"),
            ("C", "62 Harbor View Road, Millbrook."),
            ("A", "What happened?"),
            ("C", "The wind ripped part of the roof off."),
            (
                "C",
                "Rain is still coming through the bedroom ceiling, "
                "and we are staying with family until it is fixed.",
            ),
            ("A", "Was anyone hurt?"),
            ("C", "No, nobody was hurt."),
            CLOSING,
        ),
    ),
)

CLAIM_COLUMNS = (
    *("recording", "claim_number", "policy_number", "insured", "callback_phone"),
    *("date_of_loss", "loss_location", "cause_of_loss", "injuries"),
)
# The saved claims, as the intake agent typed them. Blank is a field left blank.
CLAIMS = (
    # Complete and agrees with the call.
    (
        *("fnol-1021-quintero.wav", "CLM-26-104417", "HM44712093", "Marisol Quintero"),
        *("(415) 555-0141", "2026-10-20", "214 Alder Creek Road, Millbrook", "Hail and wind", "No"),
    ),
    # The agent found the policy by name; the house number is transposed and the unit dropped.
    (
        *("fnol-1021-achterberg.wav", "CLM-26-104418", "HM30958821", "Desmond Achterberg"),
        *(
            "(415) 555-0187",
            "2026-10-21",
            "78 Maple Street, Oakdale",
            "Flood, storm drain backed up",
        ),
        "No",
    ),
    # No date of loss, and the injury field left at "No".
    (
        *("fnol-1021-vellanki.wav", "CLM-26-104419", "PR50381164", "Priya Vellanki"),
        *("(415) 555-0123", "", "1630 Quarry Hill Drive, Brightwater", "Fallen tree", "No"),
    ),
    # The injury field left at "No".
    (
        *("fnol-1021-okafor.wav", "CLM-26-104420", "HM62057731", "Tomas Okafor"),
        *("(415) 555-0164", "2026-10-21", "455 Birch Avenue, Oakdale", "Wind, fallen branch", "No"),
    ),
    # Complete and agrees with the call.
    (
        *("fnol-1021-delacroix.wav", "CLM-26-104421", "PR71940258", "Renata Delacroix"),
        *("(415) 555-0172", "2026-10-20", "62 Harbor View Road, Millbrook", "Wind", "No"),
    ),
)


def _render_turn(text: str, voice: str, workdir: Path, index: int) -> bytes:
    """Speak one turn with `say`, convert it to 16 kHz mono 16-bit PCM, return the frames."""
    aiff = workdir / f"turn-{index:02d}.aiff"
    wav = workdir / f"turn-{index:02d}.wav"
    subprocess.run(["say", "-v", voice, "-r", str(SPEECH_RATE), "-o", str(aiff), text], check=True)
    subprocess.run(
        ["afconvert", "-f", "WAVE", "-d", f"LEI16@{SAMPLE_RATE}", "-c", "1", str(aiff), str(wav)],
        check=True,
    )
    with wave.open(str(wav), "rb") as handle:
        if (handle.getframerate(), handle.getnchannels(), handle.getsampwidth()) != (
            SAMPLE_RATE,
            1,
            2,
        ):
            raise RuntimeError(f"afconvert did not produce 16 kHz mono 16-bit audio: {wav}")
        return handle.readframes(handle.getnframes())


def render(call: Call, path: Path) -> float:
    """Render one call to `path`; returns its length in seconds."""
    pause = b"\x00\x00" * int(SAMPLE_RATE * PAUSE_S)
    frames: list[bytes] = []
    with tempfile.TemporaryDirectory(prefix="fnol-") as tmp:
        for index, (speaker, text) in enumerate(call.turns):
            voice = AGENT_VOICE if speaker == "A" else call.caller_voice
            frames += [_render_turn(text, voice, Path(tmp), index), pause]
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(b"".join(frames))
    return sum(len(chunk) for chunk in frames) / 2 / SAMPLE_RATE


def write_claims(path: Path) -> None:
    """The claims-system seed, one saved claim per recording."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(CLAIM_COLUMNS)
        writer.writerows(CLAIMS)


def main() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    write_claims(DATA_DIR / "claims.csv")
    print(f"wrote {(DATA_DIR / 'claims.csv').relative_to(ROOT)} ({len(CLAIMS)} saved claims)")
    for call in CALLS:
        path = DATA_DIR / call.filename
        seconds = render(call, path)
        print(f"wrote {path.relative_to(ROOT)} ({seconds:.0f} s, {path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
