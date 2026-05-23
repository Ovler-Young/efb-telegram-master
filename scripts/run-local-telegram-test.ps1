param(
    [string]$Expression = "TextMessageFactory",
    [string]$BaseTemp = "C:\tmp\pytest-efb-local",
    [int]$DumpTracebackAfter = 0,
    [string]$TracebackPath = ""
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

$runner = Join-Path $env:TEMP "efb-pytest-runner-$PID.py"
$runnerContent = @(
    "import asyncio",
    "import faulthandler",
    "import pathlib",
    "import runpy",
    "import sys",
    "import threading",
    "import traceback",
    "",
    "asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())",
    "dump_after = int(r'$DumpTracebackAfter')",
    "if dump_after > 0:",
    "    faulthandler.dump_traceback_later(dump_after, repeat=True)",
    "traceback_path = r'$TracebackPath'",
    "if dump_after > 0 and traceback_path:",
    "    def dump_stacks():",
    "        frames = sys._current_frames()",
    "        lines = []",
    "        for thread in threading.enumerate():",
    "            lines.append(f'--- thread {thread.name} ident={thread.ident} daemon={thread.daemon} ---')",
    "            frame = frames.get(thread.ident)",
    "            if frame is not None:",
    "                lines.extend(traceback.format_stack(frame))",
    "        pathlib.Path(traceback_path).write_text(''.join(lines), encoding='utf-8')",
    "    timer = threading.Timer(dump_after, dump_stacks)",
    "    timer.daemon = True",
    "    timer.start()",
    "sys.argv = [",
    "    'pytest',",
    "    'tests/integration/test_master_message.py::test_master_message',",
    "    '-k',",
    "    r'$Expression',",
    "    '-vv',",
    "    '-r',",
    "    'a',",
    "    '--tb=short',",
    "    '--show-capture=no',",
    "    '--color=yes',",
    "    '--mode=integration',",
    "    '--basetemp',",
    "    r'$BaseTemp',",
    "]",
    "runpy.run_module('pytest', run_name='__main__')"
) -join [Environment]::NewLine

try {
    Set-Content -LiteralPath $runner -Value $runnerContent -Encoding UTF8
    & ".venv\Scripts\python.exe" $runner
    exit $LASTEXITCODE
}
finally {
    Remove-Item -LiteralPath $runner -ErrorAction SilentlyContinue
}
