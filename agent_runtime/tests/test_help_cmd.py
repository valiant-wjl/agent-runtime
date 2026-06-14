"""Tests for runtime/help_cmd.py — `/help` slash command.

Spec:
  - `/help` (with or without body) → reply unified help text listing
    every DM-usable slash command, regardless of admin role.
  - Recognizer follows the same shape as /lesson, /alert, /agent so
    scheduler integration is symmetric.
"""

from agent_runtime import help_cmd


# --- is_help_command ---


def test_is_help_command_recognizes_slash_help():
    assert help_cmd.is_help_command("/help")


def test_is_help_command_recognizes_with_trailing_args():
    """Bare `/help args` is still the command — args are ignored, we
    always render the full menu."""
    assert help_cmd.is_help_command("/help foo bar")


def test_is_help_command_strips_leading_whitespace():
    assert help_cmd.is_help_command("  /help")


def test_is_help_command_rejects_normal_text():
    assert not help_cmd.is_help_command("普通问题")


def test_is_help_command_rejects_help_substring_midsentence():
    """Don't match `/help` inside a sentence — only as a prefix command."""
    assert not help_cmd.is_help_command("用 /help 看命令")


def test_is_help_command_rejects_similar_prefix():
    """`/helper`, `/help2` must NOT match — prefix needs EOS/whitespace."""
    assert not help_cmd.is_help_command("/helper")
    assert not help_cmd.is_help_command("/help2")


def test_is_help_command_empty_text():
    assert not help_cmd.is_help_command("")


# --- render_help ---


def test_render_help_lists_all_known_commands():
    """Every slash command the user can type in DM must appear by name
    so /help is a complete index, not a partial hint."""
    text = help_cmd.render_help()
    for verb in ("/help", "/lesson", "/alert", "/agent show", "/agent alert"):
        assert verb in text, f"missing {verb!r} in help text:\n{text}"


def test_render_help_marks_admin_only_commands():
    """register/remove are admin-gated by agent_admin._is_admin; user
    needs to know not to expect them to work as a non-admin."""
    text = help_cmd.render_help()
    # We just need *some* admin marker near alert register/remove. Don't
    # over-constrain wording — but ensure it's flagged.
    assert "admin" in text.lower() or "管理" in text
