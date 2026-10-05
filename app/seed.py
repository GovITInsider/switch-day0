import json
from pathlib import Path

SEED_NAME = "C9300 Day-0 Bootstrap"
SEED_NOTE = "Seeded as the starting approved template."
SEED_DESCRIPTION = (
    "Day-0 bootstrap for a Catalyst 9300 running IOS-XE. It sets the hostname, "
    "management reachability, local accounts, a type 6 encryption key, AAA, NTP, syslog, SNMP, "
    "and the SSH baseline. It does not configure user VLANs or access ports. Out-of-band TACACS "
    "uses a server group in Mgmt-vrf. After the paste, it generates a 4096-bit SSH key and saves. "
    "Replace the placeholder defaults with your standard before the team relies on this."
)
FOLLOW_UP = "crypto key generate rsa general-keys modulus 4096\nwrite memory\n"

BODY = Path(__file__).resolve().parent.joinpath("standards", "c9300_day0.j2").read_text(encoding="utf-8")


def _choice(value, label=None):
    return {"value": value, "label": label or value}


def _field(name, label, field_type, requirement="optional", default="", help_text="", **extra):
    field = {
        "name": name,
        "label": label,
        "type": field_type,
        "requirement": requirement,
        "default": default,
        "help": help_text,
        "multiline": False,
        "token": False,
        "choices": [],
        "when_field": "",
        "when_value": "",
        "when_fields": [],
        "show_field": "",
        "show_value": "",
    }
    field.update(extra)
    return field


def seed_schema():
    timezones = [
        _choice("Eastern", "Eastern"),
        _choice("Central", "Central"),
        _choice("Mountain", "Mountain"),
        _choice("Arizona", "Arizona (no daylight saving)"),
        _choice("Pacific", "Pacific"),
        _choice("UTC", "UTC"),
    ]
    paths = [
        _choice("oob", "Dedicated port GigabitEthernet0/0"),
        _choice("svi", "In-band VLAN interface"),
    ]
    return {
        "sections": [
            {
                "title": "Identity",
                "fields": [
                    _field("hostname", "Hostname", "hostname", "required", help_text="Letters, digits, and hyphens."),
                    _field(
                        "domain_name",
                        "Domain name",
                        "text",
                        "required",
                        "example.com",
                        "Used for SSH and name display.",
                        token=True,
                    ),
                    _field("location", "Location", "text", "required", help_text="Shown in SNMP location."),
                    _field("contact", "Contact", "text", "required", "Network Operations"),
                    _field(
                        "timezone",
                        "Timezone",
                        "choice",
                        "required",
                        "",
                        "US time zones include daylight saving, except Arizona.",
                        choices=timezones,
                    ),
                ],
            },
            {
                "title": "Management",
                "fields": [
                    _field(
                        "mgmt_path",
                        "Management path",
                        "choice",
                        "required",
                        "",
                        "Use the dedicated management port when it has a cable.",
                        choices=paths,
                    ),
                    _field("mgmt_ip", "Management address", "ipv4", "required"),
                    _field("mgmt_mask", "Management mask", "netmask", "required", "255.255.255.0"),
                    _field(
                        "mgmt_gateway",
                        "Management gateway",
                        "ipv4",
                        "required",
                        help_text="Must sit in the management subnet.",
                    ),
                    _field(
                        "mgmt_vlan",
                        "Management VLAN",
                        "vlan",
                        "equals",
                        help_text="Required for the in-band path.",
                        when_field="mgmt_path",
                        when_value="svi",
                        show_field="mgmt_path",
                        show_value="svi",
                    ),
                    _field(
                        "uplink_interface",
                        "Uplink interface",
                        "interface",
                        "equals",
                        help_text="The one port that has to be up so the switch answers. Example: GigabitEthernet1/0/48.",
                        when_field="mgmt_path",
                        when_value="svi",
                        show_field="mgmt_path",
                        show_value="svi",
                    ),
                ],
            },
            {
                "title": "Local accounts",
                "fields": [
                    _field(
                        "local_username",
                        "Local username",
                        "text",
                        "required",
                        help_text="Emergency account stored on the switch.",
                        token=True,
                    ),
                    _field(
                        "local_secret",
                        "Local password",
                        "password",
                        "required",
                        help_text="Pasted in clear text. The switch hashes it.",
                    ),
                    _field(
                        "enable_secret",
                        "Enable password",
                        "password",
                        "required",
                        help_text="Pasted in clear text. The switch hashes it.",
                    ),
                    _field(
                        "config_key",
                        "Configuration encryption key",
                        "password",
                        "required",
                        help_text="Master key for type 6 encryption. Pasted once. The switch keeps it outside the configuration.",
                    ),
                ],
            },
            {
                "title": "Services",
                "fields": [
                    _field("tacacs_server_1", "TACACS server 1", "ipv4", "optional"),
                    _field("tacacs_server_2", "TACACS server 2", "ipv4", "optional"),
                    _field(
                        "tacacs_key",
                        "TACACS key",
                        "password",
                        "any",
                        help_text="Required when a TACACS server is set.",
                        when_fields=["tacacs_server_1", "tacacs_server_2"],
                    ),
                    _field("ntp_server_1", "NTP server 1", "ipv4", "optional"),
                    _field("ntp_server_2", "NTP server 2", "ipv4", "optional"),
                    _field("syslog_server_1", "Syslog server 1", "ipv4", "optional"),
                    _field("syslog_server_2", "Syslog server 2", "ipv4", "optional"),
                    _field(
                        "snmp_community",
                        "SNMP community",
                        "text",
                        "optional",
                        help_text="Leave blank to omit the read-only community.",
                        token=True,
                    ),
                    _field(
                        "banner_text",
                        "Login banner",
                        "text",
                        "required",
                        "Authorized access only. Disconnect if you are not an authorized user.",
                        "Shown before login. A caret cannot be used.",
                        multiline=True,
                    ),
                ],
            },
        ],
        "checks": [{"same_subnet": ["mgmt_ip", "mgmt_mask", "mgmt_gateway"]}],
    }


def seed_schema_json():
    return json.dumps(seed_schema(), indent=2)
