#!/usr/bin/env python3
"""最小验证脚本（对应规划第 11 章"先验证"）。

目标：用最稳定的海外 ATS 信源，回答两个核心问题——
  1) 能不能稳定抓到新增岗位？
  2) 去重靠不靠谱？

它做三件事：
  A. 真实抓取 Greenhouse/Lever/Ashby，断言至少抓到岗位（验证抓取链路）。
  B. 用构造的"同岗不同名/跨源重复"样本断言去重命中（验证 dedup_key，离线、可复现）。
  C. 打印健康报告摘要（验证健康度闭环可观测）。

运行：python3 scripts/smoke_test.py
任意一步失败会以非零码退出，方便接进 CI / GitHub Actions。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from job_radar.dedup import dedup
from job_radar.models import Job
from job_radar.normalize import make_dedup_key
from job_radar.adapters.jsonld import parse_jobs
from job_radar import keyword_config, sync
from job_radar.adapters import list_adapters
from job_radar.quality_rules import LOW_QUALITY_TAGS, quality_tags

PASS, FAIL = "✅ PASS", "❌ FAIL"
failures = 0
# 自检写到临时目录，绝不覆盖正式的 data/（与真实同步隔离）
TMP_OUT = tempfile.mkdtemp(prefix="job_radar_smoke_")


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    print(f"{PASS if ok else FAIL}  {name}" + (f"  — {detail}" if detail else ""))
    if not ok:
        failures += 1


# ---------- B. 去重单元验证（离线、必过、可复现）----------
def test_dedup_offline() -> None:
    print("\n[B] 去重逻辑（离线构造样本）")

    def mk(company, title, loc, url, src="s1", pub=""):
        key = make_dedup_key(company, title, loc)
        return Job(job_id=f"{src}:{key}", dedup_key=key, source_id=src,
                   company_name=company, title=title, location=loc,
                   official_url=url, publish_time=pub)

    # 同岗不同名（噪声后缀 + 全角空格 + 城市后缀），应归一化为同一 key
    a = mk("字节跳动", "数据产品经理（社招）", "深圳市", "https://x.com/1", pub="2026-06-10")
    b = mk("字节跳动", "数据产品经理", "深圳", "https://x.com/2", pub="2026-06-01")
    check("同岗不同名归一化为同一 dedup_key", a.dedup_key == b.dedup_key,
          f"{a.dedup_key} == {b.dedup_key}")

    # 跨源重复 + 强去重：official_url 完全一致
    c = mk("Spotify", "Backend Engineer", "Remote", "https://x.com/1", src="s2")

    merged = dedup([a, b, c])
    # a/b 合并为 1（同 key），c 与 a 同 url 也合并 → 期望 1 条
    check("跨源/同名重复后只剩 1 条", len(merged) == 1, f"got {len(merged)}")
    if merged:
        m = merged[0]
        check("合并后保留更早 publish_time", m.publish_time == "2026-06-01", m.publish_time)
        check("重复出现次数被累计", m.seen_count == 3, f"seen_count={m.seen_count}")
        check("第二条链接进入 backup_url", bool(m.backup_url), m.backup_url)


# ---------- D. JSON-LD 解析器（离线、必过、可复现）----------
def test_jsonld_offline() -> None:
    print("\n[D] 通用 JobPosting JSON-LD 解析")
    sample = '''
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"JobPosting","title":"Data Scientist",
     "hiringOrganization":{"@type":"Organization","name":"Acme AI"},
     "datePosted":"2026-06-01","validThrough":"2026-09-01","employmentType":"FULL_TIME",
     "jobLocation":{"@type":"Place","address":{"@type":"PostalAddress",
       "addressLocality":"Shanghai","addressCountry":"CN"}},
     "baseSalary":{"@type":"MonetaryAmount","currency":"CNY","value":{
       "@type":"QuantitativeValue","minValue":30000,"maxValue":50000,"unitText":"MONTH"}},
     "identifier":{"@type":"PropertyValue","name":"Acme","value":"DS-001"}}
    </script>
    <script type="application/ld+json">
    {"@graph":[{"@type":"JobPosting","title":"Remote Engineer",
      "hiringOrganization":"Acme AI","jobLocationType":"TELECOMMUTE",
      "applicantLocationRequirements":{"@type":"Country","name":"China"}}]}
    </script>'''
    jobs = parse_jobs(sample, "https://x.com/job")
    check("解析出 2 个 JobPosting（含 @graph）", len(jobs) == 2, f"got {len(jobs)}")
    if jobs:
        a = jobs[0]
        check("title/公司正确", a.title == "Data Scientist" and a.company_name == "Acme AI")
        check("validThrough→deadline", a.deadline == "2026-09-01", a.deadline)
        check("地点解析", "Shanghai" in a.location, a.location)
        check("薪资区间解析", a.raw.get("salary") == "30000-50000 CNY/MONTH", a.raw.get("salary"))
        check("identifier 解析", a.raw.get("identifier") == "DS-001", a.raw.get("identifier"))
    if len(jobs) > 1:
        check("远程岗位识别", jobs[1].location.startswith("远程"), jobs[1].location)


# ---------- E. 配置化与质量规则（离线、必过、可复现）----------
def test_config_and_quality_offline() -> None:
    print("\n[E] 配置化补抓 / adapter 自动发现 / 质量降噪")
    kws = keyword_config.iguopin_keywords()
    check("国聘补抓关键词含 2027 周期词", any(k in kws for k in ("2027", "27届")), str(kws[:8]))
    check("国聘补抓关键词含算法/产品/决策方向", all(k in kws for k in ("算法", "AI产品", "战略分析")),
          str(kws[:12]))
    adapters = set(list_adapters())
    check("adapter 自动发现包含国聘", "iguopin" in adapters, str(sorted(adapters)[:8]))
    check("adapter 自动发现包含央企公告", "gov_notice" in adapters, str(sorted(adapters)[:8]))

    missing_deadline = Job(job_id="x", dedup_key="x", source_id="test",
                           company_name="Acme AI", title="数据科学实习生",
                           location="上海", official_url="https://example.com/job")
    tags, _ = quality_tags(missing_deadline)
    # 本仓库已删除"缺截止"标签：外企 Workday/Greenhouse 等 ATS 不提供 deadline 字段，
    # 若保留该标签会让所有外企岗位都被打标，等于把主方向全部误伤。
    check("无截止日期不再打「缺截止」标（外企 ATS 无此字段）",
          "缺截止" not in tags and not (set(tags) & LOW_QUALITY_TAGS), str(tags))

    low_signal = Job(job_id="y", dedup_key="y", source_id="test",
                     company_name="某人力资源管理有限公司", title="销售专员",
                     location="某县", official_url="")
    tags, _ = quality_tags(low_signal)
    check("明显低质岗位会进入默认隐藏标签", bool(set(tags) & LOW_QUALITY_TAGS), str(tags))


# ---------- F. JD 提炼层（离线夹具，必过、可复现）----------
FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "tests", "fixtures")


def _fixture(name: str) -> dict:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


def test_jd_extract_offline() -> None:
    print("\n[F] JD 提炼层（离线夹具，5 条真实 Workday JD）")
    from job_radar import jd_extract, llm

    fx = _fixture("pfizer-ui-designer.json")
    html = fx["jobDescription"]

    # 1) HTML → 纯文本：稳定、无标签残留、中文可读
    text = jd_extract.html_to_text(html)
    check("html_to_text 输出稳定（连跑两次一致）", text == jd_extract.html_to_text(html))
    check("html_to_text 无 HTML 标签残留", "<" not in text and ">" not in text,
          text[:40].replace("\n", " "))
    check("html_to_text 解出了中文正文", "岗位职责" in text or "负责" in text, text[:40])
    check("html_to_text 长度合理（>200 字符）", len(text) > 200, f"{len(text)} 字符")

    # 2) 结构化事实：数字必须与原文一致，不能编
    f = jd_extract.facts(text)
    check("facts 提取出经验年限「5 年以上」", f.get("years") == "5 年以上", str(f.get("years")))
    check("facts 提取出学历「本科」", f.get("education") == "本科", str(f.get("education")))

    # 3) 分段归位：职责段与要求段不能混
    s = jd_extract.summarize(html, fx["title"])
    check("summarize 认出有 JD", bool(s.get("has_jd")))
    check("summarize 认对语言 zh", s.get("lang") == "zh", str(s.get("lang")))
    check("职责/要求各抽到 2 条以上", len(s.get("duty") or []) >= 2 and len(s.get("must") or []) >= 2,
          f"duty={len(s.get('duty') or [])} must={len(s.get('must') or [])}")

    # 4) 标题与正文冲突要能识别（道富那条标题含 UX、正文是后端工程岗）
    trap = _fixture("statestreet-dev-ux.json")
    trap_sum = jd_extract.summarize(trap["jobDescription"], trap["title"])
    check("识别出「标题像设计岗、正文是工程岗」", bool(trap_sum.get("flag")),
          str(trap_sum.get("flag"))[:50])

    # 5) 无 LLM key 时必须静默降级，不能抛异常、不能阻塞播报
    saved = os.environ.pop("LLM_API_KEY", None)
    os.environ.pop("OPENAI_API_KEY", None)
    try:
        got = llm.summarize("smoke", "hash", fx["title"], text)
        check("无 LLM_API_KEY 时 llm.summarize() 返回 None 且不抛异常", got is None, str(got))
    except Exception as e:                                   # noqa: BLE001
        check("无 LLM_API_KEY 时 llm.summarize() 返回 None 且不抛异常", False,
              f"{type(e).__name__}: {e}")
    finally:
        if saved is not None:
            os.environ["LLM_API_KEY"] = saved


# ---------- G. 岗位属性层（离线，必过、可复现）----------
def test_job_attrs_offline() -> None:
    print("\n[G] 岗位属性识别与加权（职级/雇佣/办公方式/方向/语言）")
    from job_radar import job_attrs
    from job_radar.score import score_job

    a = job_attrs.extract("Senior Product Designer", "", "Hangzhou")
    check("识别「Senior」职级", a["level"] == "senior", a["level"])
    check("识别产品设计方向", "product" in a["functions"], str(a["functions"]))

    b = job_attrs.extract("Design Intern", "", "Hangzhou")
    check("识别实习岗（rank 最低）", b["level"] == "intern" and b["level_rank"] == 0,
          f'{b["level"]}/{b["level_rank"]}')

    c = job_attrs.extract("UX Writer, Payments", "", "Hangzhou")
    check("识别内容设计岗（以前会漏）", "content_design" in c["functions"], str(c["functions"]))

    d = job_attrs.extract("Product Designer", "", "Remote - China")
    check("从地点识别远程", d["work_mode"] == "remote", d["work_mode"])

    e = job_attrs.extract("Design Manager", "", "Hangzhou")
    check("经理级高于资深级", e["level_rank"] > a["level_rank"],
          f'{e["level"]}={e["level_rank"]} vs {a["level"]}={a["level_rank"]}')

    # 属性权重必须真的作用到分数上（配置里 intern 是强负分、senior 是加分）
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "config", "profiles.json"), encoding="utf-8") as f:
        prof = json.load(f)["ux-product-design"]

    def mk(title):
        key = make_dedup_key("Acme", title, "杭州")
        return Job(job_id=f"t:{key}", dedup_key=key, source_id="wd-acme",
                   company_name="Acme", title=title, location="杭州",
                   official_url="https://x.com/j", jd_text=title)

    s_sen = score_job(mk("Senior Product Designer"), prof).score
    s_mid = score_job(mk("Product Designer"), prof).score
    # 用 "UX Design Intern"（过得了设计岗门槛）才能真正测到属性权重，
    # 用 "Design Intern" 会被 is_design_role 直接拦成 0 分，测了个寂寞
    s_int = score_job(mk("UX Design Intern"), prof).score
    check("资深岗分数高于普通岗", s_sen > s_mid, f"{s_sen} vs {s_mid}")
    check("实习岗被属性权重压到推送阈值以下", s_int < 55, f"实习 {s_int}（资深 {s_sen}）")
    check("非设计岗仍然 0 分（属性加分不能突破硬门槛）",
          score_job(mk("Account Manager"), prof).score == 0)
    check("画像里有 candidate 能力块", bool(prof.get("candidate", {}).get("english")),
          str((prof.get("candidate") or {}).get("english")))


# ---------- A. 真实抓取验证（网络，允许个别源失败）----------
def test_live_fetch() -> None:
    print("\n[A] 真实抓取（外企 Workday，网络）")
    # 本仓库信源全是外企官网 Workday（config/sources.csv），
    # 原先断言 greenhouse/lever/ashby 已无意义——那三个 adapter 现在没有配置任何信源。
    # 只跑 1 个信源：全量 15 家要跑 10+ 分钟，冒烟测试没必要。
    stable = {"workday"}
    report = sync.run(only_adapters=stable, only_source_ids={"wd-pfizer"},
                      verbose=True, out_dir=TMP_OUT)
    got = report["snapshot_after_dedup"]
    check("至少抓到 1 条岗位（抓取链路通）", got > 0, f"{got} 条")
    ok_sources = [s for s in report["sources"] if not s.get("not_run") and not s.get("last_error")]
    check("至少 1 个信源成功", len(ok_sources) >= 1, f"{len(ok_sources)} 个成功")


# ---------- C. 健康度可观测 ----------
def test_health_report() -> None:
    print("\n[C] 健康度闭环")
    path = os.path.join(TMP_OUT, "health_report.json")
    check("生成 health_report.json（临时目录，不覆盖正式 data/）", os.path.exists(path))
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            rep = json.load(f)
        print(f"     快照 {rep['jobs_raw']} → 去重 {rep['snapshot_after_dedup']} "
              f"→ 累积入库 {rep['store_total']}（新增 {rep['new_this_run']}），"
              f"异常信源 {len(rep['unhealthy'])} 个")


if __name__ == "__main__":
    print("=" * 56)
    print(" Job Radar 最小验证（稳定抓取 + 去重靠谱）")
    print("=" * 56)
    test_dedup_offline()
    test_jsonld_offline()
    test_config_and_quality_offline()
    test_jd_extract_offline()
    test_job_attrs_offline()
    try:
        test_live_fetch()
        test_health_report()
    except Exception as e:  # noqa: BLE001
        print(f"{FAIL}  实时抓取阶段异常: {type(e).__name__}: {e}")
        failures += 1

    import shutil
    shutil.rmtree(TMP_OUT, ignore_errors=True)

    print("\n" + "=" * 56)
    if failures == 0:
        print(" 全部通过 🎉")
        sys.exit(0)
    print(f" {failures} 项失败")
    sys.exit(1)
