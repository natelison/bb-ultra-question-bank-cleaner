# Blackboard Ultra — Orphaned Question Bank Cleaner

A Playwright-based automation script that bulk-deletes orphaned question banks from a Blackboard Ultra course. Question banks that are linked to active tests are automatically skipped.

## Why This Exists

When courses are copied repeatedly in Blackboard Ultra, orphaned question banks accumulate — banks that aren't linked to any test but can't be bulk-deleted through the UI. Manually deleting hundreds of banks one at a time through the context menu is impractical. This script automates that process.

## How It Works

The script uses Playwright to drive a headless Chromium browser through the Blackboard Ultra question banks UI. For each bank, it opens the context menu, clicks Delete, and confirms the deletion dialog. Banks that contain linked questions will show a "can't delete" modal instead — the script detects this and skips them automatically.

All actions are logged to a CSV file (`bank_deletion_log.csv`) with timestamps, bank names, and outcomes (DELETED, SKIPPED, DRY_RUN, or ERROR).

## Requirements

- Python 3.11+
- Playwright (`pip install playwright`)
- Chromium browser for Playwright (`playwright install chromium`)
- A Blackboard Learn Ultra instance (SaaS, 3900.x+)
- A dedicated service account enrolled in the target course (see [Blackboard Setup](#blackboard-setup))

## Installation

```bash
pip install playwright
playwright install chromium
```

## Configuration

The script supports two authentication methods.

### Option A: Interactive prompts (recommended for shared use)

Just run the script — if no `secrets.toml` file is found, it will prompt for the Blackboard URL, username, and password. The password is entered securely via `getpass` and is not echoed to the terminal.

### Option B: secrets.toml file

Create a `secrets.toml` file in the same directory as the script:

```toml
[blackboard_admin]
base_url = "https://learn.example.edu"
username = "qb_cleaner"
password = "your_password_here"
```

> **Do not commit `secrets.toml` to version control.** Add it to your `.gitignore`.

## Usage

```bash
python delete_orphaned_banks.py
```

The script will prompt for the course PK1 (the internal Blackboard course identifier in `_XXXXXX_1` format). You can find this in the URL when viewing the course in the admin panel, or via the Blackboard REST API.

### Script Options

These are configured as constants at the top of the script:

| Option | Default | Description |
|---|---|---|
| `DRY_RUN` | `False` | Set to `True` to test without deleting. The script will open each bank's context menu, verify the Delete option exists, then close it. |
| `HEADLESS` | `True` | Set to `False` to watch the browser perform each action. Useful for debugging. |
| `DELAY_SECONDS` | `0.5` | Pause between actions. Increase if your instance is slow to respond. |

### Recommended First Run

Set `DRY_RUN = True` and `HEADLESS = False` for your first run so you can watch the browser and verify behavior before any banks are deleted. Review `bank_deletion_log.csv` afterward to confirm the expected banks would be deleted.

## Output

The script writes a CSV log (`bank_deletion_log.csv`) with the following columns:

| Column | Description |
|---|---|
| `timestamp` | When the action occurred |
| `course_pk` | The course PK1 |
| `bank_name` | Name of the question bank |
| `action` | `DELETED`, `SKIPPED`, `DRY_RUN`, or `ERROR` |
| `note` | Additional context (e.g., "Has linked questions — cannot delete") |

A summary is also printed to the terminal at the end of each run.

## Blackboard Setup

This script is designed to run with **minimal permissions** using a dedicated service account and a custom course role. No system admin access is required.

### 1. Create a user account

Create a standard Blackboard user account (e.g., `qb_cleaner`). No system role is needed.

### 2. Create a custom course role

In the admin panel under **Course Roles**, create a new role (e.g., `Question Bank Cleaner`) with the following privileges enabled:

| Privilege | Privilege ID | Why |
|---|---|---|
| Course/Organization > Access unavailable course | `course.unavailable-course.VIEW` | Allows access if the course is set to unavailable |
| Course/Organization Control Panel (Tools) > Tests, Surveys, and Pools > Pools | `course.pool.MODIFY` | Core privilege — view and delete question banks |
| Course/Organization > Messages > View Messages | `course.user.message.VIEW` | Ultra calls the conversations API on course entry; without this, a 403 triggers a fatal error modal |
| Course/Organization Control Panel (Tools) > Tests, Surveys, and Pools > Tests > Delete Test | `course.assessment.DELETE` | Required for the delete action on question banks |
| Course/Organization (Content Areas) > View Material Settings | `course.content.designer.VIEW` | Ultra banks page loads through the content framework |
| Course/Organization Control Panel (Tools) > Tests, Surveys, and Pools > Tests > View Test Design and Settings | `course.assessment.VIEW` | Banks page needs to render linked-question status |

A privilege export is included with this script (`Question Bank Cleaner_privileges.csv`) that can be used as a reference when creating the role. The six privileges above are the minimum required set.

#### Role Capabilities

When creating the course role, set the following:

- **Treat Users with this Role like Instructor (P):** No
- **LTI Spec Role:** - (none)
- **Grant Full Permissions on Course Files:** No
- **Grant Full Permissions on Organization Files:** No
- **Limit Management of Course Enrollments:** Leave empty (no roles in "Manageable by User")

### 3. Workflow

1. Enroll the `qb_cleaner` user into the target course with the `Question Bank Cleaner` role
2. Run the script and enter the course PK1 when prompted
3. Remove the enrollment when finished

The account has no access to grades, student data, content editing, enrollments, or anything else in the course. It can only view and delete question banks.

## Troubleshooting

**"Login failed — check credentials"**
The script could not authenticate. Verify the URL includes `https://` and the credentials are correct. If your institution uses SSO/CAS, this script requires a local Blackboard account that authenticates via the native login page.

**Banks page permission error / "You do not have permission to access this content"**
The custom course role is missing a required privilege. Export the session debug log (click the `?` icon in Ultra → Session Debug) and check which API endpoint is returning a 403. See the privilege table above for the required set.

**Playwright crash on bank names with special characters**
Fixed in the current version. The script escapes apostrophes, quotes, brackets, and other special characters in bank names when building CSS selectors. If you encounter a new edge case, please report it.

**Script processes slowly**
Increase or decrease `DELAY_SECONDS` based on your instance's responsiveness. The default (0.5s) is conservative.

## Limitations

- Operates on **one course at a time** by design. Enroll the service account, run, unenroll, repeat.
- Cannot delete question banks that contain questions linked to active tests. These are skipped automatically.
- Requires the native Blackboard login page. SSO/CAS-only institutions will need a local account or a bypass URL.
- Tested against Blackboard Learn Ultra SaaS 4000.15.x. UI selectors may need updating for future Ultra versions.

## License

Use at your own risk. This script automates destructive actions (deleting question banks). Always run with `DRY_RUN = True` first and review the log before performing actual deletions.
