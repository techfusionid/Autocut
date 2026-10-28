def main() -> None:
    """Local dev entrypoint: `uv run autocut-2`."""
    import logging

    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run("autocut_2.api:app", host="127.0.0.1", port=8009, reload=True)
