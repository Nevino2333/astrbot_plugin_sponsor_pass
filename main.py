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
import json
import re
import time
from datetime import date
from sys import maxsize

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, MessageEventResult, filter
from astrbot.api.star import Context, Star, register

try:
    from astrbot.api.message_components import Plain  # noqa: F401  (预留)
except Exception:  # pragma: no cover
    Plain = None

AFDIAN_API_HOSTS = (
    "https://afdian.com/api/open/query-order",
    "https://afdian.net/api/open/query-order",
)

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

SPONSOR_KEYWORDS = ("赞助", "爱发电", "afdian")


def afdian_sign(token: str, user_id: str, params_json: str, ts: int) -> str:
    """爱发电开放接口签名（协议规定必须用 MD5，非安全场景）：
    sign = md5(token + 'params' + params + 'ts' + ts + 'user_id' + user_id)
    官方文档示例向量：token=123, params={"a":333}, ts=1624339905, user_id=abc
      -> a4acc28b81598b7e5d84ebdc3e91710c
    """
    raw = f"{token}params{params_json}ts{ts}user_id{user_id}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def safe_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
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
    "0.1.0",
    "https://github.com/Nevino2333/astrbot_plugin_sponsor_pass",
)
class SponsorPassPlugin(Star):

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.context = context
        self.config = config
        self._start_ts = int(time.time())
        # 状态均为内存态，重启后轮次重新计数（与 whitelistpro 行为一致）
        self._rounds: dict = {}      # umo -> 已放行轮数
        self._round_ts: dict = {}    # umo -> 上次发言时间戳
        self._blocked: set = set()   # 已达轮数上限的 umo
        self._remind_day: dict = {}  # umo -> 上次提示日期（每天只提示一次）
        self._check_ts: dict = {}    # umo -> 上次赞助校验时间（限频）

    # ---------------- 基础工具 ----------------

    def _cfg(self, key: str, default=None):
        try:
            value = self.config.get(key, default) if self.config is not None else default
        except Exception:
            value = default
        return default if value is None else value

    @staticmethod
    def _is_friend_private(event: AstrMessageEvent) -> bool:
        try:
            return "FriendMessage" in str(event.message_obj.type)
        except Exception:
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

    def _save_whitelist(self, whitelist: set) -> bool:
        try:
            self.config["whitelist"] = sorted(whitelist)
            self.config.save_config()
            return True
        except Exception as e:
            logger.error(f"[sponsor_pass] 保存白名单失败: {e}")
            return False

    def _format(self, text: str, qq: str = "") -> str:
        url = str(self._cfg("afdian_url", "") or "")
        return str(text).replace("{url}", url).replace("{qq}", qq)

    # ---------------- 消息入口 ----------------

    @filter.event_message_type(filter.EventMessageType.ALL, priority=maxsize)
    async def on_message(self, event: AstrMessageEvent):
        if not bool(self._cfg("enable", True)):
            return
        if not self._is_friend_private(event):
            return

        umo = event.unified_msg_origin
        sender = self._sender_id(event)
        if not sender or sender in self._whitelist():
            return  # 白名单内：完全放行

        if self._is_historical(event):
            event.stop_event()
            return

        # 已达轮数上限的会话：静默拦截，但响应赞助查询
        if umo in self._blocked:
            await self._on_blocked_message(event, umo, sender)
            return

        # 轮次计数：合并窗口内的连发消息算同一轮
        window = safe_int(self._cfg("round_window", 180), 180)
        max_rounds = safe_int(self._cfg("max_rounds", 6), 6)
        msg_ts = self._get_timestamp(event)
        last_ts = self._round_ts.get(umo)
        if last_ts is None or (msg_ts - last_ts) > window:
            count = self._rounds.get(umo, 0) + 1
            self._rounds[umo] = count
        else:
            count = self._rounds.get(umo, 0)
        self._round_ts[umo] = msg_ts

        if count <= max_rounds:
            logger.debug(f"[sponsor_pass] {umo} 第 {count}/{max_rounds} 轮自由对话")
            return  # 放行给 LLM

        # 超出轮数：发送两选项提示并进入拦截态
        self._blocked.add(umo)
        self._remind_day[umo] = date.today().isoformat()
        logger.info(f"[sponsor_pass] {umo} 已达 {max_rounds} 轮上限，发送准入提示")
        gate_text = self._cfg("gate_text", DEFAULT_GATE_TEXT)
        if not self._sponsor_enabled():
            gate_text = self._cfg("gate_text_no_sponsor", DEFAULT_GATE_TEXT_NO_SPONSOR)
        event.set_result(
            MessageEventResult()
            .message(self._format(gate_text, sender))
            .stop_event()
        )

    async def _on_blocked_message(self, event: AstrMessageEvent, umo: str, sender: str):
        text = str(event.message_str or "")
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
        now = time.time()
        cooldown = safe_int(self._cfg("check_cooldown", 300), 300)
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

        order = await self._find_order(sender, user_id, token)
        if order is None:
            amount = safe_int(self._cfg("min_amount", 5), 5)
            event.set_result(
                MessageEventResult()
                .message(self._format(self._cfg("not_found_text", DEFAULT_NOT_FOUND_TEXT), sender)
                         .replace("{amount}", str(amount)))
                .stop_event()
            )
            return

        # 校验通过：加入白名单并清理状态
        whitelist = self._whitelist()
        whitelist.add(sender)
        if not self._save_whitelist(whitelist):
            event.set_result(
                MessageEventResult()
                .message("查到你的赞助啦！但我这边保存出错了，麻烦稍后再试或联系我哥哥～")
                .stop_event()
            )
            return
        self._blocked.discard(umo)
        self._rounds.pop(umo, None)
        self._round_ts.pop(umo, None)
        self._remind_day.pop(umo, None)
        logger.info(f"[sponsor_pass] {umo} 赞助校验通过，已加入白名单")

        amount = order.get("total_amount", "?")
        trade_no = order.get("out_trade_no", "?")
        event.set_result(
            MessageEventResult()
            .message("查到啦！感谢老板大气～你已经通过考验，以后随便找我玩！(๑•̀ㅂ•́)و✧")
            .stop_event()
        )
        await self._notify_owner(
            f"🔔 有人通过爱发电赞助自动通过准入：QQ {sender}，金额 ¥{amount}，订单号 {trade_no}"
        )

    async def _fetch_order_page(self, host: str, user_id: str, token: str,
                                page: int, per_page: int) -> dict:
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
        """在最近的订单中查找：状态=交易成功、金额达标、留言含该QQ。"""
        pages = max(1, min(safe_int(self._cfg("check_pages", 3), 3), 10))
        per_page = 50
        min_amount = float(safe_int(self._cfg("min_amount", 5), 5))
        last_error = None
        for host in AFDIAN_API_HOSTS:
            try:
                for page in range(1, pages + 1):
                    data = await self._fetch_order_page(host, user_id, token, page, per_page)
                    if not isinstance(data, dict) or int(data.get("ec", -1)) != 200:
                        raise RuntimeError(f"接口返回异常 ec={data.get('ec') if isinstance(data, dict) else data}")
                    payload = data.get("data") or {}
                    orders = payload.get("list") or []
                    for order in orders:
                        try:
                            if int(order.get("status", 0)) != 2:
                                continue
                            if float(order.get("total_amount", 0) or 0) < min_amount:
                                continue
                            if remark_has_qq(str(order.get("remark") or ""), qq):
                                return order
                        except (TypeError, ValueError):
                            continue
                    total_count = int(payload.get("total_count", 0) or 0)
                    if page * per_page >= total_count:
                        return None
                return None
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, ValueError) as e:
                last_error = e
                logger.warning(f"[sponsor_pass] 查询爱发电订单失败（{host}）: {e}")
                continue
        if last_error:
            logger.warning(f"[sponsor_pass] 爱发电查询最终失败: {last_error}")
        return None

    async def _notify_owner(self, text: str):
        if not bool(self._cfg("notify_owner", True)):
            return
        target = str(self._cfg("notify_target", "") or "").strip()
        if not target:
            return
        if ":" not in target:
            target = f"aiocqhttp:FriendMessage:{target}"
        try:
            await self.context.send_message(target, MessageChain().message(text))
        except Exception as e:
            logger.warning(f"[sponsor_pass] 通知管理员失败: {e}")

    # ---------------- 管理命令 ----------------

    @filter.command("准入")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def admission(self, event: AstrMessageEvent, action: str = "", qq: str = ""):
        action = (action or "").strip()
        qq = (qq or "").strip()
        whitelist = self._whitelist()

        if action in ("列表", "list"):
            names = "、".join(sorted(whitelist)) if whitelist else "（空）"
            yield event.plain_result(f"当前准入白名单：{names}")
            return

        if action in ("同意", "通过", "allow", "add"):
            if not qq.isdigit():
                yield event.plain_result("用法：/准入 同意 <QQ号>")
                return
            whitelist.add(qq)
            if self._save_whitelist(whitelist):
                # 清理该用户的拦截态，使其立即恢复对话
                for umo in [u for u in self._blocked if u.endswith(qq)]:
                    self._blocked.discard(umo)
                    self._rounds.pop(umo, None)
                    self._round_ts.pop(umo, None)
                    self._remind_day.pop(umo, None)
                yield event.plain_result(f"好啦，已放行 {qq}～")
            else:
                yield event.plain_result("保存白名单失败，请查看控制台日志")
            return

        if action in ("移除", "删除", "remove", "del"):
            if not qq.isdigit():
                yield event.plain_result("用法：/准入 移除 <QQ号>")
                return
            whitelist.discard(qq)
            if self._save_whitelist(whitelist):
                yield event.plain_result(f"已将 {qq} 移出白名单")
            else:
                yield event.plain_result("保存白名单失败，请查看控制台日志")
            return

        yield event.plain_result(
            "用法：/准入 同意 <QQ号> | /准入 移除 <QQ号> | /准入 列表"
        )

    async def terminate(self):
        """插件卸载/停用时清理内存状态。"""
        self._rounds.clear()
        self._round_ts.clear()
        self._blocked.clear()
        self._remind_day.clear()
        self._check_ts.clear()
