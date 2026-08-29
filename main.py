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
import os
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

SPONSOR_KEYWORDS = ("赞助", "爱发电", "afdian", "购买", "拍下")

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
            "stats": {"gate_total": 0, "sponsor_pass_total": 0, "manual_pass_total": 0},
        }
        os.makedirs(_STATE_DIR, exist_ok=True)
        self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                for key in self.data:
                    if key in loaded and loaded[key] is not None:
                        self.data[key] = loaded[key]
        except Exception:
            pass  # 首次运行或文件损坏：从零开始

    def save(self):
        try:
            with open(_STATE_TMP, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)
            os.replace(_STATE_TMP, _STATE_PATH)
        except Exception as e:
            logger.warning(f"[sponsor_pass] 状态保存失败: {e}")


def afdian_sign(token: str, user_id: str, params_json: str, ts: int, algo: str = "md5") -> str:
    """按接口代次生成爱发电开放接口签名。

    新版 query-orders（优先）：sign = HMAC-SHA256(key=token, msg=params).hexdigest()
    旧版 query-order （兜底）：sign = md5(token + 'params' + params + 'ts' + ts + 'user_id' + user_id)
    旧版算法由爱发电协议强制规定（不可更换为更强算法），官方文档示例向量：
      token=123, params={"a":333}, ts=1624339905, user_id=abc -> a4acc28b81598b7e5d84ebdc3e91710c
    """
    if algo == "hmac-sha256":
        return hmac.new(token.encode("utf-8"), params_json.encode("utf-8"), hashlib.sha256).hexdigest()
    digestmod = getattr(hashlib, "md5")  # 协议强制要求，见 docstring
    raw = f"{token}params{params_json}ts{ts}user_id{user_id}"
    return digestmod(raw.encode("utf-8")).hexdigest()


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
    "0.4.2",
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
        self._stats: dict = dict(d.get("stats") or {})
        # 瞬时状态（不落盘）
        self._check_ts: dict = {}  # umo -> 上次赞助校验时间（限频）

    # ---------------- 基础工具 ----------------

    def _persist(self):
        self._store.data["rounds"] = self._rounds
        self._store.data["round_ts"] = self._round_ts
        self._store.data["blocked"] = sorted(self._blocked)
        self._store.data["remind_day"] = self._remind_day
        self._store.data["passes"] = self._passes
        self._store.data["claimed_orders"] = self._claimed_orders
        self._store.data["pending"] = self._pending
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
        bl = self._cfg("blacklist", [])
        if isinstance(bl, (list, tuple, set)):
            return {str(x) for x in bl}
        return set()

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
        self._persist()

    def _clear_state_by_qq(self, qq: str):
        for umo in [u for u in list(self._blocked) + list(self._rounds) if u.endswith(qq)]:
            self._clear_session_state(umo)

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
        window = safe_int(self._cfg("round_window", 180), 180)
        window = max(1, window)
        max_rounds = safe_int(self._cfg("max_rounds", 6), 6)
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

        result = await self._search_orders(sender, user_id, token)
        matched = result["match"]
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
        validity = self._grant_pass(sender, order, source="sponsor")
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

    @staticmethod
    def _order_units(order: dict) -> int:
        """售卖方案按份购买的数量（sku_detail 求和，兼容 dict/字符串/数字），常规赞助记 1 份。"""
        total = 1
        try:
            sku = order.get("sku_detail")
            if isinstance(sku, str):
                sku = json.loads(sku)
            if isinstance(sku, list) and sku:
                counts = []
                for item in sku:
                    if isinstance(item, dict):
                        c = safe_int(item.get("count", item.get("num", 1)), 1)
                    elif isinstance(item, (int, float)):
                        c = safe_int(item, 1)
                    else:
                        c = 1
                    counts.append(max(1, c))
                total = max(1, sum(counts))
        except Exception:
            total = 1
        return total

    def _grant_pass(self, qq: str, order: dict, source: str = "sponsor"):
        """发放放行凭证（含核销台账防重复使用）。返回有效期描述；订单已被核销时返回 None。"""
        trade_no = str(order.get("out_trade_no", ""))
        if trade_no and trade_no in self._claimed_orders:
            return None
        expire_days = safe_int(self._cfg("sponsor_expire_days", 0), 0)
        units = self._order_units(order)
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
        if source == "sponsor":
            self._bump_stat("sponsor_pass_total")
        else:
            self._bump_stat("manual_pass_total")
        self._persist()
        return validity

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
        """兼容入口：只返回与QQ匹配的订单（无匹配返回 None）。"""
        result = await self._search_orders(qq, user_id, token)
        return result["match"]

    async def _search_orders(self, qq: str, user_id: str, token: str) -> dict:
        """扫描最近订单，返回 {match, unclaimed, other}：
        match=留言含该QQ的有效订单；unclaimed=有效但留言没写任何号码（防呆线索）；
        other=存在留言写了其他号码的订单。
        """
        pages = max(1, min(safe_int(self._cfg("check_pages", 3), 3), 10))
        per_page = 50
        min_amount = float(safe_int(self._cfg("min_amount", 5), 5))
        found = {"match": None, "unclaimed": None, "other": False}
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
                                continue  # 未支付/关闭的订单不参与
                            if float(order.get("total_amount", 0) or 0) < min_amount:
                                continue  # 金额不足
                        except (TypeError, ValueError):
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
                    total_count = int(payload.get("total_count", 0) or 0)
                    if page * per_page >= total_count:
                        return found
                return found
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, ValueError) as e:
                last_error = e
                logger.warning(f"[sponsor_pass] 查询爱发电订单失败（{host}）: {e}")
                continue
        if last_error:
            logger.warning(f"[sponsor_pass] 爱发电查询最终失败: {last_error}")
        return found

    async def _find_order_by_no(self, trade_no: str, user_id: str, token: str):
        """按订单号精确查询（out_trade_no）。"""
        params = json.dumps({"out_trade_no": str(trade_no)}, separators=(",", ":"))
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
                if not isinstance(data, dict) or int(data.get("ec", -1)) != 200:
                    raise RuntimeError(f"接口返回异常 ec={data.get('ec') if isinstance(data, dict) else data}")
                for order in (data.get("data") or {}).get("list") or []:
                    if str(order.get("out_trade_no", "")) == str(trade_no):
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

    async def _send_private_via_event(self, event: AstrMessageEvent, qq: str, text: str) -> bool:
        """优先复用当前 aiocqhttp bot 直发，避免统一会话路由不支持主动私聊。"""
        try:
            bot = getattr(event, "bot", None)
            sender = getattr(bot, "send_private_msg", None)
            if sender is not None:
                result = sender(user_id=int(qq), message=text)
                if hasattr(result, "__await__"):
                    await result
                return True
            api = getattr(bot, "api", None)
            call_action = getattr(api, "call_action", None)
            if call_action is not None:
                result = call_action("send_private_msg", user_id=int(qq), message=text)
                if hasattr(result, "__await__"):
                    await result
                return True
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
                "/准入 解黑 <QQ号> | /准入 订单 <订单号> <QQ号> | /准入 状态 <QQ号> | "
                "/准入 重置 <QQ号> | /准入 统计 | /准入 列表"
            )
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
            yield event.plain_result(
                f"累计触发准入 {self._stats.get('gate_total', 0)} 次；"
                f"赞助直通 {self._stats.get('sponsor_pass_total', 0)} 人次；"
                f"手动放行 {self._stats.get('manual_pass_total', 0)} 人次。"
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
            try:
                if int(order.get("status", 0)) != 2:
                    yield event.plain_result(f"订单 {trade_no} 还未支付完成（status={order.get('status')}），不能核销")
                    return
            except (TypeError, ValueError):
                pass
            amount = order.get("total_amount", "?")
            try:
                if float(amount or 0) < float(safe_int(self._cfg("min_amount", 5), 5)):
                    yield event.plain_result(
                        f"该订单金额 ¥{amount} 低于门槛 ¥{self._cfg('min_amount', 5)}；"
                        f"如确实要放行请用 /准入 同意 {target_qq}"
                    )
                    return
            except (TypeError, ValueError):
                pass
            validity = self._grant_pass(target_qq, order, source="manual")
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
