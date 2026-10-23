#!/usr/bin/env python3
"""probe.py — 探测某家公司用的招聘系统（ATS），并统计"目标城市 + 目标岗位"命中数。

为什么要它：Workday 每家公司 tenant/site 都不一样，靠猜必错。这个脚本输入一个
careers 页面 URL（或直接给 host|tenant|site），自动判定 ATS 类型、试抓、报命中数，
最后打印一行可直接粘进 config/sources.csv 的记录。

用法：
  # 1) 给官网 careers 页面 URL，自动识别
  python3 scripts/probe.py https://careers.pfizer.com/
  python3 scripts/probe.py https://www.state street.com/careers

  # 2) 直接给 workday endpoint（已知时最省事）
  python3 scripts/probe.py "pfizer.wd1.myworkdayjobs.com|pfizer|PfizerCareers"

  # 3) 只统计命中，不改文件
  python3 scripts/probe.py <target> --city 杭州 --keywords "ux,ui,interaction,design,product"

  # 4) 批量：从 txt 里一行一个 URL 全扫，只打印有效的
  python3 scripts/probe.py --batch candidates.txt

输出：
  命中数 > 0 时打印 sources.csv 行；命中 0 时明确告诉你"接口通但没货"，别白配。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from job_radar.adapters.http import get_json, get_text, post_json  # noqa: E402

# 目标岗位关键词（标题匹配，用于统计命中；与实际 sources.csv 无关）
DEFAULT_KEYWORDS = ("ux", "ui", "user experience", "interaction", "product design",
                    "designer", "design lead", "ux research", "视觉", "交互", "体验",
                    "产品设计", "设计")
# 目标城市（location 匹配，中英都要）
CITY_ALIASES = {
    "杭州": ("hangzhou", "杭州"),
    "上海": ("shanghai", "上海"),
    "北京": ("beijing", "北京"),
    "深圳": ("shenzhen", "深圳"),
    "苏州": ("suzhou", "苏州"),
    "广州": ("guangzhou", "广州"),
}
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


# --------------------------------------------------------------------------- #
# ATS 探测
# --------------------------------------------------------------------------- #
def detect_ats(url: str) -> dict:
    """根据 URL 形态判断 ATS 类型，返回 {adapter, endpoint, host?}。"""
    u = url.strip()
    m = re.search(r"https?://([\w.-]*?)(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/?#]+)",
                  u)
    if m:
        host = f"{m.group(1)}{m.group(2)}.myworkdayjobs.com"
        site = m.group(3)
        # tenant 通常等于 host 的第一段，如 pfizer.wd1 → pfizer；也可能带连字符
        tenant = host.split(".wd")[0]
        return {"adapter": "workday", "host": host, "tenant": tenant, "site": site,
                "endpoint": f"{host}|{tenant}|{site}"}
    if "greenhouse.io" in u:
        parts = [p for p in urllib.parse.urlparse(u).path.split("/") if p]
        token = parts[0] if parts else ""
        return {"adapter": "greenhouse", "endpoint": token}
    if "lever.co" in u:
        parts = [p for p in urllib.parse.urlparse(u).path.split("/") if p]
        return {"adapter": "lever", "endpoint": parts[0] if parts else ""}
    if "ashbyhq.com" in u:
        parts = [p for p in urllib.parse.urlparse(u).path.split("/") if p]
        return {"adapter": "ashby", "endpoint": parts[0] if parts else ""}
    if "smartrecruiters.com" in u:
        parts = [p for p in urllib.parse.urlparse(u).path.split("/") if p]
        return {"adapter": "smartrecruiters", "endpoint": parts[0] if parts else ""}
    # 未识别的官网：尝试从页面里找 ATS 线索
    return {"adapter": "", "endpoint": ""}


def sniff_from_page(url: str) -> dict:
    """抓官网 careers 页面 HTML，从中找 Workday / Greenhouse / Lever 的租户信息。"""
    try:
        html = get_text(url)
    except Exception as e:  # noqa: BLE001
        return {"error": f"抓页面失败: {type(e).__name__}: {e}"}
    pats = [
        (r"([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([\w-]+)", "workday"),
        (r"boards\.greenhouse\.io/([\w-]+)", "greenhouse"),
        (r"jobs\.lever\.co/([\w-]+)", "lever"),
        (r"jobs\.ashbyhq\.com/([\w-]+)", "ashby"),
    ]
    for pat, kind in pats:
        m = re.search(pat, html)
        if not m:
            continue
        if kind == "workday":
            host = f"{m.group(1)}.{m.group(2)}.myworkdayjobs.com"
            return {"adapter": "workday", "host": host, "tenant": m.group(1),
                    "site": m.group(3), "endpoint": f"{host}|{m.group(1)}|{m.group(3)}"}
        return {"adapter": kind, "endpoint": m.group(1)}
    return {"error": "页面里没找到已知 ATS 线索（可能是自建站/北森/飞书招聘）"}


# --------------------------------------------------------------------------- #
# 抓取 + 命中统计
# --------------------------------------------------------------------------- #
def fetch_workday(host: str, tenant: str, site: str, max_pages: int = 10) -> list[dict]:
    """抓 Workday 全部在架岗位（不做城市/国家过滤，让调用方统计）。

    注意：不带 Referer 时部分租户会返回空 total / 提前结束，必须带上。
    """
    url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    out: list[dict] = []
    offset = 0
    page = 20
    total = None
    for _ in range(max_pages):
        body = {"appliedFacets": {}, "limit": page, "offset": offset, "searchText": ""}
        d = post_json(url, body, headers={"Referer": f"https://{host}/"}, timeout=25)
        posts = d.get("jobPostings") or []
        if total is None:
            total = int(d.get("total") or 0)
        if not posts:
            break
        for p in posts:
            path = p.get("externalPath", "")
            out.append({
                "title": p.get("title", ""),
                "location": p.get("locationsText", ""),
                "posted": p.get("postedOn", ""),
                "url": f"https://{host}/en-US/{site}{path}" if path else "",
            })
        offset += len(posts)
        if total and offset >= total:
            break
    return out


def fetch_greenhouse(token: str) -> list[dict]:
    d = get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=false")
    return [{"title": j.get("title", ""), "location": (j.get("location") or {}).get("name", ""),
             "posted": j.get("updated_at", ""), "url": j.get("absolute_url", "")}
            for j in d.get("jobs", [])]


def fetch_lever(slug: str) -> list[dict]:
    d = get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    return [{"title": j.get("text", ""), "location": (j.get("categories") or {}).get("location", ""),
             "posted": "", "url": j.get("hostedUrl", "")} for j in d]


def fetch_ashby(token: str) -> list[dict]:
    d = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{token}")
    return [{"title": j.get("title", ""), "location": j.get("location", ""),
             "posted": j.get("publishedAt", ""), "url": j.get("jobUrl", "")}
            for j in d.get("jobs", [])]


def run_fetch(info: dict, max_pages: int = 10) -> list[dict]:
    a = info.get("adapter")
    if a == "workday":
        return fetch_workday(info["host"], info["tenant"], info["site"], max_pages)
    if a == "greenhouse":
        return fetch_greenhouse(info["endpoint"])
    if a == "lever":
        return fetch_lever(info["endpoint"])
    if a == "ashby":
        return fetch_ashby(info["endpoint"])
    return []


def matches(jobs: list[dict], city: str, keywords: tuple[str, ...]) -> list[dict]:
    """城市 + 关键词双过滤。

    城市用**整词**匹配（避免 "changzhou" 里的 "hangzhou" 误命中）；
    关键词用子串匹配，但要求命中处是词边界起始（避免 "build" 里的 "ui"）。
    """
    aliases = CITY_ALIASES.get(city, (city.lower(),))
    hits = []
    for j in jobs:
        loc = (j.get("location") or "").lower()
        title = (j.get("title") or "").lower()
        if not any(re.search(rf"(?<![a-z]){re.escape(a.lower())}", loc) for a in aliases):
            continue
        kw_hit = any(
            (re.search(rf"(?<![a-z]){re.escape(k.lower())}", title)
             if re.match(r"^[a-z ]+$", k)
             else k.lower() in title)
            for k in keywords
        )
        if not kw_hit:
            continue
        hits.append(j)
    return hits


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def parse_endpoint(raw: str) -> dict:
    """支持手写的 host|tenant|site（workday），或 greenhouse/lever/ashby:token。"""
    s = raw.strip()
    if "|" in s:
        parts = s.split("|")
        if len(parts) == 3 and "." in parts[0]:
            host, tenant, site = parts
            return {"adapter": "workday", "host": host, "tenant": tenant,
                    "site": site, "endpoint": s}
        raise ValueError("endpoint 应为 host|tenant|site（三段）")
    m = re.match(r"^(greenhouse|lever|ashby)\s*[:＝]\s*([\w-]+)$", s, re.I)
    if m:
        return {"adapter": m.group(1).lower(), "endpoint": m.group(2)}
    return {}


def probe_one(target: str, city: str, keywords: tuple[str, ...], max_pages: int) -> dict:
    target = target.strip()
    try:
        info = parse_endpoint(target)
    except ValueError as e:
        return {"target": target, "ok": False, "reason": str(e)}
    if not info:
        info = detect_ats(target)
    if not info.get("adapter"):
        info = sniff_from_page(target)
    if info.get("error") or not info.get("adapter"):
        return {"target": target, "ok": False, "reason": info.get("error") or "无法识别 ATS"}
    try:
        jobs = run_fetch(info, max_pages)
    except Exception as e:  # noqa: BLE001
        return {"target": target, "ok": False, "reason": f"抓取失败: {type(e).__name__}: {e}",
                "info": info}
    hits = matches(jobs, city, keywords)
    city_alias = CITY_ALIASES.get(city, (city.lower(),))
    return {"target": target, "ok": True, "info": info, "total": len(jobs),
            "hits": hits, "city_total": sum(
                1 for j in jobs
                if any(re.search(rf"(?<![a-z]){re.escape(a.lower())}",
                                 (j.get("location") or "").lower())
                       for a in city_alias))}


def csv_row(info: dict, company: str, city: str) -> str:
    """生成一行 sources.csv（company 名留空由人工填，避免拼音瞎猜）。"""
    a = info["adapter"]
    sid = f"wd-{info['tenant']}" if a == "workday" else f"{a[:2]}-{info['endpoint']}"
    return (f"{sid},{company},{a},{a},{info['endpoint']},1,api,no,{city},720,active,"
            f"probe.py 自动探测·待补公司名与备注")


def main() -> None:
    p = argparse.ArgumentParser(description="探测公司招聘系统并统计目标岗位命中数。")
    p.add_argument("target", nargs="?", help="careers URL 或 workday endpoint(host|tenant|site)")
    p.add_argument("--batch", help="txt 文件，一行一个 URL/endpoint")
    p.add_argument("--city", default="杭州")
    p.add_argument("--keywords", default=",".join(DEFAULT_KEYWORDS),
                   help="标题关键词，逗号分隔")
    p.add_argument("--max-pages", type=int, default=10, help="Workday 最多翻几页(每页20)")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    args = p.parse_args()

    keywords = tuple(k.strip() for k in args.keywords.split(",") if k.strip())
    targets: list[str] = []
    if args.batch:
        with open(args.batch, encoding="utf-8") as f:
            targets = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    elif args.target:
        targets = [args.target]
    else:
        p.error("需要 target 或 --batch")

    results = []
    for t in targets:
        r = probe_one(t, args.city, keywords, args.max_pages)
        results.append(r)
        if args.json:
            continue
        if not r["ok"]:
            print(f"✗ {t}\n    {r['reason']}")
            continue
        info = r["info"]
        print(f"✓ {t}")
        print(f"    adapter={info['adapter']}  endpoint={info['endpoint']}")
        print(f"    在架 {r['total']} 条，其中 {args.city} {r['city_total']} 条，"
              f"命中设计/产品关键词 {len(r['hits'])} 条")
        for h in r["hits"][:8]:
            print(f"      · {h['title']} | {h['location']} | {h['posted']}")
        if r["hits"]:
            print(f"    CSV 行 → {csv_row(info, '<公司名>', args.city)}")
        elif r["city_total"] == 0:
            print("    ⚠ 接口通但该城市 0 岗位 → 不建议配")
        else:
            print("    ⚠ 该城市有岗位但没有设计/产品岗 → 低优先级，可配可略")
        print()

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))

    ok = [r for r in results if r["ok"] and r["hits"]]
    print(f"—— 汇总：{len(results)} 个目标，{len(ok)} 个有目标岗位命中 ——")
    if ok:
        print("\n可粘贴进 config/sources.csv 的行：")
        for r in ok:
            print(csv_row(r["info"], "<公司名>", args.city))


if __name__ == "__main__":
    main()
