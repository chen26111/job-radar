"""岗位质量标签与默认降噪规则。

质量标签不删除数据，只帮助工作台默认隐藏明显低质项，并允许用户单独查看。
本仓库定向为「杭州外企设计岗」，降噪规则相应调整为：滤掉非设计类同名词、
外包/派遣、低相关岗位等。
"""
from __future__ import annotations

from typing import List, Tuple

LOW_QUALITY_TAGS = {
    "代招/委托",
    "猎头",
    "劳务派遣",
    "泛销售",
    "低相关管培",
    "地点风险",
    "缺官网链接",
    "非设计岗",
}

_AGENCY_KW = ("代招", "外包", "第三方", "人力资源管理有限公司", "人才服务有限公司", "劳务")
_AGENCY_TITLE_KW = ("委托单位", "委托招聘", "代招", "外包", "第三方")
_HEADHUNT_KW = ("猎头", "headhunter", "猎聘")
_DISPATCH_KW = ("劳务派遣", "派遣")
_SALES_KW = ("销售代表", "销售专员", "客户经理", "电话销售", "渠道销售", "招商", "地推", "电销")
_MANAGEMENT_TRAINEE_KW = ("管培", "管理培训生", "储备干部", "储备生")
# 设计/体验相关信号（用于判断"管培生是否低相关"）
_REMOTE_BAD_KW = ("县", "乡", "镇")
# 标题级非设计同名词（与 role_rules.NON_DESIGN_TITLE 互补，这里只做打标）
_NON_DESIGN_TITLE_KW = (
    "平面设计", "美工", "美编", "包装设计", "装帧", "排版", "印前",
    "室内设计", "家装", "软装", "硬装", "空间设计", "展陈", "陈列",
    "建筑设计", "土木", "结构设计", "暖通", "给排水", "施工图", "景观设计", "园林",
    "工业设计", "模具设计", "结构工程师", "机械设计",
    "服装设计", "面料", "鞋类设计", "珠宝设计", "玩具设计",
    "原画", "角色建模", "3d建模", "场景建模", "动画设计", "特效设计",
)
_DESIGN_SIGNAL_KW = ("ux", "ui", "交互", "体验", "designer", "design",
                     "用户研究", "视觉设计", "产品设计")


def quality_tags(job) -> Tuple[List[str], List[str]]:
    """返回 (quality_tags, risk_flags)。job 可以是 Job 或含同名字段的对象。"""
    title = getattr(job, "title", "") or ""
    company = getattr(job, "company_name", "") or ""
    jd = getattr(job, "jd_text", "") or ""
    loc = getattr(job, "location", "") or ""
    url = getattr(job, "official_url", "") or ""
    deadline = getattr(job, "deadline", "") or ""
    sid = getattr(job, "source_id", "") or ""
    text = f"{title} {company} {jd}".lower()
    title_low = title.lower()

    tags: List[str] = []
    risks: List[str] = []
    agency_text = f"{title} {company}".lower()
    if any(k.lower() in agency_text for k in _AGENCY_KW) or any(k in title for k in _AGENCY_TITLE_KW):
        tags.append("代招/委托")
    if any(k.lower() in text for k in _HEADHUNT_KW):
        tags.append("猎头")
    if any(k in text for k in _DISPATCH_KW):
        tags.append("劳务派遣")
        risks.append("劳务派遣")
    if any(k in title for k in _SALES_KW):
        tags.append("泛销售")
    if any(k in title for k in _MANAGEMENT_TRAINEE_KW) and not any(k in text for k in _DESIGN_SIGNAL_KW):
        tags.append("低相关管培")
    if not url:
        tags.append("缺官网链接")
    if sid.startswith(("wd-", "gh-", "lever-", "ashby-")):
        tags.append("仅社招")
    if any(k in loc for k in _REMOTE_BAD_KW):
        tags.append("地点风险")
    # 标题是平面/室内/工业/服装/原画等"同名不同岗" → 打标降噪
    if any(k in title_low for k in _NON_DESIGN_TITLE_KW) and not any(
            k in title_low for k in ("ux", "ui", "交互", "体验", "designer", "product design")):
        tags.append("非设计岗")
    return list(dict.fromkeys(tags)), list(dict.fromkeys(risks))


def is_low_quality(tags: List[str]) -> bool:
    return bool(LOW_QUALITY_TAGS & set(tags or []))
