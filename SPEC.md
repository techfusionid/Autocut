# Image Edge Strip Removal API Specification

## Objective

Build a Python microservice that accepts images, crops watermark strips from their edges, stores the original and processed images, and exposes an API consumed by n8n through its HTTP Request node. The application must run in Docker without a GPU.

Wahyu will implement the n8n workflow integration. The agent is responsible for the Python service, Docker configuration, tests, and repository documentation. Push to GitHub once the destination repository and access are available. Server deployment has not yet been agreed upon.

## Scope

- Removal uses cropping: the strip area is discarded and image dimensions change.
- Excludes inpainting, reconstructing content underneath watermarks, Instagram scraping, video processing, and building the n8n workflow.
- The primary MVP input is an uploaded image file. An Instagram post URL is source metadata, not a download source.
- Downloading from direct image URLs is an optional extension; do not add it until needed.
- Each job processes one image. n8n can submit carousel images as separate jobs.
- Do not assume the position or width of csrelatedcompetitions strips before examining samples.

## Initial Architecture Decisions

Use Python, FastAPI, Pillow, and NumPy. FastAPI exposes the API. Pillow opens images, normalizes orientation, crops, and saves results. Use NumPy when pixel analysis is needed for automatic detection.

Use SQLite for metadata and a persistent job queue, and a persistent directory for files. Run one API process and one worker process on a single host through Docker Compose, using the same application image. Both services share a local data volume. Redis, Celery, PostgreSQL, and object storage are not required for the MVP.

This SQLite setup is limited to a single host with a local volume, not a network filesystem. Revisit the queue and storage design before expanding to multiple servers or workers.

## Unresolved Requirements

Image samples are not yet available. Request examples covering strip positions, image sizes, background colors, and images without strips before selecting an automatic detection algorithm.

The agent may complete the API, worker, storage, and manual cropping mode while waiting for samples. Do not claim that automatic detection is complete or accurate based solely on synthetic images. If samples remain unavailable, mark automatic mode as unavailable and document that limitation.

## Processing Flow

1. n8n uploads an image and cropping options.
2. The API validates the input, stores the original image, and creates a persistent job.
3. The API returns HTTP 202 with a job ID and status URL.
4. The worker atomically claims the job and changes its status to `processing`.
5. The worker validates the decoded image and orientation, determines the crop box, and saves the result atomically.
6. The worker records dimensions, crop coordinates, the result or error, and completion time.
7. n8n polls at intervals and downloads the result after success.

Statuses: `queued`, `processing`, `succeeded`, `failed`, and `needs_review`. Use `needs_review` when automatic detection is uncertain. The last three statuses are terminal; polling must stop.

## API Contract

All endpoints except the health check require `Authorization: Bearer <API_TOKEN>`. Read the token from the environment and never store it in the repository or logs.

### POST /jobs

Content-Type: `multipart/form-data`.

| Field | Required | Description |
| --- | --- | --- |
| `image` | Yes | A static JPEG or PNG file |
| `mode` | Yes | `manual` or `auto` |
| `source_post_url` | No | Source post URL metadata; never fetched |
| `crop_left` | Manual mode only | Pixels removed from the left; defaults to 0 |
| `crop_top` | Manual mode only | Pixels removed from the top; defaults to 0 |
| `crop_right` | Manual mode only | Pixels removed from the right; defaults to 0 |
| `crop_bottom` | Manual mode only | Pixels removed from the bottom; defaults to 0 |

All crop values must be nonnegative integers measured after EXIF orientation normalization. Cropping must leave a positive width and height. Reject manual crop parameters in automatic mode to avoid ambiguity.

HTTP 202 response:

```json
{
  "job_id": "<uuid>",
  "status": "queued",
  "status_url": "/jobs/<uuid>"
}
```

If automatic mode has not been implemented, return HTTP 422 with code `auto_mode_unavailable`. Do not silently substitute a fixed crop.

### GET /jobs/{job_id}

Return HTTP 200 for an existing job, including a failed job:

```json
{
  "job_id": "<uuid>",
  "status": "succeeded",
  "mode": "manual",
  "source_post_url": null,
  "original_url": "/jobs/<uuid>/original",
  "result_url": "/jobs/<uuid>/result",
  "input_size": {"width": 1080, "height": 1080},
  "output_size": {"width": 1000, "height": 1080},
  "crop_box": {"left": 0, "top": 0, "right": 1000, "bottom": 1080},
  "error": null,
  "created_at": "<UTC ISO-8601>",
  "finished_at": "<UTC ISO-8601>"
}
```

`crop_box` uses Pillow coordinates: right and bottom boundaries are exclusive. Result fields that are not yet available must be null. Callers resolve relative URLs against the API base URL. Never expose internal filesystem paths.

### GET /jobs/{job_id}/original

Return the original file with the appropriate Content-Type. Preserve the original uploaded bytes unchanged; apply normalization only during processing.

### GET /jobs/{job_id}/result

Return the processed file only when the status is `succeeded`. Use PNG output for the MVP to avoid adding JPEG compression artifacts; preserve transparency where present. Return HTTP 409 when the result is unavailable and HTTP 404 for an unknown job.

### GET /health

Return service health without sensitive data. Docker Compose must include health checks, and the worker must record a heartbeat so its health can be inspected.

### Errors and Retries

Use a consistent error structure: `{"error":{"code":"...","message":"..."}}`. Use HTTP 401 for authentication failures, 404 for unknown jobs, 413 for oversized uploads, 415 for unsupported formats, and 422 for invalid input or crop settings. Record failures occurring after enqueueing in the job's `error` field.

Support the `Idempotency-Key` header to prevent duplicate jobs when n8n retries requests. Store the key and a fingerprint of file contents and parameters atomically. Repeated requests with the same key and payload return the same job; the same key with a different payload returns HTTP 409.

## Image Processing Rules

- Validate the format using file contents, not only the filename or caller-supplied MIME type.
- Configurable initial limits: 15 MiB per upload, 25 million decoded pixels, and 60 seconds of processing per job.
- Enforce upload limits before reading the entire file into memory. Handle corrupt images, decompression bombs, and animated formats with structured errors.
- Normalize EXIF orientation before calculating crop coordinates. Do not automatically resize or stretch results.
- Manual mode must produce deterministic output based on the supplied margins.
- Once samples are available, automatic mode should analyze strip boundaries at image edges. Derive rules and thresholds from those samples and document their supported scope.
- Do not apply aggressive crops when detection is uncertain. Return `needs_review`, an explanation, and no final result file.
- For images confidently identified as having no strip, automatic mode may output the complete image and record that no cropping was applied.

## Storage and Worker Reliability

Minimum metadata: ID, status, mode and parameters, source post, internal file paths, fingerprint, idempotency key, image dimensions, crop coordinates, timestamps, attempt count, worker lease, and error code and message.

Use job IDs for filenames instead of uploaded filenames. Store files and the database on a persistent volume that survives container recreation. Do not automatically delete original or processed images in the MVP. Document that storage capacity must be monitored and that a retention policy has not yet been agreed upon.

Job claiming must be atomic. Use short transactions, a busy timeout, and SQLite settings suitable for concurrent API and worker access. Use worker leases and heartbeats. Jobs abandoned after a worker crash must be recoverable, with a maximum of 3 attempts. Result writes must be idempotent and atomic. Do not retry permanent input errors. Enforce timeouts so a stuck job cannot block the queue indefinitely.

Do not mark a job as `succeeded` before its result file is available. Handle storage failures, database insertion failures, and partial files without leaving broken result references.

## Docker and Configuration

Provide a `Dockerfile`, `compose.yaml`, `.env.example`, pinned dependencies, and `.gitignore`. Run containers as a non-root user. Never commit tokens, runtime databases, or user images.

Minimum environment variables: `API_TOKEN`, `DATA_DIR`, `MAX_UPLOAD_BYTES`, `MAX_IMAGE_PIXELS`, `JOB_TIMEOUT_SECONDS`, and `MAX_JOB_ATTEMPTS`. Define documented defaults for all settings except the token. Fail startup with a clear message if the token is not configured.

The README must explain API addressing: `localhost` inside the n8n container refers to that container itself. Use the service name when services share a Docker network, or a reachable server address when running on different servers. Use HTTPS when exposing the API over a public network.

## Documentation for the n8n Consumer

Include practical request examples for multipart uploads, polling, and binary downloads, along with success and failure examples. Explain that the n8n binary field must be submitted as `image`, how to configure bearer authentication, and how to resolve relative URLs.

Example flow: HTTP Request upload → Wait 2 seconds → check status → repeat for queued/processing → download for succeeded → handle failed/needs_review separately. Use a configurable polling deadline, such as 2 minutes, so the workflow does not wait indefinitely. A polling timeout does not cancel the server-side job.

## Tests and Acceptance Criteria

1. `docker compose up --build` starts the API and worker on a machine without a GPU.
2. Uploading a test image returns HTTP 202 and a job ID; the job eventually reaches a terminal state.
3. Manual cropping produces the correct dimensions and pixel contents on controlled fixtures.
4. The original file is identical to the upload; results and metadata remain available after container restarts.
5. Images with EXIF orientation are cropped according to their correctly displayed orientation.
6. Corrupt images, oversized files, invalid crop settings, invalid tokens, and unknown jobs produce responses matching the API contract.
7. Idempotent requests do not create duplicate jobs, including concurrent requests.
8. Jobs interrupted by worker termination recover without remaining permanently in processing or exposing partial result files.
9. Multiple sequential jobs complete without lost work; timeouts and retry limits are tested.
10. Automatic detection is considered complete only after testing representative samples, including images without strips and ambiguous cases, and inspecting original/result pairs. Report observed accuracy limitations without unsupported generalizations.
11. The README enables Wahyu to run the service and consume it through HTTP Request nodes without reading internal code.

## Agent Execution Instructions

Inspect the repository and local instructions first. Implement the API foundation, persistence, manual cropping, and worker, then verify the end-to-end flow. Implement automatic detection after samples are available. Do not expand the scope to dashboards, scraping, or inpainting.

Report completed features, tests actually executed, detection limitations, and startup instructions. If the destination GitHub repository is available and pushing is authorized, push the changes and provide a link. Otherwise, prepare the local source and request the repository destination. Do not assume authorization to create a public repository or deploy the service.
