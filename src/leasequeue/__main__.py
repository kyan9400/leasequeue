import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the LeaseQueue API and operations console")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--workers", type=int, default=1)
    arguments = parser.parse_args()
    uvicorn.run(
        "leasequeue.main:app",
        host=arguments.host,
        port=arguments.port,
        workers=arguments.workers,
    )


if __name__ == "__main__":
    main()
