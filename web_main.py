import os
import sys
sys.path.insert(0, os.path.dirname(__file__))

from flask import Flask, render_template
from flask_socketio import SocketIO
from src.iso15118_sim import ISO15118Simulator

app = Flask(__name__)
app.config['SECRET_KEY'] = 'iso15118-secret'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

sim = None


def emit_to_client(event, data):
    socketio.emit(event, data)


@app.route('/')
def index():
    return render_template('index.html')


@socketio.on('start_session')
def handle_start(data):
    global sim
    if sim and sim.running:
        return
    sim = ISO15118Simulator(emit_cb=emit_to_client)
    sim.start_session(config=data)


@socketio.on('stop_session')
def handle_stop():
    global sim
    if sim:
        sim.stop_session()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, allow_unsafe_werkzeug=True)
