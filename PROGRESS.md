# Mcp-Loom — PROGRESS

Généré le 2026-10-25 à partir du code source (`src/loom/`), de la suite de tests **exécutée
localement**, de l'historique git et des documents internes (`README.md`, `docs/DEVELOPMENT_LOG.md`,
`docs/ROADMAP.md`, `docs/NOTE-PROJET.md`, `PHASE1_REPORT.md`, `PROJECT_STORY.md`).

Convention de marquage :
- **[mesuré]** — chiffre releuvé par exécution le 2026-10-25.
- **[lu]** — déduit du code ou d'un document, sans exécution.
- **[non vérifié]** — affirmation non confirmée par une source disponible.

---

## 1. Identité

| Champ | Valeur |
|---|---|
| Dépôt | https://github.com/MlkBlk-IT/mcp-loom-mcp |
| Renommage | `Loom` → `Mcp-Loom` le 2026-10-22 (commit `183afdc`) puis `Mcp-Loom` → `mcp-loom-mcp` le 2026-10-26 (nom de paquet : `mcp-loom` est occupé par deux projets sans rapport sur PyPI et npm) |
| Paquet | `mcp-loom-mcp` 0.1.2 — MIT — Python ≥ 3.11 |
| Point d'entrée | `loom.server:main` (`loom-mcp` en console) |
| Dépendances | `mcp>=1.2.0`, `fastmcp>=2.0.0`, `tiktoken>=0.7.0` |
| Extras | `semantic` (`faiss-cpu`, `sentence-transformers`, `numpy`) · `dev` (`pytest`, `pytest-asyncio`, `ruff`) |
| Langue | code, docs et commits en **français** ; seul `README.md` est en anglais (bandeau d'avertissement en tête) |
| Environnement de dev | Windows 11, Python 3.12.10, venv `D:\Loom\.venv` |

## 2. Architecture — 17 modules dans `src/loom/` [lu]

| Module | Rôle | État |
|---|---|---|
| `server.py` | Serveur FastMCP — 17 outils, couche mince, zéro logique métier | ⚠️ en refactor |
| `anchors.py` | Extracteur d'invariants (nombres, chemins, URLs, versions, hashes, ports, contraintes) | ⚠️ régression `port` |
| `compress.py` | Construction de l'ancre + découpage en chunks, garantie byte-identique | ⚠️ API changée |
| `store.py` | Tiroir SQLite + index FTS5 (chunks, ancres, invariants) | ok |
| `verify.py` | Contrôle de couverture — preuve que rien n'est perdu | ok |
| `profile.py` | 3 profils : `strict` (zéro perte) / `balanced` / `aggressive` (lossy, opt-in explicite) | ok |
| `tokenizer.py` | Comptage de tokens (`tiktoken` `cl100k_base`, repli heuristique) | ok |
| `crypto.py` | Chiffrement dual : SQLCipher (base) ou Fernet (colonnes) | ok |
| `redact.py` | Masquage des secrets (`Bearer`, `sk-`, `password=`, `secret=`, `token=`) | ok |
| `sandbox.py` | Sandbox d'exécution (Job Object Windows / bwrap-firejail Linux), fail-closed | ok |
| `ratelimit.py` | Token bucket par session / IP | ok |
| `audit.py` | Logs d'audit structlog JSONL, rotation 30 jours | ok |
| `session.py` | Sessions UUIDv7, TTL, purge, isolation | ok |
| `auto_store.py` | Stockage automatique de sortie d'outils | ok |
| `cross_session.py` | Opérations inter-sessions | ok |
| `vector_index.py` | Index vectoriel (recherche sémantique optionnelle) | ok |

## 3. Outils MCP exposés — 17 [mesuré]

Relevé sur les décorateurs `@mcp.tool` de `src/loom/server.py`.

| # | Outil | Rôle |
|---|---|---|
| 1 | `ctx_store` | Déplace un contenu volumineux dans le tiroir → ancre + IDs + rapport d'économie |
| 2 | `ctx_store_file` | Idem, lecture **côté serveur** (le volume ne transite jamais par le prompt) |
| 3 | `ctx_store_cmd` | Idem, depuis une commande — opt-in (`LOOM_STORE_CMD=1`) et sandboxée |
| 4 | `ctx_get` | Récupère un chunk, byte pour byte |
| 5 | `ctx_search` | Recherche FTS5 sur les chunks stockés → `chunk_id`, `score`, `snippet` |
| 6 | `ctx_check` | Vérifie qu'aucun invariant critique n'a été perdu (filet de sécurité) |
| 7 | `ctx_verify` | Vérification de couverture, par session ou globale |
| 8 | `ctx_stats` | Tokens avant/après, hit rate, ratio |
| 9 | `ctx_profile` | Change de profil : `strict` \| `balanced` \| `aggressive` |
| 10 | `ctx_list_sessions` | Liste les sessions (y compris expirées) |
| 11 | `ctx_purge_session` | Purge une session |
| 12 | `ctx_purge_expired` | Purge les sessions expirées (TTL) |
| 13 | `ctx_vacuum` | Compacte la base SQLite |
| 14 | `ctx_health` | État de santé du tiroir |
| 15 | `ctx_transfer` | Transfère des chunks entre deux sessions |
| 16 | `ctx_rebuild_index` | Reconstruit l'index de recherche |
| 17 | `ctx_vector_stats` | Statistiques de l'index vectoriel |

Les opérations d'administration (`purge`, `vacuum`, `health`, `list_sessions`) sont aussi
disponibles en CLI : `python -m loom <cmd>`.

## 4. État des tests — **83 passés, 19 échoués, 1 erreur de collection** [mesuré]

```
cd D:\Loom
.venv\Scripts\python.exe -m pytest -q
```

103 fonctions de test réparties dans 20 fichiers. La suite **ne collecte pas** en l'état : il faut
ignorer `tests/test_allowed_dirs.py` pour obtenir un décompte (`--ignore=tests/test_allowed_dirs.py`).

> Le README annonce « 72 tests pass » et « 13 MCP tools ». Les deux chiffres sont périmés.

### 4.1 Erreur de collection — 1

| Fichier | Cause |
|---|---|
| `tests/test_allowed_dirs.py` | `from loom.server import _path_allowed` → `ImportError`. La fonction a été remplacée par un contrôle en ligne de `LOOM_ALLOWED_DIRS` (`src/loom/server.py:408`). **Test non mis à jour, le code est plus récent que le test.** |

### 4.2 Les 19 échecs, par cause [mesuré]

| Fichier | Nb | Cause |
|---|---|---|
| `test_anchor_port_case.py` | 2 | Le kind `port` n'est plus produit : `PORT = 8765` → `{env_var, number}`, `Port: 3000` → `{number}` |
| `test_anchor_rare_kinds.py` | 1 | Les kinds rares n'atteignent plus l'ancre en profil `strict` |
| `test_fixes_p2.py` | 4 | 3 causes : `compress.MAX_NUMBERS_IN_ANCHOR` supprimé (→ `MAX_INVARIANTS_IN_ANCHOR`), `server._store_impl` supprimé (×2), les dates sont encore extraites comme nombres |
| `test_schema_compat.py` | 1 | `anyOf` réapparu dans les schémas de `ctx_get` et `ctx_verify` |
| `test_search_rerank.py` | 3 | `search()` ne reçoit plus le kwarg `topup` |
| `test_server_integration.py` | 1 | Liste d'outils non mise à jour : `+ctx_transfer`, `+ctx_rebuild_index`, `+ctx_vector_stats` |
| `test_stdio_server.py` | 1 | Même assertion que ci-dessus, via le transport stdio |
| `test_store_file_encoding.py` | 2 | L'indication d'encodage n'est plus produite : les fichiers UTF-16 / cp1252 lèvent « invalid utf-8 » au lieu de renvoyer un hint |
| `test_transport.py` | 4 | Le transport HTTP/SSE n'est plus configuré (transport par défaut `()`), plus de `SystemExit` sur transport invalide, plus d'avertissement « no authentication » |

**Lecture d'ensemble :** la quasi-totalité des échecs vient d'un **refactor du code non suivi d'une
mise à jour des tests**, pas d'un bug fonctionnel manifeste — sauf les 4 échecs d'ancres (`port`,
kinds rares, dates), qui sont de vraies **régressions de comportement**.

## 5. Écarts entre le README et le code [mesuré]

| Sujet | README | Code réel |
|---|---|---|
| Nombre d'outils MCP | 13 | **17** |
| Tests | « 72 tests pass » | **83 passés / 19 échoués / 1 erreur de collection** |
| Module `verify` | « needs to be rewritten » | Le module existe et ses tests passent |
| `redact`, `audit`, `sandbox` | « still need to be rewritten » | Les modules existent, leurs tests passent |
| Bench `saved_pct` 94–99 % | présenté comme valide | **Invalidé** (voir §6) |
| `dist/` | présent dans la liste | le README signale lui-même que `dist/` peut être périmé |

## 6. Benchmark — pourquoi les chiffres publiés sont invalides

Le bandeau WIP du README (2026-10-22) invalide explicitement les chiffres historiques :

- Les `saved_pct` de 94–99 % mesuraient **la taille de l'ancre seule**, pas les tokens totaux.
- Mesure réelle actuelle (LM Studio, petits modèles locaux, petits documents) : **Loom coûte plus de
  tokens que la baseline** (surcharge fixe de l'ancre + schémas d'outils).
- Le test « grand contexte » avec un modèle cloud **n'a pas été fait**.
- `bench_results.json` et `bench-report.json` **contiennent encore les chiffres invalidés** (jusqu'à
  `saved_pct: 99.95` sur les fichiers `large_*`, avec `coverage` qui s'effondre à 0.018–0.4) et
  `PHASE1_REPORT.md` porte un errata en tête qui le déclare périmé.

**Critère de décision gelé avant le bench :** même taux de succès avec **≥ 35 % de tokens en moins**
(en entrée + sortie, taxes de récupération et d'outils incluses). Sinon : pivot ou arrêt.

## 7. Historique git [lu]

| Commit | Date | Sujet |
|---|---|---|
| `a8f052a` | 2026-10-21 | Commit initial : projet Loom complet — serveur MCP de contexte portable |
| `d62e92c` | 2026-10-22 | WIP (pas prêt) : réparation du cœur, 72 tests, bandeau et ERRATA |
| `183afdc` | 2026-10-22 | Renommage du dépôt `Loom` → `Mcp-Loom` (URLs `pyproject.toml`) |
| `9d96111` | 2026-10-26 | Renommage du dépôt `Mcp-Loom` → `mcp-loom-mcp` + paquet PyPI/npm `mcp-loom-mcp` (l'ancienne URL GitHub redirige) |
| `d5ced73` | 2026-10-22 | README : traduction anglaise + refonte dark neon red/green |
| `99d8261` | 2026-10-24 | docs : bandeau d'avertissement « rest of the repo in French » |
| `9044522` | 2026-10-25 | Sync des changements locaux avant merge distant |
| `30e6e09` | 2026-10-25 | Merge de `main` depuis le remote |
| `05277f8` | 2026-10-25 | chore : ignorer les artefacts SQLite WAL/SHM |
| _(ce commit)_ | 2026-10-25 | feat : packaging — CLI d'administration, wrapper npm, sdist allégé |

### Branche `loom-semantic`
Branche **stale** : 0 commit unique par rapport à `origin/main` (4 commits en arrière). Elle est
entièrement contenue dans `main`. Aucun push nécessaire — à supprimer quand la migration est close.

## 8. État des documents internes [lu]

| Document | État |
|---|---|
| `README.md` | English, banner WIP à jour sur les *chiffres*, mais table d'outils et compte de tests périmés |
| `docs/ROADMAP.md` | **Toutes les cases sont `[ ]`** alors que P0 est implémenté → tableau de bord inutilisable |
| `docs/DEVELOPMENT_LOG.md` | ADR-001 à ADR-006, à jour jusqu'au 2026-10-21 ; « Next Steps » obsolète |
| `docs/NOTE-PROJET.md` | Contexte projet |
| `docs/INSTALL.md` | Procédure d'installation |
| `PHASE1_REPORT.md` | Porte un errata en tête : « rapport périmé et partiellement contredit » |
| `PROJECT_STORY.md` | Récit du projet |
| `bench_results.json`, `bench-report.json`, `REPORT_FINAL.md` | Contiennent les chiffres invalidés |

## 9. Distribution et installation [mesuré]

`pyproject.toml` (hatchling) produit deux artefacts :

| Artefact | Taille | Contenu |
|---|---|---|
| `dist/loom_mcp-0.1.0-py3-none-any.whl` | ~53 Ko | 17 modules + `entry_points.txt` + LICENSE |
| `dist/loom_mcp-0.1.0.tar.gz` | ~85 Ko | code + tests + docs (56 fichiers) |

Le sdist exclut volontairement le corpus de benchmark (`dataset/`, ~10 Mo), le
dashboard de dev (`dash/`, captures PNG + `.bak`) et les `bench-*.json` — le
sdist est passé de **4,1 Mo à 85 Ko**. Aucun `.db`, `.db-shm`, `.db-wal` non
plus.

### Points d'entrée

| Commande | Cible | Rôle |
|---|---|---|
| `loom-mcp` | `loom.server:main` | Le serveur MCP sur stdio — ce que les clients MCP lancent |
| `loom` | `loom.cli:main` | CLI d'administration (nouveau) |
| `python -m loom` | idem | Équivalent de `loom` |

La CLI d'administration expose `health`, `stats`, `sessions`, `search`, `vacuum`,
`purge`, `purge-expired`, `serve`, `version` — elle appelle les outils MCP
existants, elle n'invente aucune logique. Elle comble une promesse du README
qui n'existait pas : `python -m loom <cmd>` était documenté mais aucun CLI
n'était implémenté.

### Wrapper npm

`package.json` + `bin/loom-mcp.js` — pour les clients Node-only. Le wrapper
localise un Python capable de lancer Loom, dans cet ordre : `loom-mcp` déjà sur
le PATH, puis `uv tool run --from git+… loom-mcp`, puis `python -m loom`. Il
n'écrit **jamais** sur stdout (stdout appartient au JSON-RPC sur stdio).

### Vérifications effectuées [mesuré]

| Contrôle | Résultat |
|---|---|
| `pip install` du wheel dans un venv vierge (Python 3.12) | OK, 75 paquets résolus |
| `pip install` du sdist dans le même venv | OK (build depuis les sources) |
| Handshake MCP stdio sur `loom-mcp.exe` | OK — **17 outils** listés |
| Handshake MCP stdio via `node bin/loom-mcp.js` | OK — **17 outils** listés |
| `loom version` / `loom health` | OK — health lit le vrai `~/.loom/loom.db` |
| Suite de tests contre le **paquet installé** (cwd hors repo) | 83 passés / 19 échoués — **identique au source** |
| `npm pack` | 4 fichiers, 8,2 Ko |

## 10. Points de vigilance

1. **La suite de tests ne passe pas en l'état** (19 échecs + 1 erreur de collection). Toute
   affirmation de type « tout est vert » est à corriger avant publication.
2. **Les chiffres de `bench_results.json` sont périmés** et circulent encore dans le dépôt.
3. **Le `ROADMAP.md` ne reflète plus la réalité** — il ne peut pas servir de suivi.
4. **Aucune économie de tokens n'est démontrée** à ce jour sur des modèles locaux. Le critère des
   35 % n'a pas été evalué.
5. `tests/test_allowed_dirs.py` empêche toute exécution de `pytest` sans option additional.

## 11. Prochaines étapes proposées

0. **Publier les artefacts** : `dist/` est ignoré par git, donc les wheel/sdist
   ne sont pas versionnés. Les pousser en GitHub Releases (et/ou PyPI) les rend
   installables par `pip install mcp-loom-mcp` sans cloner le repo.
1. Réaligner la suite de tests sur le code : corriger l'import `_path_allowed`, la liste d'outils,
   les signatures `_store_impl` / `MAX_NUMBERS_IN_ANCHOR` / `topup`, les schémas `anyOf`, l'encodage
   des fichiers, et le transport HTTP/SSE. → **Objectif : `pytest` vert sans option.**
2. Traiter les 4 régressions d'ancres (`port`, kinds rares, dates-as-nombres) — ce sont les seules
   qui touchent au comportement, pas à la signature.
3. Corriger `ROADMAP.md` et réécrire la section « Current state » du README (17 outils, état réel des tests).
4. Supprimer ou ré-étiqueter `bench_results.json` / `bench-report.json` / `REPORT_FINAL.md` pour
   qu'aucun chiffre invalide ne soit repris par erreur.
5. Rejouer le bench « grand contexte » avec un modèle cloud pour statuer le critère des 35 %.
6. Supprimer la branche `loom-semantic` une fois la migration close.

## 12. Session du 2026-11-04 — Dashboard v4 refonte + repair

**Périmètre :** `dash/` uniquement (aucun changement du moteur `src/loom/`).

- **Refonte visuelle et simplifiée** : palette claire lumineuse (glassmorphism, gradients radial),
  panneaux simplifiés, détails déplacés dans un drawer latéral accessible via clic,
  graphiques simples en canvas (calls/min, latence p50/p95, savings), suppression du blablabla.
- **Sections doc en anglais** : 3 nouvelles sections nav — **Quickstart**, **How-to**, **Help** —
  rendues en anglais, comme le reste du dashboard (`lang="en"`, `?v=8`).
- **Debug SSE :** l'état « connecting... » permanent + cartes vides n'était pas CORS au premier
  abord — cause racine : **`SyntaxError` dans `dash/static/app.js:901`**
  (`linked+' 'sessions linked'` introduit par la traduction EN — tout le script ne s'exécutait
  pas). Fix : `linked+' sessions linked'` ; `node --check dash/static/app.js` → exit 0.
- **Fix secondaire server.py** : headers `Access-Control-Allow-Origin: *` +
  `Access-Control-Allow-Headers: Cache-Control` sur le endpoint `GET /api/stream` (SSE), pour les
  navigateurs stricts sur EventSource.
- **Vérifications mesurées** : `curl http://localhost:8775/api/snapshot` → 200, `db.exists:true`,
  48 sessions, 1684 chunks, 171 sources, `saved_ratio: 0.5481` ; `curl /api/stream` → 200
  `text/event-stream` avec headers CORS et flux `data:`.
- **Cache-busting :** `?v=7` → `?v=8` sur `app.js`/`style.css` dans `index.html`.
- **Reste / non vérifié :** rendu navigateur complet post hard-refresh `Ctrl+Shift+R` non confirmé
  par capture dans cette session (serveur OK, JS parse OK) ; suite de tests moteur non rejouée
  (inchangée — voir §4, état 2026-10-25).
