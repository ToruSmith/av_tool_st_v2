"""音視頻工具 (Streamlit v2)：分離音頻 / 合併音頻，含真實進度條與波形預覽。
安裝：pip install streamlit      （另需 ffmpeg、ffprobe 在 PATH）
執行：streamlit run av_tool_st_v2.py --server.maxUploadSize 2000
"""
import collections
import io
import json
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import streamlit as st

st.set_page_config(page_title="音視頻工具", page_icon="🎬")

VIDEO_TYPES = ["mp4", "mkv", "mov", "avi", "webm", "flv", "m4v", "ts"]
AUDIO_TYPES = ["mp3", "m4a", "aac", "wav", "flac", "ogg", "opus"]
PRESETS = {
    "MP3 · 320k": (".mp3", ["-c:a", "libmp3lame", "-b:a", "320k"]),
    "MP3 · VBR V0（最高品質）": (".mp3", ["-c:a", "libmp3lame", "-q:a", "0"]),
    "MP3 · 192k": (".mp3", ["-c:a", "libmp3lame", "-b:a", "192k"]),
    "無損複製（自動判斷格式）": (None, ["-c:a", "copy"]),
}
CODEC_EXT = {"aac": ".m4a", "mp3": ".mp3", "opus": ".opus", "vorbis": ".ogg",
             "flac": ".flac", "ac3": ".ac3", "eac3": ".eac3"}
MODES = ["替換原音軌", "混入背景音樂", "獨立兩條音軌"]
DEFAULTS = {"preset": next(iter(PRESETS)), "mode": MODES[1], "fmt": "mp4",
            "abr": "192k", "ovol": 100, "bvol": 30}
CFG = Path.home() / ".av_tool_st.json"


# ---------- FFmpeg 輔助 ----------
def probe(path, *args):
    r = subprocess.run(["ffprobe", "-v", "error", *args, "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    return r.stdout.strip()


def duration(path):
    try:
        return float(probe(path, "-show_entries", "format=duration"))
    except ValueError:
        return 0.0


def has_audio(path):
    return bool(probe(path, "-select_streams", "a", "-show_entries", "stream=index"))


def audio_codec(path):
    return probe(path, "-select_streams", "a:0", "-show_entries", "stream=codec_name")


def run_ffmpeg(cmd, dur, on_progress):
    full = [cmd[0], "-nostats", "-loglevel", "error", "-progress", "pipe:1", *cmd[1:]]
    p = subprocess.Popen(full, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         encoding="utf-8", errors="replace")
    tail = collections.deque(maxlen=6)
    try:
        for line in p.stdout:
            line = line.strip()
            if line.startswith("out_time_ms="):
                try:
                    if dur:
                        on_progress(min(int(line.split("=")[1]) / 1e6 / dur, 1.0))
                except ValueError:
                    pass
            elif line and "=" not in line:
                tail.append(line)
        p.wait()
    finally:
        if p.poll() is None:  # 使用者按 Stop 時中止 FFmpeg
            p.terminate()
    return p.returncode == 0, "\n".join(tail)


def unique(path):
    p, n = Path(path), 1
    while p.exists():
        p = Path(path).with_name(f"{Path(path).stem} ({n}){Path(path).suffix}")
        n += 1
    return p


def save_upload(up, folder):
    p = unique(Path(folder) / Path(up.name).name)
    p.write_bytes(up.getbuffer())
    return p


def new_workdir():
    old = st.session_state.get("work")
    if old:
        shutil.rmtree(old, ignore_errors=True)
    d = Path(tempfile.mkdtemp(prefix="avtool_"))
    (d / "in").mkdir()
    (d / "out").mkdir()
    st.session_state["work"] = str(d)
    return d


def merge_cmd(v, a, out, mode, fmt, abr, loop, ovol, bvol):
    """回傳 (指令, 錯誤訊息)。輸出長度固定為影片長度。"""
    orig, dur = has_audio(v), duration(v)
    cap = ["-t", str(dur)] if dur else ["-shortest"]
    pad = [] if loop or not dur else ["-af", "apad"]
    base = ["ffmpeg", "-y", "-i", str(v), *(["-stream_loop", "-1"] if loop else []), "-i", str(a)]
    if mode == MODES[0]:
        return base + ["-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", abr,
                       *pad, *cap, str(out)], None
    if mode == MODES[1]:
        if not orig:
            return None, "這支影片沒有原音軌，無法混音，請改用「替換原音軌」。"
        fc = (f"[0:a]volume={ovol / 100}[o];[1:a]volume={bvol / 100}[bg];"
              "[o][bg]amix=inputs=2:duration=first[a]")
        return base + ["-filter_complex", fc, "-map", "0:v", "-map", "[a]", "-c:v", "copy",
                       "-c:a", "aac", "-b:a", abr, *cap, str(out)], None
    cmd = base + ["-map", "0:v"] + (["-map", "0:a"] if orig else []) + [
        "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", abr]
    if orig and fmt == "mkv":
        cmd += ["-c:a:0", "copy"]
    for i, t in enumerate(["原音", "音樂"] if orig else ["音樂"]):
        cmd += [f"-metadata:s:a:{i}", f"title={t}"]
    return cmd + [*cap, str(out)], None


@st.cache_data(show_spinner=False, max_entries=8)
def wave(name, size, color, _up):
    with tempfile.TemporaryDirectory() as d:
        src, out = Path(d) / "src", Path(d) / "w.png"
        src.write_bytes(_up.getbuffer())
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-t", "900", "-i", str(src),
                            "-filter_complex",
                            f"[0:a:0]aformat=channel_layouts=mono,showwavespic=s=560x48:colors={color}[w]",
                            "-map", "[w]", "-frames:v", "1", str(out)], capture_output=True)
        return out.read_bytes() if r.returncode == 0 and out.exists() else None


# ---------- 設定記憶 ----------
def load_cfg():
    try:
        return {**DEFAULTS, **json.loads(CFG.read_text(encoding="utf-8"))}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def reset_cfg():
    st.session_state.update(DEFAULTS)


if "init" not in st.session_state:
    st.session_state.update(load_cfg())
    st.session_state["remember"] = True
    st.session_state["init"] = True

# ---------- 介面 ----------
st.title("🎬 音視頻工具")
with st.sidebar:
    st.header("設定")
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        st.success("FFmpeg 已就緒")
    else:
        st.error("找不到 FFmpeg / ffprobe，請先安裝並加入 PATH。")
        st.stop()
    st.checkbox("記住我的選項", key="remember", help="儲存在執行本程式的電腦上")
    st.button("重設為預設值", on_click=reset_cfg)
    st.caption("深色或淺色主題：右上角選單 → Settings → Theme。\n\n"
               "處理中想中止，請按右上角的 Stop。")

tab1, tab2 = st.tabs(["分離音頻", "合併音頻與影片"])

with tab1:
    files = st.file_uploader("選擇影片（可多選）", type=VIDEO_TYPES, accept_multiple_files=True)
    st.selectbox("輸出格式", list(PRESETS), key="preset")
    if st.button("開始分離", disabled=not files):
        work = new_workdir()
        ext, args = PRESETS[st.session_state["preset"]]
        outs, fails = [], []
        bar, status = st.progress(0.0), st.empty()
        for i, up in enumerate(files):
            status.text(f"處理中 {i + 1}/{len(files)}：{up.name}")
            src = save_upload(up, work / "in")
            if not has_audio(src):
                fails.append(f"{up.name}：沒有音軌")
                continue
            e = ext or CODEC_EXT.get(audio_codec(src))
            if not e:
                fails.append(f"{up.name}：此音軌無法直接複製，請改選 MP3")
                continue
            out = unique(work / "out" / (src.stem + e))
            ok, err = run_ffmpeg(["ffmpeg", "-y", "-i", str(src), "-vn", "-map", "0:a:0", *args, str(out)],
                                 duration(src), lambda v, i=i: bar.progress((i + v) / len(files)))
            if ok:
                outs.append(str(out))
            else:
                out.unlink(missing_ok=True)
                fails.append(f"{up.name}：{err or '未知錯誤'}")
            src.unlink(missing_ok=True)
        bar.progress(1.0)
        status.empty()
        st.session_state["ex_res"], st.session_state["ex_fail"] = outs, fails

    for msg in st.session_state.get("ex_fail", []):
        st.error(msg)
    res = [p for p in st.session_state.get("ex_res", []) if Path(p).exists()]
    if len(res) == 1:
        data = Path(res[0]).read_bytes()
        if res[0].endswith(".mp3"):
            st.audio(data, format="audio/mp3")
        st.download_button(f"下載 {Path(res[0]).name}", data, file_name=Path(res[0]).name)
    elif res:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
            for p in res:
                z.write(p, Path(p).name)
        st.download_button(f"下載全部（{len(res)} 個，ZIP）", buf.getvalue(),
                           file_name="audio.zip", mime="application/zip")

with tab2:
    video = st.file_uploader("影片", type=VIDEO_TYPES, key="mg_v")
    audio = st.file_uploader("音頻", type=AUDIO_TYPES, key="mg_a")
    for up, color, label in ((video, "0x378ADD", "影片波形"), (audio, "0x1D9E75", "音樂波形")):
        if up:
            png = wave(up.name, up.size, color, up)
            if png:
                st.image(png, caption=f"{label}（前 15 分鐘）")
    st.radio("模式", MODES, key="mode", horizontal=True)
    mix = st.session_state["mode"] == MODES[1]
    c1, c2, c3 = st.columns(3)
    c1.selectbox("輸出格式", ["mp4", "mkv"], key="fmt")
    c2.selectbox("AAC 位元率", ["128k", "192k", "256k"], key="abr")
    loop = c3.checkbox("音樂不足時循環")
    st.slider("原音音量 %", 0, 200, key="ovol", disabled=not mix)
    st.slider("音樂音量 %", 0, 100, key="bvol", disabled=not mix)

    if st.button("合併", disabled=not (video and audio)):
        work = new_workdir()
        ss = st.session_state
        v, a = save_upload(video, work / "in"), save_upload(audio, work / "in")
        out = work / "out" / f"{Path(video.name).stem}_merged.{ss['fmt']}"
        cmd, err = merge_cmd(v, a, out, ss["mode"], ss["fmt"], ss["abr"], loop, ss["ovol"], ss["bvol"])
        ss.pop("mg_res", None)
        if err:
            st.warning(err)
        else:
            bar = st.progress(0.0)
            ok, e = run_ffmpeg(cmd, duration(v), bar.progress)
            if ok:
                bar.progress(1.0)
                ss["mg_res"] = str(out)
            else:
                out.unlink(missing_ok=True)
                st.error(e or "未知錯誤")

    res = st.session_state.get("mg_res")
    if res and Path(res).exists():
        st.success("完成")
        st.download_button(f"下載 {Path(res).name}", Path(res).read_bytes(), file_name=Path(res).name,
                           mime="video/mp4" if res.endswith(".mp4") else "video/x-matroska")

# ---------- 儲存設定 ----------
if st.session_state.get("remember"):
    snap = {k: st.session_state[k] for k in DEFAULTS}
    if snap != st.session_state.get("_saved"):
        try:
            CFG.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        st.session_state["_saved"] = snap
