"""从 JD 里抽待遇（只认原文写明的数字，绝不猜）。

为什么单独一层：待遇是用户明确要求的排序维度之一，但它最容易造假——
所以这里只做「抽取 + 归一化」，不做任何估算、不补全、不换算币种以外的推断。

能处理的写法（都是实测 JD 里出现过的）：
    20-40K / 20k-40k / 20-40K·15薪 / 月薪15-25K / 15000-25000元
    15-25千 / 30-50万（年薪）/ $120,000 - $150,000 / 12k-18k·13薪
不能处理（返回 None，宁缺毋滥）：
    「薪资面议」「有竞争力的薪酬」「competitive salary」

输出统一成**人民币月薪区间**，便于排序比较：
    年薪 → /12；美元 → ×7.2（汇率写死并标注，避免联网取数导致结果不稳定）
"""
from __future__ import annotations

import re
from typing import Dict, Optional

USD_TO_CNY = 7.2

# 20-40K、15k-18k、20K-30K
_K = re.compile(
    r"(\d{1,3}(?:\.\d)?)\s*[-–~至到]\s*(\d{1,3}(?:\.\d)?)\s*[kK千]")
# 15000-25000元 / 15000-25000 /月
_YUAN = re.compile(
    r"(\d{4,6})\s*[-–~至到]\s*(\d{4,6})\s*(?:元|rmb|RMB|/月|每月)?")
# 30-50万（几乎都是年薪）
_WAN = re.compile(r"(\d{1,3}(?:\.\d)?)\s*[-–~至到]\s*(\d{1,3}(?:\.\d)?)\s*万")
# $120,000 - $150,000
_USD = re.compile(
    r"\$\s*([\d,]{5,9})\s*[-–~至到]\s*\$?\s*([\d,]{5,9})")

_NON_INFORMATIVE = ("面议", "competitive", "有竞争力", "negotiable", "depending on")


def _fmt(v: float) -> str:
    """月薪（元）→ '2.0万' 这类短写法，用于播报展示。"""
    if v >= 10000:
        s = f"{v / 10000:.1f}万"
        return s.replace(".0万", "万")
    return f"{int(round(v / 1000))}k"


def extract(text: str) -> Optional[Dict]:
    """从 JD 文本里抽薪资区间。抽不到返回 None。"""
    t = (text or "").strip()
    if not t:
        return None
    low_t = t.lower()
    if any(k in low_t for k in _NON_INFORMATIVE) and not _K.search(t):
        return None

    lo = hi = None
    kind = ""

    m = _K.search(t)
    if m:
        lo, hi = float(m.group(1)) * 1000, float(m.group(2)) * 1000
        kind = "月薪"
    if lo is None:
        m = _WAN.search(t)
        if m:
            lo, hi = float(m.group(1)) * 10000, float(m.group(2)) * 10000
            # 年薪写法（"30-50万"）→ 折月薪；若上下文写了"月薪"则不折
            if "月薪" not in t[:m.start() + 40]:
                lo, hi = lo / 12, hi / 12
                kind = "年薪折月"
            else:
                kind = "月薪"
    if lo is None:
        m = _YUAN.search(t)
        if m:
            lo, hi = float(m.group(1)), float(m.group(2))
            kind = "月薪"
    if lo is None:
        m = _USD.search(t)
        if m:
            lo = float(m.group(1).replace(",", "")) * USD_TO_CNY / 12
            hi = float(m.group(2).replace(",", "")) * USD_TO_CNY / 12
            kind = "美元年薪折月"

    if lo is None or hi is None or hi <= 0 or lo <= 0:
        return None
    if lo > hi:
        lo, hi = hi, lo
    # 明显不合理的（比如把"5-10人团队"当钱）直接判废
    if lo < 1000 or hi > 500000:
        return None

    return {
        "monthly_min": int(round(lo)),
        "monthly_max": int(round(hi)),
        "mid": int(round((lo + hi) / 2)),
        "kind": kind or "月薪",
        "text": f"{_fmt(lo)}-{_fmt(hi)}",
    }


def rank_bonus(sal: Optional[Dict], baseline: int = 20000) -> int:
    """待遇对排序的贡献。没有薪资数据时不加分也不扣分（外企 JD 常不写）。"""
    if not sal:
        return 0
    mid = int(sal.get("mid") or 0)
    if mid <= 0:
        return 0
    # 每高出基线 5000 加 2 分，封顶 +14；低于基线最多扣 6
    delta = int(round((mid - baseline) / 5000.0 * 2))
    return max(-6, min(delta, 14))
