# Telegram publisher AUQNI: production specification

Status: specification based on the confirmed Telegram publication `https://t.me/auqni_qms/4`.

This document describes the existing working HTTP contract and separates it from requirements that must be added for production operation. It does not contain a bot token and does not prescribe PowerShell as the implementation technology.

## 1. Confirmed by publication #4

### Target

- Telegram channel: `AUQNI | СМК`.
- Public username and request target: `@auqni_qms`.
- Confirmed result: `https://t.me/auqni_qms/4`.
- Confirmed Telegram `message_id`: `4`.

### Inputs

Text source:

`posts/2026-09-17-telegram-nezhelatelnye-sobytiya-final.md`

Image source:

`images/auqni-patient-falls-smooth-title-v2.png`

The text was read as UTF-8, trimmed at the edges and passed as the photo caption. Markdown file extension described the source artifact only; no Telegram formatting mode was enabled.

For the confirmed files, the final trimmed caption contained 913 Unicode characters. The PNG was 2,059,012 bytes with dimensions 1254 x 1254 pixels.

### Telegram Bot API request

Method:

`sendPhoto`

Endpoint template:

`POST https://api.telegram.org/bot<TOKEN>/sendPhoto`

`<TOKEN>` is substituted at runtime and must never be written to logs, command-line arguments, publication records or this specification.

Content type:

`multipart/form-data`

Confirmed multipart fields:

| Field | Purpose |
| --- | --- |
| `chat_id` | Target channel. Publication #4 used `@auqni_qms`. |
| `photo` | Binary PNG image. The part used MIME type `image/png`. |
| `caption` | UTF-8 text read from the Markdown source file and trimmed. |

No `parse_mode` field was sent. Therefore the caption must be treated as plain text: Markdown characters are not interpreted as Telegram Markdown or MarkdownV2.

### Relevant Telegram limits

For this exact chain, the critical limit is the `sendPhoto` caption limit of 1024 characters after Telegram entity parsing. Because publication #4 did not use `parse_mode`, the practical preflight rule is that the final Unicode caption must not exceed 1024 characters.

The historical command checked this limit and did not truncate the caption. Production code must preserve that behavior: an over-limit caption is a validation error, not an instruction to silently remove text.

For the standard hosted Bot API, the current `sendPhoto` limits are:

- at most 10 MB;
- width plus height must not exceed 10,000 pixels;
- the ratio between the larger and smaller dimension must not exceed 20.

These values are production requirements from the current official Bot API, not facts inferred from publication #4. Recheck them against the official [`sendPhoto` documentation](https://core.telegram.org/bots/api#sendphoto) when upgrading the publisher or Bot API contract.

### Successful response

The Telegram Bot API success envelope has the following relevant type shape. This is schematic notation, not literal JSON, because the numeric channel ID is intentionally not recorded in this document:

```text
{
  "ok": true,
  "result": {
    "message_id": 4,
    "chat": {
      "id": <integer>,
      "title": "AUQNI | СМК",
      "username": "auqni_qms",
      "type": "channel"
    }
  }
}
```

Only `message_id: 4`, the public username and the resulting public URL are retained here as confirmed publication facts. The numeric channel ID is deliberately not inferred from the username and must be captured from a verified API response or configuration during production commissioning.

Success requires all of the following:

1. The HTTP request completes successfully.
2. The response is valid JSON.
3. `ok` is exactly `true`.
4. `result.message_id` is present.
5. The returned chat corresponds to the configured target channel.

### Public URL

For a public channel with username `auqni_qms`, the publication URL is formed only after confirmed success:

```text
https://t.me/auqni_qms/<message_id>
```

For publication #4 this produced:

```text
https://t.me/auqni_qms/4
```

The URL must not be predicted before Telegram returns `message_id`.

### Historical error behavior

The historical one-off request checked the Telegram `ok` result, used a network timeout, disposed HTTP resources and contained error handling. It did not establish a reusable retry or idempotency protocol.

Therefore the only behavior confirmed by publication #4 is:

- do not report publication success unless Telegram returns a successful response;
- extract `message_id` only from the successful response;
- do not expose the bot token in output.

No stronger claim about retries, recovery or duplicate prevention is supported by the historical execution.

On an unsuccessful Bot API request, the standard response has `ok: false`, a human-readable `description`, an integer `error_code`, and may include `parameters`. These fields belong to the production error contract below; publication #4 itself exercised only the successful branch.

## 2. Requirements to add for production

### Portable runtime contract

The publisher must run on Ubuntu without depending on Windows or PowerShell. The implementation may use any maintained Linux-compatible HTTP client, provided it reproduces the confirmed request contract above.

The publisher must accept explicit inputs rather than fixed Windows paths:

- exactly one content source: a path to the final `auqni-content/v1` JSON or a path to the final Telegram text/Markdown file;
- path to the image;
- target channel identifier, defaulting only from non-secret configuration;
- execution mode: `dry-run` or `publish`;
- optional publication/idempotency key override for an explicitly approved repeat or recovery operation.

For `auqni-content/v1`, the Telegram text source is `platforms.telegram.content`. For a text/Markdown input, the entire UTF-8 file after edge trimming is the caption. Supplying both sources is an input error: the publisher must not guess between divergent versions.

The default idempotency key must be derived deterministically from the Bot API method, canonical target channel, final caption hash and image hash. A caller-supplied override must not silently bypass an existing `published` or `uncertain` record; it requires an explicit repeat/recovery approval recorded in the journal.

### Required preflight validation

Before a network request:

- confirm that the token is available without printing it;
- confirm that the target equals the configured AUQNI channel;
- confirm that text and image files exist and are readable;
- decode text as UTF-8 and reject invalid input;
- reject an empty caption;
- reject a caption over the current `sendPhoto` limit;
- validate image type, signature, size and Telegram compatibility;
- reject unresolved editorial blockers required by the content contract;
- calculate stable hashes of the selected source, final caption and image;
- check the publication journal for an existing successful or uncertain attempt with the same idempotency key.

`dry-run` must perform these checks with zero Telegram API requests.

### Production error behavior

Errors must be divided into at least these classes:

- local validation failure: no network request was made;
- Telegram API rejection with a safe error code and description;
- transport failure before a response;
- uncertain outcome: the request may have reached Telegram, but no reliable response was received;
- journal/storage failure.

Automatic retry must not occur after an uncertain `sendPhoto` outcome. The operator must verify the channel before retrying. Any retry policy for failures known to precede delivery must be bounded and recorded.

Raw HTTP bodies, request headers, token-bearing URLs and exception data that may contain the token must not be logged.

### Publication journal and duplicate protection

Reserve the idempotency key atomically before the network call and update that same record after the result. A unique constraint or equivalent single-writer transaction must prevent two concurrent processes from reserving the same key. At minimum retain:

- internal publication attempt ID;
- idempotency key;
- source type: `auqni-content/v1` or `text`;
- source path or durable object identifier;
- source SHA-256;
- content schema version when the source is `auqni-content/v1`;
- final caption SHA-256 and character count;
- image path or durable object identifier;
- image SHA-256, MIME type and byte size;
- target channel username;
- numeric channel ID after it has been verified;
- Bot API method;
- mode (`dry-run` or `publish`);
- status: `prepared`, `sending`, `published`, `failed` or `uncertain`;
- attempt creation, request start and completion timestamps in UTC;
- Telegram `message_id` on confirmed success;
- public publication URL on confirmed success;
- safe Telegram error code and sanitized description on rejection;
- safe transport-error category;
- operator/initiator and approval reference;
- publisher version or build identifier.

The journal must never contain the bot token, authorization data, raw token-bearing endpoint, full raw response or multipart request body.

A second publish request with the same idempotency key must be rejected when the previous status is `sending`, `published` or `uncertain`, unless an operator has explicitly resolved the previous attempt. Resolving an `uncertain` attempt must preserve the original record and append the operator, timestamp, evidence and decision; it must not erase or overwrite the audit trail.

### Production secrets and configuration

Required secret:

- `TELEGRAM_BOT_TOKEN` — Telegram bot credential, supplied through the VPS secret/environment mechanism with access restricted to the service account.

Required non-secret configuration:

- target channel username: `@auqni_qms`;
- public channel username used for URL construction;
- paths for content, media and the publication journal;
- network timeout and explicitly approved retry policy.

The numeric Telegram channel ID is not required to perform the confirmed username-based request. When Telegram returns it in a successful `Message.chat`, store it in the journal and subsequently verify that the configured username and returned chat identify the same target.

The token must not be committed to Git, copied into `auqni-content/v1`, passed as a CLI argument or embedded in a systemd unit. A systemd `EnvironmentFile` or credentials mechanism may be used later with restrictive filesystem permissions.

## Acceptance criteria for a future implementation

A future publisher conforms to this specification when it can:

1. Reproduce the confirmed `sendPhoto` multipart contract on Linux.
2. Validate the exact inputs in dry-run with zero network requests.
3. Publish only after explicit authorization.
4. Record a confirmed `message_id` and construct the public URL from it.
5. Prevent silent duplicate publication.
6. Fail safely without leaking secrets or claiming success after an uncertain result.
