#!/usr/bin/env python3
"""
x_publisher.py — Publication des posts approuvés sur X (Twitter), brique 4.

Consomme la file `approved.jsonl` (produite par telegram_review.py) et publie
chaque post via l'API X v2 (POST /2/tweets). Tient un journal `published.jsonl`
pour ne JAMAIS publier deux fois le même post (dédup par news_id) et pour relier
chaque post à son tweet_id/URL (matière de l'apprentissage : impressions à venir).

Auth : OAuth 1.0a user-context (seul moyen de poster sur son propre compte avec
les clés du portail développeur). La signature HMAC-SHA1 est faite à la main avec
la stdlib — aucune dépendance lourde, et c'est déterministe donc testable hors-ligne.

Free tier X : ~17 posts/jour, 500/mois en écriture. On espace les posts et on
plafonne le nombre par exécution.

Prérequis (portail developer.x.com, app avec permission Read+Write) :
    export X_API_KEY="..."          # consumer key
    export X_API_SECRET="..."       # consumer secret
    export X_ACCESS_TOKEN="..."     # access token (du compte qui poste)
    export X_ACCESS_SECRET="..."    # access token secret

Usage :
    python x_publisher.py --max-posts 5 --min-interval 30
    python x_publisher.py --dry-run          # n'appelle pas X, montre ce qui partirait
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

HERE = Path(__file__).resolve().parent
APPROVED_QUEUE = HERE / "approved.jsonl"
PUBLISHED_LOG = HERE / "published.jsonl"
X_TWEETS_URL = "https://api.twitter.com/2/tweets"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# OAuth 1.0a — signature (PURE, déterministe, testable)
# --------------------------------------------------------------------------- #
def percent_encode(value: str) -> str:
    """Encodage RFC 3986 : tout sauf les caractères non réservés ALPHA/DIGIT/-._~"""
    return urllib.parse.quote(str(value), safe="-._~")


def signature_base_string(method: str, url: str, params: dict[str, str]) -> str:
    """Chaîne de base OAuth : METHOD&url&params (params triés, doublement encodés)."""
    norm = "&".join(
        f"{percent_encode(k)}={percent_encode(v)}"
        for k, v in sorted(params.items())
    )
    return "&".join([method.upper(), percent_encode(url), percent_encode(norm)])


def sign(base_string: str, consumer_secret: str, token_secret: str) -> str:
    key = f"{percent_encode(consumer_secret)}&{percent_encode(token_secret)}"
    digest = hmac.new(key.encode(), base_string.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


class OAuth1:
    """Construit l'en-tête Authorization OAuth 1.0a. nonce/timestamp injectables."""

    def __init__(self, consumer_key: str, consumer_secret: str,
                 token: str, token_secret: str):
        self.consumer_key = consumer_key
        self.consumer_secret = consumer_secret
        self.token = token
        self.token_secret = token_secret

    def auth_header(self, method: str, url: str, *,
                    nonce: str | None = None, timestamp: str | None = None) -> str:
        oauth = {
            "oauth_consumer_key": self.consumer_key,
            "oauth_nonce": nonce or base64.urlsafe_b64encode(os.urandom(16)).decode().strip("="),
            "oauth_signature_method": "HMAC-SHA1",
            "oauth_timestamp": timestamp or str(int(time.time())),
            "oauth_token": self.token,
            "oauth_version": "1.0",
        }
        base = signature_base_string(method, url, oauth)
        oauth["oauth_signature"] = sign(base, self.consumer_secret, self.token_secret)
        parts = ", ".join(f'{percent_encode(k)}="{percent_encode(v)}"'
                          for k, v in sorted(oauth.items()))
        return "OAuth " + parts


# --------------------------------------------------------------------------- #
# Client X (réseau)
# --------------------------------------------------------------------------- #
class XClient:
    def __init__(self, oauth: OAuth1 | None = None, timeout: int = 20):
        self.oauth = oauth or OAuth1(
            os.environ["X_API_KEY"], os.environ["X_API_SECRET"],
            os.environ["X_ACCESS_TOKEN"], os.environ["X_ACCESS_SECRET"])
        self.timeout = timeout

    def post_tweet(self, text: str) -> dict[str, str]:
        headers = {
            "Authorization": self.oauth.auth_header("POST", X_TWEETS_URL),
            "Content-Type": "application/json",
        }
        r = requests.post(X_TWEETS_URL, headers=headers,
                          json={"text": text}, timeout=self.timeout)
        if r.status_code >= 300:
            raise RuntimeError(f"X API {r.status_code}: {r.text}")
        return r.json().get("data", {})


# --------------------------------------------------------------------------- #
# Logique de publication (PURE, sans réseau -> testable)
# --------------------------------------------------------------------------- #
def read_jsonl(path: Path) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def published_ids(path: Path = PUBLISHED_LOG) -> set[str]:
    return {e.get("news_id") for e in read_jsonl(path) if e.get("news_id")}


def pending_posts(approved: list[dict[str, Any]], done: set[str]) -> list[dict[str, Any]]:
    """Posts approuvés pas encore publiés, dédupliqués par news_id (1ère occurrence)."""
    seen: set[str] = set()
    pending = []
    for entry in approved:
        nid = entry.get("news_id")
        if not nid or nid in done or nid in seen:
            continue
        seen.add(nid)
        pending.append(entry)
    return pending


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def publish_pending(post_fn: Callable[[str], dict[str, str]],
                    *, approved_path: Path = APPROVED_QUEUE,
                    published_path: Path = PUBLISHED_LOG,
                    max_posts: int = 10, min_interval: float = 0.0,
                    sleep_fn: Callable[[float], None] = time.sleep,
                    dry_run: bool = False) -> list[dict[str, Any]]:
    """Publie jusqu'à max_posts posts en attente. `post_fn(text) -> {id,...}`.

    Renvoie la liste des entrées publiées (avec tweet_id/url). Réseau et sleep
    sont injectés pour permettre des tests hors-ligne déterministes.
    """
    approved = read_jsonl(approved_path)
    pending = pending_posts(approved, published_ids(published_path))[:max_posts]
    results: list[dict[str, Any]] = []

    for i, entry in enumerate(pending):
        text = entry.get("post", "")
        if not text:
            continue
        if dry_run:
            results.append({"news_id": entry.get("news_id"), "dry_run": True, "text": text})
            continue

        data = post_fn(text)
        tweet_id = data.get("id", "")
        record = {
            "ts": _now(),
            "news_id": entry.get("news_id"),
            "tweet_id": tweet_id,
            "url": f"https://x.com/i/web/status/{tweet_id}" if tweet_id else "",
            "angle": entry.get("angle"),
            "source": entry.get("source"),
            "post": text,
        }
        with Path(published_path).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        results.append(record)

        if min_interval and i < len(pending) - 1:
            sleep_fn(min_interval)

    return results


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Publie les posts approuvés sur X.")
    ap.add_argument("--approved", default=str(APPROVED_QUEUE))
    ap.add_argument("--max-posts", type=int, default=10, help="Plafond par exécution.")
    ap.add_argument("--min-interval", type=float, default=30.0,
                    help="Secondes entre deux posts (anti-spam).")
    ap.add_argument("--dry-run", action="store_true", help="N'appelle pas X.")
    args = ap.parse_args(argv)

    approved_path = Path(args.approved)
    if not approved_path.exists() or not read_jsonl(approved_path):
        print("File 'approved.jsonl' vide — rien à publier. Valide d'abord via Telegram.")
        return 1

    if args.dry_run:
        out = publish_pending(lambda _t: {}, approved_path=approved_path,
                              max_posts=args.max_posts, dry_run=True)
        print(f"[dry-run] {len(out)} post(s) seraient publiés :")
        for o in out:
            print(f"  • {o['text'][:80]}…")
        return 0

    try:
        client = XClient()
    except KeyError as exc:
        print(f"Variable d'environnement manquante : {exc}. "
              f"Requiert X_API_KEY/X_API_SECRET/X_ACCESS_TOKEN/X_ACCESS_SECRET.")
        return 2

    out = publish_pending(client.post_tweet, approved_path=approved_path,
                          max_posts=args.max_posts, min_interval=args.min_interval)
    print(f"{len(out)} post(s) publié(s). Journal : {PUBLISHED_LOG}")
    for o in out:
        print(f"  ✅ {o['url']}  {o['post'][:60]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
