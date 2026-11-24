"""Verify full VoiceStudio audio synthesis, validation, and subtitle alignment on a multi-part story JSON."""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audio_checks import verify_word_timing_coverage
from subtitles import generate_ass_subtitles
from tts_engine import clean_spoken_text
from voicestudio import VoiceStudioEngine


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", required=True)
    parser.add_argument("--voice", required=True)
    parser.add_argument("--speedup", type=float, default=1.25)
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args()

    prod = json.load(open(args.json, "r", encoding="utf-8"))
    engine = VoiceStudioEngine()
    os.makedirs(args.out_dir, exist_ok=True)
    summary = []

    for part in prod.get("parts", []):
        pnum = int(part.get("part_number", 1))
        part_dir = os.path.join(args.out_dir, f"part_{pnum}")
        os.makedirs(part_dir, exist_ok=True)
        lines = part.get("lines", [])
        print(f"=== Synthesizing Part {pnum} ({len(lines)} lines, voice={args.voice}) ===", flush=True)
        res = engine.synthesize_lines(
            lines,
            args.voice,
            {"voicestudio_speedup": args.speedup},
            part_dir,
            align=True,
        )
        expected_script = [
            (w, str(ln["id"]))
            for ln in lines
            for w in clean_spoken_text(str(ln.get("text", "")))[0].split()
        ]
        cov = verify_word_timing_coverage(
            res.word_timings,
            expected_script,
            res.duration_sec,
            res.intended_pauses,
            max_gap_sec=1.5,
        )
        ass_path = os.path.join(part_dir, f"part_{pnum}.ass")
        generate_ass_subtitles(res.word_timings, ass_path)
        info = {
            "part_number": pnum,
            "lines": len(lines),
            "words": cov["word_count"],
            "chunks": res.chunk_count,
            "retries": res.retry_count,
            "duration_sec": res.duration_sec,
            "max_unintended_gap_sec": cov["max_unintended_gap_sec"],
            "ass_path": ass_path,
        }
        summary.append(info)
        print(f"=== Part {pnum} PASSED: {json.dumps(info)} ===", flush=True)

    with open(os.path.join(args.out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print("ALL PARTS PASSED:", json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
