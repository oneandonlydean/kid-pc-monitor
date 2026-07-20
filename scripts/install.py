import json
import subprocess
import os
import sys
import glob
import shutil
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from kid_pc_monitor.agent_state import (
    find_complete_daily_settings,
    program_data_daily_path,
    program_data_target_user,
)
from kid_pc_monitor.lock_policy import parse_time_hhmm
from kid_pc_monitor.shared_secret import prompt_and_store_shared_secret

TASK_NAME = "KidPCMonitor"
INSTALL_DIR_DEFAULT = r"C:\ProgramData\KidPCMonitor"
INSTALL_CONFIG_FILE = "daily_settings.json"
AGENT_PORT = 9999
FIREWALL_RULE_DISPLAY_NAME = "Kid PC Monitor Agent (TCP 9999)"
FIREWALL_RULE_GROUP = "Kid PC Monitor"


def find_repo_package_dir():
    """Locate the kid_pc_monitor package shipped with this repo."""
    here = Path(os.path.dirname(os.path.abspath(__file__)))
    candidate = here.parent / "src" / "kid_pc_monitor"
    if candidate.is_dir():
        return candidate
    return None


AGENT_LAUNCHER = '''\
"""Kid PC Monitor agent launcher (installed copy).

The kid_pc_monitor package is installed alongside this file so the scheduled
task can run the agent without a separate pip install step.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kid_pc_monitor.pc_control import main

if __name__ == "__main__":
    raise SystemExit(main())
'''


def _format_time_hhmm(raw: str) -> str:
    parsed = parse_time_hhmm(raw)
    return f"{parsed.hour:02d}:{parsed.minute:02d}"


def _format_daily_time(value) -> str:
    return f"{value.hour:02d}:{value.minute:02d}"


def prompt_wake_up_time():
    """Ask for the daily morning unlock time (local HH:MM)."""
    print("\n⏰ Wake-up time")
    print("   Bedtime locks stay active until this time each morning.")
    print("   Daily screen-time limits also reset at wake-up (not at midnight).")
    while True:
        raw = input("\nWake-up time (HH:MM, default 07:00): ").strip() or "07:00"
        try:
            return _format_time_hhmm(raw)
        except ValueError as exc:
            print(f"❌ {exc}")


def prompt_bed_time():
    """Ask for the nightly lock time (local HH:MM)."""
    print("\n🕐 Bedtime")
    print("   The PC locks at this time and stays locked until wake-up.")
    while True:
        raw = input("\nBedtime (HH:MM, default 21:00): ").strip() or "21:00"
        try:
            return _format_time_hhmm(raw)
        except ValueError as exc:
            print(f"❌ {exc}")


def prompt_daily_allowance():
    """Ask for the daily screen-time cap in minutes (optional)."""
    print("\n⏱️ Daily allowance")
    print("   Maximum screen time per day in minutes.")
    print("   Leave blank for no daily cap (bedtime lock still applies).")
    while True:
        raw = input("\nDaily allowance in minutes (blank for no limit): ").strip()
        if not raw:
            return None
        try:
            minutes = int(raw)
            if minutes <= 0:
                raise ValueError("Enter a positive number of minutes.")
            return minutes
        except ValueError as exc:
            print(f"❌ {exc}")


def agent_state_dir_for_user(username: str, *, same_user: bool) -> Path:
    """Return %LOCALAPPDATA%\\KidPCMonitor for the account that will run the agent."""
    if same_user:
        local_app = os.environ.get("LOCALAPPDATA")
        if not local_app:
            raise OSError("LOCALAPPDATA is not set")
        return Path(local_app) / "KidPCMonitor"
    resolved = resolve_user_localappdata_dir(username)
    if resolved is not None:
        return resolved
    return (
        Path(os.environ.get("SystemDrive", "C:"))
        / "Users"
        / username
        / "AppData"
        / "Local"
        / "KidPCMonitor"
    )


def resolve_user_localappdata_dir(username: str) -> Path | None:
    """
    Resolve ...\\AppData\\Local\\KidPCMonitor from the user's registry profile path.

    Returns None if the account has no profile yet (kid must sign in once).
    """
    if sys.platform != "win32":
        return None

    safe_user = username.replace("'", "''")
    ps = f"""
    $ErrorActionPreference = 'Stop'
    $name = '{safe_user}'
    $acct = New-Object System.Security.Principal.NTAccount($name)
    $sid = $acct.Translate([System.Security.Principal.SecurityIdentifier]).Value
    $key = "HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\ProfileList\\$sid"
    if (-not (Test-Path -LiteralPath $key)) {{
        Write-Output 'NO_PROFILE'
        exit 2
    }}
    $profile = (Get-ItemProperty -LiteralPath $key).ProfileImagePath
    if (-not $profile) {{
        Write-Output 'NO_PATH'
        exit 3
    }}
    Join-Path $profile 'AppData\\Local\\KidPCMonitor'
    """
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.SubprocessError:
        return None

    out = (result.stdout or "").strip()
    if result.returncode != 0 or out in ("NO_PROFILE", "NO_PATH", ""):
        return None
    return Path(out)


def _merge_agent_daily_settings_into_file(
    daily_path: Path,
    *,
    wake_time: str,
    bed_time: str,
    allowance: int | None,
) -> None:
    daily_settings: dict = {}
    if daily_path.is_file():
        try:
            daily_settings = json.loads(daily_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            daily_settings = {}
    daily_settings["wake_time"] = wake_time
    daily_settings["bed_time"] = bed_time
    daily_settings["allowance"] = allowance
    daily_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = daily_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(daily_settings, indent=2), encoding="utf-8")
    os.replace(tmp, daily_path)


def _grant_user_modify_on_dir(username: str, directory: Path) -> None:
    if sys.platform != "win32":
        return
    subprocess.run(
        ["icacls", str(directory), "/grant", f"{username}:(OI)(CI)M", "/T", "/Q"],
        capture_output=True,
        text=True,
    )


def write_agent_daily_settings_via_powershell(
    username: str,
    *,
    wake_time: str,
    bed_time: str,
    allowance: int | None,
) -> Path | None:
    """Write agent daily settings into the child's profile via an elevated PowerShell helper."""
    safe_user = username.replace("'", "''")
    safe_wake = wake_time.replace("'", "''")
    safe_bed = bed_time.replace("'", "''")
    if allowance is None:
        allowance = "$null"
    else:
        allowance_ps = str(int(allowance))
    ps = f"""
    $ErrorActionPreference = 'Stop'
    $name = '{safe_user}'
    $wake = '{safe_wake}'
    $bed = '{safe_bed}'
    $dailyLimit = {allowance_ps}
    $acct = New-Object System.Security.Principal.NTAccount($name)
    $sid = $acct.Translate([System.Security.Principal.SecurityIdentifier]).Value
    $key = "HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\ProfileList\\$sid"
    if (-not (Test-Path -LiteralPath $key)) {{
        Write-Error "No Windows profile for $name. Ask them to sign in once, then re-run install."
    }}
    $profile = (Get-ItemProperty -LiteralPath $key).ProfileImagePath
    $dir = Join-Path $profile 'AppData\\Local\\KidPCMonitor'
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $dailyPath = Join-Path $dir 'daily_settings.json'
    $daily = @{{}}
    if (Test-Path -LiteralPath $dailyPath) {{
        $raw = Get-Content -LiteralPath $dailyPath -Raw -Encoding UTF8
        if ($raw) {{
            $daily = $raw | ConvertFrom-Json
            if ($daily -is [System.Array]) {{ $daily = @{{}} }}
        }}
    }}
    $daily | Add-Member -NotePropertyName wake_time -NotePropertyValue $wake -Force
    $daily | Add-Member -NotePropertyName bed_time -NotePropertyValue $bed -Force
    $daily | Add-Member -NotePropertyName allowance -NotePropertyValue $dailyLimit -Force
    $json = $daily | ConvertTo-Json -Depth 5
    Set-Content -LiteralPath $dailyPath -Value $json -Encoding UTF8
    Write-Output $dailyPath
    """
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.SubprocessError:
        return None

    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        if err:
            print(f"\n⚠️  Could not write agent daily settings to the child's profile: {err}")
        return None

    out = (result.stdout or "").strip().splitlines()
    if not out:
        return None
    return Path(out[-1])


def write_program_data_daily_settings(
    target_user: str,
    *,
    wake_time: str,
    bed_time: str,
    allowance: int | None,
) -> Path:
    """Write machine-wide install daily settings (always writable during admin install)."""
    dest = Path(INSTALL_DIR_DEFAULT)
    dest.mkdir(parents=True, exist_ok=True)
    config_path = dest / INSTALL_CONFIG_FILE
    payload = {
        "target_user": target_user,
        "wake_time": wake_time,
        "bed_time": bed_time,
        "allowance": allowance,
    }
    tmp = config_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, config_path)
    return config_path


def write_agent_daily_settings_to_state(
    username: str,
    *,
    wake_time: str,
    bed_time: str,
    allowance: int | None,
    same_user: bool,
) -> tuple[Path | None, bool]:
    """
    Persist schedule daily settings into the child's daily_settings.json (merge if present).

    Returns (daily_path, success). Also writes ProgramData daily_settings.json.
    """
    config_path = write_program_data_daily_settings(
        username,
        wake_time=wake_time,
        bed_time=bed_time,
        allowance=allowance,
    )
    print(f"   Install daily_settings: {config_path}")

    state_dir = agent_state_dir_for_user(username, same_user=same_user)
    daily_path = state_dir / "daily_settings.json"

    try:
        _merge_agent_daily_settings_into_file(
            daily_path, wake_time=wake_time, bed_time=bed_time, allowance=allowance
        )
        _grant_user_modify_on_dir(username, state_dir)
        return daily_path, True
    except OSError as exc:
        print(f"\n⚠️  Direct write to {daily_path} failed: {exc}")

    if not same_user:
        ps_path = write_agent_daily_settings_via_powershell(
            username,
            wake_time=wake_time,
            bed_time=bed_time,
            allowance=allowance,
        )
        if ps_path is not None:
            _grant_user_modify_on_dir(username, ps_path.parent)
            return ps_path, True

    print(
        "\n⚠️  Schedule daily settings were saved to ProgramData only. The agent will apply them "
        "on the child's next logon. If the child has never signed in, have them log "
        "in once and re-run install, or edit daily_settings.json in their profile."
    )
    return None, False


def warn_self_install(current_user):
    """Explain the trade-offs of monitoring the same account that runs the installer.

    Returns True if the user wants to proceed anyway.
    """
    print(f"\n⚠️  You entered the current account ('{current_user}').")
    print("   Monitoring the same account that runs the installer is allowed,")
    print("   but it is the weakest setup:")
    print("   • The agent runs unelevated (LeastPrivilege), so this account can")
    print("     stop or delete the scheduled task and the install files.")
    print("   • A remote lock can be undone by this same account.")
    print("   • For real enforcement, monitor a separate standard (non-admin)")
    print("     account and run this installer from an administrator account.")
    choice = input("\nProceed monitoring your own account anyway? (y/N): ").strip().lower()
    return choice in ("y", "yes")


def prompt_target_user(current_user, *, default_user: str | None = None):
    """Ask for the monitored Windows username and validate it exists locally.

    Entering the current account is permitted after a warning + confirmation.
    When *default_user* is set (from a previous install), Enter accepts it.
    """
    while True:
        if default_user:
            raw = input(
                f"\nWhich Windows account should be monitored "
                f"(the child's username) [{default_user}]: "
            ).strip()
            name = raw or default_user
        else:
            name = input(
                "\nWhich Windows account should be monitored (the child's username): "
            ).strip()
        if not name:
            print("❌ Username cannot be empty.")
            continue
        if not validate_user_exists(name):
            print(f"❌ Could not find a local Windows account named '{name}'.")
            retry = input("Try a different name? (y/n): ").strip().lower()
            if retry != "y":
                return None
            default_user = None
            continue
        if current_user and name.lower() == current_user.lower():
            if not warn_self_install(current_user):
                continue
        return name


def validate_user_exists(name):
    """Return True if a local Windows account with this name exists."""
    ps = (
        f"if (Get-LocalUser -Name '{name}' -ErrorAction SilentlyContinue) "
        f"{{ Write-Host 'OK' }} else {{ Write-Host 'MISSING' }}"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True,
            text=True,
        )
        return "OK" in result.stdout
    except Exception:
        return False


def find_system_python():
    """
    Locate a system-wide pythonw.exe that a non-admin user can execute.
    Returns a path string or None if only per-user installations are found.
    """
    candidates = []

    # 1. PATH lookup
    try:
        out = subprocess.run(["where", "pythonw.exe"], capture_output=True, text=True)
        if out.returncode == 0:
            for line in out.stdout.splitlines():
                line = line.strip()
                if line:
                    candidates.append(line)
    except Exception:
        pass

    # 2. Conventional install dirs
    for pattern in [
        r"C:\Program Files\Python*\pythonw.exe",
        r"C:\Program Files (x86)\Python*\pythonw.exe",
        r"C:\Python*\pythonw.exe",
    ]:
        candidates.extend(glob.glob(pattern))

    # 3. Drop per-user installs — the child's task can't reach the admin's profile
    seen = set()
    for path in candidates:
        lower = path.lower()
        if r"\appdata\local" in lower:
            continue
        if lower in seen:
            continue
        if not os.path.exists(path):
            continue
        seen.add(lower)
        return path
    return None


def install_to_programdata(target_user, package_dir):
    """Copy the agent package into C:\\ProgramData\\KidPCMonitor and grant the child read+execute."""
    dest = Path(INSTALL_DIR_DEFAULT)
    dest.mkdir(parents=True, exist_ok=True)

    package_src = Path(package_dir)
    package_dest = dest / "kid_pc_monitor"
    if package_dest.exists():
        shutil.rmtree(package_dest)
    shutil.copytree(package_src, package_dest)

    launcher_path = dest / "pc_control.py"
    launcher_path.write_text(AGENT_LAUNCHER, encoding="utf-8")

    # Optional: copy requirements.txt for reference
    repo_root = package_src.parent.parent
    req = repo_root / "requirements.txt"
    if req.exists():
        try:
            shutil.copy2(req, dest / "requirements.txt")
        except Exception:
            pass

    # Grant read+execute to the child, recursively
    acl_result = subprocess.run(
        ["icacls", str(dest), "/grant", f"{target_user}:(OI)(CI)RX", "/T", "/Q"],
        capture_output=True,
        text=True,
    )
    if acl_result.returncode != 0:
        print(f"\n⚠️  icacls warning while granting access to {target_user}:")
        print(acl_result.stdout)
        print(acl_result.stderr)

    return str(launcher_path)


def create_task_with_power_settings(target_user, script_path, python_path, *, is_self=False):
    """Create the scheduled task via PowerShell New-ScheduledTask cmdlets."""

    task_name = TASK_NAME

    print(f"\n📋 Task Configuration:")
    print(f"   Script:     {script_path}")
    print(f"   Python:     {python_path}")
    print(f"   Task Name:  {task_name}")
    print(f"   Runs as:    {target_user}{' (self-install)' if is_self else ''}")

    confirm = input("\nProceed with these settings? (y/n): ").lower()
    if confirm != "y":
        print("❌ Setup cancelled.")
        return False

    # Logon trigger pinned to the monitored account; runs unelevated in their session.
    domain = os.environ.get("COMPUTERNAME", ".")
    triggers_block = (
        f"$triggers = @( New-ScheduledTaskTrigger -AtLogon -User '{domain}\\{target_user}' )"
    )
    principal_block = (
        f"$principal = New-ScheduledTaskPrincipal "
        f"-UserId '{domain}\\{target_user}' -LogonType Interactive -RunLevel Limited"
    )

    ps_script = f'''
    $ErrorActionPreference = 'Stop'
    try {{
        $action = New-ScheduledTaskAction -Execute "{python_path}" -Argument "{script_path}" -WorkingDirectory "{os.path.dirname(script_path)}"

        {triggers_block}

        {principal_block}

        $settings = New-ScheduledTaskSettingsSet `
            -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries `
            -StartWhenAvailable `
            -DontStopOnIdleEnd `
            -RestartCount 3 `
            -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit (New-TimeSpan -Hours 0)

        Register-ScheduledTask `
            -TaskName "{task_name}" `
            -Action $action `
            -Trigger $triggers `
            -Principal $principal `
            -Settings $settings `
            -Force

        $task = Get-ScheduledTask -TaskName "{task_name}" -ErrorAction Stop
        Write-Host "SUCCESS: Task verified in Task Scheduler"
        Write-Host "Task Path: $($task.TaskPath)"
        Write-Host "Triggers: $($task.Triggers)"
        Write-Host "Principal: $($task.Principal)"
        exit 0
    }}
    catch {{
        Write-Host "ERROR: $_"
        Write-Host "Detailed error: $($_.Exception.Message)"
        exit 1
    }}
    '''

    try:
        result = subprocess.run(
            ["powershell", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
            capture_output=True,
            text=True,
        )

        print("\n=== PowerShell Output ===")
        print(result.stdout)
        if result.stderr:
            print("=== Errors ===")
            print(result.stderr)

        if result.returncode == 0:
            verify_cmd = f'schtasks /query /tn "{task_name}"'
            verify_result = subprocess.run(verify_cmd, shell=True, capture_output=True, text=True)

            if verify_result.returncode == 0:
                print("\n✅ Task successfully created and verified!")
                print(f"   - Triggers: At logon of {target_user}")
                print(f"   - Running as: {target_user}")
                print("\nYou can verify in Task Scheduler (taskschd.msc)")
                return True
            else:
                print("\n❌ Task creation failed verification")
                print("Try running this script as Administrator again")
                return False
        else:
            print("\n❌ Error creating task")
            if "Access is denied" in result.stderr:
                print("Please ensure you're running as Administrator")
            return False

    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        return False


def create_task_simple_schtasks(target_user, script_path, python_path):
    """XML fallback for environments where the PowerShell cmdlets misbehave."""
    task_name = TASK_NAME

    print(f"\n📋 Creating task with XML method...")

    domain = os.environ.get("COMPUTERNAME", ".")
    user_id = f"{domain}\\{target_user}"
    trigger_block = f"""    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user_id}</UserId>
    </LogonTrigger>"""
    principal_block = f"""    <Principal id="Author">
      <UserId>{user_id}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>"""

    xml_content = f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Kid PC Monitor - Manages computer usage time</Description>
  </RegistrationInfo>
  <Triggers>
{trigger_block}
  </Triggers>
  <Principals>
{principal_block}
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{python_path}</Command>
      <Arguments>"{script_path}"</Arguments>
      <WorkingDirectory>{os.path.dirname(script_path)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>'''

    try:
        with open("task_config.xml", "w", encoding="utf-16") as f:
            f.write(xml_content)

        result = subprocess.run(
            f'schtasks /create /tn "{task_name}" /xml "task_config.xml" /f',
            shell=True,
            capture_output=True,
            text=True,
        )

        os.remove("task_config.xml")

        if result.returncode == 0:
            print("\n✅ Task created successfully with battery settings!")
            verify_task_settings(task_name)
            return True
        else:
            print(f"\n❌ Error: {result.stderr}")
            return False

    except Exception as e:
        print(f"\n❌ Error: {e}")
        return False


def verify_task_settings(task_name):
    """Verify the power settings of a task"""

    query_cmd = f'schtasks /query /tn "{task_name}" /xml'
    result = subprocess.run(query_cmd, shell=True, capture_output=True, text=True)

    if result.returncode == 0:
        xml = result.stdout
        battery_start = "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in xml
        battery_stop = "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>" in xml

        print("\n📋 Task Power Settings:")
        print(f"   ✅ Can start on battery: {battery_start}")
        print(f"   ✅ Won't stop on battery: {battery_stop}")


def check_admin():
    """Check if running as administrator"""
    try:
        import ctypes

        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False


def _escape_ps_single_quoted(value):
    """Escape a string for use inside a PowerShell single-quoted literal."""
    return value.replace("'", "''")


def _normalize_program_path(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _parse_firewall_rule_query_output(stdout: str) -> dict | None:
    """Parse PowerShell output from find_existing_agent_firewall_rule."""
    text = (stdout or "").strip()
    if not text or text in ("MISSING", "MISMATCH", "INCOMPLETE"):
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    program = payload.get("program")
    profiles = payload.get("profiles")
    if not isinstance(program, str) or not isinstance(profiles, str):
        return None
    return {
        "program": program,
        "profiles": profiles,
        "enabled": bool(payload.get("enabled", True)),
    }


def find_existing_agent_firewall_rule(python_path: str) -> dict | None:
    """Return firewall rule details when a matching inbound rule already exists."""
    if sys.platform != "win32":
        return None

    python_path = os.path.normpath(os.path.abspath(python_path))
    if not os.path.isfile(python_path):
        return None

    ps_python = _escape_ps_single_quoted(python_path)
    ps_name = _escape_ps_single_quoted(FIREWALL_RULE_DISPLAY_NAME)
    ps_script = f"""
    $ErrorActionPreference = 'Stop'
    $ruleName = '{ps_name}'
    $expectedPython = '{ps_python}'
    $rule = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $rule) {{
        Write-Output 'MISSING'
        exit 0
    }}
    $app = Get-NetFirewallApplicationFilter -AssociatedNetFirewallRule $rule -ErrorAction SilentlyContinue | Select-Object -First 1
    $port = Get-NetFirewallPortFilter -AssociatedNetFirewallRule $rule -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $app -or -not $port) {{
        Write-Output 'INCOMPLETE'
        exit 0
    }}
    $actualPython = [System.IO.Path]::GetFullPath($app.Program)
    $expectedNorm = [System.IO.Path]::GetFullPath($expectedPython)
    if ($actualPython -ne $expectedNorm) {{
        Write-Output 'MISMATCH'
        exit 0
    }}
    if ($port.Protocol -ne 'TCP') {{
        Write-Output 'MISMATCH'
        exit 0
    }}
    if ("$($port.LocalPort)" -ne '{AGENT_PORT}') {{
        Write-Output 'MISMATCH'
        exit 0
    }}
    @{{
        program = $actualPython
        profiles = @($rule.Profile) -join ', '
        enabled = [bool]$rule.Enabled
    }} | ConvertTo-Json -Compress
    """

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.SubprocessError, OSError):
        return None

    if result.returncode != 0:
        return None

    parsed = _parse_firewall_rule_query_output(result.stdout)
    if parsed is None:
        return None
    if _normalize_program_path(parsed["program"]) != _normalize_program_path(python_path):
        return None
    return parsed


def prompt_enable_legacy_direct_control() -> bool:
    """Ask whether to keep the legacy inbound TCP control path enabled."""
    print("\n🔁 Legacy direct control")
    print("\n   Current agents use outbound reverse TCP to the web panel (discovery on")
    print("   TCP 5000, native protocol on TCP 9998), so Windows Firewall does not need")
    print("   to expose inbound TCP 9999 by default.")
    print("\n   Enable the legacy inbound port only if you still use kid-pc-cli direct")
    print("   IP commands or need compatibility with an older parent web panel.")
    choice = input("\nEnable legacy inbound TCP 9999 control? (y/N): ").strip().lower()
    return choice in ("y", "yes")


def configure_agent_firewall(python_path: str) -> None:
    """Add the agent firewall rule, or reuse an existing matching rule."""
    existing = find_existing_agent_firewall_rule(python_path)
    if existing is not None:
        print("\n🔒 Reusing existing Windows Firewall rule")
        print(f"   Rule: {FIREWALL_RULE_DISPLAY_NAME}")
        print(f"   Program: {existing['program']}")
        print(f"   Profiles: {existing['profiles']}")
        return

    allow_public = prompt_allow_public_firewall()
    add_agent_firewall_rule(python_path, allow_public=allow_public)


def prompt_allow_public_firewall():
    """
    Ask whether inbound agent traffic should be allowed on Public (untrusted) networks.

    Default is no: only Private and Domain profiles (safer on coffee-shop Wi-Fi).
    """
    print("\n🔒 Windows Firewall — remote control port")
    print(f"\n   The agent listens on TCP {AGENT_PORT}. By default, the installer")
    print("   allows inbound connections only on Private and Domain networks")
    print("   (typical home Wi-Fi when Windows trusts the network).")
    print("\n   Public/untrusted networks are blocked so strangers on open Wi-Fi")
    print("   cannot reach the agent.")
    print("\n   Problem: A child can disconnect Wi-Fi and reconnect; Windows may")
    print("   then treat your home network as Public, and you lose remote control")
    print("   until you fix the network profile or re-run this installer.")
    print("\n   Desktop PCs rarely join random networks; laptops are the main case.")
    print("   You can allow Public networks below if you accept that trade-off.")
    choice = (
        input(f"\nAllow inbound TCP {AGENT_PORT} on Public networks too? (y/N): ").strip().lower()
    )
    return choice in ("y", "yes")


def add_agent_firewall_rule(python_path, *, allow_public=False):
    """
    Allow inbound TCP only on AGENT_PORT for the specific pythonw.exe used by the task.

    Restrictions: one program path, one local port, TCP only. Profile is Private+Domain
    by default; include Public when allow_public is True. Does not grant outbound or
    other ports for this executable.
    """
    python_path = os.path.normpath(os.path.abspath(python_path))
    if not os.path.isfile(python_path):
        print(f"\n⚠️  Firewall rule skipped: Python not found at {python_path}")
        return False

    ps_python = _escape_ps_single_quoted(python_path)
    ps_name = _escape_ps_single_quoted(FIREWALL_RULE_DISPLAY_NAME)
    ps_group = _escape_ps_single_quoted(FIREWALL_RULE_GROUP)
    if allow_public:
        firewall_profiles = "@('Private', 'Domain', 'Public')"
        profile_label = "Private, Domain, and Public profiles"
    else:
        firewall_profiles = "@('Private', 'Domain')"
        profile_label = "Private and Domain profiles"

    ps_script = f"""
    $ErrorActionPreference = 'Stop'
    $ruleName = '{ps_name}'
    $python = '{ps_python}'
    $existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
    if ($existing) {{
        $existing | Remove-NetFirewallRule -ErrorAction SilentlyContinue
    }}
    New-NetFirewallRule `
        -DisplayName $ruleName `
        -Group '{ps_group}' `
        -Description 'Kid PC Monitor agent: allow remote control only on TCP {AGENT_PORT} for the scheduled pythonw.exe' `
        -Direction Inbound `
        -Action Allow `
        -Enabled True `
        -Profile {firewall_profiles} `
        -Program $python `
        -Protocol TCP `
        -LocalPort {AGENT_PORT}
    Write-Host "SUCCESS: Firewall rule added for $python on TCP {AGENT_PORT} ({profile_label})"
    exit 0
    """

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
            capture_output=True,
            text=True,
        )
        print("\n=== Firewall ===")
        print(result.stdout.strip() or "(no output)")
        if result.stderr.strip():
            print(result.stderr.strip())
        if result.returncode == 0:
            return True
        print("\n⚠️  Could not add Windows Firewall rule (agent may prompt on first run).")
        return False
    except Exception as exc:
        print(f"\n⚠️  Firewall setup error: {exc}")
        return False


def remove_agent_firewall_rule():
    """Remove the inbound agent rule created by add_agent_firewall_rule."""
    ps_name = _escape_ps_single_quoted(FIREWALL_RULE_DISPLAY_NAME)
    ps_script = f"""
    $rules = Get-NetFirewallRule -DisplayName '{ps_name}' -ErrorAction SilentlyContinue
    if (-not $rules) {{
        Write-Host 'INFO: No firewall rule to remove'
        exit 0
    }}
    $rules | Remove-NetFirewallRule
    Write-Host 'SUCCESS: Firewall rule removed'
    exit 0
    """
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
            capture_output=True,
            text=True,
        )
        if result.stdout.strip():
            print(result.stdout.strip())
        return result.returncode == 0
    except Exception as exc:
        print(f"⚠️  Could not remove firewall rule: {exc}")
        return False


def remove_task():
    """Remove existing task"""
    task_name = TASK_NAME
    print(f"\n🗑️  Removing task '{task_name}'...")

    result = subprocess.run(
        f'schtasks /delete /tn "{task_name}" /f', shell=True, capture_output=True, text=True
    )

    if result.returncode == 0:
        print("✅ Task removed successfully!")
    else:
        print("ℹ️  Task not found or already removed.")

    print("\nRemoving Windows Firewall rule...")
    remove_agent_firewall_rule()


def run_install_flow():
    """Drive the install: gather the monitored account, paths, and create the task."""
    current_user = os.environ.get("USERNAME") or os.environ.get("USER") or ""

    existing_target = program_data_target_user()
    if existing_target:
        config_path = program_data_daily_path()
        print(f"\n👤 Previous install targeted '{existing_target}' ({config_path}).")

    target_user = prompt_target_user(current_user, default_user=existing_target)
    if not target_user:
        print("❌ No valid user provided. Aborting.")
        return False

    is_self = bool(current_user) and target_user.lower() == current_user.lower()

    python_path = find_system_python()
    if not python_path:
        if is_self:
            python_path = sys.executable.replace("python.exe", "pythonw.exe")
            print(
                f"\n🐍 No system-wide Python found; using the current interpreter "
                f"(self-install): {python_path}"
            )
        else:
            print("\n❌ No system-wide Python (pythonw.exe) was found.")
            print("   The scheduled task will run in the child's session, which cannot")
            print("   reach a Python install under your user profile.")
            print("\n   Fix: reinstall Python from https://www.python.org/downloads/")
            print("   and on the first screen choose 'Install for all users'.")
            return False
    else:
        print(f"\n🐍 Using system Python: {python_path}")

    source_package = find_repo_package_dir()
    if not source_package:
        print("\n❌ Could not locate the kid_pc_monitor package next to this installer.")
        print("   Expected at ../src/kid_pc_monitor relative to scripts/install.py.")
        return False

    print(f"\n📦 Installing agent to {INSTALL_DIR_DEFAULT} ...")
    script_path = install_to_programdata(target_user, source_package)
    print(f"   Granted {target_user} read+execute on the install directory.")

    profile_daily_path = (
        agent_state_dir_for_user(target_user, same_user=is_self) / INSTALL_CONFIG_FILE
    )
    existing_daily = find_complete_daily_settings(
        profile_path=profile_daily_path,
        program_data_path=program_data_daily_path(),
    )
    if existing_daily is not None:
        daily, source_path = existing_daily
        wake_time = _format_daily_time(daily.wake_time)
        bed_time = _format_daily_time(daily.bed_time)
        allowance = daily.allowance
        limit_label = f"{allowance} min/day"
        print(f"\n📋 Reusing daily schedule from {source_path}")
        print(f"   Wake-up: {wake_time} · Bedtime: {bed_time} · Allowance: {limit_label}")
    else:
        bed_time = prompt_bed_time()
        wake_time = prompt_wake_up_time()
        allowance = prompt_daily_allowance()
    daily_path, state_ok = write_agent_daily_settings_to_state(
        target_user,
        wake_time=wake_time,
        bed_time=bed_time,
        allowance=allowance,
        same_user=is_self,
    )
    if state_ok and daily_path is not None:
        limit_label = f"{allowance} min/day" if allowance is not None else "no daily cap"
        print(
            f"\n✅ Schedule saved to {daily_path}"
            f"\n   Wake-up: {wake_time} · Bedtime: {bed_time} · Allowance: {limit_label}"
        )

    prompt_and_store_shared_secret()
    enable_legacy_direct = prompt_enable_legacy_direct_control()

    if create_task_with_power_settings(target_user, script_path, python_path, is_self=is_self):
        if enable_legacy_direct:
            configure_agent_firewall(python_path)
        else:
            print("\n🔒 Skipped inbound TCP 9999 firewall rule (reverse TCP is the default).")
        print("\n✅ Setup complete! Task will run even on laptops using battery.")
        return True

    print("\nTrying alternative method...")
    if create_task_simple_schtasks(target_user, script_path, python_path):
        if enable_legacy_direct:
            configure_agent_firewall(python_path)
        else:
            print("\n🔒 Skipped inbound TCP 9999 firewall rule (reverse TCP is the default).")
        print("\n✅ Setup complete using XML method!")
        return True

    print("\n❌ Could not create task. Please check the error messages above.")
    return False


AUTO_UPDATE_TASK_NAME = "KidPCMonitorUpdate"


def repo_root_dir():
    """Repo root (the parent of this scripts/ directory)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def git_pull_repo():
    """Best-effort fast-forward `git pull` in the repo. Returns True on success."""
    repo = repo_root_dir()
    try:
        result = subprocess.run(
            ["git", "-C", repo, "pull", "--ff-only"],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"⚠️  Could not run 'git pull' ({exc}); redeploying the current files.")
        return False
    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    if result.returncode == 0:
        print(f"✅ git pull: {out or 'already up to date'}")
        return True
    print(f"⚠️  'git pull' failed: {err or out}; redeploying the current files.")
    return False


def redeploy_agent():
    """Re-copy the agent package to ProgramData for the previously-installed user."""
    target_user = program_data_target_user()
    if not target_user:
        print("❌ No previous install found (no monitored user recorded in ProgramData).")
        print("   Run a full install first:  python scripts\\install.py")
        return False
    source_package = find_repo_package_dir()
    if not source_package:
        print("❌ Could not locate the kid_pc_monitor package next to this installer.")
        return False

    # Stop the running agent so its files are not in use during the copy.
    subprocess.run(f'schtasks /end /tn "{TASK_NAME}"', shell=True, capture_output=True, text=True)
    print(f"📦 Updating agent files in {INSTALL_DIR_DEFAULT} for '{target_user}' ...")
    install_to_programdata(target_user, source_package)
    print("✅ Agent files updated.")

    started = subprocess.run(
        f'schtasks /run /tn "{TASK_NAME}"', shell=True, capture_output=True, text=True
    )
    if started.returncode == 0:
        print(f"🔄 Restarted the {TASK_NAME} agent with the new code.")
    else:
        print(
            "ℹ️  Could not auto-start the agent now (normal if the kid isn't logged in);\n"
            "   the new code runs at their next logon."
        )
    return True


def enable_auto_update():
    """Install a SYSTEM scheduled task that pulls + redeploys the agent automatically."""
    python_path = find_system_python() or sys.executable
    repo = repo_root_dir()
    script = os.path.join(repo, "scripts", "install.py")

    # The task runs as SYSTEM, so Git would otherwise refuse to touch a repo owned
    # by a normal user ("detected dubious ownership"). Trust it system-wide.
    try:
        subprocess.run(
            ["git", "config", "--system", "--add", "safe.directory", repo],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        pass  # git not found here; the manual updater still works
    ps_script = f"""
    $ErrorActionPreference = 'Stop'
    try {{
        $action = New-ScheduledTaskAction -Execute "{python_path}" `
            -Argument '"{script}" --update --pull' -WorkingDirectory "{repo}"
        $atStartup = New-ScheduledTaskTrigger -AtStartup
        $atStartup.Delay = 'PT2M'
        $daily = New-ScheduledTaskTrigger -Daily -At 4am
        $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
        $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        Register-ScheduledTask -TaskName '{AUTO_UPDATE_TASK_NAME}' -Action $action `
            -Trigger @($atStartup, $daily) -Principal $principal -Settings $settings -Force | Out-Null
        Write-Host 'SUCCESS'
    }} catch {{
        Write-Host "ERROR: $_"
        exit 1
    }}
    """
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and "SUCCESS" in (result.stdout or ""):
        print(f"✅ Automatic updates enabled (scheduled task '{AUTO_UPDATE_TASK_NAME}').")
        print("   The PC pulls the latest code ~2 min after startup and daily at 4am,")
        print("   then the agent runs it at the kid's next logon.")
        print("   Requires Git installed for all users (on the system PATH).")
        return True
    print("❌ Could not enable automatic updates.")
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip())
    return False


def disable_auto_update():
    result = subprocess.run(
        f'schtasks /delete /tn "{AUTO_UPDATE_TASK_NAME}" /f',
        shell=True,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print(f"✅ Automatic updates disabled ('{AUTO_UPDATE_TASK_NAME}' removed).")
    else:
        print("ℹ️  No automatic-update task was found.")
    return True


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Kid PC Monitor agent installer / updater.",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Re-deploy the agent from the current repo checkout (non-interactive).",
    )
    parser.add_argument(
        "--pull",
        action="store_true",
        help="With --update: run 'git pull' first.",
    )
    parser.add_argument(
        "--enable-auto-update",
        action="store_true",
        help="Install a scheduled task that pulls + redeploys the agent automatically.",
    )
    parser.add_argument(
        "--disable-auto-update",
        action="store_true",
        help="Remove the automatic-update scheduled task.",
    )
    args = parser.parse_args()

    non_interactive = (
        args.update or args.enable_auto_update or args.disable_auto_update
    )

    print("Kid PC Monitor - Task Scheduler Setup")
    print("=" * 45)

    if not check_admin():
        print("\n❌ This script needs to run as Administrator!")
        print("   Please right-click and select 'Run as administrator'")
        if not non_interactive:
            input("\nPress Enter to exit...")
        sys.exit(1)

    if args.disable_auto_update:
        sys.exit(0 if disable_auto_update() else 1)
    if args.enable_auto_update:
        sys.exit(0 if enable_auto_update() else 1)
    if args.update:
        if args.pull:
            git_pull_repo()
        sys.exit(0 if redeploy_agent() else 1)

    print("\nWhat would you like to do?")
    print("1. Create/Update scheduled task")
    print("2. Remove scheduled task")
    print("3. Exit")

    choice = input("\nChoice (1-3): ").strip()

    if choice == "1":
        print("\nCreating scheduled task with battery-friendly settings...\n")
        run_install_flow()
    elif choice == "2":
        remove_task()
    else:
        print("\nExiting...")

    input("\nPress Enter to close...")
