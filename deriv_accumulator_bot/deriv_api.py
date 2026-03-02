import asyncio
import websockets
import json

class DerivAPI:
    """A production-safe async wrapper for the Deriv WebSocket API."""
    
    def __init__(self, app_id: str, token: str, rate_limit_per_sec: float = 5.0):
        self.app_id = app_id
        self.token = token
        self.ws_url = f"wss://ws.derivws.com/websockets/v3?app_id={self.app_id}"
        self.ws = None

        # Traffic Control
        self._req_counter = 0

        # State Management for Auto-Healing
        self.active_subscriptions = {}
        self.subscription_ids = {}  # Map sub_type to subscription id

        # Rate limit: token bucket
        self._rate_limit_per_sec = rate_limit_per_sec
        self._last_send_time = 0.0
        self._tokens = rate_limit_per_sec
        self._last_token_time = time.time()

        # Exponential backoff state
        self._reconnect_attempts = 0

        # Message router handlers
        self.handlers = {}

    def _next_req_id(self) -> int:
        self._req_counter += 1
        return self._req_counter

    def register_handler(self, msg_type: str, handler):
        """Register a handler for a message type."""
        self.handlers[msg_type] = handler

    async def connect(self) -> float:
        """Connects, authenticates, and returns account balance."""
        self.ws = await websockets.connect(self.ws_url, ping_interval=20, ping_timeout=20)
        self._req_counter = 0
        self.subscription_ids.clear()
        # Do NOT clear active_subscriptions: we want to restore them after reconnect

        auth_req_id = self._next_req_id()
        auth_payload = {"authorize": self.token, "req_id": auth_req_id}
        await self.ws.send(json.dumps(auth_payload))

        while True:
            response = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=10))
            if response.get("req_id") == auth_req_id:
                if "error" in response:
                    raise Exception(f"Authentication Failed: {response['error']['message']}")
                return response["authorize"]["balance"]

    async def send(self, payload: dict) -> int:
        """Injects req_id, tracks subscriptions, and sends payload. Rate limited."""
        # Rate limit: token bucket
        now = time.time()
        elapsed = now - self._last_token_time
        self._tokens = min(self._rate_limit_per_sec, self._tokens + elapsed * self._rate_limit_per_sec)
        self._last_token_time = now
        if self._tokens < 1:
            await asyncio.sleep(1.0 / self._rate_limit_per_sec)
            return await self.send(payload)
        self._tokens -= 1

        if not self.ws or self.ws.closed:
            await self.reconnect()

        req_id = self._next_req_id()
        payload["req_id"] = req_id

        # Track subscriptions for auto-heal
        if payload.get("subscribe") == 1:
            if "ticks" in payload:
                self.active_subscriptions["ticks"] = payload.copy()
            elif "proposal_open_contract" in payload:
                self.active_subscriptions["proposal_open_contract"] = payload.copy()

        await self.ws.send(json.dumps(payload))
        return req_id

    async def recv(self, timeout: int = 15) -> dict:
        """Waits for a message, routes it, and auto-reconnects with exponential backoff."""
        while True:
            try:
                if not self.ws or self.ws.closed:
                    await self.reconnect()

                message = await asyncio.wait_for(self.ws.recv(), timeout=timeout)
                msg = json.loads(message)

                # Centralized message router
                msg_type = msg.get("msg_type")
                if msg_type and msg_type in self.handlers:
                    await self.handlers[msg_type](msg)

                return msg

            except (asyncio.TimeoutError, websockets.ConnectionClosed):
                print("⚠️ Transport layer dropped or timed out. Initiating auto-heal...")
                await self.reconnect()

    async def reconnect(self):
        """Automatic Reconnect Logic (Self-Healing) with exponential backoff."""
        if self.ws:
            await self.disconnect()

        # Exponential backoff: up to 60s
        delay = min(2 ** self._reconnect_attempts, 60)
        print(f"Reconnect attempt {self._reconnect_attempts + 1}, sleeping {delay}s...")
        await asyncio.sleep(delay)

        try:
            await self.connect()
            print("✅ Transport layer healed. Re-authenticated.")
            self._reconnect_attempts = 0

            # Resubscribe with new req_id and confirm
            for sub_type, sub_payload in self.active_subscriptions.items():
                print(f"🔄 Restoring {sub_type} subscription...")
                req_id = await self.send(sub_payload.copy())
                # Wait for confirmation
                confirmed = False
                for _ in range(5):
                    msg = json.loads(await self.ws.recv())
                    if msg.get("req_id") == req_id and "subscription" in msg:
                        self.subscription_ids[sub_type] = msg["subscription"]["id"]
                        confirmed = True
                        break
                if not confirmed:
                    print(f"⚠️ Subscription {sub_type} not confirmed after reconnect.")

        except Exception as e:
            print(f"❌ Reconnect sequence failed: {e}")
            self._reconnect_attempts += 1
            await asyncio.sleep(delay)

    async def disconnect(self):
        """Safely closes the connection."""
        if self.ws and not self.ws.closed:
            await self.ws.close()