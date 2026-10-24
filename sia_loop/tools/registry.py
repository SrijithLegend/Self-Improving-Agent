"""One place the actor calls tools through: the enabled built-ins plus the config's declarative macros.

There is no exec, eval, subprocess or import of anything the agent wrote (G10). An unknown or disabled tool, bad
arguments, or a tool's own refusal all come back to the model as an "error: ..." string, never as an exception.
"""
import json

from sia.llm import LLMError

from . import builtin, macro

SPECS = {
    "search_knowledge": {"description": "Search the reference documents.",
                         "args": {"type": "object", "properties": {"query": {"type": "string"},
                                                                   "k": {"type": "integer"}}, "required": ["query"]}},
    "calculator": {"description": "Exact arithmetic: numbers, + - * / // % **, parentheses, round, min, max.",
                   "args": {"type": "object", "properties": {"expression": {"type": "string"}},
                            "required": ["expression"]}},
    "lookup_table": {"description": "Exact-match lookup in an uploaded table.",
                     "args": {"type": "object", "properties": {"table": {"type": "string"}, "where": {
                         "type": "object"}}, "required": ["table", "where"]}},
    "http_get": {"description": "GET an https URL on the project's allowlist.",
                 "args": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
}


class Registry:
    def __init__(self, search=None, get_table=None, http_allowlist=(), llm=None, http_client=None,
                 resolve=builtin.public_address):
        """search(query, k) and get_table(name) are already scoped to one project."""
        self.search, self.get_table, self.llm = search or (lambda q, k: []), get_table or (lambda n: None), llm
        self.http_allowlist, self.http_client, self.resolve = set(http_allowlist), http_client, resolve

    def specs(self, cfg):
        """The tools the model may call with this config, as shown in its system prompt."""
        out = [{"name": n, **SPECS[n]} for n in cfg["tools"]["enabled"]]
        return out + [macro.spec(m) for m in cfg["tools"]["macros"]]

    def call(self, name, args, cfg):
        enabled, macros = cfg["tools"]["enabled"], {m["name"]: m for m in cfg["tools"]["macros"]}
        if not isinstance(args, dict):
            return "error: tool arguments must be a JSON object"
        try:
            if name in macros:
                return macro.run_macro(macros[name], args, self.llm, lambda t, a: self._builtin(t, a, cfg))
            if name in enabled:
                return self._builtin(name, args, cfg)
        except LLMError as e:
            return f"error: the tool's model call failed: {str(e)[:200]}"
        available = ", ".join([*enabled, *macros]) or "none"
        return f"error: unknown or disabled tool {name!r}. Available tools: {available}"

    def _builtin(self, name, args, cfg):
        if name not in cfg["tools"]["enabled"]:
            return f"error: tool {name!r} is disabled"
        try:
            if name == "calculator":
                return builtin.calculator(args.get("expression"))
            if name == "search_knowledge":
                return builtin.search_knowledge(self.search, args.get("query"), args.get("k", 3),
                                                cfg["retrieval"]["max_chars_per_chunk"])
            if name == "lookup_table":
                return builtin.lookup_table(self.get_table, args.get("table"), args.get("where"))
            if name == "http_get":
                return builtin.http_get(args.get("url"), self.http_allowlist, self.http_client, self.resolve)
        except builtin.ToolError as e:
            return f"error: {e}"
        except (TypeError, ValueError) as e:
            return f"error: bad arguments ({type(e).__name__}): {json.dumps(args)[:200]}"
        return f"error: unknown tool {name!r}"
