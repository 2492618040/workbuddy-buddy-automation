#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
deploy_with_pat.py —— 拿到 GitHub PAT 后的一键部署脚本。

完成四件事（按顺序，任何一步失败都会明确报错并停在原地）：
  1. 校验 PAT 有效（GET /user）
  2. git push 本地代码到远端仓库
  3. 用 libsodium sealed-box 加密并注入 Actions Secrets（WB_ACCESS_TOKEN / WB_USER_ID / 推送通道 / ADOPT_BUDDY）
  4. 触发 workflow_dispatch 实跑一次，并轮询到运行结束，打印结论

用法：
    python scripts/deploy_with_pat.py ghp_xxxxxxxxxxxxxxxxxxxx
    python scripts/deploy_with_pat.py --token ghp_xxx --repo 2492618040/workbuddy-buddy-automation
    set GITHUB_PAT=ghp_xxx && python scripts/deploy_with_pat.py     # 也可用环境变量传入

设计约束（通用 / 稳定 / 不绑本机）：
  - 只依赖标准库 + pynacl（Secrets 加密必需）；pynacl 缺失时给出明确安装提示而非崩溃
  - git 解释器路径按 GIT_EXE → PATH → 常见安装位置依次探测，不写死
  - 令牌值只从 --config 指定的配置文件读取，绝不打印到终端
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
DEFAULT_REPO = "2492618040/workbuddy-buddy-automation"
DEFAULT_BRANCH = "main"
WORKFLOW_FILE = "buddy-daily.yml"

LOG_LINES = []


def log(msg):
    print(msg, flush=True)
    LOG_LINES.append(msg)


# ---------------------------------------------------------------- HTTP 基础
def http(method, url, token, body=None, timeout=40):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "buddy-automation-deploy")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"message": raw[:500]}
    except Exception as e:
        return 0, {"message": "网络错误：%s" % e}


def encrypt_secret(public_key_b64, value):
    """用仓库公钥做 libsodium sealed-box 加密，返回可直接 PUT 的密文。"""
    try:
        from nacl import encoding, public
    except ImportError:
        raise SystemExit(
            "缺少依赖 pynacl（GitHub Secrets 加密必需）。请先执行：\n"
            "  pip install pynacl\n"
            "然后再重跑本脚本。"
        )
    pk = public.PublicKey(public_key_b64.encode("utf-8"), encoding.Base64Encoder())
    sealed = public.SealedBox(pk)
    return base64.b64encode(sealed.encrypt(value.encode("utf-8"))).decode("utf-8")


# ---------------------------------------------------------------- git 定位
def find_git():
    cand = os.environ.get("GIT_EXE")
    if cand and os.path.isfile(cand):
        return cand
    for name in ("git", "git.exe"):
        for p in os.environ.get("PATH", "").split(os.pathsep):
            fp = os.path.join(p, name)
            if os.path.isfile(fp):
                return fp
    common = [
        r"C:\Users\zhouy1\.workbuddy\binaries\PortableGit\versions\1.2.0\cmd\git.exe",
        r"C:\Program Files\Git\cmd\git.exe",
        r"C:\Program Files\Git\bin\git.exe",
        "/usr/bin/git",
        "/usr/local/bin/git",
    ]
    for fp in common:
        if os.path.isfile(fp):
            return fp
    return None


def run_git(git, args, cwd, env=None):
    proc = subprocess.run(
        [git] + args, cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


# ---------------------------------------------------------------- 主流程
SKIP_DIRS = {".git", "__pycache__", "artifacts", ".vscode", ".idea", "node_modules"}
SKIP_EXT = {".pyc", ".pyo", ".pyd", ".so", ".dll", ".exe", ".zip",
            ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf"}
KEEP_DOTFILES = {".gitignore"}


def collect_files(local_dir):
    """收集待推送的文本文件（相对路径 -> 内容），跳过二进制与运行产物。"""
    files = {}
    for dp, dn, fn in os.walk(local_dir):
        rel_dir = os.path.relpath(dp, local_dir)
        parts = rel_dir.replace("\\", "/").split("/")
        if any(p in SKIP_DIRS for p in parts):
            continue
        for name in fn:
            if name.startswith(".") and name not in KEEP_DOTFILES:
                continue
            if os.path.splitext(name)[1].lower() in SKIP_EXT:
                continue
            full = os.path.join(dp, name)
            rel = os.path.relpath(full, local_dir).replace("\\", "/")
            try:
                with open(full, "r", encoding="utf-8") as f:
                    files[rel] = f.read()
            except (UnicodeDecodeError, OSError):
                continue
    return files


def api_push(repo, token, branch, local_dir, message):
    """用 GitHub Git Data API 推送代码。

    为什么不用 git push：很多内网/办公网络只放行 api.github.com，
    而 git 协议走 github.com 会被拦（连接超时或 000），改用纯 HTTPS API 即可绕过。
    单次提交完成，不产生中间提交。
    """
    files = collect_files(local_dir)
    if not files:
        raise SystemExit("没有可推送的文件：%s" % local_dir)
    log("  待推送 %d 个文件：%s" % (len(files), "、".join(sorted(files)[:6]) + " …"))

    # 1) 取现有分支（空仓库时 404，属正常）
    base_tree, parents = None, []
    code, ref = http("GET", "%s/repos/%s/git/ref/heads/%s" % (API, repo, branch), token)
    if code != 200:
        # GitHub 不允许对完全空的仓库直接建 tree（HTTP 409 "Git Repository is empty"），
        # 必须先用 Contents API 造一个种子提交，把默认分支建出来。
        log("  空仓库：先用 Contents API 创建种子提交")
        seed = base64.b64encode(b"# buddy automation init\n").decode("utf-8")
        c0, r0 = http("PUT", "%s/repos/%s/contents/README.md" % (API, repo), token,
                      {"message": "chore: initialize repository", "content": seed})
        if c0 not in (200, 201):
            raise SystemExit("创建种子提交失败（HTTP %s）：%s" % (c0, r0.get("message")))
        code, ref = http("GET", "%s/repos/%s/git/ref/heads/%s" % (API, repo, branch), token)
        if code != 200:  # 兜底：默认分支可能是 master
            code, ref = http("GET", "%s/repos/%s/git/ref/heads/master" % (API, repo), token)
            if code == 200:
                branch = "master"
    if code == 200:
        base_sha = ref["object"]["sha"]
        parents = [base_sha]
        c2, cm = http("GET", "%s/repos/%s/git/commits/%s" % (API, repo, base_sha), token)
        if c2 == 200:
            base_tree = cm["tree"]["sha"]
        log("  分支 %s（基线 %s）" % (branch, base_sha[:7]))
    else:
        raise SystemExit("无法获取分支 %s（HTTP %s）：%s" % (branch, code, ref.get("message")))

    # 2) 建 tree（内容内联，无需逐个建 blob）
    tree = [{"path": p, "mode": "100644", "type": "blob", "content": c}
            for p, c in sorted(files.items())]
    body = {"tree": tree}
    if base_tree:
        body["base_tree"] = base_tree
    code, t = http("POST", "%s/repos/%s/git/trees" % (API, repo), token, body)
    if code not in (200, 201):
        raise SystemExit("创建 tree 失败（HTTP %s）：%s" % (code, t.get("message")))

    # 3) 建 commit
    code, cm = http("POST", "%s/repos/%s/git/commits" % (API, repo), token,
                    {"message": message, "tree": t["sha"], "parents": parents})
    if code not in (200, 201):
        raise SystemExit("创建 commit 失败（HTTP %s）：%s" % (code, cm.get("message")))
    new_sha = cm["sha"]
    log("  已创建提交 %s" % new_sha[:7])

    # 4) 移动分支指针
    if parents:
        code, r = http("PATCH", "%s/repos/%s/git/refs/heads/%s" % (API, repo, branch),
                       token, {"sha": new_sha})
    else:
        code, r = http("POST", "%s/repos/%s/git/refs" % (API, repo), token,
                       {"ref": "refs/heads/%s" % branch, "sha": new_sha})
    if code not in (200, 201):
        raise SystemExit("更新分支失败（HTTP %s）：%s" % (code, r.get("message")))
    log("  推送完成：%s@%s -> %s" % (repo, branch, new_sha[:7]))
    return new_sha


def main():
    ap = argparse.ArgumentParser(description="用 GitHub PAT 一键部署 Buddy 每日自动化")
    ap.add_argument("token_pos", nargs="?", help="GitHub PAT（ghp_ 开头）")
    ap.add_argument("--token", help="GitHub PAT（也可由环境变量 GITHUB_PAT 提供）")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPO", DEFAULT_REPO),
                    help="owner/repo，默认 %s" % DEFAULT_REPO)
    ap.add_argument("--branch", default=DEFAULT_BRANCH)
    ap.add_argument("--config", default=os.path.expanduser(
        r"~\.workbuddy\skills\workbuddy-checkin\config.json"),
        help="本地 WorkBuddy 配置文件路径（读取令牌，不打印）")
    ap.add_argument("--local-repo", default=os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), help="本地仓库目录")
    ap.add_argument("--skip-push", action="store_true", help="跳过 git push（已推过时用）")
    ap.add_argument("--use-git", action="store_true",
                    help="改用 git push 推送（默认走 API，适用于 github.com 不可达的网络）")
    ap.add_argument("--skip-dispatch", action="store_true", help="跳过手动触发验收")
    args = ap.parse_args()

    token = args.token or args.token_pos or os.environ.get("GITHUB_PAT", "").strip()
    if not token:
        raise SystemExit("未提供 PAT。用法：python scripts/deploy_with_pat.py ghp_xxxxxx")

    log("== 步骤 1/4：校验 PAT ==")
    code, me = http("GET", API + "/user", token)
    if code != 200:
        raise SystemExit("PAT 校验失败（HTTP %s）：%s" % (code, me.get("message")))
    log("  身份确认：%s（%s）" % (me.get("login"), me.get("html_url")))

    # 校验 PAT 对目标仓库是否有写权限
    code, repo = http("GET", "%s/repos/%s" % (API, args.repo), token)
    if code != 200:
        raise SystemExit("无法访问仓库 %s（HTTP %s）：%s" % (args.repo, code, repo.get("message")))
    log("  仓库确认：%s（私有=%s）" % (repo.get("full_name"), repo.get("private")))

    # ------------------------------------------------------------ 推代码
    if not args.skip_push:
        if args.use_git:
            log("== 步骤 2/4：推送代码（git push，需 github.com 可达） ==")
            git = find_git()
            if not git:
                raise SystemExit("未找到 git 可执行文件，请设置环境变量 GIT_EXE 指向 git.exe")
            local = args.local_repo
            push_url = "https://x-access-token:%s@github.com/%s.git" % (token, args.repo)
            rc, out = run_git(git, ["push", "-u", push_url, args.branch], local)
            if rc != 0:
                raise SystemExit("git push 失败（rc=%d）：\n%s" % (rc, out[-2000:]))
            run_git(git, ["remote", "set-url", "origin",
                          "https://github.com/%s.git" % args.repo], local)
            log("  推送成功：%s@%s" % (args.repo, args.branch))
        else:
            log("== 步骤 2/4：推送代码（Git Data API，仅需 api.github.com 可达） ==")
            api_push(args.repo, token, args.branch, args.local_repo,
                     "feat: Buddy 每日自动化（签到+派猫+推送）")
    else:
        log("== 步骤 2/4：跳过（--skip-push） ==")

    # ------------------------------------------------------------ 注入 Secrets
    log("== 步骤 3/4：注入 Actions Secrets ==")
    cfg = {}
    if os.path.isfile(args.config):
        try:
            with open(args.config, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            log("  已读取配置：%s" % args.config)
        except Exception as e:
            log("  警告：读取配置失败（%s），将跳过相关 Secret" % e)
    else:
        log("  警告：未找到配置文件 %s，将跳过需要令牌的 Secret" % args.config)

    secrets = {}
    if cfg.get("access_token"):
        secrets["WB_ACCESS_TOKEN"] = cfg["access_token"]
    if cfg.get("user_id"):
        secrets["WB_USER_ID"] = cfg["user_id"]
    if cfg.get("enterprise_id"):
        secrets["WB_ENTERPRISE_ID"] = cfg["enterprise_id"]
    if cfg.get("domain"):
        secrets["WB_DOMAIN"] = cfg["domain"]
    if cfg.get("pushplus_token"):
        secrets["PUSHPLUS_TOKEN"] = cfg["pushplus_token"]
    if cfg.get("wxpusher_token"):
        secrets["WXPUSHER_TOKEN"] = cfg["wxpusher_token"]
    if cfg.get("wxpusher_uid"):
        secrets["WXPUSHER_UID"] = cfg["wxpusher_uid"]
    secrets["ADOPT_BUDDY"] = os.environ.get("ADOPT_BUDDY", "1")

    if not secrets:
        raise SystemExit("没有任何可注入的 Secret，检查 --config 路径")

    code, pk = http("GET", "%s/repos/%s/actions/secrets/public-key" % (API, args.repo), token)
    if code != 200:
        raise SystemExit("获取仓库公钥失败（HTTP %s）：%s" % (code, pk.get("message")))

    for name, value in secrets.items():
        enc = encrypt_secret(pk["key"], str(value))
        c, r = http("PUT", "%s/repos/%s/actions/secrets/%s" % (API, args.repo, name), token,
                    {"encrypted_value": enc, "key_id": pk["key_id"]})
        if c in (201, 204):
            log("  ✓ %s 已写入（值不回显）" % name)
        else:
            log("  ✗ %s 写入失败（HTTP %s）：%s" % (name, c, r.get("message")))

    # ------------------------------------------------------------ 触发验收
    if args.skip_dispatch:
        log("== 步骤 4/4：跳过（--skip-dispatch） ==")
        log("\n全部完成。定时任务将在每天北京时间 06:00 自动运行。")
        return 0

    log("== 步骤 4/4：触发一次实跑验收 ==")
    c, r = http("POST", "%s/repos/%s/actions/workflows/%s/dispatches" % (API, args.repo, WORKFLOW_FILE),
                token, {"ref": args.branch})
    if c != 204:
        log("  触发失败（HTTP %s）：%s" % (c, r.get("message")))
        log("  可稍后在仓库 Actions 页面手动点 Run workflow。")
        return 1

    # 轮询最近的 run（dispatch 有秒级延迟，先等一会儿）
    time.sleep(8)
    run_id = None
    for _ in range(20):
        c, r = http("GET", "%s/repos/%s/actions/workflows/%s/runs?per_page=5" % (API, args.repo, WORKFLOW_FILE),
                    token)
        if c == 200 and r.get("workflow_runs"):
            run_id = r["workflow_runs"][0]["id"]
            break
        time.sleep(3)
    if not run_id:
        log("  未获取到运行记录，请到 Actions 页面查看。")
        return 1

    log("  运行 ID：%s，等待结束（最多 6 分钟）…" % run_id)
    for _ in range(72):
        c, r = http("GET", "%s/repos/%s/actions/runs/%s" % (API, args.repo, run_id), token)
        if c == 200:
            status, concl = r.get("status"), r.get("conclusion")
            if status == "completed":
                log("  运行状态：%s / 结论：%s" % (status, concl))
                log("  详情：%s" % r.get("html_url"))
                return 0 if concl == "success" else 1
        time.sleep(5)
    log("  超时未结束，请到 Actions 页面查看：https://github.com/%s/actions" % args.repo)
    return 1


if __name__ == "__main__":
    sys.exit(main())
