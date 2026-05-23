param(
    [string]$Expression = "TextMessageFactory",
    [string]$BaseTemp = "C:\tmp\pytest-efb-local"
)

$ErrorActionPreference = "Stop"

$env:ALL_PROXY = "socks5://127.0.0.1:7890"
$env:all_proxy = "socks5://127.0.0.1:7890"
Remove-Item Env:HTTPS_PROXY -ErrorAction SilentlyContinue
Remove-Item Env:https_proxy -ErrorAction SilentlyContinue
Remove-Item Env:HTTP_PROXY -ErrorAction SilentlyContinue
Remove-Item Env:http_proxy -ErrorAction SilentlyContinue
Remove-Item Env:PROXY_ALL -ErrorAction SilentlyContinue
Remove-Item Env:proxy_all -ErrorAction SilentlyContinue
$env:TMP = "C:\tmp"
$env:TEMP = "C:\tmp"

& ".venv\Scripts\python.exe" -c @"
import asyncio
import runpy
import sys

asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
sys.argv = [
    "pytest",
    "tests/integration/test_master_message.py::test_master_message",
    "-k",
    r"$Expression",
    "-vv",
    "-r",
    "a",
    "--tb=short",
    "--show-capture=no",
    "--color=yes",
    "--mode=integration",
    "--basetemp",
    r"$BaseTemp",
]
runpy.run_module("pytest", run_name="__main__")
"@
