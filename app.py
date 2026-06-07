"""
Video Render Service v2 — video ngắn dọc 9:16, cinematic.
Ảnh stock + giọng VBEE + (nhạc nền tuỳ chọn) + phụ đề → MP4.
Hiệu ứng: Ken Burns (zoom in/out luân phiên), chuyển cảnh crossfade, fade vào/ra.

POST /render  (header X-Render-Token)
Body:
{
  "images": ["url", ...],            # >=1 ảnh portrait
  "audio_url": "https://...mp3",     # giọng VBEE (1 file cho cả script — chế độ cũ)
  "sentences": [                     # (PA B) mỗi phần tử là 1 ĐOẠN: text + audio riêng
     {"text": "đoạn 1", "audio_url": "https://...mp3"},
     {"text": "đoạn 2", "audio_url": "https://...mp3"}
  ],
  "music_url": "https://...mp3",     # (tuỳ chọn) nhạc nền, sẽ lặp + hạ nhỏ dưới giọng
  "subtitle_text": "lời thoại",      # tuỳ chọn -> phụ đề (fallback khi không có sentences)
  "width":1080, "height":1920, "fps":30,
  "transition": 0.6,                 # giây crossfade giữa các ảnh
  "music_volume": 0.30,              # âm lượng nhạc nền (0-1)
  "subtitle": true
}

CHẾ ĐỘ PHỤ ĐỀ:
- Nếu có "sentences": render tải audio TỪNG ĐOẠN, ffprobe đo thời lượng THẬT từng đoạn,
  ghép lại bằng ffmpeg concat -> phụ đề khớp đúng biên đoạn (không lệch tích lũy).
- Nếu KHÔNG có "sentences": dùng "audio_url" + heuristic cũ (số từ + dấu câu). (backward-compatible)
"""
import os, re, tempfile, shutil, subprocess
import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

RENDER_TOKEN = os.environ.get("RENDER_TOKEN", "")
FONT = os.environ.get("SUB_FONT", "DejaVu Sans")
VERSION = "vidcfg-1"
app = FastAPI(title="Video Render Service v2")


def _run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg fail: " + p.stderr[-1800:])
    return p.stdout


def _probe_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except Exception:
        return 0.0


def _download(url, path, timeout=90):
    with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)


def _concat_audio(paths, out_path):
    """Nối nhiều file mp3 (mỗi đoạn 1 file) thành 1 file giọng.
    Re-encode để tránh lỗi timestamp khi nối mp3 raw."""
    lst = out_path + ".txt"
    with open(lst, "w", encoding="utf-8") as f:
        for p in paths:
            safe = p.replace("'", "'\\''")
            f.write("file '%s'\n" % safe)
    _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst,
          "-c:a", "libmp3lame", "-q:a", "2", out_path])


def _ass_time(t):
    h = int(t // 3600); t -= h * 3600
    m = int(t // 60); t -= m * 60
    s = int(t); cs = int(round((t - s) * 100))
    if cs == 100:
        s += 1; cs = 0
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def _norm_lines(text):
    """Tách script theo DÒNG, giữ thông tin ngắt đoạn (dòng trống = nghỉ dài)."""
    lines = (text or "").replace("\r", "").split("\n")
    out = []
    n = len(lines)
    for i, ln in enumerate(lines):
        s = re.sub(r"[ \t]+", " ", ln.strip())
        if not s:
            continue
        para_end = (i + 1 >= n) or (lines[i + 1].strip() == "")
        out.append((s, para_end))
    return out


def _split_chunks(text, max_words=12):
    """Trả list (chunk_text, pause_after_giây) — pause theo dấu câu cuối + ngắt đoạn,
    khớp cách giọng VBEE ngừng (chấm 0.45 / phẩy 0.25 / chấm-phẩy 0.30 / xuống dòng + đoạn)."""
    items = []
    for line, para_end in _norm_lines(text):
        end = line[-1]
        if end in ".!?…":
            p = 0.45
        elif end == ",":
            p = 0.25
        elif end in ";:":
            p = 0.30
        else:
            p = 0.20
        if para_end:
            p += 0.55                      # dòng trống sau đó -> lặng lâu hơn
        words = line.split(" ")
        if len(words) <= max_words:
            items.append((line, p))
        else:
            for i in range(0, len(words), max_words):
                piece = " ".join(words[i:i + max_words])
                last = i + max_words >= len(words)
                items.append((piece, p if last else 0.18))
    return items


def _ass_header(w, h, font, alignment=5, marginv=0):
    """Header + Style chung (chữ trắng đậm, viền đen, bóng nhẹ).
    alignment: 5 = CĂN GIỮA KHUNG (mặc định, giữ hành vi cũ); 2 = đáy-giữa (caption phim).
    marginv: khoảng cách dọc (với alignment 2 = cách ĐÁY; baseline ~ (h-marginv))."""
    fontsize = max(int(h * 0.034), 40)      # ~64px ở 1920
    marginlr = int(w * 0.08)
    return (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {w}\nPlayResY: {h}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, "
        "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font},{fontsize},&H00FFFFFF,&H00000000,&H64000000,"
        f"-1,0,0,0,100,100,0,0,1,4,2,{alignment},{marginlr},{marginlr},{marginv},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, Effect, Text\n"
    )


def _build_ass(text, total_dur, ass_path, w, h, font, alignment=5, marginv=0):
    """(CHẾ ĐỘ CŨ - heuristic) Tạo ASS chia thời lượng theo số từ + dấu câu, scale theo tổng audio.
    Lệch tích lũy về cuối — chỉ dùng khi không có sentences[]."""
    items = _split_chunks(text)
    if not items:
        return False
    # trọng số = thời gian đọc (theo số từ) + thời gian nghỉ sau chunk
    WORD_T = 0.30
    weights = [max(len(c.split(" ")), 1) * WORD_T + pause for c, pause in items]
    tot_w = sum(weights) or 1.0
    lines = [_ass_header(w, h, font, alignment, marginv)]
    t = 0.0
    for (c, _pause), wt in zip(items, weights):
        dur = total_dur * (wt / tot_w)
        start, end = t, min(t + dur, total_dur)
        t = end
        txt = c.replace("\n", " ").strip()
        lines.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,{txt}\n")
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write("".join(lines))
    return True


def _chunk_words(text, max_words=6):
    """Tách 1 câu thành các CỤM ngắn ~max_words từ (ưu tiên ngắt ở dấu phẩy/chấm).
    Trả list chuỗi cụm (không rỗng)."""
    words = str(text).split()
    if not words:
        return []
    chunks, cur = [], []
    for w in words:
        cur.append(w)
        ends_punct = w[-1] in ",;:.!?…" if w else False
        if len(cur) >= max_words or (ends_punct and len(cur) >= max(2, max_words - 2)):
            chunks.append(" ".join(cur)); cur = []
    if cur:
        # gộp cụm cuối quá ngắn (1 từ) vào cụm trước cho gọn
        if len(cur) == 1 and chunks:
            chunks[-1] = chunks[-1] + " " + cur[0]
        else:
            chunks.append(" ".join(cur))
    return chunks


def _seg_dialogues(segments, audio_total, chunk=False, max_words=6):
    """Tính (start, end, text) theo thời lượng audio THẬT của từng CÂU.
    Thời gian sub = mốc tích lũy thật của audio ghép (KHÔNG scale theo hình),
    chỉ hiệu chỉnh nhẹ theo độ dài file ghép thực tế (audio_total) để khớp tuyệt đối.
    chunk=False: MỖI câu = 1 khối phụ đề phủ đúng cửa sổ audio (hành vi cũ).
    chunk=True : chia câu thành CỤM ngắn hiện LẦN LƯỢT, chia cửa sổ audio của câu
                 theo SỐ TỪ mỗi cụm -> vẫn khớp tổng thời lượng câu (sub-sync per-câu)."""
    segs = [(t, max(float(d), 0.01)) for (t, d) in segments if (t or "").strip()]
    if not segs:
        return []
    s_sum = sum(d for _, d in segs) or 1.0
    scale = (audio_total / s_sum) if (audio_total and audio_total > 0) else 1.0
    out = []
    cursor = 0.0
    for text, d in segs:
        start = cursor * scale
        cursor += d
        end = cursor * scale
        txt = " ".join(str(text).split())   # gộp khoảng trắng; ASS tự xuống dòng (WrapStyle 0)
        if not chunk:
            out.append((start, end, txt))
            continue
        chunks = _chunk_words(txt, max_words)
        if len(chunks) <= 1:
            out.append((start, end, txt))
            continue
        wcounts = [max(len(c.split()), 1) for c in chunks]
        wtot = sum(wcounts) or 1
        span = end - start
        acc = 0
        for ci, c in enumerate(chunks):
            cs = start + span * (acc / wtot)
            acc += wcounts[ci]
            ce = start + span * (acc / wtot)
            out.append((cs, ce, c))
    return out


def _build_ass_segments(segments, audio_total, ass_path, w, h, font,
                        alignment=5, marginv=0, chunk=False, max_words=6):
    """(PA B v2) Tạo ASS theo cửa sổ audio thật của từng câu (chunk=True: chia cụm trong câu).
    audio_total = thời lượng THẬT của file giọng đã ghép (ffprobe)."""
    dials = _seg_dialogues(segments, audio_total, chunk=chunk, max_words=max_words)
    if not dials:
        return False
    lines = [_ass_header(w, h, font, alignment, marginv)]
    for start, end, txt in dials:
        lines.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,{txt}\n")
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write("".join(lines))
    return True


def _make_clip(img_path, dur, w, h, fps, idx, out_path):
    """1 ảnh -> clip dur giây, phủ kín WxH. Ken Burns MẠNH: luôn vừa zoom vừa pan
    (4 kiểu luân phiên theo idx) để hình luôn có cảm giác chuyển động."""
    frames = max(int(round(dur * fps)), 1)
    f = float(frames)
    cx = "iw/2-(iw/zoom/2)"      # tâm ngang
    cy = "ih/2-(ih/zoom/2)"      # tâm dọc
    Z0, Z1 = 1.0, 1.10            # zoom NHẸ (10%)
    amp = 0.05                    # biên pan NHẸ (5%)
    p = f"(on/{f})"               # tiến trình 0->1 TUYẾN TÍNH suốt clip (mượt, không cap)
    m = idx % 4
    if m == 0:      # zoom IN + lia sang phải
        z = f"{Z0}+{Z1 - Z0}*{p}"
        x = f"{cx}-({amp}*iw)/2+({amp}*iw)*{p}"
        y = cy
    elif m == 1:    # zoom OUT + lia sang trái
        z = f"{Z1}-{Z1 - Z0}*{p}"
        x = f"{cx}+({amp}*iw)/2-({amp}*iw)*{p}"
        y = cy
    elif m == 2:    # zoom IN + lia xuống
        z = f"{Z0}+{Z1 - Z0}*{p}"
        x = cx
        y = f"{cy}-({amp}*ih)/2+({amp}*ih)*{p}"
    else:           # zoom OUT + lia lên
        z = f"{Z1}-{Z1 - Z0}*{p}"
        x = cx
        y = f"{cy}+({amp}*ih)/2-({amp}*ih)*{p}"
    # phóng to gấp đôi rồi crop để có biên cho pan
    vf = (
        f"scale={w*2}:{h*2}:force_original_aspect_ratio=increase,crop={w*2}:{h*2},"
        f"zoompan=z='{z}':d={frames}:s={w}x{h}:fps={fps}:"
        f"x='{x}':y='{y}',"
        f"setsar=1,format=yuv420p"
    )
    _run(["ffmpeg", "-y", "-loop", "1", "-i", img_path, "-t", f"{dur:.3f}",
          "-r", str(fps), "-vf", vf, "-an", "-c:v", "libx264",
          "-preset", "veryfast", "-pix_fmt", "yuv420p", out_path])


def _make_clip_from_video(video_path, dur, w, h, fps, idx, out_path, zoom=1.0):
    """1 video B-roll -> clip dur giây, phủ kín WxH (cover-crop), cắt/lặp đúng thời lượng.
    zoom>1.0 (vd 1.10): phóng to clip rồi center-crop về WxH -> cắt mép -> XOÁ watermark
    'Veo' góc dưới-phải. Dùng cho kênh video (ManixAI/CKT/kênh 3) — KHÔNG ảnh hưởng ảnh tĩnh."""
    sw = int(round(w * max(zoom, 1.0))); sh = int(round(h * max(zoom, 1.0)))
    vf = (f"scale={sw}:{sh}:force_original_aspect_ratio=increase,crop={w}:{h},"
          f"setsar=1,fps={fps},format=yuv420p")
    _run(["ffmpeg", "-y", "-stream_loop", "-1", "-i", video_path, "-t", f"{dur:.3f}",
          "-an", "-vf", vf, "-c:v", "libx264", "-preset", "veryfast",
          "-pix_fmt", "yuv420p", out_path])


def _build_visual(clips, dur_each, trans, w, h, fps, workdir):
    """Nối các clip bằng crossfade xfade + fade vào/ra. Trả (path, total_dur)."""
    n = len(clips)
    if n == 1:
        total = dur_each
        out = os.path.join(workdir, "visual.mp4")
        fo = max(total - 0.6, 0)
        _run(["ffmpeg", "-y", "-i", clips[0],
              "-vf", f"fade=t=in:st=0:d=0.5,fade=t=out:st={fo:.2f}:d=0.6,format=yuv420p",
              "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", out])
        return out, total
    total = n * dur_each - (n - 1) * trans
    inputs = []
    for c in clips:
        inputs += ["-i", c]
    # chuỗi xfade
    parts = []
    prev = "0:v"
    for k in range(1, n):
        off = k * (dur_each - trans)
        lbl = f"x{k}" if k < n - 1 else "vx"
        parts.append(f"[{prev}][{k}:v]xfade=transition=fade:duration={trans}:offset={off:.3f}[{lbl}]")
        prev = lbl
    fo = max(total - 0.6, 0)
    parts.append(f"[vx]fade=t=in:st=0:d=0.5,fade=t=out:st={fo:.2f}:d=0.6,format=yuv420p[vout]")
    fc = ";".join(parts)
    out = os.path.join(workdir, "visual.mp4")
    _run(["ffmpeg", "-y"] + inputs + ["-filter_complex", fc, "-map", "[vout]",
          "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", out])
    return out, total


def render_video(image_paths, audio_path, music_path, subtitle_text,
                 w, h, fps, trans, music_vol, burn_sub, workdir,
                 video_paths=None, segments=None,
                 zoom_crop=False, zoom=1.10, sub_position="center",
                 sub_chunk=False, sub_max_words=6):
    audio_dur = _probe_duration(audio_path)
    if audio_dur <= 0:
        raise RuntimeError("Không đọc được thời lượng audio (giọng VBEE)")
    # B-roll video nếu có video_paths, ngược lại dùng ảnh tĩnh (Ken Burns) như cũ
    use_video = bool(video_paths)
    sources = video_paths if use_video else image_paths
    n = len(sources)
    # d sao cho tổng video (sau xfade) ~ bằng thời lượng giọng
    dur_each = (audio_dur + (n - 1) * trans) / n if n > 1 else audio_dur
    dur_each = max(dur_each, trans + 0.5)
    clip_zoom = zoom if (use_video and zoom_crop) else 1.0
    clips = []
    for i, src in enumerate(sources):
        cp = os.path.join(workdir, f"clip{i}.mp4")
        if use_video:
            _make_clip_from_video(src, dur_each, w, h, fps, i, cp, zoom=clip_zoom)
        else:
            _make_clip(src, dur_each, w, h, fps, i, cp)
        clips.append(cp)
    visual, total = _build_visual(clips, dur_each, trans, w, h, fps, workdir)

    # filter_complex cho phụ đề (video) + trộn audio
    # vị trí phụ đề: center (Alignment 5, giữ hành vi cũ) | bottom (Alignment 2 = đáy-giữa, ~1/3 dưới)
    if str(sub_position).lower() == "bottom":
        sub_align = 2
        sub_marginv = int(h * 0.16)    # baseline ~84% chiều cao -> nằm 1/3 dưới khung
    else:
        sub_align = 5
        sub_marginv = 0
    vfilters = "[0:v]"
    if burn_sub and (segments or subtitle_text):
        ass = os.path.join(workdir, "subs.ass")
        ok = False
        if segments:
            ok = _build_ass_segments(segments, audio_dur, ass, w, h, FONT,
                                     alignment=sub_align, marginv=sub_marginv,
                                     chunk=sub_chunk, max_words=sub_max_words)   # PA B: khớp audio THẬT
        if not ok and subtitle_text:
            ok = _build_ass(subtitle_text, total, ass, w, h, FONT,
                            alignment=sub_align, marginv=sub_marginv)        # fallback heuristic
        if ok:
            ass_esc = ass.replace(":", "\\:").replace("'", "\\'")
            vfilters += f"subtitles='{ass_esc}',"
    vfilters += "format=yuv420p[v]"

    out = os.path.join(workdir, "final.mp4")
    cmd = ["ffmpeg", "-y", "-i", visual, "-i", audio_path]
    if music_path:
        cmd += ["-stream_loop", "-1", "-i", music_path]
        afilter = (f"[1:a]volume=1.0[a1];[2:a]volume={music_vol}[a2];"
                   "[a1][a2]amix=inputs=2:duration=first:dropout_transition=2[aout]")
        fc = vfilters + ";" + afilter
        cmd += ["-filter_complex", fc, "-map", "[v]", "-map", "[aout]"]
    else:
        cmd += ["-filter_complex", vfilters, "-map", "[v]", "-map", "1:a"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-shortest", out]
    _run(cmd)
    return out, total


@app.get("/health")
def health():
    return {"ok": True, "ffmpeg": shutil.which("ffmpeg") is not None, "version": VERSION}


@app.post("/debug_subs")
def debug_subs(payload: dict, x_render_token: str = Header(default="")):
    """CHẨN ĐOÁN sub-sync: tải audio từng segment, ffprobe đo thật, ghép, tính mốc sub.
    Trả JSON nhỏ (KHÔNG render video) để đối chiếu sub có khớp audio không.
    Body: {"sentences":[{text,audio_url}]}"""
    if RENDER_TOKEN and x_render_token != RENDER_TOKEN:
        raise HTTPException(status_code=401, detail="invalid render token")
    sentences = payload.get("sentences") or []
    if not sentences:
        return JSONResponse(status_code=400, content={"error": "cần sentences[]"})
    workdir = tempfile.mkdtemp(prefix="dbg_")
    try:
        seg_paths, seg_durs, seg_texts = [], [], []
        for i, s in enumerate(sentences):
            au = (s.get("audio_url") or s.get("audioLink") or "").strip()
            tx = (s.get("text") or "").strip()
            if not au:
                continue
            sp = os.path.join(workdir, f"seg{i}.mp3")
            _download(au, sp)
            d = _probe_duration(sp)
            seg_paths.append(sp); seg_durs.append(round(d, 3)); seg_texts.append(tx)
        voice = os.path.join(workdir, "voice.mp3")
        _concat_audio(seg_paths, voice)
        concat_dur = round(_probe_duration(voice), 3)
        segments = list(zip(seg_texts, seg_durs))
        dials = _seg_dialogues(segments, concat_dur)
        cum = []
        c = 0.0
        for d in seg_durs:
            c += d; cum.append(round(c, 3))
        return {
            "n": len(seg_durs),
            "seg_durs": seg_durs,
            "sum_seg": round(sum(seg_durs), 3),
            "concat_dur": concat_dur,
            "cum_real": cum,
            "dialogues": [{"i": i, "start": round(s, 3), "end": round(e, 3),
                           "secs": round(e - s, 3), "words": len(t.split()),
                           "preview": t[:32]} for i, (s, e, t) in enumerate(dials)]
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@app.post("/thumbnail")
def thumbnail(payload: dict, x_render_token: str = Header(default="")):
    """Tạo ảnh bìa dọc 9:16 (ảnh nền + gradient + chữ hook căn giữa + nhãn brand).
    Body: {"image_url": "...", "hook": "câu hook ngắn",
           "brand": "MÓN QUÀ", "width":1080, "height":1920}
    Trả về file JPEG."""
    if RENDER_TOKEN and x_render_token != RENDER_TOKEN:
        raise HTTPException(status_code=401, detail="invalid render token")
    from thumb import make_thumbnail
    image_url = payload.get("image_url") or (payload.get("images") or [None])[0]
    hook = (payload.get("hook") or payload.get("subtitle_text") or "").strip()
    thumb_text = (payload.get("thumbnail_text") or "").strip()
    thumb_kw = (payload.get("thumbnail_keyword") or "").strip()
    if not image_url or (not thumb_text and not hook):
        return JSONResponse(status_code=400, content={"error": "cần image_url và thumbnail_text (hoặc hook)"})
    brand = payload.get("brand", "MÓN QUÀ")
    w = int(payload.get("width", 1080)); h = int(payload.get("height", 1920))

    workdir = tempfile.mkdtemp(prefix="thumb_")
    try:
        bg = os.path.join(workdir, "bg.jpg"); _download(image_url, bg)
        out = os.path.join(workdir, "thumb.jpg")
        make_thumbnail(bg, hook, brand=brand, w=w, h=h,
                       thumbnail_text=thumb_text, thumbnail_keyword=thumb_kw).save(out, "JPEG", quality=90)
        return FileResponse(out, media_type="image/jpeg", filename="thumbnail.jpg",
                            background=BackgroundTask(shutil.rmtree, workdir, True))
    except Exception as e:
        shutil.rmtree(workdir, ignore_errors=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/render")
def render(payload: dict, x_render_token: str = Header(default="")):
    if RENDER_TOKEN and x_render_token != RENDER_TOKEN:
        raise HTTPException(status_code=401, detail="invalid render token")
    images = payload.get("images") or []
    videos = payload.get("videos") or []          # B-roll video (Pexels video) — ưu tiên nếu có
    audio_url = payload.get("audio_url")
    sentences = payload.get("sentences") or []    # (PA B) mỗi phần tử 1 ĐOẠN {text, audio_url}
    if (not images and not videos) or (not audio_url and not sentences):
        return JSONResponse(status_code=400, content={"error": "cần images[] hoặc videos[]; và audio_url hoặc sentences[]"})
    w = int(payload.get("width", 1080)); h = int(payload.get("height", 1920))
    fps = int(payload.get("fps", 30))
    trans = float(payload.get("transition", 0.6))
    music_vol = float(payload.get("music_volume", 0.30))
    burn_sub = bool(payload.get("subtitle", True))
    sub_text = payload.get("subtitle_text", "")
    music_url = payload.get("music_url")
    # cờ cấu hình theo kênh (mặc định = hành vi cũ -> MQ ảnh tĩnh KHÔNG đổi)
    zoom_crop = bool(payload.get("zoom_crop", False))         # phóng to+crop clip video (xoá watermark)
    zoom = float(payload.get("zoom", 1.10))
    sub_position = str(payload.get("sub_position", "center"))  # center | bottom
    sub_chunk = bool(payload.get("sub_chunk", False))         # chia câu thành cụm ngắn hiện lần lượt
    sub_max_words = int(payload.get("sub_max_words", 6))

    workdir = tempfile.mkdtemp(prefix="render_")
    try:
        img_paths = []
        vid_paths = []
        if videos:
            for i, url in enumerate(videos):
                p = os.path.join(workdir, f"vid{i}.mp4")
                _download(url, p, timeout=180); vid_paths.append(p)
        else:
            for i, url in enumerate(images):
                p = os.path.join(workdir, f"img{i}.jpg")
                _download(url, p); img_paths.append(p)

        # ---- AUDIO + PHỤ ĐỀ ----
        segments = None
        audio_path = os.path.join(workdir, "voice.mp3")
        if sentences:
            seg_paths, seg_durs, seg_texts = [], [], []
            for i, s in enumerate(sentences):
                au = (s.get("audio_url") or s.get("audioLink") or "").strip()
                tx = (s.get("text") or "").strip()
                if not au:
                    continue
                sp = os.path.join(workdir, f"seg{i}.mp3")
                _download(au, sp)
                d = _probe_duration(sp)
                if d <= 0:
                    continue
                seg_paths.append(sp); seg_durs.append(d); seg_texts.append(tx)
            if seg_paths:
                _concat_audio(seg_paths, audio_path)
                segments = list(zip(seg_texts, seg_durs))
        if segments is None:
            # chế độ cũ: 1 file audio cho cả script
            if not audio_url:
                return JSONResponse(status_code=400, content={"error": "sentences[] không hợp lệ và thiếu audio_url"})
            _download(audio_url, audio_path)

        music_path = None
        if music_url:
            music_path = os.path.join(workdir, "music.mp3")
            try:
                _download(music_url, music_path)
            except Exception:
                music_path = None

        out, total = render_video(img_paths, audio_path, music_path, sub_text,
                                  w, h, fps, trans, music_vol, burn_sub, workdir,
                                  video_paths=(vid_paths or None), segments=segments,
                                  zoom_crop=zoom_crop, zoom=zoom, sub_position=sub_position,
                                  sub_chunk=sub_chunk, sub_max_words=sub_max_words)
        return FileResponse(out, media_type="video/mp4", filename="video.mp4",
                            background=BackgroundTask(shutil.rmtree, workdir, True))
    except Exception as e:
        shutil.rmtree(workdir, ignore_errors=True)
        return JSONResponse(status_code=500, content={"error": str(e)})
