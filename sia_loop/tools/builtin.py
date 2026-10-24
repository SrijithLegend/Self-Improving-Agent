"""Built-in tools. Each takes plain arguments and returns text for the model. None of them runs code it was given.

search_knowledge  FTS search over the project's documents (sanitised with fts_query), scoped to the project
calculator        arithmetic over a whitelisted AST: numbers, + - * / // % **, parentheses, round, min, max
lookup_table      exact-match lookup in a table the operator uploaded (read-only)
http_get          off by default; GET to https hosts in settings.loop.http_allowlist, public addresses only
"""
import ast
import ipaddress
import json
import operator
import socket
from urllib.parse import urlsplit

import httpx

MAX_EXPR, MAX_POW, MAX_INT = 200, 100, 10 ** 400
HTTP_TIMEOUT, HTTP_MAX_BYTES = 5.0, 100_000
_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {"round": round, "min": min, "max": max}


class ToolError(Exception):
    """A tool refused its input; the message goes back to the model."""


def calculator(expression):
    if not isinstance(expression, str) or not expression.strip() or len(expression) > MAX_EXPR:
        raise ToolError(f"expression must be 1-{MAX_EXPR} characters")
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError:
        raise ToolError("not an arithmetic expression")

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and type(n.value) in (int, float):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _BIN:
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Pow) and abs(b) > MAX_POW:
                raise ToolError(f"exponents above {MAX_POW} are not allowed")
            try:
                out = _BIN[type(n.op)](a, b)
            except (ZeroDivisionError, OverflowError) as e:
                raise ToolError(str(e))
            if isinstance(out, complex) or (isinstance(out, int) and abs(out) > MAX_INT):
                raise ToolError("result out of range")
            return out
        if isinstance(n, ast.UnaryOp) and type(n.op) in _UNARY:
            return _UNARY[type(n.op)](ev(n.operand))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUNCS and not n.keywords \
                and 0 < len(n.args) <= 10:
            return _FUNCS[n.func.id](*[ev(a) for a in n.args])
        raise ToolError("only numbers, + - * / // % **, parentheses, round, min and max are allowed")
    result = ev(tree)
    return str(round(result, 10) if isinstance(result, float) else result)


def search_knowledge(search, query, k=3, max_chars=1000):
    """search(query, k) -> [{"text", "title"}], already scoped to one project."""
    if not isinstance(query, str) or not query.strip():
        raise ToolError("query must be a non-empty string")
    k = max(1, min(int(k) if isinstance(k, (int, float)) else 3, 10))
    hits = search(query[:1000], k)
    if not hits:
        return "no matching documents"
    return "\n\n".join(f"[{i}] ({h['title']}) {h['text'][:max_chars]}" for i, h in enumerate(hits, 1))


def lookup_table(get_table, table, where):
    """get_table(name) -> list of row dicts, or None. Rows whose fields equal every value in `where`."""
    if not isinstance(table, str) or not isinstance(where, dict) or not where:
        raise ToolError('call it as {"table": "<name>", "where": {"column": "value"}}')
    rows = get_table(table)
    if rows is None:
        raise ToolError(f"no table named {table!r}")
    want = {str(k): str(v).strip().casefold() for k, v in where.items()}
    hits = [r for r in rows if all(str(r.get(k, "")).strip().casefold() == v for k, v in want.items())]
    return json.dumps(hits[:20], ensure_ascii=False) + (f" (first 20 of {len(hits)})" if len(hits) > 20 else "")


def public_address(host):
    """True if every address the host resolves to is a public one (no loopback, private, link-local, ...)."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False
    addrs = {ipaddress.ip_address(i[4][0]) for i in infos}
    return bool(addrs) and all(a.is_global and not a.is_multicast for a in addrs)


def http_get(url, allowlist, client=None, resolve=public_address):
    """GET an https URL whose host is on the allowlist and resolves to public addresses only. No redirects (they
    could lead anywhere), 5 s timeout, at most 100 KB of the body."""
    if not isinstance(url, str) or len(url) > 2000:
        raise ToolError("url must be a string")
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host or parts.username or parts.password or parts.port not in (None, 443):
        raise ToolError("only https:// URLs on the default port are allowed")
    if host not in allowlist:
        raise ToolError(f"{host} is not on this project's http_allowlist")
    # ponytail: the address is checked before the request, not pinned for it, so DNS rebinding between the two is
    # possible; pin the resolved IP in a custom transport if http_get is ever enabled for untrusted hosts.
    if not resolve(host):
        raise ToolError(f"{host} does not resolve to a public address")
    client = client or httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=False)
    try:
        with client.stream("GET", url, headers={"User-Agent": "sia-agent"}) as r:
            if 300 <= r.status_code < 400:
                raise ToolError(f"redirects are not followed ({r.status_code})")
            body = b""
            for chunk in r.iter_bytes():
                body += chunk
                if len(body) >= HTTP_MAX_BYTES:
                    break
    except httpx.HTTPError as e:
        raise ToolError(f"request failed: {type(e).__name__}")
    text = body[:HTTP_MAX_BYTES].decode("utf-8", errors="replace")
    return f"HTTP {r.status_code}\n{text}"
