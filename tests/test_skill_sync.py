"""Regression tests for documentation and skill drift checks."""

from scripts import check_skill_sync as sync


def test_project_docs_match_executable_contracts():
    errors: list[str] = []
    skills = sync._load_skills()

    sync.check_skill_inventory(skills, errors)
    sync.check_cli_flags(skills, errors)
    sync.check_doc_cli_flags(errors)
    sync.check_factor_ops(errors)
    sync.check_select_keys(errors)
    sync.check_filter_rules(errors)
    sync.check_condition_keys(errors)
    sync.check_config_defaults(errors)
    sync.check_ml_contract(errors)
    sync.check_factor_arithmetic(errors)
    sync.check_local_links(errors)

    assert errors == []


def test_contract_parser_uses_first_protocol_table_only():
    text = """
Intro.

| key | meaning |
| --- | --- |
| `buy` | entry |
| `sell` | exit |

Later execution rows include fields.

| key | meaning |
| --- | --- |
| `price` | executed price |
"""

    assert sync._first_table_tokens(text) == {"buy", "sell"}


def test_markdown_anchors_support_explicit_and_heading_targets():
    text = """
<a id="stable"></a>
## Data Alignment

### `query_bars` contract
"""

    assert sync._markdown_anchors(text) == {
        "stable",
        "data-alignment",
        "query_bars-contract",
    }
