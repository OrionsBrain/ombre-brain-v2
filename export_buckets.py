#!/usr/bin/env python3
# ============================================================
# One-shot bucket export script / 一次性记忆桶导出脚本
#
# Run inside the deployed container before changing application code:
# 在改动应用代码前，于线上容器内运行：
#   python export_buckets.py
#
# Optional:
#   python export_buckets.py --buckets-dir /data/buckets --output /tmp/backup.json
# ============================================================

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

import yaml

from utils import load_config


BUCKET_SUBDIRS = ("permanent", "dynamic", "archive")


def _parse_markdown_with_frontmatter(raw: str) -> tuple[dict, str]:
    """Parse YAML frontmatter without requiring python-frontmatter."""
    raw = raw.lstrip("\ufeff")
    if not raw.startswith("---"):
        return {}, raw

    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, raw

    end_index = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_index = index
            break

    if end_index is None:
        return {}, raw

    frontmatter_text = "\n".join(lines[1:end_index])
    content = "\n".join(lines[end_index + 1 :])
    metadata = yaml.safe_load(frontmatter_text) or {}
    if not isinstance(metadata, dict):
        metadata = {}
    return metadata, content


def _load_bucket_file(file_path: Path, base_dir: Path) -> dict:
    """Read one markdown bucket with full frontmatter and body."""
    raw = file_path.read_text(encoding="utf-8")
    metadata, content = _parse_markdown_with_frontmatter(raw)
    return {
        "kind": "bucket",
        "id": metadata.get("id", file_path.stem),
        "relative_path": file_path.relative_to(base_dir).as_posix(),
        "frontmatter": metadata,
        "content": content,
    }


def export_buckets(buckets_dir: str) -> list[dict]:
    """Export current_status.md plus all bucket markdown files as a JSON array."""
    base_dir = Path(buckets_dir).resolve()
    exported_at = datetime.now().isoformat(timespec="seconds")
    records = [
        {
            "kind": "export_meta",
            "exported_at": exported_at,
            "buckets_dir": str(base_dir),
        }
    ]

    status_path = base_dir / "current_status.md"
    records.append(
        {
            "kind": "current_status",
            "relative_path": "current_status.md",
            "content": status_path.read_text(encoding="utf-8") if status_path.exists() else "",
            "exists": status_path.exists(),
        }
    )

    for subdir in BUCKET_SUBDIRS:
        root_dir = base_dir / subdir
        if not root_dir.exists():
            continue
        for file_path in sorted(root_dir.rglob("*.md")):
            records.append(_load_bucket_file(file_path, base_dir))

    return records


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export Ombre Brain buckets and current_status.md to JSON."
    )
    parser.add_argument(
        "--buckets-dir",
        default="",
        help="Bucket storage directory. Defaults to OMBRE_BUCKETS_DIR/config buckets_dir.",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Output JSON path. Defaults to ombre_backup_YYYYMMDD_HHMMSS.json.",
    )
    args = parser.parse_args()

    config = load_config()
    buckets_dir = args.buckets_dir or config["buckets_dir"]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = Path(args.output or f"ombre_backup_{timestamp}.json").resolve()

    records = export_buckets(buckets_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    bucket_count = sum(1 for item in records if item.get("kind") == "bucket")
    has_status = any(
        item.get("kind") == "current_status" and item.get("exists")
        for item in records
    )
    print(f"Exported {bucket_count} buckets to {output_path}")
    print(f"current_status.md: {'included' if has_status else 'not found'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
