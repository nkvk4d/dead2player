from flask import Flask, request, jsonify
import time
import logging

app = Flask(__name__)
peers = {}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Tracker")

PEER_TIMEOUT = 90

@app.route('/announce', methods=['POST'])
def announce():
    data = request.json
    if not data or 'port' not in data:
        return jsonify({"status": "error", "message": "Invalid request"}), 400

    peer_ip = request.remote_addr
    peer_port = data['port']
    peer_key = (peer_ip, peer_port)

    peers[peer_key] = time.time()
    
    current_time = time.time()
    for p_key in list(peers.keys()):
        if current_time - peers[p_key] > PEER_TIMEOUT:
            del peers[p_key]

    peer_list = [
        [ip, port] for (ip, port) in peers.keys() 
        if not (ip == peer_ip and port == peer_port)
    ]

    logger.info(f"Announce from {peer_ip}:{peer_port}. Active peers: {len(peers)}")
    
    return jsonify({
        "status": "ok",
        "peers": peer_list
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
    