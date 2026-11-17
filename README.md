# Pratham Instagram Media Downloader

Paste an Instagram **post, reel or carousel** link and download every photo and video at the **highest quality Instagram serves**: no watermark, no sign-up, no re-compression.

- **Reels & videos:** the best video stream and the best audio stream are fetched separately and merged with ffmpeg. The result is often sharper than the single file Instagram's own app saves.
- **Photos:** saved at the original upload resolution (e.g. 2887×2099), not the compressed preview.
- **Carousels:** every slide, photo or video, plus a one-click `.zip`.
- **Two video modes:** *Highest quality* (may be VP9) or *Max compatibility* (H.264, which plays in QuickTime, WhatsApp, iPhone Photos and video editors).

Built on two well-established open-source projects: [yt-dlp](https://github.com/yt-dlp/yt-dlp) and [instaloader](https://github.com/instaloader/instaloader).

## Run it on your computer

You need Python 3.10+ and ffmpeg (`brew install ffmpeg` on macOS, `sudo apt install ffmpeg` on Linux).

```bash
git clone <this repo>
cd instagram-media-downloader
./run.sh
```

`run.sh` creates a virtual environment, installs the dependencies, keeps yt-dlp up to date and opens http://127.0.0.1:5173.
Files are also kept in `downloads/`.

### Private posts / "try again later" / 1080p

Without signing in, Instagram only serves public posts and caps most reels at 720p. To use your own logged-in browser session instead:

```bash
IG_COOKIES_BROWSER=chrome ./run.sh     # or safari, firefox, brave, edge
```

## Deploying (Netlify page + Render backend)

The page (`public/`) is static and goes on **Netlify**. The download engine needs Python and ffmpeg, which Netlify can't run, so it goes on **Render** (free plan) using the included `Dockerfile` + `render.yaml`.

**1. Push this repo to GitHub.**

**2. Backend on Render**
1. render.com → **New → Blueprint** → pick the repo. Render reads `render.yaml`.
2. When it asks for `ALLOWED_ORIGINS`, enter your Netlify URL (e.g. `https://pratham-ig.netlify.app`). You can fill it in later under *Environment*.
3. Deploy, then copy the service URL (e.g. `https://pratham-ig-downloader-api.onrender.com`) and check that `…/healthz` shows `{"ok":true}`.

**3. Page on Netlify**
1. Put the Render URL in `public/config.js`:
   ```js
   window.PIMD_API_BASE = "https://pratham-ig-downloader-api.onrender.com";
   ```
   Commit and push.
2. netlify.com → **Add new site → Import from Git** → pick the repo. `netlify.toml` already sets the publish folder to `public`, and there is no build command.
3. If your Netlify URL differs from what you put in `ALLOWED_ORIGINS`, update it on Render.

**Good to know**
- Render's free plan **sleeps after ~15 min idle**. The page pings the server as soon as it opens and shows "Waking up the server…", so the first download can take up to a minute.
- Instagram rate-limits cloud IPs more than home connections, so a public instance will sometimes say "try again later". Running it locally avoids that.
- Instagram changes often. If downloads start failing, open Render → **Manual Deploy → Clear build cache & deploy** to pull the newest yt-dlp.
- Each visitor gets 6 downloads per minute (`RATE_LIMIT_PER_MIN`), and files are deleted from the server after 1 hour.

| Env var | Default | What it does |
|---|---|---|
| `PORT` | `5173` | Port to listen on |
| `HOST` | `127.0.0.1` | Use `0.0.0.0` on a server |
| `IG_COOKIES_BROWSER` | – | Read cookies from this browser (local use only) |
| `IG_DOWNLOAD_DIR` | `./downloads` | Where files are saved |
| `IG_KEEP_HOURS` | `0` (keep) | Delete downloads older than N hours (the Dockerfile sets 1) |
| `ALLOWED_ORIGINS` | – | Comma-separated origins allowed to call the API (your Netlify URL) |
| `BEHIND_PROXY` | – | `1` on Render etc., so links are https and IPs are real (the Dockerfile sets it) |
| `RATE_LIMIT_PER_MIN` | `0` (off) | Downloads per visitor per minute (the Dockerfile sets 6) |

## Project layout

```
engine.py        download engine (instaloader for post info + photos, yt-dlp + ffmpeg for video)
app.py           Flask server / API
public/          the web page (index.html, config.js)
run.sh           one-command local start
Dockerfile       backend container for hosting
render.yaml      Render blueprint (one-click backend)
netlify.toml     static hosting for public/
```

## Responsible use

This tool is for saving content **you own or have permission to use**. Respect creators' rights and Instagram's Terms of Use. Not affiliated with Instagram or Meta.

## License

[MIT](LICENSE) © 2026 Pratham Soni
