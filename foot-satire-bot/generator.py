#!/usr/bin/env python3
"""
generator.py — Transforme une actu (de news_ingest) en post X satirique via Mistral.

Free tier Mistral : https://console.mistral.ai (clé dans la variable d'env MISTRAL_API_KEY).
Modèle par défaut : `mistral-small-latest` (bon texte FR, dispo en gratuit).

Usage :
    export MISTRAL_API_KEY="..."
    python news_ingest.py --out actus.json
    python generator.py --from actus.json --limit 3
    python generator.py --from actus.json --angle punchline --out drafts.json

Le code est conçu pour être testable hors-ligne : `generate_post` accepte
n'importe quel `client` ayant une méthode .chat(system, user) -> str.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

import requests

import persona

HERE = Path(__file__).resolve().parent
EXAMPLES_FILE = HERE / "examples.json"

MISTRAL_URL = "https://api.mistral.ai/v1/chat/completions"
DEFAULT_MODEL = "mistral-small-latest"
MAX_CHARS = 280


# --------------------------------------------------------------------------- #
# Client Mistral
# --------------------------------------------------------------------------- #
class ChatClient(Protocol):
    model: str
    def chat(self, system: str, user: str) -> str: ...


class MistralClient:
    """Wrapper minimal de l'API chat completions, avec retry sur 429/5xx."""

    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL,
                 temperature: float = 0.9, timeout: int = 30, max_retries: int = 4):
        self.api_key = api_key or os.environ.get("MISTRAL_API_KEY")
        if not self.api_key:
            raise RuntimeError("MISTRAL_API_KEY manquante (export MISTRAL_API_KEY=...).")
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self.max_retries = max_retries

    def chat(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": 220,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                r = requests.post(MISTRAL_URL, headers=headers, json=payload, timeout=self.timeout)
                if r.status_code == 429 or r.status_code >= 500:
                    # Free tier throttle -> backoff exponentiel.
                    raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"]
            except (requests.RequestException, KeyError, ValueError) as exc:
                last_exc = exc
                if attempt < self.max_retries - 1:
                    time.sleep(2 ** attempt)
        raise RuntimeError(f"Appel Mistral échoué après {self.max_retries} essais : {last_exc}")


# --------------------------------------------------------------------------- #
# Garde-fous / post-traitement
# --------------------------------------------------------------------------- #
_QUOTES = "\"“”«»"


def sanitize(text: str, max_chars: int = MAX_CHARS) -> str:
    """Nettoie la sortie du modèle et garantit la contrainte de longueur."""
    t = text.strip()
    # Retire d'éventuels guillemets englobants et préfixes parasites.
    t = re.sub(r"^(post|tweet)\s*[:\-]\s*", "", t, flags=re.IGNORECASE).strip()
    if len(t) >= 2 and t[0] in _QUOTES and t[-1] in _QUOTES:
        t = t[1:-1].strip()
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    if len(t) <= max_chars:
        return t
    # Trop long : coupe sur une frontière de mot, en gardant la place pour "…".
    cut = t[: max_chars - 1]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip() + "…"


@dataclass
class Draft:
    """Un brouillon de post, prêt pour la validation Telegram."""
    post: str
    angle: str
    news_id: str
    source: str
    title: str
    link: str
    model: str
    chars: int
    ok_length: bool


# --------------------------------------------------------------------------- #
# Construction du prompt
# --------------------------------------------------------------------------- #
def load_examples(angle: str | None = None) -> list[dict[str, str]]:
    if not EXAMPLES_FILE.exists():
        return []
    data = json.loads(EXAMPLES_FILE.read_text(encoding="utf-8")).get("examples", [])
    if angle:
        same = [e for e in data if e.get("angle") == angle]
        if same:  # privilégie les exemples du même angle, mais garde un fallback
            return same
    return data


def build_messages(item: dict[str, Any], angle: str | None,
                   few_shot: list[dict[str, str]] | None) -> tuple[str, str, str]:
    """Retourne (angle_resolu, system_prompt, user_prompt)."""
    resolved, consigne = persona.angle_instruction(angle)
    system = persona.PERSONA + "\n" + consigne

    parts: list[str] = []
    for ex in (few_shot or []):
        parts.append(f"Actu : {ex.get('news_title', '')}\nPost : {ex.get('post', '')}")
    shots = ("\n\n".join(parts) + "\n\n") if parts else ""

    title = item.get("title", "").strip()
    summary = (item.get("summary", "") or "").strip()
    user = (
        f"{shots}"
        f"Écris UN post satirique à partir de cette actu foot :\n"
        f"Titre : {title}\n"
        + (f"Détail : {summary}\n" if summary else "")
        + "\nRenvoie uniquement le texte du post."
    )
    return resolved, system, user


def generate_post(item: dict[str, Any], *, client: ChatClient,
                  angle: str | None = None, few_shot: list[dict[str, str]] | None = None,
                  max_chars: int = MAX_CHARS) -> Draft:
    if few_shot is None:
        resolved_peek, _ = persona.angle_instruction(angle)
        few_shot = load_examples(resolved_peek)
    resolved, system, user = build_messages(item, angle, few_shot)
    raw = client.chat(system, user)
    post = sanitize(raw, max_chars)
    return Draft(
        post=post,
        angle=resolved,
        news_id=item.get("id", ""),
        source=item.get("source", ""),
        title=item.get("title", ""),
        link=item.get("link", ""),
        model=getattr(client, "model", "?"),
        chars=len(post),
        ok_length=len(post) <= max_chars and len(post) > 0,
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Génère des posts satiriques depuis des actus.")
    ap.add_argument("--from", dest="src", required=True, help="Fichier JSON produit par news_ingest.py.")
    ap.add_argument("--angle", default=None, help=f"Angle d'humour : {', '.join(persona.ANGLES)}.")
    ap.add_argument("--limit", type=int, default=3, help="Nombre d'actus à traiter.")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="Modèle Mistral.")
    ap.add_argument("--out", default=None, help="Écrire les brouillons dans un fichier JSON.")
    args = ap.parse_args(argv)

    items = json.loads(Path(args.src).read_text(encoding="utf-8"))
    if not isinstance(items, list):
        print("Le fichier d'entrée doit être une liste d'actus.", file=sys.stderr)
        return 1
    items = items[: args.limit]
    if not items:
        print("Aucune actu à traiter.", file=sys.stderr)
        return 1

    try:
        client = MistralClient(model=args.model)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    drafts: list[dict[str, Any]] = []
    for it in items:
        try:
            d = generate_post(it, client=client, angle=args.angle)
        except Exception as exc:
            print(f"  ! génération échouée pour '{it.get('title','?')}': {exc}", file=sys.stderr)
            continue
        drafts.append(asdict(d))
        print(f"[{d.angle}] ({d.chars}c) {d.post}\n", file=sys.stderr)

    text = json.dumps(drafts, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"{len(drafts)} brouillons écrits dans {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
