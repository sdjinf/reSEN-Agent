"""peer 协议（TCP 版）：收（服务端）+ 发（`peer_agent` 工具用）。

底层传输是 [`api/connect`](connect/readme.md) 的 reSEN TCP v1 加密帧；
本模块只负责业务语义：把一段 `messages` 交给远端/本地模型答一轮。

请求/响应载荷是 UTF-8 JSON（放在 DATA 帧里）：

```json
请求  {"messages": [ccmp, ...]}
响应  一条 ccmp，或 {"error": "..."}
```

`access_key` **不再随包传输**：它直接作为会话密钥，hello/fcode/tag 过不去的连接
根本进不来，因此没有 403 这一层。

命令行：
```bash
python -m api.peer_api                          # 监听 config.json 的 peer_host/peer_port（缺省 0.0.0.0:1124）
python -m api.peer_api --port 9000 --key my-key # 临时覆盖端口/直接给 key
python -m api.peer_api --no-tools               # 强制关闭（默认看 config.json 的 peer_tools）
python -m api.peer_api --probe tcp://host:1124 --key k   # 只打一发请求看结果
```
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import traceback
from functools import partial
from pathlib import Path
from typing import Any, Callable, cast
from urllib.parse import SplitResult, urlsplit, urlunsplit

from openai import AsyncOpenAI as openai
from openai.types.chat import ChatCompletionMessageParam as ccmp

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentapi.agentapi import agent_api
from api.connect import session
from file.config import NO_ACCESS_KEY, config
from file.dick import dick
from file.jsonfile import jsonfile

_CONFIG = Path(__file__).resolve().parents[1] / "configure" / "config.json"
_CONNECT = Path(__file__).resolve().parents[1] / "configure" / "connect.json"

NO_APIURL = "NO-APIURL"
PORT = session.PORT
DEFAULT_HOST = "0.0.0.0"

_MAX_ROUNDS = 8
_DEFAULT_SCHEME = "tcp"


class api_error(Exception):
    """peer 调用失败（占位参数、连不上、对端拒绝、响应非法等）。"""


# --------------------------------------------------------------------- 地址
def _split(url: str) -> SplitResult:
    raw = (url or "").strip()
    if not raw:
        raise api_error("url 是空的：传 host[:port]，例如 192.168.1.7:1124")
    if "://" not in raw:
        raw = f"{_DEFAULT_SCHEME}://{raw}"
    parts = urlsplit(raw)
    if not parts.hostname:
        raise api_error(f"url 里没有主机名：{url!r}")
    return parts


def with_port(url: str) -> str:
    """把 url 里缺的端口补成 ``PORT``；显式写了端口就原样返回。"""
    parts = _split(url)
    if parts.port is not None:
        return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, parts.fragment))
    return urlunsplit(
        (parts.scheme, f"{parts.netloc}:{PORT}", parts.path, parts.query, parts.fragment)
    )


def _target(url: str) -> tuple[str, int]:
    parts = _split(url)
    return str(parts.hostname), parts.port or PORT


# --------------------------------------------------------------- 设备清单
BLANK_DEVICE: dict[str, Any] = {"name": "", "desc": "", "host": "", "port": "", "tag": []}


def devices() -> list[dict[str, Any]]:
    try:
        book: Any = jsonfile(str(_CONNECT)).read()
    except Exception:
        return []
    return [entry for entry in book if isinstance(entry, dict)] if isinstance(book, list) else []


def remember(
    url: str, name: str = "", desc: str = "", tag: list[str] | None = None
) -> tuple[bool, dict[str, Any]]:
    host, port = _target(url)
    book = devices()

    def matched(entry: dict[str, Any]) -> bool:
        return str(entry.get("host")) == host and str(entry.get("port")) == str(port)

    entry = next((entry for entry in book if matched(entry)), None)
    if entry is not None:
        if name:
            entry["name"] = name
        if desc:
            entry["desc"] = desc
        tags = entry.get("tag")
        if not isinstance(tags, list):
            tags = entry["tag"] = []
        tags.extend(item for item in (tag or []) if item not in tags)
        fresh = False
    else:
        blank = next(
            (entry for entry in book if not str(entry.get("host")) and not str(entry.get("port"))),
            None,
        )
        fresh = True
        if blank is None:
            entry = dict(BLANK_DEVICE)
            book.append(entry)
        else:
            entry = blank
        entry.update(
            {"name": name or host, "desc": desc, "host": host, "port": str(port), "tag": list(tag or [])}
        )
    jsonfile(str(_CONNECT)).write(book)
    return fresh, entry


# --------------------------------------------------------------------- 发送
def _check(usrmsg: list[ccmp], access_key: str, url: str) -> None:
    if not url or url == NO_APIURL:
        raise api_error("没有可用的 url：把它作为参数传给 chat(url=...)")
    if access_key == NO_ACCESS_KEY:
        raise api_error(
            f"access_key 还是占位值 {NO_ACCESS_KEY!r}：请传入真正的 key（空串表示用空密钥）"
        )
    if not usrmsg:
        raise api_error("usrmsg 是空的：至少要有一条消息")


def _parse(raw: bytes) -> ccmp:
    try:
        data: Any = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise api_error(f"响应不是合法 JSON（{type(e).__name__}）：{raw[:200]!r}") from e
    if isinstance(data, dict) and isinstance(data.get("message"), dict):
        data = data["message"]
    if isinstance(data, dict) and "error" in data:
        raise api_error(str(data["error"]))
    if not isinstance(data, dict) or not any(k in data for k in ("role", "content", "tool_calls")):
        raise api_error(f"响应不是一条 ccmp：{str(data)[:200]}")
    return cast(ccmp, data)


async def chat(
    usrmsg: list[ccmp],
    access_key: str = NO_ACCESS_KEY,
    url: str = NO_APIURL,
    timeout: float = 60.0,
    client: Any = None,
) -> ccmp:
    """把一段对话发给远端 peer，返回它的一条回复。

    ``client`` 参数仅为兼容旧 HTTP 版签名而保留，传了也会被忽略。
    """
    del client
    _check(usrmsg, access_key, url)
    host, port = _target(url)
    payload = json.dumps({"messages": usrmsg}, ensure_ascii=False).encode("utf-8")
    try:
        raw = await session.request(host, port, access_key, payload, timeout=timeout)
    except Exception as e:
        raise api_error(f"{type(e).__name__}: {e}（请求 {host}:{port}）") from e
    return _parse(raw)


# --------------------------------------------------------------------- 服务端
def _error(message: str) -> bytes:
    return json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")


async def _answer(
    api: agent_api,
    messages: list[ccmp],
    use_tools: bool,
    log: Callable[[str], None] = print,
) -> ccmp:
    chain = list(messages)
    reply: ccmp = chain[-1]
    for _ in range(_MAX_ROUNDS):
        reply = await api.chat(chain)
        calls = reply.get("tool_calls") or []
        if not calls or not use_tools:
            return reply
        log(
            "远端请求触发本地工具："
            + ", ".join(str(c.get("function", {}).get("name")) for c in calls)
        )
        chain.append(reply)
        chain.extend(await asyncio.to_thread(api.tool, reply))
    return reply


async def _handle(
    api: agent_api,
    use_tools: bool,
    log: Callable[[str], None],
    data: bytes,
) -> bytes:
    try:
        payload: Any = json.loads(data.decode("utf-8") or "{}")
    except Exception as e:
        return _error(f"请求体不是合法 JSON：{type(e).__name__}: {e}")
    if not isinstance(payload, dict):
        return _error("请求体必须是一个 JSON object")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return _error("'messages' 必须是 [ccmp, ...]（至少一条）")
    if not all(isinstance(msg, dict) for msg in messages):
        return _error("'messages' 必须是 [ccmp, ...]（至少一条）")
    try:
        reply = await _answer(api, cast(list[ccmp], messages), use_tools, log)
    except BaseException as e:  # 无论出什么事都回包，否则客户端只会看到超时
        log(f"应答失败：{type(e).__name__}: {e}")
        traceback.print_exc()
        return _error(f"{type(e).__name__}: {e}")
    return json.dumps(reply, ensure_ascii=False).encode("utf-8")


def tools_enabled() -> bool:
    return config.bool("peer_tools", True)


_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def is_loopback(host: str) -> bool:
    """监听地址是否只对本机可见。"""
    text = (host or "").strip().strip("[]").lower()
    return text in _LOOPBACK or text.startswith("127.")


def guard_exposure(host: str, access_key: str) -> None:
    """空 access_key 等于**没有**密钥，只允许绑回环，否则拒启。

    注意这与密钥长短无关：空串不是一个弱密钥，是根本没有密钥 ——
    攻击者不需要猜，配同样一个空串就能连上。约束应当是“模式”而不是“长度”。
    """
    if access_key or is_loopback(host):
        return
    raise api_error(
        f"access_key 为空时只允许监听回环地址（当前 {host}）。空密钥等于无鉴权："
        f"请把 access_key 写进 {_CONFIG}，或把 peer_host 改成 127.0.0.1"
    )


def configured_address(cfg: dick | None = None) -> tuple[str, int]:
    """监听地址：优先 config.json 的 ``peer_host``/``peer_port``，缺失或非法时回落默认值。"""
    cfg = cfg if cfg is not None else config
    host = str(cfg.str("peer_host", DEFAULT_HOST) or DEFAULT_HOST).strip() or DEFAULT_HOST
    try:
        port = int(cfg.int("peer_port", PORT))
    except (TypeError, ValueError):
        port = PORT
    if not 0 < port < 65536:
        port = PORT
    return host, port


class PeerServer(session.PeerServer):
    """在 TCP 帧通道上跑本地模型的 peer 服务。"""

    api: agent_api
    use_tools: bool

    def __init__(
        self,
        api: agent_api,
        handler: session.Handler,
        host: str,
        port: int,
        access_key: str,
        use_tools: bool,
    ) -> None:
        super().__init__(handler, host, port, access_key)
        self.api = api
        self.use_tools = use_tools


def make_server(
    key: str | None = None,
    use_tools: bool | None = None,
    host: str | None = None,
    port: int | None = None,
) -> PeerServer:
    cfg = config
    access_key = key if key is not None else cfg.str("access_key", NO_ACCESS_KEY)
    if access_key == NO_ACCESS_KEY:
        raise api_error(
            f"access_key 还是占位值 {NO_ACCESS_KEY!r}：用 --key 传一个，或把 key 写进 {_CONFIG}"
        )
    apikey = str(cfg.str("apikey") or "").strip()
    if not apikey:
        raise api_error(f"配置未完善：apikey 为空，请先在 F2 设置里填写（{_CONFIG}）")
    api = agent_api(
        openai(
            base_url=str(cfg.str("baseurl") or "").strip() or "https://api.openai.com/v1",
            api_key=apikey,
        ),
        cfg.str("modelname"),
    )
    bind_host, bind_port = configured_address(cfg)
    effective_host = host if host else bind_host
    effective_port = port if port is not None else bind_port
    guard_exposure(effective_host, access_key)
    enabled = tools_enabled() if use_tools is None else use_tools
    handler = partial(_handle, api, enabled, lambda msg: print(f"[peer] {msg}", flush=True))
    return PeerServer(
        api,
        handler,
        effective_host,
        effective_port,
        access_key,
        enabled,
    )


def serve(
    host: str | None = None,
    port: int | None = None,
    key: str | None = None,
    use_tools: bool | None = None,
) -> None:
    srv = make_server(key, use_tools, host, port)
    print(f"peer 服务已启动：tcp://{srv.host}:{srv.port}（reSEN TCP v1）")
    print(
        f"  access_key：{'来自 --key' if key is not None else '取自 config.json'}；"
        f"远端驱动本地工具：{'允许' if srv.use_tools else '禁止'}"
    )
    print(f"  本地模型：{srv.api.model_name}（每次请求独立应答，不碰本地 memory）")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()


def probe(url: str, key: str | None = None, text: str = "ping") -> int:
    """按 peer 协议打一发请求并打印结果（诊断用）。"""
    host, port = _target(url)
    if key is None:
        key = config.str("access_key", NO_ACCESS_KEY)
    print(f"TCP {host}:{port}")
    print(f"  access_key：{'（空）' if not key else '已提供'}")
    print(f"  content: {text!r}")

    async def send() -> ccmp:
        return await chat([cast(ccmp, {"role": "user", "content": text})], key or "", f"{host}:{port}")

    try:
        reply = asyncio.run(send())
    except Exception as e:
        print(f"  ✗ {type(e).__name__}: {e}")
        return 1
    print(f"  role: {reply.get('role')}")
    print(f"  content: {str(reply.get('content'))[:500]}")
    calls = cast(dict[str, Any], reply).get("tool_calls")
    if isinstance(calls, list) and calls:
        print(f"  tool_calls: {len(calls)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="peer 服务端：TCP 加密帧 + 本地 agent 应答")
    parser.add_argument("--host", default=None, help="监听地址（默认取 config.json 的 peer_host，缺省 0.0.0.0）")
    parser.add_argument("--port", type=int, default=None, help=f"监听端口（默认取 config.json 的 peer_port，缺省 {PORT}）")
    parser.add_argument("--key", default=None, help="access_key（默认取自 config.json）")
    parser.add_argument("--no-tools", action="store_true", help="禁止远端驱动本地工具")
    parser.add_argument("--probe", default=None, help="只打一发请求（如 tcp://host:1124）")
    args = parser.parse_args(argv)

    if args.probe:
        return probe(args.probe, args.key)
    try:
        serve(args.host, args.port, args.key, None if not args.no_tools else False)
    except api_error as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
