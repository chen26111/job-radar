"""岗位属性提取（通用词表）：职级 / 雇佣形式 / 办公方式 / 职能细分 / 语言要求。

为什么单独一层，而不是散在 score.py 里：

1. **评分和播报必须同一套口径**。score 用它加减分，render 用它出短标签，
   如果各写一套正则，很快就会出现"分数认为这是资深岗、播报却显示初级"。
2. **加不加分是用户口味，识别不识别是事实**。识别写死在这里，
   权重放 `config/profiles.json` 的 `attr_bonus`，改口味不用改代码。
3. **属性是可解释的**。播报里写「资深 · 远程 · 英语环境」比写「匹配 181」有用得多。

识别范围（都是招聘标题/正文里的常见写法，中英双语）：

| 维度 | 取值 |
|---|---|
| 职级 | intern / junior / senior / lead / manager / staff / principal / director |
| 雇佣形式 | internship / part_time / contract / full_time |
| 办公方式 | remote / hybrid / onsite |
| 职能细分 | ux / ui / interaction / product / visual / ux_research / content_design / design_system / design_ops / motion / brand / accessibility / service_design |
| 语言要求 | english / bilingual |

学历和年限**不在这里**：那两项必须读完整 JD 才有，统一由 `jd_extract.facts()` 从正文提取，
避免同一个"本科"在标题正则和正文正则里各判一次、判出两种结果。
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# 词表：(key, 英文正则, 中文词, 排序, 中文显示)
# 顺序 = 判定优先级。中文不走 \b（CJK 不是词字符），单独按子串匹配。
# --------------------------------------------------------------------------- #
_LEVELS: Tuple[Tuple[str, str, Tuple[str, ...], int, str], ...] = (
    # 实习先判：标题里同时出现 "Senior" 和 "Intern" 的极少，但实习是硬信号
    ("intern", r"\b(intern|interns?hip|trainee)\b", ("实习", "实习生"), 0, "实习"),
    ("director", r"\b(director|vp|vice president|head of|chief)\b", ("总监", "总经理"), 6, "总监"),
    ("principal", r"\b(principal|distinguished|fellow)\b", (), 5, "首席"),
    ("manager", r"\b(manager|head)\b", ("经理", "主管"), 5, "经理"),
    ("staff", r"\bstaff\b", (), 4, "Staff"),
    ("lead", r"\b(lead|leader|team lead)\b", ("负责人", "组长"), 4, "负责人"),
    ("senior", r"\b(senior|sr\.?)\b", ("资深", "高级"), 3, "资深"),
    ("junior", r"\b(junior|jr\.?|entry.level|graduate)\b", ("初级", "助理", "应届"), 1, "初级"),
)

# 细分方向：越具体的排越前（"UX/UI Designer" 同时命中 ui 和 ux，"Product Designer" 只命中 product）
_FUNCS: Tuple[Tuple[str, str, Tuple[str, ...], str], ...] = (
    ("ux_research", r"\b(ux research(er)?|user research(er)?|design research(er)?|usability)\b",
     ("用户研究", "用户研究员", "可用性"), "用户研究"),
    ("content_design", r"\b(content design(er)?|ux writ(er|ing))\b", ("内容设计",), "内容设计"),
    ("design_ops", r"\b(design ops|design operations|design program)\b", ("设计运营",), "设计运营"),
    ("design_system", r"\b(design systems?)\b", ("设计系统",), "设计系统"),
    ("accessibility", r"\b(accessibility design(er)?|a11y)\b", ("无障碍设计",), "无障碍"),
    ("motion", r"\b(motion design(er)?|motion graphics|动效)\b", ("动效设计",), "动效"),
    ("brand", r"\b(brand design(er)?)\b", ("品牌设计",), "品牌"),
    ("service_design", r"\b(service design(er)?)\b", ("服务设计",), "服务设计"),
    ("interaction", r"\b(interaction design(er)?)\b", ("交互设计", "交互设计师", "交互"), "交互"),
    ("visual", r"\b(visual design(er)?)\b", ("视觉设计", "视觉设计师"), "视觉"),
    ("creative", r"\b(creative design|creative lead|creative director)\b", ("创意设计", "创意"), "创意"),
    ("product", r"\b(product design(er)?)\b", ("产品设计", "产品设计师"), "产品设计"),
    ("ui", r"\b(ui design(er)?|ui/ux|ui)\b", ("界面设计", "UI设计"), "UI"),
    ("ux", r"\b(ux|user experience|experience design(er)?)\b", ("用户体验", "体验设计"), "UX"),
)

_WORK_MODE: Tuple[Tuple[str, str, Tuple[str, ...], str], ...] = (
    ("remote", r"\b(remote|work from home|wfh|anywhere)\b", ("远程", "居家", "异地面"), "远程"),
    ("hybrid", r"\b(hybrid)\b", ("混合办公", "弹性办公", "每周.*天到岗"), "混合办公"),
    ("onsite", r"\b(on.?site)\b", ("驻场",), "驻场"),
)

_EMPLOYMENT: Tuple[Tuple[str, str, Tuple[str, ...], str], ...] = (
    ("internship", r"\b(intern|interns?hip|trainee)\b", ("实习",), "实习"),
    ("part_time", r"\b(part.?time)\b", ("兼职",), "兼职"),
    ("contract", r"\b(contract(or)?|freelance|temporary|fixed.?term)\b", ("外包", "合同制"), "合同制"),
    ("full_time", r"\b(full.?time)\b", ("全职",), "全职"),
)

_LANGUAGE: Tuple[Tuple[str, str, Tuple[str, ...], str], ...] = (
    ("bilingual", r"\b(bilingual|bilinguals)\b", ("中英双语", "双语", "中英文流利"), "双语"),
    ("english", r"\b(fluent in english|english (fluency|proficiency)|native english|"
                r"english required|good english)\b",
     ("英语流利", "英语良好", "英文流利", "英语无障碍", "英语可读写"), "英语要求"),
)

# 属性 key → 播报里给人看的短标签
LABELS = {
    "level:intern": "实习", "level:junior": "初级", "level:senior": "资深",
    "level:lead": "负责人", "level:manager": "经理", "level:staff": "Staff",
    "level:principal": "首席", "level:director": "总监",
    "mode:remote": "远程", "mode:hybrid": "混合办公", "mode:onsite": "驻场",
    "employment:internship": "实习", "employment:part_time": "兼职",
    "employment:contract": "合同制", "employment:full_time": "全职",
    "lang:english": "英语要求", "lang:bilingual": "中英双语",
    "func:ux_research": "用户研究", "func:content_design": "内容设计",
    "func:design_ops": "设计运营", "func:design_system": "设计系统",
    "func:accessibility": "无障碍", "func:motion": "动效", "func:brand": "品牌",
    "func:service_design": "服务设计", "func:interaction": "交互", "func:visual": "视觉",
    "func:creative": "创意",
    "func:product": "产品设计", "func:ui": "UI", "func:ux": "UX",
}


def _hit_en(pattern: str, low: str) -> bool:
    return bool(pattern) and re.search(pattern, low) is not None


def _hit_zh(terms: Tuple[str, ...], text: str) -> bool:
    return any(t in text for t in terms)


# "产品经理 / Product Manager" 里的"经理"是岗位名的一部分，不是管理职级；
# 同理 "产品专家" 不是 "首席"。识别职级时先把这些 PM 说法挖掉再判。
_PM_PHRASE = re.compile(
    r"(产品经理|高级产品经理|资深产品经理|产品策划|产品负责人|产品专家|"
    r"product\s*manager|product\s*owner|senior\s*product\s*manager|"
    r"principal\s*product\s*manager|product\s*lead)", re.I)


def _strip_pm(title: str) -> str:
    return _PM_PHRASE.sub(" ", title or "")


def _first_level(low_title: str, low_text: str) -> Tuple[str, str]:
    """先看标题，标题没有级别词再看正文。返回 (key, 中文标签)。"""
    t = _strip_pm(low_title)
    for key, pat, zh, _rank, label in _LEVELS:
        if _hit_en(pat, t) or _hit_zh(zh, t):
            return key, label
    for key, pat, zh, _rank, label in _LEVELS:
        if _hit_en(pat, low_text) or _hit_zh(zh, low_text):
            return key, label
    return "", ""


def _first(table, low_title: str, low_text: str) -> Tuple[str, str]:
    for key, pat, zh, label in table:
        if _hit_en(pat, low_title) or _hit_zh(zh, low_title):
            return key, label
    for key, pat, zh, label in table:
        if _hit_en(pat, low_text) or _hit_zh(zh, low_text):
            return key, label
    return "", ""


def functions(low_title: str, low_text: str = "") -> List[str]:
    """职能细分，可能多个（UX/UI Designer → ["ui", "ux"]），按词表顺序。"""
    out: List[str] = []
    for key, pat, zh, _label in _FUNCS:
        if _hit_en(pat, low_title) or _hit_zh(zh, low_title):
            out.append(key)
            continue
        if low_text and (_hit_en(pat, low_text) or _hit_zh(zh, low_text)):
            out.append(key)
    return out


def extract(title: str, text: str = "", location: str = "") -> Dict:
    """岗位标题/正文/地点 → 结构化属性。

    返回:
        {
          "level": "senior"|"manager"|...,   # 空串表示没识别出职级
          "level_zh": "资深",
          "level_rank": 3,                    # 用于排序，0=实习 6=总监
          "employment": "full_time"|...,
          "employment_zh": "全职",
          "work_mode": "remote"|"hybrid"|"onsite"|"",
          "work_mode_zh": "远程",
          "functions": ["ui", "ux"],
          "functions_zh": ["UI", "UX"],
          "language": ["english"],
          "language_zh": ["英语要求"],
          "keys": ["level:senior", "func:ui", ...],   # 给 score 查权重表用
          "tags": ["资深", "UI", "UX", "英语要求"],    # 给播报显示用
        }
    """
    t = title or ""
    low_title = t.lower()
    low_text = (text or "").lower()
    # 地点也参与"远程"判断：Workday 常把 Remote 写在地点里而不写标题
    low_loc = (location or "").lower()

    level, level_zh = _first_level(low_title, low_text)
    rank = next((r for k, _p, _z, r, _l in _LEVELS if k == level), 2)

    emp, emp_zh = _first(_EMPLOYMENT, low_title, low_text)
    mode, mode_zh = _first(_WORK_MODE, low_title, low_text)
    if not mode:
        mode, mode_zh = _first(_WORK_MODE, low_loc, "")
    lang, lang_zh = _first(_LANGUAGE, low_title, low_text)

    funcs = functions(low_title, low_text)
    funcs_zh = [LABELS.get(f"func:{f}", f) for f in funcs]

    keys: List[str] = []
    if level:
        keys.append(f"level:{level}")
    if emp:
        keys.append(f"employment:{emp}")
    if mode:
        keys.append(f"mode:{mode}")
    keys.extend(f"func:{f}" for f in funcs)
    if lang:
        keys.append(f"lang:{lang}")

    tags: List[str] = []
    for x in (level_zh,):
        if x:
            tags.append(x)
    if mode_zh:
        tags.append(mode_zh)
    tags.extend(funcs_zh[:2])
    if lang_zh:
        tags.append(lang_zh)

    return {
        "level": level, "level_zh": level_zh, "level_rank": rank,
        "employment": emp, "employment_zh": emp_zh,
        "work_mode": mode, "work_mode_zh": mode_zh,
        "functions": funcs, "functions_zh": funcs_zh,
        "language": [lang] if lang else [], "language_zh": [lang_zh] if lang_zh else [],
        "keys": keys,
        "tags": list(dict.fromkeys(tags)),
    }


# --------------------------------------------------------------------------- #
# 排序辅助：同一批岗位里，"资深岗"应该排在"初级岗"前面
# --------------------------------------------------------------------------- #
def sort_rank(attrs: Optional[Dict]) -> int:
    return int((attrs or {}).get("level_rank") or 2)
