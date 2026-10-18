from google import genai
from google.genai import types
from dotenv import load_dotenv
import os
import json

load_dotenv()
api_key = os.getenv("GENAI_API_KEY")

client = genai.Client(api_key=api_key)

list_files_tool = types.Tool(
    function_declarations=[
        types.FunctionDeclaration(
            name="list_files",
            description="List files in the current directory.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={}
            )
        )
    ]
)

response = client.models.generate_content(
    model="gemini-3.8-flash", 
    contents="What files are here? Use the list_files tool.",
    config=types.GenerateContentConfig(
        tools=[list_files_tool]
    )
)

print("=" * 40)
print("READABLE FULL RESPONSE (JSON):")
print("=" * 40)

# Convert the SDK response object to a dictionary and print it cleanly as JSON
try:
    response_dict = response.model_dump()
    print(json.dumps(response_dict, indent=2, default=str))
except Exception as e:
    print("Could not dump model as dict directly:", e)
    print(response)

print("\n" + "=" * 40)
print("PARSED FUNCTION CALLS:")
print("=" * 40)

# Loop through parts to see if the model triggered a tool call
for candidate in response.candidates:
    for part in candidate.content.parts:
        if part.function_call:
            print(f"Function Name : {part.function_call.name}")
            print(f"Arguments     : {part.function_call.args}")
        elif part.text:
            print(f"Model Text    : {part.text}")