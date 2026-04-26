"""Rclone push helper for post-write sync to Cloudflare R2.

Chiamato dopo ogni operazione di scrittura/modifica per triggerare
un upload immediato del file modificato verso il remote rclone configurato.
"""

import logging
import subprocess
from pathlib import Path

from .config import VAULT_PATH

logger = logging.getLogger(__name__)

# Nome del remote rclone e del bucket, devono corrispondere a rclone.conf
RCLONE_REMOTE = "obsidian-r2"
RCLONE_BUCKET = None  # letto dinamicamente da env per evitare import circolare


def _get_remote() -> str:
    """Restituisce il remote rclone nel formato 'remote:bucket'."""
    global RCLONE_BUCKET
    if RCLONE_BUCKET is None:
        import os
        RCLONE_BUCKET = os.environ.get("R2_BUCKET_NAME", "notes")
    return f"{RCLONE_REMOTE}:{RCLONE_BUCKET}"


def push_file(vault_relative_path: str) -> None:
    """Forza l'upload di un singolo file su R2 subito dopo la scrittura.

    Usa `rclone copyto` per copiare esattamente il file locale nella
    posizione corrispondente nel bucket remoto. Operazione idempotente
    e non bloccante (lancia in background).

    Args:
        vault_relative_path: percorso relativo alla radice del vault,
                              es. "agents/mia-nota.md"
    """
    local_path = VAULT_PATH / vault_relative_path
    remote_path = f"{_get_remote()}/{vault_relative_path}"

    try:
        subprocess.Popen(
            ["rclone", "copyto", str(local_path), remote_path, "--no-update-modtime"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info(f"rclone push avviato: {vault_relative_path} → {remote_path}")
    except FileNotFoundError:
        logger.warning("rclone non trovato in PATH, skip push immediato")
    except Exception as e:
        logger.warning(f"rclone push fallito per {vault_relative_path}: {e}")


def push_deleted(vault_relative_path: str) -> None:
    """Propaga la cancellazione (move to .trash) su R2.

    Nota: vault_delete sposta i file in .trash/, non li elimina davvero.
    Quindi qui facciamo push di entrambi i path coinvolti nel move.

    Args:
        vault_relative_path: percorso originale del file eliminato
    """
    # Il file è stato spostato in .trash/ dal server, facciamo push della
    # nuova posizione nel cestino
    trash_path = f".trash/{Path(vault_relative_path).name}"
    push_file(trash_path)
