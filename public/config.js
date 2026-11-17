// Where the download API lives.
//   ""  → same server that serves this page
//   (When you open the page from localhost via ./run.sh, it always uses the local server.)
//   "https://your-backend.example.com" → when this page is hosted on Netlify and
//   the Python backend runs somewhere else (see README → "Deploying").
window.PMD_API_BASE = "https://pratham-media-downloader-api.onrender.com";
