using System;
using System.Threading;

namespace DeadlockFixture;

/// <summary>
/// Fixture: a textbook lock-order inversion deadlock between two worker threads.
/// Thread A takes LockA then tries LockB. Thread B takes LockB then tries LockA.
/// Both sleep briefly between acquisitions to guarantee the interleaving.
///
/// The main thread waits two seconds for them to deadlock, then throws to
/// trigger DOTNET_DbgEnableMiniDump. The dump captures all three threads:
/// main mid-throw, A waiting on LockB while holding LockA, B waiting on LockA
/// while holding LockB.
///
/// Expected SOS findings:
///   - `clrthreads` shows three managed threads (main + two workers).
///   - `syncblk` reports two sync blocks each with MonitorHeld=3 (1 + 2*waiters)
///     and an owning OSID, exposing the deadlock.
///   - On the worker threads, `clrstack` shows Monitor.ReliableEnter inside
///     DeadlockFixture.Program.Worker[AB].
/// </summary>
internal static class Program
{
    private static readonly object LockA = new();
    private static readonly object LockB = new();

    private static void Main()
    {
        Console.WriteLine($"PID {Environment.ProcessId} — staging two-thread lock-order inversion.");

        var workerA = new Thread(WorkerA) { Name = "Worker-A", IsBackground = true };
        var workerB = new Thread(WorkerB) { Name = "Worker-B", IsBackground = true };
        workerA.Start();
        workerB.Start();

        Thread.Sleep(2000);

        throw new TimeoutException(
            "Workers are deadlocked: A holds LockA waiting for LockB, "
            + "B holds LockB waiting for LockA.");
    }

    private static void WorkerA()
    {
        lock (LockA)
        {
            Thread.Sleep(300);
            lock (LockB) { Console.WriteLine("A done"); }
        }
    }

    private static void WorkerB()
    {
        lock (LockB)
        {
            Thread.Sleep(300);
            lock (LockA) { Console.WriteLine("B done"); }
        }
    }
}
