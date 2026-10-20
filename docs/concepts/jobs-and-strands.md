# Jobs and strands

## Code memory

Every loaded program is compiled into a region of one shared instruction table. A job is only
its strands' instruction addresses and its value slots. Jobs of different programs share the
pool, the lanes and the dispatcher. A program can be loaded while the machine runs; regions are
never moved, so running jobs are not affected.

## Rename, window, retire

- **Fetch.** A strand fetches in program order and renames every write to a fresh slot; reads
  bind to the slot of the last write. Every statement gets an order key, the same whatever the
  timing, so one job always has one sequential order.
- **Issue.** A statement issues as soon as its inputs are filled. Inline nodes run on the
  dispatcher; store hits complete at once; the rest go to their lane.
- **Window.** `window` caps a job's unfinished statements and `retire_buffer` its finished ones
  waiting to retire. The strand holding the job's oldest unfetched statement may always fetch,
  so a job never deadlocks at a cap.
- **Retire.** Statements retire in program order. A failure is taken only when it reaches the
  retire position:
  - every younger statement is squashed;
  - inside a `@try` the job continues at the handler with the failing statement's view;
  - outside one the job fails.

## Branches without guessing

When a branch's condition is still being computed, the strand parks at the branch. A second
strand starts at the point where the branch's paths rejoin, reading phis for the names the
branch region may write. Work after the join that does not read those names runs at once.
Nothing runs on a path the sequential run would not take.

## `each` as strands

A loop body without an exit runs one strand per item, started in index order as the window
allows. Each iteration reads the names it carries from its predecessor through phis, so only
statements that read carried names wait on each other. A body that can leave the loop iterates
along one strand's program counter.

## Effects

- An `ordered` node runs only when every older statement of its job has finished without
  failure and nothing older can still be fetched: exactly when the sequential run would reach
  it.
- `ordered` orders one job's effects only. All state the machine guarantees is in the jobs'
  values; state an engine or node keeps across calls, such as one shared file, is its own,
  and so are its effect on results and its locking.
- A `mutates` node changes its input in place. Older readers keep the value as it was; younger
  readers, through any alias, see the change. While older work may still fail, the node gets a
  copy (the `mutates` knob chooses copy, wait, or always copy).
