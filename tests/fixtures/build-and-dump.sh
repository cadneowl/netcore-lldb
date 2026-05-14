#!/bin/bash
# Build each fixture and run it under DOTNET_DbgEnableMiniDump=1 so we capture
# a real .NET core dump of the pathological state. The dump is moved into
# tests/dumps/<fixture-name>.dmp for the test suite to load.
#
# Idempotent: if the dump already exists AND the source hasn't changed since
# the dump was written, skips the rebuild. Otherwise rebuilds and re-dumps.
set -euo pipefail

cd "$(dirname "$0")"
FIXTURES_DIR="$(pwd)"
ROOT="$FIXTURES_DIR/.."
DUMPS_DIR="$ROOT/dumps"
mkdir -p "$DUMPS_DIR"

# Force core dumps from this shell so the .NET runtime can write them.
ulimit -c unlimited

build_and_dump() {
    local name="$1"
    local dir="$FIXTURES_DIR/$name"
    local dump_target="$DUMPS_DIR/$name.dmp"

    if [ ! -d "$dir" ]; then
        echo "[fixtures] skipping $name (no directory)"
        return
    fi

    # Skip if dump is newer than every Program.cs / csproj in the fixture.
    if [ -f "$dump_target" ]; then
        local stale=0
        while IFS= read -r src; do
            if [ "$src" -nt "$dump_target" ]; then stale=1; break; fi
        done < <(find "$dir" -type f \( -name '*.cs' -o -name '*.csproj' \))
        if [ "$stale" = 0 ]; then
            echo "[fixtures] $name: up to date ($(basename "$dump_target"))"
            return
        fi
    fi

    echo "[fixtures] $name: building..."
    (
        cd "$dir"
        dotnet publish -c Debug -o "out" --nologo -v q
    )

    # Find the produced executable (.csproj's AssemblyName).
    local exe
    exe=$(find "$dir/out" -maxdepth 1 -type f -executable ! -name '*.dll' ! -name '*.pdb' ! -name '*.json' | head -1)
    if [ -z "$exe" ]; then
        echo "[fixtures] $name: no executable produced — skipping" >&2
        return
    fi

    # Clean any previous attempt's dump artifacts in the fixture dir.
    rm -f "$dir"/*.dmp

    echo "[fixtures] $name: running to generate dump..."
    set +e
    DOTNET_DbgEnableMiniDump=1 \
    DOTNET_DbgMiniDumpType=4 \
    DOTNET_DbgMiniDumpName="$dir/$name.%p.dmp" \
    DOTNET_EnableDiagnostics=1 \
        "$exe" > "$dir/run.stdout" 2> "$dir/run.stderr"
    set -e

    # Pick up the most recent dump the runtime wrote.
    local produced
    produced=$(ls -t "$dir"/*.dmp 2>/dev/null | head -1)
    if [ -z "$produced" ]; then
        echo "[fixtures] $name: NO DUMP produced. stderr:" >&2
        sed 's/^/  /' "$dir/run.stderr" >&2
        return 1
    fi
    mv "$produced" "$dump_target"
    echo "[fixtures] $name: wrote $(basename "$dump_target") ($(stat -c%s "$dump_target") bytes)"
}

build_and_dump memory-leak
build_and_dump deadlock
build_and_dump event-handler-leak
