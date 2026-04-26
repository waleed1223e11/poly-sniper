from flask import Flask
import os

app = Flask(__name__)

@app.route("/")
def hello():
    return "OK - Flask is running!"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 80))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
