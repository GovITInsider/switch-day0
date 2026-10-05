from app.markdown import render_markdown


def test_workflow_markdown_renders_a_page_and_escapes_html():
    page = render_markdown(
        "\n".join(
            [
                "# Unbox the switch",
                "",
                "Check the **serial** before you rack it.",
                "",
                "- Label the asset",
                "- Read the <script>alert(1)</script> plate",
                "",
                "1. Console first",
                "2. Then the network",
                "",
                "Use `show version` and the [notes](https://example.com/switches).",
                "Skip [this](javascript:alert(1)).",
                "",
                "```",
                "show version",
                "<script>alert(1)</script>",
                "```",
            ]
        )
    )
    assert "<h2>Unbox the switch</h2>" in page
    assert "<strong>serial</strong>" in page
    assert "<li>Label the asset</li>" in page
    assert "<li>Read the &lt;script&gt;alert(1)&lt;/script&gt; plate</li>" in page
    assert "<li>Console first</li>" in page
    assert "<code>show version</code>" in page
    assert '<a href="https://example.com/switches">notes</a>' in page
    assert 'href="javascript:' not in page
    assert "<pre><code>show version\n&lt;script&gt;alert(1)&lt;/script&gt;</code></pre>" in page
    assert "<script>" not in page
