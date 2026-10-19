from google import genai
from google.genai import types
from dotenv import load_dotenv
import os
from pathlib import Path
from workspace import Workspace
import shutil
import tempfile
import json
import time
import uuid
from datetime import datetime, timezone


load_dotenv()
api_key = os.getenv("GENAI_API_KEY")

if not api_key:
    raise RuntimeError("GENAI_API_KEY is missing from your .env file")

client = genai.Client(api_key=api_key)

TASK_SOURCE = (
    Path(__file__).resolve().parent
    / "task_01"
)

MAX_STEPS = 5

# 1. Define tools correctly wrapped in types.Tool
tools = [
    types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="read_file",
                description="Read a UTF-8 text file inside the workspace.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "path": types.Schema(
                            type=types.Type.STRING,
                            description="Relative path of the file to read."
                        )
                    },
                    required=["path"]
                )
            ),
            types.FunctionDeclaration(
                name="write_file",
                description=(
                    "Write text to a file inside the workspace. "
                    "Writing to test_*.py files is forbidden."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "path": types.Schema(
                            type=types.Type.STRING,
                            description="Relative path of the file to write."
                        ),
                        "content": types.Schema(
                            type=types.Type.STRING,
                            description="Text content to write."
                        )
                    },
                    required=["path", "content"]
                )
            ),
            types.FunctionDeclaration(
                name="run_tests",
                description="Run pytest in the workspace and return the results.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={}
                )
            )
        ]
    )
]

config = types.GenerateContentConfig(
    tools=tools
)


def execute_tool(name, args, workspace):
    try:
        if name == "read_file":
            return workspace.read_file(args["path"])

        elif name == "write_file":
            return workspace.write_file(
                args["path"],
                args["content"]
            )

        elif name == "run_tests":
            return workspace.run_tests()

        else:
            return {
                "result": f"Unknown tool: {name}",
                "is_error": True,
            }

    except Exception as e:
        return {
            "result": str(e)[:4000],
            "is_error": True,
        }


def create_task_copy():
    temp_dir = tempfile.mkdtemp(prefix="agent_task_")
    copied_task = Path(temp_dir) / "task_01"

    try:
        shutil.copytree(TASK_SOURCE, copied_task)
        return temp_dir, copied_task
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise



TRACE_FILE = Path(__file__).resolve().parent / "trace.jsonl"


def json_safe(value):
    """Convert SDK objects and nested values into JSON-safe data."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=False)

    if isinstance(value, Path):
        return str(value)

    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}

    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]

    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    return str(value)


def log_event(run_id, event, **fields):
    """Append one JSON object as one line to the trace."""
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "event": event,
        **fields,
    }

    with TRACE_FILE.open("a", encoding="utf-8") as file:
        file.write(
            json.dumps(
                json_safe(record),
                ensure_ascii=False,
                default=str,
            ) + "\n"
        )


def get_token_counts(response):
    """Read token usage from the Gemini response, if available."""
    usage = response.usage_metadata

    if usage is None:
        return {
            "prompt": None,
            "output": None,
            "total": None,
        }

    return {
        "prompt": usage.prompt_token_count,
        "output": usage.candidates_token_count,
        "total": usage.total_token_count,
    }


def run_agent():
    temp_dir, copied_task = create_task_copy()
    run_id = str(uuid.uuid4())

    log_event(
        run_id,
        "run_start",
        task_source=str(TASK_SOURCE),
        workspace=str(copied_task),
        max_steps=MAX_STEPS,
    )

    try:
        workspace = Workspace(copied_task)

        print("Original task:", TASK_SOURCE)
        print("Agent workspace:", copied_task)

        # 2. Start one conversation
        history = [
            types.Content(
                role="user",
                parts=[
                    types.Part.from_text(
                        text=(
                            "List the files in the current directory "
                            "and tell me the full directory path. "
                            "Use the available tools."
                        )
                    )
                ]
            )
        ]
        log_event(
                run_id,
                "initial_history",
                messages=json_safe(history),
            )
        
        # 3. Agent loop
        for step in range(MAX_STEPS):
            print(f"\n--- STEP {step + 1} ---")

            request_start = time.perf_counter()

            response = client.models.generate_content(
                model="gemini-3.8-flash",
                contents=history,
                config=config,
            )

            latency_ms = round(
                (time.perf_counter() - request_start) * 1000,
                2,
            )

            candidate = response.candidates[0]
            finish_reason = candidate.finish_reason

            log_event(
                run_id,
                "model_response",
                step=step + 1,
                stop_reason=getattr(
                    finish_reason, "name", str(finish_reason)
                ),
                tokens=get_token_counts(response),
                latency_ms=latency_ms,
                content=json_safe(candidate.content),
                response_text=response.text,
                response=json_safe(response),
            )


            candidate = response.candidates[0].content
            print("FINISH REASON:", response.candidates[0].finish_reason)

            # Collect every function call in this response
            calls = [
                part.function_call
                for part in candidate.parts
                if part.function_call
            ]

            # No function calls means the model has finished
            if not calls:
                print("\nFINAL ANSWER:")
                print(response.text or candidate)
                break

            # Preserve the complete model response
            history.append(candidate)

            # Execute every requested function
            results = []

            for call in calls:
                print("\nREQUESTED TOOL:", call.name)
                print("ARGUMENTS:", call.args)

                result = execute_tool(call.name, call.args, workspace)

                print("TOOL RESULT:", result)

                results.append(
                    types.Part.from_function_response(
                        name=call.name,
                        response=result
                    )
                )

            # Send all results in one message after the loop
            history.append(
                types.Content(
                    role="user",
                    parts=results
                )
            )
            
            for call in calls:
                tool_name = call.name
                tool_input = json_safe(call.args)

                result = execute_tool(
                    tool_name,
                    call.args,
                    workspace,
                )

                tool_error = bool(result.get("is_error", False))

                log_event(
                    run_id,
                    "tool_call",
                    step=step + 1,
                    name=tool_name,
                    input=tool_input,
                    output=json_safe(result),
                    error=tool_error,
                )

                print("\nREQUESTED TOOL:", tool_name)
                print("ARGUMENTS:", tool_input)
                print("TOOL RESULT:", result)

                results.append(
                    types.Part.from_function_response(
                        name=tool_name,
                        response=result,
                    )
                )


        else:
            print("\nStopped: MAX_STEPS reached without a final answer.")

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    run_agent()