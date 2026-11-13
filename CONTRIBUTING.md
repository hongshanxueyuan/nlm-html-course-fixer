# Offline verification

The tests use simulated Studio, TTS and isolated agents. They do not publish a course.

Install Python 3.12 or later, FFmpeg and FFprobe, then run:

```text
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

The test suite reuses four sibling checkouts: `nlm-course-slides`, `ppt-to-imagesgallery`, `nlm-course-auditor` and `studio-imagegallery-publish`. When they are not siblings of this checkout (for example in a worktree), set `NLM_COURSE_SLIDES_ROOT`, `PPT_TO_IMAGESGALLERY_ROOT`, `NLM_COURSE_AUDITOR_ROOT` and `STUDIO_IMAGEGALLERY_PUBLISH_ROOT` to their respective absolute paths. CI checks out the exact commits used for the integration tests; update those refs deliberately when testing newer dependencies.

Studio response fixtures and their source provenance are documented in [tests/fixtures/README.md](tests/fixtures/README.md). Real UAT is separate and requires an explicit request.
