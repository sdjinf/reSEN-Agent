"""`tool_history` 元工具：长期/短期记忆库。

记忆以 Markdown 文件形式存放在项目根目录的 ``history/`` 下，按用途目录隔离：

- ``history/long/``  长期记忆：永久性的修改记录、结论、约定
- ``history/short/`` 短期记忆：临时抓取的大段冗杂数据，随用随取、用完即删

文件名就是关键词组或句子。写操作：add / mod / del；
读操作：search（按关键字检索正文与文件名）、find（按文件名检索）、read、list。
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .interface.meta_tool import meta_tool

REL_DIR = Path("history")
BUCKETS = ("long", "short")

_TYPE_ALIASES = {
    "long": "long",
    "长期": "long",
    "长期记忆": "long",
    "permanent": "long",
    "persistent": "long",
    "short": "short",
    "短期": "short",
    "短期记忆": "short",
    "temp": "short",
    "temporary": "short",
    "cache": "short",
}

_ACTION_ALIASES = {
    "add": "add",
    "create": "add",
    "新增": "add",
    "添加": "add",
    "写入": "add",
    "mod": "mod",
    "modify": "mod",
    "update": "mod",
    "修改": "mod",
    "更新": "mod",
    "del": "del",
    "delete": "del",
    "remove": "del",
    "删除": "del",
    "search": "search",
    "搜索": "search",
    "检索": "search",
    "find": "find",
    "filename": "find",
    "按文件名": "find",
    "文件名": "find",
    "read": "read",
    "get": "read",
    "cat": "read",
    "读取": "read",
    "查看": "read",
    "list": "list",
    "ls": "list",
    "列表": "list",
}

_INVALID_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_WHITESPACE = re.compile(r"\s+")
_MAX_NAME_LEN = 100


def _root() -> Path:
    env = os.environ.get("RESEN_ROOT")
    return Path(env).expanduser().resolve() if env else Path(__file__).resolve().parents[2]


def _bucket(value) -> str:
    key = str(value or "long").strip().lower()
    bucket = _TYPE_ALIASES.get(key)
    if bucket is None:
        raise ValueError(f"未知记忆类型 {value!r}：仅支持 long（长期）或 short（短期）")
    return bucket


def _clean_name(raw) -> str:
    name = str(raw or "").strip()
    name = _INVALID_CHARS.sub("_", name)
    if name.lower().endswith(".md"):
        name = name[:-3]
    name = _WHITESPACE.sub(" ", name).strip(" .")
    if not name or name in (".", ".."):
        raise ValueError("name（关键词组或句子）不能为空，也不能是 '.' 或 '..'")
    if len(name) > _MAX_NAME_LEN:
        name = name[:_MAX_NAME_LEN].rstrip(" .")
    return name


def _parse(path: Path) -> tuple[dict[str, str], str]:
    raw = path.read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    body = raw
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            for line in parts[1].splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                meta[key.strip()] = value.strip()
            body = parts[2].lstrip("\n")
    return meta, body


def _render(name: str, bucket: str, body: str, created: str | None = None) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (
        "---\n"
        f"name: {name}\n"
        f"type: {bucket}\n"
        f"created: {created or now}\n"
        f"updated: {now}\n"
        "---\n\n"
        + (body or "").rstrip()
        + "\n"
    )


def _iter_files(root: Path, bucket: str | None = None) -> Iterator[tuple[str, Path]]:
    for current in (bucket,) if bucket else BUCKETS:
        directory = root / REL_DIR / current
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            if path.is_file():
                yield current, path


def _display(root: Path, bucket: str, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return f"{REL_DIR.as_posix()}/{bucket}/{path.name}"


def _resolve(root: Path, bucket: str | None, raw_name) -> tuple[str, Path]:
    name = _clean_name(raw_name)
    files = list(_iter_files(root, bucket))
    low = name.lower()

    exact = [(b, p) for b, p in files if p.stem == name]
    if not exact:
        exact = [(b, p) for b, p in files if p.stem.lower() == low]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        where = ", ".join(f"{b}/{p.stem}" for b, p in exact)
        raise ValueError(f"名称 {name!r} 在多个目录重复：{where}；请指定 memory_type")

    partial = [(b, p) for b, p in files if low in p.stem.lower()]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise FileNotFoundError(f"未找到记忆：{name}")
    where = ", ".join(f"{b}/{p.stem}" for b, p in partial[:20])
    raise ValueError(f"名称 {name!r} 不唯一，候选：{where}；请使用更精确的 name 或指定 memory_type")


def _snippet(text: str, keyword: str, width: int = 120) -> str:
    flat = _WHITESPACE.sub(" ", text or "").strip()
    if not flat:
        return ""
    index = flat.lower().find(keyword.lower())
    if index < 0:
        return flat[:width] + ("…" if len(flat) > width else "")
    start = max(0, index - width // 3)
    end = min(len(flat), index + len(keyword) + width)
    fragment = flat[start:end]
    return ("…" if start > 0 else "") + fragment + ("…" if end < len(flat) else "")


def _dump(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


class tool_history(meta_tool):

    def __init__(self) -> None:
        self.schema += [{
            "type": "function",
            "function": {
                "name": "tool_history",
                "description": (
                    "记忆库管理工具：把记忆以 Markdown 文件存放在项目根目录 history/ 下，"
                    "文件名即关键词组或句子。history/long/ 存长期记忆（永久性修改记录、结论、约定），"
                    "history/short/ 存短期记忆（临时抓取的冗杂数据，用完即删）。"
                    "写操作：add 新增 / mod 修改 / del 删除；"
                    "读操作：search 按关键字检索（正文+文件名）/ find 按文件名检索 / read 读取全文 / list 列出。"
                    "写操作务必先确认 name 与 memory_type，避免误改长期记忆。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["add", "mod", "del", "search", "find", "read", "list"],
                            "description": "操作类型：add 新增 / mod 修改 / del 删除 / search 按关键字搜索 / find 按文件名搜索 / read 读取全文 / list 列出全部。",
                        },
                        "memory_type": {
                            "type": "string",
                            "enum": ["long", "short"],
                            "description": "记忆类型：long 长期记忆（默认）/ short 短期记忆。也可写 长期/短期。search/find/read/del/list 省略时同时查两类。",
                        },
                        "name": {
                            "type": "string",
                            "description": "记忆名称，即文件名（不含 .md），通常为关键词组或句子。add 必填；mod/del/read 用它定位，支持唯一子串匹配。",
                        },
                        "new_name": {
                            "type": "string",
                            "description": "mod 时重命名记忆文件（仍为不含 .md 的关键词组/句子）。",
                        },
                        "content": {
                            "type": "string",
                            "description": "记忆正文，Markdown（add 必填；mod 省略则保留原文）。",
                        },
                        "keyword": {
                            "type": "string",
                            "description": "search 在正文与文件名中检索的关键词；find 只匹配文件名。",
                        },
                        "append": {
                            "type": "boolean",
                            "description": "mod 时是否把 content 追加到原文末尾，默认 false（整体替换）。",
                        },
                        "overwrite": {
                            "type": "boolean",
                            "description": "add 时同名记忆已存在是否覆盖，默认 false。",
                        },
                    },
                    "required": ["action"],
                },
            },
        }]

    def run(self, *args, **kwargs):
        raw_action = str(kwargs.get("action") or "").strip().lower()
        action = _ACTION_ALIASES.get(raw_action, raw_action)
        if action not in ("add", "mod", "del", "search", "find", "read", "list"):
            raise ValueError(
                f"未知 action {kwargs.get('action')!r}：仅支持 add/mod/del/search/find/read/list"
            )

        root = _root()
        bucket = _bucket(kwargs["memory_type"]) if kwargs.get("memory_type") else None

        if action == "add":
            name = _clean_name(kwargs.get("name"))
            bucket = bucket or "long"
            if kwargs.get("content") is None:
                raise ValueError("add 需要 content")
            path = root / REL_DIR / bucket / f"{name}.md"
            if path.exists() and not kwargs.get("overwrite"):
                raise FileExistsError(
                    f"记忆已存在：{_display(root, bucket, path)}（如需覆盖请传 overwrite=true）"
                )
            created = _parse(path)[0].get("created") if path.exists() else None
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                _render(name, bucket, str(kwargs.get("content")), created),
                encoding="utf-8",
            )
            return _dump({
                "action": "add",
                "memory_type": bucket,
                "name": name,
                "path": _display(root, bucket, path),
                "overwritten": bool(created),
            })

        if action == "mod":
            current, path = _resolve(root, bucket, kwargs.get("name"))
            meta, body = _parse(path)
            has_content = kwargs.get("content") is not None
            new_name = _clean_name(kwargs["new_name"]) if kwargs.get("new_name") else path.stem
            if not has_content and new_name == path.stem:
                raise ValueError("mod 需要 content 或 new_name")

            if has_content:
                piece = str(kwargs.get("content"))
                if kwargs.get("append"):
                    body = (body.rstrip() + "\n\n" + piece.lstrip()).strip() + "\n"
                else:
                    body = piece

            target = path.with_name(f"{new_name}.md")
            if target != path and target.exists():
                raise FileExistsError(f"目标记忆已存在：{_display(root, current, target)}")

            target.write_text(
                _render(new_name, current, body, meta.get("created")),
                encoding="utf-8",
            )
            if target != path:
                path.unlink()
            return _dump({
                "action": "mod",
                "memory_type": current,
                "name": new_name,
                "path": _display(root, current, target),
                "renamed_from": path.stem if target != path else None,
                "appended": bool(has_content and kwargs.get("append")),
            })

        if action == "del":
            current, path = _resolve(root, bucket, kwargs.get("name"))
            path.unlink()
            return _dump({
                "action": "del",
                "memory_type": current,
                "name": path.stem,
                "deleted": _display(root, current, path),
            })

        if action == "read":
            current, path = _resolve(root, bucket, kwargs.get("name"))
            meta, body = _parse(path)
            return _dump({
                "memory_type": current,
                "name": path.stem,
                "path": _display(root, current, path),
                "meta": meta,
                "content": body,
            })

        if action == "list":
            items = []
            for current, path in _iter_files(root, bucket):
                meta, body = _parse(path)
                items.append({
                    "memory_type": current,
                    "name": path.stem,
                    "path": _display(root, current, path),
                    "updated": meta.get("updated", ""),
                    "chars": len(body),
                    "preview": _snippet(body, "", 80),
                })
            return _dump({"count": len(items), "memories": items})

        keyword = str(kwargs.get("keyword") or "").strip()
        if not keyword:
            raise ValueError(f"{action} 需要 keyword")

        low = keyword.lower()
        needle = (
            lambda path: low in path.stem.lower()
            if action == "find"
            else (low in path.stem.lower() or low in _parse(path)[1].lower())
        )

        hits = []
        for current, path in _iter_files(root, bucket):
            if not needle(path):
                continue
            meta, body = _parse(path)
            in_name = low in path.stem.lower()
            in_body = low in body.lower()
            hits.append({
                "memory_type": current,
                "name": path.stem,
                "path": _display(root, current, path),
                "updated": meta.get("updated", ""),
                "matched_in": [k for k, ok in (("name", in_name), ("content", in_body)) if ok],
                "snippet": _snippet(body, keyword) if in_body else _snippet(path.stem, keyword, 60),
            })

        hits.sort(key=lambda h: ("name" not in h["matched_in"], h["memory_type"] != "long", h["name"]))
        return _dump({"action": action, "keyword": keyword, "count": len(hits), "results": hits})


tool = tool_history()
