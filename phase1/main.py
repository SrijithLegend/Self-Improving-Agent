from google import genai
from google.genai import types
from dotenv import load_dotenv
import os
import json

load_dotenv()
api_key = os.getenv("GENAI_API_KEY")

client = genai.Client(api_key=api_key)

# 1. Define the tool
list_files_tool = types.Tool(
    function_declarations=[
        types.FunctionDeclaration(
            name="list_files",
            description="List files in the current working directory.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={}
            )
        )
    ]
)

config = types.GenerateContentConfig(
    tools=[list_files_tool]
)

# 2. Ask Gemini to use the tool
prompt = "What files are here? Use list_files to find out."

response = client.models.generate_content(
    model="gemini-3.8-flash",
    contents=prompt,
    config=config
)

print("STEP 1: INITIAL RESPONSE")
print(response)

# 3. Extract the function call
candidate = response.candidates[0].content
function_call = next(
    (
        part.function_call
        for part in candidate.parts
        if part.function_call
    ),
    None
)

if function_call is None:
    raise RuntimeError(
        "Gemini did not request list_files. Inspect the response."
    )

print("\nFUNCTION REQUESTED:", function_call.name)
print("FUNCTION ARGUMENTS:", function_call.args)
print("FUNCTION CALL ID:", function_call.id)

if function_call.name != "list_files":
    raise RuntimeError("Unexpected function requested")

# 4. Actually execute the function in Python
files = os.listdir(os.getcwd())

tool_output = {
    "files": files
}

print("\nACTUAL FILE LIST:")
print(tool_output)

# 5. Send the original function-call content AND the result
tool_result = types.Part.from_function_response(
    name="list_files",
    response=tool_output
)

response2 = client.models.generate_content(
    model="gemini-3.8-flash",
    contents=[
        types.Content(
            role="user",
            parts=[types.Part.from_text(text=prompt)]
        ),
        candidate,
        types.Content(
            role="user",
            parts=[tool_result]
        )
    ],
    config=config
)

# 6. Inspect the final answer
print("\nSTEP 2: FINAL RESPONSE")
print(response2)

print("\nFINAL ANSWER:")
print(response2.text)
