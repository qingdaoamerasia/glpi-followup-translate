"""Test script to create sample tickets and run translation."""

import sys
import os
import logging

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

# Sample test data: mix of Chinese and English followups
TEST_TICKETS = [
    {
        "name": "网络连接问题 / Network Connection Issue",
        "content": "用户报告办公室网络不稳定，经常断开连接。已尝试重启路由器但问题仍然存在。",
        "followups": [
            "已派遣技术人员前往现场检查，预计下午3点到达。",
            "The network switch on the 3rd floor was found to be faulty. Replacement has been ordered.",
            "设备已更换，网络恢复正常。请用户确认是否还有问题。",
        ],
    },
    {
        "name": "Printer Not Working",
        "content": "The office printer on the 2nd floor is showing a paper jam error. Multiple users have reported this issue.",
        "followups": [
            "Technician cleared the paper jam. The printer is now operational.",
            "用户反馈打印机仍然无法打印彩色文档，只显示黑白。",
            "Checked the printer settings - color mode was disabled. Re-enabled it and confirmed color printing works.",
        ],
    },
    {
        "name": "邮箱配置问题 / Email Configuration Issue",
        "content": "新员工无法配置公司邮箱，Outlook提示认证失败。",
        "followups": [
            "IT部门已重置该员工的邮箱密码，请通知员工使用新密码重新配置。",
            "New password has been communicated to the employee. They were able to set up Outlook successfully.",
            "问题已解决，员工可以正常使用邮箱了。",
        ],
    },
]


def main():
    config = load_config()
    setup_logging(config)

    # Enable debug logging for troubleshooting
    logging.getLogger("glpi_followup_translate").setLevel(logging.DEBUG)

    glpi = GlpiClient(config.glpi)
    ollama = OllamaClient(config.ollama)
    state = ProcessedState()

    # Check Ollama
    print("Checking Ollama availability...")
    if not ollama.is_available():
        print("ERROR: Ollama is not available or model not found!")
        print(f"Please ensure Ollama is running and model '{config.ollama.model}' is pulled.")
        sys.exit(1)
    print("Ollama is ready.\n")

    # Test GLPI auth
    print("Testing GLPI authentication...")
    print(f"Auth method: {config.glpi.auth_method}")
    print(f"API URL: {config.glpi.api_url}")
    try:
        glpi._ensure_token()
        if glpi.access_token:
            print(f"OAuth2 token obtained (first 50 chars): {glpi.access_token[:50]}...")
        elif glpi.session_token:
            print(f"Session token obtained: {glpi.session_token[:20]}...")
        print("GLPI authentication successful.\n")
    except Exception as e:
        print(f"ERROR: GLPI authentication failed: {e}")
        print("\nTroubleshooting tips:")
        print("1. Check if OAuth2 client has 'api' scope configured in GLPI")
        print("2. Try setting auth_method to 'app_token' in config.yaml")
        print("3. Verify your client_id and client_secret are correct")
        sys.exit(1)

    # Create test tickets
    created_tickets = []
    print("Creating test tickets...")
    for i, ticket_data in enumerate(TEST_TICKETS, 1):
        try:
            result = glpi.create_ticket(
                name=ticket_data["name"],
                content=ticket_data["content"],
            )
            ticket_id = result.get("id")
            print(f"  [{i}/{len(TEST_TICKETS)}] Created ticket #{ticket_id}: {ticket_data['name']}")

            # Add followups
            for j, followup_content in enumerate(ticket_data["followups"], 1):
                fu_result = glpi.create_followup(ticket_id, followup_content)
                fu_id = fu_result.get("id")
                print(f"    - Added followup #{fu_id}")

            created_tickets.append(ticket_id)
        except Exception as e:
            print(f"  [{i}/{len(TEST_TICKETS)}] FAILED to create ticket: {e}")

    if not created_tickets:
        print("\nNo tickets were created. Exiting.")
        sys.exit(1)

    print(f"\nCreated {len(created_tickets)} tickets with followups.")
    print("=" * 60)

    # Run translation
    print("\nRunning translation pass...\n")
    stats = run_once(config, glpi, ollama, state)
    print(f"\nTranslation complete:")
    print(f"  Tickets checked: {stats['tickets_checked']}")
    print(f"  Translated:      {stats['translated']}")
    print(f"  Skipped:         {stats['skipped']}")
    print(f"  Failed:          {stats['failed']}")

    # Show results
    print("\n" + "=" * 60)
    print("TRANSLATION RESULTS:")
    print("=" * 60)
    for ticket_id in created_tickets:
        try:
            followups = glpi.get_ticket_followups(ticket_id)
            print(f"\nTicket #{ticket_id}:")
            for fu in followups:
                content = fu.get("content", "")
                print(f"  Followup #{fu.get('id')}:")
                for line in content.split("\n"):
                    print(f"    {line}")
        except Exception as e:
            print(f"  Error fetching followups for ticket #{ticket_id}: {e}")


if __name__ == "__main__":
    main()
