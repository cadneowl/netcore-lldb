using System;
using System.Collections.Generic;

namespace EventHandlerLeakFixture;

/// <summary>
/// Fixture: an event-handler memory leak — the textbook one Tess names in
/// several case studies. A long-lived static Publisher has an event; we create
/// many short-lived Subscriber instances and `+=` their handler onto the
/// event. The local `sub` reference goes out of scope, but the publisher's
/// invocation list still pins them via the delegate's `Target` reference.
///
/// We force a GC to prove the subscribers are NOT collected (they would be if
/// the event subscription didn't hold them), then throw to capture state.
///
/// Expected SOS findings:
///   - `dumpheap -stat` shows EventHandlerLeakFixture.Subscriber instances
///     and many EventHandler/MulticastDelegate objects.
///   - `gcroot <addr-of-a-Subscriber>` traces back through
///     System.EventHandler / System.Object[] / Publisher.Tick → static
///     Program.SharedPublisher.
/// </summary>
internal static class Program
{
    private static readonly Publisher SharedPublisher = new();

    private static void Main()
    {
        Console.WriteLine($"PID {Environment.ProcessId} — staging event-handler leak.");

        for (var i = 0; i < 100; i++)
        {
            var sub = new Subscriber(i);
            SharedPublisher.Tick += sub.OnTick;
            // intentionally drop the reference so it'd be eligible for GC if
            // the event subscription didn't pin it
        }

        GC.Collect();
        GC.WaitForPendingFinalizers();
        GC.Collect();

        Console.WriteLine(
            "Created 100 Subscriber instances; all still rooted via "
            + "Publisher.Tick despite no local references and a full GC.");

        throw new InvalidOperationException(
            "100 Subscriber objects are retained by SharedPublisher.Tick "
            + "(the event invocation list pins each subscriber).");
    }
}

internal sealed class Publisher
{
    public event EventHandler? Tick;

    public void Fire() => Tick?.Invoke(this, EventArgs.Empty);
}

internal sealed class Subscriber
{
    private readonly int _id;
    private readonly byte[] _payload;

    public Subscriber(int id)
    {
        _id = id;
        // 4 KB each so they show up as a real payload in dumpheap, but stay off
        // the Large Object Heap (so this fixture exercises SOH retention).
        _payload = new byte[4096];
    }

    public void OnTick(object? sender, EventArgs e)
    {
        // Touch _id so the compiler doesn't optimise the field away.
        if (_id < 0) throw new Exception();
    }
}
