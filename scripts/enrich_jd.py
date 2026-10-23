#!/usr/bin/env python3
"""补全达标岗位的真实 JD 正文与详情字段 → `data/jd_cache.json`。

为什么单独一步、单独存储（三个原因，都很实际）：

1. **列表接口没有 JD**。Workday 列表只返回标题 + `locationsText`（多地点时是
   "2 Locations"），JD 正文、真实发布日期、职位编号、能否投递都只有详情接口有。

2. **写回 jobs.json 会被冲掉**。`sync._merge_incremental()` 会用本次抓取的快照
   「整行重建」每条岗位，只保留 `first_seen` / `status` / `seen_count` 三个字段。
   所以补全字段必须落在 sync 不碰的地方。

3. **不能塞进 jd_text**。`score.py` 的关键词命中用的是 `title + jd_text`，
   把 6000 字的 JD 塞进去会让 `must_keywords` 命中数虚高，破坏已校准的打分
   （`is_design_role()` 门槛只看标题，但分数会被 JD 堆词抬起来）。

流程：jobs.json（已打分） → 只挑达标岗位 → 详情接口 → jd_cache.json

用法：
    python scripts/enrich_jd.py                    # 默认 min-match 55
    python scripts/enrich_jd.py --force            # 忽略 7 天缓存，强制重抓
    python scripts/enrich_jd.py --limit 10         # 最多补 10 条（调试用）
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
from typing import Dict, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from job_radar import jd_extract                      # noqa: E402
from job_radar.adapters.workday import _get_detail    # noqa: E402
from job_radar.sync import read_sources               # noqa: E402

DATA_DIR = os.path.join(ROOT, "data")
JOBS = os.path.join(DATA_DIR, "jobs.json")
CACHE = os.path.join(DATA_DIR, "jd_cache.json")
TTL_DAYS = 7
MAX_JD_CHARS = 12000     # 单条 JD 入库上限，防止个别岗位贴了整本手册


# --------------------------------------------------------------------------- #
# 缓存读写
# --------------------------------------------------------------------------- #
def load_cache(path: str = CACHE) -> Dict:
    if not os.path.exists(path):
        return {"version": 1, "entries": {}}
    try:
        d = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "entries": {}}
    if not isinstance(d.get("entries"), dict):
        d["entries"] = {}
    d.setdefault("version", 1)
    return d


def save_cache(cache: Dict, path: str = CACHE) -> None:
    cache["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _fresh(entry: Dict, ttl_days: int) -> bool:
    """缓存是否还在有效期内（按 fetched_at 判断）。"""
    raw = str(entry.get("fetched_at") or "")
    if not raw:
        return False
    try:
        when = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    age = dt.datetime.now(dt.timezone.utc) - when
    return age.days < ttl_days


# --------------------------------------------------------------------------- #
# URL / 信源解析
# --------------------------------------------------------------------------- #
def path_from_url(url: str) -> str:
    """从岗位公开链接里反解出 Workday 的 externalPath。

    https://host/en-US/<site>/job/<path>  →  <path>
    """
    if "/job/" not in (url or ""):
        return ""
    return url.split("/job/", 1)[1].strip("/")


def workday_endpoints() -> Dict[str, str]:
    """source_id → "host|tenant|site"。只保留 workday 信源。"""
    out: Dict[str, str] = {}
    try:
        rows = read_sources()
    except OSError:
        return out
    for r in rows:
        if (r.get("adapter") or "").strip() != "workday":
            continue
        ep = (r.get("endpoint") or "").strip()
        if ep.count("|") == 2:
            out[r["source_id"]] = ep
    return out


def _split(endpoint: str):
    host, tenant, site = endpoint.split("|", 2)
    return host, tenant, site


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def enrich(min_match: int = 55, limit: int = 40, force: bool = False,
           ttl_days: int = TTL_DAYS, verbose: bool = True) -> Dict:
    jobs = json.load(open(JOBS, encoding="utf-8"))
    if isinstance(jobs, dict):
        jobs = jobs.get("jobs") or []
    endpoints = workday_endpoints()
    cache = load_cache()
    entries: Dict[str, Dict] = cache["entries"]

    targets = [j for j in jobs
               if not j.get("gone")
               and int(j.get("match_score") or 0) >= min_match
               and (j.get("source_id") or "") in endpoints]
    targets.sort(key=lambda j: int(j.get("match_score") or 0), reverse=True)
    targets = targets[:limit]

    stats = {"targets": len(targets), "fetched": 0, "cached": 0, "failed": 0,
             "with_jd": 0, "flagged": 0}

    for job in targets:
        key = job.get("dedup_key") or job.get("job_id") or ""
        if not key:
            continue
        old = entries.get(key) or {}
        if not force and _fresh(old, ttl_days) and old.get("jd_text"):
            stats["cached"] += 1
            if verbose:
                print(f"  · 缓存命中 {job.get('company_name')} / {job.get('title')[:40]}")
            continue

        path = path_from_url(job.get("official_url") or "")
        if not path:
            stats["failed"] += 1
            if verbose:
                print(f"  ! 无法从链接解析岗位路径：{job.get('official_url')}")
            continue

        host, tenant, site = _split(endpoints[job["source_id"]])
        det = _get_detail(host, tenant, site, path)
        if not det:
            stats["failed"] += 1
            if verbose:
                print(f"  ! 详情接口失败 {job.get('company_name')} / {job.get('title')[:40]}")
            continue

        jd_html = det.get("jd_html") or ""
        jd_text = jd_extract.strip_boilerplate(jd_extract.html_to_text(jd_html))[:MAX_JD_CHARS]
        summary = jd_extract.summarize(jd_html, job.get("title") or "")
        summary.pop("text", None)          # 全文单独存，summary 里不重复

        entries[key] = {
            "source_id": job.get("source_id") or "",
            "company_name": job.get("company_name") or "",
            "title": job.get("title") or "",
            "jd_html_len": len(jd_html),
            "jd_text": jd_text,
            "jd_hash": hashlib.sha1(jd_text.encode("utf-8")).hexdigest()[:16] if jd_text else "",
            "start_date": det.get("start_date") or "",
            "end_date": det.get("end_date") or "",
            "time_type": det.get("time_type") or "",
            "req_id": det.get("req_id") or "",
            "can_apply": det.get("can_apply"),
            "posted_on": det.get("posted_on") or "",
            "location": det.get("location") or "",
            "additional_locations": det.get("additional_locations") or [],
            "summary": summary,
            "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        stats["fetched"] += 1
        if summary.get("has_jd"):
            stats["with_jd"] += 1
        if summary.get("flag"):
            stats["flagged"] += 1
        if verbose:
            tag = "有JD" if summary.get("has_jd") else "无JD"
            flag = " ⚠冲突" if summary.get("flag") else ""
            print(f"  ✓ {job.get('company_name')} / {(job.get('title') or '')[:40]} "
                  f"[{tag} {len(jd_text)}字]{flag}")

    # 清掉已下线岗位的缓存，避免文件无限膨胀
    alive = {j.get("dedup_key") for j in jobs if j.get("dedup_key")}
    pruned = [k for k in entries if k not in alive]
    for k in pruned:
        entries.pop(k, None)
    if pruned:
        stats["pruned"] = len(pruned)

    save_cache(cache)
    if verbose:
        print(f"\n补全完成：目标 {stats['targets']}｜新抓 {stats['fetched']}｜"
              f"缓存 {stats['cached']}｜失败 {stats['failed']}｜"
              f"有JD {stats['with_jd']}｜正文冲突 {stats['flagged']}")
        print(f"写入：{CACHE}")
    return stats


def main() -> None:
    p = argparse.ArgumentParser(description="补全达标岗位的真实 JD 正文与详情字段。")
    p.add_argument("--min-match", type=int, default=55, help="只补这个分数以上的岗位")
    p.add_argument("--limit", type=int, default=40, help="单次最多补多少条")
    p.add_argument("--ttl-days", type=int, default=TTL_DAYS, help="缓存有效期（天）")
    p.add_argument("--force", action="store_true", help="忽略缓存强制重抓")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()
    enrich(args.min_match, args.limit, args.force, args.ttl_days, not args.quiet)


if __name__ == "__main__":
    main()
