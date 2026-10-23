#!/usr/bin/env python3
"""发送设计岗日报（多通道）。

通道优先级：
  1) 飞书群机器人（FEISHU_WEBHOOK_URL）—— 主通道，卡片样式好、免费不限量
  2) PushPlus 推个人微信（PUSHPLUS_TOKEN）—— 兜底通道，手机上收
  3) 企业微信群机器人（WECHAT_WEBHOOK_URL / WECOM_WEBHOOK_URL）—— 可选

行为：
  - 只发送 notify_preview 选出的内容（新增 + 存量兜底）
  - 任一通道发送成功才写 notify_state.json，避免"发送失败却标已推"
  - 全部通道失败时：把日报写到 data/reports/YYYY-MM-DD.md 并退出码 1（Actions 里可见）
  - 没配任何通道时安全跳过，不报错

环境变量：
  FEISHU_WEBHOOK_URL   飞书群机器人 webhook
  FEISHU_SECRET        可选，飞书机器人开了"签名校验"才需要
  PUSHPLUS_TOKEN       pushplus.plus 的 token（微信）
  PUSHPLUS_TOPIC       可选，pushplus 群组编码
  WECHAT_WEBHOOK_URL   企业微信群机器人 webhook（别名 WECOM_WEBHOOK_URL）
  WORKBENCH_URL        信息台链接，会附在消息末尾
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import notify_preview  # noqa: E402
from job_radar import render  # noqa: E402

ROOT = notify_preview.ROOT
REPORTS_DIR = os.path.join(notify_preview.DATA_DIR, "reports")


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def post_json(url: str, payload: dict, timeout: int = 20) -> tuple[int, str]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="ignore")


def post_form(url: str, fields: dict, timeout: int = 20) -> tuple[int, str]:
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="ignore")


# --------------------------------------------------------------------------- #
# 飞书
# --------------------------------------------------------------------------- #
def feishu_sign(secret: str) -> tuple[str, str]:
    ts = str(int(time.time()))
    string_to_sign = f"{ts}\n{secret}"
    h = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return ts, base64.b64encode(h).decode("utf-8")


def feishu_payload(report: dict, secret: str = "") -> dict:
    """飞书 post 卡片。

    直接吃 `notify_preview.build_report()` 产出的报告模型，由 `render.render_feishu()`
    生成结构化节点——早先这里是拿正则反解析 Markdown，加粗、缩进、链接一多就拆错。
    """
    payload = render.render_feishu(report)
    if secret:
        ts, sign = feishu_sign(secret)
        payload["timestamp"] = ts
        payload["sign"] = sign
    return payload


# --------------------------------------------------------------------------- #
# PushPlus（个人微信）
# --------------------------------------------------------------------------- #
def pushplus_payload(report: dict, token: str, topic: str = "") -> dict:
    title = str(report.get("title") or "杭州外企设计岗日报")[:90]
    fields = {
        "token": token,
        "title": title,
        # pushplus 的 markdown 模板支持标准 Markdown；tpl=markdown
        "content": render.render_markdown(report),
        "template": "markdown",
    }
    if topic:
        fields["topic"] = topic
    return fields


# --------------------------------------------------------------------------- #
# 企业微信
# --------------------------------------------------------------------------- #
def wecom_payload(report: dict) -> dict:
    # 企微 markdown 有 4096 字节上限，卡片降为紧凑版 + 超长截断（render 内部处理）
    return render.render_wecom(report)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def send_all(report: dict, dry_run: bool = False) -> dict:
    results: dict[str, tuple[bool, str]] = {}

    feishu = os.getenv("FEISHU_WEBHOOK_URL", "").strip()
    if feishu:
        if dry_run:
            results["feishu"] = (True, "dry-run")
        else:
            code, body = post_json(feishu, feishu_payload(report, os.getenv("FEISHU_SECRET", "").strip()))
            ok = 200 <= code < 300 and '"code":0' in body.replace(" ", "")
            results["feishu"] = (ok, f"HTTP {code} {body[:200]}")

    token = os.getenv("PUSHPLUS_TOKEN", "").strip()
    if token:
        if dry_run:
            results["pushplus"] = (True, "dry-run")
        else:
            code, body = post_form("https://www.pushplus.plus/send",
                                   pushplus_payload(report, token, os.getenv("PUSHPLUS_TOPIC", "").strip()))
            try:
                ok = json.loads(body).get("code") == 200
            except ValueError:
                ok = False
            results["pushplus"] = (ok, f"HTTP {code} {body[:200]}")

    wecom = (os.getenv("WECHAT_WEBHOOK_URL", "").strip()
             or os.getenv("WECOM_WEBHOOK_URL", "").strip())
    if wecom:
        if dry_run:
            results["wecom"] = (True, "dry-run")
        else:
            code, body = post_json(wecom, wecom_payload(report))
            ok = 200 <= code < 300 and '"errcode":0' in body.replace(" ", "")
            results["wecom"] = (ok, f"HTTP {code} {body[:200]}")

    return results


def save_report(md: str) -> str:
    os.makedirs(REPORTS_DIR, exist_ok=True)
    path = os.path.join(REPORTS_DIR, f"{dt.date.today().isoformat()}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)
    return path


def main() -> None:
    p = argparse.ArgumentParser(description="发送设计岗日报到飞书/PushPlus/企业微信。")
    p.add_argument("--out", default=notify_preview.OUT)
    p.add_argument("--state", default=notify_preview.STATE)
    p.add_argument("--limit", type=int, default=8)
    p.add_argument("--standby-limit", type=int, default=5)
    p.add_argument("--min-match", type=int, default=55)
    p.add_argument("--since", default="")
    p.add_argument("--no-standby", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-mark", action="store_true", help="发送成功后不写 notify_state.json")
    p.add_argument("--force", action="store_true", help="即使没有新增也发（发存量兜底）")
    p.add_argument("--workbench-url", default=os.getenv("WORKBENCH_URL", "").strip())
    p.add_argument("--no-ai", action="store_true", help="不调用大模型")
    args = p.parse_args()

    report = notify_preview.build_report(
        limit=args.limit, standby_limit=args.standby_limit, min_match=args.min_match,
        mode="new", since=args.since, state_path=args.state,
        standby=not args.no_standby, workbench_url=args.workbench_url,
        use_ai=not args.no_ai)
    selected, has_new = report["selected"], report["has_new"]
    md = render.render_markdown(report)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(md)

    if not has_new and not args.force:
        print("今日无新增，按配置跳过发送（加 --force 可发存量兜底）。")
        save_report(md)
        return

    results = send_all(report, args.dry_run)
    if not results:
        print(f"未配置任何推送通道，仅生成预览：{args.out}")
        print("配置方式：FEISHU_WEBHOOK_URL / PUSHPLUS_TOKEN / WECHAT_WEBHOOK_URL")
        save_report(md)
        return

    ok_any = False
    for channel, (ok, detail) in results.items():
        print(f"{'✓' if ok else '✗'} {channel}: {detail}")
        ok_any = ok_any or ok

    if ok_any and not args.dry_run and not args.no_mark:
        added = notify_preview.mark_pushed(args.state, selected)
        print(f"已标记 {added} 条为已推送：{args.state}")

    path = save_report(md)
    print(f"日报存档：{path}")

    if not ok_any:
        print("全部通道发送失败，请检查 webhook/token。", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
