"""HA Core 2026.7.2 SupervisorAddonUpdateEntity.async_release_notes contract.

Source: https://github.com/home-assistant/core/blob/2026.7.2/homeassistant/components/hassio/update.py
The exact upstream regex requires version-only headings, including the stop heading.
"""
import re
from conftest import ADDON


def release_notes(changelog, latest, installed):
    pattern = re.compile(
        rf"^#* {re.escape(latest)}\n"
        rf"(?:^(?!#* {re.escape(installed)}).*\n)*", re.MULTILINE,
    )
    match = pattern.search(changelog)
    return match.group(0) if match else changelog


def test_real_changelog_is_bounded_to_new_release():
    changelog = (ADDON/'CHANGELOG.md').read_text()
    headings = re.findall(r'^## (.+)$', changelog, re.MULTILINE)
    latest, installed = [re.search(r'\d+\.\d+\.\d+', h).group(0) for h in headings[:2]]
    actual = release_notes(changelog, latest, installed)
    assert actual.startswith(f'## {latest}\n')
    assert installed not in actual
    assert len(actual) < len(changelog)


def test_plain_heading_extracts_but_brackets_and_inline_dates_fall_back():
    plain = '# Changelog\n\n## 0.1.3\n\n2026-10-13\n\nnew\n\n## 0.1.2\n\nold\n'
    assert release_notes(plain, '0.1.3', '0.1.2') == '## 0.1.3\n\n2026-10-13\n\nnew\n\n'
    for bad in ('## [0.1.3] - 2026-10-13', '## 0.1.3 — 2026-10-13'):
        text = plain.replace('## 0.1.3', bad)
        assert release_notes(text, '0.1.3', '0.1.2') == text
