# Proof

Every node call of a sampled job is recorded in retire order, which is the job's sequential
order. `kohakunode`'s interpreter then runs the same job with a hook that checks each call's
node and input digest against the record:

- **replay** returns the recorded result, so the check holds for any node;
- **re-execution** runs pure nodes again and compares their results, so a result delivered to
  the wrong statement or job is caught too.

Outputs, or failure kind and node, must be equal. Generated programs on simulated engines with
random latencies, unit outages, injected failures, every selector, both forming modes, several
programs per machine, snapshots and stores run through the same check. See the
[conformance guide](../guides/conformance.md).
