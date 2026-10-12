import sys
import numpy as np

print("Python server started", file=sys.stderr, flush=True)

for line in sys.stdin:
    parts = line.strip().split()

    if not parts:
        print("ERROR: empty command", flush=True)
        continue

    command, *args = parts

    try:
        if command == "sum":
            numbers = np.array([float(x) for x in args])
            result = np.sum(numbers)

        elif command == "mean":
            numbers = np.array([float(x) for x in args])
            result = np.mean(numbers)

        elif command == "pid":
            import os
            result = os.getpid()

        elif command == "quit" or command == "exit":
            pid = os.getpid()
            print(f"EXIT: {pid} PID", flush=True)
            break

        else:
            result = "ERROR: unknown command"

        print(result, flush=True)

    except Exception as e:
        print(f"ERROR: {e}", flush=True)
