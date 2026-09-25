---
paths:
  - "agents/skills/**/*"
---

A skill under `agents/skills/` is read only by the deployed agent, on every run that loads it, so every token is paid
per session. Write for that model:

- Keep a line only if it changes what the agent does and the agent cannot learn it at runtime. It reads the protos,
  module docstrings, `help()`, tool output and library errors itself: point at them once, never restate them.
- No background, history or motivation it cannot act on: release dates, why a service was built, what something used to
  do.
- One rule, one place: state it where the workflow first needs it, in the imperative, naming the exact call, field or
  path.
- A line that steers around a defect in our code is a defect to fix in the code, not a line to add.
- `test_skill_calls` holds rpc and accessor names to the guest surface. Check every other claim against the code before
  committing.
