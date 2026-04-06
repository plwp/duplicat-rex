"""
Video Transcription Module — VideoTranscribeModule.

Downloads training/tutorial videos, extracts audio, transcribes with whisper,
then uses an LLM to extract feature walkthroughs from the transcript.

Produces one Fact per feature walkthrough with category=USER_FLOW.

INV-020: run() MUST NOT raise.
INV-013: All facts have authority=OBSERVATIONAL.
INV-001: Every Fact has at least one EvidenceRef.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.models import (
    Authority,
    Confidence,
    EvidenceRef,
    Fact,
    FactCategory,
    SourceType,
)
from scripts.recon.base import (
    ReconError,
    ReconModule,
    ReconModuleStatus,
    ReconProgress,
    ReconRequest,
    ReconResult,
    ReconServices,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default search queries to find tutorial videos when none configured
_DEFAULT_SEARCH_QUERIES = [
    "{target} tutorial",
    "{target} walkthrough",
    "{target} how to use",
    "{target} getting started",
]

# Maximum number of videos to process (safety valve)
_MAX_VIDEOS = 10

# Whisper model to use (can be overridden via module_config)
_DEFAULT_WHISPER_MODEL = "base"

# Maximum video duration in seconds (skip longer ones — likely not tutorials)
_MAX_DURATION_SECONDS = 3600  # 1 hour

# LLM prompt for feature walkthrough extraction
_EXTRACTION_PROMPT = "\n".join([
    "You are analysing a transcript from a tutorial or walkthrough video about {target}.",
    "",
    "Extract all distinct feature walkthroughs described in the transcript.",
    "For each walkthrough:",
    "1. Identify the feature being demonstrated (e.g. creating a board, adding a card)",
    "2. Describe the user flow step-by-step",
    "3. Note any UI elements, buttons, or menus mentioned",
    "",
    "Return a JSON array. Each element must have:",
    '  - "feature": short slug (e.g. "create-board"), lowercase, hyphens not spaces',
    '  - "title": human-readable name (e.g. "Creating a Board")',
    '  - "description": concise description of what the user does',
    '  - "steps": list of step strings describing the walkthrough sequence',
    '  - "ui_elements": list of UI element names mentioned (buttons, menus, etc.)',
    '  - "timestamp_hint": approximate video position if mentioned (e.g. "0:30", "")',
    "",
    "Return ONLY valid JSON — no markdown fences, no preamble.",
    "If no walkthroughs found, return [].",
    "",
    "Transcript:",
    "{transcript}",
])


# ---------------------------------------------------------------------------
# Internal data containers
# ---------------------------------------------------------------------------


@dataclass
class _VideoInfo:
    """Metadata about a discovered video."""

    url: str
    title: str = ""
    duration_seconds: float = 0.0
    description: str = ""
    uploader: str = ""
    upload_date: str = ""


@dataclass
class _Walkthrough:
    """A single feature walkthrough extracted from a transcript."""

    feature: str
    title: str
    description: str
    steps: list[str] = field(default_factory=list)
    ui_elements: list[str] = field(default_factory=list)
    timestamp_hint: str = ""
    video_url: str = ""
    video_title: str = ""
    transcript_excerpt: str = ""


# ---------------------------------------------------------------------------
# Module
# ---------------------------------------------------------------------------


class VideoTranscribeModule(ReconModule):
    """
    Downloads and transcribes training/tutorial videos to extract feature walkthroughs.

    Strategy:
      1. Find tutorial videos via yt-dlp search (or use configured URLs).
      2. Download each video.
      3. Extract audio with ffmpeg.
      4. Transcribe audio with whisper.
      5. Use LLM to extract feature walkthroughs from transcript.
      6. Produce one Fact per walkthrough with category=USER_FLOW.

    All external tools (yt-dlp, ffmpeg, whisper) are invoked as subprocesses
    so they can be mocked in tests.
    """

    # --- ReconModule interface ---

    @property
    def name(self) -> str:
        return "video_transcribe"

    @property
    def authority(self) -> Authority:
        return Authority.OBSERVATIONAL

    @property
    def source_type(self) -> SourceType:
        return SourceType.VIDEO

    @property
    def requires_credentials(self) -> list[str]:
        return []  # Public videos — no credentials needed

    # --- Main entry point ---

    async def run(
        self,
        request: ReconRequest,
        services: ReconServices,
        progress: Callable[[ReconProgress], None] | None = None,
    ) -> ReconResult:
        """
        Execute video transcription recon.

        ENSURES: ReconResult.module == "video_transcribe".
        ENSURES: run() does not raise (INV-020).
        """
        started_at = datetime.now(UTC).isoformat()
        t0 = time.monotonic()

        def emit(
            phase: str,
            message: str,
            completed: int | None = None,
            total: int | None = None,
        ) -> None:
            if progress:
                progress(
                    ReconProgress(
                        run_id=request.run_id,
                        module=self.name,
                        phase=phase,
                        message=message,
                        completed=completed,
                        total=total,
                    )
                )

        emit("init", f"Starting video transcription recon for {request.target}")

        facts: list[Fact] = []
        errors: list[ReconError] = []
        urls_visited: list[str] = []

        try:
            # Resolve video sources: either configured URLs or search
            video_urls: list[str] = request.module_config.get("video_urls", [])
            max_videos = request.module_config.get("max_videos", _MAX_VIDEOS)
            whisper_model = request.module_config.get("whisper_model", _DEFAULT_WHISPER_MODEL)

            if not video_urls:
                emit("discover", f"Searching for tutorial videos about {request.target}")
                video_urls, search_errors = self._search_videos(request.target, max_videos)
                errors.extend(search_errors)
            else:
                emit("discover", f"Using {len(video_urls)} configured video URL(s)")

            if not video_urls:
                emit("complete", "No videos found — nothing to transcribe")
                return ReconResult(
                    module=self.name,
                    status=ReconModuleStatus.SKIPPED,
                    facts=[],
                    errors=errors,
                    started_at=started_at,
                    finished_at=datetime.now(UTC).isoformat(),
                    duration_seconds=time.monotonic() - t0,
                )

            emit("discover", f"Found {len(video_urls)} video(s) to process", 0, len(video_urls))

            # Process each video in a temporary directory
            with tempfile.TemporaryDirectory(prefix="duplicat-rex-video-") as tmpdir:
                tmp = Path(tmpdir)

                for idx, url in enumerate(video_urls[:max_videos]):
                    emit(
                        "extract",
                        f"Processing video {idx + 1}/{min(len(video_urls), max_videos)}: {url}",
                        idx,
                        min(len(video_urls), max_videos),
                    )

                    video_facts, video_errors = self._process_video(
                        url=url,
                        tmp=tmp,
                        run_id=request.run_id,
                        target=request.target,
                        whisper_model=whisper_model,
                        services=services,
                        emit=emit,
                    )
                    facts.extend(video_facts)
                    errors.extend(video_errors)
                    urls_visited.append(url)

        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error in VideoTranscribeModule.run")
            errors.append(
                ReconError(
                    source_url=None,
                    error_type="parse_error",
                    message=f"Unexpected error: {exc}",
                    recoverable=False,
                )
            )

        finished_at = datetime.now(UTC).isoformat()
        duration = time.monotonic() - t0

        if facts:
            status = ReconModuleStatus.PARTIAL if errors else ReconModuleStatus.SUCCESS
        elif errors:
            status = ReconModuleStatus.FAILED
        else:
            status = ReconModuleStatus.SKIPPED

        emit("complete", f"Done: {len(facts)} facts from {len(urls_visited)} video(s)")

        return ReconResult(
            module=self.name,
            status=status,
            facts=facts,
            errors=errors,
            urls_visited=urls_visited,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=duration,
            metrics={
                "videos_processed": len(urls_visited),
                "facts_produced": len(facts),
                "errors": len(errors),
            },
        )

    # --- Video discovery ---

    def _search_videos(
        self, target: str, max_videos: int
    ) -> tuple[list[str], list[ReconError]]:
        """
        Use yt-dlp to search YouTube for tutorial videos about the target.

        Returns (url_list, errors).
        """
        urls: list[str] = []
        errors: list[ReconError] = []

        for query_template in _DEFAULT_SEARCH_QUERIES:
            if len(urls) >= max_videos:
                break

            query = query_template.format(target=target)
            try:
                result = subprocess.run(  # noqa: S603
                    [
                        "yt-dlp",
                        f"ytsearch{max_videos}:{query}",
                        "--get-url",
                        "--no-playlist",
                        "--flat-playlist",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
                if result.returncode != 0:
                    logger.warning("yt-dlp search failed for query %r: %s", query, result.stderr)
                    errors.append(
                        ReconError(
                            source_url=None,
                            error_type="parse_error",
                            message=f"yt-dlp search failed for {query!r}: {result.stderr[:200]}",
                            recoverable=True,
                        )
                    )
                    continue

                found = [u.strip() for u in result.stdout.splitlines() if u.strip()]
                for url in found:
                    if url not in urls:
                        urls.append(url)
                        if len(urls) >= max_videos:
                            break

            except subprocess.TimeoutExpired:
                errors.append(
                    ReconError(
                        source_url=None,
                        error_type="timeout",
                        message=f"yt-dlp search timed out for query {query!r}",
                        recoverable=True,
                    )
                )
            except FileNotFoundError:
                errors.append(
                    ReconError(
                        source_url=None,
                        error_type="parse_error",
                        message="yt-dlp not found — install it with: pip install yt-dlp",
                        recoverable=False,
                    )
                )
                break

        return urls, errors

    # --- Single video processing ---

    def _process_video(
        self,
        url: str,
        tmp: Path,
        run_id: str,
        target: str,
        whisper_model: str,
        services: ReconServices,
        emit: Callable[[str, str, int | None, int | None], None],
    ) -> tuple[list[Fact], list[ReconError]]:
        """
        Full pipeline for a single video: download -> extract audio -> transcribe -> extract facts.

        Returns (facts, errors). Never raises.
        """
        facts: list[Fact] = []
        errors: list[ReconError] = []

        try:
            # Step 1: Get video metadata
            emit("extract", f"Fetching metadata for {url}")
            video_info, meta_error = self._get_video_info(url)
            if meta_error:
                errors.append(meta_error)
                return facts, errors

            # Step 2: Download video
            video_path = tmp / f"video_{abs(hash(url))}.mp4"
            emit("extract", f"Downloading: {video_info.title or url}")
            dl_error = self._download_video(url, video_path)
            if dl_error:
                errors.append(dl_error)
                return facts, errors

            # Step 3: Extract audio
            audio_path = tmp / f"audio_{abs(hash(url))}.wav"
            emit("extract", "Extracting audio")
            audio_error = self._extract_audio(video_path, audio_path)
            if audio_error:
                errors.append(audio_error)
                return facts, errors

            # Step 4: Transcribe
            emit("extract", f"Transcribing with whisper ({whisper_model})")
            transcript, transcribe_error = self._transcribe(audio_path, whisper_model)
            if transcribe_error:
                errors.append(transcribe_error)
                return facts, errors

            if not transcript.strip():
                logger.warning("Empty transcript for %s — skipping", url)
                return facts, errors

            # Step 5: Extract walkthroughs via LLM
            emit("extract", "Extracting feature walkthroughs from transcript")
            walkthroughs, extract_errors = self._extract_walkthroughs(
                transcript=transcript,
                video_info=video_info,
                target=target,
                services=services,
            )
            errors.extend(extract_errors)

            # Step 6: Convert walkthroughs to Facts
            for wt in walkthroughs:
                fact = self._walkthrough_to_fact(wt, run_id)
                facts.append(fact)

        except Exception as exc:  # noqa: BLE001
            logger.exception("Error processing video %s", url)
            errors.append(
                ReconError(
                    source_url=url,
                    error_type="parse_error",
                    message=f"Error processing video {url}: {exc}",
                    recoverable=False,
                )
            )

        return facts, errors

    # --- External tool wrappers ---

    def _get_video_info(self, url: str) -> tuple[_VideoInfo, ReconError | None]:
        """Fetch video metadata using yt-dlp --dump-json."""
        try:
            result = subprocess.run(  # noqa: S603
                ["yt-dlp", "--dump-json", "--no-playlist", url],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if result.returncode != 0:
                return _VideoInfo(url=url), ReconError(
                    source_url=url,
                    error_type="parse_error",
                    message=f"yt-dlp metadata failed for {url}: {result.stderr[:200]}",
                    recoverable=True,
                )

            data = json.loads(result.stdout)
            duration = float(data.get("duration") or 0)

            if duration > _MAX_DURATION_SECONDS:
                return _VideoInfo(url=url), ReconError(
                    source_url=url,
                    error_type="parse_error",
                    message=(
                        f"Video too long ({duration:.0f}s > {_MAX_DURATION_SECONDS}s) — skipping"
                    ),
                    recoverable=False,
                )

            return (
                _VideoInfo(
                    url=url,
                    title=data.get("title", ""),
                    duration_seconds=duration,
                    description=data.get("description", "")[:500],
                    uploader=data.get("uploader", ""),
                    upload_date=data.get("upload_date", ""),
                ),
                None,
            )

        except subprocess.TimeoutExpired:
            return _VideoInfo(url=url), ReconError(
                source_url=url,
                error_type="timeout",
                message=f"yt-dlp metadata timed out for {url}",
                recoverable=True,
            )
        except FileNotFoundError:
            return _VideoInfo(url=url), ReconError(
                source_url=url,
                error_type="parse_error",
                message="yt-dlp not found — install it with: pip install yt-dlp",
                recoverable=False,
            )
        except json.JSONDecodeError as exc:
            return _VideoInfo(url=url), ReconError(
                source_url=url,
                error_type="parse_error",
                message=f"Failed to parse yt-dlp metadata JSON: {exc}",
                recoverable=False,
            )

    def _download_video(self, url: str, output_path: Path) -> ReconError | None:
        """Download a video using yt-dlp."""
        try:
            result = subprocess.run(  # noqa: S603
                [
                    "yt-dlp",
                    "--no-playlist",
                    "--format",
                    "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                    "--output",
                    str(output_path),
                    url,
                ],
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
            if result.returncode != 0:
                return ReconError(
                    source_url=url,
                    error_type="parse_error",
                    message=f"yt-dlp download failed for {url}: {result.stderr[:200]}",
                    recoverable=True,
                )
            return None

        except subprocess.TimeoutExpired:
            return ReconError(
                source_url=url,
                error_type="timeout",
                message=f"yt-dlp download timed out for {url}",
                recoverable=True,
            )
        except FileNotFoundError:
            return ReconError(
                source_url=url,
                error_type="parse_error",
                message="yt-dlp not found — install it with: pip install yt-dlp",
                recoverable=False,
            )

    def _extract_audio(self, video_path: Path, audio_path: Path) -> ReconError | None:
        """Extract audio from a video file using ffmpeg."""
        try:
            result = subprocess.run(  # noqa: S603
                [
                    "ffmpeg",
                    "-i",
                    str(video_path),
                    "-vn",  # no video
                    "-acodec",
                    "pcm_s16le",  # WAV format whisper expects
                    "-ar",
                    "16000",  # 16kHz sample rate
                    "-ac",
                    "1",  # mono
                    "-y",  # overwrite output
                    str(audio_path),
                ],
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
            if result.returncode != 0:
                return ReconError(
                    source_url=str(video_path),
                    error_type="parse_error",
                    message=f"ffmpeg audio extraction failed: {result.stderr[-200:]}",
                    recoverable=False,
                )
            return None

        except subprocess.TimeoutExpired:
            return ReconError(
                source_url=str(video_path),
                error_type="timeout",
                message="ffmpeg audio extraction timed out",
                recoverable=True,
            )
        except FileNotFoundError:
            return ReconError(
                source_url=str(video_path),
                error_type="parse_error",
                message="ffmpeg not found — install it: https://ffmpeg.org",
                recoverable=False,
            )

    def _transcribe(
        self, audio_path: Path, model: str
    ) -> tuple[str, ReconError | None]:
        """Transcribe audio using OpenAI Whisper CLI."""
        try:
            result = subprocess.run(  # noqa: S603
                [
                    "whisper",
                    str(audio_path),
                    "--model",
                    model,
                    "--output_format",
                    "txt",
                    "--output_dir",
                    str(audio_path.parent),
                    "--fp16",
                    "False",  # avoid GPU requirement
                ],
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
            )
            if result.returncode != 0:
                return "", ReconError(
                    source_url=str(audio_path),
                    error_type="parse_error",
                    message=f"whisper transcription failed: {result.stderr[-200:]}",
                    recoverable=False,
                )

            # whisper writes <audio_filename>.txt in the output dir
            txt_path = audio_path.parent / (audio_path.stem + ".txt")
            if txt_path.exists():
                return txt_path.read_text(encoding="utf-8"), None

            # Fall back to stdout if file not found
            return result.stdout, None

        except subprocess.TimeoutExpired:
            return "", ReconError(
                source_url=str(audio_path),
                error_type="timeout",
                message="whisper transcription timed out",
                recoverable=True,
            )
        except FileNotFoundError:
            return "", ReconError(
                source_url=str(audio_path),
                error_type="parse_error",
                message="whisper not found — install it: pip install openai-whisper",
                recoverable=False,
            )

    # --- LLM extraction ---

    def _extract_walkthroughs(
        self,
        transcript: str,
        video_info: _VideoInfo,
        target: str,
        services: ReconServices,
    ) -> tuple[list[_Walkthrough], list[ReconError]]:
        """
        Use the LLM (via services.http_client or a direct call) to extract
        feature walkthroughs from the transcript.

        Returns (walkthroughs, errors). Never raises.
        """
        walkthroughs: list[_Walkthrough] = []
        errors: list[ReconError] = []

        prompt = _EXTRACTION_PROMPT.format(
            target=target,
            transcript=transcript[:8000],  # Token budget guard
        )

        try:
            # Use spec_store's LLM integration if available, else attempt direct call
            raw_json = self._call_llm(prompt, services)
            if raw_json is None:
                errors.append(
                    ReconError(
                        source_url=video_info.url,
                        error_type="parse_error",
                        message="LLM returned no response for walkthrough extraction",
                        recoverable=True,
                    )
                )
                return walkthroughs, errors

            items: list[dict[str, Any]] = json.loads(raw_json)
            if not isinstance(items, list):
                raise ValueError(f"Expected JSON array, got {type(items).__name__}")

            for item in items:
                if not isinstance(item, dict):
                    continue
                feature = str(item.get("feature", "")).strip()
                if not feature:
                    continue

                walkthroughs.append(
                    _Walkthrough(
                        feature=feature,
                        title=str(item.get("title", feature)),
                        description=str(item.get("description", "")),
                        steps=[str(s) for s in item.get("steps", []) if s],
                        ui_elements=[str(e) for e in item.get("ui_elements", []) if e],
                        timestamp_hint=str(item.get("timestamp_hint", "")),
                        video_url=video_info.url,
                        video_title=video_info.title,
                        transcript_excerpt=transcript[:500],
                    )
                )

        except json.JSONDecodeError as exc:
            errors.append(
                ReconError(
                    source_url=video_info.url,
                    error_type="parse_error",
                    message=f"Failed to parse LLM JSON response: {exc}",
                    recoverable=True,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Error extracting walkthroughs from transcript")
            errors.append(
                ReconError(
                    source_url=video_info.url,
                    error_type="parse_error",
                    message=f"Walkthrough extraction error: {exc}",
                    recoverable=True,
                )
            )

        return walkthroughs, errors

    def _call_llm(self, prompt: str, services: ReconServices) -> str | None:
        """
        Call an LLM to process the prompt.

        Uses services.http_client if it has a `chat` method (injected mock or
        real implementation). Falls back to attempting the anthropic SDK if
        available. Returns raw text response or None.
        """
        # Allow tests / orchestrator to inject an LLM client via http_client
        http_client = services.http_client
        if http_client is not None and hasattr(http_client, "chat"):
            return http_client.chat(prompt)

        # Attempt anthropic SDK as fallback (not a hard dependency for the module)
        try:
            import anthropic  # type: ignore[import]

            # Secrets are fetched from keychain by the orchestrator — we expect
            # the client to have been pre-configured. Construct a minimal client
            # here only as a last resort; never log the key.
            client = anthropic.Anthropic()
            message = client.messages.create(
                model="claude-3-5-haiku-20241022",
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            )
            content = message.content
            if content and hasattr(content[0], "text"):
                return content[0].text
        except ImportError:
            logger.warning(
                "anthropic SDK not installed — cannot call LLM for walkthrough extraction"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM call failed: %s", exc)

        return None

    # --- Fact creation ---

    def _walkthrough_to_fact(self, wt: _Walkthrough, run_id: str) -> Fact:
        """Convert a _Walkthrough to a Fact."""
        # Build human-readable claim
        steps_summary = (
            f" Steps: {'; '.join(wt.steps[:3])}{'...' if len(wt.steps) > 3 else ''}."
            if wt.steps
            else ""
        )
        claim = f"Video walkthrough demonstrates '{wt.title}': {wt.description}.{steps_summary}"
        claim = claim[:500]  # Keep within reasonable length

        locator = wt.timestamp_hint or "video"

        evidence = EvidenceRef(
            source_url=wt.video_url,
            locator=locator,
            source_title=wt.video_title or None,
            raw_excerpt=wt.transcript_excerpt[:2000] if wt.transcript_excerpt else None,
        )

        structured_data: dict[str, Any] = {
            "feature": wt.feature,
            "title": wt.title,
            "description": wt.description,
            "steps": wt.steps,
            "ui_elements": wt.ui_elements,
            "timestamp_hint": wt.timestamp_hint,
            "video_url": wt.video_url,
            "video_title": wt.video_title,
        }

        return Fact(
            feature=wt.feature,
            category=FactCategory.USER_FLOW,
            claim=claim,
            evidence=[evidence],
            source_type=self.source_type,
            structured_data=structured_data,
            module_name=self.name,
            authority=self.authority,
            confidence=Confidence.MEDIUM,
            run_id=run_id,
        )

    async def validate_prerequisites(self) -> list[str]:
        """Check that yt-dlp, ffmpeg, and whisper are installed."""
        missing: list[str] = []
        for tool in ("yt-dlp", "ffmpeg", "whisper"):
            if shutil.which(tool) is None:
                missing.append(tool)
        return missing
