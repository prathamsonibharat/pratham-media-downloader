"""Download engine for Pratham Instagram Media Downloader.

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

SHORTCODE_RE = re.compile(r"instagram\.com/(?:[\w.]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)")
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)


class DownloadError(Exception):
    pass


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


def _media_info(path: Path, kind: str, width=None, height=None) -> dict:
    return {
        "kind": kind,
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
    shortcode = extract_shortcode(url)
    _cleanup_old()
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
                items.append(_media_info(p, "video", *_probe(p)))
            else:
                p = out_dir / f"{shortcode}_{i:02d}.jpg"
                _http_download(n["image_url"], p)
                items.append(_media_info(p, "image", *_probe(p)))

    return {
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
