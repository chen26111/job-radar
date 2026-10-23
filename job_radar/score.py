"""规则粗分（对应规划 4.1 节第一层）。

全量、零成本、可解释。只有粗分 >= min_score_to_push 的岗位，
后续 Phase 3 才会调 AI 做精排和匹配原因（4.1 节第二层）。

本仓库已定向为「杭州外企 UX/交互/产品设计」——
评分维度：来源可信度 + 设计岗方向匹配 + 城市匹配 + 外企加成 - 误命中/风险扣分。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List

from .models import Job
from .normalize import normalize_city
from .job_attrs import LABELS as ATTR_LABELS
from .job_attrs import extract as extract_attrs
from .role_rules import (NON_DESIGN_TITLE, employer_tier, is_design_role,
                         role_ring, role_signal_score)

# 纯 ASCII 且长度 <= 3 的词（ui/ux/ai/vr…）用词边界匹配，避免 "build" 命中 "ui"
_SHORT_ASCII = re.compile(r"^[a-z0-9/+&. -]{1,3}$")


def kw_hit(kw: str, text: str) -> bool:
    """关键词是否命中 text（text 需已 lower）。短英文词走词边界，中文走子串。"""
    k = (kw or "").lower().strip()
    if not k:
        return False
    if _SHORT_ASCII.match(k):
        return re.search(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])", text) is not None
    return k in text


# 风险关键词 → 扣分（规划第 4 章风险扣分项）
RISK_KEYWORDS = {
    "劳务派遣": 40,
    "派遣": 30,
    "外包": 30,
    "第三方": 15,
    "电话客服": 25,
}


@dataclass
class ScoreResult:
    """单画像打分结果（纯数据，不改 Job）。"""
    score: int = 0
    tags: List[str] = field(default_factory=list)
    risk_flags: List[str] = field(default_factory=list)


def score_job(job: Job, profile: Dict) -> ScoreResult:
    """按单个画像给 job 打粗分，返回结果对象——**不修改 job**（便于对多画像取最优）。

    总分 = 来源可信度 + 专业匹配 + 城市匹配 - 风险扣分（规划第 4 章公式简化版）。
    """
    text = (job.title + " " + job.jd_text).lower()
    score = int(job.source_confidence * 0.2)  # 来源可信度（权重调低，避免聚合源虚高）
    tags: List[str] = []

    # ------------------------------------------------------------------
    # 硬门槛：既不是设计岗也不是产品经理就直接出局，分数归零。
    # 不能靠"外企+34 / 杭州+8 / keyword 命中"把无关岗凑过阈值——
    # 那样播报里会混进 Fund Administration、Tax Planning、Package Design 硬件岗。
    #
    # 两个环（用户口径：以交互/UI 设计为中心，向外辐射，产品经理也算）：
    #   design 核心环 —— 正常计分
    #   pm     外环  —— 降权后计分，播报里单独成节，不与设计岗混排
    # ------------------------------------------------------------------
    ring = role_ring(job.title, job.jd_text)
    if not ring:
        return ScoreResult(score=0, tags=["非设计岗"], risk_flags=[])

    # ------------------------------------------------------------------
    # 城市硬过滤：只要杭州。
    # 地点已知且明确不在目标城市 → 直接出局（以前只是加分，结果上海岗也混进来了）。
    # 地点为空时**不杀**：很多外企 Workday 列表不带地点，杀了会误伤。
    # 纯远程岗只在工作地/JD 里写了杭州或中国时才留 —— 否则大概率要工签。
    # ------------------------------------------------------------------
    if profile.get("city_hard_filter"):
        targets = {normalize_city(c) for c in profile.get("cities", []) if c}
        job_city = normalize_city(job.location)
        loc_text = f"{job.location} {job.title}".lower()
        is_remote = any(k in loc_text for k in ("remote", "远程", "anywhere", "wfh"))
        china_remote = is_remote and any(k in loc_text for k in ("杭州", "hangzhou", "中国", "china"))
        if job_city and job_city not in targets and not china_remote:
            return ScoreResult(score=0, tags=["不在杭州"], risk_flags=[])

    if ring == "pm":
        score -= int(profile.get("pm_ring_penalty", 12))
        tags.append("产品经理")

    # 专业匹配（关键词命中**封顶**：防止 JD 堆词把无名小公司刷到第一）
    # 短英文词（ui/ux/ai…）用词边界匹配，否则 "build"/"quick"/"guide" 都会误命中 "ui"
    must = [k for k in profile.get("must_keywords", []) if kw_hit(k, text)]
    nice = [k for k in profile.get("nice_keywords", []) if kw_hit(k, text)]
    neg = [k for k in profile.get("negative_keywords", []) if kw_hit(k, text)]
    score += min(len(must), 4) * 12 + min(len(nice), 3) * 5 - len(neg) * 20
    tags += must[:4] + nice[:3]

    # 目标岗位名直接命中（对口是硬指标）
    if any(r.lower() in job.title.lower() for r in profile.get("target_roles", [])):
        score += 25
        tags.append("role_match")

    role_score, role_tags = role_signal_score(job.title, job.jd_text)
    score += role_score
    tags += role_tags

    # ------------------------------------------------------------------
    # 岗位属性加权（职级 / 雇佣形式 / 办公方式 / 职能细分 / 语言要求）
    # 识别口径在 job_attrs.py，权重在 profile["attr_bonus"]——改口味只改配置。
    # 属性总分封顶，防止"资深+远程+英语+设计系统"叠到 60 分把公司层级差异抹平。
    # ------------------------------------------------------------------
    attrs = extract_attrs(job.title, job.jd_text, job.location)
    bonus_map = profile.get("attr_bonus") or {}
    attr_total = 0
    for k in attrs["keys"]:
        if k in bonus_map:
            attr_total += int(bonus_map[k])
            label = ATTR_LABELS.get(k)
            if label:
                tags.append(label)
    attr_total = max(-60, min(attr_total, 24))
    score += attr_total

    # 行业匹配：偏好行业加分、回避行业减分
    if job.industry and job.industry in profile.get("target_industries", []):
        score += 20
        tags.append("industry_match")
    if job.industry and job.industry in profile.get("negative_industries", []):
        score -= 20

    # 雇主层级：从**公司名**判外企/大厂/硬科技/央国企。用户最看重"外企"这一维度。
    tier, boost = employer_tier(job.company_name, job.industry, job.source_id or "")
    if tier:
        score += boost
        tags.append(tier)

    # 城市匹配
    job_city = normalize_city(job.location)
    if job_city and any(normalize_city(c) == job_city for c in profile.get("cities", [])):
        score += 10
        tags.append("city_match")
    # 杭州岗位额外小幅加权（本仓库主战场）
    if job_city in ("hangzhou", "杭州"):
        score += 8
        tags.append("杭州")

    # 地点可达性：国外岗位（地点无中文 且 非远程）对国内求职者基本投不了 → 降权。
    off = profile.get("offshore_penalty", 0)
    if off and job.location:
        loc = job.location.lower()
        has_cjk = any("一" <= c <= "鿿" for c in job.location)
        # 仅放过"明确全球远程/中国"的；"Remote - California"等外企区域远程要美国工签，照罚
        global_ok = any(k in loc for k in ("worldwide", "global", "anywhere", "中国", "china"))
        if not has_cjk and not global_ok:
            score -= off
            tags.append("offshore")

    # 岗位对口性已由函数开头的 is_design_role() 硬门槛保证，这里只补一层
    # 细分"非目标设计岗"惩罚：平面/室内/工业/服装/CAD/剪辑等，即使含设计字样也降权。
    title = job.title or ""
    low = title.lower()
    sid = job.source_id or ""
    exempt = sid.startswith("edu-") or sid in ("gov-sasac", "gov-qyzp")
    if not exempt:
        block = list(profile.get("exclude_roles", [])) + list(profile.get("exclude_keywords", []))
        block += [k for k in NON_DESIGN_TITLE if k not in block]
        if any(k.lower() in low for k in block):
            score -= 100
            tags.append("非目标设计方向")

    # 县级 base 地降权（不考虑县/县级市岗位）。区(urban)不算。
    cp = profile.get("county_penalty", 0)
    if cp and "县" in (job.location or ""):
        score -= cp
        tags.append("县级")

    # 风险扣分
    risk: List[str] = []
    for kw, penalty in RISK_KEYWORDS.items():
        if kw in text:
            score -= penalty
            risk.append(kw)

    return ScoreResult(score=max(0, score),
                       tags=list(dict.fromkeys(tags)),
                       risk_flags=risk)
