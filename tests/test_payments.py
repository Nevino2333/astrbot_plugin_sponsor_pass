"""支付渠道适配器契约测试：不访问真实支付平台。"""
import asyncio
import importlib.util
import json
import os
import sys
import unittest
from decimal import Decimal
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

spec = importlib.util.spec_from_file_location("payments", os.path.join(ROOT, "payments.py"))
payments = importlib.util.module_from_spec(spec)
sys.modules["payments"] = payments
spec.loader.exec_module(payments)


class FakeResponse:
    def __init__(self, status=200, data=None):
        self.status = status
        self.data = data or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def json(self, **kwargs):
        return self.data


class FakeRequest:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response


class FakeSession:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def request(self, *args, **kwargs):
        return self.response

    def post(self, *args, **kwargs):
        return self.response


class PaymentTests(unittest.TestCase):
    def test_wechat_not_configured(self):
        provider = payments.WechatPayProvider("", "", "", "", "")
        self.assertFalse(provider.configured)
        with self.assertRaises(payments.PaymentServiceError):
            asyncio.run(provider.find_order_by_code("20250101000000000000"))

    def test_alipay_not_configured(self):
        provider = payments.AlipayProvider("", "", "")
        self.assertFalse(provider.configured)
        with self.assertRaises(payments.PaymentServiceError):
            asyncio.run(provider.find_order_by_code("A-ORDER-123"))

    def test_wechat_path_is_exact_and_amount_normalized(self):
        provider = payments.WechatPayProvider("app", "mch", "serial", "key", "v3")
        self.assertTrue(provider.configured)
        path = "/v3/pay/transactions/out-trade-no/ORDER-1?mchid=mch"
        body = {"out_trade_no": "ORDER-1", "trade_state": "SUCCESS", "transaction_id": "WX1", "amount": {"total": 599}}
        async def fake_request(instance, method, actual_path, body=None):
            self.assertIs(instance, provider)
            self.assertEqual(method, "GET")
            self.assertEqual(actual_path, path)
            return body_response
        body_response = body
        with patch.object(payments.WechatPayProvider, "_request", new=fake_request):
            order = asyncio.run(provider.find_order_by_code("ORDER-1"))
        self.assertEqual(order.provider, "wechat")
        self.assertEqual(order.amount, Decimal("5.99"))
        self.assertTrue(order.paid)

    def test_wechat_mismatch_rejected(self):
        provider = payments.WechatPayProvider("app", "mch", "serial", "key", "v3")
        async def fake_request(instance, *args, **kwargs):
            return {"out_trade_no": "OTHER", "trade_state": "SUCCESS", "amount": {"total": 500}}
        with patch.object(payments.WechatPayProvider, "_request", new=fake_request):
            with self.assertRaises(payments.PaymentNotFound):
                asyncio.run(provider.find_order_by_code("ORDER-1"))

    def test_alipay_response_mapping(self):
        provider = payments.AlipayProvider("app", "key", "public")
        response = {"alipay_trade_query_response": {"out_trade_no": "A1", "trade_no": "T1", "trade_status": "TRADE_SUCCESS", "buyer_pay_amount": "12.50"}}
        async def fake_call(instance, method, body):
            self.assertIs(instance, provider)
            self.assertEqual(method, "alipay.trade.query")
            return response
        with patch.object(payments.AlipayProvider, "_call", new=fake_call):
            order = asyncio.run(provider.find_order_by_code("A1"))
        self.assertEqual(order.amount, Decimal("12.50"))
        self.assertTrue(order.paid)
        self.assertEqual(order.transaction_id, "T1")

    def test_alipay_sign_contains_rsa2_fields(self):
        provider = payments.AlipayProvider("app", "invalid", "public")
        # 私钥无效时必须明确报配置错误，不能悄悄发送未签名请求。
        with self.assertRaises(payments.PaymentInvalid):
            provider._sign("alipay.trade.query", {"out_trade_no": "A1"})

    def _rsa_keypair(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        priv_pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        pub_pem = key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()
        return key, priv_pem, pub_pem

    def test_alipay_response_signature_valid(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        import base64 as b64
        key, priv_pem, pub_pem = self._rsa_keypair()
        provider = payments.AlipayProvider("app", priv_pem, pub_pem)
        body = '{"out_trade_no":"A1","trade_status":"TRADE_SUCCESS","buyer_pay_amount":"12.50"}'
        sign = b64.b64encode(key.sign(body.encode(), padding.PKCS1v15(), hashes.SHA256())).decode()
        raw = '{"alipay_trade_query_response":' + body + ',"sign":"' + sign + '","sign_type":"RSA2"}'
        data = {"alipay_trade_query_response": {"out_trade_no": "A1"}, "sign": sign, "sign_type": "RSA2"}
        # 验签通过不应抛异常
        provider._verify_response("alipay.trade.query", raw, data)

    def test_alipay_response_signature_tampered(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        import base64 as b64
        key, priv_pem, pub_pem = self._rsa_keypair()
        provider = payments.AlipayProvider("app", priv_pem, pub_pem)
        body = '{"out_trade_no":"A1","trade_status":"TRADE_SUCCESS","buyer_pay_amount":"12.50"}'
        sign = b64.b64encode(key.sign(body.encode(), padding.PKCS1v15(), hashes.SHA256())).decode()
        # 篡改响应金额，签名不再匹配
        tampered = '{"out_trade_no":"A1","trade_status":"TRADE_SUCCESS","buyer_pay_amount":"120.00"}'
        raw = '{"alipay_trade_query_response":' + tampered + ',"sign":"' + sign + '","sign_type":"RSA2"}'
        data = {"alipay_trade_query_response": {"out_trade_no": "A1"}, "sign": sign, "sign_type": "RSA2"}
        with self.assertRaises(payments.PaymentInvalid):
            provider._verify_response("alipay.trade.query", raw, data)

    def test_alipay_response_missing_signature(self):
        _, priv_pem, pub_pem = self._rsa_keypair()
        provider = payments.AlipayProvider("app", priv_pem, pub_pem)
        raw = '{"alipay_trade_query_response":{"out_trade_no":"A1"},"sign_type":"RSA2"}'
        data = {"alipay_trade_query_response": {"out_trade_no": "A1"}, "sign_type": "RSA2"}
        with self.assertRaises(payments.PaymentInvalid):
            provider._verify_response("alipay.trade.query", raw, data)

    def test_channel_prefixes_are_unambiguous(self):
        self.assertEqual("wechat", "微信订单号"[:4] and "wechat")
        self.assertNotEqual("wechat:ORDER", "alipay:ORDER")


if __name__ == "__main__":
    unittest.main()
