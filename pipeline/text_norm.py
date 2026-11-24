"""Spoken-word text normalization for TTS + mapping spoken timings back to display tokens."""
from __future__ import annotations

import re
from typing import Sequence

_ONES = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen",
)
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")
_ORD_ONES = {
    1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth",
    6: "sixth", 7: "seventh", 8: "eighth", 9: "ninth", 10: "tenth",
    11: "eleventh", 12: "twelfth", 13: "thirteenth", 14: "fourteenth", 15: "fifteenth",
    16: "sixteenth", 17: "seventeenth", 18: "eighteenth", 19: "nineteenth",
}
_ORD_TENS = {
    20: "twentieth", 30: "thirtieth", 40: "fortieth", 50: "fiftieth",
    60: "sixtieth", 70: "seventieth", 80: "eightieth", 90: "ninetieth",
}
_SCALES = ((10**9, "billion"), (10**6, "million"), (1000, "thousand"), (100, "hundred"))
_TAG_RE = re.compile(r"(\[[a-z0-9_-]+\])", re.IGNORECASE)
_YEAR_HINTS = {"in", "since", "until", "from", "by", "around", "before", "after", "circa", "year", "born", "dated"}
_KEEP_UNITS = {"s", "k", "m", "b", "x"}


def int_to_words(n: int) -> str:
    if n < 0:
        return "minus " + int_to_words(-n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        t, r = divmod(n, 10)
        return _TENS[t] if r == 0 else f"{_TENS[t]} {_ONES[r]}"
    for scale, name in _SCALES:
        if n >= scale:
            q, r = divmod(n, scale)
            head = f"{int_to_words(q)} {name}"
            return head if r == 0 else f"{head} {int_to_words(r)}"
    return str(n)


def int_to_ordinal_words(n: int) -> str:
    if n in _ORD_ONES:
        return _ORD_ONES[n]
    if n < 100:
        if n in _ORD_TENS:
            return _ORD_TENS[n]
        t, r = divmod(n, 10)
        return f"{_TENS[t]} {_ORD_ONES[r]}"
    for scale, name in _SCALES:
        if n >= scale:
            q, r = divmod(n, scale)
            if r == 0:
                return f"{int_to_words(q)} {name}th"
            return f"{int_to_words(q)} {name} {int_to_ordinal_words(r)}"
    return int_to_words(n) + "th"


def _year_to_words(n: int) -> str:
    if 2000 <= n <= 2009:
        return int_to_words(n)
    hi, lo = divmod(n, 100)
    if lo == 0:
        return f"{int_to_words(hi)} hundred"
    if lo < 10:
        return f"{int_to_words(hi)} oh {int_to_words(lo)}"
    return f"{int_to_words(hi)} {int_to_words(lo)}"


def _digits_to_words(s: str) -> str:
    return " ".join(_ONES[int(ch)] for ch in s)


def _format_time(hh: int, mm: int, ampm: str = "") -> str:
    h_word = "twelve" if hh == 0 else int_to_words(hh)
    if mm == 0:
        core = f"{h_word} o'clock" if not ampm else h_word
    elif mm < 10:
        core = f"{h_word} oh {int_to_words(mm)}"
    else:
        core = f"{h_word} {int_to_words(mm)}"
    if ampm:
        tag = "A M" if ampm.lower().startswith("a") else "P M"
        return f"{core} {tag}"
    return core


def _normalize_core(core: str, prev_lower: str = "") -> str:
    if not core:
        return core

    # Standalone symbols
    sym_map = {"&": "and", "%": "percent", "=": "equals", "@": "at", "+": "plus"}
    if core in sym_map:
        return sym_map[core]

    # Clock time: 2:13, 02:05, 2:13pm, 2:13 a.m.
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::\d{2})?(am|pm|a\.m\.|p\.m\.)?", core, re.IGNORECASE)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return _format_time(hh, mm, m.group(3) or "")

    # Stand-alone AM/PM immediately following a clock time
    if core.upper() in {"AM", "PM", "A.M.", "P.M."} and re.search(r"\d{1,2}:\d{2}", prev_lower):
        return "A M" if core.upper().startswith("A") else "P M"

    # Currency: $50, $3.50, $1,200, $2.5M
    m = re.fullmatch(r"\$(\d+(?:,\d{3})*)(?:\.(\d+))?([KMBkmb])?", core)
    if m:
        whole = int(m.group(1).replace(",", ""))
        frac = m.group(2)
        suf = (m.group(3) or "").upper()
        if suf:
            scale_word = {"K": "thousand", "M": "million", "B": "billion"}[suf]
            num = f"{int_to_words(whole)} point {_digits_to_words(frac)}" if frac else int_to_words(whole)
            return f"{num} {scale_word} dollars"
        if frac and len(frac) == 2:
            cents = int(frac)
            d_lbl = "dollar" if whole == 1 else "dollars"
            if cents == 0:
                return f"{int_to_words(whole)} {d_lbl}"
            c_lbl = "cent" if cents == 1 else "cents"
            return f"{int_to_words(whole)} {d_lbl} and {int_to_words(cents)} {c_lbl}"
        if frac:
            return f"{int_to_words(whole)} point {_digits_to_words(frac)} dollars"
        return f"{int_to_words(whole)} {'dollar' if whole == 1 else 'dollars'}"

    # Percentage: 50%, 3.5%
    m = re.fullmatch(r"(\d+(?:,\d{3})*)(?:\.(\d+))?%", core)
    if m:
        whole = int(m.group(1).replace(",", ""))
        frac = m.group(2)
        num = f"{int_to_words(whole)} point {_digits_to_words(frac)}" if frac else int_to_words(whole)
        return f"{num} percent"

    # Ordinals: 1st, 2nd, 13th
    m = re.fullmatch(r"(\d+)(st|nd|rd|th)", core, re.IGNORECASE)
    if m:
        n = int(m.group(1))
        if 0 < n <= 999_999_999:
            return int_to_ordinal_words(n)

    # Number + hash prefix: #12
    m = re.fullmatch(r"#(\d+)", core)
    if m:
        return f"number {int_to_words(int(m.group(1)))}"

    # Decades/plural numbers: 1990s, 90s, 1980's
    m = re.fullmatch(r"(\d+)'?s", core, re.IGNORECASE)
    if m:
        raw_d = m.group(1)
        n = int(raw_d)
        if 1900 <= n <= 1999 and n % 10 == 0:
            decade = (n % 100)
            base = _TENS[decade // 10]
            plural = base[:-1] + "ies" if base.endswith("y") else base + "s"
            return f"nineteen {plural}"
        if 10 <= n <= 90 and n % 10 == 0:
            base = _TENS[n // 10]
            return base[:-1] + "ies" if base.endswith("y") else base + "s"

    # 24/7 and simple fractions
    if core == "24/7":
        return "twenty four seven"
    m = re.fullmatch(r"(\d+)/(\d+)", core)
    if m:
        num, den = int(m.group(1)), int(m.group(2))
        if den == 2:
            return f"{int_to_words(num)} {'half' if num == 1 else 'halves'}"
        if den == 4:
            return f"{int_to_words(num)} {'quarter' if num == 1 else 'quarters'}"
        if 1 <= den <= 100:
            return f"{int_to_words(num)} {int_to_ordinal_words(den)}{'' if num == 1 else 's'}"

    # Apartment / room / unit codes like 8A, 8B, 8C, 12B
    m = re.fullmatch(r"(\d+)([A-Za-z])", core)
    if m and (m.group(2).isupper() or m.group(2).lower() not in _KEEP_UNITS):
        return f"{int_to_words(int(m.group(1)))} {m.group(2).upper()}"

    # Number ranges: 3-10 or 12-14
    m = re.fullmatch(r"(\d+)[-\u2013](\d+)", core)
    if m:
        return f"{int_to_words(int(m.group(1)))} to {int_to_words(int(m.group(2)))}"

    # Decimals: 3.14
    m = re.fullmatch(r"(\d+(?:,\d{3})*)\.(\d+)", core)
    if m:
        return f"{int_to_words(int(m.group(1).replace(',', '')))} point {_digits_to_words(m.group(2))}"

    # Hyphenated tokens containing digits: 12-year-old, 13th-floor
    if "-" in core and re.search(r"\d", core):
        subs = [_normalize_core(part) for part in core.split("-") if part]
        return " ".join(subs)

    # Slash-joined words like and/or
    if "/" in core and re.fullmatch(r"[A-Za-z0-9]+(?:/[A-Za-z0-9]+)+", core):
        subs = [_normalize_core(part) for part in core.split("/") if part]
        return " ".join(subs)

    # Signed integer: +5 or -5
    m = re.fullmatch(r"([+-])(\d+)", core)
    if m:
        pref = "plus" if m.group(1) == "+" else "minus"
        return f"{pref} {int_to_words(int(m.group(2)))}"

    # Plain integer (with optional commas)
    m = re.fullmatch(r"\d+(?:,\d{3})*", core)
    if m:
        raw_d = core.replace(",", "")
        if len(raw_d) > 1 and raw_d.startswith("0"):
            return _digits_to_words(raw_d)
        n = int(raw_d)
        if "," not in core and 1900 <= n <= 1999:
            return _year_to_words(n)
        if "," not in core and 2010 <= n <= 2099 and prev_lower in _YEAR_HINTS:
            return _year_to_words(n)
        if n <= 999_999_999_999:
            return int_to_words(n)

    # Inline symbol substitutions inside non-digit tokens (e.g. door—slowly)
    out = core.replace("\u2014", ", ").replace("\u2013", ", ").replace("&", " and ")
    return re.sub(r"\s{2,}", " ", out).strip()


def normalize_token_for_tts(token: str, prev_token: str = "") -> str:
    """Convert a single display token (preserving leading/trailing punctuation) into spoken form."""
    m = re.match(r"^([\"'(\[{<\u201c\u2018]*)(.*?)([\"')\]}>.,!?;:\u201d\u2019\u2026]*)$", token)
    if not m:
        return token
    lead, core, trail = m.group(1), m.group(2), m.group(3)
    if not core:
        return token
    prev_core = re.sub(r"^[^A-Za-z0-9:]+|[^A-Za-z0-9]+$", "", prev_token).lower()
    spoken_core = _normalize_core(core, prev_lower=prev_core)
    return f"{lead}{spoken_core}{trail}"


def normalize_text_for_tts(text: str) -> str:
    """Convert numbers, times, ordinals, and symbols in `text` to spoken words while preserving [tags]."""
    raw = str(text or "")
    if not raw.strip():
        return raw
    pieces = _TAG_RE.split(raw)
    out_pieces: list[str] = []
    prev_tok = ""
    for piece in pieces:
        if not piece:
            continue
        if _TAG_RE.fullmatch(piece):
            out_pieces.append(piece)
            continue
        # Preserve whitespace structure while converting each whitespace token
        parts = re.split(r"(\s+)", piece)
        converted: list[str] = []
        for p in parts:
            if not p or p.isspace():
                converted.append(p)
            else:
                nt = normalize_token_for_tts(p, prev_tok)
                converted.append(nt)
                prev_tok = p
        out_pieces.append("".join(converted))
    return re.sub(r" {2,}", " ", "".join(out_pieces)).strip()


def prepare_alignment_expansion(
    display_words: Sequence[tuple[str, str]],
) -> tuple[list[tuple[str, str]], list[int]]:
    """
    Expand display `(word, line_id)` tokens into spoken `(word, line_id)` tokens for TTS alignment.
    Returns `(spoken_words, counts_per_display_word)` so each display token maps to >=1 spoken token
    whenever possible (or 0 for punctuation-only display tokens).
    """
    spoken_words: list[tuple[str, str]] = []
    counts: list[int] = []
    prev_tok = ""
    for w, lid in display_words:
        conv = normalize_token_for_tts(w, prev_tok)
        prev_tok = w
        parts = [p for p in conv.split() if p]
        if not parts:
            parts = [w]
        spoken_words.extend((p, lid) for p in parts)
        counts.append(len(parts))
    return spoken_words, counts


def map_spoken_timings_to_display(
    display_words: Sequence[tuple[str, str]],
    spoken_timings: Sequence[object],
    counts_per_display: Sequence[int],
) -> list[object]:
    """
    Collapse aligned spoken-word WordTiming entries back onto original display words
    so on-screen subtitles preserve original text ("2:13", "12", "$50", etc.) with accurate spans.
    """
    from tts_engine import WordTiming

    out: list[WordTiming] = []
    cursor = 0
    for (orig_w, lid), cnt in zip(display_words, counts_per_display):
        take = max(1, int(cnt))
        slice_wt = list(spoken_timings[cursor : cursor + take])
        cursor += take
        if not slice_wt:
            prev_end = out[-1].end if out else 0.0
            out.append(WordTiming(word=orig_w, start=round(prev_end, 3), end=round(prev_end + 0.04, 3), line_id=lid))
            continue
        st = float(slice_wt[0].start)
        en = max(st + 0.04, float(slice_wt[-1].end))
        out.append(WordTiming(word=orig_w, start=round(st, 3), end=round(en, 3), line_id=lid))
    return out
