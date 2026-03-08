from threading import Thread
import os

from flask import Flask

app = Flask(__name__)


@app.route("/")
def index() -> str:
    return "Bot online"


def run() -> None:
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)


def keep_alive() -> None:
    server = Thread(target=run, daemon=True)
    server.start()
