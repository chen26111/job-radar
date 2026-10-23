"""播报渲染层（三通道共用一套视图模型）。

改造前的问题：`notify_preview` 生成 Markdown，`send_notify` 再用正则逐行拆回
飞书 post 的节点。加粗、缩进列表、多级符号一多就拆错，而且三个通道看到的
内容容易不一致。

现在改成：**数据 → 视图模型 → 各通道格式化**。Markdown 只是其中一种输出，
不再是中间格式。

视图模型（`view()` 的返回）：
    {
      "company": "辉瑞", "city": "杭州", "role": "UX/UI",
      "title": "UI Designer 交互视觉设计师",
      "meta": "全职 · 职位号 4961960 · 地点 厦门 / 杭州",   # 空串则不输出该行
      "posted": "2026-08-14 发布（70 天前）",
      "duty": [...], "must": [...], "facts": "5 年以上 · 本科 · 组件库",
      "warn": "",            # 可信度提示（正文冲突/已截止/链接打不开）
      "url": "https://...",
    }

三条硬规则（都是为了「真实可靠」）：
1. **不展示内部匹配分**。分数是相对指标，给用户看只会让人怀疑"这数怎么算的"。
2. **发布时间三档口径，绝不混用**：有官网 startDate 就写具体日期；只有相对文本
   就照抄"发布于 30 天前"；都没有就**不写**——绝不用 first_seen 冒充发布日期。
3. **没有 JD 就明说**「官网未提供职位描述」，不留白也不编。
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Dict, List, Optional

from . import job_attrs

# --------------------------------------------------------------------------- #
# 词表
# --------------------------------------------------------------------------- #
CITY_ZH = {
    "hangzhou": "杭州", "shanghai": "上海", "beijing": "北京", "shenzhen": "深圳",
    "guangzhou": "广州", "suzhou": "苏州", "nanjing": "南京", "chengdu": "成都",
    "wuhan": "武汉", "xian": "西安", "xi'an": "西安", "tianjin": "天津",
    "xiamen": "厦门", "qingdao": "青岛", "dalian": "大连", "wuxi": "无锡",
    "hefei": "合肥", "changsha": "长沙", "zhengzhou": "郑州", "jinan": "济南",
    "shenyang": "沈阳", "fuzhou": "福州", "dongguan": "东莞", "zhuhai": "珠海",
    "ningbo": "宁波", "kunshan": "昆山", "tonglu": "桐庐", "zhejiang": "浙江",
    "jiangsu": "江苏", "guangdong": "广东", "hong kong": "香港",
}
TIME_ZH = {
    "full time": "全职", "part time": "兼职", "contract": "合同制",
    "internship": "实习", "temporary": "临时", "casual": "兼职",
    "fixed term": "固定期限",
}
# 这些词当城市显示时没有信息量，宁可留空
_CITY_NOISE = {"(", ")", "multiple locations", "remote", "hybrid", "onsite"}

ROLE_ORDER = ("产品设计", "UX", "UI", "用户研究", "设计系统", "内容设计",
              "服务设计", "设计运营", "动效", "品牌", "无障碍", "交互", "创意", "设计")


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def city_zh(location: str) -> List[str]:
    """地点字符串 → 中文城市列表。

    "Xiamen, China / Hangzhou"       → ["厦门", "杭州"]
    "China - Zhejiang - Hangzhou"    → ["杭州"]
    "CN, Shanghai"                   → ["上海"]
    """
    out, seen = [], set()
    for part in re.split(r"[/|;、]", location or ""):
        found = ""
        low = part.strip().lower()
        for key, zh in CITY_ZH.items():
            if re.search(rf"(?<![a-z]){re.escape(key)}(?![a-z])", low):
                found = zh
                break
        if not found:
            # 中文地点（"杭州市西湖区"）
            m = re.search(r"([\u4e00-\u9fa5]{2,4})(?:市|区|省)?", part)
            if m and m.group(1) not in _CITY_NOISE and not re.search(r"中国|china", low):
                found = m.group(1)
        found = (found or "").strip()
        if found and found not in seen and found not in _CITY_NOISE:
            seen.add(found)
            out.append(found)
    return out[:3]


def company_display(job: Dict) -> str:
    name = re.sub(r"\s+", " ", str(job.get("company_name") or "")).strip()
    return name or "未知公司"


def clean_title(job: Dict) -> str:
    title = re.sub(r"\s+", " ", str(job.get("title") or "")).strip()
    company = company_display(job)
    if company and title.lower().startswith(company.lower()):
        title = title[len(company):].strip(" -—·:：、，,")
    return title or company


def role_tag(job: Dict) -> str:
    tags = set(job.get("tags") or [])
    hit = [t for t in ROLE_ORDER if t in tags]
    return "/".join(hit[:2]) if hit else "设计"


def _days_between(iso_date: str) -> Optional[int]:
    if not iso_date or len(iso_date) < 10:
        return None
    try:
        d = dt.date.fromisoformat(iso_date[:10])
    except ValueError:
        return None
    return (dt.date.today() - d).days


def posted_text(jd: Dict) -> str:
    """发布时间——三档口径，绝不混用。

    1) 有官网 startDate（真实发布日期）→ "2026-08-14 发布（70 天前）"
    2) 只有相对文本 → "官网显示「发布于 30 天前」"
    3) 都没有 → ""（不输出，不拿 first_seen 冒充）
    """
    start = str((jd or {}).get("start_date") or "")
    n = _days_between(start)
    if n is not None and n >= 0:
        when = "今天发布" if n == 0 else ("昨天发布" if n == 1 else f"{n} 天前")
        return f"{start} 发布（{when}）"
    raw = str((jd or {}).get("posted_on") or "").strip()
    if raw:
        return f"官网显示「{_zh_posted(raw)}」"
    return ""


_EN_DAYS = re.compile(r"(\d+)\+?\s*days?", re.I)
_EN_MONTHS = re.compile(r"(\d+)\+?\s*months?", re.I)
_ZH_DAYS = re.compile(r"(\d+)\+?\s*天")


def _zh_posted(raw: str) -> str:
    """把 "Posted 30+ Days Ago" 转成 "发布于 30+ 天前"；已是中文就原样返回。"""
    if re.search(r"[\u4e00-\u9fa5]", raw):
        return raw
    low = raw.lower()
    m = _EN_DAYS.search(low)
    if m:
        return f"发布于 {m.group(1)}{'+' if '+' in low else ''} 天前"
    m = _EN_MONTHS.search(low)
    if m:
        return f"发布于 {m.group(1)}{'+' if '+' in low else ''} 个月前"
    if "today" in low:
        return "今天发布"
    if "yesterday" in low:
        return "昨天发布"
    return raw


def facts_line(facts: Dict) -> str:
    """把结构化事实拼成一行：年限 · 学历 · 语言 · 工具。"""
    f = facts or {}
    parts = [f.get("years") or "", f.get("education") or "",
             f.get("language") or ""]
    tools = [t for t in (f.get("tools") or []) if t]
    if tools:
        parts.append("、".join(tools[:3]))
    return " · ".join(p for p in parts if p)


# --------------------------------------------------------------------------- #
# 视图模型
# --------------------------------------------------------------------------- #
def _pick_points(jd: Dict, ai: Optional[Dict], lang: str):
    """决定「做什么 / 要求」用哪一版内容。

    优先大模型（已过数字校验），其次规则提取。英文原文在没有大模型时
    只保留 2 条并标注"原文"，避免播报变成英文长文。
    """
    rule = (jd or {}).get("summary") or {}
    duty = list((ai or {}).get("duty") or [])
    must = list((ai or {}).get("must") or [])
    from_ai = bool(duty or must)
    if not duty:
        duty = list(rule.get("duty") or [])
    if not must:
        must = list(rule.get("must") or [])
    if not from_ai and lang == "en":
        duty, must = duty[:2], must[:2]
    return duty[:3], must[:3], from_ai


def view(job: Dict, jd: Optional[Dict] = None, ai: Optional[Dict] = None,
         link_ok: Optional[bool] = None) -> Dict:
    """单条岗位 → 视图模型（渲染层唯一入口）。"""
    jd = jd or {}
    summary = jd.get("summary") or {}
    lang = summary.get("lang") or "zh"
    duty, must, from_ai = _pick_points(jd, ai, lang)

    cities = city_zh(job.get("location") or jd.get("location") or "")
    if not cities and jd.get("additional_locations"):
        cities = city_zh(" / ".join(jd["additional_locations"]))

    meta_bits = []
    tt = str(jd.get("time_type") or "").strip()
    if tt:
        meta_bits.append(TIME_ZH.get(tt.lower(), tt))
    emp = str(job.get("employment_type") or "").strip()
    if emp and not tt:
        meta_bits.append(TIME_ZH.get(emp.lower(), emp))
    # 职级 / 办公方式：从标题识别（事实层），权重在 profile["attr_bonus"]
    a = job_attrs.extract(job.get("title") or "", "", job.get("location") or "")
    if a["level_zh"]:
        meta_bits.append(a["level_zh"])
    if a["work_mode_zh"]:
        meta_bits.append(a["work_mode_zh"])
    if jd.get("req_id"):
        meta_bits.append(f"职位号 {jd['req_id']}")
    # 不再重复写地点：多城市已经在卡片首行的「· 厦门 / 杭州」里了

    # ---- 可信度提示（都是原文能查证的事实，不是猜测） ----
    warn = ""
    age = _days_between(jd.get("start_date") or "")
    if summary.get("flag"):
        warn = summary["flag"]
    elif jd.get("can_apply") is False:
        warn = "官网显示该岗位当前不可投递"
    elif jd.get("end_date") and (_days_between(jd["end_date"]) or 0) > 0:
        warn = f"官网报名已于 {jd['end_date']} 截止，仍可留意后续重开"
    elif link_ok is False:
        warn = "岗位链接当前打不开，建议去官网搜职位名"
    elif age is not None and age >= 180:
        # 挂了大半年还在架，多半是滚动招聘的储备岗，不是"投了没反应"的那种
        warn = f"官网标注发布已 {age} 天，可能是长期滚动招聘，投递前建议先确认"
    elif age is not None and age >= 60:
        warn = f"已挂 {age} 天，投递前建议先确认是否仍在招"
    elif cities and "杭州" not in cities:
        # 日报标题写的是杭州，混进外地岗必须就地说明，不能让人点进去才发现
        warn = f"工作地为「{' / '.join(cities)}」，不在杭州"

    one = (ai or {}).get("one_liner") or ""
    return {
        "company": company_display(job),
        "city": " / ".join(cities),
        "role": role_tag(job),
        "title": clean_title(job),
        "meta": " · ".join(meta_bits),
        "posted": posted_text(jd),
        "one_liner": one,
        "duty": duty,
        "must": must,
        "facts": facts_line(summary.get("facts") or {}),
        "lang": lang,
        "has_jd": bool(summary.get("has_jd")),
        "warn": warn,
        "url": job.get("official_url") or "",
        "key": job.get("dedup_key") or "",
    }


# --------------------------------------------------------------------------- #
# 卡片 → 纯文本行（三通道共用的中间形态）
# --------------------------------------------------------------------------- #
def card_lines(v: Dict, compact: bool = False) -> List[str]:
    """一条岗位 → 若干行文本。`compact` 用于企业微信（4096 字节限制）。"""
    head = f"▌{v['company']}"
    if v["city"]:
        head += f" · {v['city']}"
    if v["role"]:
        head += f" ｜ {v['role']}"
    lines = [head, f"{v['title']}" + (f" · {v['meta']}" if v["meta"] else "")]
    if v["posted"]:
        lines.append(v["posted"])

    if v["warn"]:
        lines.append(f"⚠ {v['warn']}")

    cap = 1 if compact else 3
    if v["one_liner"]:
        lines.append(f"一句话：{v['one_liner']}")

    if v["duty"]:
        lines.append("做什么" if not compact else "做什么：" + "；".join(v["duty"][:cap]))
        if not compact:
            lines.extend(f"· {x}" for x in v["duty"][:cap])

    if compact:
        if v["must"]:
            lines.append("要求：" + "；".join(v["must"][:1]))
        if v["facts"]:
            lines.append(f"硬条件：{v['facts']}")
    else:
        if v["must"]:
            lines.append("要求")
            lines.extend(f"· {x}" for x in v["must"][:cap])
        if v["facts"]:
            lines.append(f"硬条件：{v['facts']}")

    if not v["has_jd"] and not v["one_liner"]:
        lines.append("官网未提供职位描述，建议打开链接查看")
    elif v["lang"] == "en" and not v["one_liner"] and v["duty"]:
        lines.append("（英文原文）")

    if v["url"]:
        lines.append(f"→ {v['url']}" if compact else "→ 查看岗位")
    return lines


# --------------------------------------------------------------------------- #
# 三通道输出
# --------------------------------------------------------------------------- #
def render_markdown(report: Dict) -> str:
    """标准 Markdown（PushPlus / 存档 / 网页共用）。"""
    out: List[str] = [f"# {report['title']}"]
    if report.get("stats"):
        out.extend(["", report["stats"]])
    if report.get("subtitle"):
        out.append(report["subtitle"])

    for sec in report.get("sections") or []:
        out.extend(["", f"## {sec['title']}"])
        if sec.get("note"):
            out.append(f"*{sec['note']}*")
        items = sec.get("items") or []
        if not items:
            out.append("暂无")
            continue
        for v in items:
            out.append("")
            out.extend(_md_card(v))
    if report.get("excluded"):
        out.extend(["", "## 已排除"])
        for e in report["excluded"]:
            out.append(f"- {e['label']} —— {e['reason']}")
    if report.get("footer"):
        out.extend(["", f"---", report["footer"]])
    return "\n".join(out)


def _md_card(v: Dict) -> List[str]:
    lines = card_lines(v)
    out = [f"**{lines[0]}**", f"**{lines[1]}**"]
    for ln in lines[2:]:
        if ln.startswith("→ "):
            out.append(f"[{ln[2:]}]({v['url']})" if v["url"] else ln)
        elif ln.startswith("· "):
            # 用真列表语法，否则两个空格缩进在 Markdown 里会被并成一段
            out.append(f"- {ln[2:]}")
        elif ln in ("做什么", "要求"):
            out.append(f"{ln}：")
        else:
            out.append(ln)
    return out


def render_feishu(report: Dict) -> Dict:
    """飞书 post 消息体。

    直接产出结构化节点，不再从 Markdown 反解析——加粗/缩进/链接一多就拆错。
    用 `▌` 做视觉分隔、`【】` 做小节标题，不依赖 style 字段（部分客户端不渲染）。
    """
    content: List[List[Dict]] = []

    def add(text: str) -> None:
        content.append([{"tag": "text", "text": text}])

    if report.get("stats"):
        add(report["stats"])
    if report.get("subtitle"):
        add(report["subtitle"])

    for sec in report.get("sections") or []:
        content.append([])                      # 空行分组，飞书用空数组
        add(f"【{sec['title']}】")
        if sec.get("note"):
            add(sec["note"])
        items = sec.get("items") or []
        if not items:
            add("今日无")
            continue
        for v in items:
            content.append([])
            head = f"▌{v['company']}"
            if v["city"]:
                head += f" · {v['city']}"
            if v["role"]:
                head += f" ｜ {v['role']}"
            add(head)
            add(v["title"] + (f" · {v['meta']}" if v["meta"] else ""))
            if v["posted"]:
                add(v["posted"])
            if v["warn"]:
                add(f"⚠ {v['warn']}")
            if v["one_liner"]:
                add(f"一句话：{v['one_liner']}")
            if v["duty"]:
                add("做什么")
                for x in v["duty"][:3]:
                    add(f"· {x}")
            if v["must"]:
                add("要求")
                for x in v["must"][:3]:
                    add(f"· {x}")
            if v["facts"]:
                add(f"硬条件：{v['facts']}")
            if not v["has_jd"] and not v["one_liner"]:
                add("官网未提供职位描述，建议打开链接查看")
            elif v["lang"] == "en" and not v["one_liner"] and v["duty"]:
                add("（以上为英文原文）")
            if v["url"]:
                content.append([{"tag": "a", "text": "→ 查看岗位", "href": v["url"]}])

    if report.get("excluded"):
        content.append([])
        add("【已排除】")
        for e in report["excluded"]:
            add(f"· {e['label']} —— {e['reason']}")
    if report.get("footer"):
        content.append([])
        add(report["footer"])

    return {"msg_type": "post",
            "content": {"post": {"zh_cn": {"title": report["title"],
                                           "content": content}}}}


def _wecom_body(report: Dict, max_items: int) -> str:
    """企业微信版正文：紧凑卡片（1 条职责 + 1 条要求），不带 note（省字节）。"""
    out: List[str] = [f"# {report['title']}"]
    if report.get("stats"):
        out.extend(["", report["stats"]])
    if report.get("subtitle"):
        out.append(report["subtitle"])

    for sec in report.get("sections") or []:
        items = sec.get("items") or []
        if not items:
            continue
        out.extend(["", f"## {sec['title']}"])
        for v in items[:max_items]:
            lines = card_lines(v, compact=True)
            out.append("")
            out.append(f"**{lines[0]}**")
            out.append(f"**{lines[1]}**")
            for ln in lines[2:]:
                if ln.startswith("→ "):
                    out.append(f"[→ 查看岗位]({v['url']})")
                else:
                    out.append(ln)
        if len(items) > max_items:
            out.append(f"…另有 {len(items) - max_items} 条")

    if report.get("excluded"):
        out.extend(["", "## 已排除"])
        for e in report["excluded"][:3]:
            out.append(f"- {e['label']} —— {e['reason']}")
    if report.get("footer"):
        out.extend(["", "---", report["footer"]])
    return "\n".join(out)


def render_wecom(report: Dict, limit: int = 4000) -> Dict:
    """企业微信 markdown（4096 字节上限，按 UTF-8 字节算，中文 3 字节/字）。

    先降条目数再截断：直接按字节切会把最后一条岗位从中截断，读起来是乱码。
    """
    for max_items in (4, 3, 2, 1):
        body = _wecom_body(report, max_items)
        if len(body.encode("utf-8")) <= limit:
            return {"msgtype": "markdown", "markdown": {"content": body}}
    body = _wecom_body(report, 1).encode("utf-8")[:limit].decode("utf-8", "ignore")
    body += "\n\n（内容过长已截断，完整版见信息台）"
    return {"msgtype": "markdown", "markdown": {"content": body}}
