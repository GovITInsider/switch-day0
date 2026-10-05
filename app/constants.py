ROLES = ("operator", "editor", "approver", "admin")
RANK = {name: index for index, name in enumerate(ROLES)}
ROLE_LABELS = {
    "operator": "Operator",
    "editor": "Editor",
    "approver": "Approver",
    "admin": "Admin",
}
MAX_USERS = 10
MAX_PASTE_BLOCKS = 6
PLATFORM = "Cisco IOS-XE"
SEED_ORIGIN = "c9300-day0"
LOCK_MINUTES = 20
FIELD_TYPES = (
    "text",
    "hostname",
    "vlan",
    "ipv4",
    "netmask",
    "interface",
    "interface_range",
    "password",
    "bool",
    "choice",
    "choices",
)
FIELD_TYPE_LABELS = {
    "text": "text",
    "hostname": "hostname",
    "vlan": "vlan",
    "ipv4": "ipv4",
    "netmask": "netmask",
    "interface": "interface",
    "interface_range": "interface range",
    "password": "password",
    "bool": "bool",
    "choice": "choice",
    "choices": "choices",
}
REQUIREMENTS = ("optional", "required", "equals", "any")
MONTH_LINES = (
    "A new year, and the day-0 paste is still a privilege.",
    "The console cable is not a Valentine. Label it anyway.",
    "May your uplinks be up and your STP be rapid.",
    "April 1. Nothing in this configuration is a joke.",
    "May the first hop answer, and the second hop too.",
    "Midyear is a fine time to read the banner before you lock the template.",
    "The other ticket will keep. A half-pasted config will not.",
    "Someone is on vacation. The local account is why you can still get in.",
    "Change-window season. Generate it, read it, then paste it.",
    "If the switch feels haunted, check the native VLAN.",
    "It was DNS. It is always DNS.",
    "Happy Holidays. Shut the port, no shut the port, go home.",
    "Change management isn't about control—it's about balance. Structure keeps us aligned. Inclusion keeps us safe.",
    "When engineers have a real seat and leadership has real data, risk drops, trust rises, and security becomes a team sport.",
    "Your worst incident will be the change somebody did \"just this once,\" because review felt like friction.",
    "Read the configuration twice. The second look is the job.",
    "You can't secure a ghost. Define it or deny it.",
    "Every change is a security decision. Treat it as one.",
    "Emergencies happen. Fake emergencies create the next real one.",
    "It's not about saying no. It's saying yes, safely.",
    "Protect the system. Protect your integrity. Protect the public.",
    "Test hard. Kill fast. Fix cheap. Learn forever.",
    "Layer 1 is the only layer you can't patch, firewall, or reboot.",
    "Hope is not a cabling strategy.",
)
MAX_STORED_SECRETS = 8
STARTER_SECRETS = (
    ("local_secret", "Admin password"),
    ("enable_secret", "Enable password"),
    ("config_key", "Configuration encryption key"),
    ("tacacs_key", "TACACS key"),
)
LEGACY_SECRET_LABELS = {
    "tacacs_key": "TACACS key",
    "radius_key": "RADIUS key",
    "local_secret": "Admin password",
    "enable_secret": "Enable password",
    "snmp_auth_key": "SNMPv3 auth key",
    "snmp_priv_key": "SNMPv3 priv key",
    "config_key": "Configuration encryption key",
}
STORED_KEY_PLACEHOLDER = "Stored key"
