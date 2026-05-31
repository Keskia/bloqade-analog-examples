# ⚽😈 Foot Satire Bot

Compte X (Twitter) d'**actu foot au ton satirique**, avec posts générés
automatiquement, **validation humaine via Telegram**, et (à terme) un agent
qui apprend ce qui génère le plus d'impressions.

> État actuel : **briques 1-4** — source d'actu (`news_ingest.py`), générateur
> Mistral (`generator.py`), validation Telegram (`telegram_review.py`),
> publication X (`x_publisher.py`). Reste : la boucle d'apprentissage.

## Pipeline (état actuel)

```bash
export MISTRAL_API_KEY="..."                          # clé gratuite console.mistral.ai
export TELEGRAM_BOT_TOKEN="..." TELEGRAM_CHAT_ID="..." # bot @BotFather + ton chat id
export X_API_KEY="..." X_API_SECRET="..." \
       X_ACCESS_TOKEN="..." X_ACCESS_SECRET="..."      # app X (Read+Write)

python news_ingest.py --max-age 12 --out actus.json    # 1) actu gratuite
python generator.py --from actus.json --out drafts.json # 2) brouillons satiriques
python telegram_review.py --from drafts.json            # 3) validation ✅/✏️/❌ sur Telegram
python x_publisher.py --dry-run                         # 4a) aperçu de ce qui partirait
python x_publisher.py --max-posts 5 --min-interval 30   # 4b) publication réelle sur X
# approved.jsonl -> published.jsonl (tweet_id + URL, jamais publié deux fois)
```

Angles d'humour disponibles (`--angle`) : `ironie`, `fausse_breaking`,
`punchline`, `fan_depressif`, `consultant_bidon`. Chaque brouillon garde son
angle → la future boucle d'apprentissage saura lequel performe.

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
python test_news_ingest.py     # RSS, Atom, Reddit, dédup, tri, filtre (réseau mocké)
python test_generator.py       # sanitization, longueur, angles, few-shot (LLM mocké)
python test_telegram_review.py # approve/reject/edit, file, log, dispatch (Telegram mocké)
python test_x_publisher.py     # OAuth 1.0a (signature prouvée), dédup, idempotence, dry-run
```

> La signature OAuth est testée contre l'exemple **officiel** de la doc X
> (base string identique octet pour octet) et contre une référence `openssl`
> indépendante — la crypto maison est donc prouvée, sans dépendance externe.

---

## Architecture cible (100 % gratuit)

```
 ┌─────────────┐   ┌──────────────┐   ┌────────────────┐   ┌──────────┐   ┌───────────────┐
 │ news_ingest │──▶│  Générateur  │──▶│ Validation     │──▶│ Publish  │──▶│  Métriques    │
 │ (RSS+Reddit)│   │  (Mistral)   │   │ Telegram (HITL)│   │   X      │   │ + Apprentissage│
 └─────────────┘   └──────────────┘   └────────────────┘   └──────────┘   └───────────────┘
       ✅ FAIT          ✅ FAIT            ✅ FAIT             ✅ FAIT          à venir
```

1. **`news_ingest`** ✅ — récupère l'actu gratuitement.
2. **`generator`** ✅ — Mistral (free tier) + persona satirique (`persona.py`) +
   exemples few-shot (`examples.json`, remplaçables par les meilleurs posts réels).
   Garde-fous : ≤280 car., satire évidente, pas de diffamation, pas de fausses citations.
3. **`telegram_review`** ✅ — bot polling avec boutons `✅ Publier / ✏️ Éditer / ❌ Rejeter`.
   Humain dans la boucle, posts approuvés → `approved.jsonl`, décisions → `decisions.jsonl`.
4. **`x_publisher`** ✅ — API X v2 (écriture, free tier), OAuth 1.0a signé à la
   main (stdlib). Idempotent (dédup par `news_id`, jamais 2× le même post),
   plafond `--max-posts` + intervalle `--min-interval` anti-spam, mode `--dry-run`.
   Posts publiés → `published.jsonl` (tweet_id + URL). ⚠️ Les **réponses
   automatiques** et les **impressions par post** nécessitent le tier payant
   *Basic (~100 $/mois)* : tant qu'on reste gratuit, on publie sans mesurer finement.
5. **Apprentissage** — quand les métriques seront dispo : on logge chaque post +
   ses performances (SQLite), et un *bandit* privilégie les angles qui marchent,
   réinjectés en few-shot dans le générateur.

## Notes légales / ToS

- X impose depuis 2025 d'**étiqueter les comptes parodie** (nom + bio). À respecter.
- Satire ≠ fake news : éviter diffamation et fausses infos prises au 1er degré.
- Espacer les publications pour ne pas être flag comme spam.
