# ⚽😈 Foot Satire Bot

Compte X (Twitter) d'**actu foot au ton satirique**, avec posts générés
automatiquement, **validation humaine via Telegram**, et (à terme) un agent
qui apprend ce qui génère le plus d'impressions.

> État actuel : **brique 1 — la source d'actu gratuite** (`news_ingest.py`).
> Le reste de l'architecture est décrit plus bas.

---

## Pourquoi cette brique d'abord ?

Tout part de l'actu : impossible de faire de la satire foot sans savoir ce qui
se passe. On veut une source **100 % gratuite, sans clé API, sans quota payant**.

### La stratégie « actu gratuite »

| Source | Coût | Apporte |
|--------|------|---------|
| **Flux RSS** des médias foot (L'Équipe, So Foot, RMC, Foot Mercato, BBC…) | 0 € | Titres + résumés d'articles réels à détourner |
| **API JSON publique de Reddit** (`/r/ligue1`, `/r/soccer`) | 0 € | Ce qui *buzze* (score, nb de commentaires) = bons sujets viraux |

Pas de scraping HTML fragile, pas de tokens. Les flux sont déclarés dans
[`feeds.json`](./feeds.json) — ajoute/retire ce que tu veux.

---

## Installation

```bash
cd foot-satire-bot
pip install -r requirements.txt   # juste `requests`
```

`feedparser` est **optionnel** : s'il est installé, il est utilisé (plus robuste) ;
sinon le script bascule sur le parser RSS/Atom de la lib standard. Aucune
compilation requise.

## Utilisation

```bash
# 1) Vérifier quels flux répondent VRAIMENT depuis ta machine (à faire une fois)
python news_ingest.py --validate

# 2) Récupérer les dernières actus (JSON sur stdout)
python news_ingest.py --max-age 12 --limit 20

# 3) Écrire dans un fichier pour la suite du pipeline
python news_ingest.py --out actus.json

# 4) Ne garder que certaines sources (par tags définis dans feeds.json)
python news_ingest.py --tags fr mercato
```

Chaque actu est normalisée ainsi (prête pour le générateur) :

```json
{
  "id": "a1b2c3d4e5f6...",          // hash stable -> évite de reposter 2x
  "source": "So Foot",
  "title": "...",
  "summary": "...",                  // HTML nettoyé, tronqué
  "link": "https://...",
  "published": "2026-05-31T20:00:00+00:00",
  "published_ts": 1748722800.0,
  "lang": "fr",
  "tags": ["fr", "general"]
}
```

> ⚠️ Beaucoup de médias FR renvoient **403** aux bots. Le script envoie un
> User-Agent navigateur par défaut. Si un flux reste bloqué, `--validate` te le
> montre (`❌`) et tu remplaces l'URL dans `feeds.json`. Pense à personnaliser
> `USER_AGENT` (Reddit throttle les UA génériques).

## Tests

```bash
python test_news_ingest.py   # tests hors-ligne (réseau mocké) : RSS, Atom, Reddit, dédup, tri, filtre
```

---

## Architecture cible (100 % gratuit)

```
 ┌─────────────┐   ┌──────────────┐   ┌────────────────┐   ┌──────────┐   ┌───────────────┐
 │ news_ingest │──▶│  Générateur  │──▶│ Validation     │──▶│ Publish  │──▶│  Métriques    │
 │ (RSS+Reddit)│   │  (Mistral)   │   │ Telegram (HITL)│   │   X      │   │ + Apprentissage│
 └─────────────┘   └──────────────┘   └────────────────┘   └──────────┘   └───────────────┘
       ✅ FAIT            à venir            à venir            à venir          à venir
```

1. **`news_ingest`** ✅ — récupère l'actu gratuitement (cette brique).
2. **Générateur** — Mistral (free tier) + prompt de persona satirique + exemples
   few-shot des meilleurs posts passés.
3. **Validation Telegram** — bot avec boutons `✅ Publier / ✏️ Éditer / ❌ Rejeter`
   avant toute publication (humain dans la boucle).
4. **Publication X** — API gratuite (écriture). ⚠️ Les **réponses automatiques**
   et les **impressions par post** nécessitent le tier payant *Basic (~100 $/mois)* :
   tant qu'on reste gratuit, on publie sans répondre ni mesurer finement.
5. **Apprentissage** — quand les métriques seront dispo : on logge chaque post +
   ses performances (SQLite), et un *bandit* privilégie les angles qui marchent,
   réinjectés en few-shot dans le générateur.

## Notes légales / ToS

- X impose depuis 2025 d'**étiqueter les comptes parodie** (nom + bio). À respecter.
- Satire ≠ fake news : éviter diffamation et fausses infos prises au 1er degré.
- Espacer les publications pour ne pas être flag comme spam.
