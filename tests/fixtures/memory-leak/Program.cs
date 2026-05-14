using System;
using System.Collections.Generic;

namespace MemoryLeakFixture;

/// <summary>
/// Fixture: a classic static-collection memory leak. We allocate 50 byte arrays
/// of 256 KB each (~12.8 MB total) into a static list. The list stays rooted on
/// the type, so the byte[] instances are never collected. Then we throw an
/// unhandled exception to trigger DOTNET_DbgEnableMiniDump and capture the
/// leaked state.
///
/// Expected SOS findings on the resulting dump:
///   - `dumpheap -stat` shows `System.Byte[]` near the top with ~50 instances
///     totalling ~12.8 MB.
///   - `gcroot <addr-of-a-byte-array>` resolves to
///         MemoryLeakFixture.LeakHolder.RetainedAllocations  (a static field).
///   - This is Tess's "not a leak, retention by design" pattern.
/// </summary>
internal static class Program
{
    private static void Main()
    {
        Console.WriteLine($"PID {Environment.ProcessId} — staging static-collection leak.");

        for (var i = 0; i < 50; i++)
        {
            LeakHolder.RetainedAllocations.Add(new byte[256 * 1024]);
        }

        Console.WriteLine(
            $"Allocated {LeakHolder.RetainedAllocations.Count} byte[]s totalling "
            + $"~{LeakHolder.RetainedAllocations.Count * 256} KB. Throwing now.");

        throw new InvalidOperationException(
            "Simulated static-collection retention: 50 × 256 KB byte[] arrays "
            + "are held by MemoryLeakFixture.LeakHolder.RetainedAllocations.");
    }
}

internal static class LeakHolder
{
    public static readonly List<byte[]> RetainedAllocations = new();
}
