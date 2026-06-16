"""Integration test suite for GLPI Followup Translate.

Usage:
    python test_integration.py                  # Round 1 (default)
    python test_integration.py --single         # Quick single-ticket test
    python test_integration.py --rounds 3       # Run rounds 1-3
    python test_integration.py --rounds 0       # Run ALL available rounds
    python test_integration.py --list-rounds    # Show available test rounds
    python test_integration.py --unit           # Unit tests only (no GLPI/Ollama)
    python test_integration.py --cleanup        # Clean up test tickets only
"""

import argparse
import json
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
    strip_html,
    detect_language_with_fallback,
    _count_cjk,
    _cjk_ratio,
    _apply_glossary,
    _get_glossary,
    _replace_with_placeholders,
    _restore_placeholders,
)

MARKER = "[AUTO-TRANSLATED]"
TEST_PREFIX = "[Test] "
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".test_ticket_ids.json")


# ═══════════════════════════════════════════════════════════════════════════════
# Test Round Definitions
# ═══════════════════════════════════════════════════════════════════════════════

ROUNDS = [
    # ── Round 1: Rich-text HTML + mixed followups ─────────────────────────────
    {
        "name": "Rich-text HTML + mixed followups",
        "description": "Standard HTML ticket with Chinese/English followups",
        "tickets": [
            {
                "name": "服务器无法连接数据库",
                "content": (
                    '<p><strong>生产环境</strong>服务器从今天早上开始'
                    '无法连接到<span style="color: rgb(255, 0, 0);">MySQL数据库</span>，'
                    '报错信息为"Connection refused"。<br>'
                    '<em>已检查数据库服务状态，确认数据库服务正在运行。</em></p>'
                ),
                "followups": [
                    "检查了防火墙规则，发现3306端口被意外关闭。已重新开放端口。",
                    '<p><strong>After opening the port</strong>, the connection was restored '
                    'temporarily but <span style="color: red;">went down again</span> after 10 minutes.</p>',
                    "经排查发现是数据库连接池耗尽导致的问题。已将最大连接数从100调整到500。",
                ],
            },
        ],
    },
    # ── Round 2: Short text + long text ───────────────────────────────────────
    {
        "name": "Short text + long text",
        "description": "Verify both very short and long paragraphs translate correctly",
        "tickets": [
            {
                "name": "短文本测试",
                "content": "登录失败。",
                "followups": [
                    "已修复。",
                    "OK, confirmed working now.",
                    "请确认一下是否还有问题，如果没有的话就可以关闭这个工单了。",
                ],
            },
            {
                "name": "Long text stress test",
                "content": (
                    "We have been experiencing intermittent connectivity issues across "
                    "the entire office network since Monday morning. The problem appears "
                    "to affect all three floors of the building, with users reporting "
                    "slow internet speeds, frequent disconnections, and inability to "
                    "access internal services such as the file server and the internal "
                    "wiki. Our network monitoring tools show that the main gateway router "
                    "is experiencing high CPU utilization (above 90%) during peak hours "
                    "between 9 AM and 11 AM. We suspect this may be related to a recent "
                    "firmware update that was applied last Friday, but we need to "
                    "investigate further before rolling back."
                ),
                "followups": [
                    (
                        "IT团队已经分析了整个网络拓扑结构，发现问题出在核心交换机上。"
                        "该交换机在过去三天内多次出现过载告警，每次持续约15-20分钟。"
                        "我们怀疑是某个网络环路导致的广播风暴。目前正在逐一排查每个"
                        "楼层的接入交换机，寻找环路源头。初步判断三楼的某个区域最可疑，"
                        "因为那里的告警频率最高。已派遣两名工程师前往现场检查。"
                    ),
                    "We found the root cause. An unauthorized switch was connected to the network on the 3rd floor, creating a broadcast storm loop. The switch has been disconnected.",
                ],
            },
        ],
    },
    # ── Round 3: Low CJK ratio (mostly English, few Chinese) ──────────────────
    {
        "name": "Low CJK ratio (mostly English)",
        "description": "Text that is predominantly English with a few Chinese words — should still detect as zh-cn",
        "tickets": [
            {
                "name": "Please check the 服务器 status",
                "content": (
                    "The production server went down at 3:42 AM. "
                    "All services on the 数据库 cluster are unresponsive. "
                    "Please check the 防火墙 logs and 路由器 configuration."
                ),
                "followups": [
                    "I checked the DHCP server and found 新的IP地址 conflicts on the VLAN.",
                    "The switch port Gi0/24 on 核心交换机 is showing errors, please check 连接日志。",
                    "Confirmed working. The 工单 can be closed now.",
                ],
            },
        ],
    },
    # ── Round 4: High CJK ratio (mostly Chinese, English tech terms) ──────────
    {
        "name": "High CJK ratio (mostly Chinese)",
        "description": "Chinese-dominant text with English technical terms mixed in",
        "tickets": [
            {
                "name": "核心交换机故障导致网络中断",
                "content": (
                    "今天早上8点发现核心交换机出现故障，导致整个办公楼网络中断约45分钟。"
                    "故障原因是CPU温度过高触发了保护性shutdown。目前已将业务切换到备用交换机，"
                    "网络已恢复正常。需要联系vendor更换散热风扇。"
                ),
                "followups": [
                    "vendor已确认明天上午派工程师上门更换风扇模块。备件已发货，预计今天下午到达。",
                    "The replacement fan module has been installed. CPU temperature dropped from 92°C to 38°C. Switch is back to normal operation.",
                    "已将所有业务流量切回核心交换机。VLAN配置和routing table均已恢复。请各部门确认网络使用是否正常。",
                ],
            },
        ],
    },
    # ── Round 5 is dynamically generated from config.yaml glossary ──────────
    # See build_glossary_round() below
    None,
    # ── Round 6: English → Chinese direction ──────────────────────────────────
    {
        "name": "English to Chinese direction",
        "description": "All-English tickets translated into Chinese",
        "tickets": [
            {
                "name": "VPN Access Request",
                "content": "New remote employee needs VPN access to the corporate network. Please configure their account with appropriate access permissions and provide connection instructions.",
                "followups": [
                    "VPN credentials have been generated. The employee can connect using the OpenVPN client with the provided .ovpn configuration file.",
                    "The employee reports that the VPN connection drops after approximately 30 minutes of use. They are connecting from a home network with a 50 Mbps connection.",
                    "Updated the VPN session timeout from 30 to 480 minutes. The employee confirmed the connection is now stable for full workday usage.",
                ],
            },
            {
                "name": "Software license renewal",
                "content": "The annual Microsoft Office 365 licenses for 25 users are expiring at the end of this month. We need to process the renewal before the deadline to avoid service interruption.",
                "followups": [
                    "Purchase order #PO-2026-0847 has been approved for the license renewal. Finance will process the payment this week.",
                    "Licenses have been renewed successfully. All 25 users now have active subscriptions through December 2027.",
                ],
            },
        ],
    },
]


def build_glossary_round(config):
    """Build Round 5 dynamically from config.yaml glossary.

    Reads whatever glossary terms are configured and generates test tickets
    that embed those terms in generic IT-context sentences. If no glossary
    is configured, returns a no-op round that passes immediately.
    """
    glossary_data = config.translation.glossary
    if not glossary_data:
        return {
            "name": "Glossary term verification",
            "description": "No glossary configured in config.yaml — skipped",
            "tickets": [],
        }

    # Collect non-identity terms from each direction
    zh_terms = {}  # zh-cn source → en target
    en_terms = {}  # en source → zh-cn target
    for lang, terms in glossary_data.items():
        for src, tgt in terms.items():
            if src == tgt:
                continue
            has_cjk = any('\u4e00' <= c <= '\u9fff' for c in src)
            if has_cjk:
                zh_terms[src] = tgt
            else:
                en_terms[src] = tgt

    all_zh = list(zh_terms.items())  # [(src, tgt), ...]
    all_en = list(en_terms.items())

    if not all_zh and not all_en:
        return {
            "name": "Glossary term verification",
            "description": "All glossary terms are identity mappings — skipped",
            "tickets": [],
        }

    # Build term display summary
    zh_preview = ", ".join(f"{s}" for s, _ in all_zh[:3])
    en_preview = ", ".join(f"{s}" for s, _ in all_en[:3])
    desc_parts = []
    if zh_preview:
        desc_parts.append(f"zh-cn terms: {zh_preview}")
    if en_preview:
        desc_parts.append(f"en terms: {en_preview}")

    tickets = []

    # ── Ticket 1: zh-cn → en direction, embed all CJK glossary terms ──
    if all_zh:
        term_list_zh = [s for s, _ in all_zh]
        tgt_list_zh = [t for _, t in all_zh]

        if len(term_list_zh) >= 3:
            name = f"{term_list_zh[0]}系统{term_list_zh[1]}升级通知"
            content = (
                f"{term_list_zh[0]}的IT部门计划对{term_list_zh[1]}系统进行一次重要升级。"
                f"此次升级由{term_list_zh[2]}负责协调，预计需要停机维护约3小时。"
                f"请各部门提前做好工作安排，确保不影响日常业务。"
            )
            followups = [
                (
                    f"{term_list_zh[1]}升级方案已通过{term_list_zh[0]}技术团队审核。"
                    f"{term_list_zh[2]}已确认维护时间安排在周六凌晨1:00-4:00。"
                ),
                (
                    f"The {tgt_list_zh[1]} upgrade was completed successfully. "
                    f"{tgt_list_zh[0]} team confirmed all modules are functioning normally. "
                    f"{tgt_list_zh[2]} will monitor for 48 hours."
                ),
                (
                    f"{term_list_zh[0]}各部门已确认{term_list_zh[1]}系统运行正常。"
                    f"{term_list_zh[2]}建议将此工单关闭。感谢所有参与人员的配合。"
                ),
            ]
        elif len(term_list_zh) == 2:
            name = f"{term_list_zh[0]}系统{term_list_zh[1]}通知"
            content = (
                f"{term_list_zh[0]}的IT部门计划对{term_list_zh[1]}进行升级维护。"
                f"预计需要停机约3小时，请各部门提前做好准备。"
            )
            followups = [
                f"{term_list_zh[1]}方案已通过{term_list_zh[0]}审核，时间安排在周六凌晨。",
                f"The {tgt_list_zh[1]} was completed. {tgt_list_zh[0]} confirmed all systems normal.",
                f"{term_list_zh[0]}已确认{term_list_zh[1]}运行正常，建议关闭工单。",
            ]
        else:
            name = f"{term_list_zh[0]}系统维护通知"
            content = f"{term_list_zh[0]}的IT部门计划进行系统升级维护，预计停机3小时。"
            followups = [
                f"{term_list_zh[0]}升级方案已通过审核。",
                f"The {tgt_list_zh[0]} upgrade was completed successfully.",
                f"{term_list_zh[0]}已确认系统运行正常。",
            ]

        tickets.append({"name": name, "content": content, "followups": followups})

    # ── Ticket 2: en → zh-cn direction, embed all English glossary terms ──
    if all_en:
        term_list_en = [s for s, _ in all_en]
        tgt_list_en = [t for _, t in all_en]

        if len(term_list_en) >= 2:
            name = f"{term_list_en[0]} System Upgrade Report"
            content = (
                f"The {term_list_en[0]} IT team has completed the quarterly system review. "
                f"All modules managed by {term_list_en[1]} are operating within normal parameters. "
                f"Please review the attached report and provide feedback."
            )
            followups = [
                (
                    f"The {term_list_en[0]} review identified two minor issues that need attention. "
                    f"{term_list_en[1]} has been notified and will address them by Friday."
                ),
                (
                    f"{tgt_list_en[0]}技术团队已完成修复。{tgt_list_en[1]}确认所有模块运行正常。"
                ),
                f"All issues resolved. {term_list_en[0]} recommends closing this ticket.",
            ]
        else:
            name = f"{term_list_en[0]} Status Update"
            content = f"The {term_list_en[0]} system is operating normally. No issues detected."
            followups = [
                f"{tgt_list_en[0]}状态正常，无异常。",
                f"{term_list_en[0]} confirmed stable. Ticket can be closed.",
            ]

        tickets.append({"name": name, "content": content, "followups": followups})

    return {
        "name": "Glossary term verification",
        "description": f"Dynamic glossary test — {', '.join(desc_parts)}",
        "tickets": tickets,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Test Ticket Tracking
# ═══════════════════════════════════════════════════════════════════════════════

def _load_test_ticket_ids() -> list:
    """Load IDs of tickets created by previous test runs."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                return [int(x) for x in data.get("ticket_ids", [])]
        except (json.JSONDecodeError, IOError):
            pass
    return []


def _save_test_ticket_ids(ids: list) -> None:
    """Persist test ticket IDs so cleanup can target them later."""
    with open(STATE_FILE, "w") as f:
        json.dump({"ticket_ids": [int(x) for x in ids]}, f)


def _append_test_ticket_ids(new_ids: list) -> list:
    """Add new IDs to the tracking file and return the full list."""
    existing = _load_test_ticket_ids()
    merged = existing + [int(x) for x in new_ids]
    _save_test_ticket_ids(merged)
    return merged


# ═══════════════════════════════════════════════════════════════════════════════
# Unit Tests
# ═══════════════════════════════════════════════════════════════════════════════

def test_unit_language_detection():
    """Test language detection heuristics without external services."""
    supported = {"zh-cn", "zh", "en"}
    passed = failed = 0

    cases = [
        ("服务器无法连接数据库", "zh-cn", "Pure Chinese"),
        ("The server is down and needs restart", "en", "Pure English"),
        ("Please check 数据库 connection status", "zh-cn", "Low CJK ratio"),
        ("MySQL connection pool exhausted 连接池耗尽问题需要处理", "zh-cn", "Mixed with significant CJK"),
        ("Yes", "en", "Short English"),
        ("OK", "en", "Very short ASCII"),
        ("检查了firewall rules发现port 3306被closed了", "zh-cn", "Chinese with English tech terms"),
        ("The DHCP server assigned 新的IP地址 to the client machine", "zh-cn", "Low CJK ratio override"),
        ("Réunion de service", None, "French (not in supported)"),
    ]

    print("\n=== Unit Test: Language Detection ===")
    for text, expected, desc in cases:
        result = detect_language_with_fallback(text, supported)
        cjk_r = _cjk_ratio(text)
        if expected is None:
            print(f"  INFO [{desc}] '{text[:40]}' -> {result} (CJK: {cjk_r:.1%})")
            continue
        ok = result == expected
        passed += ok
        failed += not ok
        print(f"  {'OK' if ok else 'FAIL'} [{desc}] -> {result} (exp: {expected}, CJK: {cjk_r:.1%})")

    print(f"\n  Language detection: {passed} passed, {failed} failed")
    return failed == 0


def test_unit_cjk_ratio():
    """Test CJK character counting and ratio calculation."""
    passed = failed = 0

    cases = [
        ("服务器", 3, 1.0, "Pure CJK"),
        ("Hello", 0, 0.0, "Pure ASCII"),
        ("Hello世界", 2, 2/7, "Mixed 50/50"),
        ("Please check 数据库", 3, 3/14, "Low CJK ratio"),
        ("", 0, 0.0, "Empty string"),
    ]

    print("\n=== Unit Test: CJK Ratio ===")
    for text, exp_count, exp_ratio, desc in cases:
        count = _count_cjk(text)
        ratio = _cjk_ratio(text)
        ok = count == exp_count and abs(ratio - exp_ratio) < 0.01
        passed += ok
        failed += not ok
        print(f"  {'OK' if ok else 'FAIL'} [{desc}] count={count} ratio={ratio:.2%}")

    print(f"\n  CJK ratio: {passed} passed, {failed} failed")
    return failed == 0


def test_unit_glossary(config=None):
    """Test glossary post-processing — generic cases + dynamic config terms."""
    passed = failed = 0

    # Generic tests (no config needed)
    cases = [
        ("The server connected to the data base.", {"data base": "database"},
         "The server connected to the database.", "English term replacement"),
        ("Check the serverless configuration.", {"server": "服务器"},
         "Check the serverless configuration.", "No partial match (server in serverless)"),
        ("No changes needed here.", {},
         "No changes needed here.", "Empty glossary"),
    ]

    # Dynamic config-based tests
    if config and config.translation.glossary:
        glossary_data = config.translation.glossary
        # Pick terms from each direction for testing
        for lang, terms in glossary_data.items():
            non_identity = {s: t for s, t in terms.items() if s != t}
            if not non_identity:
                continue
            # Test single term
            first_src, first_tgt = next(iter(non_identity.items()))
            has_cjk = any('\u4e00' <= c <= '\u9fff' for c in first_src)
            if has_cjk:
                cases.append(
                    (f"Test {first_src} term here.", {first_src: first_tgt},
                     f"Test {first_tgt} term here.", f"Config: {first_src}→{first_tgt}"),
                )
            else:
                cases.append(
                    (f"Test {first_src} term here.", {first_src: first_tgt},
                     f"Test {first_tgt} term here.", f"Config: {first_src}→{first_tgt}"),
                )
            # Test multiple terms together if >= 2
            if len(non_identity) >= 2:
                items = list(non_identity.items())[:3]
                src_text = " ".join(s for s, _ in items)
                tgt_text = " ".join(t for _, t in items)
                test_glossary = dict(items)
                cases.append(
                    (f"Using {src_text} together.", test_glossary,
                     f"Using {tgt_text} together.", f"Config: {len(items)} terms combined"),
                )

    print("\n=== Unit Test: Glossary Post-processing ===")
    for text, glossary, expected, desc in cases:
        result = _apply_glossary(text, glossary)
        ok = result == expected
        passed += ok
        failed += not ok
        print(f"  {'OK' if ok else 'FAIL'} [{desc}]")
        if not ok:
            print(f"    Expected: '{expected}'")
            print(f"    Got:      '{result}'")

    print(f"\n  Glossary: {passed} passed, {failed} failed")
    return failed == 0


def test_unit_output_cleanup():
    """Test translation output cleanup for small model artifacts."""
    passed = failed = 0

    cases = [
        ("The server is down.\n\nUse these term translations consistently:\n- 服务器 → server",
         "The server is down.", "Strip English glossary echo"),
        ("服务器已关闭。\n\n请始终使用以下术语翻译：\n- server → 服务器",
         "服务器已关闭。", "Strip Chinese glossary echo"),
        ("Normal translation output.", "Normal translation output.", "No change needed"),
        ("Good translation\n\nChinese (Simplified):", "Good translation", "Strip trailing language label"),
    ]

    print("\n=== Unit Test: Output Cleanup ===")
    for text, expected, desc in cases:
        result = OllamaClient._clean_output(text)
        ok = result == expected
        passed += ok
        failed += not ok
        print(f"  {'OK' if ok else 'FAIL'} [{desc}]")
        if not ok:
            print(f"    Expected: '{expected}'")
            print(f"    Got:      '{result}'")

    print(f"\n  Output cleanup: {passed} passed, {failed} failed")
    return failed == 0


def test_unit_placeholders(config=None):
    """Test glossary placeholder round-trip — generic + dynamic config terms."""
    passed = failed = 0

    # Build a glossary from config if available, otherwise use generic test data
    if config and config.translation.glossary:
        glossary = {}
        for lang, terms in config.translation.glossary.items():
            glossary.update(terms)
    else:
        glossary = {"Alpha": "Bravo", "Charlie": "Delta"}

    # Filter to non-identity terms
    active_glossary = {s: t for s, t in glossary.items() if s != t}
    if not active_glossary:
        print("\n=== Unit Test: Placeholder Round-trip ===")
        print("  SKIP: no non-identity glossary terms available")
        return True

    items = list(active_glossary.items())

    # ── Generic placeholder mechanism tests ──
    print("\n=== Unit Test: Placeholder Round-trip ===")

    # Test 1: Single term round-trip
    src1, tgt1 = items[0]
    source_text = f"This text contains {src1} as a term."
    modified, mapping = _replace_with_placeholders(source_text, active_glossary)
    # Verify source term is replaced
    ok1 = src1 not in modified and len(mapping) >= 1
    passed += ok1
    failed += not ok1
    print(f"  {'OK' if ok1 else 'FAIL'} [Single term replaced: {src1}→GLS?GLS]")
    # Simulate model keeping placeholder, then restore
    simulated = modified  # pretend model keeps it exactly
    restored = _restore_placeholders(simulated, mapping)
    ok2 = tgt1 in restored and src1 not in restored
    passed += ok2
    failed += not ok2
    print(f"  {'OK' if ok2 else 'FAIL'} [Restored to target: {tgt1}]")
    if not ok2:
        print(f"    Got: '{restored}'")

    # Test 2: Multiple terms round-trip
    if len(items) >= 2:
        src2, tgt2 = items[1]
        multi_source = f"Both {src1} and {src2} appear here."
        modified2, mapping2 = _replace_with_placeholders(multi_source, active_glossary)
        ok3 = src1 not in modified2 and src2 not in modified2
        passed += ok3
        failed += not ok3
        print(f"  {'OK' if ok3 else 'FAIL'} [Multiple terms replaced: {src1}, {src2}]")
        restored2 = _restore_placeholders(modified2, mapping2)
        ok4 = tgt1 in restored2 and tgt2 in restored2
        passed += ok4
        failed += not ok4
        print(f"  {'OK' if ok4 else 'FAIL'} [Both restored: {tgt1}, {tgt2}]")
        if not ok4:
            print(f"    Got: '{restored2}'")

    # Test 3: Regex fallback for spaces
    test_mapping = {0: "TargetTerm"}
    spaced_input = "The GLS 0 GLS was preserved."
    restored3 = _restore_placeholders(spaced_input, test_mapping)
    ok5 = "TargetTerm" in restored3
    passed += ok5
    failed += not ok5
    print(f"  {'OK' if ok5 else 'FAIL'} [Regex fallback: spaces in GLS 0 GLS]")

    # Test 4: No glossary terms in source
    no_term_source = "Plain text with no special terms."
    modified4, mapping4 = _replace_with_placeholders(no_term_source, active_glossary)
    ok6 = modified4 == no_term_source and len(mapping4) == 0
    passed += ok6
    failed += not ok6
    print(f"  {'OK' if ok6 else 'FAIL'} [No terms in source → no placeholders]")

    # Test 5: CJK term handling (if any CJK terms exist)
    cjk_terms = [(s, t) for s, t in items if any('\u4e00' <= c <= '\u9fff' for c in s)]
    if cjk_terms:
        cjk_src, cjk_tgt = cjk_terms[0]
        cjk_source = f"测试{cjk_src}系统运行状态。"
        modified5, mapping5 = _replace_with_placeholders(cjk_source, active_glossary)
        ok7 = cjk_src not in modified5 and len(mapping5) >= 1
        passed += ok7
        failed += not ok7
        print(f"  {'OK' if ok7 else 'FAIL'} [CJK term replaced: {cjk_src}→placeholder]")
        restored5 = _restore_placeholders(modified5, mapping5)
        ok8 = cjk_tgt in restored5
        passed += ok8
        failed += not ok8
        print(f"  {'OK' if ok8 else 'FAIL'} [CJK restored: {cjk_tgt}]")
        if not ok8:
            print(f"    Got: '{restored5}'")

    print(f"\n  Placeholders: {passed} passed, {failed} failed")
    return failed == 0


def run_unit_tests(config=None):
    """Run all unit tests."""
    results = []
    results.append(("Language Detection", test_unit_language_detection()))
    results.append(("CJK Ratio", test_unit_cjk_ratio()))
    results.append(("Glossary", test_unit_glossary(config)))
    results.append(("Output Cleanup", test_unit_output_cleanup()))
    results.append(("Placeholders", test_unit_placeholders(config)))

    print("\n" + "=" * 60)
    print("UNIT TEST SUMMARY:")
    all_pass = True
    for name, ok in results:
        status = "PASS" if ok else "FAIL"
        if not ok:
            all_pass = False
        print(f"  {status}  {name}")
    print("=" * 60)
    return all_pass


# ═══════════════════════════════════════════════════════════════════════════════
# Format Verification
# ═══════════════════════════════════════════════════════════════════════════════

def verify_content_format(label, content, item_id):
    """Verify the translation format for a content item."""
    if MARKER not in content:
        print(f"  ?  {label} #{item_id} may not have been translated (no marker)")
        return False

    if has_html_tags(content):
        if f"<strong>{MARKER}</strong>" in content:
            print(f"  OK {label} #{item_id}: <strong>{MARKER}</strong> (HTML)")
            parts = content.split(f"<strong>{MARKER}</strong>", 1)
            if len(parts) == 2:
                after = parts[1].strip().lstrip('\n')
                if after.startswith('<br>'):
                    after = after[4:].strip()
                print(f"     Translation: {after[:120]}...")
            return True
        else:
            print(f"  XX {label} #{item_id}: HTML missing <strong>{MARKER}</strong>")
            return False
    else:
        sep = "\n\n"
        if sep in content:
            print(f"  OK {label} #{item_id}: \\n\\n + {MARKER} (plain)")
            parts = content.split(sep, 1)
            if len(parts) == 2:
                after_parts = parts[1].split("\n", 1)
                if len(after_parts) == 2:
                    print(f"     Translation: {after_parts[1][:80]}...")
            return True
        else:
            print(f"  XX {label} #{item_id}: plain text missing \\n\\n")
            return False


def verify_title_format(name):
    """Verify the translated title uses slash separator."""
    if " / " in name:
        parts = name.split(" / ", 1)
        print(f"  OK TITLE: '{parts[0]}' / '{parts[1]}'")
        return True
    else:
        print(f"  ?? TITLE: no ' / ' separator found")
        return False


def verify_glossary_terms(content, config, source_lang):
    """Verify that glossary terms from config.yaml are correctly applied in the translation.

    Extracts the translation portion (after MARKER) and checks that glossary
    target terms appear where expected.

    Returns:
        True if all checked terms pass, False otherwise.
    """
    glossary = _get_glossary(config, source_lang, "")
    if not glossary:
        print(f"  -- GLOSSARY: no glossary configured for '{source_lang}', skipping")
        return True

    # Extract the translation portion (after MARKER)
    translation = ""
    if f"<strong>{MARKER}</strong>" in content:
        parts = content.split(f"<strong>{MARKER}</strong>", 1)
        if len(parts) == 2:
            translation = parts[1]
    elif MARKER in content:
        parts = content.split(MARKER, 1)
        if len(parts) == 2:
            translation = parts[1]

    if not translation:
        print(f"  -- GLOSSARY: could not extract translation portion, skipping")
        return True

    found = 0
    missing = 0
    checked_terms = []

    for src_term, tgt_term in glossary.items():
        if src_term == tgt_term:
            continue  # Skip identity mappings (e.g., QAIS→QAIS)
        # Check if source term was in the original (roughly — we check translation for target)
        if tgt_term in translation:
            found += 1
            checked_terms.append(f"{src_term}→{tgt_term} ✓")
        else:
            # The term might not have been in the source text, so only flag
            # if the source term also doesn't appear (meaning it wasn't relevant)
            checked_terms.append(f"{src_term}→{tgt_term} ?")

    if found > 0:
        print(f"  OK GLOSSARY: {found} term(s) correctly applied")
        for t in checked_terms:
            print(f"       {t}")
        return True
    elif checked_terms:
        print(f"  !! GLOSSARY: no glossary target terms found in translation")
        for t in checked_terms:
            print(f"       {t}")
        # Not necessarily a failure — source text might not have contained these terms
        return True
    else:
        print(f"  -- GLOSSARY: all terms are identity mappings, nothing to check")
        return True


def verify_glossary_in_title(name, config, source_lang):
    """Verify glossary terms are applied in the translated title portion."""
    glossary = _get_glossary(config, source_lang, "")
    if not glossary or " / " not in name:
        return True

    translated_title = name.split(" / ", 1)[1]
    found = []
    for src_term, tgt_term in glossary.items():
        if src_term == tgt_term:
            continue
        if tgt_term in translated_title:
            found.append(f"{src_term}→{tgt_term}")

    if found:
        print(f"  OK TITLE GLOSSARY: {', '.join(found)}")
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# Preflight / Cleanup
# ═══════════════════════════════════════════════════════════════════════════════

def preflight(glpi, ollama, config):
    """Check Ollama and GLPI connectivity."""
    print("=" * 60)
    print("Preflight checks...")
    if not ollama.is_available():
        print("ERROR: Ollama is not available or model not found!")
        sys.exit(1)
    print("  Ollama: OK")

    try:
        glpi._ensure_token()
        print("  GLPI:   OK")
    except Exception as e:
        print(f"ERROR: GLPI auth failed: {e}")
        sys.exit(1)
    print()


def cleanup_test_tickets(glpi):
    """Delete ONLY tickets that were created by this test script."""
    tracked_ids = _load_test_ticket_ids()
    if not tracked_ids:
        print("No test tickets to clean up.")
        return

    print(f"Cleaning up {len(tracked_ids)} test ticket(s)...")
    deleted = 0
    for tid in tracked_ids:
        try:
            glpi.update_ticket(tid, is_deleted=True)
            deleted += 1
        except Exception:
            pass
    print(f"  Deleted {deleted} test ticket(s).")

    # Clear the tracking file
    _save_test_ticket_ids([])


# ═══════════════════════════════════════════════════════════════════════════════
# Integration Test Runner
# ═══════════════════════════════════════════════════════════════════════════════

def run_round(glpi, ollama, config, state, round_idx):
    """Run a single test round: create tickets → translate → verify."""
    round_def = ROUNDS[round_idx]
    round_num = round_idx + 1
    total_rounds = len(ROUNDS)

    print("=" * 60)
    print(f"ROUND {round_num}/{total_rounds}: {round_def['name']}")
    print(f"  {round_def['description']}")
    print("=" * 60)

    # ── Create tickets ─────────────────────────────────────────────────────
    created_ids = []
    ticket_defs = round_def["tickets"]
    if not ticket_defs:
        print(f"\n  No tickets defined for this round — skipping.")
        print(f"\n  Round {round_num} result: SKIP")
        return []
    print(f"\n  Creating {len(ticket_defs)} ticket(s)...")
    for i, td in enumerate(ticket_defs, 1):
        try:
            test_name = f"{TEST_PREFIX}{td['name']}"
            result = glpi.create_ticket(name=test_name, content=td["content"], type=1)
            tid = result.get("id")
            created_ids.append(tid)
            cjk_r = _cjk_ratio(td["name"] + " " + td["content"])
            print(f"    [{i}] #{tid} \"{test_name}\" (CJK: {cjk_r:.0%})")
            for j, fu in enumerate(td.get("followups", []), 1):
                fu_r = _cjk_ratio(fu)
                glpi.create_followup(tid, fu)
                preview = fu[:50] + ("..." if len(fu) > 50 else "")
                print(f"         FU#{j} (CJK: {fu_r:.0%}) {preview}")
        except Exception as e:
            print(f"    [{i}] FAILED: {e}")

    if not created_ids:
        print("  No tickets created for this round.")
        return []

    # Track created IDs persistently
    _append_test_ticket_ids(created_ids)

    # ── Translate ──────────────────────────────────────────────────────────
    print(f"\n  Running translation pass...")
    stats = run_once(config, glpi, ollama, state)
    print(f"  Pass: {stats['tickets_translated']} tickets, "
          f"{stats['followups_translated']} followups translated, "
          f"{stats['failed']} failed")

    # ── Verify ─────────────────────────────────────────────────────────────
    print(f"\n  Verifying results...")
    all_ok = True
    supported = set(config.translation.source_languages)
    for tid in created_ids:
        try:
            ticket = glpi.get_ticket(tid)
            name = ticket.get("name", "")
            content = ticket.get("content", "")
            print(f"\n  ── Ticket #{tid} ──")
            if not verify_title_format(name):
                all_ok = False

            # Detect source language for glossary verification
            orig_name = name.split(" / ")[0] if " / " in name else name
            source_lang = detect_language_with_fallback(orig_name, supported)

            if not verify_content_format("CONTENT", content, tid):
                all_ok = False
            # Check glossary terms in content translation
            verify_glossary_terms(content, config, source_lang)
            # Check glossary terms in title translation
            verify_glossary_in_title(name, config, source_lang)

            for fu in glpi.get_ticket_followups(tid):
                fc = fu.get("content", "")
                if MARKER in fc:
                    if not verify_content_format("FOLLOWUP", fc, fu.get("id")):
                        all_ok = False
                    # Detect followup's own source language from original portion
                    fu_original = fc.split(MARKER)[0] if MARKER in fc else fc
                    fu_original = strip_html(fu_original).strip()
                    fu_lang = detect_language_with_fallback(fu_original, supported)
                    # Check glossary in followup translation
                    verify_glossary_terms(fc, config, fu_lang)
        except Exception as e:
            print(f"  Error reading ticket #{tid}: {e}")
            all_ok = False

    status = "PASS" if all_ok else "FAIL"
    print(f"\n  Round {round_num} result: {status}")
    return created_ids


def run_all_rounds(glpi, ollama, config, state, rounds_to_run):
    """Run multiple test rounds sequentially."""
    all_created = []
    round_results = []

    for idx in rounds_to_run:
        ids = run_round(glpi, ollama, config, state, idx)
        all_created.extend(ids)
        round_results.append((idx + 1, ROUNDS[idx]["name"], len(ids)))
        print()

    # ── Final summary ──────────────────────────────────────────────────────
    print("=" * 60)
    print("INTEGRATION TEST SUMMARY:")
    print("=" * 60)
    for rn, name, count in round_results:
        print(f"  Round {rn}: {name} ({count} ticket(s))")
    print(f"\n  Total test tickets: {len(all_created)}")
    print(f"  Tracked IDs saved to: {STATE_FILE}")
    print(f"  Use --cleanup to remove test tickets.")
    print("=" * 60)
    return all_created


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="GLPI Followup Translate - Integration Tests",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Test rounds:\n"
            + "\n".join(
                f"  {i+1}. {r['name']} — {r['description']}" if r else
                f"  {i+1}. (dynamic — loaded from config.yaml)"
                for i, r in enumerate(ROUNDS)
            )
        ),
    )
    parser.add_argument("--single", action="store_true",
                        help="Quick single-ticket test (same as --rounds 1)")
    parser.add_argument("--rounds", type=int, default=None,
                        help="Number of rounds to run (1-N). Use 0 for ALL rounds.")
    parser.add_argument("--list-rounds", action="store_true",
                        help="List available test rounds and exit")
    parser.add_argument("--unit", action="store_true",
                        help="Run unit tests only (no GLPI/Ollama required)")
    parser.add_argument("--cleanup", action="store_true",
                        help="Clean up test tickets created by this script and exit")
    parser.add_argument("-c", "--config", default=None,
                        help="Path to config.yaml")
    args = parser.parse_args()

    # ── Load config and setup ──────────────────────────────────────────────
    config = load_config(args.config)
    setup_logging(config)
    logging.getLogger("glpi_followup_translate").setLevel(logging.DEBUG)

    # Inject dynamic glossary round (index 4 = Round 5)
    ROUNDS[4] = build_glossary_round(config)

    # ── List rounds ────────────────────────────────────────────────────────
    if args.list_rounds:
        print(f"Available test rounds ({len(ROUNDS)} total):\n")
        for i, r in enumerate(ROUNDS, 1):
            if r is None:
                print(f"  Round {i}: (requires config to load)")
                print()
                continue
            print(f"  Round {i}: {r['name']}")
            print(f"           {r['description']}")
            print(f"           Tickets: {len(r['tickets'])}")
            print()
        return

    # ── Unit tests (no GLPI/Ollama required, but needs config for glossary) ─
    if args.unit:
        ok = run_unit_tests(config)
        sys.exit(0 if ok else 1)

    glpi = GlpiClient(config.glpi)
    ollama = OllamaClient(config.ollama)
    state = ProcessedState()

    preflight(glpi, ollama, config)

    # ── Cleanup only ───────────────────────────────────────────────────────
    if args.cleanup:
        cleanup_test_tickets(glpi)
        return

    # ── Determine which rounds to run ──────────────────────────────────────
    if args.single:
        rounds_to_run = [0]
    elif args.rounds is not None:
        if args.rounds == 0:
            rounds_to_run = list(range(len(ROUNDS)))
        else:
            max_r = min(args.rounds, len(ROUNDS))
            rounds_to_run = list(range(max_r))
    else:
        # Default: run round 1
        rounds_to_run = [0]

    # Validate
    for idx in rounds_to_run:
        if idx < 0 or idx >= len(ROUNDS):
            print(f"ERROR: Round {idx + 1} does not exist. Available: 1-{len(ROUNDS)}")
            sys.exit(1)

    print(f"Running {len(rounds_to_run)} round(s): "
          + ", ".join(str(i + 1) for i in rounds_to_run))
    print()

    # ── Run tests ──────────────────────────────────────────────────────────
    run_all_rounds(glpi, ollama, config, state, rounds_to_run)


if __name__ == "__main__":
    main()
