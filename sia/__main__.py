"""python -m sia serve [--port 8000]      start the API
python -m sia create-tenant <name>      create a customer and print its API key (shown once)"""
import os
import sys

from dotenv import load_dotenv

load_dotenv()

if len(sys.argv) >= 2 and sys.argv[1] == "serve":
    import uvicorn
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else int(os.getenv("PORT", 8000))
    uvicorn.run("sia.api:app", host=os.getenv("HOST", "127.0.0.1"), port=port)
elif len(sys.argv) == 3 and sys.argv[1] == "create-tenant":
    from .store import Store
    print(Store(os.getenv("SIA_DB", "sia.db")).create_tenant(sys.argv[2]))
else:
    print(__doc__)
