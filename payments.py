"""商户支付渠道适配器。

只实现官方商户 API，不支持个人收款码、账单抓取或非公开接口。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

import aiohttp


class PaymentError(RuntimeError):
    """支付服务或协议错误。"""


class PaymentNotFound(PaymentError):
    pass


class PaymentUnpaid(PaymentError):
    pass


class PaymentInvalid(PaymentError):
    pass


class PaymentAlreadyClaimed(PaymentError):
    pass


class PaymentServiceError(PaymentError):
    pass


@dataclass(slots=True)
class PaymentOrder:
    provider: str
    order_id: str
    paid: bool
    amount: Decimal
    remark: str = ""
    plan_id: str = ""
    product_id: str = ""
    units: int = 1
    created_at: int | None = None
    transaction_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def _decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PaymentInvalid("金额字段异常") from exc
    if not result.is_finite() or result < 0:
        raise PaymentInvalid("金额字段异常")
    return result


def _response_json(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise PaymentServiceError("支付接口返回格式异常")
    return data


def _rsa_sign(private_key_pem: str, message: bytes) -> str:
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
        # PKCS#1 v1.5 是微信支付 API v3 与支付宝 RSA2 签名协议要求的签名填充，
        # 这里不是解密场景，不能替换为 OAEP，否则官方接口会拒绝请求。
        signature = key.sign(message, padding.PKCS1v15(), hashes.SHA256())
        return base64.b64encode(signature).decode()
    except ImportError as exc:
        raise PaymentServiceError("缺少 cryptography 依赖") from exc
    except Exception as exc:
        raise PaymentInvalid("商户私钥无效") from exc


def _rsa_verify(public_key_pem: str, message: bytes, signature_b64: str) -> bool:
    """使用支付宝公钥验证 RSA2 响应签名；任何失败都返回 False（fail-closed）。"""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        key = serialization.load_pem_public_key(public_key_pem.encode("utf-8"))
        key.verify(
            base64.b64decode(signature_b64),
            message,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return True
    except Exception:
        return False


@dataclass(slots=True)
class WechatPayProvider:
    appid: str
    mchid: str
    serial_no: str
    private_key: str
    api_v3_key: str
    notify_url: str = ""
    base_url: str = "https://api.mch.weixin.qq.com"
    timeout_seconds: int = 15

    @property
    def configured(self) -> bool:
        return all((self.appid, self.mchid, self.serial_no, self.private_key, self.api_v3_key))

    def _authorization(self, method: str, path: str, body: str = "") -> str:
        timestamp = str(int(time.time()))
        nonce = secrets.token_hex(16)
        message = f"{method}\n{path}\n{timestamp}\n{nonce}\n{body}\n".encode()
        signature = _rsa_sign(self.private_key, message)
        return (
            'WECHATPAY2-SHA256-RSA2048 '
            f'mchid="{self.mchid}",nonce_str="{nonce}",timestamp="{timestamp}",'
            f'serial_no="{self.serial_no}",signature="{signature}"'
        )

    async def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.configured:
            raise PaymentServiceError("微信支付商户配置不完整")
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")) if body is not None else ""
        headers = {
            "Authorization": self._authorization(method, path, payload),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(method, self.base_url + path, headers=headers, data=payload or None) as response:
                    if response.status >= 500:
                        raise PaymentServiceError(f"微信支付服务异常 HTTP {response.status}")
                    data = await response.json(content_type=None)
                    if response.status >= 400:
                        raise PaymentInvalid("微信支付请求被拒绝")
                    return _response_json(data)
        except PaymentError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise PaymentServiceError("微信支付服务暂时不可用") from exc

    async def create_payment(self, order_id: str, amount: Decimal, description: str) -> str:
        body = {
            "appid": self.appid,
            "mchid": self.mchid,
            "description": description[:127],
            "out_trade_no": order_id,
            "notify_url": self.notify_url,
            "amount": {"total": int(amount * 100), "currency": "CNY"},
        }
        data = await self._request("POST", "/v3/pay/transactions/native", body)
        code_url = data.get("code_url")
        if not isinstance(code_url, str) or not code_url:
            raise PaymentServiceError("微信支付未返回二维码链接")
        return code_url

    async def find_order_by_code(self, order_id: str) -> PaymentOrder:
        path = f"/v3/pay/transactions/out-trade-no/{quote(order_id, safe='')}?mchid={quote(self.mchid, safe='')}"
        data = await self._request("GET", path)
        returned = str(data.get("out_trade_no", ""))
        if returned != order_id:
            raise PaymentNotFound("微信订单号不匹配")
        state = str(data.get("trade_state", ""))
        amount = _decimal((data.get("amount") or {}).get("total")) / 100
        paid = state == "SUCCESS"
        return PaymentOrder(
            provider="wechat",
            order_id=returned,
            paid=paid,
            amount=amount,
            transaction_id=str(data.get("transaction_id", "")),
            created_at=None,
            raw=data,
        )


@dataclass(slots=True)
class AlipayProvider:
    app_id: str
    private_key: str
    alipay_public_key: str
    notify_url: str = ""
    gateway: str = "https://openapi.alipay.com/gateway.do"
    timeout_seconds: int = 15

    @property
    def configured(self) -> bool:
        return all((self.app_id, self.private_key, self.alipay_public_key))

    def _sign(self, method: str, biz_content: dict[str, Any]) -> dict[str, str]:
        params = {
            "app_id": self.app_id,
            "method": method,
            "format": "json",
            "charset": "utf-8",
            "sign_type": "RSA2",
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "version": "1.0",
            "biz_content": json.dumps(biz_content, ensure_ascii=False, separators=(",", ":")),
        }
        sign_content = "&".join(f"{key}={params[key]}" for key in sorted(params) if params[key] != "")
        params["sign"] = _rsa_sign(self.private_key, sign_content.encode())
        return params

    def _verify_response(self, method: str, raw_text: str, data: dict[str, Any]) -> None:
        """校验支付宝网关响应的 RSA2 签名；缺失或验签失败一律拒绝。

        支付宝对响应对象原始 JSON 字符串签名（排序键、紧凑分隔符），
        因此从原始响应文本中精确截取响应对象再验签，避免序列化差异。
        """
        response_keys = {
            "alipay.trade.precreate": "alipay_trade_precreate_response",
            "alipay.trade.query": "alipay_trade_query_response",
        }
        response_key = response_keys.get(method, "")
        if not response_key:
            raise PaymentInvalid("不支持的支付宝接口")
        sign = data.get("sign")
        if not isinstance(sign, str) or not sign:
            raise PaymentInvalid("支付宝响应缺少签名")
        marker = f'"{response_key}":'
        start_idx = raw_text.find(marker)
        if start_idx == -1:
            raise PaymentInvalid("支付宝响应结构异常")
        body_start = raw_text.find("{", start_idx)
        sign_idx = raw_text.find('"sign"', body_start)
        if sign_idx == -1:
            sign_idx = len(raw_text)
        body_end = raw_text.rfind("}", body_start, sign_idx)
        if body_start == -1 or body_end == -1 or body_end <= body_start:
            raise PaymentInvalid("支付宝响应结构异常")
        content = raw_text[body_start:body_end + 1]
        if not _rsa_verify(self.alipay_public_key, content.encode("utf-8"), sign):
            raise PaymentInvalid("支付宝响应验签失败")

    async def _call(self, method: str, biz_content: dict[str, Any]) -> dict[str, Any]:
        if not self.configured:
            raise PaymentServiceError("支付宝商户配置不完整")
        params = self._sign(method, biz_content)
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(self.gateway, data=params) as response:
                    if response.status >= 500:
                        raise PaymentServiceError(f"支付宝服务异常 HTTP {response.status}")
                    raw_text = await response.text()
                    try:
                        data = _response_json(json.loads(raw_text))
                    except (ValueError, TypeError) as exc:
                        raise PaymentInvalid("支付宝响应格式异常") from exc
                    self._verify_response(method, raw_text, data)
                    return data
        except PaymentError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise PaymentServiceError("支付宝服务暂时不可用") from exc

    async def create_payment(self, order_id: str, amount: Decimal, subject: str) -> str:
        data = await self._call("alipay.trade.precreate", {
            "out_trade_no": order_id,
            "total_amount": f"{amount:.2f}",
            "subject": subject[:256],
            "timeout_express": "30m",
            "notify_url": self.notify_url,
        })
        response = data.get("alipay_trade_precreate_response") or {}
        if response.get("code") != "10000" or not response.get("qr_code"):
            raise PaymentServiceError("支付宝未返回二维码链接")
        return str(response["qr_code"])

    async def find_order_by_code(self, order_id: str) -> PaymentOrder:
        data = await self._call("alipay.trade.query", {"out_trade_no": order_id})
        response = data.get("alipay_trade_query_response") or {}
        returned = str(response.get("out_trade_no", ""))
        if returned != order_id:
            raise PaymentNotFound("支付宝订单号不匹配")
        amount = _decimal(response.get("buyer_pay_amount", response.get("receipt_amount", "0")))
        status = str(response.get("trade_status", ""))
        return PaymentOrder(
            provider="alipay",
            order_id=returned,
            paid=status == "TRADE_SUCCESS",
            amount=amount,
            transaction_id=str(response.get("trade_no", "")),
            raw=response,
        )
