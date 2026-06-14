"""Clean Claude output for Feishu markdown rendering.

Feishu markdown renderer has quirks:
- lark-cli --markdown converts blank lines (\\n\\n) into <br> tags, breaking
  code block rendering
- Certain HTML tags (div/span/p/table etc.) are not rendered by Feishu
- <br> tags from any source should be normalized to single newlines
"""

import re


def clean_for_feishu(text: str) -> str:
    """Normalize text for Feishu markdown rendering.

    1. Collapse any <br>/<br/> (including with attributes) to newline
    2. Strip unsupported HTML tags (div/span/p/table/tr/td/th/thead/tbody/
       code/pre/a/ul/ol/li)
    3. Collapse consecutive newlines (prevents <br> insertion by lark-cli)
    4. Strip leading/trailing whitespace
    """
    # <br>, <br/>, <br />, <br class="x">, <BR /> etc. → newline
    text = re.sub(r"<br\b[^>]*/?>", "\n", text, flags=re.IGNORECASE)
    # Strip tags Feishu can't render (block + inline + list)
    text = re.sub(
        r"</?(?:div|span|p|table|tr|td|th|thead|tbody|code|pre|a|ul|ol|li)[^>]*>",
        "",
        text,
    )
    # Collapse double+ newlines to single newline
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()
