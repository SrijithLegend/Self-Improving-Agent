"""Agent-made tools, as data only (G10: no code execution).

A macro is either
  kind="prompt":   one model call with a filled-in template, its answer checked against output_schema, or
  kind="pipeline": up to 5 built-in tools run in order, with string templating in their arguments.

Templating replaces {field} placeholders with plain text and nothing else. It is a regex substitution, never
str.format, so "{0.__class__}" or "__import__('os')" in a template are just characters. Pipelines have no loops and
no conditionals, and can only call built-in tools, never other macros.
"""
import json
import re

from sia_devtools.errors import ValidationError

NAME_RX = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
FIELD_RX = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]{0,40})\}")
MAX_STEPS, MAX_TEMPLATE, MAX_SCHEMA = 5, 4000, 2000
TYPES = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "boolean": bool,
         "null": type(None)}
SCHEMA_KEYS = {"type", "properties", "required", "enum", "items", "maxLength", "minLength", "minimum", "maximum",
               "additionalProperties", "description"}


# --- a small JSON-schema subset, enough for tool inputs, outputs and answer validators ----------------------------
def check_schema_shape(schema, depth=0):
    if depth > 6 or not isinstance(schema, dict) or set(schema) - SCHEMA_KEYS:
        raise ValidationError(f"schemas may use only: {', '.join(sorted(SCHEMA_KEYS))} (and nest at most 6 deep)")
    if "type" in schema and schema["type"] not in TYPES:
        raise ValidationError(f"schema type must be one of {', '.join(TYPES)}")
    for sub in (schema.get("properties") or {}).values():
        check_schema_shape(sub, depth + 1)
    if "items" in schema:
        check_schema_shape(schema["items"], depth + 1)
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        raise ValidationError("schema enum must be a non-empty list")


def schema_errors(value, schema, path="$"):
    """Why `value` doesn't match `schema` (a list, empty if it matches)."""
    errs, t = [], schema.get("type")
    if t:
        ok = isinstance(value, TYPES[t]) and not (t in ("integer", "number") and isinstance(value, bool))
        if not ok:
            return [f"{path} should be {t}"]
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path} should be one of {schema['enum']}")
    if isinstance(value, str):
        if len(value) > schema.get("maxLength", 1e18) or len(value) < schema.get("minLength", 0):
            errs.append(f"{path} has the wrong length")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < schema.get("minimum", -1e300) or value > schema.get("maximum", 1e300):
            errs.append(f"{path} is out of range")
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        errs += [f"{path}.{k} is required" for k in schema.get("required", []) if k not in value]
        if schema.get("additionalProperties") is False:
            errs += [f"{path}.{k} is not allowed" for k in value if k not in props]
        for k, sub in props.items():
            if k in value:
                errs += schema_errors(value[k], sub, f"{path}.{k}")
    if isinstance(value, list) and "items" in schema:
        for i, x in enumerate(value):
            errs += schema_errors(x, schema["items"], f"{path}[{i}]")
    return errs


# --- validation ------------------------------------------------------------------------------------------------
def _schema(v, what):
    if not isinstance(v, dict) or len(json.dumps(v)) > MAX_SCHEMA:
        raise ValidationError(f"{what} must be an object of at most {MAX_SCHEMA} characters")
    check_schema_shape(v)
    return v


def validate_macro(m, locked=(), enabled_tools=()):
    """The macro, checked; raises ValidationError. Prompt templates must keep every locked_text line (G4)."""
    if not isinstance(m, dict) or not isinstance(m.get("name"), str) or not NAME_RX.match(m["name"]):
        raise ValidationError("a macro needs a name: lowercase letters, digits and '_', starting with a letter")
    what = f"macro {m['name']}"
    desc = m.get("description", "")
    if not isinstance(desc, str) or len(desc) > 500:
        raise ValidationError(f"{what}: description must be a string of at most 500 characters")
    if m.get("kind") == "prompt":
        if set(m) - {"name", "description", "kind", "input_schema", "template", "output_schema"}:
            raise ValidationError(f"{what}: a prompt macro has name, description, kind, input_schema, template, "
                                  "output_schema")
        tpl = m.get("template")
        if not isinstance(tpl, str) or not tpl.strip() or len(tpl) > MAX_TEMPLATE:
            raise ValidationError(f"{what}: template must be 1-{MAX_TEMPLATE} characters")
        if miss := [t for t in locked if t not in tpl]:
            raise ValidationError(f"{what}: template drops locked text required by project settings: {miss}")
        inp = _schema(m.get("input_schema", {"type": "object", "properties": {}}), f"{what}: input_schema")
        if inp.get("type") != "object":
            raise ValidationError(f"{what}: input_schema must have type object")
        unknown = set(FIELD_RX.findall(tpl)) - set(inp.get("properties") or {})
        if unknown:
            raise ValidationError(f"{what}: template uses fields not in input_schema: {sorted(unknown)}")
        return {"name": m["name"], "description": desc, "kind": "prompt", "input_schema": inp, "template": tpl,
                "output_schema": _schema(m.get("output_schema", {"type": "string"}), f"{what}: output_schema")}
    if m.get("kind") == "pipeline":
        if set(m) - {"name", "description", "kind", "steps"}:
            raise ValidationError(f"{what}: a pipeline macro has name, description, kind, steps")
        steps = m.get("steps")
        if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
            raise ValidationError(f"{what}: steps must list 1-{MAX_STEPS} tool calls")
        out = []
        for i, s in enumerate(steps):
            if not isinstance(s, dict) or set(s) - {"tool", "args"} or s.get("tool") not in enabled_tools:
                raise ValidationError(f"{what}: step {i} must call an enabled built-in tool ({sorted(enabled_tools)})")
            args = s.get("args") or {}
            if not isinstance(args, dict) or len(args) > 10 or not all(
                    isinstance(k, str) and isinstance(v, (str, int, float, bool)) and len(str(v)) <= 1000
                    for k, v in args.items()):
                raise ValidationError(f"{what}: step {i} args must be up to 10 plain values")
            out.append({"tool": s["tool"], "args": args})
        return {"name": m["name"], "description": desc, "kind": "pipeline", "steps": out}
    raise ValidationError(f"{what}: kind must be 'prompt' or 'pipeline'")


# --- running ---------------------------------------------------------------------------------------------------
def fill(template, values):
    """Replace {field} with the value's text. Nothing is evaluated; unknown fields stay as written."""
    return FIELD_RX.sub(lambda mt: str(values[mt.group(1)]) if mt.group(1) in values else mt.group(0), template)


def spec(m):
    """How the actor sees the macro in its tool list."""
    if m["kind"] == "prompt":
        return {"name": m["name"], "description": m["description"], "args": m["input_schema"]}
    fields = sorted({f for s in m["steps"] for v in s["args"].values() if isinstance(v, str) for f in
                     FIELD_RX.findall(v)})
    return {"name": m["name"], "description": m["description"] or "pipeline of " + ", ".join(
        s["tool"] for s in m["steps"]), "args": {"type": "object", "properties": {f: {"type": "string"}
                                                                                  for f in fields}}}


def run_macro(m, args, llm, call_builtin):
    """Run a validated macro. call_builtin(tool, args) runs one built-in tool and returns its text."""
    if not isinstance(args, dict):
        return "error: tool arguments must be an object"
    if m["kind"] == "prompt":
        if errs := schema_errors(args, m["input_schema"]):
            return "error: " + "; ".join(errs[:5])
        out = llm.json("You are a precise sub-tool. Follow the instruction exactly.",
                       fill(m["template"], args) + '\n\nReturn {"result": <your answer>}', temperature=0)
        result = out.get("result") if isinstance(out, dict) else None
        if errs := schema_errors(result, m["output_schema"]):
            return "error: the sub-tool's answer didn't match its output schema: " + "; ".join(errs[:5])
        return result if isinstance(result, str) else json.dumps(result)
    values = {k: str(v) for k, v in args.items()}
    results = []
    for s in m["steps"]:
        step_args = {k: fill(v, values) if isinstance(v, str) else v for k, v in s["args"].items()}
        results.append(f"[{s['tool']}] {call_builtin(s['tool'], step_args)}")
    return "\n".join(results)
