I deliberately overloaded a Postgres database until it started rejecting connections. Then I put PGBouncer in front of it and gave it the exact same overload.

The database stopped rejecting anything.

I ran a controlled POC in Kubernetes: two identical, isolated PostgreSQL instances (16, 1 CPU core, max_connections=100) — one taking direct application connections, one sitting behind PGBouncer (transaction pooling). Same workload generator, same mixed 80/20 read/write traffic, same duration, hitting both paths independently so neither test could skew the other.

Two load levels, same story both times:

→ 50 concurrent clients (under the connection limit):
• +46% throughput via PGBouncer (9,517 tps vs 6,516 tps)
• p95 latency down 61% (32.96ms → 12.69ms)
• p99 latency down 50% (51.89ms → 25.73ms)
• Connection setup cost down 49%

→ 120 concurrent clients (deliberately over the 100-connection limit):
• Direct connections: exactly 20 hard connection failures — precisely the overshoot past max_connections
• Via PGBouncer: 0 errors, same overload fully absorbed by pooling + queuing
• Still +22% throughput, p99 latency down 32%

The most counterintuitive part: PGBouncer won on throughput even when the database wasn't even close to its connection limit. With everything capped at a single CPU core, running fewer, well-managed real backend connections was measurably faster than throwing more raw connections at Postgres — not just safer.

Connection pooling isn't just an overload safety net. It's a genuine performance lever, with numbers to back it.

Full breakdown — architecture, methodology, all five findings, and the complete raw data — in the linked article. 👇

#PostgreSQL #PGBouncer #DatabasePerformance #Kubernetes #SRE #DevOps #backend
