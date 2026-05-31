"""Tests hors-ligne du générateur : on mocke le client LLM, aucune clé requise."""
import generator as gen
import persona


class _FakeClient:
    """Renvoie un texte prédéfini et capture le prompt reçu."""
    model = "fake-model"

    def __init__(self, reply: str):
        self.reply = reply
        self.last_system = None
        self.last_user = None

    def chat(self, system: str, user: str) -> str:
        self.last_system = system
        self.last_user = user
        return self.reply


ITEM = {
    "id": "abc123",
    "source": "So Foot",
    "title": "Le PSG recrute un 7e gardien",
    "summary": "Le club continue son mercato hivernal.",
    "link": "https://exemple.fr/psg",
}


def test_sanitize_quotes_and_prefix():
    assert gen.sanitize('"Salut tout le monde"') == "Salut tout le monde"
    assert gen.sanitize("Post : une punchline") == "une punchline"
    assert gen.sanitize("Tweet: autre") == "autre"
    print("sanitize (guillemets/prefixe) ok")


def test_sanitize_length():
    long = "mot " * 100  # 400 chars
    out = gen.sanitize(long, max_chars=280)
    assert len(out) <= 280, len(out)
    assert out.endswith("…")
    assert not out[:-1].endswith(" "), repr(out)  # coupe propre sur un mot
    print("sanitize (longueur) ok :", len(out), "chars")


def test_generate_post_basic():
    client = _FakeClient("  Le PSG cherche surtout un gardien pour ses gardiens. 🧤  ")
    d = gen.generate_post(ITEM, client=client, angle="punchline", few_shot=[])
    assert d.post == "Le PSG cherche surtout un gardien pour ses gardiens. 🧤"
    assert d.angle == "punchline"
    assert d.news_id == "abc123"
    assert d.source == "So Foot"
    assert d.model == "fake-model"
    assert d.ok_length is True
    assert d.chars == len(d.post)
    print("generate_post (base) ok :", d.post)


def test_prompt_contains_persona_angle_and_news():
    client = _FakeClient("ok")
    gen.generate_post(ITEM, client=client, angle="consultant_bidon", few_shot=[])
    assert "SATIRIQUE" in client.last_system
    assert persona.ANGLES["consultant_bidon"] in client.last_system
    assert "Le PSG recrute un 7e gardien" in client.last_user
    assert "mercato hivernal" in client.last_user
    print("prompt (persona+angle+actu) ok")


def test_unknown_angle_falls_back():
    client = _FakeClient("ok")
    d = gen.generate_post(ITEM, client=client, angle="nimporte_quoi", few_shot=[])
    assert d.angle == persona.DEFAULT_ANGLE
    print("angle inconnu -> défaut ok :", d.angle)


def test_few_shot_injected():
    client = _FakeClient("ok")
    shots = [{"news_title": "Exemple", "post": "PUNCHLINE_EXEMPLE_42"}]
    gen.generate_post(ITEM, client=client, angle="ironie", few_shot=shots)
    assert "PUNCHLINE_EXEMPLE_42" in client.last_user
    print("few-shot injecté ok")


def test_examples_file_loads():
    ex = gen.load_examples("ironie")
    assert ex and all("post" in e for e in ex)
    print("examples.json chargé ok :", len(ex), "exemples")


def run():
    test_sanitize_quotes_and_prefix()
    test_sanitize_length()
    test_generate_post_basic()
    test_prompt_contains_persona_angle_and_news()
    test_unknown_angle_falls_back()
    test_few_shot_injected()
    test_examples_file_loads()
    print("\n✅ Tous les tests générateur passent.")


if __name__ == "__main__":
    run()
