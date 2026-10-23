"""推送 payload 结构单测：不实际发送，只验证飞书卡片与微信 HTML 的分节结构。"""
import importlib.util
import json
import sys

spec = importlib.util.spec_from_file_location(
    "buddy_daily",
    r"C:\Users\zhouy1\WorkBuddy\2026-10-19-16-33-46\workbuddy-buddy-automation\scripts\buddy_daily.py")
bd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bd)

checkin = {"action": "claim", "success": True, "today_credit": 100, "streak_days": 17,
           "message": "签到成功"}
remaining = "总结分 901.06/2500；附加额度 0/500"
cat = {"success": True, "buddy_name": "小喵", "state": "traveling",
       "claim": {"reward_credit": 10, "letter": "玩的开心"},
       "depart": {"location": "咖啡馆", "location_id": 1},
       "notes": ["猫猫已到家，领取旅行积分 10 分", "已派猫猫前往「咖啡馆」旅行"]}

title, ck, catl, html = bd.build_report(checkin, remaining, cat)
print("== 标题 ==", title)
print("== 签到行 ==")
for l in ck:
    print("  ", l)
print("== 猫猫行 ==")
for l in catl:
    print("  ", l)

# 飞书卡片结构验证
import urllib.request
captured = {}
orig = urllib.request.Request
def fake_request(url, data=None, method=None, headers=None):
    captured["url"] = url
    captured["body"] = json.loads(data.decode("utf-8"))
    raise SystemExit  # 中断，不真正发送
urllib.request.Request = fake_request
try:
    bd.push_feishu("https://open.feishu.cn/open-apis/bot/v2/hook/fake", title, ck, catl)
except SystemExit:
    pass
urllib.request.Request = orig
card = captured["body"]
elems = card["card"]["elements"]
texts = [e.get("text", {}).get("content", "") for e in elems if e.get("tag") == "div"]
print("== 飞书卡片分节验证 ==")
print("  包含『签到结果』节:", any("签到结果" in t for t in texts))
print("  包含『猫猫状态』节:", any("猫猫状态" in t for t in texts))
print("  两节内容不为空:", all(t.strip() for t in texts))

# 微信 HTML 分节验证
print("== 微信 HTML 分节验证 ==")
print("  含签到节标题:", "<h4>📅 签到结果</h4>" in html)
print("  含猫猫节标题:", "<h4>🐱 猫猫状态</h4>" in html)
print("  含关键内容:", ("+100 积分" in html) and ("咖啡馆" in html) and ("901.06" in html))

# 脱敏函数验证
print("== 脱敏验证 ==")
sample = {"auth": {"accessToken": "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9abcdefghijklmn"},
          "data": {"credit": 100, "nested": [{"refresh_token": "rt_value_1234567890"}]}}
s = bd.sanitize(sample)
print("  accessToken 脱敏:", s["auth"]["accessToken"])
print("  refresh_token 脱敏:", s["data"]["nested"][0]["refresh_token"])
print("  非敏感字段保留:", s["data"]["credit"] == 100)
