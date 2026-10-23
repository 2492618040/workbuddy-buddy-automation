#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本机令牌刷新工具：读取 WorkBuddy 桌面端登录态，把凭证注入 GitHub 仓库 Secret。

用途：GitHub Actions 云端没有本机登录态，必须把 accessToken / uid 存到仓库
Secrets（Settings → Secrets and variables → Actions → New repository secret）。

安全设计：
  - 默认优先调用 gh CLI（gh secret set --body），token 不回显、不落盘；
  - 无 gh 时引导主人到 GitHub 网页手动粘贴；
  - 仅当显式加 --show 才打印令牌明文（供网页粘贴时复制）；
  - 绝不把令牌写入任何仓库文件。

用法（在本机普通终端运行，勿经 agent 沙箱——沙箱会杀死读取登录文件的进程）：
    python3 refresh_token.py --repo <owner/repo>
    python3 refresh_token.py --repo <owner/repo> --show     # 网页粘贴方式
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

INFO_FILE_NAME = "workbuddy-desktop.info"


def candidate_info_files():
    """按品牌历史枚举登录凭证文件候选路径（只读，不修改）。"""
    paths = []
    if os.name == "nt":
        bases = [os.environ.get("LOCALAPPDATA"), os.environ.get("APPDATA")]
        apps = ("CodeBuddyExtension", "WorkBuddyExtension", "WorkBuddy", "CodeBuddy")
        auths = ("Auth", "auth")
        for base in bases:
            if not base:
                continue
            for app in apps:
                for auth in auths:
                    p = os.path.join(base, app, "Data", "Public", auth, INFO_FILE_NAME)
                    if p not in paths:
                        paths.append(p)
    else:
        home = os.path.expanduser("~")
        base = os.path.join(home, "Library", "Application Support")
        for app in ("CodeBuddyExtension", "WorkBuddyExtension", "WorkBuddy", "CodeBuddy"):
            for auth in ("Auth", "auth"):
                paths.append(os.path.join(base, app, "Data", "Public", auth, INFO_FILE_NAME))
    return paths


def find_info_file():
    for p in candidate_info_files():
        if os.path.isfile(p):
            return p
    raise FileNotFoundError(
        "找不到 WorkBuddy 登录凭证文件，请确认已安装并登录桌面端。已检查：\n  - "
        + "\n  - ".join(candidate_info_files())
    )


def load_credentials(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    auth = data.get("auth") or {}
    account = data.get("account") or {}
    token = auth.get("accessToken")
    uid = account.get("uid") or ""
    if not isinstance(token, str) or len(token) < 20:
        raise RuntimeError("登录文件中没有有效的 accessToken")
    return token, uid, auth.get("expiresIn")


def gh_available():
    return shutil.which("gh") is not None


def gh_set_secret(repo, name, value):
    """通过 gh CLI 设置 secret；value 经 stdin/参数传入，不回显。"""
    cmd = ["gh", "secret", "set", name, "--repo", repo, "--body", value]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode == 0, (r.stderr or r.stdout or "").strip()


def main(argv=None):
    ap = argparse.ArgumentParser(description="刷新 GitHub Secrets 中的 WorkBuddy 令牌")
    ap.add_argument("--repo", required=True,
                    help="GitHub 仓库，格式 owner/repo（gh 模式必填）")
    ap.add_argument("--show", action="store_true",
                    help="打印令牌明文（仅用于 GitHub 网页手动粘贴）")
    ap.add_argument("--info-file", help="显式指定登录凭证文件路径")
    args = ap.parse_args(argv)

    try:
        info_path = args.info_file or find_info_file()
        token, uid, expires_in = load_credentials(info_path)
    except (FileNotFoundError, RuntimeError, ValueError) as e:
        print(f"错误: {e}")
        return 2

    exp_days = f"，约 {expires_in // 86400} 天有效期" if isinstance(expires_in, int) and expires_in > 0 else ""
    print(f"已读取登录态: {info_path}{exp_days}")

    if gh_available():
        ok1, msg1 = gh_set_secret(args.repo, "WB_ACCESS_TOKEN", token)
        print(("✅" if ok1 else "❌") + f" WB_ACCESS_TOKEN {msg1 or '已设置'}")
        if uid:
            ok2, msg2 = gh_set_secret(args.repo, "WB_USER_ID", uid)
            print(("✅" if ok2 else "❌") + f" WB_USER_ID {msg2 or '已设置'}")
        if ok1:
            print("\n完成。GitHub Actions 将在下一次运行时自动使用新令牌。")
            print("提示：令牌约 28 天有效，过期后重跑本脚本即可。")
            return 0
        print("\ngh 设置失败，可改用网页方式（见下），或先执行 gh auth login。")
    else:
        print("未检测到 gh CLI。")

    print("\n—— 网页手动方式 ——")
    print(f"1. 打开 https://github.com/{args.repo}/settings/secrets/actions")
    print("2. New repository secret，依次添加：")
    print("   Name: WB_ACCESS_TOKEN")
    print("   Name: WB_USER_ID")
    if args.show:
        print("\n以下为待粘贴的明文（请勿转发/截图外发）：")
        print(f"WB_ACCESS_TOKEN = {token}")
        if uid:
            print(f"WB_USER_ID      = {uid}")
    else:
        print("\n（未打印令牌明文。需要复制时请加 --show 参数重跑；"
              "或安装 gh CLI 后重跑本脚本自动注入。）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
