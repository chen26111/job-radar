#!/usr/bin/env python3
"""校验即将播报岗位的链接是否还能打开 → `data/link_health.json`。

为什么必须单独一步（三个理由）：

1. **Workday 的岗位下线后不一定回 404**。实测多数是 200 + 一句
   "The job posting you are looking for might have expired"，或者直接 302 到
   careers 首页。只看状态码会把死岗判成活岗。
2. **反过来，403/429 也不能判定岗位已下线**。那是我们被反爬拦了，不是岗位没了。
   把网络问题算到岗位头上，会把好岗位误杀。
   所以本脚本区分三种结论：**可用 / 已下线 / 无法判定**。
3. **单次抖动不能当结论**。`fails` 只累计「已下线」的判定，连续 3 次才由
   `notify_preview` 剔除，偶发一次 503 不会影响播报。

**Workday 岗位不能靠抓公开页判断**（实测）：公开页是前端渲染的空壳，随便编一个不存在的
岗位路径也照样返回 200 + 6KB 的壳，正文里连 "not found" 都没有。能判定生死的只有它自己的
详情接口 `GET /wday/cxs/{tenant}/{site}/job/{path}`：

    · 活岗 → 200 + `{"jobPostingInfo": {...}}`
    · 死岗 → **404** + `{"errorCode":"S21", "message":"not found: Job_Posting_Anchor_ID=..."}`

所以 Workday 岗位一律走详情接口（判定权威、响应只有几 KB），非 Workday 链接才退回
「抓页面 + 找下线文案」的启发式判断：

    · HTTP 404 / 410
    · 跳转后的地址里已经没有 `/job/`（被丢回 careers 首页了）
    · 页面正文出现明确下线提示（`no longer available` / `该职位已关闭` …）

写入结构（`notify_preview.py` 读的是 `entries[key].ok` 和 `entries[key].fails`）：

```jsonc
{ "version": 1, "entries": {
    "<dedup_key>": {
      "url": "https://pfizer.wd1.myworkdayjobs.com/en-US/PfizerCareers/job/...",
      "ok": true,          // true 可用 / false 已下线 / null 无法判定
      "status": 200,
      "reason": "",        // 不可用时给人看的原因
      "fails": 0,          // 连续「已下线」次数，可用了立刻归零
      "checked_at": "2026-10-23T08:31:00+08:00",
      "last_error": ""     // 无法判定时的错误摘要，只排障用
    } } }
```

用法：
    python scripts/verify_links.py                  # 默认 min-match 55，最多 20 条
    python scripts/verify_links.py --limit 5        # 只查 5 条
    python scripts/verify_links.py --force          # 忽略 6 小时缓存
    python scripts/verify_links.py --dry-run        # 只打印，不写文件
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
for _p in (ROOT, SCRIPTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from notify_preview import LOW, job_key          # noqa: E402  低质标签与主键口径唯一来源
from enrich_jd import path_from_url, workday_endpoints   # noqa: E402
from job_radar.adapters.http import get_json     # noqa: E402

DATA_DIR = os.path.join(ROOT, "data")
JOBS = os.path.join(DATA_DIR, "jobs.json")
LINK_HEALTH = os.path.join(DATA_DIR, "link_health.json")

TTL_HOURS = 6            # 6 小时内查过的不重复请求
TIMEOUT = 12
MAX_BODY = 96 * 1024     # 只读前 96KB 找下线提示，够用，不拖时间
MIN_INTERVAL = 0.4       # 请求间隔，别把人家官网打疼

FAIL_LIMIT = 3           # 与 notify_preview.LINK_FAIL_LIMIT 保持一致
DEAD_STATUS = {404, 410}

# 页面里出现这些就当已下线。全部是 Workday/Greenhouse 家自带的错误文案，
# 不写泛化的 "not found"（正常页面里也可能有这个词）。
DEAD_MARKERS = (
    "no longer available",
    "no longer accepting applications",
    "job posting you are looking for",
    "position has been filled",
    "this job has expired",
    "该职位已关闭",
    "该职位不存在",
    "职位已下线",
    "岗位已下线",
    "此职位已过期",
    "职位已不再开放",
)

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "identity",   # 不要压缩，省得自己解 gzip
}


# --------------------------------------------------------------------------- #
# 读写
# --------------------------------------------------------------------------- #
def _load_json(path: str, default):
    if not path or not os.path.exists(path):
        return default
    try:
        return json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return default


def load_health(path: str = LINK_HEALTH) -> Dict:
    d = _load_json(path, None)
    if not isinstance(d, dict):
        d = {"version": 1, "entries": {}}
    if not isinstance(d.get("entries"), dict):
        d["entries"] = {}
    d.setdefault("version", 1)
    return d


def save_health(h: Dict, path: str = LINK_HEALTH) -> None:
    h["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(h, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)      # 原子写，避免 Actions 中途挂掉留半个 JSON


def _recent(entry: Dict, ttl_hours: int) -> bool:
    raw = str(entry.get("checked_at") or "")
    if not raw:
        return False
    try:
        when = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - when).total_seconds() < ttl_hours * 3600


# --------------------------------------------------------------------------- #
# 单条链接校验
# --------------------------------------------------------------------------- #
# https://pfizer.wd1.myworkdayjobs.com/en-US/PfizerCareers/job/China---../UI-Designer-_4961960
_WD_URL = re.compile(
    r"^https://(?P<host>[^/]+)/(?:[A-Za-z]{2}-[A-Za-z]{2}/)?(?P<site>[^/]+)/job/(?P<path>.+)$"
)


def _workday_parts(job: Dict, url: str,
                   endpoints: Dict[str, str]) -> Optional[Tuple[str, str, str, str]]:
    """解析出 (host, tenant, site, path)。不是 Workday 岗位链接就返回 None。

    tenant 优先取 `config/sources.csv` 里的权威值（`host|tenant|site`），
    取不到才用域名首段猜——很多公司 tenant 与域名首段一致，但并非全部，
    所以从这里拿到的结果只用于「查得到就查」，查不到不算岗位的错。
    """
    if "myworkdayjobs.com" not in (url or "") or "/job/" not in url:
        return None
    ep = endpoints.get(job.get("source_id") or "")
    if ep and ep.count("|") == 2:
        host, tenant, site = ep.split("|", 2)
        path = path_from_url(url)
        if path:
            return host, tenant, site, path
    m = _WD_URL.match(url)
    if not m:
        return None
    return (m.group("host"), m.group("host").split(".")[0],
            m.group("site"), m.group("path").strip("/"))


def check_workday(host: str, tenant: str, site: str,
                  path: str) -> Tuple[Optional[bool], int, str]:
    """查 Workday 详情接口判生死。活岗 200，死岗 404。"""
    url = f"https://{host}/wday/cxs/{tenant}/{site}/job/{path.lstrip('/')}"
    try:
        d = get_json(url, headers={"Referer": f"https://{host}/"})
    except urllib.error.HTTPError as e:
        if e.code in DEAD_STATUS:
            return False, int(e.code), "官网详情接口查不到该岗位，已下线"
        return None, int(e.code), f"HTTP {e.code}，无法判定（可能被反爬拦截）"
    except Exception as e:                                   # noqa: BLE001
        return None, 0, f"{type(e).__name__}: {e}"
    if not ((d or {}).get("jobPostingInfo") or {}):
        return False, 200, "详情接口返回空内容，疑似已下线"
    return True, 200, ""


def check_html(url: str, timeout: int = TIMEOUT) -> Tuple[Optional[bool], int, str]:
    """非 Workday 链接：抓页面 + 找下线文案。"""
    req = urllib.request.Request(url, method="GET", headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = int(getattr(resp, "status", 200) or 200)
            final = resp.geturl() or url
            body = resp.read(MAX_BODY)
    except urllib.error.HTTPError as e:
        if e.code in DEAD_STATUS:
            return False, e.code, f"HTTP {e.code}，岗位页面已不存在"
        return None, int(e.code), f"HTTP {e.code}，无法判定（可能被反爬拦截）"
    except Exception as e:                                   # noqa: BLE001
        return None, 0, f"{type(e).__name__}: {e}"

    # 被丢回列表页 / 首页 —— 下线岗位最常见的样子
    if "/job/" in url and "/job/" not in final:
        return False, status, "跳转到了非岗位页，疑似已下线"

    text = body.decode("utf-8", "ignore").lower()
    for marker in DEAD_MARKERS:
        if marker in text:
            return False, status, f"页面提示「{marker}」"

    return True, status, ""


def check(job: Dict, endpoints: Optional[Dict[str, str]] = None
          ) -> Tuple[Optional[bool], int, str]:
    """返回 (ok, status, reason)。ok=None 表示「无法判定」，不算岗位的错。"""
    url = job.get("official_url") or job
    if not isinstance(url, str) or not url.startswith("http"):
        return None, 0, "不是 http 链接"
    parts = _workday_parts(job if isinstance(job, dict) else {}, url, endpoints or {})
    if parts:
        return check_workday(*parts)
    return check_html(url)


# --------------------------------------------------------------------------- #
# 选目标
# --------------------------------------------------------------------------- #
def targets(min_match: int = 55, limit: int = 20) -> List[Dict]:
    """和 notify_preview 同一套筛选口径：在架、非低质、达标、有链接。"""
    jobs = _load_json(JOBS, [])
    if isinstance(jobs, dict):
        jobs = jobs.get("jobs") or []
    rows: List[Dict] = []
    for j in jobs:
        if j.get("gone"):
            continue
        if LOW & set(j.get("tags") or []):
            continue
        if int(j.get("match_score") or 0) < min_match:
            continue
        if not (j.get("official_url") or "").startswith("http"):
            continue
        rows.append(j)
    rows.sort(key=lambda j: int(j.get("match_score") or 0), reverse=True)
    return rows[:limit]


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run(min_match: int = 55, limit: int = 20, force: bool = False,
        ttl_hours: int = TTL_HOURS, dry_run: bool = False,
        verbose: bool = True) -> Dict:
    rows = targets(min_match, limit)
    endpoints = workday_endpoints()
    health = load_health()
    entries: Dict[str, Dict] = health["entries"]

    stats = {"targets": len(rows), "checked": 0, "ok": 0, "dead": 0,
             "unknown": 0, "cached": 0, "pruned": 0}

    for i, job in enumerate(rows):
        key = job_key(job)
        url = job.get("official_url") or ""
        if not key:
            continue

        old = entries.get(key) or {}
        if not force and _recent(old, ttl_hours):
            stats["cached"] += 1
            continue

        if i:
            time.sleep(MIN_INTERVAL)
        ok, status, reason = check(job, endpoints)
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        stats["checked"] += 1

        if ok is True:
            entries[key] = {"url": url, "ok": True, "status": status, "reason": "",
                            "fails": 0, "checked_at": now, "last_error": ""}
            stats["ok"] += 1
            if verbose:
                print(f"  ✓ {job.get('company_name')} / {(job.get('title') or '')[:40]}")
        elif ok is False:
            fails = int(old.get("fails") or 0) + 1
            entries[key] = {"url": url, "ok": False, "status": status, "reason": reason,
                            "fails": fails, "checked_at": now, "last_error": ""}
            stats["dead"] += 1
            if verbose:
                tail = f"（连续 {fails}/{FAIL_LIMIT} 次）" if fails < FAIL_LIMIT else \
                       f"（已达 {FAIL_LIMIT} 次，将不再播报）"
                print(f"  ✗ {job.get('company_name')} / {(job.get('title') or '')[:40]} "
                      f"— {reason}{tail}")
        else:
            # 无法判定：保留上一次的 ok / fails，只记错误，绝不让网络问题误杀岗位
            entry = dict(old)
            entry.update({"url": url, "last_error": reason, "last_attempt_at": now})
            entry.setdefault("ok", None)
            entry.setdefault("fails", 0)
            entry.setdefault("checked_at", "")
            entries[key] = entry
            stats["unknown"] += 1
            if verbose:
                print(f"  ? {job.get('company_name')} / {(job.get('title') or '')[:40]} "
                      f"— {reason}")

    # 清掉已不在库里的岗位，避免文件无限增长
    alive = set()
    all_jobs = _load_json(JOBS, [])
    if isinstance(all_jobs, dict):
        all_jobs = all_jobs.get("jobs") or []
    for j in all_jobs:
        if not j.get("gone") and job_key(j):
            alive.add(job_key(j))
    pruned = [k for k in entries if k not in alive]
    for k in pruned:
        entries.pop(k, None)
    stats["pruned"] = len(pruned)

    if dry_run:
        if verbose:
            print("\n[dry-run] 未写入文件")
    else:
        save_health(health)

    if verbose:
        print(f"\n链接校验：目标 {stats['targets']}｜检查 {stats['checked']}｜"
              f"正常 {stats['ok']}｜已下线 {stats['dead']}｜无法判定 {stats['unknown']}｜"
              f"缓存 {stats['cached']}"
              + (f"｜清理 {stats['pruned']}" if stats["pruned"] else ""))
        if stats["dead"]:
            print(f"已下线岗位连续 {FAIL_LIMIT} 次后自动停止播报，未满 {FAIL_LIMIT} 次的仍会展示并标注")
        if not dry_run:
            print(f"写入：{LINK_HEALTH}")
    return stats


def main() -> None:
    p = argparse.ArgumentParser(description="校验岗位链接可用性。")
    p.add_argument("--min-match", type=int, default=55, help="只查这个分数以上的岗位")
    p.add_argument("--limit", type=int, default=20, help="单次最多查多少条")
    p.add_argument("--ttl-hours", type=int, default=TTL_HOURS, help="多久内查过就不再查")
    p.add_argument("--force", action="store_true", help="忽略缓存强制重查")
    p.add_argument("--dry-run", action="store_true", help="只打印，不写 link_health.json")
    p.add_argument("--quiet", action="store_true")
    a = p.parse_args()
    run(a.min_match, a.limit, a.force, a.ttl_hours, a.dry_run, not a.quiet)


if __name__ == "__main__":
    main()
