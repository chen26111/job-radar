#!/usr/bin/env python3
"""按关键词搜"杭州 + 设计/UX"岗位，不依赖公司名单 —— 先看看市面上到底有什么。

用途：在配信源之前，先确认"杭州外企设计岗"到底有多少在招，避免白跑。
用 Workday 的 searchText 做模糊搜索（它匹配职位名+地点+公司），再用 locationsText
二次校验城市，最后按设计关键词筛标题。

用法：
  python3 scripts/search_hangzhou.py                      # 默认搜设计/UX
  python3 scripts/search_hangzhou.py --city 杭州 --top 40
  python3 scripts/search_hangzhou.py --keywords "ux,product designer,交互"
"""
from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from job_radar.adapters.http import post_json  # noqa: E402
from probe import matches  # noqa: E402

# 已确认可用的外企 Workday 租户（host, tenant, site, 中文名）
TENANTS = [
    ("pfizer.wd1.myworkdayjobs.com", "pfizer", "PfizerCareers", "辉瑞 Pfizer"),
    ("statestreet.wd1.myworkdayjobs.com", "statestreet", "Global", "道富 State Street"),
    ("3m.wd1.myworkdayjobs.com", "3m", "Search", "3M"),
    ("intel.wd1.myworkdayjobs.com", "intel", "External", "英特尔 Intel"),
    ("nxp.wd3.myworkdayjobs.com", "nxp", "careers", "恩智浦 NXP"),
    ("abbott.wd5.myworkdayjobs.com", "abbott", "abbottcareers", "雅培 Abbott"),
    ("amat.wd1.myworkdayjobs.com", "amat", "External", "应用材料 AMAT"),
    ("kla.wd1.myworkdayjobs.com", "kla", "Search", "科磊 KLA"),
    ("astrazeneca.wd3.myworkdayjobs.com", "astrazeneca", "Careers", "阿斯利康 AZ"),
    ("nvidia.wd5.myworkdayjobs.com", "nvidia", "NVIDIAExternalCareerSite", "英伟达 NVIDIA"),
    # 候选（未知 site 名，失败自动跳过）
    ("siemens.wd3.myworkdayjobs.com", "siemens", "Siemens_Careers", "西门子 Siemens"),
    ("bosch.wd3.myworkdayjobs.com", "bosch", "Bosch_External", "博世 Bosch"),
    ("schneider.wd3.myworkdayjobs.com", "schneider", "SchneiderElectric", "施耐德 Schneider"),
    ("philips.wd3.myworkdayjobs.com", "philips", "Philips", "飞利浦 Philips"),
    ("roche.wd3.myworkdayjobs.com", "roche", "Roche", "罗氏 Roche"),
    ("jnj.wd5.myworkdayjobs.com", "jnj", "JJ", "强生 J&J"),
    ("ringcentral.wd1.myworkdayjobs.com", "ringcentral", "RingCentral", "RingCentral 铃盛"),
    ("zoom.wd5.myworkdayjobs.com", "zoom", "Zoom", "Zoom"),
    ("danaher.wd1.myworkdayjobs.com", "danaher", "DanaherJobs", "丹纳赫 Danaher"),
    ("medtronic.wd1.myworkdayjobs.com", "medtronic", "MedtronicCareers", "美敦力 Medtronic"),
]

DESIGN_KW = ("ux", "ui", "designer", "design", "interaction", "user experience",
             "product design", "visual", "creative", "research", "设计", "交互", "体验")


def search(host: str, tenant: str, site: str, term: str, limit: int = 20) -> dict:
    url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    body = {"appliedFacets": {}, "limit": limit, "offset": 0, "searchText": term}
    return post_json(url, body, headers={"Referer": f"https://{host}/"}, timeout=25)


def main() -> None:
    p = argparse.ArgumentParser(description="按关键词搜索杭州设计岗")
    p.add_argument("--city", default="杭州")
    p.add_argument("--terms", default="designer,UX,design,Hangzhou",
                   help="送进 searchText 的查询词，逗号分隔")
    p.add_argument("--keywords", default=",".join(DESIGN_KW),
                   help="标题必须命中的设计关键词，逗号分隔")
    p.add_argument("--top", type=int, default=25)
    args = p.parse_args()

    terms = [t.strip() for t in args.terms.split(",") if t.strip()]
    keywords = tuple(k.strip() for k in args.keywords.split(",") if k.strip())
    aliases = ("hangzhou", "杭州")

    found: dict[str, dict] = {}
    for host, tenant, site, cn in TENANTS:
        for term in terms:
            try:
                d = search(host, tenant, site, term)
            except Exception as e:  # noqa: BLE001
                print(f"  ✗ {cn} · {term}: {type(e).__name__}")
                continue
            posts = d.get("jobPostings") or []
            for j in posts:
                loc = (j.get("locationsText") or "").lower()
                if not any(a in loc for a in aliases):
                    continue
                title = j.get("title") or ""
                tl = title.lower()
                if not any(k.lower() in tl for k in keywords):
                    continue
                path = j.get("externalPath", "")
                url = f"https://{host}/en-US/{site}{path}" if path else ""
                found[url or f"{cn}:{title}"] = {
                    "company": cn, "title": title,
                    "location": j.get("locationsText", ""),
                    "posted": j.get("postedOn", ""), "url": url,
                }
    rows = list(found.values())
    rows.sort(key=lambda r: r["posted"])
    print(f"\n{'='*70}\n杭州设计/UX 岗命中 {len(rows)} 条（跨 {len(TENANTS)} 家外企租户）\n{'='*70}")
    for r in rows[:args.top]:
        print(f"- {r['company']}｜{r['title']}\n  {r['location']}｜{r['posted']}\n  {r['url']}")


if __name__ == "__main__":
    main()
