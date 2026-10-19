from google import genai
from google.genai import types
from dotenv import load_dotenv
import os
from pathlib import Path
from workspace import Workspace
import shutil
import tempfile


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


def run_agent():
    temp_dir, copied_task = create_task_copy()

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

        # 3. Agent loop
        for step in range(MAX_STEPS):
            print(f"\n--- STEP {step + 1} ---")

            response = client.models.generate_content(
                model="gemini-3.8-flash",
                contents=history,
                config=config
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

        else:
            print("\nStopped: MAX_STEPS reached without a final answer.")

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    run_agent()