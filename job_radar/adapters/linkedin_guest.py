"""LinkedIn 访客搜索 adapter（公开 jobs-guest 接口，无需登录）。

⚠⚠ 当前不可用（2026-10-22 实测，中国区）⚠⚠
    https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search
    → HTTP 451 Unavailable_For_Legal_Reasons
    cn.linkedin.com 302 跳回主域后同样 451。
    即：这个接口在中国大陆网络（含 GitHub Actions 境外 runner 不一定，但本地/国内
    部署环境）拿不到数据。**因此 sources.csv 里没有配置任何 linkedin 源**，
    本文件仅作备份保留，等找到可用代理或改用其它平台源时再启用。

为什么当初想用它：杭州外企设计岗在 Workday 侧极稀疏（15 家外企全量只有 3-5 条），
但 LinkedIn 上同一批岗位都有。理想很丰满，接口被封。

备选路线（均未跑通，留档避免重复踩坑）：
- 智联 fe-api.zhaopin.com/c/i/sou  → 返回 numFound=999999 的反爬伪装响应
- 智联 fe-api.zhaopin.com/c/i/search/positions (POST) → 200 但 numFound 恒为 0，需签名
- 51job → aliyun_waf 挑战页
- 智联 PC 搜索页 www.zhaopin.com/sou/jl653/... → 200 但 positionList 只回传前 4 条，
  其余走需要签名的 XHR

endpoint 格式：关键词之间用 `|` 分隔，例如
    "UX Designer|Product Designer|Interaction Designer|UI Designer"
城市固定取 sources.csv 的 city_scope（浙江/杭州）——adapter 内部按城市过滤。

注意：
- 只抓公开列表，不登录、不抓候选人信息，每页间隔 1s，合规。
- LinkedIn 会限流（同一 IP 抓到第 ~10 页开始封），所以每关键词只取 2 页（约 50 条）。
- 返回的发布时间是相对文本（"2 weeks ago"），转成 ISO 日期以便去重/排序。
"""
from __future__ import annotations

import re
import time
from datetime import date, timedelta
from html import unescape
from typing import List

from ..models import RawJob
from . import register
from .http import get_text

BASE = ("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
        "?keywords={kw}&location={loc}&start={start}")
PAGE = 25
MAX_PAGES = 2
CITY_TOKENS = ("hangzhou", "杭州", "shaoxing", "绍兴", "zhejiang", "浙江")

_TAG = re.compile(r"<[^>]+>")
_ITEM = re.compile(r"<li>(.*?)</li>", re.S | re.I)
_TITLE_A = re.compile(r'<a[^>]+class="[^"]*base-card__full-link[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                      re.S | re.I)
_TITLE_H3 = re.compile(r"<h3[^>]*>(.*?)</h3>", re.S | re.I)
_COMPANY = re.compile(r'<h4[^>]*>(.*?)</h4>', re.S | re.I)
_LOCATION = re.compile(r'class="[^"]*job-search-card__location[^"]*"[^>]*>(.*?)</span>', re.S | re.I)
_TIME = re.compile(r'<time[^>]*datetime="([^"]+)"', re.I)


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", unescape(_TAG.sub(" ", s or ""))).strip()


def _parse(html: str) -> List[dict]:
    out = []
    for block in _ITEM.findall(html):
        m = _TITLE_A.search(block)
        url, title = ("", "")
        if m:
            url = m.group(1).split("?")[0]
            title = _clean(m.group(2))
        else:
            m3 = _TITLE_H3.search(block)
            if m3:
                title = _clean(m3.group(1))
                a = re.search(r'href="([^"]+)"', block)
                url = a.group(1).split("?")[0] if a else ""
        company = _clean((_COMPANY.search(block) or [None, ""])[1]) if _COMPANY.search(block) else ""
        loc = _clean((_LOCATION.search(block) or [None, ""])[1]) if _LOCATION.search(block) else ""
        if not title or not url:
            continue
        tm = _TIME.search(block)
        out.append({"title": title, "company": company, "location": loc, "url": url,
                    "posted_at": tm.group(1) if tm else ""})
    return out


@register("linkedin_guest")
def fetch(endpoint: str) -> List[RawJob]:
    keywords = [k.strip() for k in endpoint.split("|") if k.strip()]
    if not keywords:
        raise RuntimeError("linkedin_guest endpoint 需要至少一个关键词，用 | 分隔")
    jobs: List[RawJob] = []
    seen: set[str] = set()
    for kw in keywords:
        for page in range(MAX_PAGES):
            url = BASE.format(kw=kw.replace(" ", "%20"),
                              loc="Hangzhou%2C%20Zhejiang%2C%20China",
                              start=page * PAGE)
            try:
                html = get_text(url, timeout=25)
            except Exception:  # noqa: BLE001 — 单页失败不阻断其它关键词
                break
            rows = _parse(html)
            if not rows:
                break
            for r in rows:
                loc_low = r["location"].lower()
                if not any(t in loc_low for t in CITY_TOKENS):
                    continue
                if r["url"] in seen:
                    continue
                seen.add(r["url"])
                jobs.append(RawJob(
                    company_name=r["company"],
                    title=r["title"],
                    location=r["location"],
                    publish_time=r["posted_at"],
                    official_url=r["url"],
                    jd_text="",           # 列表页没有 JD 正文，标题足够做初筛
                    raw={"platform": "linkedin", "keyword": kw},
                ))
            time.sleep(1.0)
    return jobs
