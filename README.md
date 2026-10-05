# Switch Day-0

A web app for Cisco switch day-0 configuration. An engineer picks an approved template, fills in the form, and copies the rendered IOS-XE text into the switch CLI. The server does not store the generated configuration. One template can cover one model, and another template can cover a different model.

Drafts can be edited in the browser. They stay off the generate page until an approver locks them. Revising a locked template opens a new draft and leaves the locked copy in place.

## Features

- **Approved templates** — Operators generate configuration from locked templates
- **Workshop** — Editors draft changes; approvers lock, send back, reject, retire, restore, and roll back
- **Generated text stays in the browser** — The rendered IOS-XE text is shown for copying and is not saved on the server
- **Accounts** — Up to 10 users, with operator, editor, approver, and admin roles

## Tech Stack

- Python 3.9 or newer
- FastAPI + Uvicorn
- SQLite
- Jinja2

## Getting Started

### Installation

The included installation script installs the app with apt, runs it under systemd, and listens on port 8001.

Generated configurations contain passwords and keys. Keep port 8001 on the engineering network. Do not publish it to the internet.

#### 1. Clone the Repository

```bash
git clone https://github.com/GovITInsider/switch-day-0.git
cd switch-day-0
```

#### 2. Run the Installation Script

```bash
sudo bash scripts/install.sh
```

This script automates the following tasks:

- Creates a dedicated system user (`switchday0`)
- Copies the application to `/opt/switch-day0`
- Sets up a Python virtual environment and installs dependencies
- Creates the `data/` directory
- Creates `.env` with an admin username, a generated admin password, and a generated session secret when `.env` is not already there
- Installs and starts the systemd service on port 8001

Running the script again leaves the existing `.env` and `data/` directory in place. After a `git pull`, run it again to update the app and restart the service.

#### 3. Read the Admin Password

On the first install, the script prints the admin username and password. They are also stored here:

```bash
sudo nano /opt/switch-day0/.env
```

`SWITCH_DAY0_ADMIN_PASSWORD` applies only while the database has no users. After the first start, change passwords from the Users page.

#### 4. Access the Web Interface

Open your browser and go to:

```
http://your-server-ip:8001/
```

Sign in with the admin username and password from `.env`.

## Access the Web Interface

- **Sign in:** http://your-server:8001/login
- **Generate:** http://your-server:8001/

## Configuration

Switch Day-0 reads `/opt/switch-day0/.env`. The install script creates that file from `.env.example`.

| Variable | Purpose |
| --- | --- |
| `SWITCH_DAY0_ADMIN_USERNAME` | First account name. Defaults to `admin`. Starts with a letter, then lowercase letters, digits, `.`, `_`, or `-`. |
| `SWITCH_DAY0_ADMIN_PASSWORD` | Password for that first account. 8 to 200 characters. Used only when the database has no users yet. |
| `SWITCH_DAY0_SECRET` | Signs the login cookie. If omitted, the app stores one in `data/secret.key`. |
| `SWITCH_DAY0_DB` | Optional path to the SQLite file. Defaults to `data/switch-day0.db`. |

The `data/` directory holds the SQLite database, and the session secret when `SWITCH_DAY0_SECRET` is unset. It is not part of the git repository. Back it up if this host is the copy the team uses.

## Managing the Service

```bash
# Check status
sudo systemctl status switch-day0

# Restart the service
sudo systemctl restart switch-day0

# View logs
sudo journalctl -u switch-day0 -f

# Stop the service
sudo systemctl stop switch-day0

# Keep it from starting on boot
sudo systemctl disable switch-day0
```

## Update

From a clone you can pull:

```bash
cd switch-day0
git pull
sudo bash scripts/install.sh
```

## Accounts

The app holds at most 10 accounts, including the first admin. An admin can add accounts, reset passwords, remove accounts, and assign any account to any role. At least one admin has to remain.

| Role | What they can do |
| --- | --- |
| Operator | Generate configuration from locked templates |
| Editor | Also create drafts, preview them, and submit them |
| Approver | Also lock a draft, send it back, reject it, retire, restore, and roll back |
| Admin | Also manage the 10 accounts, the stored keys, and permanent deletion of retired or rejected templates and workflows that have nothing else attached |

Sign in as admin, open **Users**, and add the rest of the team. After signing in, any account can change its own password from **Password**. An admin can still reset another account from **Users** without the current password.

Open **Secrets** to store up to 8 shared secrets. Each one has a name and a field name. A new database lists four unset names that match the sample template: `local_secret`, `enable_secret`, `config_key`, and `tacacs_key`. Rename them, remove them, or add others. A matching password field on the generate form shows `Stored key` in gray when that secret has a value. Leaving the field blank uses the stored value. Typing in the field overrides it for that configuration only and does not change the stored value. The template text does not contain these secrets. Removing every secret and starting the app again lists those four names once more.

## Templates

The sample template is **C9300 Day-0 Bootstrap**. It is added when the database has no templates. It is one starting standard, not a limit on which Cisco models the app can render. It sets the hostname, management reachability, local accounts, AAA, NTP, syslog, SNMP, banner, and the SSH baseline. It does not configure user VLANs or access ports. Add another template for a different model.

An interface range field takes a Cisco range such as `GigabitEthernet1/0/1 - 48`. Up to five ranges can be separated by commas. In the IOS-XE text, put that field after `interface range`, for example `interface range {{ access_ports }}`.

A dropdown starts on Choose One. The configuration is not generated until a listed value is selected. An optional dropdown left on Choose One stays blank.

A choices field is a checkbox list of up to 40 rows, written as `value | label`. Nothing starts checked. The template loops over the checked rows in that written order. Each row has `value` and `label`, so a VLAN id and its name can both be pasted. A choices field can be shown from another field. Another field cannot be shown from which rows were checked.

Instructions are optional text on the template, up to 50 lines. The engineer reads them above the form, and they stay above the generated configuration. They are not pasted into the switch and they are not in the downloaded file.

Replace its placeholder defaults with your standard:

1. Open **Workshop**.
2. Choose **Revise** on the locked template.
3. Edit the form fields and the IOS-XE text.
4. Save and submit.
5. An approver reviews the diff and chooses **Approve and lock**.

A workflow is a separate locked page: one markdown procedure and the templates that procedure uses. **New workflow** in Workshop starts the draft. The generate page lists locked workflows above the templates. Opening a template from a workflow uses the same generate form. The locked template stays available to operators while a revision is in progress. Approving the draft replaces it. **Duplicate** on a locked template opens a new draft with the same fields and commands. The locked template stays as it is. Give the copy its own name before you submit it. **Reject** on the review page leaves that revision unlocked and records the reason. A template already on the generate page stays there. **Roll back** on the history page locks an older revision again.

An approver can **Download backup** from Workshop. The file contains the locked templates and the retired templates, including older locked revisions, and the locked workflows. It leaves drafts out. **Import backup** on a rebuilt system puts them back. A template with the same origin is updated in place. A template with no origin is updated when the locked name matches. An open draft stops the import.

The configuration encryption key is a stored key, and a required password on the form. Leaving it blank uses the stored value. The paste includes `key config-key password-encrypt` and `password encryption aes`, so reversible secrets such as the TACACS key are stored as type 6. The switch keeps that master key outside the configuration.

A template can add up to six paste blocks between the main configuration and the follow-up commands. Each block has a title and its own commands, and the generate page gives it a separate copy box. A template with no paste blocks still has one configuration paste. The downloaded `.cfg` joins the main configuration and the paste blocks in order. Each copy box can have a title and a short note. The note is shown above that paste and left out of the downloaded file.

Each paste, including the main configuration and the follow-up, can be marked Console, SSH, or Pause. The generate page shows that word and changes the color of the paste. The mark is left out of the downloaded file. In the editor, Move up and Move down reorder fields within a section and reorder the sections. The generate form follows that order.

Follow-up commands belong to the template. Edit them with the IOS-XE text. They render with the same fields into their own box, after every paste block. The seeded follow-up is `crypto key generate rsa general-keys modulus 4096` and `write memory`. Those commands stay out of the downloaded file. A note on that box is text the template author writes. A blank note adds nothing.

An install that still has only the original seeded revision picks up this standard the next time the app starts. A template that has already been revised keeps its text and its previous follow-up until you approve a new draft.

Out-of-band TACACS uses a server group in `Mgmt-vrf` (`ip vrf forwarding Mgmt-vrf` and `ip tacacs source-interface GigabitEthernet0/0`). If your IOS-XE release expects `vrf forwarding` under that group, change it in the draft before you lock the template.

## Project Structure

```
switch-day0/
├── app/                    # FastAPI application
│   ├── standards/          # Jinja templates for IOS-XE
│   └── web/                # HTML templates, CSS, and JavaScript
├── data/                   # SQLite database (created at runtime)
├── scripts/                # Installation script
├── systemd/                # systemd service unit file
├── tests/
└── requirements.txt
```

## Local Development

On a workstation, create a virtual environment in the clone. `python -m app` listens on `127.0.0.1:8000`.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
export SWITCH_DAY0_ADMIN_USERNAME=admin
export SWITCH_DAY0_ADMIN_PASSWORD='choose-a-long-password'
python -m app
```

On Windows PowerShell, activate the virtual environment with `.venv\Scripts\Activate.ps1`.

Open http://127.0.0.1:8000 and sign in with that username and password.

Stop the app with Ctrl+C.

If `SWITCH_DAY0_ADMIN_PASSWORD` is missing on a brand-new database, the app writes a one-time password to `data/initial-admin-password.txt` and prints it in the terminal. Set the variable yourself so that file is never needed.

To listen on another port without editing the code:

```bash
python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8001
```

Use the port from that command in the browser address.

## Tests

With the virtual environment active:

```bash
pytest
```

## License

This project is licensed under the MIT License (LICENSE).

Copyright 2026 Ryan M Johns (@GovITInsider)
