"""Clean tests migrated from feishu-agent-gateway/tests/test_clean_feishu.py."""

from agent_runtime.channels.feishu.clean import clean_for_feishu


def test_br_tags_removed():
    """<br> and <br/> should be converted to newlines."""
    assert clean_for_feishu("line1<br>line2") == "line1\nline2"
    assert clean_for_feishu("line1<br/>line2") == "line1\nline2"
    assert clean_for_feishu("line1<br />line2") == "line1\nline2"


def test_html_tags_stripped():
    """HTML block tags should be removed entirely."""
    assert clean_for_feishu("<div>hello</div>") == "hello"
    assert clean_for_feishu("<span>world</span>") == "world"
    assert clean_for_feishu("<table><tr><td>cell</td></tr></table>") == "cell"
    assert clean_for_feishu("<p>para</p>") == "para"
    assert clean_for_feishu("<thead><th>h</th></thead>") == "h"
    assert clean_for_feishu("<tbody><tr><td>r</td></tr></tbody>") == "r"


def test_double_newlines_collapsed():
    """Multiple consecutive newlines should be collapsed to a single newline."""
    assert clean_for_feishu("a\n\nb") == "a\nb"
    assert clean_for_feishu("a\n\n\nb") == "a\nb"
    assert clean_for_feishu("a\n\n\n\nb") == "a\nb"


def test_mixed_cleanup():
    """Combined scenario: bold heading + br tags + list item + double newline."""
    raw = "**标题**<br><br>- 列表项\n\n代码"
    result = clean_for_feishu(raw)
    # <br><br> → \n\n → collapsed to \n
    # \n\n also collapsed to \n
    assert "<br" not in result
    assert "\n\n" not in result
    assert "**标题**" in result
    assert "- 列表项" in result
    assert "代码" in result


def test_br_with_attributes():
    """<br> with attributes and mixed case should also be converted to newlines."""
    assert clean_for_feishu('a<br class="x">b') == "a\nb"
    assert clean_for_feishu('a<BR />b') == "a\nb"


def test_strip_code_pre_a_tags():
    """code/pre/a tags are stripped (Feishu does not render them as HTML)."""
    assert clean_for_feishu("<code>x</code>") == "x"
    assert clean_for_feishu("<a href='y'>link</a>") == "link"
