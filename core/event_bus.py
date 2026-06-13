import asyncio
import logging
from enum import Enum
from typing import Dict, List, Callable, Any

class EventType(Enum):
    TICK = "TICK"
    SIGNAL = "SIGNAL"
    ORDER_UPDATE = "ORDER_UPDATE"
    ERROR = "ERROR"

class Event:
    def __init__(self, type: EventType, payload: Any):
        self.type = type
        self.payload = payload

class EventBus:
    def __init__(self):
        self.subscribers: Dict[EventType, List[Callable]] = {
            event_type: [] for event_type in EventType
        }
        self.queue = asyncio.Queue()
        self.running = False

    def subscribe(self, event_type: EventType, callback: Callable):
        """Subscribes a callback function to an event type."""
        if event_type not in self.subscribers:
            self.subscribers[event_type] = []
        self.subscribers[event_type].append(callback)
        logging.info(f"Subscribed to {event_type.name}")

    async def publish(self, event: Event):
        """Publishes an event to the queue."""
        await self.queue.put(event)

    async def run(self):
        """Main loop to process events."""
        self.running = True
        logging.info("EventBus started.")
        while self.running:
            try:
                event = await self.queue.get()
                if event.type in self.subscribers:
                    for callback in self.subscribers[event.type]:
                        try:
                            # If callback is async, await it
                            if asyncio.iscoroutinefunction(callback):
                                await callback(event)
                            else:
                                callback(event)
                        except Exception as e:
                            logging.error(f"Error in subscriber for {event.type.name}: {e}")
                self.queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.error(f"Error processing event: {e}")

    def stop(self):
        self.running = False
