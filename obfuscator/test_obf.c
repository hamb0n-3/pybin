/* Correctness harness for the obfuscation plugin. Exercises arithmetic, loops,
 * nested branches, function calls and string constants. Build it twice (with
 * and without the plugin) and diff the output — they must match. The marker
 * strings must NOT appear in `strings` of the obfuscated build. */
#include <stdio.h>
#include <string.h>

static int fib(int n) {
  int a = 0, b = 1;
  for (int i = 0; i < n; i++) {
    int t = a + b;
    a = b;
    b = t;
  }
  return a;
}

static int collatz(int n) {
  int steps = 0;
  while (n > 1) {
    if (n & 1)
      n = 3 * n + 1;
    else
      n = n / 2;
    steps++;
  }
  return steps;
}

static unsigned mix(unsigned x) {
  x ^= x << 13;
  x *= 0x9E3779B1u;
  x &= 0x7fffffffu;
  x |= 1u;
  return x - 7u;
}

/* Entry block ends in a conditional branch -> exercises the flattener's
 * entry-split path (functions whose entry falls straight into an `if`). */
static int classify(int x) {
  if (x > 100)
    return 3;
  if (x > 10)
    return 2;
  if (x < 0)
    return -1;
  return 1;
}

int main(int argc, char **argv) {
  const char *secret = "MARKER_TOP_SECRET_9F3A_DO_NOT_LEAK";
  puts("MARKER_HELLO from the test binary");
  printf("fib(20)=%d\n", fib(20));
  printf("collatz(27)=%d\n", collatz(27));
  printf("mix(12345)=%u\n", mix(12345));
  for (int v = -5; v <= 200; v += 41)
    printf("classify(%d)=%d\n", v, classify(v));

  int sum = 0;
  for (int i = 0; i < argc; i++)
    sum += (int)strlen(argv[i]);
  printf("argc=%d arglen=%d parity=%s\n", argc, sum,
         (sum % 2 == 0) ? "even" : "odd");
  printf("secret-len=%zu\n", strlen(secret));
  return 0;
}
