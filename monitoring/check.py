"""Manual monitoring of three selected AUQNI external-environment resources."""

import json
import sqlite3
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen


HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
CONFIG = HERE / "config.json"
DATABASE = HERE / "state.sqlite3"
USER_AGENT = "AUQNI-external-monitor/1.0 (manual research)"
ALLOWED_KINDS = {"nqi": "nqi_news", "pubmed": "pubmed_eutils", "who": "who_newsroom"}


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fetch(url):
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html, application/json"})
    with urlopen(request, timeout=25) as response:
        data = response.read(5_000_001)
        if len(data) > 5_000_000:
            raise ValueError("Response exceeds 5 MB")
        charset = response.headers.get_content_charset() or "utf-8"
        return data.decode(charset, errors="replace")


def canonical_url(url):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") + "/", "", ""))


class Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        attrs = dict(attrs)
        if attrs.get("href"):
            self.current = {"href": attrs["href"], "label": attrs.get("aria-label", ""), "text": []}

    def handle_data(self, data):
        if self.current is not None:
            self.current["text"].append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.current is not None:
            link = self.current
            link["text"] = " ".join(" ".join(link["text"]).split())
            self.links.append(link)
            self.current = None


def html_items(source, expected_prefix):
    base = source["url"]
    parser = Links()
    parser.feed(fetch(base))
    items = {}
    for link in parser.links:
        url = canonical_url(urljoin(base, link["href"]))
        parsed = urlsplit(url)
        if parsed.netloc != urlsplit(base).netloc or not parsed.path.startswith(expected_prefix):
            continue
        if parsed.path.rstrip("/") == expected_prefix.rstrip("/"):
            continue
        title = link["label"] or link["text"]
        if not title:
            continue
        item = {"external_id": url, "url": url, "title": title, "published_at": None}
        if source["id"] == "who":
            slug = parsed.path.rsplit("/", 1)[-1]
            date = slug[:10]
            try:
                item["published_at"] = datetime.strptime(date, "%d-%m-%Y").date().isoformat()
            except ValueError:
                pass
        items[url] = item
    if not items:
        raise ValueError("No material links found; page layout may have changed")
    return list(items.values())


def pubmed_items(source):
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
    params = {"db": "pubmed", "term": source["query"], "retmax": source["limit"], "retmode": "json"}
    search = json.loads(fetch(base + "esearch.fcgi?" + urlencode(params)))
    ids = search["esearchresult"]["idlist"]
    if not ids:
        return []
    summary_url = base + "esummary.fcgi?" + urlencode({"db": "pubmed", "id": ",".join(ids), "retmode": "json"})
    summary = json.loads(fetch(summary_url))["result"]
    items = []
    for pmid in ids:
        record = summary[pmid]
        title = record.get("title", "").strip()
        if not title:
            raise ValueError("PubMed summary has no title for PMID " + pmid)
        items.append({"external_id": pmid, "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                      "title": title, "published_at": record.get("epubdate") or record.get("pubdate") or None})
    return items


def validate_config(config):
    registry_file = (PROJECT / config["registry_path"]).resolve()
    registry = json.loads(registry_file.read_text(encoding="utf-8-sig"))
    objects = {obj["id"]: obj for obj in registry["objects"]}
    sources = config["sources"]
    if {source["id"] for source in sources} != set(ALLOWED_KINDS) or len(sources) != 3:
        raise ValueError("Only nqi, pubmed and who may be monitored in this version")
    for source in sources:
        sid = source["id"]
        if source["kind"] != ALLOWED_KINDS[sid] or not objects[sid].get("official_resource"):
            raise ValueError("Invalid monitoring source: " + sid)
        if sid != "pubmed" and urlsplit(source["url"]).netloc != urlsplit(objects[sid]["official_resource"]).netloc:
            raise ValueError("Monitoring URL is outside the registered resource: " + sid)
        if sid == "pubmed" and (not source.get("query") or not 1 <= source.get("limit", 0) <= 100):
            raise ValueError("PubMed query or limit is invalid")
    return sources


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS materials (
        source_id TEXT NOT NULL,
        external_id TEXT NOT NULL,
        url TEXT NOT NULL,
        title TEXT NOT NULL,
        published_at TEXT,
        first_seen_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'обнаружено' CHECK (status = 'обнаружено'),
        PRIMARY KEY (source_id, external_id)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS checks (
        source_id TEXT PRIMARY KEY,
        checked_at TEXT NOT NULL,
        last_success_at TEXT,
        last_error TEXT
    )""")
    db.commit()


def run():
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    sources = validate_config(config)
    with sqlite3.connect(DATABASE) as db:
        initialize(db)
        had_error = False
        for source in sources:
            sid = source["id"]
            try:
                if sid == "nqi":
                    items = html_items(source, "/about/news/")
                elif sid == "who":
                    items = html_items(source, "/news/item/")
                else:
                    items = pubmed_items(source)
                stamp = now_utc()
                with db:
                    before = db.total_changes
                    for item in items:
                        db.execute("""INSERT OR IGNORE INTO materials
                            (source_id, external_id, url, title, published_at, first_seen_at)
                            VALUES (?, ?, ?, ?, ?, ?)""",
                            (sid, item["external_id"], item["url"], item["title"], item["published_at"], stamp))
                    added = db.total_changes - before
                    db.execute("""INSERT INTO checks (source_id, checked_at, last_success_at, last_error)
                        VALUES (?, ?, ?, NULL) ON CONFLICT(source_id) DO UPDATE SET
                        checked_at=excluded.checked_at, last_success_at=excluded.last_success_at, last_error=NULL""",
                        (sid, stamp, stamp))
                total = db.execute("SELECT COUNT(*) FROM materials WHERE source_id=?", (sid,)).fetchone()[0]
                print(f"{sid}: found={len(items)}, new={added}, stored={total}")
            except (OSError, ValueError, KeyError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
                had_error = True
                error = f"{type(exc).__name__}: {exc}"
                with db:
                    db.execute("""INSERT INTO checks (source_id, checked_at, last_error)
                        VALUES (?, ?, ?) ON CONFLICT(source_id) DO UPDATE SET
                        checked_at=excluded.checked_at, last_error=excluded.last_error""",
                        (sid, now_utc(), error))
                print(f"{sid}: ERROR {error}", file=sys.stderr)
        return 1 if had_error else 0


if __name__ == "__main__":
    try:
        sys.exit(run())
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"Configuration error: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(2)
