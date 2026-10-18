"""Carry independent WebSocket receipt timing beside legacy Engine timestamps."""


class EventReceipts(dict):
    def __init__(self, values=(), *, websocket=None):
        super().__init__(values)
        self.websocket = dict(websocket or {})
