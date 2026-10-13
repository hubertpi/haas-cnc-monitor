"""Loopback-only development MQTT broker; never required by the add-on."""
import asyncio
from amqtt.broker import Broker

async def main():
    broker = Broker({"listeners": {"default": {"type": "tcp", "bind": "127.0.0.1:18883"}},
                     "plugins": {"amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True}}})
    await broker.start()
    try:
        await asyncio.Event().wait()
    finally:
        await broker.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
