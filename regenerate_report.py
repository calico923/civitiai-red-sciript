#!/usr/bin/env python3
"""
API なしで out/hits_lora.jsonl 等から out/report.html だけを書き直す。
HTML テンプレ変更や JSONL 更新後に run:  python3 regenerate_report.py
"""

from __future__ import annotations

import json
import os
import sys

from scrape_newest_ratio import _rows_for_html_report, build_html_report


def _script_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def load_jsonl(path: str) -> list[dict]:
    if not os.path.isfile(path):
        return []
    rows: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def main() -> int:
    out_dir = os.path.join(_script_dir(), "out")
    os.makedirs(out_dir, exist_ok=True)
    bundle = {
        "lora": load_jsonl(os.path.join(out_dir, "hits_lora.jsonl")),
        "checkpoint": load_jsonl(os.path.join(out_dir, "hits_checkpoint.jsonl")),
        "embedding": load_jsonl(os.path.join(out_dir, "hits_embedding.jsonl")),
    }
    min_p = 15.0
    for rows in bundle.values():
        if rows and rows[0].get("threshold_pct") is not None:
            min_p = float(rows[0]["threshold_pct"])
            break
    sections_html = {
        "lora": _rows_for_html_report(bundle["lora"], show_base_model=False),
        "checkpoint": _rows_for_html_report(bundle["checkpoint"], show_base_model=True),
        "embedding": _rows_for_html_report(bundle["embedding"], show_base_model=False),
    }
    titles = {
        "lora": "LoRA / LyCORIS（Style・Concept・Pose）",
        "checkpoint": "Checkpoint（Illustrious / NoobAI）",
        "embedding": "Embedding",
    }
    html = build_html_report(min_thumb_pct=min_p, sections=sections_html, titles=titles)
    out_path = os.path.join(out_dir, "report.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Wrote {out_path}", file=sys.stderr)
    print(out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
