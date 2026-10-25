import time

from textkit.primes import count_primes

best = float("inf")
for _ in range(3):
    start = time.perf_counter()
    count_primes(200_000)
    best = min(best, time.perf_counter() - start)
print(f"seconds: {best:.4f}")
