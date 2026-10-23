"""岗位方向与雇主层级规则。

这里集中维护会频繁变化的"求职方向"口径：
- UX / UI / 交互 / 产品设计（本仓库主方向）
- 用户研究 / 体验策略
- 雇主层级（外企 / 大厂 / 硬科技 / 央国企）

score.py 只负责把这些规则组装成最终分数，避免评分主流程变成关键词仓库。
"""
from __future__ import annotations

import re
from typing import List, Tuple

# 短英文词必须词边界匹配："AIx"/"Linux"/"Nuxeo" 里都含 "ux"，子串匹配会大量误判
_SHORT_TOK = re.compile(r"^[a-z0-9/+&. -]{1,3}$")
_tok_cache: dict = {}


def _tok_hit(token: str, text: str) -> bool:
    """token 是否在 text（已 lower）中作为独立片段出现。"""
    t = (token or "").lower()
    if not t:
        return False
    if not _SHORT_TOK.match(t):
        return t in text
    pat = _tok_cache.get(t)
    if pat is None:
        pat = re.compile(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])")
        _tok_cache[t] = pat
    return pat.search(text) is not None


def _any_tok(tokens, text: str) -> bool:
    return any(_tok_hit(k, text) for k in tokens)

# ---------------------------------------------------------------------------
# 硬门槛：标题必须命中以下"强设计信号"之一，才允许进入推送候选。
# 注意：这里**故意不放裸词 "design" / "设计"**——
#   "Package Design Electrical Engineer"（封装设计）、"Fund Administration"（JD 里
#   偶尔出现 design 字样）都会被裸词误放进来。强信号必须是设计职能本身。
# ---------------------------------------------------------------------------
STRONG_DESIGN_TITLE = (
    # 英文
    "ux", "ui designer", "ui/ux", "ui设计", "user experience", "interaction design",
    "product design", "product designer", "visual design", "visual designer",
    "experience design", "experience designer", "service design", "service designer",
    "design lead", "design manager", "design director", "design strategist",
    "design system", "creative design", "information architect", "ux researcher",
    "user researcher", "usability", "hci", "human-computer",
    # 外企常见但以前漏掉的设计岗（英语无障碍后这些都能投，不能再当非设计岗挡掉）
    "content design", "content designer", "ux writer", "ux writing",
    "design ops", "design operations", "design program",
    "accessibility design", "accessible design",
    "motion design", "motion designer", "brand design", "brand designer",
    "conversational design", "voice design", "localization design",
    # 中文
    "交互设计", "交互设计师", "用户体验", "体验设计", "体验设计师", "用户研究",
    "产品设计", "产品设计师", "视觉设计", "视觉设计师", "界面设计", "设计经理",
    "设计主管", "设计负责人", "设计专家", "设计总监", "设计策略",
    "内容设计", "设计运营", "动效设计", "品牌设计", "无障碍设计", "本地化设计",
)

# 标题级"对口信号"：用于 exclude_roles 的豁免判断（比硬门槛宽，含体验/创意类）
TARGET_ROLE_SIGNAL = STRONG_DESIGN_TITLE + (
    "设计", "designer", "design", "creative", "体验", "prototyp", "figma",
)

# ---------------------------------------------------------------------------
# 方向信号（细分标签）
# ---------------------------------------------------------------------------
UX_SIGNAL = (
    "ux", "user experience", "用户体验", "体验设计", "体验设计师", "交互", "interaction",
    "usability", "可用性", "user research", "用户研究", "ux research", "信息架构",
    "information architect", "journey", "用户旅程", "persona", "用户画像", "服务设计",
    "service design", "体验策略", "experience design", "digital experience",
)
UI_SIGNAL = (
    "ui", "interface", "界面", "视觉设计", "visual design", "视觉", "design system",
    "设计系统", "设计规范", "组件库", "component library", "design token", "图标", "icon",
    "品牌视觉", "brand design",
)
PRODUCT_DESIGN_SIGNAL = (
    "product design", "产品设计", "产品设计师", "product designer", "design lead",
    "design manager", "设计经理", "设计主管", "设计负责人", "设计专家", "资深设计",
    "creative design", "design strategist", "设计策略",
)
RESEARCH_SIGNAL = ("用户研究", "user research", "ux research", "可用性测试", "usability testing",
                   "调研", "问卷", "访谈", "persona", "用户画像")
# 强设计工具/方法词：即使标题只有 "Designer"，正文命中这些也判为设计岗
DESIGN_TOOL_SIGNAL = (
    "figma", "sketch", "axure", "invision", "miro", "adobe xd", "principle",
    "design system", "设计系统", "设计规范", "组件库", "prototype", "原型",
    "wireframe", "线框", "交互稿", "高保真", "视觉稿", "切图", "设计走查",
    "usability testing", "可用性测试", "design thinking", "设计思维",
)

# 非设计（同名词误命中）：出现在标题里且没有 TARGET_ROLE_SIGNAL 时重罚
NON_DESIGN_TITLE = (
    "平面设计", "美工", "美编", "包装设计", "装帧", "排版", "印前", "印刷",
    "室内设计", "家装", "软装", "硬装", "空间设计", "展陈", "陈列", "门店设计",
    "建筑设计", "土木", "结构设计", "暖通", "给排水", "施工图", "景观设计", "园林",
    "工业设计", "模具设计", "结构工程师", "机械设计", "机械工程师",
    "服装设计", "面料", "鞋类设计", "珠宝设计", "玩具设计",
    "cad", "solidworks", "pro/e", "ug设计",
    "游戏原画", "原画", "角色建模", "3d建模", "场景建模", "动画设计", "特效设计",
    "剪辑", "视频", "摄影", "摄像", "后期", "编导", "主播",
    "电气", "硬件", "嵌入式", "售前", "售后", "解决方案工程师",
    "采购", "人力资源", "hrbp", "招聘", "行政", "法务", "会计", "审计", "税务",
    "销售代表", "销售专员", "客户经理", "招商", "外贸", "业务员", "跟单",
    "教师", "助教", "班主任", "护士", "临床", "药师", "医师",
    "检测", "化验", "监理", "测绘", "造价", "施工", "环评",
)

# ---------------------------------------------------------------------------
# 雇主层级
# ---------------------------------------------------------------------------
FOREIGN_KW = (
    "（中国）", "(中国)", "中国）有限公司", "外资", "外商独资", "中外合资", "合资",
    "inc.", "inc", "ltd", "limited", "gmbh", "co., ltd", "corporation", "corp.",
    "group", "holdings", "technologies", "solutions", "systems", "international",
)
# 已知外企品牌名（中文/英文），命中即判外企 —— 比靠 source_id 更可靠
FOREIGN_BRANDS = (
    "辉瑞", "pfizer", "道富", "state street", "阿斯利康", "astrazeneca", "罗氏", "roche",
    "强生", "johnson", "jnj", "飞利浦", "philips", "西门子", "siemens", "博世", "bosch",
    "施耐德", "schneider", "3m", "英特尔", "intel", "恩智浦", "nxp", "雅培", "abbott",
    "应用材料", "applied materials", "科磊", "kla", "英伟达", "nvidia", "戴尔", "dell",
    "惠普", "hp", "思科", "cisco", "诺基亚", "nokia", "爱立信", "ericsson", "索尼", "sony",
    "松下", "panasonic", "日立", "hitachi", "三菱", "mitsubishi", "东芝", "toshiba",
    "通用电气", "ge ", "ge healthcare", "霍尼韦尔", "honeywell", "abb", "艾默生", "emerson",
    "丹佛斯", "danfoss", "采埃孚", "zf", "大陆", "continental", "李尔", "lear",
    "安波福", "aptiv", "法雷奥", "valeo", "麦格纳", "magna", "佛吉亚", "forvia",
    "宜家", "ikea", "耐克", "nike", "阿迪达斯", "adidas", "欧莱雅", "l'oreal",
    "联合利华", "unilever", "宝洁", "p&g", "雀巢", "nestle", "百威", "budweiser",
    "可口可乐", "coca-cola", "百事", "pepsi", "麦当劳", "mcdonald", "星巴克", "starbucks",
    "汇丰", "hsbc", "渣打", "standard chartered", "花旗", "citi", "摩根", "jpmorgan",
    "高盛", "goldman", "瑞银", "ubs", "安永", "ey", "德勤", "deloitte", "普华永道", "pwc",
    "毕马威", "kpmg", "埃森哲", "accenture", "麦肯锡", "mckinsey", "bcg", "贝恩", "bain",
    "尼尔森", "nielsen", "益普索", "ipsos", "凯度", "kantar",
    "thoughtworks", "zoom", "shopee", "grab", "wise", "agoda", "booking",
    "canonical", "notion", "figma", "autodesk", "unity", "epic games",
    "联合技术", "otis", "蒂森克虏伯", "thyssenkrupp", "林德", "linde", "液化空气", "air liquide",
    "巴斯夫", "basf", "拜耳", "bayer", "默沙东", "merck", "诺华", "novartis",
    "赛诺菲", "sanofi", "葛兰素史克", "gsk", "礼来", "lilly", "安进", "amgen",
    "美敦力", "medtronic", "波士顿科学", "boston scientific", "丹纳赫", "danaher",
    "赛默飞", "thermo fisher", "安捷伦", "agilent", "沃特世", "waters",
    "施贵宝", "bms", "艾伯维", "abbvie", "武田", "takeda", "卫材", "eisai",
)

BIGTECH = ("腾讯", "字节", "抖音", "阿里", "蚂蚁", "美团", "京东", "百度", "快手",
           "网易", "滴滴", "拼多多", "小米", "华为", "哔哩哔哩", "小红书", "携程",
           "微博", "vivo", "oppo", "顺丰", "大疆", "海康", "大华", "宇视", "萤石")
HARDTECH_CO = ("商汤", "地平线", "旷视", "云从", "依图", "寒武纪", "黑芝麻", "壁仞",
               "摩尔线程", "燧原", "比亚迪", "宁德时代", "蔚来", "理想汽车", "小鹏",
               "中芯", "海光", "华大", "涂鸦")
SOE_KW = ("中国", "国家电网", "南方电网", "中核", "中船", "中航", "航空工业", "航发",
          "中国电子", "中国电科", "中电科", "中铁", "中建", "中交", "中粮", "中化",
          "中石油", "中石化", "中海油", "华能", "大唐", "国家能源", "国电投",
          "中国移动", "中国电信", "中国联通", "国投", "中信", "华润", "招商局",
          "中广核", "中国华电", "中国能建", "中国电建", "中煤", "中国邮政")
HARD_IND = ("半导体/电子", "航空航天/军工", "新材料", "汽车/新能源车", "能源/电力/石化")


def has_target_role_signal(title: str) -> bool:
    return _any_tok(TARGET_ROLE_SIGNAL, (title or "").lower())


def is_design_role(title: str, jd_text: str = "") -> bool:
    """硬门槛：判断这是不是一个真正的 UX/交互/产品设计岗。

    判定顺序：
    1. 标题命中 STRONG_DESIGN_TITLE  → 是（最可靠）
    2. 标题命中 NON_DESIGN_TITLE     → 否（平面/室内/工业/机械…直接否掉）
    3. 标题含 "design/设计" 且正文有强设计工具/方法词 → 是
    4. 其余一律否

    这是**门槛不是加分项**：宁可漏，也不要把 "Package Design Electrical Engineer"
    这种硬件封装岗、或者 JD 里偶然提到 design 的岗位推进播报。
    """
    title_low = (title or "").lower()
    text_low = f"{title or ''} {jd_text or ''}".lower()

    # 1. 强信号直接通过（词边界匹配，"AIx"/"Linux" 不会被当成 ux）
    if _any_tok(STRONG_DESIGN_TITLE, title_low):
        return True

    # 2. 命中非设计同名词，直接否掉（即使正文有 figma 也不放行）
    if any(k.lower() in title_low for k in NON_DESIGN_TITLE):
        return False

    # 3. 裸 design 标题靠正文确认
    if "design" in title_low or "设计" in title_low or "designer" in title_low:
        hits = sum(1 for k in DESIGN_TOOL_SIGNAL if k in text_low)
        return hits >= 2

    return False


def is_foreign_company(company: str, sid: str = "") -> bool:
    """判定是否外企：优先品牌白名单，其次信源前缀，最后公司名后缀特征。"""
    c = (company or "").lower()
    if not c:
        return sid.startswith(("wd-", "gh-", "lever-", "ashby-"))
    if any(b in c for b in FOREIGN_BRANDS):
        return True
    if sid.startswith(("wd-", "gh-", "lever-", "ashby-")):
        return True
    # 有英文公司名 + 外企后缀特征（Inc./Ltd./Corporation/Group…）也算
    has_latin = any("a" <= ch <= "z" for ch in c)
    if has_latin and any(k in c for k in FOREIGN_KW):
        return True
    if any(k in c for k in ("（中国）", "(中国)", "外商独资", "外资企业", "中外合资")):
        return True
    return False


def role_signal_score(title: str, jd_text: str) -> Tuple[int, List[str]]:
    """设计岗方向打分：标题权重高于正文。"""
    title_low = (title or "").lower()
    text_low = f"{title or ''} {jd_text or ''}".lower()
    score = 0
    tags: List[str] = []

    t_ux = _any_tok(UX_SIGNAL, title_low)
    t_ui = _any_tok(UI_SIGNAL, title_low)
    t_pd = _any_tok(PRODUCT_DESIGN_SIGNAL, title_low)
    t_rs = _any_tok(RESEARCH_SIGNAL, title_low)

    if t_ux:
        score += 26
        tags.append("UX")
    if t_ui:
        score += 22
        tags.append("UI")
    if t_pd:
        score += 24
        tags.append("产品设计")
    if t_rs:
        score += 20
        tags.append("用户研究")

    # 只有裸 design 标题（未命中任何细分信号）时才走这里：
    # 正文必须有 >=2 个设计工具/方法词才给分，否则视为非设计岗，给 0。
    if not (t_ux or t_ui or t_pd or t_rs) and ("design" in title_low or "设计" in title_low
                                               or "designer" in title_low):
        hits = sum(1 for k in DESIGN_TOOL_SIGNAL if k in text_low)
        if hits >= 2:
            score += 18
            tags.append("设计")
        # 否则不加分、不打标——交给 is_design_role() 门槛挡掉

    # 正文补充（标题未命中时给较弱加分，避免 JD 堆词虚高）
    if not tags and _any_tok(UX_SIGNAL + UI_SIGNAL, title_low):
        score += 10
        tags.append("体验相关")

    # 职级/雇佣形式（资深、带团队、实习）**不在这里算**：
    # 已统一到 job_attrs.py + profile["attr_bonus"]，两边都算会重复加分。
    return score, list(dict.fromkeys(tags))


def employer_tier(company: str, industry: str, sid: str) -> Tuple[str, int]:
    """据公司名(+行业/信源)判雇主层级 → (标签, 加分)。外企是本仓库最看重的维度。"""
    c = company or ""
    low = c.lower()
    if is_foreign_company(c, sid):
        return "外企", 34
    if any(b in c for b in BIGTECH):
        return "大厂", 22
    if any(b in c or b in low for b in HARDTECH_CO):
        return "硬科技", 20
    if any(k in c for k in SOE_KW) or sid in ("gov-sasac", "gov-qyzp", "cn-iguopin"):
        return "央国企", 18
    if industry in HARD_IND and not sid.startswith(("edu-", "gov-")):
        return "硬科技", 14
    return "", 0
