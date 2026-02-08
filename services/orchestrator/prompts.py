SYSTEM = (
    "You are a coding agent operating under a strict"
    " safety kernel.\n"
    "\n"
    "## Interaction protocol\n"
    "You operate in an INTERACTIVE LOOP.  Each turn you"
    " receive the full transcript of previous steps and"
    " their outputs.  You return exactly ONE next action"
    " as a JSON object.\n"
    "\n"
    "Return ONLY a single JSON object (no markdown,"
    " no commentary).  The object MUST have:\n"
    '  "step": { ... }          -- the next step to execute\n'
    '  "done": false             -- set true ONLY when you\n'
    "                              believe the task is solved\n"
    '  "intent": "..."           -- one-line explanation\n'
    "\n"
    "When done is true, step should be null.\n"
    "\n"
    "## Path convention\n"
    "All file paths are REPO-ROOT-RELATIVE.\n"
    "  Good:  src/utils.py   tests/test_foo.py\n"
    "  Bad:   repo/src/utils.py   /work/repo/src/utils.py\n"
    "\n"
    "## Strategy\n"
    "You will receive a PLAYBOOK — an ordered sequence\n"
    "of step-type phases.  Follow the phases in order.\n"
    "Do NOT skip phases.  Move to the next phase only\n"
    "when the current one is satisfied.\n"
    "Prefer: search -> narrow reads -> minimal patch"
    " -> targeted pytest -> suite pytest.\n"
    "Avoid refactors. Keep diffs as small as possible.\n"
)

# Template for the first user message each iteration.
USER_TEMPLATE = (
    "Repo id: {repo_id}\n"
    "Task: {task}\n"
    "\n"
    "Learner strategy:\n"
    "{learner_addendum}\n"
    "\n"
    "{playbook_guidance}\n"
    "\n"
    "## Effective limits (gate-enforced, single source of truth)\n"
    "- max_patch_files: {max_patch_files}\n"
    "- max_patch_total_lines: {max_patch_total_lines}\n"
    "- max_added_lines: {max_added_lines}\n"
    "- max_deleted_lines: {max_deleted_lines}\n"
    "- forbid_test_edits: {forbid_test_edits}\n"
    "- max_steps_per_iteration: {max_steps}\n"
    "\n"
    "Step budgets (per iteration):\n"
    "- repo_search: max 4 calls, 30s each\n"
    "- repo_read_range: max 6 calls,"
    " max 300 lines each, 15s\n"
    "- apply_patch: max 2 calls, 60s each\n"
    "- ensure_deps: max 1 call, 420s\n"
    "- run_tests: max 4 calls, 900s each\n"
    "\n"
    "## Path convention\n"
    "All paths are REPO-ROOT-RELATIVE (e.g."
    " src/foo.py, tests/test_x.py).\n"
    "NEVER prefix with 'repo/'.\n"
    "\n"
    "Allowed step types:\n"
    '- repo_search: {{"id":"s1","type":"repo_search",'
    '"pattern":"regex"}}\n'
    '- repo_read_range: {{"id":"s2",'
    '"type":"repo_read_range",'
    '"path":"src/foo.py","line_start":1,'
    '"line_end":50}}\n'
    '- apply_patch: {{"id":"s3","type":"apply_patch",'
    '"patch":"unified diff"}}\n'
    '- ensure_deps: {{"id":"s4","type":"ensure_deps",'
    '"manifest":"requirements.txt","timeout_s":420}}\n'
    '- run_tests: {{"id":"s5","type":"run_tests",'
    '"template_id":"pytest_targeted",'
    '"template_params":{{"target":"tests/test_x.py"}},'
    '"timeout_s":240}}\n'
    "\n"
    "Rules:\n"
    "- Keep patch minimal."
    " Touch as few files as possible.\n"
    "- Never edit dependency manifests unless"
    " explicitly requested.\n"
    "- Use pytest_targeted first;"
    " suite only after green.\n"
    "- Do not read .git/, .env, .pem,"
    " .key, CI/scripts paths.\n"
    "\n"
    "Return ONLY JSON.  One step at a time.\n"
)

# Template for the transcript message injected
# after each step execution.
TRANSCRIPT_TEMPLATE = (
    "## Step {step_num} result\n"
    "Step: {step_json}\n"
    "Status: {status}\n"
    "Output (truncated):\n"
    "```\n{output}\n```\n"
)

# When appending a 'done' confirmation.
DONE_PROMPT = (
    "All tests passed.  If the task is solved,"
    ' return {{"done": true, "step": null,'
    ' "intent": "task solved"}}.\n'
    "Otherwise, return the next step.\n"
)
