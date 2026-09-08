# Browser Control MCP Server — Full Local AI Agent

A Python MCP server that gives Claude (Desktop or Code) full local agent
capabilities: browser control, filesystem access, shell execution, persistent
memory, SQLite storage, email sending, desktop notifications, and a task
scheduler — all running locally, all using your real accounts and data.

---

## What it can do

### 🌐 Browser
| Tool | Purpose |
|---|---|
| `launch_browser` | Start Chrome (real or bundled Chromium) |
| `connect_to_existing_browser` | Attach to a Chrome you already have open, instead of launching a new one |
| `close_browser` | Shut it down (or just disconnect, if attached to an existing Chrome) |
| `navigate` | Go to a URL |
| `go_back` / `go_forward` / `reload_page` | History navigation |
| `get_page_info` | Current URL + title |
| `new_tab` / `list_tabs` / `switch_tab` / `close_tab` | Tab management |
| `get_visible_text` | Extract visible page text |
| `get_page_html` | Raw HTML |
| `screenshot` | Return image of the page |
| `list_interactive_elements` | Numbered list of clickable/typable elements |
| `click_element` / `click_selector` | Click by index or CSS selector |
| `type_text` | Type into an input (uniform speed) |
| `press_key` | Send keyboard key |
| `scroll` | Scroll up/down/top/bottom |
| `wait_for_selector` | Wait for async content |

### 📥 Downloads & PDFs
| Tool | Purpose |
|---|---|
| `download_file` | Download by URL |
| `click_and_download` | Click a button that triggers a download |
| `list_downloads` | List downloaded files |
| `read_pdf_text` | Extract text from a PDF |

### 📁 Filesystem
| Tool | Purpose |
|---|---|
| `read_file` | Read a local file |
| `write_file` | Write or append to a file |
| `list_files` | List files in a directory (with glob filter) |
| `delete_file` | Delete a file or empty directory |
| `move_file` | Move or rename a file |
| `copy_file` | Copy a file |
| `search_files` | Grep-style content search across files |
| `make_directory` | Create directories |

### 💻 Shell
| Tool | Purpose |
|---|---|
| `run_command` | Run any shell command, return output |
| `run_python` | Write and execute Python code inline |

### 🧠 Persistent Memory
| Tool | Purpose |
|---|---|
| `memory_set` | Store a key/value (persists across sessions) |
| `memory_get` | Retrieve a stored value |
| `memory_list` | List all stored keys |
| `memory_delete` | Delete a stored key |

### 🗄️ SQLite Database
| Tool | Purpose |
|---|---|
| `db_query` | Run any SQL query |
| `db_create_table` | Create a table |
| `db_insert` | Insert a row |

### 📧 Email
| Tool | Purpose |
|---|---|
| `send_email` | Send email via SMTP (Gmail, Outlook, etc.) |

### 🔔 Notifications
| Tool | Purpose |
|---|---|
| `notify` | Send a desktop notification (macOS/Linux/Windows) |

### ⏰ Scheduler
| Tool | Purpose |
|---|---|
| `schedule_task` | Run a shell command on an interval or cron schedule |
| `list_scheduled_tasks` | List scheduled tasks and last output |
| `cancel_scheduled_task` | Cancel a scheduled task |

### 🔧 Utilities
| Tool | Purpose |
|---|---|
| `get_system_info` | OS, Python, disk space, paths |
| `clipboard_copy` | Copy text to system clipboard |

---

## Setup

```bash
pip install -r requirements.txt
playwright install chrome
```

For Gmail email sending, enable 2FA on your Google account and create an
[App Password](https://myaccount.google.com/apppasswords). Then store it:

```
# In Claude Desktop, just say:
"Store my email credentials"
# Claude will call:
memory_set('email_user', 'you@gmail.com')
memory_set('email_pass', 'your-16-char-app-password')
```

---

## Launching vs. attaching to your real Chrome

There are two ways to start a session:

- **`launch_browser`** — opens a fresh Chrome window/tab. Simple, but it's
  a blank slate: no logins, no existing tabs, no history.
- **`connect_to_existing_browser`** — attaches to a Chrome you already have
  running, with your real profile, logins, and open tabs. Requires
  starting Chrome with remote debugging enabled first — quit Chrome
  completely, then relaunch it with:

  ```bash
  # macOS
  open -a "Google Chrome" --args --remote-debugging-port=9222
  # Linux
  google-chrome --remote-debugging-port=9222
  # Windows
  chrome.exe --remote-debugging-port=9222
  ```

  Then just say "connect to my existing Chrome." Calling `close_browser`
  afterward disconnects without quitting Chrome — your tabs stay open
  exactly as they were.

---

## Run modes

### stdio (Claude Desktop — recommended)
```bash
python browser_mcp.py
```

### HTTP server (for MCP clients that support it)
```bash
python browser_mcp.py --http 8080
# Accessible at http://127.0.0.1:8080/mcp
# Requires mcp SDK >= 1.6
```

---

## Wire into Claude Desktop

Add to `Settings → Developer → Edit Config`:

```json
{
  "mcpServers": {
    "browser": {
      "command": "python",
      "args": ["/absolute/path/to/browser_mcp.py"]
    }
  }
}
```

Restart Claude Desktop. The tools appear automatically.

---

## Example workflows

### Job application pipeline
```
"Read my resume from ~/resume.pdf, find 5 matching jobs on LinkedIn,
tailor the resume for each, save them to ~/resumes/, and log each
application to the database."
```

### Daily competitive intelligence
```
"Every morning at 9am, check competitor.com/pricing and email me
a summary of any changes to pricing@mycompany.com"
```

### Research pipeline
```
"Search Google Scholar for papers on 'transformer attention mechanisms'
published in 2024, download the top 5 PDFs, extract their text, and
write a summary to ~/research/attention_2024.md"
```

### Tedious form autofill
```
"Read my details from ~/profile.json, go to this insurance quote
form, and fill in every field from it. Stop before submitting so
I can review it."
```

---

## Architecture

```
┌─────────────────────────────────────────────┐
│              Claude (Brain)                 │
└──────┬──────────┬──────────┬────────────────┘
       │          │          │
  ┌────▼───┐ ┌───▼────┐ ┌───▼──────────────┐
  │Browser │ │  File  │ │ Shell / Python   │
  │ Tools  │ │ System │ │ run_command      │
  └────┬───┘ └───┬────┘ └───┬──────────────┘
       │          │          │
  ┌────▼──────────▼──────────▼──────────────┐
  │          Persistent Layer               │
  │  memory.json  |  agent.db  | downloads/ │
  └─────────────────────────────────────────┘
       │                    │
  ┌────▼────┐          ┌────▼───┐
  │  Email  │          │Notify  │
  │  SMTP   │          │Desktop │
  └─────────┘          └────────┘
```

---

## Data locations

All data stored under `~/.browser_mcp/`:

| Path | Contents |
|---|---|
| `~/.browser_mcp/downloads/` | Downloaded files |
| `~/.browser_mcp/memory.json` | Persistent key/value memory |
| `~/.browser_mcp/agent.db` | SQLite database |

---

## Safety notes

- This gives Claude real control over a real browser, including anywhere
  you're logged in. Don't use on sensitive accounts without supervision.
- `run_command` and `run_python` can execute anything the local user
  account can. Nothing in this code currently gates or confirms shell
  commands before running them — that's on the roadmap, not yet built.
  Review commands before approving them, and don't leave the scheduler
  running unattended with `run_command` jobs until that's in place.
- `read_file`/`write_file`/`delete_file` operate on any path the process
  can reach — there's no directory allowlist yet.
- `memory.json` stores credentials (e.g. email app passwords) in plaintext
  on disk. Fine for a local POC; swap for OS keychain storage (e.g. the
  `keyring` package) before using this with real accounts long-term.
- For email: use App Passwords, never your main account password.
- Consider running in a separate Chrome profile for isolation.
