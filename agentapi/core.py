import asyncio
import json
from functools import wraps
from typing import Any, Awaitable, Callable, ParamSpec, TypeVar, cast
from pathlib import Path

from openai import AsyncOpenAI as openai
from openai.lib.streaming.chat import AsyncChatCompletionStreamManager as accsm
from openai.types.chat import ChatCompletionMessageParam as ccmp
from rich.markdown import Markdown
from textual.widgets import Static

from .agentapi import agent_api as agentapi, _from_message
from api.peer_api import NO_ACCESS_KEY
from file.config import config
from file.dick import dick

class agent_error(Exception):
    pass

_P = ParamSpec("_P")
_R = TypeVar("_R")

_CONFIG_KEYS = ("reasoning_effort", "think_collapsed", "context_limit", "peer_serve", "peer_tools", "extra")
_ADDRESS_KEYS = ("peer_host", "peer_port")
_SETTING_ORDER = ("apikey", "baseurl", "modelname", "access_key", *_CONFIG_KEYS, *_ADDRESS_KEYS)
_RESERVED_KEYS = ("config", "memory", "current")

# 仅用于启动提示，不会因此拒绝启动
_PLACEHOLDER_VALUES = {
    "",
    "sk-你的密钥",
    "你的密钥",
    "your-api-key",
    "your_api_key",
    "your-api-key-here",
    "sk-your-key",
    "sk-xxx",
    "sk-xxxx",
    "xxx",
    "changeme",
    "none",
    "null",
}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ("" if value is None else str(value))


def _is_placeholder(value: Any) -> bool:
    text = _text(value).lower()
    return (
        text in _PLACEHOLDER_VALUES
        or "你的密钥" in text
        or "your-api-key" in text
        or "your_api_key" in text
    )


def _client_for(apikey: str, baseurl: str, modelname: str) -> agentapi:
    # 空值/占位符不阻止启动：用临时占位凭据构造，真正调用失败时再由 UI 提示
    return agentapi(openai(
        base_url=_text(baseurl) or "https://api.openai.com/v1",
        api_key=_text(apikey) or "sk-unconfigured",
    ), _text(modelname))

def _guarded(fn: Callable[_P, Awaitable[_R]]) -> Callable[_P, Awaitable[_R]]:
    @wraps(fn)
    async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return await fn(*args, **kwargs)
        except agent_error:
            raise
        except Exception as e:
            raise agent_error(f"{type(e).__name__}: {e}") from e
    return wrapper

class core:
    _client: agentapi
    _sessions: dict[str, list[ccmp]] = {}
    _current: str = ""
    _notice: str = ""
    _config: dict[str, Any] = {}

    @property
    def _memory(self) -> list[ccmp]:
        if self._current not in self._sessions:
            self._current = self._current or "会话1"
            self._sessions.setdefault(self._current, [])
        return self._sessions[self._current]

    @_memory.setter
    def _memory(self, chain: list[ccmp]) -> None:
        self._sessions[self._current] = chain

    _used: int | None = None
    _extra_index: int = 0
    _stopped: bool = False

    _dick: dick
    _memfile: dick

    def usage(self) -> tuple[int | None, int]:
        return self._used, self._config.get("context_limit", 128000)

    def stop(self) -> None:
        self._stopped = True
        self._client.stop()

    def stopped(self) -> bool:
        return self._stopped

    def peer_serve(self) -> bool:
        return bool(self._config.get("peer_serve", True))

    def sessions(self) -> list[str]:
        return list(self._sessions)

    def count(self, name: str) -> int:
        return len(self._sessions.get(name, []))

    def current(self) -> str:
        return self._current

    def notice(self) -> str:
        return self._notice

    def missing_settings(self) -> list[str]:
        return [
            key for key in ("apikey", "baseurl", "modelname")
            if _is_placeholder(self._dick.str(key))
        ]

    def configured(self) -> bool:
        return not self.missing_settings()

    def switch(self, name: str) -> None:
        if name == self._current or name not in self._sessions:
            return
        self._current = name
        self._used = None
        self._memfile.set("current", name)
        self._memfile.update()

    def _save_sessions(self) -> None:
        self._memfile.set("memory", self._sessions)
        self._memfile.set("current", self._current)
        self._memfile.update()

    def _save_config(self) -> None:
        for key in _CONFIG_KEYS:
            if key in self._config:
                self._dick.set(key, self._config[key])
        self._dick.pop("config")
        self._dick.update()

    def _migrate_dotted_keys(self) -> list[str]:
        """把 ``config.peer_serve`` 这类带点号的扁平键迁回 ``peer_serve``。

        ``dick`` 只按字面键取值，所以 ``"config.peer_serve": false`` 永远不会生效，
        表现出来就是“设置里把 peer 关了，peer 照样起在 0.0.0.0”。
        迁移时以扁平键为准：扁平键不存在（或为 null）才用点号键的值。
        """
        moved: list[str] = []
        for key in list(self._dick.items()):
            if not isinstance(key, str) or not key.startswith("config."):
                continue
            flat = key[len("config."):]
            if not flat or "." in flat:
                continue
            if self._dick.any(flat, None) is None:
                self._dick.set(flat, self._dick.any(key, None))
                moved.append(f"{key} → {flat}")
            self._dick.pop(key)
        if moved:
            self._dick.update()
        return moved

    def _load_config(self) -> dict[str, Any]:
        self._migrate_dotted_keys()
        legacy = self._dick.any("config", None)
        if isinstance(legacy, dict) and legacy:
            for key, value in legacy.items():
                self._dick.set(key, value)
            self._dick.pop("config")
            self._dick.update()
        return {key: self._dick.any(key, None) for key in _CONFIG_KEYS if self._dick.any(key, None) is not None}

    def flush(self) -> None:
        try:
            self._save_config()
        except Exception:
            pass
        try:
            self._save_sessions()
        except Exception:
            pass

    @_guarded
    async def new_session(self, name: str) -> None:
        name = name.strip()
        if not name:
            raise ValueError("会话名不能为空")
        if name in self._sessions:
            raise ValueError(f"会话 {name!r} 已存在")
        self._sessions[name] = []
        self._current = name
        self._used = None
        self._save_sessions()

    @_guarded
    async def delete_session(self, name: str) -> None:
        if name not in self._sessions:
            raise ValueError(f"会话 {name!r} 不存在")
        del self._sessions[name]
        if self._current == name:
            self._current = next(iter(self._sessions), "")
            self._used = None
        self._save_sessions()

    @_guarded
    async def compress(self) -> None:
        summary = cast(dict[str, Any], await self._compact_history(self._memory)).get("content") or ""
        self._memory = [cast(ccmp, {"role": "system", "content": f"以下是此前对话的摘要：\n{summary}"})]
        self._used = None

    def model(self) -> str:
        return self._client.model_name

    async def models(self) -> list[str]:
        try:
            return await self._client.models()
        except Exception:
            return [self.model()]

    @_guarded
    async def balance(self) -> str:
        info = (await self._client.balance())["balance_infos"][0]
        unit = {"CNY": "¥", "USD": "$"}.get(info["currency"], info["currency"] + " ")
        return f"{unit}{info['total_balance']}"

    def set_model(self, name: str) -> None:
        self._client.model_name = name
        self._dick.set("modelname", name)
        self._dick.update()

    def effort(self) -> str:
        return self._config.get("reasoning_effort", "medium")

    def set_effort(self, effort: str) -> None:
        self._config["reasoning_effort"] = effort
        self._save_config()

    def think_collapsed(self) -> bool:
        return bool(self._config.get("think_collapsed", True))

    def set_think_collapsed(self, collapsed: bool) -> None:
        self._config["think_collapsed"] = collapsed
        self._save_config()

    def extras(self) -> list[dict[str, Any]]:
        extra = self._config.get("extra", {"thinking": {"type": "enabled"}})
        extras = extra if isinstance(extra, list) else [extra]
        return extras or [{}]

    def extra(self) -> dict[str, Any]:
        return self.extras()[self.extra_index()]

    def extra_index(self) -> int:
        return self._extra_index % len(self.extras())

    def set_extra_index(self, index: int) -> None:
        self._extra_index = index % len(self.extras())

    def settings(self) -> list[tuple[str, Any]]:
        entries = {key: value for key, value in self._dick.items().items() if key not in _RESERVED_KEYS}
        ordered = [key for key in _SETTING_ORDER if key in entries]
        ordered += [key for key in entries if key not in _SETTING_ORDER]
        return [(key, self._setting_value(key, entries[key])) for key in ordered]

    def _setting_value(self, key: str, raw: Any) -> Any:
        if key == "modelname":
            return self.model()
        if key == "reasoning_effort":
            return self.effort()
        if key == "think_collapsed":
            return self.think_collapsed()
        if key == "context_limit":
            return self._config.get("context_limit", 128000)
        if key == "peer_serve":
            return self.peer_serve()
        if key == "peer_tools":
            return self._config.get("peer_tools", True)
        if key == "extra":
            return self.extras()
        return raw

    def _reconnect(self) -> None:
        name = self.model()
        self._client = _client_for(self._dick.str("apikey"), self._dick.str("baseurl"), name)

    @staticmethod
    def _parse_extra(raw: str) -> list[dict[str, Any]]:
        try:
            extra = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"extra 不是合法 JSON：{e}") from e
        if not isinstance(extra, list) or not all(isinstance(item, dict) for item in extra):
            raise ValueError('extra 需要 [dict, ...] 形式的 JSON，例如 [{"thinking": {"type": "enabled"}}]')
        return extra

    @staticmethod
    def _parse_bool(raw: str) -> bool:
        low = raw.strip().lower()
        if low in ("true", "1", "yes", "on", "开"):
            return True
        if low in ("false", "0", "no", "off", "关"):
            return False
        raise ValueError(f"开关键只认 true/false（1/0、yes/no、on/off、开/关也行），收到 {raw!r}")

    @staticmethod
    def _parse_value(raw: str) -> Any:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    @_guarded
    async def set_setting(self, path: str, raw: str) -> None:
        value = raw.strip()
        if not value and path in _SETTING_ORDER and path != "access_key":
            raise ValueError(f"{path} 不能为空")
        if path in _RESERVED_KEYS:
            raise ValueError(f"{path} 是保留键，不能通过设置面板修改")
        if path in ("apikey", "baseurl"):
            self._dick.set(path, value)
            self._reconnect()
            self._dick.update()
        elif path == "access_key":
            self._dick.set("access_key", value)
            self._dick.update()
        elif path == "modelname":
            self.set_model(value)
        elif path == "reasoning_effort":
            self.set_effort(value)
        elif path == "think_collapsed":
            self.set_think_collapsed(self._parse_bool(value))
        elif path == "context_limit":
            if not value.isdigit():
                raise ValueError(f"context_limit 需要正整数，收到 {value!r}")
            self._config["context_limit"] = int(value)
            self._save_config()
        elif path in ("peer_serve", "peer_tools"):
            self._config[path] = self._parse_bool(value)
            self._save_config()
        elif path == "peer_host":
            self._dick.set("peer_host", value)
            self._dick.update()
        elif path == "peer_port":
            if not value.isdigit() or not 0 < int(value) < 65536:
                raise ValueError(f"peer_port 需要 1-65535 的端口号，收到 {value!r}")
            self._dick.set("peer_port", int(value))
            self._dick.update()
        elif path == "extra":
            self._config["extra"] = self._parse_extra(value)
            self._extra_index = 0
            self._save_config()
        else:
            self._dick.set(path, self._parse_value(value))
            self._dick.update()

    def _load_sessions(self) -> None:
        memory = self._memfile.any("memory", {})
        current = self._memfile.str("current", "")
        legacy = self._dick.any("memory", None)
        if isinstance(legacy, dict) and legacy:
            fresh = {name: chain for name, chain in memory.items() if chain} if isinstance(memory, dict) else {}
            fresh.update(legacy)
            memory, current = fresh, self._dick.str("current", "") or current
            self._memfile.set("memory", memory)
            self._memfile.set("current", current)
            self._memfile.update()
            self._drop_legacy_memory()
        self._sessions = memory if isinstance(memory, dict) else {}
        if current in self._sessions:
            self._current = current
            return
        self._current = next(iter(self._sessions), "会话1")
        self._sessions.setdefault(self._current, [])
        if current:
            self._notice = f"memory 里没有会话 {current!r}，已按 {self._current!r} 继续"

    def _drop_legacy_memory(self) -> None:
        try:
            self._dick.pop("memory")
            self._dick.pop("current")
            self._dick.update()
        except Exception:
            pass

    def __init__(self, apikey: str, baseurl: str, modelname: str, id:str|None=None) -> None:
        here = Path(__file__).resolve().parent.parent / "configure"
        self._dick = config
        self._memfile = dick(str(here / "memory.json"))
        self._client = _client_for(apikey, baseurl, modelname)
        self._load_sessions()
        self._config = self._load_config()
        self._extra_index = self._dick.int("extra_index", 0)


    def _analyze_usrmsg(self, usrmsg: str) -> ccmp:
        return {"role":"user", "content":usrmsg}

    @_guarded
    async def stream(self, usrmsg: str) -> accsm:
        self._stopped = False
        self._client.resume()
        self._memory.append(self._analyze_usrmsg(usrmsg))
        return await self._client.chat_stream(
            usrmsg=self._memory,
            reasoning_effort=self._config.get("reasoning_effort", "medium"),
            extra=self.extra()
        )

    async def tool(self, toolmsg: ccmp, msgchain: list[ccmp] | None = None, rtime: int = 0) -> accsm:
        results = await asyncio.to_thread(self._client.tool, toolmsg)
        return await self._continue(toolmsg, results, msgchain)

    async def deny(self, toolmsg: ccmp, msgchain: list[ccmp] | None = None) -> accsm:
        return await self._continue(toolmsg, self._client.deny(toolmsg), msgchain)

    @_guarded
    async def _continue(self, toolmsg: ccmp, results: list[ccmp], msgchain: list[ccmp] | None = None) -> accsm:
        if msgchain is None:
            msgchain = self._memory.copy()
        msgchain.append(toolmsg)
        msgchain.extend(results)
        self._memory = msgchain
        return await self._client.chat_stream(
            usrmsg=self._memory,
            reasoning_effort=self._config.get("reasoning_effort", "medium"),
            extra=self.extra()
        )


    @_guarded
    async def update_screen(self,think:Static,context:Static,stream:accsm)->ccmp|None:
        async with stream as events:
            async for event in events:
                if event.type == "chunk":
                    think.update((event.snapshot.choices[0].message.model_extra or {}).get("reasoning_content", ""))
                elif event.type == "content.delta":
                    context.update(Markdown(event.snapshot))
            completion = await events.get_final_completion()
            message = completion.choices[0].message
        if completion.usage:
            self._used = completion.usage.total_tokens
        reply = _from_message(message)
        return reply if reply.get("tool_calls") else None

    @_guarded
    async def _compact_history(self, history:list[ccmp])->ccmp:
        extra: list[str] = []

        for sig in history:
            temp = sig.get("content")
            if isinstance(temp, list):
                for cont in temp: extra += f"{sig.get('role')}:{cont.get('type') if cont.get('type') != 'text' else cont.get('text')}"
            elif isinstance(temp, str): extra += f"{sig.get('role')}:{sig.get('content')}"

        exstr: str = ""
        for strs in extra:
            exstr += strs
        return await self._client.chat(cast(list[ccmp],[{"role":"system","content":"总结以下对话，压缩到300字左右，保留关键信息"},{"role":"user", "content":f"{exstr}"}]))