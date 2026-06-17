"""Bitget UTA v3 API 客户端 - 签名与请求

统一账户(UTA)专用，仅使用 /api/v3 接口
核心接口：
  - GET /api/v3/account/assets - 查资产
  - GET /api/v3/position/current-position - 查持仓
  - POST /api/v3/trade/place-order - 下单 (市价/限价/postOnly)
  - POST /api/v3/trade/cancel-order - 撤单
  - POST /api/v3/account/set-leverage - 设杠杆
  - POST /api/v3/account/set-hold-mode - 设持仓模式
"""
import hashlib
import hmac
import base64
import json
import time
import asyncio
import logging
import string
import random
from urllib.parse import urlencode

import requests

try:
    import websockets
except ImportError:
    websockets = None

BASE_URL = "https://api.bitget.com"


def _gen_cl_ord_id(prefix: str = "sp") -> str:
    """生成唯一 clOrdId (alphanumeric, 最长32字符)"""
    suffix = "".join(random.choices(string.ascii_letters + string.digits, k=8))
    return f"{prefix}{int(time.time()*1000)%1000000:06d}{suffix}"[:32]


class BitgetClient:
    def __init__(
        self,
        api_key: str,
        secret_key: str,
        passphrase: str,
        logger: logging.Logger,
    ):
        self.api_key = api_key
        self.secret_key = secret_key
        self.passphrase = passphrase
        self.log = logger
        self.session = requests.Session()

    def _timestamp(self) -> str:
        return str(int(time.time() * 1000))

    def _sign(self, timestamp: str, method: str, path: str,
              query: str = "", body: str = "") -> str:
        pre_hash = timestamp + method.upper() + path
        if query:
            pre_hash += "?" + query
        pre_hash += body
        mac = hmac.new(
            self.secret_key.encode("utf-8"),
            pre_hash.encode("utf-8"),
            hashlib.sha256,
        )
        return base64.b64encode(mac.digest()).decode("utf-8")

    def _headers(self, method: str, path: str,
                 query: str = "", body: str = "") -> dict:
        ts = self._timestamp()
        sign = self._sign(ts, method, path, query, body)
        return {
            "ACCESS-KEY": self.api_key,
            "ACCESS-SIGN": sign,
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
            "locale": "zh-CN",
        }

    # ====== 公开接口 ======

    def get_price(self, symbol: str) -> float:
        """GET /api/v3/market/tickers - 获取标记价格"""
        path = "/api/v3/market/tickers"
        params = {"category": "USDT-FUTURES", "symbol": symbol}
        resp = self._get(path, params)
        if resp.get("code") != "00000":
            return 0.0
        data = resp.get("data", [])
        if isinstance(data, list) and data:
            try:
                return float(data[0].get("markPrice") or data[0].get("lastPrice") or 0)
            except (TypeError, ValueError):
                return 0.0
        return 0.0

    def get_funding_rate(self, symbol: str) -> float:
        """GET /api/v3/market/tickers - 获取当前资金费率（8h 周期）"""
        path = "/api/v3/market/tickers"
        params = {"category": "USDT-FUTURES", "symbol": symbol}
        resp = self._get(path, params)
        if resp.get("code") != "00000":
            return 0.0
        data = resp.get("data", [])
        if isinstance(data, list) and data:
            try:
                return float(data[0].get("fundingRate") or 0)
            except (TypeError, ValueError):
                return 0.0
        return 0.0

    def get_contracts(self, symbol: str) -> dict:
        """GET /api/v3/market/instruments - 获取合约信息

        将 v3 字段归一化为 v2 兼容名 (minTradeNum / sizeMultiplier 等)，
        供 precision.py 使用。
        """
        path = "/api/v3/market/instruments"
        params = {"category": "USDT-FUTURES", "symbol": symbol}
        resp = self._get(path, params)
        if resp.get("code") != "00000":
            return {}
        data = resp.get("data", [])
        if isinstance(data, list):
            for item in data:
                if item.get("symbol") == symbol:
                    # v3 -> v2 字段映射
                    status = item.get("status", "")
                    return {
                        **item,
                        "symbolStatus": "normal" if status == "online" else status,
                        "minTradeNum": item.get("minOrderQty", "1"),
                        "sizeMultiplier": item.get("quantityMultiplier", "1"),
                        "minTradeUSDT": item.get("minOrderAmount", "5"),
                        "pricePlace": item.get("pricePrecision", "4"),
                        "volumePlace": item.get("quantityPrecision", "0"),
                    }
        return {}

    # ====== 私密接口：账户 ======

    def get_account(self, margin_coin: str = "USDT") -> dict:
        """GET /api/v3/account/assets - 获取账户资产（联合保证金 UTA）

        顶层即含账户级 accountEquity / usdtEquity / mmr / mgnRatio。
        """
        path = "/api/v3/account/assets"
        params = {"category": "USDT-FUTURES"}  # GET 必带 category
        resp = self._get(path, params, private=True)
        if resp.get("code") != "00000":
            return {}
        data = resp.get("data") or {}
        if isinstance(data, list):
            data = data[0] if data else {}

        # 总权益：优先 accountEquity(USD)，回退 usdtEquity
        equity = data.get("accountEquity") or data.get("usdtEquity") or "0"

        # 找出 USDT 可用余额
        available = "0"
        for asset in data.get("assets") or []:
            if str(asset.get("coin", "")).upper() == margin_coin.upper():
                available = asset.get("available", "0")
                break

        # 诊断：权益为空时打印顶层字段名，便于定位结构差异
        if not equity or float(equity or 0) == 0:
            self.log.warning(f"账户权益为 0，原始顶层字段: {list(data.keys())}")

        return {
            "code": "00000",
            "data": [{
                "available": available,
                "accountEquity": equity,
                "usdtEquity": data.get("usdtEquity", "0"),
                "mmr": data.get("mmr", "0"),
                "mgnRatio": data.get("mgnRatio", "0"),
            }]
        }

    def get_position(self, symbol: str) -> dict:
        """GET /api/v3/position/current-position - 获取持仓"""
        path = "/api/v3/position/current-position"
        params = {"category": "USDT-FUTURES"}
        if symbol:
            params["symbol"] = symbol
        resp = self._get(path, params, private=True)
        if resp.get("code") != "00000":
            return resp
        # 响应格式: {"code": "00000", "data": {"list": [...]}}
        data = resp.get("data") or {}
        positions = data.get("list") or []
        # 转换 v3 字段为 v2 兼容名称
        converted = []
        for p in positions:
            item = {
                **p,
                "holdSide": p.get("posSide") or p.get("holdSide", ""),
                "openPriceAvg": p.get("avgPrice") or p.get("openPriceAvg", "0"),
                "unrealizedPL": p.get("unrealisedPnl") or p.get("unrealizedPL", "0"),
                "marginRatio": p.get("mmr") or p.get("marginRatio", "0"),
                "total": p.get("pos") or p.get("total", "0"),
                "lever": float(p.get("leverage") or 0),
            }
            converted.append(item)
        return {
            "code": "00000",
            "data": converted,
        }

    # ====== 私密接口：交易 ======

    def place_order(self, inst_id: str, side: str, sz: float, px: float,
                    order_type: str = "market", reduce_only: bool = False,
                    cl_ord_id: str = None) -> dict:
        """下单"""
        if not cl_ord_id:
            cl_ord_id = _gen_cl_ord_id("sp")

        body = {
            "symbol": inst_id,
            "category": "USDT-FUTURES",
            "tdMode": "cross",
            "side": side,
            "orderType": order_type,
            "qty": str(sz),
            "clientOid": cl_ord_id,
        }
        if order_type == "limit":
            body["price"] = str(px)
        if reduce_only:
            body["reduceOnly"] = "yes"

        return self._post("/api/v3/trade/place-order", body)

    def cancel_order(self, inst_id: str, ord_id: str) -> dict:
        """撤单"""
        return self._post("/api/v3/trade/cancel-order", {
            "symbol": inst_id,
            "category": "USDT-FUTURES",
            "orderId": ord_id,
        })

    def get_open_orders(self, inst_id: str) -> list:
        """查挂单"""
        resp = self._get("/api/v3/trade/unfilled-orders", {"category": "USDT-FUTURES", "symbol": inst_id}, private=True)
        return (resp.get("data") or {}).get("list") or [] if resp.get("code") == "00000" else []

    def get_order_info(self, inst_id: str, ord_id: str) -> dict:
        """查订单"""
        resp = self._get("/api/v3/trade/order-info", {"category": "USDT-FUTURES", "symbol": inst_id, "orderId": ord_id}, private=True)
        return resp.get("data") or {} if resp.get("code") == "00000" else {}

    def cancel(self, inst_id: str, ord_id: str) -> dict:
        """撤单别名"""
        return self.cancel_order(inst_id, ord_id)

    def set_leverage(self, inst_id: str, leverage: int) -> dict:
        """设杠杆"""
        return self._post("/api/v3/account/set-leverage", {"symbol": inst_id, "category": "USDT-FUTURES", "leverage": str(leverage)})

    def set_hold_mode(self, hold_mode: str) -> dict:
        """设持仓模式"""
        return self._post("/api/v3/account/set-hold-mode", {"holdMode": hold_mode})

    # ====== 内部方法 ======

    def _build_query(self, params: dict) -> str:
        """按 key 字母升序拼接 queryString"""
        sorted_keys = sorted(params.keys())
        return urlencode({k: params[k] for k in sorted_keys})

    def _get(self, path: str, params: dict, private: bool = False) -> dict:
        query = self._build_query(params) if params else ""
        url = BASE_URL + path + ("?" + query if query else "")
        headers = {}
        if private:
            headers = self._headers("GET", path, query=query)
        try:
            resp = self.session.get(url, headers=headers, timeout=10)
            data = resp.json()
        except Exception as e:
            self.log.error(f"GET {path} 请求失败: {e}")
            return {"code": "NETWORK_ERROR", "msg": str(e), "data": None}
        if data.get("code") != "00000":
            self.log.warning(f"GET {path} 返回异常: code={data.get('code')} msg={data.get('msg')}")
        return data

    def _post(self, path: str, body: dict) -> dict:
        body_str = json.dumps(body, ensure_ascii=False)
        headers = self._headers("POST", path, body=body_str)
        url = BASE_URL + path
        try:
            resp = self.session.post(url, headers=headers, data=body_str.encode("utf-8"), timeout=10)
            data = resp.json()
        except Exception as e:
            self.log.error(f"POST {path} 请求失败: {e}")
            return {"code": "NETWORK_ERROR", "msg": str(e), "data": None}

        if data.get("code") != "00000":
            self.log.warning(f"POST {path} 返回异常: code={data.get('code')} msg={data.get('msg')}")
        else:
            self.log.info(f"POST {path} 成功")
        return data

    # ====== WebSocket 方法 ======
    # 私有频道 URL（V2）: wss://ws.bitget.com/v2/ws/private
    # 登录签名: Base64(HMAC_SHA256(secretKey, timestamp + "GET" + "/user/verify"))

    WS_PRIVATE_URL = "wss://ws.bitget.com/v2/ws/private"

    def _ws_login_sign(self, ts: str) -> str:
        """生成 WebSocket 登录签名"""
        pre_hash = ts + "GET" + "/user/verify"
        mac = hmac.new(
            self.secret_key.encode("utf-8"),
            pre_hash.encode("utf-8"),
            hashlib.sha256,
        )
        return base64.b64encode(mac.digest()).decode("utf-8")

    async def ws_connect(self, symbol: str, on_message):
        """连接 WebSocket 并订阅订单频道（Order-Channel）
        https://www.bitget.com/zh-CN/api-doc/contract/websocket/private/Order-Channel
        on_message: async 回调，接收单个订单 dict（已含 status/orderId/priceAvg 等）
        """
        if not websockets:
            self.log.warning("websockets 库未安装，降级到 REST 轮询")
            return

        try:
            async with websockets.connect(self.WS_PRIVATE_URL) as ws:
                # ① 登录认证（op=login, args 为数组）
                ts = str(int(time.time()))
                login_msg = {
                    "op": "login",
                    "args": [{
                        "apiKey": self.api_key,
                        "passphrase": self.passphrase,
                        "timestamp": ts,
                        "sign": self._ws_login_sign(ts),
                    }],
                }
                await ws.send(json.dumps(login_msg))
                login_resp = await ws.recv()
                self.log.debug(f"WS 登录: {login_resp}")

                # ② 订阅订单频道（op=subscribe, args 为数组）
                sub_msg = {
                    "op": "subscribe",
                    "args": [{"instType": "USDT-FUTURES", "channel": "orders", "instId": symbol}],
                }
                await ws.send(json.dumps(sub_msg))
                self.log.info(f"WS 订阅 {symbol} orders")

                # ③ 心跳协程：每 30s 发送 ping
                async def _heartbeat():
                    while True:
                        await asyncio.sleep(30)
                        await ws.send("ping")

                hb_task = asyncio.create_task(_heartbeat())
                try:
                    # ④ 接收推送
                    async for msg in ws:
                        if msg == "pong":
                            continue
                        try:
                            data = json.loads(msg)
                        except (ValueError, TypeError):
                            continue
                        # 仅处理 orders 频道的快照/更新推送
                        if data.get("arg", {}).get("channel") != "orders":
                            continue
                        if data.get("action") not in ("snapshot", "update"):
                            continue
                        for item in data.get("data", []):
                            await on_message(item)
                finally:
                    hb_task.cancel()

        except Exception as e:
            self.log.warning(f"WebSocket 连接失败，降级到 REST 轮询: {e}")
