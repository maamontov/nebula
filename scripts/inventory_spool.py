#!/usr/bin/env python3
"""
Spool Inventory and Diagnostic Tool for Nebula.
Безопасная инвентаризация и диагностика аудиоспула Nebula.

Adheres strictly to Stage A requirements in docs/desktop-pipeline-remediation-plan.md:
- Scans spool directory for interviews, tracks, epochs, sequence numbers, chunk sizes, and checksums.
- Verifies presence and completeness of sealed track manifests.
- Correlates with SQLite database records if available.
- NEVER reads or outputs speech audio or transcript text in diagnostics.
- Distinguishes SEALED, UNSEALED, and ORPHAN sessions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def get_default_spool_dirs() -> list[Path]:
    """Returns candidate spool directories to inspect."""
    candidates = [
        # Desktop native historical spool
        DEFAULT_PROJECT_ROOT / "apps" / "desktop" / "src-tauri" / "spool",
        # New canonical capture spool
        Path(os.getenv("NEBULA_CAPTURE_SPOOL_DIR", str(DEFAULT_PROJECT_ROOT / "data" / "spool_capture"))),
        # New canonical backend spool
        Path(os.getenv("NEBULA_BACKEND_SPOOL_DIR", str(DEFAULT_PROJECT_ROOT / "data" / "spool_backend"))),
        # Legacy root spool
        DEFAULT_PROJECT_ROOT / "spool",
    ]
    seen = set()
    unique = []
    for c in candidates:
        r = c.resolve()
        if r not in seen and r.exists():
            seen.add(r)
            unique.append(r)
    return unique


def verify_chunk_checksum(chunk_file: Path, expected_sha256: str) -> bool:
    """Verifies that the chunk payload matches its declared checksum without outputting audio data."""
    try:
        h = hashlib.sha256()
        with open(chunk_file, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest().lower() == expected_sha256.lower()
    except Exception:
        return False


def inspect_track(track_dir: Path) -> dict[str, Any]:
    """Inspects a single track directory (e.g., candidate or interviewer)."""
    track_name = track_dir.name
    chunk_files = sorted(track_dir.glob("*.chunk"))
    meta_files = sorted(track_dir.glob("*.meta.json"))
    manifest_file = track_dir / "manifest.json"

    meta_by_seq = {}
    for m in meta_files:
        try:
            with open(m, "r", encoding="utf-8") as f:
                data = json.load(f)
                seq = data.get("sequence")
                if seq is not None:
                    meta_by_seq[seq] = data
        except Exception:
            continue

    total_chunks = len(chunk_files)
    total_bytes = sum(c.stat().st_size for c in chunk_files)
    verified_checksums = 0
    epochs = set()

    for c in chunk_files:
        stem = c.stem
        try:
            seq = int(stem)
        except ValueError:
            seq = -1
        meta = meta_by_seq.get(seq)
        if meta and "checksum_sha256" in meta:
            epochs.add(meta.get("capture_epoch", 0))
            if verify_chunk_checksum(c, meta["checksum_sha256"]):
                verified_checksums += 1

    manifest_data = None
    is_sealed = False
    if manifest_file.exists():
        try:
            with open(manifest_file, "r", encoding="utf-8") as f:
                manifest_data = json.load(f)
                is_sealed = manifest_data.get("is_sealed", False)
        except Exception:
            is_sealed = False

    return {
        "track_name": track_name,
        "total_chunks": total_chunks,
        "total_bytes": total_bytes,
        "verified_checksums": verified_checksums,
        "epochs": sorted(list(epochs)),
        "is_sealed": is_sealed,
        "has_manifest": manifest_file.exists(),
        "manifest_data": manifest_data,
    }


def inspect_interview(interview_dir: Path, db_conn: sqlite3.Connection | None) -> dict[str, Any]:
    """Inspects an interview spool folder and correlates with database."""
    interview_id = interview_dir.name
    tracks = {}
    for sub in sorted(interview_dir.iterdir()):
        if sub.is_dir() and sub.name in ("candidate", "interviewer"):
            tracks[sub.name] = inspect_track(sub)

    # DB correlation
    db_interview = None
    db_chunks_count = 0
    db_jobs_count = 0
    if db_conn:
        try:
            cur = db_conn.execute("SELECT id, status, candidate_name, role, created_at FROM interviews WHERE id = ?", (interview_id,))
            row = cur.fetchone()
            if row:
                db_interview = dict(row)
                c_cur = db_conn.execute("SELECT COUNT(*) FROM audio_chunks WHERE interview_id = ?", (interview_id,))
                db_chunks_count = c_cur.fetchone()[0]
                j_cur = db_conn.execute("SELECT COUNT(*) FROM jobs WHERE interview_id = ?", (interview_id,))
                db_jobs_count = j_cur.fetchone()[0]
        except Exception:
            pass

    # Status classification
    all_sealed = bool(tracks) and all(t["is_sealed"] for t in tracks.values())
    if not db_interview:
        classification = "ORPHAN_SPOOL"  # In spool, not in DB
    elif all_sealed:
        classification = "SEALED_SESSION"
    else:
        classification = "UNSEALED_SESSION"

    return {
        "interview_id": interview_id,
        "spool_path": str(interview_dir),
        "classification": classification,
        "tracks": tracks,
        "in_database": db_interview is not None,
        "db_interview": db_interview,
        "db_audio_chunks": db_chunks_count,
        "db_jobs": db_jobs_count,
    }


def run_inventory(spool_dirs: list[Path], db_path: Path | None) -> dict[str, Any]:
    """Runs inventory across all specified spool paths."""
    db_conn = None
    if db_path and db_path.exists():
        try:
            db_conn = sqlite3.connect(str(db_path))
            db_conn.row_factory = sqlite3.Row
        except Exception as e:
            print(f"[WARN] Cannot connect to DB at {db_path}: {e}", file=sys.stderr)

    inventory = {
        "scanned_spool_directories": [str(p) for p in spool_dirs],
        "database_path": str(db_path) if db_path else None,
        "interviews": [],
        "summary": {
            "total_interviews_found": 0,
            "total_chunks_found": 0,
            "total_bytes_found": 0,
            "sealed_sessions": 0,
            "unsealed_sessions": 0,
            "orphan_spool_sessions": 0,
        },
    }

    total_chunks = 0
    total_bytes = 0

    for s_dir in spool_dirs:
        if not s_dir.exists():
            continue
        for item in sorted(s_dir.iterdir()):
            if item.is_dir() and (item.name.startswith("inv-") or (item / "candidate").exists() or (item / "interviewer").exists()):
                inv_data = inspect_interview(item, db_conn)
                inventory["interviews"].append(inv_data)
                cls = inv_data["classification"]
                if cls == "SEALED_SESSION":
                    inventory["summary"]["sealed_sessions"] += 1
                elif cls == "UNSEALED_SESSION":
                    inventory["summary"]["unsealed_sessions"] += 1
                elif cls == "ORPHAN_SPOOL":
                    inventory["summary"]["orphan_spool_sessions"] += 1

                for tr in inv_data["tracks"].values():
                    total_chunks += tr["total_chunks"]
                    total_bytes += tr["total_bytes"]

    if db_conn:
        db_conn.close()

    inventory["summary"]["total_interviews_found"] = len(inventory["interviews"])
    inventory["summary"]["total_chunks_found"] = total_chunks
    inventory["summary"]["total_bytes_found"] = total_bytes
    return inventory


def main():
    parser = argparse.ArgumentParser(description="Nebula Audio Spool Inventory Tool")
    parser.add_argument("--spool", action="append", help="Spool directory to scan (repeatable)")
    parser.add_argument("--db", help="Path to nebula.db SQLite database")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    spool_paths = [Path(p).resolve() for p in args.spool] if args.spool else get_default_spool_dirs()
    db_path = Path(args.db).resolve() if args.db else Path(os.getenv("NEBULA_DB_PATH", str(DEFAULT_PROJECT_ROOT / "data" / "nebula.db")))

    results = run_inventory(spool_paths, db_path)

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        print("=" * 70)
        print("              NEBULA AUDIO SPOOL INVENTORY REPORT")
        print("=" * 70)
        print(f"Scanned Directories : {', '.join(results['scanned_spool_directories'])}")
        print(f"Database Path       : {results['database_path']}")
        print("-" * 70)
        print("SUMMARY:")
        s = results["summary"]
        print(f"  Total Interviews  : {s['total_interviews_found']}")
        print(f"  Total Audio Chunks: {s['total_chunks_found']}")
        print(f"  Total Audio Bytes : {s['total_bytes_found'] / (1024 * 1024):.2f} MB")
        print(f"  Sealed Sessions   : {s['sealed_sessions']}")
        print(f"  Unsealed Sessions : {s['unsealed_sessions']}")
        print(f"  Orphan Sessions   : {s['orphan_spool_sessions']}")
        print("-" * 70)
        print("INTERVIEWS DETAIL:")
        for inv in results["interviews"]:
            inv_id = inv["interview_id"]
            cls = inv["classification"]
            in_db = "YES" if inv["in_database"] else "NO"
            print(f"\n  ● Interview ID: {inv_id} [{cls}] (In DB: {in_db})")
            print(f"    Path: {inv['spool_path']}")
            if inv["in_database"]:
                print(f"    DB Status: {inv['db_interview'].get('status')} | Candidate: {inv['db_interview'].get('candidate_name')} | Chunks in DB: {inv['db_audio_chunks']}")
            for tname, tr in inv["tracks"].items():
                sealed_str = "SEALED" if tr["is_sealed"] else "NOT SEALED"
                print(f"    - Track [{tname}]: {tr['total_chunks']} chunks ({tr['total_bytes'] / 1024:.1f} KB), Checksums Verified: {tr['verified_checksums']}/{tr['total_chunks']}, Status: {sealed_str}")
        print("\n" + "=" * 70)


if __name__ == "__main__":
    main()
