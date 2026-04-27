#!/usr/bin/env python3
"""
Fetch Civiti.red /api/v1/models with Newest sort, cursor pagination (via metadata.nextPage),
filter by thumbsUpCount/downloadCount ratio (model-level cumulative stats from the API),
write JSONL and optionally an HTML report.

Requires an API key (Bearer) for login-gated and NSFW-inclusive listing.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, TextIO

# サムネ候補から除外する動画拡張子（URL path ベース）
_THUMB_SKIP_VIDEO_EXTS = frozenset({".mp4", ".webm", ".mov", ".m4v", ".avi", ".mkv"})

DEFAULT_BASE = "https://civitai.red"
DEFAULT_TYPES = ("Checkpoint", "LORA", "TextualInversion")
DEFAULT_HEARTBEAT_SEC = 30.0
DEFAULT_CHECKPOINT_BASE_MODELS = ("Illustrious", "NoobAI")
# LoRA/LyCORIS 枝: Civitai 互換 API は LyCORIS を type=LoCon で返す
DEFAULT_LORA_API_TYPES = ("LORA", "LoCon")
# サイト上の Style / Concept / Pose に相当する一覧フィルタ（query: tag=）
DEFAULT_LORA_TAG_CATEGORIES = ("style", "concept", "pose")
# tag= だけでは「キャラ用 LoRA」も混ざる（例: pose + character タグ）。一覧の tags との完全一致で除外する
DEFAULT_LORA_EXCLUDE_TAGS = ("character", "characters", "anime character")

# Rating% = いいね率（thumbsUp/download*100）と同義。足切り通過者を % で帯分け S/A/B/C（境界はこの定数を編集）
RATING_GRADE_S_MIN = 35.0
RATING_GRADE_A_MIN = 28.0
RATING_GRADE_B_MIN = 22.0
# 足切り以上かつ上記未満 → C

_parent_parser = argparse.ArgumentParser(add_help=False)
_parent_parser.add_argument(
    "--env-file",
    default=".env",
    help="Load KEY=VALUE into environment before other options (default: ./.env). Missing file is ignored.",
)


def load_env_file(path: str, *, override: bool = False) -> None:
    """Minimal .env parser (no extra deps). Does not expand variable references."""
    if not path or not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if not key:
                continue
            val = value.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            if override or key not in os.environ:
                os.environ[key] = val


def build_first_url(
    base: str,
    limit: int,
    types: tuple[str, ...],
    nsfw: bool | None,
    base_models: tuple[str, ...] | None = None,
    tag: str | None = None,
) -> str:
    q: list[tuple[str, str]] = [
        ("sort", "Newest"),
        ("limit", str(limit)),
    ]
    for t in types:
        q.append(("types", t))
    for bm in base_models or ():
        q.append(("baseModels", bm))
    if tag is not None and str(tag).strip():
        q.append(("tag", str(tag).strip()))
    if nsfw is not None:
        q.append(("nsfw", "true" if nsfw else "false"))
    return f"{base.rstrip('/')}/api/v1/models?{urllib.parse.urlencode(q)}"


def fetch_json(url: str, timeout: float, api_key: str) -> dict[str, Any]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "civitiai-red-sciript/1.0",
        "Authorization": f"Bearer {api_key}",
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        raw = resp.read().decode(charset)
    return json.loads(raw)


def earliest_version_created_at(detail: dict[str, Any]) -> str | None:
    """初回目安: modelVersions 各所の createdAt のうち最も古い ISO 日時。一覧 API には含まれない。"""
    best: str | None = None
    for v in detail.get("modelVersions") or []:
        if not isinstance(v, dict):
            continue
        c = v.get("createdAt")
        if not isinstance(c, str) or not c.strip():
            continue
        if best is None or c < best:
            best = c
    return best


def max_version_published_at(item: dict[str, Any]) -> str | None:
    """全 modelVersions の publishedAt のうち最新。アップデ版が増えても「最後に更新した」日に追従する。"""
    best: str | None = None
    for v in item.get("modelVersions") or []:
        if not isinstance(v, dict):
            continue
        c = v.get("publishedAt")
        if not isinstance(c, str) or not c.strip():
            continue
        if best is None or c > best:
            best = c
    return best


def parse_exclude_tag_set(arg: str) -> frozenset[str]:
    """Comma-separated exact tag matches (case-insensitive). Empty arg → no exclusions."""
    if not (arg or "").strip():
        return frozenset()
    return frozenset(x.strip().lower() for x in arg.split(",") if x.strip())


def item_has_excluded_tag(item: dict[str, Any], exclude: frozenset[str]) -> bool:
    """True if any model tag string equals one of exclude (after strip + lower)."""
    if not exclude:
        return False
    tags = item.get("tags")
    if not isinstance(tags, list):
        return False
    for t in tags:
        if not isinstance(t, str):
            continue
        if t.strip().lower() in exclude:
            return True
    return False


def thumb_ratio_pct(item: dict[str, Any]) -> float | None:
    stats = item.get("stats") or {}
    try:
        d = int(stats.get("downloadCount") or 0)
        u = int(stats.get("thumbsUpCount") or 0)
    except (TypeError, ValueError):
        return None
    if d <= 0:
        return None
    return 100.0 * u / d


def passes_ratio(item: dict[str, Any], min_pct: float) -> bool:
    r = thumb_ratio_pct(item)
    return r is not None and r >= min_pct


def rating_grade_from_ratio_pct(r: float | None) -> str:
    """S/A/B/C from いいね率%（足切り後の帯; None は C）."""
    if r is None:
        return "C"
    if r >= RATING_GRADE_S_MIN:
        return "S"
    if r >= RATING_GRADE_A_MIN:
        return "A"
    if r >= RATING_GRADE_B_MIN:
        return "B"
    return "C"


def _primary_version(item: dict[str, Any]) -> dict[str, Any]:
    versions = item.get("modelVersions") or []
    if versions and isinstance(versions[0], dict):
        return versions[0]
    return {}


def _url_path_endswith_video(url: str) -> bool:
    try:
        path = urllib.parse.urlparse(url.strip()).path.lower()
    except Exception:
        return False
    return any(path.endswith(ext) for ext in _THUMB_SKIP_VIDEO_EXTS)


def _image_entry_is_video_thumb(img: dict[str, Any]) -> bool:
    """API の type=video または URL が動画拡張子のときスキップして次の静止画を使う。"""
    t = img.get("type")
    if isinstance(t, str) and t.strip().lower() == "video":
        return True
    u = img.get("url")
    if isinstance(u, str) and u.strip() and _url_path_endswith_video(u):
        return True
    return False


def _first_image_url(item: dict[str, Any]) -> str | None:
    """先頭バージョンの images から、最初の非動画（静止画）の url。mp4/webm 先頭は次を使う。"""
    v0 = _primary_version(item)
    images = v0.get("images") or []
    for img in images:
        if not isinstance(img, dict):
            continue
        if _image_entry_is_video_thumb(img):
            continue
        u = img.get("url")
        if isinstance(u, str) and u.strip():
            return u.strip()
    return None


def row_for_item(
    base: str,
    item: dict[str, Any],
    min_thumb_pct: float,
    category: str,
    *,
    tag_category: str | None = None,
) -> dict[str, Any]:
    mid = item.get("id")
    v0 = _primary_version(item)
    published = max_version_published_at(item)
    r = thumb_ratio_pct(item)
    stats = item.get("stats") or {}
    return {
        "id": mid,
        "name": item.get("name"),
        "type": item.get("type"),
        "category": category,
        "tagCategory": (tag_category.strip().lower() if isinstance(tag_category, str) and tag_category.strip() else None),
        "baseModel": v0.get("baseModel"),
        "url": f"{base.rstrip('/')}/models/{mid}" if mid is not None else None,
        "imageUrl": _first_image_url(item),
        "thumbsUpCount": stats.get("thumbsUpCount"),
        "downloadCount": stats.get("downloadCount"),
        "stats": stats,
        "thumb_ratio_pct": round(r, 6) if r is not None else None,
        "rating_pct": round(r, 2) if r is not None else None,
        "rating_grade": rating_grade_from_ratio_pct(r),
        "threshold_pct": min_thumb_pct,
        # 全バージョンの publishedAt の最大（詳細取得後に一覧より正確に上書き可）
        "latestVersionPublishedAt": published,
        # 詳細 API の各バージョン createdAt の最古（初回掲載の目安）
        "modelCreatedAt": None,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    pre, _ = _parent_parser.parse_known_args(argv)
    load_env_file(pre.env_file)
    p = argparse.ArgumentParser(
        description="Civiti.red Newest scrape with thumbs/download ratio filter",
        parents=[_parent_parser],
    )
    p.add_argument(
        "--preset",
        choices=("custom", "lora", "checkpoint", "embedding", "all"),
        default="custom",
        help=(
            "custom: use --types; lora: LORA+LoCon (LyCORIS), --lora-tags ごとに API tag= フィルタ; "
            "checkpoint: Checkpoint + Illustrious/NoobAI; embedding: TextualInversion; all: lora+ckpt+emb + HTML"
        ),
    )
    p.add_argument(
        "--base-models",
        default=",".join(DEFAULT_CHECKPOINT_BASE_MODELS),
        help=(
            "For preset checkpoint or all (checkpoint leg). "
            f"Comma-separated baseModels (default: {','.join(DEFAULT_CHECKPOINT_BASE_MODELS)})"
        ),
    )
    p.add_argument(
        "--out-dir",
        default="scrape_output",
        help="For --preset all: directory for jsonl + report.html (default: scrape_output)",
    )
    p.add_argument(
        "--html",
        default="",
        help="Write graphical HTML report to this path (use with single presets). Empty skips.",
    )
    p.add_argument(
        "--api-key",
        default=os.environ.get("CIVITAI_API_KEY", ""),
        help="Bearer token (required). From env / .env as CIVITAI_API_KEY if unset here",
    )
    p.add_argument("--base-url", default=DEFAULT_BASE, help=f"API host (default: {DEFAULT_BASE})")
    p.add_argument("--limit", type=int, default=100, help="Models per page (1–100 typical)")
    p.add_argument(
        "--types",
        default=",".join(DEFAULT_TYPES),
        help=f"Comma-separated types when --preset custom (default: {','.join(DEFAULT_TYPES)})",
    )
    p.add_argument(
        "--lora-tags",
        default=",".join(DEFAULT_LORA_TAG_CATEGORIES),
        help=(
            "Comma-separated listing tag filters for --preset lora and all (lora leg). "
            f"Each runs a separate API pass with tag=... (default: {','.join(DEFAULT_LORA_TAG_CATEGORIES)})"
        ),
    )
    p.add_argument(
        "--lora-exclude-tags",
        default=",".join(DEFAULT_LORA_EXCLUDE_TAGS),
        help=(
            "For lora preset / all (lora leg): skip hits when any model tag equals one of these "
            '(comma-separated, case-insensitive; empty "" disables). '
            f"Default: {','.join(DEFAULT_LORA_EXCLUDE_TAGS)} — reduces Character-tagged LoRAs mixed into tag= passes."
        ),
    )
    p.add_argument("--min-thumb-pct", type=float, default=15.0, help="Min (thumbsUp/download)*100 (default: 15)")
    p.add_argument("--max-pages", type=int, default=10, help="Max pages per pass (default: 10)")
    p.add_argument("--max-items", type=int, default=0, help="Stop after scanning this many items per pass (0 = no limit)")
    p.add_argument("--out", default="-", help="Output JSONL path, or - for stdout (not used with --preset all)")
    p.add_argument("--sleep", type=float, default=0.0, help="Seconds to sleep between requests")
    p.add_argument("--timeout", type=float, default=60.0, help="HTTP timeout seconds")
    p.add_argument(
        "--skip-model-detail",
        action="store_true",
        help=(
            "Skip GET /api/v1/models/{id} per hit (faster; modelCreatedAt と latestVersionPublishedAt の詳細上書きなし)"
        ),
    )
    p.add_argument(
        "--model-detail-sleep",
        type=float,
        default=0.0,
        help="Seconds to sleep after each per-model detail request (default 0; rate limit 対策用)",
    )
    p.add_argument(
        "--nsfw",
        choices=("true", "false", "omit"),
        default="true",
        help="nsfw query: true (default, include NSFW with auth), false, or omit",
    )
    p.add_argument(
        "--heartbeat-sec",
        type=float,
        default=DEFAULT_HEARTBEAT_SEC,
        help=(
            f"Print progress to stderr every N seconds while running (default: {DEFAULT_HEARTBEAT_SEC}). "
            "0 disables."
        ),
    )
    return p.parse_args(argv)


def _heartbeat_worker(stop: threading.Event, interval: float, state: dict[str, Any]) -> None:
    while True:
        if stop.wait(timeout=interval):
            return
        cat = state.get("category") or "?"
        print(
            "[heartbeat] "
            f"category={cat} "
            f"phase={state.get('phase')} "
            f"pages_done={state.get('pages_done')} "
            f"scanned={state.get('scanned')} "
            f"hits={state.get('hits')}",
            file=sys.stderr,
            flush=True,
        )


def _run_one_pass(
    *,
    base: str,
    api_key: str,
    types: tuple[str, ...],
    base_models: tuple[str, ...] | None,
    category: str,
    list_tag: str | None = None,
    limit: int,
    nsfw_val: bool | None,
    min_thumb_pct: float,
    max_pages: int,
    max_items: int,
    sleep: float,
    timeout: float,
    out_f: TextIO | None,
    hit_rows: list[dict[str, Any]] | None,
    hb_state: dict[str, Any],
    skip_model_detail: bool = False,
    model_detail_sleep: float = 0.0,
    tag_category: str | None = None,
    exclude_exact_tags: frozenset[str] | None = None,
) -> dict[str, Any]:
    url = build_first_url(base, limit, types, nsfw_val, base_models, list_tag)
    seen_ids: set[Any] = set()
    dup_warnings = 0
    pages = 0
    scanned = 0
    hits = 0
    empty_streak = 0
    hb_state["category"] = f"{category}[{list_tag}]" if list_tag else category
    hb_state["phase"] = "starting"
    hb_state["pages_done"] = 0
    hb_state["scanned"] = 0
    hb_state["hits"] = 0

    while pages < max_pages:
        if sleep > 0 and pages > 0:
            time.sleep(sleep)
        hb_state["phase"] = "http_fetch"
        try:
            payload = fetch_json(url, timeout, api_key)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code} {e.reason}: {url}") from e
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
            raise RuntimeError(f"Request failed: {e}: {url}") from e

        hb_state["phase"] = "processing_page"
        items = payload.get("items") or []
        if not items:
            empty_streak += 1
            print(f"Warning: empty items on page {pages + 1} ({url})", file=sys.stderr)
            if empty_streak >= 2:
                print("Stopping: consecutive empty pages.", file=sys.stderr)
                break
        else:
            empty_streak = 0

        meta = payload.get("metadata") or {}
        next_page = meta.get("nextPage")

        for item in items:
            if not isinstance(item, dict):
                continue
            scanned += 1
            mid = item.get("id")
            if mid in seen_ids:
                dup_warnings += 1
                print(f"Warning: duplicate model id in stream: {mid}", file=sys.stderr)
            seen_ids.add(mid)

            if passes_ratio(item, min_thumb_pct):
                if not (exclude_exact_tags and item_has_excluded_tag(item, exclude_exact_tags)):
                    row = row_for_item(
                        base,
                        item,
                        min_thumb_pct,
                        category,
                        tag_category=tag_category if tag_category is not None else list_tag,
                    )
                    if not skip_model_detail and mid is not None:
                        durl = f"{base.rstrip('/')}/api/v1/models/{mid}"
                        try:
                            detail = fetch_json(durl, timeout, api_key)
                            ts = earliest_version_created_at(detail)
                            if ts is not None:
                                row["modelCreatedAt"] = ts
                            mp = max_version_published_at(detail)
                            if mp is not None:
                                row["latestVersionPublishedAt"] = mp
                        except Exception as e:
                            print(
                                f"Warning: model detail {mid} failed, modelCreatedAt / latestVersionPublishedAt unset: {e}",
                                file=sys.stderr,
                            )
                        if model_detail_sleep > 0:
                            time.sleep(model_detail_sleep)
                    if out_f is not None:
                        out_f.write(json.dumps(row, ensure_ascii=False) + "\n")
                    if hit_rows is not None:
                        hit_rows.append(row)
                    hits += 1
            hb_state["scanned"] = scanned
            hb_state["hits"] = hits

            if max_items and scanned >= max_items:
                next_page = None
                break

        pages += 1
        hb_state["pages_done"] = pages
        hb_state["scanned"] = scanned
        hb_state["hits"] = hits

        if max_items and scanned >= max_items:
            break
        if not next_page or not isinstance(next_page, str):
            break
        url = next_page

    hb_state["phase"] = "done"
    return {
        "pages_fetched": pages,
        "items_scanned": scanned,
        "hits_written": hits,
        "duplicate_id_warnings": dup_warnings,
        "category": category,
        "listTag": list_tag,
    }


def _rows_for_html_report(rows: list[dict[str, Any]], *, show_base_model: bool) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, r in enumerate(rows):
        ratio = r.get("thumb_ratio_pct")
        rp = r.get("rating_pct")
        if rp is not None:
            n = float(rp)
        elif ratio is not None:
            n = round(float(ratio), 2)
        else:
            n = None
        rg = (r.get("rating_grade") or "").strip() or None
        if not rg and ratio is not None:
            rg = rating_grade_from_ratio_pct(float(ratio))
        elif not rg:
            rg = rating_grade_from_ratio_pct(n) if n is not None else "C"
        out.append(
            {
                "order": i,
                "name": r.get("name") or "",
                "url": r.get("url") or "",
                "imageUrl": r.get("imageUrl") or None,
                "thumbsUp": r.get("thumbsUpCount"),
                "downloads": r.get("downloadCount"),
                "ratioPct": n,
                "ratingPct": n,
                "ratingGrade": rg,
                "thresholdPct": r.get("threshold_pct"),
                "baseModel": r.get("baseModel") if show_base_model else None,
                "id": r.get("id"),
                "publishedAt": r.get("latestVersionPublishedAt") or None,
                "modelCreatedAt": r.get("modelCreatedAt") or None,
                "tagCategory": r.get("tagCategory"),
            }
        )
    return out


def build_html_report(
    *,
    min_thumb_pct: float,
    sections: dict[str, list[dict[str, Any]]],
    titles: dict[str, str],
) -> str:
    """sections keys: lora, checkpoint, embedding — list of display rows."""
    payload = {
        "threshold": min_thumb_pct,
        "ratingBands": {
            "S": RATING_GRADE_S_MIN,
            "A": RATING_GRADE_A_MIN,
            "B": RATING_GRADE_B_MIN,
        },
        "sections": sections,
        "titles": titles,
    }
    json_blob = json.dumps(payload, ensure_ascii=False)
    json_blob = json_blob.replace("</", "<\\/")
    gen_ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    return f"""<!DOCTYPE html>
<!-- civitai report build {gen_ts} -->
<html lang="ja">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Civiti.red 採取レポート</title>
  <style>
    :root {{
      --bg: #0f1219;
      --panel: #171c28;
      --text: #e8eaed;
      --muted: #9aa0b4;
      --accent: #7c9cff;
      --bar-bg: #2a3142;
      --ok: #4fd1a5;
      --border: #2d3548;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      margin: 0;
      line-height: 1.5;
      padding: 1.25rem 1.5rem 3rem;
    }}
    h1 {{ font-size: 1.35rem; font-weight: 600; margin: 0 0 0.75rem; }}
    .sub {{ color: var(--muted); font-size: 0.9rem; margin-bottom: 0.75rem; }}
    .toolbar {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.65rem 1rem;
      margin: 0.75rem 0 1rem;
      padding: 0.65rem 0.75rem;
      background: #141824;
      border: 1px solid var(--border);
      border-radius: 8px;
    }}
    .toolbar label {{
      display: flex;
      align-items: center;
      gap: 0.4rem;
      font-size: 0.88rem;
      color: var(--muted);
    }}
    .toolbar label.ck input {{ margin: 0; cursor: pointer; }}
    .toolbar label.ck {{ cursor: pointer; user-select: none; }}
    .toolbar select {{
      background: var(--panel);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 0.35rem 0.5rem;
      font-size: 0.88rem;
    }}
    .tabs {{
      display: flex;
      gap: 0.35rem;
      flex-wrap: wrap;
      margin-bottom: 0.5rem;
      border-bottom: 1px solid var(--border);
      padding-bottom: 0.5rem;
    }}
    .tabs button {{
      background: var(--panel);
      color: var(--text);
      border: 1px solid var(--border);
      padding: 0.45rem 0.9rem;
      border-radius: 8px 8px 0 0;
      cursor: pointer;
      font-size: 0.9rem;
    }}
    .tabs button.active {{
      border-bottom-color: var(--bg);
      background: var(--bg);
      color: var(--accent);
    }}
    .panel {{
      display: none;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 0 10px 10px 10px;
      padding: 1rem;
      overflow-x: auto;
    }}
    .panel.active {{ display: block; }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.88rem;
    }}
    th, td {{
      text-align: left;
      padding: 0.55rem 0.65rem;
      border-bottom: 1px solid var(--border);
      vertical-align: middle;
    }}
    th {{
      color: var(--muted);
      font-weight: 600;
      white-space: nowrap;
    }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    .bar-wrap {{
      width: 120px;
      height: 10px;
      background: var(--bar-bg);
      border-radius: 6px;
      overflow: hidden;
      display: inline-block;
      vertical-align: middle;
      margin-right: 0.5rem;
    }}
    .bar {{
      height: 100%;
      border-radius: 6px;
      background: linear-gradient(90deg, #5b8cff, var(--ok));
      max-width: 100%;
    }}
    .badge {{
      display: inline-block;
      padding: 0.15rem 0.45rem;
      border-radius: 6px;
      font-size: 0.75rem;
      background: #2d3a52;
      color: var(--muted);
    }}
    .empty {{ color: var(--muted); padding: 1.5rem; text-align: center; }}
    .thumb-40 {{ width: 40px; height: 40px; object-fit: cover; border-radius: 4px; vertical-align: middle; background: var(--bar-bg); }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
      gap: 0.9rem;
    }}
    .card {{
      display: block;
      background: #121620;
      border: 1px solid var(--border);
      border-radius: 10px;
      overflow: hidden;
      color: inherit;
      text-decoration: none;
    }}
    .card:hover {{ border-color: #4a5a7a; }}
    .thumb-wrap {{
      aspect-ratio: 1;
      background: #0a0c12;
      position: relative;
    }}
    .thumb-img {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
    .thumb-ph {{
      width: 100%; height: 100%;
      display: flex; align-items: center; justify-content: center;
      color: var(--muted); font-size: 0.75rem;
    }}
    .card-body {{ padding: 0.55rem 0.65rem; font-size: 0.8rem; }}
    .card-title {{ font-weight: 600; font-size: 0.82rem; line-height: 1.35; margin: 0 0 0.25rem; color: var(--text); display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }}
    .card-meta {{ color: var(--muted); font-size: 0.75rem; display: flex; flex-wrap: wrap; gap: 0.35rem 0.5rem; align-items: center; }}
    .g {{ display: inline-block; font-weight: 700; min-width: 1.1em; text-align: center; font-size: 0.85em; border-radius: 4px; padding: 0.1em 0.32em; }}
    .g-S {{ color: #1a1a12; background: linear-gradient(135deg, #e8c84a, #b8860b); }}
    .g-A {{ color: #0c1410; background: #3d6b55; }}
    .g-B {{ color: #e8f0ff; background: #2a3d6a; }}
    .g-C {{ color: #c4c8d4; background: #2d3548; }}
    .gcell {{ width: 2.2rem; text-align: center; white-space: nowrap; }}
    .grade-float {{ position: absolute; top: 0.4rem; right: 0.4rem; z-index: 1; font-size: 0.7rem; }}
    .dt-cell {{ font-size: 0.8rem; color: #b4b8c8; white-space: nowrap; }}
    tr.tr-ck-ill {{ background: rgba(110, 75, 195, 0.11) !important; }}
    tr.tr-ck-noob {{ background: rgba(0, 150, 125, 0.13) !important; }}
    tr.tr-ck-oth {{ background: rgba(55, 65, 85, 0.1) !important; }}
    a.card-ck-ill {{ border-color: #6b5299; background: #13101a; border-left: 4px solid #7c5fd4; }}
    a.card-ck-noob {{ border-color: #0a7d68; background: #0a1514; border-left: 4px solid #0caa8b; }}
    a.card-ck-oth {{ border-color: #4d5669; background: #121620; border-left: 4px solid #5a6578; }}
    tr.tr-lora-style {{ background: rgba(180, 130, 40, 0.12) !important; }}
    tr.tr-lora-concept {{ background: rgba(90, 110, 200, 0.14) !important; }}
    tr.tr-lora-pose {{ background: rgba(40, 140, 115, 0.13) !important; }}
    a.card-lora-style {{ border-color: #8a6b2d; background: #18140c; border-left: 4px solid #c9a227; }}
    a.card-lora-concept {{ border-color: #4d5a9e; background: #10131c; border-left: 4px solid #6b7fd4; }}
    a.card-lora-pose {{ border-color: #2a6b5c; background: #0c1412; border-left: 4px solid #2db896; }}
    .cat-pill {{ display:inline-block; font-size:0.68rem; font-weight:600; text-transform:capitalize; padding:0.12em 0.42em; border-radius:4px; margin-left:0.35rem; vertical-align:middle; }}
    .cat-pill-style {{ background: rgba(201,162,39,0.25); color: #e8c86a; }}
    .cat-pill-concept {{ background: rgba(107,127,212,0.28); color: #a8b4ff; }}
    .cat-pill-pose {{ background: rgba(45,184,150,0.22); color: #6ee0c5; }}
    .lora-cat-toolbar {{
      display: none;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.4rem 0.85rem;
      padding-left: 0.25rem;
      border-left: 1px solid var(--border);
      margin-left: 0.25rem;
    }}
    .lora-cat-toolbar.visible {{ display: flex; }}
    .lora-cat-label {{ color: var(--muted); font-size: 0.85rem; white-space: nowrap; }}
    .grade-flt-toolbar {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.4rem 0.65rem;
      padding-left: 0.25rem;
      border-left: 1px solid var(--border);
      margin-left: 0.25rem;
    }}
    .grade-flt-label {{ color: var(--muted); font-size: 0.85rem; white-space: nowrap; }}
  </style>
</head>
<body>
  <h1>Civiti.red 採取レポート</h1>
  <div class="toolbar" id="toolbar">
    <label>並び替え
      <select id="sort-key" title="高い/多い/新しい順（取得順＝scrape 時の並び）">
        <option value="order" selected>取得順（新着走査の順）</option>
        <option value="grade">等級（S → C）</option>
        <option value="grade_date">等級 S→C ＋ 日付（新しい）</option>
        <option value="ratio">Rating%（高い＝いいね率）</option>
        <option value="likes">いいね数（多い）</option>
        <option value="dl">DL 数（多い）</option>
        <option value="date">最終更新（新しい）</option>
      </select>
    </label>
    <label>表示
      <select id="view-mode" title="表かグリッド">
        <option value="table">表</option>
        <option value="grid" selected>グリッド</option>
      </select>
    </label>
    <span id="grade-flt-toolbar" class="grade-flt-toolbar" title="チェックした等級の行だけ表示（S/A/B/C）">
      <span class="grade-flt-label">等級</span>
      <label class="ck"><input type="checkbox" id="grade-flt-s" checked /> <span class="g g-S">S</span></label>
      <label class="ck"><input type="checkbox" id="grade-flt-a" checked /> <span class="g g-A">A</span></label>
      <label class="ck"><input type="checkbox" id="grade-flt-b" checked /> <span class="g g-B">B</span></label>
      <label class="ck"><input type="checkbox" id="grade-flt-c" checked /> <span class="g g-C">C</span></label>
    </span>
    <label class="ck" title="信頼度の低い行を隠す（DL が十分に多いものだけ見る）">
      <input type="checkbox" id="hide-low-dl" checked />
      DL 100 以下を非表示
    </label>
    <span id="lora-cat-toolbar" class="lora-cat-toolbar" title="LoRA タブ表示中のみ有効。Style / Concept / Pose 列の表示切替">
      <span class="lora-cat-label">LoRA 表示</span>
      <label class="ck"><input type="checkbox" id="lora-flt-style" checked /> Style</label>
      <label class="ck"><input type="checkbox" id="lora-flt-concept" checked /> Concept</label>
      <label class="ck"><input type="checkbox" id="lora-flt-pose" checked /> Pose</label>
    </span>
  </div>
  <div class="tabs" id="tabs"></div>
  <div id="panels"></div>
  <script type="application/json" id="report-data">{json_blob}</script>
  <script>
    const data = JSON.parse(document.getElementById('report-data').textContent);
    const tabIds = Object.keys(data.sections);
    const tabsEl = document.getElementById('tabs');
    const panelsEl = document.getElementById('panels');
    const sortKeyEl = document.getElementById('sort-key');
    const viewModeEl = document.getElementById('view-mode');
    const hideLowDlEl = document.getElementById('hide-low-dl');
    const gradeFltS = document.getElementById('grade-flt-s');
    const gradeFltA = document.getElementById('grade-flt-a');
    const gradeFltB = document.getElementById('grade-flt-b');
    const gradeFltC = document.getElementById('grade-flt-c');
    const loraFltStyle = document.getElementById('lora-flt-style');
    const loraFltConcept = document.getElementById('lora-flt-concept');
    const loraFltPose = document.getElementById('lora-flt-pose');
    const loraCatToolbar = document.getElementById('lora-cat-toolbar');
    const MIN_DL_VISIBLE = 100;
    function esc(s) {{
      const d = document.createElement('div');
      d.textContent = s == null ? '' : String(s);
      return d.innerHTML;
    }}
    function num(v) {{
      if (v == null || v === '') return 0;
      const n = Number(v);
      return Number.isFinite(n) ? n : 0;
    }}
    function ratioForSort(r) {{
      if (r.ratingPct != null) return num(r.ratingPct);
      if (r.ratioPct == null) return -1e9;
      return num(r.ratioPct);
    }}
    function gradeVal(g) {{
      if (g === 'S') return 4;
      if (g === 'A') return 3;
      if (g === 'B') return 2;
      return 1;
    }}
    function gradeForRow(r) {{
      if (r.ratingGrade && /^[SABC]$/.test(r.ratingGrade)) return r.ratingGrade;
      const p = (r.ratingPct != null && r.ratingPct !== '') ? r.ratingPct : r.ratioPct;
      if (p == null) return 'C';
      const b = data.ratingBands || {{ S: 35, A: 28, B: 22 }};
      const v = num(p);
      if (v >= b.S) return 'S';
      if (v >= b.A) return 'A';
      if (v >= b.B) return 'B';
      return 'C';
    }}
    function timeVal(r) {{
      if (!r.publishedAt) return 0;
      const t = Date.parse(r.publishedAt);
      return Number.isNaN(t) ? 0 : t;
    }}
    function sortRows(rows, key) {{
      const copy = rows.slice();
      if (key === 'order') {{
        copy.sort((a, b) => a.order - b.order);
        return copy;
      }}
      if (key === 'likes') {{
        copy.sort((a, b) => num(b.thumbsUp) - num(a.thumbsUp) || a.order - b.order);
        return copy;
      }}
      if (key === 'dl') {{
        copy.sort((a, b) => num(b.downloads) - num(a.downloads) || a.order - b.order);
        return copy;
      }}
      if (key === 'ratio') {{
        copy.sort((a, b) => ratioForSort(b) - ratioForSort(a) || a.order - b.order);
        return copy;
      }}
      if (key === 'date') {{
        copy.sort((a, b) => timeVal(b) - timeVal(a) || a.order - b.order);
        return copy;
      }}
      if (key === 'grade') {{
        copy.sort(
          (a, b) =>
            gradeVal(gradeForRow(b)) - gradeVal(gradeForRow(a))
            || ratioForSort(b) - ratioForSort(a)
            || a.order - b.order,
        );
        return copy;
      }}
      if (key === 'grade_date') {{
        copy.sort(
          (a, b) =>
            gradeVal(gradeForRow(b)) - gradeVal(gradeForRow(a))
            || timeVal(b) - timeVal(a)
            || a.order - b.order,
        );
        return copy;
      }}
      return copy;
    }}
    function applyDlFilter(rows) {{
      if (!hideLowDlEl.checked) return rows;
      return rows.filter((r) => num(r.downloads) > MIN_DL_VISIBLE);
    }}
    function applyGradeFilter(rows) {{
      const show = {{
        S: gradeFltS.checked,
        A: gradeFltA.checked,
        B: gradeFltB.checked,
        C: gradeFltC.checked,
      }};
      if (show.S && show.A && show.B && show.C) return rows;
      return rows.filter((r) => show[gradeForRow(r)] === true);
    }}
    function applyLoraCategoryFilter(key, rows) {{
      if (key !== 'lora') return rows;
      const st = loraFltStyle.checked;
      const co = loraFltConcept.checked;
      const po = loraFltPose.checked;
      if (st && co && po) return rows;
      return rows.filter((r) => {{
        const t = (r.tagCategory || '').toLowerCase();
        if (t === 'style') return st;
        if (t === 'concept') return co;
        if (t === 'pose') return po;
        return true;
      }});
    }}
    function checkpointBaseVariant(bm) {{
      if (bm == null || bm === '') return 'oth';
      const s = String(bm).toLowerCase();
      if (s.indexOf('illustrious') >= 0) return 'ill';
      if (s.indexOf('noob') >= 0) return 'noob';
      return 'oth';
    }}
    function trClassCheckpoint(r) {{
      return 'tr-ck tr-ck-' + checkpointBaseVariant(r.baseModel);
    }}
    function cardClassCheckpoint(r) {{
      return 'card card-ck card-ck-' + checkpointBaseVariant(r.baseModel);
    }}
    function trClassLora(r) {{
      const t = (r.tagCategory || '').toLowerCase();
      if (t === 'style') return 'tr-lora-style';
      if (t === 'concept') return 'tr-lora-concept';
      if (t === 'pose') return 'tr-lora-pose';
      return '';
    }}
    function cardClassLora(r) {{
      const t = (r.tagCategory || '').toLowerCase();
      if (t === 'style') return 'card card-lora-style';
      if (t === 'concept') return 'card card-lora-concept';
      if (t === 'pose') return 'card card-lora-pose';
      return 'card';
    }}
    function catPillHtml(r) {{
      const t = (r.tagCategory || '').toLowerCase();
      if (t === 'style') return ' <span class="cat-pill cat-pill-style">Style</span>';
      if (t === 'concept') return ' <span class="cat-pill cat-pill-concept">Concept</span>';
      if (t === 'pose') return ' <span class="cat-pill cat-pill-pose">Pose</span>';
      return '';
    }}
    function formatModelCreated(iso) {{
      if (iso == null || iso === '') return '—';
      const t = Date.parse(iso);
      if (Number.isNaN(t)) return esc(String(iso));
      const d = new Date(t);
      return esc(
        d.toLocaleString('ja-JP', {{ year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }}),
      );
    }}
    function renderTable(key, rows, showBase) {{
      if (!rows || rows.length === 0) {{
        return '<p class="empty">該当なし</p>';
      }}
      const maxBar = Math.max(100, data.threshold * 2);
      const thumbTh = showBase
        ? '<th> </th><th>名前</th><th class="dt-cell">最終更新</th><th class="gcell">等級</th><th>Base</th><th class="num">いいね</th><th class="num">DL</th><th class="num">Rating%</th><th class="num">閾値%</th><th>グラフ</th>'
        : '<th> </th><th>名前</th><th class="dt-cell">最終更新</th><th class="gcell">等級</th><th class="num">いいね</th><th class="num">DL</th><th class="num">Rating%</th><th class="num">閾値%</th><th>グラフ</th>';
      let h = '<table><thead><tr>' + thumbTh + '</tr></thead><tbody>';
      for (const r of rows) {{
        const pct = r.ratioPct == null ? 0 : r.ratioPct;
        const w = Math.min(100, (pct / maxBar) * 100);
        const gr = gradeForRow(r);
        let trC = '';
        if (key === 'checkpoint') trC = trClassCheckpoint(r);
        else if (key === 'lora') trC = trClassLora(r);
        const img = r.imageUrl
          ? '<img class="thumb-40" src="' + esc(r.imageUrl) + '" alt="" loading="lazy" decoding="async" />'
          : '<span class="thumb-ph" style="width:40px;height:40px;display:inline-block"></span>';
        h += '<tr' + (trC ? ' class=\"' + trC + '\"' : '') + '><td style="width:48px;">' + img + '</td><td><a href="' + esc(r.url) + '" target="_blank" rel="noopener">'
          + esc(r.name) + '</a> <span class="badge">#' + esc(r.id) + '</span>' + catPillHtml(r) + '</td>'
          + '<td class="dt-cell">' + formatModelCreated(r.publishedAt) + '</td>'
          + '<td class="gcell"><span class="g g-' + gr + '">' + esc(gr) + '</span></td>'
          + (showBase ? '<td>' + esc(r.baseModel || '—') + '</td>' : '')
          + '<td class="num">' + esc(r.thumbsUp) + '</td>'
          + '<td class="num">' + esc(r.downloads) + '</td>'
          + '<td class="num">' + esc(r.ratingPct == null ? r.ratioPct : r.ratingPct) + '</td>'
          + '<td class="num">' + esc(r.thresholdPct) + '</td>'
          + '<td><span class="bar-wrap"><span class="bar" style="width:' + w.toFixed(1) + '%"></span></span>'
          + esc(pct) + '%</td>'
          + '</tr>';
      }}
      h += '</tbody></table>';
      return h;
    }}
    function renderGrid(key, rows, showBase) {{
      if (!rows || rows.length === 0) {{
        return '<p class="empty">該当なし</p>';
      }}
      let h = '<div class="grid">';
      for (const r of rows) {{
        const gr = gradeForRow(r);
        const rp = (r.ratingPct != null && r.ratingPct !== '') ? r.ratingPct : r.ratioPct;
        let cCl = 'card';
        if (key === 'checkpoint') cCl = cardClassCheckpoint(r);
        else if (key === 'lora') cCl = cardClassLora(r);
        const dlab = (r.publishedAt != null && r.publishedAt !== '') ? (formatModelCreated(r.publishedAt) + ' · ') : '';
        const meta = dlab
          + '<span class="g g-' + gr + '">' + esc(gr) + '</span> · '
          + (showBase ? (esc(r.baseModel || '—') + ' · ') : '')
          + 'Rating ' + esc(rp) + '% · ♥' + esc(r.thumbsUp) + ' · DL' + esc(r.downloads);
        h += '<a class="' + cCl + '" href="' + esc(r.url) + '" target="_blank" rel="noopener">'
          + '<div class="thumb-wrap" style="position:relative">'
          + '<span class="grade-float g g-' + gr + '">' + esc(gr) + '</span>'
          + (r.imageUrl
            ? '<img class="thumb-img" src="' + esc(r.imageUrl) + '" alt="" loading="lazy" decoding="async" />'
            : '<div class="thumb-ph">no image</div>')
          + '</div>'
          + '<div class="card-body">'
          + '<p class="card-title">' + esc(r.name) + ' <span class="badge">#' + esc(r.id) + '</span>' + catPillHtml(r) + '</p>'
          + '<div class="card-meta">' + meta + '</div>'
          + '</div></a>';
      }}
      h += '</div>';
      return h;
    }}
    function renderPanel(key) {{
      const showBase = key === 'checkpoint' || key === 'custom';
      const raw = data.sections[key] || [];
      const sorted = sortRows(raw, sortKeyEl.value);
      const afterDl = applyDlFilter(sorted);
      const afterGrade = applyGradeFilter(afterDl);
      const visible = applyLoraCategoryFilter(key, afterGrade);
      const isGrid = viewModeEl.value === 'grid';
      if (isGrid) return renderGrid(key, visible, showBase);
      return renderTable(key, visible, showBase);
    }}
    function setLoraToolbarVisible(show) {{
      if (loraCatToolbar) loraCatToolbar.classList.toggle('visible', !!show);
    }}
    function refreshAllPanels() {{
      tabIds.forEach((k) => {{
        const p = document.getElementById('panel-' + k);
        if (p) p.innerHTML = renderPanel(k);
      }});
    }}
    tabIds.forEach((key, i) => {{
      const t = document.createElement('button');
      t.textContent = data.titles[key] || key;
      t.type = 'button';
      if (i === 0) t.className = 'active';
      t.dataset.tab = key;
      tabsEl.appendChild(t);
      const p = document.createElement('div');
      p.className = 'panel' + (i === 0 ? ' active' : '');
      p.id = 'panel-' + key;
      p.innerHTML = renderPanel(key);
      panelsEl.appendChild(p);
    }});
    setLoraToolbarVisible(tabIds.length && tabIds[0] === 'lora');
    sortKeyEl.addEventListener('change', refreshAllPanels);
    viewModeEl.addEventListener('change', refreshAllPanels);
    hideLowDlEl.addEventListener('change', refreshAllPanels);
    [gradeFltS, gradeFltA, gradeFltB, gradeFltC].forEach((el) => {{
      if (el) el.addEventListener('change', refreshAllPanels);
    }});
    [loraFltStyle, loraFltConcept, loraFltPose].forEach((el) => {{
      if (el) el.addEventListener('change', refreshAllPanels);
    }});
    tabsEl.addEventListener('click', (e) => {{
      const btn = e.target.closest('button');
      if (!btn || !btn.dataset.tab) return;
      const k = btn.dataset.tab;
      tabsEl.querySelectorAll('button').forEach(b => b.classList.toggle('active', b === btn));
      panelsEl.querySelectorAll('.panel').forEach(p => p.classList.toggle('active', p.id === 'panel-' + k));
      setLoraToolbarVisible(k === 'lora');
    }});
  </script>
  <footer class="sub" style="margin-top:2rem; padding-top:0.75rem; border-top:1px solid var(--border); font-size:0.8rem; color: var(--muted);">
    レポート生成: <strong>{gen_ts}</strong>
    <br />
    開いているのが <strong>本プロジェクトの <code style="user-select:all">out/report.html</code></strong> か確認し、新しい版は <code style="user-select:all">python3 regenerate_report.py</code> で再生成（ブラウザは強制再読み込み推奨）。
  </footer>
</body>
</html>
"""


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if not (args.api_key or "").strip():
        print(
            "API key is required (login-gated / NSFW). "
            "Pass --api-key or set env CIVITAI_API_KEY.",
            file=sys.stderr,
        )
        return 2
    api_key = args.api_key.strip()
    base = args.base_url.rstrip("/")

    nsfw_val: bool | None
    if args.nsfw == "omit":
        nsfw_val = None
    else:
        nsfw_val = args.nsfw == "true"

    hb_state: dict[str, Any] = {
        "phase": "starting",
        "pages_done": 0,
        "scanned": 0,
        "hits": 0,
        "category": "",
    }
    hb_stop = threading.Event()
    if args.heartbeat_sec > 0:
        print(
            f"[heartbeat] run started; every {args.heartbeat_sec}s on stderr",
            file=sys.stderr,
            flush=True,
        )
        threading.Thread(
            target=_heartbeat_worker,
            args=(hb_stop, args.heartbeat_sec, hb_state),
            name="heartbeat",
            daemon=True,
        ).start()

    base_models_ckpt = tuple(
        s.strip() for s in args.base_models.split(",") if s.strip()
    )
    if args.preset in ("checkpoint", "all") and not base_models_ckpt:
        print(
            "Checkpoint fetch needs at least one --base-models value "
            f"(default: {','.join(DEFAULT_CHECKPOINT_BASE_MODELS)}).",
            file=sys.stderr,
        )
        return 2

    if args.preset in ("lora", "all"):
        lora_tags = tuple(
            s.strip().lower() for s in (args.lora_tags or "").split(",") if s.strip()
        )
        if not lora_tags:
            print(
                "--lora-tags needs at least one tag (default: style,concept,pose).",
                file=sys.stderr,
            )
            return 2

    lora_exclude = parse_exclude_tag_set(args.lora_exclude_tags)

    try:
        if args.preset == "all":
            os.makedirs(args.out_dir, exist_ok=True)
            bundle: dict[str, list[dict[str, Any]]] = {
                "lora": [],
                "checkpoint": [],
                "embedding": [],
            }
            summaries: list[dict[str, Any]] = []

            out_path_lora = os.path.join(args.out_dir, "hits_lora.jsonl")
            print(f"--- pass: lora (LORA+LoCon; tags={lora_tags}) -> {out_path_lora}", file=sys.stderr)
            with open(out_path_lora, "w", encoding="utf-8") as out_f:
                for list_tag in lora_tags:
                    summ = _run_one_pass(
                        base=base,
                        api_key=api_key,
                        types=DEFAULT_LORA_API_TYPES,
                        base_models=None,
                        category="lora",
                        list_tag=list_tag,
                        limit=args.limit,
                        nsfw_val=nsfw_val,
                        min_thumb_pct=args.min_thumb_pct,
                        max_pages=args.max_pages,
                        max_items=args.max_items,
                        sleep=args.sleep,
                        timeout=args.timeout,
                        out_f=out_f,
                        hit_rows=bundle["lora"],
                        hb_state=hb_state,
                        skip_model_detail=args.skip_model_detail,
                        model_detail_sleep=args.model_detail_sleep,
                        exclude_exact_tags=lora_exclude,
                    )
                    summaries.append({"leg": "lora", **summ})

            out_path_ck = os.path.join(args.out_dir, "hits_checkpoint.jsonl")
            print(f"--- pass: checkpoint -> {out_path_ck}", file=sys.stderr)
            with open(out_path_ck, "w", encoding="utf-8") as out_f:
                summ = _run_one_pass(
                    base=base,
                    api_key=api_key,
                    types=("Checkpoint",),
                    base_models=base_models_ckpt,
                    category="checkpoint",
                    limit=args.limit,
                    nsfw_val=nsfw_val,
                    min_thumb_pct=args.min_thumb_pct,
                    max_pages=args.max_pages,
                    max_items=args.max_items,
                    sleep=args.sleep,
                    timeout=args.timeout,
                    out_f=out_f,
                    hit_rows=bundle["checkpoint"],
                    hb_state=hb_state,
                    skip_model_detail=args.skip_model_detail,
                    model_detail_sleep=args.model_detail_sleep,
                )
                summaries.append({"leg": "checkpoint", **summ})

            out_path_emb = os.path.join(args.out_dir, "hits_embedding.jsonl")
            print(f"--- pass: embedding -> {out_path_emb}", file=sys.stderr)
            with open(out_path_emb, "w", encoding="utf-8") as out_f:
                summ = _run_one_pass(
                    base=base,
                    api_key=api_key,
                    types=("TextualInversion",),
                    base_models=None,
                    category="embedding",
                    limit=args.limit,
                    nsfw_val=nsfw_val,
                    min_thumb_pct=args.min_thumb_pct,
                    max_pages=args.max_pages,
                    max_items=args.max_items,
                    sleep=args.sleep,
                    timeout=args.timeout,
                    out_f=out_f,
                    hit_rows=bundle["embedding"],
                    hb_state=hb_state,
                    skip_model_detail=args.skip_model_detail,
                    model_detail_sleep=args.model_detail_sleep,
                )
                summaries.append({"leg": "embedding", **summ})

            report_path = os.path.join(args.out_dir, "report.html")
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
            html_doc = build_html_report(
                min_thumb_pct=args.min_thumb_pct,
                sections=sections_html,
                titles=titles,
            )
            with open(report_path, "w", encoding="utf-8") as rf:
                rf.write(html_doc)
            print(
                json.dumps({"passes": summaries, "report_html": report_path}, ensure_ascii=False),
                file=sys.stderr,
            )
        else:
            if args.preset == "custom":
                types = tuple(s.strip() for s in args.types.split(",") if s.strip())
                base_models: tuple[str, ...] | None = None
                category = "custom"
            elif args.preset == "lora":
                types = DEFAULT_LORA_API_TYPES
                base_models = None
                category = "lora"
            elif args.preset == "checkpoint":
                types = ("Checkpoint",)
                base_models = base_models_ckpt
                category = "checkpoint"
            else:
                types = ("TextualInversion",)
                base_models = None
                category = "embedding"

            if not types:
                print("No types to fetch.", file=sys.stderr)
                return 2

            hit_rows: list[dict[str, Any]] | None = [] if args.html else None
            out_f: TextIO
            if args.out == "-":
                out_f = sys.stdout
                close_out = False
            else:
                out_f = open(args.out, "w", encoding="utf-8")
                close_out = True
            try:
                if args.preset == "lora":
                    summaries_lora: list[dict[str, Any]] = []
                    for list_tag in lora_tags:
                        summ = _run_one_pass(
                            base=base,
                            api_key=api_key,
                            types=types,
                            base_models=base_models,
                            category=category,
                            list_tag=list_tag,
                            limit=args.limit,
                            nsfw_val=nsfw_val,
                            min_thumb_pct=args.min_thumb_pct,
                            max_pages=args.max_pages,
                            max_items=args.max_items,
                            sleep=args.sleep,
                            timeout=args.timeout,
                            out_f=out_f,
                            hit_rows=hit_rows,
                            hb_state=hb_state,
                            skip_model_detail=args.skip_model_detail,
                            model_detail_sleep=args.model_detail_sleep,
                            exclude_exact_tags=lora_exclude,
                        )
                        summaries_lora.append(summ)
                    summ_out: dict[str, Any] | list[dict[str, Any]] = (
                        summaries_lora[0]
                        if len(summaries_lora) == 1
                        else {"preset": "lora", "passes": summaries_lora}
                    )
                else:
                    summ_out = _run_one_pass(
                        base=base,
                        api_key=api_key,
                        types=types,
                        base_models=base_models,
                        category=category,
                        limit=args.limit,
                        nsfw_val=nsfw_val,
                        min_thumb_pct=args.min_thumb_pct,
                        max_pages=args.max_pages,
                        max_items=args.max_items,
                        sleep=args.sleep,
                        timeout=args.timeout,
                        out_f=out_f,
                        hit_rows=hit_rows,
                        hb_state=hb_state,
                        skip_model_detail=args.skip_model_detail,
                        model_detail_sleep=args.model_detail_sleep,
                    )
            except RuntimeError as e:
                print(str(e), file=sys.stderr)
                return 1
            finally:
                if close_out:
                    out_f.close()

            print(json.dumps(summ_out, ensure_ascii=False), file=sys.stderr)

            if args.html and hit_rows is not None:
                single_sections = {
                    category: _rows_for_html_report(
                        hit_rows,
                        show_base_model=(category in ("checkpoint", "custom")),
                    ),
                }
                single_titles = {
                    category: {
                        "lora": "LoRA / LyCORIS（Style・Concept・Pose）",
                        "checkpoint": "Checkpoint（Illustrious / NoobAI）",
                        "embedding": "Embedding",
                        "custom": "Custom types",
                    }.get(category, "Results"),
                }
                html_doc = build_html_report(
                    min_thumb_pct=args.min_thumb_pct,
                    sections=single_sections,
                    titles=single_titles,
                )
                with open(args.html, "w", encoding="utf-8") as hf:
                    hf.write(html_doc)
                print(json.dumps({"report_html": args.html}, ensure_ascii=False), file=sys.stderr)
            return 0
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1
    finally:
        hb_stop.set()

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
