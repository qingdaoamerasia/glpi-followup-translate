"""Quick test: create one ticket and verify translation format."""

import sys
import os
import logging

# Set UTF-8 encoding for Windows console
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# Add parent directory to path so we can import the package
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from glpi_followup_translate.config import load_config
from glpi_followup_translate.glpi_client import GlpiClient
from glpi_followup_translate.ollama_client import OllamaClient
from glpi_followup_translate.main import (
    setup_logging,
    run_once,
    ProcessedState,
    has_html_tags,
)

# Single test ticket with Chinese title, rich-text description, and mixed followups
TEST_TICKET = {
    "name": "服务器无法连接数据库",
    "content": (
        '<p><strong>生产环境</strong>服务器从今天早上开始'
        '无法连接到<span style="color: rgb(255, 0, 0);">MySQL数据库</span>，'
        '报错信息为"Connection refused"。<br>'
        '<em>已检查数据库服务状态，确认数据库服务正在运行。</em></p>'
    ),
    "followups": [
        "检查了防火墙规则，发现3306端口被意外关闭。已重新开放端口。",
        "<p><strong>After opening the port</strong>, the connection was restored temporarily but <span style=\"color: red;\">went down again</span> after 10 minutes.</p>",
        "经排查发现是数据库连接池耗尽导致的问题。已将最大连接数从100调整到500。",
    ],
}


def verify_content_format(label, content, item_id):
    """Verify the translation format for a content item."""
    marker = "[AUTO-TRANSLATED]"
    if marker not in content:
        print(f"  ?  {label} #{item_id} may not have been translated (no marker found)")
        return

    if has_html_tags(content):
        # HTML content: should use <br> tags
        sep = "<br><br>"
        if sep in content:
            print(f"  OK {label} #{item_id} format correct: uses <br><br> + {marker} (HTML)")
            parts = content.split(sep, 1)
            if len(parts) == 2:
                before = parts[0]
                after = parts[1]
                after_parts = after.split("<br>", 1)
                if len(after_parts) == 2:
                    print(f"     原文(HTML): {before[:120]}...")
                    print(f"     标记部分: {after_parts[0][:100]}")
                    print(f"     翻译(HTML): {after_parts[1][:120]}...")
                else:
                    print(f"     原文(HTML): {before[:120]}...")
                    print(f"     之后: {after[:120]}...")
        else:
            print(f"  XX {label} #{item_id} format WRONG: HTML content missing <br><br> separator")
    else:
        # Plain text
        sep = "\n\n"
        if sep in content:
            print(f"  OK {label} #{item_id} format correct: uses \\n\\n + {marker} (plain)")
            parts = content.split(sep, 1)
            if len(parts) == 2:
                before, after = parts
                after_parts = after.split("\n", 1)
                if len(after_parts) == 2:
                    print(f"     原文: {before[:80]}...")
                    print(f"     标记: {after_parts[0]}")
                    print(f"     翻译: {after_parts[1][:80]}...")
        else:
            print(f"  XX {label} #{item_id} format WRONG: plain text missing double newline")


def main():
    config = load_config()
    setup_logging(config)

    # Enable debug logging for details
    logging.getLogger("glpi_followup_translate").setLevel(logging.DEBUG)

    glpi = GlpiClient(config.glpi)
    ollama = OllamaClient(config.ollama)
    state = ProcessedState()

    # Check Ollama
    print("=" * 60)
    print("1. Checking Ollama availability...")
    if not ollama.is_available():
        print("ERROR: Ollama is not available or model not found!")
        sys.exit(1)
    print("   Ollama is ready.\n")

    # Check GLPI auth
    print("2. Testing GLPI authentication...")
    try:
        glpi._ensure_token()
        print("   GLPI authentication successful.\n")
    except Exception as e:
        print(f"ERROR: GLPI authentication failed: {e}")
        sys.exit(1)

    # Create test ticket
    print("3. Creating test ticket...")
    try:
        result = glpi.create_ticket(
            name=TEST_TICKET["name"],
            content=TEST_TICKET["content"],
            type=1,
        )
        ticket_id = result.get("id")
        print(f"   Created ticket #{ticket_id}: {TEST_TICKET['name']}")

        # Add followups
        fu_ids = []
        for i, followup_content in enumerate(TEST_TICKET["followups"], 1):
            fu_result = glpi.create_followup(ticket_id, followup_content)
            fu_id = fu_result.get("id")
            fu_ids.append(fu_id)
            content_preview = followup_content[:50] + ("..." if len(followup_content) > 50 else "")
            print(f"   - Followup #{fu_id}: {content_preview}")

    except Exception as e:
        print(f"ERROR: Failed to create ticket: {e}")
        sys.exit(1)

    # Show original content before translation
    print("\n" + "=" * 60)
    print("4. ORIGINAL CONTENT (before translation):")
    print("=" * 60)
    ticket = glpi.get_ticket(ticket_id)
    print(f"\n  [TITLE]")
    print(f"  {ticket.get('name', '')}")
    print(f"\n  [DESCRIPTION]")
    print(f"  {ticket.get('content', '')}")

    followups_before = glpi.get_ticket_followups(ticket_id)
    for fu in followups_before:
        print(f"\n  [FOLLOWUP #{fu.get('id')}]")
        print(f"  {fu.get('content', '')}")

    # Run translation
    print("\n" + "=" * 60)
    print("5. Running translation pass...")
    print("=" * 60)
    stats = run_once(config, glpi, ollama, state)
    print(f"\n  Tickets checked:      {stats['tickets_checked']}")
    print(f"  Tickets translated:   {stats['tickets_translated']}")
    print(f"  Followups translated: {stats['followups_translated']}")
    print(f"  Skipped:              {stats['tickets_skipped'] + stats['followups_skipped']}")
    print(f"  Failed:               {stats['failed']}")

    # Show translated content
    print("\n" + "=" * 60)
    print("6. TRANSLATED CONTENT (after translation):")
    print("=" * 60)
    ticket = glpi.get_ticket(ticket_id)

    print(f"\n  [TITLE] (expected: 翻译前 / 翻译后)")
    print(f"  >>> {ticket.get('name', '')}")

    print(f"\n  [DESCRIPTION] (expected: HTML with <br><br> + marker)")
    content = ticket.get("content", "")
    print(f"  >>> {content}")

    followups_after = glpi.get_ticket_followups(ticket_id)
    for fu in followups_after:
        print(f"\n  [FOLLOWUP #{fu.get('id')}]")
        fu_content = fu.get("content", "")
        print(f"  >>> {fu_content}")

    # Verify format
    print("\n" + "=" * 60)
    print("7. FORMAT VERIFICATION:")
    print("=" * 60)

    name = ticket.get("name", "")

    # Title check
    if " / " in name:
        parts = name.split(" / ", 1)
        print(f"  OK TITLE format correct: ' / ' separator")
        print(f"     原文: {parts[0]}")
        print(f"     翻译: {parts[1]}")
    elif "[AUTO-TRANSLATED]" in name:
        print(f"  XX TITLE format WRONG: still uses old [AUTO-TRANSLATED] format")
    else:
        print(f"  ?? TITLE may not have been translated (no separator found)")

    # Description and followups verification
    verify_content_format("DESCRIPTION", content, ticket_id)
    for fu in followups_after:
        verify_content_format("FOLLOWUP", fu.get("content", ""), fu.get("id"))

    print("\nDone!")


if __name__ == "__main__":
    main()
