"""ONE request for the arXiv submission history of the preprint (PREREG §4.1, D18).

    .venv/bin/python scripts/fetch_arxiv_history.py

GET https://arxiv.org/abs/2605.17062 (no retry). The body goes to data/private/arxiv/; its SHA-256,
HTTP status and fetch time go to notes/arxiv-2605.17062.sha256. Refuses to run if the body exists.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
ARXIV_ID = "2605.17062"
URL = f"https://arxiv.org/abs/{ARXIV_ID}"
BODY = REPO / "data" / "private" / "arxiv" / f"abs_{ARXIV_ID}.html"
NOTE = REPO / "notes" / f"arxiv-{ARXIV_ID}.sha256"


def main() -> None:
    if BODY.exists():
        raise SystemExit(f"{BODY.relative_to(REPO)} exists; only one request is allowed")
    at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = httpx.get(URL, headers={"User-Agent": "slop-bench-revalidation/1.0 (research)"}, timeout=60,
                  follow_redirects=True)
    BODY.parent.mkdir(parents=True, exist_ok=True)
    BODY.write_bytes(r.content)
    sha = hashlib.sha256(r.content).hexdigest()
    NOTE.write_text(f"{sha}  data/private/arxiv/abs_{ARXIV_ID}.html\n"
                    f"# GET {URL} -> HTTP {r.status_code}, final URL {r.url}, fetched {at}, {len(r.content)} bytes\n"
                    f"# single request (PREREG §4.1, PREREG-deviations D18); body kept private\n")
    print(f"HTTP {r.status_code}, {len(r.content)} bytes, sha256 {sha}")


if __name__ == "__main__":
    main()
