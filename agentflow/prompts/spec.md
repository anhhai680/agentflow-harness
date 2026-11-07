Goal: draft `{spec}` for module `{module}`.

Read, by path:
- The work source, if one is configured (`{capability_map}`): the `{module}` row (scope, dependencies, build order).
- The idea doc, if one is configured (`{idea}`): `grep -n '^## '` first, then only the sections the module touches. Never read it whole.
- The decision records that the module touches, and one finished spec of another module for the format, if there is one.

If `{run_dir}/task.md` exists, it is the developer's description of this module: build the spec on it. Put its `Depends on:` line in the spec's header, above the first `## ` heading, as `**Depends on:** <ids>` (or `**Depends on:** none`).

If `{questions}` exists, it holds a human's answers to the idea questions for this module: treat them as settled and build the spec on them. Record any product-level answer as the repository's agent instructions say to edit the design docs.

Follow the format and the rules in the repository's agent instructions. Put anything the docs do not settle under "Open Questions" instead of guessing.
Leave the status line as pending approval: only a human approves a spec.
