#!/usr/bin/env python3
"""
news_ingest.py — Récupération GRATUITE de l'actualité foot (RSS + Reddit).

C'est la "source" qui alimentera le générateur de posts satiriques.
Aucune clé API, aucun quota payant : on lit des flux RSS publics et l'API
JSON publique de Reddit.

Dépendances : `requests` seulement (voir requirements.txt).
Le parsing RSS utilise la lib standard (xml.etree). Si `feedparser` est
installé, il est utilisé automatiquement car plus robuste — mais ce n'est
PAS obligatoire.

Usage :
    python news_ingest.py --validate            # teste chaque flux (à faire 1x chez toi)
    python news_ingest.py                        # affiche les dernières actus (JSON)
    python news_ingest.py --max-age 12 --limit 20
    python news_ingest.py --out actus.json       # écrit dans un fichier
    python news_ingest.py --tags fr mercato      # ne garde que certains flux
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable

import requests

# feedparser est optionnel : on l'utilise s'il est là, sinon parser stdlib.
try:
    import feedparser  # type: ignore
    _HAS_FEEDPARSER = True
except ImportError:
    _HAS_FEEDPARSER = False


HERE = Path(__file__).resolve().parent
FEEDS_FILE = HERE / "feeds.json"

# Beaucoup de médias FR renvoient 403 sans User-Agent "navigateur".
# Reddit EXIGE un User-Agent descriptif et unique sinon il throttle (429).
USER_AGENT = "FootSatireBot/1.0 (+https://example.com; contact: toi@example.com)"
HTTP_TIMEOUT = 12
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class NewsItem:
    """Une actu normalisée, prête à être passée au générateur."""
    id: str            # hash stable -> déduplication + ne pas reposter 2x
    source: str
    title: str
    summary: str
    link: str
    published: str | None   # ISO 8601 (UTC) si connu
    published_ts: float     # epoch (0 si inconnu) -> pour trier
    lang: str
    tags: list[str]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _clean(text: str | None) -> str:
    """Retire le HTML et normalise les espaces."""
    if not text:
        return ""
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _make_id(*parts: str) -> str:
    return hashlib.sha1("||".join(parts).encode("utf-8")).hexdigest()[:16]


def _parse_date_any(s: str | None) -> tuple[str | None, float]:
    """Accepte le RFC-822 (RSS) ET l'ISO-8601 (Atom) -> (iso_utc, epoch)."""
    if not s:
        return None, 0.0
    s = s.strip()
    for parser in (parsedate_to_datetime, lambda x: datetime.fromisoformat(x.replace("Z", "+00:00"))):
        try:
            dt = parser(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt = dt.astimezone(timezone.utc)
            return dt.isoformat(), dt.timestamp()
        except (TypeError, ValueError):
            continue
    return None, 0.0


def _http_get(url: str) -> requests.Response:
    return requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)


def _localname(tag: str) -> str:
    """'{http://www.w3.org/2005/Atom}entry' -> 'entry'."""
    return tag.rsplit("}", 1)[-1]


# --------------------------------------------------------------------------- #
# Parsers RSS/Atom
# --------------------------------------------------------------------------- #
def _items_from_xml(content: bytes) -> list[dict[str, str]]:
    """Parser stdlib : gère RSS 2.0 (<item>) et Atom (<entry>), avec namespaces."""
    root = ET.fromstring(content)
    out: list[dict[str, str]] = []
    for el in root.iter():
        if _localname(el.tag) not in ("item", "entry"):
            continue
        title = summary = link = pub = ""
        for child in el:
            name = _localname(child.tag)
            txt = (child.text or "").strip()
            if name == "title" and txt:
                title = txt
            elif name in ("description", "summary", "content") and txt and not summary:
                summary = txt
            elif name == "link":
                # RSS: texte ; Atom: attribut href (rel=alternate de préférence)
                if txt:
                    link = txt
                else:
                    href = child.attrib.get("href", "")
                    if href and (child.attrib.get("rel", "alternate") == "alternate" or not link):
                        link = href
            elif name in ("pubDate", "published", "updated") and txt and not pub:
                pub = txt
        if title:
            out.append({"title": title, "summary": summary, "link": link, "pub": pub})
    return out


def parse_rss(feed_cfg: dict[str, Any]) -> list[NewsItem]:
    resp = _http_get(feed_cfg["url"])
    resp.raise_for_status()

    raw: list[dict[str, str]]
    if _HAS_FEEDPARSER:
        parsed = feedparser.parse(resp.content)
        raw = [{
            "title": e.get("title", ""),
            "summary": e.get("summary", ""),
            "link": e.get("link", ""),
            "pub": e.get("published", e.get("updated", "")),
        } for e in parsed.entries]
    else:
        raw = _items_from_xml(resp.content)

    items: list[NewsItem] = []
    for e in raw:
        title = _clean(e["title"])
        if not title:
            continue
        iso, ts = _parse_date_any(e["pub"])
        items.append(NewsItem(
            id=_make_id(feed_cfg["name"], e["link"] or title),
            source=feed_cfg["name"],
            title=title,
            summary=_clean(e["summary"])[:600],
            link=e["link"],
            published=iso,
            published_ts=ts,
            lang=feed_cfg.get("lang", "fr"),
            tags=feed_cfg.get("tags", []),
        ))
    return items


def parse_reddit(feed_cfg: dict[str, Any]) -> list[NewsItem]:
    resp = _http_get(feed_cfg["url"])
    resp.raise_for_status()
    data = resp.json()
    items: list[NewsItem] = []
    for child in data.get("data", {}).get("children", []):
        p = child.get("data", {})
        if p.get("stickied"):
            continue
        title = _clean(p.get("title"))
        if not title:
            continue
        created = float(p.get("created_utc", 0) or 0)
        iso = datetime.fromtimestamp(created, tz=timezone.utc).isoformat() if created else None
        # Le "buzz" (score, nb commentaires) est un signal précieux pour la satire.
        summary = f"score={p.get('score', 0)} | comments={p.get('num_comments', 0)} | {_clean(p.get('selftext'))[:400]}"
        items.append(NewsItem(
            id=_make_id(feed_cfg["name"], p.get("id", title)),
            source=feed_cfg["name"],
            title=title,
            summary=summary.strip(" |"),
            link="https://www.reddit.com" + p.get("permalink", ""),
            published=iso,
            published_ts=created,
            lang=feed_cfg.get("lang", "en"),
            tags=feed_cfg.get("tags", []),
        ))
    return items


PARSERS = {"rss": parse_rss, "reddit": parse_reddit}


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def load_feeds(tags_filter: list[str] | None = None) -> list[dict[str, Any]]:
    cfg = json.loads(FEEDS_FILE.read_text(encoding="utf-8"))
    feeds = cfg["feeds"]
    if tags_filter:
        wanted = set(tags_filter)
        feeds = [f for f in feeds if wanted & set(f.get("tags", []))]
    return feeds


def fetch_all(feeds: list[dict[str, Any]], verbose: bool = False) -> list[NewsItem]:
    seen: set[str] = set()
    all_items: list[NewsItem] = []
    for f in feeds:
        parser = PARSERS.get(f.get("type", "rss"))
        if parser is None:
            if verbose:
                print(f"  ! type inconnu pour {f['name']}: {f.get('type')}", file=sys.stderr)
            continue
        try:
            items = parser(f)
        except Exception as exc:  # un flux mort ne doit pas tuer toute l'ingestion
            if verbose:
                print(f"  ! {f['name']}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        for it in items:
            if it.id in seen:
                continue
            seen.add(it.id)
            all_items.append(it)
        if verbose:
            print(f"  ✓ {f['name']}: {len(items)} items", file=sys.stderr)
    all_items.sort(key=lambda x: x.published_ts, reverse=True)
    return all_items


def filter_recent(items: Iterable[NewsItem], max_age_hours: float | None) -> list[NewsItem]:
    if not max_age_hours:
        return list(items)
    import time
    cutoff = time.time() - max_age_hours * 3600
    # On garde les items sans date (published_ts == 0) : on ne sait pas, donc on n'exclut pas.
    return [it for it in items if it.published_ts == 0 or it.published_ts >= cutoff]


# --------------------------------------------------------------------------- #
# Commandes
# --------------------------------------------------------------------------- #
def cmd_validate(feeds: list[dict[str, Any]]) -> int:
    backend = "feedparser" if _HAS_FEEDPARSER else "xml.etree (stdlib)"
    print(f"Validation des flux (backend RSS : {backend})…\n")
    ok = 0
    for f in feeds:
        try:
            r = _http_get(f["url"])
            n = 0
            if r.status_code == 200:
                parser = PARSERS.get(f.get("type", "rss"))
                try:
                    n = len(parser(f)) if parser else 0
                except Exception:
                    n = -1
            flag = "✅" if r.status_code == 200 and n > 0 else "⚠️ " if r.status_code == 200 else "❌"
            print(f"  {flag} [{r.status_code}] {f['name']:<22} items={n}  {f['url']}")
            if r.status_code == 200 and n > 0:
                ok += 1
        except Exception as exc:
            print(f"  ❌ [ERR] {f['name']:<22} {type(exc).__name__}: {exc}")
    print(f"\n{ok}/{len(feeds)} flux exploitables.")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Ingestion gratuite d'actu foot (RSS + Reddit).")
    ap.add_argument("--validate", action="store_true", help="Teste chaque flux et sort.")
    ap.add_argument("--max-age", type=float, default=None, metavar="H",
                    help="Ne garde que les actus de moins de H heures.")
    ap.add_argument("--limit", type=int, default=30, help="Nombre max d'items affichés.")
    ap.add_argument("--tags", nargs="*", default=None, help="Filtrer les flux par tags (ex: fr mercato).")
    ap.add_argument("--out", type=str, default=None, help="Écrire le JSON dans un fichier.")
    ap.add_argument("--verbose", action="store_true", help="Logs par flux sur stderr.")
    args = ap.parse_args(argv)

    feeds = load_feeds(args.tags)
    if not feeds:
        print("Aucun flux ne correspond à ces tags.", file=sys.stderr)
        return 1

    if args.validate:
        return cmd_validate(feeds)

    items = fetch_all(feeds, verbose=args.verbose)
    items = filter_recent(items, args.max_age)
    items = items[: args.limit]

    payload = [asdict(it) for it in items]
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"{len(payload)} actus écrites dans {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
