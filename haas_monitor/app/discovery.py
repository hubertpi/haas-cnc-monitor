"""User-started, bounded TCP discovery. An open port is not a HAAS identity."""
import asyncio
import ipaddress


async def port_open(host, port):
    writer = None
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 0.5)
        return True
    except (OSError, asyncio.TimeoutError):
        return False
    finally:
        if writer:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


class Discovery:
    def __init__(self, simulation=False, probe=port_open):
        self.simulation, self.probe = simulation, probe
        self.task = None
        self.state = {"running": False, "checked": 0, "total": 0, "candidates": []}

    def view(self,records=()):
        configured={(item["host"],item["port"]) for item in records}
        candidates=[c for c in self.state["candidates"] if (c["host"],c["port"]) not in configured]
        return {**self.state,"candidates":candidates,"already_configured":self.state.get("already_configured",0)+len(self.state["candidates"])-len(candidates)}

    def start(self, network, port, records=()):
        if self.task and not self.task.done():
            raise ValueError("A discovery scan is already running")
        hosts = list(ipaddress.ip_network(network).hosts())
        self.state = {"running": True, "checked": 0, "total": len(hosts),
                      "network": network, "port": port, "simulation": self.simulation,
                      "candidates": [], "cancelled": False, "error": None}
        self.excluded={(item["host"],item["port"]) for item in records}
        self.state["already_configured"]=0
        self.task = asyncio.create_task(self.scan(hosts, port))

    async def scan(self, hosts, port):
        queue = asyncio.Queue()
        for host in hosts:
            queue.put_nowait(str(host))
        async def worker():
            while not queue.empty():
                host = queue.get_nowait()
                if (host,port) in self.excluded:
                    self.state["checked"]+=1
                    self.state["already_configured"]+=1
                    continue
                if self.simulation:
                    await asyncio.sleep(0)
                    found = host in {str(h) for h in hosts[:2]}
                else:
                    found = await self.probe(host, port)
                if found:
                    self.state["candidates"].append({"host": host, "port": port})
                self.state["checked"] += 1
        workers = [asyncio.create_task(worker()) for _ in range(min(16, len(hosts)))]
        try:
            await asyncio.gather(*workers)
        except asyncio.CancelledError:
            self.state["cancelled"] = True
            raise
        except Exception:
            self.state["error"] = "Discovery failed; check network access"
        finally:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            self.state["candidates"].sort(key=lambda c: int(ipaddress.ip_address(c["host"])))
            self.state["running"] = False

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
