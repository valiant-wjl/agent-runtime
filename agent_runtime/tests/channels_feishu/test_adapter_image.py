"""Tests for LarkChannel.download_image — lark-cli image download wrapper.

Mocks asyncio.create_subprocess_exec; uses tmp_path for filesystem assertions.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agent_runtime.channels.feishu.adapter import Channel, ImageDownloadFailed, ImageTooLarge


def _make_channel():
    return Channel({"lark_cli": "lark-cli"})


def _fake_proc(returncode=0, stderr=b""):
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(b"", stderr))
    proc.wait = AsyncMock(return_value=returncode)
    proc.terminate = lambda: None
    proc.kill = lambda: None
    return proc


def _writing_spawn(dest_dir: Path, image_key: str, ext: str = "png", size: int = 100, returncode: int = 0):
    """Build a side_effect for create_subprocess_exec that simulates lark-cli
    writing dest_dir/<image_key>.<ext> with `size` bytes, then returns a
    mock proc with the given returncode."""
    async def side_effect(*args, **kwargs):
        if returncode == 0:
            # simulate lark-cli writing the file (cwd is dest_dir)
            cwd = Path(kwargs.get("cwd", dest_dir))
            (cwd / f"{image_key}.{ext}").write_bytes(b"x" * size)
        return _fake_proc(returncode=returncode)
    return side_effect


@pytest.mark.asyncio
async def test_download_image_success_returns_path(tmp_path):
    ch = _make_channel()
    spawn = AsyncMock(side_effect=_writing_spawn(tmp_path, "img_xxx"))
    with patch("asyncio.create_subprocess_exec", spawn):
        path = await ch.download_image(
            message_id="om_msg1", image_key="img_xxx", dest_dir=tmp_path,
        )
    assert path == tmp_path / "img_xxx.png"
    assert path.exists()


@pytest.mark.asyncio
async def test_download_image_passes_correct_subprocess_args(tmp_path):
    ch = _make_channel()
    captured = {}

    async def capture(*args, **kwargs):
        captured["args"] = args
        captured["cwd"] = kwargs.get("cwd")
        # simulate file write
        (Path(kwargs["cwd"]) / "img_yyy.jpg").write_bytes(b"x" * 50)
        return _fake_proc(0)

    with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=capture)):
        await ch.download_image(
            message_id="om_msg1", image_key="img_yyy", dest_dir=tmp_path,
        )

    args = captured["args"]
    assert args[0] == "lark-cli"
    assert "im" in args
    assert "+messages-resources-download" in args
    # required flags + values present
    assert "--message-id" in args and args[args.index("--message-id") + 1] == "om_msg1"
    assert "--file-key" in args and args[args.index("--file-key") + 1] == "img_yyy"
    assert "--type" in args and args[args.index("--type") + 1] == "image"
    # --output basename equals image_key (lark-cli appends ext)
    assert "--output" in args and args[args.index("--output") + 1] == "img_yyy"
    # IM operations must run as bot — current lark-cli user token has zero im:
    # scopes on this deployment (verified via `lark-cli auth scopes`), and the
    # rest of the codebase (reply.py:55, adapter.py:132,227) follows the same
    # convention.
    assert "--as" in args and args[args.index("--as") + 1] == "bot"
    # cwd = dest_dir (relative-only constraint)
    assert captured["cwd"] == str(tmp_path)


@pytest.mark.asyncio
async def test_download_image_nonzero_exit_raises(tmp_path):
    ch = _make_channel()

    async def fail_spawn(*args, **kwargs):
        return _fake_proc(returncode=1, stderr=b"network error")

    with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=fail_spawn)):
        with pytest.raises(ImageDownloadFailed) as exc:
            await ch.download_image(
                message_id="om_msg1", image_key="img_xxx", dest_dir=tmp_path,
            )
    assert "network error" in str(exc.value) or "exit 1" in str(exc.value)


@pytest.mark.asyncio
async def test_download_image_no_file_produced_raises(tmp_path):
    """exit 0 but lark-cli wrote nothing — defensive raise."""
    ch = _make_channel()

    async def empty_spawn(*args, **kwargs):
        return _fake_proc(returncode=0)

    with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=empty_spawn)):
        with pytest.raises(ImageDownloadFailed):
            await ch.download_image(
                message_id="om_msg1", image_key="img_zzz", dest_dir=tmp_path,
            )


@pytest.mark.asyncio
async def test_download_image_oversized_raises_and_removes(tmp_path):
    ch = _make_channel()
    spawn = AsyncMock(side_effect=_writing_spawn(tmp_path, "img_big", ext="png", size=2000))
    with patch("asyncio.create_subprocess_exec", spawn):
        with pytest.raises(ImageTooLarge):
            await ch.download_image(
                message_id="om_msg1", image_key="img_big", dest_dir=tmp_path, max_bytes=1000,
            )
    # offending file must be deleted
    assert not (tmp_path / "img_big.png").exists()


@pytest.mark.asyncio
async def test_download_image_timeout_raises(tmp_path):
    ch = _make_channel()

    async def hang_spawn(*args, **kwargs):
        proc = AsyncMock()
        proc.returncode = None

        async def never_returns(*a, **k):
            await asyncio.sleep(10)
            return (b"", b"")

        proc.communicate = AsyncMock(side_effect=never_returns)
        proc.wait = AsyncMock(return_value=0)
        proc.terminate = lambda: None
        proc.kill = lambda: None
        return proc

    with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=hang_spawn)):
        with pytest.raises(ImageDownloadFailed) as exc:
            await ch.download_image(
                message_id="om_msg1", image_key="img_slow",
                dest_dir=tmp_path, timeout=0.05,
            )
    assert "tim" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_download_image_spawn_filenotfound_raises(tmp_path):
    """lark-cli not on PATH — raise ImageDownloadFailed (not bubble up)."""
    ch = _make_channel()
    with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError("no lark-cli")):
        with pytest.raises(ImageDownloadFailed):
            await ch.download_image(
                message_id="om_msg1", image_key="img_xxx", dest_dir=tmp_path,
            )
