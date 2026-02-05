# dead2player
The system is designed to receive and play video streams under unstable network conditions by utilizing the resources of local nodes.
Technical Description
The system consists of a client (player) and a server (tracker). The client application implements a hybrid data retrieval scheme: an initial request to the broadcast source and subsequent filling of the playback queue via a P2P protocol. Interaction between nodes is carried out through a tracker server, which registers active IP addresses.
System Requirements and Installation
Python 3.8 or higher is required to run the system.
The following dependencies must be installed:
pip install opencv-python pillow customtkinter av requests flask
Playback: After the broadcast is initialized, the system enters pre-buffering mode. Playback begins automatically after 3 to 5 video segments (chunks) have accumulated in the queue.
Architecture Features
The player reads bytes directly from asyncio.Queue. The P2P module assembles video segments and transfers them to the queue only after verifying data integrity. If no peers are available, the system switches to direct traffic consumption from the main server.
