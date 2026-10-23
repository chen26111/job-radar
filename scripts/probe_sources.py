"""批量探测候选信源，只把真能产出「杭州 + 设计岗」的留下来。

用途：手工往 config/sources.csv 加信源前先验证，避免加一堆空壳进来。
判定口径（事实，不猜）：
    total    该信源返回的岗位总数
    cn       地点/正文含中国城市关键词的条数
    hz       地点明确含 杭州 / Hangzhou 的条数
    design   过 is_design_role 硬门槛的条数（中文英文都算）
    hz_design 同时满足 hz + design —— 这个才是我们要的

用法：
    python scripts/probe_sources.py                 # 探测全部候选
    python scripts/probe_sources.py --only workday  # 只探测某一类
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from job_radar.adapters import get_adapter            # noqa: E402
from job_radar.role_rules import is_design_role       # noqa: E402

HZ = ("杭州", "hangzhou")
CN = ("杭州", "hangzhou", "上海", "shanghai", "北京", "beijing", "深圳", "shenzhen",
      "苏州", "suzhou", "广州", "guangzhou", "成都", "chengdu", "中国", "china")

# --------------------------------------------------------------------------- #
# 候选池：按「外企优先，国内大厂补量」排列，全部是公开接口（无需登录）
# --------------------------------------------------------------------------- #

# Workday：tenant 可猜，site 常见几种，逐个试
WORKDAY_TENANTS = [
    "nike", "starbucks", "cocacola", "ge", "siemens", "bosch", "honeywell",
    "abbott", "gsk", "sanofi", "novartis", "roche", "astrazeneca", "merck",
    "unilever", "philips", "sap", "siemenshealthineers", "zeiss", "agilent",
    "thermofisher", "keysight", "micron", "nvidia", "qualcomm", "nxp",
    "infineon", "stmicro", "adidas", "decathlon", "ikea", "lego", "sony",
    "panasonic", "canon", "fujitsu", "rakuten", "cisco", "oracle", "ibm",
    "schneider", "abb", "basf", "bayer", "dupont", "dow", "jnj", "bms",
    "novonordisk", "olympus", "terumo", "stryker", "edwards", "baxter",
]
WORKDAY_SITES = ["External", "Careers", "Jobs", "External_Career_Site", "Recruiting"]

GREENHOUSE = [
    "databricks", "snowflake", "confluent", "elastic", "mongodb", "hashicorp",
    "grafana", "cloudflare", "thoughtworks", "docker", "zscaler", "crowdstrike",
    "paloaltonetworks", "nutanix", "netskope", "purestorage", "cockroachlabs",
    "temporal", "sourcegraph", "sentry", "influxdata", "airtable", "miro",
    "gitlab", "sumologic", "okta", "splunk", "twilio", "dropbox",
]

LEVER = ["plaid", "brex", "mixpanel", "amplitude", "samsara", "toast",
         "affirm", "chime", "discourse", "scaleapi", "netlify"]

ASHBY = ["ramp", "vanta", "notion", "linear", "anthropic", "perplexityai",
         "cursor", "elevenlabs", "cohere", "scale"]

# 已有 adapter、endpoint 不起作用的国内源（直接全量 feed）
DOMESTIC = [
    ("字节跳动", "bytedance", ""),
    ("腾讯", "tencent", ""),
    ("美团", "meituan", ""),
    ("网易", "netease", ""),
    ("京东", "jd", ""),
    ("华为", "huawei", ""),
    ("实习僧", "shixiseng", ""),
    ("牛客", "nowcoder", ""),
]


def _head(url: str) -> bool:
    try:
        req = urllib.request.Request(url, method="GET", headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) job-radar/1.0",
            "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return 200 <= r.status < 300
    except Exception:                                     # noqa: BLE001
        return False


def probe_workday(tenant: str):
    """试出该 tenant 可用的 site，返回 (endpoint, jobs) 或 None。"""
    for site in WORKDAY_SITES:
        host = f"{tenant}.wd1.myworkdayjobs.com"
        url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
        try:
            req = urllib.request.Request(url, method="POST", data=b"{}", headers={
                "Content-Type": "application/json", "Accept": "application/json",
                "Referer": f"https://{host}/{site}",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) job-radar/1.0"})
            with urllib.request.urlopen(req, timeout=25) as r:
                data = json.loads(r.read().decode("utf-8", "ignore"))
            if isinstance(data.get("jobPostings"), list):
                return f"{host}|{tenant}|{site}"
        except Exception:                                 # noqa: BLE001
            continue
    # 有的公司在 wd3 / wd5 上
    for idx in (3, 5, 2, 4):
        host = f"{tenant}.wd{idx}.myworkdayjobs.com"
        for site in WORKDAY_SITES:
            url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
            try:
                req = urllib.request.Request(url, method="POST", data=b"{}", headers={
                    "Content-Type": "application/json", "Accept": "application/json",
                    "Referer": f"https://{host}/{site}",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) job-radar/1.0"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    data = json.loads(r.read().decode("utf-8", "ignore"))
                if isinstance(data.get("jobPostings"), list):
                    return f"{host}|{tenant}|{site}"
            except Exception:                             # noqa: BLE001
                continue
    return None


def count_jobs(adapter: str, endpoint: str):
    """返回 (total, cn, hz, design, hz_design, err)。"""
    try:
        fn = get_adapter(adapter)
        jobs = fn(endpoint) or []
    except Exception as e:                                # noqa: BLE001
        return (0, 0, 0, 0, 0, f"{type(e).__name__}: {e}"[:40])
    total = len(jobs)
    cn = hz = design = hz_design = 0
    for j in jobs:
        # 适配器返回的是 RawJob dataclass，不是 dict
        def g(key):
            if isinstance(j, dict):
                return j.get(key) or ""
            return getattr(j, key, "") or ""
        loc = str(g("location")).lower()
        text = f"{g('title')} {g('jd_text')}".lower()
        is_hz = any(k.lower() in loc for k in HZ) or "hangzhou" in text
        is_cn = any(k.lower() in loc for k in CN) or any(k.lower() in text for k in CN)
        is_des = is_design_role(str(g("title")), str(g("jd_text")))
        cn += 1 if is_cn else 0
        hz += 1 if is_hz else 0
        design += 1 if is_des else 0
        hz_design += 1 if (is_hz and is_des) else 0
    return (total, cn, hz, design, hz_design, "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["workday", "ats", "domestic", "all"], default="all")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    tasks = []
    if args.only in ("workday", "all"):
        tasks += [("workday", t, None) for t in WORKDAY_TENANTS]
    if args.only in ("ats", "all"):
        tasks += [("greenhouse", t, t) for t in GREENHOUSE]
        tasks += [("lever", t, t) for t in LEVER]
        tasks += [("ashby", t, t) for t in ASHBY]
    if args.only in ("domestic", "all"):
        tasks += [(a, n, e) for (n, a, e) in DOMESTIC]

    print(f"探测 {len(tasks)} 个候选，并发 {args.workers} —— 只关心 hz_design 列的数字\n")

    def run(t):
        adapter, name, endpoint = t
        if adapter == "workday":
            ep = probe_workday(name)
            if not ep:
                return (name, adapter, "", 0, 0, 0, 0, 0, "无可用 site")
            endpoint = ep
        total, cn, hz, design, hzd, err = count_jobs(adapter, endpoint)
        return (name, adapter, endpoint or "", total, cn, hz, design, hzd, err)

    rows = []
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for r in ex.map(run, tasks):
            rows.append(r)
            if r[7]:      # 有产出才打印，减少噪音
                print(f"  ✓ {r[0]:16} {r[1]:10} 总{r[3]:>5} 中国{r[4]:>4} 杭州{r[5]:>4} "
                      f"设计{r[6]:>4} → 杭州设计 {r[7]}")

    rows.sort(key=lambda r: (-r[7], -r[6], -r[3]))
    print("\n=== 汇总（按「杭州设计岗」数量排序）===")
    print(f"{'名称':18}{'类型':12}{'总':>6}{'中国':>6}{'杭州':>6}{'设计':>6}{'杭设':>6}  备注")
    for name, adapter, ep, total, cn, hz, design, hzd, err in rows:
        if total == 0 and not err:
            continue
        print(f"{name:18}{adapter:12}{total:>6}{cn:>6}{hz:>6}{design:>6}{hzd:>6}  {err or ep[:44]}")

    keep = [r for r in rows if r[7] > 0 or (r[1] == "workday" and r[5] > 0)]
    print(f"\n可入库候选（杭州设计>0，或 Workday 有杭州岗）：{len(keep)} 个")
    out = {
        "probed_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "keep": [{"name": r[0], "adapter": r[1], "endpoint": r[2],
                  "total": r[3], "cn": r[4], "hz": r[5], "design": r[6], "hz_design": r[7]}
                 for r in keep],
    }
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "probe_result.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"结果已写入 {path}")


if __name__ == "__main__":
    main()
