"""
astrbot_plugin_sponsor_pass 逻辑测试（stub 掉 astrbot 依赖，可在任意环境运行）
运行：python3 tests/test_sponsor_pass.py
"""

import asyncio
import importlib.util
import logging
import os
import sys
import time
import types

logging.basicConfig(level=logging.CRITICAL)

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------- astrbot 模块桩 ----------------

astrbot_pkg = types.ModuleType("astrbot")
api_pkg = types.ModuleType("astrbot.api")
event_pkg = types.ModuleType("astrbot.api.event")
star_pkg = types.ModuleType("astrbot.api.star")
api_msg_pkg = types.ModuleType("astrbot.api.message_components")

sys.modules["astrbot"] = astrbot_pkg
sys.modules["astrbot.api"] = api_pkg
sys.modules["astrbot.api.event"] = event_pkg
sys.modules["astrbot.api.star"] = star_pkg
sys.modules["astrbot.api.message_components"] = api_msg_pkg


class _AstrBotConfig(dict):
    def __init__(self, **kw):
        super().__init__()
        self.update(kw)
        self.saved = 0

    def get(self, k, d=None):
        return dict.get(self, k, d)

    def save_config(self):
        self.saved += 1


class _Logger:
    def debug(self, *a, **k):
        pass

    info = warning = error = debug


api_pkg.AstrBotConfig = _AstrBotConfig
api_pkg.logger = _Logger()


class _MessageChain:
    def __init__(self, chain=None):
        self.chain = chain or []

    def message(self, t):
        self.chain.append(t)
        return self


class _MessageEventResult(_MessageChain):
    def __init__(self):
        super().__init__()
        self.stopped = False

    def stop_event(self):
        self.stopped = True
        return self


event_pkg.AstrMessageEvent = type("AstrMessageEvent", (), {})
event_pkg.MessageChain = _MessageChain
event_pkg.MessageEventResult = _MessageEventResult


class _Filter:
    HANDLERS = []
    COMMANDS = {}

    def event_message_type(self, event_type, priority=0):
        def deco(fn):
            _Filter.HANDLERS.append((fn, priority))
            return fn
        return deco

    def command(self, name, **kw):
        def deco(fn):
            _Filter.COMMANDS[name] = fn
            return fn
        return deco

    def permission_type(self, pt):
        def deco(fn):
            return fn
        return deco


filter_obj = _Filter()
event_pkg.filter = filter_obj
event_pkg.MessageEventResult = _MessageEventResult

_EVENT_TYPE = types.SimpleNamespace(ALL="ALL", PRIVATE_MESSAGE="PRIVATE", GROUP_MESSAGE="GROUP")
filter_obj.EventMessageType = _EVENT_TYPE
filter_obj.PermissionType = types.SimpleNamespace(ADMIN="ADMIN")

star_pkg.Context = type("Context", (), {})
star_pkg.Star = type("Star", (), {"__init__": lambda self, context=None: setattr(self, "context", context)})


def _register(*a, **k):
    def deco(cls):
        return cls
    return deco


star_pkg.register = _register

# ---------------- 导入插件 ----------------

spec = importlib.util.spec_from_file_location("sp_main", f"{PLUGIN_DIR}/main.py")
main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(main)

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f" FAIL {name} {extra}")


def make_plugin(**cfg):
    conf = _AstrBotConfig(
        enable=True,
        whitelist=[],
        max_rounds=6,
        round_window=180,
        min_amount=5,
        check_pages=3,
        check_cooldown=300,
        notify_target="",
        afdian_url="https://afdian.com/a/test",
        afdian_user_id="uid123",
        afdian_token="tok",
    )
    conf.update(cfg)
    return main.SponsorPassPlugin(context=None, config=conf)


class _MT:
    def __init__(self, name):
        self._n = name

    def __str__(self):
        return self._n


class FakeEvent:
    def __init__(self, sender="10001", text="hi", ts=None,
                 mtype="MessageType.FRIEND_MESSAGE", umo=None, raw=None):
        self._sender = str(sender)
        self.message_str = text
        t = int(ts) if ts is not None else int(time.time())
        self.message_obj = types.SimpleNamespace(
            type=_MT(mtype), time=t, message=[], raw_message=raw
        )
        self.unified_msg_origin = umo or f"aiocqhttp:FriendMessage:{sender}"
        self.results = []
        self.stopped = False

    def get_message_type(self):
        return self.message_obj.type

    def get_sender_id(self):
        return self._sender

    def set_result(self, r):
        self.results.append(r)
        # 与真实 AstrBot 一致：结果对象调用 stop_event() 意味着事件停止传播
        if getattr(r, "stopped", False):
            self.stopped = True

    def stop_event(self):
        self.stopped = True

    def plain_result(self, t):
        return _MessageEventResult().message(t)

    def reply_text(self):
        return "".join("".join(r.chain) for r in self.results)


HANDLER = None
for fn, prio in _Filter.HANDLERS:
    HANDLER = fn
assert HANDLER is not None, "未找到事件处理器"


def run(coro):
    return asyncio.run(coro)


# 时间基准：必须晚于插件 _start_ts - 60，否则会被判定为历史消息
T0 = int(time.time())
W = 180

# ---------------- 用例 ----------------

print("== 签名与工具函数 ==")
check("T01 爱发电官方签名向量",
      main.afdian_sign("123", "abc", '{"a":333}', 1624339905) == "a4acc28b81598b7e5d84ebdc3e91710c")
check("T02 留言含独立QQ匹配", main.remark_has_qq("QQ123456 来了", "123456"))
check("T03 前缀数字不误匹配", not main.remark_has_qq("1123456 abc", "123456"))
check("T04 空留言不匹配", not main.remark_has_qq("", "123456"))

print("== 轮次与放行 ==")
pl = make_plugin()
for i in range(10):  # 窗口内连发 10 条 = 1 轮
    ev = FakeEvent(ts=T0 + i)
    run(HANDLER(pl, ev))
check("T05 连发10条只算1轮且全放行", pl._rounds.get("aiocqhttp:FriendMessage:10001") == 1 and not ev.stopped and not ev.results)

pl = make_plugin()
gate_seen = False
for r in range(8):  # 8 轮，每轮一条，间隔超窗口
    ev = FakeEvent(ts=T0 + r * (W + 10), text=f"轮{r}")
    run(HANDLER(pl, ev))
    if ev.results:
        gate_seen = True
        gate_round = r
check("T06 第7轮触发门槛提示", gate_seen and gate_round == 6)
check("T07 门槛消息已停止传播", ev.stopped)

print("== 白名单 ==")
pl = make_plugin(whitelist=["2745978770"])
for r in range(10):
    ev = FakeEvent(sender="2745978770", ts=T0 + r * (W + 10))
    run(HANDLER(pl, ev))
check("T08 白名单用户永不拦截", not ev.stopped and not ev.results)

print("== 非好友/历史消息 ==")
pl = make_plugin()
ev = FakeEvent(mtype="MessageType.GROUP_MESSAGE", ts=T0)
run(HANDLER(pl, ev))
check("T09 群消息完全忽略", not ev.stopped and not ev.results and not pl._rounds)
ev = FakeEvent(ts=pl._start_ts - 3600)
run(HANDLER(pl, ev))
check("T10 历史消息静默拦截且不计数", ev.stopped and not ev.results and not pl._rounds)

print("== 拦截态行为 ==")
pl = make_plugin(afdian_url="", afdian_user_id="", afdian_token="")  # 赞助未配置
for r in range(7):
    ev = FakeEvent(ts=T0 + r * (W + 10))
    run(HANDLER(pl, ev))
check("T11 未配置赞助时门槛文案不含赞助入口", "奶茶" not in ev.reply_text() and "问问我哥" in ev.reply_text())
umo = ev.unified_msg_origin
ev = FakeEvent(ts=T0 + 99999, text="在吗")
run(HANDLER(pl, ev))
check("T12 拦截态普通消息静默(同日不重复提示)", ev.stopped and not ev.results)
ev = FakeEvent(ts=T0 + 99999, text="我赞助了")
run(HANDLER(pl, ev))
check("T13 未配置赞助时赞助声明静默处理", ev.stopped and not ev.results)

print("== 赞助校验 ==")
pl = make_plugin()
order = {"out_trade_no": "X1", "status": 2, "total_amount": "10.00", "remark": f"QQ123456 赞助"}
async def fake_find(qq, uid, token):
    return order if qq == "123456" else None
pl._find_order = fake_find
for r in range(7):
    ev = FakeEvent(sender="123456", ts=T0 + r * (W + 10))
    run(HANDLER(pl, ev))
ev = FakeEvent(sender="123456", ts=T0 + 99999, text="我赞助了")
run(HANDLER(pl, ev))
check("T14 校验通过加白名单", "123456" in pl._whitelist())
check("T15 校验通过回复成功文案", "感谢老板" in ev.reply_text())
check("T16 校验通过后解除拦截并保存配置", "123456" not in pl._blocked and pl.config.saved >= 1)
ev = FakeEvent(sender="123456", ts=T0 + 99999 + 5, text="嗨")
run(HANDLER(pl, ev))
check("T17 通过后恢复自由对话", not ev.stopped and not ev.results)

pl = make_plugin()
async def fake_none(qq, uid, token):
    return None
pl._find_order = fake_none
for r in range(7):
    ev = FakeEvent(sender="654321", ts=T0 + r * (W + 10))
    run(HANDLER(pl, ev))
ev = FakeEvent(sender="654321", ts=T0 + 99999, text="我赞助了")
run(HANDLER(pl, ev))
check("T18 查无订单给出核对提示", "654321" in ev.reply_text() and "5 元" in ev.reply_text())
ev2 = FakeEvent(sender="654321", ts=T0 + 99999 + 10, text="我赞助了")
run(HANDLER(pl, ev2))
check("T19 冷却期内不再调用接口", "稍等" in ev2.reply_text())

print("== 订单匹配规则 ==")
pl = make_plugin()
QQ = "123456"
fetches = []
async def fake_fetch(host, uid, token, page, per_page):
    fetches.append((host, page))
    if page == 1:
        return {"ec": 200, "data": {"list": [
            {"out_trade_no": "A1", "status": 2, "total_amount": "10.00", "remark": "送给朋友"},
            {"out_trade_no": "A2", "status": 1, "total_amount": "10.00", "remark": QQ},
            {"out_trade_no": "A3", "status": 2, "total_amount": "1.00", "remark": QQ},
            {"out_trade_no": "A4", "status": 2, "total_amount": "10.00", "remark": "1123456 abc"},
            {"out_trade_no": "A5", "status": 2, "total_amount": "10.00", "remark": f"备注{QQ}"},
        ], "total_count": 5}}
    raise AssertionError("不应翻页")
pl._fetch_order_page = fake_fetch
got = run(pl._find_order(QQ, "uid", "tok"))
check("T20 状态/金额/留言三重过滤后命中A5", got and got.get("out_trade_no") == "A5", f"got={got}")

pl = make_plugin()
async def fake_fetch2(host, uid, token, page, per_page):
    if host == main.AFDIAN_API_HOSTS[0]:
        raise RuntimeError("host1 down")
    return {"ec": 200, "data": {"list": [
        {"out_trade_no": "B1", "status": 2, "total_amount": "6.00", "remark": QQ},
    ], "total_count": 1}}
pl._fetch_order_page = fake_fetch2
got = run(pl._find_order(QQ, "uid", "tok"))
check("T21 主域名失败自动切备用域名", got and got.get("out_trade_no") == "B1")

print("== 管理命令 ==")
async def run_cmd(pl, ev, action, qq):
    async for r in main.SponsorPassPlugin.admission(pl, ev, action, qq):
        ev.results.append(r)
pl = make_plugin()
ev = FakeEvent(sender="8888")
run(run_cmd(pl, ev, "同意", "123456"))
check("T22 /准入 同意 写入白名单", "123456" in pl._whitelist() and pl.config.saved >= 1)
run(run_cmd(pl, ev, "列表", ""))
check("T23 /准入 列表 展示", "123456" in ev.reply_text())
run(run_cmd(pl, ev, "移除", "123456"))
check("T24 /准入 移除", "123456" not in pl._whitelist())
ev = FakeEvent(sender="8888")
run(run_cmd(pl, ev, "同意", ""))
check("T25 参数缺失给用法", "用法" in ev.reply_text())

print("== 请求事件与临时会话 ==")
pl = make_plugin()
ev = FakeEvent(ts=T0, raw={"post_type": "request"})
run(HANDLER(pl, ev))
check("T26 好友申请事件独立放行不计数", not ev.stopped and not ev.results and not pl._rounds)

pl = make_plugin()
for r in range(7):
    ev = FakeEvent(ts=T0 + r * (W + 10), mtype="MessageType.OTHER_MESSAGE")
    run(HANDLER(pl, ev))
check("T27 临时会话也纳入准入", ev.stopped and bool(ev.results))

pl = make_plugin(enable_temp_session=False)
ev = FakeEvent(ts=T0, mtype="MessageType.OTHER_MESSAGE")
run(HANDLER(pl, ev))
check("T28 关闭临时会话准入则忽略", not ev.stopped and not ev.results and not pl._rounds)

pl = make_plugin()
for r in range(3):
    ev = FakeEvent(sender="7777", ts=T0 + r * (W + 10))
    run(HANDLER(pl, ev))
ev = FakeEvent(sender="8888")
run(run_cmd(pl, ev, "状态", "7777"))
check("T29 /准入 状态 查询", "第 3/6 轮" in ev.reply_text())

pl = make_plugin()
ev = FakeEvent(ts=T0, text="")
run(HANDLER(pl, ev))
check("T30 空消息体事件不计数", not pl._rounds and not ev.results)

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
