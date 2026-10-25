# Host-side OpenAI Responses adapter

`OpenAIJSONModel(model_config).complete(context, *, max_input_tokens,
max_output_tokens, timeout_seconds)` returns `{content: dict, input_tokens: int,
output_tokens: int, tokens: int}`. `ModelError(message, tokens=...)` exposes usage:
0 for local/preflight validation with no inference, a known total for invalid
inference content, and None for transport uncertainty, missing/inconsistent usage,
or an envelope anomaly. Outer accounting must retain the reservation when None.

Configuration requires provider `openai`, nonempty id, options `{}`, api_key_env,
and count_fee_acknowledged `true`. Check provider pricing and count-request fees before setting budgets;
`count_fee_acknowledged` records this requirement. The experiment accounts for each
role using the configured token rate. The adapter rejects arbitrary model options, custom URLs,
inline API keys and remote conversation references. Default fixtures remain mock.

Every call posts identical self-contained JSON bytes first to
`https://api.openai.com/v1/responses/input_tokens`, then only after a valid count
within max_input_tokens to `https://api.openai.com/v1/responses`. The request contains
model, developer/user input, max_output_tokens, JSON-object text format, stream=false,
and store=false. No tools, previous_response_id or conversation are sent. The count
therefore includes the actual framed request and model overhead; real-token bounds
never use the mock byte estimate. Requests are limited to 1 MiB and responses to 4 MiB. Token accounting
uses input-token preflight and reported usage. There are no automatic retries.

Only HTTPS to the fixed API endpoint is used. Redirects and ambient proxy settings
are disabled. The API key is read host-side from the named environment variable at
call time, never passed to the sandbox, included in context, or echoed in errors.
The request deadline covers count and inference together. Socket timeouts and
monotonic checks bound reads, and a Unix main-thread interval timer bounds blocking
DNS/connect and slow transport too. Calls outside the Unix main thread, or with an
already active real interval timer, fail before generation with tokens=0. This
restriction fits the current synchronous Unix CLI and avoids altering caller timers.

Inference usage is validated before content: input_tokens/output_tokens/total_tokens
must be nonnegative strict integers and sum consistently. Reasoning tokens are already
included in output_tokens and are not added again. Input or output usage above its
reserved envelope produces an anomaly error with tokens=None. Incomplete status,
refusals, malformed JSON, duplicate keys, nonfinite JSON, and non-object content are
rejected while preserving valid known usage. Exactly one assistant output_text is
accepted, with optional reasoning output items.

The runner sends only public case fields, skill, transcript and action instructions.
It validates each local action schema before invoking its four Docker tools. The
existing SKILL.md read evidence remains required for skill_loaded=true. Known actual
usage is summed across steps; any unknown inference usage returns indeterminate and
tokens=None so core retains the full trial reservation. Mock synthetic accounting
and shell conversion remain unchanged. The reservation remains exactly
max_steps*(max_input_tokens+max_output_tokens).

## Verification

All HTTP tests patch urllib transport and use a dedicated fake environment-key name.
No real API key was read and no live or paid API call was made. Ten adapter tests cover
identical payloads, preflight gating, usage/anomalies, known-cost invalid content,
reasoning accounting, option/key validation, request/response byte bounds, redirects,
transport failures, shared deadlines and actual wall-timer interruption. Runner unit
tests cover actual token aggregation, expected-answer exclusion, strict action schema,
known/unknown cost handling and SKILL read evidence. Docker tests continue to exercise
the real mock tools and isolation boundaries. Validate the chosen model and token-count request against the live API before
starting a quality experiment. The adapter rejects API errors.
