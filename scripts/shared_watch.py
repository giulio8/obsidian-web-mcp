#!/usr/bin/env python3
"""Lightweight bi-directional watchdog synchronizer.

Keeps a specific sub-folder (e.g., Knowledge/Shared_Zone) in sync between
two separate mounts/directories on the same VM.
Uses a Last Write Wins (LWW) resolution strategy and ignores identical files
to prevent infinite feedback loops.
"""

import argparse
import logging
import os
import shutil
import time
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("shared_watch")


def get_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def sync_file_lww(src: Path, dst: Path) -> bool:
    """Sync file from src to dst using Last Write Wins.
    
    Returns True if a copy operation occurred, False otherwise.
    """
    if not src.exists():
        # If source is deleted, delete target
        if dst.exists():
            try:
                logger.info(f"Propagation: Deleting target {dst}")
                if dst.is_dir():
                    shutil.rmtree(dst)
                else:
                    dst.unlink()
                return True
            except Exception as e:
                logger.error(f"Failed to delete {dst}: {e}")
        return False

    if src.is_dir():
        if not dst.exists():
            try:
                logger.info(f"Creating directory {dst}")
                dst.mkdir(parents=True, exist_ok=True)
                return True
            except Exception as e:
                logger.error(f"Failed to create directory {dst}: {e}")
        return False

    # File copy logic
    try:
        if dst.exists():
            src_mtime = get_mtime(src)
            dst_mtime = get_mtime(dst)
            
            # Avoid loop: if mtimes are identical, skip copy
            if abs(src_mtime - dst_mtime) < 0.01:
                return False
                
            # LWW: Only copy if source is strictly newer
            if src_mtime <= dst_mtime:
                return False

        logger.info(f"Syncing: {src} -> {dst}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)  # copy2 preserves metadata including mtime
        return True
    except Exception as e:
        logger.error(f"Failed to sync {src} -> {dst}: {e}")
        return False


class BiDirectionalSyncHandler(FileSystemEventHandler):
    def __init__(self, src_dir: Path, dst_dir: Path):
        self.src_dir = src_dir
        self.dst_dir = dst_dir

    def _process_event(self, path_str: str):
        path = Path(path_str)
        try:
            rel = path.relative_to(self.src_dir)
        except ValueError:
            return  # Path not in watched directory
            
        target = self.dst_dir / rel
        sync_file_lww(path, target)

    def on_created(self, event: FileSystemEvent):
        if event.is_directory:
            return
        self._process_event(event.src_path)

    def on_modified(self, event: FileSystemEvent):
        if event.is_directory:
            return
        self._process_event(event.src_path)

    def on_deleted(self, event: FileSystemEvent):
        # Deleted files also pass the deleted path
        self._process_event(event.src_path)

    def on_moved(self, event: FileSystemEvent):
        # Delete old destination first
        try:
            rel_src = Path(event.src_path).relative_to(self.src_dir)
            old_dst = self.dst_dir / rel_src
            if old_dst.exists():
                if old_dst.is_dir():
                    shutil.rmtree(old_dst)
                else:
                    old_dst.unlink()
                logger.info(f"Deleted old moved target: {old_dst}")
        except Exception as e:
            logger.error(f"Failed to clean up old moved path: {e}")
            
        # Then sync new target
        self._process_event(event.dest_path)


def run_initial_sync(dir_a: Path, dir_b: Path):
    logger.info("Running initial bidirectional synchronization...")
    
    # 1. Walk A -> B
    for root, dirs, files in os.walk(dir_a):
        rel_root = Path(root).relative_to(dir_a)
        for d in dirs:
            (dir_b / rel_root / d).mkdir(parents=True, exist_ok=True)
        for f in files:
            src = Path(root) / f
            dst = dir_b / rel_root / f
            sync_file_lww(src, dst)
            
    # 2. Walk B -> A
    for root, dirs, files in os.walk(dir_b):
        rel_root = Path(root).relative_to(dir_b)
        for d in dirs:
            (dir_a / rel_root / d).mkdir(parents=True, exist_ok=True)
        for f in files:
            src = Path(root) / f
            dst = dir_a / rel_root / f
            sync_file_lww(src, dst)

    logger.info("Initial synchronization completed.")


def main():
    parser = argparse.ArgumentParser(description="Bidirectional LWW sync watchdog daemon.")
    parser.add_argument("--dir-a", required=True, help="First shared folder path")
    parser.add_argument("--dir-b", required=True, help="Second shared folder path")
    args = parser.parse_args()

    dir_a = Path(args.dir_a).resolve()
    dir_b = Path(args.dir_b).resolve()

    logger.info(f"Initializing watchdog sync: A={dir_a} <===> B={dir_b}")

    # Ensure parent directories exist
    dir_a.mkdir(parents=True, exist_ok=True)
    dir_b.mkdir(parents=True, exist_ok=True)

    # Initial LWW alignment
    run_initial_sync(dir_a, dir_b)

    observer = Observer()
    
    handler_a = BiDirectionalSyncHandler(dir_a, dir_b)
    handler_b = BiDirectionalSyncHandler(dir_b, dir_a)

    observer.schedule(handler_a, str(dir_a), recursive=True)
    observer.schedule(handler_b, str(dir_b), recursive=True)

    observer.start()
    logger.info("Watchdog bi-directional sync daemon is running. Press Ctrl+C to exit.")
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Stopping observer...")
        observer.stop()
    observer.join()
    logger.info("Daemon stopped successfully.")


if __name__ == "__main__":
    main()
