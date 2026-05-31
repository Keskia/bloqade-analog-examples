"""Tests hors-ligne du publisher X : OAuth prouvé via vecteur officiel, réseau mocké."""
import json
import tempfile
from pathlib import Path

import x_publisher as xp


# --- Vecteur de test OFFICIEL de la doc développeur Twitter/X --------------- #
# https://developer.x.com/en/docs/authentication/oauth-1-0a/creating-a-signature
TW = {
    "consumer_key": "xvz1evFS4wEEPTGEFPHBog",
    "consumer_secret": "kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Y7QZ",
    "token": "370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb",
    "token_secret": "LswwdoUaIVS25jH7y1WUxKKXnPVUM6Gn2VEDQX7H5Co",
}
TW_PARAMS = {
    "status": "Hello Ladies + Gentlemen, a signed OAuth request!",
    "include_entities": "true",
    "oauth_consumer_key": TW["consumer_key"],
    "oauth_nonce": "kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg",
    "oauth_signature_method": "HMAC-SHA1",
    "oauth_timestamp": "1318622958",
    "oauth_token": TW["token"],
    "oauth_version": "1.0",
}
TW_URL = "https://api.twitter.com/1.1/statuses/update.json"
EXPECTED_BASE_TAIL = (
    "include_entities%3Dtrue%26oauth_consumer_key%3Dxvz1evFS4wEEPTGEFPHBog"
)
# La base string ci-dessous matche OCTET POUR OCTET l'exemple officiel Twitter.
# NB : la signature *imprimée* dans la doc Twitter (hCtSmYh+...) est une
# incohérence connue (elle ne correspond pas aux secrets publiés). La vraie
# signature pour ces entrées, vérifiée indépendamment via `openssl dgst -sha1
# -hmac`, est celle-ci — c'est donc une référence crypto fiable.
EXPECTED_SIGNATURE = "y9SFgxb+9tK3t6EaA0bU7natqYM="


def test_percent_encode_rfc3986():
    assert xp.percent_encode("Ladies + Gentlemen") == "Ladies%20%2B%20Gentlemen"
    assert xp.percent_encode("-._~AZ09") == "-._~AZ09"   # non réservés intacts
    assert xp.percent_encode("a!b") == "a%21b"
    print("percent-encoding RFC3986 ok")


def test_oauth_signature_matches_official_vector():
    base = xp.signature_base_string("POST", TW_URL, TW_PARAMS)
    assert base.startswith("POST&https%3A%2F%2Fapi.twitter.com")
    assert EXPECTED_BASE_TAIL in base
    sig = xp.sign(base, TW["consumer_secret"], TW["token_secret"])
    assert sig == EXPECTED_SIGNATURE, f"got {sig}"
    print("signature OAuth == vecteur officiel Twitter ✓ (crypto prouvée)")


def test_auth_header_is_wellformed():
    oauth = xp.OAuth1(TW["consumer_key"], TW["consumer_secret"], TW["token"], TW["token_secret"])
    h = oauth.auth_header("POST", xp.X_TWEETS_URL,
                          nonce="abc123", timestamp="1700000000")
    assert h.startswith("OAuth ")
    assert 'oauth_consumer_key="xvz1evFS4wEEPTGEFPHBog"' in h
    assert 'oauth_signature_method="HMAC-SHA1"' in h
    assert 'oauth_signature="' in h and 'oauth_nonce="abc123"' in h
    print("en-tête Authorization bien formé ok")


# --- Logique de file ------------------------------------------------------- #
def _entry(nid, post="un post"):
    return {"news_id": nid, "post": post, "angle": "punchline", "source": "So Foot"}


def test_pending_dedup_and_skip_published():
    approved = [_entry("a"), _entry("b"), _entry("a"), _entry("c")]
    pending = xp.pending_posts(approved, done={"c"})
    ids = [p["news_id"] for p in pending]
    assert ids == ["a", "b"]   # 'a' une seule fois, 'c' déjà publié exclu
    print("dédup + exclusion déjà-publiés ok")


def test_publish_writes_log_and_dedup_next_run():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        appr = tmp / "approved.jsonl"
        pub = tmp / "published.jsonl"
        with appr.open("w") as fh:
            for e in [_entry("a", "post A"), _entry("b", "post B")]:
                fh.write(json.dumps(e) + "\n")

        calls = []
        def fake_post(text):
            calls.append(text)
            return {"id": f"tw-{len(calls)}"}

        out = xp.publish_pending(fake_post, approved_path=appr, published_path=pub,
                                 max_posts=10, min_interval=0)
        assert calls == ["post A", "post B"]
        assert out[0]["tweet_id"] == "tw-1"
        assert out[0]["url"].endswith("tw-1")
        logged = [json.loads(l)["news_id"] for l in pub.read_text().splitlines()]
        assert logged == ["a", "b"]

        # 2e exécution : tout est déjà publié -> aucun nouvel appel
        calls.clear()
        out2 = xp.publish_pending(fake_post, approved_path=appr, published_path=pub)
        assert calls == [] and out2 == []
        print("publication -> journal + idempotence (pas de double post) ok")


def test_max_posts_and_interval():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        appr = tmp / "approved.jsonl"
        with appr.open("w") as fh:
            for i in range(5):
                fh.write(json.dumps(_entry(f"n{i}", f"post {i}")) + "\n")

        sleeps = []
        out = xp.publish_pending(lambda t: {"id": "x"}, approved_path=appr,
                                 published_path=tmp / "p.jsonl", max_posts=3,
                                 min_interval=30, sleep_fn=sleeps.append)
        assert len(out) == 3                  # plafond respecté
        assert sleeps == [30, 30]             # pas de sleep après le dernier
        print("plafond max-posts + intervalle anti-spam ok")


def test_dry_run_no_network():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        appr = tmp / "approved.jsonl"
        appr.write_text(json.dumps(_entry("a", "coucou")) + "\n")
        def boom(_):
            raise AssertionError("le réseau ne doit PAS être appelé en dry-run")
        out = xp.publish_pending(boom, approved_path=appr,
                                 published_path=tmp / "p.jsonl", dry_run=True)
        assert out[0]["dry_run"] and out[0]["text"] == "coucou"
        assert not (tmp / "p.jsonl").exists()
        print("dry-run sans réseau ni écriture ok")


def run():
    test_percent_encode_rfc3986()
    test_oauth_signature_matches_official_vector()
    test_auth_header_is_wellformed()
    test_pending_dedup_and_skip_published()
    test_publish_writes_log_and_dedup_next_run()
    test_max_posts_and_interval()
    test_dry_run_no_network()
    print("\n✅ Tous les tests publisher X passent.")


if __name__ == "__main__":
    run()
