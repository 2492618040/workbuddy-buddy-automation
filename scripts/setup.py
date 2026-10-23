#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
setup.py —— Buddy 云端自动化「换机即用」一键部署向导。

在任何一台装了 Python 3.8+ 的电脑上，按提示填几项，一条命令完成：
  1. 校验 GitHub 令牌（PAT）
  2. 仓库不存在就自动创建（默认私有）
  3. 推送全部代码（走 GitHub API，只需 api.github.com 可达，
     公司网络只放行 api.github.com 时也能成功，不需要 git、不需要 github.com 网页）
  4. 加密注入 Actions Secrets（WorkBuddy 令牌、用户 ID、推送通道）
  5. 立刻触发一次真实运行并等到出结论，打印运行结果链接与微信是否应收到推送

三种用法（任选其一）：
  A 交互（推荐，什么都不用记）：
      python scripts/setup.py
  B 命令行一次给全（适合远程/无人值守）：
      python scripts/setup.py --pat ghp_xxx --wb-token eyJhbGci... \
             --wb-user-id 8ab80412-... --wxpusher-token AT_xxx --wxpusher-uid UID_xxx
  C 环境变量：
      GITHUB_PAT / WB_ACCESS_TOKEN / WB_USER_ID / WXPUSHER_TOKEN / WXPUSHER_UID

设计约束（通用 / 稳定 / 不绑本机）：
  - 只用标准库；仅 Secrets 加密需要 pynacl，缺失时自动尝试安装，装不上则
    降级为「打印需在网页填写的清单 + 直达链接」，不会中途崩溃。
  - 所有路径基于本脚本自身位置推导，不写死盘符、用户名、主机名。
  - 令牌类输入一律掩码，日志只回显前后缀，绝不打印完整令牌。
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
WORKFLOW_FILE = "buddy-daily.yml"
DEFAULT_REPO_NAME = "workbuddy-buddy-automation"

# 包根目录 = 本文件所在目录的上一级
PACK_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_CONFIG = os.path.join(PACK_ROOT, "config.json")

SKIP_DIRS = {".git", "__pycache__", "artifacts", ".vscode", ".idea", "node_modules"}
SKIP_EXT = {".pyc", ".pyo", ".pyd", ".so", ".dll", ".exe", ".zip",
            ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".7z"}
# 本地配置含令牌明文，绝不推送到 GitHub
SKIP_FILES = {"config.json", "config.local.json", ".env", "_pat.txt"}
KEEP_DOTFILES = {".gitignore"}

LOG = []


def log(msg=""):
    print(msg, flush=True)
    LOG.append(str(msg))


def mask(v):
    v = str(v or "")
    if len(v) <= 10:
        return "*" * len(v)
    return "%s…%s（长度%d）" % (v[:4], v[-4:], len(v))


# ------------------------------------------------------------------ HTTP
def http(method, url, token, body=None, timeout=40):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "buddy-automation-setup")
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
            return e.code, {"message": raw[:300]}
    except Exception as e:
        return 0, {"message": "网络错误：%s" % e}


# ------------------------------------------------------------------ 输入
def ask(prompt, default="", secret=False, required=True):
    """交互式提问。非交互环境（无 tty）直接返回 default。"""
    if default:
        shown = mask(default) if secret else default
        full = "%s [%s]: " % (prompt, shown)
    else:
        full = "%s: " % prompt
    if not sys.stdin.isatty():
        return default
    try:
        if secret:
            import getpass
            v = getpass.getpass(full)
        else:
            v = input(full)
    except (EOFError, KeyboardInterrupt):
        v = ""
    v = (v or "").strip()
    if not v:
        v = default
    if required and not v:
        log("  此项必填。")
        return ask(prompt, default, secret, required)
    return v


def ensure_pynacl():
    """确保 pynacl 可用；装不上返回 False（由调用方降级处理）。"""
    try:
        import nacl  # noqa: F401
        return True
    except ImportError:
        pass
    log("  未检测到 pynacl（GitHub Secrets 加密需要），正在尝试安装…")
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                        "--disable-pip-version-check", "pynacl"],
                       timeout=240, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        import nacl  # noqa: F401
        log("  pynacl 安装成功。")
        return True
    except Exception as e:
        log("  pynacl 自动安装未成功（%s）。将降级为手动填写 Secrets。" % e)
        return False


# ------------------------------------------------------------------ 推送
def collect_files(root):
    files = {}
    for dp, dn, fn in os.walk(root):
        rel_dir = os.path.relpath(dp, root)
        parts = rel_dir.replace("\\", "/").split("/")
        if any(p in SKIP_DIRS for p in parts):
            continue
        for name in fn:
            if name in SKIP_FILES:
                continue
            if name.startswith(".") and name not in KEEP_DOTFILES:
                continue
            if os.path.splitext(name)[1].lower() in SKIP_EXT:
                continue
            full = os.path.join(dp, name)
            rel = os.path.relpath(full, root).replace("\\", "/")
            try:
                with open(full, "r", encoding="utf-8") as f:
                    files[rel] = f.read()
            except (UnicodeDecodeError, OSError):
                continue
    return files


def api_push(repo, token, branch, root, message):
    files = collect_files(root)
    if not files:
        raise SystemExit("没有可推送的文件：%s" % root)
    log("  待推送 %d 个文件" % len(files))
    for p in sorted(files):
        log("    - %s" % p)

    code, ref = http("GET", "%s/repos/%s/git/ref/heads/%s" % (API, repo, branch), token)
    if code != 200:
        # 空仓库不能直接建 tree（409 Git Repository is empty），先造种子提交
        log("  空仓库：先用 API 创建种子提交")
        seed = base64.b64encode(b"# buddy automation\n").decode("utf-8")
        c0, r0 = http("PUT", "%s/repos/%s/contents/README.md" % (API, repo), token,
                      {"message": "chore: initialize repository", "content": seed})
        if c0 not in (200, 201):
            raise SystemExit("创建种子提交失败（HTTP %s）：%s" % (c0, r0.get("message")))
        code, ref = http("GET", "%s/repos/%s/git/ref/heads/%s" % (API, repo, branch), token)
        if code != 200:  # 兜底：默认分支可能是 master
            code, ref = http("GET", "%s/repos/%s/git/ref/heads/master" % (API, repo), token)
            if code == 200:
                branch = "master"
    if code != 200:
        raise SystemExit("无法获取分支 %s（HTTP %s）：%s" % (branch, code, ref.get("message")))

    base_sha = ref["object"]["sha"]
    parents = [base_sha]
    base_tree = None
    c2, cm = http("GET", "%s/repos/%s/git/commits/%s" % (API, repo, base_sha), token)
    if c2 == 200:
        base_tree = cm["tree"]["sha"]
    log("  分支 %s（基线 %s）" % (branch, base_sha[:7]))

    body = {"tree": [{"path": p, "mode": "100644", "type": "blob", "content": c}
                     for p, c in sorted(files.items())]}
    if base_tree:
        body["base_tree"] = base_tree
    code, t = http("POST", "%s/repos/%s/git/trees" % (API, repo), token, body)
    if code not in (200, 201):
        raise SystemExit("创建 tree 失败（HTTP %s）：%s" % (code, t.get("message")))

    code, cm = http("POST", "%s/repos/%s/git/commits" % (API, repo), token,
                    {"message": message, "tree": t["sha"], "parents": parents})
    if code not in (200, 201):
        raise SystemExit("创建 commit 失败（HTTP %s）：%s" % (code, cm.get("message")))
    new_sha = cm["sha"]
    log("  已创建提交 %s" % new_sha[:7])

    code, r = http("PATCH", "%s/repos/%s/git/refs/heads/%s" % (API, repo, branch),
                   token, {"sha": new_sha})
    if code not in (200, 201):
        raise SystemExit("更新分支失败（HTTP %s）：%s" % (code, r.get("message")))
    log("  推送完成：%s@%s -> %s" % (repo, branch, new_sha[:7]))
    return branch


# ------------------------------------------------------------------ Secrets
def encrypt_secret(public_key_b64, value):
    from nacl import encoding, public
    pk = public.PublicKey(public_key_b64.encode("utf-8"), encoding.Base64Encoder())
    return base64.b64encode(public.SealedBox(pk).encrypt(value.encode("utf-8"))).decode("utf-8")


def inject_secrets(repo, token, secrets):
    code, pk = http("GET", "%s/repos/%s/actions/secrets/public-key" % (API, repo), token)
    if code != 200:
        raise SystemExit("获取仓库公钥失败（HTTP %s）：%s" % (code, pk.get("message")))
    ok, fail = [], []
    for name, value in secrets.items():
        try:
            enc = encrypt_secret(pk["key"], str(value))
        except Exception as e:
            fail.append((name, "加密失败：%s" % e))
            continue
        c, r = http("PUT", "%s/repos/%s/actions/secrets/%s" % (API, repo, name), token,
                    {"encrypted_value": enc, "key_id": pk["key_id"]})
        (ok if c in (201, 204) else fail).append(
            name if c in (201, 204) else (name, "HTTP %s %s" % (c, r.get("message"))))
    for n in ok:
        log("  ✓ %s 已写入（值不回显）" % n)
    for n, why in fail:
        log("  ✗ %s 写入失败：%s" % (n, why))
    return ok, fail


def manual_secret_guide(repo, secrets):
    log("")
    log("  ⚠ 无法自动写入 Secrets（缺少加密库）。请在浏览器打开下面链接手动添加：")
    log("    https://github.com/%s/settings/secrets/actions/new" % repo)
    log("  Name / Value 对照（Value 请从你手上的值复制，本脚本不打印明文）：")
    for name in secrets:
        log("    - %s" % name)


# ------------------------------------------------------------------ 触发
def trigger_and_wait(repo, token, branch, wait=True):
    c, r = http("POST", "%s/repos/%s/actions/workflows/%s/dispatches"
                % (API, repo, WORKFLOW_FILE), token, {"ref": branch})
    if c != 204:
        log("  触发失败（HTTP %s）：%s" % (c, r.get("message")))
        log("  可稍后在仓库 Actions 页面点 Run workflow 手动触发。")
        return None
    if not wait:
        return None
    time.sleep(8)
    run_id = None
    for _ in range(20):
        c, r = http("GET", "%s/repos/%s/actions/workflows/%s/runs?per_page=5"
                    % (API, repo, WORKFLOW_FILE), token)
        if c == 200 and r.get("workflow_runs"):
            run_id = r["workflow_runs"][0]["id"]
            break
        time.sleep(3)
    if not run_id:
        log("  未取到运行记录，请到 Actions 页面查看。")
        return None
    log("  运行 ID %s，等待结束（最多 8 分钟）…" % run_id)
    for _ in range(96):
        c, r = http("GET", "%s/repos/%s/actions/runs/%s" % (API, repo, run_id), token)
        if c == 200 and r.get("status") == "completed":
            log("  运行状态：%s / 结论：%s" % (r.get("status"), r.get("conclusion")))
            log("  详情：%s" % r.get("html_url"))
            return r.get("conclusion")
        time.sleep(5)
    log("  等待超时，请查看：https://github.com/%s/actions" % repo)
    return None


# ------------------------------------------------------------------ 主流程
def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="Buddy 云端自动化一键部署（换机即用）")
    ap.add_argument("--pat", default=os.environ.get("GITHUB_PAT", ""),
                    help="GitHub 个人访问令牌（ghp_ 开头），也可由环境变量 GITHUB_PAT 提供")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPO", ""),
                    help="owner/repo；留空则用 PAT 对应账号 + 默认仓库名")
    ap.add_argument("--repo-name", default=os.environ.get("REPO_NAME", DEFAULT_REPO_NAME))
    ap.add_argument("--private", dest="private", action="store_true", default=True)
    ap.add_argument("--public", dest="private", action="store_false")
    ap.add_argument("--wb-token", default=os.environ.get("WB_ACCESS_TOKEN", ""))
    ap.add_argument("--wb-user-id", default=os.environ.get("WB_USER_ID", ""))
    ap.add_argument("--wxpusher-token", default=os.environ.get("WXPUSHER_TOKEN", ""))
    ap.add_argument("--wxpusher-uid", default=os.environ.get("WXPUSHER_UID", ""))
    ap.add_argument("--pushplus-token", default=os.environ.get("PUSHPLUS_TOKEN", ""))
    ap.add_argument("--feishu-webhook", default=os.environ.get("FEISHU_WEBHOOK", ""))
    ap.add_argument("--no-run", action="store_true", help="部署但不触发实跑")
    ap.add_argument("--yes", action="store_true", help="非交互：缺少的值直接留空/用默认")
    args = ap.parse_args()

    log("=" * 62)
    log("Buddy 云端自动化 · 一键部署")
    log("=" * 62)

    # ---- 1. 收集 GitHub 令牌
    pat = args.pat
    if not pat:
        log("")
        log("【第 1 步】GitHub 令牌（PAT）—— 用来建仓库、推代码、写密钥")
        log("  获取：github.com → Settings → Developer settings →")
        log("        Personal access tokens → Tokens (classic) → Generate new token")
        log("  权限：勾选 repo、workflow")
        pat = ask("请粘贴 PAT", "", secret=True)
    if not pat:
        raise SystemExit("缺少 GitHub PAT，无法部署。")

    log("")
    log("== 1/5 校验 PAT ==")
    code, me = http("GET", API + "/user", pat)
    if code != 200:
        raise SystemExit("PAT 校验失败（HTTP %s）：%s" % (code, me.get("message")))
    login = me.get("login")
    log("  身份确认：%s" % login)

    # ---- 2. 仓库：指定 / 已存在 / 新建
    repo = args.repo.strip()
    if not repo:
        repo = "%s/%s" % (login, args.repo_name)
    log("")
    log("== 2/5 准备仓库 ==")
    code, info = http("GET", "%s/repos/%s" % (API, repo), pat)
    if code == 200:
        log("  仓库已存在：%s（私有=%s）" % (info.get("full_name"), info.get("private")))
    elif code == 404:
        create = True
        if sys.stdin.isatty() and not args.yes:
            ans = ask("  仓库 %s 不存在，是否自动创建（私有）？[Y/n]" % repo, "y")
            create = str(ans).lower() not in ("n", "no", "0")
        if not create:
            raise SystemExit("已取消：请手动创建仓库后重跑，或用 --repo owner/name 指定。")
        c, r = http("POST", API + "/user/repos", pat,
                    {"name": repo.split("/")[-1], "private": bool(args.private),
                     "auto_init": False, "description": "WorkBuddy 每日自动签到与推送"})
        if c not in (200, 201):
            raise SystemExit("创建仓库失败（HTTP %s）：%s" % (c, r.get("message")))
        log("  已创建仓库：%s（私有=%s）" % (r.get("full_name"), r.get("private")))
    else:
        raise SystemExit("访问仓库失败（HTTP %s）：%s" % (code, info.get("message")))

    # ---- 3. 收集业务配置
    log("")
    log("【第 3 步】WorkBuddy 与推送配置（值只写入 GitHub Secrets，不进代码）")
    wb_token = args.wb_token
    wb_uid = args.wb_user_id
    wxt = args.wxpusher_token
    wxu = args.wxpusher_uid
    ppt = args.pushplus_token
    fsh = args.feishu_webhook
    if not args.yes and sys.stdin.isatty():
        if not wb_token:
            log("  WorkBuddy 令牌：本机路径 ~/.workbuddy/skills/workbuddy-checkin/config.json")
            log("                  的 access_token 字段；换机器需重新取。")
            wb_token = ask("  WorkBuddy 令牌 access_token", "", secret=True, required=False)
        if not wb_uid:
            wb_uid = ask("  WorkBuddy 用户 ID user_id", "", secret=False, required=False)
        if not (wxt and wxu):
            log("  微信推送（WxPusher，免费）：不填也能部署，只是收不到微信消息。")
            wxt = ask("  WxPusher appToken（AT_ 开头，可留空）", "", secret=True, required=False)
            wxu = ask("  WxPusher UID（UID_ 开头，可留空）", "", secret=True, required=False)

    # 本地已存在 config.json 时自动补全缺失项
    if os.path.isfile(LOCAL_CONFIG):
        try:
            c = json.load(open(LOCAL_CONFIG, encoding="utf-8"))
            wb_token = wb_token or c.get("access_token", "")
            wb_uid = wb_uid or c.get("user_id", "")
            wxt = wxt or c.get("wxpusher_token", "")
            wxu = wxu or c.get("wxpusher_uid", "")
            ppt = ppt or c.get("pushplus_token", "")
            log("  已从本地 config.json 补全缺失项。")
        except Exception:
            pass

    secrets = {}
    if wb_token:
        secrets["WB_ACCESS_TOKEN"] = wb_token
    if wb_uid:
        secrets["WB_USER_ID"] = wb_uid
    if wxt:
        secrets["WXPUSHER_TOKEN"] = wxt
    if wxu:
        secrets["WXPUSHER_UID"] = wxu
    if ppt:
        secrets["PUSHPLUS_TOKEN"] = ppt
    if fsh:
        secrets["FEISHU_WEBHOOK"] = fsh
    secrets["ADOPT_BUDDY"] = os.environ.get("ADOPT_BUDDY", "1")

    if not wb_token or not wb_uid:
        log("")
        log("  ⚠ 未提供 WorkBuddy 令牌或用户 ID，云端将无法签到。")
        log("    部署会继续，但运行必然失败；之后可补填 Secrets 再手动触发。")

    # ---- 4. 推代码
    log("")
    log("== 3/5 推送代码（走 API，不需要 git，也不需要打开 github.com） ==")
    branch = api_push(repo, pat, "main", PACK_ROOT,
                      "feat: Buddy 每日自动化（签到 + 派猫 + 推送）")

    # ---- 5. 注入 Secrets
    log("")
    log("== 4/5 注入 Secrets ==")
    if not secrets:
        log("  无 Secret 可写入（全部留空），跳过。")
    elif ensure_pynacl():
        inject_secrets(repo, pat, secrets)
    else:
        manual_secret_guide(repo, secrets)

    # ---- 6. 触发验收
    if args.no_run:
        log("")
        log("== 5/5 已跳过实跑（--no-run） ==")
    else:
        log("")
        log("== 5/5 触发一次真实运行 ==")
        concl = trigger_and_wait(repo, pat, branch)
        if concl == "success":
            log("")
            log("  ✅ 云端实跑成功。微信应已收到一条带有签到结果的消息。")
        elif concl:
            log("")
            log("  ⚠ 云端运行结束但结论为 %s，请打开上面的链接看日志。" % concl)

    # ---- 保存本地配置（供本机手动试跑）
    # 采用「合并」而非覆盖：保留文件中已有的其他字段（如 refresh_token），
    # 只更新本次拿到的值，避免部署一次就把历史字段冲掉。
    try:
        local = {}
        if os.path.isfile(LOCAL_CONFIG):
            try:
                local = json.load(open(LOCAL_CONFIG, encoding="utf-8"))
            except Exception:
                local = {}
        local.update({"endpoint": "https://copilot.tencent.com",
                      "access_token": wb_token, "user_id": wb_uid,
                      "wxpusher_token": wxt, "wxpusher_uid": wxu,
                      "pushplus_token": ppt})
        with open(LOCAL_CONFIG, "w", encoding="utf-8") as f:
            json.dump(local, f, ensure_ascii=False, indent=2)
        log("")
        log("  本地配置已写入：%s（含令牌明文，已在 .gitignore 中排除，不会上传）" % LOCAL_CONFIG)
    except Exception as e:
        log("  本地配置写入失败（不影响云端）：%s" % e)

    log("")
    log("=" * 62)
    log("部署完成。仓库：https://github.com/%s" % repo)
    log("定时任务：每天北京时间 06:00 自动签到 + 派猫 + 推送。")
    log("手动触发：仓库 → Actions → Buddy 每日自动化 → Run workflow")
    log("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
