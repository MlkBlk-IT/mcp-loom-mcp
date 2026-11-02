# Changelog

## 2026-11-02 - restauration du fix P9 + isolation de session

### Fixed
- Restauration du fix P9, perdu apres le merge distant (code reste dans un stash): quota d'invariants dans l'ancre (`MAX_NUMBERS_IN_ANCHOR`), rerank de la recherche + `topup` OR apres AND strict, kind `port`, `_store_impl`, hints d'encodage (UTF-16 / cp1252), schemas MCP sans `anyOf`, transport http/sse.
- `vector_index.rebuild_from_db` lit la DB chiffree (`crypto.connect_encrypted`); `NameError` sqlite3 corrige.
- `vector_index.save()` apres `save_all`: l'index vectoriel persiste.
- Sandbox Windows: limites Job Object (memoire, processus, kill-on-close), fail closed si le process n'est pas assigne au job.
- `ctx_get(chunk_id, session_id)`: isolation de session (chunk d'une autre session lisible seulement apres `ctx_transfer`), y compris pour les refs `<sha12>#<idx>`.
- `_path_allowed`: realpath + commonpath (plus de faux positif par prefixe, pas d'echappement par symlink).

### Changed
- Tests alignes sur les 17 outils (ajout de `ctx_transfer`, `ctx_rebuild_index`, `ctx_vector_stats`) + 6 tests d'isolation de session. Suite: 110 passed.
- Dashboard (`dash/`): economie affichee = net estime (store - ancres - relectures), sources dedupliquees par sha256, detail `saved_detail` dans `/api/snapshot`.
- `.gitignore`: `_test/` et `*.bak_*`.

### Known
- Go/no-go 35 % (tokens totaux in+out) toujours non mesure: bench cloud requis.
- Le projet reste WIP.
