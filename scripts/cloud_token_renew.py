#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cloud-side token self-renewal (GitHub Actions pre-step, stdlib only).

Every day:
  1. read the current refresh_token from the encrypted state/rt.json
     (fallback: Secret WB_REFRESH_TOKEN for the first run / disaster recovery)
  2. call the official plugin refresh endpoint -> fresh access_token + rotated refresh_token
  3. export the fresh access_token as WB_LIVE_ACCESS_TOKEN via GITHUB_ENV
  4. encrypt the rotated refresh_token back into state/rt.json via Contents API

Design rules:
  - NEVER block the check-in: any failure degrades to the static Secret token.
  - Never print token values; only lengths.
  - state/rt.json stores ciphertext only (HMAC-SHA256 keystream + auth tag),
    key lives in Secret RT_ENC_KEY. Even if the repo is exposed, tokens are not.
"""
import base64
import datetime
import hashlib
import hmac
import json
import os
import secrets
import urllib.request

REPO = os.environ.get("GITHUB_REPOSITORY", "2492618040/workbuddy-buddy-automation")
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")
BRANCH = os.environ.get("GITHUB_REF_NAME", "main")
ENC_KEY_B64 = os.environ.get("RT_ENC_KEY", "")
SEED_RT = os.environ.get("WB_REFRESH_TOKEN", "")
STATE_PATH = "state/rt.json"
API = "https://api.github.com"
REFRESH_URL = "https://copilot.tencent.com/v2/plugin/auth/token/refresh"
TIMEOUT = 30


def log(msg):
    print("[renew] %s" % msg)


def warn(msg):
    print("::warning::[renew] %s" % msg)


def keystream(key, n):
    out = bytearray()
    ctr = 0
    while len(out) < n:
        out.extend(hmac.new(key, b"buddy-ctr-" + ctr.to_bytes(8, "big"),
                            hashlib.sha256).digest())
        ctr += 1
    return bytes(out[:n])


def encrypt(plain, key):
    nonce = secrets.token_bytes(16)
    sub = hashlib.sha256(key + nonce).digest()
    data = plain.encode("utf-8")
    ct = bytes(a ^ b for a, b in zip(data, keystream(sub, len(data))))
    tag = hmac.new(sub, nonce + ct, hashlib.sha256).digest()
    return base64.b64encode(nonce + tag + ct).decode("ascii")


def decrypt(blob, key):
    raw = base64.b64decode(blob)
    nonce, tag, ct = raw[:16], raw[16:48], raw[48:]
    sub = hashlib.sha256(key + nonce).digest()
    if not hmac.compare_digest(tag, hmac.new(sub, nonce + ct, hashlib.sha256).digest()):
        raise ValueError("auth tag mismatch")
    ks = keystream(sub, len(ct))
    return bytes(a ^ b for a, b in zip(ct, ks)).decode("utf-8")


def gh(method, path, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + GH_TOKEN)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "buddy-token-renew")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.status, json.loads(r.read() or b"{}")


def read_state(key):
    """Return (refresh_token, sha) or (None, None)."""
    try:
        st, obj = gh("GET", "/repos/%s/contents/%s?ref=%s" % (REPO, STATE_PATH, BRANCH))
        if st != 200:
            return None, None
        cipher = base64.b64decode(obj.get("content") or "").decode("ascii")
        return decrypt(cipher, key), obj.get("sha")
    except Exception:
        return None, None


def write_state(key, rt, sha):
    cipher = encrypt(rt, key)
    content = base64.b64encode(cipher.encode("ascii")).decode("ascii")
    payload = {
        "message": "chore: rotate refresh token (%s UTC)"
                   % datetime.datetime.utcnow().strftime("%Y-%m-%d"),
        "content": content,
        "branch": BRANCH,
    }
    if sha:
        payload["sha"] = sha
    st, _ = gh("PUT", "/repos/%s/contents/%s" % (REPO, STATE_PATH), payload)
    return st in (200, 201)


def refresh(rt):
    req = urllib.request.Request(REFRESH_URL, data=b"{}", method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Refresh-Token", rt)
    req.add_header("X-Auth-Refresh-Source", "plugin")
    req.add_header("X-Domain", "copilot.tencent.com")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        obj = json.loads(r.read() or b"{}")
    if obj.get("code") != 0 or not isinstance(obj.get("data"), dict):
        raise RuntimeError("refresh code=%s msg=%s" % (obj.get("code"), obj.get("msg")))
    data = obj["data"]
    at, nrt = data.get("accessToken") or "", data.get("refreshToken") or ""
    if not at:
        raise RuntimeError("refresh response has no accessToken")
    return at, nrt


def wx_alert(text):
    """Best-effort WeChat alert on renewal failure (never raises)."""
    t = os.environ.get("WXPUSHER_TOKEN", "")
    u = os.environ.get("WXPUSHER_UID", "")
    if not (t and u):
        return
    try:
        body = json.dumps({"appToken": t, "uids": [u], "content": text,
                           "contentType": 1}).encode("utf-8")
        req = urllib.request.Request(
            "https://wxpusher.zjiecode.com/api/send/message",
            data=body, method="POST",
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).read()
    except Exception:
        pass


def main():
    key = b""
    if ENC_KEY_B64:
        try:
            key = base64.b64decode(ENC_KEY_B64)
        except Exception:
            warn("RT_ENC_KEY 不是合法 base64，本次不写 state")
    if not GH_TOKEN:
        warn("缺少 GITHUB_TOKEN，无法读写 state/rt.json，回退静态令牌")
        return 0

    rt, sha = (None, None)
    if key:
        rt, sha = read_state(key)
        log("state/rt.json: %s" % ("已读取轮换令牌" if rt else "无有效记录，使用 Secret 种子"))
    rt = rt or SEED_RT
    if not rt:
        warn("没有可用的 refresh_token，跳过自续期（沿用 Secret 静态令牌）")
        wx_alert("⚠️ Buddy 自动续期未配置 refresh_token，令牌将按原有效期到期。")
        return 0

    try:
        at, nrt = refresh(rt)
    except Exception as e:
        warn("刷新失败，回退静态令牌：%s" % str(e)[:200])
        wx_alert("⚠️ Buddy 令牌自动续期失败：%s。今日仍尝试用现有令牌签到。" % str(e)[:120])
        return 0

    log("刷新成功：access_token 长度 %d，refresh_token 长度 %d" % (len(at), len(nrt or "")))
    env_path = os.environ.get("GITHUB_ENV")
    if env_path:
        try:
            with open(env_path, "a", encoding="utf-8") as f:
                f.write("WB_LIVE_ACCESS_TOKEN=%s\n" % at)
        except Exception as e:
            warn("写 GITHUB_ENV 失败：%s" % e)
            wx_alert("⚠️ Buddy 新令牌未能注入运行环境变量：%s" % str(e)[:120])
            return 0
    else:
        warn("无 GITHUB_ENV（非 Actions 环境），跳过注入")

    if key and nrt:
        try:
            ok = write_state(key, nrt, sha)
            log("轮换令牌写回 state/rt.json：%s" % ("成功" if ok else "未确认"))
            if not ok:
                wx_alert("⚠️ Buddy 轮换令牌未能写回 state/rt.json，请检查仓库权限。")
        except Exception as e:
            warn("轮换令牌写回失败（今日签到不受影响）：%s" % str(e)[:200])
            wx_alert("⚠️ Buddy 轮换令牌写回失败：%s" % str(e)[:120])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:
        warn("未捕获异常，回退静态令牌：%s" % str(e)[:200])
        raise SystemExit(0)
