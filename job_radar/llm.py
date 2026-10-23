"""可选的大模型摘要层（纯标准库，OpenAI 兼容端点）。

定位很克制——**只做「压缩 + 翻译」，不做「生成」**。

播报里最容易翻车的就是这一环：大模型很擅长把一段英文 JD 写成"负责核心产品的
体验设计，与优秀团队一起打造行业领先的解决方案"，读起来顺，但原 JD 里根本没
说这些。这类内容一旦进了每日播报，整份报告的可信度就没了。

所以这里的约束是硬编码的：

1. Prompt 里写死「只能用原文出现的信息」「原文没写就省略」「禁止套话」；
2. 输出回来后，把里面每个数字跟原文数字集合做交叉校验，对不上就丢掉该条；
3. 字数上限（一句话 ≤ 34 字，每条 ≤ 40 字），逼它只留主干；
4. 出现营销套话黑名单词，丢掉该条；
5. **任何环节失败都返回 None**，上层自动降级到 `jd_extract` 的规则版。
   宁可读起来朴素，也不能让播报挂掉或者出错。

配置（三个都是环境变量 / GitHub Secrets，全可选）：
    LLM_API_KEY     必填。没有就完全跳过本模块，走规则版
    LLM_BASE_URL    默认智谱 https://open.bigmodel.cn/api/paas/v4
                    （GLM-4-Flash 永久免费、OpenAI 兼容；也可换硅基流动
                     https://api.siliconflow.cn/v1 等）
    LLM_MODEL       默认 glm-4-flash

缓存 `data/ai_cache.json`：key = dedup_key + jd_hash + model。
存量兜底每天推同样的岗位，不缓存的话天天白烧 token。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from typing import Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_PATH = os.path.join(ROOT, "data", "ai_cache.json")

DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"   # 智谱，GLM-4-Flash 永久免费
DEFAULT_MODEL = "glm-4-flash"
TIMEOUT = 40
MAX_JD_CHARS = 6000

# --------------------------------------------------------------------------- #
# Prompt：约束写死在这里，不接受调用方覆盖
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = """你是招聘信息编辑，任务是把公司官网的职位描述压缩成中文要点。

绝对规则，违反任何一条都算失败：
1. 只能使用原文出现过的信息。禁止推测公司规模、薪资、团队人数、融资情况、发展前景。
2. 年限、学历、语言要求必须与原文数字一致；原文没写就不写。
3. 原文没提到的内容一律省略。宁可少写一条，也不要补一句像样的话。
4. 禁止使用这些套话：有竞争力的薪资、广阔的发展空间、优秀团队、行业领先、
   核心业务、快速成长、扁平化管理、五险一金、弹性工作。
5. 每条要点直说做什么 / 要什么，不要"负责……相关工作"这种空话。

只输出 JSON，不要 markdown 代码块，不要解释：
{"one_liner": "一句话说这个岗位具体做什么，不超过 30 字",
 "duty": ["职责1", "职责2", "职责3"],
 "must": ["硬性要求1", "硬性要求2", "硬性要求3"]}

duty 最多 3 条，must 最多 3 条，每条不超过 32 字。中文输出。"""

# 营销套话黑名单：出现即丢弃该条
_BANNED = [
    "有竞争力的薪资", "有竞争力的薪酬", "广阔的发展空间", "广阔发展空间",
    "优秀团队", "行业领先", "核心业务", "快速成长", "扁平化管理",
    "五险一金", "弹性工作", "良好的晋升", "充满活力", "行业头部",
    "共同成长", "赋能", "闭环", "抓手", "一站式解决方案",
]

_NUM = re.compile(r"\d+(?:\.\d+)?")
MAX_ONE_LINER = 34
MAX_ITEM = 42


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def config() -> Dict[str, str]:
    key = (os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()
    return {
        "key": key,
        "base": (os.getenv("LLM_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/"),
        "model": (os.getenv("LLM_MODEL") or DEFAULT_MODEL).strip(),
    }


def available() -> bool:
    """有没有配 key。没有就整层跳过，不报错。"""
    return bool(config()["key"])


# --------------------------------------------------------------------------- #
# 缓存
# --------------------------------------------------------------------------- #
def _load_cache() -> Dict:
    if not os.path.exists(CACHE_PATH):
        return {"version": 1, "entries": {}}
    try:
        d = json.load(open(CACHE_PATH, encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "entries": {}}
    if not isinstance(d.get("entries"), dict):
        d["entries"] = {}
    d.setdefault("version", 1)
    return d


def _save_cache(cache: Dict) -> None:
    cache["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
        f.write("\n")


def cache_key(dedup_key: str, jd_hash: str, model: str) -> str:
    raw = f"{dedup_key}|{jd_hash}|{model}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


# --------------------------------------------------------------------------- #
# 调用
# --------------------------------------------------------------------------- #
def _chat(messages: List[Dict[str, str]], cfg: Dict[str, str]) -> Optional[str]:
    body = json.dumps({
        "model": cfg["model"],
        "messages": messages,
        "temperature": 0.1,          # 要的是稳定复述，不是创意
        "max_tokens": 400,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{cfg['base']}/chat/completions", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg['key']}"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            d = json.loads(resp.read().decode("utf-8", "ignore"))
        return (d.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError,
            KeyError, IndexError, TimeoutError):
        return None


# --------------------------------------------------------------------------- #
# 校验：这是本模块最重要的部分
# --------------------------------------------------------------------------- #
def _numbers(text: str) -> set:
    return {n.rstrip("0").rstrip(".") if "." in n else n for n in _NUM.findall(text or "")}


def _ok_item(s: str, source_nums: set) -> bool:
    """单条要点是否可信：长度合规、无套话、数字都能在原文找到。"""
    s = (s or "").strip()
    if not (4 <= len(s) <= MAX_ITEM):
        return False
    if any(b in s for b in _BANNED):
        return False
    # 输出里的每个数字都必须在原文出现过，否则就是编的
    for n in _NUM.findall(s):
        if n not in source_nums and n.rstrip("0").rstrip(".") not in source_nums:
            return False
    return True


def validate(payload: Dict, source_text: str) -> Optional[Dict]:
    """把模型输出过一遍硬校验。不可信的部分一律丢掉，全空则返回 None。"""
    if not isinstance(payload, dict):
        return None
    src = _numbers(source_text)
    one = str(payload.get("one_liner") or "").strip()
    one_ok = bool(one) and len(one) <= MAX_ONE_LINER and _ok_item(one, src)

    out = {"one_liner": one if one_ok else "", "duty": [], "must": []}
    for field, cap in (("duty", 3), ("must", 3)):
        for item in (payload.get(field) or []):
            s = str(item or "").strip()
            if _ok_item(s, src):
                out[field].append(s)
            if len(out[field]) >= cap:
                break

    if not out["one_liner"] and not out["duty"] and not out["must"]:
        return None
    return out


# --------------------------------------------------------------------------- #
# 对外入口
# --------------------------------------------------------------------------- #
def summarize(dedup_key: str, jd_hash: str, title: str, jd_text: str,
              use_cache: bool = True) -> Optional[Dict]:
    """把一条 JD 压成中文要点。不可用/失败/校验不过 → None（上层降级）。

    返回 {"one_liner": str, "duty": [str], "must": [str], "model": str}
    """
    cfg = config()
    if not cfg["key"] or not (jd_text or "").strip():
        return None

    ck = cache_key(dedup_key, jd_hash, cfg["model"])
    cache = _load_cache()
    if use_cache and ck in cache["entries"]:
        hit = cache["entries"][ck]
        return dict(hit, model=cfg["model"], cached=True)

    user = (f"岗位标题：{title}\n\n职位描述原文：\n{jd_text[:MAX_JD_CHARS]}")
    content = _chat([{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": user}], cfg)
    if not content:
        return None
    try:
        payload = json.loads(content.strip().strip("`").replace("json\n", "", 1))
    except ValueError:
        return None

    result = validate(payload, jd_text)
    if not result:
        return None
    result["model"] = cfg["model"]
    cache["entries"][ck] = dict(result)
    _save_cache(cache)
    return result
