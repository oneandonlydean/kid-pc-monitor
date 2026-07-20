# Kid PC Monitor

DIY parental control system for tech-savvy parents. If you know what 'pip install' means, this could be for you!

![Python](https://img.shields.io/badge/python-3.7+-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Kids PC](https://img.shields.io/badge/kids_PC-Windows-lightgrey.svg)
![Web panel](https://img.shields.io/badge/web_panel-Windows%20%7C%20Linux%20%7C%20macOS-green.svg)

## 🎯 Features

- **📱 Control from your phone** - Web interface works on any device
- **🔒 Remote lock/unlock detection** - See if kids' PCs are locked
- **⏰ Scheduled bedtime locks** - Automatically lock at set times
- **⏱️ Daily usage limits** - Set maximum screen time
- **💬 Send messages** - Display warnings or reminders
- **🏠 Auto-discovery** - Finds all PCs on your network
- **⏰ Grace period warnings** - 30, 15, 5, and 1-minute warnings before locks
- **💾 Persistent settings** - Limits survive PC restarts
- **👤 User-specific restrictions** - Monitor only specific Windows accounts
- **📊 Real-time status** - See current limits and time remaining
- **⏳ On-screen countdown** - Kids always see the time left today; it turns red in the final minutes (can be hidden per PC)
- **🧍 Break reminders** - Force a stretch break for a few minutes after a set amount of continuous use
- **🙋 Request more time** - Kids ask for more time from their screen; you approve or dismiss from your phone
- **🎁 One-tap bonus time** - Grant +15/+30 minutes straight from the dashboard, no drilling in
- **🎉 Weekday / weekend schedules** - Optionally use a later bedtime and bigger allowance on Saturday and Sunday
- **🧠 Earn time by spelling** - Kids unscramble words to earn extra minutes, up to a daily cap you set
- **📈 Usage history chart** - A per-day usage bar chart for each PC

Note: this is a fork of [cpmurphy's fork](https://github.com/cpmurphy/kid-pc-monitor)
(itself a fork of [rookie7799's original](https://github.com/rookie7799/kid-pc-monitor)),
with the extra features listed above the fold. Huge thanks to both for the
groundwork. As a fork, neither of them is responsible for any problems with this
version.


## ⚠️ Technical Skills Required

This is NOT a one-click solution. You'll need to:
- Install Python
- Use a terminal / command prompt
- Understand IP addresses
- Open firewall ports where needed
- Set up a Windows scheduled task (the installer does this)

If these terms are unfamiliar or you don't want to spend the time, consider commercial alternatives like:
- Qustodio
- Net Nanny
- Windows Family Safety

## Quick Setup

This is the most common scenario.  For others, see below.

You need:

* One or more Windows PCs used by your child
* One parent Windows PC (For Mac/Linux see below)
* Both on the same home network

### Before You Start

On every computer you will need:

1. [Python](https://www.python.org/downloads/)


During installation on Windows:

- Check "Add Python to PATH"
- Choose "Install for all users"

Test it:

```powershell
python --version
```

2.  [Git](https://git-scm.com/install/)


Test it:

```powershell
git --version
```

### On the Kid’s PC

Log in  as an Admin user and open PowerShell.

Run:
```powershell
git clone https://github.com/oneandonlydean/kid-pc-monitor.git
cd kid-pc-monitor
pip install -r requirements.txt
python scripts\install.py
```

The install script walks you through the installation.
For this setup, you want to:

 1. You would like the first option, `Create/Update scheduled task`
 2. Enter the kid's username when asked.
 3. Enter the regular bed time, wake-up time and daily time allowance.
 4. When prompted about public networks, you should allow it unless you
    really are concerned about your home network being insecure.
 

### On the parent PC

 1. Install Python from python.org
 2. Open PowerShell

```powershell
git clone https://github.com/oneandonlydean/kid-pc-monitor.git
cd kid-pc-monitor
pip install -r requirements.txt
pip install -e .
scripts\install_web_panel.py
```

Then

Open this on your phone or laptop:

http://`<IP address of parent's PC>`:5000

**iPhone password autofill:** Safari only auto-fills saved passwords over trusted HTTPS. For one-tap login from your phone, see [Safari autofill in the FAQ](docs/FAQ.md#safari-on-iphone-wont-autofill-my-saved-password) and run `./scripts/setup_web_panel_https.sh` on the parent machine.

#### How to Find Your PC's IP Address

Open Powershell and type:

```powershell
ipconfig
```

You'll want the line that says `IPv4 Address`,

```powershell
IPv4 Address . . . . . . . . . . : 192.168.x.x
```

## To Uninstall

### On the Kid’s PC

```powershell
cd kid-pc-monitor
python scripts\install.py
```

Choose option 2, `Remove scheduled task`

### On the Parent’s PC

```powershell
cd kid-pc-monitor
python scripts\install_web_panel.py
```

Choose option 2, `Remove scheduled task`

## 📸 Screenshots

![Main control panel](screenshots/main-control-panel.png)
![View of an individual PC](screenshots/individual-pc.png)
![Daily settings](screenshots/daily-settings.png)

## Prerequisites

To use this tool effectively, you'll want to have a separate parent/admin
machine to run the web interface.

- **Kid's PCs:** Windows 10/11 (the monitoring agent uses Windows APIs)
- **Parent / admin machine:** Windows, Linux, or macOS with Python 3.7+ (runs the Flask web panel only) The web panel listens on TCP **5000** for your browser or phone.

## Network Considerations

Current agents make outbound connections to the parent web panel: HTTP discovery
on TCP **5000**, then a native protocol TCP session on **9998** (configurable).
The kid PCs no longer need inbound TCP **9999** for normal web-panel control,
which avoids the common Windows Firewall problem where the agent is running but
blocked from the parent side.

The parent computer and kid computers usually need to be:

* on the same home Wi-Fi network
* able to reach the parent web panel at `http://<parent-pc-ip>:5000`
* able to reach the reverse control port at `tcp://<parent-pc-ip>:9998`

The web panel host must allow inbound TCP **5000** (browsers + discovery) and
**9998** (agent reverse control) from the LAN. On Linux, for example:

```bash
sudo ufw allow 5000/tcp
sudo ufw allow 9998/tcp
```

<details>
<summary>Discovery and legacy scanning (Technical Details)</summary>
Agents first try `KID_PC_MONITOR_PANEL_URL` if set, then their cached panel URL,
then scan their local `/24` for the web panel discovery endpoint on TCP 5000.
They open reverse control on the `reverse_port` advertised by discovery
(default 9998). The old parent-side scan still exists for legacy direct mode and
`kid-pc-cli` IP workflows; it scans the parent machine's local `/24` for agents
listening on TCP 9999 and is capped at `/24` (256 addresses).
</details>

## Installation

Installation is in two parts.  You install the agent on the kid's PC (as
many PCs and as many accounts as needed) and you install the admin UI on
a computer you control.

## Option A -- Separate Kid and Parent PCs

### Kid's PC

Only Windows is supported currently.

```powershell
git clone https://github.com/oneandonlydean/kid-pc-monitor.git
cd kid-pc-monitor
pip install -r requirements.txt

<# Run installer as administrator #>
python scripts\install.py
```

Run the installer from an administrator account and, when prompted,
enter the **child's Windows username**. The agent then launches in that
child's session at their logon. Specifics:
- Files install to `C:\ProgramData\KidPCMonitor` and the child account is granted read+execute.
- Python must be installed **for all users** (not the per-user `%LOCALAPPDATA%\Programs\Python\…` install) so the child's task can launch `pythonw.exe`. The installer refuses with a clear message if only a per-user Python is found.
- The scheduled task runs at the child's logon only, with `LeastPrivilege` (no UAC prompt for the kid).
- The agent writes its log and state to `%LOCALAPPDATA%\KidPCMonitor` in the child's profile.

You may also enter your **own** username to monitor the account you are
signed in as. This is the weakest setup — the agent runs unelevated, so
that account can stop the task and undo locks — so the installer warns
and asks you to confirm. In this self-install case a per-user Python is
accepted (the task runs in your own session and can reach it).

#### Windows agent firewall

The default agent path is outbound reverse TCP to the web panel, so the installer
no longer needs to add an inbound Windows Firewall rule for normal use. During
install it asks whether to enable **legacy direct control** on TCP **9999**.
Choose **yes** only if you still use `kid-pc-cli` direct IP commands, an older
web panel, or troubleshooting tools that connect directly to the agent.

If you enable legacy direct control, the installer creates the same scoped
Windows Firewall inbound rule for TCP **9999**. It still defaults to
**Private** and **Domain** profiles, with a separate prompt for **Public**.

### Parent's PC

Run the web panel on a separate PC (your own computer). More secure since kids can't access the admin interface.

#### On your Windows PC

(For MacOS/Linux, see below.)

```powershell
git clone https://github.com/oneandonlydean/kid-pc-monitor.git
cd kid-pc-monitor
pip install -r requirements.txt
pip install -e .

<# Run the web panel (any of these work): #>
kid-pc-web-panel
<# or: python -m kid_pc_monitor.web_panel #>
<# or: python scripts\run_web_panel.py #>

<# Open in browser: http://YOUR-PC-IP:5000 #>
```

#### MacOS/Linux

```bash
cd kid-pc-monitor
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install -e .
kid-pc-web-panel
```

After that, use `./venv/bin/kid-pc-web-panel`, `./venv/bin/kid-pc-cli`,
or (the launch scripts switch to `venv/` automatically when it exists).

#### Install as a Service (Linux Only)

Assuming you have a system that runs systemd, you can run the web
panel as a background service.

After creating the venv and running
`./venv/bin/python3 -m pip install -r requirements.txt`:

```bash
./scripts/install_web_panel_linux.sh install   # writes ~/.config/systemd/user/kid-pc-monitor-web-panel.service
./scripts/install_web_panel_linux.sh status
# ./scripts/install_web_panel_linux.sh uninstall   # when you want it gone
```

## Option B: Single PC Setup

Run everything on the kid's PC and access the admin panel from your
phone. Convenient if you don't have a separate PC always running.

1. **On the kid's PC (as administrator):**
```powershell
git clone https://github.com/oneandonlydean/kid-pc-monitor.git
cd kid-pc-monitor
pip install -r requirements.txt

<# Install both services #>
python scripts/install.py           <# Installs pc_control #>
python scripts/install_web_panel.py <# Installs web panel #>
```

2. **On your phone:**
   - Open browser and go to `http://KIDS-PC-IP:5000`
   - Bookmark it for easy access
   - For iOS Safari password autofill hints, see [FAQ](docs/FAQ.md#safari-on-iphone-wont-autofill-my-saved-password)

Both services run invisibly in the background using `pythonw.exe`.

**Note:** With this setup, a tech-savvy child could potentially discover the web panel at `localhost:5000`. Option A is more secure.

---

*Side note: if your kid is "good" with computers, consider copying the scripts somewhere less obvious.*

## 📖 Usage Guide

### Ad-hoc Changes
1. Open the web interface on your phone
2. Click on a PC
3. View current settings in the "📊 Current Settings" section

#### Granting More Time
1. Use quick buttons to extend the time allowance for today: "30 min", "1 hour", "2 hours"
2. Or enter number of minutes for a custom time extension
3. Page refreshes to show the new time

#### Manual Lock
1. Use the "Lock Computer Now" button to do that.
2. A more extreme option, "Shutdown Computer" is also available.
3. Use the "Clear" button next to Manual lock to unlock.

### Changing Daily Settings
1. Select a PC
2. Follow the link "Daily settings"
2. Edit or remove daily allowance, bedtime and wake-up time.
4. PC will lock automatically at bedtime and stay locked until the wake-up time.

Note: when a usage limit, bedtime, or manual lock is active, the agent
re-issues the lock whenever it detects the screen has been unlocked, so
the child can't bypass it by typing their Windows password. The **Lock
Computer Now** button enables a manual lock that remains active until
you clear all limits.

### On-screen timer (this fork)

By default the kid's PC shows a small always-on-top countdown of the time left
today. It turns **red in the final few minutes** before a lock. On a PC's
control page, use **⏳ On-Screen Timer → Show/Hide** to turn it off or on per
PC.

### Break reminders (this fork)

On a PC's control page, the **🧍 Break reminders** section lets you lock the
screen for a short stretch break after a set amount of *active* use — for
example, every **45** minutes of use, lock for **5** minutes. The break shows a
"stand up and stretch" message, does **not** count against the daily allowance,
and releases automatically. Set the interval to **0** to turn breaks off.

### Kids asking for more time (this fork)

When the on-screen timer is visible, the kid can click **Ask for more time**.
Their request appears as a banner on that PC's control page, where you can
**grant an extension to approve** it or **dismiss** it. Granting any extension
clears the request.

### One-tap bonus time (this fork)

Each reachable PC on the dashboard has **🎁 Bonus time: +15 / +30** buttons that
grant an extension immediately — handy for rewarding chores without opening the
PC's page.

### Weekday / weekend schedules (this fork)

On a PC's **Daily settings** page, the **🎉 Weekend schedule** section lets you
turn on a separate **bedtime and allowance for Saturday and Sunday**. When it's
on, those values apply on Sat/Sun and the weekday settings apply the rest of the
week. (Weekend is Saturday and Sunday based on the wake-to-wake day.)

### Earn time by spelling (this fork)

On a PC's **Daily settings** page, the **🧠 Earn time** section lets your child
earn extra minutes by unscrambling words. You configure whether it's on, the
**minutes earned per correct answer**, the **number of questions** per quiz, and
a **daily cap** on how much can be earned. The child taps **Earn time
(spelling)** on the on-screen timer to play; correct answers grant time up to the
cap.

### Usage history chart (this fork)

The usage-history page (linked from a PC's page) shows a **per-day bar chart** of
screen time for the last week, with the daily allowance marked and over-limit
days highlighted, above the existing table.

## ⚙️ Configuration

### Custom PC Names
Edit `CUSTOM_PC_NAMES` in `src/kid_pc_monitor/remote_client.py` (used by the web panel and `kid-pc-cli`):
```python
CUSTOM_PC_NAMES = {
    '192.168.1.105': 'Tommy\'s Laptop',
    '192.168.1.112': 'Sarah\'s Desktop',
}
```

## 🖥️ Command-line client (Advanced, Experimental)

From the repo root, run the CLI on your parent machine (Linux, macOS,
or Windows). On Linux, create a venv first if you have not already (see
[Python virtual environment (Linux)](#python-virtual-environment-linux)):

```bash
cd kid-pc-monitor
python3 -m venv venv
./venv/bin/python3 -m pip install -e .
./venv/bin/kid-pc-cli scan
./venv/bin/kid-pc-cli inspect 192.168.1.105
./venv/bin/kid-pc-cli set-limit 192.168.1.105 60
./venv/bin/kid-pc-cli add-lock-time 192.168.1.105 21:00
./venv/bin/kid-pc-cli lock 192.168.1.105
```

Use `./venv/bin/kid-pc-cli --help` for all commands (`message`, `shutdown`, `extend-time`, `clear-all`, etc.). Add `--json` for scripting. These commands use the legacy direct TCP path and require the agent's inbound TCP **9999** compatibility option. Scan a specific subnet with `./venv/bin/kid-pc-cli scan --subnet 192.168.1.0/24`.


## 🔧 Troubleshooting

See [docs/FAQ.md](docs/FAQ.md) for questions and answers.

### "PC shows as Unknown"
- Add custom names in configuration
- Check Windows Firewall settings
- Ensure PCs are on same network

### Agent does not appear in the web panel
- Ensure the web panel is running (`kid-pc-web-panel` or `python -m kid_pc_monitor.web_panel`).
- Ensure the parent/web-panel host firewall allows inbound **5000/tcp** (UI + discovery) and **9998/tcp** (agent reverse control) from kid PCs.
- On the kid PC, read `%LOCALAPPDATA%\KidPCMonitor\pc_control.log` for reverse TCP and discovery messages.
- If auto-discovery fails, set `KID_PC_MONITOR_PANEL_URL=http://<parent-pc-ip>:5000` for the scheduled agent task.

### Legacy scan finds no PCs
- Parent-side scan and `kid-pc-cli` direct IP commands require the optional legacy inbound TCP **9999** agent firewall rule.
- If you enabled that compatibility option, check the kid PC network profile and firewall rule as before.

### "Can't connect from phone"
- Check the web panel host firewall allows inbound **5000/tcp** and **9998/tcp**.
- Use the web panel machine's IP address, not localhost.
- Ensure the web panel is running (`kid-pc-web-panel` or `python -m kid_pc_monitor.web_panel`).

### "Lock status not updating"
- Restart `pc_control.py`
- Check if LogonUI.exe detection works
- See logs in console window

## 🛡️ Security Notes

- Only works on local network (not internet)
- Optional **parent web panel** password: use **Add password protection** on the home page. Only a secure hash is stored in `web_panel_auth.json` next to the app (not the plain password). Until you set one, anyone on the LAN can use the panel—the same as before this feature.
- Can't bypass Windows lock screen
- Kids can close the agent if their account has admin rights. If you install in cross-user mode (admin installs, non-admin child runs), the child cannot stop or delete the scheduled task or its files.

## 🤝 Contributing

Parents and developers welcome! Please:
1. Fork the repository
2. Create a feature branch
3. Submit a pull request

### Recent Improvements
- ✅ **On-screen countdown timer** — Always-visible time-left overlay that turns red in the final minutes; hideable per PC
- ✅ **Break reminders** — Configurable forced stretch breaks after a set amount of continuous use
- ✅ **Request more time** — Kids ask from their screen; parents approve (grant extension) or dismiss from the panel
- ✅ **One-tap bonus time** — +15/+30 minute buttons on the dashboard PC cards
- ✅ **Weekday / weekend schedules** — Optional separate bedtime and allowance for Saturday and Sunday
- ✅ **Earn time by spelling** — Kids unscramble words to earn extra minutes, up to a parent-set daily cap
- ✅ **Usage history chart** — Per-day usage bar chart on the usage-history page
- ✅ **Mutual authentication** — HMAC-signed agent protocol (v3); shared secret stored encrypted at rest
- ✅ **Web panel security** — Optional password login; CSRF protection on state-changing actions
- ✅ **Linux/macOS parent support** — Run the web panel on non-Windows machines; systemd user-service installer
- ✅ **Cross-user install** — Admin installs the agent; the child's non-admin account runs it (harder to bypass)
- ✅ **Wake-up time** — Bedtime curfew ends at wake-up, not midnight; configurable in the web panel and at install
- ✅ **Smarter time tracking** — Only active use counts toward the daily allowance; locked sessions stop the clock
- ✅ **Lock hardening** — Re-locks automatically if the kid unlocks while a limit is active; persistent manual locks
- ✅ **Grace period warnings** — 30, 15, 5, and 1-minute warnings before lock; warnings reset after a time extension
- ✅ **Reverse agent TCP** — Agents call home with the native protocol, avoiding inbound kid-PC firewall issues
- ✅ **Live PC status** — Background status refresh with SQLite snapshot history; last-known state when a PC goes offline
- ✅ **IP address changes** — Control pages keyed by hostname so DHCP changes do not break bookmarks
- ✅ **Agent log viewer** — View agent logs remotely from the web panel or `kid-pc-cli logs`
- ✅ **CLI tool** — `kid-pc-cli` for scan, inspect, lock, extend, and other remote commands
- ✅ **Easier reinstall** — Installer reuses previous settings; legacy inbound firewall setup is optional

### Ideas for Future Contributions
- Linux/macOS **agent** (kid-side monitoring; the web panel already runs on Linux/macOS/Windows)
- Mobile app
- Usage statistics/reports
- Reward system integration
- Application-specific time limits

## 📄 License

MIT License - feel free to modify for your family's needs!

## ❤️ Acknowledgments

Created by parents, for parents. Special thanks to all contributors who help make screen time management easier!

---

**Need Help?** Open an [issue](https://github.com/oneandonlydean/kid-pc-monitor/issues) or check our [FAQ](docs/FAQ.md)
