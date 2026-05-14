using System;
using System.Collections.Generic;

namespace Crasher;

internal static class Program
{
    private static readonly List<byte[]> KeepAlive = new();

    private static void Main(string[] args)
    {
        Console.WriteLine($"PID {Environment.ProcessId} — allocating some objects, then throwing.");

        for (var i = 0; i < 100; i++)
        {
            KeepAlive.Add(new byte[64 * 1024]);
        }

        var customers = new List<Customer>
        {
            new("Ada Lovelace", 1815),
            new("Alan Turing", 1912),
            new("Grace Hopper", 1906),
        };

        DoWork(customers, null!);
    }

    private static void DoWork(List<Customer> customers, string mandatoryTag)
    {
        if (mandatoryTag is null)
        {
            throw new InvalidOperationException(
                $"mandatoryTag must not be null (customers.Count={customers.Count}).");
        }

        Console.WriteLine(mandatoryTag);
    }

    private sealed record Customer(string Name, int BirthYear);
}
