#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_secret.py —— 把本机最新的 WorkBuddy 令牌上传到云端 GitHub Secret。

为什么需要它：WorkBuddy 的登录令牌约 28 天过期，过期后云端签到会失败。
本脚本在**本机普通窗口**运行（不在 agent 沙箱内），直接读取桌面端登录态里
的最新令牌，加密写入 GitHub Secrets，云端下一次运行即可用上新令牌。

用法（双击同目录的「更新令牌.bat」即可）：
    python scripts/update_secret.py                 # 读登录态 → 更新云端
    python scripts/update_secret.py --run           # 更新后再触发一次云端实跑验证
    python scripts/update_secret.py --pat ghp_xxx   # 当场给 GitHub 令牌

令牌来源优先级：命令行 --pat > 环境变量 GITHUB_PAT > config.json 的 github_pat > 交互输入。
输出只显示令牌首尾几位与长度，绝不打印明文。
"""

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from setup import http, ensure_pynacl, inject_secrets, mask, ask, log  # noqa: E402

PACK_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_CONFIG = os.path.join(PACK_ROOT, "config.json")
INFO_FILE_NAME = "workbuddy-desktop.info"


def candidate_info_files():
    paths = []
    bases = []
    if os.name == "nt":
        bases = [os.environ.get("LOCALAPPDATA"), os.environ.get("APPDATA")]
    else:
        bases = [os.path.join(os.path.expanduser("~"), "Library", "Application Support")]
    for base in bases:
        if not base:
            continue
        for app in ("CodeBuddyExtension", "WorkBuddyExtension", "WorkBuddy", "CodeBuddy"):
            for a in ("Auth", "auth"):
                p = os.path.join(base, app, "Data", "Public", a, INFO_FILE_NAME)
                if p not in paths:
                    paths.append(p)
    return paths


def read_live_credential():
    """从桌面端登录态读取最新 access_token / user_id / refresh_token。"""
    for p in candidate_info_files():
        if not os.path.isfile(p):
            continue
        try:
            data = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        auth = data.get("auth") or {}
        account = data.get("account") or {}
        access = None
        for k in ("accessToken", "access_token", "token", "idToken"):
            if isinstance(auth.get(k), str) and len(auth[k]) > 20:
                access = auth[k]
                break
        refresh = None
        for k, v in auth.items():
            if "refresh" in k.lower() and isinstance(v, str) and v:
                refresh = v
                break
        uid = None
        for src in (account, auth, data):
            if not isinstance(src, dict):
                continue
            for k in ("uid", "userId", "user_id", "sub"):
                if isinstance(src.get(k), str) and src[k]:
                    uid = src[k]
                    break
            if uid:
                break
        if access:
            log("  已读取桌面端登录态：%s" % p)
            return access, uid, refresh
    return None, None, None


def push_wx(token, uid, text):
    if not (token and uid):
        return False
    body = {"appToken": token, "content": text, "summary": text[:40],
            "contentType": 1, "uids": [uid]}
    req = urllib.request.Request("https://wxpusher.zjiecode.com/api/send/message",
                                 data=json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8", "replace")).get("code") == 1000
    except Exception:
        return False


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="更新云端 WorkBuddy 令牌")
    ap.add_argument("--pat", default=os.environ.get("GITHUB_PAT", ""))
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPO", ""))
    ap.add_argument("--run", action="store_true", help="更新后触发一次云端实跑")
    ap.add_argument("--no-live-scan", action="store_true",
                    help="跳过读取桌面端登录态，直接用 config.json 里的现有令牌")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    cfg = {}
    if os.path.isfile(LOCAL_CONFIG):
        try:
            cfg = json.load(open(LOCAL_CONFIG, encoding="utf-8"))
        except Exception:
            cfg = {}

    log("=" * 60)
    log("更新云端 WorkBuddy 令牌")
    log("=" * 60)

    log("")
    log("== 1/3 读取本机最新令牌 ==")
    if args.no_live_scan:
        log("  已跳过登录态读取（--no-live-scan）")
        access, uid, refresh = None, None, None
    else:
        access, uid, refresh = read_live_credential()
    if access:
        log("  最新 access_token：%s" % mask(access))
    else:
        log("  未读到桌面端登录态，改用本地 config.json 的现有值。")
        access = cfg.get("access_token", "")
    uid = uid or cfg.get("user_id", "")
    refresh = refresh or cfg.get("refresh_token", "")
    if not access:
        raise SystemExit("  没有可用令牌：请确认 WorkBuddy 桌面端已登录，或先在 config.json 填好。")
    log("  user_id：%s" % (uid[:8] + "…" if uid else "(空)"))

    # 回写本地（合并，不丢字段）
    cfg.update({"access_token": access})
    if uid:
        cfg["user_id"] = uid
    if refresh:
        cfg["refresh_token"] = refresh
    try:
        json.dump(cfg, open(LOCAL_CONFIG, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        log("  本地 config.json 已更新。")
    except Exception as e:
        log("  本地配置写入失败（不影响云端）：%s" % e)

    log("")
    log("== 2/3 连接 GitHub ==")
    pat = args.pat or cfg.get("github_pat", "")
    repo = args.repo or cfg.get("github_repo", "")
    if not pat:
        pat = ask("GitHub 令牌 PAT（ghp_ 开头）", "", secret=True)
    if not pat:
        raise SystemExit("缺少 GitHub PAT。")
    code, me = http("GET", "https://api.github.com/user", pat)
    if code != 200:
        raise SystemExit("PAT 校验失败（HTTP %s）：%s" % (code, me.get("message")))
    if not repo:
        repo = ask("目标仓库 owner/repo", "%s/workbuddy-buddy-automation" % me.get("login"))
    code, info = http("GET", "https://api.github.com/repos/%s" % repo, pat)
    if code != 200:
        raise SystemExit("仓库不可用（HTTP %s）：%s" % (code, info.get("message")))
    log("  仓库：%s" % repo)
    cfg["github_repo"] = repo
    cfg["github_pat"] = pat
    try:
        json.dump(cfg, open(LOCAL_CONFIG, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
    except Exception:
        pass

    log("")
    log("== 3/3 加密写入云端 Secrets ==")
    if not ensure_pynacl():
        raise SystemExit("缺少 pynacl 且自动安装失败，无法加密写入。请手动 pip install pynacl 后重试。")
    secrets = {"WB_ACCESS_TOKEN": access}
    if uid:
        secrets["WB_USER_ID"] = uid
    if refresh:
        secrets["WB_REFRESH_TOKEN"] = refresh
    ok, fail = inject_secrets(repo, pat, secrets)

    if args.run:
        from setup import trigger_and_wait
        log("")
        log("  触发一次云端实跑验证…")
        concl = trigger_and_wait(repo, pat, "main")
        if concl == "success":
            log("  ✅ 云端实跑成功，新令牌已生效。")
        elif concl:
            log("  ⚠ 云端结论：%s" % concl)

    if ok:
        push_wx(cfg.get("wxpusher_token"), cfg.get("wxpusher_uid"),
                "Buddy 自动化：云端令牌已更新（%s），下次到期前会再提醒。"
                % ", ".join(ok))
        log("")
        log("  已推送微信确认：云端令牌已更新。")

    log("")
    log("完成。云端下次运行即使用新令牌。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
