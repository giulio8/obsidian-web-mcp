# Agent Instructions — Obsidian Knowledge Base

> Questo file contiene le istruzioni operative per gli agenti AI che accedono a questo vault.
> Non modificare manualmente.

## Tool di Ricerca

Questo vault è dotato di un motore di ricerca integrato (`vault_search`) che supporta sia la ricerca semantica ibrida che la ricerca esatta per parole chiave.
Segui queste linee guida per scegliere i parametri corretti:

### `vault_search` (con `semantic=True`) ← **Usa questo per default**

Pipeline completa: query expansion → BM25 + vector search → RRF fusion.

```
vault_search(query="...", semantic=True, top_k=5)
```

**Quando usarlo:**
- Domande concettuali o open-ended ("come funziona X?", "cosa so di Y?")
- Ricerche dove non conosci il nome esatto del file
- Vuoi trovare note semanticamente correlate, non solo per parola chiave

**Parametri chiave:**
| Parametro | Default | Quando cambiarlo |
|:---|:---|:---|
| `top_k` | 5 | Aumenta a 10 se vuoi più contesto |
| `rerank` | False | Metti `True` se la query è complessa, multi-concetto o ambigua |
| `expand` | True | Metti `False` solo per query con nomi propri esatti |
| `path_prefix` | None | Usa per limitare la ricerca a una cartella specifica |

**Regola per `rerank`:**
- Query semplice con parole chiave precise → `rerank=False` (veloce)
- Query complessa, concetti astratti, domanda con più aspetti → `rerank=True` (più lenta ma più precisa)

---

### `vault_search` (con `semantic=False`) ← Solo per keyword esatte

Usa `semantic=False` solo quando cerchi una stringa/parola esatta (un errore specifico, un ID, una parola specifica non semantica).

```
vault_search(query="error 403", semantic=False, path_prefix="logs/")
```

### `vault_search_frontmatter` ← Per filtrare per metadati

Usa quando vuoi trovare note con un tag, stato o data specifici.

```
vault_search_frontmatter(field="status", value="in-progress")
```

---

## Struttura del Vault

- `progetti/` — Note e log di progetti attivi
- `docs/` — Documenti di design e architettura
- `inbox/` — Note non ancora classificate
- `risorse/` — Articoli, libri, riferimenti
- `_meta/` — File di sistema (non modificare)

---

## Best Practices per l'Agente

1. **Prima cerca, poi leggi**: usa `vault_search` per trovare i file rilevanti, poi `vault_read` per leggere quelli più pertinenti al risultato.
2. **Non listare tutto**: evita `vault_list` ricorsivo sull'intero vault — è costoso. Usa la ricerca.
3. **Usa i link Obsidian**: il campo `obsidian_link` nel risultato di `vault_search` ti dà il link wiki `[[percorso/nota]]` già formattato.
4. **Combina le fonti**: se `vault_search` trova più frammenti dello stesso file, leggi il file intero con `vault_read` per avere il contesto completo.
