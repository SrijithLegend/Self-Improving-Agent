"""HTTP API. Tenants authenticate with their SIA key; every LLM call uses the caller's own provider key.

Auth:  Authorization: Bearer <sia api key>
LLM:   X-LLM-Provider (gemini|anthropic|openai|groq|openrouter|...), X-LLM-Key, optional X-LLM-Model
Keys sent in X-LLM-Key are used for the request only and never stored.
"""
import os
import secrets

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .agent import Agent
from .llm import LLM, LLMError
from .store import Store

app = FastAPI(title="Self-Improving Agent API", version="1.0.0",
              description="Create domain agents that improve from experience, feedback and documents. "
                          "Bring your own LLM key.")
store = Store(os.getenv("SIA_DB", "sia.db"))


# --- dependencies ------------------------------------------------------------
def tenant(authorization: str = Header(default="")):
    key = authorization.removeprefix("Bearer ").strip()
    t = store.tenant_by_key(key) if key else None
    if not t:
        raise HTTPException(401, "Invalid or missing API key (Authorization: Bearer <key>).")
    return t


def llm(x_llm_provider: str = Header(default="gemini"), x_llm_key: str = Header(default=""),
        x_llm_model: str = Header(default=""), x_llm_base_url: str = Header(default="")):
    # Self-hosters may set a server-side default key (e.g. their own free Gemini key).
    key = x_llm_key or (os.getenv("SIA_DEFAULT_LLM_KEY", "") if x_llm_provider == "gemini" else "")
    try:
        return LLM(x_llm_provider, key, x_llm_model or None, x_llm_base_url or None)
    except LLMError as e:
        raise HTTPException(400, str(e))


def owned_agent(agent_id: str, t=Depends(tenant)):
    a = store.agent(agent_id, t["id"])
    if not a:
        raise HTTPException(404, "Agent not found.")
    return a


def call(fn, *args, **kwargs):
    """Map engine errors onto HTTP status codes."""
    try:
        return fn(*args, **kwargs)
    except LLMError as e:
        raise HTTPException(502, f"LLM provider error: {e}")
    except KeyError as e:
        raise HTTPException(404, str(e).strip("'"))
    except ValueError as e:
        raise HTTPException(400, str(e))


# --- request bodies ----------------------------------------------------------
class TenantIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class AgentIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    domain: str = Field(min_length=1, max_length=500, description="e.g. 'Indian income-tax advisory'")
    instructions: str = Field("", max_length=20000, description="Role, tone, constraints, output format.")
    rubric: str = Field("", max_length=5000, description="What a great answer looks like; used for self-grading.")


class AgentPatch(BaseModel):
    name: str | None = Field(None, max_length=200)
    domain: str | None = Field(None, max_length=500)
    instructions: str | None = Field(None, max_length=20000)
    rubric: str | None = Field(None, max_length=5000)


class DocIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=2_000_000)


class ExampleIn(BaseModel):
    input: str = Field(min_length=1, max_length=20000)
    expected: str = Field(min_length=1, max_length=20000, description="Reference answer or grading notes.")


class RunIn(BaseModel):
    task: str = Field(min_length=1, max_length=20000)
    learn: bool = Field(True, description="Self-critique, revise and learn. False = one fast call, no learning.")
    max_revisions: int = Field(2, ge=0, le=5)


class FeedbackIn(BaseModel):
    episode_id: str
    rating: int = Field(ge=1, le=5)
    correction: str | None = Field(None, max_length=20000, description="The answer it should have given.")
    comment: str | None = Field(None, max_length=5000)


class StudyIn(BaseModel):
    rounds: int = Field(3, ge=1, le=20)


class ImproveIn(BaseModel):
    max_examples: int = Field(8, ge=1, le=30)


# --- routes ------------------------------------------------------------------
@app.get("/health")
def health():
    return {"ok": True}


@app.post("/v1/tenants", status_code=201)
def create_tenant(body: TenantIn, x_admin_key: str = Header(default="")):
    admin = os.getenv("SIA_ADMIN_KEY", "")
    if not admin or not secrets.compare_digest(x_admin_key, admin):
        raise HTTPException(403, "Admin key required.")
    return store.create_tenant(body.name)  # the api_key is shown only once


@app.post("/v1/agents", status_code=201)
def create_agent(body: AgentIn, t=Depends(tenant)):
    return store.create_agent(t["id"], body.name, body.domain, body.instructions, body.rubric)


@app.get("/v1/agents")
def list_agents(t=Depends(tenant)):
    return store.agents(t["id"])


@app.get("/v1/agents/{agent_id}")
def get_agent(a=Depends(owned_agent)):
    return {**a, "stats": store.stats(a["id"]), "playbook": store.active_playbook(a["id"])["content"]}


@app.patch("/v1/agents/{agent_id}")
def update_agent(body: AgentPatch, a=Depends(owned_agent)):
    store.update_agent(a["id"], **body.model_dump())
    return store.agent(a["id"])


@app.delete("/v1/agents/{agent_id}", status_code=204)
def delete_agent(a=Depends(owned_agent)):
    store.delete_agent(a["id"])


@app.post("/v1/agents/{agent_id}/knowledge", status_code=201)
def add_knowledge(body: DocIn, a=Depends(owned_agent)):
    return store.add_doc(a["id"], body.title, body.content)


@app.get("/v1/agents/{agent_id}/knowledge")
def list_knowledge(a=Depends(owned_agent)):
    return store.docs(a["id"])


@app.delete("/v1/agents/{agent_id}/knowledge/{doc_id}", status_code=204)
def delete_knowledge(doc_id: str, a=Depends(owned_agent)):
    store.delete_doc(a["id"], doc_id)


@app.post("/v1/agents/{agent_id}/examples", status_code=201)
def add_example(body: ExampleIn, a=Depends(owned_agent)):
    return {"id": store.add_example(a["id"], body.input, body.expected, "user")}


@app.get("/v1/agents/{agent_id}/examples")
def list_examples(a=Depends(owned_agent)):
    return store.examples(a["id"])


@app.delete("/v1/agents/{agent_id}/examples/{example_id}", status_code=204)
def delete_example(example_id: int, a=Depends(owned_agent)):
    store.delete_example(a["id"], example_id)


@app.post("/v1/agents/{agent_id}/run")
def run(body: RunIn, a=Depends(owned_agent), model=Depends(llm)):
    out = call(Agent(store, model, a).run, body.task, body.learn, body.max_revisions)
    return {**out, "llm_calls": model.calls}


@app.post("/v1/agents/{agent_id}/feedback")
def feedback(body: FeedbackIn, a=Depends(owned_agent), model=Depends(llm)):
    out = call(Agent(store, model, a).feedback, body.episode_id, body.rating, body.correction, body.comment)
    return {**out, "llm_calls": model.calls}


# ponytail: study/improve run synchronously (can take minutes on rate-limited free tiers);
# move to a job queue + polling endpoint if requests start hitting proxy timeouts.
@app.post("/v1/agents/{agent_id}/study")
def study(body: StudyIn, a=Depends(owned_agent), model=Depends(llm)):
    return {**call(Agent(store, model, a).study, body.rounds), "llm_calls": model.calls}


@app.post("/v1/agents/{agent_id}/improve")
def improve(body: ImproveIn, a=Depends(owned_agent), model=Depends(llm)):
    return {**call(Agent(store, model, a).improve, body.max_examples), "llm_calls": model.calls}


@app.get("/v1/agents/{agent_id}/lessons")
def list_lessons(include_inactive: bool = False, a=Depends(owned_agent)):
    return store.lessons(a["id"], include_inactive)


@app.post("/v1/agents/{agent_id}/lessons/{lesson_id}/{action}")
def toggle_lesson(lesson_id: int, action: str, a=Depends(owned_agent)):
    if action not in ("enable", "disable"):
        raise HTTPException(404, "Use /enable or /disable.")
    store.set_lesson_active(a["id"], lesson_id, action == "enable")
    return {"id": lesson_id, "active": action == "enable"}


@app.get("/v1/agents/{agent_id}/episodes")
def list_episodes(limit: int = 50, a=Depends(owned_agent)):
    return store.episodes(a["id"], min(max(limit, 1), 500))


@app.get("/v1/agents/{agent_id}/playbooks")
def list_playbooks(a=Depends(owned_agent)):
    return store.playbooks(a["id"])


@app.post("/v1/agents/{agent_id}/playbooks/{version}/activate")
def activate_playbook(version: int, a=Depends(owned_agent)):
    if not store.activate_playbook(a["id"], version):
        raise HTTPException(404, "Playbook version not found.")
    return {"active_version": version}
