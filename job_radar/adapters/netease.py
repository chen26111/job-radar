"""网易招聘官网接口（hr.163.com，公开 JSON、无强反爬）。

接口（通过抓包发现）：
    POST https://hr.163.com/api/hr163/position/queryPage
    body(JSON): {"currentPage":1,"pageSize":20,"workType":"","firstType":"","keyword":""}
    返回 data.list[]：name 职位 / workPlaceList 地点 / firstPostTypeName 职类 /
        requirement+description JD / reqEducationName 学历 / recruitNum 招聘数 / id

网易以游戏为主（互娱/雷火），含数据/算法/后端/游戏AI 等对口岗，亦有实习生/校招。
endpoint 仅记录根 URL。
"""
from __future__ import annotations

from typing import List

from ..models import RawJob
from . import register
from .http import post_json

_API = "https://hr.163.com/api/hr163/position/queryPage"
PAGES = 4          # 通用 feed
FOCUS_PAGES = 3    # 每个设计/产品关键词翻几页
PAGE = 50          # 接口实测 pageSize 可以开到 50，减少请求数


def _ts_to_date(ts) -> str:
    """updateTime 是 epoch 毫秒（实测 1792738657000 这种），要先换算再取日期。"""
    import datetime as dt
    try:
        v = int(ts)
    except (TypeError, ValueError):
        return ""
    if v <= 0:
        return ""
    if v > 1e12:
        v //= 1000
    try:
        return dt.datetime.fromtimestamp(v).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        return ""


def _loc(wp) -> str:
    if not wp:
        return ""
    if isinstance(wp, list):
        names = []
        for w in wp:
            if isinstance(w, dict):
                names.append(w.get("name") or w.get("workPlaceName") or "")
            else:
                names.append(str(w))
        return "、".join(n for n in names if n)
    return str(wp)


@register("netease")
def fetch(endpoint: str) -> List[RawJob]:
    from ..keyword_config import design_focus_keywords

    jobs: List[RawJob] = []
    seen = set()
    # 通用 feed + 设计/产品关键词定向。只翻通用 feed 时杭州设计岗只能抓到个位数，
    # 加关键词后能到几十条 —— 默认排序把设计岗排在很后面，必须主动搜。
    plans = [("", PAGES)] + [(kw, FOCUS_PAGES) for kw in design_focus_keywords()]
    for kw, pages in plans:
        for p in range(1, pages + 1):
            body = {"currentPage": p, "pageSize": PAGE, "workType": "",
                    "firstType": "", "secondType": "", "keyword": kw}
            try:
                d = post_json(_API, body,
                              headers={"Referer": "https://hr.163.com/job-list.html"})
            except Exception:  # noqa: BLE001 — 翻页失败即停
                break
            lst = (d.get("data") or {}).get("list") or []
            if not lst:
                break
            for e in lst:
                jid = e.get("id")
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                jd = " ".join(filter(None, [e.get("requirement", ""), e.get("description", "")]))[:2000]
                jobs.append(RawJob(
                    company_name="网易",
                    title=e.get("name", ""),
                    location=_loc(e.get("workPlaceNameList") or e.get("workPlaceList")),
                    publish_time=_ts_to_date(e.get("updateTime")),
                official_url=f"https://hr.163.com/job-detail.html?positionId={jid}",
                jd_text=jd,
                raw={"platform": "netease", "degree": e.get("reqEducationName", ""),
                     "postType": e.get("firstPostTypeName", ""), "needs_ai": False}))
        if len(lst) < PAGE:
            break
    return jobs
