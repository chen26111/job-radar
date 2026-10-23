"""Workday ATS 通用 adapter（在华外企，公开 cxs JSON 接口）。

大量跨国公司(英伟达/3M/戴尔/施耐德等)用 Workday 托管招聘，接口公开、无强反爬：
    POST https://<host>/wday/cxs/<tenant>/<site>/jobs
本 adapter 面向"在华外企"——只取在华/杭州岗位。

endpoint 用竖线分隔：host|tenant|site
    例：nvidia.wd5.myworkdayjobs.com|nvidia|NVIDIAExternalCareerSite

踩过的坑（都已在代码里处理）：
1. **必须带 Referer 头**，否则请求会挂起直到超时。
2. **facet 不一定有 China**：Zoom / RingCentral 的 facets 里根本没有 Country 分组。
   此时若只靠 facet 过滤，就必须把全部岗位翻完才能捞出中国岗——RingCentral 的杭州岗
   在 80 条里的第 13 页之后，盲目翻页很容易漏。所以本 adapter 走**双路**：
     A. facet 有 China → 直接按国家筛（快、准）
     B. facet 没有 China → 用 searchText 定向搜 "China"/"Hangzhou" 等关键词
   两路结果按 URL 去重合并。
3. **locationsText 不可靠**：多地点岗显示 "2 Locations" 或 "2 个地点"（注意会随
   Accept-Language 变中文），真实地点在详情接口的 `location` + `additionalLocations` 里。
   例：RingCentral 的 Senior Product Designer 显示 "2 个地点"，
   实际是 Xiamen + Hangzhou。只认 locationsText 会漏掉杭州岗，必须回查详情。
4. 翻页用 `offset += len(batch)`，不能用 `offset = page * PAGE`。
"""
from __future__ import annotations

import re
from typing import List

from ..models import RawJob
from . import register
from .http import get_json, post_json

PAGE = 20
# 单源最多翻多少页（20 条/页）。大租户(Danaher 1400+、Medtronic 1000+)需要翻很深，
# 但也不能无限翻：单次 action 有总时长预算。
MAX_PAGES = 60
# 多地点岗回查详情的最多次数（控制请求量）
MAX_DETAIL = 30

# facet 缺失时用这些关键词定向搜（searchText 是模糊匹配，命中地点/标题）
SEARCH_TERMS = ("China", "Hangzhou", "Shanghai", "Beijing", "Shenzhen", "Suzhou")

_CN = re.compile(r"(China|中国|Shanghai|Beijing|Shenzhen|Suzhou|Guangzhou|Chengdu|Hangzhou|"
                 r"Wuhan|Xi'an|Tianjin|Nanjing|Xiamen|Qingdao|Dalian|Wuxi|Hefei|Changsha|"
                 r"Zhengzhou|Jinan|Shenyang|Fuzhou|Dongguan|Zhuhai|Ningbo|Kunshan|Tonglu|"
                 r"上海|北京|深圳|苏州|广州|成都|杭州|武汉|天津|南京|厦门|青岛|大连|无锡|"
                 r"合肥|长沙|郑州|济南|沈阳|福州|东莞|珠海|宁波|昆山|桐庐|"
                 r"Hong Kong|香港|Taiwan|台湾)", re.I)
# "2 Locations" / "3 个地点" / "2 Locations in China" 这类聚合文本
_MULTI = re.compile(r"(\d+\s*个?地点|\d+\s+Locations?)", re.I)


def _post(host, tenant, site, body):
    url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    return post_json(url, body, headers={"Referer": f"https://{host}/"})


# 详情接口的 jobPostingInfo 实测字段（辉瑞/道富/3M/RingCentral 均一致）：
#   title, location, country{descriptor,id}, additionalLocations,
#   jobDescription(HTML), startDate(ISO 真实发布日), endDate, postedOn(相对文本),
#   timeType, jobReqId, canApply, externalUrl, remoteType
# 注意是 jobReqId，不是 jobRequisitionId。
def _get_detail(host, tenant, site, external_path):
    """取岗位详情，返回扁平字段 dict。失败返回 {}。

    列表接口的 `locationsText` 只给 "2 Locations"，真实地点、JD 正文、
    发布日期都只有详情接口才有——所以这里一次把要用的字段全取回来，
    避免调用方再发一次请求。
    """
    if not external_path:
        return {}
    p = external_path.lstrip("/")
    if p.startswith("job/"):
        p = p[4:]
    url = f"https://{host}/wday/cxs/{tenant}/{site}/job/{p}"
    try:
        d = get_json(url, headers={"Referer": f"https://{host}/"})
    except Exception:  # noqa: BLE001
        return {}
    info = d.get("jobPostingInfo") or {}
    if not info:
        return {}
    return {
        "title": info.get("title") or "",
        "location": info.get("location") or "",
        "additional_locations": list(info.get("additionalLocations") or []),
        "jd_html": info.get("jobDescription") or "",
        "start_date": (info.get("startDate") or "")[:10],
        "end_date": (info.get("endDate") or "")[:10],
        "time_type": info.get("timeType") or "",
        "req_id": info.get("jobReqId") or "",
        "can_apply": info.get("canApply"),
        "posted_on": info.get("postedOn") or "",
        "external_url": info.get("externalUrl") or "",
    }


def _find_china_facet(data):
    """从 facets 里找"中国/China"的 (facetParameter, id)。没有返回 (None, None)。"""
    for f in data.get("facets", []):
        param = f.get("facetParameter", "")
        for v in f.get("values", []):
            if re.search(r"^\s*(China|中国)\s*$", str(v.get("descriptor", "")), re.I):
                return param, v.get("id")
    return None, None


def _harvest(host, tenant, site, jobs, seen, applied, search_text, detail_state):
    """抓一轮列表（带分页），把命中中国的岗位追加到 jobs。"""
    offset = 0
    total = None
    for _ in range(MAX_PAGES):
        body = {"appliedFacets": applied, "limit": PAGE, "offset": offset,
                "searchText": search_text}
        try:
            d = _post(host, tenant, site, body)
        except Exception:  # noqa: BLE001
            break
        if total is None:
            total = d.get("total", 0)
        posts = d.get("jobPostings", [])
        if not posts:
            break
        offset += len(posts)

        for p in posts:
            loc = (p.get("locationsText", "") or "").strip()
            path = p.get("externalPath", "")

            # 多地点聚合文本 → 回查详情拿真实地点
            if _MULTI.search(loc) and detail_state[0] < MAX_DETAIL:
                detail_state[0] += 1
                det = _get_detail(host, tenant, site, path)
                parts = [x for x in ([det.get("location")] + det.get("additional_locations", [])) if x]
                if parts:
                    loc = " / ".join(parts)

            if not _CN.search(loc):
                continue

            url = f"https://{host}/en-US/{site}{path}" if path else f"https://{host}/"
            if url in seen:
                continue
            seen.add(url)
            jobs.append(RawJob(
                company_name="",   # 由 config/sources.csv company_name 兜底
                title=p.get("title", ""),
                location=loc,
                official_url=url,
                jd_text=p.get("title", ""),
                raw={"platform": "workday", "needs_ai": True,
                     "posted_on": p.get("postedOn", "")},
            ))

        if total and offset >= total:
            break


@register("workday")
def fetch(endpoint: str) -> List[RawJob]:
    try:
        host, tenant, site = endpoint.split("|", 2)
    except ValueError:
        raise RuntimeError("workday endpoint 需为 host|tenant|site 格式")

    # 1) 探针：读 total 与 facets
    try:
        probe = _post(host, tenant, site, {"appliedFacets": {}, "limit": 1,
                                           "offset": 0, "searchText": ""})
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"workday 探针失败 {host}/{tenant}/{site}: {e}") from e

    total = probe.get("total", 0) or 0
    param, cid = _find_china_facet(probe)

    jobs: List[RawJob] = []
    seen: set = set()
    detail_state = [0]   # 已回查详情次数（用 list 便于在 _harvest 内累加）

    if cid:
        # A 路：有中国 facet，直接按国家筛（快、准、不漏）
        _harvest(host, tenant, site, jobs, seen, {param: [cid]}, "", detail_state)
    else:
        # B 路：没有中国 facet。用 searchText 定向搜主要城市。
        # 岗位数不多（<=200）时也顺手全量翻一遍兜底。
        for term in SEARCH_TERMS:
            _harvest(host, tenant, site, jobs, seen, {}, term, detail_state)
        if total and total <= 200:
            _harvest(host, tenant, site, jobs, seen, {}, "", detail_state)

    return jobs
