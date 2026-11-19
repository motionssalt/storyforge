"""
Final FFmpeg video compositor for StoryForge.
Combines:
  - 1080x1920 silent gameplay background
  - Persistent Reddit post card shell + smooth 30fps narration-synced scrolling body
    with subtle active-sentence highlight and soft top/bottom edge fades
  - Burned-in ASS comic subtitles (1-3 words, active word highlighted)
  - Narration WAV encoded as AAC
"""

from __future__ import annotations

import math
import subprocess
from typing import Any, Sequence

from tts_engine import LineTiming, WordTiming


def compute_scroll_curve(
    word_timings: Sequence[WordTiming],
    word_ys: Sequence[dict[str, Any]],
    viewport_h: int,
    body_h: int,
    duration_sec: float,
    fps: int = 30,
) -> list[float]:
    """
    Build a smooth, monotonic 30fps scroll_y[frame] curve (in pixels)
    synced to real WordTiming timestamps and Chromium-measured word Y coordinates.
    """
    total_frames = max(1, int(math.ceil(duration_sec * fps)))
    max_scroll = max(0.0, float(body_h - viewport_h))
    if max_scroll <= 1.0 or not word_timings or not word_ys:
        return [0.0] * total_frames

    # Pair each WordTiming with its matching word Y coordinate
    # Match primarily by index; fall back cleanly if lengths differ slightly
    samples: list[tuple[float, float]] = [(0.0, 0.0)]
    count = min(len(word_timings), len(word_ys))
    target_anchor = 0.36 * float(viewport_h)
    prev_y = 0.0

    for i in range(count):
        wt = word_timings[i]
        wy = float(word_ys[i]["y_mid"])
        desired = max(0.0, min(max_scroll, wy - target_anchor))
        if desired < prev_y:
            desired = prev_y
        prev_y = desired
        t_mid = max(0.0, (float(wt.start) + float(wt.end)) / 2.0)
        samples.append((t_mid, desired))

    # Ensure scroll glides cleanly to max_scroll by the end of narration
    last_word_end = float(word_timings[count - 1].end) if count > 0 else duration_sec
    end_t = max(samples[-1][0] + 0.2, min(duration_sec, last_word_end))
    if prev_y < max_scroll:
        samples.append((end_t, max_scroll))
    if end_t < duration_sec:
        samples.append((duration_sec, max_scroll))

    # Deduplicate / ensure strictly non-decreasing time keys
    clean_samples: list[tuple[float, float]] = []
    for t, y in samples:
        if clean_samples and t <= clean_samples[-1][0]:
            clean_samples[-1] = (clean_samples[-1][0], max(clean_samples[-1][1], y))
        else:
            clean_samples.append((t, y))

    # Sample onto 30fps grid via linear interpolation
    raw_frames: list[float] = []
    s_idx = 0
    n_s = len(clean_samples)
    for f in range(total_frames):
        t = f / float(fps)
        while s_idx + 1 < n_s and clean_samples[s_idx + 1][0] < t:
            s_idx += 1
        if s_idx + 1 >= n_s:
            raw_frames.append(clean_samples[-1][1])
        else:
            t0, y0 = clean_samples[s_idx]
            t1, y1 = clean_samples[s_idx + 1]
            if t1 <= t0:
                raw_frames.append(y1)
            else:
                alpha = max(0.0, min(1.0, (t - t0) / (t1 - t0)))
                raw_frames.append(y0 + (y1 - y0) * alpha)

    # Apply symmetric triangular smoothing (~0.8s window) for silky easing without staircase jumps
    radius = max(1, int(round(0.4 * fps)))
    weights = [radius + 1 - abs(k) for k in range(-radius, radius + 1)]
    w_sum = float(sum(weights))
    smoothed: list[float] = []
    for f in range(total_frames):
        acc = 0.0
        for k, w in zip(range(-radius, radius + 1), weights):
            idx = max(0, min(total_frames - 1, f + k))
            acc += raw_frames[idx] * w
        val = max(0.0, min(max_scroll, acc / w_sum))
        if smoothed and val < smoothed[-1]:
            val = smoothed[-1]
        smoothed.append(val)

    # Guarantee exact rest at start (if initial raw was 0) and exact end at max_scroll
    if smoothed:
        smoothed[-1] = max_scroll
    return smoothed


def _make_edge_fade_masks(w: int, h: int, bg_rgb: tuple[int, int, int], top_h: int = 24, bot_h: int = 30):
    from PIL import Image

    top_mask = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    bot_mask = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    r, g, b = bg_rgb

    top_px = top_mask.load()
    for y in range(min(top_h, h)):
        u = 1.0 - (y / float(max(1, top_h)))
        a = int(round(255 * (u * u)))
        for x in range(w):
            top_px[x, y] = (r, g, b, a)

    bot_px = bot_mask.load()
    for i in range(min(bot_h, h)):
        y = h - 1 - i
        u = 1.0 - (i / float(max(1, bot_h)))
        a = int(round(255 * (u * u)))
        for x in range(w):
            bot_px[x, y] = (r, g, b, a)

    return top_mask, bot_mask


def compose_scrolling_video(
    bg_video_path: str,
    audio_wav_path: str,
    ass_path: str,
    fonts_dir: str,
    post_assets: dict[str, Any],
    word_timings: Sequence[WordTiming],
    line_timings: dict[str, LineTiming],
    out_mp4_path: str,
    duration_sec: float,
) -> str:
    """Render final 1080x1920 30fps H.264 MP4 with persistent auto-scrolling Reddit post card."""
    from PIL import Image

    fps = 30
    vw = int(post_assets["viewport_w"])
    vh = int(post_assets["viewport_h"])
    vp_x = int(post_assets["viewport_screen_x"])
    vp_y = int(post_assets["viewport_screen_y"])
    stage_y = int(post_assets["stage_screen_y"])
    bg_rgb = tuple(post_assets["card_bg_rgb"])
    line_rects: dict[str, list[int]] = post_assets.get("line_rects", {})

    dim_img = Image.open(post_assets["body_dim_png"]).convert("RGBA")
    bright_img = Image.open(post_assets["body_bright_png"]).convert("RGBA")
    if dim_img.width != vw:
        dim_img = dim_img.resize((vw, dim_img.height), Image.Resampling.LANCZOS)
    if bright_img.width != vw or bright_img.height != dim_img.height:
        bright_img = bright_img.resize(dim_img.size, Image.Resampling.LANCZOS)

    actual_body_h = dim_img.height
    max_scroll = max(0.0, float(actual_body_h - vh))

    scroll_curve = compute_scroll_curve(
        word_timings=word_timings,
        word_ys=post_assets.get("word_ys", []),
        viewport_h=vh,
        body_h=actual_body_h,
        duration_sec=duration_sec,
        fps=fps,
    )
    total_frames = len(scroll_curve)

    # Build sorted line timing intervals so each spoken line stays highlighted smoothly
    sorted_lines = sorted(line_timings.values(), key=lambda lt: lt.start)
    frame_active_lid: list[str | None] = [None] * total_frames
    for idx, lt in enumerate(sorted_lines):
        st_f = max(0, int(math.floor(lt.start * fps)))
        next_start = sorted_lines[idx + 1].start if idx + 1 < len(sorted_lines) else duration_sec
        en_t = min(duration_sec, max(lt.end, next_start if (next_start - lt.end) < 0.25 else lt.end))
        en_f = min(total_frames, int(math.ceil(en_t * fps)))
        for f in range(st_f, en_f):
            frame_active_lid[f] = lt.line_id

    top_fade_base, bot_fade_base = _make_edge_fade_masks(vw, vh, bg_rgb, top_h=22, bot_h=28)
    solid_bg = Image.new("RGBA", (vw, vh), (*bg_rgb, 255))

    ass_esc = ass_path.replace("\\", "/").replace(":", "\\:")
    fonts_esc = fonts_dir.replace("\\", "/").replace(":", "\\:")

    filter_complex = (
        f"[0:v][2:v]overlay=x=0:y={stage_y}:shortest=1[vcard];"
        f"[vcard][3:v]overlay=x={vp_x}:y={vp_y}:shortest=1[vbody];"
        f"[vbody]ass='{ass_esc}':fontsdir='{fonts_esc}'[vfinal]"
    )

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        bg_video_path,
        "-i",
        audio_wav_path,
        "-loop",
        "1",
        "-i",
        post_assets["card_shell_png"],
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{vw}x{vh}",
        "-r",
        str(fps),
        "-i",
        "pipe:0",
        "-filter_complex",
        filter_complex,
        "-map",
        "[vfinal]",
        "-map",
        "1:a",
        "-t",
        f"{duration_sec:.3f}",
        "-r",
        str(fps),
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "22",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "44100",
        "-movflags",
        "+faststart",
        out_mp4_path,
    ]

    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    assert proc.stdin is not None

    try:
        for f in range(total_frames):
            sy = scroll_curve[f]
            y0 = int(math.floor(sy))
            frac = sy - y0
            y1 = min(actual_body_h, y0 + vh + 2)

            strip_h = max(vh + 2, y1 - y0)
            strip = Image.new("RGBA", (vw, strip_h), (0, 0, 0, 0))
            if y1 > y0:
                dim_crop = dim_img.crop((0, y0, vw, y1))
                strip.paste(dim_crop, (0, 0))

            lid = frame_active_lid[f]
            if lid and lid in line_rects:
                lt_px, lb_px = line_rects[lid]
                ov_top = max(int(lt_px), y0)
                ov_bot = min(int(lb_px), y1)
                if ov_bot > ov_top:
                    hi_crop = bright_img.crop((0, ov_top, vw, ov_bot))
                    strip.paste(hi_crop, (0, ov_top - y0))

            if frac > 1e-4:
                scrolled = strip.transform(
                    (vw, vh),
                    Image.Transform.AFFINE,
                    (1.0, 0.0, 0.0, 0.0, 1.0, frac),
                    resample=Image.Resampling.BILINEAR,
                )
            else:
                scrolled = strip.crop((0, 0, vw, vh))

            frame_rgba = Image.alpha_composite(solid_bg, scrolled)

            if sy > 2.0:
                if sy < 20.0:
                    alpha_scale = sy / 20.0
                    top_m = top_fade_base.copy()
                    r_ch, g_ch, b_ch, a_ch = top_m.split()
                    a_ch = a_ch.point(lambda p: int(p * alpha_scale))
                    top_m = Image.merge("RGBA", (r_ch, g_ch, b_ch, a_ch))
                    frame_rgba = Image.alpha_composite(frame_rgba, top_m)
                else:
                    frame_rgba = Image.alpha_composite(frame_rgba, top_fade_base)

            rem = max_scroll - sy
            if max_scroll > 2.0 and rem > 2.0:
                if rem < 20.0:
                    alpha_scale = rem / 20.0
                    bot_m = bot_fade_base.copy()
                    r_ch, g_ch, b_ch, a_ch = bot_m.split()
                    a_ch = a_ch.point(lambda p: int(p * alpha_scale))
                    bot_m = Image.merge("RGBA", (r_ch, g_ch, b_ch, a_ch))
                    frame_rgba = Image.alpha_composite(frame_rgba, bot_m)
                else:
                    frame_rgba = Image.alpha_composite(frame_rgba, bot_fade_base)

            proc.stdin.write(frame_rgba.convert("RGB").tobytes())

        proc.stdin.close()
    except BrokenPipeError:
        pass

    ret = proc.wait()
    if ret != 0:
        raise subprocess.CalledProcessError(ret, cmd)
    return out_mp4_path


def compute_card_windows(
    cards: list[dict[str, Any]],
    line_timings: dict[str, LineTiming],
    total_duration: float,
) -> list[tuple[int, float, float]]:
    """Legacy card window calculator preserved for optional reuse."""
    windows: list[tuple[int, float, float]] = []
    for idx, item in enumerate(cards):
        from_id = str(item["from_line"])
        to_id = str(item["to_line"])
        st = line_timings[from_id].start if from_id in line_timings else 0.0
        en = line_timings[to_id].end if to_id in line_timings else min(total_duration, st + 3.0)

        if "duration_sec" in item and item["duration_sec"] is not None:
            en = min(total_duration, st + float(item["duration_sec"]))
        elif idx == 0:
            en = min(total_duration, st + 2.8)

        en = max(st + 0.5, min(total_duration, en))
        windows.append((idx, round(st, 3), round(en, 3)))
    return windows


def compose_video(
    bg_video_path: str,
    audio_wav_path: str,
    ass_path: str,
    fonts_dir: str,
    card_pngs: list[str],
    card_windows: list[tuple[int, float, float]],
    out_mp4_path: str,
    duration_sec: float,
) -> str:
    """Legacy multi-card compositor preserved for optional reuse."""
    cmd: list[str] = ["ffmpeg", "-y", "-i", bg_video_path, "-i", audio_wav_path]

    for idx, (_, st, en) in enumerate(card_windows):
        dur = max(0.5, en - st)
        cmd += ["-loop", "1", "-t", f"{dur:.3f}", "-i", card_pngs[idx]]

    filter_parts: list[str] = []
    last_v = "0:v"

    for i, (_, st, en) in enumerate(card_windows):
        in_idx = 2 + i
        dur = max(0.5, en - st)
        fade_d = min(0.18, dur / 4.0)
        fade_out_st = max(0.0, dur - fade_d)
        card_tag = f"card{i}"
        out_tag = f"v{i}"

        filter_parts.append(
            f"[{in_idx}:v]format=rgba,"
            f"fade=t=in:st=0:d={fade_d:.3f}:alpha=1,"
            f"fade=t=out:st={fade_out_st:.3f}:d={fade_d:.3f}:alpha=1,"
            f"setpts=PTS-STARTPTS+{st:.3f}/TB[{card_tag}]"
        )
        filter_parts.append(
            f"[{last_v}][{card_tag}]overlay=x=0:y=190:eof_action=pass:enable='between(t,{st:.3f},{en:.3f})'[{out_tag}]"
        )
        last_v = out_tag

    ass_esc = ass_path.replace("\\", "/").replace(":", "\\:")
    fonts_esc = fonts_dir.replace("\\", "/").replace(":", "\\:")
    filter_parts.append(f"[{last_v}]ass='{ass_esc}':fontsdir='{fonts_esc}'[vfinal]")

    filter_complex = ";".join(filter_parts)

    cmd += [
        "-filter_complex",
        filter_complex,
        "-map",
        "[vfinal]",
        "-map",
        "1:a",
        "-t",
        f"{duration_sec:.3f}",
        "-r",
        "30",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "22",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "44100",
        "-movflags",
        "+faststart",
        out_mp4_path,
    ]
    subprocess.run(cmd, check=True)
    return out_mp4_path

