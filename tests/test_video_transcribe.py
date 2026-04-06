"""
Tests for scripts/recon/video_transcribe.py (ticket #11).

All external tools (yt-dlp, ffmpeg, whisper) and LLM calls are fully mocked.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from scripts.models import Authority, FactCategory, SourceType
from scripts.recon.base import (
    ReconModuleStatus,
    ReconRequest,
    ReconResult,
    ReconServices,
)
from scripts.recon.video_transcribe import (
    VideoTranscribeModule,
    _VideoInfo,
    _Walkthrough,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent


def _make_request(**kwargs: Any) -> ReconRequest:
    defaults: dict[str, Any] = {
        "run_id": "test-run-001",
        "target": "trello.com",
        "module_config": {},
        "budgets": {},
    }
    defaults.update(kwargs)
    return ReconRequest(**defaults)


def _make_services(llm_response: str | None = None) -> ReconServices:
    """Build a minimal ReconServices with an injectable LLM mock."""
    http_client = MagicMock()
    if llm_response is not None:
        http_client.chat.return_value = llm_response
    else:
        http_client.chat.return_value = None
    return ReconServices(
        spec_store=None,
        credentials={},
        artifact_store=None,
        http_client=http_client,
        browser=None,
    )


def _sample_walkthroughs_json() -> str:
    return json.dumps([
        {
            "feature": "create-board",
            "title": "Creating a Board",
            "description": "How to create a new board in Trello",
            "steps": ["Click + button", "Select 'Create Board'", "Enter name", "Click Create"],
            "ui_elements": ["+ button", "Create Board dialog", "Board name field"],
            "timestamp_hint": "0:30",
        },
        {
            "feature": "add-card",
            "title": "Adding a Card",
            "description": "How to add a card to a list",
            "steps": ["Click 'Add a card'", "Type card title", "Press Enter"],
            "ui_elements": ["Add a card button", "card title input"],
            "timestamp_hint": "1:45",
        },
    ])


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Module identity tests
# ---------------------------------------------------------------------------


class TestModuleIdentity:
    def test_name(self) -> None:
        m = VideoTranscribeModule()
        assert m.name == "video_transcribe"

    def test_authority(self) -> None:
        m = VideoTranscribeModule()
        assert m.authority == Authority.OBSERVATIONAL

    def test_source_type(self) -> None:
        m = VideoTranscribeModule()
        assert m.source_type == SourceType.VIDEO

    def test_requires_no_credentials(self) -> None:
        m = VideoTranscribeModule()
        assert m.requires_credentials == []


# ---------------------------------------------------------------------------
# INV-020: run() must not raise
# ---------------------------------------------------------------------------


class TestRunNeverRaises:
    def test_run_returns_result_on_yt_dlp_not_found(self) -> None:
        """If yt-dlp is missing, run() must return FAILED, never raise."""
        module = VideoTranscribeModule()
        request = _make_request()
        services = _make_services()

        with patch("subprocess.run", side_effect=FileNotFoundError("yt-dlp not found")):
            result = _run(module.run(request, services))

        assert isinstance(result, ReconResult)
        assert result.module == "video_transcribe"
        assert result.status in (ReconModuleStatus.FAILED, ReconModuleStatus.SKIPPED)

    def test_run_returns_result_on_generic_exception(self) -> None:
        """Totally unexpected exception must be swallowed and returned as FAILED."""
        module = VideoTranscribeModule()
        request = _make_request()
        services = _make_services()

        with patch.object(module, "_search_videos", side_effect=RuntimeError("boom")):
            result = _run(module.run(request, services))

        assert isinstance(result, ReconResult)
        assert result.status == ReconModuleStatus.FAILED
        assert any("boom" in e.message for e in result.errors)

    def test_result_module_name_always_matches(self) -> None:
        module = VideoTranscribeModule()
        request = _make_request()
        services = _make_services()

        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = _run(module.run(request, services))

        assert result.module == module.name


# ---------------------------------------------------------------------------
# Configured video URLs bypass search
# ---------------------------------------------------------------------------


class TestConfiguredVideoUrls:
    def _make_yt_dlp_meta(self, url: str, title: str = "Test Video") -> str:
        return json.dumps({
            "title": title,
            "duration": 300,
            "description": "A test video",
            "uploader": "Test User",
            "upload_date": "20240101",
        })

    def test_uses_configured_urls_without_search(self) -> None:
        """module_config['video_urls'] should skip yt-dlp search."""
        module = VideoTranscribeModule()
        url = "https://www.youtube.com/watch?v=test123"
        request = _make_request(module_config={"video_urls": [url]})
        services = _make_services(llm_response=_sample_walkthroughs_json())

        search_called = []

        def fake_search(target: str, max_videos: int) -> tuple[list[str], list[Any]]:
            search_called.append(True)
            return [], []

        with (
            patch.object(module, "_search_videos", side_effect=fake_search),
            patch.object(
                module,
                "_get_video_info",
                return_value=(_VideoInfo(url=url, title="Test Video", duration_seconds=300), None),
            ),
            patch.object(module, "_download_video", return_value=None),
            patch.object(module, "_extract_audio", return_value=None),
            patch.object(module, "_transcribe", return_value=("Hello world transcript", None)),
        ):
            result = _run(module.run(request, services))

        assert not search_called, "search should NOT be called when URLs are configured"
        assert url in result.urls_visited

    def test_skipped_when_no_videos_found(self) -> None:
        """If search finds nothing, status should be SKIPPED."""
        module = VideoTranscribeModule()
        request = _make_request()
        services = _make_services()

        with patch.object(module, "_search_videos", return_value=([], [])):
            result = _run(module.run(request, services))

        assert result.status == ReconModuleStatus.SKIPPED
        assert result.facts == []


# ---------------------------------------------------------------------------
# Search videos
# ---------------------------------------------------------------------------


class TestSearchVideos:
    def test_parses_yt_dlp_output(self) -> None:
        module = VideoTranscribeModule()
        fake_output = "https://youtu.be/abc\nhttps://youtu.be/def\n"
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = fake_output
        proc.stderr = ""

        with patch("subprocess.run", return_value=proc):
            urls, errors = module._search_videos("trello.com", max_videos=10)

        assert "https://youtu.be/abc" in urls
        assert "https://youtu.be/def" in urls
        assert errors == []

    def test_returns_error_on_yt_dlp_failure(self) -> None:
        module = VideoTranscribeModule()
        proc = MagicMock()
        proc.returncode = 1
        proc.stdout = ""
        proc.stderr = "ERROR: some yt-dlp error"

        with patch("subprocess.run", return_value=proc):
            urls, errors = module._search_videos("trello.com", max_videos=5)

        assert urls == []
        assert len(errors) > 0
        assert errors[0].error_type == "parse_error"

    def test_returns_error_on_timeout(self) -> None:
        module = VideoTranscribeModule()

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("yt-dlp", 60)):
            urls, errors = module._search_videos("trello.com", max_videos=5)

        assert len(errors) > 0
        assert errors[0].error_type == "timeout"

    def test_returns_error_when_not_installed(self) -> None:
        module = VideoTranscribeModule()

        with patch("subprocess.run", side_effect=FileNotFoundError):
            urls, errors = module._search_videos("trello.com", max_videos=5)

        assert len(errors) > 0
        assert "yt-dlp not found" in errors[0].message

    def test_deduplicates_urls(self) -> None:
        module = VideoTranscribeModule()
        # Return same URL multiple times across queries
        fake_output = "https://youtu.be/abc\n"
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = fake_output
        proc.stderr = ""

        with patch("subprocess.run", return_value=proc):
            urls, _ = module._search_videos("trello.com", max_videos=10)

        assert urls.count("https://youtu.be/abc") == 1


# ---------------------------------------------------------------------------
# Video info
# ---------------------------------------------------------------------------


class TestGetVideoInfo:
    def test_parses_metadata(self) -> None:
        module = VideoTranscribeModule()
        url = "https://youtu.be/abc"
        meta = json.dumps({
            "title": "Trello Tutorial",
            "duration": 600,
            "description": "Learn Trello",
            "uploader": "Trello HQ",
            "upload_date": "20240101",
        })
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = meta
        proc.stderr = ""

        with patch("subprocess.run", return_value=proc):
            info, error = module._get_video_info(url)

        assert error is None
        assert info.title == "Trello Tutorial"
        assert info.duration_seconds == 600

    def test_rejects_oversized_video(self) -> None:
        module = VideoTranscribeModule()
        url = "https://youtu.be/huge"
        meta = json.dumps({"title": "Long Video", "duration": 99999})
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = meta
        proc.stderr = ""

        with patch("subprocess.run", return_value=proc):
            info, error = module._get_video_info(url)

        assert error is not None
        assert "too long" in error.message

    def test_returns_error_on_yt_dlp_failure(self) -> None:
        module = VideoTranscribeModule()
        proc = MagicMock()
        proc.returncode = 1
        proc.stdout = ""
        proc.stderr = "Video unavailable"

        with patch("subprocess.run", return_value=proc):
            _, error = module._get_video_info("https://youtu.be/gone")

        assert error is not None
        assert error.error_type == "parse_error"

    def test_returns_error_on_timeout(self) -> None:
        module = VideoTranscribeModule()
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("yt-dlp", 30)):
            _, error = module._get_video_info("https://youtu.be/slow")

        assert error is not None
        assert error.error_type == "timeout"


# ---------------------------------------------------------------------------
# Download video
# ---------------------------------------------------------------------------


class TestDownloadVideo:
    def test_succeeds_on_zero_returncode(self) -> None:
        module = VideoTranscribeModule()
        proc = MagicMock()
        proc.returncode = 0
        proc.stderr = ""

        with patch("subprocess.run", return_value=proc):
            error = module._download_video("https://youtu.be/abc", Path("/tmp/video.mp4"))

        assert error is None

    def test_returns_error_on_failure(self) -> None:
        module = VideoTranscribeModule()
        proc = MagicMock()
        proc.returncode = 1
        proc.stderr = "Download failed"

        with patch("subprocess.run", return_value=proc):
            error = module._download_video("https://youtu.be/abc", Path("/tmp/video.mp4"))

        assert error is not None
        assert error.error_type == "parse_error"

    def test_returns_timeout_error(self) -> None:
        module = VideoTranscribeModule()
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("yt-dlp", 300)):
            error = module._download_video("https://youtu.be/abc", Path("/tmp/video.mp4"))

        assert error is not None
        assert error.error_type == "timeout"


# ---------------------------------------------------------------------------
# Audio extraction
# ---------------------------------------------------------------------------


class TestExtractAudio:
    def test_succeeds_on_zero_returncode(self) -> None:
        module = VideoTranscribeModule()
        proc = MagicMock()
        proc.returncode = 0
        proc.stderr = ""

        with patch("subprocess.run", return_value=proc):
            error = module._extract_audio(Path("/tmp/video.mp4"), Path("/tmp/audio.wav"))

        assert error is None

    def test_returns_error_on_ffmpeg_failure(self) -> None:
        module = VideoTranscribeModule()
        proc = MagicMock()
        proc.returncode = 1
        proc.stderr = "ffmpeg error output"

        with patch("subprocess.run", return_value=proc):
            error = module._extract_audio(Path("/tmp/video.mp4"), Path("/tmp/audio.wav"))

        assert error is not None
        assert error.error_type == "parse_error"

    def test_returns_error_when_ffmpeg_missing(self) -> None:
        module = VideoTranscribeModule()
        with patch("subprocess.run", side_effect=FileNotFoundError):
            error = module._extract_audio(Path("/tmp/video.mp4"), Path("/tmp/audio.wav"))

        assert error is not None
        assert "ffmpeg not found" in error.message


# ---------------------------------------------------------------------------
# Transcription
# ---------------------------------------------------------------------------


class TestTranscribe:
    def test_reads_output_txt_file(self, tmp_path: Path) -> None:
        module = VideoTranscribeModule()
        audio_path = tmp_path / "audio.wav"
        audio_path.touch()

        # Whisper writes audio.txt in the same dir
        txt_path = tmp_path / "audio.txt"
        txt_path.write_text("Hello this is a transcript", encoding="utf-8")

        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = ""

        with patch("subprocess.run", return_value=proc):
            transcript, error = module._transcribe(audio_path, "base")

        assert error is None
        assert "Hello this is a transcript" in transcript

    def test_falls_back_to_stdout(self, tmp_path: Path) -> None:
        module = VideoTranscribeModule()
        audio_path = tmp_path / "audio.wav"
        audio_path.touch()
        # No .txt file written

        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = "Transcript from stdout"

        with patch("subprocess.run", return_value=proc):
            transcript, error = module._transcribe(audio_path, "base")

        assert error is None
        assert "Transcript from stdout" in transcript

    def test_returns_error_on_failure(self, tmp_path: Path) -> None:
        module = VideoTranscribeModule()
        audio_path = tmp_path / "audio.wav"
        audio_path.touch()

        proc = MagicMock()
        proc.returncode = 1
        proc.stderr = "whisper error"
        proc.stdout = ""

        with patch("subprocess.run", return_value=proc):
            _, error = module._transcribe(audio_path, "base")

        assert error is not None
        assert error.error_type == "parse_error"

    def test_returns_timeout_error(self, tmp_path: Path) -> None:
        module = VideoTranscribeModule()
        audio_path = tmp_path / "audio.wav"
        audio_path.touch()

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("whisper", 600)):
            _, error = module._transcribe(audio_path, "base")

        assert error is not None
        assert error.error_type == "timeout"

    def test_returns_error_when_whisper_missing(self, tmp_path: Path) -> None:
        module = VideoTranscribeModule()
        audio_path = tmp_path / "audio.wav"
        audio_path.touch()

        with patch("subprocess.run", side_effect=FileNotFoundError):
            _, error = module._transcribe(audio_path, "base")

        assert error is not None
        assert "whisper not found" in error.message


# ---------------------------------------------------------------------------
# Walkthrough extraction
# ---------------------------------------------------------------------------


class TestExtractWalkthroughs:
    def test_parses_valid_llm_json(self) -> None:
        module = VideoTranscribeModule()
        services = _make_services(llm_response=_sample_walkthroughs_json())
        video_info = _VideoInfo(url="https://youtu.be/abc", title="Trello Tutorial")

        walkthroughs, errors = module._extract_walkthroughs(
            transcript="A long transcript...",
            video_info=video_info,
            target="trello.com",
            services=services,
        )

        assert errors == []
        assert len(walkthroughs) == 2
        assert walkthroughs[0].feature == "create-board"
        assert walkthroughs[1].feature == "add-card"

    def test_walkthrough_has_video_context(self) -> None:
        module = VideoTranscribeModule()
        services = _make_services(llm_response=_sample_walkthroughs_json())
        video_info = _VideoInfo(
            url="https://youtu.be/abc", title="Trello Tutorial", duration_seconds=300
        )

        walkthroughs, _ = module._extract_walkthroughs(
            transcript="A long transcript...",
            video_info=video_info,
            target="trello.com",
            services=services,
        )

        for wt in walkthroughs:
            assert wt.video_url == "https://youtu.be/abc"
            assert wt.video_title == "Trello Tutorial"

    def test_returns_error_on_invalid_json(self) -> None:
        module = VideoTranscribeModule()
        services = _make_services(llm_response="not valid json {{{{")
        video_info = _VideoInfo(url="https://youtu.be/abc")

        walkthroughs, errors = module._extract_walkthroughs(
            transcript="transcript",
            video_info=video_info,
            target="trello.com",
            services=services,
        )

        assert walkthroughs == []
        assert len(errors) > 0
        assert errors[0].error_type == "parse_error"

    def test_returns_error_when_llm_returns_none(self) -> None:
        module = VideoTranscribeModule()
        services = _make_services(llm_response=None)
        video_info = _VideoInfo(url="https://youtu.be/abc")

        walkthroughs, errors = module._extract_walkthroughs(
            transcript="transcript",
            video_info=video_info,
            target="trello.com",
            services=services,
        )

        assert walkthroughs == []
        assert len(errors) > 0

    def test_skips_items_without_feature(self) -> None:
        module = VideoTranscribeModule()
        bad_json = json.dumps([
            {"feature": "", "title": "No Feature"},
            {"feature": "valid-feature", "title": "Good One", "description": "desc"},
        ])
        services = _make_services(llm_response=bad_json)
        video_info = _VideoInfo(url="https://youtu.be/abc")

        walkthroughs, errors = module._extract_walkthroughs(
            transcript="transcript",
            video_info=video_info,
            target="trello.com",
            services=services,
        )

        assert len(walkthroughs) == 1
        assert walkthroughs[0].feature == "valid-feature"

    def test_handles_empty_array_response(self) -> None:
        module = VideoTranscribeModule()
        services = _make_services(llm_response="[]")
        video_info = _VideoInfo(url="https://youtu.be/abc")

        walkthroughs, errors = module._extract_walkthroughs(
            transcript="transcript",
            video_info=video_info,
            target="trello.com",
            services=services,
        )

        assert walkthroughs == []
        assert errors == []


# ---------------------------------------------------------------------------
# Fact creation
# ---------------------------------------------------------------------------


class TestWalkthroughToFact:
    def test_creates_user_flow_fact(self) -> None:
        module = VideoTranscribeModule()
        wt = _Walkthrough(
            feature="create-board",
            title="Creating a Board",
            description="Learn to create boards",
            steps=["Step 1", "Step 2"],
            ui_elements=["+ button"],
            timestamp_hint="0:30",
            video_url="https://youtu.be/abc",
            video_title="Trello Tutorial",
            transcript_excerpt="Click the plus button...",
        )

        fact = module._walkthrough_to_fact(wt, run_id="run-001")

        assert fact.category == FactCategory.USER_FLOW
        assert fact.feature == "create-board"
        assert fact.authority == Authority.OBSERVATIONAL
        assert fact.source_type == SourceType.VIDEO
        assert fact.module_name == "video_transcribe"
        assert fact.run_id == "run-001"

    def test_fact_has_evidence_ref(self) -> None:
        module = VideoTranscribeModule()
        wt = _Walkthrough(
            feature="add-card",
            title="Adding a Card",
            description="desc",
            video_url="https://youtu.be/abc",
            video_title="Tutorial",
            transcript_excerpt="some text",
        )

        fact = module._walkthrough_to_fact(wt, run_id="run-001")

        assert len(fact.evidence) == 1
        assert fact.evidence[0].source_url == "https://youtu.be/abc"
        assert fact.evidence[0].source_title == "Tutorial"

    def test_claim_contains_title(self) -> None:
        module = VideoTranscribeModule()
        wt = _Walkthrough(
            feature="drag-drop",
            title="Drag and Drop Cards",
            description="Move cards between lists",
            video_url="https://youtu.be/abc",
        )

        fact = module._walkthrough_to_fact(wt, run_id="run-001")

        assert "Drag and Drop Cards" in fact.claim

    def test_structured_data_contains_steps(self) -> None:
        module = VideoTranscribeModule()
        wt = _Walkthrough(
            feature="create-board",
            title="Creating a Board",
            description="desc",
            steps=["Click +", "Enter name"],
            ui_elements=["+ button"],
            video_url="https://youtu.be/abc",
        )

        fact = module._walkthrough_to_fact(wt, run_id="run-001")

        assert fact.structured_data["steps"] == ["Click +", "Enter name"]
        assert fact.structured_data["ui_elements"] == ["+ button"]


# ---------------------------------------------------------------------------
# Full end-to-end happy path (all tools mocked)
# ---------------------------------------------------------------------------


class TestFullPipeline:
    def test_happy_path_produces_facts(self) -> None:
        """Full pipeline with all tools mocked — should produce USER_FLOW facts."""
        module = VideoTranscribeModule()
        url = "https://www.youtube.com/watch?v=trello-tutorial"
        request = _make_request(module_config={"video_urls": [url]})
        services = _make_services(llm_response=_sample_walkthroughs_json())

        with (
            patch.object(
                module,
                "_get_video_info",
                return_value=(
                    _VideoInfo(url=url, title="Trello Tutorial", duration_seconds=300),
                    None,
                ),
            ),
            patch.object(module, "_download_video", return_value=None),
            patch.object(module, "_extract_audio", return_value=None),
            patch.object(
                module,
                "_transcribe",
                return_value=("Welcome to this Trello tutorial...", None),
            ),
        ):
            result = _run(module.run(request, services))

        assert result.module == "video_transcribe"
        assert result.status == ReconModuleStatus.SUCCESS
        assert len(result.facts) == 2
        assert all(f.category == FactCategory.USER_FLOW for f in result.facts)
        assert all(f.authority == Authority.OBSERVATIONAL for f in result.facts)
        assert all(f.source_type == SourceType.VIDEO for f in result.facts)
        assert all(f.module_name == "video_transcribe" for f in result.facts)
        assert all(f.run_id == "test-run-001" for f in result.facts)
        assert url in result.urls_visited

    def test_partial_status_when_some_videos_fail(self) -> None:
        """If some videos fail but some produce facts, status should be PARTIAL."""
        module = VideoTranscribeModule()
        url1 = "https://youtu.be/good"
        url2 = "https://youtu.be/bad"
        request = _make_request(module_config={"video_urls": [url1, url2]})
        services = _make_services(llm_response=_sample_walkthroughs_json())

        def fake_get_info(url: str) -> tuple[_VideoInfo, Any]:
            if url == url1:
                return _VideoInfo(url=url, title="Good Video", duration_seconds=300), None
            # url2 fails
            from scripts.recon.base import ReconError
            return _VideoInfo(url=url), ReconError(
                source_url=url,
                error_type="parse_error",
                message="Video unavailable",
                recoverable=False,
            )

        with (
            patch.object(module, "_get_video_info", side_effect=fake_get_info),
            patch.object(module, "_download_video", return_value=None),
            patch.object(module, "_extract_audio", return_value=None),
            patch.object(
                module,
                "_transcribe",
                return_value=("Some transcript", None),
            ),
        ):
            result = _run(module.run(request, services))

        assert result.status == ReconModuleStatus.PARTIAL
        assert len(result.facts) > 0
        assert len(result.errors) > 0

    def test_failed_status_when_all_videos_fail(self) -> None:
        """If all videos fail and no facts produced, status should be FAILED."""
        module = VideoTranscribeModule()
        url = "https://youtu.be/unavailable"
        request = _make_request(module_config={"video_urls": [url]})
        services = _make_services()

        from scripts.recon.base import ReconError as RE

        with patch.object(
            module,
            "_get_video_info",
            return_value=(
                _VideoInfo(url=url),
                RE(
                    source_url=url,
                    error_type="parse_error",
                    message="Gone",
                    recoverable=False,
                ),
            ),
        ):
            result = _run(module.run(request, services))

        assert result.status == ReconModuleStatus.FAILED
        assert result.facts == []

    def test_progress_callback_is_called(self) -> None:
        """Progress events should be emitted throughout the pipeline."""
        module = VideoTranscribeModule()
        url = "https://youtu.be/abc"
        request = _make_request(module_config={"video_urls": [url]})
        services = _make_services(llm_response=_sample_walkthroughs_json())

        progress_events: list[Any] = []

        with (
            patch.object(
                module,
                "_get_video_info",
                return_value=(_VideoInfo(url=url, title="T", duration_seconds=100), None),
            ),
            patch.object(module, "_download_video", return_value=None),
            patch.object(module, "_extract_audio", return_value=None),
            patch.object(module, "_transcribe", return_value=("transcript", None)),
        ):
            _run(module.run(request, services, progress=progress_events.append))

        assert len(progress_events) > 0
        phases = {e.phase for e in progress_events}
        assert "init" in phases
        assert "complete" in phases

    def test_metrics_populated(self) -> None:
        module = VideoTranscribeModule()
        url = "https://youtu.be/abc"
        request = _make_request(module_config={"video_urls": [url]})
        services = _make_services(llm_response=_sample_walkthroughs_json())

        with (
            patch.object(
                module,
                "_get_video_info",
                return_value=(_VideoInfo(url=url, title="T", duration_seconds=100), None),
            ),
            patch.object(module, "_download_video", return_value=None),
            patch.object(module, "_extract_audio", return_value=None),
            patch.object(module, "_transcribe", return_value=("transcript", None)),
        ):
            result = _run(module.run(request, services))

        assert "videos_processed" in result.metrics
        assert "facts_produced" in result.metrics
        assert result.metrics["videos_processed"] == 1


# ---------------------------------------------------------------------------
# validate_prerequisites
# ---------------------------------------------------------------------------


class TestValidatePrerequisites:
    def test_all_present_returns_empty(self) -> None:
        module = VideoTranscribeModule()

        with patch("shutil.which", return_value="/usr/bin/tool"):
            missing = asyncio.run(module.validate_prerequisites())

        assert missing == []

    def test_missing_tools_listed(self) -> None:
        module = VideoTranscribeModule()

        def fake_which(tool: str) -> str | None:
            return None if tool == "yt-dlp" else "/usr/bin/tool"

        with patch("shutil.which", side_effect=fake_which):
            missing = asyncio.run(module.validate_prerequisites())

        assert "yt-dlp" in missing
        assert "ffmpeg" not in missing
        assert "whisper" not in missing
