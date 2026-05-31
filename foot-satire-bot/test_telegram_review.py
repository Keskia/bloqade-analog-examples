"""Tests hors-ligne du flux Telegram : réseau et persistance mockés."""
import json
import tempfile
from pathlib import Path

import telegram_review as tr


class FakeTelegram:
    """Capture tout ce qui serait envoyé à Telegram."""
    def __init__(self):
        self.sent = []          # (text, buttons, chat_id)
        self.edited = []        # (chat_id, message_id, suffix)
        self.toasts = []        # (callback_id, text)
        self._mid = 100

    def send_message(self, text, buttons=None, chat_id=None):
        self._mid += 1
        self.sent.append((text, buttons, chat_id))
        return self._mid

    def edit_markup(self, chat_id, message_id, text_suffix=""):
        self.edited.append((chat_id, message_id, text_suffix))

    def answer_callback(self, callback_id, text=""):
        self.toasts.append((callback_id, text))


def _session(tmp):
    return tr.ReviewSession(log_path=tmp / "dec.jsonl", approved_path=tmp / "appr.jsonl")


DRAFT = {"news_id": "id1", "source": "So Foot", "title": "Un titre",
         "post": "une punchline", "angle": "punchline", "chars": 13,
         "link": "https://x.fr/a", "model": "m"}


def _cb_update(action, draft_id, chat_id=42, uid=1):
    return {"update_id": uid, "callback_query": {
        "id": "cb1", "data": tr.callback_data(action, draft_id),
        "message": {"chat": {"id": chat_id}, "message_id": 101}}}


def _msg_update(text, chat_id=42, uid=2):
    return {"update_id": uid, "message": {"chat": {"id": chat_id}, "text": text}}


def test_callback_roundtrip():
    assert tr.parse_callback(tr.callback_data("approve", "abc")) == ("approve", "abc")
    print("callback encode/decode ok")


def test_approve_writes_queue_and_log():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        s = _session(tmp)
        s.register(DRAFT, message_id=101)
        res = s.handle_action(tr.BTN_APPROVE, "id1")
        assert res["type"] == "approved"
        assert "id1" not in s.pending  # retiré de la file
        appr = (tmp / "appr.jsonl").read_text().strip()
        assert json.loads(appr)["post"] == "une punchline"
        log = json.loads((tmp / "dec.jsonl").read_text().strip())
        assert log["action"] == "approved" and log["draft_id"] == "id1"
        print("approve -> file + log ok")


def test_reject_logs_only():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        s = _session(tmp)
        s.register(DRAFT, 101)
        res = s.handle_action(tr.BTN_REJECT, "id1")
        assert res["type"] == "rejected"
        assert not (tmp / "appr.jsonl").exists()  # rien en file
        assert json.loads((tmp / "dec.jsonl").read_text().strip())["action"] == "rejected"
        print("reject -> log seul, pas de file ok")


def test_edit_flow_updates_post():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        s = _session(tmp)
        s.register(DRAFT, 101)
        r1 = s.handle_action(tr.BTN_EDIT, "id1")
        assert r1["type"] == "await_edit" and s.awaiting_edit == "id1"
        r2 = s.apply_edit("nouveau texte plus drôle")
        assert r2["type"] == "edited"
        assert s.pending["id1"]["draft"]["post"] == "nouveau texte plus drôle"
        assert s.pending["id1"]["draft"]["_edited"] is True
        assert s.awaiting_edit is None
        # puis approbation -> le texte édité part en file
        s.handle_action(tr.BTN_APPROVE, "id1")
        appr = json.loads((tmp / "appr.jsonl").read_text().strip())
        assert appr["post"] == "nouveau texte plus drôle"
        actions = [json.loads(l)["action"] for l in (tmp / "dec.jsonl").read_text().splitlines()]
        assert actions == ["edited", "approved"]
        print("edit -> maj post + approbation ok")


def test_unknown_draft_is_safe():
    with tempfile.TemporaryDirectory() as td:
        s = _session(Path(td))
        res = s.handle_action(tr.BTN_APPROVE, "inconnu")
        assert res["type"] == "unknown"
        print("draft inconnu géré proprement ok")


def test_dispatch_full_loop_with_fake_client():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        client = FakeTelegram()
        s = _session(tmp)
        s.register(DRAFT, 101)
        # clic Éditer -> bot demande le texte
        tr.dispatch_update(_cb_update("edit", "id1"), client, s)
        assert any("nouveau texte" in t[0].lower() or "Envoie" in t[0] for t in client.sent)
        # l'utilisateur envoie le nouveau texte -> renvoi du brouillon avec boutons
        tr.dispatch_update(_msg_update("version corrigée"), client, s)
        last_text, last_buttons, _ = client.sent[-1]
        assert "version corrigée" in last_text and last_buttons is not None
        # clic Publier -> markup retiré + toast
        tr.dispatch_update(_cb_update("approve", "id1", uid=3), client, s)
        assert client.edited and client.toasts
        assert json.loads((tmp / "appr.jsonl").read_text().strip())["post"] == "version corrigée"
        print("dispatch complet (edit->msg->approve) ok")


def run():
    test_callback_roundtrip()
    test_approve_writes_queue_and_log()
    test_reject_logs_only()
    test_edit_flow_updates_post()
    test_unknown_draft_is_safe()
    test_dispatch_full_loop_with_fake_client()
    print("\n✅ Tous les tests Telegram passent.")


if __name__ == "__main__":
    run()
