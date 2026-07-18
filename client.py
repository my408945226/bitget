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
        self._account_cache = None   # (ts, result) — get_account 的 TTL 缓存

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

    def get_book_ticker(self, symbol: str) -> dict:
        """GET /api/v3/market/tickers - 返回买一/卖一盘口(BBO)：{bidPrice, askPrice}。
        tickers 直接带 bid1Price/ask1Price，无需单独 orderbook 接口。用于 post-only
        盘口追价起仓。查不到返回空 dict。"""
        resp = self._get("/api/v3/market/tickers",
                         {"category": "USDT-FUTURES", "symbol": symbol})
        if resp.get("code") != "00000":
            return {}
        data = resp.get("data") or []
        if isinstance(data, list):
            data = data[0] if data else {}
        if not isinstance(data, dict):
            return {}
        return {"bidPrice": data.get("bid1Price"), "askPrice": data.get("ask1Price")}

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

    def get_account(self, margin_coin: str = "USDT",
                    use_cache: bool = False, ttl: float = 3.0) -> dict:
        """GET /api/v3/account/assets - 统一账户(UTA)资产

        UTA 统一账户禁用 v2 经典接口（40085），故用 v3。账户级权益按数值择优
        （accountEquity → usdtEquity → effEquity），避免某一字段为 0 时误判。

        :param use_cache: True 时命中 ttl 秒内的账户快照缓存直接返回（保证金率秒级
            不剧变，下单/对账高频查账走缓存可减轻多币共用账户对 /account/assets 的
            压力、避免限频）。默认 False（对账/监控要实时）。查询失败不写缓存、返回
            空 dict 让上层保守处理。
        """
        if use_cache and self._account_cache:
            ts, cached = self._account_cache
            if time.time() - ts < ttl:
                return cached

        path = "/api/v3/account/assets"
        resp = self._get(path, {}, private=True)
        if resp.get("code") != "00000":
            return {}
        data = resp.get("data") or {}
        if isinstance(data, list):
            data = data[0] if data else {}

        def _f(key):
            try:
                return float(data.get(key) or 0)
            except (TypeError, ValueError):
                return 0.0

        # 账户总权益：按数值择优（"0" 是真值，不能用 or 回退）
        equity = _f("accountEquity") or _f("usdtEquity") or _f("effEquity")

        # USDT 可用余额
        available = "0"
        for asset in data.get("assets") or []:
            if str(asset.get("coin", "")).upper() == margin_coin.upper():
                available = asset.get("available", "0")
                break

        if equity == 0:
            self.log.warning(
                f"账户权益为 0 — accountEquity={data.get('accountEquity')} "
                f"usdtEquity={data.get('usdtEquity')} effEquity={data.get('effEquity')} "
                f"mmr={data.get('mmr')} mgnRatio={data.get('mgnRatio')}")

        result = {
            "code": "00000",
            "data": [{
                "available": available,
                "accountEquity": str(equity),
                "usdtEquity": data.get("usdtEquity", "0"),
                "mmr": data.get("mmr", "0"),
                "mgnRatio": data.get("mgnRatio", "0"),
            }]
        }
        self._account_cache = (time.time(), result)   # 仅缓存成功结果
        return result

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
                    cl_ord_id: str = None, post_only: bool = False) -> dict:
        """下单

        :param post_only: True → force=post_only（只做 maker，会立即 taker 成交时
            交易所直接拒单 code≠00000）。用于盘口追价起仓。
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
        if post_only:
            body["force"] = "post_only"
        if reduce_only:
            body["reduceOnly"] = "yes"

        resp = self._post("/api/v3/trade/place-order", body)
        # ★ Bitget reduceOnly（BUY 平仓）单下单成功却返回 data.orderId=None，只给 clientOid。
        # 用 clientOid 反查 order-info 回填真 orderId，否则上层拿不到 id、无法追踪该单：
        # 成交推送被 on_fill 当「非追踪订单」丢弃、平仓只能靠对账兜底 → 账目滞后疯狂重挂
        # （BGBUSDT/MIRAUSDT/REDUSDT 一系列事故的第一性根因，2026-07-10 诊断确认）。
        if resp.get("code") == "00000":
            data = resp.get("data")
            if isinstance(data, dict) and not data.get("orderId"):
                oid = self._resolve_ord_id_by_client_oid(
                    inst_id, data.get("clientOid") or cl_ord_id)
                if oid:
                    data["orderId"] = oid
        return resp

    def _resolve_ord_id_by_client_oid(self, inst_id: str, client_oid: str,
                                      retries: int = 4, delay: float = 0.2) -> str:
        """下单响应缺 orderId 时用 clientOid 反查真实 orderId（Bitget reduceOnly 单常见）。
        刚下单可能短暂未落库 → 重试几次；全部失败返回空串（上层据此告警）。"""
        if not client_oid:
            return ""
        for _ in range(retries):
            resp = self._get("/api/v3/trade/order-info",
                             {"category": "USDT-FUTURES", "symbol": inst_id,
                              "clientOid": client_oid}, private=True)
            if resp.get("code") == "00000":
                data = resp.get("data") or {}
                if isinstance(data, list):
                    data = data[0] if data else {}
                oid = data.get("orderId") if isinstance(data, dict) else None
                if oid:
                    return oid
            time.sleep(delay)
        self.log.warning(f"clientOid {client_oid} 反查 orderId 失败（{retries} 次）")
        return ""

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

    def get_last_fill_price(self, symbol: str) -> float:
        """GET /api/v3/trade/fills - 账户成交明细，返回最近一笔成交价（查不到返回 0）。

        用于 `--adopt` 裸写时自动取基准价。按成交时间取最新一条的 price。
        """
        resp = self._get("/api/v3/trade/fills",
                         {"category": "USDT-FUTURES", "symbol": symbol}, private=True)
        if resp.get("code") != "00000":
            return 0.0
        lst = (resp.get("data") or {}).get("list") or []
        if not lst:
            return 0.0
        try:
            # 按成交时间取最新一条（兼容 fillTime/cTime/ts 命名）
            latest = max(lst, key=lambda x: int(
                x.get("fillTime") or x.get("cTime") or x.get("ts") or 0))
            return float(latest.get("price") or latest.get("fillPrice")
                         or latest.get("priceAvg") or 0)
        except (ValueError, TypeError):
            return 0.0

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
            # 逐请求成功日志降到 DEBUG：长跑 + 多币时 INFO 会刷屏吃磁盘、淹没策略日志。
            # 保留 WARNING/ERROR（上面的异常分支）。
            self.log.debug(f"POST {path} 成功")
        return data

    # ====== WebSocket 方法 ======
    # 私有频道 URL（V2）: wss://ws.bitget.com/v2/ws/private
    # 登录签名: Base64(HMAC_SHA256(secretKey, timestamp + "GET" + "/user/verify"))

    WS_PRIVATE_URL = "wss://ws.bitget.com/v2/ws/private"
    WS_PING_SEC = 20            # 应用层 ping 间隔（官方建议 30s，取 20s 更保险）
    WS_PONG_TIMEOUT = 50        # 超过此秒数没收到任何消息/pong → 判定僵尸连接重连
    WS_RECONNECT_MAX = 30       # 重连退避上限（秒）

    def _ws_login_sign(self, ts: str) -> str:
        """生成 WebSocket 登录签名"""
        pre_hash = ts + "GET" + "/user/verify"
        mac = hmac.new(
            self.secret_key.encode("utf-8"),
            pre_hash.encode("utf-8"),
            hashlib.sha256,
        )
        return base64.b64encode(mac.digest()).decode("utf-8")

    async def ws_connect(self, symbol: str, on_message,
                         on_reconnect=None, should_stop=None):
        """连接 WebSocket 并订阅订单频道（Order-Channel），带断线/僵尸自动重连。
        https://www.bitget.com/zh-CN/api-doc/contract/websocket/private/Order-Channel

        官方规则（websocket-intro）：客户端定时发字符串 "ping"、期待 "pong"；
        **收不到 pong 即应重连**；服务器 2 分钟收不到 ping 才断开。故这里：
          - 应用层每 WS_PING_SEC 发 "ping"
          - 任何消息/pong 都刷新存活时间戳；超过 WS_PONG_TIMEOUT 无任何消息 →
            判定为半开/僵尸连接，主动关闭并重连（光发 ping 不检测 pong 会静默丢消息）
          - 关掉库自带协议级 ping（ping_interval=None），避免与应用层心跳互相干扰误关

        on_message:   async 回调，接收单个订单 dict
        on_reconnect: 可选，(重)连成功时同步回调，用于触发一次对账补上断连空窗期的成交
        should_stop:  可选，返回 True 时退出重连循环（优雅停止）
        """
        if not websockets:
            self.log.warning("websockets 库未安装，降级到 REST 轮询")
            return

        backoff = 1
        while not (should_stop and should_stop()):
            try:
                async with websockets.connect(self.WS_PRIVATE_URL,
                                              ping_interval=None,
                                              close_timeout=5) as ws:
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
                    try:
                        lr = json.loads(login_resp)
                        if str(lr.get("code", "0")) not in ("0", "00000") and lr.get("event") != "login":
                            raise RuntimeError(f"WS 登录失败: {login_resp}")
                    except (ValueError, TypeError):
                        pass

                    # ② 订阅订单频道（op=subscribe, args 为数组）
                    sub_msg = {
                        "op": "subscribe",
                        "args": [{"instType": "USDT-FUTURES", "channel": "orders", "instId": symbol}],
                    }
                    await ws.send(json.dumps(sub_msg))
                    self.log.info(f"WS 订阅 {symbol} orders")

                    backoff = 1                       # 连上即重置退避
                    last_seen = time.time()           # 最近一次收到任何消息/pong 的时间
                    if on_reconnect:
                        try:
                            on_reconnect()            # (重)连成功 → 触发对账补空窗
                        except Exception as e:
                            self.log.debug(f"on_reconnect 回调异常: {e}")

                    # ③ 心跳协程：定时发 ping，并检测 pong/消息超时（僵尸连接）
                    async def _heartbeat():
                        while True:
                            await asyncio.sleep(self.WS_PING_SEC)
                            try:
                                await ws.send("ping")
                            except Exception:
                                return
                            if time.time() - last_seen > self.WS_PONG_TIMEOUT:
                                self.log.warning(
                                    f"WS {self.WS_PONG_TIMEOUT}s 无任何消息/pong，判定僵尸连接，主动重连")
                                await ws.close()
                                return

                    hb_task = asyncio.create_task(_heartbeat())
                    try:
                        # ④ 接收推送
                        async for msg in ws:
                            last_seen = time.time()   # 收到任何消息都算连接存活
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

                # async with 正常退出（连接关闭）→ 进入重连
                self.log.warning(f"WS 连接已关闭，{backoff}s 后重连")
            except Exception as e:
                self.log.warning(f"WS 连接异常: {e}，{backoff}s 后重连")

            if should_stop and should_stop():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self.WS_RECONNECT_MAX)

        self.log.info("WS 重连循环已退出")
