@echo off
rem Thin wrapper for netcore_lldb.py.
rem Put this directory on PATH and use as the `command` in Claude Code's .mcp.json.
python "%~dp0netcore_lldb.py" %*
