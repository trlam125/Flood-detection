import uvicorn

from app.config import settings

if __name__ == "__main__":
    # Long-lived camera streams must not keep shutdown waiting indefinitely.
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False,
                timeout_graceful_shutdown=5)
