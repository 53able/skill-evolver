# Docker runtime evidence

Verified locally on 2026-10-25 using uv-managed Python stdlib and Docker Engine
server 29.8.2 (Docker Desktop). Alpine `alpine:3` resolved locally to
`sha256:f7bbaec1bd0330a8dba26f03e05f421bb3c8b807bb41873df3c3b0c3d1083d8f`.
Prepare the image locally before starting trials. All container invocations use
--pull never; missing images stop execution.

## Public API and frozen environment

`TrialRunner(config).run(skill_text, case, trial_id=...)` returns status, outputs,
tokens, duration_seconds, termination_reason, model (provider/id/options),
environment_hash, skill_loaded and transcript. Success means collection succeeded;
correctness belongs to the external evaluator. Only id, group_id, prompt, files,
output_paths and operation cross the public case boundary. expected is never read.

`prepare_environment(pinned_image_id=None)` resolves one local immutable image ID,
sets the internal sandbox image to it and returns it. Subsequent calls preserve that
ID; a conflicting supplied ID fails. On resume, supply the manifest's exact ID;
it must be locally available and resolve identically. Each sandbox verifies its
configured immutable image, never re-resolving the original mutable tag after freeze.
Core owns durable manifest storage and comparison acceptance. The environment hash
includes the pinned image, harness version 2, fixed flags, timeout, runtime storage
bounds, collector limits and supervisor/candidate identities. Nonce labels and host
staging paths are ownership data and excluded from comparison identity.

Mock reads /SKILL.md through the real Docker read tool, performs a real shell
conversion, then finishes (three steps). operation=uppercase invokes tr; copy
invokes cp. It uses input.txt when available, otherwise the first public input, and
requires exactly one output path. Default paths are /input/input.txt and
/output/result.txt. Use this deterministic, unpaid mock to test the tool loop. For real-model
execution, configure the adapter described in model-evidence.md.

`reservation_tokens(config)` equals exactly
max_steps*(max_input_tokens+max_output_tokens). Mock counts UTF-8 bytes of serialized
requests/responses and enforces each per-step bound. Real mode uses token preflight
and reported usage, never a byte estimate. Unknown usage retains the full reservation.

## Storage and process isolation

Only dedicated read-only inputs and SKILL.md are mounted from the host. No project,
answers, evaluator, other candidates or Docker socket are mounted. Network is none,
rootfs read-only, capabilities all dropped, no-new-privileges enabled, and CPU,
memory/swap, PID and /tmp tmpfs byte/inode limits are set. Model keys remain host-side.

Output uses a unique Docker local-driver **tmpfs** volume with type=tmpfs,
device=tmpfs and o=size=<cap>,nr_inodes=<bound>,uid=65534,gid=65534,mode=0770.
Driver options
are inspected before use and the mounted filesystem must report tmpfs with the
supported 4096-byte allocation unit; unsupported storage fails closed before model
execution. Kernel allocated-byte cap is ceil(max_total_bytes/4096)*4096. This page
rounding is explicit: a logical limit below one page still permits one allocated
page during execution. Sparse logical lengths are not the kernel allocated-byte
limit. Collection separately enforces exact logical file/total sizes. Inodes are
bounded to max_files+1, including the root directory; nested directories consume
this same budget, reducing available file capacity.

The main container's trusted root PID1 runs only `/bin/sleep <timeout_seconds>`.
Every candidate tool uses docker exec --user 65534:65534. No tool exposes root exec,
and nonroot candidates cannot signal the root supervisor without CAP_KILL. The
supervisor's daemon-side exit terminates all candidate descendants independently
of host Python cleanup.
A separate trusted root keeper mounts the same output volume and sleeps for the
trial timeout plus a fixed 60-second collection grace. While main is stopped, keeper
keeps the tmpfs mounted so Docker cp can read final artifacts. The keeper is finite
and independently exits even after owner death; once all mounts release, tmpfs data
vanishes. It receives no candidate commands. Main remains stopped until explicit
cleanup; volume metadata and staging also require journal recovery after owner death.

Stop checks State.Running before kill and confirms false afterwards; natural expiry
and repeated stop are handled idempotently. Collection additionally requires a live
keeper, uses stopped-container `docker cp ID:/output/. -` without -L, bounds archive
bytes/time and validates every member through tar streaming mode. No extractall is
used. Traversal, absolute paths, duplicate names, links, sparse/special files,
unexpected/missing outputs, invalid UTF-8 and logical byte/file/count excess reject.
Returned strings are detached copies; later filesystem writes cannot change them.

## Exact resource journal and recovery

`TrialRunner.resource_recorder` is a callable property, default None. Core installs
Store.append before execution. BEFORE any staging/volume/container creation, the
sandbox emits resource_allocated with resource_id=<UUID hex>, logical trial_id,
and resources={main,keeper,volume,labels,staging}. Names derive from exactly that
nonce; labels contain skill-evolver.nonce; staging records exact tempdir path, nonce
and host UID. Its .owner marker records the nonce. Image inspection creates no resource.

`recover_resources(events)` reduces matched allocations/releases, then recovers only
unmatched exact records. It validates canonical names/path/UID, nonce labels on
existing containers/volume and staging marker/ownership before deletion. Containers
are removed by their inspected immutable IDs, not a prefix search. Unlabelled or
mismatched resources fail closed. After successful complete cleanup, resource_released
is emitted with the same resource_id/resources. Lost create responses remain
recoverable because names and labels were recorded before Docker creation. No
broad enumeration, unrelated container access, or prefix cleanup is used.

## Verification

```sh
SKILL_EVOLVER_DOCKER_TESTS=1 PYTHONPATH=src uv run --no-project python -m unittest discover -s tests -p test_sandbox.py -v
SKILL_EVOLVER_DOCKER_TESTS=1 PYTHONPATH=src uv run --no-project python -m unittest discover -s tests -p test_runner.py -v
uv tool run --from ruff==0.14.14 ruff check --select F,E9 src/skill_evolver/sandbox.py src/skill_evolver/runner.py tests/test_sandbox.py tests/test_runner.py
uv tool run --from mypy==1.19.1 mypy --check-untyped-defs --ignore-missing-imports src/skill_evolver/sandbox.py src/skill_evolver/runner.py
```

The storage tests exercise real Docker storage: at a 4096-byte cap, an 8192-byte
write fails with at most 4096 bytes allocated to its file; at three available file
inodes, the fourth empty file fails. After replacing these probe files, stopped-main
collection still returns stable text while keeper holds the mount.

A subprocess owner starts main, keeper and a 30-second candidate descendant, then
is SIGKILLed. Tests use a three-second main timer and one-second keeper grace to
keep validation bounded. After 4.2 seconds, main and keeper report Running=false and
Pid=0. Recovery from the fsynced allocation journal removes exact containers,
volume and staging and emits release. Repeated cleanup is safe. Other tests cover
read-only inputs, host secrets/paths hidden, loopback-only networking, nonroot tools,
root timer signal rejection, actual conversion/copy, fresh state/conversation,
malicious artifacts, exact ownership rejection and image freeze/resume behavior.
No live/paid API calls or real credential reads are used by these runtime tests.

## Limits

These results apply to Docker Engine 29.8.2 and the tested local configuration.
Live provider validation and billing checks are separate prerequisites for real-model
experiments.
