"""Tests hors-ligne : on mocke le réseau pour valider le parsing/normalisation."""
import json
import types

import news_ingest as ni


class _FakeResp:
    def __init__(self, content=b"", payload=None, status=200):
        self.content = content
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _patch_http(monkey_value):
    ni._http_get = lambda url: monkey_value  # type: ignore


RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>Le PSG gagne 1-0</title>
    <description>Un &lt;b&gt;match&lt;/b&gt; fou ce soir.</description>
    <link>https://exemple.fr/psg</link>
    <pubDate>Sat, 31 May 2026 20:00:00 +0000</pubDate>
  </item>
  <item>
    <title>Sans date ni lien</title>
  </item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>OM en feu</title>
    <summary>Doublé inattendu</summary>
    <link rel="alternate" href="https://exemple.fr/om"/>
    <published>2026-05-31T18:30:00Z</published>
  </entry>
</feed>""".encode("utf-8")

REDDIT = {"data": {"children": [
    {"data": {"id": "abc", "title": "Mbappé fait un truc", "score": 1200,
              "num_comments": 89, "permalink": "/r/soccer/abc", "created_utc": 1748722800,
              "selftext": "", "stickied": False}},
    {"data": {"id": "pin", "title": "Sticky à ignorer", "stickied": True}},
]}}


def run():
    cfg_rss = {"name": "TestRSS", "url": "x", "lang": "fr", "tags": ["t"]}
    cfg_atom = {"name": "TestAtom", "url": "x", "lang": "fr", "tags": ["t"]}
    cfg_red = {"name": "TestReddit", "url": "x", "lang": "en", "tags": ["t"]}

    # --- RSS ---
    _patch_http(_FakeResp(content=RSS))
    items = ni.parse_rss(cfg_rss)
    assert len(items) == 2, items
    a = items[0]
    assert a.title == "Le PSG gagne 1-0"
    assert a.summary == "Un match fou ce soir.", repr(a.summary)  # HTML retiré + unescape
    assert a.link == "https://exemple.fr/psg"
    assert a.published_ts > 0 and a.published.startswith("2026-05-31")
    assert items[1].published_ts == 0  # date inconnue -> 0
    print("RSS ok :", a.title, "|", a.published)

    # --- Atom ---
    _patch_http(_FakeResp(content=ATOM))
    items = ni.parse_rss(cfg_atom)
    assert len(items) == 1
    assert items[0].link == "https://exemple.fr/om", items[0].link  # href d'Atom
    assert items[0].published_ts > 0
    print("Atom ok :", items[0].title, "|", items[0].link)

    # --- Reddit ---
    _patch_http(_FakeResp(payload=REDDIT))
    items = ni.parse_reddit(cfg_red)
    assert len(items) == 1  # le sticky est filtré
    assert "score=1200" in items[0].summary
    assert items[0].link == "https://www.reddit.com/r/soccer/abc"
    print("Reddit ok :", items[0].title, "|", items[0].summary[:30])

    # --- dédup + tri dans fetch_all ---
    calls = {"n": 0}

    def fake_parser(cfg):
        calls["n"] += 1
        return [
            ni.NewsItem("DUP", "S", "ancien", "", "", None, 100.0, "fr", []),
            ni.NewsItem("DUP", "S", "doublon", "", "", None, 100.0, "fr", []),
            ni.NewsItem("NEW", "S", "recent", "", "", None, 999.0, "fr", []),
        ]

    ni.PARSERS["rss"] = fake_parser
    merged = ni.fetch_all([{"name": "F", "type": "rss"}])
    assert [m.id for m in merged] == ["NEW", "DUP"], merged  # dédup + tri récent d'abord
    print("fetch_all ok : dédup + tri")

    # --- filtre d'âge ---
    import time
    now = time.time()
    fresh = ni.NewsItem("f", "S", "f", "", "", None, now - 60, "fr", [])
    old = ni.NewsItem("o", "S", "o", "", "", None, now - 10 * 3600, "fr", [])
    undated = ni.NewsItem("u", "S", "u", "", "", None, 0.0, "fr", [])
    kept = ni.filter_recent([fresh, old, undated], max_age_hours=1)
    assert {k.id for k in kept} == {"f", "u"}, kept  # vieux exclu, sans-date gardé
    print("filter_recent ok")

    print("\n✅ Tous les tests passent.")


if __name__ == "__main__":
    run()
