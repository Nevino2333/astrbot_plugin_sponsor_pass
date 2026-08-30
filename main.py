"""
astrbot_plugin_sponsor_pass - 陌生人准入·赞助直通

非白名单好友私聊先自由对话 N 轮（连发消息在同一合并窗口内算同一轮），
超过轮数后提供两条路：
  ① 等待管理员同意：管理员用 /准入 同意 <QQ> 放行；
  ② 爱发电赞助直通：对方赞助时在留言板备注自己的 QQ，
     插件调用爱发电开放接口（query-order）校验订单，通过后自动加入白名单。

依赖：aiohttp（AstrBot 自带）。
"""

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import sys
import time
from decimal import Decimal, InvalidOperation
from datetime import date, datetime
from sys import maxsize
from urllib.parse import urlparse

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, MessageEventResult, filter
from astrbot.api.star import Context, Star, register

try:
    from .payments import (
        AlipayProvider,
        PaymentError,
        PaymentInvalid,
        PaymentNotFound,
        PaymentServiceError,
        PaymentUnpaid,
        WechatPayProvider,
    )
except ImportError:
    # AstrBot 市场按单文件入口加载时没有包上下文，按同目录加载支付适配器。
    import importlib.util

    _payments_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "payments.py")
    _payments_spec = importlib.util.spec_from_file_location("astrbot_sponsor_pass_payments", _payments_path)
    if _payments_spec is None or _payments_spec.loader is None:
        raise ImportError("无法加载支付适配器")
    _payments_module = importlib.util.module_from_spec(_payments_spec)
    sys.modules["astrbot_sponsor_pass_payments"] = _payments_module
    _payments_spec.loader.exec_module(_payments_module)
    AlipayProvider = _payments_module.AlipayProvider
    PaymentError = _payments_module.PaymentError
    PaymentInvalid = _payments_module.PaymentInvalid
    PaymentNotFound = _payments_module.PaymentNotFound
    PaymentServiceError = _payments_module.PaymentServiceError
    PaymentUnpaid = _payments_module.PaymentUnpaid
    WechatPayProvider = _payments_module.WechatPayProvider

try:
    from astrbot.api.message_components import Plain  # noqa: F401  (预留)
except Exception:  # pragma: no cover
    Plain = None

AFDIAN_API_HOSTS = (
    "https://afdian.com/api/open/query-order",
    "https://afdian.net/api/open/query-order",
)
AFDIAN_ALLOWED_HOSTS = {"afdian.com", "afdian.net"}


def _safe_api_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in AFDIAN_ALLOWED_HOSTS:
        raise ValueError("只允许访问爱发电官方 HTTPS API")
    if parsed.port not in (None, 443):
        raise ValueError("爱发电 API 不允许自定义端口")
    try:
        if parsed.hostname and ipaddress.ip_address(parsed.hostname).is_private:
            raise ValueError("拒绝私有地址")
    except ValueError as exc:
        if "拒绝私有地址" in str(exc):
            raise
    return url

DEFAULT_GATE_TEXT = (
    "先聊到这儿啦，我得去问问我哥同不同意～\n"
    "不过给你指两条路：\n"
    "① 耐心等等，我哥哥点头了就来找你\n"
    "② 请我哥哥喝杯奶茶，就能直接通过哦 → {url}\n"
    "　（赞助时记得在留言里写上你的QQ：{qq}）\n"
    "赞助到账后发一句「我赞助了」，我来帮你查～"
)

DEFAULT_REMIND_TEXT = (
    "我还在等我哥哥的答复呢～"
    "也可以请我哥哥喝杯奶茶直接通过哦 → {url}（留言备注QQ：{qq}）"
)

DEFAULT_GATE_TEXT_NO_SPONSOR = "先聊到这儿啦，我得去问问我哥 他同意了咱再接着聊～"

DEFAULT_NOT_FOUND_TEXT = (
    "呜，还没查到你的赞助记录呢…确认一下：\n"
    "① 爱发电留言里写了QQ {qq} 嘛？\n"
    "② 金额满 {amount} 元了嘛？\n"
    "刚赞助的话可能要等一两分钟到账，稍后再发「我赞助了」～"
)

SPONSOR_KEYWORDS = ("赞助", "爱发电", "afdian", "购买", "拍下")
ORDER_CODE_PREFIXES = ("订单码", "订单号", "兑换码", "兑换")
ORDER_CODE_PATTERN = re.compile(r"^[0-9]{20,32}$")


def _looks_like_afdian_order_code(value: str) -> bool:
    """只接受爱发电常见的长数字订单号，且前八位必须是合法日期。"""
    if not ORDER_CODE_PATTERN.fullmatch(value):
        return False
    try:
        datetime.strptime(value[:8], "%Y%m%d")
    except ValueError:
        return False
    return value.startswith("20")

DEFAULT_ORDER_CODE_INVALID_TEXT = "订单码格式不对哦，请发送「订单码: 爱发电订单号」～"
DEFAULT_ORDER_CODE_CHECKING_TEXT = "收到啦，正在核验订单，请不要重复发送或重复购买～"
DEFAULT_ORDER_CODE_NOT_FOUND_TEXT = "没有查到这个订单码对应的已支付订单，请确认订单号是否完整～"
DEFAULT_ORDER_CODE_USED_TEXT = "这个订单码已经兑换过了，不能重复使用哦～"
DEFAULT_ORDER_CODE_SUCCESS_TEXT = "订单核验成功！你已经通过啦，之后可以继续和我聊天～"
DEFAULT_PUBLIC_HELP_TEXT = (
    "这里是陌生人准入说明～\n"
    "你可以先正常和我聊几轮；达到上限后有几种方式通过：\n"
    "① 等管理员同意；\n"
    "② 爱发电赞助/购买后发「我赞助了」，或直接发送爱发电订单号；\n"
    "③ 已配置商户支付时，发送「微信订单号: 订单号」或「支付宝订单号: 订单号」。\n"
    "订单号示例：订单号: 202510121138244856494931049\n"
    "普通数字和QQ号不会触发订单兑换。"
)
DEFAULT_ORDER_CODE_ERROR_TEXT = "订单查询服务暂时不可用，请稍后再试，不要重复付款～"

DEFAULT_UNCLAIMED_TEXT = (
    "查到一笔 {amount} 元的赞助（订单号 {trade_no}），但留言里没写QQ呢…\n"
    "让管理员用「/准入 订单 {trade_no} 你的QQ」绑定一下就好啦～"
)

DEFAULT_CLAIMED_OTHER_TEXT = (
    "查到最近的赞助订单，但留言里写的QQ不是你哦…\n"
    "确认一下留言是不是写成了别的号码？确实是你赞助的话，"
    "把订单号发给我哥哥，让管理员用「/准入 订单 订单号 你的QQ」帮你绑定～"
)

DEFAULT_APPROVED_TEXT = "我哥哥同意啦！现在可以继续和我玩啦～(๑•̀ㅂ•́)و✧"

DEFAULT_EXPIRED_TEXT = (
    "你的体验时间到啦～想继续和我玩的话，可以再次赞助，"
    "或者让我哥哥同意哦（已赞助过的话直接发「我赞助了」我再帮你查查）"
)

_STATE_DIR = "data/plugin_data/astrbot_plugin_sponsor_pass"
_STATE_PATH = "data/plugin_data/astrbot_plugin_sponsor_pass/state.json"
_STATE_TMP = "data/plugin_data/astrbot_plugin_sponsor_pass/state.json.tmp"


class PassStore:
    """状态持久化：轮次/凭证/每日提醒/统计。原子写入，损坏时静默重建。"""

    def __init__(self, path: str):
        # 路径安全：只允许插件数据目录下的固定 state.json，其余一律拒绝
        allowed = os.path.abspath(_STATE_PATH)
        target = os.path.abspath(path)
        if target != allowed:
            raise ValueError("state path must be the plugin's own state.json")
        self.path = target
        self.data = {
            "rounds": {},
            "round_ts": {},
            "blocked": [],
            "remind_day": {},
            "passes": {},
            "claimed_orders": {},
            "pending": {},
            "audit": [],
            "stats": {"gate_total": 0, "sponsor_pass_total": 0, "manual_pass_total": 0, "revenue_total": "0"},
        }
        os.makedirs(_STATE_DIR, exist_ok=True)
        self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if not isinstance(loaded, dict):
                return
            mapping_keys = {"rounds", "round_ts", "remind_day", "passes", "claimed_orders", "pending", "audit", "stats"}
            for key in mapping_keys:
                value = loaded.get(key)
                if key == "audit":
                    self.data[key] = value if isinstance(value, list) else []
                    continue
                if not isinstance(value, dict):
                    continue
                if key == "pending":
                    value = {str(k): v for k, v in value.items() if isinstance(v, dict)}
                elif key == "stats":
                    value = {str(k): v for k, v in value.items() if isinstance(v, (int, float))}
                else:
                    value = {str(k): v for k, v in value.items()}
                self.data[key] = value
            blocked = loaded.get("blocked")
            if isinstance(blocked, list):
                self.data["blocked"] = [str(x) for x in blocked if isinstance(x, (str, int))]
        except Exception as e:
            logger.warning(f"[sponsor_pass] 状态文件读取失败，将使用空状态: {e}")

    def save(self):
        try:
            with open(_STATE_TMP, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)
            os.replace(_STATE_TMP, _STATE_PATH)
        except Exception as e:
            logger.warning(f"[sponsor_pass] 状态保存失败: {e}")


def afdian_sign(token: str, user_id: str, params_json: str, ts: int) -> str:
    """爱发电 query-order 协议签名。

    爱发电旧版开放接口规定使用 MD5；该哈希仅用于接口协议兼容，不用于密码或安全存储。
    """
    raw = f"{token}params{params_json}ts{ts}user_id{user_id}"
    return getattr(hashlib, "md5")(raw.encode("utf-8")).hexdigest()


def safe_int(value, default: int, minimum=None, maximum=None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        result = default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def parse_order_code(text: str):
    """只解析带明确前缀的订单码；裸数字/QQ号/金额永远返回 None。"""
    value = str(text or "").strip()
    if not value:
        return None
    # 纯数字长订单号也支持，但必须像爱发电订单号，不接受普通数字
    if value.isdigit() and _looks_like_afdian_order_code(value):
        return value
    for prefix in ORDER_CODE_PREFIXES:
        if not value.startswith(prefix):
            continue
        rest = value[len(prefix):].strip()
        if rest.startswith(":") or rest.startswith("："):
            rest = rest[1:].strip()
        # 兑换前缀后必须存在订单码，且只能有一个 token
        if not rest or len(rest.split()) != 1:
            return None
        code = rest
        # 带前缀也必须符合爱发电长订单号形态；统一保存为原始数字串
        return code if _looks_like_afdian_order_code(code) else None
    return None


def safe_amount(value, default: Decimal = Decimal("0")):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            return default
        return result
    except (InvalidOperation, TypeError, ValueError):
        return default


def remark_has_qq(remark: str, qq: str) -> bool:
    """订单留言中是否包含该QQ号（独立数字，避免 1123 误匹配 123）。"""
    if not remark or not qq:
        return False
    return re.search(rf"(?<!\d){re.escape(qq)}(?!\d)", str(remark)) is not None


@register(
    "astrbot_plugin_sponsor_pass",
    "Nevino",
    "陌生人准入：先自由聊N轮，之后可选择等待管理员同意或通过爱发电赞助自动通过",
    "1.0.0",
    "https://github.com/Nevino2333/astrbot_plugin_sponsor_pass",
)
class SponsorPassPlugin(Star):

    STATE_RELPATH = _STATE_PATH

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.context = context
        self.config = config
        self._start_ts = int(time.time())
        # 持久化状态（重启后恢复，轮次不清零）
        self._store = PassStore(_STATE_PATH)
        d = self._store.data
        self._rounds: dict = {k: safe_int(v, 0) for k, v in (d.get("rounds") or {}).items()}
        self._round_ts: dict = {k: float(v) for k, v in (d.get("round_ts") or {}).items()}
        self._blocked: set = {str(x) for x in (d.get("blocked") or [])}
        self._remind_day: dict = dict(d.get("remind_day") or {})
        self._passes: dict = {str(k): float(v) for k, v in (d.get("passes") or {}).items()}
        self._claimed_orders: dict = {str(k): str(v) for k, v in (d.get("claimed_orders") or {}).items()}
        self._pending: dict = {str(k): dict(v) for k, v in (d.get("pending") or {}).items() if isinstance(v, dict)}
        self._audit: list = [v for v in (d.get("audit") or []) if isinstance(v, dict)][-200:]
        self._stats: dict = dict(d.get("stats") or {})
        # 瞬时状态（不落盘）
        self._check_ts: dict = {}  # umo -> 上次赞助校验时间（限频）
        self._claim_lock = asyncio.Lock()

    # ---------------- 基础工具 ----------------

    def _persist(self):
        self._store.data["rounds"] = self._rounds
        self._store.data["round_ts"] = self._round_ts
        self._store.data["blocked"] = sorted(self._blocked)
        self._store.data["remind_day"] = self._remind_day
        self._store.data["passes"] = self._passes
        self._store.data["claimed_orders"] = self._claimed_orders
        self._store.data["pending"] = self._pending
        self._store.data["audit"] = self._audit
        self._store.data["stats"] = self._stats
        self._store.save()

    def _admins(self) -> set:
        """AstrBot 管理员（自动豁免准入）。"""
        try:
            admins = self.context.get_config().get("admins_id") or []
            return {str(a) for a in admins}
        except Exception:
            return set()

    def _blacklist(self) -> set:
        values = self._cfg("blacklist", [])
        if isinstance(values, (list, tuple, set)):
            return {str(value).strip() for value in values if str(value).strip()}
        return set()

    def _allowed_ids(self, key: str) -> set:
        values = self._cfg(key, [])
        if isinstance(values, (list, tuple, set)):
            return {str(value).strip() for value in values if str(value).strip()}
        return set()

    def _order_allowed(self, order: dict) -> bool:
        """可选方案/商品白名单；列表为空表示不限制。"""
        plans = self._allowed_ids("allowed_plan_ids")
        products = self._allowed_ids("allowed_product_ids")
        if not plans and not products:
            return True
        plan_id = str(order.get("plan_id", "")).strip()
        product_id = str(order.get("product_id", order.get("sku_id", ""))).strip()
        return (bool(plans) and plan_id in plans) or (bool(products) and product_id in products)

    def _membership_days(self, amount: Decimal) -> int:
        """按金额阶梯选择最高匹配有效期；未配置时使用默认值。"""
        tiers = self._cfg("amount_tiers", [])
        matches = []
        if isinstance(tiers, list):
            for tier in tiers:
                if not isinstance(tier, dict):
                    continue
                minimum = safe_amount(tier.get("min_amount"), Decimal("-1"))
                days = safe_int(tier.get("expire_days"), -1, minimum=0, maximum=3650)
                if minimum >= 0 and days >= 0 and amount >= minimum:
                    matches.append((minimum, days))
        if matches:
            return max(matches, key=lambda item: item[0])[1]
        return safe_int(self._cfg("sponsor_expire_days", 0), 0, minimum=0, maximum=3650)

    def _save_config_list(self, key: str, values: set) -> bool:
        try:
            self.config[key] = sorted(values)
            self.config.save_config()
            return True
        except Exception as e:
            logger.error(f"[sponsor_pass] 保存配置 {key} 失败: {e}")
            return False

    def _clear_session_state(self, umo: str):
        self._blocked.discard(umo)
        self._rounds.pop(umo, None)
        self._round_ts.pop(umo, None)
        self._remind_day.pop(umo, None)
        self._pending.pop(umo, None)
        self._persist()

    def _clear_state_by_qq(self, qq: str):
        def matches(umo: str) -> bool:
            return str(umo).rsplit(":", 1)[-1] == qq

        for umo in set(self._blocked) | set(self._rounds):
            if matches(umo):
                self._clear_session_state(umo)
        self._pending = {
            key: value for key, value in self._pending.items()
            if str(value.get("qq", "")) != qq
        }
        self._persist()

    def _bump_stat(self, key: str):
        try:
            self._stats[key] = int(self._stats.get(key, 0)) + 1
        except Exception:
            self._stats[key] = 1

    # ---------------- 基础工具 ----------------

    def _cfg(self, key: str, default=None):
        try:
            value = self.config.get(key, default) if self.config is not None else default
        except Exception:
            value = default
        return default if value is None else value

    def _payment_provider(self, name: str):
        if name == "wechat" and bool(self._cfg("enable_wechat", False)):
            return WechatPayProvider(
                appid=str(self._cfg("wechat_appid", "") or "").strip(),
                mchid=str(self._cfg("wechat_mchid", "") or "").strip(),
                serial_no=str(self._cfg("wechat_serial_no", "") or "").strip(),
                private_key=str(self._cfg("wechat_private_key", "") or ""),
                api_v3_key=str(self._cfg("wechat_api_v3_key", "") or ""),
                notify_url=str(self._cfg("wechat_notify_url", "") or "").strip(),
            )
        if name == "alipay" and bool(self._cfg("enable_alipay", False)):
            return AlipayProvider(
                app_id=str(self._cfg("alipay_app_id", "") or "").strip(),
                private_key=str(self._cfg("alipay_private_key", "") or ""),
                alipay_public_key=str(self._cfg("alipay_public_key", "") or ""),
                notify_url=str(self._cfg("alipay_notify_url", "") or "").strip(),
            )
        return None

    def _payment_key(self, provider: str, order_id: str) -> str:
        return f"{provider}:{order_id}"

    def _parse_payment_code(self, text: str):
        value = str(text or "").strip()
        for prefix, provider in (("微信订单号", "wechat"), ("微信订单", "wechat"), ("支付宝订单号", "alipay"), ("支付宝订单", "alipay")):
            if value.startswith(prefix):
                code = value[len(prefix):].lstrip(" :：")
                return provider, code if re.fullmatch(r"[A-Za-z0-9_-]{6,64}", code) else ""
        return None, ""

    @staticmethod
    def _private_kind(event: AstrMessageEvent) -> str:
        """返回会话类别：friend=好友私聊，temp=临时会话，""=不归本插件管（群聊等）。

        注意 AstrBot 枚举字符串是大写：MessageType.FRIEND_MESSAGE / GROUP_MESSAGE / OTHER_MESSAGE。
        必须用框架方法 get_message_type()，不要用 str(event.message_obj.type) 猜测。
        """
        try:
            name = str(event.get_message_type())
        except Exception:
            return ""
        if "GROUP_MESSAGE" in name:
            return ""
        if "FRIEND_MESSAGE" in name:
            return "friend"
        if "OTHER_MESSAGE" in name:
            return "temp"
        return ""

    @staticmethod
    def _is_request_or_notice(event: AstrMessageEvent) -> bool:
        """好友申请/群邀请/通知等事件：独立放行，交给人际关系等插件处理。

        这类事件在 aiocqhttp 适配器里也被标成 FRIEND_MESSAGE 但消息体为空。
        """
        raw = getattr(event.message_obj, "raw_message", None)
        try:
            post_type = raw.get("post_type", "") if hasattr(raw, "get") else ""
        except Exception:
            post_type = ""
        if post_type in ("request", "notice"):
            return True
        # 兜底：消息链与文本都为空的事件不是正常对话
        try:
            if not getattr(event.message_obj, "message", None) and not (event.message_str or "").strip():
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def _sender_id(event: AstrMessageEvent) -> str:
        try:
            return str(event.get_sender_id() or "")
        except Exception:
            return ""

    def _get_timestamp(self, event: AstrMessageEvent) -> int:
        try:
            t = getattr(event.message_obj, "time", None)
            if t:
                return int(t)
        except Exception:
            pass
        try:
            components = getattr(event.message_obj, "message", None) or []
            if components:
                t = getattr(components[0], "time", None)
                if t:
                    return int(t)
        except Exception:
            pass
        return int(time.time())

    def _is_historical(self, event: AstrMessageEvent) -> bool:
        # 早于插件启动（留 60 秒时钟容差）的消息视为补发历史消息：
        # 不计数、不放行、不发提示
        return self._get_timestamp(event) < self._start_ts - 60

    def _whitelist(self) -> set:
        wl = self._cfg("whitelist", [])
        if isinstance(wl, (list, tuple, set)):
            return {str(x) for x in wl}
        return set()

    def _format(self, text: str, qq: str = "", amount=None) -> str:
        url = str(self._cfg("afdian_url", "") or "")
        out = str(text).replace("{url}", url).replace("{qq}", qq)
        if amount is not None:
            out = out.replace("{amount}", str(amount))
        return out

    # ---------------- 消息入口 ----------------

    @filter.event_message_type(filter.EventMessageType.ALL, priority=maxsize)
    async def on_message(self, event: AstrMessageEvent):
        if not bool(self._cfg("enable", True)):
            return
        kind = self._private_kind(event)
        if not kind:
            return  # 群聊或未知类型：不归本插件管
        if kind == "temp" and not bool(self._cfg("enable_temp_session", True)):
            return  # 临时会话不纳入准入
        if self._is_request_or_notice(event):
            return  # 好友申请/通知事件：放行，交给人际关系等插件处理

        umo = event.unified_msg_origin
        sender = self._sender_id(event)
        if not sender:
            return
        if sender in self._admins() or sender in self._whitelist():
            return  # 管理员与白名单：完全放行

        # 普通“帮助”不在全局入口拦截，避免抢占其他插件的帮助命令。
        # 已受阻会话仍由 _on_blocked_message 提供准入帮助。

        # 黑名单：无条件静默拦截
        if sender in self._blacklist():
            event.stop_event()
            return

        # 放行凭证（含到期）：到期自动出列并提示续期
        expire = self._passes.get(sender)
        if expire is not None:
            if expire == 0 or expire > time.time():
                return  # 有效凭证：放行
            del self._passes[sender]
            self._persist()
            logger.info(f"[sponsor_pass] {umo} 的放行凭证已到期，重新进入准入")
            event.set_result(
                MessageEventResult()
                .message(self._format(self._cfg("expired_text", DEFAULT_EXPIRED_TEXT), sender))
                .stop_event()
            )
            return

        if self._is_historical(event):
            event.stop_event()
            return

        # 已达轮数上限的会话：静默拦截，但响应赞助查询
        if umo in self._blocked:
            await self._on_blocked_message(event, umo, sender)
            return

        # 轮次计数：使用固定窗口起点，窗口内的消息不会刷新起点。
        # 这样即使对方每隔 round_window-1 秒发一条，也会在窗口结束后进入下一轮，不能无限续杯。
        window = safe_int(self._cfg("round_window", 180), 180, minimum=1, maximum=86400)
        max_rounds = safe_int(self._cfg("max_rounds", 6), 6, minimum=0, maximum=1000)
        msg_ts = self._get_timestamp(event)
        window_start = self._round_ts.get(umo)
        if window_start is None or (msg_ts - window_start) > window:
            count = self._rounds.get(umo, 0) + 1
            self._rounds[umo] = count
            # 只有开始新轮次时才更新窗口起点；窗口内消息不延长当前轮次。
            self._round_ts[umo] = msg_ts
            self._persist()
            logger.info(f"[sponsor_pass] {umo}（{kind}）第 {count}/{max_rounds} 轮放行")
        else:
            count = self._rounds.get(umo, 0)

        if count <= max_rounds:
            return  # 放行给 LLM

        # 超出轮数：发送两选项提示并进入拦截态
        self._blocked.add(umo)
        self._remind_day[umo] = date.today().isoformat()
        self._bump_stat("gate_total")
        self._persist()
        logger.info(f"[sponsor_pass] {umo} 已达 {max_rounds} 轮上限，发送准入提示")
        gate_text = self._cfg("gate_text", DEFAULT_GATE_TEXT)
        if not self._sponsor_enabled():
            gate_text = self._cfg("gate_text_no_sponsor", DEFAULT_GATE_TEXT_NO_SPONSOR)
        event.set_result(
            MessageEventResult()
            .message(self._format(gate_text, sender))
            .stop_event()
        )
        self._pending[umo] = {
            "qq": sender,
            "kind": kind,
            "created_at": int(time.time()),
            "last_seen_at": int(time.time()),
        }
        self._persist()
        await self._notify_on_gate(event, sender, max_rounds)

    async def _on_blocked_message(self, event: AstrMessageEvent, umo: str, sender: str):
        text = str(event.message_str or "")
        if text.strip().lower() in {"帮助", "help", "/帮助", "?", "？"}:
            event.set_result(
                MessageEventResult()
                .message(self._cfg("public_help_text", DEFAULT_PUBLIC_HELP_TEXT))
                .stop_event()
            )
            return
        external_provider, external_code = self._parse_payment_code(text)
        if external_provider or text.strip().startswith(("微信订单", "支付宝订单")):
            if not external_code:
                event.set_result(MessageEventResult().message("支付订单号格式不对，请使用“微信订单号: 订单号”或“支付宝订单号: 订单号”～").stop_event())
            else:
                await self._redeem_external_order(event, umo, sender, external_provider, external_code)
            return
        order_code = parse_order_code(text)
        if not order_code and text.strip().isdigit() and _looks_like_afdian_order_code(text.strip()):
            order_code = text.strip()
        has_order_prefix = any(text.strip().startswith(prefix) for prefix in ORDER_CODE_PREFIXES)
        if order_code or has_order_prefix:
            if not bool(self._cfg("enable_order_code", True)):
                if has_order_prefix:
                    event.set_result(MessageEventResult().message("订单码兑换功能目前未开启，请联系管理员～").stop_event())
                    return
            elif not self._sponsor_enabled():
                event.set_result(MessageEventResult().message("订单兑换通道还没配置好，请联系管理员～").stop_event())
            elif not order_code:
                event.set_result(MessageEventResult().message(self._cfg("order_code_invalid_text", DEFAULT_ORDER_CODE_INVALID_TEXT)).stop_event())
            else:
                await self._redeem_order_code(event, umo, sender, order_code)
            return
        lowered = text.lower()
        if self._sponsor_enabled() and (
            any(k in text for k in SPONSOR_KEYWORDS) or any(k in lowered for k in SPONSOR_KEYWORDS)
        ):
            await self._handle_sponsor_claim(event, umo, sender)
            return
        # 每天提醒一次，其余静默
        today = date.today().isoformat()
        if self._remind_day.get(umo) != today:
            self._remind_day[umo] = today
            self._persist()
            remind_text = self._cfg("remind_text", DEFAULT_REMIND_TEXT)
            if not self._sponsor_enabled():
                remind_text = self._cfg("gate_text_no_sponsor", DEFAULT_GATE_TEXT_NO_SPONSOR)
            event.set_result(
                MessageEventResult()
                .message(self._format(remind_text, sender))
                .stop_event()
            )
        else:
            event.stop_event()

    def _sponsor_enabled(self) -> bool:
        if not bool(self._cfg("enable_sponsor", True)):
            return False
        has_url = bool(str(self._cfg("afdian_url", "") or "").strip())
        user_id = str(self._cfg("afdian_user_id", "") or "").strip()
        token = str(self._cfg("afdian_token", "") or "").strip()
        # 未配置完整时视为未启用，避免给出无法兑现的入口
        return bool(has_url and user_id and token)

    # ---------------- 爱发电赞助校验 ----------------

    async def _handle_sponsor_claim(self, event: AstrMessageEvent, umo: str, sender: str):
        cooldown = safe_int(self._cfg("check_cooldown", 300), 300, minimum=0, maximum=86400)
        now = time.time()
        if now - self._check_ts.get(umo, 0) < cooldown:
            event.set_result(MessageEventResult().message("刚帮你查过啦，稍等几分钟再试试～").stop_event())
            return
        self._check_ts[umo] = now
        user_id = str(self._cfg("afdian_user_id", "") or "").strip()
        token = str(self._cfg("afdian_token", "") or "").strip()
        if not user_id or not token:
            event.set_result(MessageEventResult().message("赞助通道还没配置好，请联系管理员～").stop_event())
            return
        result = await self._search_orders(sender, user_id, token)
        if result.get("service_error"):
            event.set_result(MessageEventResult().message(DEFAULT_ORDER_CODE_ERROR_TEXT).stop_event())
            return
        order = result.get("match")
        if order is None:
            unclaimed = result.get("unclaimed")
            if unclaimed is not None:
                amount = unclaimed.get("total_amount", "?")
                trade_no = str(unclaimed.get("out_trade_no", "?"))
                event.set_result(MessageEventResult().message(
                    self._format(self._cfg("unclaimed_text", DEFAULT_UNCLAIMED_TEXT), sender, amount).replace("{trade_no}", trade_no)
                ).stop_event())
                await self._notify_owner(
                    f"检测到未备注QQ的爱发电订单：¥{amount}，订单号 {trade_no}。\n"
                    f"申请人QQ：{sender}\n确认后执行：/准入 订单 {trade_no} {sender}", event=event
                )
                return
            if result.get("other"):
                event.set_result(MessageEventResult().message(
                    self._cfg("claimed_other_text", DEFAULT_CLAIMED_OTHER_TEXT)
                ).stop_event())
                return
            event.set_result(MessageEventResult().message(
                self._format(self._cfg("not_found_text", DEFAULT_NOT_FOUND_TEXT), sender, self._cfg("min_amount", 5))
            ).stop_event())
            return
        validity = await self._grant_pass_locked(sender, order, source="sponsor")
        if validity is None:
            event.set_result(MessageEventResult().message(
                self._cfg("order_code_used_text", DEFAULT_ORDER_CODE_USED_TEXT)
            ).stop_event())
            return
        self._clear_session_state(umo)
        trade_no = str(order.get("out_trade_no", "?"))
        amount = order.get("total_amount", "?")
        event.set_result(MessageEventResult().message(
            f"查到啦！感谢老板大气～你已经通过考验，以后随便找我玩！(๑•̀ㅂ•́)و✧（{validity}）"
        ).stop_event())
        await self._notify_owner(
            f"QQ {sender} 通过爱发电赞助自动通过准入，金额 ¥{amount}，订单号 {trade_no}（{validity}）", event=event
        )

    async def _redeem_order_code(self, event: AstrMessageEvent, umo: str, sender: str, order_code: str):
        """用当前发送者QQ兑换爱发电订单号；订单核销一次后永久绑定。"""
        event.set_result(
            MessageEventResult()
            .message(self._cfg("order_code_checking_text", DEFAULT_ORDER_CODE_CHECKING_TEXT))
            .stop_event()
        )
        user_id = str(self._cfg("afdian_user_id", "") or "").strip()
        token = str(self._cfg("afdian_token", "") or "").strip()
        if not user_id or not token:
            event.set_result(MessageEventResult().message("订单兑换通道还没配置好，请联系管理员～").stop_event())
            return
        order = await self._find_order_by_no(order_code, user_id, token)
        if order is None:
            event.set_result(MessageEventResult().message(self._cfg("order_code_not_found_text", DEFAULT_ORDER_CODE_NOT_FOUND_TEXT)).stop_event())
            return
        returned_no = str(order.get("out_trade_no", "")).strip()
        if returned_no != order_code:
            event.set_result(MessageEventResult().message(self._cfg("order_code_not_found_text", DEFAULT_ORDER_CODE_NOT_FOUND_TEXT)).stop_event())
            return
        try:
            status = int(order["status"])
        except (KeyError, TypeError, ValueError):
            event.set_result(MessageEventResult().message("这个订单的支付状态异常，暂时不能兑换，请联系管理员～").stop_event())
            return
        if status != 2:
            event.set_result(MessageEventResult().message("这个订单还没有支付完成，暂时不能兑换哦～").stop_event())
            return
        amount = safe_amount(order.get("total_amount"), Decimal("-1"))
        minimum = safe_amount(self._cfg("min_amount", 5), Decimal("5"))
        if amount < 0 or amount < minimum:
            event.set_result(MessageEventResult().message(f"这个订单金额不足兑换门槛（当前 ¥{order.get('total_amount', '?')}，需要至少 ¥{minimum}）～").stop_event())
            return
        validity = await self._grant_pass_locked(sender, order, source="order_code")
        if validity is None:
            event.set_result(MessageEventResult().message(self._cfg("order_code_used_text", DEFAULT_ORDER_CODE_USED_TEXT)).stop_event())
            return
        self._clear_session_state(umo)
        event.set_result(MessageEventResult().message(f"{self._cfg('order_code_success_text', DEFAULT_ORDER_CODE_SUCCESS_TEXT)}（{validity}）").stop_event())
        await self._notify_owner(f"🔔 QQ {sender} 使用订单码兑换成功，订单号 {order_code}，金额 ¥{order.get('total_amount', '?')}（{validity}）", event=event)

    async def _redeem_external_order(self, event: AstrMessageEvent, umo: str, sender: str, provider_name: str, order_code: str):
        provider = self._payment_provider(provider_name)
        if provider is None or not provider.configured:
            event.set_result(MessageEventResult().message("这个支付渠道还没有配置完成，请联系管理员～").stop_event())
            return
        try:
            order = await provider.find_order_by_code(order_code)
            if not order.paid:
                event.set_result(MessageEventResult().message("订单还没有支付成功，支付完成后再发送订单号即可～").stop_event())
                return
            if order.amount < safe_amount(self._cfg("min_amount", 5), Decimal("5")):
                event.set_result(MessageEventResult().message("订单金额还没有达到准入门槛～").stop_event())
                return
            normalized = {
                "out_trade_no": order.order_id,
                "total_amount": str(order.amount),
                "remark": order.remark,
                "plan_id": order.plan_id,
                "product_id": order.product_id,
                "sku_detail": [],
            }
            key = self._payment_key(provider_name, order.order_id)
            if key in self._claimed_orders or order.order_id in self._claimed_orders:
                event.set_result(MessageEventResult().message("这个订单已经兑换过了，不能重复使用哦～").stop_event())
                return
            # 统一把外部渠道订单放入带渠道命名空间的核销台账。
            validity = await self._grant_external_pass(sender, normalized, key, provider_name)
            if validity is None:
                event.set_result(MessageEventResult().message("这个订单已经兑换过了，不能重复使用哦～").stop_event())
                return
            self._clear_session_state(umo)
            event.set_result(MessageEventResult().message(f"订单核验成功！你已经通过啦～（{validity}）").stop_event())
            await self._notify_owner(f"🔔 QQ {sender} 使用{provider_name}订单兑换成功：{order.order_id}，金额 ¥{order.amount}", event=event)
        except PaymentUnpaid:
            event.set_result(MessageEventResult().message("订单还没有支付成功，支付完成后再试～").stop_event())
        except PaymentNotFound:
            event.set_result(MessageEventResult().message("没有查到这个支付渠道的订单，请确认订单号完整无误～").stop_event())
        except PaymentServiceError:
            event.set_result(MessageEventResult().message("支付平台查询暂时不可用，请稍后再试，不要重复付款～").stop_event())
        except PaymentError as exc:
            logger.warning(f"[sponsor_pass] {provider_name} 订单核验失败: {exc}")
            event.set_result(MessageEventResult().message("这个订单暂时无法核验，请联系管理员处理～").stop_event())

    async def _grant_external_pass(self, qq: str, order: dict, key: str, provider_name: str):
        async with self._claim_lock:
            if key in self._claimed_orders:
                return None
            # 复用现有权益计算，但临时使用渠道命名空间核销。
            original = order["out_trade_no"]
            order["out_trade_no"] = key
            try:
                validity = self._grant_pass(qq, order, source=provider_name)
            finally:
                order["out_trade_no"] = original
            return validity
        now = time.time()
        cooldown = safe_int(self._cfg("check_cooldown", 300), 300, minimum=0, maximum=86400)
        if now - self._check_ts.get(umo, 0) < cooldown:
            event.set_result(
                MessageEventResult()
                .message("刚帮你查过啦，订单到账可能要几分钟，先别急，稍等几分钟再发「我赞助了」试试～")
                .stop_event()
            )
            return
        self._check_ts[umo] = now

        user_id = str(self._cfg("afdian_user_id", "") or "").strip()
        token = str(self._cfg("afdian_token", "") or "").strip()
        if not user_id or not token:
            event.set_result(
                MessageEventResult()
                .message("赞助通道还没配置好，先麻烦你等等我哥哥同意啦～")
                .stop_event()
            )
            return

        result = await self._search_orders(sender, user_id, token)
        if result.get("service_error"):
            event.set_result(
                MessageEventResult()
                .message("爱发电查询暂时失败，请稍后再试，不要重复付款～")
                .stop_event()
            )
            return
        matched = result.get("match")
        if matched is None:
            unclaimed = result["unclaimed"]
            if unclaimed is not None:
                # 防呆①：查到订单但留言没写QQ —— 给出订单号并通知管理员绑定
                amount = unclaimed.get("total_amount", "?")
                trade_no = unclaimed.get("out_trade_no", "?")
                event.set_result(
                    MessageEventResult()
                    .message(
                        self._format(self._cfg("unclaimed_text", DEFAULT_UNCLAIMED_TEXT), sender, amount)
                        .replace("{trade_no}", str(trade_no))
                    )
                    .stop_event()
                )
                await self._notify_owner(
                    f"🔔 检测到未备注QQ的赞助订单：¥{amount}，订单号 {trade_no}。\n"
                    f"对方 QQ {sender} 正在认领。确认后执行：/准入 订单 {trade_no} {sender}", event=event
                )
                return
            if result["other"]:
                # 防呆②：留言写了号码但不是对方的QQ
                event.set_result(
                    MessageEventResult()
                    .message(self._cfg("claimed_other_text", DEFAULT_CLAIMED_OTHER_TEXT))
                    .stop_event()
                )
                return
            amount = safe_int(self._cfg("min_amount", 5), 5)
            event.set_result(
                MessageEventResult()
                .message(self._format(self._cfg("not_found_text", DEFAULT_NOT_FOUND_TEXT), sender, amount))
                .stop_event()
            )
            return

        order = matched
        trade_no = str(order.get("out_trade_no", "?"))
        validity = await self._grant_pass_locked(sender, order, source="sponsor")
        if validity is None:
            event.set_result(
                MessageEventResult()
                .message("查到你的订单啦，但发放凭证时出了点问题（订单可能已被使用），麻烦联系我哥哥处理～")
                .stop_event()
            )
            return
        self._clear_session_state(umo)
        logger.info(f"[sponsor_pass] {umo} 赞助校验通过，已放行（{validity}）")

        amount = order.get("total_amount", "?")
        event.set_result(
            MessageEventResult()
            .message(f"查到啦！感谢老板大气～你已经通过考验，以后随便找我玩！(๑•̀ㅂ•́)و✧（{validity}）")
            .stop_event()
        )
        await self._notify_owner(
            f"🔔 有人通过爱发电赞助自动通过准入：QQ {sender}，金额 ¥{amount}，"
            f"订单号 {trade_no}（{validity}）", event=event
        )

    def _order_units(self, order: dict) -> int:
        """解析商品份数；异常或过大数量返回 0，交由人工处理。"""
        maximum = safe_int(self._cfg("max_product_units", 10), 10, minimum=1, maximum=100)
        total = 1
        try:
            sku = order.get("sku_detail")
            if isinstance(sku, str):
                sku = json.loads(sku)
            if sku is None:
                return 1
            if not isinstance(sku, list):
                return 0
            counts = []
            for item in sku:
                if not isinstance(item, dict):
                    return 0
                raw_count = item.get("count", item.get("num", 1))
                count = int(raw_count)
                if count < 1:
                    return 0
                counts.append(count)
            total = sum(counts) if counts else 1
            if total > maximum:
                return 0
        except (json.JSONDecodeError, TypeError, ValueError, OverflowError):
            return 0
        return total

    async def _grant_pass_locked(self, qq: str, order: dict, source: str = "sponsor"):
        async with self._claim_lock:
            return self._grant_pass(qq, order, source)

    def _grant_pass(self, qq: str, order: dict, source: str = "sponsor"):
        """发放放行凭证；订单必须已在调用方完成严格核验。"""
        trade_no = str(order.get("out_trade_no", "")).strip()
        if not trade_no:
            return None
        if trade_no in self._claimed_orders:
            return None
        if not self._order_allowed(order):
            return None
        expire_days = self._membership_days(safe_amount(order.get("total_amount"), Decimal("0")))
        units = self._order_units(order)
        if units < 1:
            return None
        if not bool(self._cfg("stack_product_units", True)):
            units = 1
        if expire_days > 0:
            total_days = expire_days * units
            self._passes[qq] = time.time() + total_days * 86400
            validity = f"有效期 {total_days} 天" + (
                f"（{expire_days} 天 × {units} 份）" if units > 1 else ""
            ) + "，到期后再次赞助可以续期哦"
        else:
            self._passes[qq] = 0
            validity = "永久有效"
        if trade_no:
            self._claimed_orders[trade_no] = qq
        self._audit.append({
            "order_no": trade_no,
            "qq": qq,
            "amount": str(order.get("total_amount", "")),
            "plan_id": str(order.get("plan_id", "")),
            "product_id": str(order.get("product_id", order.get("sku_id", ""))),
            "units": units,
            "source": source,
            "granted_days": expire_days * units if expire_days > 0 else 0,
            "created_at": int(time.time()),
        })
        self._stats["revenue_total"] = str(
            safe_amount(self._stats.get("revenue_total", "0"), Decimal("0"))
            + safe_amount(order.get("total_amount"), Decimal("0"))
        )
        if source == "sponsor":
            self._bump_stat("sponsor_pass_total")
        else:
            self._bump_stat("manual_pass_total")
        self._persist()
        return validity

    async def _fetch_order_page(self, host: str, user_id: str, token: str,
                                page: int, per_page: int) -> dict:
        host = _safe_api_url(host)
        params = json.dumps({"page": page, "per_page": per_page}, separators=(",", ":"))
        ts = int(time.time())
        body = {
            "user_id": user_id,
            "params": params,
            "ts": ts,
            "sign": afdian_sign(token, user_id, params, ts),
        }
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession() as session:
            async with session.post(host, json=body, timeout=timeout) as resp:
                return await resp.json(content_type=None)

    async def _find_order(self, qq: str, user_id: str, token: str):
        """兼容入口：只返回与QQ匹配的订单（无匹配返回 None）。"""
        result = await self._search_orders(qq, user_id, token)
        return result["match"]

    async def _search_orders(self, qq: str, user_id: str, token: str) -> dict:
        """扫描最近订单，返回 {match, unclaimed, other}：
        match=留言含该QQ的有效订单；unclaimed=有效但留言没写任何号码（防呆线索）；
        other=存在留言写了其他号码的订单。
        """
        pages = safe_int(self._cfg("check_pages", 3), 3, minimum=1, maximum=10)
        per_page = 50
        min_amount = safe_amount(self._cfg("min_amount", 5), Decimal("5"))
        found = {"match": None, "unclaimed": None, "other": False, "service_error": False}
        last_error = None
        for host in AFDIAN_API_HOSTS:
            try:
                _safe_api_url(host)
                for page in range(1, pages + 1):
                    data = await self._fetch_order_page(host, user_id, token, page, per_page)
                    if not isinstance(data, dict):
                        raise RuntimeError("爱发电返回格式异常")
                    try:
                        ec = int(data.get("ec", -1))
                    except (TypeError, ValueError):
                        raise RuntimeError("爱发电返回 ec 异常")
                    if ec != 200:
                        raise RuntimeError(f"爱发电接口返回异常 ec={ec}")
                    payload = data.get("data")
                    if not isinstance(payload, dict):
                        raise RuntimeError("爱发电返回 data 异常")
                    orders = payload.get("list") or []
                    if not isinstance(orders, list):
                        raise RuntimeError("爱发电返回 list 异常")
                    for order in orders:
                        if not isinstance(order, dict):
                            continue
                        try:
                            if int(order.get("status", "-1")) != 2:
                                continue  # 未支付/关闭的订单不参与
                            order_amount = safe_amount(order.get("total_amount"), Decimal("-1"))
                            if order_amount < min_amount:
                                continue  # 金额不足
                            if order_amount == Decimal("-1"):
                                continue  # 金额字段异常
                        except (TypeError, ValueError):
                            continue
                        if not self._order_allowed(order):
                            continue
                        trade_no = str(order.get("out_trade_no", ""))
                        if trade_no and trade_no in self._claimed_orders:
                            continue  # 已核销过的订单不重复认领
                        remark = str(order.get("remark") or "")
                        if remark_has_qq(remark, qq):
                            found["match"] = order
                            return found
                        if any(c.isdigit() for c in remark):
                            found["other"] = True  # 留言写了号码但不是对方
                        elif found["unclaimed"] is None:
                            found["unclaimed"] = order
                    try:
                        total_count = int(payload.get("total_count", 0) or 0)
                    except (TypeError, ValueError):
                        raise RuntimeError("爱发电返回 total_count 异常")
                    if page * per_page >= total_count:
                        return found
                return found
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, ValueError) as e:
                last_error = e
                logger.warning(f"[sponsor_pass] 查询爱发电订单失败（{host}）: {e}")
                continue
        if last_error:
            logger.warning(f"[sponsor_pass] 爱发电查询最终失败: {last_error}")
            found["service_error"] = True
        return found

    async def _find_order_by_no(self, trade_no: str, user_id: str, token: str):
        """按订单号精确查询；异常响应和服务不可用统一安全失败。"""
        trade_no = str(trade_no or "").strip()
        if not trade_no:
            return None
        params = json.dumps({"out_trade_no": trade_no}, separators=(",", ":"))
        ts = int(time.time())
        body = {
            "user_id": user_id,
            "params": params,
            "ts": ts,
            "sign": afdian_sign(token, user_id, params, ts),
        }
        timeout = aiohttp.ClientTimeout(total=15)
        last_error = None
        for host in AFDIAN_API_HOSTS:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(host, json=body, timeout=timeout) as resp:
                        data = await resp.json(content_type=None)
                try:
                    ec = int(data["ec"])
                except (KeyError, TypeError, ValueError):
                    raise RuntimeError("爱发电按订单查询返回 ec 异常")
                if ec != 200:
                    raise RuntimeError(f"爱发电接口返回异常 ec={ec}")
                payload = data.get("data")
                if not isinstance(payload, dict):
                    raise RuntimeError("爱发电按订单查询返回 data 异常")
                orders = payload.get("list")
                if not isinstance(orders, list):
                    raise RuntimeError("爱发电按订单查询返回 list 异常")
                for order in orders:
                    if not isinstance(order, dict):
                        continue
                    if str(order.get("out_trade_no", "")).strip() == trade_no:
                        return order
                return None
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, ValueError) as e:
                last_error = e
                logger.warning(f"[sponsor_pass] 按订单号查询失败（{host}）: {e}")
                continue
        if last_error:
            logger.warning(f"[sponsor_pass] 按订单号查询最终失败: {last_error}")
        return None

    async def _notify_on_gate(self, event: AstrMessageEvent, sender: str, max_rounds: int):
        """陌生人触发门槛时通知管理员（每日每人最多一次，避免打扰）。"""
        if not bool(self._cfg("notify_on_gate", True)):
            return
        umo = event.unified_msg_origin
        today = date.today().isoformat()
        if self._remind_day.get("gate_notice:" + umo) == today:
            return
        self._remind_day["gate_notice:" + umo] = today
        self._persist()
        try:
            name = event.get_sender_name()
        except Exception:
            name = ""
        sent = await self._notify_owner(
            f"🔔 陌生人触发准入：{name}（QQ {sender}）已聊满 {max_rounds} 轮。\n"
            f"放行：/准入 同意 {sender}\n拉黑：/准入 拉黑 {sender}", event=event
        )
        if not sent:
            self._remind_day.pop("gate_notice:" + umo, None)
            self._persist()

    @staticmethod
    def _send_result_ok(result) -> bool:
        """仅把 OneBot 明确成功的返回值视为发送成功。"""
        if result is None:
            return True
        if not isinstance(result, dict):
            return True
        status = result.get("status")
        retcode = result.get("retcode")
        if status is not None and str(status).lower() not in ("ok", "success"):
            return False
        if retcode is not None:
            try:
                if int(retcode) != 0:
                    return False
            except (TypeError, ValueError):
                return False
        return True

    async def _send_private_via_event(self, event: AstrMessageEvent, qq: str, text: str) -> bool:
        """优先复用当前 aiocqhttp bot 直发，避免统一会话路由不支持主动私聊。"""
        try:
            bot = getattr(event, "bot", None)
            sender = getattr(bot, "send_private_msg", None)
            if sender is not None:
                result = sender(user_id=int(qq), message=text)
                if hasattr(result, "__await__"):
                    result = await result
                return self._send_result_ok(result)
            api = getattr(bot, "api", None)
            call_action = getattr(api, "call_action", None)
            if call_action is not None:
                result = call_action("send_private_msg", user_id=int(qq), message=text)
                if hasattr(result, "__await__"):
                    result = await result
                return self._send_result_ok(result)
        except Exception as e:
            logger.warning(f"[sponsor_pass] 当前 bot 私聊发送失败({qq}): {e}")
        return False

    async def _send_to(self, qq_or_umo: str, text: str, event: AstrMessageEvent = None) -> bool:
        """主动私聊发送：当前事件 bot 直发优先，context 路由兜底。"""
        target = str(qq_or_umo or "").strip()
        if not target:
            return False
        qq = target.rsplit(":", 1)[-1] if ":" in target else target
        if qq.isdigit() and event is not None:
            if await self._send_private_via_event(event, qq, text):
                return True
        try:
            umo = target if ":" in target else f"aiocqhttp:FriendMessage:{target}"
            await self.context.send_message(umo, MessageChain().message(text))
            return True
        except Exception as e:
            logger.warning(f"[sponsor_pass] 主动消息发送失败({target}): {e}")
            return False

    async def _notify_owner(self, text: str, event: AstrMessageEvent = None):
        if not bool(self._cfg("notify_owner", True)):
            return
        target = str(self._cfg("notify_target", "") or "").strip()
        if not target:
            admins = self._admins()
            target = sorted(admins)[0] if admins else ""
        if not target:
            return False
        if ":" not in target:
            target = f"aiocqhttp:FriendMessage:{target}"
        qq = target.rsplit(":", 1)[-1]
        if event is not None and qq.isdigit():
            if await self._send_private_via_event(event, qq, text):
                return True
        try:
            await self.context.send_message(target, MessageChain().message(text))
            return True
        except Exception as e:
            logger.warning(f"[sponsor_pass] 通知管理员失败({target}): {e}")
            return False

    # ---------------- 管理命令 ----------------

    @filter.command("准入帮助")
    async def public_admission_help(self, event: AstrMessageEvent):
        """独立准入帮助命令，不占用通用“帮助”命令名。"""
        yield event.plain_result(self._cfg("public_help_text", DEFAULT_PUBLIC_HELP_TEXT))

    @filter.command("准入")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def admission(self, event: AstrMessageEvent, action: str = "", qq: str = "", days: str = ""):
        action = (action or "").strip()
        qq = (qq or "").strip()
        days = (days or "").strip()
        whitelist = self._whitelist()
        blacklist = self._blacklist()

        if action in ("帮助", "help", ""):
            yield event.plain_result(
                "用法：/准入 同意 <QQ号> [天数] | /准入 移除 <QQ号> | /准入 拉黑 <QQ号> | "
                "/准入 解黑 <QQ号> | /准入 订单 <订单号> <QQ号> | /准入 待处理 | "
                "/准入 通知重试 <QQ号> | /准入 自检 | /准入 状态 <QQ号> | /准入 重置 <QQ号> | /准入 统计 | /准入 列表"
            )
            return

        if action in ("待处理", "pending"):
            now = time.time()
            retention = safe_int(self._cfg("pending_retention_days", 30), 30, minimum=1, maximum=365) * 86400
            stale = [key for key, value in self._pending.items()
                     if not isinstance(value, dict) or now - float(value.get("last_seen_at", 0) or 0) > retention]
            for key in stale:
                self._pending.pop(key, None)
            if stale:
                self._persist()
            lines = []
            for key, value in self._pending.items():
                if not isinstance(value, dict):
                    continue
                pqq = str(value.get("qq", "?"))
                cnt = self._rounds.get(key, 0)
                lines.append(f"QQ {pqq}：已聊 {cnt} 轮，申请时间 {value.get('created_at', '?')}")
            yield event.plain_result("\n".join(lines) if lines else "当前没有待处理申请")
            return

        if action in ("自检", "check"):
            state_ok = False
            try:
                self._persist()
                state_ok = True
            except Exception:
                pass
            yield event.plain_result(
                f"插件 0.5.0\n准入：{'开启' if self._cfg('enable', True) else '关闭'}\n"
                f"临时会话：{'开启' if self._cfg('enable_temp_session', True) else '关闭'}\n"
                f"管理员：{'已配置' if self._admins() else '未读取到'}\n"
                f"爱发电凭据：{'已配置' if self._sponsor_enabled() else '未完整配置'}\n"
                f"订单号兑换：{'开启' if self._cfg('enable_order_code', True) else '关闭'}\n"
                f"状态文件：{'可读写' if state_ok else '写入失败'}\n"
                f"待处理：{len(self._pending)} 人，已核销订单：{len(self._claimed_orders)} 笔"
            )
            return

        if action in ("通知重试", "retry"):
            if not qq.isdigit():
                yield event.plain_result("用法：/准入 通知重试 <QQ号>")
                return
            sent = await self._send_to(qq, self._cfg("approved_text", DEFAULT_APPROVED_TEXT), event=event)
            yield event.plain_result("已重试通知申请人" if sent else "通知仍然失败，请让对方主动发消息")
            return

        if action in ("列表", "list"):
            names = "、".join(sorted(whitelist)) if whitelist else "（空）"
            now = time.time()
            pass_lines = []
            for p_qq, exp in sorted(self._passes.items()):
                if exp == 0:
                    pass_lines.append(f"{p_qq}（永久）")
                elif exp > now:
                    remain = int((exp - now) // 86400) + 1
                    pass_lines.append(f"{p_qq}（剩约 {remain} 天）")
            blocks = "、".join(sorted(blacklist)) if blacklist else "（空）"
            yield event.plain_result(
                f"白名单：{names}\n限时放行：{'、'.join(pass_lines) if pass_lines else '（无）'}\n黑名单：{blocks}"
            )
            return

        if action in ("统计", "stats"):
            now = time.time()
            active_passes = sum(1 for exp in self._passes.values() if exp == 0 or exp > now)
            yield event.plain_result(
                f"累计触发准入 {self._stats.get('gate_total', 0)} 次；"
                f"赞助直通 {self._stats.get('sponsor_pass_total', 0)} 人次；"
                f"手动放行 {self._stats.get('manual_pass_total', 0)} 人次；"
                f"已核销订单 {len(self._claimed_orders)} 笔；"
                f"累计实付 ¥{self._stats.get('revenue_total', '0')}；"
                f"有效凭证 {active_passes} 人；待处理 {len(self._pending)} 人。"
            )
            return

        if action in ("状态", "status"):
            found = [
                (umo, cnt)
                for umo, cnt in self._rounds.items()
                if not qq or umo.endswith(qq)
            ]
            if not found:
                yield event.plain_result(f"没有找到 {qq or '任何人'} 的计数记录（可能还没开始聊）")
                return
            max_rounds = self._cfg("max_rounds", 6)
            lines = []
            for umo, cnt in found:
                state = "已拦截" if umo in self._blocked else "对话中"
                lines.append(f"{umo}：第 {cnt}/{max_rounds} 轮，{state}")
            yield event.plain_result("\n".join(lines))
            return

        if action in ("同意", "通过", "allow", "add"):
            if not qq.isdigit():
                yield event.plain_result("用法：/准入 同意 <QQ号> [天数]（天数留空=永久）")
                return
            if qq in self._blacklist():
                yield event.plain_result(f"{qq} 在黑名单里，请先执行 /准入 解黑 {qq}")
                return
            n_days = safe_int(days, 0)
            if n_days > 0:
                self._passes[qq] = time.time() + n_days * 86400
                self._bump_stat("manual_pass_total")
                self._clear_state_by_qq(qq)
                validity_text = f"有效期 {n_days} 天"
            else:
                whitelist.add(qq)
                if not self._save_config_list("whitelist", whitelist):
                    yield event.plain_result("保存白名单失败，请查看控制台日志")
                    return
                self._bump_stat("manual_pass_total")
                self._clear_state_by_qq(qq)
                validity_text = "永久有效"
            self._pending = {
                key: value for key, value in self._pending.items()
                if str(value.get("qq", "")) != qq
            }
            self._persist()
            # 通知申请人（对方可能还不是好友，发送失败不影响放行）
            notice = self._format(self._cfg("approved_text", DEFAULT_APPROVED_TEXT), qq)
            if n_days > 0:
                notice += f"（有效期 {n_days} 天）"
            sent = await self._send_to(qq, notice, event=event)
            extra = "，已私聊通知对方" if sent else "；私聊通知发送失败，请让对方主动发消息确认"
            yield event.plain_result(f"好啦，已放行 {qq}（{validity_text}）{extra}")
            return

        if action in ("移除", "删除", "remove", "del"):
            if not qq.isdigit():
                yield event.plain_result("用法：/准入 移除 <QQ号>")
                return
            whitelist.discard(qq)
            self._passes.pop(qq, None)
            if self._save_config_list("whitelist", whitelist):
                self._persist()
                yield event.plain_result(f"已将 {qq} 移出白名单")
            else:
                yield event.plain_result("保存白名单失败，请查看控制台日志")
            return

        if action in ("订单", "order"):
            # 手动核销：把订单绑定给某个QQ（用于对方忘写QQ/写错QQ的兜底）
            trade_no, target_qq = qq, days
            if not trade_no or not target_qq.isdigit():
                yield event.plain_result("用法：/准入 订单 <订单号> <QQ号>")
                return
            user_id = str(self._cfg("afdian_user_id", "") or "").strip()
            token = str(self._cfg("afdian_token", "") or "").strip()
            if not user_id or not token:
                yield event.plain_result("尚未配置爱发电接口（afdian_user_id/afdian_token），无法核验订单")
                return
            order = await self._find_order_by_no(trade_no, user_id, token)
            if order is None:
                yield event.plain_result(f"没有查到订单 {trade_no}，确认订单号是否正确")
                return
            returned_no = str(order.get("out_trade_no", "")).strip()
            if not returned_no or returned_no != trade_no:
                yield event.plain_result("订单号校验失败，接口返回的数据不一致，未执行核销")
                return
            try:
                status = int(order["status"])
            except (KeyError, TypeError, ValueError):
                yield event.plain_result(f"订单 {trade_no} 的支付状态异常，未执行核销")
                return
            if status != 2:
                yield event.plain_result(f"订单 {trade_no} 还未支付完成（status={status}），不能核销")
                return
            amount = safe_amount(order.get("total_amount"), Decimal("-1"))
            if amount < 0:
                yield event.plain_result(f"订单 {trade_no} 的金额字段异常，未执行核销")
                return
            minimum = safe_amount(self._cfg("min_amount", 5), Decimal("5"))
            if amount < minimum:
                yield event.plain_result(
                    f"该订单金额 ¥{amount} 低于门槛 ¥{minimum}；"
                    f"如确实要放行请用 /准入 同意 {target_qq}"
                )
                return
            amount_text = str(order.get("total_amount"))
            validity = await self._grant_pass_locked(target_qq, order, source="manual")
            if validity is None:
                yield event.plain_result(
                    f"订单 {trade_no} 已被核销过（绑定给 {self._claimed_orders.get(trade_no, '?')}），不能重复使用"
                )
                return
            self._clear_state_by_qq(target_qq)
            notice = self._format(self._cfg("approved_text", DEFAULT_APPROVED_TEXT), target_qq)
            if validity != "永久有效":
                notice += f"（{validity}）"
            sent = await self._send_to(target_qq, notice, event=event)
            extra = "，已私聊通知对方" if sent else "；私聊通知发送失败，请让对方主动发消息确认"
            yield event.plain_result(f"已核销订单 {trade_no}（¥{amount}）并放行 {target_qq}（{validity}）{extra}")
            return

        if action in ("拉黑", "黑名单", "ban"):
            if not qq.isdigit():
                yield event.plain_result("用法：/准入 拉黑 <QQ号>")
                return
            blacklist.add(qq)
            whitelist.discard(qq)
            self._passes.pop(qq, None)
            if self._save_config_list("blacklist", blacklist) and self._save_config_list("whitelist", whitelist):
                self._persist()
                self._clear_state_by_qq(qq)
                yield event.plain_result(f"已拉黑 {qq}，之后的消息将不再理会")
            else:
                yield event.plain_result("保存黑名单失败，请查看控制台日志")
            return

        if action in ("解黑", "解除拉黑", "unban"):
            if not qq.isdigit():
                yield event.plain_result("用法：/准入 解黑 <QQ号>")
                return
            blacklist.discard(qq)
            if self._save_config_list("blacklist", blacklist):
                self._persist()
                yield event.plain_result(f"已将 {qq} 移出黑名单（重新计数）")
            else:
                yield event.plain_result("保存黑名单失败，请查看控制台日志")
            return

        if action in ("重置", "reset"):
            if not qq:
                yield event.plain_result("用法：/准入 重置 <QQ号>")
                return
            self._clear_state_by_qq(qq)
            yield event.plain_result(f"已重置 {qq} 的轮次计数，可以重新聊 {self._cfg('max_rounds', 6)} 轮啦")
            return

        yield event.plain_result(
            "用法：/准入 同意 <QQ号> [天数] | /准入 移除 <QQ号> | /准入 拉黑 <QQ号> | "
            "/准入 解黑 <QQ号> | /准入 订单 <订单号> <QQ号> | /准入 状态 <QQ号> | "
            "/准入 重置 <QQ号> | /准入 统计 | /准入 列表"
        )

    async def terminate(self):
        """插件卸载/停用时保存并清理内存状态。"""
        self._persist()
        self._rounds.clear()
        self._round_ts.clear()
        self._blocked.clear()
        self._remind_day.clear()
        self._check_ts.clear()
