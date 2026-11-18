"""One-worker runtime entry point shared by local and container execution."""

from pathlib import Path

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "cahuita_conditions.app:create_app",
        factory=True,
        host="0.0.0.0",
        port=8000,
        workers=1,
        access_log=False,
        log_config=str(Path(__file__).with_name("logging.json")),
    )
