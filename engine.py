"""Download engine for Pratham Media Downloader (Instagram + YouTube).

Strategy (always aims for the original / highest quality):
  * instaloader enumerates the post (single image, single video, or carousel)
    and gives the full-resolution image URLs.
  * yt-dlp downloads every video, picking the best video + best audio streams
    (DASH) and merging them with ffmpeg, which is often higher than the
    progressive file Instagram serves by default.
  * If one tool fails (rate limit, layout change) the other is used as fallback.
"""

from __future__ import annotations

import os
import re
import shutil
import urllib.request
from pathlib import Path

import instaloader
import yt_dlp

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = Path(os.environ.get("IG_DOWNLOAD_DIR", BASE_DIR / "downloads"))
# Optional: "chrome", "safari", "firefox", "brave", "edge" — lets yt-dlp use your
# logged-in browser session for private / login-walled posts you can see.
# On a hosted server, delete downloads older than this many hours (0 = keep forever).
KEEP_HOURS = float(os.environ.get("IG_KEEP_HOURS", "0") or 0)
COOKIES_BROWSER = os.environ.get("IG_COOKIES_BROWSER", "").strip() or None

# YouTube limits for hosted servers (0 = no limit).
YT_MAX_MINUTES = float(os.environ.get("YT_MAX_MINUTES", "0") or 0)
YT_MAX_HEIGHT = int(os.environ.get("YT_MAX_HEIGHT", "0") or 0)
# yt-dlp needs a JavaScript runtime to unlock all YouTube formats.
JS_RUNTIMES = {name: {} for name in ("deno", "node", "bun") if shutil.which(name)}

YT_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|live/|embed/|v/)|youtu\.be/)([A-Za-z0-9_-]{11})"
)
SHORTCODE_RE = re.compile(r"instagram\.com/(?:[\w.]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)")
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)


class DownloadError(Exception):
    pass


def detect_platform(url: str) -> str:
    if YT_RE.search(url):
        return "youtube"
    if SHORTCODE_RE.search(url):
        return "instagram"
    raise DownloadError(
        "That doesn't look like an Instagram or YouTube link. Paste a post, reel, "
        "video or Shorts link, e.g. https://www.instagram.com/reel/XXXX/ or "
        "https://youtube.com/shorts/XXXX"
    )


def extract_shortcode(url: str) -> str:
    m = SHORTCODE_RE.search(url.strip())
    if not m:
        raise DownloadError(
            "That doesn't look like an Instagram post/reel link. "
            "Expected something like https://www.instagram.com/reel/XXXX/"
        )
    return m.group(1)


def _http_download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def _has_audio(path: Path) -> bool | None:
    """True/False if the file has an audio track; None if ffprobe isn't available."""
    if not shutil.which("ffprobe"):
        return None
    import subprocess

    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=20,
    ).stdout.strip()
    return bool(out)


def _ensure_audio(video: Path, fallback_url: str | None) -> None:
    """Safety net: if the best-quality file came out silent but Instagram's regular
    MP4 has sound, copy that sound into it (no re-encoding). If merging fails,
    use Instagram's regular MP4 instead, since a file with sound beats a silent one."""
    if not fallback_url or _has_audio(video) is not False or not shutil.which("ffmpeg"):
        return
    import subprocess

    alt = video.with_name(video.stem + "_alt.mp4")
    merged = video.with_name(video.stem + "_merged.mp4")
    try:
        _http_download(fallback_url, alt)
        if not _has_audio(alt):
            return  # the original post is genuinely silent
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(video), "-i", str(alt),
             "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-shortest",
             "-movflags", "+faststart", str(merged)],
            capture_output=True, timeout=120,
        )
        if r.returncode == 0 and _has_audio(merged):
            merged.replace(video)
        else:
            alt.replace(video)
    except Exception:
        pass
    finally:
        for f in (alt, merged):
            f.unlink(missing_ok=True)


def _media_info(path: Path, kind: str, width=None, height=None) -> dict:
    return {
        "kind": kind,
        "has_audio": _has_audio(path) if kind == "video" else None,
        "filename": path.name,
        "relpath": str(path.relative_to(DOWNLOAD_DIR)),
        "size": path.stat().st_size,
        "width": width,
        "height": height,
    }


def _probe(path: Path) -> tuple[int | None, int | None]:
    """Return (width, height) of a media file via ffprobe, if available."""
    if not shutil.which("ffprobe"):
        return None, None
    import subprocess

    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(path)],
            capture_output=True, text=True, timeout=20,
        ).stdout.strip()
        w, h = out.split("x")[:2]
        return int(w), int(h)
    except Exception:
        return None, None


FORMATS = {
    # Highest resolution + bitrate, any codec (often VP9 — best quality).
    "best": "bestvideo*+bestaudio/best",
    # Best H.264 stream: plays everywhere (QuickTime, iPhone, WhatsApp, editors).
    "compat": "bestvideo*[vcodec^=avc1]+bestaudio/best[vcodec^=avc1]/best",
}


def _ytdlp_videos(url: str, out_dir: Path, shortcode: str, quality: str) -> list[Path]:
    """Download all videos of a post with yt-dlp. Returns files in order."""
    opts = {
        "format": FORMATS.get(quality, FORMATS["best"]),
        "format_sort": ["res", "fps", "vbr", "abr"],
        "merge_output_format": "mp4",
        "outtmpl": str(out_dir / f"{shortcode}_v%(playlist_index|1)02d.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "http_headers": {"User-Agent": UA},
    }
    if COOKIES_BROWSER:
        opts["cookiesfrombrowser"] = (COOKIES_BROWSER,)
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    return sorted(p for p in out_dir.glob(f"{shortcode}_v*.mp4"))


def _instaloader_nodes(shortcode: str):
    L = instaloader.Instaloader(quiet=True, user_agent=UA)
    post = instaloader.Post.from_shortcode(L.context, shortcode)
    if post.typename == "GraphSidecar":
        nodes = [
            {"is_video": n.is_video, "video_url": n.video_url, "image_url": n.display_url}
            for n in post.get_sidecar_nodes()
        ]
    else:
        nodes = [{"is_video": post.is_video, "video_url": post.video_url, "image_url": post.url}]
    meta = {
        "owner": post.owner_username,
        "caption": (post.caption or "")[:300],
        "typename": post.typename,
    }
    return nodes, meta


def _cleanup_old() -> None:
    if KEEP_HOURS <= 0 or not DOWNLOAD_DIR.exists():
        return
    import time
    cutoff = time.time() - KEEP_HOURS * 3600
    for d in DOWNLOAD_DIR.iterdir():
        if d.is_dir() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)


def fetch(url: str, quality: str = "best") -> dict:
    platform = detect_platform(url.strip())
    _cleanup_old()
    if platform == "youtube":
        return fetch_youtube(url.strip(), quality)
    return fetch_instagram(url, quality)


YT_FORMATS = {
    # Highest resolution/bitrate (often VP9 or AV1, up to 4K/8K) + best audio.
    "best": "bestvideo*{h}+bestaudio/best{h}",
    # H.264 + AAC: plays in QuickTime, iPhone, WhatsApp, editors (YouTube tops out at 1080p here).
    "compat": "bestvideo*[vcodec^=avc1]{h}+bestaudio[ext=m4a]/best[ext=mp4]{h}/best{h}",
}


def fetch_youtube(url: str, quality: str = "best") -> dict:
    video_id = YT_RE.search(url).group(1)
    is_short = "/shorts/" in url
    out_dir = DOWNLOAD_DIR / f"youtube_{video_id}"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    h = f"[height<={YT_MAX_HEIGHT}]" if YT_MAX_HEIGHT else ""
    opts = {
        "format": YT_FORMATS.get(quality, YT_FORMATS["best"]).format(h=h),
        "merge_output_format": "mp4",
        "outtmpl": str(out_dir / "%(title).80B [%(id)s].%(ext)s"),
        "windowsfilenames": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    if JS_RUNTIMES:
        opts["js_runtimes"] = JS_RUNTIMES
    if COOKIES_BROWSER:
        opts["cookiesfrombrowser"] = (COOKIES_BROWSER,)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
                raise DownloadError("This is a live stream. Try again once the stream has ended.")
            minutes = (info.get("duration") or 0) / 60
            if YT_MAX_MINUTES and minutes > YT_MAX_MINUTES:
                raise DownloadError(
                    f"This video is {minutes:.0f} min long. This server only handles videos up to "
                    f"{YT_MAX_MINUTES:.0f} min. Run the tool on your own computer for longer ones."
                )
            ydl.process_ie_result(info, download=True)
    except DownloadError:
        raise
    except Exception as e:
        raise DownloadError(_friendly_youtube(e))

    files = sorted(out_dir.glob("*.mp4"))
    if not files:
        raise DownloadError("YouTube didn't return a downloadable video for this link.")
    p = files[0]
    return {
        "platform": "youtube",
        "kind": "short" if is_short else "video",
        "shortcode": video_id,
        "owner": info.get("channel") or info.get("uploader"),
        "caption": info.get("title"),
        "folder": str(out_dir),
        "items": [_media_info(p, "video", *_probe(p))],
    }


def _friendly_youtube(e: Exception) -> str:
    text = str(e).replace("ERROR: ", "")
    low = text.lower()
    if "not a bot" in low or "sign in to confirm" in low:
        return ("YouTube is blocking this server as a bot (common on cloud hosting). "
                "Running the tool on your own computer usually works.")
    if "private video" in low:
        return "This video is private."
    if "age" in low and ("restricted" in low or "confirm your age" in low):
        return ("This video is age-restricted. Run the tool locally with "
                "IG_COOKIES_BROWSER=chrome so it uses your signed-in YouTube session.")
    if "members" in low:
        return "This video is for channel members only."
    if "unavailable" in low:
        return "This video is unavailable (removed, private, or blocked in this region)."
    return "Couldn't download this YouTube video. Details: " + text[:300]


def fetch_instagram(url: str, quality: str = "best") -> dict:
    shortcode = extract_shortcode(url)
    canonical = f"https://www.instagram.com/p/{shortcode}/"

    nodes, meta, insta_err = None, {}, None
    try:
        nodes, meta = _instaloader_nodes(shortcode)
    except Exception as e:  # rate limited / login required / etc.
        insta_err = e

    folder_name = f"{meta.get('owner', 'instagram')}_{shortcode}"
    out_dir = DOWNLOAD_DIR / folder_name
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    # 1) Videos via yt-dlp (best quality, merged audio)
    videos: list[Path] = []
    ytdlp_err = None
    needs_video = nodes is None or any(n["is_video"] for n in nodes)
    if needs_video:
        try:
            videos = _ytdlp_videos(canonical, out_dir, shortcode, quality)
        except Exception as e:
            ytdlp_err = e

    items: list[dict] = []
    if nodes is None:
        # instaloader failed; yt-dlp is all we have (videos only)
        if not videos:
            raise DownloadError(_friendly(insta_err, ytdlp_err))
        for p in videos:
            items.append(_media_info(p, "video", *_probe(p)))
    else:
        vid_iter = iter(videos)
        for i, n in enumerate(nodes, 1):
            if n["is_video"]:
                p = next(vid_iter, None)
                if p is None:  # yt-dlp failed → fall back to Instagram's progressive file
                    p = out_dir / f"{shortcode}_{i:02d}.mp4"
                    _http_download(n["video_url"], p)
                else:
                    final = out_dir / f"{shortcode}_{i:02d}.mp4"
                    p.rename(final)
                    p = final
                    _ensure_audio(p, n.get("video_url"))
                items.append(_media_info(p, "video", *_probe(p)))
            else:
                p = out_dir / f"{shortcode}_{i:02d}.jpg"
                _http_download(n["image_url"], p)
                items.append(_media_info(p, "image", *_probe(p)))

    return {
        "platform": "instagram",
        "kind": None,
        "shortcode": shortcode,
        "owner": meta.get("owner"),
        "caption": meta.get("caption"),
        "folder": str(out_dir),
        "items": items,
    }


def _friendly(*errors) -> str:
    text = " | ".join(str(e) for e in errors if e)
    low = text.lower()
    if "login" in low or "401" in low or "private" in low or "rate" in low or "wait a few" in low:
        return (
            "Instagram blocked the anonymous request (private post or rate limit). "
            "Try again in a few minutes, or restart with IG_COOKIES_BROWSER=chrome "
            "so it uses your logged-in browser session. Details: " + text[:300]
        )
    return "Couldn't download this post. Details: " + text[:400]
