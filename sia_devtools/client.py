"""SIAClient: record runs, collect feedback, build datasets, evaluate, compare and release prompt versions.

Two modes with the same API and the same exceptions:
- hosted: `SIAClient(api_key=...)` talks to a SIA server over HTTPS. Runs are sent in the background in batches.
- local:  `SIAClient()` with no key writes to a SQLite file (default sia_local.db). No account or network needed.
Nothing touches the network at import time or in the constructor.
"""
import atexit
import collections
import contextvars
import dataclasses
import functools
import hashlib
import inspect
import json
import logging
import os
import sys
import threading
import time
import uuid
from collections import Counter
from statistics import mean
from typing import Any, Callable, Iterable

import httpx

from .errors import BY_STATUS, NotFound, RateLimited, ServerError, SIAError, ValidationError
from .scoring import Scorer, as_text, resolve
from .store import DEFAULT_REDACT, LOCAL_ORG, MAX_FIELD, DevStore, redactor

__version__ = "0.1.0"
log = logging.getLogger("sia_devtools")
DEFAULT_URL = "http://127.0.0.1:8000"
_current = contextvars.ContextVar("sia_current_run", default=None)
_in_eval = contextvars.ContextVar("sia_in_eval", default=False)


def _jsonable(x):
    """A JSON-safe copy of user values: dataclasses, pydantic models, sets and bytes are converted, the rest repr'd."""
    def default(o):
        if dataclasses.is_dataclass(o) and not isinstance(o, type):
            return dataclasses.asdict(o)
        if hasattr(o, "model_dump"):
            return o.model_dump()
        if isinstance(o, (set, frozenset)):
            return list(o)
        if isinstance(o, bytes):
            return f"<{len(o)} bytes>"
        return repr(o)[:1000]
    return json.loads(json.dumps(x, default=default))


class Prompt(str):
    """Prompt text that remembers its version (None = your fallback default). Pass it to start_run(version=...)."""

    def __new__(cls, text, version=None, name="system_prompt"):
        s = super().__new__(cls, text)
        s.version, s.name = version, name
        return s


class _Http:
    def __init__(self, base_url, api_key, timeout, max_retries):
        self.base, self.retries = base_url.rstrip("/"), max_retries
        self.client = httpx.Client(timeout=timeout, headers={"Authorization": f"Bearer {api_key}",
                                                             "User-Agent": f"sia-devtools/{__version__}"})

    def request(self, method, path, body=None, params=None, raw=False):
        """Bounded retries with backoff on network errors, 429 and 5xx. Errors never include the API key."""
        params = {k: v for k, v in (params or {}).items() if v is not None}
        for attempt in range(self.retries + 1):
            wait = None
            try:
                r = self.client.request(method, f"{self.base}/v1{path}", json=body, params=params)
            except httpx.HTTPError as e:
                err = ServerError(f"Could not reach SIA at {self.base} ({type(e).__name__}).")
            else:
                if r.status_code < 400:
                    return r.text if raw else (r.json() if r.content else None)
                try:
                    detail = r.json().get("detail", r.text)
                except ValueError:
                    detail = r.text
                cls = BY_STATUS.get(r.status_code) or (ServerError if r.status_code >= 500 else SIAError)
                err = cls(f"{r.status_code}: {str(detail)[:500]}")
                if r.status_code not in (429, 500, 502, 503, 504):
                    raise err
                wait = r.headers.get("retry-after")
            if attempt == self.retries:
                raise err
            time.sleep(min(float(wait) if wait and wait.isdigit() else 0.5 * 2 ** attempt, 10))


class Run:
    """One traced execution. Use it as a context manager, or call end() yourself.

    Inside `with run:` (or a @trace function) new runs become its children automatically.
    """

    def __init__(self, client, name, input=None, *, tags=None, metadata=None, parent_id=None, version=None,
                 version_name=None):
        self._client = client
        self.id = uuid.uuid4().hex
        self.name, self.input, self.parent_id = name, input, parent_id
        self.tags, self.metadata = list(tags or []), dict(metadata or {})
        self.version, self.version_name = version, version_name
        self.output = self.error = self.model = self.provider = None
        self.prompt_tokens = self.completion_tokens = self.cost = None
        self.scores: dict[str, float] = {}
        self.status, self.started, self.ended, self.duration_ms = "running", time.time(), None, None
        self._t0, self._token = time.perf_counter(), None

    def log_output(self, output: Any) -> "Run":
        self.output = output
        return self

    def log_error(self, error: "str | BaseException") -> "Run":
        self.error = error if isinstance(error, str) else f"{type(error).__name__}: {error}"
        return self

    def log_score(self, name: str, value: float) -> "Run":
        self.scores[name] = float(value)
        return self

    def set_usage(self, *, model: str | None = None, provider: str | None = None, prompt_tokens: int | None = None,
                  completion_tokens: int | None = None, cost: float | None = None) -> "Run":
        for k, v in dict(model=model, provider=provider, prompt_tokens=prompt_tokens,
                         completion_tokens=completion_tokens, cost=cost).items():
            if v is not None:
                setattr(self, k, v)
        return self

    def child(self, name: str, input: Any = None, **kw) -> "Run":
        return self._client.start_run(name, input, parent=self, **kw)

    def end(self, status: str | None = None) -> "Run":
        if self.ended is not None:
            return self
        status = status or ("error" if self.error else "success")
        if status not in ("success", "error"):
            raise ValueError("status must be 'success' or 'error'")
        self.status, self.ended = status, time.time()
        self.duration_ms = (time.perf_counter() - self._t0) * 1000
        self._client._enqueue(self._record())
        return self

    def _record(self):
        c = self._client
        return {"id": self.id, "parent_id": self.parent_id, "name": str(self.name)[:200], "input": c._prep(self.input),
                "output": c._prep(self.output), "status": self.status,
                "error": c._scrub(self.error)[:10_000] if self.error else None, "started": self.started,
                "ended": self.ended, "duration_ms": self.duration_ms, "model": self.model, "provider": self.provider,
                "prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens, "cost": self.cost,
                "tags": [str(t)[:64] for t in self.tags][:20], "metadata": c._prep(self.metadata) or {},
                "scores": self.scores, "version_name": self.version_name, "version": self.version}

    def __enter__(self):
        self._token = _current.set(self)
        return self

    def __exit__(self, exc_type, exc, tb):
        _current.reset(self._token)
        if exc is not None:
            self.log_error(exc).end("error")
        else:
            self.end()
        return False  # never swallow the application's exception


class Dataset:
    """Handle to a versioned dataset. Every add, edit or delete creates a new immutable version."""

    def __init__(self, client, row):
        self._c, self.id, self.name, self.version = client, row["id"], row["name"], row["version"]

    def __repr__(self):
        return f"Dataset(name={self.name!r}, version={self.version})"

    def _path(self, rest=""):
        return self._c._p(f"/datasets/{self.id}{rest}")

    def add_example(self, input: Any, expected_output: Any = None, *, metadata: dict | None = None,
                    critical: bool = False, allow_duplicate: bool = False) -> int:
        """Returns the example's id. An identical (input, expected_output) already in the dataset is not added
        again (its existing id is returned) unless allow_duplicate=True."""
        return self.add_examples([{"input": input, "expected_output": expected_output, "metadata": metadata,
                                   "critical": critical}], allow_duplicates=allow_duplicate)[0]

    def add_examples(self, examples: Iterable[dict], *, allow_duplicates: bool = False) -> list[int]:
        """Add examples as one new dataset version; identical (input, expected_output) pairs already in the dataset
        or repeated in this batch are skipped (their existing ids are returned), so re-running a script is safe."""
        c = self._c
        items = [{"input": c._prep(e.get("input")), "expected_output": c._prep(e.get("expected_output")),
                  "metadata": e.get("metadata") or {}, "critical": bool(e.get("critical"))} for e in examples]
        out = c._do(lambda: c._store.add_examples(LOCAL_ORG, c.project_id, self.id, items, allow_duplicates),
                    "POST", self._path("/examples"), {"examples": items, "allow_duplicates": allow_duplicates})
        if out.get("skipped"):
            print(f"SIA: dataset {self.name}: skipped {out['skipped']} duplicate example(s) already in it "
                  "(pass allow_duplicate(s)=True to add them anyway)", file=sys.stderr)
        self.refresh()
        return out["ids"]

    def update_example(self, example_id: int, *, input: Any = None, expected_output: Any = None,
                       metadata: dict | None = None, critical: bool | None = None) -> int:
        """Replace an example. Returns the new example id; the old one stays in earlier versions."""
        c = self._c
        fields = {k: v for k, v in dict(input=c._prep(input), expected_output=c._prep(expected_output),
                                        metadata=metadata, critical=critical).items() if v is not None}
        out = c._do(lambda: {"id": c._store.update_example(LOCAL_ORG, c.project_id, self.id, example_id, fields)},
                    "PUT", self._path(f"/examples/{example_id}"), fields)
        self.refresh()
        return out["id"]

    def delete_example(self, example_id: int) -> None:
        c = self._c
        c._do(lambda: c._store.delete_example(LOCAL_ORG, c.project_id, self.id, example_id),
              "DELETE", self._path(f"/examples/{example_id}"))
        self.refresh()

    def snapshot(self, version: int | None = None, split: str | None = None,
                 include_pending: bool = False) -> dict:
        """The examples as of `version`. split='dev' or 'holdout' keeps one split (None: both). Pending examples
        are never evaluated; include_pending lists them too."""
        c = self._c
        return c._do(lambda: c._store.dataset(LOCAL_ORG, c.project_id, self.id, version, split, include_pending),
                     "GET", self._path(), params={"version": version, "split": split,
                                                  "include_pending": include_pending or None})

    def examples(self, version: int | None = None, split: str | None = None) -> list[dict]:
        return self.snapshot(version, split)["examples"]

    def review_example(self, example_id: int, status: str) -> int | None:
        """Verify or reject a pending example. Verifying returns the id of its verified copy."""
        c = self._c
        out = c._do(lambda: {"new_id": c._store.review_example(LOCAL_ORG, c.project_id, self.id, example_id, status)},
                    "POST", self._path(f"/examples/{example_id}/{'verify' if status == 'verified' else 'reject'}"))
        self.refresh()
        return out["new_id"]

    def history(self) -> list[dict]:
        c = self._c
        return c._do(lambda: c._store.dataset_history(LOCAL_ORG, c.project_id, self.id), "GET",
                     self._path("/history"))

    def refresh(self) -> "Dataset":
        self.version = self.snapshot()["version"]
        return self


class SIAClient:
    def __init__(self, api_key: str | None = None, project: str = "default", *, base_url: str | None = None,
                 local: str | None = None, timeout: float = 10.0, max_retries: int = 3,
                 redact: list[str] | None = None, batch_size: int = 50, flush_interval: float = 1.0,
                 max_buffer: int = 10_000):
        """api_key (or SIA_API_KEY) selects hosted mode; without one, runs go to the local SQLite file `local`
        (or SIA_LOCAL_DB, default sia_local.db). `redact` lists rules applied before anything leaves your app
        (default: secrets and card numbers; see sia_devtools.store.REDACTORS)."""
        api_key = os.getenv("SIA_API_KEY") if api_key is None else api_key
        self.mode = "hosted" if api_key else "local"
        self.project = project
        self._local_path = local or os.getenv("SIA_LOCAL_DB") or "sia_local.db"
        self._http = _Http(base_url or os.getenv("SIA_BASE_URL") or DEFAULT_URL, api_key, timeout,
                           max_retries) if api_key else None
        self._db, self._pid = None, None
        self._scrub = redactor(DEFAULT_REDACT if redact is None else redact)
        self.batch_size, self.flush_interval, self.max_buffer = batch_size, flush_interval, max_buffer
        self._buffer, self._lock, self._send_lock = collections.deque(), threading.Lock(), threading.Lock()
        self._wake, self._thread = threading.Event(), None
        self.dropped, self.last_error = 0, None
        self._prompts: dict[str, tuple[float, Prompt | None]] = {}

    def __repr__(self):
        return f"SIAClient(mode={self.mode!r}, project={self.project!r})"  # never shows the key

    # --- plumbing -------------------------------------------------------------
    @property
    def _store(self) -> DevStore:
        if self._db is None:
            self._db = DevStore(self._local_path)
        return self._db

    def _do(self, local: Callable[[], Any], method: str, path: str, body=None, params=None, raw=False):
        if self.mode == "local":
            return local()
        return self._http.request(method, path, body, params, raw)

    @property
    def project_id(self) -> str:
        if self._pid is None:
            self._pid = self._do(lambda: self._store.create_project(LOCAL_ORG, self.project),
                                 "POST", "/projects", {"name": self.project})["id"]
        return self._pid

    def _p(self, rest: str) -> str:
        return f"/projects/{self.project_id}{rest}"

    def _prep(self, x):
        """JSON-safe, redacted and size-bounded copy of a payload, before it leaves the application."""
        if x is None:
            return None
        x = self._scrub(_jsonable(x))
        s = json.dumps(x)
        return {"_truncated": True, "original_chars": len(s), "preview": s[:MAX_FIELD]} if len(s) > MAX_FIELD else x

    def _report(self, err, n, dropped=False):
        self.last_error = str(err)
        log.warning("SIA: %s %d run(s): %s", "dropped" if dropped else "could not send (will retry)", n, err)

    # --- runs -----------------------------------------------------------------
    def start_run(self, name: str, input: Any = None, *, tags: list[str] | None = None,
                  metadata: dict | None = None, parent: "Run | str | None" = None,
                  version: "Prompt | int | None" = None) -> Run:
        """Start a run. `parent` defaults to the run currently open in this context. `version` (a Prompt from
        get_prompt, or a version number) records which prompt version produced it."""
        parent = _current.get() if parent is None else parent
        if isinstance(version, Prompt):
            vnum, vname = version.version, version.name
        elif isinstance(version, int) and not isinstance(version, bool):
            vnum, vname = version, "system_prompt"
        elif version is not None:
            raise TypeError("version must be a Prompt (from get_prompt) or a version number")
        elif isinstance(parent, Run):
            vnum, vname = parent.version, parent.version_name
        else:
            vnum = vname = None
        tags = list(tags or []) + (["eval"] if _in_eval.get() else [])
        return Run(self, name, input, tags=tags, metadata=metadata,
                   parent_id=parent.id if isinstance(parent, Run) else parent, version=vnum,
                   version_name=vname if vnum is not None else None)

    def trace(self, name: "str | Callable | None" = None, *, tags: list[str] | None = None,
              capture_input: bool = True, capture_output: bool = True):
        """Decorator: record each call of a function as a run (arguments in, return value out, exceptions as
        errors, which are re-raised unchanged). Works bare (@sia.trace) or with options (@sia.trace("name")), on
        regular and async functions. Recording never waits on the network: hosted mode sends from a background
        thread, so an async app's event loop isn't blocked (local mode writes one SQLite row when the run ends)."""
        def deco(fn):
            sig = inspect.signature(fn)

            def run_for(args, kwargs):
                inp = None
                if capture_input:
                    try:
                        bound = sig.bind(*args, **kwargs)
                        bound.apply_defaults()
                        inp = {k: v for k, v in bound.arguments.items() if k not in ("self", "cls")}
                    except TypeError:
                        inp = {"args": args, "kwargs": kwargs}
                return self.start_run(label or fn.__qualname__, inp, tags=tags)

            if inspect.iscoroutinefunction(fn):
                @functools.wraps(fn)
                async def awrapper(*args, **kwargs):
                    with run_for(args, kwargs) as run:  # the current run is a contextvar: each asyncio task has its own
                        out = await fn(*args, **kwargs)
                        if capture_output:
                            run.log_output(out)
                        return out
                return awrapper

            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                with run_for(args, kwargs) as run:
                    out = fn(*args, **kwargs)
                    if capture_output:
                        run.log_output(out)
                    return out
            return wrapper

        if callable(name):
            label = None
            return deco(name)
        label = name
        return deco

    def _enqueue(self, rec):
        if self.mode == "local":
            try:
                self._store.log_runs(LOCAL_ORG, self.project_id, [rec])
            except Exception as e:  # recording must never break the application
                self.dropped += 1
                self._report(e, 1, dropped=True)
            return
        overflow = 0
        with self._lock:
            self._buffer.append(rec)
            while len(self._buffer) > self.max_buffer:
                self._buffer.popleft()
                overflow += 1
            self.dropped += overflow
            if self._thread is None:
                self._thread = threading.Thread(target=self._worker, name="sia-flush", daemon=True)
                self._thread.start()
                atexit.register(self.flush)
            full = len(self._buffer) >= self.batch_size
        if overflow:
            log.warning("SIA: buffer full (%d runs), dropped the %d oldest. last error: %s",
                        self.max_buffer, overflow, self.last_error)
        if full:
            self._wake.set()

    def _worker(self):
        while True:
            self._wake.wait(self.flush_interval)
            self._wake.clear()
            self._send()

    def _send(self):
        with self._send_lock:
            while True:
                with self._lock:
                    batch = [self._buffer.popleft() for _ in range(min(self.batch_size, len(self._buffer)))]
                if not batch:
                    return True
                try:
                    self._http.request("POST", self._p("/runs"), {"runs": batch})
                except (RateLimited, ServerError) as e:  # transient: keep the runs for the next attempt
                    with self._lock:
                        self._buffer.extendleft(reversed(batch))
                    self._report(e, len(batch))
                    return False
                except SIAError as e:  # permanent (auth, quota, validation): retrying would never succeed
                    self.dropped += len(batch)
                    self._report(e, len(batch), dropped=True)

    def flush(self) -> bool:
        """Send buffered runs now (blocking). True when nothing is left unsent; see last_error otherwise."""
        return True if self.mode == "local" else self._send()

    @property
    def pending(self) -> int:
        return len(self._buffer)

    def runs(self, *, status: str | None = None, tag: str | None = None, name: str | None = None,
             q: str | None = None, since: float | None = None, until: float | None = None, root_only: bool = False,
             limit: int = 50, offset: int = 0) -> list[dict]:
        self.flush()
        f = dict(status=status, tag=tag, name=name, q=q, since=since, until=until, root_only=root_only or None)
        return self._do(lambda: self._store.runs(LOCAL_ORG, self.project_id, limit, offset,
                                                 **{**f, "root_only": root_only}),
                        "GET", self._p("/runs"), params={**f, "limit": limit, "offset": offset})["items"]

    def get_run(self, run_id: str) -> dict:
        self.flush()
        return self._do(lambda: self._store.run(LOCAL_ORG, self.project_id, run_id), "GET", self._p(f"/runs/{run_id}"))

    def export_runs(self, path: str | None = None, **filters) -> list[dict]:
        """All runs matching the filters (oldest first). With `path`, also written there as JSON lines."""
        self.flush()
        if self.mode == "local":
            rows = self._store.export_runs(LOCAL_ORG, self.project_id, **filters)
        else:
            text = self._http.request("GET", self._p("/runs/export"), params=filters, raw=True)
            rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(json.dumps(r) + "\n" for r in rows)
        return rows

    def outcome(self, run: "Run | str", ok: bool, *, source: str) -> dict:
        """Report what really happened after a run, from your own system (source names it, e.g. 'refunds-db'). It is
        the strongest live signal SIA gets, because a person's opinion isn't involved."""
        run_id = run.id if isinstance(run, Run) else run
        self.flush()
        body = {"ok": ok, "source": source}
        return self._do(lambda: self._store.record_outcome(LOCAL_ORG, self.project_id, run_id, ok, source),
                        "POST", self._p(f"/runs/{run_id}/outcome"), body)

    # --- feedback -------------------------------------------------------------
    def feedback(self, run: "Run | str", rating: int, *, correction: Any = None, category: str | None = None,
                 explanation: str | None = None, reviewer: str | None = None) -> dict:
        """Attach feedback to a run: rating 1 (good) or -1 (bad), optionally the correct output. It is stored as
        *pending*; only a reviewer's verify step can turn it into a dataset example."""
        run_id = run.id if isinstance(run, Run) else run
        self.flush()  # the run has to be stored before feedback can point at it
        body = {"id": uuid.uuid4().hex, "rating": rating, "correction": self._prep(correction), "category": category,
                "explanation": self._scrub(explanation) if explanation else None, "reviewer": reviewer}
        return self._do(lambda: self._store.add_feedback(LOCAL_ORG, self.project_id, run_id, **body),
                        "POST", self._p(f"/runs/{run_id}/feedback"), body)

    def list_feedback(self, status: str | None = None, *, run_id: str | None = None, limit: int = 50,
                      offset: int = 0) -> list[dict]:
        return self._do(lambda: self._store.feedback_list(LOCAL_ORG, self.project_id, status, run_id, limit, offset),
                        "GET", self._p("/feedback"),
                        params={"status": status, "run_id": run_id, "limit": limit, "offset": offset})["items"]

    def review_feedback(self, feedback_id: str, status: str, *, dataset: "Dataset | str | None" = None,
                        expected_output: Any = None, note: str | None = None, reviewer: str | None = None,
                        critical: bool = False) -> dict:
        """Mark pending feedback 'verified' or 'rejected'. Verified feedback with a dataset becomes an example
        (input from the run, expected output from the correction or `expected_output`), linked to both."""
        did = None if dataset is None else (dataset.id if isinstance(dataset, Dataset) else self.dataset(dataset).id)
        body = {"status": status, "dataset_id": did, "expected_output": self._prep(expected_output), "note": note,
                "reviewer": reviewer, "critical": critical}
        return self._do(lambda: self._store.review_feedback(LOCAL_ORG, self.project_id, feedback_id, **body),
                        "POST", self._p(f"/feedback/{feedback_id}/review"), body)

    # --- datasets -------------------------------------------------------------
    def dataset(self, name: str) -> Dataset:
        """Get or create a dataset by name."""
        return Dataset(self, self._do(lambda: self._store.create_dataset(LOCAL_ORG, self.project_id, name),
                                      "POST", self._p("/datasets"), {"name": name}))

    create_dataset = dataset

    def datasets(self) -> list[dict]:
        return self._do(lambda: self._store.datasets(LOCAL_ORG, self.project_id), "GET", self._p("/datasets"))

    # --- evaluation -----------------------------------------------------------
    def evaluate(self, dataset: "Dataset | str", target: Callable, scorers: Iterable = ("exact_match",), *,
                 prompt: str | None = None, version: "int | str | None" = None, version_name: str = "system_prompt",
                 trials: int = 1, pass_threshold: float = 0.75, name: str | None = None,
                 metadata: dict | None = None, dataset_version: int | None = None, split: str | None = None) -> dict:
        """Run `target` on every example of a pinned dataset version and score it.

        target(input) is called as is, or target(input, prompt) when a prompt is given directly or through
        `version` (a version number or "active"). Exceptions in target are recorded per example (score 0), not
        raised. The evaluation is stored only once every example has a result. split='dev' or 'holdout' evaluates
        one split only (two evaluations are comparable only if they used the same split).
        """
        ds = dataset if isinstance(dataset, Dataset) else self.dataset(dataset)
        snap = ds.snapshot(dataset_version, split)
        if not snap["examples"]:
            raise ValidationError(f"Dataset '{ds.name}' has no examples at version {snap['snapshot_version']}.")
        if not 1 <= trials <= 10:
            raise ValidationError("trials must be between 1 and 10")
        sc: list[Scorer] = [resolve(s) for s in scorers]
        if len({s.name for s in sc}) != len(sc) or not sc:
            raise ValidationError("Give at least one scorer, with unique names.")
        vnum = None
        if version is not None:
            v = self.active_version(version_name) if version == "active" else self.get_version(int(version),
                                                                                              version_name)
            if v is None:
                raise NotFound(f"No active version of '{version_name}'.")
            prompt, vnum = v["content"], v["version"]
        if prompt is not None:  # the target gets a Prompt, so runs it records carry the version under test
            prompt = Prompt(prompt, vnum, version_name)
        results, token = [], _in_eval.set(True)
        try:
            for ex in snap["examples"]:
                per, errors, outputs, took = {s.name: [] for s in sc}, [], [], []
                for _ in range(trials):
                    t0 = time.perf_counter()
                    try:
                        out, err = (target(ex["input"]) if prompt is None else target(ex["input"], prompt)), None
                    except Exception as e:
                        out, err = None, f"{type(e).__name__}: {e}"
                    took.append((time.perf_counter() - t0) * 1000)
                    outputs.append(out)
                    for s in sc:
                        if err:
                            per[s.name].append(0.0)
                            continue
                        try:
                            per[s.name].append(s(out, ex["expected_output"], ex))
                        except Exception as e:
                            per[s.name].append(0.0)
                            err = f"scorer {s.name} failed: {type(e).__name__}: {e}"
                    if err:
                        errors.append(err)
                results.append({"example_id": ex["id"], "output": self._prep(outputs[-1]),
                                "scores": {k: mean(v) for k, v in per.items()},
                                "error": errors[0][:5000] if errors else None, "duration_ms": mean(took)})
        finally:
            _in_eval.reset(token)
        ev = {"id": uuid.uuid4().hex, "name": name, "dataset_id": ds.id, "dataset_version": snap["snapshot_version"],
              "version_name": version_name if vnum is not None else None, "version": vnum,
              "prompt_sha": hashlib.sha256(prompt.encode()).hexdigest() if prompt is not None else None,
              "config": {"scorers": [s.describe() for s in sc], "trials": trials, "pass_threshold": pass_threshold,
                         "metadata": metadata or {}, **({"split": split} if split else {})},
              "results": results}
        return self._do(lambda: self._store.record_evaluation(LOCAL_ORG, self.project_id, ev),
                        "POST", self._p("/evaluations"), ev)

    def get_evaluation(self, evaluation_id: str) -> dict:
        return self._do(lambda: self._store.evaluation(LOCAL_ORG, self.project_id, evaluation_id),
                        "GET", self._p(f"/evaluations/{evaluation_id}"))

    def evaluations(self, dataset: "Dataset | str | None" = None, limit: int = 50) -> list[dict]:
        did = None if dataset is None else (dataset.id if isinstance(dataset, Dataset) else self.dataset(dataset).id)
        return self._do(lambda: self._store.evaluations(LOCAL_ORG, self.project_id, did, limit),
                        "GET", self._p("/evaluations"), params={"dataset_id": did, "limit": limit})["items"]

    def compare(self, baseline: "dict | str", candidate: "dict | str", policy: dict | None = None) -> dict:
        """Paired comparison of two evaluations of the same dataset version. `policy` overrides the project's
        policy for this comparison only (an overridden comparison cannot be used to approve a version)."""
        a = baseline["id"] if isinstance(baseline, dict) else baseline
        b = candidate["id"] if isinstance(candidate, dict) else candidate
        body = {"baseline_eval_id": a, "candidate_eval_id": b, "policy": policy}
        return self._do(lambda: self._store.compare(LOCAL_ORG, self.project_id, a, b, policy),
                        "POST", self._p("/comparisons"), body)

    # --- versions -------------------------------------------------------------
    def project_settings(self) -> dict:
        return self._do(lambda: self._store.project(LOCAL_ORG, self.project_id), "GET", self._p(""))["settings"]

    def update_settings(self, **settings) -> dict:
        return self._do(lambda: self._store.update_project(LOCAL_ORG, self.project_id, settings),
                        "PATCH", self._p(""), {"settings": settings})["settings"]

    def create_version(self, content: str, *, name: str = "system_prompt", parent_version: int | None = None,
                       lessons: list | None = None, patterns: list | None = None, note: str | None = None) -> dict:
        body = {"content": content, "name": name, "parent_version": parent_version, "lessons": lessons,
                "patterns": patterns, "note": note}
        return self._do(lambda: self._store.create_version(LOCAL_ORG, self.project_id, **body),
                        "POST", self._p("/versions"), body)

    def versions(self, name: str | None = None) -> list[dict]:
        return self._do(lambda: self._store.versions(LOCAL_ORG, self.project_id, name), "GET", self._p("/versions"),
                        params={"name": name})

    def get_version(self, version: int, name: str = "system_prompt") -> dict:
        return self._do(lambda: self._store.version(LOCAL_ORG, self.project_id, name, version),
                        "GET", self._p(f"/versions/{name}/{version}"))

    def active_version(self, name: str = "system_prompt") -> dict | None:
        return self._do(lambda: self._store.active_version(LOCAL_ORG, self.project_id, name),
                        "GET", self._p("/versions/active"), params={"name": name})

    def get_prompt(self, name: str = "system_prompt", default: str | None = None, ttl: float = 30.0) -> Prompt | None:
        """The active version's text for your app to use, cached for `ttl` seconds. If SIA is unreachable it
        falls back to the last known text, then to `default`, so a SIA outage never takes your app down."""
        hit = self._prompts.get(name)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        fallback = Prompt(default, None, name) if default is not None else None
        try:
            v = self.active_version(name)
        except SIAError as e:
            log.warning("SIA: could not fetch prompt %r, using %s: %s", name, "cached" if hit else "default", e)
            return hit[1] if hit else fallback
        p = Prompt(v["content"], v["version"], name) if v else fallback
        self._prompts[name] = (time.monotonic(), p)
        return p

    def approve(self, version: int, *, name: str = "system_prompt", comparison: "dict | str | None" = None,
                override_reason: str | None = None, reviewer: str | None = None) -> dict:
        cid = comparison["id"] if isinstance(comparison, dict) else comparison
        body = {"comparison_id": cid, "override_reason": override_reason, "reviewer": reviewer}
        return self._do(lambda: self._store.approve(LOCAL_ORG, self.project_id, name, version, cid, override_reason,
                                                    reviewer), "POST", self._p(f"/versions/{name}/{version}/approve"),
                        body)

    def reject(self, version: int, *, name: str = "system_prompt", reason: str | None = None,
               reviewer: str | None = None) -> dict:
        body = {"reason": reason, "reviewer": reviewer}
        return self._do(lambda: self._store.reject(LOCAL_ORG, self.project_id, name, version, reason, reviewer),
                        "POST", self._p(f"/versions/{name}/{version}/reject"), body)

    def activate(self, version: int, *, name: str = "system_prompt", reviewer: str | None = None) -> dict:
        """Make an approved version the one get_prompt() returns. Activating an older one is a rollback."""
        self._prompts.pop(name, None)
        return self._do(lambda: self._store.activate(LOCAL_ORG, self.project_id, name, version, reviewer),
                        "POST", self._p(f"/versions/{name}/{version}/activate"), {"reviewer": reviewer})

    def audit_log(self, limit: int = 100) -> list[dict]:
        return self._do(lambda: self._store.audit(LOCAL_ORG, self.project_id, limit), "GET", self._p("/audit"),
                        params={"limit": limit})

    def overview(self) -> dict:
        self.flush()
        return self._do(lambda: self._store.overview(LOCAL_ORG, self.project_id), "GET", self._p("/overview"))

    # --- improvement ----------------------------------------------------------
    def propose_improvement(self, llm, *, name: str = "system_prompt", evaluation: "dict | str | None" = None,
                            max_items: int = 20, note: str | None = None) -> dict:
        """Draft a *candidate* version from evidence: failed cases of `evaluation`, verified feedback and recurring
        run errors. Unverified feedback is never used. `llm` is anything with .text(system, prompt) (for example
        sia.llm.LLM). Nothing is approved or activated: evaluate, compare and approve it explicitly."""
        from sia.llm import parse_json  # pure function; sia.llm only needs httpx

        current = self.active_version(name)
        ev = self.get_evaluation(evaluation) if isinstance(evaluation, str) else evaluation
        fails = [r for r in (ev or {}).get("results", []) if not r["passed"]][:max_items]
        verified = self.list_feedback("verified", limit=max_items)
        errors = self.runs(status="error", limit=200)
        counts = Counter(f"feedback category: {f['category']}" for f in verified if f["category"])
        counts.update(f"error: {(r['error'] or '').splitlines()[0][:120]}" for r in errors if "eval" not in r["tags"])
        patterns = [{"pattern": p, "count": n} for p, n in counts.most_common(10)]
        if not (fails or verified or patterns):
            raise ValidationError("No evidence to learn from: pass a failing evaluation, verify some feedback, "
                                  "or record runs with errors first.")
        locked = self.project_settings()["locked_text"]
        evidence = "\n".join(
            [f"- FAILED CASE input={as_text(r['input'])[:600]} | expected={as_text(r['expected_output'])[:600]} | "
             f"got={as_text(r['output'])[:600]} | error={r['error'] or ''}" for r in fails]
            + [f"- VERIFIED FEEDBACK on input={as_text(f['run_input'])[:400]} | category={f['category']} | "
               f"explanation={f['explanation'] or ''} | correct output={as_text(f['correction'])[:600]}"
               for f in verified]
            + [f"- RECURRING ({p['count']}x) {p['pattern']}" for p in patterns])
        raw = llm.text(
            "You improve the instructions of an AI application. You only change instruction text, never code.",
            f"CURRENT INSTRUCTIONS:\n{current['content'] if current else '(none yet)'}\n\nEVIDENCE OF FAILURES:\n"
            f"{evidence}\n\n"
            + (f"These lines are locked and must appear verbatim in your output:\n"
               + "\n".join(f"- {t}" for t in locked) + "\n\n" if locked else "")
            + "Find the recurring failure patterns, extract up to 5 short reusable lessons, then write improved "
              "instructions that fix the patterns and keep everything that already works. Return JSON: "
              '{"patterns": ["..."], "lessons": ["..."], "content": "<the full improved instructions>"}')
        out = parse_json(raw)
        content = out.get("content") if isinstance(out, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise ValidationError("The model did not return candidate instructions.")
        lessons = [str(x)[:500] for x in (out.get("lessons") or []) if x][:10]
        patterns += [{"pattern": str(x)[:300], "count": None} for x in (out.get("patterns") or []) if x][:10]
        return self.create_version(content.strip(), name=name, parent_version=current["version"] if current else None,
                                   lessons=lessons, patterns=patterns,
                                   note=note or f"generated from {len(fails)} failed eval case(s), {len(verified)} "
                                                f"verified feedback item(s) and {len(errors)} error run(s)")
