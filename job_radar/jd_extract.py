"""JD 正文清洗与要点提取（纯规则，零依赖）。

目标：把 Workday 详情接口返回的 `jobDescription`（HTML）变成**可核验的结构化事实**，
供播报渲染使用。

设计原则——**只做压缩与选择，不做推断**：
    每一行输出都必须能在原文里找到出处。招聘信息一旦出现被编造的内容
    （薪资、团队规模、"有竞争力的薪酬"），整份播报就失去可信度。
    所以规则提取宁可少写，也不会写错。

大模型在这里只做可选的中文改写（见 `llm.py`），且必须经过本模块产出的
事实做交叉校验。

结构怪癖（都实测过，代码里已处理）：

1. **小标题五花八门**（5 条真实 JD 里出现了 12 种写法）：
     3M            Key Responsibilities / Your Skills and Expertise / Work Location:
     辉瑞中文       岗位职责：/ 任职资格:
     辉瑞英文       Job Summary / Responsibilities / Education and Experience / Technical Skills Requirements
     道富           What You Will Be Responsible For / What We Value / Where Will The Role Be Based
     RingCentral   To succeed in this role you must have experience in and be able to: / Required Qualifications:
   → 用词表 + 「匹配后剩余文字不能太长」双重判定，既认出标题，也不会把
     「Requirements include a bachelor degree...」这种长句误当标题。
2. **不是每条都是 bullet**：辉瑞 CreateSmart 的 Responsibilities 是
   「Creative Design Leadership: Lead high-complexity ...」这种「小标题: 长句」段落。
   → bullet 抽不到时回落到长段落。
3. **正文里塞了大量招聘广告噪声**：3M 开头的公司简介、辉瑞的
   「equal opportunity employer」、各家的「candidate AI-use guidelines」。
   → 统一在 `strip_boilerplate()` 里按行剔除。
4. **实体是数字形式**：Workday 返回 `&#xff1a;`（全角冒号）、`&#xff0c;`（逗号），
   必须走 `html.unescape()` 才能还原。
5. **年限写法有 4 种**：「5 年以上」「5+ years」「10–15+ years」「Minimum 8-10 years」。
   → 一个带可选分组的正则统一处理，注意「年」在「以上」之前（反了就永远抽不到）。
"""
from __future__ import annotations

import html as _html
import re
from typing import Dict, List

# --------------------------------------------------------------------------- #
# HTML → 纯文本
# --------------------------------------------------------------------------- #
_BLOCK_END = re.compile(r"</(p|div|tr|h[1-6]|ul|ol|table|section|article)\s*>", re.I)
_BR = re.compile(r"<br\s*/?>", re.I)
_LI_OPEN = re.compile(r"<li[^>]*>", re.I)
_TD_END = re.compile(r"</td>\s*<(?:td|th)", re.I)
_ANY_TAG = re.compile(r"<[^>]+>")
_MULTI_SPACE = re.compile(r"[ \t\u00a0\u3000]+")
_MULTI_NL = re.compile(r"\n[ \t]*\n+")


def html_to_text(raw: str) -> str:
    """Workday 的 jobDescription HTML → 逐行纯文本。

    `<li>` 转成 `- ` 前缀，块级标签结尾转换行，其余标签直接剥掉。
    """
    if not raw:
        return ""
    t = str(raw)
    # 列表项先转成 "- "，否则整段会挤成一行，后面就没法按行抽 bullet 了
    t = _LI_OPEN.sub("\n- ", t)
    t = _TD_END.sub(" | ", t)
    t = _BR.sub("\n", t)
    t = _BLOCK_END.sub("\n", t)
    t = _ANY_TAG.sub("", t)
    t = _html.unescape(t)
    t = _MULTI_SPACE.sub(" ", t)
    lines = [ln.strip() for ln in t.splitlines()]
    t = "\n".join(ln for ln in lines if ln)
    return _MULTI_NL.sub("\n", t).strip()


def is_english(text: str) -> bool:
    """中文字符占比 < 5% 视为英文正文。"""
    if not text:
        return False
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    letters = sum(1 for c in text if c.isalpha())
    if letters == 0:
        return False
    return cjk / max(1, letters + cjk) < 0.05


# --------------------------------------------------------------------------- #
# 招聘广告噪声（各家 JD 里都会塞一段，跟岗位内容无关）
# --------------------------------------------------------------------------- #
_BOILERPLATE = [
    r"equal\s+opportunity\s+employer",
    r"affirmative\s+action",
    r"\beeo\b",
    r"we\s+do\s+not\s+accept\s+unsolicited",
    r"staffing\s+agenc|search\s+firm|headhunter|recruit(ment|ing)\s+agenc",
    r"privacy\s+(notice|statement|policy)|\bgdpr\b|personal\s+data",
    r"candidate\s+ai[- ]use|candidates?\s+should\s+not\s+use\s+ai",
    r"recruitment\s+fraud|beware\s+of\s+scam",
    r"has\s+a\s+long[- ]standing\s+reputation",
    r"choosing\s+where\s+to\s+start\s+and\s+grow\s+your\s+career",
    r"to\s+learn\s+more\s+about\s+acceptable\s+and\s+prohibited",
    r"apply\s+now|submit\s+your\s+(resume|cv)|follow\s+us|equal\s+employment",
    r"^information\s*&\s*business\s+tech$",
    r"^job\s+description\s*[:：]?$",
    r"^about\s+(us|the\s+company|3m|state\s+street)$",
    r"^company\s+overview$",
    r"^discover\s+more\s+information",
    r"^read\s+our\s+ceo\s+statement",
    r"我们(是|提供|致力于)|^本公司|^公司简介",
    r"^以上岗位|^本岗位|^注[：:]|^备注[：:]",
]
_BOILER_RE = re.compile("|".join(_BOILERPLATE), re.I)


def strip_boilerplate(text: str) -> str:
    """按行剔除招聘广告噪声（公司简介、EEO、隐私声明等）。"""
    keep = []
    for ln in (text or "").splitlines():
        s = ln.strip()
        if not s:
            continue
        if _BOILER_RE.search(s):
            continue
        keep.append(s)
    return "\n".join(keep)


# --------------------------------------------------------------------------- #
# 分段：小标题识别
# --------------------------------------------------------------------------- #
# 匹配方式：`re.match` 前缀匹配，再用「剩余文字长度」判断是不是真标题。
_HEADERS: List[tuple] = [
    # (段名, 正则)
    ("duty", r"to\s+succeed\s+in\s+this\s+role.*be\s+able\s+to"),
    ("duty", r"key\s+(duties|responsibilities)"),
    ("duty", r"(responsibilities|responsibility)"),
    ("duty", r"what\s+you\s+will\s+be\s+responsible\s+for"),
    ("duty", r"what\s+you('|’)?ll\s+(be\s+doing|do|own)"),
    ("duty", r"day\s+to\s+day|your\s+(role|mission)"),
    ("duty", r"(main|key)\s+duties|duties"),
    ("duty", r"the\s+impact\s+you('|’)?ll\s+make"),
    ("duty", r"岗位职责|工作职责|主要职责|职责描述|职位描述|工作内容|你将负责"),
    ("duty", r"the\s+role|about\s+the\s+role"),

    ("summary", r"job\s+summary|position\s+summary|role\s+summary|overview"),
    ("summary", r"job\s+description"),

    ("must", r"required\s+qualifications?|minimum\s+qualifications?"),
    ("must", r"preferred\s+qualifications?"),   # 见下方特判：preferred 归 nice
    ("must", r"qualifications?"),
    ("must", r"requirements?"),
    ("must", r"your\s+skills\s+and\s+expertise"),
    ("must", r"education\s+and\s+experience"),
    ("must", r"technical\s+skills\s+requirements?"),
    ("must", r"what\s+we\s+value"),
    ("must", r"what\s+we('|’)?re\s+looking\s+for"),
    ("must", r"what\s+you('|’)?ll\s+need"),
    ("must", r"who\s+you\s+are"),
    ("must", r"must[- ]have"),
    ("must", r"skills?\s*(and|&|as)\s*(experience|requirements)"),
    ("must", r"education\s*(&|and)\s*preferred"),
    ("must", r"任职资格|任职要求|岗位要求|资格要求|任职条件|我们希望你|你需要"),

    ("nice", r"preferred\s+qualifications?|nice\s+to\s+have|bonus\s+points?"),
    ("nice", r"desired\s+(skills|qualifications)"),
    ("nice", r"加分项|优先条件|加分"),

    # 只标注、不参与渲染（避免 "Travel: 10%" 混进要求里）
    ("meta", r"work\s+location|location\s*:|travel\s*:|where\s+will\s+the\s+role\s+be\s+based"),
    ("meta", r"reporting\s+to|department\s*:|job\s+family|employment\s+type"),
]
_HEADER_TAIL = re.compile(r"[\s:：\-–—]+$")
# 标题后面剩余的字符数上限：超过这个长度说明是句子，不是标题
_HEADER_REST_MAX = 32


def _match_header(line: str) -> str:
    """判断一行是不是小标题，返回 duty/summary/must/nice/meta/''。"""
    s = line.strip()
    if not s or len(s) > 80:
        return ""
    probe = s.lower()
    for tag, pat in _HEADERS:
        m = re.match(pat, probe)
        if not m:
            continue
        rest = _HEADER_TAIL.sub("", probe[m.end():]).strip()
        # "Requirements include a bachelor degree and 5+ years..." → rest 很长 → 不是标题
        if len(rest) > _HEADER_REST_MAX:
            continue
        # "Preferred Qualifications:" 被 must 表先匹配到，这里纠正为 nice
        if tag == "must" and probe.startswith("preferred"):
            return "nice"
        return tag
    return ""


def split_sections(text: str) -> Dict[str, List[str]]:
    """按小标题把正文切成 duty/summary/must/nice/meta/intro（每段是行列表）。

    认不出小标题时全部归 intro——上层再用整段兜底，不做位置猜测。
    """
    out: Dict[str, List[str]] = {"duty": [], "summary": [], "must": [],
                                 "nice": [], "meta": [], "intro": []}
    cur = "intro"
    for ln in (text or "").splitlines():
        s = ln.strip()
        if not s:
            continue
        tag = _match_header(s)
        if tag:
            cur = tag
            continue
        out[cur].append(s)
    return out


# --------------------------------------------------------------------------- #
# 抽取要点
# --------------------------------------------------------------------------- #
_BULLET = re.compile(r"^(?:[-•·*–—]\s*|\d{1,2}\s*[.、)）]\s*|[（(]\d{1,2}[)）]\s*)")


def _clip(s: str, limit: int) -> str:
    s = re.sub(r"\s+", " ", (s or "").strip())
    if len(s) <= limit:
        return s
    cut = s[:limit]
    # 中文长句常在「；，、」处断一层意思，优先回退到最后一个标点，
    # 免得截出「搭建设计规范； 在产品市场调研、用户故事…」这种半截话
    for sep in ("；", "。", "；", "，", "、", "; ", ", "):
        i = cut.rfind(sep)
        if i >= int(limit * 0.6):
            return cut[:i].rstrip(" ,，、;；:：。.") + "…"
    # 英文尽量在词边界截断，免得截出半个单词
    if re.search(r"[A-Za-z]$", cut) and re.search(r"[A-Za-z]\s", cut):
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip(" ,，、;；:：。.") + "…"


def bullets(lines: List[str], max_n: int = 3, limit: int = 60) -> List[str]:
    """从一段里取要点。

    优先取显式 bullet（`-`/`•`/`1.`）；没有则取长段落（≥40 字符），
    再没有就按句号切。返回值已去重、已截断。
    """
    items: List[str] = []
    for ln in lines:
        m = _BULLET.match(ln)
        if not m:
            continue
        body = ln[m.end():].strip()
        if len(body) >= 6:
            items.append(body)
    if not items:
        # 段落式（辉瑞 CreateSmart 那种 "Creative Design Leadership: Lead ..."）
        items = [ln for ln in lines if len(ln) >= 40]
    if not items:
        # 兜底：按句切
        joined = " ".join(lines)
        parts = re.split(r"(?<=[。！？.!?])\s+", joined) if is_english(joined) \
            else re.split(r"(?<=[。！？])", joined)
        items = [p.strip() for p in parts if len(p.strip()) >= 12]

    seen, out = set(), []
    for it in items:
        key = it[:24].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(_clip(it, limit))
        if len(out) >= max_n:
            break
    return out


# --------------------------------------------------------------------------- #
# 结构化事实（全部来自原文，可核验）
# --------------------------------------------------------------------------- #
# 统一覆盖 4 种写法：「5 年以上」「5+ years」「10–15+ years」「Minimum 8-10 years」
_YEARS = re.compile(
    r"(\d{1,2})\s*(?:[-–~至到]\s*(\d{1,2}))?\s*(\+)?\s*"
    r"(?:years?|yrs?|年)\s*(以上|或以上|or\s+more|plus)?",
    re.I,
)

# (显示名, 正则, 是否区分大小写)
# 区分大小写是为了避开 "principles"(原则) → "Principle"(设计工具) 这类误命中
_TOOLS = [
    ("Figma", r"Figma", False),
    ("Sketch", r"Sketch", True),
    ("Adobe XD", r"Adobe\s+XD|\bXD\b", False),
    ("Axure", r"Axure", False),
    ("Principle", r"Principle", True),
    ("ProtoPie", r"ProtoPie", False),
    ("Framer", r"Framer", True),
    ("InVision", r"InVision", False),
    ("Zeplin", r"Zeplin", False),
    ("Photoshop", r"Photoshop", False),
    ("Illustrator", r"Illustrator", False),
    ("After Effects", r"After\s+Effects", False),
    ("Cinema 4D", r"Cinema\s*4D|C4D", False),
    ("Blender", r"Blender", False),
    ("Jira", r"Jira", False),
    ("Miro", r"Miro", False),
    ("Design System", r"design\s+systems?", False),
    ("设计系统", r"设计系统", False),
    ("组件库", r"组件库", False),
    ("Material Design", r"Material\s+Design", False),
    ("iOS HIG", r"iOS\s+(HIG|Human\s+Interface\s+Guidelines)", False),
    ("HTML/CSS", r"HTML\s*/?\s*CSS", False),
    ("JavaScript", r"JavaScript", False),
    ("WCAG", r"WCAG", False),
    ("无障碍", r"无障碍", False),
]
_TOOL_RE = [
    (name, re.compile(rf"(?<![A-Za-z0-9])(?:{pat})(?![A-Za-z0-9])", 0 if cs else re.I))
    for name, pat, cs in _TOOLS
]

_EDU = [
    (r"本科|学士|bachelor", "本科"),
    (r"硕士|研究生|master", "硕士"),
    (r"博士|ph\.?d|doctor", "博士"),
    (r"大专|专科|associate\s+degree", "大专"),
]
_LANG = [
    (r"fluen(cy|t)\s+in\s+chinese.{0,60}english|chinese\s*\(mandarin\).{0,40}english", "中英文流利"),
    (r"英语|english", "英语"),
    (r"中文|chinese|mandarin", "中文"),
]


def _extract_years(low: str) -> str:
    """按文档顺序取第一个合理年限（1-30 年），避免 "100 years history" 这类噪声。"""
    for m in _YEARS.finditer(low):
        a = int(m.group(1))
        if not (1 <= a <= 30):
            continue
        b = int(m.group(2)) if m.group(2) else None
        if b is not None and not (a <= b <= 30):
            b = None
        plus = bool(m.group(3) or m.group(4))
        if b:
            # 区间就写区间："10–15+ years" → "10-15 年"（再加"以上"读起来是病句）
            return f"{a}-{b} 年"
        return f"{a} 年以上" if plus else f"{a} 年"
    return ""


def facts(text: str) -> Dict[str, object]:
    """从 JD 正文提取可核验的硬事实：经验年限、工具、学历、语言。"""
    t = text or ""
    low = t.lower()
    out: Dict[str, object] = {}

    years = _extract_years(low)
    if years:
        out["years"] = years

    tools = [name for name, rx in _TOOL_RE if rx.search(t)]
    if tools:
        out["tools"] = tools[:4]

    edu = next((label for pat, label in _EDU if re.search(pat, low)), "")
    if edu:
        out["education"] = edu

    for pat, label in _LANG:
        if re.search(pat, low):
            out["language"] = label
            break

    return out


# --------------------------------------------------------------------------- #
# 标题与正文冲突检测（防止「标题含 UX、正文是写后端的」混进播报）
# --------------------------------------------------------------------------- #
_ENG_STRONG = [
    r"software\s+engineer", r"sdlc", r"back[- ]?end", r"front[- ]?end\s+develop",
    r"全栈", r"算法工程", r"application\s+support", r"api\s+development",
    r"微服务", r"microservice", r"\bjava\b", r"c\+\+", r"\bdevops\b",
    r"自动化测试", r"运维", r"database\s+administration",
]
_DESIGN_CRAFT = [
    r"figma", r"sketch", r"design\s+system", r"设计规范", r"交互设计", r"视觉设计",
    r"user\s+research", r"user\s+experience\s+design", r"wireframe", r"prototyp",
    r"usability", r"用户研究", r"用户体验", r"portfolio", r"brand\s+identity",
    r"视觉识别", r"ui\s+design", r"ux\s+design", r"interaction\s+design",
]
_ENG_RE = re.compile("|".join(_ENG_STRONG), re.I)
_CRAFT_RE = re.compile("|".join(_DESIGN_CRAFT), re.I)


def contradiction(jd_text: str, title: str = "") -> str:
    """标题像设计岗、正文却明显是工程岗时，返回一句中文说明；否则返回 ''。

    实测案例：道富 `Platform Engineering - Dev/UX (Production Website)`，
    正文第一句是 "seeking a hands-on Senior Software Engineer ... SDLC ... backend services"。
    标题里的 "Dev/UX" 能通过 `is_design_role()` 门槛，但正文根本不是设计岗。
    """
    if not jd_text:
        return ""
    eng = len({m.group(0).lower() for m in _ENG_RE.finditer(jd_text)})
    craft = len({m.group(0).lower() for m in _CRAFT_RE.finditer(jd_text)})
    if eng >= 2 and craft == 0:
        return "正文写的是软件工程岗（提到 SDLC / 后端服务 / 应用支持），无设计职责描述"
    if eng >= 4 and craft <= 1:
        return "正文以开发工程为主，设计相关内容很少"
    return ""


# --------------------------------------------------------------------------- #
# 统一入口
# --------------------------------------------------------------------------- #
def summarize(jd_html: str, title: str = "") -> Dict[str, object]:
    """JD HTML → 播报要用的一切（分段要点 + 事实 + 冲突提示）。

    返回:
        {
          "has_jd": bool,          # 正文是否真的抓到了
          "lang": "zh"|"en",
          "duty": [str],           # 做什么，最多 3 条
          "must": [str],           # 要求，最多 3 条
          "facts": {...},          # 年限/工具/学历/语言
          "flag": str,             # 标题与正文冲突提示，空串表示没问题
          "text": str,             # 清洗后的全文（供 llm.py 用）
        }
    """
    text = strip_boilerplate(html_to_text(jd_html))
    result: Dict[str, object] = {
        "has_jd": bool(text and len(text) >= 40),
        "lang": "en" if is_english(text) else "zh",
        "duty": [], "must": [], "facts": {}, "flag": "", "text": text,
    }
    if not result["has_jd"]:
        return result

    secs = split_sections(text)
    limit = 110 if result["lang"] == "en" else 60
    # 职责优先取 Responsibilities 段，没有再退回 Job Summary
    result["duty"] = bullets(secs["duty"], 3, limit) or bullets(secs["summary"], 2, limit)
    result["must"] = bullets(secs["must"], 3, limit)
    if not result["duty"] and not result["must"]:
        # 整篇都没有可识别小标题 → 从正文主体里取前 2 条，仍然只做摘录
        body = [ln for ln in secs["intro"] if len(ln) >= 40]
        result["duty"] = [_clip(x, limit) for x in body[:2]]
    result["facts"] = facts(text)
    result["flag"] = contradiction(text, title)
    return result
