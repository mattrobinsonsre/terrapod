"""Runner Job phase implementations.

Each phase has its own module so unit tests stay tightly scoped and
bash-side guards (TP_RUNNER_*_DONE env-var markers) read 1:1 against
the porting commit. The orchestrator in
terrapod.runner.job_entrypoint composes them in the right order.
"""
