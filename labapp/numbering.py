"""Registration-number, case-number and NIC generation, validation and formatting."""

import re
import secrets

REG_BODY_DIGITS = 8
REG_LENGTH = REG_BODY_DIGITS + 1
NIC_LENGTH = 13

_ASCII_DIGITS = re.compile(r"[0-9]+")
_NON_DIGITS = re.compile(r"[^0-9]")
_CASE_NO = re.compile(r"([0-9]{9})-([0-9]{1,4})")


# ---------- Luhn ----------

def luhn_check_digit(body: str) -> str:
    """Return the Luhn check digit for a string of ASCII digits."""
    if not _ASCII_DIGITS.fullmatch(body):
        raise ValueError(f"Not a digit string: {body!r}")
    total = 0
    # Walking right-to-left, the digit next to the (future) check digit is doubled.
    for i, ch in enumerate(reversed(body)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return str((10 - total % 10) % 10)


def is_valid_luhn(number: str) -> bool:
    return (
        len(number) >= 2
        and _ASCII_DIGITS.fullmatch(number) is not None
        and luhn_check_digit(number[:-1]) == number[-1]
    )


# ---------- Registration number (Patient #) ----------

def generate_reg_no() -> str:
    """8 cryptographically random digits + Luhn check digit, e.g. '482719365'."""
    body = f"{secrets.randbelow(10 ** REG_BODY_DIGITS):0{REG_BODY_DIGITS}d}"
    return body + luhn_check_digit(body)


def normalize_reg_no(raw: str) -> str:
    """Accept typed or scanned input ('4827-1936-5', ' 482719365\\n'); return 9 bare digits.

    Raises ValueError if the length or check digit is wrong (catches typos
    before they can match a different, real patient).
    """
    digits = _NON_DIGITS.sub("", raw or "")
    if len(digits) != REG_LENGTH or not is_valid_luhn(digits):
        raise ValueError(f"Invalid registration number: {raw!r}")
    return digits


def format_reg_no(reg_no: str) -> str:
    """'482719365' -> '4827-1936-5' (display only; never stored with dashes)."""
    return f"{reg_no[:4]}-{reg_no[4:8]}-{reg_no[8:]}"


# ---------- Case number ----------

def format_case_no(reg_no: str, visit_no: int) -> str:
    """('482719365', 2) -> '482719365-2'  (Patient # + visit number)."""
    return f"{reg_no}-{visit_no}"


def parse_case_no(text: str) -> tuple[str, int]:
    """'482719365-2' -> ('482719365', 2). Raises ValueError if malformed or check digit fails."""
    m = _CASE_NO.fullmatch((text or "").strip())
    if not m or not is_valid_luhn(m.group(1)) or int(m.group(2)) < 1:
        raise ValueError(f"Invalid case number: {text!r}")
    return m.group(1), int(m.group(2))


# ---------- NIC ----------

def normalize_nic(raw: str | None) -> str | None:
    """'35202-1234567-1' -> '3520212345671'; blank -> None. Raises ValueError if not 13 digits."""
    if raw is None or not raw.strip():
        return None
    digits = _NON_DIGITS.sub("", raw)
    if len(digits) != NIC_LENGTH:
        raise ValueError(f"NIC must have {NIC_LENGTH} digits: {raw!r}")
    return digits


def format_nic(nic: str) -> str:
    """'3520212345671' -> '35202-1234567-1'."""
    return f"{nic[:5]}-{nic[5:12]}-{nic[12:]}"
