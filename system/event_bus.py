"""Event Bus for async messaging."""

import asyncio
from typing import Callable, Dict, List, Any, Awaitable

EventHandler = Callable[[dict], Awaitable[None]]


class EventBus:
    """Simple in-memory event bus."""

    def __init__(self):
        self._subscribers: Dict[str, List[EventHandler]] = {}

    def subscribe(self, event_type: str, handler: EventHandler):
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append(handler)

    async def publish(self, event: dict):
        event_type = event.get("type", "unknown")
        handlers = self._subscribers.get(event_type, [])
        for handler in handlers:
            try:
                await handler(event)
            except Exception as e:
                print(f"ERROR: Event handler failed: {e}")


# Global instance
_BUS = EventBus()


def get_event_bus() -> EventBus:
    return _BUS
