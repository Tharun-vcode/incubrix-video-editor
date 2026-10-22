# Sources and Dependency Provenance

The implementation is based on the assessment brief included in this
repository and the public documentation for the selected libraries and media
formats.

| Source | Use |
| --- | --- |
| `IncuBrix_Assessment_2_Prompt_Based_Video_Editing_Engine_Brief.pdf` | Functional requirements and assessment criteria |
| [FastAPI documentation](https://fastapi.tiangolo.com/) | HTTP application framework and OpenAPI generation |
| [Pydantic documentation](https://docs.pydantic.dev/) | Request validation and typed configuration patterns |
| [SQLite documentation](https://www.sqlite.org/docs.html) | Local persistent job/session/idempotency store |
| [FFmpeg documentation](https://ffmpeg.org/documentation.html) | Media probing, filters, and rendering |
| [OpenTimelineIO documentation](https://opentimelineio.readthedocs.io/) | Timeline interchange structure |
| [MinIO documentation](https://min.io/docs/minio/linux/index.html) | S3-compatible object storage deployment |
| [faster-whisper documentation](https://github.com/SYSTRAN/faster-whisper) | Optional local transcription provider |

Runtime dependencies and their version ranges are declared in
`requirements.txt`; optional transcription dependencies are declared in
`requirements-whisper.txt`; test/developer dependencies are declared in
`requirements-dev.txt`. No API keys or external service credentials are
required by the deterministic editing engine.
