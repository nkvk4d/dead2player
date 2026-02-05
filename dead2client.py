import tkinter as tk
from tkinter import messagebox
import customtkinter as ctk
import cv2
from PIL import Image, ImageTk
import asyncio
import threading
import struct
import heapq
import time
import av
import os
import logging
import random
import requests
from io import BytesIO
from dataclasses import dataclass, field
from typing import Dict, Set, List, Optional, Tuple

logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger(__name__)

@dataclass(order=True)
class ChunkRequest:
    deadline: float
    chunk_id: int = field(compare=False)

class PeerProtocol(asyncio.Protocol):
    def __init__(self, manager):
        self.manager = manager
        self.transport = None
        self.remote_peer_id = None
        self.am_choking = True
        self.peer_choking = True
        self.peer_interested = False
        self.known_chunks = set()
        self._buffer = bytearray()
        self.MAX_PAYLOAD = 15 * 1024 * 1024 

    def connection_made(self, transport):
        self.transport = transport
        self.manager.add_peer(self)

    def connection_lost(self, exc):
        self.manager.remove_peer(self)

    def data_received(self, data):
        self._buffer.extend(data)
        while len(self._buffer) >= 4:
            try:
                msg_len = struct.unpack('!I', self._buffer[:4])[0]
                if msg_len > self.MAX_PAYLOAD or msg_len < 0:
                    self.transport.close()
                    return
                if len(self._buffer) < 4 + msg_len:
                    break
                payload = self._buffer[4:4+msg_len]
                self._buffer = self._buffer[4+msg_len:]
                if not payload: continue
                
                msg_type = payload[0]
                msg_data = payload[1:]

                if msg_type == 0x01:
                    if len(msg_data) >= 4:
                        ids = struct.unpack(f'!{len(msg_data)//4}I', msg_data)
                        self.known_chunks.update(ids)
                        self.manager.update_interest()
                elif msg_type == 0x02:
                    self.peer_interested = True
                    self.send_choke(False)
                elif msg_type == 0x03:
                    if not self.am_choking and len(msg_data) >= 4:
                        cid = struct.unpack('!I', msg_data[:4])[0]
                        self.send_chunk(cid)
                elif msg_type == 0x04:
                    self.peer_choking = True
                elif msg_type == 0x05:
                    self.peer_choking = False
                elif msg_type == 0x06:
                    if len(msg_data) >= 4:
                        cid = struct.unpack('!I', msg_data[:4])[0]
                        self.manager.loop.create_task(self.manager.on_chunk_received(cid, msg_data[4:]))
            except Exception:
                break

    def _send_msg(self, msg_type: int, payload: bytes = b''):
        if self.transport and not self.transport.is_closing():
            try:
                full_msg = struct.pack('!I', len(payload) + 1) + bytes([msg_type]) + payload
                self.transport.write(full_msg)
            except Exception: pass

    def send_have(self, chunk_ids: List[int]):
        if chunk_ids:
            self._send_msg(0x01, struct.pack(f'!{len(chunk_ids)}I', *chunk_ids))

    def send_interested(self):
        self._send_msg(0x02)

    def send_choke(self, state: bool):
        self.am_choking = state
        self._send_msg(0x04 if state else 0x05)

    def send_request(self, chunk_id: int):
        self._send_msg(0x03, struct.pack('!I', chunk_id))

    def send_chunk(self, chunk_id: int):
        data = self.manager.get_chunk_data(chunk_id)
        if data: 
            self._send_msg(0x06, struct.pack('!I', chunk_id) + data)

class SwarmManager:
    def __init__(self, video_queue, loop, tracker_url):
        self.peers = set()
        self.max_uploads = 10
        self.request_queue = []
        self.storage = {}
        self.max_storage_size = 500
        self.playback_history = []
        self.video_queue = video_queue
        self.loop = loop
        self.requested_ids = {}
        self.lock = asyncio.Lock()
        self.tracker_url = tracker_url
        self.my_port = 8888

    def add_peer(self, peer):
        self.peers.add(peer)

    def remove_peer(self, peer):
        self.peers.discard(peer)

    async def on_chunk_received(self, chunk_id, data):
        async with self.lock:
            if chunk_id not in self.storage:
                self.storage[chunk_id] = data
                self.playback_history.append(chunk_id)
                if len(self.storage) > self.max_storage_size:
                    oldest = self.playback_history.pop(0)
                    self.storage.pop(oldest, None)
                await self.video_queue.put((chunk_id, data))
                self.requested_ids.pop(chunk_id, None)
                for p in list(self.peers):
                    p.send_have([chunk_id])

    def set_needed_chunks(self, chunks: Dict[int, float]):
        now = time.time()
        for cid, deadline in chunks.items():
            if cid not in self.storage:
                if cid not in self.requested_ids or (now - self.requested_ids[cid]) > 3.0:
                    heapq.heappush(self.request_queue, ChunkRequest(deadline, cid))
                    self.requested_ids[cid] = now

    def update_interest(self):
        needed_ids = {r.chunk_id for r in self.request_queue}
        for peer in self.peers:
            if any(cid in peer.known_chunks for cid in needed_ids):
                peer.send_interested()

    def manage_choking(self):
        interested = [p for p in self.peers if p.peer_interested]
        for i, peer in enumerate(interested):
            peer.send_choke(i >= self.max_uploads)

    def process_requests(self):
        if not self.request_queue: return
        retry_list = []
        while self.request_queue:
            req = heapq.heappop(self.request_queue)
            available_peers = [p for p in self.peers if not p.peer_choking and req.chunk_id in p.known_chunks]
            if available_peers:
                random.choice(available_peers).send_request(req.chunk_id)
            else:
                retry_list.append(req)
            if len(retry_list) > 100: break
        for r in retry_list:
            heapq.heappush(self.request_queue, r)

    def get_chunk_data(self, chunk_id):
        return self.storage.get(chunk_id)

    async def contact_tracker(self):
        while True:
            try:
                resp = requests.post(f"{self.tracker_url}/announce", json={"port": self.my_port}, timeout=5)
                if resp.status_code == 200:
                    peers_list = resp.json().get("peers", [])
                    for p_info in peers_list:
                        host, port = p_info[0], p_info[1]
                        if port != self.my_port:
                            self.loop.create_task(self.connect_to_peer(host, port))
            except: pass
            await asyncio.sleep(30)

    async def connect_to_peer(self, host, port):
        try:
            await self.loop.create_connection(lambda: PeerProtocol(self), host, port)
        except: pass

class P2PPlayer(ctk.CTk):
    def __init__(self, loop):
        super().__init__()
        self.loop = loop
        self.title("dead2player")
        self.geometry("1280x720")
        ctk.set_appearance_mode("dark")
        
        self.video_queue = asyncio.Queue()
        self.playback_buffer = {}
        self.next_chunk_id = 0
        self.is_playing = False
        self.is_recording = False
        self._current_img = None
        self.manager = None
        
        self.setup_ui()
        self.setup_menubar()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def setup_menubar(self):
        self.menubar = tk.Menu(self)
        self.config(menu=self.menubar)
        settings_menu = tk.Menu(self.menubar, tearoff=0)
        self.menubar.add_cascade(label="Настройки", menu=settings_menu)
        theme_menu = tk.Menu(settings_menu, tearoff=0)
        theme_menu.add_command(label="Светлая", command=lambda: ctk.set_appearance_mode("light"))
        theme_menu.add_command(label="Тёмная", command=lambda: ctk.set_appearance_mode("dark"))
        settings_menu.add_cascade(label="Тема", menu=theme_menu)
        self.menubar.add_command(label="Чат", command=self.toggle_chat)

    def setup_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.container = ctk.CTkFrame(self, fg_color="black")
        self.container.grid(row=0, column=0, sticky="nsew")

        self.video_canvas = tk.Label(self.container, bg="black")
        self.video_canvas.pack(side="left", expand=True, fill="both")

        self.overlay_controls = ctk.CTkFrame(self.container, fg_color="transparent")
        self.overlay_controls.place(relx=1.0, rely=0.0, anchor="ne", x=-10, y=10)

        self.url_entry = ctk.CTkEntry(self.overlay_controls, placeholder_text="Tracker URL...", width=200, height=28, fg_color="#1a1a1a", border_color="#333333")
        self.url_entry.insert(0, "http://localhost:5000")
        self.url_entry.pack(side="left", padx=5)

        self.btn_start = ctk.CTkButton(self.overlay_controls, text="START", width=60, height=28, fg_color="transparent", border_width=1, border_color="#555555", hover_color="#333333", command=self.start_stream)
        self.btn_start.pack(side="left", padx=2)

        self.btn_stop_stream = ctk.CTkButton(self.overlay_controls, text="STOP", width=60, height=28, fg_color="transparent", border_width=1, border_color="#555555", hover_color="#333333", command=self.stop_stream)
        self.btn_stop_stream.pack(side="left", padx=2)

        self.chat_frame = ctk.CTkFrame(self.container, width=0)
        self.chat_box = ctk.CTkTextbox(self.chat_frame)
        self.chat_box.pack(fill="both", expand=True, padx=5, pady=5)

        self.status_bar = ctk.CTkFrame(self, height=40)
        self.status_bar.grid(row=1, column=0, sticky="ew")

        self.btn_rec = ctk.CTkButton(self.status_bar, text="● REC", width=60, height=30, fg_color="transparent", border_width=1, command=self.toggle_record)
        self.btn_rec.pack(side="left", padx=5, pady=5)

        self.lbl_time = ctk.CTkLabel(self.status_bar, text="00:00:00")
        self.lbl_time.pack(side="left", padx=10)

        self.lbl_peers = ctk.CTkLabel(self.status_bar, text="0 peers")
        self.lbl_peers.pack(side="right", padx=10)

        self.lbl_traffic = ctk.CTkLabel(self.status_bar, text="0 / 0")
        self.lbl_traffic.pack(side="right", padx=10)

    def toggle_chat(self):
        if self.chat_frame.winfo_width() <= 1:
            self.chat_frame.pack(side="right", fill="y", padx=2)
            self.chat_frame.configure(width=300)
        else:
            self.chat_frame.pack_forget()
            self.chat_frame.configure(width=0)

    def toggle_record(self):
        self.is_recording = not self.is_recording
        self.btn_rec.configure(border_color="red" if self.is_recording else "#555555")

    def start_stream(self):
        if not self.is_playing:
            self.is_playing = True
            self.loop.create_task(self.video_engine())
            self.loop.create_task(self.run_p2p())

    def stop_stream(self):
        self.is_playing = False
        self.video_canvas.config(image='')
        self._current_img = None

    def on_close(self):
        self.is_playing = False
        self.loop.stop()
        self.destroy()

    def decode_frame(self, chunk_data):
        frames_out = []
        try:
            with av.open(BytesIO(chunk_data)) as container:
                stream = container.streams.video[0]
                fps = float(stream.average_rate or 30)
                for frame in container.decode(video=0):
                    frames_out.append((frame.to_image(), 1/fps))
        except: pass
        return frames_out

    async def video_engine(self):
        while self.is_playing:
            while not self.video_queue.empty():
                cid, data = await self.video_queue.get()
                self.playback_buffer[cid] = data
            
            if self.next_chunk_id not in self.playback_buffer:
                if self.playback_buffer:
                    max_buffered = max(self.playback_buffer.keys())
                    if max_buffered > self.next_chunk_id + 10:
                        self.next_chunk_id = max_buffered - 5
                await asyncio.sleep(0.1)
                continue

            chunk = self.playback_buffer.pop(self.next_chunk_id)
            frames = await self.loop.run_in_executor(None, self.decode_frame, chunk)
            
            for img, delay in frames:
                if not self.is_playing: break
                t_start = time.perf_counter()
                w, h = self.video_canvas.winfo_width(), self.video_canvas.winfo_height()
                if w > 10 and h > 10:
                    img = img.resize((w, h), Image.Resampling.BILINEAR)
                photo = ImageTk.PhotoImage(img)
                self.video_canvas.config(image=photo)
                self._current_img = photo
                await asyncio.sleep(max(0, delay - (time.perf_counter() - t_start)))
            self.next_chunk_id += 1

    async def run_p2p(self):
        tracker_url = self.url_entry.get()
        self.manager = SwarmManager(self.video_queue, self.loop, tracker_url)
        self.loop.create_task(self.manager.contact_tracker())
        try:
            server = await self.loop.create_server(lambda: PeerProtocol(self.manager), '0.0.0.0', 8888)
            async with server:
                while self.is_playing:
                    needed = {self.next_chunk_id + i: time.time() + (i * 0.4) for i in range(50)}
                    self.manager.set_needed_chunks(needed)
                    self.manager.manage_choking()
                    self.manager.process_requests()
                    self.lbl_peers.configure(text=f"{len(self.manager.peers)} peers")
                    self.lbl_traffic.configure(text=f"S: {len(self.manager.storage)} | C: {self.next_chunk_id}")
                    await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"P2P Error: {e}")

async def main():
    loop = asyncio.get_running_loop()
    app = P2PPlayer(loop)
    while True:
        try:
            app.update()
            await asyncio.sleep(0.01)
        except: break

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
        