import importlib.util

from . import TOOLS_DIR
from .interface.hotplug_tool import hotplug_tool
from .interface.meta_tool import meta_tool


def _load_tool(name: str):
    module_path = TOOLS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"tools_{name}", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for obj in vars(module).values():
        if isinstance(obj, type) and issubclass(obj, hotplug_tool) and obj is not hotplug_tool:
            return obj()
    raise TypeError(f"No hotplug_tool subclass found in: {module_path}")


class tool_search(meta_tool):

    def __init__(self) -> None:
        self.schema += [{
            "type": "function",
            "function": {
                "name": "tool_search",
                "description": (
                    "List or search hot-pluggable tool modules under the tools/ directory. "
                    "Without a keyword all tools are returned; with a keyword only tools "
                    "whose name or description contains it are returned."
                    "There might be a lot of tools, so you shouldn't use empty searches unless necessary."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keyword": {
                            "type": "string",
                            "description": "Optional substring to match against tool name or description.",
                        },
                    },
                    "required": [],
                },
            },
        }]

    def run(self, *args, **kwargs) -> dict:
        keyword = kwargs.get("keyword") or ""
        results = []
        for module_path in sorted(TOOLS_DIR.glob("*.py")):
            if module_path.name.startswith("_"):
                continue
            name = module_path.stem
            try:
                schema = _load_tool(name).get_schema()
            except Exception:
                continue
            fn = schema.get("function", {})
            tool_name = fn.get("name", name)
            description = fn.get("description", "")
            if keyword and keyword not in f"{tool_name} {description}":
                continue
            results.append({
                "name": tool_name,
                "description": description,
                "parameters": fn.get("parameters", {}),
            })
        return {
            "keyword": keyword,
            "count": len(results),
            "tools": results,
        }

tool = tool_search()