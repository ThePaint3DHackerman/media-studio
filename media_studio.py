"""
Media Studio - convert media and compress files or entire folders with a friendly GUI.
Powered by FFmpeg (downloaded automatically the first time on Windows).
"""
import hashlib
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

try:  # optional: enables drag-and-drop (pip install tkinterdnd2)
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except ImportError:
    HAS_DND = False

APP_NAME = "Media Studio"
NOWIN = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
NULL_DEV = "NUL" if os.name == "nt" else "/dev/null"
_BTBN = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
COMPAT_FFMPEG_URL = "https://github.com/GyanD/codexffmpeg/releases/download/7.1.1/ffmpeg-7.1.1-full_build.zip"
COMPAT_FFMPEG_SHA256 = "d760e1b3574402ed18b4865851f87d87e73965a982e6453212df8621fed1c508"
FFMPEG_URLS = [  # tried in order; the first one that works wins
    COMPAT_FFMPEG_URL,  # pinned build works with older NVIDIA drivers and includes all app codecs
    _BTBN + "ffmpeg-n9.0-latest-win64-gpl-9.0.zip",
    _BTBN + "ffmpeg-n8.1-latest-win64-gpl-8.1.zip",
    _BTBN + "ffmpeg-master-latest-win64-gpl.zip",
]

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".wmv", ".m4v"}
AUDIO_EXT = {".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".opus", ".wma"}
ALL_EXT = VIDEO_EXT | AUDIO_EXT

CONVERT_FORMATS = [
    ("MP4 video (.mp4)", ".mp4"), ("MOV video (.mov)", ".mov"),
    ("MKV video (.mkv)", ".mkv"), ("AVI video (.avi)", ".avi"),
    ("WebM video (.webm)", ".webm"),
    ("MP3 audio (.mp3)", ".mp3"), ("WAV audio (.wav)", ".wav"),
    ("FLAC audio (.flac)", ".flac"), ("M4A / AAC audio (.m4a)", ".m4a"),
    ("OGG audio (.ogg)", ".ogg"), ("Opus audio (.opus)", ".opus"),
]
VIDEO_OUT = {".mp4", ".mov", ".mkv", ".avi", ".webm"}

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif", ".img"}
IMAGE_OUT_FORMATS = [
    ("JPEG image (.jpg)", ".jpg"), ("PNG image (.png)", ".png"),
    ("WebP image (.webp)", ".webp"), ("BMP image (.bmp)", ".bmp"),
    ("TIFF image (.tiff)", ".tiff"), ("GIF image (.gif)", ".gif"),
]
IMAGE_CODECS = {"mjpeg", "mjpegb", "png", "apng", "webp", "bmp", "tiff", "gif", "gem"}


AUDIO_CODEC_ARGS = {
    ".mp3": ["-c:a", "libmp3lame", "-b:a", "320k"],
    ".wav": ["-c:a", "pcm_s16le"],
    ".flac": ["-c:a", "flac"],
    ".m4a": ["-c:a", "aac", "-b:a", "256k"],
    ".aac": ["-c:a", "aac", "-b:a", "256k"],
    ".ogg": ["-c:a", "libvorbis", "-q:a", "6"],
    ".opus": ["-c:a", "libopus", "-b:a", "192k"],
}

VIDEO_CODECS = {
    "H.265 / HEVC  (great balance)": "x265",
    "AV1  (smallest files, slower)": "av1",
    "H.264  (plays everywhere)": "x264",
    "VP9  (WebM)": "vp9",
}
AUDIO_CODECS = {
    "Opus  (best quality per MB)": "opus",
    "AAC  (.m4a)": "aac",
    "MP3  (plays everywhere)": "mp3",
}
SPEEDS = {"Fast  (uses your GPU if it can)": "fast", "Balanced": "balanced",
          "Best quality  (slowest)": "best"}
PRESETS = {
    "fast":     {"x264": "veryfast", "x265": "veryfast", "vp9": "5", "av1": "10"},
    "balanced": {"x264": "medium",   "x265": "fast",     "vp9": "3", "av1": "8"},
    "best":     {"x264": "slow",     "x265": "medium",   "vp9": "2", "av1": "6"},
}
RESOLUTIONS = {"Keep original": 0, "Max 1080p": 1080, "Max 720p": 720,
               "Max 480p": 480, "Max 360p": 360}

# Remux (copy streams, no re-encode) is instant and lossless. These are the source
# codecs each container can hold as-is. None = any audio codec works.
COPY_OK = {
    ".mp4": ({"h264", "hevc", "av1"}, {"aac", "mp3", "ac3", "eac3", "alac"}),
    ".mov": ({"h264", "hevc"}, {"aac", "mp3", "alac", "ac3"}),
    ".mkv": ({"h264", "hevc", "vp9", "vp8", "av1", "mpeg4"}, None),
    ".webm": ({"vp8", "vp9", "av1"}, {"opus", "vorbis"}),
}
AUDIO_COPY = {".m4a": {"aac"}, ".mp3": {"mp3"}, ".flac": {"flac"},
              ".opus": {"opus"}, ".ogg": {"vorbis"}}

# GPU encoders, fastest first, per codec family. A listed encoder isn't proof it works
# (driver or GPU may lack it), so hw_encoder() test-encodes before using one.
HW_CANDIDATES = {
    "x264": ["h264_nvenc", "h264_qsv", "h264_amf"],
    "x265": ["hevc_nvenc", "hevc_qsv", "hevc_amf"],
    "av1": ["av1_nvenc", "av1_qsv", "av1_amf"],
}
HW_ARGS = {
    # Fast favors throughput; the output-size check below corrects rate-control overshoot.
    "h264_nvenc": ["-preset", "p2", "-tune", "hq", "-rc", "vbr", "-multipass", "disabled", "-rc-lookahead", "0"],
    "hevc_nvenc": ["-preset", "p2", "-tune", "hq", "-rc", "vbr", "-multipass", "disabled", "-rc-lookahead", "0"],
    "av1_nvenc": ["-preset", "p2", "-tune", "hq", "-rc", "vbr", "-multipass", "disabled", "-rc-lookahead", "0"],
    "h264_qsv": ["-preset", "veryfast"],
    "hevc_qsv": ["-preset", "veryfast"],
    "av1_qsv": ["-preset", "veryfast"],
    "h264_amf": ["-quality", "speed", "-rc", "vbr_latency"],
    "hevc_amf": ["-quality", "speed", "-rc", "vbr_latency"],
    "av1_amf": ["-quality", "speed", "-rc", "vbr_latency"],
}
HWDEC = ["-hwaccel", "auto"]  # GPU decodes the input when it can, CPU otherwise

MIB = 1024 * 1024  # what Windows Explorer calls "MB"
DEFAULT_TARGET_MB = 9.5  # 9,961,472 bytes: below both decimal 10 MB and 10 MiB


# --------------------------------------------------------------------------- #
#  Tool discovery / download
# --------------------------------------------------------------------------- #
def app_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def tools_dir():
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "MediaStudio" / "ffmpeg"


def find_tool(name):
    exe = name + (".exe" if os.name == "nt" else "")
    for d in (app_dir(), tools_dir() / "compatible", tools_dir()):
        p = d / exe
        if p.exists():
            return str(p)
    return shutil.which(name)


def _download_one(url, zpath, progress_cb):
    req = urllib.request.Request(url, headers={"User-Agent": "MediaStudio"})
    with urllib.request.urlopen(req, timeout=60) as r, open(zpath, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if total:
                progress_cb(got / total)


def download_ffmpeg(progress_cb):
    dest = tools_dir()
    dest.mkdir(parents=True, exist_ok=True)
    zpath = dest / "ffmpeg.zip"
    last_err = None
    for attempt in range(2):          # two sweeps over the mirror list
        for url in FFMPEG_URLS:
            try:
                _download_one(url, zpath, progress_cb)
                if url == COMPAT_FFMPEG_URL:
                    digest = hashlib.sha256()
                    with zpath.open("rb") as archive:
                        for chunk in iter(lambda: archive.read(1024 * 1024), b""):
                            digest.update(chunk)
                    if digest.hexdigest() != COMPAT_FFMPEG_SHA256:
                        raise ValueError("FFmpeg download failed its integrity check.")
                last_err = None
                break
            except Exception as e:    # 404 during a daily rebuild, network blip, etc.
                last_err = e
        if last_err is None:
            break
        time.sleep(4)
    if last_err is not None:
        raise last_err
    with zipfile.ZipFile(zpath) as z:
        for member in z.namelist():
            base = os.path.basename(member).lower()
            if base in ("ffmpeg.exe", "ffprobe.exe"):
                with z.open(member) as src, open(dest / base, "wb") as out:
                    shutil.copyfileobj(src, out)
    zpath.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
def fmt_time(sec):
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_size(n):
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f} GB"
    return f"{n / MIB:.2f} MB"


def _within(path, directory):
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


def collect_media_paths(source, output_root=None, cancelled=None, extensions=None):
    """Snapshot paths only; never copy media or follow linked directories."""
    source = Path(source).resolve()
    output_root = Path(output_root).resolve() if output_root else None
    cancelled = cancelled or (lambda: False)
    extensions = ALL_EXT if extensions is None else {e.lower() for e in extensions}
    files, warnings = [], []
    if not source.is_dir():
        raise ValueError("Choose an existing input folder.")
    if output_root == source:
        raise ValueError("Choose an output folder different from the input folder.")
    excluded_output = output_root if output_root and _within(output_root, source) else None

    def walk_error(error):
        warnings.append(str(error))

    for directory, dirs, names in os.walk(source, onerror=walk_error, followlinks=False):
        if cancelled():
            break
        parent = Path(directory)
        keep = []
        for name in sorted(dirs):
            if cancelled():
                break
            child = parent / name
            try:
                linked = child.is_symlink() or getattr(child, "is_junction", lambda: False)()
                if not linked and not (excluded_output and _within(child.resolve(), excluded_output)):
                    keep.append(name)
            except OSError as error:
                warnings.append(str(error))
        dirs[:] = keep
        for name in sorted(names):
            if cancelled():
                break
            path = parent / name
            if path.suffix.lower() in extensions and not path.is_symlink():
                files.append(path)
    return files, warnings


def reserve_batch_output(source, source_root, output_root, extension, action="compressed"):
    """Claim a fresh destination without overwriting existing files or source media."""
    relative = Path(source).relative_to(source_root)
    folder = Path(output_root) / relative.parent
    # A linked destination subfolder must not send output outside the chosen folder.
    if not _within(folder.resolve(), Path(output_root).resolve()):
        raise ValueError(f"Output subfolder points outside the chosen folder: {folder}")
    folder.mkdir(parents=True, exist_ok=True)
    stem = relative.stem + "_" + action
    number = 1
    while True:
        name = stem if number == 1 else f"{stem}_{number}"
        destination = folder / (name + extension)
        try:
            with destination.open("xb"):
                pass
            return destination
        except FileExistsError:
            number += 1


def image_input_args(path):
    """Recognize common raster headers without trusting the filename or copying data."""
    with Path(path).open("rb") as source:
        header = source.read(32)
    image_format = None
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        image_format = "png_pipe"
    elif header.startswith(b"\xff\xd8\xff"):
        image_format = "jpeg_pipe"
    elif header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        image_format = "webp_pipe"
    elif header.startswith(b"BM"):
        image_format = "bmp_pipe"
    elif header.startswith((b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")):
        image_format = "tiff_pipe"
    elif header.startswith((b"GIF87a", b"GIF89a")):
        image_format = "gif"
    return ["-f", image_format] if image_format else []


def build_image_convert(ffmpeg, src, dst, info):
    """Convert actual raster data, saving the first frame of animated inputs.

    The input extension is never trusted: a PNG called .img can be converted,
    while a disk image or unsupported .img is rejected instead of renamed.
    """
    ext = Path(dst).suffix.lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif"}:
        raise ValueError("Choose JPG, PNG, WebP, BMP, TIFF, or GIF for image output. "
                         ".img does not identify a single image format.")
    hints = image_input_args(src)
    video_source = Path(src).suffix.lower() in {".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".wmv", ".m4v"}
    readable_picture = (bool(hints) or (info.get("has_video") and info.get("height", 0) > 0
                                       and (info.get("vcodec") in IMAGE_CODECS or video_source)))
    if not readable_picture:
        raise ValueError("This file is not a supported raster image. A .img file may be a disk image; "
                         "changing its extension cannot turn it into a photograph.")

    cmd = [str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
           "-progress", "pipe:1", "-nostats", *hints, "-i", str(src)]
    if ext in {".jpg", ".jpeg", ".bmp"}:
        # Composite transparency onto white at the source dimensions. Merely
        # dropping an alpha channel makes transparent areas unexpectedly black.
        cmd += ["-filter_complex",
                "[0:v:0]split[foreground][background];"
                "[background]format=rgb24,lutrgb=r=255:g=255:b=255[white];"
                "[white][foreground]overlay=shortest=1:format=auto,format=rgb24[picture]",
                "-map", "[picture]"]
    else:
        cmd += ["-map", "0:v:0"]
    cmd += ["-frames:v", "1", "-an", "-map_metadata", "-1"]
    if ext in {".jpg", ".jpeg"}:
        cmd += ["-c:v", "mjpeg", "-q:v", "2", "-pix_fmt", "yuvj444p"]
    elif ext == ".png":
        cmd += ["-c:v", "png"]
    elif ext == ".webp":
        cmd += ["-c:v", "libwebp", "-quality", "90", "-f", "webp"]
    elif ext == ".bmp":
        cmd += ["-c:v", "bmp", "-pix_fmt", "bgr24"]
    elif ext in {".tif", ".tiff"}:
        cmd += ["-c:v", "tiff"]
    else:
        cmd += ["-c:v", "gif", "-f", "gif"]
    if ext not in {".webp", ".gif"}:
        cmd += ["-f", "image2", "-update", "1"]
    cmd.append(str(dst))
    return [(cmd, 1.0)]


def probe(ffprobe, path):
    out = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries",
         "format=duration,size:stream=codec_type,codec_name,width,height:stream_disposition=attached_pic",
         "-of", "json", *image_input_args(path), str(path)],
        capture_output=True, text=True, creationflags=NOWIN)
    data = json.loads(out.stdout or "{}")
    streams = data.get("streams", [])
    vids = [s for s in streams if s.get("codec_type") == "video"
            and not s.get("disposition", {}).get("attached_pic", 0)]
    return {
        "duration": float(data.get("format", {}).get("duration") or 0),
        "size": int(data.get("format", {}).get("size") or os.path.getsize(path)),
        "has_video": bool(vids),
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
        "height": int(vids[0].get("height") or 0) if vids else 0,
        "vcodec": vids[0].get("codec_name", "") if vids else "",
        "acodec": next((s.get("codec_name", "") for s in streams
                        if s.get("codec_type") == "audio"), ""),
    }


def base_cmd(ffmpeg):
    return [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-progress", "pipe:1", "-nostats"]


_hw_cache = {}


def hw_encoder(ffmpeg, codec):
    """Fastest GPU encoder that really works on this machine for a codec family, or None.
    Each candidate gets a tiny test encode with the exact arguments it would use.
    Cached per FFmpeg path and codec, so a different build gets its own test."""
    cache_key = (str(ffmpeg), codec)
    if cache_key not in _hw_cache:
        found = None
        for enc in HW_CANDIDATES.get(codec, []):
            test = [ffmpeg, "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=black:s=640x360:d=0.4", "-frames:v", "8",
                    "-c:v", enc, "-b:v", "2M", *HW_ARGS[enc], "-f", "null", "-"]
            try:
                if subprocess.run(test, capture_output=True, timeout=20,
                                  creationflags=NOWIN).returncode == 0:
                    found = enc
                    break
            except (OSError, subprocess.TimeoutExpired):
                pass
        _hw_cache[cache_key] = found
    return _hw_cache[cache_key]


def can_copy(ext, info):
    """True when the streams can go into the new container as they are (no re-encode)."""
    if ext in VIDEO_OUT and info["has_video"]:
        if ext not in COPY_OK:
            return False
        vset, aset = COPY_OK[ext]
        audio_ok = (not info["has_audio"]) or aset is None or info["acodec"] in aset
        return info["vcodec"] in vset and audio_ok
    if ext in VIDEO_OUT:
        return False  # audio-only source needs a generated picture, so it must encode
    return info["has_audio"] and info["acodec"] in AUDIO_COPY.get(ext, set())


# --------------------------------------------------------------------------- #
#  Command builders
# --------------------------------------------------------------------------- #
def build_convert(ffmpeg, src, dst, info):
    ext = Path(dst).suffix.lower()
    src, dst = str(src), str(dst)
    cmd = base_cmd(ffmpeg)
    copy = can_copy(ext, info)  # remux when the codecs already fit: instant, zero loss

    if ext in VIDEO_OUT:
        webm = ext == ".webm"
        acodec = ["-c:a", "libopus", "-b:a", "192k"] if webm else (
            ["-c:a", "libmp3lame", "-b:a", "192k"] if ext == ".avi"
            else ["-c:a", "aac", "-b:a", "192k"])
        if info["has_video"]:
            cmd += ([] if copy else HWDEC) + ["-i", src, "-map", "0:v:0"]
            if info["has_audio"]:
                cmd += ["-map", "0:a:0"]
            if copy:
                cmd += ["-c", "copy"]
            elif webm:
                cmd += ["-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0",
                        "-row-mt", "1", "-deadline", "good", "-cpu-used", "4"]
            else:
                cmd += ["-c:v", "libx264", "-preset", "faster", "-crf", "20",
                        "-pix_fmt", "yuv420p"]
            if info["has_audio"] and not copy:
                cmd += acodec
        else:
            # audio-only source -> video with a black screen
            cmd += ["-f", "lavfi", "-i", "color=c=black:s=1280x720:r=10",
                    "-i", src, "-map", "0:v", "-map", "1:a:0", "-shortest"]
            if webm:
                cmd += ["-c:v", "libvpx-vp9", "-crf", "45", "-b:v", "0",
                        "-deadline", "good", "-cpu-used", "5"]
            else:
                cmd += ["-c:v", "libx264", "-tune", "stillimage",
                        "-preset", "veryfast", "-crf", "30", "-pix_fmt", "yuv420p"]
            cmd += acodec
        if ext in (".mp4", ".mov"):
            cmd += ["-movflags", "+faststart"]
    else:
        cmd += ["-i", src, "-vn", "-map", "0:a:0", "-map_metadata", "0"]
        cmd += ["-c:a", "copy"] if copy else AUDIO_CODEC_ARGS[ext]

    cmd.append(dst)
    return [(cmd, 1.0)]


def compress_ext(codec_key, is_video):
    if is_video:
        return ".webm" if codec_key == "vp9" else ".mp4"
    return {"opus": ".opus", "aac": ".m4a", "mp3": ".mp3"}[codec_key]


def audio_kbps(total_kbps):
    return 128 if total_kbps > 600 else 64 if total_kbps > 250 else 32


def fit_picture(v_kbps, top_h):
    """(height, fps) for a video bitrate. Steps down through the standard heights until each
    pixel gets a fair share of bits (0.03 bits per pixel). If even 240p is starved, the frame
    rate halves too, because fewer, sharper frames look better than many mushy ones."""
    ladder = [top_h] + [h for h in (1080, 720, 480, 360, 240) if h < top_h]

    def bits_per_pixel(h, fps):
        return v_kbps * 1000 / (h * h * 16 / 9 * fps)

    height = next((h for h in ladder if bits_per_pixel(h, 30) >= 0.03), ladder[-1])
    fps = 30 if bits_per_pixel(height, 30) >= 0.015 else 15
    return height, fps


def plan_compress(ffmpeg, src, dst, info, target_mb, codec, scale_h, speed="fast", denoise=False):
    """Returns (list of (cmd, weight), notes). Raises ValueError if impossible."""
    src, dst = str(src), str(dst)
    dur = info["duration"]
    notes = ""

    if not info["has_video"]:
        # ---------- audio ----------
        kbps = int(target_mb * MIB * 8 * 0.97 / dur / 1000)
        limits = {"opus": (6, 510), "aac": (16, 320), "mp3": (8, 320)}[codec]
        if kbps < limits[0]:
            raise ValueError(
                f"{target_mb:g} MB is too small for {fmt_time(dur)} of audio "
                f"with this codec (would need {kbps} kbps, minimum is {limits[0]}).\n"
                "Pick a bigger size or a shorter file.")
        if kbps > limits[1]:
            notes = (f"Your target is bigger than this codec can use, so the file "
                     f"will come out around {limits[1] * dur * 1000 / 8 / MIB:.1f} MB.")
            kbps = limits[1]
        enc = {"opus": ["-c:a", "libopus", "-vbr", "constrained"],
               "aac": ["-c:a", "aac"],
               "mp3": ["-c:a", "libmp3lame"]}[codec]
        cmd = base_cmd(ffmpeg) + ["-i", src, "-vn", "-map", "0:a:0"] + enc + \
            ["-b:a", f"{kbps}k", dst]
        return [(cmd, 1.0)], notes

    # ---------- video ----------
    margin = 0.92 if codec == "av1" else 0.96
    total_kbps = target_mb * MIB * 8 * margin / dur / 1000
    a_kbps = audio_kbps(total_kbps) if info["has_audio"] else 0
    v_kbps = int(total_kbps - a_kbps)
    if v_kbps < 10:  # below this, not even 240p can show anything
        raise ValueError(
            f"{target_mb:g} MB is too small for {fmt_time(dur)} of video, even at 240p "
            f"(only {max(v_kbps, 0)} kbps left for the picture).\n"
            "Pick a bigger size.")

    # Pick the sharpest picture this size can show. Pixels starved of bits look worse than
    # fewer pixels with enough bits, so the resolution (and, if needed, frame rate) steps down.
    src_h = info["height"] or 99999
    top = min(scale_h or 99999, src_h)
    height, fps = fit_picture(v_kbps, top)
    auto_note = ""
    if height < top or fps < 30:
        auto_note = f"To fit {target_mb:g} MB the picture is {height}p at {fps} fps."
        if height <= 360 or fps < 30:
            auto_note += " Expect it to look soft."

    pr = PRESETS[speed]
    filters = []
    if denoise:  # grain/noise eats bits; removing it first makes the same size look sharper
        filters.append("hqdn3d=1.5:1.5:3:3")
    if height < src_h:
        filters.append(f"scale=-2:'min({height},ih)'")
    if fps < 30:
        filters.append("fps=15")
    vf = ["-vf", ",".join(filters)] if filters else []
    inp = HWDEC + ["-i", src, "-map", "0:v:0"] + (["-map", "0:a:0"] if info["has_audio"] else [])

    if codec == "vp9":
        venc = ["-c:v", "libvpx-vp9", "-b:v", f"{v_kbps}k", "-row-mt", "1",
                "-deadline", "good", "-cpu-used", pr["vp9"]]
        aenc = ["-c:a", "libopus", "-b:a", f"{a_kbps}k"]
    elif codec == "av1":
        venc = ["-c:v", "libsvtav1", "-b:v", f"{v_kbps}k", "-preset", pr["av1"]]
        aenc = ["-c:a", "aac", "-b:a", f"{a_kbps}k"]
    elif codec == "x265":
        venc = ["-c:v", "libx265", "-b:v", f"{v_kbps}k", "-preset", pr["x265"],
                "-tag:v", "hvc1"]
        aenc = ["-c:a", "aac", "-b:a", f"{a_kbps}k"]
    else:
        venc = ["-c:v", "libx264", "-b:v", f"{v_kbps}k", "-preset", pr["x264"]]
        aenc = ["-c:a", "aac", "-b:a", f"{a_kbps}k"]
    if not info["has_audio"]:
        aenc = []
    tail = ["-pix_fmt", "yuv420p"] + (["-movflags", "+faststart"] if dst.endswith(".mp4") else [])

    hw = hw_encoder(ffmpeg, codec) if speed == "fast" else None
    if hw:  # GPU path: one pass, far quicker. maxrate keeps busy scenes from overshooting.
        venc_hw = ["-c:v", hw, "-b:v", f"{v_kbps}k", "-maxrate", f"{int(v_kbps * 1.3)}k",
                   "-bufsize", f"{v_kbps * 2}k"] + HW_ARGS[hw]
        cmd = base_cmd(ffmpeg) + inp + vf + venc_hw + tail + aenc + [dst]
        hw_note = (f"Using your GPU ({hw}) with a fast preset. "
                   "The final file size is checked before saving.")
        return [(cmd, 1.0)], " ".join(x for x in (auto_note, hw_note) if x)

    if codec == "av1":
        cmd = base_cmd(ffmpeg) + inp + vf + venc + tail + aenc + [dst]
        av1_note = "AV1 is single-pass, so the final size may land a little under your target."
        return [(cmd, 1.0)], " ".join(x for x in (auto_note, av1_note) if x)

    # two-pass for exact sizing (stats files live in the job's temp dir = cwd)
    if codec == "x265":
        p1x, p2x = ["-x265-params", "pass=1:no-slow-firstpass=1"], ["-x265-params", "pass=2"]
    else:
        p1x, p2x = ["-pass", "1", "-passlogfile", "ffpass"], ["-pass", "2", "-passlogfile", "ffpass"]
    # pass 1 only measures how hard each scene is, so x265 and VP9 run it on a faster preset.
    # x264 must use identical settings in both passes, so it keeps its preset (ffmpeg already
    # gives x264 a fast first pass on its own).
    venc1 = list(venc)
    if codec == "vp9":
        venc1[venc1.index("-cpu-used") + 1] = "5"
    elif codec == "x265":
        venc1[venc1.index("-preset") + 1] = "veryfast"
    pass1 = base_cmd(ffmpeg) + inp + vf + venc1 + ["-pix_fmt", "yuv420p"] + p1x + \
        ["-an", "-f", "null", NULL_DEV]
    pass2 = base_cmd(ffmpeg) + inp + vf + venc + tail + p2x + aenc + [dst]
    return [(pass1, 0.4), (pass2, 0.6)], auto_note


# --------------------------------------------------------------------------- #
#  GUI
# --------------------------------------------------------------------------- #
def run_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    BG, CARD, FG, MUTED = "#12141c", "#1c2030", "#eef0f7", "#8b92ab"
    ACCENT, ACCENT_H = "#6c8cff", "#8aa3ff"

    class App(TkinterDnD.Tk if HAS_DND else tk.Tk):
        def __init__(self):
            super().__init__()
            self.title(APP_NAME)
            self.geometry("900x880")
            self.minsize(860, 860)
            self.configure(bg=BG)
            self.q = queue.Queue()
            self.proc = None
            self.job_thread = None
            self.closing = False
            self.cancel = False
            self.busy = False
            self.last_out = None
            self.ffmpeg = self.ffprobe = None
            self.c_info = self.z_info = None
            self._style()
            self._build()
            self.after(80, self._poll)
            self.after(300, self._ensure_tools)
            self.protocol("WM_DELETE_WINDOW", self._close)

        # ---------- styling ----------
        def _style(self):
            s = ttk.Style(self)
            s.theme_use("clam")
            s.configure(".", background=BG, foreground=FG, fieldbackground=CARD,
                        font=("Segoe UI", 10), bordercolor=CARD, lightcolor=CARD,
                        darkcolor=CARD)
            s.configure("TFrame", background=BG)
            s.configure("Card.TFrame", background=CARD)
            s.configure("TLabel", background=BG, foreground=FG)
            s.configure("Card.TLabel", background=CARD, foreground=FG)
            s.configure("Muted.TLabel", background=CARD, foreground=MUTED)
            s.configure("Title.TLabel", font=("Segoe UI", 24, "bold"))
            s.configure("Sub.TLabel", foreground=MUTED, font=("Segoe UI", 11))
            s.configure("Status.TLabel", foreground=MUTED)
            s.configure("Card.TCheckbutton", background=CARD, foreground=FG)
            s.configure("TButton", background="#2a2f47", foreground=FG,
                        borderwidth=0, padding=(14, 7))
            s.map("TButton", background=[("active", "#383e5e"), ("disabled", "#20243a")],
                  foreground=[("disabled", "#5b627c")])
            s.configure("Accent.TButton", background=ACCENT, foreground="white",
                        padding=(22, 11), font=("Segoe UI", 11, "bold"))
            s.map("Accent.TButton", background=[("active", ACCENT_H), ("disabled", "#34395a")])
            s.configure("TNotebook", background=BG, borderwidth=0)
            s.configure("TNotebook.Tab", background=BG, foreground=MUTED,
                        padding=(26, 10), font=("Segoe UI", 11, "bold"), borderwidth=0)
            s.map("TNotebook.Tab", background=[("selected", CARD)],
                  foreground=[("selected", FG)])
            s.configure("TEntry", insertcolor=FG, padding=6)
            s.configure("TCombobox", arrowcolor=FG, padding=5,
                        selectbackground=CARD, selectforeground=FG)
            s.map("TCombobox", fieldbackground=[("readonly", CARD)],
                  foreground=[("readonly", FG)])
            s.configure("TSpinbox", arrowcolor=FG, padding=5)
            s.configure("Horizontal.TProgressbar", troughcolor=CARD,
                        background=ACCENT, thickness=12, borderwidth=0)
            self.option_add("*TCombobox*Listbox.background", CARD)
            self.option_add("*TCombobox*Listbox.foreground", FG)
            self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)

        # ---------- layout ----------
        def _build(self):
            head = ttk.Frame(self)
            head.pack(fill="x", padx=30, pady=(26, 8))
            ttk.Label(head, text="Welcome to Media Studio 👋", style="Title.TLabel").pack(anchor="w")
            sub = ("Drop a file or folder anywhere, or browse below. Originals stay in place." if HAS_DND
                   else "Convert and shrink your videos and music. Pick a tab to get started.")
            ttk.Label(head, style="Sub.TLabel", text=sub).pack(anchor="w", pady=(2, 0))

            nb = self.nb = ttk.Notebook(self)
            nb.pack(fill="both", expand=True, padx=30, pady=10)
            self.tab_conv = ttk.Frame(nb, style="Card.TFrame", padding=22)
            self.tab_comp = ttk.Frame(nb, style="Card.TFrame", padding=22)
            self.tab_folder = ttk.Frame(nb, style="Card.TFrame", padding=22)
            self.tab_batch_convert = ttk.Frame(nb, style="Card.TFrame", padding=22)
            nb.add(self.tab_conv, text="  Convert  ")
            nb.add(self.tab_comp, text="  Compress  ")
            nb.add(self.tab_batch_convert, text="  Convert folder  ")
            nb.add(self.tab_folder, text="  Compress folder  ")
            self._build_convert()
            self._build_compress()
            self._build_folder()
            self._build_folder_convert()

            if HAS_DND:  # the whole window is a drop zone, not just one box
                for w in (self, nb, self.tab_conv, self.tab_comp, self.tab_folder, self.tab_batch_convert):
                    w.drop_target_register(DND_FILES)
                    w.dnd_bind("<<Drop>>", self._on_drop)

            foot = ttk.Frame(self)
            foot.pack(fill="x", padx=30, pady=(0, 22))
            self.bar = ttk.Progressbar(foot, maximum=1.0)
            self.bar.pack(fill="x")
            row = ttk.Frame(foot)
            row.pack(fill="x", pady=(8, 0))
            self.status = tk.StringVar(value="Ready.")
            status_label = ttk.Label(row, textvariable=self.status, style="Status.TLabel",
                                     wraplength=560, justify="left")
            self.btn_open = ttk.Button(row, text="Open folder", command=self._open_folder, state="disabled")
            self.btn_open.pack(side="right")
            self.btn_cancel = ttk.Button(row, text="Cancel", command=self._cancel, state="disabled")
            self.btn_cancel.pack(side="right", padx=8)
            status_label.pack(side="left", fill="x", expand=True)
            row.bind("<Configure>", lambda event: status_label.configure(wraplength=max(220, event.width - 250)))

        def _file_row(self, parent, label, var, browse_cmd, row, save=False):
            ttk.Label(parent, text=label, style="Card.TLabel").grid(row=row, column=0, sticky="w", pady=(0, 4))
            e = ttk.Entry(parent, textvariable=var)
            e.grid(row=row + 1, column=0, sticky="ew", padx=(0, 10))
            ttk.Button(parent, text="Browse…" if not save else "Save as…",
                       command=browse_cmd).grid(row=row + 1, column=1)

        def _build_convert(self):
            t = self.tab_conv
            t.columnconfigure(0, weight=1)
            self.c_in = tk.StringVar()
            self.c_out = tk.StringVar()
            self.c_fmt = tk.StringVar(value=CONVERT_FORMATS[0][0])
            self._file_row(t, "1.  Choose a file", self.c_in, self._c_pick, 0)
            self.c_info_lbl = ttk.Label(t, text="Convert video, audio and pictures, including JPG, PNG and WebP.",
                                        style="Muted.TLabel", wraplength=640)
            self.c_info_lbl.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 18))
            ttk.Label(t, text="2.  Convert to", style="Card.TLabel").grid(row=3, column=0, sticky="w", pady=(0, 4))
            cb = ttk.Combobox(t, textvariable=self.c_fmt, state="readonly",
                              values=[f for f, _ in CONVERT_FORMATS + IMAGE_OUT_FORMATS], width=34)
            cb.grid(row=4, column=0, sticky="w")
            cb.bind("<<ComboboxSelected>>", lambda e: self._c_refresh())
            self.c_hint = ttk.Label(t, text="", style="Muted.TLabel", wraplength=640)
            self.c_hint.grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 18))
            self._file_row(t, "3.  Save to", self.c_out, self._c_save, 6, save=True)
            self.btn_convert = ttk.Button(t, text="Convert", style="Accent.TButton", command=self._start_convert)
            self.btn_convert.grid(row=9, column=0, columnspan=2, sticky="e", pady=(24, 0))

        def _build_compress(self):
            t = self.tab_comp
            t.columnconfigure(0, weight=1)
            self.z_in = tk.StringVar()
            self.z_out = tk.StringVar()
            self.z_mb = tk.StringVar(value=f"{DEFAULT_TARGET_MB:g}")
            self.z_codec = tk.StringVar()
            self.z_res = tk.StringVar(value="Keep original")
            self.z_speed = tk.StringVar(value=list(SPEEDS)[0])
            self.z_denoise = tk.BooleanVar(value=False)
            self._file_row(t, "1.  Choose a video or audio file", self.z_in, self._z_pick, 0)
            self.z_info_lbl = ttk.Label(t, text="Default: under 10 MB per file. Output size is checked after encoding.",
                                       style="Muted.TLabel", wraplength=640)
            self.z_info_lbl.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 16))

            opts = ttk.Frame(t, style="Card.TFrame")
            opts.grid(row=3, column=0, columnspan=2, sticky="w")
            ttk.Label(opts, text="2.  Target size (MB)", style="Card.TLabel").grid(row=0, column=0, sticky="w")
            ttk.Label(opts, text="Compression", style="Card.TLabel").grid(row=0, column=1, sticky="w", padx=(18, 0))
            ttk.Label(opts, text="Resolution", style="Card.TLabel").grid(row=0, column=2, sticky="w", padx=(18, 0))
            ttk.Spinbox(opts, from_=0.1, to=100000, increment=1, textvariable=self.z_mb,
                        width=9).grid(row=1, column=0, sticky="w", pady=(4, 0))
            self.z_codec_cb = ttk.Combobox(opts, textvariable=self.z_codec, state="readonly", width=30,
                                           values=list(VIDEO_CODECS))
            self.z_codec_cb.grid(row=1, column=1, padx=(18, 0), pady=(4, 0))
            self.z_codec_cb.bind("<<ComboboxSelected>>", lambda e: self._z_refresh())
            self.z_res_cb = ttk.Combobox(opts, textvariable=self.z_res, state="readonly", width=14,
                                         values=list(RESOLUTIONS))
            self.z_res_cb.grid(row=1, column=2, padx=(18, 0), pady=(4, 0))
            self.z_res_cb.bind("<<ComboboxSelected>>", lambda e: self._z_hint())
            self.z_codec.set(list(VIDEO_CODECS)[0])
            self.z_speed_lbl = ttk.Label(opts, text="Speed", style="Card.TLabel")
            self.z_speed_lbl.grid(row=2, column=0, sticky="w", pady=(12, 0))
            self.z_speed_cb = ttk.Combobox(opts, textvariable=self.z_speed, state="readonly", width=24,
                                           values=list(SPEEDS))
            self.z_speed_cb.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
            self.z_denoise_cb = ttk.Checkbutton(opts, text="Smooth grain first (usually smaller files)",
                                                variable=self.z_denoise, style="Card.TCheckbutton")
            self.z_denoise_cb.grid(row=3, column=2, sticky="w", padx=(18, 0), pady=(4, 0))
            self.z_mb.trace_add("write", lambda *a: self._z_hint())

            self.z_hint = ttk.Label(t, text="", style="Muted.TLabel", wraplength=640)
            self.z_hint.grid(row=4, column=0, columnspan=2, sticky="w", pady=(10, 14))
            self._file_row(t, "3.  Save to", self.z_out, self._z_save, 5, save=True)
            self.btn_compress = ttk.Button(t, text="Compress", style="Accent.TButton", command=self._start_compress)
            self.btn_compress.grid(row=8, column=0, columnspan=2, sticky="e", pady=(24, 0))

        def _build_folder(self):
            t = self.tab_folder
            t.columnconfigure(0, weight=1)
            self.b_in = tk.StringVar()
            self.b_out = tk.StringVar()
            self.b_mb = tk.StringVar(value=f"{DEFAULT_TARGET_MB:g}")
            self.b_codec = tk.StringVar(value=list(VIDEO_CODECS)[0])
            self.b_audio = tk.StringVar(value=list(AUDIO_CODECS)[0])
            self.b_res = tk.StringVar(value="Keep original")
            self.b_speed = tk.StringVar(value=list(SPEEDS)[0])
            self.b_denoise = tk.BooleanVar(value=False)
            self._file_row(t, "1.  Input folder (includes subfolders)", self.b_in, self._b_pick, 0)
            ttk.Label(t, text="Only file locations are queued. Source files are never copied or changed.",
                      style="Muted.TLabel", wraplength=700).grid(
                          row=2, column=0, columnspan=2, sticky="w", pady=(6, 14))
            self._file_row(t, "2.  Output folder", self.b_out, self._b_save, 3)
            ttk.Label(t, text="Subfolders are preserved. Existing files are kept; new names get a number.",
                      style="Muted.TLabel", wraplength=700).grid(
                          row=5, column=0, columnspan=2, sticky="w", pady=(6, 16))
            opts = ttk.Frame(t, style="Card.TFrame")
            opts.grid(row=6, column=0, columnspan=2, sticky="ew")
            ttk.Label(opts, text="3.  Target MB per file", style="Card.TLabel").grid(row=0, column=0, sticky="w")
            ttk.Spinbox(opts, from_=0.1, to=100000, increment=1, textvariable=self.b_mb,
                        width=12).grid(row=1, column=0, sticky="w", pady=(4, 12))
            ttk.Label(opts, text="Video compression", style="Card.TLabel").grid(row=0, column=1, sticky="w", padx=(20, 0))
            ttk.Combobox(opts, textvariable=self.b_codec, state="readonly", width=33,
                         values=list(VIDEO_CODECS)).grid(row=1, column=1, sticky="w", padx=(20, 0), pady=(4, 12))
            ttk.Label(opts, text="Audio compression", style="Card.TLabel").grid(row=2, column=0, sticky="w")
            ttk.Combobox(opts, textvariable=self.b_audio, state="readonly", width=28,
                         values=list(AUDIO_CODECS)).grid(row=3, column=0, sticky="w", pady=(4, 12))
            ttk.Label(opts, text="Video resolution", style="Card.TLabel").grid(row=2, column=1, sticky="w", padx=(20, 0))
            ttk.Combobox(opts, textvariable=self.b_res, state="readonly", width=20,
                         values=list(RESOLUTIONS)).grid(row=3, column=1, sticky="w", padx=(20, 0), pady=(4, 12))
            ttk.Label(opts, text="Speed", style="Card.TLabel").grid(row=4, column=0, sticky="w")
            ttk.Combobox(opts, textvariable=self.b_speed, state="readonly", width=30,
                         values=list(SPEEDS)).grid(row=5, column=0, sticky="w", pady=(4, 0))
            ttk.Checkbutton(opts, text="Smooth grain first", variable=self.b_denoise,
                            style="Card.TCheckbutton").grid(row=5, column=1, sticky="w", padx=(20, 0))
            ttk.Label(t, text="Default: EACH file stays under 10 MB. Size is verified; oversized encodes retry at a lower bitrate.\n"
                      "Fast uses your GPU when supported. Files already below the target are skipped.",
                      style="Muted.TLabel", wraplength=700).grid(
                          row=7, column=0, columnspan=2, sticky="w", pady=(14, 0))
            self.btn_folder = ttk.Button(t, text="Compress folder", style="Accent.TButton", command=self._start_folder)
            self.btn_folder.grid(row=8, column=0, columnspan=2, sticky="e", pady=(20, 0))

        def _build_folder_convert(self):
            t = self.tab_batch_convert
            t.columnconfigure(0, weight=1)
            self.f_in = tk.StringVar()
            self.f_out = tk.StringVar()
            self.f_ext = tk.StringVar(value=".wav")
            self.f_fmt = tk.StringVar(value="MP3 audio (.mp3)")
            self._file_row(t, "1.  Input folder (includes subfolders)", self.f_in, self._f_pick, 0)
            ttk.Label(t, text="Match one extension and convert every matching file. Originals stay in place.",
                      style="Muted.TLabel", wraplength=700).grid(
                          row=2, column=0, columnspan=2, sticky="w", pady=(6, 18))
            opts = ttk.Frame(t, style="Card.TFrame")
            opts.grid(row=3, column=0, columnspan=2, sticky="w")
            ttk.Label(opts, text="2.  Files ending in", style="Card.TLabel").grid(row=0, column=0, sticky="w")
            ttk.Combobox(opts, textvariable=self.f_ext, width=16,
                         values=sorted(ALL_EXT | IMAGE_EXT)).grid(row=1, column=0, sticky="w", pady=(4, 0))
            ttk.Label(opts, text="Convert to", style="Card.TLabel").grid(row=0, column=1, sticky="w", padx=(24, 0))
            ttk.Combobox(opts, textvariable=self.f_fmt, state="readonly", width=34,
                         values=[label for label, ext in CONVERT_FORMATS + IMAGE_OUT_FORMATS]).grid(
                             row=1, column=1, sticky="w", padx=(24, 0), pady=(4, 0))
            ttk.Label(t, text="Examples: .wav → MP3, .webp → JPG, .png → WebP. You can type an input extension.\n"
                      "Picture outputs use the first frame. JPG uses a white background for transparency.\n"
                      "An .img input must contain a supported picture; disk images cannot become JPG photos.",
                      style="Muted.TLabel", wraplength=700).grid(
                          row=4, column=0, columnspan=2, sticky="w", pady=(12, 20))
            self._file_row(t, "3.  Output folder", self.f_out, self._f_save, 5)
            ttk.Label(t, text="Real format conversion. Subfolders are preserved; existing files get unique new names.",
                      style="Muted.TLabel", wraplength=700).grid(
                          row=7, column=0, columnspan=2, sticky="w", pady=(8, 0))
            self.btn_folder_convert = ttk.Button(t, text="Convert matching files", style="Accent.TButton",
                                                  command=self._start_folder_convert)
            self.btn_folder_convert.grid(row=8, column=0, columnspan=2, sticky="e", pady=(24, 0))

        def _f_pick(self, p=None):
            if self.busy:
                return
            if p is None:
                p = filedialog.askdirectory(title="Choose a folder to convert", mustexist=True)
            if not p:
                return
            source = Path(p).expanduser().resolve()
            if not source.is_dir():
                messagebox.showerror(APP_NAME, "Choose an existing input folder.")
                return
            self.f_in.set(str(source))
            if not self.f_out.get():
                self.f_out.set(str(source.with_name(source.name + "_converted")) if source.name
                               else str(Path.home() / "MediaStudio_converted"))
            self.nb.select(self.tab_batch_convert)
            self.status.set("Folder selected. Choose the input extension, output format and destination.")

        def _f_save(self):
            p = filedialog.askdirectory(title="Save converted files in…", mustexist=False,
                                        initialdir=self.f_out.get() or None)
            if p:
                self.f_out.set(p)

        def _start_folder_convert(self):
            if self.busy or not self._need_tools():
                return
            try:
                if not self.f_in.get().strip() or not self.f_out.get().strip():
                    raise ValueError("Choose an input folder and an output folder first.")
                source = Path(self.f_in.get()).expanduser().resolve()
                output = Path(self.f_out.get()).expanduser().resolve()
                if not source.is_dir():
                    raise ValueError("Choose an existing input folder.")
                if source == output:
                    raise ValueError("Choose an output folder different from the input folder.")
                if output.exists() and not output.is_dir():
                    raise ValueError("The output location must be a folder.")
                extension = self.f_ext.get().strip().lower()
                if extension.startswith("*."):
                    extension = extension[1:]
                if not extension.startswith("."):
                    extension = "." + extension
                if not re.fullmatch(r"\.[a-z0-9][a-z0-9_-]{0,15}", extension):
                    raise ValueError("Enter one file extension, such as .wav, .webp or .img.")
                target_ext = dict(CONVERT_FORMATS + IMAGE_OUT_FORMATS)[self.f_fmt.get()]
            except (ValueError, OSError, KeyError) as error:
                messagebox.showerror(APP_NAME, str(error))
                return
            self.f_ext.set(extension)
            self.cancel = False
            self.last_out = None
            self.bar["value"] = 0
            self.status.set(f"Scanning for {extension} files (file locations only)…")
            self._set_busy(True)
            self.job_thread = threading.Thread(target=self._folder_worker,
                                               args=(source, output, None, (extension, target_ext)), daemon=True)
            self.job_thread.start()

        # ---------- folder compression ----------
        def _b_pick(self, p=None):
            if self.busy:
                return
            if p is None:
                p = filedialog.askdirectory(title="Choose a folder to compress", mustexist=True)
            if not p:
                return
            source = Path(p).expanduser().resolve()
            if not source.is_dir():
                messagebox.showerror(APP_NAME, "Choose an existing input folder.")
                return
            self.b_in.set(str(source))
            if not self.b_out.get():
                self.b_out.set(str(source.with_name(source.name + "_compressed")) if source.name
                               else str(Path.home() / "MediaStudio_compressed"))
            self.nb.select(self.tab_folder)
            self.status.set("Folder selected. Choose an output folder and target size per file.")

        def _b_save(self):
            p = filedialog.askdirectory(title="Save compressed files in…", mustexist=False,
                                        initialdir=self.b_out.get() or None)
            if p:
                self.b_out.set(p)

        def _start_folder(self):
            if self.busy or not self._need_tools():
                return
            try:
                if not self.b_in.get().strip() or not self.b_out.get().strip():
                    raise ValueError("Choose an input folder and an output folder first.")
                source = Path(self.b_in.get()).expanduser().resolve()
                output = Path(self.b_out.get()).expanduser().resolve()
                if not source.is_dir():
                    raise ValueError("Choose an existing input folder.")
                if source == output:
                    raise ValueError("Choose an output folder different from the input folder.")
                if output.exists() and not output.is_dir():
                    raise ValueError("The output location must be a folder.")
                mb = float(self.b_mb.get())
                if not math.isfinite(mb) or mb <= 0:
                    raise ValueError("Enter a positive target size in MB, like 25 or 8.5.")
            except (ValueError, OSError) as error:
                messagebox.showerror(APP_NAME, str(error))
                return
            settings = (mb, VIDEO_CODECS[self.b_codec.get()], AUDIO_CODECS[self.b_audio.get()],
                        RESOLUTIONS[self.b_res.get()], SPEEDS[self.b_speed.get()], self.b_denoise.get())
            self.cancel = False
            self.last_out = None
            self.bar["value"] = 0
            self.status.set("Scanning folder and subfolders (file locations only)…")
            self._set_busy(True)
            self.job_thread = threading.Thread(target=self._folder_worker, args=(source, output, settings), daemon=True)
            self.job_thread.start()

        def _folder_worker(self, source, output, settings, conversion=None):
            if not conversion:
                mb, video_codec, audio_codec, scale_h, speed, denoise = settings
            action = "converted" if conversion else "compressed"
            verb = "Converting" if conversion else "Compressing"
            completed = skipped = 0
            failures, warnings = [], []
            count = 0
            try:
                files, warnings = collect_media_paths(source, output, lambda: self.cancel,
                                                      {conversion[0]} if conversion else None)
                count = len(files)
                for index, src in enumerate(files):
                    if self.cancel:
                        break
                    relative = src.relative_to(source)
                    label = f"{index + 1}/{count} · {relative}"
                    dst = partial = None
                    self.q.put(("status", f"Checking {label}"))
                    try:
                        info = probe(self.ffprobe, src)
                        if not (info["has_video"] or info["has_audio"]):
                            raise ValueError("No readable media streams. The file may not be a supported image, audio or video format.")
                        if not conversion and info["duration"] <= 0:
                            raise ValueError("No readable media duration.")
                        if self.cancel:
                            break
                        if not conversion and info["size"] <= mb * MIB:
                            skipped += 1
                            continue
                        if conversion:
                            ext = conversion[1]
                        else:
                            key = video_codec if info["has_video"] else audio_codec
                            ext = compress_ext(key, info["has_video"])
                        dst = reserve_batch_output(src, source, output, ext, action=action)
                        # Only compressed output is written. Renaming on success avoids a second copy.
                        with tempfile.NamedTemporaryFile(prefix=".mstudio_", suffix=ext,
                                                         dir=dst.parent, delete=False) as f:
                            partial = Path(f.name)
                        with tempfile.TemporaryDirectory(prefix="mstudio_") as cwd:
                            if conversion:
                                builder = build_image_convert if ext in dict(IMAGE_OUT_FORMATS).values() else build_convert
                                plans = builder(self.ffmpeg, src, partial, info)
                                self.q.put(("status", f"{verb} {label}"))
                                self._execute_plans(plans, info["duration"], cwd, index / count,
                                                    1 / count, f"{verb} {label}")
                            else:
                                self._compress_to_limit(src, partial, info, mb, key, scale_h, speed,
                                                        denoise, cwd, index / count, 1 / count, f"{verb} {label}")
                        if self.cancel:
                            break
                        if partial.stat().st_size == 0:
                            raise RuntimeError("FFmpeg produced an empty output.")
                        os.replace(partial, dst)
                        partial = None
                        dst = None  # successfully published files are never deleted on cancellation
                        completed += 1
                    except Exception as error:
                        if not self.cancel:
                            failures.append(f"{relative}: {error}")
                    finally:
                        for path in (partial, dst):
                            if path is not None:
                                try:
                                    path.unlink(missing_ok=True)
                                except OSError as error:
                                    warnings.append(f"Could not remove incomplete output {path}: {error}")
                        self.q.put(("progress", (index + 1) / count))
                report_path = None
                if failures or warnings:
                    try:
                        output.mkdir(parents=True, exist_ok=True)
                        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="MediaStudio_report_",
                                                         suffix=".txt", dir=output, delete=False) as report:
                            target = (f"Conversion: {conversion[0]} to {conversion[1]}" if conversion
                                      else f"Target per file: {mb:g} MB")
                            report.write(f"Input: {source}\nOutput: {output}\n{target}\n"
                                         f"Completed: {completed}\nSkipped (already below target): {skipped}\n"
                                         f"Failed: {len(failures)}\nCancelled: {self.cancel}\n\n")
                            report.write("\n\n".join(failures + warnings))
                            report_path = report.name
                    except OSError as error:
                        warnings.append(f"Could not save the report: {error}")
                self.q.put(("batch_done", {"output": output, "completed": completed, "skipped": skipped,
                                         "failed": len(failures), "warnings": warnings,
                                         "cancelled": self.cancel, "count": count, "report": report_path,
                                         "details": failures[:3], "operation": action}))
            except Exception as error:
                self.q.put(("error", str(error)))
            finally:
                self.proc = None

        # ---------- convert tab logic ----------
        def _types(self, images=False):
            return " ".join("*" + e for e in sorted(ALL_EXT | (IMAGE_EXT if images else set())))

        def _c_pick(self, p=None):
            if self.busy:
                return
            if p is None:
                p = filedialog.askopenfilename(filetypes=[("Media and image files", self._types(images=True)), ("All files", "*.*")])
            if not p or not self._need_tools():
                return
            self.c_in.set(p)
            self.c_out.set("")
            try:
                self.c_info = probe(self.ffprobe, p)
            except Exception:
                self.c_info = None
                self.c_info_lbl.config(text="⚠ Couldn't read that file.")
                return
            i = self.c_info
            is_image = Path(p).suffix.lower() in IMAGE_EXT and i["has_video"]
            kind = "Picture" if is_image else "Video" if i["has_video"] else "Audio"
            self.c_info_lbl.config(text=f"{kind} · {fmt_time(i['duration'])} · {fmt_size(i['size'])}")
            default = ".jpg" if is_image else ".mp3" if i["has_video"] else ".mp4"
            self.c_fmt.set(next(l for l, e in CONVERT_FORMATS + IMAGE_OUT_FORMATS if e == default))
            self._c_refresh()

        def _c_ext(self):
            return dict(CONVERT_FORMATS + IMAGE_OUT_FORMATS)[self.c_fmt.get()]

        def _c_refresh(self):
            if self.c_in.get():
                src = Path(self.c_in.get())
                dst = (Path(self.c_out.get()).with_suffix(self._c_ext()) if self.c_out.get()
                       else src.with_name(src.stem + "_converted" + self._c_ext()))
                self.c_out.set(str(dst))
            i = self.c_info
            if self._c_ext() in dict(IMAGE_OUT_FORMATS).values():
                self.c_hint.config(text="Save the first picture/frame. JPG uses white behind transparent areas.")
            elif i and not i["has_video"] and self._c_ext() in VIDEO_OUT:
                self.c_hint.config(text="🎵 This is audio only, so you'll get a video with a black screen.")
            elif i and i["has_video"] and self._c_ext() not in VIDEO_OUT:
                self.c_hint.config(text="🎬 The picture will be dropped and only the audio kept.")
            elif i and can_copy(self._c_ext(), i):
                self.c_hint.config(text="⚡ Same codecs already, so this just repackages the file: no re-encode, no quality loss.")
            else:
                self.c_hint.config(text="")

        def _c_save(self):
            ext = self._c_ext()
            p = filedialog.asksaveasfilename(defaultextension=ext, filetypes=[(ext, "*" + ext)],
                                             initialfile=Path(self.c_out.get()).name if self.c_out.get() else "")
            if p:
                self.c_out.set(p)

        def _start_convert(self):
            if self.busy or not self._need_tools():
                return
            if not self.c_in.get() or not self.c_info:
                messagebox.showinfo(APP_NAME, "Choose a file first.")
                return
            src, dst = Path(self.c_in.get()), Path(self.c_out.get())
            if dst.suffix.lower() != self._c_ext():
                dst = dst.with_suffix(self._c_ext())
            if not self._ok_dst(src, dst):
                return
            builder = build_image_convert if dst.suffix.lower() in dict(IMAGE_OUT_FORMATS).values() else build_convert
            try:
                plans = builder(self.ffmpeg, src, dst, self.c_info)
            except ValueError as error:
                messagebox.showerror(APP_NAME, str(error))
                return
            msg = "Repackaging (no re-encode)…" if can_copy(dst.suffix.lower(), self.c_info) else "Converting…"
            self._run(plans, dst, self.c_info["duration"], None, msg)

        # ---------- compress tab logic ----------
        def _z_pick(self, p=None):
            if self.busy:
                return
            if p is None:
                p = filedialog.askopenfilename(filetypes=[("Media files", self._types()), ("All files", "*.*")])
            if not p or not self._need_tools():
                return
            self.z_in.set(p)
            self.z_out.set("")
            try:
                self.z_info = probe(self.ffprobe, p)
            except Exception:
                self.z_info = None
                self.z_info_lbl.config(text="⚠ Couldn't read that file.")
                return
            i = self.z_info
            is_v = i["has_video"]
            kind = "Video" if is_v else "Audio"
            self.z_info_lbl.config(text=f"{kind} · {fmt_time(i['duration'])} · {fmt_size(i['size'])}")
            values = list(VIDEO_CODECS if is_v else AUDIO_CODECS)
            self.z_codec_cb.config(values=values)
            self.z_codec.set(values[0])
            self.z_res_cb.config(state="readonly" if is_v else "disabled")
            self.z_speed_cb.config(state="readonly" if is_v else "disabled")
            self.z_denoise_cb.config(state="normal" if is_v else "disabled")
            self._z_refresh()

        def _z_key(self):
            table = VIDEO_CODECS if (self.z_info and self.z_info["has_video"]) else AUDIO_CODECS
            return table.get(self.z_codec.get())

        def _z_refresh(self):
            if self.z_in.get() and self.z_info and self._z_key():
                src = Path(self.z_in.get())
                ext = compress_ext(self._z_key(), self.z_info["has_video"])
                dst = (Path(self.z_out.get()).with_suffix(ext) if self.z_out.get()
                       else src.with_name(src.stem + "_compressed" + ext))
                self.z_out.set(str(dst))
            self._z_hint()

        def _z_hint(self):
            i = self.z_info
            if not i or i["duration"] <= 0:
                return
            try:
                mb = float(self.z_mb.get())
                if not math.isfinite(mb) or mb <= 0:
                    raise ValueError("Invalid target size")
            except ValueError:
                self.z_hint.config(text="")
                return
            want = mb * MIB * 8 / i["duration"] / 1000
            have = i["size"] * 8 / i["duration"] / 1000
            msg = f"That's about {want:,.0f} kbps overall (original: {have:,.0f} kbps)."
            if mb * MIB >= i["size"]:
                msg += "  ⚠ Target is not smaller than the original."
            elif i["has_video"]:
                total = want * 0.96
                v = int(total - (audio_kbps(total) if i["has_audio"] else 0))
                if v < 10:
                    msg += "  ⚠ Too small, even at 240p. Pick a bigger size."
                else:
                    top = min(RESOLUTIONS.get(self.z_res.get(), 0) or 99999, i["height"] or 99999)
                    h, f = fit_picture(v, top)
                    if h < top or f < 30:
                        msg += (f"  ⚠ At this size the picture will be about {h}p at {f} fps, "
                                "so expect it to look soft.")
            self.z_hint.config(text=msg)

        def _z_save(self):
            ext = Path(self.z_out.get()).suffix or ".mp4"
            p = filedialog.asksaveasfilename(defaultextension=ext, filetypes=[(ext, "*" + ext)],
                                             initialfile=Path(self.z_out.get()).name if self.z_out.get() else "")
            if p:
                self.z_out.set(p)

        def _start_compress(self):
            if self.busy or not self._need_tools():
                return
            if not self.z_in.get() or not self.z_info:
                messagebox.showinfo(APP_NAME, "Choose a file first.")
                return
            try:
                mb = float(self.z_mb.get())
                if not math.isfinite(mb) or mb <= 0:
                    raise ValueError("Invalid target size")
            except Exception:
                messagebox.showerror(APP_NAME, "Enter a target size in MB, like 25 or 8.5.")
                return
            info = self.z_info
            if info["duration"] <= 0:
                messagebox.showerror(APP_NAME, "Couldn't work out the length of this file.")
                return
            if mb * MIB >= info["size"] and not messagebox.askyesno(
                    APP_NAME, "Your target is not smaller than the original file. Continue anyway?"):
                return
            key = self._z_key()
            src = Path(self.z_in.get())
            dst = Path(self.z_out.get())
            ext = compress_ext(key, info["has_video"])
            if dst.suffix.lower() != ext:
                dst = dst.with_suffix(ext)
            if not self._ok_dst(src, dst):
                return
            settings = (mb, key, RESOLUTIONS.get(self.z_res.get(), 0),
                        SPEEDS.get(self.z_speed.get(), "fast"), self.z_denoise.get())
            self.cancel = False
            self.last_out = dst
            self.bar["value"] = 0
            self.status.set("Preparing compression and checking GPU support…")
            self._set_busy(True)
            self.job_thread = threading.Thread(target=self._compression_worker,
                                               args=(src, dst, info, settings), daemon=True)
            self.job_thread.start()

        def _compression_worker(self, src, dst, info, settings):
            partial = None
            try:
                with tempfile.NamedTemporaryFile(prefix=".mstudio_", suffix=dst.suffix,
                                                 dir=dst.parent, delete=False) as f:
                    partial = Path(f.name)
                with tempfile.TemporaryDirectory(prefix="mstudio_") as cwd:
                    self._compress_to_limit(src, partial, info, *settings, cwd)
                if self.cancel:
                    self.q.put(("cancelled", None))
                else:
                    os.replace(partial, dst)
                    self.q.put(("done", None))
            except Exception as error:
                self.q.put(("cancelled", None) if self.cancel else ("error", str(error)))
            finally:
                self.proc = None
                if partial:
                    partial.unlink(missing_ok=True)

        # ---------- running jobs ----------
        def _ok_dst(self, src, dst):
            if src.resolve() == dst.resolve():
                messagebox.showerror(APP_NAME, "The output can't be the same file as the input.")
                return False
            if dst.exists():
                return messagebox.askyesno(APP_NAME, f"{dst.name} already exists. Replace it?")
            return True

        def _set_busy(self, busy):
            self.busy = busy
            st = "disabled" if busy or self.closing else "normal"
            self.btn_convert.config(state=st)
            self.btn_compress.config(state=st)
            self.btn_folder.config(state=st)
            self.btn_folder_convert.config(state=st)
            self.btn_cancel.config(state="normal" if busy else "disabled")
            if busy:
                self.btn_open.config(state="disabled")

        def _run(self, plans, dst, dur, cwd, msg):
            self.cancel = False
            self.last_out = dst
            self.bar["value"] = 0
            self.status.set(msg)
            self._set_busy(True)
            self.job_thread = threading.Thread(target=self._worker, args=(plans, dst, dur, cwd), daemon=True)
            self.job_thread.start()

        def _compress_to_limit(self, src, dst, info, target_mb, codec, scale_h,
                               speed, denoise, cwd, base=0.0, span=1.0, label="Compressing…"):
            """Encode from the source and publish only a nonempty file within the byte cap."""
            if not math.isfinite(target_mb) or target_mb <= 0:
                raise ValueError("Enter a positive target size in MB.")
            byte_limit = int(target_mb * MIB)
            attempt_mb = target_mb
            notes = ""
            weights = (0.80, 0.12, 0.05, 0.03)
            used = 0.0
            for attempt, weight in enumerate(weights, 1):
                if self.cancel:
                    return notes
                plans, notes = plan_compress(self.ffmpeg, src, dst, info, attempt_mb,
                                             codec, scale_h, speed, denoise=denoise)
                if self.cancel:
                    return notes
                phase = label if attempt == 1 else f"{label} · adjusting size, attempt {attempt}/4"
                self.q.put(("status", phase + (f" · {notes}" if notes else "")))
                self._execute_plans(plans, info["duration"], cwd, base + span * used,
                                    span * weight, phase)
                if self.cancel:
                    return notes
                size = Path(dst).stat().st_size
                if size <= 0:
                    raise RuntimeError("FFmpeg produced an empty output.")
                if size <= byte_limit:
                    self.q.put(("progress", base + span))
                    return notes
                # Retry the original source, avoiding an extra generation of quality loss.
                # Reserve headroom for container overhead and imperfect hardware rate control.
                attempt_mb *= min(0.90, byte_limit / size * 0.94)
                used += weight
            raise RuntimeError(
                f"Could not fit this file below {target_mb:g} MB after four attempts "
                f"(last result: {fmt_size(size)}). No oversized result was saved. "
                "Try a lower resolution, a different codec, or a larger target.")

        def _execute_plans(self, plans, dur, cwd, base=0.0, span=1.0, label="Compressing…"):
            """Run one media job; aggregate progress without touching Tk from a thread."""
            total = sum(w for _, w in plans)
            done = 0.0
            tail = []
            for n, (cmd, w) in enumerate(plans, 1):
                if self.cancel:
                    return
                if len(plans) > 1:
                    self.q.put(("status", f"{label} · pass {n} of {len(plans)}"
                                          + (" (analysing)" if n == 1 else " (encoding)")))
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace",
                                     creationflags=NOWIN, cwd=cwd)
                self.proc = p
                try:
                    if self.cancel:
                        p.terminate()
                    for line in p.stdout:
                        line = line.strip()
                        m = re.match(r"out_time_(?:us|ms)=(\d+)", line)
                        if m and dur > 0:
                            frac = min(1.0, int(m.group(1)) / 1e6 / dur)
                            self.q.put(("progress", base + span * (done + frac * w) / total))
                        elif line and not re.match(r"^\w+=", line):
                            tail.append(line)
                            tail = tail[-8:]
                    p.wait()
                    if self.cancel:
                        return
                    if p.returncode != 0:
                        raise RuntimeError("\n".join(tail) or "FFmpeg failed.")
                    done += w
                finally:
                    if p.poll() is None:
                        p.terminate()
                        p.wait()
                    p.stdout.close()
                    self.proc = None

        def _worker(self, plans, dst, dur, cwd):
            partial = None
            try:
                with tempfile.NamedTemporaryFile(prefix=".mstudio_", suffix=Path(dst).suffix,
                                                 dir=Path(dst).parent, delete=False) as f:
                    partial = Path(f.name)
                staged_plans = []
                for cmd, weight in plans:
                    staged = list(cmd)
                    if staged[-1] == str(dst):
                        staged[-1] = str(partial)
                    staged_plans.append((staged, weight))
                self._execute_plans(staged_plans, dur, cwd)
                if self.cancel:
                    self.q.put(("cancelled", None))
                else:
                    os.replace(partial, dst)
                    self.q.put(("done", None))
            except Exception as e:
                self.q.put(("cancelled", None) if self.cancel else ("error", str(e)))
            finally:
                self.proc = None
                if partial:
                    partial.unlink(missing_ok=True)
                if cwd:
                    shutil.rmtree(cwd, ignore_errors=True)

        def _cancel(self):
            self.cancel = True
            if self.proc:
                try:
                    self.proc.terminate()
                except Exception:
                    pass

        def _poll(self):
            try:
                while True:
                    kind, val = self.q.get_nowait()
                    if kind == "progress":
                        self.bar["value"] = val
                    elif kind == "status":
                        self.status.set(val)
                    elif kind == "done":
                        self._set_busy(False)
                        self.bar["value"] = 1
                        size = os.path.getsize(self.last_out)
                        self.status.set(f"✅ Done! {Path(self.last_out).name} · {fmt_size(size)}")
                        self.btn_open.config(state="normal")
                    elif kind == "batch_done":
                        self._set_busy(False)
                        self.bar["value"] = 0 if val["cancelled"] else 1
                        self.last_out = val["output"] if val["output"].is_dir() else None
                        self.btn_open.config(state="normal" if self.last_out else "disabled")
                        prefix = "Stopped" if val["cancelled"] else "Folder complete"
                        if not val["count"] and not val["cancelled"]:
                            prefix = "No matching media files found"
                        operation = val.get("operation", "compressed")
                        skipped = f", {val['skipped']} already below target" if operation == "compressed" else ""
                        self.status.set(f"{prefix}: {val['completed']} {operation}{skipped}, {val['failed']} failed.")
                        if (val["failed"] or val["warnings"]) and not self.closing:
                            detail = (f"Details saved to:\n{val['report']}" if val["report"] else
                                      "\n\n".join(val["details"] + val["warnings"][:3]))
                            messagebox.showwarning(APP_NAME, self.status.get() + "\n\n" + detail)
                    elif kind == "cancelled":
                        self._set_busy(False)
                        self.bar["value"] = 0
                        self.status.set("Cancelled.")
                    elif kind == "error":
                        self._set_busy(False)
                        self.bar["value"] = 0
                        self.status.set("Something went wrong.")
                        if not self.closing:
                            messagebox.showerror(APP_NAME, f"FFmpeg reported a problem:\n\n{val}")
                    elif kind == "dl":
                        self.bar["value"] = val
                    elif kind == "tools_ready":
                        self.ffmpeg, self.ffprobe = find_tool("ffmpeg"), find_tool("ffprobe")
                        self._set_busy(False)
                        self.bar["value"] = 0
                        self.status.set("All set! Choose a file or folder to begin.")
            except queue.Empty:
                pass
            self.after(80, self._poll)

        def _open_folder(self):
            if self.last_out and os.name == "nt":
                args = ([str(self.last_out)] if Path(self.last_out).is_dir()
                        else ["/select,", str(self.last_out)])
                subprocess.Popen(["explorer", *args])
            elif self.last_out:
                folder = self.last_out if Path(self.last_out).is_dir() else Path(self.last_out).parent
                subprocess.Popen(["xdg-open", str(folder)])

        # ---------- FFmpeg setup ----------
        def _need_tools(self):
            if not (self.ffmpeg and self.ffprobe):
                self.ffmpeg, self.ffprobe = find_tool("ffmpeg"), find_tool("ffprobe")
            if self.ffmpeg and self.ffprobe:
                return True
            self._ensure_tools()
            return False

        def _ensure_tools(self):
            self.ffmpeg, self.ffprobe = find_tool("ffmpeg"), find_tool("ffprobe")
            if self.ffmpeg and self.ffprobe or self.busy:
                return
            if os.name != "nt":
                messagebox.showerror(APP_NAME, "FFmpeg wasn't found. Please install it with your package manager.")
                return
            if not messagebox.askyesno(
                    "One-time setup",
                    "Media Studio uses FFmpeg to do the heavy lifting.\n\n"
                    "Download it now? (about 180 MB, one time only)"):
                self.status.set("FFmpeg is needed before you can convert or compress.")
                return
            self._set_busy(True)
            self.btn_cancel.config(state="disabled")
            self.status.set("Downloading FFmpeg…")

            def work():
                try:
                    download_ffmpeg(lambda f: self.q.put(("dl", f)))
                    self.q.put(("tools_ready", None))
                except Exception as e:
                    self.q.put(("error", f"Couldn't download FFmpeg: {e}\n\n"
                                         "Check your internet connection and try again. Or download FFmpeg yourself "
                                         "from github.com/BtbN/FFmpeg-Builds/releases (the win64-gpl zip) and put "
                                         "ffmpeg.exe and ffprobe.exe next to this app."))
            threading.Thread(target=work, daemon=True).start()

        # ---------- drag and drop ----------
        def _on_drop(self, event):
            if self.busy:
                return event.action
            files = self.tk.splitlist(event.data)  # handles spaces and braces in paths
            if not files:
                return event.action
            path = files[0]
            if Path(path).is_dir():
                if str(self.nb.select()) in (str(self.tab_conv), str(self.tab_batch_convert)):
                    self._f_pick(path)
                else:
                    self._b_pick(path)
                if len(files) > 1:
                    self.status.set("Selected the first folder (including its subfolders). Process one folder at a time.")
                return event.action
            if Path(path).suffix.lower() not in ALL_EXT | IMAGE_EXT:
                messagebox.showinfo(APP_NAME, "That file type isn't supported. Try a video, audio or picture file.")
                return event.action
            if str(self.nb.select()) in (str(self.tab_conv), str(self.tab_batch_convert)) or Path(path).suffix.lower() in IMAGE_EXT:
                self.nb.select(self.tab_conv)
                self._c_pick(path)
            else:
                self.nb.select(self.tab_comp)
                self._z_pick(path)
            if len(files) > 1:
                self.status.set("Loaded the first file. Drop files one at a time.")
            return event.action

        def _close(self):
            self.closing = True
            self._cancel()
            if self.job_thread and self.job_thread.is_alive():
                self.status.set("Stopping and cleaning up the current output…")
                self.after(100, self._finish_close)
            else:
                self.destroy()

        def _finish_close(self):
            if self.job_thread and self.job_thread.is_alive():
                self.after(100, self._finish_close)
            else:
                self.destroy()

    App().mainloop()


if __name__ == "__main__":
    run_gui()
