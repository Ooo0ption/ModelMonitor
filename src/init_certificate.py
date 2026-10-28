"""Initialize mitmproxy's own CA without opening a proxy listener."""
from mitmproxy import ctx


def running():
    ctx.master.shutdown()
