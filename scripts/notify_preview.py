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

from job_radar import llm, render, salary  # noqa: E402
from job_radar.role_rules import role_ring  # noqa: E402

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
# 同一家公司每次最多出几条（网易杭州岗很多，但一家占满整个播报也没意义）
PER_COMPANY = 5
# 辐射环（产品经理）每次最多几条
PM_LIMIT = 4
# 老岗阈值：挂超过这么久的岗位不再进播报（config/profiles.json 的 max_age_days 可改）
def _profile() -> dict:
    """读画像配置（口径和 sync 一致）。读不到就用默认 90 天。"""
    import json
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "config", "profiles.json")
    try:
        with open(path, encoding="utf-8") as f:
            return (json.load(f).get("ux-product-design") or {})
    except Exception:                                    # noqa: BLE001
        return {}


MAX_AGE_DAYS = int(_profile().get("max_age_days") or 90)


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
    """排序权重：匹配度 + 待遇 + 新鲜度（三者叠加，可解释）。

    待遇这一项**只在 JD 里写明了数字时才生效**，没写就是 0，不猜也不罚——
    外企 JD 普遍不写薪资，不能因为"没写"就把它排到后面。
    """
    d = days_since(str((jd or {}).get("start_date") or ""))
    if d is None:
        d = days_since(str(job.get("publish_time") or "")[:10])
    if d is None:
        d = days_since(str(job.get("first_seen") or "")[:10])
    if d is not None:
        if d <= 7:
            score += 6
        elif d <= 30:
            score += 3
        elif d >= 180:
            score -= 4          # 挂了半年以上，大概率是滚动招聘
    # JD 文本两个来源：Workday 详情接口（jd_cache）或同步时抓到的正文（jobs.json）
    jd_text = str((jd or {}).get("jd_text") or job.get("jd_text") or "")
    sal = salary.extract(jd_text)
    score += salary.rank_bonus(sal)
    if sal:
        job["_salary"] = sal    # 渲染层要展示
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

        # 老岗治理：挂太久的岗位（默认 >90 天）不再进播报，
        # 但会写进「已排除」说明原因——不静默消失，也不占版面。
        age = days_since(str(jd.get("start_date") or ""))
        if age is None:
            age = days_since(str(j.get("publish_time") or "")[:10])
        if age is None:
            age = days_since(str(j.get("first_seen") or "")[:10])
        if age is not None and age > MAX_AGE_DAYS:
            excluded.append({
                "label": f"{render.company_display(j)} {render.clean_title(j)}",
                "reason": f"已挂 {age} 天（超过 {MAX_AGE_DAYS} 天阈值），多半已招满",
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

    # ---- 存量兜底：在架 + 近 14 天更新，不看推送状态 ----
    # 外企杭州设计岗实测长期只有 3-5 条，若存量也按"推过就不再提"处理，
    # 第 2 天起播报就永远是空的。
    standby_rows: List[Dict] = []
    if standby:
        standby_rows = [j for j in active
                        if (lambda d: d is not None and d <= STANDBY_DAYS)(
                            days_since(str(j.get("last_seen") or "")))
                        and job_key(j) not in {job_key(x) for x in pool}]

    # ---- 按环分别挑选 ----
    # 关键：先分环再按公司限额。否则网易的设计岗会把同公司的产品经理名额挤光，
    # 结果辐射环永远出不来。
    def ring_of(j: Dict) -> str:
        return role_ring(j.get("title") or "", j.get("jd_text") or "") or ""

    combined = pool + standby_rows
    design_rows = _pick([j for j in combined if ring_of(j) == "design"], limit)
    pm_rows = _pick([j for j in combined if ring_of(j) == "pm"], PM_LIMIT)
    picked_keys = {job_key(j) for j in design_rows + pm_rows}
    pool = [j for j in pool if job_key(j) in picked_keys]

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

    # ---- 分节：设计岗是核心，产品经理是向外辐射的一环 ----
    # 两节分开排，避免"产品经理"混在设计岗里让人以为是设计岗。
    all_views = new_views + standby_views
    design_views = [v for v in all_views if v.get("ring") == "design"]
    pm_views = [v for v in all_views if v.get("ring") == "pm"]

    sections = []
    if design_views or not pm_views:
        sections.append({
            "title": f"设计岗 · 杭州（{len(design_views)}）",
            "note": "交互/UI/体验/视觉方向，本次主推" if design_views else "今日暂无达标设计岗",
            "items": design_views,
        })
    if pm_views:
        sections.append({
            "title": f"产品经理 · 杭州（{len(pm_views)}）",
            "note": "向外辐射的一环：与设计强相关，非设计岗",
            "items": pm_views,
        })

    subtitle_bits = []
    if not new_views and standby_views:
        subtitle_bits.append(f"今日无新增岗位，以下为近 {STANDBY_DAYS} 天在招")
    if excluded:
        subtitle_bits.append("已剔除正文与标题不符、链接失效的岗位，见文末「已排除」")

    footer_bits = ["数据来源：各公司官网招聘接口直连（Workday / 网易 / 字节 / 腾讯 / 美团）"]
    if total_src:
        footer_bits.append(f"{total_src} 个信源 {ok_src} 个正常")
    footer_bits.append(f"抓取于 {now.strftime('%m/%d %H:%M')}")
    footer_bits.append("由 job-radar 自动生成")
    footer = " · ".join(footer_bits)
    if workbench_url:
        footer += f"\n信息台：{workbench_url}"

    return {
        # 信源已经不止外企（网易/字节等国内头部也在列），标题不能再写"外企"
        "title": f"杭州设计岗日报 · {today.strftime('%m/%d')}",
        "stats": (f"设计岗 {len(design_views)} 条 · 产品经理 {len(pm_views)} 条 · "
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


def _pick(rows: List[Dict], limit: int, per_company: int = PER_COMPANY,
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
