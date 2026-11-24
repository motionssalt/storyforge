"""Diagnostic script to inspect raw OmniVoice outputs on failing StoryForge lines."""
from __future__ import annotations

import os
import sys
import time
import numpy as np
import torch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audio_checks import clean_omnivoice_wave, analyze_chunk_energy, validate_tts_chunk
from voicestudio import MODEL, REVISION, load_builtin_presets


def inspect_tensor_stats(name: str, arr: np.ndarray, text: str, factor: float = 1.25) -> None:
    arr = np.asarray(arr).reshape(-1)
    n = int(arr.size)
    dur = n / 24000.0
    nan_cnt = int(np.isnan(arr).sum()) if n else 0
    inf_cnt = int(np.isinf(arr).sum()) if n else 0
    mn = float(np.nanmin(arr)) if n else 0.0
    mx = float(np.nanmax(arr)) if n else 0.0
    mean = float(np.nanmean(arr)) if n else 0.0
    raw_ac = float(np.sqrt(np.mean((arr - mean) ** 2))) if n else 0.0
    raw_ac_db = 20.0 * np.log10(max(raw_ac, 1e-9))

    cleaned, raw_dc = clean_omnivoice_wave(arr.astype(np.float32), 24000)
    ok, reason, stats = validate_tts_chunk(
        cleaned, 24000, text, is_voicestudio=True, speedup_factor=factor
    )
    print(
        f"[{name}] len={n} ({dur:.2f}s) min={mn:+.4f} max={mx:+.4f} mean={mean:+.5f} "
        f"nan={nan_cnt} inf={inf_cnt} raw_ac_db={raw_ac_db:.1f} "
        f"clean_ac_db={stats['ac_rms_db']:.1f} peak_db={stats['peak_db']:.1f} "
        f"act={stats['active_frame_ratio']*100:.0f}% max_sil={stats['max_internal_silence_sec']:.2f}s "
        f"val={'OK' if ok else 'FAIL(' + reason + ')'}",
        flush=True,
    )


def main() -> None:
    from huggingface_hub import snapshot_download
    from omnivoice import OmniVoice

    presets = load_builtin_presets()
    torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
    path = snapshot_download(MODEL, revision=REVISION)
    t0 = time.monotonic()
    model = OmniVoice.from_pretrained(path, device_map="cpu", dtype=torch.float32).eval()
    print(f"Loaded in {time.monotonic()-t0:.2f}s", flush=True)

    llm_dtypes = {str(p.dtype) for p in model.llm.parameters()}
    tok_dtypes = {str(p.dtype) for p in model.audio_tokenizer.parameters()}
    emb_dtypes = {str(p.dtype) for p in model.parameters()}
    print(f"Dtypes before cast: model={emb_dtypes}, llm={llm_dtypes}, audio_tokenizer={tok_dtypes}", flush=True)

    harper = presets["vs-harper"]
    morgan = presets["vs-morgan"]

    cases = [
        ("A1_harper_it_was_me_default_s1001", "It was me.", harper["instruct"], 1001, {"num_step": 16}),
        ("A2_harper_it_was_me_default_s1102", "It was me.", harper["instruct"], 1102, {"num_step": 16}),
        ("A3_harper_it_was_me_default_s1203", "It was me.", harper["instruct"], 1203, {"num_step": 16}),
        ("A4_morgan_it_was_me_same_face_s1003", "It was me. Same face.", morgan["instruct"], 1003, {"num_step": 16}),
        ("A5_morgan_it_was_me_same_face_s1104", "It was me. Same face.", morgan["instruct"], 1104, {"num_step": 16}),
        ("A6_morgan_it_was_me_same_face_s1205", "It was me. Same face.", morgan["instruct"], 1205, {"num_step": 16}),
        ("A7_harper_i_stood_up_s1001", "I stood up.", harper["instruct"], 1001, {"num_step": 16}),
        ("A8_morgan_thirteen_s1003", "Thirteen.", morgan["instruct"], 1003, {"num_step": 16}),
        ("B1_harper_2_13_digits", "At exactly 2:13, the television switched on.", harper["instruct"], 1001, {"num_step": 16}),
        ("B2_harper_2_13_words", "At exactly two thirteen, the television switched on.", harper["instruct"], 1001, {"num_step": 16}),
    ]

    for name, txt, instr, seed, kw in cases:
        with torch.inference_mode():
            torch.manual_seed(seed)
            out = model.generate(text=txt, instruct=instr, **kw)[0]
        inspect_tensor_stats(name, out, txt)

    # Now force full float32 on audio_tokenizer too and test variations
    model = model.to(torch.float32).eval()
    model.audio_tokenizer = model.audio_tokenizer.to(torch.float32).eval()
    print("Cast model + audio_tokenizer to float32.", flush=True)

    var_cases = [
        ("C1_f32_harper_it_was_me_postF", "It was me.", harper["instruct"], 1001, {"num_step": 16, "postprocess_output": False}),
        ("C2_f32_harper_it_was_me_lang_en", "It was me.", harper["instruct"], 1001, {"num_step": 16, "language": "en"}),
        ("C3_f32_harper_it_was_me_step32", "It was me.", harper["instruct"], 1001, {"num_step": 32, "language": "en"}),
        ("C4_f32_harper_it_was_me_step32_postF", "It was me.", harper["instruct"], 1001, {"num_step": 32, "language": "en", "postprocess_output": False}),
        ("C5_f32_harper_it_was_me_dur1.8", "It was me.", harper["instruct"], 1001, {"num_step": 16, "language": "en", "duration": 1.8}),
        ("C6_f32_harper_it_was_me_classtemp0.4", "It was me.", harper["instruct"], 1102, {"num_step": 16, "language": "en", "class_temperature": 0.4}),
        ("C7_f32_harper_L20_packed15", "It was me. Same clothes, same shoes, and the old brass key in my hand.", harper["instruct"], 1001, {"num_step": 16, "language": "en"}),
        ("C8_f32_harper_L20_full24", "It was me. Same clothes, same shoes, and the old brass key in my hand. But I was sitting on the bed watching the screen.", harper["instruct"], 1001, {"num_step": 16, "language": "en"}),
        ("D1_f32_morgan_it_was_me_same_face_lang_en", "It was me. Same face.", morgan["instruct"], 1003, {"num_step": 16, "language": "en"}),
        ("D2_f32_morgan_it_was_me_same_face_step32", "It was me. Same face.", morgan["instruct"], 1003, {"num_step": 32, "language": "en"}),
        ("D3_f32_morgan_L6_packed19", "It was me. Same face. Same scar above my eyebrow. Even the little birthmark on my neck was there.", morgan["instruct"], 1003, {"num_step": 16, "language": "en"}),
        ("D4_f32_morgan_L6_packed_step32", "It was me. Same face. Same scar above my eyebrow. Even the little birthmark on my neck was there.", morgan["instruct"], 1003, {"num_step": 32, "language": "en"}),
        ("D5_f32_morgan_thirteen_lang_en", "Thirteen.", morgan["instruct"], 1003, {"num_step": 16, "language": "en"}),
        ("D6_f32_morgan_thirteen_step32", "Thirteen.", morgan["instruct"], 1003, {"num_step": 32, "language": "en"}),
    ]

    for name, txt, instr, seed, kw in var_cases:
        with torch.inference_mode():
            torch.manual_seed(seed)
            out = model.generate(text=txt, instruct=instr, **kw)[0]
        inspect_tensor_stats(name, out, txt)


if __name__ == "__main__":
    main()
