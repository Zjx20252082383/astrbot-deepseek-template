"""Load the plugin in an isolated real AstrBot instance without QQ or LLMs."""

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def main():
    source = Path(__file__).resolve().parents[1] / "astrbot_plugin_shihai_demo"
    temporary = tempfile.TemporaryDirectory(prefix="shihai-astrbot-smoke-")
    root = Path(temporary.name).resolve()
    process = None
    output = None
    try:
        (root / ".astrbot").touch()
        plugin = root / "data" / "plugins" / source.name
        shutil.copytree(source, plugin, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        with socket.socket() as port_probe:
            port_probe.bind(("127.0.0.1", 0))
            port = port_probe.getsockname()[1]
        configuration = {
            "dashboard": {"host": "127.0.0.1", "port": port},
            "platform": [],
            "provider": [],
            "provider_sources": [],
        }
        (root / "data" / "cmd_config.json").write_text(
            json.dumps(configuration), encoding="utf-8"
        )
        environment = dict(os.environ)
        environment["ASTRBOT_ROOT"] = str(root)
        environment["ASTRBOT_DASHBOARD_INITIAL_PASSWORD"] = secrets.token_urlsafe(24)
        environment["PYTHONIOENCODING"] = "utf-8"
        output = (root / "smoke.log").open("w", encoding="utf-8")
        process = subprocess.Popen(
            [sys.executable, "-B", "-m", "astrbot.cli", "run", "--port", str(port)],
            cwd=root,
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            log = (root / "smoke.log").read_text(encoding="utf-8", errors="replace")
            if "[识海Demo] 已启动" in log:
                try:
                    with opener.open(f"http://127.0.0.1:{port}/", timeout=1) as response:
                        if response.status == 200:
                            print(json.dumps({
                                "plugin_loaded": True,
                                "dashboard_http_status": response.status,
                                "qq_platforms_enabled": 0,
                                "model_providers_configured": 0,
                            }))
                            return
                except (OSError, urllib.error.URLError):
                    pass
            if process.poll() is not None:
                break
            time.sleep(0.25)
        # Never print the generated dashboard password or authentication lines.
        lines = (root / "smoke.log").read_text(encoding="utf-8", errors="replace").splitlines()
        safe = [line for line in lines if not any(
            word in line.lower() for word in ("password", "密码", "token", "secret", "api_key")
        )]
        print("\n".join(safe[-45:]), file=sys.stderr)
        raise RuntimeError("AstrBot did not finish loading the demo in 45 seconds")
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if output is not None:
            output.close()
        # Delete only this explicitly named test runtime, never the user's runtime.
        if root.parent != Path(tempfile.gettempdir()).resolve() or not root.name.startswith("shihai-astrbot-smoke-"):
            raise RuntimeError("Unexpected temporary runtime path; cleanup refused")
        temporary.cleanup()


if __name__ == "__main__":
    main()
