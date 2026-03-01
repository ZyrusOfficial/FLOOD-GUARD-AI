import sys
import os
import asyncio
import threading
import logging

# Add bitchat-python to path
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'bitchat-python'))

from bitchat import BitchatClient

class BitchatBridge:
    def __init__(self, nickname="HYDROGUARD_NODE"):
        self.client = BitchatClient()
        self.client.nickname = nickname
        self.loop = None
        self.thread = None
        self.is_connected = False
        self._logger = logging.getLogger("BitchatBridge")

    def start(self):
        """Start the BitChat client in a background thread."""
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        
    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._connect_and_run())

    async def _connect_and_run(self):
        try:
            self._logger.info("Connecting to BitChat network...")
            await self.client.connect()
            self.is_connected = True
            # Keep the loop alive
            while True:
                await asyncio.sleep(1)
        except Exception as e:
            self._logger.error(f"BitChat bridge error: {e}")
            self.is_connected = False

    def send_message(self, content):
        """Programmatically send a message to public chat."""
        if not self.is_connected:
            self._logger.warning("Bitchat not connected. Message queued or dropped.")
            # We could implement a small queue here if needed
        
        if self.loop:
            asyncio.run_coroutine_threadsafe(
                self.client.send_public_message(content), 
                self.loop
            )

    def send_private(self, content, peer_id, nickname):
        """Programmatically send a private message."""
        if self.loop:
            asyncio.run_coroutine_threadsafe(
                self.client.send_private_message(content, peer_id, nickname),
                self.loop
            )
