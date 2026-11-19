"""
ASS subtitle generator for StoryForge.
- 1080x1920 vertical frame
- Bold comic font (Lilita One / Bangers) with thick black outline + soft drop shadow
- 1-3 words per beat in the lower-middle safe zone (never overlapping top cards or bottom UI)
- Active spoken word highlighted in bright yellow (#FFE14D), others crisp white
"""

from __future__ import annotations

import re
from typing import Sequence

from tts_engine import WordTiming


BREAK_PUNCT_RE = re.compile(r"[.!?,;:—\-]+$")


def _ass_time(sec: float) -> str:
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    cs = int(round((sec - int(sec)) * 100))
    if cs >= 100:
        cs = 99
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _esc_ass(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def group_word_chunks(
    word_timings: Sequence[WordTiming],
    max_words: int = 3,
    max_chars: int = 17,
) -> list[list[WordTiming]]:
    """Group WordTiming entries into readable 1-3 word beats."""
    chunks: list[list[WordTiming]] = []
    cur: list[WordTiming] = []
    cur_len = 0

    for wt in word_timings:
        w = wt.word.strip()
        if not w:
            continue
        would_exceed = cur and (len(cur) >= max_words or cur_len + 1 + len(w) > max_chars)
        if would_exceed:
            chunks.append(cur)
            cur = []
            cur_len = 0

        cur.append(wt)
        cur_len += len(w) + (1 if len(cur) > 1 else 0)

        # Natural punctuation break if we already have >= 1 word
        if BREAK_PUNCT_RE.search(w) and len(cur) >= 1:
            chunks.append(cur)
            cur = []
            cur_len = 0

    if cur:
        chunks.append(cur)
    return chunks


def generate_ass_subtitles(
    word_timings: Sequence[WordTiming],
    out_ass_path: str,
    font_name: str = "Lilita One",
    font_size: int = 88,
    margin_v: int = 470,
    highlight_bgr: str = "&H004DE1FF&",  # #FFE14D bright warm yellow in ASS BGR
    base_bgr: str = "&H00FFFFFF&",       # white
) -> str:
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_name},{font_size},{base_bgr},&H000000FF&,&H00000000&,&HA0000000&,1,0,0,0,100,100,1,0,1,8,4,2,60,60,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    chunks = group_word_chunks(word_timings, max_words=3, max_chars=17)
    events: list[str] = []

    for c_idx, chunk in enumerate(chunks):
        next_chunk_start = chunks[c_idx + 1][0].start if c_idx + 1 < len(chunks) else None
        for active_idx, active_w in enumerate(chunk):
            st = active_w.start
            if active_idx + 1 < len(chunk):
                en = chunk[active_idx + 1].start
            elif next_chunk_start is not None and next_chunk_start - active_w.end < 0.14:
                en = next_chunk_start
            else:
                en = active_w.end
            en = max(st + 0.04, en)

            parts: list[str] = []
            for j, w in enumerate(chunk):
                txt = _esc_ass(w.word.upper())
                if j == active_idx:
                    parts.append(f"{{\\c{highlight_bgr}\\fscx108\\fscy108}}{txt}{{\\c{base_bgr}\\fscx100\\fscy100}}")
                else:
                    parts.append(txt)
            line_text = " ".join(parts)
            events.append(f"Dialogue: 0,{_ass_time(st)},{_ass_time(en)},Default,,0,0,0,,{line_text}")

    with open(out_ass_path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n".join(events))
        f.write("\n")
    return out_ass_path
