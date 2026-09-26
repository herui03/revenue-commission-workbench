"""Money, rate and rounding primitives.

Every amount in the workbench is an ``int`` of *minor units* (cents).  Rates are
``int`` basis points (500 = 5.00%).  There is deliberately no float anywhere in
the money path: parsing is done on the decimal *string*, and every rounding step
is exact integer arithmetic.

Rounding policy (the only one used for money):
    ROUND HALF UP on non-negative magnitudes, applied once per calculation line.
    Negative lines (clawbacks) are computed as positive magnitudes, then negated,
    so a clawback is the exact mirror of an earning.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction

# Currencies supported by this demo and their minor-unit exponent.
# No FX table exists on purpose: amounts in different currencies are never combined.
CURRENCIES: dict[str, int] = {"SGD": 2, "USD": 2}

BPS_DENOMINATOR = 10_000          # 10,000 bps = 100%
MAX_AMOUNT_MINOR = 100_000_000_000  # 1,000,000,000.00 — sanity cap, keeps SQLite int64 far away

_UNSIGNED_AMOUNT = re.compile(r"^(?P<units>\d{1,12})(?:\.(?P<frac>\d{1,2}))?$")
_SIGNED_AMOUNT = re.compile(r"^(?P<sign>-)?(?P<units>\d{1,12})(?:\.(?P<frac>\d{1,2}))?$")
_PERCENT = re.compile(r"^(?P<units>\d{1,3})(?:\.(?P<frac>\d{1,2}))?$")


class MoneyFormatError(ValueError):
    """Raised when a text amount does not match the strict documented format."""


def parse_amount(text: str, *, allow_negative: bool = False) -> int:
    """Parse a plain decimal string ("1234.5", "0.01") into minor units.

    Rejected on purpose (ambiguous or float-like): thousands separators, more than
    two decimals, exponents, NaN/inf, leading '+', currency symbols, blanks.
    """
    if text is None:
        raise MoneyFormatError("amount is blank")
    s = text.strip()
    pattern = _SIGNED_AMOUNT if allow_negative else _UNSIGNED_AMOUNT
    m = pattern.match(s)
    if not m:
        raise MoneyFormatError(
            f"amount {text!r} must look like 1234.56 (digits, optional 1-2 decimals"
            + (", optional leading '-'" if allow_negative else ", no sign") + ", no separators)"
        )
    units = int(m.group("units"))
    frac = (m.group("frac") or "").ljust(2, "0")
    value = units * 100 + int(frac)
    if allow_negative and m.group("sign"):
        value = -value
    if abs(value) > MAX_AMOUNT_MINOR:
        raise MoneyFormatError(f"amount {text!r} exceeds the sanity cap")
    return value


def parse_percent_to_bps(text: str) -> int:
    """'60' -> 6000, '5.00' -> 500, '33.33' -> 3333. Range 0..100%."""
    s = (text or "").strip()
    m = _PERCENT.match(s)
    if not m:
        raise MoneyFormatError(f"percentage {text!r} must look like 60 or 60.00 (max 2 decimals)")
    bps = int(m.group("units")) * 100 + int((m.group("frac") or "").ljust(2, "0"))
    if bps > BPS_DENOMINATOR:
        raise MoneyFormatError(f"percentage {text!r} is above 100")
    return bps


def half_up_div(numerator: int, denominator: int) -> int:
    """Exact round-half-up of numerator/denominator for numerator >= 0, denominator > 0."""
    if numerator < 0 or denominator <= 0:
        raise ValueError("half_up_div expects numerator >= 0 and denominator > 0")
    return (2 * numerator + denominator) // (2 * denominator)


def earning_for_portions(base_portion: int, base_bps: int, accel_portion: int, accel_bps: int) -> tuple[int, Fraction]:
    """Commission for one line: exact value and the single rounded value (cents)."""
    numerator = base_portion * base_bps + accel_portion * accel_bps
    exact = Fraction(numerator, BPS_DENOMINATOR)
    return half_up_div(numerator, BPS_DENOMINATOR), exact


def cumulative_reversal(original_earning: int, refunded_cumulative: int, original_amount: int) -> int:
    """Cumulative clawback (positive magnitude) after refunds totalling ``refunded_cumulative``.

    Path independent: the sum of all reversals after refunds totalling R is always
    half_up(E * R / x), so a full refund (R == x) reverses exactly E with no drift.
    """
    if not 0 <= refunded_cumulative <= original_amount:
        raise ValueError("cumulative refund outside 0..original amount")
    if original_earning < 0:
        raise ValueError("original earning must be non-negative")
    return half_up_div(original_earning * refunded_cumulative, original_amount)


def allocate_largest_remainder(total: int, weights: list[tuple[str, int]]) -> dict[str, int]:
    """Split ``total`` cents across keys proportionally to integer weights.

    Floors every exact share, then hands the leftover cents one each to the largest
    fractional remainders; ties go to the smallest key (rep id) — deterministic and
    independent of input order.  Result always sums exactly to ``total``.
    """
    if total < 0:
        raise ValueError("allocate expects a non-negative total")
    keys = [k for k, _ in weights]
    if len(set(keys)) != len(keys):
        # A repeated key would silently overwrite a floor and break conservation.
        raise ValueError(f"duplicate allocation keys: {sorted({k for k in keys if keys.count(k) > 1})}")
    weight_sum = sum(w for _, w in weights)
    if weight_sum <= 0:
        raise ValueError("weights must sum to a positive number")
    if any(w < 0 for _, w in weights):
        raise ValueError("weights must be non-negative")
    floors: dict[str, int] = {}
    remainders: list[tuple[int, str]] = []
    for key, w in weights:
        q, r = divmod(total * w, weight_sum)
        floors[key] = q
        remainders.append((r, key))
    leftover = total - sum(floors.values())
    # largest remainder first, then key ascending
    remainders.sort(key=lambda item: (-item[0], item[1]))
    for _, key in remainders[:leftover]:
        floors[key] += 1
    return floors


def fraction_to_str(value: Fraction, places: int = 4) -> str:
    """Render an exact Fraction of cents as a decimal string; mark non-terminating values with '~'."""
    sign = "-" if value < 0 else ""
    v = abs(value)
    scaled = v * (10 ** places)
    whole = scaled.numerator // scaled.denominator
    exact = scaled.denominator == 1 or (scaled.numerator % scaled.denominator == 0)
    int_part, frac_part = divmod(whole, 10 ** places)
    s = f"{int_part}.{str(frac_part).rjust(places, '0')}".rstrip("0").rstrip(".")
    return f"{sign}{s}" if exact else f"~{sign}{s}"


@dataclass(frozen=True)
class Money:
    minor: int
    currency: str


def format_minor(minor: int, currency: str | None = None, *, ascii_minus: bool = False, signed: bool = False) -> str:
    """12345 -> '123.45'; with currency -> 'SGD 123.45'; negatives use a real minus sign in HTML."""
    exponent = CURRENCIES.get(currency or "", 2)
    neg = minor < 0
    units, frac = divmod(abs(minor), 10 ** exponent)
    body = f"{units:,}.{str(frac).rjust(exponent, '0')}" if exponent else f"{units:,}"
    minus = "-" if ascii_minus else "−"
    sign = minus if neg else ("+" if signed and minor > 0 else "")
    text = f"{sign}{body}"
    return f"{currency} {text}" if currency else text


def format_plain(minor: int) -> str:
    """Machine-friendly decimal string for CSV: '-80.00', '1234.56' (no separators)."""
    neg = minor < 0
    units, frac = divmod(abs(minor), 100)
    return f"{'-' if neg else ''}{units}.{frac:02d}"


def format_bps(bps: int) -> str:
    units, frac = divmod(bps, 100)
    return f"{units}.{frac:02d}%"
