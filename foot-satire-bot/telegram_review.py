#!/usr/bin/env python3
"""
telegram_review.py — Validation humaine des posts via Telegram (mode POLLING).

Flux : on envoie chaque brouillon (issu de generator.py) dans ton chat Telegram
avec 3 boutons → ✅ Publier / ✏️ Éditer / ❌ Rejeter. Rien n'est publié sur X
sans ton accord. Les posts approuvés sont écrits dans une file (`approved.jsonl`)
que consommera la brique de publication X. Chaque décision est loggée
(`decisions.jsonl`) — c'est la donnée d'entraînement du futur agent.

Pas de serveur public requis (long polling getUpdates). Dépendance : `requests`.

Prérequis :
    1. Crée un bot via @BotFather -> récupère le token.
    2. Récupère ton chat_id (parle à ton bot puis getUpdates, ou @userinfobot).
    export TELEGRAM_BOT_TOKEN="123:ABC"
    export TELEGRAM_CHAT_ID="123456789"

Usage :
    python generator.py --from actus.json --out drafts.json
    python telegram_review.py --from drafts.json
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

HERE = Path(__file__).resolve().parent
DECISIONS_LOG = HERE / "decisions.jsonl"
APPROVED_QUEUE = HERE / "approved.jsonl"

BTN_APPROVE = "approve"
BTN_EDIT = "edit"
BTN_REJECT = "reject"


# --------------------------------------------------------------------------- #
# Client Telegram (réseau)
# --------------------------------------------------------------------------- #
class TelegramClient:
    def __init__(self, token: str | None = None, chat_id: str | None = None, timeout: int = 35):
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = str(chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")) or None
        if not self.token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN manquant.")
        self.timeout = timeout

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def _call(self, method: str, **params: Any) -> dict:
        r = requests.post(self._url(method), json=params, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method} a échoué : {data}")
        return data.get("result", {})

    def send_message(self, text: str, buttons: list[tuple[str, str]] | None = None,
                     chat_id: str | None = None) -> int:
        params: dict[str, Any] = {"chat_id": chat_id or self.chat_id, "text": text,
                                  "disable_web_page_preview": True}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": [[
                {"text": label, "callback_data": data} for label, data in buttons]]}
        return self._call("sendMessage", **params).get("message_id", 0)

    def edit_markup(self, chat_id: str, message_id: int, text_suffix: str = "") -> None:
        # Retire les boutons (on ne peut plus re-cliquer) et marque le statut.
        self._call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                   reply_markup={"inline_keyboard": []})
        if text_suffix:
            self._call("sendMessage", chat_id=chat_id, text=text_suffix,
                       reply_to_message_id=message_id, disable_web_page_preview=True)

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self._call("answerCallbackQuery", callback_query_id=callback_id, text=text)

    def get_updates(self, offset: int | None, timeout: int = 25) -> list[dict]:
        params = {"timeout": timeout, "allowed_updates": ["callback_query", "message"]}
        if offset is not None:
            params["offset"] = offset
        r = requests.post(self._url("getUpdates"), json=params, timeout=timeout + 10)
        r.raise_for_status()
        return r.json().get("result", [])


# --------------------------------------------------------------------------- #
# Logique de revue (PURE, sans réseau -> testable hors-ligne)
# --------------------------------------------------------------------------- #
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def callback_data(action: str, draft_id: str) -> str:
    return f"{action}:{draft_id}"


def parse_callback(data: str) -> tuple[str, str]:
    action, _, draft_id = data.partition(":")
    return action, draft_id


def buttons_for(draft_id: str) -> list[tuple[str, str]]:
    return [
        ("✅ Publier", callback_data(BTN_APPROVE, draft_id)),
        ("✏️ Éditer", callback_data(BTN_EDIT, draft_id)),
        ("❌ Rejeter", callback_data(BTN_REJECT, draft_id)),
    ]


def format_draft(d: dict[str, Any]) -> str:
    return (
        f"🗞 {d.get('source', '?')}\n"
        f"{d.get('title', '')}\n\n"
        f"📝 [{d.get('angle', '?')}] ({d.get('chars', len(d.get('post','')))}c)\n"
        f"{d.get('post', '')}\n\n"
        f"🔗 {d.get('link', '')}"
    )


class ReviewSession:
    """État de la revue + journalisation. Ne fait AUCUN appel réseau."""

    def __init__(self, log_path: Path = DECISIONS_LOG, approved_path: Path = APPROVED_QUEUE):
        self.log_path = Path(log_path)
        self.approved_path = Path(approved_path)
        self.pending: dict[str, dict[str, Any]] = {}   # draft_id -> {draft, message_id}
        self.awaiting_edit: str | None = None           # draft_id en attente d'un nouveau texte

    # -- enregistrement des brouillons envoyés -------------------------------
    def register(self, draft: dict[str, Any], message_id: int) -> None:
        self.pending[draft["news_id"]] = {"draft": dict(draft), "message_id": message_id}

    def has_pending(self) -> bool:
        return bool(self.pending) or self.awaiting_edit is not None

    # -- persistance ---------------------------------------------------------
    def _append(self, path: Path, obj: dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def _log(self, draft: dict[str, Any], action: str, edited: bool = False) -> None:
        self._append(self.log_path, {
            "ts": _now(), "draft_id": draft.get("news_id"), "action": action,
            "edited": edited, "angle": draft.get("angle"), "source": draft.get("source"),
            "final_post": draft.get("post"),
        })

    # -- actions -------------------------------------------------------------
    def handle_action(self, action: str, draft_id: str) -> dict[str, Any]:
        """Retourne une instruction pour le bot : {type, ...}. Aucun réseau ici."""
        entry = self.pending.get(draft_id)
        if entry is None:
            return {"type": "unknown", "toast": "Brouillon introuvable ou déjà traité."}
        draft = entry["draft"]
        mid = entry["message_id"]

        if action == BTN_APPROVE:
            self._append(self.approved_path, {"ts": _now(), **draft})
            self._log(draft, "approved", edited=draft.get("_edited", False))
            self.pending.pop(draft_id, None)
            return {"type": "approved", "message_id": mid, "toast": "✅ En file de publication"}

        if action == BTN_REJECT:
            self._log(draft, "rejected", edited=draft.get("_edited", False))
            self.pending.pop(draft_id, None)
            return {"type": "rejected", "message_id": mid, "toast": "❌ Rejeté"}

        if action == BTN_EDIT:
            self.awaiting_edit = draft_id
            return {"type": "await_edit", "message_id": mid,
                    "toast": "✏️ Envoie le nouveau texte du post"}

        return {"type": "unknown", "toast": "Action inconnue."}

    def apply_edit(self, new_text: str) -> dict[str, Any]:
        """Applique le texte reçu au brouillon en attente d'édition."""
        draft_id = self.awaiting_edit
        self.awaiting_edit = None
        if draft_id is None or draft_id not in self.pending:
            return {"type": "no_edit_pending"}
        draft = self.pending[draft_id]["draft"]
        draft["post"] = new_text.strip()
        draft["chars"] = len(draft["post"])
        draft["_edited"] = True
        self._log(draft, "edited", edited=True)
        return {"type": "edited", "draft": draft, "buttons": buttons_for(draft_id)}


# --------------------------------------------------------------------------- #
# Boucle de revue (branche la logique sur le réseau)
# --------------------------------------------------------------------------- #
def dispatch_update(update: dict[str, Any], client: TelegramClient, session: ReviewSession) -> None:
    if "callback_query" in update:
        cq = update["callback_query"]
        action, draft_id = parse_callback(cq.get("data", ""))
        chat_id = str(cq["message"]["chat"]["id"])
        result = session.handle_action(action, draft_id)
        client.answer_callback(cq["id"], result.get("toast", ""))
        if result["type"] in ("approved", "rejected"):
            client.edit_markup(chat_id, result["message_id"], result["toast"])
        elif result["type"] == "await_edit":
            client.send_message(result["toast"], chat_id=chat_id)
        return

    if "message" in update:
        msg = update["message"]
        text = msg.get("text", "")
        chat_id = str(msg["chat"]["id"])
        if text and session.awaiting_edit:
            res = session.apply_edit(text)
            if res["type"] == "edited":
                d = res["draft"]
                mid = client.send_message(format_draft(d), buttons=res["buttons"], chat_id=chat_id)
                session.pending[d["news_id"]]["message_id"] = mid


def run_review(drafts: list[dict[str, Any]], client: TelegramClient, session: ReviewSession,
               poll_timeout: int = 25, max_idle_loops: int | None = None) -> None:
    for d in drafts:
        mid = client.send_message(format_draft(d), buttons=buttons_for(d["news_id"]))
        session.register(d, mid)

    offset: int | None = None
    idle = 0
    while session.has_pending():
        updates = client.get_updates(offset, timeout=poll_timeout)
        if not updates:
            idle += 1
            if max_idle_loops is not None and idle >= max_idle_loops:
                break
            continue
        idle = 0
        for u in updates:
            offset = u["update_id"] + 1
            dispatch_update(u, client, session)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Validation Telegram des posts (polling).")
    ap.add_argument("--from", dest="src", required=True, help="JSON de brouillons (generator.py).")
    ap.add_argument("--poll-timeout", type=int, default=25)
    args = ap.parse_args(argv)

    drafts = json.loads(Path(args.src).read_text(encoding="utf-8"))
    if not drafts:
        print("Aucun brouillon à valider.")
        return 1
    try:
        client = TelegramClient()
    except RuntimeError as exc:
        print(str(exc)); return 2

    session = ReviewSession()
    print(f"Envoi de {len(drafts)} brouillons sur Telegram. Valide-les depuis ton chat…")
    run_review(drafts, client, session, poll_timeout=args.poll_timeout)
    print(f"Revue terminée. File de publication : {APPROVED_QUEUE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
