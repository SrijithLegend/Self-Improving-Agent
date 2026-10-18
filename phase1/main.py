
from google import genai
from google.genai import types
from dotenv import load_dotenv
import os

load_dotenv()
api_key = os.getenv("GENAI_API_KEY")

if not api_key:
    raise RuntimeError("GENAI_API_KEY is missing from your .env file")

client = genai.Client(api_key=api_key)

MAX_STEPS = 5

# 1. Define both tools
tools = [
    types.FunctionDeclaration(
        name="list_files",
        description="List files in the current working directory.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={}
        )
    ),
    types.FunctionDeclaration(
        name="get_current_directory",
        description="Return the current working directory path.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={}
        )
    )
]

config = types.GenerateContentConfig(
    tools=[types.Tool(function_declarations=tools)]
)

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

        try:
            if call.name == "list_files":
                result = {
                    "files": os.listdir(os.getcwd())
                }

            elif call.name == "get_current_directory":
                result = {
                    "directory": os.getcwd()
                }

            else:
                result = {
                    "error": f"Unknown tool: {call.name}"
                }

        except Exception as e:
            result = {"error": str(e)}

        print("TOOL RESULT:", result)

        results.append(
            types.Part.from_function_response(
                name=call.name,
                response=result
            )
        )

    # Send all results in one message
    history.append(
        types.Content(
            role="user",
            parts=results
        )
    )

else:
    print("\nStopped: MAX_STEPS reached without a final answer.")
