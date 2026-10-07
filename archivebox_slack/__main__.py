import os

import uvicorn


def main():
    uvicorn.run(
        "archivebox_slack.app:create_app",
        factory=True,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8001")),
        workers=1,
        access_log=False,
    )


if __name__ == "__main__":
    main()
