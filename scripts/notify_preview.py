#!/usr/bin/env python3
"""生成自动化推送预览（本仓库定向版：杭州外企设计岗）。

数据流：
    data/jobs.json      打分后的岗位（sync 产出）
    data/jd_cache.json  真实 JD 正文 + 官网发布日期 + 职位号（enrich_jd 产出）
    data/link_health.json 链接可用性（verify_links 产出）
    data/ai_cache.json  大模型摘要缓存（llm 层，可选）
        ↓
    build_report()  →  报告视图模型
        ↓
    render.render_markdown() / render_feishu() / render_wecom()

报告结构：
    1) 今日新收录   —— 本次首次抓到的岗位
    2) 近 14 天在招 —— 存量兜底（外企杭州设计岗本就稀疏，没这个多数天会空）
    3) 已排除       —— 被自动剔除的岗位 + 原因（不静默隐藏）

三条「真实可靠」约束（和 render.py 一致）：
    · 不展示内部匹配分
    · 发布时间只认真实来源（start_date / 官网 postedOn），都不写就不显示
    · 正文与标题冲突的岗位（标题含 UX、正文是写后端的）单独列出并剔除
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
from typing import Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys  # noqa: E402

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from job_radar import llm, render  # noqa: E402

DATA_DIR = os.path.join(ROOT, "data")
JOBS = os.path.join(DATA_DIR, "jobs.json")
HEALTH = os.path.join(DATA_DIR, "health_report.json")
JD_CACHE = os.path.join(DATA_DIR, "jd_cache.json")
LINK_HEALTH = os.path.join(DATA_DIR, "link_health.json")
OUT = os.path.join(DATA_DIR, "notify_preview.md")
STATE = os.path.join(DATA_DIR, "notify_state.json")
DEFAULT_WORKBENCH_URL = os.getenv("WORKBENCH_URL", "").strip()

# 低质标签：这些岗位不进推送
LOW = {"代招/委托", "猎头", "劳务派遣", "泛销售", "低相关管培", "地点风险", "非设计岗"}
# 链接连续失败多少次才剔除（偶发抖动不算）
LINK_FAIL_LIMIT = 3
# 存量兜底的回看窗口（天）
STANDBY_DAYS = 14


# --------------------------------------------------------------------------- #
# 状态读写
# --------------------------------------------------------------------------- #
def job_key(job: Dict) -> str:
    return str(job.get("dedup_key") or job.get("job_id") or job.get("official_url") or "")


def _load_json(path: str, default):
    if not path or not os.path.exists(path):
        return default
    try:
        return json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return default


def load_state(path: str) -> Dict:
    state = _load_json(path, None)
    if not isinstance(state, dict):
        state = {"version": 1, "pushed_keys": {}}
    if not isinstance(state.get("pushed_keys"), dict):
        state["pushed_keys"] = {}
    state.setdefault("version", 1)
    return state


def is_pushed(job: Dict, state: Dict) -> bool:
    key = job_key(job)
    return bool(key and key in state.get("pushed_keys", {}))


def mark_pushed(path: str, jobs: List[Dict]) -> int:
    state = load_state(path)
    pushed = state.setdefault("pushed_keys", {})
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    added = 0
    for job in jobs:
        key = job_key(job)
        if not key or key in pushed:
            continue
        pushed[key] = {
            "pushed_at": now,
            "first_seen": job.get("first_seen") or "",
            "company": job.get("company_name") or "",
            "title": job.get("title") or "",
            "source_id": job.get("source_id") or "",
        }
        added += 1
    state["last_marked_at"] = now
    state["last_marked_count"] = added
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return added


# --------------------------------------------------------------------------- #
# 外部数据加载
# --------------------------------------------------------------------------- #
def load_jd_cache() -> Dict[str, Dict]:
    return (_load_json(JD_CACHE, {}) or {}).get("entries") or {}


def load_link_health() -> Dict[str, Dict]:
    return (_load_json(LINK_HEALTH, {}) or {}).get("entries") or {}


def health_summary() -> Tuple[int, int, List[str]]:
    """返回 (正常信源数, 总信源数, 需关注的信源 id 列表)。"""
    report = _load_json(HEALTH, {}) or {}
    rows = report.get("sources") or []
    total = len(rows)
    ok = sum(1 for r in rows if r.get("status") == "active" and not r.get("last_error"))
    bad = [r.get("source_id", "?") for r in rows
           if r.get("status") != "active" or r.get("alert") or r.get("last_error")]
    return ok, total, bad


def days_since(iso: str) -> Optional[int]:
    if not iso or len(iso) < 10:
        return None
    try:
        d = dt.date.fromisoformat(iso[:10])
    except ValueError:
        return None
    return (dt.date.today() - d).days


def _freshness(score: int, jd: Dict, job: Dict) -> int:
    """排序权重：分数 + 新鲜度。新岗应该排在老岗前面。"""
    d = days_since(str((jd or {}).get("start_date") or ""))
    if d is None:
        d = days_since(str(job.get("first_seen") or "")[:10])
    if d is None:
        return score
    if d <= 7:
        return score + 6
    if d <= 30:
        return score + 3
    return score


def tags(job: Dict) -> set:
    return set(job.get("tags") or [])


def is_foreign(job: Dict) -> bool:
    if "外企" in tags(job):
        return True
    return (job.get("source_id") or "").startswith(("wd-", "gh-", "lever-", "ashby-"))


# --------------------------------------------------------------------------- #
# 主构建
# --------------------------------------------------------------------------- #
def build_report(limit: int = 8, standby_limit: int = 5, min_match: int = 55,
                 mode: str = "new", since: str = "", state_path: str = STATE,
                 ignore_state: bool = False, standby: bool = True,
                 workbench_url: str = DEFAULT_WORKBENCH_URL,
                 use_ai: bool = True) -> Dict:
    """选出要播报的岗位，组装成渲染层要的报告模型。"""
    jobs = _load_json(JOBS, [])
    if isinstance(jobs, dict):
        jobs = jobs.get("jobs") or []
    jd_cache = load_jd_cache()
    link_health = load_link_health()
    state = load_state(state_path) if state_path and not ignore_state else {"pushed_keys": {}}

    excluded: List[Dict] = []
    active: List[Dict] = []
    for j in jobs:
        if j.get("gone"):
            continue
        if LOW & tags(j):
            continue
        if int(j.get("match_score") or 0) < min_match:
            continue
        key = job_key(j)
        jd = jd_cache.get(key) or {}
        summary = jd.get("summary") or {}

        # 标题含设计、正文却是工程岗 → 剔除并说明原因（不静默丢弃）
        if summary.get("flag"):
            excluded.append({
                "label": f"{render.company_display(j)} {render.clean_title(j)}",
                "reason": summary["flag"],
            })
            continue

        # 链接连续 3 次打不开才剔除，避免偶发抖动误杀
        lh = link_health.get(key) or {}
        if int(lh.get("fails") or 0) >= LINK_FAIL_LIMIT:
            excluded.append({
                "label": f"{render.company_display(j)} {render.clean_title(j)}",
                "reason": f"岗位链接连续 {LINK_FAIL_LIMIT} 次无法打开，疑似已下线",
            })
            continue

        j["_jd"] = jd
        j["_sort"] = _freshness(int(j.get("match_score") or 0), jd, j)
        active.append(j)

    active.sort(key=lambda j: j["_sort"], reverse=True)

    # ---- 新增：first_seen 恰好是今天 ----
    if mode == "all":
        pool = active
    else:
        since_day = since or dt.date.today().isoformat()
        pool = [j for j in active if str(j.get("first_seen") or "")[:10] >= since_day]
        pool = [j for j in pool if not is_pushed(j, state)]

    pool = _pick(pool, limit)
    pool_keys = {job_key(j) for j in pool}

    # ---- 存量兜底：在架 + 近 14 天更新，不看推送状态 ----
    # 外企杭州设计岗实测长期只有 3-5 条，若存量也按"推过就不再提"处理，
    # 第 2 天起播报就永远是空的。
    standby_rows: List[Dict] = []
    if standby:
        standby_rows = [j for j in active
                        if (lambda d: d is not None and d <= STANDBY_DAYS)(
                            days_since(str(j.get("last_seen") or "")))
                        and job_key(j) not in pool_keys]
        standby_rows = _pick(standby_rows, standby_limit, skip=pool_keys)

    # ---- 视图模型 ----
    def to_views(rows: List[Dict]) -> List[Dict]:
        out = []
        for j in rows:
            key = job_key(j)
            jd = j.get("_jd") or {}
            ai = None
            if use_ai and jd:
                summary = jd.get("summary") or {}
                # 只有英文/长 JD 才值得花 token：中文短 JD 规则版已经够清楚
                worth = summary.get("lang") == "en" or len(jd.get("jd_text") or "") > 600
                if worth:
                    ai = llm.summarize(key, jd.get("jd_hash") or "",
                                       j.get("title") or "", jd.get("jd_text") or "")
            lh = link_health.get(key) or {}
            link_ok = lh.get("ok") if lh else None
            out.append(render.view(j, jd, ai, link_ok))
        return out

    new_views = to_views(pool)
    standby_views = to_views(standby_rows)

    # ---- 报告头 ----
    today = dt.date.today()
    now = dt.datetime.now()
    ok_src, total_src, bad_src = health_summary()
    foreign_total = sum(1 for j in active if is_foreign(j))
    hz_total = sum(1 for j in active
                   if "杭州" in (j.get("location") or "")
                   or "hangzhou" in (j.get("location") or "").lower())

    sections = []
    # 没有新增时不空占一节——开场直接说明"今日无新增，以下是近 14 天在招"更清楚
    if new_views or not standby_views:
        sections.append({
            "title": "今日新收录",
            "note": "本次首次抓到，非官网发布日期" if new_views else "",
            "items": new_views,
        })
    if standby:
        sections.append({
            "title": f"近 {STANDBY_DAYS} 天在招",
            "note": "官网仍在架，已挂时间较久的会单独标注",
            "items": standby_views,
        })

    subtitle_bits = []
    if not new_views and standby_views:
        subtitle_bits.append(f"今日无新增岗位，以下为近 {STANDBY_DAYS} 天在招")
    if excluded:
        subtitle_bits.append("已剔除正文与标题不符、链接失效的岗位，见文末「已排除」")

    footer_bits = ["数据来源：各公司官网 Workday 直连"]
    if total_src:
        footer_bits.append(f"{total_src} 个信源 {ok_src} 个正常")
    footer_bits.append(f"抓取于 {now.strftime('%m/%d %H:%M')}")
    footer_bits.append("由 job-radar 自动生成")
    footer = " · ".join(footer_bits)
    if workbench_url:
        footer += f"\n信息台：{workbench_url}"

    return {
        "title": f"杭州外企设计岗日报 · {today.strftime('%m/%d')}",
        "stats": (f"新收录 {len(new_views)} 条 · 近 {STANDBY_DAYS} 天在招 {len(standby_views)} 条 · "
                  f"在架达标 {len(active)}（外企 {foreign_total} / 杭州 {hz_total}）"),
        "subtitle": "　".join(subtitle_bits),
        "sections": sections,
        "excluded": excluded,
        "footer": footer,
        "has_new": bool(pool),
        "counts": {"new": len(new_views), "standby": len(standby_views),
                   "active": len(active), "excluded": len(excluded)},
        "selected": pool + standby_rows,
    }


def _pick(rows: List[Dict], limit: int, per_company: int = 2,
          skip: Optional[set] = None) -> List[Dict]:
    """按公司限额挑选，避免同一家公司占满整个分组。"""
    skip = skip or set()
    seen: Dict[str, int] = {}
    picked: List[Dict] = []
    for job in rows:
        key = job_key(job)
        if key and key in skip:
            continue
        c = (job.get("company_name") or "").lower()
        if seen.get(c, 0) >= per_company:
            continue
        picked.append(job)
        seen[c] = seen.get(c, 0) + 1
        if len(picked) >= limit:
            break
    return picked


def build(limit: int = 8, standby_limit: int = 5, min_match: int = 55,
          mode: str = "new", since: str = "", state_path: str = STATE,
          ignore_state: bool = False, standby: bool = True,
          workbench_url: str = DEFAULT_WORKBENCH_URL) -> Tuple[str, List[Dict], bool]:
    """兼容旧调用：返回 (markdown, 本次选中的岗位, 是否有新增)。"""
    report = build_report(limit, standby_limit, min_match, mode, since, state_path,
                          ignore_state, standby, workbench_url)
    return render.render_markdown(report), report["selected"], report["has_new"]


def main() -> None:
    p = argparse.ArgumentParser(description="生成设计岗日报推送预览。")
    p.add_argument("--out", default=OUT)
    p.add_argument("--limit", type=int, default=8, help="今日新收录最多几条")
    p.add_argument("--standby-limit", type=int, default=5, help="存量兜底最多几条")
    p.add_argument("--min-match", type=int, default=55, help="进入推送的最低匹配分")
    p.add_argument("--mode", choices=("new", "all"), default="new")
    p.add_argument("--since", default="", help="first_seen 起始日 YYYY-MM-DD")
    p.add_argument("--no-standby", action="store_true", help="不输出存量兜底模块")
    p.add_argument("--state", default=STATE)
    p.add_argument("--ignore-state", action="store_true")
    p.add_argument("--mark-pushed", action="store_true")
    p.add_argument("--workbench-url", default=DEFAULT_WORKBENCH_URL)
    p.add_argument("--no-ai", action="store_true", help="不调用大模型")
    args = p.parse_args()

    report = build_report(args.limit, args.standby_limit, args.min_match, args.mode,
                          args.since, args.state, args.ignore_state,
                          not args.no_standby, args.workbench_url,
                          use_ai=not args.no_ai)
    md = render.render_markdown(report)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"✅ 推送预览已生成：{args.out}")
    c = report.get("counts") or {}
    print(f"   新收录 {c.get('new', 0)} 条 · 近 {STANDBY_DAYS} 天在招 {c.get('standby', 0)} 条 · "
          f"已排除 {c.get('excluded', 0)} 条")
    if args.mark_pushed:
        state = load_state(args.state)
        added = mark_pushed(args.state, [j for j in report["selected"]
                                        if not is_pushed(j, state)])
        print(f"✅ 已标记 {added} 条为已推送：{args.state}")


if __name__ == "__main__":
    main()
