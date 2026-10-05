from app.constants import FIELD_TYPE_LABELS, FIELD_TYPES
from app.engine import render_body
from app.schema import (
    apply_stored_secrets,
    clean_instructions,
    clean_paste_note,
    instructions_error,
    parse_editor,
    paste_blocks_from_row,
    paste_note_problem,
    template_values,
    validate_values,
)
from app.seed import seed_schema


def values(**overrides):
    base = {
        "hostname": "SITE-IDF1-SW01",
        "domain_name": "example.com",
        "location": "Building A IDF1",
        "contact": "Network Operations",
        "timezone": "Eastern",
        "mgmt_path": "oob",
        "mgmt_ip": "10.20.30.40",
        "mgmt_mask": "255.255.255.0",
        "mgmt_gateway": "10.20.30.1",
        "mgmt_vlan": "",
        "uplink_interface": "",
        "local_username": "netadmin",
        "local_secret": "LocalPass123",
        "enable_secret": "EnablePass123",
        "config_key": "ConfigKey123",
        "tacacs_server_1": "",
        "tacacs_server_2": "",
        "tacacs_key": "",
        "ntp_server_1": "",
        "ntp_server_2": "",
        "syslog_server_1": "",
        "syslog_server_2": "",
        "snmp_community": "",
        "banner_text": "Authorized access only.",
    }
    base.update(overrides)
    return base


def test_seed_values_accept_a_management_subnet():
    cleaned, errors = validate_values(seed_schema(), values())
    assert errors == {}
    assert cleaned["hostname"] == "SITE-IDF1-SW01"


def test_gateway_must_sit_in_the_management_subnet():
    _, errors = validate_values(seed_schema(), values(mgmt_gateway="10.20.31.1"))
    assert "subnet" in errors["mgmt_gateway"]


def test_vlan_and_uplink_are_required_only_for_the_in_band_path():
    _, missing = validate_values(seed_schema(), values(mgmt_path="svi"))
    assert "mgmt_vlan" in missing
    assert "uplink_interface" in missing
    cleaned, errors = validate_values(
        seed_schema(),
        values(mgmt_path="svi", mgmt_vlan="100", uplink_interface="GigabitEthernet1/0/48"),
    )
    assert errors == {}
    assert cleaned["mgmt_vlan"] == "100"


def test_a_blank_radius_key_uses_the_stored_secret():
    schema = {
        "sections": [
            {
                "title": "RADIUS",
                "fields": [
                    {
                        "name": "radius_key",
                        "label": "RADIUS key",
                        "type": "password",
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
                    }
                ],
            }
        ],
        "checks": [],
    }
    _, missing = validate_values(
        schema,
        {"radius_key": ""},
        stored_secret_labels={"radius_key": "RADIUS key"},
    )
    assert "stored RADIUS key" in missing["radius_key"]
    cleaned, errors = validate_values(schema, {"radius_key": ""}, {"radius_key"})
    assert errors == {}
    filled = apply_stored_secrets(schema, cleaned, {"radius_key": "RadiusKey123"})
    assert filled["radius_key"] == "RadiusKey123"


def test_tacacs_key_is_required_when_a_server_is_set():
    _, errors = validate_values(seed_schema(), values(tacacs_server_1="10.1.1.10"))
    assert "tacacs_key" in errors


def _range_schema(**field_overrides):
    field = {
        "name": "access_ports",
        "label": "Access ports",
        "type": "interface_range",
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
    }
    field.update(field_overrides)
    return {"sections": [{"title": "Ports", "fields": [field]}], "checks": []}


def _editor_form(**overrides):
    form = {
        "template_name": "Access ports",
        "template_description": "Day-0 access port range for a closet switch.",
        "body": "interface range {{ access_ports }}\n",
        "follow_up": "",
        "section_title": ["Ports"],
        "field_section": ["0"],
        "field_name": ["access_ports"],
        "field_label": ["Access ports"],
        "field_type": ["interface_range"],
        "field_requirement": ["required"],
        "field_default": ["GigabitEthernet1/0/1 - 48"],
        "field_help": [""],
        "field_choices": [""],
        "field_when_field": [""],
        "field_when_value": [""],
        "field_when_list": [""],
        "field_show_field": [""],
        "field_show_value": [""],
        "field_multiline": ["no"],
        "field_token": ["no"],
    }
    form.update(overrides)
    return form


def _vlan_choices():
    return [
        {"value": "10", "label": "DATA"},
        {"value": "20", "label": "VOICE"},
        {"value": "30", "label": "User Data"},
    ]


def _listed_field(name, field_type, **overrides):
    field = {
        "name": name,
        "label": "User VLANs" if field_type == "choices" else "Note",
        "type": field_type,
        "requirement": "optional",
        "default": "",
        "help": "",
        "multiline": False,
        "token": False,
        "choices": _vlan_choices() if field_type == "choices" else [],
        "when_field": "",
        "when_value": "",
        "when_fields": [],
        "show_field": "",
        "show_value": "",
    }
    field.update(overrides)
    return field


def test_checked_choices_keep_the_written_order_and_render_value_and_label():
    schema = {
        "sections": [{"title": "VLANs", "fields": [_listed_field("user_vlans", "choices", requirement="required")]}],
        "checks": [],
    }
    body = "{% for vlan in user_vlans %}\nvlan {{ vlan.value }}\n name {{ vlan.label }}\n{% endfor %}\n"
    missing, errors = validate_values(schema, {"user_vlans": []})
    assert errors["user_vlans"] == "Check at least one for User VLANs."
    assert missing["user_vlans"] == []
    cleaned, errors = validate_values(schema, {"user_vlans": ["30", "10", "30"]})
    assert errors == {}
    assert cleaned["user_vlans"] == ["10", "30"]
    text = render_body(body, template_values(schema, cleaned))
    assert text == "vlan 10\n name DATA\n\nvlan 30\n name User Data\n"
    rejected, errors = validate_values(schema, {"user_vlans": ["10", "999"]})
    assert errors["user_vlans"] == "Check a listed value for User VLANs."
    assert rejected["user_vlans"] == ["10"]


def test_a_filled_choices_field_can_make_another_field_required():
    schema = {
        "sections": [
            {
                "title": "VLANs",
                "fields": [
                    _listed_field("user_vlans", "choices"),
                    _listed_field("note", "text", requirement="any", when_fields=["user_vlans"]),
                ],
            }
        ],
        "checks": [],
    }
    _, quiet = validate_values(schema, {"user_vlans": [], "note": ""})
    assert quiet == {}
    _, errors = validate_values(schema, {"user_vlans": ["20"], "note": ""})
    assert "note" in errors


def test_a_choices_field_cannot_be_the_single_value_another_field_matches():
    columns = {
        "section_title": ["VLANs", "More"],
        "field_section": ["0", "1"],
        "field_name": ["user_vlans", "note"],
        "field_label": ["User VLANs", "Note"],
        "field_type": ["choices", "text"],
        "field_requirement": ["optional", "optional"],
        "field_default": ["", ""],
        "field_help": ["", ""],
        "field_choices": ["10 | DATA\n20 | VOICE", ""],
        "field_when_field": ["", ""],
        "field_when_value": ["", ""],
        "field_when_list": ["", ""],
        "field_show_field": ["", "user_vlans"],
        "field_show_value": ["", "10"],
        "field_multiline": ["no", "no"],
        "field_token": ["no", "no"],
        "body": "hostname SW\n",
    }
    shown = parse_editor(_editor_form(**columns))
    assert any("list" in message for message in shown.errors)
    columns["field_show_field"] = ["", ""]
    columns["field_requirement"] = ["optional", "equals"]
    columns["field_when_field"] = ["", "user_vlans"]
    columns["field_when_value"] = ["", "10"]
    matched = parse_editor(_editor_form(**columns))
    assert any("list" in message for message in matched.errors)
    columns["field_requirement"] = ["optional", "any"]
    columns["field_when_field"] = ["", ""]
    columns["field_when_value"] = ["", ""]
    columns["field_when_list"] = ["", "user_vlans"]
    allowed = parse_editor(_editor_form(**columns))
    assert allowed.errors == []
    too_many = parse_editor(
        _editor_form(
            field_type=["choices"],
            field_choices=["\n".join("%d | VLAN%d" % (index, index) for index in range(1, 42))],
            body="hostname SW\n",
        )
    )
    assert any("40" in message for message in too_many.errors)
    defaulted = parse_editor(_editor_form(field_type=["choices"], field_default=["10"], field_choices=["10 | DATA"], body="hostname SW\n"))
    assert any("nothing checked" in message for message in defaulted.errors)


def test_every_field_type_has_a_label():
    assert set(FIELD_TYPE_LABELS) == set(FIELD_TYPES)
    assert FIELD_TYPE_LABELS["interface_range"] == "interface range"


def test_interface_range_accepts_cisco_ranges():
    schema = _range_schema()
    accepted = [
        "gigabitEthernet1/0/1 - 48",
        "GigabitEthernet1/0/1 - 48",
        "Gi1/0/1 - 24",
        "GigabitEthernet0/1 - 24",
        "TenGigabitEthernet1/1/1 - 8",
        "Te1/1/1 - 4",
        "TwentyFiveGigE1/0/1 - 2",
        "Twe1/0/1 - 2",
        "AppGigabitEthernet1/0/1 - 4",
        "Ap1/0/1 - 2",
        "FortyGigabitEthernet1/0/1 - 2",
        "Fo1/0/1 - 2",
        "HundredGigE1/0/1 - 2",
        "Hu1/0/1 - 2",
        "GigabitEthernet1/0/1  -  48",
        "GigabitEthernet1/0/1 - 24, GigabitEthernet1/0/30 - 48",
        "GigabitEthernet1/0/1 - 24,GigabitEthernet1/0/48",
        "GigabitEthernet1/0/1 - 12, GigabitEthernet1/0/13 - 24, GigabitEthernet1/0/25 - 36, GigabitEthernet1/0/37 - 48, TenGigabitEthernet1/1/1 - 4",
    ]
    for text in accepted:
        cleaned, errors = validate_values(schema, {"access_ports": "  %s  " % text})
        assert errors == {}, text
        assert cleaned["access_ports"] == text


def test_interface_range_rejects_bad_ranges():
    schema = _range_schema()
    rejected = [
        "GigabitEthernet1/0/1-48",
        "GigabitEthernet1/0/1 -48",
        "GigabitEthernet1/0/1- 48",
        "GigabitEthernet1/0/48 - 1",
        "GigabitEthernet1/0/1 - 1",
        "GigabitEthernet1/0/1 - 08",
        "Ethernet1/0/1 - 48",
        "Vlan1 - 10",
        "Port-channel1 - 4",
        "GigabitEthernet1/0/1 - 1/0/48",
        "GigabitEthernet1/0/1 - 12, GigabitEthernet1/0/13 - 24, GigabitEthernet1/0/25 - 36, GigabitEthernet1/0/37 - 48, TenGigabitEthernet1/1/1 - 4, AppGigabitEthernet1/0/1 - 2",
    ]
    for text in rejected:
        _, errors = validate_values(schema, {"access_ports": text})
        assert "access_ports" in errors, text


def test_instructions_allow_fifty_lines_and_reject_more():
    fifty = "\n".join("Step %s. Check the console cable." % index for index in range(1, 51))
    assert instructions_error(clean_instructions(fifty)) is None
    assert instructions_error(clean_instructions(fifty + "\nOne line too many.")) is not None
    assert instructions_error("x" * 4000) is None
    assert instructions_error("x" * 4001) is not None
    saved = parse_editor(_editor_form(template_instructions="Read this first.\nLeave {{ hostname }} as written."))
    assert saved.errors == []
    assert saved.instructions == "Read this first.\nLeave {{ hostname }} as written."
    rejected = parse_editor(_editor_form(template_instructions=fifty + "\nextra"))
    assert any("50 lines" in message for message in rejected.errors)


def test_paste_notes_allow_three_lines_and_a_missing_note():
    three = "Wait for the links.\nThen confirm the lights.\nLeave {{ hostname }} as written."
    assert paste_note_problem(clean_paste_note(three), "Note") is None
    assert paste_note_problem(clean_paste_note(three + "\nOne line too many."), "Note") is not None
    assert paste_note_problem("x" * 400, "Note") is None
    assert paste_note_problem("x" * 401, "Note") is not None
    saved = parse_editor(
        _editor_form(
            config_title="Day-0 configuration",
            config_note=three,
            follow_up="write memory\n",
            follow_up_title="Save the switch",
            follow_up_note="Leave {{ hostname }} as written.",
            paste_title=["Interfaces"],
            paste_body=["interface GigabitEthernet1/0/1\n"],
            paste_note=["Wait for the links."],
        )
    )
    assert saved.errors == []
    assert saved.config_title == "Day-0 configuration"
    assert saved.config_note == three
    assert saved.follow_up_title == "Save the switch"
    assert saved.paste_blocks == [
        {"title": "Interfaces", "body": "interface GigabitEthernet1/0/1\n", "note": "Wait for the links."}
    ]
    omitted = parse_editor(
        _editor_form(
            paste_title=["Interfaces"],
            paste_body=["interface GigabitEthernet1/0/1\n"],
        )
    )
    assert omitted.errors == []
    assert omitted.paste_blocks[0]["note"] == ""
    assert paste_blocks_from_row([{"title": "Interfaces", "body": "!\n"}]) == [
        {"title": "Interfaces", "body": "!\n", "note": ""}
    ]
    assert paste_blocks_from_row([{"title": "Interfaces", "body": "!\n", "note": 1}]) == []
    rejected = parse_editor(
        _editor_form(
            config_title="x" * 81,
            paste_title=["Interfaces"],
            paste_body=["!\n"],
            paste_note=["a\nb\nc\nd"],
        )
    )
    assert any("80 characters" in message for message in rejected.errors)
    assert any("3 lines" in message for message in rejected.errors)


def test_paste_marks_accept_only_the_listed_choices():
    saved = parse_editor(
        _editor_form(
            config_mark="console",
            follow_up="write memory\n",
            follow_up_mark="pause",
            paste_title=["Interfaces"],
            paste_body=["interface GigabitEthernet1/0/1\n"],
            paste_mark=["ssh"],
        )
    )
    assert saved.errors == []
    assert saved.config_mark == "console"
    assert saved.follow_up_mark == "pause"
    assert saved.paste_blocks[0]["mark"] == "ssh"
    blank = parse_editor(_editor_form())
    assert blank.config_mark == ""
    assert blank.follow_up_mark == ""
    assert paste_blocks_from_row([{"title": "Interfaces", "body": "!\n", "note": "", "mark": "ssh"}])[0]["mark"] == "ssh"
    loaded = paste_blocks_from_row([{"title": "Interfaces", "body": "!\n", "note": "", "mark": "enable"}])
    assert "mark" not in loaded[0]
    rejected = parse_editor(_editor_form(config_mark="enable"))
    assert any("unknown mark" in message for message in rejected.errors)


def test_sections_and_fields_keep_the_posted_order():
    saved = parse_editor(
        _editor_form(
            section_title=["Services", "Identity"],
            field_section=["1", "0", "1"],
            field_name=["hostname", "banner_text", "domain_name"],
            field_label=["Hostname", "Banner", "Domain"],
            field_type=["hostname", "text", "text"],
            field_requirement=["required", "optional", "required"],
            field_default=["", "", ""],
            field_help=["", "", ""],
            field_choices=["", "", ""],
            field_when_field=["", "", ""],
            field_when_value=["", "", ""],
            field_when_list=["", "", ""],
            field_show_field=["", "", ""],
            field_show_value=["", "", ""],
            field_multiline=["no", "yes", "no"],
            field_token=["no", "no", "no"],
            body="hostname {{ hostname }}\n",
        )
    )
    assert saved.errors == []
    assert [section["title"] for section in saved.schema["sections"]] == ["Services", "Identity"]
    assert [item["name"] for item in saved.schema["sections"][0]["fields"]] == ["banner_text"]
    assert [item["name"] for item in saved.schema["sections"][1]["fields"]] == ["hostname", "domain_name"]


def test_a_dropdown_rejects_choose_one_until_a_value_is_selected():
    _, errors = validate_values(seed_schema(), values(timezone="choose", mgmt_path="choose"))
    assert errors["timezone"] == "Choose one for Timezone."
    assert errors["mgmt_path"] == "Choose one for Management path."
    optional = {
        "sections": [
            {
                "title": "Options",
                "fields": [
                    {
                        "name": "speed",
                        "label": "Speed",
                        "type": "choice",
                        "requirement": "optional",
                        "default": "",
                        "help": "",
                        "multiline": False,
                        "token": False,
                        "choices": [
                            {"value": "100", "label": "100 Mb"},
                            {"value": "1000", "label": "1 Gb"},
                        ],
                        "when_field": "",
                        "when_value": "",
                        "when_fields": [],
                        "show_field": "",
                        "show_value": "",
                    }
                ],
            }
        ],
        "checks": [],
    }
    cleaned, ok = validate_values(optional, {"speed": "choose"})
    assert ok == {}
    assert cleaned["speed"] == ""
    _, rejected_value = validate_values(optional, {"speed": "nope"})
    assert rejected_value["speed"] == "Choose one of the listed values."
    reserved = parse_editor(
        _editor_form(
            field_type=["choice"],
            field_default=["full"],
            field_choices=["choose | Choose One\nfull | Full"],
        )
    )
    assert any("reserved" in message for message in reserved.errors)
    assert any("Remove the default" in message for message in reserved.errors)
    saved = parse_editor(
        _editor_form(
            field_type=["choice"],
            field_default=[""],
            field_choices=["full | Full\nhalf | Half"],
        )
    )
    assert saved.errors == []
    assert saved.schema["sections"][0]["fields"][0]["default"] == ""
    assert [item["value"] for item in saved.schema["sections"][0]["fields"][0]["choices"]] == ["full", "half"]


def test_interface_range_default_uses_the_same_rules():
    saved = parse_editor(_editor_form())
    assert saved.errors == []
    assert saved.schema["sections"][0]["fields"][0]["default"] == "GigabitEthernet1/0/1 - 48"
    rejected = parse_editor(_editor_form(field_default=["GigabitEthernet1/0/1-48"]))
    assert any("interface range" in message for message in rejected.errors)


def test_an_interface_name_can_include_a_space_before_the_numbers():
    cleaned, errors = validate_values(
        seed_schema(),
        values(mgmt_path="svi", mgmt_vlan="100", uplink_interface="tenGigabitEthernet 1/1/4"),
    )
    assert errors == {}
    assert cleaned["uplink_interface"] == "tenGigabitEthernet1/1/4"
    spaced, errors = validate_values(
        _range_schema(),
        {"access_ports": "TenGigabitEthernet 1/1/1 - 8, GigabitEthernet 1/0/1 - 24"},
    )
    assert errors == {}
    assert spaced["access_ports"] == "TenGigabitEthernet1/1/1 - 8, GigabitEthernet1/0/1 - 24"
    saved = parse_editor(
        _editor_form(field_type=["interface"], field_default=["Te 1/1/4"], body="interface {{ access_ports }}\n")
    )
    assert saved.errors == []
    assert saved.schema["sections"][0]["fields"][0]["default"] == "Te1/1/4"


def test_hostname_vlan_and_interface_rules():
    _, errors = validate_values(
        seed_schema(),
        values(hostname="bad host", mgmt_path="svi", mgmt_vlan="5000", uplink_interface="Ethernet1"),
    )
    assert "hostname" in errors
    assert "mgmt_vlan" in errors
    assert "uplink_interface" in errors
