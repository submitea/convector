"""Ядро конвертера: ffmpeg (аудио/видео), Pillow (картинки), Demucs (акапелла)."""
from __future__ import annotations

import functools
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Optional

APP_DIR = Path(__file__).resolve().parent
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LOW_PRIO = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)

AUDIO_EXT = {"mp3", "flac", "wav", "ogg", "opus", "m4a", "aac", "wma", "aiff", "aif", "ape", "wv"}
VIDEO_EXT = {"mp4", "mkv", "avi", "mov", "webm", "flv", "wmv", "m4v", "ts", "mpg", "mpeg", "3gp"}
IMAGE_EXT = {"png", "jpg", "jpeg", "webp", "bmp", "gif", "tif", "tiff", "ico"}
ALL_EXT = AUDIO_EXT | VIDEO_EXT | IMAGE_EXT

AUDIO_FORMATS = {
    "MP3":  dict(ext="mp3",  codec=["-c:a", "libmp3lame"], br=True,  cover=True),
    "FLAC": dict(ext="flac", codec=["-c:a", "flac"],       br=False, cover=True),
    "WAV":  dict(ext="wav",  codec=["-c:a", "pcm_s16le"],  br=False, cover=False),
    "OGG":  dict(ext="ogg",  codec=["-c:a", "libvorbis"],  br=True,  cover=False),
    "OPUS": dict(ext="opus", codec=["-c:a", "libopus"],    br=True,  cover=False),
    "M4A":  dict(ext="m4a",  codec=["-c:a", "aac"],        br=True,  cover=True),
    "AAC":  dict(ext="aac",  codec=["-c:a", "aac"],        br=True,  cover=False),
    "WMA":  dict(ext="wma",  codec=["-c:a", "wmav2"],      br=True,  cover=False),
    "AIFF": dict(ext="aiff", codec=["-c:a", "pcm_s16be"],  br=False, cover=False),
}

# --- видео: кодеки (в порядке предпочтения подбираются автоматически) ---
_EVEN = ["-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2"]   # чётные размеры для yuv420p
X264 = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p"] + _EVEN
NVENC = ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "21", "-b:v", "0",
         "-pix_fmt", "yuv420p"] + _EVEN
AMF = ["-c:v", "h264_amf", "-quality", "balanced", "-rc", "cqp", "-qp_i", "21", "-qp_p", "21",
       "-pix_fmt", "yuv420p"] + _EVEN
VP9 = ["-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0", "-row-mt", "1", "-cpu-used", "4",
       "-deadline", "good", "-pix_fmt", "yuv420p"] + _EVEN
GIF_ARGS = ["-vf", "fps=12,scale=480:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse",
            "-an", "-loop", "0"]

VIDEO_FORMATS = {
    "MP4":  dict(ext="mp4",  v="h264", a=["-c:a", "aac", "-b:a", "192k"], extra=["-movflags", "+faststart"]),
    "MKV":  dict(ext="mkv",  v="h264", a=["-c:a", "aac", "-b:a", "192k"], extra=[]),
    "MOV":  dict(ext="mov",  v="h264", a=["-c:a", "aac", "-b:a", "192k"], extra=[]),
    "AVI":  dict(ext="avi",  v="h264", a=["-c:a", "libmp3lame", "-b:a", "192k"], extra=[]),
    "WEBM": dict(ext="webm", v="vp9",  a=["-c:a", "libopus", "-b:a", "128k"], extra=[]),
    "GIF":  dict(ext="gif",  v="gif",  a=[], extra=[]),
}
# в какие контейнеры можно переложить потоки без перекодирования: (видеокодеки, аудиокодеки)
COPY_RULES = {
    "MP4": ({"h264", "hevc", "mpeg4"}, {"aac", "mp3"}),
    "MOV": ({"h264", "hevc", "mpeg4"}, {"aac", "mp3"}),
    "MKV": (None, None),
    "WEBM": ({"vp8", "vp9", "av1"}, {"opus", "vorbis"}),
}

IMAGE_FORMATS = {
    "PNG":  dict(ext="png",  pil="PNG"),
    "JPG":  dict(ext="jpg",  pil="JPEG"),
    "WEBP": dict(ext="webp", pil="WEBP"),
    "BMP":  dict(ext="bmp",  pil="BMP"),
    "GIF":  dict(ext="gif",  pil="GIF"),
    "TIFF": dict(ext="tiff", pil="TIFF"),
    "ICO":  dict(ext="ico",  pil="ICO"),
    "PDF":  dict(ext="pdf",  pil="PDF"),
}

Progress = Callable[[float, str], None]


class Cancelled(Exception):
    pass


class ConvError(Exception):
    pass


def category(path) -> Optional[str]:
    ext = Path(path).suffix.lower().lstrip(".")
    if ext in AUDIO_EXT:
        return "audio"
    if ext in VIDEO_EXT:
        return "video"
    if ext in IMAGE_EXT:
        return "image"
    return None


@functools.lru_cache(maxsize=None)
def find_tool(name: str) -> str:
    for cand in (APP_DIR / "ffmpeg" / f"{name}.exe", APP_DIR / "ffmpeg" / "bin" / f"{name}.exe"):
        if cand.exists():
            return str(cand)
    found = shutil.which(name)
    if found:
        return found
    raise ConvError(
        "ffmpeg не найден.\nУстанови командой:  winget install Gyan.FFmpeg\n"
        "(потом перезапусти конвертер) или положи ffmpeg.exe и ffprobe.exe в папку ffmpeg рядом с программой."
    )


# ---------- выходные файлы ----------
_LOCK = threading.Lock()


def reserve_path(p: Path) -> Path:
    """Выбирает свободное имя и сразу занимает его — параллельные задачи не столкнутся."""
    p.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        i = 0
        while True:
            c = p if i == 0 else p.with_name(f"{p.stem} ({i}){p.suffix}")
            try:
                with open(c, "xb"):
                    pass
                return c
            except FileExistsError:
                i += 1


@contextmanager
def _output(dst: Path):
    """Если конвертация упала или отменена — недописанный файл удаляется."""
    ok = False
    try:
        yield dst
        ok = True
    finally:
        if not ok:
            try:
                dst.unlink()
            except OSError:
                pass


def _dest(src: Path, ext: str, opts: dict, suffix: str = "") -> Path:
    base = Path(opts["out_dir"]) if opts.get("out_dir") else src.parent
    return reserve_path(base / f"{src.stem}{suffix}.{ext}")


# ---------- информация о файле и превью ----------
def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def file_info(path) -> dict:
    p = Path(path)
    cat = category(p)
    info: dict = {"cat": cat, "name": p.stem, "ext": p.suffix.lstrip(".").upper(), "size": p.stat().st_size}
    if cat == "image":
        from PIL import Image
        with Image.open(p) as im:
            info.update(w=im.width, h=im.height, mode=im.mode, fmt=im.format)
        return info
    r = subprocess.run(
        [find_tool("ffprobe"), "-v", "error", "-show_format", "-show_streams", "-of", "json", str(p)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=NO_WINDOW, timeout=30)
    data = json.loads(r.stdout or "{}")
    fmt = data.get("format", {})
    info["dur"] = _f(fmt.get("duration"))
    info["br"] = int(_f(fmt.get("bit_rate")) / 1000)
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    for s in data.get("streams", []):
        t = s.get("codec_type")
        if t == "audio" and "acodec" not in info:
            info.update(acodec=s.get("codec_name"), sr=int(_f(s.get("sample_rate"))), ch=s.get("channels"))
            for k, v in (s.get("tags") or {}).items():
                tags.setdefault(k.lower(), v)
        elif t == "video" and "vcodec" not in info and not (s.get("disposition") or {}).get("attached_pic"):
            num, _, den = str(s.get("avg_frame_rate", "0/1")).partition("/")
            fps = _f(num) / (_f(den) or 1)
            info.update(vcodec=s.get("codec_name"), w=s.get("width"), h=s.get("height"), fps=fps)
    info.update(title=tags.get("title"), artist=tags.get("artist") or tags.get("album_artist"),
                album=tags.get("album"), year=(tags.get("date") or tags.get("year") or "")[:4] or None)
    return info


def make_thumb(path, cat: str, size: int = 160):
    """Квадратное превью со скруглёнными углами: обложка трека, кадр из видео или сама картинка."""
    try:
        from PIL import Image, ImageDraw, ImageOps
        if cat == "image":
            im = Image.open(path)
            im.draft("RGB", (size * 2, size * 2))
            im = ImageOps.exif_transpose(im).convert("RGBA")
        else:
            ff = find_tool("ffmpeg")
            seeks = [["-ss", "1"], []] if cat == "video" else [[]]
            data = b""
            for sk in seeks:
                cmd = [ff, "-v", "error"] + sk + ["-i", str(path)]
                cmd += ["-an", "-map", "0:v:0", "-frames:v", "1", "-vf", f"scale={size * 2}:-2",
                        "-f", "image2pipe", "-vcodec", "png", "-"]
                data = subprocess.run(cmd, capture_output=True, creationflags=NO_WINDOW, timeout=20).stdout
                if data:
                    break
            if not data:
                return None
            im = Image.open(io.BytesIO(data)).convert("RGBA")
        im = ImageOps.fit(im, (size, size), Image.LANCZOS)
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=size // 5, fill=255)
        im.putalpha(mask)
        return im
    except Exception:
        return None


# ---------- запуск ffmpeg ----------
_KV = re.compile(r"^[a-z_0-9]+=")
_DUR = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def run_ffmpeg(cmd, duration, progress: Progress, cancel: threading.Event,
               base=0.0, span=1.0, label=""):
    """duration=0 — длительность берётся из вывода самого ffmpeg (без отдельного вызова ffprobe)."""
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace", creationflags=NO_WINDOW,
    )
    tail: list[str] = []
    try:
        for line in proc.stdout:
            if cancel.is_set():
                proc.terminate()
                raise Cancelled()
            line = line.strip()
            if duration <= 0:
                m = _DUR.search(line)
                if m:
                    duration = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
            if line.startswith(("out_time_us=", "out_time_ms=")):
                v = line.split("=", 1)[1]
                if v.isdigit() and duration > 0:
                    progress(base + span * min(int(v) / 1e6 / duration, 1.0), label)
            elif line and not _KV.match(line):
                tail = (tail + [line])[-12:]
        code = proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
    if code != 0:
        raise ConvError("ffmpeg: " + "\n".join(tail[-5:]))


def _base_cmd(src):
    return [find_tool("ffmpeg"), "-hide_banner", "-y", "-nostdin", "-progress", "pipe:1",
            "-nostats", "-i", str(src)]


# ---------- аудио ----------
def encode_audio(src, dst, fmt, bitrate, keep_cover, progress, cancel, base=0.0, span=1.0, label=""):
    spec = AUDIO_FORMATS[fmt]
    ogg_q = {128: 4, 192: 6, 256: 8, 320: 10}

    def build(with_br: bool):
        cmd = _base_cmd(src) + ["-map", "0:a:0"]
        if keep_cover and spec["cover"]:
            cmd += ["-map", "0:v:0?", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
        else:
            cmd += ["-vn"]
        cmd += spec["codec"]
        if with_br and spec["br"]:
            if fmt == "OGG":
                cmd += ["-q:a", str(ogg_q.get(int(bitrate), 6))]
            else:
                cmd += ["-b:a", f"{bitrate}k"]
        cmd += ["-map_metadata", "0"]
        if fmt == "MP3":
            cmd += ["-id3v2_version", "3"]
        cmd.append(str(dst))
        return cmd

    try:
        run_ffmpeg(build(True), 0, progress, cancel, base, span, label)
    except ConvError:
        if not spec["br"]:
            raise
        # некоторые кодеки не принимают заданный битрейт (например, для моно) — пробуем по умолчанию
        run_ffmpeg(build(False), 0, progress, cancel, base, span, label)


# ---------- видео ----------
@functools.lru_cache(maxsize=None)
def _encoders() -> frozenset:
    try:
        out = subprocess.run([find_tool("ffmpeg"), "-hide_banner", "-encoders"], capture_output=True,
                             text=True, errors="replace", creationflags=NO_WINDOW, timeout=20).stdout
        return frozenset(re.findall(r"^\s*V[\w.]{5}\s+(\S+)", out, re.M))
    except Exception:
        return frozenset()


_GOOD_H264: Optional[str] = None  # какой кодер уже сработал — со следующего файла берём его сразу


def _h264_variants():
    enc = _encoders()
    v = []
    if "h264_nvenc" in enc:
        v.append(("nvenc", "видеокарте NVIDIA", NVENC))
    if "h264_amf" in enc:
        v.append(("amf", "видеокарте AMD", AMF))
    v.append(("x264", "процессоре", X264))
    v.sort(key=lambda x: x[0] != _GOOD_H264)
    return v


def can_copy(info, fmt) -> bool:
    rule = COPY_RULES.get(fmt)
    if not rule or not info or not info.get("vcodec"):
        return False
    vs, as_ = rule
    if vs is not None and info["vcodec"] not in vs:
        return False
    a = info.get("acodec")
    return not (a and as_ is not None and a not in as_)


def encode_video(src, dst, fmt, progress, cancel, info=None):
    global _GOOD_H264
    spec = VIDEO_FORMATS[fmt]
    base = _base_cmd(src)
    tail = ["-map_metadata", "0"] + spec["extra"] + [str(dst)]

    if spec["v"] == "gif":
        run_ffmpeg(base + GIF_ARGS + tail, 0, progress, cancel, label="Делаю GIF…")
        return
    try:
        info = info or file_info(src)
    except Exception:
        info = None
    if can_copy(info, fmt):  # мгновенно: потоки просто перекладываются в новый контейнер
        try:
            run_ffmpeg(base + ["-map", "0:v:0", "-map", "0:a?", "-c", "copy"] + tail,
                       0, progress, cancel, label="Без перекодирования ⚡")
            return
        except ConvError:
            pass
    maps = ["-map", "0:v:0", "-map", "0:a?"]
    if spec["v"] == "vp9":
        run_ffmpeg(base + maps + VP9 + spec["a"] + tail, 0, progress, cancel, label="Кодирую на процессоре…")
        return
    last = None
    for key, where, args in _h264_variants():
        try:
            run_ffmpeg(base + maps + args + spec["a"] + tail, 0, progress, cancel,
                       label=f"Кодирую на {where}…")
            _GOOD_H264 = key
            return
        except ConvError as e:
            last = e
    raise last


# ---------- картинки ----------
def convert_image(src: Path, fmt: str, quality: int, opts: dict) -> Path:
    from PIL import Image, ImageOps

    spec = IMAGE_FORMATS[fmt]
    with _output(_dest(src, spec["ext"], opts)) as dst:
        with Image.open(src) as im:
            im.load()
            im = ImageOps.exif_transpose(im)
            pil = spec["pil"]
            kwargs: dict = {}
            if pil in ("JPEG", "BMP", "PDF"):
                if im.mode in ("RGBA", "LA", "P"):
                    rgba = im.convert("RGBA")
                    bg = Image.new("RGB", rgba.size, (255, 255, 255))
                    bg.paste(rgba, mask=rgba.split()[3])
                    im = bg
                else:
                    im = im.convert("RGB")
            if pil == "JPEG":
                kwargs = dict(quality=quality, optimize=True)
            elif pil == "WEBP":
                kwargs = dict(quality=quality, method=4)
            elif pil == "PNG":
                kwargs = dict(optimize=False, compress_level=6)
            elif pil == "TIFF":
                kwargs = dict(compression="tiff_lzw")
            elif pil == "ICO":
                im = im.convert("RGBA")
                kwargs = dict(sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
            elif pil == "PDF":
                kwargs = dict(resolution=100.0)
            im.save(dst, pil, **kwargs)
    return dst


# ---------- акапелла (Demucs) ----------
def _python() -> str:
    py = sys.executable
    if py.lower().endswith("pythonw.exe"):
        py = py[: -len("pythonw.exe")] + "python.exe"
    return py


_warmed = False


def warm_demucs():
    """Фоновый прогрев: подгружает PyTorch/Demucs в кэш диска и заранее скачивает модель."""
    global _warmed
    if _warmed or importlib.util.find_spec("demucs") is None:
        return
    _warmed = True
    code = ("import torch\ntry:\n from demucs.pretrained import get_model\n get_model('htdemucs')\n"
            "except Exception:\n pass\n")
    try:
        subprocess.Popen([_python(), "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=NO_WINDOW | LOW_PRIO)
    except OSError:
        pass


def separate_vocals(src: Path, fmt: str, bitrate: int, opts: dict, progress: Progress,
                    cancel: threading.Event) -> list[Path]:
    if importlib.util.find_spec("demucs") is None:
        raise ConvError("Для акапеллы нужен Demucs.\nУстанови:  python -m pip install demucs\n"
                        "(при первом запуске он сам скачает модель ~80 МБ).")
    ffmpeg = find_tool("ffmpeg")
    ext = AUDIO_FORMATS[fmt]["ext"]
    tmp = Path(tempfile.mkdtemp(prefix="converter_"))
    dsts: list[Path] = []
    try:
        wav = tmp / "track.wav"
        run_ffmpeg([ffmpeg, "-hide_banner", "-y", "-nostdin", "-progress", "pipe:1", "-nostats",
                    "-i", str(src), "-vn", "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", str(wav)],
                   0, progress, cancel, 0.0, 0.05, "Подготовка…")

        env = {**os.environ, "PYTHONIOENCODING": "utf-8",
               "PATH": str(Path(ffmpeg).parent) + os.pathsep + os.environ.get("PATH", "")}
        cmd = [_python(), "-m", "demucs", "--two-stems=vocals", "-n", "htdemucs", "--overlap", "0.1",
               "-o", str(tmp / "out"), str(wav)]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                env=env, creationflags=NO_WINDOW)
        tail = ""
        while True:
            if cancel.is_set():
                proc.terminate()
                raise Cancelled()
            chunk = os.read(proc.stderr.fileno(), 4096)
            if not chunk:
                break
            txt = chunk.decode("utf-8", "replace")
            tail = (tail + txt)[-1500:]
            found = re.findall(r"(\d{1,3})%", txt)
            if found:
                progress(0.05 + 0.85 * min(int(found[-1]), 100) / 100,
                         "Нейросеть делит трек на вокал и минус…")
        if proc.wait() != 0:
            raise ConvError("Demucs завершился с ошибкой:\n" + tail[-500:])

        stems = tmp / "out" / "htdemucs" / "track"
        pairs = []
        for label, name in (("акапелла", "vocals.wav"), ("минус", "no_vocals.wav")):
            f = stems / name
            if not f.exists():
                raise ConvError("Demucs не создал файл " + name + "\n" + tail[-300:])
            d = _dest(src, ext, opts, f" ({label})")
            dsts.append(d)
            pairs.append((f, d))
        # оба файла кодируются одновременно
        with ThreadPoolExecutor(2) as ex:
            futs = [ex.submit(encode_audio, f, d, fmt, bitrate, False, progress, cancel,
                              0.90, 0.10, "Сохраняю результат…") for f, d in pairs]
            for fut in futs:
                fut.result()
        return dsts
    except BaseException:
        for d in dsts:
            try:
                d.unlink()
            except OSError:
                pass
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def workers_for(cat: str) -> int:
    cpu = os.cpu_count() or 2
    return 2 if cat == "video" else max(1, min(4, cpu // 2 or 1))


def convert(src, mode: str, fmt: str, opts: dict, progress: Progress,
            cancel: threading.Event) -> list[Path]:
    """mode: audio | video | extract | image | split"""
    src = Path(src)
    br = int(opts.get("bitrate", 320))
    if mode == "image":
        progress(0.0, "Конвертирую изображение…")
        out = convert_image(src, fmt, int(opts.get("quality", 92)), opts)
        progress(1.0, "")
        return [out]
    if mode in ("audio", "extract"):
        with _output(_dest(src, AUDIO_FORMATS[fmt]["ext"], opts)) as dst:
            encode_audio(src, dst, fmt, br, mode == "audio", progress, cancel,
                         label="Извлекаю звук…" if mode == "extract" else "Конвертирую аудио…")
        return [dst]
    if mode == "video":
        with _output(_dest(src, VIDEO_FORMATS[fmt]["ext"], opts)) as dst:
            encode_video(src, dst, fmt, progress, cancel)
        return [dst]
    if mode == "split":
        return separate_vocals(src, opts.get("stem_fmt", "FLAC"), br, opts, progress, cancel)
    raise ConvError("Неизвестный режим: " + mode)
