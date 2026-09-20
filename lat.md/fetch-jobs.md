# Fetch jobs

A process-local registry of background fetch jobs lets the Streamlit UI start a long-running fetch in a worker thread and poll its progress on a timer without rerunning the whole app script on every tick.

The registry lives in a normally imported module (not `app.py`) because Streamlit re-executes `app.py` in a fresh namespace on every rerun, which would reset module-level state; imported modules are cached in `sys.modules` and execute once per process.

## Fetch job registry

- [[fetch_jobs.py#FetchJob]] — thread-safe state shared by one fetch worker and its UI: a `cancel_event`, a lock, progress counters/message, and the terminal `result_payload` or `error`. [[fetch_jobs.py#FetchJob#publish_progress]] updates progress under the lock; [[fetch_jobs.py#FetchJob#complete]] records the terminal result or error; [[fetch_jobs.py#FetchJob#snapshot]] returns an immutable [[fetch_jobs.py#FetchJobSnapshot]] for one UI poll.
- [[fetch_jobs.py#start_fetch_job]] — creates a `FetchJob`, spawns a daemon thread running the supplied `runner(job)`, registers it, and returns the job id. The runner wires progress and cancellation directly onto the job.
- [[fetch_jobs.py#get_fetch_job]] — looks up a live job by id without exposing the registry.
- [[fetch_jobs.py#discard_fetch_job]] — removes a completed job from the registry.

The UI side (start button, cancel, and the polling fragment) is described in [[architecture#Fetch execution]].
