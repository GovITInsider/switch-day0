from app.engine import check_template, join_config, render_config, render_follow_up, render_paste_blocks
from app.schema import validate_values
from app.seed import BODY, FOLLOW_UP, seed_schema
from tests.test_schema import values


def rendered(**overrides):
    cleaned, errors = validate_values(seed_schema(), values(**overrides))
    assert errors == {}
    return render_config(
        BODY,
        cleaned,
        ["! Template: C9300 Day-0 Bootstrap  revision 1  approved 2026-09-24 18:00 UTC by admin"],
    )


def test_a_choices_field_is_checked_as_a_list():
    schema = {"sections": [{"fields": [{"name": "user_vlans", "type": "choices"}]}], "checks": []}
    errors, _warnings = check_template("{{ user_vlans.split(',') }}\n", schema)
    assert errors
    errors, warnings = check_template("{% for vlan in user_vlans %}\n{{ vlan.value }}\n{% endfor %}\n", schema)
    assert errors == []
    assert warnings == []


def test_seed_template_matches_its_form():
    errors, warnings = check_template(BODY, seed_schema(), FOLLOW_UP)
    assert errors == []
    assert warnings == []


def test_out_of_band_config_uses_the_management_vrf():
    text = rendered(
        tacacs_server_1="10.1.1.10",
        tacacs_server_2="10.1.1.11",
        tacacs_key="TacacsKey123",
        ntp_server_1="10.1.1.20",
        syslog_server_1="10.1.1.30",
        snmp_community="monitor-ro",
    )
    assert "hostname SITE-IDF1-SW01" in text
    assert "clock timezone EST -5 0" in text
    assert "vrf forwarding Mgmt-vrf" in text
    assert "ip address 10.20.30.40 255.255.255.0" in text
    assert "ip route vrf Mgmt-vrf 0.0.0.0 0.0.0.0 10.20.30.1" in text
    assert "aaa group server tacacs+ MGMT-TACACS" in text
    assert "ip tacacs source-interface GigabitEthernet0/0" in text
    assert "aaa authentication login default group MGMT-TACACS local" in text
    assert "key 0 TacacsKey123" in text
    assert "ntp server vrf Mgmt-vrf 10.1.1.20" in text
    assert "logging host 10.1.1.30 vrf Mgmt-vrf" in text
    assert "snmp-server community monitor-ro RO" in text
    assert "banner motd ^" in text
    assert "transport input ssh" in text
    assert "key config-key password-encrypt ConfigKey123" in text
    assert "password encryption aes" in text
    assert text.index("password encryption aes") < text.index("key 0 TacacsKey123")
    assert "crypto key generate" not in text
    assert "write memory" not in text
    assert "interface Vlan" not in text
    follow = render_follow_up(FOLLOW_UP, {})
    assert follow == "crypto key generate rsa general-keys modulus 4096\nwrite memory\n"


def test_in_band_config_uses_one_uplink_and_an_svi():
    text = rendered(
        mgmt_path="svi",
        mgmt_vlan="100",
        uplink_interface="GigabitEthernet1/0/48",
        tacacs_server_1="10.1.1.10",
        tacacs_key="TacacsKey123",
    )
    assert "interface GigabitEthernet1/0/48" in text
    assert "switchport access vlan 100" in text
    assert "interface Vlan100" in text
    assert "ip default-gateway 10.20.30.1" in text
    assert "aaa group server tacacs+ TACACS" in text
    assert "ip tacacs source-interface Vlan100" in text
    assert "Mgmt-vrf" not in text
    assert "GigabitEthernet0/0" not in text


def test_local_only_aaa_when_tacacs_is_blank():
    text = rendered()
    assert "aaa authentication login default local" in text
    assert "tacacs server" not in text


def test_templates_cannot_include_files_or_unknown_variables():
    errors, _warnings = check_template("{% include 'other.j2' %}\nhostname {{ hostname }}\n", seed_schema())
    assert any("self-contained" in message for message in errors)
    errors, _warnings = check_template("hostname {{ missing_name }}\n", seed_schema())
    assert any("missing_name" in message for message in errors)
    errors, _warnings = check_template("hostname {{ hostname }}\n", seed_schema(), "{% include 'other.j2' %}\n")
    assert any("Follow-up commands are self-contained" in message for message in errors)


def test_a_field_used_only_in_the_follow_up_counts_as_used():
    schema = {
        "sections": [
            {
                "title": "Identity",
                "fields": [
                    {
                        "name": "hostname",
                        "label": "Hostname",
                        "type": "hostname",
                        "requirement": "required",
                        "default": "",
                        "help": "",
                        "multiline": False,
                        "token": False,
                        "choices": [],
                        "when_field": "",
                        "when_value": "",
                        "when_fields": [],
                        "show_field": "",
                        "show_value": "",
                    },
                    {
                        "name": "key_label",
                        "label": "Key label",
                        "type": "text",
                        "requirement": "required",
                        "default": "SSH",
                        "help": "",
                        "multiline": False,
                        "token": True,
                        "choices": [],
                        "when_field": "",
                        "when_value": "",
                        "when_fields": [],
                        "show_field": "",
                        "show_value": "",
                    },
                ],
            }
        ],
        "checks": [],
    }
    body = "hostname {{ hostname }}\n"
    errors, warnings = check_template(body, schema, "crypto key generate rsa label {{ key_label }} modulus 4096\n")
    assert errors == []
    assert warnings == []
    errors, _warnings = check_template(body, schema, "crypto key generate rsa label {{ missing_label }}\n")
    assert any("missing_label" in message for message in errors)


def test_a_field_used_only_in_a_paste_block_counts_as_used():
    schema = {
        "sections": [{"title": "Identity", "fields": [{"name": "hostname"}, {"name": "key_label"}]}],
        "checks": [],
    }
    body = "hostname {{ hostname }}\n"
    blocks = [{"title": "Keys", "body": "crypto key generate rsa label {{ key_label }} modulus 4096\n"}]
    errors, warnings = check_template(body, schema, "", blocks)
    assert errors == []
    assert warnings == []
    errors, _warnings = check_template(body, schema, "", [{"title": "Keys", "body": "{% include 'other.j2' %}\n"}])
    assert any("Paste blocks are self-contained" in message for message in errors)
    errors, _warnings = check_template(
        body,
        schema,
        "",
        [{"title": "Keys", "body": "crypto key generate rsa label {{ missing_label }}\n"}],
    )
    assert any("missing_label" in message for message in errors)


def test_an_empty_paste_block_is_left_out_of_the_configuration():
    blocks = render_paste_blocks(
        [
            {"title": "Hidden", "body": "{% if false %}\nhidden\n{% endif %}\n"},
            {"title": "Interfaces", "body": "interface GigabitEthernet1/0/1\n"},
        ],
        {},
    )
    assert [block["title"] for block in blocks] == ["Interfaces"]
    assert blocks[0]["dom_id"] == "paste-block-1"
    assert join_config("hostname SW\n", blocks) == "hostname SW\ninterface GigabitEthernet1/0/1\n"
