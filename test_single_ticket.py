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
)

# Single test ticket with Chinese title, Chinese description, and mixed followups
TEST_TICKET = {
    "name": "服务器无法连接数据库",
    "content": "<p>生产环境服务器从今天早上开始无法连接到MySQL数据库，报错信息为\"Connection refused\"。已检查数据库服务状态，确认数据库服务正在运行。</p>",
    "followups": [
        "检查了防火墙规则，发现3306端口被意外关闭。已重新开放端口。",
        "After opening the port, the connection was restored temporarily but went down again after 10 minutes.",
        "经排查发现是数据库连接池耗尽导致的问题。已将最大连接数从100调整到500。",
    ],
}


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

    print(f"\n  [TITLE] (expected format: 翻译前 / 翻译后)")
    print(f"  >>> {ticket.get('name', '')}")

    print(f"\n  [DESCRIPTION] (expected format: 翻译前\\n\\n[AUTO-TRANSLATED]\\n翻译后)")
    content = ticket.get("content", "")
    print(f"  >>> {content}")

    followups_after = glpi.get_ticket_followups(ticket_id)
    for fu in followups_after:
        print(f"\n  [FOLLOWUP #{fu.get('id')}] (expected format: 翻译前\\n\\n[AUTO-TRANSLATED]\\n翻译后)")
        fu_content = fu.get("content", "")
        print(f"  >>> {fu_content}")

    # Verify format
    print("\n" + "=" * 60)
    print("7. FORMAT VERIFICATION:")
    print("=" * 60)

    name = ticket.get("name", "")
    content = ticket.get("content", "")

    # Title: should contain " / " (slash separator) and NOT contain [AUTO-TRANSLATED]
    if " / " in name:
        parts = name.split(" / ", 1)
        print(f"  ✅ TITLE format correct: uses ' / ' separator")
        print(f"     原文: {parts[0]}")
        print(f"     翻译: {parts[1]}")
    elif "[AUTO-TRANSLATED]" in name:
        print(f"  ❌ TITLE format WRONG: still uses old [AUTO-TRANSLATED] format")
    else:
        print(f"  ⚠️  TITLE may not have been translated (no separator found)")

    # Description: should contain \n\n and [AUTO-TRANSLATED]
    if "\n\n" in content and "[AUTO-TRANSLATED]" in content:
        print(f"  ✅ DESCRIPTION format correct: uses \\n\\n + [AUTO-TRANSLATED]")
        # Show the structure
        parts = content.split("\n\n", 1)
        if len(parts) == 2:
            before, after = parts
            # after should be: [AUTO-TRANSLATED]\n{translated}
            after_parts = after.split("\n", 1)
            if len(after_parts) == 2:
                print(f"     原文: {before[:80]}...")
                print(f"     标记: {after_parts[0]}")
                print(f"     翻译: {after_parts[1][:80]}...")
    elif "[AUTO-TRANSLATED]" in content:
        print(f"  ❌ DESCRIPTION format WRONG: [AUTO-TRANSLATED] found but no double newline")
    else:
        print(f"  ⚠️  DESCRIPTION may not have been translated (no marker found)")

    # Followups
    for fu in followups_after:
        fu_content = fu.get("content", "")
        if "\n\n" in fu_content and "[AUTO-TRANSLATED]" in fu_content:
            print(f"  ✅ FOLLOWUP #{fu.get('id')} format correct: uses \\n\\n + [AUTO-TRANSLATED]")
        elif "[AUTO-TRANSLATED]" in fu_content:
            print(f"  ❌ FOLLOWUP #{fu.get('id')} format WRONG: double newline missing")
        else:
            print(f"  ⚠️  FOLLOWUP #{fu.get('id')} may not need translation")

    print("\nDone!")


if __name__ == "__main__":
    main()
