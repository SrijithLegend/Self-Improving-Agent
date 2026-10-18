from google import genai
from dotenv import load_dotenv
import os

load_dotenv()
api_key = os.getenv("GENAI_API_KEY")

client = genai.Client(api_key=api_key)

response = client.models.generate_content(
    model="gemini-3.8-flash", 
    contents="Say hi",
)

print(response.text)
print("FULL RESPONSE:\n", response)
print("\nCONTENT:\n", response.candidates[0].content)
print("\nSTOP REASON:\n", response.candidates[0].finish_reason)
print("\nUSAGE:\n", response.usage_metadata)