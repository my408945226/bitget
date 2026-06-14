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
import logging
import string
import random
from urllib.parse import urlencode
from typing import Optional, Dict, Any

import requests

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
        dry_run: bool = False,
    ):
        self.api_key = api_key
        self.secret_key = secret_key
        self.passphrase = passphrase
        self.log = logger
        self.dry_run = dry_run
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
        """GET /api/v3/account/assets - 获取账户资产"""
        path = "/api/v3/account/assets"
        params = {}
        resp = self._get(path, params, private=True)
        if resp.get("code") != "00000":
            return {}
        data = resp.get("data") or {}
        if isinstance(data, list):
            data = data[0] if data else {}
        # 找出 USDT 余额
        available = "0"
        for asset in data.get("assets") or []:
            if str(asset.get("coin", "")).upper() == margin_coin.upper():
                available = asset.get("available", "0")
                break
        return {
            "code": "00000",
            "data": [{
                "available": available,
                "accountEquity": data.get("accountEquity", "0"),
                "usdtEquity": data.get("usdtEquity", "0"),
            }]
        }

    def get_position(self, symbol: str) -> dict:
        """GET /api/v3/position/current-position - 获取持仓"""
        path = "/api/v3/position/current-position"
        params = {"category": "USDT-FUTURES", "symbol": symbol}
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
            }
            converted.append(item)
        return {
            "code": "00000",
            "data": converted,
        }

    # ====== 私密接口：交易 ======

    def place_order(self, inst_id: str, side: str, sz: float, px: float,
                    order_type: str = "market", reduce_only: bool = False,
                    cl_ord_id: Optional[str] = None) -> Dict[str, Any]:
        """
        POST /api/v3/trade/place-order - 下单（市价或限价）

        :param inst_id: 合约ID / symbol (e.g. "BGBUSDT")
        :param side: "buy" 或 "sell"
        :param sz: 数量
        :param px: 限价价格 (market 时可忽略)
        :param order_type: "market" 或 "limit"
        :param reduce_only: 平仓标记 (yes/no)
        :param cl_ord_id: 客户端订单ID (可选，自动生成)
        :return: {"code": "00000", "data": {"orderId": "xxx", ...}} 或错误
        """
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

    def place_post_only(self, inst_id: str, side: str, sz: float, px: float,
                        reduce_only: bool = False,
                        cl_ord_id: Optional[str] = None) -> Dict[str, Any]:
        """
        POST /api/v3/trade/place-order (postOnly) - 挂限价单

        :param inst_id: 合约ID (e.g. "BGBUSDT")
        :param side: "buy" 或 "sell"
        :param sz: 数量
        :param px: 限价
        :param reduce_only: 平仓标记
        :param cl_ord_id: 客户端订单ID
        :return: {"code": "00000", "data": {"orderId": "xxx", ...}} 或错误
        """
        if not cl_ord_id:
            cl_ord_id = _gen_cl_ord_id("sp")

        body = {
            "symbol": inst_id,
            "category": "USDT-FUTURES",
            "tdMode": "cross",
            "side": side,
            "orderType": "limit",
            "price": str(px),
            "qty": str(sz),
            "timeInForce": "post_only",
            "clientOid": cl_ord_id,
        }
        if reduce_only:
            body["reduceOnly"] = "yes"

        return self._post("/api/v3/trade/place-order", body)

    def cancel_order(self, inst_id: str, ord_id: str) -> Dict[str, Any]:
        """POST /api/v3/trade/cancel-order - 撤单"""
        body = {
            "symbol": inst_id,
            "category": "USDT-FUTURES",
            "orderId": ord_id,
        }
        return self._post("/api/v3/trade/cancel-order", body)

    def get_open_orders(self, inst_id: str) -> list:
        """GET /api/v3/trade/unfilled-orders - 查询当前挂单"""
        path = "/api/v3/trade/unfilled-orders"
        params = {"category": "USDT-FUTURES", "symbol": inst_id}
        resp = self._get(path, params, private=True)
        if resp.get("code") != "00000":
            return []
        data = resp.get("data") or {}
        return data.get("list") or []

    def get_order_info(self, inst_id: str, ord_id: str) -> dict:
        """GET /api/v3/trade/order-info - 查询订单详情

        orderStatus: new / partially_filled / filled / cancelled
        """
        path = "/api/v3/trade/order-info"
        params = {"category": "USDT-FUTURES", "symbol": inst_id, "orderId": ord_id}
        resp = self._get(path, params, private=True)
        if resp.get("code") != "00000":
            return {}
        return resp.get("data") or {}

    def cancel(self, inst_id: str, ord_id: str) -> Dict[str, Any]:
        """撤单（别名，兼容 OKX 框架）"""
        return self.cancel_order(inst_id, ord_id)

    def set_leverage(self, inst_id: str, leverage: int) -> Dict[str, Any]:
        """POST /api/v3/account/set-leverage - 设杠杆"""
        body = {
            "symbol": inst_id,
            "category": "USDT-FUTURES",
            "leverage": str(leverage),
        }
        return self._post("/api/v3/account/set-leverage", body)

    def set_hold_mode(self, hold_mode: str) -> Dict[str, Any]:
        """POST /api/v3/account/set-hold-mode - 设持仓模式"""
        body = {
            "holdMode": hold_mode,  # "one_way_mode" 或 "hedge_mode"
        }
        return self._post("/api/v3/account/set-hold-mode", body)

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
        if self.dry_run:
            self.log.debug(f"[DRY-RUN] POST {path} body={json.dumps(body, ensure_ascii=False)}")
            return {
                "code": "00000",
                "msg": "success",
                "data": {"orderId": "dry_run_order", "clientOid": body.get("clientOid", "")},
            }
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
