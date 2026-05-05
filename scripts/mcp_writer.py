import json
import urllib.request
import os

URL = "https://obsidian-mcp.duckdns.org/mcp"
TOKEN = "e11d12c5f57c64d91a45f02ebb501f164551a7f64df2c3b500422a30816da96e"

def call_mcp(method, params):
    req = urllib.request.Request(URL, method="POST")
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    
    data = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params
    }
    
    try:
        with urllib.request.urlopen(req, data=json.dumps(data).encode("utf-8")) as response:
            return json.loads(response.read().decode())
    except Exception as e:
        print(f"Error: {e}")
        return None

files = [
    {
        "path": "system/obsidian-mcp/Motore-di-Ricerca-Ibrido.md",
        "content": """---
tags:
  - mcp
  - rag
  - search
aliases:
  - Motore di Ricerca Ibrido
  - query_vault
---
# Motore di Ricerca Ibrido (Hybrid Search)

Il cuore "intelligente" dell'integrazione è rappresentato dal motore di ricerca ibrido implementato all'interno del modulo `qmd/` (Quantitative Markdown Database).

## Architettura di Ricerca
Il tool principale, `query_vault`, combina diverse tecniche di information retrieval:
1. **Keyword Search (BM25)**: Utilizza SQLite FTS5 per una ricerca testuale esatta, con sanitizzazione dei caratteri speciali.
2. **Semantic Search (Vector)**: Utilizza embeddings generati tramite **Vertex AI** (`embed_query`) salvati e ricercati tramite l'estensione `sqlite-vec`.
3. **Query Expansion (Gemini)**: Se abilitato (`expand=True`), il server invoca Gemini per generare formulazioni alternative della query dell'utente, migliorando drasticamente la *recall*.
4. **Reciprocal Rank Fusion (RRF)**: I risultati BM25 e Vector (compresi quelli delle query espanse) vengono fusi assieme usando l'algoritmo RRF ($k=60$), assegnando un peso doppio alla query originale.
5. **Reranking e Blending**: I migliori 30 candidati subiscono un processo di blending sensibile alla posizione (position-aware blending), e, se richiesto (`rerank=True`), vengono ri-ordinati in base alla pertinenza calcolata da Gemini Flash.

## Vantaggi per gli Agenti AI
Questo approccio permette a un agente (come me) di effettuare query vaghe, concettuali o multi-termine e ottenere frammenti altamente rilevanti (con il relativo contesto) senza dover scansionare o ipotizzare percorsi specifici dei file.
"""
    },
    {
        "path": "system/obsidian-mcp/Gestione-del-Vault.md",
        "content": """---
tags:
  - mcp
  - tools
aliases:
  - Tools di Gestione
  - Vault Tools
---
# Gestione del Vault e Tools Esportati

Il server MCP esporta vari tools per manipolare in sicurezza e in modo chirurgico la knowledge base.

## Tools di Lettura e Scrittura
- **`vault_read` / `vault_batch_read`**: Legge uno o più file. Notevole il fatto che restituisce separatamente il body e il frontmatter già decodificato.
- **`vault_write` / `vault_batch_write`**: Sovrascrive o crea file (supportando `merge_frontmatter` per aggiornare senza perdere dati preesistenti).
- **`vault_patch`**: Esegue sostituzioni mirate (*surgical str_replace*), essenziali per modificare una piccola parte di un file lungo senza ritrasmettere tutto il contenuto e rischiare troncamenti (risparmio token e sicurezza).
- **`vault_append`**: Utile per aggiungere entry a log o task in fondo al file o sotto una specifica intestazione markdown (`## Section`).

## Gestione Avanzata Frontmatter e File
- **`vault_batch_frontmatter_update`**: Modifica massiva di proprietà YAML su più file, ideale per taggare file analizzati in batch.
- **`vault_move`**: Oltre a spostare il file, esegue il re-wiring automatico dei `[[wikilinks]]` basandosi sul [[Index-e-Link-Graph]], permettendo ristrutturazioni senza rompere la coerenza del vault.
- **`vault_delete`**: Cancella i file spostandoli in una cartella `.trash/` e richiede il parametro `confirm=true` per prevenire delezioni accidentali da parte degli LLM.
"""
    }
]

print("Calling vault_batch_write...")
res = call_mcp("tools/call", {
    "name": "vault_batch_write",
    "arguments": {
        "files": files
    }
})

print(json.dumps(res, indent=2))
